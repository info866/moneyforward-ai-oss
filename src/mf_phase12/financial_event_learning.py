from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .financial_event_classification import normalize_financial_event
from .hash_utils import stable_hash
from .journal_pattern_provenance import FINANCIAL_LEARNING_SOURCE, record_journal_pattern_provenance
from .local_artifacts import require_ignored_untracked_data_path
from .rag import build_rag_cards, upsert_sync_manifest


REQUIRED_CANDIDATE_FIELDS = ("debit_account", "credit_account", "tax_category")


@dataclass(frozen=True)
class FinancialEventLearningItem:
    learning_hash: str
    event_hash: str
    pattern_hash: str
    source_event_type: str
    cash_flow_direction: str
    transaction_date: str
    description_key: str
    amount_bucket: str
    auto_post_eligibility: str

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "learning_hash": self.learning_hash,
            "event_hash": self.event_hash,
            "pattern_hash": self.pattern_hash,
            "source_event_type": self.source_event_type,
            "cash_flow_direction": self.cash_flow_direction,
            "transaction_date": self.transaction_date,
            "description_key": self.description_key,
            "amount_bucket": self.amount_bucket,
            "auto_post_eligibility": self.auto_post_eligibility,
        }


@dataclass(frozen=True)
class FinancialEventLearningBatch:
    items: list[FinancialEventLearningItem]
    rag_cards_upserted: int
    persisted: bool

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "learned_count": len(self.items),
            "rag_cards_upserted": self.rag_cards_upserted,
            "persisted": self.persisted,
            "items": [item.to_redacted_dict() for item in self.items],
            "raw_event_ids_printed": False,
            "raw_amounts_printed": False,
            "raw_secret_stored": False,
        }


def learn_financial_events_from_file(
    *,
    learning_json: Path,
    output_json: Path,
    conn: sqlite3.Connection,
    persist: bool = True,
) -> FinancialEventLearningBatch:
    learning_json = require_ignored_untracked_data_path(learning_json, purpose="financial event learning input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="financial event learning output")
    payload = json.loads(learning_json.read_text(encoding="utf-8"))
    batch = learn_financial_events(payload, conn=conn, persist=persist)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(batch.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return batch


def learn_financial_events(
    payload: Any,
    *,
    conn: sqlite3.Connection,
    persist: bool = True,
) -> FinancialEventLearningBatch:
    _ensure_tables(conn)
    source_items = _extract_learning_items(payload)
    learned = [_learn_one(conn, item, persist=persist) for item in source_items]
    rag_cards = _materialize_learning_rag(conn, learned) if persist else 0
    if persist:
        conn.commit()
    return FinancialEventLearningBatch(items=learned, rag_cards_upserted=rag_cards, persisted=persist)


def _learn_one(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    persist: bool,
) -> FinancialEventLearningItem:
    event = item.get("event") or item.get("financial_event") or item.get("transaction")
    if not isinstance(event, dict):
        raise ValueError("each learning item requires event")
    candidate = item.get("resolved_candidate") or item.get("candidate") or item.get("journal_candidate")
    if not isinstance(candidate, dict):
        raise ValueError("each learning item requires resolved_candidate")
    missing = [field for field in REQUIRED_CANDIDATE_FIELDS if not str(candidate.get(field) or "").strip()]
    if missing:
        raise ValueError(f"resolved_candidate missing required fields: {', '.join(missing)}")

    normalized = normalize_financial_event(event)
    broad_content = str(event.get("broad_content") or "").strip()
    if broad_content and normalized["source_event_type"] in {"receipt", "received_invoice"}:
        broad_normalized = normalize_financial_event(
            {**event, "description": broad_content}
        )
        normalized = {
            **normalized,
            "description_key": broad_normalized["description_key"],
        }
    auto_post_eligibility = _auto_post_eligibility(item, normalized)
    pattern_hash = stable_hash(
        {
            "source": "financial_event_learning",
            "vendor_key": normalized["vendor_key"],
            "description_key": normalized["description_key"],
            "amount_bucket": normalized["amount_bucket"],
            "cash_flow_direction": normalized["cash_flow_direction"],
            "debit_account": str(candidate["debit_account"]).strip(),
            "debit_sub_account": _optional_text(candidate.get("debit_sub_account")),
            "credit_account": str(candidate["credit_account"]).strip(),
            "credit_sub_account": _optional_text(candidate.get("credit_sub_account")),
            "tax_category": str(candidate["tax_category"]).strip(),
        }
    )
    learning_hash = stable_hash(
        {
            "event_hash": normalized["event_hash"],
            "pattern_hash": pattern_hash,
            "learning_source": str(item.get("learning_source") or "user_corrected_financial_event"),
        }
    )
    learned = FinancialEventLearningItem(
        learning_hash=learning_hash,
        event_hash=normalized["event_hash"],
        pattern_hash=pattern_hash,
        source_event_type=normalized["source_event_type"],
        cash_flow_direction=normalized["cash_flow_direction"],
        transaction_date=normalized["transaction_date"],
        description_key=normalized["description_key"],
        amount_bucket=normalized["amount_bucket"],
        auto_post_eligibility=auto_post_eligibility,
    )
    if persist:
        already_learned = conn.execute(
            "SELECT 1 FROM financial_event_learning_events WHERE learning_hash=?",
            (learning_hash,),
        ).fetchone()
        if already_learned is None:
            _upsert_journal_pattern(
                conn, normalized, candidate, pattern_hash, auto_post_eligibility
            )
            record_journal_pattern_provenance(
                conn,
                pattern_hash=pattern_hash,
                source_kind=FINANCIAL_LEARNING_SOURCE,
                source_ref_hash=learning_hash,
            )
        _upsert_learning_event(conn, learned, item)
    return learned


def _upsert_journal_pattern(
    conn: sqlite3.Connection,
    event: dict[str, str],
    candidate: dict[str, Any],
    pattern_hash: str,
    auto_post_eligibility: str,
) -> None:
    conn.execute(
        """
        INSERT INTO journal_patterns (
            pattern_hash, vendor_key, description_key, amount_bucket,
            debit_account, debit_sub_account, credit_account, credit_sub_account,
            tax_category, success_count, auto_post_eligibility, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(pattern_hash) DO UPDATE SET
            success_count = journal_patterns.success_count + 1,
            failure_count = 0,
            auto_post_eligibility = CASE journal_patterns.auto_post_eligibility
                WHEN 'enabled' THEN 'enabled'
                ELSE excluded.auto_post_eligibility
            END,
            last_seen_at = excluded.last_seen_at
        """,
        (
            pattern_hash,
            event["vendor_key"],
            event["description_key"],
            event["amount_bucket"],
            str(candidate["debit_account"]).strip(),
            _optional_text(candidate.get("debit_sub_account")),
            str(candidate["credit_account"]).strip(),
            _optional_text(candidate.get("credit_sub_account")),
            str(candidate["tax_category"]).strip(),
            auto_post_eligibility,
            event["transaction_date"],
        ),
    )


def _upsert_learning_event(
    conn: sqlite3.Connection,
    learned: FinancialEventLearningItem,
    item: dict[str, Any],
) -> None:
    learning_source = str(item.get("learning_source") or "user_corrected_financial_event")
    summary = " | ".join(
        [
            "source=user_corrected_financial_event",
            f"event={learned.event_hash}",
            f"pattern={learned.pattern_hash}",
            f"eligibility={learned.auto_post_eligibility}",
        ]
    )
    conn.execute(
        """
        INSERT INTO financial_event_learning_events (
            learning_hash, event_hash, pattern_hash, source_event_type,
            cash_flow_direction, learning_source, result_summary_redacted
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(learning_hash) DO UPDATE SET
            result_summary_redacted = excluded.result_summary_redacted,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (
            learned.learning_hash,
            learned.event_hash,
            learned.pattern_hash,
            learned.source_event_type,
            learned.cash_flow_direction,
            learning_source[:80],
            summary,
        ),
    )


def _materialize_learning_rag(conn: sqlite3.Connection, learned: list[FinancialEventLearningItem]) -> int:
    cards = [
        {
            "card_type": "bank_transaction_pattern",
            "title": f"learned {item.source_event_type} {item.description_key}"[:120],
            "body": " | ".join(
                [
                    "source=financial_event_learning",
                    f"description={item.description_key}",
                    f"amount_bucket={item.amount_bucket}",
                    f"direction={item.cash_flow_direction}",
                    f"eligibility={item.auto_post_eligibility}",
                    f"pattern={item.pattern_hash}",
                ]
            ),
        }
        for item in learned
    ]
    if not cards:
        return 0
    source_hash = upsert_sync_manifest(conn, "local", "financial_event_learning", cards)
    return build_rag_cards(conn, cards, source_hash)


def _extract_learning_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("learning_items", "items", "events", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _auto_post_eligibility(item: dict[str, Any], event: dict[str, str]) -> str:
    requested = str(item.get("auto_post_eligibility") or "").strip()
    if requested in {"limited", "enabled", "shadow", "disabled"}:
        return requested
    if event["cash_flow_direction"] == "outflow":
        return "limited"
    return "shadow"


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_event_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learning_hash TEXT NOT NULL UNIQUE,
            event_hash TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            source_event_type TEXT NOT NULL,
            cash_flow_direction TEXT NOT NULL
                CHECK (cash_flow_direction IN ('outflow', 'inflow', 'unknown')),
            learning_source TEXT NOT NULL,
            result_summary_redacted TEXT NOT NULL,
            raw_event_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_event_id_stored = 0),
            raw_amount_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amount_stored = 0),
            raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY(pattern_hash) REFERENCES journal_patterns(pattern_hash)
        )
        """
    )
