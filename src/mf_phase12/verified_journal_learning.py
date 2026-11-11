from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .financial_event_classification import normalize_financial_event
from .google_chat_notify import (
    _accounting_run_id_hash,
    _notification_item_hashes,
    _notification_scope,
)
from .hash_utils import stable_hash
from .journal_pattern_provenance import (
    PAYROLL_LEARNING_SOURCE,
    VERIFIED_LEARNING_SOURCE,
    record_journal_pattern_provenance,
)
from .local_artifacts import require_ignored_untracked_data_path
from .rag import build_rag_cards, upsert_sync_manifest
from .shiwake_run import _record_run_receipt


VERIFIED_READBACK_STATUSES = {"verified", "passed"}


@dataclass(frozen=True)
class VerifiedJournalLearningResult:
    journal_patterns_upserted: int
    rag_cards_upserted: int
    payroll_rules_upserted: int
    complex_journal_items_skipped: int
    malformed_patterns_quarantined: int
    persisted: bool

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "journal_patterns_upserted": self.journal_patterns_upserted,
            "rag_cards_upserted": self.rag_cards_upserted,
            "payroll_rules_upserted": self.payroll_rules_upserted,
            "complex_journal_items_skipped": self.complex_journal_items_skipped,
            "malformed_patterns_quarantined": self.malformed_patterns_quarantined,
            "persisted": self.persisted,
            "raw_amounts_printed": False,
            "raw_personal_data_stored": False,
            "raw_secret_stored": False,
        }


def learn_verified_workflow_from_files(
    *,
    completion_summary_json: Path | None,
    payroll_summary_json: Path | None,
    output_json: Path,
    conn: sqlite3.Connection,
    persist: bool = True,
) -> VerifiedJournalLearningResult:
    completion_payload = None
    payroll_payload = None
    if completion_summary_json is not None:
        completion_summary_json = require_ignored_untracked_data_path(
            completion_summary_json,
            purpose="verified journal completion summary",
        )
        completion_payload = json.loads(completion_summary_json.read_text(encoding="utf-8"))
    if payroll_summary_json is not None:
        payroll_summary_json = require_ignored_untracked_data_path(
            payroll_summary_json,
            purpose="verified payroll summary",
        )
        payroll_payload = json.loads(payroll_summary_json.read_text(encoding="utf-8"))
    output_json = require_ignored_untracked_data_path(output_json, purpose="verified journal learning output")
    result = learn_verified_workflow(
        completion_summary=completion_payload,
        payroll_summary=payroll_payload,
        conn=conn,
        persist=persist,
    )
    receipt_hash = None
    if persist and isinstance(completion_payload, dict):
        receipt_hash = _record_learning_run_receipt_if_active(
            completion_payload,
            result,
            conn,
        )
    output_payload = result.to_redacted_dict()
    if receipt_hash:
        output_payload["accounting_run_receipt_hash"] = receipt_hash
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _require_matching_sent_completion_notification(
    completion_summary: dict[str, Any] | None,
    conn: sqlite3.Connection,
) -> None:
    """Require durable Google Chat delivery for every item in the same accounting run."""
    if not isinstance(completion_summary, dict):
        raise SystemExit("persisted verified learning requires a completion summary")
    accounting_run_id_hash = _accounting_run_id_hash(completion_summary)
    if accounting_run_id_hash is None:
        raise SystemExit("persisted verified learning requires accounting_run_id")
    items = completion_summary.get("items")
    item_hashes = _notification_item_hashes(
        "completion",
        items,
        accounting_run_id_hash=accounting_run_id_hash,
        notification_scope=_notification_scope(completion_summary),
    )
    if not isinstance(items, list) or not items or len(item_hashes) != len(items):
        raise SystemExit("persisted verified learning requires identifiable completion items")

    placeholders = ",".join("?" for _ in item_hashes)
    rows = conn.execute(
        f"""
        SELECT i.item_hash
        FROM google_chat_notification_items AS i
        JOIN google_chat_notification_records AS r
          ON r.notification_hash = i.notification_hash
        WHERE i.item_hash IN ({placeholders})
          AND i.accounting_run_id_hash = ?
          AND r.accounting_run_id_hash = ?
          AND i.status IN ('sent', 'historical_sent')
          AND r.status IN ('sent', 'historical_sent')
          AND i.sent_at IS NOT NULL
          AND r.sent_at IS NOT NULL
          AND (r.status = 'historical_sent' OR r.response_hash IS NOT NULL)
        """,
        (*item_hashes, accounting_run_id_hash, accounting_run_id_hash),
    ).fetchall()
    delivered_item_hashes = {str(row["item_hash"]) for row in rows}
    if delivered_item_hashes == set(item_hashes):
        return
    expected_dedup_source_hash = stable_hash(
        {
            "accounting_run_id_hash": accounting_run_id_hash,
            "already_sent_message_hash": stable_hash(
                {"already_sent": item_hashes}
            ),
            "blocked_reasons": ["all_items_already_recorded"],
        }
    )
    dedup_receipt = conn.execute(
        """
        SELECT r.receipt_hash
        FROM accounting_run_receipts AS r
        JOIN accounting_run_stages AS s
          ON s.run_id_hash = r.run_id_hash
         AND s.stage_key = r.stage_key
        WHERE r.run_id_hash = ?
          AND r.stage_key = 'google_chat_durable_sent'
          AND r.receipt_kind = 'google_chat_dedup_readback'
          AND r.status = 'verified'
          AND r.source_hash = ?
          AND s.status = 'succeeded'
          AND s.binding_hash = r.receipt_hash
        LIMIT 1
        """,
        (accounting_run_id_hash, expected_dedup_source_hash),
    ).fetchone()
    if dedup_receipt is None:
        raise SystemExit(
            "persisted verified learning requires sent Google Chat notification "
            "or a verified dedup read-back receipt for every completion item "
            "in the same accounting run"
        )


