import json
import sqlite3
from dataclasses import replace

import pytest

from mf_phase12.accounting_api import MoneyForwardAccountingClient, WriteExecutionGrant
from mf_phase12.db import Phase12Database
from mf_phase12.office_profile import OfficeProfile, bind_database_office_identity, using_office_profile
from mf_phase12.rag import search_rag


def company(root, slug):
    return OfficeProfile(schema_version=2, slug=slug, legal_name=f'Tenant {slug}',
        display_name=slug, expected_office_names=(f'Tenant {slug}',),
        google_account=f'{slug}@tenant.test', moneyforward_keychain_service=f'mf-{slug}',
        moneyforward_db_path=root / 'data/phase12/offices' / slug / 'ledger.sqlite',
        google_drive_account='shared@storage.test',
        google_drive_evidence_root_id=f'{slug}_evidence_root',
        google_drive_evidence_source_folder_ids=(f'{slug}_evidence_root',),
        google_drive_processed_folder_id=f'{slug}_processed_folder',
        repository_root=root, configured=True)


class Api:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        token = kwargs['headers']['Authorization'].split()[-1]
        if url.endswith('/offices'):
            payload = {'data': [{'id': f'{token}-office', 'name': f'Tenant {token}'}]}
        else:
            payload = {'id': 'synthetic-journal', 'data': []}
        return 200, {}, json.dumps(payload).encode()


def bound(profile, api):
    client = MoneyForwardAccountingClient(profile.slug, office_profile=profile,
        keychain_service=profile.moneyforward_keychain_service, transport=api)
    with using_office_profile(profile):
        db = Phase12Database(profile.moneyforward_db_path)
        db.initialize()
        with db.connect() as conn:
            bind_database_office_identity(conn, profile, client.validate_office_identity())
    return client, db


def test_official_read_and_wrong_token_after_cached_validation(tmp_path):
    api = Api()
    client, _ = bound(company(tmp_path, 'alpha'), api)
    assert client.accounts()['data'] == []
    client.access_token = 'beta'
    count = len(api.calls)
    with pytest.raises(PermissionError):
        client.accounts()
    assert all(url.endswith('/offices') for _, url in api.calls[count:])


@pytest.mark.parametrize('foreign_token', [False, True])
def test_voucher_readback_without_optional_office_still_validates_identity(tmp_path, foreign_token):
    from mf_phase12.hash_utils import stable_hash
    from mf_phase12.voucher_execute import verify_voucher_attach_read_back

    class VoucherApi(Api):
        def request(self, method, url, **kwargs):
            if '/journals/' in url:
                self.calls.append((method, url))
                return 200, {}, b'{"id":"synthetic-journal","voucher_file_ids":["synthetic-voucher"]}'
            return super().request(method, url, **kwargs)

    api = VoucherApi()
    client, db = bound(company(tmp_path, 'alpha'), api)
    before = db.path.read_bytes()
    if foreign_token:
        client.access_token = 'beta'
    arguments = dict(client=client, journal_id='synthetic-journal',
        expected_voucher_file_ids=['synthetic-voucher'],
        post_result={'voucher_file_ids_hash': stable_hash(['synthetic-voucher'])})
    if foreign_token:
        with pytest.raises(PermissionError):
            verify_voucher_attach_read_back(**arguments)
        assert all(url.endswith('/offices') for _, url in api.calls)
    else:
        assert verify_voucher_attach_read_back(**arguments).verified
        assert sum('/journals/' in url for _, url in api.calls) == 1
    assert db.path.read_bytes() == before


