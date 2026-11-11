from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from dataclasses import replace

from .drive_policy import apply_drive_credit_policy, company_policies
from .hash_utils import stable_hash
from .models import Evidence, JournalCandidate
from .rag import search_rag


@dataclass(frozen=True)
class EvidenceFacts:
    observed: dict
    predicted: dict
    unresolved_points: tuple[str, ...]


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    risk_flags: tuple[str, ...]
    reason: str


def _apply_configured_rules(candidate: JournalCandidate, path: str, text: str) -> JournalCandidate:
    matches = []
    for rule in company_policies(path):
        aliases = rule.get("description_contains", [])
        if not isinstance(aliases, (list, tuple)) or not aliases or any(not str(x).strip() for x in aliases):
            raise ValueError("receipt rule requires explicit nonempty description conditions")
        if any(str(alias) in text for alias in aliases):
            matches.append(rule)
    if len(matches) > 1:
        raise ValueError("ambiguous company receipt rule")
    if not matches:
        return candidate
    fields = matches[0].get("candidate", {})
    if not isinstance(fields, dict) or not fields or set(fields) - {"debit_account", "debit_sub_account", "tax_category", "tax_amount", "tax_rate"}:
        raise ValueError("receipt rule contains unsupported fields")
    return replace(candidate, **fields)


def apply_fixed_rules(candidate: JournalCandidate, evidence: Evidence | None = None) -> JournalCandidate:
    fixed = _apply_configured_rules(candidate, "accounting_rules.receipt_fixed_rules", candidate.description)
    return apply_drive_credit_policy(fixed, evidence) if evidence is not None else fixed


def apply_receipt_usage_rules(candidate: JournalCandidate, usage_text: str | None = None) -> JournalCandidate:
    return _apply_configured_rules(
        candidate, "accounting_rules.receipt_usage_rules",
        " ".join((candidate.description or "", usage_text or "")),
    )


