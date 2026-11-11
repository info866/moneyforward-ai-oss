from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .hash_utils import stable_hash
from .office_profile import resolve_office_profile, require_database_office_identity


def upsert_sync_manifest(
    conn: sqlite3.Connection,
    source_name: str,
    source_kind: str,
    records: list[dict],
    *,
    synced_from: str | None = None,
    synced_to: str | None = None,
) -> str:
    source_hash = stable_hash(records)
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO sync_manifests (
            source_name, source_kind, source_hash, item_count,
            synced_from, synced_to, synced_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ok')
        ON CONFLICT(source_name, source_kind, source_hash) DO UPDATE SET
            item_count = excluded.item_count,
            synced_from = excluded.synced_from,
            synced_to = excluded.synced_to,
            synced_at = excluded.synced_at,
            status = 'ok',
            error_message = NULL
        """,
        (
            source_name,
            source_kind,
            source_hash,
            len(records),
            synced_from,
            synced_to,
            synced_at,
        ),
    )
    return source_hash


def build_rag_cards(
    conn: sqlite3.Connection,
    records: list[dict],
    source_hash: str,
    *,
    commit: bool = True,
) -> int:
    count = 0
    for record in records:
        card_type = record["card_type"]
        title = record["title"]
        body = record["body"]
        card_hash = stable_hash({"card_type": card_type, "title": title, "body": body, "source_hash": source_hash})
        conn.execute(
            """
            INSERT OR REPLACE INTO rag_cards (card_hash, card_type, title, body, source_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (card_hash, card_type, title, body, source_hash),
        )
        count += 1
    if commit:
        conn.commit()
    return count


def search_rag(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    require_database_office_identity(conn, resolve_office_profile(required=True))
    try:
        rows = conn.execute(
            """
            SELECT r.card_type, r.title, r.body
            FROM rag_cards_fts f
            JOIN rag_cards r ON r.id = f.rowid
            WHERE rag_cards_fts MATCH ?
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        like_query = f"%{query[:80]}%"
        rows = conn.execute(
            """
            SELECT card_type, title, body
            FROM rag_cards
            WHERE title LIKE ? OR body LIKE ?
            LIMIT ?
            """,
            (like_query, like_query, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_correction_diff(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str | None,
    moneyforward_journal_id_hash: str,
    before: dict,
    after: dict,
    diff_summary_redacted: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO correction_diffs (
            idempotency_key, moneyforward_journal_id_hash, before_hash,
            after_hash, diff_summary_redacted
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (idempotency_key, moneyforward_journal_id_hash, stable_hash(before), stable_hash(after), diff_summary_redacted),
    )
    conn.commit()
    return int(cursor.lastrowid)


def convert_correction_diff_to_failure_rag(conn: sqlite3.Connection, diff_id: int) -> str:
    row = conn.execute("SELECT * FROM correction_diffs WHERE id = ?", (diff_id,)).fetchone()
    if not row:
        raise KeyError("unknown correction diff")
    body = str(row["diff_summary_redacted"])[:240]
    card = {
        "card_type": "failure_example",
        "title": f"修正差分 {row['moneyforward_journal_id_hash'][:20]}",
        "body": body,
    }
    source_hash = stable_hash({"correction_diff_id": diff_id, "after_hash": row["after_hash"]})
    build_rag_cards(conn, [card], source_hash)
    card_hash = stable_hash({**card, "source_hash": source_hash})
    conn.execute("UPDATE correction_diffs SET converted_to_rag_card_hash = ? WHERE id = ?", (card_hash, diff_id))
    conn.commit()
    return card_hash