def learn_verified_workflow(
    *,
    completion_summary: dict[str, Any] | None,
    payroll_summary: dict[str, Any] | None,
    conn: sqlite3.Connection,
    persist: bool = True,
) -> VerifiedJournalLearningResult:
    if persist:
        _require_matching_sent_completion_notification(completion_summary, conn)
    journal_cards: list[dict[str, str]] = []
    journal_patterns = 0
    payroll_rules = 0
    complex_journal_items_skipped = 0
    malformed_patterns_quarantined = quarantine_malformed_verified_journal_learning(conn) if persist else 0

    for item in _verified_completion_items(completion_summary):
        try:
            normalized, candidate = _completion_item_to_pattern_parts(item)
        except ValueError:
            complex_journal_items_skipped += 1
            continue
        if persist:
            pattern_hash = _upsert_journal_pattern(conn, normalized, candidate, auto_post_eligibility="limited")
            record_journal_pattern_provenance(
                conn,
                pattern_hash=pattern_hash,
                source_kind=VERIFIED_LEARNING_SOURCE,
            )
        journal_patterns += 1
        journal_cards.append(_journal_pattern_card(item, normalized, candidate))

    if payroll_summary:
        payroll_cards, payroll_pattern_candidates = _payroll_learning_cards_and_patterns(payroll_summary)
        journal_cards.extend(payroll_cards)
        payroll_rules = len(payroll_cards)
        if persist:
            for normalized, candidate in payroll_pattern_candidates:
                pattern_hash = _upsert_journal_pattern(conn, normalized, candidate, auto_post_eligibility="limited")
                record_journal_pattern_provenance(
                    conn,
                    pattern_hash=pattern_hash,
                    source_kind=PAYROLL_LEARNING_SOURCE,
                )
                journal_patterns += 1

    rag_cards = 0
    if persist and journal_cards:
        source_hash = upsert_sync_manifest(conn, "local", "verified_journal_learning", journal_cards)
        rag_cards = build_rag_cards(conn, journal_cards, source_hash)
        if payroll_rules:
            upsert_sync_manifest(conn, "local", "payroll_verified_journal_learning", journal_cards[-payroll_rules:])
        conn.commit()

    return VerifiedJournalLearningResult(
        journal_patterns_upserted=journal_patterns,
        rag_cards_upserted=rag_cards,
        payroll_rules_upserted=payroll_rules,
        complex_journal_items_skipped=complex_journal_items_skipped,
        malformed_patterns_quarantined=malformed_patterns_quarantined,
        persisted=persist,
    )


