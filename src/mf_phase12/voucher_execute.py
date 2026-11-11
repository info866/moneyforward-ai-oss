from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounting_api import MoneyForwardAccountingClient, WriteExecutionGrant
from .hash_utils import mask_identifier, stable_hash
from .voucher_audit import require_voucher_attach_execution_approval_details
from .voucher_preflight import load_json_file, prepare_voucher_attach_preflight


@dataclass(frozen=True)
class VoucherAttachExecutionDryRun:
    operation_type: str
    ready_to_post: bool
    executed: bool
    request_hash: str
    target_hash: str
    approval_hash: str
    file_hash: str
    moneyforward_journal_id_hash: str

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "operation_type": self.operation_type,
            "ready_to_post": self.ready_to_post,
            "executed": self.executed,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "approval_hash": self.approval_hash,
            "file_hash": self.file_hash,
            "moneyforward_journal_id_hash": self.moneyforward_journal_id_hash,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_file_bytes_stored": False,
            "moneyforward_api_called": False,
        }


@dataclass(frozen=True)
class VoucherAttachPostResult:
    operation_type: str
    executed: bool
    request_hash: str
    target_hash: str
    approval_hash: str
    response_hash: str
    voucher_file_ids_hash: str
    voucher_file_ids: list[str]
    moneyforward_api_called: bool

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "operation_type": self.operation_type,
            "executed": self.executed,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "approval_hash": self.approval_hash,
            "response_hash": self.response_hash,
            "voucher_file_ids_hash": self.voucher_file_ids_hash,
            "moneyforward_api_called": self.moneyforward_api_called,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_file_bytes_stored": False,
            "raw_api_response_stored": False,
        }


@dataclass(frozen=True)
class VoucherAttachReadBackVerification:
    operation_type: str
    verified: bool
    journal_id_hash: str
    expected_voucher_file_ids_hash: str
    actual_voucher_file_ids_hash: str
    post_result_voucher_file_ids_hash: str
    response_hash: str
    moneyforward_api_called: bool

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "operation_type": self.operation_type,
            "verified": self.verified,
            "journal_id_hash": self.journal_id_hash,
            "expected_voucher_file_ids_hash": self.expected_voucher_file_ids_hash,
            "actual_voucher_file_ids_hash": self.actual_voucher_file_ids_hash,
            "post_result_voucher_file_ids_hash": self.post_result_voucher_file_ids_hash,
            "response_hash": self.response_hash,
            "moneyforward_api_called": self.moneyforward_api_called,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_voucher_file_ids_stored": False,
            "raw_api_response_stored": False,
        }


def dry_run_voucher_attach_execution(
    *,
    preflight: dict[str, Any],
    grant: dict[str, Any],
    journal_id: str,
    file_path: Path,
    office_id: str = "",
) -> VoucherAttachExecutionDryRun:
    _require(grant.get("operation_type") == "voucher_attach", "grant operation_type mismatch")
    _require(grant.get("registration_allowed") is True, "grant registration_allowed must be true")
    for field in ("approval_hash", "scope_hash", "request_hash", "target_hash"):
        _require(bool(grant.get(field)), f"grant {field} is required")
    _require(grant.get("request_hash") == preflight.get("request_hash"), "grant request_hash does not match preflight")
    _require(grant.get("target_hash") == preflight.get("target_hash"), "grant target_hash does not match preflight")

    recomputed = prepare_voucher_attach_preflight(journal_id=journal_id, file_path=Path(file_path), office_id=office_id)
    recomputed_payload = recomputed.to_redacted_dict()
    for field in (
        "moneyforward_journal_id_hash",
        "file_name",
        "file_size_bytes",
        "file_hash",
        "content_type",
        "request_hash",
        "target_hash",
    ):
        _require(recomputed_payload.get(field) == preflight.get(field), f"execution {field} does not match preflight")

    return VoucherAttachExecutionDryRun(
        operation_type="voucher_attach",
        ready_to_post=True,
        executed=False,
        request_hash=str(preflight["request_hash"]),
        target_hash=str(preflight["target_hash"]),
        approval_hash=str(grant["approval_hash"]),
        file_hash=str(preflight["file_hash"]),
        moneyforward_journal_id_hash=mask_identifier(journal_id),
    )


def load_execution_inputs(
    *,
    preflight_json: Path,
    grant_json: Path,
    journal_id: str,
    file_path: Path,
    office_id: str = "",
) -> VoucherAttachExecutionDryRun:
    return dry_run_voucher_attach_execution(
        preflight=load_json_file(preflight_json),
        grant=load_json_file(grant_json),
        journal_id=journal_id,
        file_path=file_path,
        office_id=office_id,
    )


def dry_run_hash(result: VoucherAttachExecutionDryRun) -> str:
    return stable_hash(result.to_redacted_dict())


