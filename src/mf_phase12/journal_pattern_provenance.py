from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .hash_utils import stable_hash


SNAPSHOT_SOURCE = "moneyforward_snapshot"
VERIFIED_LEARNING_SOURCE = "verified_journal_learning"
PAYROLL_LEARNING_SOURCE = "payroll_verified_journal_learning"
FINANCIAL_LEARNING_SOURCE = "financial_event_learning"
PAYROLL_TEXT_MARKERS = ("給与", "給料", "賞与", "キユウヨ", "キュウヨ")
PAYROLL_ACCOUNT_MARKERS = ("給料手当", "給料賃金", "給与", "賞与", "役員報酬")


def is_payroll_pattern(pattern: Mapping[str, Any]) -> bool:
    """Identify payroll accounting without changing or anonymizing the pattern."""
    if str(pattern.get("debit_account") or "") in PAYROLL_ACCOUNT_MARKERS:
        return True
    if str(pattern.get("credit_account") or "") in PAYROLL_ACCOUNT_MARKERS:
        return True
    searchable = " ".join(
        str(pattern.get(key) or "")
        for key in ("vendor_key", "description_key", "debit_account", "credit_account")
    ).casefold()
    return any(marker.casefold() in searchable for marker in PAYROLL_TEXT_MARKERS)


def record_journal_pattern_provenance(
    conn: sqlite3.Connection,
    *,
    pattern_hash: str,
    source_kind: str,
    source_ref_hash: str | None = None,
) -> None:
    """Record why a reusable pattern exists so snapshot refreshes cannot erase learning."""
    if source_kind not in {
        SNAPSHOT_SOURCE,
        VERIFIED_LEARNING_SOURCE,
        PAYROLL_LEARNING_SOURCE,
        FINANCIAL_LEARNING_SOURCE,
    }:
        raise ValueError(f"unsupported journal pattern source_kind: {source_kind}")
    reference = source_ref_hash or stable_hash({"source_kind": source_kind, "pattern_hash": pattern_hash})
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_pattern_provenance (
            pattern_hash, source_kind, source_ref_hash
        ) VALUES (?, ?, ?)
        """,
        (pattern_hash, source_kind, reference),
    )


def backfill_verified_journal_learning_provenance(conn: sqlite3.Connection) -> int:
    """Recognize pre-provenance verified-learning rows from their stable hash shape."""
    rows = conn.execute(
        """
        SELECT pattern_hash, vendor_key, description_key, amount_bucket,
               debit_account, debit_sub_account, credit_account,
               credit_sub_account, tax_category
        FROM journal_patterns
        """
    ).fetchall()
    recovered = 0
    for row in rows:
        candidate: dict[str, Any] = {
            "debit_account": str(row["debit_account"]),
            "debit_sub_account": row["debit_sub_account"],
            "credit_account": str(row["credit_account"]),
            "credit_sub_account": row["credit_sub_account"],
            "tax_category": str(row["tax_category"]),
        }
        expected_hash = stable_hash(
            {
                "source": VERIFIED_LEARNING_SOURCE,
                "vendor_key": str(row["vendor_key"]),
                "description_key": str(row["description_key"]),
                "amount_bucket": str(row["amount_bucket"]),
                "cash_flow_direction": "outflow",
                **candidate,
            }
        )
        if expected_hash != str(row["pattern_hash"]):
            continue
        record_journal_pattern_provenance(
            conn,
            pattern_hash=str(row["pattern_hash"]),
            source_kind=VERIFIED_LEARNING_SOURCE,
        )
        recovered += 1
    return recovered


def move_journal_pattern_provenance(
    conn: sqlite3.Connection,
    *,
    old_pattern_hash: str,
    new_pattern_hash: str,
) -> None:
    """Retain source ownership when normalization changes a pattern hash."""
    if old_pattern_hash == new_pattern_hash:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_pattern_provenance (
            pattern_hash, source_kind, source_ref_hash
        )
        SELECT ?, source_kind, source_ref_hash
        FROM journal_pattern_provenance
        WHERE pattern_hash = ?
        """,
        (new_pattern_hash, old_pattern_hash),
    )
    conn.execute("DELETE FROM journal_pattern_provenance WHERE pattern_hash = ?", (old_pattern_hash,))
