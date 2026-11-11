from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounting_api import MoneyForwardAccountingClient
from .db import Phase12Database
from .hash_utils import mask_identifier, stable_hash
from .journal_pattern_provenance import PAYROLL_TEXT_MARKERS, is_payroll_pattern
from .local_artifacts import require_ignored_untracked_data_path
from .transaction_journalize import (
    JOURNAL_CREATE_EXECUTION_CHANNEL,
    TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
    expected_api_action,
    transaction_journalize_binding_proof,
)


GENERIC_PAYROLL_SUB_ACCOUNTS = {
    "",
    "給与",
    "給料",
    "賞与",
    "従業員",
    "社員",
    "スタッフ",
    "給与一式",
    "従業員一同",
    "<employee_or_payroll_liability>",
}
STATUTORY_PAYROLL_LIABILITY_SUB_ACCOUNTS = {"社会保険料", "雇用保険", "労働保険"}


@dataclass(frozen=True)
class JournalWriteQueueSummary:
    status: str
    prepare_summary_json: Path
    output_json: Path
    input_count: int
    ready_count: int
    queued_count: int
    items: tuple[dict[str, Any], ...]

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "prepare_summary_json": str(self.prepare_summary_json),
            "output_json": str(self.output_json),
            "input_count": self.input_count,
            "ready_count": self.ready_count,
            "queued_count": self.queued_count,
            "items": list(self.items),
            "next_action": "create_journal_write_approval_records_for_queue",
            "registration_allowed": False,
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
            "raw_attachment_local_file_path_printed": False,
            "raw_attachment_local_file_path_stored_in_ignored_local_json": any(
                bool((item.get("attachment_handoff") or {}).get("local_file_path")) for item in self.items
            ),
        }

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "prepare_summary_json": str(self.prepare_summary_json),
            "output_json": str(self.output_json),
            "input_count": self.input_count,
            "ready_count": self.ready_count,
            "queued_count": self.queued_count,
            "items": [_redact_queue_item(item) for item in self.items],
            "next_action": "create_journal_write_approval_records_for_queue",
            "registration_allowed": False,
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
            "raw_attachment_local_file_path_printed": False,
        }


