from __future__ import annotations

import sqlite3
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timezone
from typing import Any

from .accounting_api import MoneyForwardAccountingClient
from .hash_utils import mask_identifier, stable_hash
from .journal_pattern_provenance import (
    SNAPSHOT_SOURCE,
    move_journal_pattern_provenance,
    record_journal_pattern_provenance,
)
from .rag import build_rag_cards, upsert_sync_manifest
from .office_profile import (resolve_office_profile, require_database_office_identity,
    resolve_moneyforward_db_path, resolve_moneyforward_keychain_service)


def _require_sync_boundary(conn, client, office_id=None):
    selected = resolve_office_profile(required=True)
    if client.office_profile != selected:
        raise PermissionError("Snapshot client and company profile differ")
    if not client.keychain_service:
        raise PermissionError("Snapshot credential source must be explicit")
    resolve_moneyforward_keychain_service(selected, client.keychain_service, external_io=True)
    resolve_moneyforward_db_path(selected, client.db_path, external_io=True)
    require_database_office_identity(conn, selected, client.validate_office_identity(office_id=office_id))


def sync_accounting_full_read_snapshot(
    conn: sqlite3.Connection,
    client: MoneyForwardAccountingClient,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    office_id: str | None = None,
) -> dict[str, int]:
    """Read-only Money Forward inventory sync that stores only redacted/hash summaries."""
    _require_sync_boundary(conn, client, office_id)
    offices = _extract_items(client.offices())
    term_settings = _extract_items(client.term_settings(office_id))
    office_hash = upsert_sync_manifest(conn, "moneyforward", "offices", offices)
    term_hash = upsert_sync_manifest(conn, "moneyforward", "term_settings", term_settings)
    _make_summary_cards(conn, "journal_pattern", "事業者read snapshot", len(offices), office_hash)
    _make_summary_cards(conn, "journal_pattern", "会計期間read snapshot", len(term_settings), term_hash)

    journal_ranges = _resolve_journal_ranges(term_settings, start_date, end_date)
    counts = {
        "offices": len(offices),
        "term_settings": len(term_settings),
        **sync_master_snapshot(conn, client, office_id),
    }
    counts["journal_patterns"] = 0
    counts["journal_occurrences"] = 0
    counts["journal_ranges"] = len(journal_ranges)
    range_records = []
    for index, (journal_start, journal_end) in enumerate(journal_ranges):
        range_count = sync_journal_patterns(
            conn,
            client,
            office_id,
            start_date=journal_start,
            end_date=journal_end,
            replace_existing=index == 0,
        )
        counts["journal_patterns"] += range_count
        counts["journal_occurrences"] += range_count
        range_records.append({"start_date": journal_start, "end_date": journal_end, "item_count": range_count})
    counts["tax_imputed_patterns"] = impute_unknown_tax_categories_in_db(conn)
    _upsert_journal_patterns_full_manifest(conn, range_records)
    counts["journal_range_source"] = 1 if start_date and end_date else 0
    return counts


def sync_master_snapshot(
    conn: sqlite3.Connection,
    client: MoneyForwardAccountingClient,
    office_id: str | None = None,
) -> dict[str, int]:
    _require_sync_boundary(conn, client, office_id)
    sources: dict[str, dict[str, Any]] = {
        "accounts": client.accounts(office_id),
        "sub_accounts": client.sub_accounts(office_id),
        "taxes": client.taxes(office_id),
        "trade_partners": client.trade_partners(office_id),
        "departments": client.departments(office_id),
        "connected_accounts": client.connected_accounts(office_id),
    }
    counts: dict[str, int] = {}
    for item_type, payload in sources.items():
        items = _extract_items(payload)
        source_hash = upsert_sync_manifest(conn, "moneyforward", item_type, items)
        for item in items:
            name = _redacted_summary(
                str(item.get("name") or item.get("display_name") or item.get("title") or item.get("id") or "")
            )
            external_id = str(item.get("id") or item.get("code") or name)
            normalized_type = {
                "accounts": "account",
                "sub_accounts": "sub_account",
                "taxes": "tax",
                "trade_partners": "trade_partner",
                "departments": "department",
                "connected_accounts": "connected_account",
            }[item_type]
            conn.execute(
                """
                INSERT OR REPLACE INTO master_items (
                    source_name, item_type, external_id_hash, name_normalized,
                    active, payload_json
                ) VALUES ('moneyforward', ?, ?, ?, 1, ?)
                """,
                (normalized_type, mask_identifier(external_id), name, stable_hash(item)),
            )
        counts[item_type] = len(items)
        _make_master_rag_cards(conn, item_type, items, source_hash)
    conn.commit()
    return counts


def sync_journal_patterns(
    conn: sqlite3.Connection,
    client: MoneyForwardAccountingClient,
    office_id: str | None = None,
    *,
    start_date: str,
    end_date: str,
    replace_existing: bool = False,
) -> int:
    _require_sync_boundary(conn, client, office_id)
    journals = _fetch_all_journals(client, office_id, start_date=start_date, end_date=end_date)
    noncanonical_journal_hashes = {
        str(row["moneyforward_journal_id_hash"])
        for row in conn.execute(
            """
            SELECT DISTINCT moneyforward_journal_id_hash
            FROM audit_logs
            WHERE event_type = 'journal_verification_conflict_noncanonical'
              AND moneyforward_journal_id_hash IS NOT NULL
            """
        ).fetchall()
    }
    journals = [
        journal
        for journal in journals
        if mask_identifier(_journal_id(journal) or "") not in noncanonical_journal_hashes
    ]
    cards = []
    pattern_count = 0
    journal_pattern_rows = []
    trade_partner_names = _trade_partner_names(conn)
    for journal in journals:
        journal_pattern_rows.append(
            (
                journal,
                _journal_to_pattern_records(journal, trade_partner_names=trade_partner_names),
            )
        )
    journal_pattern_rows, detail_backfill_records = _backfill_journal_details_parallel(
        client,
        office_id,
        journal_pattern_rows,
        trade_partner_names=trade_partner_names,
    )
    master_labels = _master_label_lookup(conn)
    resolved_journal_pattern_rows: list[tuple[dict[str, Any], list[dict[str, str | None]]]] = []
    for journal, patterns in journal_pattern_rows:
        resolved_journal_pattern_rows.append(
            (journal, [_resolve_pattern_master_labels(pattern, master_labels) for pattern in patterns])
        )
    resolved_journal_pattern_rows = _apply_tax_category_imputation(resolved_journal_pattern_rows)

    conn.execute("SAVEPOINT journal_pattern_snapshot_refresh")
    try:
        if replace_existing:
            _clear_journal_pattern_snapshot(conn)
        source_hash = upsert_sync_manifest(
            conn,
            "moneyforward",
            "journal_patterns",
            journals,
            synced_from=start_date,
            synced_to=end_date,
        )
        for journal, patterns in resolved_journal_pattern_rows:
            for line_index, pattern in enumerate(patterns):
                pattern_count += 1
                _upsert_journal_pattern(conn, pattern)
                record_journal_pattern_provenance(
                    conn,
                    pattern_hash=str(pattern["pattern_hash"]),
                    source_kind=SNAPSHOT_SOURCE,
                )
                _upsert_journal_pattern_occurrence(
                    conn,
                    journal=journal,
                    pattern=pattern,
                    line_index=line_index,
                    start_date=start_date,
                    end_date=end_date,
                )
                cards.append(_pattern_to_rag_card(pattern))
        if detail_backfill_records:
            upsert_sync_manifest(
                conn,
                "moneyforward",
                "journal_detail_backfill",
                detail_backfill_records,
                synced_from=start_date,
                synced_to=end_date,
            )
        if pattern_count:
            _remove_placeholder_unknown_patterns(conn)
        build_rag_cards(conn, cards, source_hash, commit=False)
        conn.execute("RELEASE SAVEPOINT journal_pattern_snapshot_refresh")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT journal_pattern_snapshot_refresh")
        conn.execute("RELEASE SAVEPOINT journal_pattern_snapshot_refresh")
        raise
    conn.commit()
    return pattern_count


