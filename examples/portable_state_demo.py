#!/usr/bin/env python3
"""Offline synthetic restart demonstration; no provider or real credential access.

Creates only a disposable SQLite fixture, never a production-ready company.
Its synthetic binding is test data and must not be reused for real accounting.
"""
from pathlib import Path
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mf_phase12.db import Phase12Database
from mf_phase12.hash_utils import stable_hash
from mf_phase12.office_profile import (OfficeProfile, OfficeIdentityBinding,
    bind_database_office_identity, normalize_office_name, using_office_profile)
from mf_phase12.workflow_bridge import WorkflowBridge


def demonstrate(root):
    profile = OfficeProfile(schema_version=2, configured=True, slug='synthetic-demo',
        legal_name='Synthetic Demo', display_name='Synthetic Demo',
        expected_office_names=('Synthetic Demo',), google_account='demo@synthetic.invalid',
        google_drive_account='demo@synthetic.invalid', moneyforward_keychain_service='unused-synthetic',
        moneyforward_db_path=root / 'data/phase12/offices/synthetic-demo/ledger.sqlite',
        repository_root=root, google_drive_evidence_root_id='synthetic_root',
        google_drive_evidence_source_folder_ids=('synthetic_root',),
        google_drive_processed_folder_id='synthetic_processed',
        google_drive_temporary_ocr_folder_id='synthetic_temporary')
    with using_office_profile(profile):
        db = Phase12Database(profile.moneyforward_db_path)
        db.initialize()
        with db.connect() as conn:
            bind_database_office_identity(conn, profile, OfficeIdentityBinding(
                profile_slug=profile.slug, profile_hash=profile.profile_hash,
                office_id='synthetic-office', office_id_hash=stable_hash('synthetic-office'),
                legal_name_hash=stable_hash(normalize_office_name(profile.legal_name)),
                keychain_service_hash=stable_hash(profile.moneyforward_keychain_service),
                boundary_fingerprint=profile.boundary_fingerprint))
    request = dict(instruction='Synthetic exception review', accounting_date='2026-01-01',
        source_scope={'selected_workflows': ['sales_invoice']}, request_id='retained-demo-request')
    WorkflowBridge(profile).run_begin(**request)  # intentionally disregard first reply
    recovered = WorkflowBridge(profile).run_begin(**request)
    run = recovered['run_id']
    bridge = WorkflowBridge(profile)
    identity, facts = stable_hash('synthetic-document'), stable_hash('synthetic-facts-v1')
    bridge.run_record_source_snapshot(run, 'sales_invoice', [identity], facts)
    decision = dict(run_id=run, workflow='sales_invoice', source_identity_hash=identity,
        state='NOT_APPLICABLE', decision={'basis': 'synthetic non-accounting document'},
        current_facts_hash=facts)
    bridge.run_record_item(**decision)
    replay = WorkflowBridge(profile).run_record_item(**decision)
    with using_office_profile(profile):
        bridge._path(run).unlink()  # simulate a missing derived cache
    resumed = WorkflowBridge(profile).run_resume(run)
    with sqlite3.connect(profile.moneyforward_db_path) as conn:
        run_count = conn.execute('SELECT count(*) FROM accounting_runs').fetchone()[0]
    assert recovered['already_started'] is True and run_count == 1
    assert replay['status'] == 'already_recorded'
    assert resumed['local_state_cache'] == 'verified'
    assert resumed['external_operations_executed'] == 0
    assert resumed['accounting_completed'] is False
    return {'evidence_level': 'OFFLINE_SYNTHETIC', 'run_count': run_count,
        'same_request_reused': True, 'same_decision_reused': True,
        'cache_rebuilt_from_bound_db': True, 'provider_operations': 0,
        'accounting_completed': False, 'production_ready': False}


if __name__ == '__main__':
    with TemporaryDirectory(prefix='mf-synthetic-demo-') as directory:
        print(json.dumps(demonstrate(Path(directory)), sort_keys=True))
