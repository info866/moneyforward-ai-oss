"""Portable capability declaration, adapted from the first-party bridge contract."""
from .chatgpt_cloud_contract import MODE_WORKFLOWS, execution_contract as _contract

PROJECT_WORKFLOWS = MODE_WORKFLOWS


def execution_contract(company_slug):
    return {**_contract(company_slug),
            'portable_local_run_state': True,
            'portable_modes': ['DAILY'],
            'source_snapshot_evidence': 'caller_attestation_only',
            'official_accounting_verification': False,
            'cross_audit_supported': False,
            'final_notification_supported': False,
            'journal_provider_writes': False}
