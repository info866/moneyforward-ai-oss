from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hash_utils import stable_hash
from .local_artifacts import require_ignored_untracked_data_path


FINANCIAL_CLASSIFICATION_QUEUE_ACTIONS = {"queue_for_learning", "queue_for_review"}
PROVENANCE_ONLY_ACTIONS = {"ai_proposed_candidate"}


@dataclass(frozen=True)
class EventQueueItem:
    queue_hash: str
    source_kind: str
    source_hash: str
    recommended_action: str
    priority: str
    reason_codes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_hash": self.queue_hash,
            "source_kind": self.source_kind,
            "source_hash": self.source_hash,
            "recommended_action": self.recommended_action,
            "priority": self.priority,
            "reason_codes": self.reason_codes,
        }


@dataclass(frozen=True)
class EventQueueBuildResult:
    items: list[EventQueueItem]
    output_json: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "item_count": len(self.items),
            "priority_counts": _counts(item.priority for item in self.items),
            "action_counts": _counts(item.recommended_action for item in self.items),
            "output_json": str(self.output_json),
            "items": [item.to_dict() for item in self.items],
            "raw_ids_printed": False,
            "raw_amounts_printed": False,
            "raw_secret_stored": False,
        }


def build_event_queue_from_files(
    *,
    output_json: Path,
    financial_classification_json: Path | None = None,
    financial_reconcile_json: Path | None = None,
    invoice_accounting_link_json: Path | None = None,
    drive_evidence_json: Path | None = None,
    gmail_drive_upload_request_json: Path | None = None,
    human_decisions_json: Path | None = None,
) -> EventQueueBuildResult:
    output_json = require_ignored_untracked_data_path(output_json, purpose="event queue output")
    items: list[EventQueueItem] = []
    if financial_classification_json:
        path = require_ignored_untracked_data_path(
            financial_classification_json,
            purpose="financial classification input",
        )
        items.extend(_items_from_financial_classification(json.loads(path.read_text(encoding="utf-8"))))
    if financial_reconcile_json:
        path = require_ignored_untracked_data_path(financial_reconcile_json, purpose="financial reconcile input")
        items.extend(_items_from_financial_reconcile(json.loads(path.read_text(encoding="utf-8"))))
    if invoice_accounting_link_json:
        path = require_ignored_untracked_data_path(
            invoice_accounting_link_json,
            purpose="invoice accounting link input",
        )
        items.extend(_items_from_invoice_accounting_link(json.loads(path.read_text(encoding="utf-8"))))
    if drive_evidence_json:
        path = require_ignored_untracked_data_path(drive_evidence_json, purpose="drive evidence input")
        items.extend(_items_from_drive_evidence(json.loads(path.read_text(encoding="utf-8"))))
    if gmail_drive_upload_request_json:
        path = require_ignored_untracked_data_path(
            gmail_drive_upload_request_json,
            purpose="Gmail Drive upload request input",
        )
        items.extend(_items_from_gmail_drive_upload_requests(json.loads(path.read_text(encoding="utf-8"))))
    excluded_source_hashes = _excluded_source_hashes_from_decisions(human_decisions_json)
    deduped = _dedupe([item for item in items if item.source_hash not in excluded_source_hashes])
    output_json.parent.mkdir(parents=True, exist_ok=True)
    result = EventQueueBuildResult(items=deduped, output_json=output_json)
    output_json.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _excluded_source_hashes_from_decisions(path: Path | None) -> set[str]:
    if path is None:
        return set()
    checked = require_ignored_untracked_data_path(path, purpose="event queue human decisions input")
    payload = json.loads(checked.read_text(encoding="utf-8"))
    excluded: set[str] = set()
    for item in _extract_items(payload):
        source_hash = str(item.get("source_hash") or "").strip()
        if not source_hash:
            continue
        reason_text = " ".join(_string_list(item.get("reason")) + _string_list(item.get("reason_codes")))
        explicit_exclusion = item.get("exclude_from_queue") is True or item.get("exclude_from_registration") is True
        duplicate_exclusion = "duplicate" in reason_text or "重複" in reason_text
        returned = str(item.get("action") or item.get("decision") or "") in {"差戻し", "return", "exclude", "除外"}
        if explicit_exclusion or (returned and duplicate_exclusion):
            excluded.add(source_hash)
    return excluded


def _items_from_financial_classification(payload: dict[str, Any]) -> list[EventQueueItem]:
    output: list[EventQueueItem] = []
    for item in _extract_items(payload):
        action = str(item.get("recommended_action") or "")
        if action == "auto_post_candidate":
            continue
        action, action_reasons = _financial_classification_queue_action(action)
        source_hash = str(item.get("event_hash") or stable_hash(item))
        reasons = (
            action_reasons
            + (_string_list(item.get("unresolved_points")) or [str(item.get("learning_action") or "financial_classification_review")])
        )
        output.append(
            _queue_item(
                source_kind="financial_classification",
                source_hash=source_hash,
                recommended_action=action,
                priority="high" if action == "queue_for_learning" else "normal",
                reason_codes=reasons,
            )
        )
    return output


