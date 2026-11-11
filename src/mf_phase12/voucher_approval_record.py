from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .voucher_approval import extract_user_approval
from .voucher_preflight import VOUCHER_ATTACH_OPERATION, VOUCHER_WRITE_SCOPE, build_voucher_attach_grant


@dataclass(frozen=True)
class VoucherAttachApprovalRecord:
    payload: dict[str, str]

    def to_local_dict(self) -> dict[str, str]:
        return dict(self.payload)

    def to_redacted_summary(self) -> dict[str, str | bool]:
        return {
            "operation_type": VOUCHER_ATTACH_OPERATION,
            "scope": self.payload["scope"],
            "request_hash_prefix": self.payload["request_hash"][:16],
            "target_hash_prefix": self.payload["target_hash"][:16],
            "scope_hash_prefix": self.payload["scope_hash"][:16],
            "user_approval_stored_in_ignored_local_file": True,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_file_bytes_stored": False,
            "raw_api_response_stored": False,
            "keychain_accessed": False,
            "moneyforward_api_called": False,
        }


def build_voucher_attach_approval_record(
    *,
    preflight: dict[str, Any],
    user_approval_payload: Any,
    scope_hash: str,
) -> VoucherAttachApprovalRecord:
    _require(preflight.get("operation_type") == VOUCHER_ATTACH_OPERATION, "preflight operation_type mismatch")
    _require(preflight.get("scope") == VOUCHER_WRITE_SCOPE, "preflight scope must be voucher.write")
    _require(bool(scope_hash), "scope_hash is required")
    template = preflight.get("approval_record_template")
    _require(isinstance(template, dict), "preflight approval_record_template is required")
    user_approval = extract_user_approval(user_approval_payload)
    payload = {
        "scope": VOUCHER_WRITE_SCOPE,
        "purpose": str(template.get("purpose") or ""),
        "expected_api_action": str(preflight.get("expected_api_action") or template.get("expected_api_action") or ""),
        "rollback_method": str(preflight.get("rollback_method") or template.get("rollback_method") or ""),
        "user_approval": user_approval,
        "scope_hash": scope_hash,
        "request_hash": str(preflight.get("request_hash") or ""),
        "target_hash": str(preflight.get("target_hash") or ""),
    }
    for field in ("purpose", "expected_api_action", "rollback_method", "request_hash", "target_hash"):
        _require(bool(payload[field]), f"approval record {field} is required")
    _require("POST /api/v3/vouchers" in payload["expected_api_action"], "approval record must name POST /api/v3/vouchers")
    _require("GET /api/v3/journals/{id}" in payload["expected_api_action"], "approval record must name journal read-back")
    _require("voucher_file_id" in payload["rollback_method"], "approval record rollback must mention voucher_file_id")
    _require(
        "separate explicit approval" in payload["rollback_method"],
        "approval record rollback must require separate explicit approval",
    )
    build_voucher_attach_grant(preflight=preflight, approval_record=payload, scope_hash=scope_hash)
    return VoucherAttachApprovalRecord(payload=payload)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