def receipt_broad_content(value: str | None) -> str:
    """Return only a coarse category explicitly supported by current receipt text."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if any(marker in text for marker in ("飲食", "食事", "会食")):
        return "飲食"
    if "宿泊" in text:
        return "宿泊"
    if any(marker in text for marker in ("燃料", "給油")):
        return "燃料"
    if any(marker in text for marker in ("駐車", "パーキング", "交通")):
        return "交通"
    if any(marker in text for marker in ("事務用品", "消耗品")):
        return "事務用消耗品"
    if "日用品" in text:
        return "日用品"
    if "専門サービス" in text:
        return "専門サービス"
    return ""


def detect_failure_examples(candidate: JournalCandidate) -> tuple[str, ...]:
    expected = _apply_configured_rules(candidate, "accounting_rules.receipt_fixed_rules", candidate.description)
    return ("COMPANY_FIXED_RULE_VIOLATION",) if expected != candidate else ()


def classify_receipt_from_facts(facts: EvidenceFacts) -> JournalCandidate:
    observed = facts.observed
    predicted = facts.predicted
    candidate = JournalCandidate(
        journal_date=str(observed["date"]),
        vendor_name=str(observed.get("vendor_name", "")),
        amount=int(observed["amount"]),
        tax_amount=int(observed.get("tax_amount") or 0),
        tax_rate=float(observed.get("tax_rate") or 0.0),
        debit_account=str(predicted.get("debit_account") or "未分類"),
        debit_sub_account=predicted.get("debit_sub_account"),
        credit_account=str(predicted.get("credit_account") or "未分類"),
        credit_sub_account=predicted.get("credit_sub_account"),
        tax_category=str(predicted.get("tax_category") or "対象外"),
        description=str(predicted.get("description") or observed.get("description") or observed.get("vendor_name") or ""),
        invoice_number=observed.get("invoice_number"),
        trade_partner_code=predicted.get("trade_partner_code"),
        trade_partner_name=predicted.get("trade_partner_name") or observed.get("vendor_name"),
        source_type=str(observed.get("source_type") or "receipt"),
        risk_flags=tuple(facts.unresolved_points),
    )
    evidence = Evidence(
        google_drive_file_id=str(observed.get("evidence_file_id") or "unknown"),
        file_name=str(observed.get("file_name") or ""),
        file_hash=str(observed.get("file_hash") or ""),
        parent_folder_id=observed.get("parent_folder_id"),
        parent_folder_name=observed.get("parent_folder_name"),
        local_file_path=observed.get("local_file_path"),
    )
    return apply_receipt_usage_rules(
        apply_fixed_rules(candidate, evidence),
        str(observed.get("description") or ""),
    )


def apply_learned_rules(conn: sqlite3.Connection, candidate: JournalCandidate) -> JournalCandidate:
    candidate = _apply_vendor_profile(conn, candidate)
    candidate = _apply_fixed_rule_candidate(conn, candidate)
    return candidate


def _apply_vendor_profile(conn: sqlite3.Connection, candidate: JournalCandidate) -> JournalCandidate:
    rows = conn.execute(
        """
        SELECT vendor_key, default_debit_account, default_debit_sub_account,
               default_credit_account, default_credit_sub_account,
               default_tax_category, confidence_score
        FROM vendor_profiles
        WHERE confidence_score >= 0.8
        ORDER BY confidence_score DESC, updated_at DESC
        """
    ).fetchall()
    for row in rows:
        vendor_key = str(row["vendor_key"])
        if vendor_key and vendor_key in candidate.vendor_name:
            return replace(
                candidate,
                debit_account=row["default_debit_account"] or candidate.debit_account,
                debit_sub_account=row["default_debit_sub_account"] or candidate.debit_sub_account,
                credit_account=row["default_credit_account"] or candidate.credit_account,
                credit_sub_account=row["default_credit_sub_account"] or candidate.credit_sub_account,
                tax_category=row["default_tax_category"] or candidate.tax_category,
            )
    return candidate


def _apply_fixed_rule_candidate(conn: sqlite3.Connection, candidate: JournalCandidate) -> JournalCandidate:
    rows = conn.execute(
        """
        SELECT trigger_key, debit_account, debit_sub_account, credit_account,
               credit_sub_account, tax_category, confidence_score
        FROM fixed_rule_candidates
        WHERE trigger_type = 'description_key'
          AND status IN ('shadow', 'approved')
          AND confidence_score >= 0.8
        ORDER BY confidence_score DESC, support_count DESC
        """
    ).fetchall()
    for row in rows:
        trigger = str(row["trigger_key"])
        if trigger and trigger in candidate.description:
            return replace(
                candidate,
                debit_account=row["debit_account"],
                debit_sub_account=(
                    row["debit_sub_account"]
                    if row["debit_sub_account"] is not None
                    else candidate.debit_sub_account
                ),
                credit_account=row["credit_account"],
                credit_sub_account=(
                    row["credit_sub_account"]
                    if row["credit_sub_account"] is not None
                    else candidate.credit_sub_account
                ),
                tax_category=row["tax_category"],
            )
    return candidate


def compare_with_past_journals(conn: sqlite3.Connection, candidate: JournalCandidate) -> dict:
    query = f"{candidate.vendor_name} {candidate.description} {candidate.debit_account}"
    matches = search_rag(conn, query)
    if not matches:
        matches = search_rag(conn, candidate.debit_account)
    if not matches and candidate.vendor_name:
        matches = search_rag(conn, candidate.vendor_name[:6])
    failure_matches = [
        match
        for match in search_rag(conn, candidate.description or candidate.debit_account)
        if match.get("card_type") == "failure_example"
    ]
    failure_blocks = [_failure_card_blocks_candidate(match, candidate) for match in failure_matches]
    match_text = " ".join(f"{match.get('title', '')} {match.get('body', '')}" for match in matches)
    aligned = all(
        value in match_text
        for value in [candidate.debit_account, candidate.credit_account, candidate.tax_category]
        if value
    )
    return {
        "query_hash": stable_hash(query),
        "match_count": len(matches),
        "aligned": aligned,
        "top_cards": [
            {"card_type": match["card_type"], "title": match["title"][:80], "body": match["body"][:120]}
            for match in matches[:3]
        ],
        "failure_match_count": len(failure_matches),
        "failure_blocked": any(failure_blocks),
        "failure_cards": [
            {"card_type": match["card_type"], "title": match["title"][:80], "body": match["body"][:120]}
            for match, blocked in zip(failure_matches, failure_blocks)
            if blocked
        ][:3],
    }


def verify_candidate(candidate: JournalCandidate, past_comparison: dict | None = None) -> VerificationResult:
    flags = list(detect_failure_examples(candidate))
    if candidate.amount <= 0:
        flags.append("AMOUNT_NOT_POSITIVE")
    if candidate.tax_category == "対象外" and candidate.tax_amount != 0:
        flags.append("TAX_OUT_OF_SCOPE_WITH_TAX_AMOUNT")
    if past_comparison is not None and not past_comparison.get("aligned"):
        flags.append("PAST_JOURNAL_MISMATCH")
    if past_comparison is not None and past_comparison.get("failure_blocked"):
        flags.append("FAILURE_RAG_MATCH")
    return VerificationResult(
        passed=not flags,
        risk_flags=tuple(flags),
        reason="passed" if not flags else ",".join(flags),
    )


def _failure_card_blocks_candidate(match: dict, candidate: JournalCandidate) -> bool:
    fields = _parse_card_body(str(match.get("body") or ""))
    predicted_debit = fields.get("predicted_debit")
    expected_debit = fields.get("expected_debit")
    predicted_sub = fields.get("predicted_sub")
    expected_sub = fields.get("expected_sub")
    predicted_tax = fields.get("predicted_tax")
    expected_tax = fields.get("expected_tax")
    return any(
        [
            bool(predicted_debit and predicted_debit == candidate.debit_account and expected_debit != predicted_debit),
            bool(
                predicted_sub
                and (candidate.debit_sub_account or "") == predicted_sub
                and (expected_sub or "") != predicted_sub
            ),
            bool(predicted_tax and predicted_tax == candidate.tax_category and expected_tax != predicted_tax),
        ]
    )


def _parse_card_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in body.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields
