"""Read-only readiness from the selected company's existing evidence ledger."""
from __future__ import annotations

from .db import Phase12Database
from .hash_utils import stable_hash
from .office_profile import resolve_office_profile, require_database_office_identity
from .product_readiness import build_product_readiness_status
from .starter_audit import audit


def inspect_readiness(db_path=None) -> dict:
    profile = resolve_office_profile(required=True)
    profile.require_configured()
    db = Phase12Database(db_path)
    modes = {mode: build_product_readiness_status(db.path, product_mode=mode)
             for mode in ('limited_auto', 'workflow_ready', 'managed_operations')}
    local = audit()
    with db.connect_readonly() as conn:
        require_database_office_identity(conn, profile)
        run = conn.execute('''SELECT run_id_hash,status FROM accounting_runs
            WHERE run_mode='production' AND office_hash=? AND execution_principal_hash=?
            ORDER BY id DESC LIMIT 1''',
            (stable_hash({'office': profile.legal_name}), stable_hash({'principal': profile.google_account.strip().casefold()}))).fetchone()
        run_hash = run['run_id_hash'] if run else ''
        kinds = {(row['stage_key'], row['receipt_kind']) for row in conn.execute(
            "SELECT stage_key,receipt_kind FROM accounting_run_receipts WHERE run_id_hash=? AND status='verified'", (run_hash,))}
        from .shiwake_run import ACTIVE_ACCOUNTING_WORKFLOWS
        source_receipts = all((stage, 'source_stage_readback') in kinds for stage in ACTIVE_ACCOUNTING_WORKFLOWS)
        chat = conn.execute("SELECT 1 FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND status IN ('sent','historical_sent') AND response_hash IS NOT NULL LIMIT 1", (run_hash,)).fetchone()
        scope = conn.execute('SELECT has_journal_write,has_voucher_write FROM oauth_scope_checks ORDER BY id DESC LIMIT 1').fetchone()
    checks = {
        'local_audit': bool(local['passed']),
        'bound_company_database': True,
        'classification_and_workflow_gates': all(v['phase12_product_ready'] for v in modes.values()),
        'recorded_accounting_write_scopes': bool(scope and scope['has_journal_write'] and scope['has_voucher_write']),
        'same_company_production_run_complete': bool(run and run['status'] == 'complete'),
        'all_active_workflow_official_readbacks': source_receipts,
        'same_run_cross_audit': ('cross_audit', 'cross_audit_readback') in kinds,
        'same_run_durable_notification': chat is not None,
    }
    ready = all(checks.values())
    return {'status': 'recorded_gates_passed' if ready else 'needs_attention',
        'recorded_gates_passed': ready, 'checks': checks,
        'failed_checks': [key for key, passed in checks.items() if not passed],
        'product_modes': modes, 'external_io': False, 'database_write_called': False,
        'live_authentication_rechecked': False,
        'note': 'Recorded evidence does not replace current per-operation identity, scope and read-back guards.'}
