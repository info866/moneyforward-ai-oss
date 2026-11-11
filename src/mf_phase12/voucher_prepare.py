from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import stable_hash
from .voucher_approval import build_voucher_attach_execution_approval, extract_user_approval
from .voucher_approval_record import build_voucher_attach_approval_record
from .voucher_execute import dry_run_hash, dry_run_voucher_attach_execution
from .voucher_preflight import build_voucher_attach_grant, prepare_voucher_attach_preflight


@dataclass(frozen=True)
class VoucherAttachPrepareBundle:
    preflight: dict[str, Any]
    approval_record: dict[str, str]
    grant: dict[str, str | bool]
    dry_run: dict[str, str | bool]
    execution_approval: dict[str, Any]

    def to_redacted_summary(self) -> dict[str, Any]:
        return {
            "operation_type": "voucher_attach",
            "request_hash_prefix": str(self.preflight["request_hash"])[:16],
            "target_hash_prefix": str(self.preflight["target_hash"])[:16],
            "approval_hash_prefix": str(self.grant["approval_hash"])[:16],
            "dry_run_hash_prefix": dry_run_hash_from_payload(self.dry_run)[:16],
            "dry_run_ready_to_post": bool(self.dry_run["ready_to_post"]),
            "executed": bool(self.dry_run["executed"]),
            "moneyforward_api_called": False,
            "keychain_accessed": False,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_file_bytes_stored": False,
            "raw_voucher_file_ids_stored": False,
            "raw_api_response_stored": False,
        }


def build_voucher_attach_prepare_bundle(
    *,
    journal_id: str,
    file_path: Path,
    scope_hash: str,
    approval_record_user_approval_payload: Any,
    execution_user_approval_payload: Any,
    office_id: str = "",
    approved_at: datetime | None = None,
    ttl_minutes: int = 30,
) -> VoucherAttachPrepareBundle:
    preflight = prepare_voucher_attach_preflight(journal_id=journal_id, file_path=file_path, office_id=office_id)
    preflight_payload = preflight.to_redacted_dict()
    approval_record = build_voucher_attach_approval_record(
        preflight=preflight_payload,
        user_approval_payload=approval_record_user_approval_payload,
        scope_hash=scope_hash,
    ).to_local_dict()
    grant = build_voucher_attach_grant(
        preflight=preflight_payload,
        approval_record=approval_record,
        scope_hash=scope_hash,
    ).to_redacted_dict()
    dry_run = dry_run_voucher_attach_execution(
        preflight=preflight_payload,
        grant=grant,
        journal_id=journal_id,
        file_path=file_path,
        office_id=office_id,
    )
    execution_approval = build_voucher_attach_execution_approval(
        preflight=preflight_payload,
        grant=grant,
        dry_run=dry_run,
        user_approval=extract_user_approval(execution_user_approval_payload),
        approved_at=approved_at,
        ttl_minutes=ttl_minutes,
    ).to_redacted_dict()
    return VoucherAttachPrepareBundle(
        preflight=preflight_payload,
        approval_record=approval_record,
        grant=grant,
        dry_run=dry_run.to_redacted_dict(),
        execution_approval=execution_approval,
    )


def dry_run_hash_from_payload(payload: dict[str, Any]) -> str:
    return stable_hash(payload)


def approval_record_hash_from_payload(payload: dict[str, str]) -> str:
    redacted = dict(payload)
    redacted["user_approval_hash"] = stable_hash(redacted.pop("user_approval", ""))
    return stable_hash(redacted)
