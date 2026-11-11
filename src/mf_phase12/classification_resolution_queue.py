from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .hash_utils import stable_hash
from .local_artifacts import require_ignored_untracked_data_path


@dataclass(frozen=True)
class ClassificationResolutionQueueSummary:
    run_hash: str
    queued_count: int
    by_tag: dict[str, int]
    by_state: dict[str, int]
    correction_review_count: int
    evidence_collection_count: int
    tracked_count: int
    active_count: int
    stale_active_count: int
    missing_tracking_count: int
    metadata_missing_count: int
    duplicate_tracking_count: int
    overdue_count: int
    managed_queue_ready: bool
    raw_data_stored: bool = False
    moneyforward_api_called: bool = False
    google_drive_write_called: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_classification_resolution_queue(
    conn: sqlite3.Connection,
    *,
    persist: bool = True,
) -> ClassificationResolutionQueueSummary:
    run = _latest_learned_evaluation(conn)
    if not run:
        raise ValueError("no learned_rules_first classification evaluation is available")
    run_hash = str(run["run_hash"])
    rows = conn.execute(
        """
        SELECT i.source_pattern_hash, i.is_classifiable, i.account_match,
               i.sub_account_match, i.tax_match,
               COALESCE((
                   SELECT COUNT(*)
                   FROM journal_pattern_occurrences occurrence
                   WHERE occurrence.pattern_hash = i.source_pattern_hash
               ), 0) AS occurrence_count
        FROM classification_evaluation_items i
        WHERE i.run_hash = ?
          AND i.auto_post_candidate = 0
        ORDER BY i.source_pattern_hash
        """,
        (run_hash,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    items = [_resolution_item(dict(row), now=now) for row in rows]
    if persist:
        current_pattern_hashes = {item["source_pattern_hash"] for item in items}
        if current_pattern_hashes:
            placeholders = ", ".join("?" for _ in current_pattern_hashes)
            conn.execute(
                f"""
                UPDATE classification_resolution_queue
                SET resolution_state = 'not_applicable',
                    next_action = 'no_action_current_evaluation',
                    escalation_state = 'normal',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE resolution_state NOT IN ('resolved', 'not_applicable')
                  AND source_pattern_hash NOT IN ({placeholders})
                """,
                tuple(sorted(current_pattern_hashes)),
            )
        else:
            conn.execute(
                """
                UPDATE classification_resolution_queue
                SET resolution_state = 'not_applicable',
                    next_action = 'no_action_current_evaluation',
                    escalation_state = 'normal',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE resolution_state NOT IN ('resolved', 'not_applicable')
                """
            )
        for item in items:
            conn.execute(
                """
                INSERT INTO classification_resolution_queue (
                    resolution_hash, source_pattern_hash, last_evaluation_run_hash,
                    resolution_tag, resolution_state, next_action,
                    owner_role, priority, target_due_at,
                    correction_required, source_evidence_required, occurrence_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resolution_hash) DO UPDATE SET
                    last_evaluation_run_hash = excluded.last_evaluation_run_hash,
                    resolution_tag = excluded.resolution_tag,
                    next_action = excluded.next_action,
                    owner_role = excluded.owner_role,
                    priority = excluded.priority,
                    target_due_at = CASE
                        WHEN classification_resolution_queue.target_due_at = '1970-01-01T00:00:00Z'
                          OR classification_resolution_queue.resolution_tag <> excluded.resolution_tag
                        THEN excluded.target_due_at
                        ELSE classification_resolution_queue.target_due_at
                    END,
                    correction_required = excluded.correction_required,
                    source_evidence_required = excluded.source_evidence_required,
                    occurrence_count = excluded.occurrence_count,
                    resolution_state = CASE
                        WHEN classification_resolution_queue.resolution_state IN ('resolved', 'not_applicable')
                        THEN classification_resolution_queue.resolution_state
                        WHEN classification_resolution_queue.resolution_tag <> excluded.resolution_tag
                        THEN excluded.resolution_state
                        ELSE classification_resolution_queue.resolution_state
                    END,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    item["resolution_hash"],
                    item["source_pattern_hash"],
                    run_hash,
                    item["resolution_tag"],
                    item["resolution_state"],
                    item["next_action"],
                    item["owner_role"],
                    item["priority"],
                    item["target_due_at"],
                    1 if item["correction_required"] else 0,
                    1 if item["source_evidence_required"] else 0,
                    item["occurrence_count"],
                ),
            )
        conn.commit()
    managed = build_classification_resolution_status(conn, now=now)
    return ClassificationResolutionQueueSummary(
        run_hash=run_hash,
        queued_count=len(items),
        by_tag=_count_by(items, "resolution_tag"),
        by_state=managed["by_state"],
        correction_review_count=sum(1 for item in items if item["correction_required"]),
        evidence_collection_count=sum(1 for item in items if item["source_evidence_required"]),
        tracked_count=managed["tracked_count"],
        active_count=managed["active_count"],
        stale_active_count=managed["stale_active_count"],
        missing_tracking_count=managed["missing_tracking_count"],
        metadata_missing_count=managed["metadata_missing_count"],
        duplicate_tracking_count=managed["duplicate_tracking_count"],
        overdue_count=managed["overdue_count"],
        managed_queue_ready=managed["managed_queue_ready"],
    )


def build_classification_resolution_queue_from_file(
    *,
    conn: sqlite3.Connection,
    output_json: Path,
) -> ClassificationResolutionQueueSummary:
    output_json = require_ignored_untracked_data_path(output_json, purpose="classification resolution queue output")
    summary = build_classification_resolution_queue(conn=conn, persist=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _latest_learned_evaluation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT run_hash
        FROM classification_evaluation_runs
        WHERE prediction_strategy = 'learned_rules_first'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def build_classification_resolution_status(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    run = _latest_learned_evaluation(conn)
    if not run:
        return {
            "managed_queue_ready": False,
            "run_hash": None,
            "non_auto_count": 0,
            "tracked_count": 0,
            "active_count": 0,
            "stale_active_count": 0,
            "missing_tracking_count": 0,
            "metadata_missing_count": 0,
            "duplicate_tracking_count": 0,
            "overdue_count": 0,
            "by_state": {},
            "by_owner": {},
        }
    run_hash = str(run["run_hash"])
    non_auto_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM classification_evaluation_items WHERE run_hash = ? AND auto_post_candidate = 0",
            (run_hash,),
        ).fetchone()[0]
    )
    tracked_count = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT i.source_pattern_hash)
            FROM classification_evaluation_items i
            JOIN classification_resolution_queue q ON q.source_pattern_hash = i.source_pattern_hash
            WHERE i.run_hash = ? AND i.auto_post_candidate = 0
            """,
            (run_hash,),
        ).fetchone()[0]
    )
    active_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM classification_resolution_queue
            WHERE resolution_state NOT IN ('resolved', 'not_applicable')
            """
        ).fetchone()[0]
    )
    stale_active_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM classification_resolution_queue q
            WHERE q.resolution_state NOT IN ('resolved', 'not_applicable')
              AND NOT EXISTS (
                  SELECT 1
                  FROM classification_evaluation_items i
                  WHERE i.run_hash = ?
                    AND i.auto_post_candidate = 0
                    AND i.source_pattern_hash = q.source_pattern_hash
              )
            """,
            (run_hash,),
        ).fetchone()[0]
    )
    metadata_missing_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM classification_resolution_queue q
            WHERE q.resolution_state NOT IN ('resolved', 'not_applicable')
              AND (q.owner_role = '' OR q.next_action = '' OR q.target_due_at = '')
            """
        ).fetchone()[0]
    )
    active_distinct_count = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT source_pattern_hash)
            FROM classification_resolution_queue
            WHERE resolution_state NOT IN ('resolved', 'not_applicable')
            """
        ).fetchone()[0]
    )
    duplicate_tracking_count = max(0, active_count - active_distinct_count)
    current = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    overdue_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM classification_resolution_queue q
            WHERE q.resolution_state NOT IN ('resolved', 'not_applicable')
              AND q.target_due_at < ?
            """,
            (current,),
        ).fetchone()[0]
    )
    missing = max(0, non_auto_count - tracked_count)
    by_state = _grouped_active_queue_counts(conn, column="resolution_state")
    by_owner = _grouped_active_queue_counts(conn, column="owner_role")
    return {
        "managed_queue_ready": (
            missing == 0
            and stale_active_count == 0
            and metadata_missing_count == 0
            and duplicate_tracking_count == 0
            and overdue_count == 0
        ),
        "run_hash": run_hash,
        "non_auto_count": non_auto_count,
        "tracked_count": tracked_count,
        "active_count": active_count,
        "stale_active_count": stale_active_count,
        "missing_tracking_count": missing,
        "metadata_missing_count": metadata_missing_count,
        "duplicate_tracking_count": duplicate_tracking_count,
        "overdue_count": overdue_count,
        "by_state": by_state,
        "by_owner": by_owner,
    }


def record_classification_resolution_attempt(
    conn: sqlite3.Connection,
    *,
    resolution_hash: str,
    next_state: str,
    next_action: str,
    error_code: str = "",
) -> None:
    allowed_states = {
        "open",
        "evidence_requested",
        "evidence_collected",
        "correction_review",
        "correction_draft_ready",
        "correction_posted",
        "resolved",
        "not_applicable",
    }
    if next_state not in allowed_states:
        raise ValueError("unsupported classification resolution state")
    if not next_action:
        raise ValueError("next_action is required")
    transition = {
        "open": ("accounting_review", 2, 7),
        "evidence_requested": ("evidence_retrieval", 3, 14),
        "evidence_collected": ("accounting_review", 2, 7),
        "correction_review": ("accounting_correction", 1, 3),
        "correction_draft_ready": ("accounting_correction", 1, 3),
        "correction_posted": ("accounting_correction", 1, 3),
    }.get(next_state)
    owner_role, priority, due_days = transition or (None, None, None)
    updated = conn.execute(
        """
        UPDATE classification_resolution_queue
        SET resolution_state = ?, next_action = ?,
            owner_role = COALESCE(?, owner_role),
            priority = COALESCE(?, priority),
            target_due_at = CASE
                WHEN ? IS NOT NULL
                  AND (
                      resolution_state <> ?
                      OR target_due_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                  )
                THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+' || ? || ' days')
                ELSE target_due_at
            END,
            attempt_count = attempt_count + 1,
            last_attempt_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            last_error_code = ?,
            escalation_state = CASE
                WHEN target_due_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now') THEN 'overdue'
                ELSE 'normal'
            END,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE resolution_hash = ?
        """,
        (
            next_state,
            next_action,
            owner_role,
            priority,
            due_days,
            next_state,
            due_days,
            error_code,
            resolution_hash,
        ),
    )
    if updated.rowcount != 1:
        raise KeyError("classification resolution task was not found")
    conn.commit()


def _resolution_item(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    source_pattern_hash = str(row["source_pattern_hash"])
    if int(row["is_classifiable"]) == 0:
        tag = "needs_source_evidence"
        state = "evidence_requested"
        next_action = "retrieve_journal_detail_then_moneyforward_ui_tsv_source_evidence"
        correction_required = False
        owner_role = "evidence_retrieval"
        priority = 3
        due_days = 14
    elif int(row["account_match"]) == 0:
        tag = "account_correction_review"
        state = "correction_review"
        next_action = "prepare_exact_journal_correction_from_source_evidence_and_readback"
        correction_required = True
        owner_role = "accounting_correction"
        priority = 1
        due_days = 3
    elif int(row["sub_account_match"]) == 0:
        tag = "sub_account_correction_review"
        state = "correction_review"
        next_action = "prepare_exact_sub_account_correction_from_source_evidence_and_readback"
        correction_required = True
        owner_role = "accounting_correction"
        priority = 1
        due_days = 3
    elif int(row["tax_match"]) == 0:
        tag = "tax_correction_review"
        state = "correction_review"
        next_action = "prepare_exact_tax_correction_from_invoice_or_receipt_evidence"
        correction_required = True
        owner_role = "accounting_correction"
        priority = 1
        due_days = 3
    else:
        tag = "manual_classification_review"
        state = "open"
        next_action = "collect_comparable_evidence_or_mark_decision_not_applicable"
        correction_required = False
        owner_role = "accounting_review"
        priority = 2
        due_days = 7
    return {
        "resolution_hash": stable_hash({"source_pattern_hash": source_pattern_hash}),
        "source_pattern_hash": source_pattern_hash,
        "resolution_tag": tag,
        "resolution_state": state,
        "next_action": next_action,
        "owner_role": owner_role,
        "priority": priority,
        "target_due_at": (now + timedelta(days=due_days)).isoformat().replace("+00:00", "Z"),
        "correction_required": correction_required,
        "source_evidence_required": True,
        "occurrence_count": int(row["occurrence_count"]),
    }


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item[key])
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _grouped_active_queue_counts(
    conn: sqlite3.Connection,
    *,
    column: str,
) -> dict[str, int]:
    if column not in {"resolution_state", "owner_role"}:
        raise ValueError("unsupported classification resolution grouping")
    rows = conn.execute(
        f"""
        SELECT q.{column} AS group_value, COUNT(*) AS item_count
        FROM classification_resolution_queue q
        WHERE q.resolution_state NOT IN ('resolved', 'not_applicable')
        GROUP BY q.{column}
        ORDER BY q.{column}
        """,
    ).fetchall()
    return {str(row["group_value"]): int(row["item_count"]) for row in rows}