def _items_from_financial_reconcile(payload: dict[str, Any]) -> list[EventQueueItem]:
    output: list[EventQueueItem] = []
    for item in _extract_items(payload):
        action = str(item.get("recommended_action") or "")
        if item.get("counterpart_hash") and action == "use_counterpart_context_for_classification":
            continue
        source_hash = str(item.get("event_hash") or stable_hash(item))
        output.append(
            _queue_item(
                source_kind="financial_reconcile",
                source_hash=source_hash,
                recommended_action=action or "queue_for_review",
                priority="high" if str(item.get("reconciliation_status") or "") in {"金額不一致", "相手先不明", "要確認"} else "normal",
                reason_codes=_string_list(item.get("unresolved_points")) or ["no_invoice_or_evidence_match"],
            )
        )
    return output


def _items_from_invoice_accounting_link(payload: dict[str, Any]) -> list[EventQueueItem]:
    output: list[EventQueueItem] = []
    for item in _extract_items(payload):
        status = str(item.get("settlement_status") or "")
        action = str(item.get("settlement_action") or "")
        sales_accrual_observed = "sales_accrual_match_count" in item or "sales_accrual_journals" in item
        sales_accrual_missing = sales_accrual_observed and int(item.get("sales_accrual_match_count") or 0) == 0
        if status == "入金済み" and action == "settled" and not sales_accrual_missing:
            continue
        source_hash = str(item.get("billing_ref_hash") or item.get("journal_ref_hash") or stable_hash(item))
        reasons = _string_list(item.get("settlement_reasons")) or [f"settlement_status={status or 'unknown'}"]
        if str(item.get("credit_account") or "") == "売掛金" or str(item.get("debit_account") or "") == "売掛金":
            reasons = ["nonstandard_accounts_receivable_trade_used"] + reasons
        queue_action = _settlement_queue_action(action)
        source_kind = "invoice_accounting_settlement"
        priority = "high" if status in {"金額不一致", "相手先不明", "要確認"} else "normal"
        if sales_accrual_missing:
            source_kind = "invoice_accounting_sales_accrual"
            queue_action = "queue_for_review"
            priority = "high"
            reasons = ["sales_accrual_missing"] + reasons
        output.append(
            _queue_item(
                source_kind=source_kind,
                source_hash=source_hash,
                recommended_action=queue_action,
                priority=priority,
                reason_codes=reasons,
            )
        )
    return output


def _items_from_drive_evidence(payload: dict[str, Any]) -> list[EventQueueItem]:
    output: list[EventQueueItem] = []
    for item in _extract_items(payload):
        status = str(item.get("status") or item.get("processing_status") or item.get("suggested_status") or "")
        if status in {"verified", "moved", "processed", "done"}:
            continue
        source_hash = str(item.get("file_hash") or item.get("source_file_hash") or stable_hash(item))
        evidence_type = str(item.get("evidence_type") or item.get("suggested_source_type") or item.get("source_type") or "evidence")
        output.append(
            _queue_item(
                source_kind=f"drive_{evidence_type}",
                source_hash=source_hash,
                recommended_action="prepare_evidence_classification",
                priority="high" if evidence_type == "invoice" else "normal",
                reason_codes=["drive_evidence_unprocessed"],
            )
        )
    return output


def _items_from_gmail_drive_upload_requests(payload: dict[str, Any]) -> list[EventQueueItem]:
    output: list[EventQueueItem] = []
    for item in _extract_items(payload):
        status = str(item.get("status") or item.get("processing_status") or "")
        if status in {"uploaded", "processed", "done"}:
            continue
        source_hash = str(item.get("file_hash") or item.get("attachment_hash") or stable_hash(item))
        output.append(
            _queue_item(
                source_kind="gmail_drive_upload_request",
                source_hash=source_hash,
                recommended_action="upload_gmail_attachment_to_drive",
                priority="high",
                reason_codes=["gmail_attachment_not_yet_in_drive"],
            )
        )
    return output


def _queue_item(
    *,
    source_kind: str,
    source_hash: str,
    recommended_action: str,
    priority: str,
    reason_codes: list[str],
) -> EventQueueItem:
    queue_hash = stable_hash(
        {
            "source_kind": source_kind,
            "source_hash": source_hash,
            "recommended_action": recommended_action,
            "reason_codes": reason_codes,
        }
    )
    return EventQueueItem(
        queue_hash=queue_hash,
        source_kind=source_kind,
        source_hash=source_hash,
        recommended_action=recommended_action,
        priority=priority,
        reason_codes=reason_codes,
    )


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("matches"), list):
            return [item for item in payload["matches"] if isinstance(item, dict)]
        for key in ("items", "events", "transactions", "data", "evidence", "queue"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _dedupe(items: list[EventQueueItem]) -> list[EventQueueItem]:
    seen: set[str] = set()
    output: list[EventQueueItem] = []
    for item in items:
        if item.queue_hash in seen:
            continue
        seen.add(item.queue_hash)
        output.append(item)
    return output


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _settlement_queue_action(action: str) -> str:
    if action in {"queue_for_fee_confirmation", "queue_for_withholding_confirmation"}:
        return action
    if action == "queue_for_followup":
        return "queue_for_followup"
    return "queue_for_review"


def _financial_classification_queue_action(action: str) -> tuple[str, list[str]]:
    if action in FINANCIAL_CLASSIFICATION_QUEUE_ACTIONS:
        return action, []
    if action in PROVENANCE_ONLY_ACTIONS:
        return "queue_for_review", ["ai_proposed_candidate_requires_recommended_action_resolution"]
    if not action:
        return "queue_for_review", []
    return "queue_for_review", [f"unsupported_financial_classification_action={action}"]