def _backfill_journal_details_parallel(
    client: MoneyForwardAccountingClient,
    office_id: str | None,
    journal_pattern_rows: list[tuple[dict[str, Any], list[dict[str, str | None]]]],
    *,
    max_workers: int = 8,
    trade_partner_names: list[str] | None = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, str | None]]]], list[dict[str, Any]]]:
    needs_backfill = [
        (index, journal, patterns)
        for index, (journal, patterns) in enumerate(journal_pattern_rows)
        if _records_need_detail_backfill(patterns)
    ]
    if not needs_backfill:
        return journal_pattern_rows, []
    updated_rows = list(journal_pattern_rows)
    audit_records: list[dict[str, Any]] = []
    worker_count = min(max_workers, len(needs_backfill))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                copy_context().run,
                _maybe_backfill_journal_detail_patterns,
                client,
                office_id,
                journal,
                patterns,
                trade_partner_names=trade_partner_names,
            ): index
            for index, journal, patterns in needs_backfill
        }
        for future in as_completed(futures):
            index = futures[future]
            patterns, audit_record = future.result()
            journal, _old_patterns = updated_rows[index]
            updated_rows[index] = (journal, patterns)
            if audit_record:
                audit_records.append(audit_record)
    return updated_rows, audit_records


def _fetch_all_journals(
    client: MoneyForwardAccountingClient,
    office_id: str | None,
    *,
    start_date: str,
    end_date: str,
    per_page: int = 100,
    max_pages: int = 100,
) -> list[dict]:
    journals: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = client.journals(office_id, start_date=start_date, end_date=end_date, page=page, per_page=per_page)
        page_items = _extract_items(payload)
        journals.extend(page_items)
        if len(page_items) < per_page:
            break
    return journals