@pytest.mark.parametrize('mismatch', ['office', 'keychain', 'db', 'profile'])
def test_wrong_combinations_never_read_business_data(tmp_path, mismatch):
    api = Api()
    alpha, beta = company(tmp_path, 'alpha'), company(tmp_path, 'beta')
    client, _ = bound(alpha, api)
    _, beta_db = bound(beta, api)
    kwargs = {}
    if mismatch == 'office':
        kwargs['office_id'] = 'beta-office'
    elif mismatch == 'keychain':
        client.keychain_service = beta.moneyforward_keychain_service
    elif mismatch == 'db':
        client.db_path = beta_db.path
    else:
        client.office_profile = beta
    count = len(api.calls)
    with pytest.raises((PermissionError, ValueError)):
        client.accounts(**kwargs)
    assert all(url.endswith('/offices') for _, url in api.calls[count:])


def test_custom_transport_cannot_bypass_profile(tmp_path):
    api = Api()
    with pytest.raises(PermissionError):
        MoneyForwardAccountingClient('alpha', transport=api).offices()
    assert api.calls == []


def test_request_metadata_is_local_but_does_not_authorize_business_io():
    api = Api()
    client = MoneyForwardAccountingClient('metadata-only', transport=api)
    journal = {'transaction_date': '2026-01-01', 'journal_type': 'journal_entry',
        'branches': [{'debitor': {'account_id': 'debit', 'value': 100},
                      'creditor': {'account_id': 'credit', 'value': 100}}]}
    metadata = client.create_journal_request_metadata(journal=journal, office_id='synthetic-office')
    assert metadata['request_shape']['path'] == '/api/v3/journals'
    assert api.calls == []
    with pytest.raises(PermissionError):
        client.accounts(office_id='synthetic-office')
    assert api.calls == []


@pytest.mark.parametrize('route', ['/api/v3/offices/alpha-office/../beta-office/accounts', '/api/v3/offices/%2e%2e/beta-office/accounts', '/api/v3/accounts#fragment'])
def test_direct_transport_rejects_noncanonical_office_route(tmp_path, route):
    api = Api()
    client, _ = bound(company(tmp_path, 'alpha'), api)
    before = len(api.calls)
    with pytest.raises(PermissionError):
        client.transport.request('GET', client.base_url + route,
            headers={'Authorization': 'Bearer alpha'})
    assert len(api.calls) == before


def test_direct_transport_rejects_duplicate_authorization(tmp_path):
    api = Api()
    client, _ = bound(company(tmp_path, 'alpha'), api)
    before = len(api.calls)
    with pytest.raises(PermissionError):
        client.transport.request('GET', client.base_url + '/api/v3/accounts',
            headers={'Authorization': 'Bearer alpha', 'authorization': 'Bearer beta'})
    assert len(api.calls) == before


def test_write_default_is_read_only_even_with_grant(tmp_path):
    api = Api()
    client, _ = bound(company(tmp_path, 'alpha'), api)
    count = len(api.calls)
    grant = WriteExecutionGrant('journal_create', 'approved', 'scope', 'target')
    journal = {'transaction_date': '2026-01-01', 'journal_type': 'journal_entry',
        'branches': [{'debitor': {'account_id': 'debit', 'value': 100},
                      'creditor': {'account_id': 'credit', 'value': 100}}]}
    with pytest.raises(PermissionError):
        client.create_journal(journal=journal, grant=grant)
    assert all(method == 'GET' for method, _ in api.calls[count:])


def test_rag_read_rejects_other_company_database(tmp_path):
    alpha, beta = company(tmp_path, 'alpha'), company(tmp_path, 'beta')
    _, alpha_db = bound(alpha, Api())
    with alpha_db.connect() as conn, using_office_profile(beta):
        with pytest.raises(PermissionError):
            search_rag(conn, 'invoice')


def test_explicit_sqlite_path_is_not_redirected_by_ambient_postgres(tmp_path, monkeypatch):
    monkeypatch.setenv('MF_PHASE12_DATABASE_URL', 'postgresql://forbidden')
    db = Phase12Database(tmp_path / 'disposable.sqlite')
    assert not db.is_postgres


