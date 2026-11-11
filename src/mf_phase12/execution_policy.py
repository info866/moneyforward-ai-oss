from __future__ import annotations
from .office_profile import get_current_profile
from .shiwake_run import (
    _setting,
    _office_name,
    _mf_principal,
    _drive_principal,
    _artifact_root,
)
from dataclasses import dataclass
from typing import Any

API = "api"
COMPUTER_USE = "computer_use"
LOCAL_PIPELINE = "local_pipeline"


@dataclass(frozen=True)
class ExecutionChannelDecision:
    operation: str
    selected_channel: str
    allowed_channels: tuple[str, ...]
    reason_code: str
    requires_candidate_binding: bool
    required_readback: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "selected_channel": self.selected_channel,
            "allowed_channels": list(self.allowed_channels),
            "reason_code": self.reason_code,
            "requires_candidate_binding": self.requires_candidate_binding,
            "required_readback": list(self.required_readback),
        }


def select_execution_channel(
    operation: str,
    *,
    public_api_supported: bool = True,
    payroll_identifier_available: bool = False,
    simple_single_branch_transaction: bool = True,
) -> ExecutionChannelDecision:
    if operation == "connected_unposted_inventory":
        if public_api_supported:
            return _decision(
                operation,
                API,
                (API,),
                "official_transaction_read_api_verified",
                False,
                "configured_company_tenant",
                "all_connected_account_counts",
                "candidate_count",
                "candidate_body",
            )
        return _decision(
            operation,
            COMPUTER_USE,
            (COMPUTER_USE,),
            "public_candidate_read_api_not_verified",
            False,
            "configured_company_tenant",
            "all_connected_account_counts",
            "captured_candidate_body",
        )
    if operation == "connected_unposted_consume":
        if public_api_supported and simple_single_branch_transaction:
            return _decision(
                operation,
                API,
                (API,),
                "official_transaction_journalize_api_verified",
                True,
                "transaction_status_registered",
                "journal_readback",
                "transaction_id_linkage",
                "account_unposted_count",
            )
        if public_api_supported:
            return _decision(
                operation,
                COMPUTER_USE,
                (COMPUTER_USE,),
                "official_transaction_journalize_supports_one_counterpart_only",
                True,
                "configured_company_tenant",
                "candidate_removed",
                "journal_readback",
                "transaction_id_linkage",
                "account_unposted_count",
            )
        return _decision(
            operation,
            COMPUTER_USE,
            (COMPUTER_USE,),
            "journal_api_does_not_consume_connected_candidate",
            True,
            "candidate_removed",
            "journal_readback",
            "account_unposted_count",
        )
    if operation in {
        "journal_create",
        "journal_update",
        "journal_readback",
        "voucher_attach",
        "voucher_readback",
    }:
        return _decision(
            operation,
            API,
            (API,),
            "official_moneyforward_api_is_primary",
            False,
            "api_readback",
        )
    if operation in {
        "rag_search",
        "classification",
        "duplicate_probe",
        "cross_source_audit",
    }:
        return _decision(
            operation,
            LOCAL_PIPELINE,
            (LOCAL_PIPELINE,),
            "deterministic_local_pipeline",
            False,
            "redacted_artifact",
        )
    if operation == "payroll_details":
        if payroll_identifier_available:
            return _decision(
                operation,
                API,
                (API,),
                "payroll_identifier_available",
                False,
                "payroll_api_readback",
            )
        return _decision(
            operation,
            LOCAL_PIPELINE,
            (LOCAL_PIPELINE, COMPUTER_USE),
            "payroll_identifier_missing_use_reviewed_export_first",
            False,
            "wage_ledger_period",
            "employee_totals",
        )
    raise ValueError(f"unknown shiwake operation: {operation}")


def validate_execution_channel(
    operation: str,
    selected_channel: str,
    *,
    public_api_supported: bool = True,
    payroll_identifier_available: bool = False,
    candidate_bound: bool = False,
    simple_single_branch_transaction: bool = True,
) -> tuple[bool, str]:
    decision = select_execution_channel(
        operation,
        public_api_supported=public_api_supported,
        payroll_identifier_available=payroll_identifier_available,
        simple_single_branch_transaction=simple_single_branch_transaction,
    )
    if selected_channel not in decision.allowed_channels:
        return (False, f"channel_not_allowed:{decision.reason_code}")
    if decision.requires_candidate_binding and (not candidate_bound):
        return (False, "connected_candidate_binding_required")
    return (True, "execution_channel_allowed")


def build_pdca_recovery_policy() -> dict[str, Any]:
    return {
        "loop": [
            "inventory",
            "classify",
            "execute",
            "readback",
            "reconcile",
            "next_delta",
        ],
        "completion_condition": "current_configured_company_connected_accounts_all_zero_and_all_writes_read_back",
        "no_progress_rule": "do_not_repeat_the_same_failed_action; change_channel_or_input_after_one_failed_cycle",
        "recovery_ladder": {
            "transaction_read_scope_missing": [
                "record_scope_purpose",
                "oauth_transaction_read_reauthorization",
                "api_readback",
            ],
            "public_candidate_api_outage": [
                "record_api_error",
                "computer_use_full_inventory",
                "tsv_capture",
                "local_ingest",
            ],
            "computer_use_selector_stale": [
                "refresh_state",
                "accessibility_index",
                "visual_coordinate",
                "tsv_capture",
            ],
            "journal_delivery_uncertain": [
                "do_not_repost",
                "api_search_exact_body",
                "resolve_readback_or_review",
            ],
            "unposted_count_not_decreasing": [
                "stop_transaction_journalize_repost",
                "get_transaction_status",
                "get_linked_journal",
                "manual_review_if_inconsistent",
            ],
            "rag_exact_match_missing": [
                "ocr_or_statement_facts",
                "similar_configured_company_patterns",
                "master_resolution",
                "hook_gate",
            ],
            "readback_mismatch": [
                "freeze_target",
                "compare_requested_and_actual_body",
                "correct_once",
                "readback_again",
            ],
        },
        "computer_use_limits": {
            "allowed": ["verified_api_outage_fallback", "api_gap_read_only_capture"],
            "prohibited": [
                "freehand_journal_create_when_api_supported",
                "classification_from_visible_text_only",
                "completion_without_refresh",
            ],
            "each_action_requires": [
                "configured_company_tenant_readback",
                "candidate_binding",
                "post_action_readback",
            ],
        },
    }


def build_execution_policy_contract() -> dict[str, Any]:
    operations = (
        "connected_unposted_inventory",
        "connected_unposted_consume",
        "journal_create",
        "journal_update",
        "journal_readback",
        "voucher_attach",
        "voucher_readback",
        "rag_search",
        "classification",
        "duplicate_probe",
        "cross_source_audit",
        "payroll_details",
    )
    return {
        "status": "ready",
        "api_first": True,
        "decisions": {
            operation: select_execution_channel(operation).to_dict()
            for operation in operations
        },
        "pdca": build_pdca_recovery_policy(),
        "raw_secret_stored": False,
        "external_write_called": False,
    }


def _decision(
    operation: str,
    selected_channel: str,
    allowed_channels: tuple[str, ...],
    reason_code: str,
    requires_candidate_binding: bool,
    *required_readback: str,
) -> ExecutionChannelDecision:
    return ExecutionChannelDecision(
        operation=operation,
        selected_channel=selected_channel,
        allowed_channels=allowed_channels,
        reason_code=reason_code,
        requires_candidate_binding=requires_candidate_binding,
        required_readback=tuple(required_readback),
    )
