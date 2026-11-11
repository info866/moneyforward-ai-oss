from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .daily_accounting_control import POST_AUDIT_ORDER
from .hash_utils import mask_identifier, stable_hash
from .journal_backfill import _journal_id
from .journal_duplicate_probe import _journal_details_for_date
from .journal_write_verify import (
    _align_expected_to_actual,
    _normalize_expected_journal,
    _normalize_read_back,
)
from .local_artifacts import require_ignored_untracked_data_path
from .same_chain_status import build_same_chain_status


class DailyPostAuditReadClient(Protocol):
    def journals(
        self,
        office_id: str | None = None,
        *,
        start_date: str,
        end_date: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict: ...

    def journal(self, journal_id: str, office_id: str | None = None) -> dict: ...


def run_daily_post_audit(
    *,
    client: DailyPostAuditReadClient,
    daily_inventory_json: Path,
    batch_summary_json: Path,
    voucher_queue_json: Path,
    full_manifest_json: Path,
    output_json: Path,
    updated_inventory_json: Path,
    db_path: Path,
    office_id: str | None = None,
) -> dict[str, Any]:
    daily_inventory_json = require_ignored_untracked_data_path(
        daily_inventory_json, purpose="daily post-audit inventory input"
    )
    batch_summary_json = require_ignored_untracked_data_path(
        batch_summary_json, purpose="daily post-audit journal batch input"
    )
    voucher_queue_json = require_ignored_untracked_data_path(
        voucher_queue_json, purpose="daily post-audit voucher queue input"
    )
    full_manifest_json = require_ignored_untracked_data_path(
        full_manifest_json, purpose="daily post-audit full manifest input"
    )
    output_json = require_ignored_untracked_data_path(output_json, purpose="daily post-audit output")
    updated_inventory_json = require_ignored_untracked_data_path(
        updated_inventory_json, purpose="daily post-audit updated inventory"
    )
    inventory = _load_object(daily_inventory_json)
    batch = _load_object(batch_summary_json)
    queue = _load_object(voucher_queue_json)
    manifest = _load_object(full_manifest_json)
    accounting_date = str(inventory.get("accounting_date") or "")
    batch_items = _items(batch, "journal batch")
    queue_items = _items(queue, "voucher queue")
    zero_activity_verified = (
        not batch_items
        and not queue_items
        and inventory.get("zero_activity_verified") is True
    )
    queue_by_idempotency = {
        str(item.get("idempotency_key") or ""): item
        for item in queue_items
        if isinstance(item, dict)
    }

    journal_inputs: list[dict[str, Any]] = []
    for item in batch_items:
        if not isinstance(item, dict):
            raise ValueError("journal batch item must be an object")
        idempotency_key = str(item.get("idempotency_key") or "")
        queue_item = queue_by_idempotency.get(idempotency_key)
        if not queue_item:
            raise ValueError("daily post-audit is missing a voucher queue item")
        journal_path = require_ignored_untracked_data_path(
            Path(str(item.get("journal_json") or "")), purpose="daily post-audit journal body"
        )
        verification_path = require_ignored_untracked_data_path(
            Path(str(queue_item.get("journal_verification_json") or "")),
            purpose="daily post-audit journal verification",
        )
        journal = _load_object(journal_path)
        verification = _load_object(verification_path)
        journal_inputs.append(
            {
                "idempotency_key": idempotency_key,
                "source_file_hash": str(item.get("source_file_hash") or ""),
                "journal": journal,
                "journal_id_hash": str(queue_item.get("journal_id_hash") or ""),
                "verification": verification,
                "processed_readback_required": queue_item.get(
                    "processed_readback_required", True
                )
                is not False,
            }
        )

    details_by_date = {
        journal_date: _journal_details_for_date(client, journal_date, office_id=office_id)
        for journal_date in sorted(
            {str(item["journal"].get("transaction_date") or "")[:10] for item in journal_inputs}
        )
    }
    journal_checks: list[dict[str, Any]] = []
    for item in journal_inputs:
        verification = item["verification"]
        allow_missing_tax_id = (
            verification.get("verified") is True
            and verification.get("tax_readback_verified") is True
            and verification.get("tax_method") == "FREE"
        )
        expected = _normalize_expected_journal(item["journal"])
        details = details_by_date[expected["transaction_date"]]
        exact_hashes: list[str] = []
        for detail in details:
            actual = _normalize_read_back(detail)
            if (
                _align_expected_to_actual(
                    expected,
                    actual,
                    allow_missing_tax_id=allow_missing_tax_id,
                )
                == actual
            ):
                raw_id = _journal_id(detail)
                if raw_id:
                    exact_hashes.append(mask_identifier(raw_id))
        body_verified = (
            verification.get("verified") is True
            and verification.get("expected_hash") == verification.get("actual_hash")
            and verification.get("journal_id_hash") == item["journal_id_hash"]
        )
        journal_checks.append(
            {
                "item_hash": stable_hash(
                    {
                        "idempotency_key": item["idempotency_key"],
                        "source_file_hash": item["source_file_hash"],
                    }
                ),
                "exact_match_count": len(exact_hashes),
                "expected_journal_present": item["journal_id_hash"] in exact_hashes,
                "body_readback_verified": body_verified,
            }
        )

    batch_count = len(journal_checks)
    exact_once_count = sum(
        check["exact_match_count"] == 1 and check["expected_journal_present"]
        for check in journal_checks
    )
    body_verified_count = sum(check["body_readback_verified"] for check in journal_checks)
    same_chain = build_same_chain_status(db_path).to_redacted_dict()
    chain_by_idempotency = {chain["idempotency_key"]: chain for chain in same_chain["chains"]}
    processed_count = sum(
        (
            item["verification"].get("verified") is True
            if not item["processed_readback_required"]
            else bool(
                chain_by_idempotency.get(item["idempotency_key"], {}).get(
                    "same_chain_ready"
                )
            )
        )
        for item in journal_inputs
    )

    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise ValueError("daily post-audit full manifest must contain files")
    posted_file_hashes = {item["source_file_hash"] for item in journal_inputs}
    manifest_file_hashes = {
        str(item.get("file_hash") or "") for item in manifest_files if isinstance(item, dict)
    }
    unreconciled_evidence_count = len(manifest_file_hashes - posted_file_hashes)
    card_source = ((inventory.get("sources") or {}).get("card") or {})
    card_evidence = card_source.get("evidence") if isinstance(card_source, dict) else None
    unposted_card_count = int((card_evidence or {}).get("item_count") or 0)
    missing_readback_count = int(inventory.get("missing_journal_readback_count") or 0)
    journal_owner_conflict_count = batch_count - len(
        {item["journal_id_hash"] for item in journal_inputs}
    )
    source_owner_conflict_count = batch_count - len(
        {item["source_file_hash"] for item in journal_inputs}
    )

    results = {
        "duplicate_journal_check": _result(
            passed=(batch_count > 0 or zero_activity_verified)
            and exact_once_count == batch_count
            and missing_readback_count == 0
            and journal_owner_conflict_count == 0
            and source_owner_conflict_count == 0,
            batch_item_count=batch_count,
            exact_once_count=exact_once_count,
            ambiguous_or_missing_count=batch_count - exact_once_count,
            missing_readback_count=missing_readback_count,
            journal_owner_conflict_count=journal_owner_conflict_count,
            source_owner_conflict_count=source_owner_conflict_count,
        ),
        "debit_credit_amount_check": _result(
            passed=(batch_count > 0 or zero_activity_verified)
            and body_verified_count == batch_count
            and missing_readback_count == 0,
            batch_item_count=batch_count,
            exact_body_readback_count=body_verified_count,
            mismatch_count=batch_count - body_verified_count,
        ),
        "account_sub_account_tax_check": _result(
            passed=(batch_count > 0 or zero_activity_verified)
            and body_verified_count == batch_count
            and missing_readback_count == 0,
            batch_item_count=batch_count,
            exact_body_readback_count=body_verified_count,
            mismatch_count=batch_count - body_verified_count,
        ),
        "source_statement_evidence_reconciliation": _result(
            passed=unreconciled_evidence_count == 0 and unposted_card_count == 0,
            manifest_file_count=len(manifest_file_hashes),
            posted_evidence_count=len(posted_file_hashes),
            unreconciled_evidence_count=unreconciled_evidence_count,
            unposted_card_count=unposted_card_count,
        ),
        "processed_folder_readback": _result(
            passed=(batch_count > 0 or zero_activity_verified)
            and processed_count == batch_count,
            batch_item_count=batch_count,
            same_chain_ready_count=processed_count,
            incomplete_count=batch_count - processed_count,
        ),
    }
    payload = {
        "status": "ready" if all(result["passed"] for result in results.values()) else "needs_attention",
        "accounting_date": accounting_date,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "batch_item_count": batch_count,
        "results": results,
        "journal_checks": journal_checks,
        "moneyforward_read_called": True,
        "moneyforward_write_called": False,
        "google_drive_write_called": False,
        "google_chat_called": False,
        "raw_amounts_stored": False,
        "raw_descriptions_stored": False,
        "raw_journal_ids_stored": False,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifact_hash = "sha256:" + hashlib.sha256(output_json.read_bytes()).hexdigest()
    updated_inventory = json.loads(json.dumps(inventory))
    updated_inventory["post_audit"] = {
        key: {
            "state": value["state"],
            "evidence": {
                "artifact_path": str(output_json),
                "artifact_hash": artifact_hash,
                "result_hash": value["result_hash"],
            },
        }
        for key, value in results.items()
    }
    updated_inventory_json.parent.mkdir(parents=True, exist_ok=True)
    updated_inventory_json.write_text(
        json.dumps(updated_inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def redacted_daily_post_audit_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload["status"],
        "accounting_date": payload["accounting_date"],
        "batch_item_count": payload["batch_item_count"],
        "result_states": {key: value["state"] for key, value in payload["results"].items()},
        "result_counts": {
            key: {count_key: count_value for count_key, count_value in value.items() if count_key.endswith("_count")}
            for key, value in payload["results"].items()
        },
        "moneyforward_read_called": True,
        "moneyforward_write_called": False,
        "raw_data_printed": False,
    }


def _result(*, passed: bool, **counts: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": "verified" if passed else "failed",
        "passed": passed,
        **counts,
    }
    payload["result_hash"] = "sha256:" + stable_hash(payload)
    return payload


def _items(payload: dict[str, Any], label: str) -> list[Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{label} must contain an items array")
    return items


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain an object")
    return payload