def test_new_company_read_only_sync_builds_only_own_memory(tmp_path):
    from mf_phase12.office_rag_pipeline import build_office_rag
    profile = company(tmp_path, 'gamma')
    api = Api()
    client = MoneyForwardAccountingClient('gamma', office_profile=profile,
        keychain_service=profile.moneyforward_keychain_service, transport=api)
    with using_office_profile(profile):
        result = build_office_rag(db_path=profile.moneyforward_db_path, client=client,
            start_date='2026-01-01', end_date='2026-01-31')
    assert result.sync_executed
    assert all(method == 'GET' for method, _ in api.calls)
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        assert conn.execute('SELECT profile_slug FROM office_identity_bindings').fetchall() == [('gamma',)]
        assert conn.execute('SELECT COUNT(*) FROM sync_manifests').fetchone()[0] > 0


def test_rag_pipeline_wrong_client_does_not_create_database(tmp_path):
    from mf_phase12.office_rag_pipeline import build_office_rag
    profile = company(tmp_path, 'gamma')
    client = MoneyForwardAccountingClient('beta', office_profile=company(tmp_path, 'beta'),
        keychain_service='mf-beta', transport=Api())
    with using_office_profile(profile), pytest.raises(PermissionError):
        build_office_rag(db_path=profile.moneyforward_db_path, client=client)
    assert not profile.moneyforward_db_path.exists()


def test_readiness_checks_do_not_initialize_or_modify_database(tmp_path, monkeypatch):
    from mf_phase12.product_readiness import build_product_readiness_status
    from mf_phase12.classification_scope import build_classification_scope_status
    from mf_phase12.same_chain_status import build_same_chain_status
    profile = company(tmp_path, 'gamma')
    _, db = bound(profile, Api())
    before = db.path.read_bytes()
    def no_initialization(*args, **kwargs):
        pytest.fail('A status check must not initialize or migrate the DB')
    monkeypatch.setattr(Phase12Database, 'initialize', no_initialization)
    with using_office_profile(profile):
        assert not build_product_readiness_status(db.path)['phase12_product_ready']
        assert build_classification_scope_status(db.path).status == 'not_ready'
        assert build_same_chain_status(db.path).to_redacted_dict()['status'] == 'needs_action'
    assert db.path.read_bytes() == before


def test_recorded_readiness_preserves_external_gates_and_database(tmp_path, monkeypatch):
    from mf_phase12.starter_readiness import inspect_readiness
    profile = company(tmp_path, 'gamma')
    _, db = bound(profile, Api())
    before = db.path.read_bytes()
    def refuse(*args, **kwargs):
        pytest.fail('Readiness may not initialize a database')
    monkeypatch.setattr(Phase12Database, 'initialize', refuse)
    with using_office_profile(profile):
        result = inspect_readiness(db.path)
    assert not result['recorded_gates_passed']
    assert not result['live_authentication_rechecked']
    assert 'same_company_production_run_complete' in result['failed_checks']
    assert 'recorded_accounting_write_scopes' in result['failed_checks']
    assert db.path.read_bytes() == before


