from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounting_api import MoneyForwardAccountingClient, WriteExecutionGrant
from .hash_utils import mask_identifier, stable_hash


VOUCHER_WRITE_SCOPE = "mfc/accounting/voucher.write"
VOUCHER_ATTACH_OPERATION = "voucher_attach"


@dataclass(frozen=True)
class VoucherAttachPreflight:
    operation_type: str
    scope: str
    moneyforward_journal_id_hash: str
    file_name: str
    file_size_bytes: int
    file_hash: str
    content_type: str
    request_hash: str
    target_hash: str
    registration_allowed: bool
    approval_record_template: dict[str, str]
    rollback_method: str
    expected_api_action: str

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "scope": self.scope,
            "moneyforward_journal_id_hash": self.moneyforward_journal_id_hash,
            "file_name": self.file_name,
            "file_size_bytes": self.file_size_bytes,
            "file_hash": self.file_hash,
            "content_type": self.content_type,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "registration_allowed": self.registration_allowed,
            "approval_record_template": self.approval_record_template,
            "rollback_method": self.rollback_method,
            "expected_api_action": self.expected_api_action,
            "raw_file_bytes_stored": False,
            "raw_journal_id_stored": False,
        }


@dataclass(frozen=True)
class VoucherAttachGrantBundle:
    grant: WriteExecutionGrant
    approval_hash: str
    approval_record_hash: str
    request_hash: str
    target_hash: str

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "operation_type": self.grant.operation_type,
            "approval_hash": self.approval_hash,
            "approval_record_hash": self.approval_record_hash,
            "scope_hash": self.grant.scope_hash,
            "target_hash": self.target_hash,
            "request_hash": self.request_hash,
            "registration_allowed": True,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_file_bytes_stored": False,
        }


def prepare_voucher_attach_preflight(
    *,
    journal_id: str,
    file_path: Path,
    office_id: str = "",
    client: MoneyForwardAccountingClient | None = None,
) -> VoucherAttachPreflight:
    if not journal_id:
        raise ValueError("journal_id is required")
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"voucher file not found: {path}")
    file_hash = sha256_file(path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    accounting_client = client or MoneyForwardAccountingClient("PRELIGHT_TOKEN_NOT_USED")
    metadata = accounting_client.attach_voucher_request_metadata(
        office_id=office_id,
        journal_id=journal_id,
        file_name=path.name,
        content_type=content_type,
        file_hash=file_hash,
    )
    target_hash = stable_hash(
        {
            "operation_type": VOUCHER_ATTACH_OPERATION,
            "scope": VOUCHER_WRITE_SCOPE,
            "moneyforward_journal_id_hash": mask_identifier(journal_id),
            "file_hash": file_hash,
            "content_type": content_type,
        }
    )
    rollback_method = (
        "If the attachment is executed, record voucher_file_id from the response and verify "
        "journal.voucher_file_ids by read-back. Detach/delete/cancel only after separate explicit "
        "approval with target journal_id and voucher_file_id."
    )
    expected_api_action = (
        "POST /api/v3/vouchers with multipart journal_id and one evidence file, then "
        "GET /api/v3/journals/{id} read-back verification."
    )
    return VoucherAttachPreflight(
        operation_type=VOUCHER_ATTACH_OPERATION,
        scope=VOUCHER_WRITE_SCOPE,
        moneyforward_journal_id_hash=mask_identifier(journal_id),
        file_name=path.name,
        file_size_bytes=path.stat().st_size,
        file_hash=file_hash,
        content_type=content_type,
        request_hash=metadata["request_hash"],
        target_hash=target_hash,
        registration_allowed=False,
        approval_record_template={
            "scope": VOUCHER_WRITE_SCOPE,
            "purpose": "Phase 12 voucher attachment verification for one specified evidence file.",
            "expected_api_action": expected_api_action,
            "rollback_method": rollback_method,
            "user_approval": "",
            "scope_hash": "",
            "request_hash": metadata["request_hash"],
            "target_hash": target_hash,
        },
        rollback_method=rollback_method,
        expected_api_action=expected_api_action,
    )


def build_voucher_attach_grant(
    *,
    preflight: dict[str, Any],
    approval_record: dict[str, Any],
    scope_hash: str,
) -> VoucherAttachGrantBundle:
    _require(preflight.get("operation_type") == VOUCHER_ATTACH_OPERATION, "preflight operation_type mismatch")
    _require(preflight.get("scope") == VOUCHER_WRITE_SCOPE, "preflight scope must be voucher.write")
    _require(approval_record.get("scope") == VOUCHER_WRITE_SCOPE, "approval scope must be voucher.write")
    _require(bool(approval_record.get("user_approval")), "approval user_approval is required")
    _require(bool(scope_hash), "scope_hash is required")
    _require(approval_record.get("scope_hash") == scope_hash, "approval scope_hash does not match recorded scope")

    for field in ("request_hash", "target_hash"):
        _require(bool(preflight.get(field)), f"preflight {field} is required")
        _require(approval_record.get(field) == preflight.get(field), f"approval {field} does not match preflight")

    expected_action = str(preflight.get("expected_api_action", ""))
    rollback_method = str(preflight.get("rollback_method", ""))
    _require(approval_record.get("expected_api_action") == expected_action, "approval expected_api_action mismatch")
    _require(approval_record.get("rollback_method") == rollback_method, "approval rollback_method mismatch")
    _require("POST /api/v3/vouchers" in expected_action, "approval must name POST /api/v3/vouchers")
    _require("voucher_file_id" in rollback_method, "rollback method must mention voucher_file_id")

    approval_record_hash = stable_hash(_redacted_approval_record_for_hash(approval_record))
    approval_hash = stable_hash(
        {
            "operation_type": VOUCHER_ATTACH_OPERATION,
            "approval_record_hash": approval_record_hash,
            "scope_hash": scope_hash,
            "request_hash": preflight["request_hash"],
            "target_hash": preflight["target_hash"],
        }
    )
    return VoucherAttachGrantBundle(
        grant=WriteExecutionGrant(
            operation_type=VOUCHER_ATTACH_OPERATION,
            approval_hash=approval_hash,
            scope_hash=scope_hash,
            target_hash=preflight["target_hash"],
        ),
        approval_hash=approval_hash,
        approval_record_hash=approval_record_hash,
        request_hash=preflight["request_hash"],
        target_hash=preflight["target_hash"],
    )


def load_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON file must contain an object")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redacted_approval_record_for_hash(approval_record: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(approval_record)
    redacted["user_approval_hash"] = stable_hash(str(redacted.pop("user_approval", "")))
    return redacted


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
