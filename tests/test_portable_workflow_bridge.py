"""Synthetic offline acceptance for the portable first-party bridge subset."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sqlite3
import sys
import types

import pytest

from mf_phase12.db import Phase12Database
from mf_phase12.hash_utils import stable_hash, accounting_run_hash
from mf_phase12.office_profile import (
    OfficeProfile, OfficeIdentityBinding, using_office_profile,
    bind_database_office_identity, normalize_office_name, get_current_profile,
)
from mf_phase12.workflow_bridge import WorkflowBridge


@pytest.fixture
def companies(tmp_path):
    profiles = []
    for slug in ('alpha', 'beta'):
        p = OfficeProfile(schema_version=2, configured=True, slug=slug,
            legal_name='Synthetic ' + slug, display_name=slug,
            expected_office_names=('Synthetic ' + slug,),
            google_account=slug + '@tenant.test', google_drive_account=slug + '@tenant.test',
            moneyforward_keychain_service='synthetic-' + slug,
            moneyforward_db_path=tmp_path / 'data/phase12/offices' / slug / 'ledger.sqlite',
            repository_root=tmp_path, google_drive_evidence_root_id=slug + '_root_folder',
            google_drive_evidence_source_folder_ids=(slug + '_root_folder',),
            google_drive_processed_folder_id=slug + '_processed_folder',
            google_drive_temporary_ocr_folder_id=slug + '_temporary_folder')
        with using_office_profile(p):
            db = Phase12Database(p.moneyforward_db_path)
            db.initialize()
            with db.connect() as conn:
                bind_database_office_identity(conn, p, OfficeIdentityBinding(
                    profile_slug=p.slug, profile_hash=p.profile_hash, office_id='synthetic-' + slug,
                    office_id_hash=stable_hash('synthetic-' + slug),
                    legal_name_hash=stable_hash(normalize_office_name(p.legal_name)),
                    keychain_service_hash=stable_hash(p.moneyforward_keychain_service),
                    boundary_fingerprint=p.boundary_fingerprint))
        profiles.append(p)
    return profiles


def begin(bridge):
    return bridge.run_begin('Synthetic inspection', '2026-11-01',
                            {'selected_workflows': ['sales_invoice']}, request_id='synthetic-request')['run_id']


@pytest.mark.parametrize('selection', [None, [], ['card', 'sales_invoice'], ['card', 'card'],
                                        ['payroll_bonus'], ['tax_insurance']])
def test_scope_rejected_before_run_creation(companies, selection):
    bridge = WorkflowBridge(companies[0])
    with pytest.raises(ValueError):
        bridge.run_begin('Synthetic', '2026-11-01', {'selected_workflows': selection})
    with sqlite3.connect(companies[0].moneyforward_db_path) as conn:
        assert conn.execute('SELECT count(*) FROM accounting_runs').fetchone()[0] == 0
    with pytest.raises(ValueError):
        bridge.run_begin('Synthetic', '2026-11-01', {}, 'PAYROLL')


def test_restart_two_companies_and_finished_decision_reuse(companies, monkeypatch):
    # No provider client is constructed, including when resuming.
    monkeypatch.setattr('mf_phase12.office_connection_probe.bind_company_accounting_client_from_keychain',
                        lambda **kw: pytest.fail('provider access'))
    identity, facts = stable_hash('document'), stable_hash('facts')
    for profile in companies:
        bridge = WorkflowBridge(profile)
        run = begin(bridge)
        bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], stable_hash('synthetic-source-version'))
        result = bridge.run_record_item(run, 'sales_invoice', identity, 'NOT_APPLICABLE',
                                        {'basis': 'synthetic exclusion'}, facts)
        assert result['status'] == 'recorded'
        recreated = WorkflowBridge(profile)
        replay = recreated.run_record_item(run, 'sales_invoice', identity, 'NOT_APPLICABLE',
                                           {'basis': 'synthetic exclusion'}, facts)
        assert replay['status'] == 'already_recorded'
        resumed = recreated.run_resume(run)
        assert resumed['resume_workflows'] == []
        assert resumed['accounting_completed'] is False
        assert resumed['external_operations_executed'] == 0
        assert resumed['production_ready'] is False
        assert recreated.run_status(run)['workflows'][0]['items'][0]['state'] == 'NOT_APPLICABLE'
        assert get_current_profile() is None
        other = next(p for p in companies if p != profile)
        with pytest.raises(ValueError):
            WorkflowBridge(other).run_resume(run)


def test_changed_snapshot_invalidates_finished_steps(companies):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    identity = stable_hash('source')
    bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], stable_hash('coverage-one'))
    bridge.run_record_item(run, 'sales_invoice', identity, 'REVIEW', {'basis': 'missing'}, stable_hash('facts'))
    assert bridge.run_resume(run)['resume_workflows'] == []
    before = bridge.run_status(run)['artifact_hash']
    assert bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], stable_hash('coverage-one'))['reused']
    assert bridge.run_status(run)['artifact_hash'] == before
    bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], stable_hash('coverage-two'))
    resumed = WorkflowBridge(companies[0]).run_resume(run)
    assert resumed['resume_workflows'][0]['items'] == [{'item_hash': identity, 'state': 'pending'}]
    bridge.run_record_source_snapshot(run, 'sales_invoice', [], stable_hash('coverage-three'))
    assert bridge.run_status(run)['workflows'][0]['items'] == []
    assert not bridge.run_status(run)['accounting_completed']


@pytest.mark.parametrize('state', ['REVIEW', 'NOT_READY', 'NOT_APPLICABLE'])
def test_exceptions_are_not_accounting_completion(companies, state):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    identity, facts = stable_hash('document'), stable_hash('facts')
    bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], stable_hash('synthetic-source-version'))
    bridge.run_record_item(run, 'sales_invoice', identity, state, {'basis': 'synthetic'}, facts)
    assert bridge.run_status(run)['status'] == 'incomplete'
    with pytest.raises(ValueError):
        bridge.run_record_item(run, 'sales_invoice', identity, 'VERIFIED', {'official': True}, facts)
    with sqlite3.connect(companies[0].moneyforward_db_path) as conn:
        assert conn.execute('SELECT status FROM accounting_run_items').fetchone()[0] == 'planned'
        assert conn.execute('SELECT count(*) FROM accounting_run_receipts').fetchone()[0] == 0


def test_db_binding_and_missing_db_rejected(companies):
    profile = companies[0]
    missing = replace(profile, moneyforward_db_path=profile.moneyforward_db_path.parent / 'missing.sqlite')
    with pytest.raises(sqlite3.OperationalError):
        begin(WorkflowBridge(missing))
    assert not missing.moneyforward_db_path.exists()
    with pytest.raises(PermissionError):
        begin(WorkflowBridge(replace(profile, google_account='different@tenant.test')))
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        conn.execute("UPDATE office_identity_bindings SET profile_slug='beta'")
    with pytest.raises(PermissionError):
        begin(WorkflowBridge(profile))


def test_unknown_effect_blocks_resume_and_changes(companies):
    profile = companies[0]
    bridge = WorkflowBridge(profile)
    run = begin(bridge)
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        conn.execute("UPDATE accounting_runs SET status='uncertain_external_write' WHERE run_id_hash=?",
                     (accounting_run_hash(run),))
    result = WorkflowBridge(profile).run_resume(run)
    assert result['official_reconciliation_required']
    assert result['resume_workflows'] == []
    assert result['external_operations_executed'] == 0
    with pytest.raises(ValueError):
        bridge.run_record_source_snapshot(run, 'sales_invoice', [], stable_hash('synthetic-source-version'))


def test_scope_and_artifact_binding_fail_closed(companies):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    with pytest.raises(ValueError):
        bridge.run_record_source_snapshot(run, 'card', [], stable_hash('synthetic-source-version'))
    with using_office_profile(companies[0]):
        path = bridge._path(run)
    path.write_text('{}')
    resumed = WorkflowBridge(companies[0]).run_resume(run)
    assert resumed['external_operations_executed'] == 0
    assert resumed['local_state_cache'] == 'verified'
    assert path.read_text() != '{}'


def test_interrupted_artifact_commit_fails_closed(companies, monkeypatch):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    original = bridge._save
    def crash(*args):
        original(*args)
        raise RuntimeError('synthetic interruption before DB commit')
    monkeypatch.setattr(bridge, '_save', crash)
    with pytest.raises(RuntimeError):
        bridge.run_record_source_snapshot(run, 'sales_invoice', [stable_hash('new')], stable_hash('synthetic-source-version'))
    # The failed transaction never published its new cache or state.
    resumed = WorkflowBridge(companies[0]).run_resume(run)
    assert resumed['workflows'][0]['source_discovery_complete'] is False
    assert resumed['external_operations_executed'] == 0


def test_mcp_registration_contract_only(monkeypatch, companies):
    # Stub optional MCP package: this checks declarations, NOT real MCP runtime.
    class Annotations:
        def __init__(self, **kw): self.__dict__.update(kw)
    class StubToolError(Exception):
        pass
    class Server:
        def __init__(self, *a, **kw): self.registry = {}
        def tool(self, annotations):
            def register(fn):
                self.registry[fn.__name__] = (fn, annotations)
                return fn
            return register
    for name, attrs in [('mcp', {}), ('mcp.server', {'MCPServer': Server}),
                        ('mcp.types', {'ToolAnnotations': Annotations}),
                        ('mcp.server.mcpserver.exceptions', {'ToolError': StubToolError})]:
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / 'mcp_servers/company_accounting_server.py'
    spec = importlib.util.spec_from_file_location('synthetic_company_server', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('run_begin', 'run_status', 'run_resume', 'run_record_source_snapshot', 'run_record_item'):
        assert module.mcp.registry[name][1].read_only_hint is False
    assert 'final_google_chat_send' not in module.mcp.registry
    assert 'cross_audit' not in module.mcp.registry
    assert module.mcp.registry['get_journal'][1].read_only_hint is True
    with pytest.raises(StubToolError, match='disabled'):
        module.run_resume('synthetic')

    monkeypatch.setattr(module, '_load_profile', lambda: companies[0])
    monkeypatch.setenv('MF_CLOUD_LOCAL_WORKFLOW_ENABLED', '1')
    run = module.run_begin('Synthetic', '2026-11-01', {'selected_workflows': ['card']}, request_id='synthetic-request')
    resumed = module.run_resume(run['run_id'])
    assert resumed['profile_slug'] == 'alpha'
    assert resumed['external_operations_executed'] == 0
    assert resumed['accounting_completed'] is False


def test_committed_state_recovers_missing_cache_without_duplicate_db_events(companies, monkeypatch):
    profile = companies[0]
    bridge = WorkflowBridge(profile)
    monkeypatch.setattr(bridge, '_write_cache', lambda *a: (_ for _ in ()).throw(OSError('synthetic cache failure')))
    created = bridge.run_begin('Synthetic', '2026-11-01', {'selected_workflows': ['card']}, request_id='synthetic-request')
    assert created['local_state_cache'] == 'repair_pending'
    run = created['run_id']
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        before = conn.execute("SELECT count(*) FROM audit_logs WHERE event_type='portable_workflow_state'").fetchone()[0]
    restored = WorkflowBridge(profile)
    resumed = restored.run_resume(run)
    assert resumed['local_state_cache'] == 'verified'
    assert resumed['external_operations_executed'] == 0
    with using_office_profile(profile):
        assert restored._path(run).is_file()
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_logs WHERE event_type='portable_workflow_state'").fetchone()[0] == before


def test_durable_db_tampering_still_fails_closed(companies):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    with sqlite3.connect(companies[0].moneyforward_db_path) as conn:
        conn.execute("UPDATE audit_logs SET rollback_plan='{}' WHERE event_type='portable_workflow_state'")
    with pytest.raises(ValueError, match='integrity'):
        WorkflowBridge(companies[0]).run_resume(run)


def test_actual_backend_response_passes_existing_scope_verifier(companies):
    from mf_phase12.chatgpt_cloud_contract import verify_run_scope
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    response = bridge.run_status(run)
    scope = verify_run_scope('alpha', 'DAILY', ['sales_invoice'], response)
    assert scope['business_write_authorized'] is False
    assert scope['office_and_auth_verified'] is False
    response['workflows'].append(dict(response['workflows'][0]))
    with pytest.raises(PermissionError):
        verify_run_scope('alpha', 'DAILY', ['sales_invoice'], response)


@pytest.fixture
def real_mcp_server():
    # Optional cloud-extra acceptance: the real SDK, not the earlier stub.
    pytest.importorskip("mcp", reason="install .[cloud] for real SDK dispatch tests")
    path = Path(__file__).resolve().parents[1] / 'mcp_servers/company_accounting_server.py'
    spec = importlib.util.spec_from_file_location('real_sdk_company_server', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_sdk_default_opt_in_and_provider_write_boundaries(real_mcp_server, monkeypatch):
    import asyncio
    server = real_mcp_server
    monkeypatch.delenv('MF_CLOUD_LOCAL_WORKFLOW_ENABLED', raising=False)
    monkeypatch.setattr(server, '_load_profile', lambda: pytest.fail('disabled mode must not load profile'))
    async def scenario():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert not {'create_journal', 'delete_journal', 'final_google_chat_send', 'cross_audit'} & tools.keys()
        assert tools['get_journal'].annotations.read_only_hint is True
        assert tools['run_begin'].annotations.read_only_hint is False
        with pytest.raises(Exception, match='disabled'):
            await server.mcp.call_tool('run_resume', {'run_id': 'synthetic'})
    asyncio.run(scenario())


def test_real_sdk_restart_dispatch_schema_and_source_scope(real_mcp_server, companies, monkeypatch):
    import asyncio
    server = real_mcp_server
    monkeypatch.setenv('MF_CLOUD_LOCAL_WORKFLOW_ENABLED', '1')
    monkeypatch.setattr(server, '_load_profile', lambda: companies[0])
    monkeypatch.setattr('mf_phase12.office_connection_probe.bind_company_accounting_client_from_keychain',
                        lambda **kw: pytest.fail('local SDK dispatch must not access provider'))
    def data(result):
        import json
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)
    async def scenario():
        created = data(await server.mcp.call_tool('run_begin', {
            'instruction': 'Synthetic SDK test', 'accounting_date': '2026-11-01',
            'source_scope': {'selected_workflows': ['card']}, 'request_id': 'synthetic-sdk-request', 'mode': 'DAILY'}))
        run = created['run_id']
        assert created['accounting_completed'] is False
        identity = stable_hash('synthetic-sdk-document')
        await server.mcp.call_tool('run_record_source_snapshot', {
            'run_id': run, 'workflow': 'card', 'source_identity_hashes': [identity], 'coverage_hash': stable_hash('sdk-source-version')})
        resumed = data(await server.mcp.call_tool('run_resume', {'run_id': run}))
        assert resumed['run_id'] == run
        assert resumed['resume_workflows'][0]['items'][0]['item_hash'] == identity
        assert resumed['external_operations_executed'] == 0
        with pytest.raises(Exception):
            await server.mcp.call_tool('run_begin', {'instruction': 'missing required arguments'})
        with pytest.raises(Exception, match='outside'):
            await server.mcp.call_tool('run_record_source_snapshot', {
                'run_id': run, 'workflow': 'sales_invoice', 'source_identity_hashes': [], 'coverage_hash': stable_hash('sdk-source-version')})
        with pytest.raises(Exception, match='VERIFIED'):
            await server.mcp.call_tool('run_record_item', {
                'run_id': run, 'workflow': 'card', 'source_identity_hash': identity,
                'state': 'VERIFIED', 'decision': {'synthetic': True},
                'current_facts_hash': stable_hash('synthetic-facts')})
    asyncio.run(scenario())


def test_lost_begin_response_reuses_same_run_after_restart(companies):
    profile = companies[0]
    args = ('Synthetic retained request', '2026-11-01', {'selected_workflows': ['card']})
    # Deliberately discard the first successful response (client connection lost).
    WorkflowBridge(profile).run_begin(*args, request_id='request-retained-before-send')
    restored = WorkflowBridge(profile)
    recovered = restored.run_begin(*args, request_id='request-retained-before-send')
    assert recovered['already_started'] is True
    assert recovered['accounting_completed'] is False
    assert restored.run_resume(recovered['run_id'])['external_operations_executed'] == 0
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        assert conn.execute('SELECT count(*) FROM accounting_runs').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_logs WHERE event_type='portable_workflow_state'").fetchone()[0] == 1
    with pytest.raises(ValueError, match='immutable'):
        restored.run_begin('Changed purpose', args[1], args[2], request_id='request-retained-before-send')


def test_begin_requires_retained_token_and_rejects_unimplemented_narrow_scope(companies):
    bridge = WorkflowBridge(companies[0])
    with pytest.raises(ValueError, match='request_id'):
        bridge.run_begin('Synthetic', '2026-11-01', {'selected_workflows': ['card']})
    with pytest.raises(ValueError, match='unsupported source scope'):
        bridge.run_begin('Synthetic', '2026-11-01',
            {'selected_workflows': ['card'], 'account': 'synthetic-account'}, request_id='synthetic')


def test_explicit_facts_version_required_for_source_snapshot(companies):
    bridge = WorkflowBridge(companies[0])
    run = begin(bridge)
    with pytest.raises(ValueError, match='SHA-256'):
        bridge.run_record_source_snapshot(run, 'sales_invoice', [stable_hash('source')], '')
    assert bridge.run_status(run)['workflows'][0]['source_discovery_complete'] is False


def test_real_sdk_lock_contention_preserves_actionable_error(real_mcp_server, companies, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    server = real_mcp_server
    monkeypatch.setenv('MF_CLOUD_LOCAL_WORKFLOW_ENABLED', '1')
    monkeypatch.setattr(server, '_load_profile', lambda: companies[0])
    @contextmanager
    def busy():
        raise RuntimeError('another production aggregate run is executing for this office')
        yield
    monkeypatch.setattr('mf_phase12.cli._aggregate_production_execution_lock', busy)
    async def scenario():
        with pytest.raises(Exception, match='company_run_busy'):
            await server.mcp.call_tool('run_begin', {
                'instruction': 'Synthetic', 'accounting_date': '2026-11-01',
                'source_scope': {'selected_workflows': ['card']}, 'request_id': 'synthetic-request'})
    asyncio.run(scenario())


def test_process_exit_after_commit_before_reply_recovers_same_run(companies):
    import json
    import os
    import subprocess
    profile = companies[0]
    keys = ('schema_version', 'configured', 'slug', 'legal_name', 'display_name',
            'expected_office_names', 'google_account', 'google_drive_account',
            'moneyforward_keychain_service', 'google_drive_evidence_root_id',
            'google_drive_evidence_source_folder_ids', 'google_drive_processed_folder_id',
            'google_drive_temporary_ocr_folder_id')
    settings = {key: getattr(profile, key) for key in keys}
    settings['moneyforward_db_path'] = str(profile.moneyforward_db_path)
    settings['repository_root'] = str(profile.repository_root)
    worker = """