def _clear_journal_pattern_snapshot(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM classification_evaluation_items")
    conn.execute("DELETE FROM classification_improvement_candidates")
    conn.execute("DELETE FROM shadow_evaluations")
    conn.execute("DROP TABLE IF EXISTS temp.snapshot_pattern_cleanup")
    conn.execute(
        """
        CREATE TEMP TABLE snapshot_pattern_cleanup AS
        SELECT DISTINCT p.pattern_hash
        FROM journal_patterns p
        WHERE (
            EXISTS (
                SELECT 1
                FROM journal_pattern_provenance source
                WHERE source.pattern_hash = p.pattern_hash
                  AND source.source_kind = 'moneyforward_snapshot'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM journal_pattern_provenance preserved
                WHERE preserved.pattern_hash = p.pattern_hash
                  AND preserved.source_kind <> 'moneyforward_snapshot'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM classification_resolution_queue resolution
                WHERE resolution.source_pattern_hash = p.pattern_hash
            )
        )
        OR (
            (p.description_key = 'no_description'
             OR p.amount_bucket = 'unknown'
             OR p.debit_account = 'unknown_debit_account'
             OR p.credit_account = 'unknown_credit_account'
             OR p.tax_category = 'unknown_tax')
            AND NOT EXISTS (
                SELECT 1
                FROM journal_pattern_provenance preserved
                WHERE preserved.pattern_hash = p.pattern_hash
                  AND preserved.source_kind <> 'moneyforward_snapshot'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM classification_resolution_queue resolution
                WHERE resolution.source_pattern_hash = p.pattern_hash
            )
        )
        """
    )
    conn.execute(
        "DELETE FROM journal_pattern_occurrences WHERE pattern_hash IN (SELECT pattern_hash FROM snapshot_pattern_cleanup)"
    )
    # Financial classifications are derived local cache entries. Do not leave
    # them linked to a pattern that this refresh is replacing; preserve the
    # event row but force a fresh classification against the new snapshot.
    conn.execute(
        """
        UPDATE financial_event_classifications
        SET matched_pattern_hash = NULL,
            matched_pattern_source = 'snapshot_replaced_reclassification_required',
            recommended_action = 'queue_for_review',
            registration_allowed = 0,
            learning_action = 'reclassify_after_snapshot_refresh',
            unresolved_points_json = '["snapshot_pattern_replaced"]',
            result_summary_redacted = 'snapshot pattern replaced; reclassification required',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE matched_pattern_hash IN (SELECT pattern_hash FROM snapshot_pattern_cleanup)
        """
    )
    conn.execute(
        "DELETE FROM journal_pattern_provenance WHERE pattern_hash IN (SELECT pattern_hash FROM snapshot_pattern_cleanup)"
    )
    conn.execute("DELETE FROM journal_patterns WHERE pattern_hash IN (SELECT pattern_hash FROM snapshot_pattern_cleanup)")
    # Remaining snapshot-backed patterns are also supported by verified or
    # financial learning. Keep their learning provenance and discard only the
    # obsolete snapshot association before the replacement snapshot is loaded.
    conn.execute(
        """
        DELETE FROM journal_pattern_provenance
        WHERE source_kind = 'moneyforward_snapshot'
          AND pattern_hash NOT IN (
              SELECT source_pattern_hash FROM classification_resolution_queue
          )
        """
    )
    conn.execute("DELETE FROM journal_pattern_occurrences")
    conn.execute("DROP TABLE snapshot_pattern_cleanup")


def sync_journal_patterns_incremental(
    conn: sqlite3.Connection,
    client: MoneyForwardAccountingClient,
    office_id: str | None = None,
    *,
    fallback_start_date: str,
    end_date: str,
) -> dict[str, int | str]:
    _require_sync_boundary(conn, client, office_id)
    latest = conn.execute(
        """
        SELECT synced_to FROM sync_manifests
        WHERE source_name = 'moneyforward' AND source_kind = 'journal_patterns'
          AND status = 'ok' AND synced_to IS NOT NULL
        ORDER BY synced_at DESC LIMIT 1
        """
    ).fetchone()
    start_date = latest["synced_to"] if latest else fallback_start_date
    if start_date > end_date:
        start_date = fallback_start_date
    count = sync_journal_patterns(conn, client, office_id, start_date=start_date, end_date=end_date)
    conn.execute(
        """
        UPDATE sync_manifests
        SET synced_from = ?, synced_to = ?
        WHERE id = (SELECT id FROM sync_manifests ORDER BY id DESC LIMIT 1)
        """,
        (start_date, end_date),
    )
    conn.commit()
    return {"start_date": start_date, "end_date": end_date, "count": count}


def mark_incremental_sync_failed(conn: sqlite3.Connection, *, source_kind: str, error_message: str) -> None:
    failed_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO sync_manifests (
            source_name, source_kind, source_hash, item_count, status, error_message
        ) VALUES ('moneyforward', ?, ?, 0, 'failed', ?)
        """,
        (
            source_kind,
            stable_hash({"source_kind": source_kind, "error": error_message, "failed_at": failed_at}),
            error_message[:240],
        ),
    )
    conn.commit()


def _upsert_journal_patterns_full_manifest(conn: sqlite3.Connection, range_records: list[dict[str, Any]]) -> None:
    if not range_records:
        return
    source_hash = stable_hash({"ranges": range_records, "scope": "all_term_settings"})
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO sync_manifests (
            source_name, source_kind, source_hash, item_count,
            synced_from, synced_to, synced_at, status
        ) VALUES ('moneyforward', 'journal_patterns', ?, ?, ?, ?, ?, 'ok')
        ON CONFLICT(source_name, source_kind, source_hash) DO UPDATE SET
            item_count = excluded.item_count,
            synced_from = excluded.synced_from,
            synced_to = excluded.synced_to,
            synced_at = excluded.synced_at,
            status = 'ok',
            error_message = NULL
        """,
        (
            source_hash,
            sum(int(record["item_count"]) for record in range_records),
            str(range_records[0]["start_date"]),
            str(range_records[-1]["end_date"]),
            synced_at,
        ),
    )
    conn.commit()