def build_journal_write_queue(
    *,
    prepare_summary_json: Path,
    output_json: Path,
    db_path: Path | None = None,
    require_attachment_handoff_ready: bool = False,
) -> JournalWriteQueueSummary:
    prepare_summary_json = require_ignored_untracked_data_path(
        prepare_summary_json, purpose="journal write queue prepare summary"
    )
    output_json = require_ignored_untracked_data_path(output_json, purpose="journal write queue output")
    payload = json.loads(prepare_summary_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("prepare summary must be an object")
    if _is_exact_batch_summary(payload):
        return _build_exact_batch_write_queue(
            payload=payload,
            prepare_summary_json=prepare_summary_json,
            output_json=output_json,
            db_path=db_path,
            require_attachment_handoff_ready=require_attachment_handoff_ready,
        )
    dry_run = payload.get("dry_run")
    if not isinstance(dry_run, dict):
        raise ValueError("prepare summary must contain dry_run")
    dry_items = dry_run.get("items")
    if not isinstance(dry_items, list):
        raise ValueError("prepare summary dry_run must contain items")
    batch_items_by_index = _load_batch_input_items(payload)
    ready_items = [item for item in dry_items if isinstance(item, dict) and item.get("registration_allowed") is True]
    db = Phase12Database(db_path)
    db.initialize()
    queued: list[dict[str, Any]] = []
    with db.connect() as conn:
        for index, item in enumerate(ready_items):
            idempotency_key = str(item.get("idempotency_key") or "")
            candidate_hash = str(item.get("candidate_hash") or "")
            dry_run_result_hash = str(item.get("dry_run_result_hash") or "")
            if not idempotency_key or not candidate_hash or not dry_run_result_hash:
                raise ValueError(f"ready item {index} is missing queue keys")
            row = conn.execute(
                """
                SELECT request_hash, status, source_file_id_hash
                FROM write_intents
                WHERE idempotency_key = ?
                  AND operation_type = 'journal_create'
                  AND candidate_hash = ?
                  AND dry_run_result_hash = ?
                """,
                (idempotency_key, candidate_hash, dry_run_result_hash),
            ).fetchone()
            if not row:
                raise ValueError(f"ready item {index} is missing journal_create write_intent")
            if row["status"] != "planned":
                raise ValueError(f"ready item {index} write_intent status is not planned")
            source_index = int(item.get("index", index))
            source_batch_item = batch_items_by_index.get(source_index, {})
            _validate_payroll_write_queue_item(
                source_item=source_batch_item,
                queue_item=item,
                item_index=source_index,
            )
            attachment_handoff = _build_attachment_handoff(
                source_batch_item.get("evidence") if isinstance(source_batch_item, dict) else None,
                source_file_id_hash=str(row["source_file_id_hash"] or ""),
            )
            if require_attachment_handoff_ready and attachment_handoff.get("voucher_prepare_ready") is not True:
                raise ValueError(
                    "ready item "
                    f"{source_index} is missing voucher attachment handoff: {attachment_handoff.get('status')}"
                )
            execution_channel = str(item.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
            api_action = str(item.get("expected_api_action") or expected_api_action(execution_channel))
            if api_action != expected_api_action(execution_channel):
                raise ValueError(f"ready item {source_index} execution endpoint does not match its channel")
            binding_proof = {
                "source_row_hash": "",
                "transaction_facts_hash": "",
                "connected_account_binding_hash": "",
                "connected_ledger_binding_hash": "",
            }
            if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
                binding_proof = transaction_journalize_binding_proof(item)
            queued.append(
                {
                    "index": source_index,
                    "idempotency_key": idempotency_key,
                    "candidate_hash": candidate_hash,
                    "dry_run_result_hash": dry_run_result_hash,
                    "request_hash": row["request_hash"],
                    "source_file_id_hash": row["source_file_id_hash"],
                    "operation_type": "journal_create",
                    "execution_channel": execution_channel,
                    "expected_api_action": api_action,
                    "write_request_json": str(item.get("write_request_json") or ""),
                    "write_intent_status": row["status"],
                    "approval_required": True,
                    "required_scope": "mfc/accounting/journal.write",
                    "approval_boundary": "explicit_user_approval_required_before_moneyforward_post",
                    "rollback_method": "delete_or_reverse_test_journal_requires_explicit_target_approval",
                    "registration_allowed": False,
                    **binding_proof,
                    "attachment_handoff": attachment_handoff,
                }
            )
    summary = JournalWriteQueueSummary(
        status="ready_for_approval" if queued else "empty",
        prepare_summary_json=prepare_summary_json,
        output_json=output_json,
        input_count=int(dry_run.get("input_count") or len(dry_items)),
        ready_count=int(dry_run.get("ready_count") or len(ready_items)),
        queued_count=len(queued),
        items=tuple(queued),
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary.to_local_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _is_exact_batch_summary(payload: dict[str, Any]) -> bool:
    items = payload.get("items")
    return (
        payload.get("status") == "planned"
        and isinstance(items, list)
        and bool(items)
        and all(isinstance(item, dict) and item.get("target_request_hash") for item in items)
    )


def _build_exact_batch_write_queue(
    *,
    payload: dict[str, Any],
    prepare_summary_json: Path,
    output_json: Path,
    db_path: Path | None,
    require_attachment_handoff_ready: bool,
) -> JournalWriteQueueSummary:
    exact_items = payload.get("items")
    if not isinstance(exact_items, list):
        raise ValueError("exact journal batch summary must contain items")
    db = Phase12Database(db_path)
    db.initialize()
    queued: list[dict[str, Any]] = []
    with db.connect() as conn:
        for index, item in enumerate(exact_items):
            if not isinstance(item, dict) or item.get("status") != "planned":
                raise ValueError(f"exact journal batch item {index} is not planned")
            idempotency_key = str(item.get("idempotency_key") or "")
            target_request_hash = str(item.get("target_request_hash") or "")
            source_file_hash = str(item.get("source_file_hash") or "")
            if not idempotency_key or not target_request_hash or not source_file_hash:
                raise ValueError(f"exact journal batch item {index} is missing queue keys")
            row = conn.execute(
                """
                SELECT candidate_hash, dry_run_result_hash, request_hash, status, source_file_id_hash
                FROM write_intents
                WHERE idempotency_key = ? AND operation_type = 'journal_create'
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"exact journal batch item {index} is missing journal_create write_intent")
            if row["status"] != "planned":
                raise ValueError(f"exact journal batch item {index} write_intent status is not planned")
            if str(row["request_hash"] or "") != target_request_hash:
                raise ValueError(f"exact journal batch item {index} request_hash does not match write_intent")
            journal_path = require_ignored_untracked_data_path(
                Path(str(item.get("journal_json") or "")), purpose="exact journal body for write queue"
            )
            evidence_path = require_ignored_untracked_data_path(
                Path(str(item.get("evidence_json") or "")), purpose="exact journal evidence for write queue"
            )
            if not journal_path.is_file() or not evidence_path.is_file():
                raise ValueError(f"exact journal batch item {index} replay artifacts are missing")
            journal_payload = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(journal_payload, dict):
                raise ValueError(f"exact journal batch item {index} journal body must be an object")
            _validate_payroll_write_queue_item(
                source_item=item,
                journal=journal_payload,
                item_index=index,
            )
            metadata = MoneyForwardAccountingClient("METADATA_ONLY_TOKEN_NOT_USED").create_journal_request_metadata(
                journal=journal_payload
            )
            if str(metadata["body_hash"]) != str(item.get("journal_hash") or ""):
                raise ValueError(f"exact journal batch item {index} journal body hash mismatch")
            handoff = item.get("attachment_handoff")
            if not isinstance(handoff, dict):
                raise ValueError(f"exact journal batch item {index} is missing attachment handoff")
            if str(handoff.get("file_hash") or "") != source_file_hash:
                raise ValueError(f"exact journal batch item {index} attachment file_hash mismatch")
            if require_attachment_handoff_ready and (
                handoff.get("status") != "ready" or handoff.get("voucher_prepare_ready") is not True
            ):
                raise ValueError(f"exact journal batch item {index} attachment handoff is not ready")
            queue_handoff = dict(handoff)
            queue_handoff["source_file_id_hash"] = str(row["source_file_id_hash"] or "")
            queued.append(
                {
                    "index": int(item.get("index", index)),
                    "idempotency_key": idempotency_key,
                    "candidate_hash": str(row["candidate_hash"] or ""),
                    "dry_run_result_hash": str(row["dry_run_result_hash"] or ""),
                    "request_hash": target_request_hash,
                    "source_file_id_hash": str(row["source_file_id_hash"] or ""),
                    "operation_type": "journal_create",
                    "write_intent_status": "planned",
                    "approval_required": True,
                    "required_scope": "mfc/accounting/journal.write",
                    "approval_boundary": "explicit_user_approval_required_before_moneyforward_post",
                    "rollback_method": "delete_or_reverse_test_journal_requires_explicit_target_approval",
                    "registration_allowed": False,
                    "journal_json": str(journal_path),
                    "evidence_json": str(evidence_path),
                    "attachment_handoff": queue_handoff,
                }
            )
    summary = JournalWriteQueueSummary(
        status="ready_for_approval" if queued else "empty",
        prepare_summary_json=prepare_summary_json,
        output_json=output_json,
        input_count=int(payload.get("input_count") or len(exact_items)),
        ready_count=int(payload.get("planned_count") or len(queued)),
        queued_count=len(queued),
        items=tuple(queued),
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary.to_local_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _validate_payroll_write_queue_item(
    *,
    source_item: dict[str, Any],
    item_index: int,
    queue_item: dict[str, Any] | None = None,
    journal: dict[str, Any] | None = None,
) -> None:
    candidate = source_item.get("candidate")
    candidate = candidate if isinstance(candidate, dict) else {}
    if not _looks_like_payroll_queue_item(source_item, candidate, queue_item, journal):
        return
    if journal is None and isinstance(source_item.get("journal"), dict):
        journal = source_item["journal"]

    values = _payroll_queue_values(candidate, journal)
    if any(value <= 0 for value in values):
        raise ValueError(f"payroll item {item_index} contains a non-positive journal value")

    contract = source_item.get("payroll_contract") or candidate.get("payroll_contract")
    liabilities = _payroll_liability_bindings(candidate, journal)
    if not liabilities and isinstance(contract, dict) and isinstance(contract.get("liabilities"), list):
        liabilities = [
            {
                "side": str(item.get("side") or ""),
                "account": str(item.get("account") or ""),
                "sub_account": str(item.get("sub_account") or ""),
                "value": int(item.get("value") or 0),
            }
            for item in contract["liabilities"]
            if isinstance(item, dict)
        ]
    if not liabilities or any(
        _is_generic_payroll_sub_account(str(item["sub_account"]))
        for item in liabilities
    ):
        raise ValueError(
            f"payroll item {item_index} uses a generic employee/payroll sub-account"
        )

    evidence = source_item.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    evidence_hash = str(
        source_item.get("payroll_evidence_hash")
        or evidence.get("file_hash")
        or ""
    ).strip()
    if not _verified_payroll_allocation_contract(
        contract,
        liabilities=liabilities,
        evidence_hash=evidence_hash,
        period=str(source_item.get("payroll_period") or candidate.get("payroll_period") or ""),
        payment_date=str(
            source_item.get("payment_date")
            or candidate.get("payment_date")
            or candidate.get("journal_date")
            or ""
        ),
        workflow_step=str(
            source_item.get("payroll_workflow_step")
            or candidate.get("payroll_workflow_step")
            or ""
        ),
    ):
        raise ValueError(
            f"payroll item {item_index} is missing verified employee allocation evidence"
        )


def _is_generic_payroll_sub_account(value: str) -> bool:
    normalized = value.strip()
    return (
        not normalized
        or normalized in GENERIC_PAYROLL_SUB_ACCOUNTS
        or any(
            marker in normalized
            for marker in ("給与", "給料", "賞与", "従業員", "社員一同", "スタッフ一同")
        )
    )


def _looks_like_payroll_queue_item(
    source_item: dict[str, Any],
    candidate: dict[str, Any],
    queue_item: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> bool:
    if candidate.get("source_type") == "social_insurance_reclassification":
        return False
    if is_payroll_pattern(candidate):
        return True
    searchable_values = [
        source_item.get("workflow_type"),
        source_item.get("source_type"),
        source_item.get("credit_owner"),
        candidate.get("source_type"),
        candidate.get("description"),
        candidate.get("vendor_name"),
    ]
    if queue_item:
        searchable_values.extend(
            [
                queue_item.get("source_event_type"),
                queue_item.get("description_key"),
                queue_item.get("matched_pattern_source"),
            ]
        )
    if journal:
        for branch in journal.get("branches") or []:
            if isinstance(branch, dict):
                searchable_values.append(branch.get("remark"))
    searchable = " ".join(str(value or "") for value in searchable_values).casefold()
    return any(marker.casefold() in searchable for marker in PAYROLL_TEXT_MARKERS)


def _payroll_queue_values(
    candidate: dict[str, Any],
    journal: dict[str, Any] | None,
) -> list[int]:
    values: list[int] = []
    amount = candidate.get("amount")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        values.append(int(amount))
    if journal:
        for branch in journal.get("branches") or []:
            if not isinstance(branch, dict):
                continue
            for side_name in ("debitor", "creditor"):
                side = branch.get(side_name)
                value = side.get("value") if isinstance(side, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append(int(value))
    return values


def _payroll_liability_bindings(
    candidate: dict[str, Any],
    journal: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    if journal:
        for branch in journal.get("branches") or []:
            if not isinstance(branch, dict):
                continue
            for side_name in ("debitor", "creditor"):
                side = branch.get(side_name)
                if not isinstance(side, dict):
                    continue
                account = str(
                    side.get("account_item_name")
                    or side.get("account_name")
                    or side.get("account")
                    or ""
                ).strip()
                if account not in {"未払費用", "未払金"}:
                    continue
                sub_account = str(
                    side.get("sub_account_name")
                    or side.get("sub_account")
                    or side.get("sub_account_id")
                    or ""
                ).strip()
                if account == "未払費用" and sub_account in STATUTORY_PAYROLL_LIABILITY_SUB_ACCOUNTS:
                    continue
                bindings.append(
                    {
                        "side": side_name,
                        "account": account,
                        "sub_account": sub_account,
                        "value": int(side.get("value") or 0),
                    }
                )
    else:
        amount = int(candidate.get("amount") or 0)
        for side_name in ("debit", "credit"):
            account = str(candidate.get(f"{side_name}_account") or "").strip()
            if account not in {"未払費用", "未払金"}:
                continue
            bindings.append(
                {
                    "side": side_name,
                    "account": account,
                    "sub_account": str(
                        candidate.get(f"{side_name}_sub_account") or ""
                    ).strip(),
                    "value": amount,
                }
            )
    return sorted(
        bindings,
        key=lambda item: (
            item["side"],
            item["account"],
            item["sub_account"],
            item["value"],
        ),
    )


def _payroll_allocation_binding_hash(
    *,
    evidence_hash: str,
    source_employee_count: int,
    allocated_employee_count: int,
    nonzero_liability_employee_count: int,
    period: str,
    payment_date: str,
    accrual_readback_hash: str,
    liabilities: list[dict[str, Any]],
) -> str:
    return stable_hash(
        {
            "schema": "payroll_allocation_binding_v1",
            "evidence_hash": evidence_hash,
            "source_employee_count": source_employee_count,
            "allocated_employee_count": allocated_employee_count,
            "nonzero_liability_employee_count": nonzero_liability_employee_count,
            "period": period,
            "payment_date": payment_date,
            "accrual_readback_hash": accrual_readback_hash,
            "liabilities": liabilities,
        }
    )


def _verified_payroll_allocation_contract(
    contract: Any,
    *,
    liabilities: list[dict[str, Any]],
    evidence_hash: str,
    period: str,
    payment_date: str,
    workflow_step: str,
) -> bool:
    if not isinstance(contract, dict) or contract.get("employee_allocation_verified") is not True:
        return False
    try:
        source_employee_count = int(
            contract.get("source_employee_count")
            or contract.get("employee_count")
            or 0
        )
        allocated_count = int(contract.get("allocated_employee_count") or 0)
        nonzero_count = int(
            contract.get("nonzero_liability_employee_count")
            or 0
        )
    except (TypeError, ValueError):
        return False
    allocation_hash = str(contract.get("allocation_hash") or "").removeprefix("sha256:")
    contract_evidence_hash = str(contract.get("evidence_hash") or "")
    normalized_evidence_hash = evidence_hash.removeprefix("sha256:")
    normalized_contract_evidence_hash = contract_evidence_hash.removeprefix("sha256:")
    liability_employee_count = len({item["sub_account"] for item in liabilities})
    accrual_readback_hash = str(contract.get("accrual_readback_hash") or "")
    if workflow_step == "settlement" and not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        accrual_readback_hash,
    ):
        return False
    expected_hash = _payroll_allocation_binding_hash(
        evidence_hash=f"sha256:{normalized_evidence_hash}",
        source_employee_count=source_employee_count,
        allocated_employee_count=allocated_count,
        nonzero_liability_employee_count=nonzero_count,
        period=period,
        payment_date=payment_date,
        accrual_readback_hash=accrual_readback_hash,
        liabilities=liabilities,
    )
    return (
        source_employee_count > 0
        and allocated_count == source_employee_count
        and nonzero_count > 0
        and liability_employee_count == nonzero_count
        and bool(period)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", payment_date) is not None
        and workflow_step in {"accrual", "settlement"}
        and re.fullmatch(r"[0-9a-f]{64}", normalized_evidence_hash) is not None
        and normalized_contract_evidence_hash == normalized_evidence_hash
        and re.fullmatch(r"[0-9a-f]{64}", allocation_hash) is not None
        and allocation_hash == expected_hash
    )


def _load_batch_input_items(prepare_payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    input_path_value = str(prepare_payload.get("input_json") or "")
    if not input_path_value:
        return {}
    input_path = require_ignored_untracked_data_path(Path(input_path_value), purpose="journal batch input for queue handoff")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("journal batch input must contain items")
    return {index: item for index, item in enumerate(items) if isinstance(item, dict)}


def _build_attachment_handoff(evidence: Any, *, source_file_id_hash: str) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {
            "status": "missing_evidence",
            "source_file_id_hash": source_file_id_hash,
            "voucher_prepare_ready": False,
        }
    local_file_path = str(evidence.get("local_file_path") or "").strip()
    local_file_exists = Path(local_file_path).is_file() if local_file_path else False
    return {
        "status": "ready" if local_file_exists else "missing_local_file_path",
        "file_name": str(evidence.get("file_name") or ""),
        "file_hash": str(evidence.get("file_hash") or ""),
        "source_file_id_hash": source_file_id_hash,
        "parent_folder_id_hash": mask_identifier(str(evidence.get("parent_folder_id") or "")),
        "parent_folder_name": str(evidence.get("parent_folder_name") or ""),
        "local_file_path": local_file_path,
        "local_file_path_present": bool(local_file_path),
        "local_file_exists": local_file_exists,
        "voucher_prepare_ready": local_file_exists,
        "next_action": (
            "run_phase12_voucher_attach_prepare_after_journal_post"
            if local_file_exists
            else "provide_local_file_path_before_voucher_prepare"
        ),
    }


def _redact_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(item)
    handoff = redacted.get("attachment_handoff")
    if isinstance(handoff, dict):
        redacted["attachment_handoff"] = _redact_attachment_handoff(handoff)
    return redacted


def _redact_attachment_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    local_file_path = str(handoff.get("local_file_path") or "")
    redacted = dict(handoff)
    redacted.pop("local_file_path", None)
    redacted["local_file_path_hash"] = stable_hash({"local_file_path": local_file_path}) if local_file_path else ""
    redacted["raw_local_file_path_printed"] = False
    return redacted
