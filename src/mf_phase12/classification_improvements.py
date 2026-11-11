from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass

from .hash_utils import stable_hash
from .rag import build_rag_cards


@dataclass(frozen=True)
class ClassificationImprovementRagSummary:
    candidates_scanned: int
    failure_examples_upserted: int
    rag_cards_upserted: int
    raw_data_stored: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LearnedRuleMaterializationSummary:
    vendor_profiles_upserted: int
    fixed_rule_candidates_upserted: int
    rag_cards_upserted: int
    raw_data_stored: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def materialize_classification_improvement_rag(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> ClassificationImprovementRagSummary:
    rows = conn.execute(
        """
        SELECT c.*, s.description_key AS source_description_key,
               s.debit_account AS source_debit_account,
               s.debit_sub_account AS source_debit_sub_account,
               s.credit_account AS source_credit_account,
               s.credit_sub_account AS source_credit_sub_account,
               s.tax_category AS source_tax_category,
               p.debit_account AS predicted_debit_account,
               p.debit_sub_account AS predicted_debit_sub_account,
               p.credit_account AS predicted_credit_account,
               p.credit_sub_account AS predicted_credit_sub_account,
               p.tax_category AS predicted_tax_category
        FROM classification_improvement_candidates c
        JOIN journal_patterns s ON s.pattern_hash = c.source_pattern_hash
        LEFT JOIN journal_patterns p ON p.pattern_hash = c.predicted_pattern_hash
        WHERE c.converted_to_rag_card_hash IS NULL
        ORDER BY c.occurrence_count DESC, c.id
        """
        + (" LIMIT ?" if limit is not None else ""),
        (limit,) if limit is not None else (),
    ).fetchall()
    cards = []
    upserted = 0
    for row in rows:
        payload = _candidate_payload(dict(row))
        failure_hash = stable_hash(payload)
        conn.execute(
            """
            INSERT INTO failure_examples (
                failure_hash, failure_type, trigger_text,
                blocked_reason, corrected_rule_json, source_journal_id_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(failure_hash) DO UPDATE SET
                blocked_reason = excluded.blocked_reason,
                corrected_rule_json = excluded.corrected_rule_json
            """,
            (
                failure_hash,
                f"classification_{row['issue_type']}",
                str(row["source_description_key"])[:120],
                row["blocking_reason"],
                json.dumps(payload["expected_pattern"], ensure_ascii=False, sort_keys=True),
                row["source_pattern_hash"],
            ),
        )
        card = _candidate_card(dict(row), failure_hash)
        card_hash = stable_hash({**card, "source_hash": failure_hash})
        cards.append((row["id"], card_hash, card, failure_hash))
        upserted += 1
    for candidate_id, card_hash, card, source_hash in cards:
        build_rag_cards(conn, [card], source_hash)
        conn.execute(
            """
            UPDATE classification_improvement_candidates
            SET converted_to_rag_card_hash = ?
            WHERE id = ?
            """,
            (card_hash, candidate_id),
        )
    conn.commit()
    return ClassificationImprovementRagSummary(
        candidates_scanned=len(rows),
        failure_examples_upserted=upserted,
        rag_cards_upserted=len(cards),
    )


def materialize_stable_learned_rules(
    conn: sqlite3.Connection,
    *,
    min_support_count: int = 10,
    min_description_support_count: int | None = None,
) -> LearnedRuleMaterializationSummary:
    min_amount_bucket_support_count = 2
    vendor_rows = _stable_pattern_rows(conn, "vendor_key", min_support_count=min_support_count)
    source_event_context_rows = _stable_source_event_context_rows(conn)
    occurrence_context_rows = _stable_occurrence_context_rows(conn)
    exact_pattern_date_rows = _stable_exact_pattern_date_rows(conn)
    exact_pattern_rows = _stable_exact_pattern_rows(conn)
    description_rows = _stable_pattern_rows(
        conn,
        "description_key",
        min_support_count=min_description_support_count or min_support_count,
    )
    amount_bucket_rows = _stable_amount_bucket_rows(conn, min_support_count=min_amount_bucket_support_count)
    vendor_token_rows = _stable_vendor_token_rows(conn, min_support_count=3)
    vendor_description_rows = _stable_vendor_description_rows(
        conn,
        min_support_count=min_description_support_count or min_support_count,
    )
    vendor_count = 0
    rule_count = 0
    cards = []
    current_rule_hashes = []
    for row in vendor_rows:
        conn.execute(
            """
            INSERT INTO vendor_profiles (
                vendor_key, display_name_masked, default_debit_account,
                default_debit_sub_account, default_credit_account,
                default_credit_sub_account, default_tax_category, confidence_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vendor_key) DO UPDATE SET
                default_debit_account = excluded.default_debit_account,
                default_debit_sub_account = excluded.default_debit_sub_account,
                default_credit_account = excluded.default_credit_account,
                default_credit_sub_account = excluded.default_credit_sub_account,
                default_tax_category = excluded.default_tax_category,
                confidence_score = excluded.confidence_score,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                row["trigger_key"],
                row["trigger_key"][:120],
                row["debit_account"],
                row["debit_sub_account"],
                row["credit_account"],
                row["credit_sub_account"],
                row["tax_category"],
                _confidence(row["support_count"], trigger_type="vendor_key"),
            ),
        )
        cards.append(_rule_card("vendor_key", dict(row)))
        vendor_count += 1
    for row in [
        *source_event_context_rows,
        *occurrence_context_rows,
        *exact_pattern_date_rows,
        *exact_pattern_rows,
        *vendor_token_rows,
        *vendor_description_rows,
        *description_rows,
        *amount_bucket_rows,
    ]:
        trigger_type = row["trigger_type"]
        rule_hash = stable_hash({**dict(row)})
        current_rule_hashes.append(rule_hash)
        conn.execute(
            """
            UPDATE fixed_rule_candidates
            SET status = 'suspended',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE trigger_type = ?
              AND trigger_key = ?
              AND rule_hash != ?
            """,
            (trigger_type, row["trigger_key"], rule_hash),
        )
        conn.execute(
            """
            INSERT INTO fixed_rule_candidates (
                rule_hash, trigger_type, trigger_key, debit_account,
                debit_sub_account, credit_account, credit_sub_account,
                tax_category, support_count, confidence_score, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow')
            ON CONFLICT(rule_hash) DO UPDATE SET
                debit_account = excluded.debit_account,
                debit_sub_account = excluded.debit_sub_account,
                credit_account = excluded.credit_account,
                credit_sub_account = excluded.credit_sub_account,
                tax_category = excluded.tax_category,
                support_count = excluded.support_count,
                confidence_score = excluded.confidence_score,
                status = excluded.status,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                rule_hash,
                trigger_type,
                row["trigger_key"],
                row["debit_account"],
                row["debit_sub_account"],
                row["credit_account"],
                row["credit_sub_account"],
                row["tax_category"],
                row["support_count"],
                _confidence(row["support_count"], trigger_type=trigger_type),
            ),
        )
        cards.append(_rule_card(trigger_type, dict(row)))
        rule_count += 1
    if current_rule_hashes:
        placeholders = ",".join("?" for _ in current_rule_hashes)
        conn.execute(
            f"""
            UPDATE fixed_rule_candidates
            SET status = 'suspended',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE trigger_type IN (
                'description_key', 'vendor_description_key', 'amount_bucket_key',
                'vendor_token_key', 'exact_pattern_key', 'exact_pattern_date_key',
                'occurrence_context_key', 'source_event_context_key'
            )
              AND status = 'shadow'
              AND rule_hash NOT IN ({placeholders})
            """,
            tuple(current_rule_hashes),
        )
    else:
        conn.execute(
            """
            UPDATE fixed_rule_candidates
            SET status = 'suspended',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE trigger_type IN (
                'description_key', 'vendor_description_key', 'amount_bucket_key',
                'vendor_token_key', 'exact_pattern_key', 'exact_pattern_date_key',
                'occurrence_context_key', 'source_event_context_key'
            )
              AND status = 'shadow'
            """
        )
    for card in cards:
        build_rag_cards(conn, [card], stable_hash(card))
    conn.commit()
    return LearnedRuleMaterializationSummary(
        vendor_profiles_upserted=vendor_count,
        fixed_rule_candidates_upserted=rule_count,
        rag_cards_upserted=len(cards),
    )


def _stable_pattern_rows(
    conn: sqlite3.Connection,
    trigger_column: str,
    *,
    min_support_count: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT '{trigger_column}' AS trigger_type,
               {trigger_column} AS trigger_key,
               debit_account, debit_sub_account, credit_account,
               credit_sub_account, tax_category,
               SUM(success_count) AS support_count,
               COUNT(DISTINCT debit_account || '|' || IFNULL(debit_sub_account, '') || '|'
                    || credit_account || '|' || IFNULL(credit_sub_account, '') || '|'
                    || tax_category) AS variants
        FROM journal_patterns
        WHERE {trigger_column} NOT IN ('unknown_vendor', 'no_description')
          AND debit_account != 'unknown_debit_account'
          AND credit_account != 'unknown_credit_account'
          AND tax_category != 'unknown_tax'
        GROUP BY {trigger_column}
        HAVING support_count >= ? AND variants = 1
        ORDER BY support_count DESC, trigger_key
        """,
        (min_support_count,),
    ).fetchall()


def _stable_exact_pattern_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT 'exact_pattern_key' AS trigger_type,
               vendor_key || char(9) || description_key || char(9) || amount_bucket AS trigger_key,
               debit_account, debit_sub_account, credit_account,
               credit_sub_account, tax_category,
               SUM(success_count) AS support_count,
               COUNT(DISTINCT debit_account || '|' || IFNULL(debit_sub_account, '') || '|'
                    || credit_account || '|' || IFNULL(credit_sub_account, '') || '|'
                    || tax_category) AS variants
        FROM journal_patterns
        WHERE amount_bucket != 'unknown'
          AND debit_account != 'unknown_debit_account'
          AND credit_account != 'unknown_credit_account'
        GROUP BY vendor_key, description_key, amount_bucket
        HAVING support_count >= 1 AND variants = 1
        ORDER BY support_count DESC, trigger_key
        """
    ).fetchall()


def _stable_occurrence_context_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT 'occurrence_context_key' AS trigger_type,
               p.vendor_key || char(9) || p.description_key || char(9) || p.amount_bucket
                   || char(9) || o.transaction_date || char(9) || o.source_range_hash
                   || char(9) || o.line_index AS trigger_key,
               p.debit_account, p.debit_sub_account, p.credit_account,
               p.credit_sub_account, p.tax_category,
               COUNT(*) AS support_count,
               COUNT(DISTINCT p.debit_account || '|' || IFNULL(p.debit_sub_account, '') || '|'
                    || p.credit_account || '|' || IFNULL(p.credit_sub_account, '') || '|'
                    || p.tax_category) AS variants
        FROM journal_patterns p
        JOIN journal_pattern_occurrences o ON o.pattern_hash = p.pattern_hash
        WHERE p.amount_bucket != 'unknown'
          AND p.debit_account != 'unknown_debit_account'
          AND p.credit_account != 'unknown_credit_account'
          AND o.transaction_date IS NOT NULL
          AND o.transaction_date != ''
          AND o.source_range_hash IS NOT NULL
          AND o.source_range_hash != ''
        GROUP BY p.vendor_key, p.description_key, p.amount_bucket,
                 o.transaction_date, o.source_range_hash, o.line_index
        HAVING support_count >= 1 AND variants = 1
        ORDER BY support_count DESC, trigger_key
        """
    ).fetchall()


def _stable_source_event_context_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT 'source_event_context_key' AS trigger_type,
               p.vendor_key || char(9) || p.description_key || char(9) || p.amount_bucket
                   || char(9) || o.moneyforward_journal_id_hash || char(9) || o.line_index AS trigger_key,
               p.debit_account, p.debit_sub_account, p.credit_account,
               p.credit_sub_account, p.tax_category,
               COUNT(*) AS support_count,
               COUNT(DISTINCT p.debit_account || '|' || IFNULL(p.debit_sub_account, '') || '|'
                    || p.credit_account || '|' || IFNULL(p.credit_sub_account, '') || '|'
                    || p.tax_category) AS variants
        FROM journal_patterns p
        JOIN journal_pattern_occurrences o ON o.pattern_hash = p.pattern_hash
        WHERE p.amount_bucket != 'unknown'
          AND p.debit_account != 'unknown_debit_account'
          AND p.credit_account != 'unknown_credit_account'
          AND o.moneyforward_journal_id_hash IS NOT NULL
          AND o.moneyforward_journal_id_hash != ''
        GROUP BY p.vendor_key, p.description_key, p.amount_bucket,
                 o.moneyforward_journal_id_hash, o.line_index
        HAVING support_count >= 1 AND variants = 1
        ORDER BY support_count DESC, trigger_key
        """
    ).fetchall()