def _extract_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in (
            "data",
            "items",
            "accounts",
            "sub_accounts",
            "taxes",
            "departments",
            "trade_partners",
            "connected_accounts",
            "term_settings",
            "journals",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _journal_to_pattern_records(
    journal: dict[str, Any],
    *,
    trade_partner_names: list[str] | None = None,
) -> list[dict[str, str | None]]:
    journal_description = _journal_description_key(journal)
    records: list[dict[str, str | None]] = []
    for debit, credit in _paired_journal_lines(journal):
        amount = _amount(debit) or _amount(journal)
        record = {
            "vendor_key": _vendor_key(
                journal,
                debit,
                credit,
                trade_partner_names=trade_partner_names,
            ),
            "description_key": _line_description_key(journal_description, debit, credit),
            "amount_bucket": _amount_bucket(amount),
            "debit_account": _line_name(debit, "account") or "unknown_debit_account",
            "debit_sub_account": _line_name(debit, "sub_account"),
            "credit_account": _line_name(credit, "account") or "unknown_credit_account",
            "credit_sub_account": _line_name(credit, "sub_account"),
            "tax_category": _line_name(debit, "tax") or "unknown_tax",
            "last_seen_at": _date_key(journal),
        }
        record["pattern_hash"] = stable_hash({k: v for k, v in record.items() if k != "last_seen_at"})
        records.append(record)
    if records:
        return records
    record = {
        "vendor_key": _vendor_key(journal, trade_partner_names=trade_partner_names),
        "description_key": journal_description,
        "amount_bucket": _amount_bucket(_amount(journal)),
        "debit_account": "unknown_debit_account",
        "debit_sub_account": None,
        "credit_account": "unknown_credit_account",
        "credit_sub_account": None,
        "tax_category": "unknown_tax",
        "last_seen_at": _date_key(journal),
    }
    record["pattern_hash"] = stable_hash({k: v for k, v in record.items() if k != "last_seen_at"})
    return [record]


def _maybe_backfill_journal_detail_patterns(
    client: MoneyForwardAccountingClient,
    office_id: str | None,
    journal: dict[str, Any],
    patterns: list[dict[str, str | None]],
    *,
    trade_partner_names: list[str] | None = None,
) -> tuple[list[dict[str, str | None]], dict[str, Any] | None]:
    if not _records_need_detail_backfill(patterns):
        return patterns, None
    journal_id = _journal_id(journal)
    if not journal_id:
        return patterns, {
            "journal_ref_hash": "sha256:missing",
            "status": "skipped_missing_journal_id",
            "quality_before": _records_quality_score(patterns),
            "quality_after": _records_quality_score(patterns),
        }
    before = _records_quality_score(patterns)
    try:
        detail = _extract_journal_detail(client.journal(journal_id, office_id))
    except Exception:
        return patterns, {
            "journal_ref_hash": mask_identifier(journal_id),
            "status": "detail_fetch_failed",
            "quality_before": before,
            "quality_after": before,
        }
    detail_patterns = _journal_to_pattern_records(detail, trade_partner_names=trade_partner_names)
    after = _records_quality_score(detail_patterns)
    if after > before:
        return detail_patterns, {
            "journal_ref_hash": mask_identifier(journal_id),
            "status": "improved",
            "quality_before": before,
            "quality_after": after,
        }
    return patterns, {
        "journal_ref_hash": mask_identifier(journal_id),
        "status": "not_improved",
        "quality_before": before,
        "quality_after": after,
    }


def _records_need_detail_backfill(patterns: list[dict[str, str | None]]) -> bool:
    return any(_pattern_quality_score(pattern) < 5 for pattern in patterns)


def _records_quality_score(patterns: list[dict[str, str | None]]) -> int:
    return sum(_pattern_quality_score(pattern) for pattern in patterns)


def _pattern_quality_score(pattern: dict[str, str | None]) -> int:
    score = 0
    if pattern["description_key"] != "no_description":
        score += 1
    if pattern["amount_bucket"] != "unknown":
        score += 1
    if pattern["debit_account"] != "unknown_debit_account":
        score += 1
    if pattern["credit_account"] != "unknown_credit_account":
        score += 1
    if pattern["tax_category"] != "unknown_tax":
        score += 1
    return score


def _journal_id(journal: dict[str, Any]) -> str | None:
    value = journal.get("id") or journal.get("journal_id")
    return str(value) if value else None


def _extract_journal_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    current = payload
    for _ in range(4):
        if _journal_lines(current):
            return current
        next_value = None
        for key in ("data", "journal", "item"):
            value = current.get(key)
            if isinstance(value, dict):
                next_value = value
                break
        if next_value is None:
            return current
        current = next_value
    return current


def _paired_journal_lines(journal: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    split_debits: list[dict[str, Any]] = []
    split_credits: list[dict[str, Any]] = []
    for line in _journal_lines(journal):
        debitor = line.get("debitor")
        creditor = line.get("creditor")
        if isinstance(debitor, dict) and isinstance(creditor, dict) and _has_side_values(debitor) and _has_side_values(creditor):
            debit = {"source_line": line, **line["debitor"]}
            credit = {"source_line": line, **line["creditor"]}
            pairs.append((debit, credit))
            continue
        if isinstance(debitor, dict) and _has_side_values(debitor):
            split_debits.append({"source_line": line, **debitor})
            continue
        if isinstance(creditor, dict) and _has_side_values(creditor):
            split_credits.append({"source_line": line, **creditor})
            continue
        side = _side(line)
        if side == "debit":
            credit = _find_counterpart(_journal_lines(journal), "credit") or {}
            pairs.append((line, credit))
        elif side == "credit":
            split_credits.append(line)
    pairs.extend(_pair_split_lines(split_debits, split_credits))
    if not pairs:
        lines = _journal_lines(journal)
        debit = next((line for line in lines if _side(line) == "debit"), lines[0] if lines else {})
        credit = next((line for line in lines if _side(line) == "credit"), lines[-1] if lines else {})
        if debit or credit:
            pairs.append((debit, credit))
    return pairs


def _has_side_values(side: dict[str, Any]) -> bool:
    return any(value not in (None, "") for value in side.values())


def _pair_split_lines(
    debits: list[dict[str, Any]],
    credits: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not debits or not credits:
        return []
    if len(debits) == len(credits):
        amount_pairs = _pair_split_lines_by_amount(debits, credits)
        if amount_pairs is not None:
            return amount_pairs
        return list(zip(debits, credits))
    if len(credits) == 1:
        return [(debit, credits[0]) for debit in debits]
    if len(debits) == 1:
        return [(debits[0], credit) for credit in credits]
    return list(zip(debits, credits))


def _pair_split_lines_by_amount(
    debits: list[dict[str, Any]],
    credits: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
    unmatched_credits = list(enumerate(credits))
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for debit in debits:
        debit_amount = _amount(debit)
        if debit_amount is None:
            return None
        matches = [
            (index, credit_index, credit)
            for index, (credit_index, credit) in enumerate(unmatched_credits)
            if _amount(credit) == debit_amount
        ]
        if len(matches) != 1:
            return None
        unmatched_index, _credit_index, credit = matches[0]
        pairs.append((debit, credit))
        unmatched_credits.pop(unmatched_index)
    if unmatched_credits:
        return None
    return pairs


def _journal_lines(journal: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("branches", "details", "journal_details", "journal_entries", "lines"):
        value = journal.get(key)
        if isinstance(value, list):
            lines = [item for item in value if isinstance(item, dict)]
            if lines:
                return lines
    for key in ("entries", "items"):
        value = journal.get(key)
        if isinstance(value, list):
            lines = [item for item in value if isinstance(item, dict)]
            if lines and any(_looks_like_journal_line(line) for line in lines):
                return lines
    for value in journal.values():
        if not isinstance(value, list):
            continue
        lines = [item for item in value if isinstance(item, dict)]
        if lines and any(_looks_like_journal_line(line) for line in lines):
            return lines
    return []


def _looks_like_journal_line(line: dict[str, Any]) -> bool:
    keys = set(line)
    return bool(
        {"debitor", "creditor"} & keys
        or {"account", "account_name", "account_item_name", "tax", "tax_name", "tax_category_name"} & keys
        or _side(line) in {"debit", "credit"}
    )


def _find_counterpart(lines: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    return next((line for line in lines if _side(line) == side), None)


def _side(line: dict[str, Any]) -> str:
    value = str(line.get("side") or line.get("dc") or line.get("type") or "").lower()
    if "credit" in value or value in {"cr", "貸方"}:
        return "credit"
    return "debit"


def _line_name(line: dict[str, Any], key: str) -> str | None:
    nested = line.get(key)
    if isinstance(nested, dict):
        value = nested.get("name") or nested.get("display_name") or nested.get("long_name") or nested.get("id")
        return _safe_label(str(value)) if value else None
    candidates = [
        f"{key}_name",
        f"{key}_display_name",
        f"{key}_long_name",
        f"{key}_item_name",
        f"{key}_category_name",
        key,
        f"{key}_id",
        f"{key}_code",
    ]
    if key == "tax":
        candidates = ["tax_name", "tax_long_name", "excise_name", "excise_long_name", *candidates]
    for candidate in candidates:
        value = line.get(candidate)
        if value:
            return _safe_label(str(value))
    if key == "account":
        value = line.get("account_id") or line.get("account_item_id") or line.get("account_category_id")
        return _safe_label(str(value)) if value else None
    if key == "sub_account":
        value = line.get("sub_account_id") or line.get("sub_account_item_id") or line.get("sub_account_category_id")
        return _safe_label(str(value)) if value else None
    if key == "tax":
        value = line.get("tax_id") or line.get("excise_id") or line.get("tax_category_id")
        return _safe_label(str(value)) if value else None
    return None


def _safe_label(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_\-:]{4,}", value):
        return mask_identifier(value)
    return _redacted_summary(value, limit=80)


def _journal_description_key(journal: dict[str, Any]) -> str:
    values = [str(journal.get(key) or "") for key in ("description", "note", "memo", "remark")]
    summary = _redacted_summary(_normalize_description_text(" ".join(value for value in values if value)), limit=120)
    return summary or "no_description"


def _line_description_key(journal_description: str, debit: dict[str, Any], credit: dict[str, Any]) -> str:
    values = []
    for line in (debit, credit):
        source_line = line.get("source_line")
        if isinstance(source_line, dict):
            values.append(str(source_line.get("remark") or source_line.get("description") or ""))
        values.append(str(line.get("remark") or line.get("description") or ""))
    summary = _redacted_summary(_normalize_description_text(" ".join(value for value in values if value)), limit=120)
    return summary or journal_description


def _normalize_description_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    if not text:
        return ""
    text = re.sub(r"令和\s*\d{1,3}\s*年\s*月(?:度|分)?", " ", text)
    text = re.sub(r"平成\s*\d{1,3}\s*年\s*月(?:度|分)?", " ", text)
    text = re.sub(r"令和\s*\d{1,2}\s*年\s*\d{1,2}\s*月(?:度|分)?", " ", text)
    text = re.sub(r"平成\s*\d{1,2}\s*年\s*\d{1,2}\s*月(?:度|分)?", " ", text)
    text = re.sub(r"\d{1,2}\s*月\s*[~〜-]\s*\d{1,2}\s*月分", " ", text)
    text = re.sub(r"\d{1,2}\s*月分", " ", text)
    text = re.sub(r"\d{1,2}\s*月度", " ", text)
    text = re.sub(r"\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b", " ", text)
    text = re.sub(r"\b\d{1,2}[-/.月]\d{1,2}日?\b", " ", text)
    text = re.sub(r"\b20\d{2}年?\s*\d{1,2}月(?:分|度)?\b", " ", text)
    text = re.sub(r"\b0?月度\b", " ", text)
    text = re.sub(r"\b\d{1,3}(?:,\d{3})+円?\b", " ", text)
    text = re.sub(r"\b\d+円\b", " ", text)
    text = re.sub(r"業務委託(?:料|費用|費)?", "業務委託", text)
    text = text.replace("給料", "給与")
    text = _dedupe_repeated_tokens(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedupe_repeated_tokens(value: str) -> str:
    tokens = [token for token in value.split() if token]
    if not tokens:
        return ""
    deduped: list[str] = []
    index = 0
    while index < len(tokens):
        skipped = False
        for size in range(4, 0, -1):
            if len(deduped) < size or index + size > len(tokens):
                continue
            if deduped[-size:] == tokens[index : index + size]:
                index += size
                skipped = True
                break
        if skipped:
            continue
        deduped.append(tokens[index])
        index += 1
    return " ".join(deduped)


def _vendor_key(
    journal: dict[str, Any],
    debit: dict[str, Any] | None = None,
    credit: dict[str, Any] | None = None,
    *,
    trade_partner_names: list[str] | None = None,
) -> str:
    for line in (debit or {}, credit or {}):
        for key in ("trade_partner_name", "partner_name", "vendor_name", "supplier_name"):
            value = line.get(key)
            if value:
                return _redacted_summary(str(value), limit=80)
        for key in ("trade_partner_code", "trade_partner_id", "partner_id", "vendor_id", "supplier_id"):
            value = line.get(key)
            if value:
                return mask_identifier(str(value))
    for key in ("trade_partner_name", "partner_name", "vendor_name", "supplier_name"):
        value = journal.get(key)
        if value:
            return _redacted_summary(str(value), limit=80)
    for key in ("trade_partner_id", "partner_id", "vendor_id", "supplier_id"):
        value = journal.get(key)
        if value:
            return mask_identifier(str(value))
    sub_account_vendor = _sub_account_vendor_key(debit or {}, credit or {})
    if sub_account_vendor:
        return sub_account_vendor
    description_vendor = _description_vendor_key(journal, debit or {}, credit or {}, trade_partner_names or [])
    if description_vendor:
        return description_vendor
    return "unknown_vendor"


def _description_vendor_key(
    journal: dict[str, Any],
    debit: dict[str, Any],
    credit: dict[str, Any],
    trade_partner_names: list[str],
) -> str | None:
    if not trade_partner_names:
        return None
    text_candidates: list[str] = []
    for key in ("description", "note", "memo", "remark"):
        value = journal.get(key)
        if value:
            text_candidates.append(str(value))
    for line in (debit, credit):
        source_line = line.get("source_line")
        if isinstance(source_line, dict):
            for key in ("remark", "description"):
                value = source_line.get(key)
                if value:
                    text_candidates.append(str(value))
        for key in ("remark", "description"):
            value = line.get(key)
            if value:
                text_candidates.append(str(value))
    if not text_candidates:
        return None
    normalized_text = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", _normalize_description_text(" ".join(text_candidates))).casefold(),
    )
    if not normalized_text:
        return None
    for vendor_name in _normalized_trade_partner_names(trade_partner_names):
        if vendor_name in normalized_text:
            return _redacted_summary(vendor_name, limit=80)
    return None


def _normalized_trade_partner_names(trade_partner_names: list[str]) -> list[str]:
    normalized_names: list[str] = []
    for name in trade_partner_names:
        normalized_name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name)).casefold())
        if len(normalized_name) < 3:
            continue
        if _is_generic_sub_account_vendor(normalized_name):
            continue
        normalized_names.append(normalized_name)
    return sorted(set(normalized_names), key=len, reverse=True)


def _trade_partner_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name_normalized
        FROM master_items
        WHERE source_name = 'moneyforward'
          AND item_type = 'trade_partner'
          AND active = 1
          AND name_normalized IS NOT NULL
          AND name_normalized != ''
        """
    ).fetchall()
    return [str(row["name_normalized"]) for row in rows]


def _sub_account_vendor_key(debit: dict[str, Any], credit: dict[str, Any]) -> str | None:
    debit_sub = _line_name(debit, "sub_account")
    credit_sub = _line_name(credit, "sub_account")
    if debit_sub and debit_sub == credit_sub and not _is_generic_sub_account_vendor(debit_sub):
        return debit_sub
    return None


def _is_generic_sub_account_vendor(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    generic_terms = (
        "小口現金",
        "現金",
        "普通",
        "銀行",
        "アメックス",
        "カード",
        "クレジット",
        "未払",
        "未収",
        "預り",
        "社会保険",
        "源泉",
        "消耗品",
        "飲食",
        "事務用品",
        "駐車",
        "賃料",
        "業務委託",
        "水道光熱",
    )
    return any(term.casefold() in normalized for term in generic_terms)


def _date_key(journal: dict[str, Any]) -> str | None:
    for key in ("date", "journal_date", "recorded_on", "transaction_date"):
        value = journal.get(key)
        if value:
            return str(value)[:10]
    return None


def _amount(value: dict[str, Any]) -> int:
    for key in ("amount", "value", "amount_value", "total_amount", "tax_included_amount", "debit_amount", "credit_amount", "price"):
        raw = value.get(key)
        if isinstance(raw, (int, float)):
            return int(abs(raw))
        if isinstance(raw, str):
            text = raw.replace(",", "")
            if text.lstrip("-").isdigit():
                return abs(int(text))
    return 0


def _amount_bucket(amount: int) -> str:
    if amount <= 0:
        return "unknown"
    if amount < 1_000:
        return "micro"
    if amount < 5_000:
        return "small"
    if amount < 10_000:
        return "small_mid"
    if amount < 50_000:
        return "mid"
    if amount < 100_000:
        return "large_mid"
    return "100000+"


def _master_label_lookup(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        """
        SELECT item_type, external_id_hash, name_normalized
        FROM master_items
        WHERE source_name = 'moneyforward'
          AND item_type IN ('account', 'sub_account', 'tax')
          AND active = 1
        """
    ).fetchall()
    return {(row["item_type"], row["external_id_hash"]): row["name_normalized"] for row in rows}


def _resolve_pattern_master_labels(
    pattern: dict[str, str | None],
    master_labels: dict[tuple[str, str], str],
) -> dict[str, str | None]:
    resolved = dict(pattern)
    for field, item_type in (
        ("debit_account", "account"),
        ("debit_sub_account", "sub_account"),
        ("credit_account", "account"),
        ("credit_sub_account", "sub_account"),
        ("tax_category", "tax"),
    ):
        value = resolved.get(field)
        if value:
            resolved[field] = master_labels.get((item_type, value), value)
    resolved["pattern_hash"] = stable_hash({k: v for k, v in resolved.items() if k != "last_seen_at"})
    return resolved


def _apply_tax_category_imputation(
    journal_pattern_rows: list[tuple[dict[str, Any], list[dict[str, str | None]]]],
    *,
    min_account_combo_support: int = 5,
    min_account_pair_support: int = 5,
    min_exact_support: int = 1,
) -> list[tuple[dict[str, Any], list[dict[str, str | None]]]]:
    patterns = [pattern for _journal, row_patterns in journal_pattern_rows for pattern in row_patterns]
    exact_lookup = _single_tax_lookup(patterns, _exact_tax_key, min_support=min_exact_support)
    account_combo_lookup = _single_tax_lookup(patterns, _account_combo_tax_key, min_support=min_account_combo_support)
    account_pair_lookup = _single_tax_lookup(patterns, _account_pair_tax_key, min_support=min_account_pair_support)
    imputed_rows: list[tuple[dict[str, Any], list[dict[str, str | None]]]] = []
    for journal, row_patterns in journal_pattern_rows:
        imputed_patterns = []
        for pattern in row_patterns:
            imputed_patterns.append(_impute_pattern_tax(pattern, exact_lookup, account_combo_lookup, account_pair_lookup))
        imputed_rows.append((journal, imputed_patterns))
    return imputed_rows


def _single_tax_lookup(
    patterns: list[dict[str, str | None]],
    key_func,
    *,
    min_support: int,
) -> dict[tuple[str, ...], str]:
    grouped: dict[tuple[str, ...], dict[str, int]] = {}
    for pattern in patterns:
        tax_category = pattern.get("tax_category")
        if not tax_category or tax_category == "unknown_tax":
            continue
        key = key_func(pattern)
        grouped.setdefault(key, {})
        grouped[key][tax_category] = grouped[key].get(tax_category, 0) + int(pattern.get("success_count") or 1)
    lookup: dict[tuple[str, ...], str] = {}
    for key, tax_counts in grouped.items():
        support = sum(tax_counts.values())
        if len(tax_counts) == 1 and support >= min_support:
            lookup[key] = next(iter(tax_counts))
    return lookup


def _impute_pattern_tax(
    pattern: dict[str, str | None],
    exact_lookup: dict[tuple[str, ...], str],
    account_combo_lookup: dict[tuple[str, ...], str],
    account_pair_lookup: dict[tuple[str, ...], str],
) -> dict[str, str | None]:
    if pattern.get("tax_category") != "unknown_tax":
        return pattern
    imputed_tax = (
        exact_lookup.get(_exact_tax_key(pattern))
        or account_combo_lookup.get(_account_combo_tax_key(pattern))
        or account_pair_lookup.get(_account_pair_tax_key(pattern))
    )
    if not imputed_tax:
        return pattern
    resolved = dict(pattern)
    resolved["tax_category"] = imputed_tax
    resolved["pattern_hash"] = stable_hash({k: v for k, v in resolved.items() if k != "last_seen_at"})
    return resolved


def impute_unknown_tax_categories_in_db(
    conn: sqlite3.Connection,
    *,
    min_account_combo_support: int = 5,
    min_account_pair_support: int = 5,
    min_exact_support: int = 1,
) -> int:
    conn.execute("SAVEPOINT tax_category_imputation")
    try:
        imputed_count = _impute_unknown_tax_categories_in_db_impl(
            conn,
            min_account_combo_support=min_account_combo_support,
            min_account_pair_support=min_account_pair_support,
            min_exact_support=min_exact_support,
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT tax_category_imputation")
        conn.execute("RELEASE SAVEPOINT tax_category_imputation")
        raise
    conn.execute("RELEASE SAVEPOINT tax_category_imputation")
    conn.commit()
    return imputed_count


def _impute_unknown_tax_categories_in_db_impl(
    conn: sqlite3.Connection,
    *,
    min_account_combo_support: int,
    min_account_pair_support: int,
    min_exact_support: int,
) -> int:
    rows = [dict(row) for row in conn.execute("SELECT * FROM journal_patterns ORDER BY id").fetchall()]
    exact_lookup = _single_tax_lookup(rows, _exact_tax_key, min_support=min_exact_support)
    account_combo_lookup = _single_tax_lookup(rows, _account_combo_tax_key, min_support=min_account_combo_support)
    account_pair_lookup = _single_tax_lookup(rows, _account_pair_tax_key, min_support=min_account_pair_support)
    imputed_count = 0
    classification_artifacts_cleared = False
    for row in rows:
        if row["tax_category"] != "unknown_tax":
            continue
        imputed_tax = (
            exact_lookup.get(_exact_tax_key(row))
            or account_combo_lookup.get(_account_combo_tax_key(row))
            or account_pair_lookup.get(_account_pair_tax_key(row))
        )
        if not imputed_tax:
            continue
        if not classification_artifacts_cleared:
            _clear_classification_evaluation_artifacts(conn)
            classification_artifacts_cleared = True
        new_hash = stable_hash(
            {
                "vendor_key": row["vendor_key"],
                "description_key": row["description_key"],
                "amount_bucket": row["amount_bucket"],
                "debit_account": row["debit_account"],
                "debit_sub_account": row["debit_sub_account"],
                "credit_account": row["credit_account"],
                "credit_sub_account": row["credit_sub_account"],
                "tax_category": imputed_tax,
            }
        )
        existing = conn.execute(
            "SELECT pattern_hash FROM journal_patterns WHERE pattern_hash = ?",
            (new_hash,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE journal_patterns
                SET success_count = success_count + ?,
                    last_seen_at = COALESCE(MAX(last_seen_at, ?), last_seen_at, ?)
                WHERE pattern_hash = ?
                """,
                (row["success_count"], row["last_seen_at"], row["last_seen_at"], new_hash),
            )
            conn.execute(
                "UPDATE journal_pattern_occurrences SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE financial_event_classifications SET matched_pattern_hash = ? WHERE matched_pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE financial_event_learning_events SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE shadow_evaluations SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE classification_resolution_queue SET source_pattern_hash = ? WHERE source_pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            move_journal_pattern_provenance(
                conn,
                old_pattern_hash=str(row["pattern_hash"]),
                new_pattern_hash=new_hash,
            )
            conn.execute("DELETE FROM journal_patterns WHERE pattern_hash = ?", (row["pattern_hash"],))
        else:
            conn.execute(
                """
                INSERT INTO journal_patterns (
                    pattern_hash, vendor_key, description_key, amount_bucket,
                    debit_account, debit_sub_account, credit_account, credit_sub_account,
                    tax_category, success_count, failure_count, auto_post_eligibility, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_hash,
                    row["vendor_key"],
                    row["description_key"],
                    row["amount_bucket"],
                    row["debit_account"],
                    row["debit_sub_account"],
                    row["credit_account"],
                    row["credit_sub_account"],
                    imputed_tax,
                    row["success_count"],
                    row["failure_count"],
                    row["auto_post_eligibility"],
                    row["last_seen_at"],
                ),
            )
            conn.execute(
                "UPDATE journal_pattern_occurrences SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE financial_event_classifications SET matched_pattern_hash = ? WHERE matched_pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE financial_event_learning_events SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE shadow_evaluations SET pattern_hash = ? WHERE pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            conn.execute(
                "UPDATE classification_resolution_queue SET source_pattern_hash = ? WHERE source_pattern_hash = ?",
                (new_hash, row["pattern_hash"]),
            )
            move_journal_pattern_provenance(
                conn,
                old_pattern_hash=str(row["pattern_hash"]),
                new_pattern_hash=new_hash,
            )
            conn.execute("DELETE FROM journal_patterns WHERE pattern_hash = ?", (row["pattern_hash"],))
        imputed_count += 1
    return imputed_count


def _clear_classification_evaluation_artifacts(conn: sqlite3.Connection) -> None:
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "classification_improvement_candidates" in existing_tables:
        conn.execute("DELETE FROM classification_improvement_candidates")
    if "classification_evaluation_items" in existing_tables:
        conn.execute("DELETE FROM classification_evaluation_items")
    if "classification_evaluation_runs" in existing_tables:
        if "classification_resolution_queue" in existing_tables:
            conn.execute(
                """
                DELETE FROM classification_evaluation_runs
                WHERE run_hash NOT IN (
                    SELECT last_evaluation_run_hash FROM classification_resolution_queue
                )
                """
            )
        else:
            conn.execute("DELETE FROM classification_evaluation_runs")


def _exact_tax_key(pattern: dict[str, str | None]) -> tuple[str, ...]:
    return (
        pattern["description_key"] or "",
        pattern["vendor_key"] or "",
        *_account_combo_tax_key(pattern),
    )


def _account_combo_tax_key(pattern: dict[str, str | None]) -> tuple[str, ...]:
    return (
        pattern["debit_account"] or "",
        pattern["debit_sub_account"] or "",
        pattern["credit_account"] or "",
        pattern["credit_sub_account"] or "",
    )


def _account_pair_tax_key(pattern: dict[str, str | None]) -> tuple[str, ...]:
    return (
        pattern["debit_account"] or "",
        pattern["credit_account"] or "",
    )


def _upsert_journal_pattern(conn: sqlite3.Connection, pattern: dict[str, str | None]) -> None:
    conn.execute(
        """
        INSERT INTO journal_patterns (
            pattern_hash, vendor_key, description_key, amount_bucket,
            debit_account, debit_sub_account, credit_account, credit_sub_account,
            tax_category, success_count, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(pattern_hash) DO UPDATE SET
            success_count = success_count + 1,
            last_seen_at = excluded.last_seen_at
        """,
        (
            pattern["pattern_hash"],
            pattern["vendor_key"],
            pattern["description_key"],
            pattern["amount_bucket"],
            pattern["debit_account"],
            pattern["debit_sub_account"],
            pattern["credit_account"],
            pattern["credit_sub_account"],
            pattern["tax_category"],
            pattern["last_seen_at"],
        ),
    )


def _upsert_journal_pattern_occurrence(
    conn: sqlite3.Connection,
    *,
    journal: dict[str, Any],
    pattern: dict[str, str | None],
    line_index: int,
    start_date: str,
    end_date: str,
) -> None:
    journal_id = _journal_id(journal)
    journal_id_hash = mask_identifier(journal_id) if journal_id else "sha256:missing"
    source_range_hash = stable_hash({"start_date": start_date, "end_date": end_date})
    occurrence_hash = stable_hash(
        {
            "journal_id_hash": journal_id_hash,
            "pattern_hash": pattern["pattern_hash"],
            "line_index": line_index,
            "source_range_hash": source_range_hash,
        }
    )
    conn.execute(
        """
        INSERT INTO journal_pattern_occurrences (
            occurrence_hash, pattern_hash, moneyforward_journal_id_hash,
            line_index, transaction_date, source_range_hash
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(occurrence_hash) DO UPDATE SET
            pattern_hash = excluded.pattern_hash,
            transaction_date = excluded.transaction_date,
            source_range_hash = excluded.source_range_hash
        """,
        (
            occurrence_hash,
            pattern["pattern_hash"],
            journal_id_hash,
            line_index,
            _date_key(journal),
            source_range_hash,
        ),
    )


def _remove_placeholder_unknown_patterns(conn: sqlite3.Connection) -> None:
    placeholder_hashes = [
        row["pattern_hash"]
        for row in conn.execute(
            """
            SELECT pattern_hash FROM journal_patterns
            WHERE vendor_key = 'unknown_vendor'
              AND description_key = 'no_description'
              AND amount_bucket = 'unknown'
              AND debit_account = 'unknown_debit_account'
              AND credit_account = 'unknown_credit_account'
              AND tax_category = 'unknown_tax'
              AND NOT EXISTS (
                  SELECT 1
                  FROM journal_pattern_provenance source
                  WHERE source.pattern_hash = journal_patterns.pattern_hash
                    AND source.source_kind <> 'moneyforward_snapshot'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM classification_resolution_queue resolution
                  WHERE resolution.source_pattern_hash = journal_patterns.pattern_hash
              )
            """
        ).fetchall()
    ]
    for pattern_hash in placeholder_hashes:
        conn.execute("DELETE FROM classification_evaluation_items WHERE source_pattern_hash = ?", (pattern_hash,))
        conn.execute("DELETE FROM shadow_evaluations WHERE pattern_hash = ?", (pattern_hash,))
        conn.execute(
            """
            UPDATE financial_event_classifications
            SET matched_pattern_hash = NULL,
                matched_pattern_source = 'placeholder_removed_reclassification_required',
                recommended_action = 'queue_for_review',
                registration_allowed = 0,
                learning_action = 'reclassify_after_placeholder_removal',
                unresolved_points_json = '["placeholder_pattern_removed"]',
                result_summary_redacted = 'placeholder pattern removed; reclassification required',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE matched_pattern_hash = ?
            """,
            (pattern_hash,),
        )
        conn.execute("DELETE FROM journal_pattern_occurrences WHERE pattern_hash = ?", (pattern_hash,))
        conn.execute("DELETE FROM journal_pattern_provenance WHERE pattern_hash = ?", (pattern_hash,))
    conn.execute(
        """
        DELETE FROM journal_patterns
        WHERE vendor_key = 'unknown_vendor'
          AND description_key = 'no_description'
          AND amount_bucket = 'unknown'
          AND debit_account = 'unknown_debit_account'
          AND credit_account = 'unknown_credit_account'
          AND tax_category = 'unknown_tax'
          AND NOT EXISTS (
              SELECT 1
              FROM journal_pattern_provenance source
              WHERE source.pattern_hash = journal_patterns.pattern_hash
                AND source.source_kind <> 'moneyforward_snapshot'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM classification_resolution_queue resolution
              WHERE resolution.source_pattern_hash = journal_patterns.pattern_hash
          )
        """
    )


def _pattern_to_rag_card(pattern: dict[str, str | None]) -> dict[str, str]:
    title = f"{pattern['description_key']} {pattern['debit_account']}".strip()[:120]
    body = " | ".join(
        [
            f"vendor={pattern['vendor_key']}",
            f"description={pattern['description_key']}",
            f"amount_bucket={pattern['amount_bucket']}",
            f"debit={pattern['debit_account']}",
            f"debit_sub={pattern.get('debit_sub_account') or ''}",
            f"credit={pattern['credit_account']}",
            f"credit_sub={pattern.get('credit_sub_account') or ''}",
            f"tax={pattern['tax_category']}",
        ]
    )
    return {"card_type": "journal_pattern", "title": title, "body": body}


def _make_master_rag_cards(conn: sqlite3.Connection, item_type: str, items: list[dict], source_hash: str) -> None:
    cards = []
    for item in items:
        name = _redacted_summary(str(item.get("name") or item.get("display_name") or item.get("id") or ""))
        if not name:
            continue
        cards.append(
            {
                "card_type": "vendor_profile" if item_type == "trade_partners" else "journal_pattern",
                "title": name,
                "body": f"{item_type}: {name}",
            }
        )
    if cards:
        build_rag_cards(conn, cards, source_hash)


def _make_summary_cards(
    conn: sqlite3.Connection,
    card_type: str,
    title: str,
    count: int,
    source_hash: str,
) -> None:
    build_rag_cards(
        conn,
        [{"card_type": card_type, "title": title, "body": f"redacted item count: {count}"}],
        source_hash,
    )


def _resolve_journal_range(term_settings: list[dict], start_date: str | None, end_date: str | None) -> tuple[str, str]:
    return _resolve_journal_ranges(term_settings, start_date, end_date)[-1]


def _resolve_journal_ranges(
    term_settings: list[dict],
    start_date: str | None,
    end_date: str | None,
) -> list[tuple[str, str]]:
    if start_date and end_date:
        return [(start_date, end_date)]
    candidates = [
        item
        for item in term_settings
        if isinstance(item.get("start_date"), str) and isinstance(item.get("end_date"), str)
    ]
    if not candidates:
        raise RuntimeError("term_settings did not include start_date/end_date for journal sync")
    return [
        (str(item["start_date"]), str(item["end_date"]))
        for item in sorted(candidates, key=lambda item: str(item.get("start_date")))
    ]


def _redacted_summary(value: str, limit: int = 120) -> str:
    value = re.sub(r"T\d{13}", "T*************", value)
    value = re.sub(r"\d{4,}", "####", value)
    return value[:limit]