def learn_verified_payroll_rules(
    *,
    payroll_summary: dict[str, Any],
    conn: sqlite3.Connection,
    journal_readback_verified: bool,
    voucher_readback_verified: bool,
) -> int:
    """Upsert stable payroll cards only after both official read-backs pass."""

    if not journal_readback_verified or not voucher_readback_verified:
        raise ValueError("payroll learning requires journal and PDF attachment read-back")
    cards, _patterns = _payroll_learning_cards_and_patterns(payroll_summary)
    source_hash = upsert_sync_manifest(conn, "local", "payroll_verified_journal_learning", cards)
    count = build_rag_cards(conn, cards, source_hash)
    conn.commit()
    return count


def _record_learning_run_receipt_if_active(
    completion_summary: dict[str, Any],
    result: VerifiedJournalLearningResult,
    conn: sqlite3.Connection,
) -> str | None:
    run_id = str(completion_summary.get("accounting_run_id") or "").strip()
    if not run_id:
        return None
    run_hash = _accounting_run_id_hash(completion_summary)
    row = conn.execute(
        """
        SELECT current_stage, run_mode
        FROM accounting_runs
        WHERE run_id_hash = ?
        """,
        (run_hash,),
    ).fetchone()
    if row is None or row["current_stage"] != "rag_learning" or row["run_mode"] != "production":
        return None
    source_hash = "sha256:" + stable_hash(
        {
            "run_id_hash": run_hash,
            "learning_result": result.to_redacted_dict(),
        }
    )
    return _record_run_receipt(
        conn,
        run_id=run_id,
        stage="rag_learning",
        receipt_kind="verified_journal_learning",
        source_hash=source_hash,
        actor="verified_journal_learning",
    )


def _verified_completion_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    verified: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        readback = str(
            item.get("registration_readback_status")
            or item.get("moneyforward_registration_readback")
            or item.get("readback_status")
            or ""
        )
        processed = str(item.get("processed_folder_status") or "")
        if readback not in VERIFIED_READBACK_STATUSES:
            continue
        evidence_required = item.get("evidence_required", True) is not False
        voucher_status = str(item.get("voucher_status") or "")
        if evidence_required and voucher_status != "verified":
            continue
        if not evidence_required and voucher_status not in {"", "not_required", "verified"}:
            continue
        if processed not in {"verified", "not_required"}:
            continue
        verified.append(item)
    return verified


def _completion_item_to_pattern_parts(item: dict[str, Any]) -> tuple[dict[str, str], dict[str, str | None]]:
    debit_path, credit_path = _completion_item_paths(item)
    debit = _parse_debit_path(debit_path)
    credit = _parse_credit_path(credit_path)
    event = {
        "source_event_type": "verified_journal",
        "source_event_id": f"journal:{item.get('journal_number') or stable_hash(item)[:12]}",
        "transaction_date": str(item.get("date") or "")[:10],
        "description": str(item.get("description") or item.get("label") or ""),
        "counterparty": str(item.get("label") or item.get("vendor_name") or ""),
        "amount": int(item.get("amount") or 0),
        "cash_flow_direction": "outflow",
    }
    normalized = normalize_financial_event(event)
    candidate = {
        "debit_account": debit["account"],
        "debit_sub_account": debit["sub_account"],
        "credit_account": credit["account"],
        "credit_sub_account": credit["sub_account"],
        "tax_category": debit["tax_category"],
    }
    return normalized, candidate