def _stable_exact_pattern_date_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT 'exact_pattern_date_key' AS trigger_type,
               p.vendor_key || char(9) || p.description_key || char(9) || p.amount_bucket
                   || char(9) || o.transaction_date AS trigger_key,
               p.debit_account, p.debit_sub_account, p.credit_account,
               p.credit_sub_account, p.tax_category,
               COUNT(*) AS support_count,
               COUNT(DISTINCT p.debit_account || '|' || IFNULL(p.debit_sub_account, '') || '|'
                    || p.credit_account || '|' || IFNULL(p.credit_sub_account, '') || '|'
                    || p.tax_category) AS variants
        FROM journal_patterns p
        JOIN journal_pattern_occurrences o ON o.pattern_hash = p.pattern_hash
        WHERE p.amount_bucket != 'unknown'
          AND p.debit_account != 'unknown_debit_account'
          AND p.credit_account != 'unknown_credit_account'
          AND o.transaction_date IS NOT NULL
          AND o.transaction_date != ''
        GROUP BY p.vendor_key, p.description_key, p.amount_bucket, o.transaction_date
        HAVING support_count >= 1 AND variants = 1
        ORDER BY support_count DESC, trigger_key
        """
    ).fetchall()


def _stable_vendor_description_rows(
    conn: sqlite3.Connection,
    *,
    min_support_count: int,
) -> list[sqlite3.Row]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT vendor_key, description_key, debit_account, debit_sub_account,
                   credit_account, credit_sub_account, tax_category, success_count
            FROM journal_patterns
            WHERE description_key != 'no_description'
              AND debit_account != 'unknown_debit_account'
              AND credit_account != 'unknown_credit_account'
              AND tax_category != 'unknown_tax'
            ORDER BY vendor_key, description_key, id
            """
        ).fetchall()
    ]
    trade_partner_names = _normalized_trade_partner_names(_trade_partner_names(conn))
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        inferred_vendor_key = row["vendor_key"]
        if inferred_vendor_key == "unknown_vendor":
            inferred_vendor_key = _infer_vendor_from_description(row["description_key"], trade_partner_names)
            if inferred_vendor_key is None:
                continue
        normalized_description = _normalize_vendor_description_for_similarity(
            inferred_vendor_key,
            row["description_key"],
        )
        if not normalized_description:
            continue
        key = (inferred_vendor_key, normalized_description)
        group = grouped.setdefault(
            key,
            {
                "trigger_type": "vendor_description_key",
                "trigger_key": f"{inferred_vendor_key}\t{normalized_description}",
                "debit_account": row["debit_account"],
                "debit_sub_account": row["debit_sub_account"],
                "credit_account": row["credit_account"],
                "credit_sub_account": row["credit_sub_account"],
                "tax_category": row["tax_category"],
                "support_count": 0,
                "variants": {},
            },
        )
        group["support_count"] += int(row["success_count"] or 0)
        variant = (
            row["debit_account"],
            row["debit_sub_account"] or "",
            row["credit_account"],
            row["credit_sub_account"] or "",
            row["tax_category"],
        )
        group["variants"][variant] = group["variants"].get(variant, 0) + int(row["success_count"] or 0)
    result: list[dict] = []
    for group in grouped.values():
        if group["support_count"] < min_support_count:
            continue
        variants = group["variants"]
        top_variant, top_support = max(variants.items(), key=lambda item: (item[1], item[0]))
        if len(variants) != 1:
            if top_support / group["support_count"] < 0.75:
                continue
            if top_support < min_support_count:
                continue
        debit_account, debit_sub_account, credit_account, credit_sub_account, tax_category = top_variant
        result.append(
            {
                "trigger_type": group["trigger_type"],
                "trigger_key": group["trigger_key"],
                "debit_account": debit_account,
                "debit_sub_account": debit_sub_account or None,
                "credit_account": credit_account,
                "credit_sub_account": credit_sub_account or None,
                "tax_category": tax_category,
                "support_count": top_support if len(variants) != 1 else group["support_count"],
            }
        )
    return sorted(result, key=lambda row: (-row["support_count"], row["trigger_key"]))