def test_cli_verify_without_execute_inspects_actual_pending_gates(tmp_path, monkeypatch, capsys):
    from mf_phase12 import cli
    profile = company(tmp_path, 'gamma')
    _, db = bound(profile, Api())
    before = db.path.read_bytes()
    monkeypatch.setattr('mf_phase12.office_profile.load_office_profile', lambda *a, **kw: profile)
    assert cli.main(['--office-profile', 'gamma.json', 'verify', '--strict']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'needs_attention'
    assert not result['recorded_gates_passed']
    assert db.path.read_bytes() == before


def test_readiness_finds_the_actual_ledger_identity_hashes(tmp_path):
    from mf_phase12.starter_readiness import inspect_readiness
    from mf_phase12.shiwake_run import start_run
    profile = company(tmp_path, 'gamma')
    _, db = bound(profile, Api())
    with using_office_profile(profile):
        with db.connect() as conn:
            start_run(conn, 'synthetic-readiness-fixture', accounting_date='2026-09-11')
            # A deliberately incomplete fixture: completed status alone must
            # never satisfy the remaining official receipt/scope gates.
            conn.execute("UPDATE accounting_runs SET status='complete'")
        before = db.path.read_bytes()
        result = inspect_readiness(db.path)
    assert result['checks']['same_company_production_run_complete']
    assert not result['recorded_gates_passed']
    assert not result['checks']['all_active_workflow_official_readbacks']
    assert db.path.read_bytes() == before


def test_payable_external_optional_code_and_wrong_refresh_never_persists(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mf_phase12 import cli
    from mf_phase12.hash_utils import stable_hash
    profile = replace(company(tmp_path, 'gamma'), accounting_rules={
        'payable': {'office_id_hash': stable_hash('gamma-payable')}})
    observed = ['gamma-payable']
    monkeypatch.setattr('mf_phase12.accounting_api.UrllibTransport.request',
        lambda *a, **kw: (200, {}, json.dumps({'offices': [{'id': observed[0]}]}).encode()))
    writes = []
    class Store:
        service = profile.moneyforward_keychain_service
        def get_secret(self, label):
            if label.endswith('access_token'):
                raise KeyError(label)
            return 'synthetic'
        def set_secret(self, *args):
            writes.append(args)
    with using_office_profile(profile):
        assert cli._read_payable_external_office_id(token='synthetic', base_url='https://example.test') == observed[0]
        observed[0] = 'foreign-payable'
        with pytest.raises(PermissionError):
            cli._bind_payable_external_token(keychain=Store(), oauth_factory=lambda *a: SimpleNamespace(
                refresh_access_token=lambda token: SimpleNamespace(access_token='synthetic-new', refresh_token='synthetic-rotate')))
    assert writes == []


@pytest.mark.parametrize('method', ['sync_master_snapshot', 'sync_journal_patterns', 'sync_journal_patterns_incremental'])
def test_direct_snapshot_functions_reject_foreign_connection(tmp_path, method):
    from mf_phase12 import sync
    alpha, beta = company(tmp_path, 'alpha'), company(tmp_path, 'beta')
    client, _ = bound(alpha, Api())
    _, db = bound(beta, Api())
    before = db.path.read_bytes()
    kwargs = {}
    if method == 'sync_journal_patterns':
        kwargs = {'start_date': '2026-01-01', 'end_date': '2026-01-31'}
    elif method.endswith('incremental'):
        kwargs = {'fallback_start_date': '2026-01-01', 'end_date': '2026-01-31'}
    with using_office_profile(alpha), db.connect() as conn, pytest.raises(PermissionError):
        getattr(sync, method)(conn, client, **kwargs)
    assert db.path.read_bytes() == before


@pytest.mark.parametrize('method', ['sync_accounting_full_read_snapshot', 'sync_master_snapshot', 'sync_journal_patterns'])
@pytest.mark.parametrize('fault', ['keychain', 'db'])
def test_snapshot_rejects_wrong_client_binding_before_any_memory_write(tmp_path, method, fault):
    from mf_phase12 import sync
    profile = company(tmp_path, 'alpha')
    client, db = bound(profile, Api())
    if fault == 'keychain':
        client.keychain_service = 'foreign-service'
    else:
        client.db_path = tmp_path / 'foreign.sqlite'
    before = db.path.read_bytes()
    kwargs = {'start_date': '2026-01-01', 'end_date': '2026-01-31'} if method != 'sync_master_snapshot' else {}
    with using_office_profile(profile), db.connect() as conn:
        with pytest.raises((PermissionError, ValueError)):
            getattr(sync, method)(conn, client, **kwargs)
        conn.commit()
    assert db.path.read_bytes() == before


def test_all_bundled_modules_import_without_credentials_or_company_configuration():
    import importlib
    import pkgutil
    import mf_phase12
    for module in pkgutil.walk_packages(mf_phase12.__path__, prefix='mf_phase12.'):
        importlib.import_module(module.name)