def _completion_item_paths(item: dict[str, Any]) -> tuple[str, str]:
    debit_path = str(item.get("debit") or "").strip()
    credit_path = str(item.get("credit") or "").strip()
    summary = str(item.get("journal_summary") or "").strip()
    if summary and "→" in summary:
        summary_debit, summary_credit = (part.strip() for part in summary.split("→", 1))
        debit_path = debit_path or summary_debit
        credit_path = credit_path or summary_credit
    if not debit_path or not credit_path:
        raise ValueError("verified completion item is missing a complete debit/credit path")
    # Chat summaries deliberately collapse multi-line journals. They are not a safe RAG source.
    if " + " in debit_path or "/・" in debit_path:
        raise ValueError("verified completion item has a compound debit path")
    return debit_path, credit_path


def _upsert_journal_pattern(
    conn: sqlite3.Connection,
    normalized: dict[str, str],
    candidate: dict[str, str | None],
    *,
    auto_post_eligibility: str,
) -> str:
    pattern_hash = _verified_journal_pattern_hash(normalized, candidate)
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
            auto_post_eligibility = excluded.auto_post_eligibility,
            last_seen_at = excluded.last_seen_at
        """,
        (
            pattern_hash,
            normalized["vendor_key"],
            normalized["description_key"],
            normalized["amount_bucket"],
            candidate["debit_account"],
            candidate["debit_sub_account"],
            candidate["credit_account"],
            candidate["credit_sub_account"],
            candidate["tax_category"],
            auto_post_eligibility,
            normalized["transaction_date"],
        ),
    )
    return pattern_hash


def _verified_journal_pattern_hash(
    normalized: dict[str, str], candidate: dict[str, str | None]
) -> str:
    return stable_hash(
        {
            "source": "verified_journal_learning",
            "vendor_key": normalized["vendor_key"],
            "description_key": normalized["description_key"],
            "amount_bucket": normalized["amount_bucket"],
            "cash_flow_direction": normalized["cash_flow_direction"],
            **candidate,
        }
    )


def _record_verified_sales_invoice_occurrence(
    conn: sqlite3.Connection,
    *,
    pattern_hash: str,
    journal_id_hash: str,
    transaction_date: str,
    response_hash: str,
) -> None:
    occurrence_hash = stable_hash(
        {
            "source": "verified_sales_invoice_official_readback",
            "pattern_hash": pattern_hash,
            "moneyforward_journal_id_hash": journal_id_hash,
            "line_index": 0,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_pattern_occurrences (
            occurrence_hash,pattern_hash,moneyforward_journal_id_hash,
            line_index,transaction_date,source_range_hash,raw_data_stored
        ) VALUES (?,?,?,?,?,?,0)
        """,
        (
            occurrence_hash,
            pattern_hash,
            journal_id_hash,
            0,
            transaction_date,
            stable_hash(
                {
                    "official_readback_response_hash": response_hash
                    or journal_id_hash,
                }
            ),
        ),
    )