def _stable_amount_bucket_rows(
    conn: sqlite3.Connection,
    *,
    min_support_count: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT amount_bucket, debit_account, debit_sub_account, credit_account, credit_sub_account,
               tax_category, SUM(success_count) AS support_count,
               COUNT(DISTINCT debit_account || '|' || IFNULL(debit_sub_account, '') || '|'
                    || credit_account || '|' || IFNULL(credit_sub_account, '') || '|'
                    || tax_category) AS variants
        FROM journal_patterns
        WHERE description_key = 'no_description'
          AND amount_bucket != 'unknown'
          AND debit_account != 'unknown_debit_account'
          AND credit_account != 'unknown_credit_account'
          AND tax_category != 'unknown_tax'
        GROUP BY amount_bucket, debit_account, debit_sub_account, credit_account, credit_sub_account, tax_category
        HAVING support_count >= ? AND variants = 1
        ORDER BY support_count DESC, amount_bucket, debit_account, credit_account, tax_category
        """,
        (min_support_count,),
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        result.append(
            {
                "trigger_type": "amount_bucket_key",
                "trigger_key": "|".join(
                    [
                        row["amount_bucket"],
                        row["debit_account"],
                        row["debit_sub_account"] or "",
                        row["credit_account"],
                        row["credit_sub_account"] or "",
                        row["tax_category"],
                    ]
                ),
                "debit_account": row["debit_account"],
                "debit_sub_account": row["debit_sub_account"] or None,
                "credit_account": row["credit_account"],
                "credit_sub_account": row["credit_sub_account"] or None,
                "tax_category": row["tax_category"],
                "support_count": row["support_count"],
            }
        )
    return result


def _stable_vendor_token_rows(
    conn: sqlite3.Connection,
    *,
    min_support_count: int,
) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    rows = conn.execute(
        """
        SELECT vendor_key, description_key, debit_account, debit_sub_account,
               credit_account, credit_sub_account, tax_category, success_count
        FROM journal_patterns
        WHERE vendor_key != 'unknown_vendor'
          AND description_key != 'no_description'
          AND debit_account != 'unknown_debit_account'
          AND credit_account != 'unknown_credit_account'
          AND tax_category != 'unknown_tax'
        ORDER BY vendor_key, description_key, id
        """
    ).fetchall()
    for row in rows:
        for token in _description_tokens(row["description_key"]):
            key = (row["vendor_key"], token)
            group = grouped.setdefault(
                key,
                {
                    "trigger_type": "vendor_token_key",
                    "trigger_key": f"{row['vendor_key']}\t{token}",
                    "support_count": 0,
                    "variants": {},
                },
            )
            success_count = int(row["success_count"] or 0)
            group["support_count"] += success_count
            variant = (
                row["debit_account"],
                row["debit_sub_account"] or "",
                row["credit_account"],
                row["credit_sub_account"] or "",
                row["tax_category"],
            )
            group["variants"][variant] = group["variants"].get(variant, 0) + success_count
    result: list[dict] = []
    for group in grouped.values():
        if group["support_count"] < min_support_count:
            continue
        variants = group["variants"]
        top_variant, top_support = max(variants.items(), key=lambda item: (item[1], item[0]))
        if len(variants) != 1:
            continue
        if top_support < min_support_count:
            continue
        debit_account, debit_sub_account, credit_account, credit_sub_account, tax_category = top_variant
        result.append(
            {
                "trigger_type": group["trigger_type"],
                "trigger_key": group["trigger_key"],
                "debit_account": debit_account,
                "debit_sub_account": debit_sub_account or None,
                "credit_account": credit_account,
                "credit_sub_account": credit_sub_account or None,
                "tax_category": tax_category,
                "support_count": top_support,
            }
        )
    return sorted(result, key=lambda row: (-row["support_count"], row["trigger_key"]))


def _confidence(support_count: int, *, trigger_type: str) -> float:
    if trigger_type in {
        "description_key",
        "vendor_description_key",
        "amount_bucket_key",
        "vendor_token_key",
        "exact_pattern_key",
        "exact_pattern_date_key",
        "occurrence_context_key",
        "source_event_context_key",
    }:
        return min(0.99, round(0.82 + (support_count / 200), 4))
    return min(0.99, round(0.70 + (support_count / 100), 4))


def _normalize_description_for_similarity(description: str) -> str:
    normalized = unicodedata.normalize("NFKC", description).casefold()
    normalized = re.sub(r"\bno\.?\s*[a-z0-9_-]+\b", " ", normalized)
    normalized = re.sub(r"[#\d〇零一二三四五六七八九十]{1,8}年", " ", normalized)
    normalized = re.sub(r"\br\s*\d{1,2}\s*[.年]?\s*\d{1,2}\s*月(?:度|分)?\b", " ", normalized)
    normalized = re.sub(r"\br\s*\d{1,2}\s*[.．]?\s*", " ", normalized)
    normalized = re.sub(r"\d{1,2}\s*月\s*[~〜\-]\s*\d{1,2}\s*月(?:度|分)?", " ", normalized)
    normalized = re.sub(r"\d{1,2}\s*月(?:度|分)?", " ", normalized)
    normalized = re.sub(r"\d[\d,]*(?:\.\d+)?\s*円", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _description_tokens(description: str) -> set[str]:
    normalized = _normalize_description_for_similarity(description)
    return {
        token
        for token in re.findall(r"[a-z0-9]+|[ぁ-んァ-ンー]{2,}|[一-龥々〆ヵヶ]{2,}", normalized)
        if len(token) >= 3 and token not in _DESCRIPTION_STOP_TOKENS
    }


def _normalize_vendor_description_for_similarity(vendor_key: str, description: str) -> str:
    normalized = _normalize_description_for_similarity(description)
    for alias in _vendor_name_aliases(vendor_key):
        normalized = re.sub(re.escape(alias.casefold()), " ", normalized)
    normalized = re.sub(r"(株式会社|有限会社|合同会社)", " ", normalized)
    normalized = re.sub(r"[・･]+", " ", normalized)
    compact = re.sub(r"\s+", "", normalized)
    for alias in _vendor_name_aliases(vendor_key):
        alias_compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", alias).casefold())
        if alias_compact:
            compact = compact.replace(alias_compact, "")
    compact = re.sub(r"(株式会社|有限会社|合同会社)", "", compact)
    compact = re.sub(r"[・･]+", "", compact)
    return compact.strip()


def _infer_vendor_from_description(description: str, trade_partner_names: list[str]) -> str | None:
    if not trade_partner_names:
        return None
    normalized_description = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", _normalize_description_for_similarity(description)).casefold(),
    )
    if not normalized_description:
        return None
    for vendor_name in trade_partner_names:
        if vendor_name and vendor_name in normalized_description:
            return vendor_name
    return None


def _normalized_trade_partner_names(trade_partner_names: list[str]) -> list[str]:
    normalized_names = []
    for name in trade_partner_names:
        normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name)).casefold())
        if len(normalized) < 3:
            continue
        normalized_names.append(normalized)
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


_DESCRIPTION_STOP_TOKENS = {
    "出金",
    "入金",
    "利用",
    "料金",
    "請求書",
    "株式会社",
    "有限会社",
    "合同会社",
    "沖縄県",
    "東京都",
}


def _vendor_name_aliases(vendor_key: str) -> list[str]:
    normalized_vendor_key = unicodedata.normalize("NFKC", vendor_key)
    aliases = {normalized_vendor_key}
    stripped = normalized_vendor_key
    for suffix in ("株式会社", "有限会社", "合同会社", "（有）", "(有)"):
        if stripped.endswith(suffix):
            aliases.add(stripped[: -len(suffix)])
    for suffix in ("株式会社", "有限会社", "合同会社", "（有）", "(有)"):
        stripped = stripped.replace(suffix, "")
    stripped = re.sub(r"\s+", "", stripped).strip()
    if stripped:
        aliases.add(stripped)
    return [alias for alias in aliases if alias]


def _rule_card(trigger_type: str, row: dict) -> dict[str, str]:
    body = " | ".join(
        [
            f"trigger_type={trigger_type}",
            f"trigger_key={row['trigger_key']}",
            f"debit={row['debit_account']}",
            f"debit_sub={row['debit_sub_account'] or ''}",
            f"credit={row['credit_account']}",
            f"credit_sub={row['credit_sub_account'] or ''}",
            f"tax={row['tax_category']}",
            f"support={row['support_count']}",
        ]
    )
    return {"card_type": "fixed_rule_candidate", "title": f"{trigger_type} {row['trigger_key']}"[:120], "body": body}


def _candidate_payload(row: dict) -> dict:
    return {
        "issue_type": row["issue_type"],
        "feature_match_score": row["feature_match_score"],
        "expected_pattern": {
            "debit_account": row["source_debit_account"],
            "debit_sub_account": row["source_debit_sub_account"],
            "credit_account": row["source_credit_account"],
            "credit_sub_account": row["source_credit_sub_account"],
            "tax_category": row["source_tax_category"],
        },
        "predicted_pattern": {
            "debit_account": row["predicted_debit_account"],
            "debit_sub_account": row["predicted_debit_sub_account"],
            "credit_account": row["predicted_credit_account"],
            "credit_sub_account": row["predicted_credit_sub_account"],
            "tax_category": row["predicted_tax_category"],
        },
    }


def _candidate_card(row: dict, failure_hash: str) -> dict[str, str]:
    body = " | ".join(
        [
            f"issue={row['issue_type']}",
            f"trigger={row['source_description_key']}",
            f"expected_debit={row['source_debit_account']}",
            f"expected_sub={row['source_debit_sub_account'] or ''}",
            f"expected_tax={row['source_tax_category']}",
            f"predicted_debit={row['predicted_debit_account'] or ''}",
            f"predicted_sub={row['predicted_debit_sub_account'] or ''}",
            f"predicted_tax={row['predicted_tax_category'] or ''}",
            f"block={row['blocking_reason']}",
        ]
    )
    return {
        "card_type": "failure_example",
        "title": f"classification failure {row['issue_type']} {failure_hash[:12]}",
        "body": body[:500],
    }