import json, os, sys
from pathlib import Path
from mf_phase12.office_profile import OfficeProfile
from mf_phase12.workflow_bridge import WorkflowBridge
settings=json.loads(sys.stdin.read())
for field in ('moneyforward_db_path', 'repository_root'):
    settings[field]=Path(settings[field])
bridge=WorkflowBridge(OfficeProfile(**settings))
created=bridge.run_begin('Synthetic process recovery', '2026-11-01',
    {'selected_workflows':['card']}, request_id='persisted-client-request')
if sys.argv[1]=='lose-reply':
    os._exit(17)
resumed=bridge.run_resume(created['run_id'])
print(json.dumps({'already_started':created['already_started'],
                 'run_id':created['run_id'],
                 'external_operations_executed':resumed['external_operations_executed']}))
"""
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    lost = subprocess.run([sys.executable, '-c', worker, 'lose-reply'],
        input=json.dumps(settings), text=True, capture_output=True, env=env, timeout=20)
    assert lost.returncode == 17
    assert lost.stdout == ''
    recovered = subprocess.run([sys.executable, '-c', worker, 'recover'],
        input=json.dumps(settings), text=True, capture_output=True, env=env, timeout=20, check=True)
    facts = json.loads(recovered.stdout)
    assert facts['already_started'] is True
    assert facts['external_operations_executed'] == 0
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        assert conn.execute('SELECT count(*) FROM accounting_runs').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_logs WHERE event_type='portable_workflow_state'").fetchone()[0] == 1