def learn_verified_sales_invoice_pattern(
    *,
    conn: sqlite3.Connection,
    normalized: dict[str, str],
    candidate: dict[str, str | None],
    official_readback: dict[str, Any],
) -> dict[str, Any]:
    """Persist one official-GET-verified sales shape through the existing RAG path."""

    if official_readback.get("status") != "verified":
        raise ValueError("sales invoice learning requires verified official read-back")
    journal_id_hash = str(official_readback.get("journal_id_hash") or "")
    if not journal_id_hash:
        raise ValueError("sales invoice learning requires a redacted journal identity")
    required = {
        "debit_account",
        "debit_sub_account",
        "credit_account",
        "credit_sub_account",
        "tax_category",
        "trade_partner_mode",
    }
    if any(not str(candidate.get(key) or "").strip() for key in required):
        raise ValueError("sales invoice learning requires a complete journal shape")
    if candidate["debit_account"] != "未収入金" or candidate["credit_account"] != "売上高":
        raise ValueError("sales invoice learning accepts only the sales accrual shape")
    if candidate["trade_partner_mode"] not in {
        "both",
        "debit_only",
        "credit_only",
        "none",
    }:
        raise ValueError("sales invoice learning requires a verified trade-partner mode")

    pattern_hash = _verified_journal_pattern_hash(normalized, candidate)
    existing = conn.execute(
        """
        SELECT 1
        FROM journal_pattern_provenance
        WHERE pattern_hash = ? AND source_kind = ?
        """,
        (pattern_hash, VERIFIED_LEARNING_SOURCE),
    ).fetchone()
    if existing is not None:
        _record_verified_sales_invoice_occurrence(
            conn,
            pattern_hash=pattern_hash,
            journal_id_hash=journal_id_hash,
            transaction_date=normalized["transaction_date"],
            response_hash=str(official_readback.get("response_hash") or ""),
        )
        conn.commit()
        return {
            "status": "already_learned",
            "pattern_hash": pattern_hash,
            "rag_cards_upserted": 0,
            "official_readback_verified": True,
            "raw_amount_stored": False,
            "raw_journal_id_stored": False,
        }

    pattern_hash = _upsert_journal_pattern(
        conn,
        normalized,
        candidate,
        auto_post_eligibility="limited",
    )
    record_journal_pattern_provenance(
        conn,
        pattern_hash=pattern_hash,
        source_kind=VERIFIED_LEARNING_SOURCE,
        source_ref_hash=journal_id_hash,
    )
    _record_verified_sales_invoice_occurrence(
        conn,
        pattern_hash=pattern_hash,
        journal_id_hash=journal_id_hash,
        transaction_date=normalized["transaction_date"],
        response_hash=str(official_readback.get("response_hash") or ""),
    )
    card = _journal_pattern_card(
        {
            "journal_number": "",
            "label": normalized["vendor_key"],
        },
        normalized,
        candidate,
    )
    source_hash = upsert_sync_manifest(
        conn,
        "local",
        "verified_journal_learning",
        [card],
    )
    rag_cards_upserted = build_rag_cards(conn, [card], source_hash)
    conn.commit()
    return {
        "status": "learned",
        "pattern_hash": pattern_hash,
        "rag_cards_upserted": rag_cards_upserted,
        "official_readback_verified": True,
        "raw_amount_stored": False,
        "raw_journal_id_stored": False,
    }


def _journal_pattern_card(
    item: dict[str, Any],
    normalized: dict[str, str],
    candidate: dict[str, str | None],
) -> dict[str, str]:
    number = str(item.get("journal_number") or "")
    title_number = f"No.{number} " if number else ""
    return {
        "card_type": "journal_pattern",
        "title": f"verified {title_number}{normalized['vendor_key']} {normalized['description_key']}"[:120],
        "body": " | ".join(
            [
                "source=verified_journal_learning",
                f"journal_number={number or 'none'}",
                f"description={normalized['description_key']}",
                f"amount_bucket={normalized['amount_bucket']}",
                f"debit={candidate['debit_account']}/{candidate['debit_sub_account'] or ''}",
                f"credit={candidate['credit_account']}/{candidate['credit_sub_account'] or ''}",
                f"tax={candidate['tax_category']}",
                *(
                    [f"trade_partner_mode={candidate['trade_partner_mode']}"]
                    if candidate.get("trade_partner_mode")
                    else []
                ),
                "eligibility=limited",
            ]
        ),
    }


