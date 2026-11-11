from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DemoLedgerCleanupPlan:
    matched_ledger_count: int
    matched_audit_log_count: int
    dependent_row_counts: dict[str, int]
    status_counts: dict[str, int]
    execute_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_ledger_count": self.matched_ledger_count,
            "matched_audit_log_count": self.matched_audit_log_count,
            "dependent_row_counts": self.dependent_row_counts,
            "status_counts": self.status_counts,
            "execute_allowed": self.execute_allowed,
            "target_filter": "file_name=demo.pdf AND file_hash LIKE demo-file-hash-%",
            "raw_drive_file_ids_stored": False,
            "raw_moneyforward_ids_stored": False,
        }


def build_demo_ledger_cleanup_plan(conn: sqlite3.Connection) -> DemoLedgerCleanupPlan:
    rows = conn.execute(
        """
        SELECT processing_status, COUNT(*) AS count
        FROM processing_ledger
        WHERE file_name = 'demo.pdf'
          AND file_hash LIKE 'demo-file-hash-%'
          AND moneyforward_journal_id_hash IS NULL
          AND moneyforward_voucher_id_hash IS NULL
        GROUP BY processing_status
        ORDER BY processing_status
        """
    ).fetchall()
    status_counts = {str(row["processing_status"]): int(row["count"]) for row in rows}
    idempotency_keys = _demo_ledger_idempotency_keys(conn)
    dependent_row_counts = _dependent_row_counts(conn, idempotency_keys)
    return DemoLedgerCleanupPlan(
        matched_ledger_count=sum(status_counts.values()),
        matched_audit_log_count=dependent_row_counts.get("audit_logs", 0),
        dependent_row_counts=dependent_row_counts,
        status_counts=status_counts,
        execute_allowed=True,
    )


def execute_demo_ledger_cleanup(conn: sqlite3.Connection) -> DemoLedgerCleanupPlan:
    plan = build_demo_ledger_cleanup_plan(conn)
    idempotency_keys = _demo_ledger_idempotency_keys(conn)
    if idempotency_keys:
        for table in _DEPENDENT_TABLES:
            conn.executemany(f"DELETE FROM {table} WHERE idempotency_key = ?", [(key,) for key in idempotency_keys])
    conn.execute(
        """
        DELETE FROM processing_ledger
        WHERE file_name = 'demo.pdf'
          AND file_hash LIKE 'demo-file-hash-%'
          AND moneyforward_journal_id_hash IS NULL
          AND moneyforward_voucher_id_hash IS NULL
        """
    )
    conn.commit()
    return plan


def _demo_ledger_idempotency_keys(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["idempotency_key"])
        for row in conn.execute(
            """
            SELECT idempotency_key
            FROM processing_ledger
            WHERE file_name = 'demo.pdf'
              AND file_hash LIKE 'demo-file-hash-%'
              AND moneyforward_journal_id_hash IS NULL
              AND moneyforward_voucher_id_hash IS NULL
            """
        ).fetchall()
    ]


def _dependent_row_counts(conn: sqlite3.Connection, idempotency_keys: list[str]) -> dict[str, int]:
    if not idempotency_keys:
        return {table: 0 for table in _DEPENDENT_TABLES}
    placeholders = ",".join("?" for _ in idempotency_keys)
    counts: dict[str, int] = {}
    for table in _DEPENDENT_TABLES:
        counts[table] = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE idempotency_key IN ({placeholders})",
                idempotency_keys,
            ).fetchone()[0]
        )
    return counts


_DEPENDENT_TABLES = [
    "write_intents",
    "approval_records",
    "safety_gate_results",
    "post_registration_verifications",
    "correction_diffs",
    "audit_logs",
]
