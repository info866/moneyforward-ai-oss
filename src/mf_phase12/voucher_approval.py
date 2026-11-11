from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .voucher_audit import require_voucher_attach_execution_approval_details
from .voucher_execute import VoucherAttachExecutionDryRun, dry_run_hash


@dataclass(frozen=True)
class VoucherAttachExecutionApproval:
    payload: dict[str, Any]

    def to_redacted_dict(self) -> dict[str, Any]:
        allowed = {
            "operation_type",
            "approval_hash",
            "approval_record_hash",
            "request_hash",
            "target_hash",
            "scope_hash",
            "dry_run_hash",
            "moneyforward_journal_id_hash",
            "user_approval",
            "expected_api_action",
            "rollback_method",
            "approved_at",
            "expires_at",
            "execution_confirmation",
            "moneyforward_api_called",
            "raw_secret_stored",
            "raw_journal_id_stored",
            "raw_voucher_file_ids_stored",
            "raw_api_response_stored",
        }
        return {key: self.payload[key] for key in allowed if key in self.payload}


def build_voucher_attach_execution_approval(
    *,
    preflight: dict[str, Any],
    grant: dict[str, Any],
    dry_run: VoucherAttachExecutionDryRun,
    user_approval: str,
    approved_at: datetime | None = None,
    ttl_minutes: int = 30,
) -> VoucherAttachExecutionApproval:
    _require(grant.get("operation_type") == "voucher_attach", "grant operation_type mismatch")
    _require(grant.get("registration_allowed") is True, "grant registration_allowed must be true")
    _require(bool(user_approval.strip()), "user_approval is required")
    _require(ttl_minutes > 0, "ttl_minutes must be positive")
    _require(preflight.get("request_hash") == dry_run.request_hash, "preflight request_hash mismatch")
    _require(preflight.get("target_hash") == dry_run.target_hash, "preflight target_hash mismatch")
    _require(grant.get("request_hash") == dry_run.request_hash, "grant request_hash mismatch")
    _require(grant.get("target_hash") == dry_run.target_hash, "grant target_hash mismatch")
    _require(grant.get("approval_hash") == dry_run.approval_hash, "grant approval_hash mismatch")
    _require(dry_run.ready_to_post is True, "dry_run must be ready_to_post")
    _require(dry_run.executed is False, "dry_run must not be executed")

    approval_record_hash = str(grant.get("approval_record_hash") or "")
    expected_api_action = str(preflight.get("expected_api_action") or "")
    rollback_method = str(preflight.get("rollback_method") or "")
    approved = (approved_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = approved + timedelta(minutes=ttl_minutes)
    computed_dry_run_hash = dry_run_hash(dry_run)
    payload: dict[str, Any] = {
        "operation_type": "voucher_attach",
        "approval_hash": dry_run.approval_hash,
        "approval_record_hash": approval_record_hash,
        "request_hash": dry_run.request_hash,
        "target_hash": dry_run.target_hash,
        "scope_hash": str(grant.get("scope_hash") or ""),
        "dry_run_hash": computed_dry_run_hash,
        "moneyforward_journal_id_hash": dry_run.moneyforward_journal_id_hash,
        "user_approval": user_approval.strip(),
        "expected_api_action": expected_api_action,
        "rollback_method": rollback_method,
        "approved_at": _format_utc(approved),
        "expires_at": _format_utc(expires),
        "moneyforward_api_called": False,
        "raw_secret_stored": False,
        "raw_journal_id_stored": False,
        "raw_voucher_file_ids_stored": False,
        "raw_api_response_stored": False,
    }
    payload["execution_confirmation"] = {
        "operation_type": "voucher_attach",
        "approval_hash": dry_run.approval_hash,
        "approval_record_hash": approval_record_hash,
        "request_hash": dry_run.request_hash,
        "target_hash": dry_run.target_hash,
        "scope_hash": str(grant.get("scope_hash") or ""),
        "dry_run_hash": computed_dry_run_hash,
        "moneyforward_journal_id_hash": dry_run.moneyforward_journal_id_hash,
        "expected_api_action": expected_api_action,
        "rollback_method": rollback_method,
        "execute_approved": True,
        "moneyforward_post_allowed": True,
        "read_back_required": True,
        "rollback_requires_separate_approval": True,
        "raw_secret_stored": False,
        "raw_journal_id_stored": False,
        "raw_voucher_file_ids_stored": False,
        "raw_api_response_stored": False,
    }
    require_voucher_attach_execution_approval_details(
        execution_approval=payload,
        expected_dry_run_hash=computed_dry_run_hash,
        preflight=preflight,
        grant=grant,
        now=approved,
    )
    return VoucherAttachExecutionApproval(payload=payload)


def extract_user_approval(payload: Any) -> str:
    if isinstance(payload, str):
        value = payload
    elif isinstance(payload, dict):
        value = payload.get("user_approval") or payload.get("approval") or payload.get("text")
    else:
        value = None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("user approval JSON must contain a non-empty string or user_approval field")
    return value.strip()


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("approved_at must include timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