def _payroll_learning_cards_and_patterns(
    payroll_summary: dict[str, Any],
) -> tuple[list[dict[str, str]], list[tuple[dict[str, str], dict[str, str | None]]]]:
    correct_date = str(payroll_summary.get("correct_date") or payroll_summary.get("accrual_date") or "period_end")
    payment_due = str(payroll_summary.get("payment_due") or "next_month_10")
    cards = [
        {
            "card_type": "journal_pattern",
            "title": "給与発生 月末締め 翌月10日支払",
            "body": " | ".join(
                [
                    "source=payroll_verified_journal_learning",
                    f"correct_date={correct_date}",
                    f"payment_due={payment_due}",
                    "debit=給料手当 and 法定福利費",
                    "credit=未払費用/<employee> and 預り金/<deduction> and 未払費用/社会保険料",
                    "negative_net=do_not_use_未収入金",
                    "negative_net_treatment=employee_unpaid_debit_offset_against_withholding_or_social_insurance",
                    "tax=対象外",
                    "personal_raw_amounts_stored=false",
                ]
            ),
        },
        {
            "card_type": "fixed_rule_candidate",
            "title": "給与控除 預り金ルール",
            "body": " | ".join(
                [
                    "source=payroll_verified_journal_learning",
                    "deductions=源泉所得税,住民税,健康保険料,厚生年金保険料,雇用保険料",
                    "account=預り金",
                    "negative_net=do_not_book_as_revenue_or_accounts_receivable",
                    "settlement=social_insurance_payment_clears_withholding_and_accrued_company_liability",
                    "settlement_month=derive_from_ordinary_deposit_withdrawal_date_not_same_day_payroll_journal",
                    "month_end_weekend_withdrawal_may_post_next_business_day",
                    "do_not_double_book_company_social_insurance_expense_when_unpaid_expense_exists",
                ]
            ),
        },
    ]
    # Summary policy belongs in RAG cards only. Never promote placeholders such
    # as 未払費用/従業員 to executable patterns; those come from verified
    # employee-specific Money Forward read-back.
    return cards, []


def _parse_debit_path(value: str) -> dict[str, str | None]:
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if not parts:
        return {"account": "未分類", "sub_account": None, "tax_category": "対象外"}
    account = parts[0]
    tax_category = "対象外"
    sub_parts = parts[1:]
    if sub_parts and _looks_like_tax(sub_parts[-1]):
        tax_category = sub_parts[-1]
        sub_parts = sub_parts[:-1]
    return {
        "account": account,
        "sub_account": "/".join(sub_parts) or None,
        "tax_category": tax_category,
    }


def _parse_credit_path(value: str) -> dict[str, str | None]:
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if not parts:
        return {"account": "未分類", "sub_account": None}
    return {"account": parts[0], "sub_account": "/".join(parts[1:]) or None}


def _looks_like_tax(value: str) -> bool:
    return any(marker in value for marker in ("税", "課仕", "対象外", "%", "軽8", "非仕", "非課税", "不課税"))


def quarantine_malformed_verified_journal_learning(conn: sqlite3.Connection) -> int:
    """Disable only the historic one-sided rows made from a collapsed Chat summary."""
    bad_subaccount_fragments = ("/普通預金/", "/未払金/", "/未収入金/", "/預り金/")
    rows = conn.execute(
        """
        SELECT pattern_hash
        FROM journal_patterns
        WHERE credit_account = '未払金'
          AND COALESCE(credit_sub_account, '') = ''
          AND (
              debit_sub_account LIKE ? OR debit_sub_account LIKE ?
              OR debit_sub_account LIKE ? OR debit_sub_account LIKE ?
          )
        """,
        tuple(f"%{fragment}%" for fragment in bad_subaccount_fragments),
    ).fetchall()
    if not rows:
        return 0
    conn.executemany(
        """
        UPDATE journal_patterns
        SET auto_post_eligibility = 'disabled', failure_count = failure_count + 1
        WHERE pattern_hash = ?
        """,
        [(str(row["pattern_hash"]),) for row in rows],
    )
    conn.execute(
        """
        DELETE FROM rag_cards
        WHERE card_type = 'journal_pattern'
          AND body LIKE 'source=verified_journal_learning%'
          AND body LIKE '%credit=未払金/%'
          AND (
              body LIKE '%/普通預金/%' OR body LIKE '%/未払金/%'
              OR body LIKE '%/未収入金/%' OR body LIKE '%/預り金/%'
          )
        """
    )
    conn.commit()
    return len(rows)