def execute_voucher_attach_with_client(
    *,
    client: MoneyForwardAccountingClient,
    dry_run: VoucherAttachExecutionDryRun,
    preflight: dict[str, Any],
    grant: dict[str, Any],
    journal_id: str,
    file_path: Path,
    office_id: str = "",
    execution_approval: dict[str, Any],
) -> VoucherAttachPostResult:
    _require(dry_run.ready_to_post is True, "dry_run must be ready_to_post")
    _require(dry_run.executed is False, "dry_run must not already be executed")
    _require(execution_approval.get("operation_type") == "voucher_attach", "execution approval operation mismatch")
    _require(execution_approval.get("approval_hash") == dry_run.approval_hash, "execution approval_hash mismatch")
    _require(execution_approval.get("request_hash") == dry_run.request_hash, "execution request_hash mismatch")
    _require(execution_approval.get("target_hash") == dry_run.target_hash, "execution target_hash mismatch")
    _require(execution_approval.get("dry_run_hash") == dry_run_hash(dry_run), "execution dry_run_hash mismatch")
    require_voucher_attach_execution_approval_details(
        execution_approval=execution_approval,
        expected_dry_run_hash=dry_run_hash(dry_run),
        preflight=preflight,
        grant=grant,
    )
    _require(grant.get("approval_hash") == dry_run.approval_hash, "grant approval_hash mismatch")
    _require(grant.get("scope_hash") == execution_approval.get("scope_hash"), "grant scope_hash mismatch")
    _require(grant.get("request_hash") == dry_run.request_hash, "grant request_hash mismatch")
    _require(grant.get("target_hash") == dry_run.target_hash, "grant target_hash mismatch")
    _require(preflight.get("request_hash") == dry_run.request_hash, "preflight request_hash mismatch")
    _require(preflight.get("target_hash") == dry_run.target_hash, "preflight target_hash mismatch")
    immediate_dry_run = dry_run_voucher_attach_execution(
        preflight=preflight,
        grant=grant,
        journal_id=journal_id,
        file_path=file_path,
        office_id=office_id,
    )
    _require(dry_run_hash(immediate_dry_run) == dry_run_hash(dry_run), "immediate dry_run_hash mismatch")
    path = Path(file_path)
    file_bytes = path.read_bytes()
    response = client.attach_voucher(
        office_id=office_id or None,
        journal_id=journal_id,
        file_name=path.name,
        content_type=str(preflight["content_type"]),
        file_bytes=file_bytes,
        grant=WriteExecutionGrant(
            operation_type="voucher_attach",
            approval_hash=str(grant["approval_hash"]),
            scope_hash=str(grant["scope_hash"]),
            target_hash=str(grant["target_hash"]),
        ),
    )
    voucher_file_ids = _normalize_voucher_file_ids(response.get("voucher_file_ids", []))
    _require(bool(voucher_file_ids), "voucher attachment response must include voucher_file_ids")
    return VoucherAttachPostResult(
        operation_type="voucher_attach",
        executed=True,
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        approval_hash=dry_run.approval_hash,
        response_hash=stable_hash(_redact_response(response)),
        voucher_file_ids_hash=stable_hash(voucher_file_ids),
        voucher_file_ids=voucher_file_ids,
        moneyforward_api_called=True,
    )


def _redact_response(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(response.keys()),
        "voucher_file_ids_hash": stable_hash(_normalize_voucher_file_ids(response.get("voucher_file_ids", []))),
        "raw_response_stored": False,
    }


def verify_voucher_attach_read_back(
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    expected_voucher_file_ids: list[str],
    post_result: dict[str, Any],
    office_id: str = "",
) -> VoucherAttachReadBackVerification:
    _require(bool(journal_id), "journal_id is required")
    _require(bool(expected_voucher_file_ids), "expected_voucher_file_ids is required")
    expected_ids = _normalize_voucher_file_ids(expected_voucher_file_ids)
    expected_hash = stable_hash(expected_ids)
    post_hash = str(post_result.get("voucher_file_ids_hash") or "")
    _require(bool(post_hash), "post_result voucher_file_ids_hash is required")
    _require(expected_hash == post_hash, "expected voucher_file_ids do not match post result hash")

    response = client.journal(journal_id, office_id=office_id or None)
    actual_ids = _extract_voucher_file_ids(response)
    actual_hash = stable_hash(actual_ids)
    return VoucherAttachReadBackVerification(
        operation_type="voucher_attach_read_back",
        # POST /vouchers returns the IDs created by that request, while the
        # journal read-back contains every voucher attached to the journal.
        # A journal may therefore legitimately contain additional, previously
        # verified vouchers.  Require every ID from this POST to be present
        # instead of requiring the two lists to be identical.
        verified=set(expected_ids).issubset(set(actual_ids)),
        journal_id_hash=mask_identifier(journal_id),
        expected_voucher_file_ids_hash=expected_hash,
        actual_voucher_file_ids_hash=actual_hash,
        post_result_voucher_file_ids_hash=post_hash,
        response_hash=stable_hash(_redact_journal_read_back(response, actual_ids)),
        moneyforward_api_called=True,
    )


def _extract_voucher_file_ids(response: dict[str, Any]) -> list[str]:
    candidates = response.get("voucher_file_ids")
    if candidates is None and isinstance(response.get("data"), dict):
        candidates = response["data"].get("voucher_file_ids")
    if candidates is None and isinstance(response.get("journal"), dict):
        candidates = response["journal"].get("voucher_file_ids")
    if candidates is None:
        candidates = []
    return _normalize_voucher_file_ids(candidates)


def _redact_journal_read_back(response: dict[str, Any], actual_ids: list[str]) -> dict[str, Any]:
    return {
        "keys": sorted(response.keys()),
        "voucher_file_ids_hash": stable_hash(actual_ids),
        "raw_response_stored": False,
    }


def _normalize_voucher_file_ids(values: Any) -> list[str]:
    _require(isinstance(values, list), "voucher_file_ids must be a list")
    normalized: list[str] = []
    for value in values:
        if isinstance(value, dict):
            file_id = value.get("file_id") or value.get("id") or value.get("voucher_file_id")
            _require(isinstance(file_id, str) and bool(file_id), "voucher_file_ids item must contain file_id")
            normalized.append(file_id)
        else:
            normalized.append(str(value))
    return sorted(normalized)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
