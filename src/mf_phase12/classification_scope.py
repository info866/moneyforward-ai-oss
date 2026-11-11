from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .db import DEFAULT_DB_PATH, Phase12Database


@dataclass(frozen=True)
class ClassificationScopeStatus:
    db_path: str
    status: str
    run_hash: str
    prediction_strategy: str
    sample_count: int
    classifiable_sample_count: int
    auto_post_eligible_count: int
    auto_post_eligible_accuracy: float
    blocked_count: int
    promotion_allowed: bool
    auto_post_scope_ready: bool
    block_buckets: dict[str, int]
    prediction_source_buckets: dict[str, int]
    raw_data_stored: bool
    raw_ids_printed: bool = False
    moneyforward_api_called: bool = False
    google_drive_write_called: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def build_classification_scope_status(db_path: Path | str = DEFAULT_DB_PATH) -> ClassificationScopeStatus:
    db = Phase12Database(db_path)
    with db.connect_readonly() as conn:
        run = _latest_run(conn)
        if run is None:
            return ClassificationScopeStatus(
                db_path=str(db.path),
                status="not_ready",
                run_hash="",
                prediction_strategy="",
                sample_count=0,
                classifiable_sample_count=0,
                auto_post_eligible_count=0,
                auto_post_eligible_accuracy=0.0,
                blocked_count=0,
                promotion_allowed=False,
                auto_post_scope_ready=False,
                block_buckets={"missing_classification_evaluation_run": 1},
                prediction_source_buckets={},
                raw_data_stored=False,
            )
        run_hash = str(run["run_hash"])
        block_buckets = _block_buckets(conn, run_hash)
        prediction_source_buckets = _prediction_source_buckets(conn, run_hash)
        blocked_count = sum(block_buckets.values())
        raw_data_stored = bool(run["raw_data_stored"])
        ready = (
            bool(run["promotion_allowed"])
            and int(run["auto_post_eligible_count"]) > 0
            and float(run["auto_post_eligible_accuracy"]) == 1.0
            and not raw_data_stored
        )
        return ClassificationScopeStatus(
            db_path=str(db.path),
            status="ready" if ready else "not_ready",
            run_hash=run_hash,
            prediction_strategy=str(run["prediction_strategy"]),
            sample_count=int(run["sample_count"]),
            classifiable_sample_count=int(run["classifiable_sample_count"]),
            auto_post_eligible_count=int(run["auto_post_eligible_count"]),
            auto_post_eligible_accuracy=float(run["auto_post_eligible_accuracy"]),
            blocked_count=blocked_count,
            promotion_allowed=bool(run["promotion_allowed"]),
            auto_post_scope_ready=ready,
            block_buckets=block_buckets,
            prediction_source_buckets=prediction_source_buckets,
            raw_data_stored=raw_data_stored,
        )


def status_to_json(status: ClassificationScopeStatus) -> str:
    return json.dumps(status.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def _latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT run_hash, prediction_strategy, sample_count, classifiable_sample_count,
               auto_post_eligible_count, auto_post_eligible_accuracy,
               promotion_allowed, raw_data_stored
        FROM classification_evaluation_runs
        WHERE prediction_strategy = 'learned_rules_first'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def _block_buckets(conn: sqlite3.Connection, run_hash: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT
          CASE
            WHEN auto_post_candidate = 1 THEN 'auto_post_candidate'
            WHEN is_classifiable = 0 THEN 'low_information_pattern'
            WHEN overall_passed = 0 THEN failure_reason_code
            ELSE 'manual_gate_not_auto_candidate'
          END AS bucket,
          COUNT(*) AS count
        FROM classification_evaluation_items
        WHERE run_hash = ?
          AND auto_post_candidate = 0
        GROUP BY bucket
        ORDER BY count DESC, bucket
        """,
        (run_hash,),
    ).fetchall()
    return {str(row["bucket"] or "unknown"): int(row["count"]) for row in rows}


def _prediction_source_buckets(conn: sqlite3.Connection, run_hash: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT prediction_source, COUNT(*) AS count
        FROM classification_evaluation_items
        WHERE run_hash = ?
          AND auto_post_candidate = 1
        GROUP BY prediction_source
        ORDER BY count DESC, prediction_source
        """,
        (run_hash,),
    ).fetchall()
    return {str(row["prediction_source"] or "unknown"): int(row["count"]) for row in rows}
