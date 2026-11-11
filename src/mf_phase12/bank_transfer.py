from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .financial_event_classification import (
    FinancialEventClassification,
    _attach_moneyforward_transaction_binding,
    _find_pattern_matches,
    _journal_candidate_from_pattern,
    classify_financial_events,
    normalize_financial_event,
)
from .hash_utils import stable_hash
from .live_journal_resolver import (
    _external_id,
    _name,
    _unique_named,
    _unique_sub_account,
)


_AI_FIXED_FACT_FIELDS = {
    "amount",
    "cash_flow_direction",
    "connected_account",
    "connected_ledger_account_id",
    "date",
    "journal_date",
    "transaction_date",
    "transaction_id",
    "transaction_side",
}
_AI_DECISION_FIELDS = {
    "basis_codes",
    "counterpart_account",
    "counterpart_sub_account",
    "evidence_hashes",
    "packet_hash",
    "reason_codes",
    "status",
    "tax_category",
}
_AI_BASIS_CODES = {
    "accounting_rule",
    "canonical_evidence",
    "current_bank_facts",
    "official_related_information",
}
_AI_REVIEW_REASON_CODES = {
    "candidate_evidence_conflict",
    "candidate_tie_after_current_facts",
    "necessary_evidence_missing",
    "specific_bank_fact_missing",
}


@dataclass(frozen=True)
class BankDecisionBatch:
    items: list[FinancialEventClassification]
    decision_sources: list[str]
    review_reasons: list[list[str]]
    ai_packets: list[dict[str, Any]] = field(default_factory=list)

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_local_dict() for item in self.items],
            "decision_sources": self.decision_sources,
            "review_reasons": self.review_reasons,
            "ai_packets": self.ai_packets,
        }


def decide_bank_transactions(
    transactions: Sequence[dict[str, Any]],
    *,
    conn: sqlite3.Connection,
    masters: Mapping[str, list[dict[str, Any]]],
    related_candidates: Sequence[dict[str, Any]] = (),
    ai_decisions: Mapping[str, Mapping[str, Any]] | None = None,
    persist: bool = True,
) -> BankDecisionBatch:
    """Resolve each bank row using current facts, official RAG and related journals.

    Money Forward identity, connected-account side, amount and direction never
    enter a candidate-controlled field.  Candidate sources may resolve only the
    opposite account/sub-account/tax fields.
    """

    baseline = classify_financial_events(
        {"events": list(transactions)}, conn=conn, persist=False, engine="python"
    ).items
    decisions: list[FinancialEventClassification] = []
    sources: list[str] = []
    reasons: list[list[str]] = []
    ai_packets: list[dict[str, Any]] = []
    supplied_ai_decisions = ai_decisions or {}
    for raw_event, classified in zip(transactions, baseline, strict=True):
        event = dict(raw_event)
        if not str(event.get("counterparty") or "").strip():
            event["counterparty"] = _counterparty_from_bank_description(
                str(event.get("description") or event.get("memo") or "")
            )
        normalized = normalize_financial_event(event)
        matches = _bank_pattern_matches(conn, normalized, event, masters)
        rag_shapes = _unique_rag_shapes(matches, normalized, masters=masters)
        candidate: dict[str, Any] | None = None
        source = ""
        unresolved: list[str] = []

        if len(rag_shapes) == 1:
            candidate = rag_shapes[0]
            source = "verified_rag_current_facts"
        elif len(rag_shapes) > 1:
            unresolved.append("rag_candidates_not_unique")

        related = _matching_related_candidates(event, related_candidates)
        if candidate is None and len(related) == 1:
            candidate = _candidate_from_related(event, related[0])
            source = str(related[0].get("source") or "official_related_journal")
            unresolved = []
        elif candidate is None and len(related) > 1:
            unresolved.append("related_journal_candidates_tied")
        elif candidate is not None and related:
            related_shapes = {_counterpart_shape(_candidate_from_related(event, item), event) for item in related}
            if _counterpart_shape(candidate, event) not in related_shapes:
                candidate = None
                source = ""
                unresolved.append("rag_related_journal_conflict")

        if candidate is None and not matches and not related:
            packet = _first_transaction_ai_packet(event, masters)
            ai_packets.append(packet)
            ai_decision = supplied_ai_decisions.get(str(packet["packet_hash"]))
            ai_candidate, ai_source, ai_reasons, ai_waiting = _resolve_first_transaction_ai_decision(
                event,
                masters,
                packet=packet,
                decision=ai_decision,
            )
            if ai_waiting:
                waiting_reasons = sorted({"ai_decision_required", *ai_reasons})
                waiting = replace(
                    classified,
                    matched_pattern_source="ai_decision_required",
                    confidence=0.0,
                    recommended_action="queue_for_learning",
                    registration_allowed=False,
                    learning_action="await_existing_ai_harness_decision",
                    unresolved_points=waiting_reasons,
                    journal_candidate=None,
                )
                decisions.append(_attach_moneyforward_transaction_binding(waiting, event))
                sources.append("ai_decision_required")
                reasons.append([])
                continue
            if ai_candidate is not None:
                candidate = ai_candidate
                source = ai_source
                unresolved = []
            else:
                review = replace(
                    classified,
                    matched_pattern_source=ai_source,
                    confidence=0.0,
                    recommended_action="queue_for_review",
                    registration_allowed=False,
                    learning_action="resolve_specific_bank_fact_conflict",
                    unresolved_points=ai_reasons,
                    journal_candidate=None,
                )
                decisions.append(_attach_moneyforward_transaction_binding(review, event))
                sources.append(ai_source)
                reasons.append(ai_reasons)
                continue

        if candidate is not None:
            master_reasons = _master_resolution_reasons(candidate, event, masters)
            if not master_reasons:
                resolved = replace(
                    classified,
                    matched_pattern_hash=str(candidate.get("matched_pattern_hash") or "") or None,
                    matched_pattern_source=source,
                    confidence=1.0,
                    recommended_action="auto_post_candidate",
                    registration_allowed=True,
                    learning_action="learn_after_transaction_journalize_readback",
                    unresolved_points=[],
                    journal_candidate=candidate,
                )
                resolved = _attach_moneyforward_transaction_binding(resolved, event)
                if resolved.registration_allowed:
                    decisions.append(resolved)
                    sources.append(source)
                    reasons.append([])
                    continue
                unresolved.extend(resolved.unresolved_points)
            else:
                unresolved.extend(master_reasons)

        if not matches:
            unresolved.append("no_verified_rag_candidate")
        if not related:
            unresolved.append("no_open_related_journal_candidate")
        unresolved.append("candidate_not_uniquely_decidable")
        unresolved = sorted(set(unresolved))
        review = replace(
            classified,
            matched_pattern_source=source or "bank_facts_not_uniquely_resolved",
            confidence=0.0,
            recommended_action="queue_for_review",
            registration_allowed=False,
            learning_action="resolve_specific_bank_fact_conflict",
            unresolved_points=unresolved,
            journal_candidate=candidate,
        )
        decisions.append(_attach_moneyforward_transaction_binding(review, event))
        sources.append(source or "review")
        reasons.append(unresolved)

    if persist:
        from .financial_event_classification import _persist_classifications

        _persist_classifications(conn, decisions)
    return BankDecisionBatch(decisions, sources, reasons, ai_packets)


def _first_transaction_ai_packet(
    event: Mapping[str, Any], masters: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Create the local, provider-neutral packet consumed by the agent harness."""

    account_names_by_id = {
        _external_id(item): _name(item)
        for item in masters.get("accounts", [])
        if _external_id(item) and _name(item)
    }
    bank_facts = {
        "source_event_id_hash": str(event.get("source_event_id_hash") or ""),
        "transaction_facts_hash": str(event.get("transaction_facts_hash") or ""),
        "transaction_date": str(event.get("transaction_date") or ""),
        "description": str(event.get("description") or ""),
        "counterparty": str(event.get("counterparty") or ""),
        "cash_flow_direction": str(event.get("cash_flow_direction") or ""),
        "transaction_side": str(event.get("transaction_side") or ""),
        "amount": int(event.get("transaction_value") or event.get("amount") or 0),
        "connected_account": "普通預金",
    }
    live_master = {
        "accounts": sorted(
            {
                _name(item)
                for item in masters.get("accounts", [])
                if item.get("available") is not False and _name(item)
            }
        ),
        "sub_accounts": sorted(
            {
                (
                    str(
                        item.get("account_name")
                        or account_names_by_id.get(str(item.get("account_id") or ""))
                        or ""
                    ),
                    _name(item),
                )
                for item in masters.get("sub_accounts", [])
                if item.get("available") is not False and _name(item)
            }
        ),
        "taxes": sorted(
            {
                _name(item)
                for item in masters.get("taxes", [])
                if item.get("available") is not False and _name(item)
            }
        ),
    }
    packet_body = {
        "decision_required": True,
        "decision_kind": "first_bank_transaction",
        "current_bank_facts": bank_facts,
        "necessary_evidence": list(event.get("necessary_evidence") or []),
        "verified_rag_candidates": [],
        "open_related_journal_candidates": [],
        "live_master": live_master,
        "allowed_decision_fields": [
            "counterpart_account",
            "counterpart_sub_account",
            "tax_category",
        ],
        "allowed_basis_codes": sorted(_AI_BASIS_CODES),
        "allowed_review_reason_codes": sorted(_AI_REVIEW_REASON_CODES),
    }
    return {
        "packet_hash": "sha256:" + stable_hash(packet_body),
        **packet_body,
    }


def _resolve_first_transaction_ai_decision(
    event: Mapping[str, Any],
    masters: Mapping[str, list[dict[str, Any]]],
    *,
    packet: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str, list[str], bool]:
    if decision is None:
        return None, "ai_decision_required", [], True
    if str(decision.get("packet_hash") or "") != str(packet["packet_hash"]):
        return None, "ai_decision_required", ["ai_decision_packet_hash_mismatch"], True
    if _AI_FIXED_FACT_FIELDS.intersection(decision):
        return None, "ai_decision_required", ["ai_fixed_fact_override_attempt"], True
    if set(decision).difference(_AI_DECISION_FIELDS):
        return None, "ai_decision_required", ["ai_decision_field_not_allowed"], True

    status = str(decision.get("status") or "")
    if status == "review":
        declared = {str(value) for value in decision.get("reason_codes") or []}
        if not declared or not declared.issubset(_AI_REVIEW_REASON_CODES):
            return None, "ai_decision_required", ["ai_review_reason_invalid"], True
        return (
            None,
            "ai_first_transaction_review",
            sorted(declared),
            False,
        )
    if status != "decided":
        return None, "ai_decision_required", ["ai_decision_status_invalid"], True

    basis_codes = {str(value) for value in decision.get("basis_codes") or []}
    if (
        not basis_codes
        or not basis_codes.issubset(_AI_BASIS_CODES)
        or "current_bank_facts" not in basis_codes
    ):
        return None, "ai_decision_required", ["ai_decision_basis_missing"], True
    evidence_hashes = {
        str(value) for value in decision.get("evidence_hashes") or [] if str(value)
    }
    evidence_basis = {"canonical_evidence", "official_related_information"}
    if basis_codes.intersection(evidence_basis) and not evidence_hashes:
        return None, "ai_decision_required", ["ai_evidence_reference_missing"], True
    account = str(decision.get("counterpart_account") or "").strip()
    sub_account = str(decision.get("counterpart_sub_account") or "").strip()
    tax = str(decision.get("tax_category") or "").strip()
    if not account or not tax:
        return None, "ai_decision_required", ["ai_decision_fields_missing"], True
    if _normalize_name(account) == _normalize_name("普通預金"):
        return None, "ai_decision_required", ["ai_counterpart_matches_connected_account"], True
    candidate = _candidate(event, account, sub_account, tax, "")
    master_reasons = _master_resolution_reasons(candidate, event, masters)
    if master_reasons:
        return None, "ai_first_transaction_review", sorted(set(master_reasons)), False
    return candidate, "ai_first_transaction_current_facts", [], False


def is_bank_transaction(
    event: Mapping[str, Any], masters: Mapping[str, list[dict[str, Any]]]
) -> bool:
    """A bank row is one whose connected ledger account is live `普通預金`.

    Connected services such as Amazon Business also appear in
    ``GET /api/v3/transactions``.  Their names are not a reliable type marker;
    the Money Forward ledger binding is the authoritative workflow boundary.
    """

    ledger_account_id = str(event.get("connected_ledger_account_id") or "")
    matches = [
        item
        for item in masters.get("accounts", [])
        if _external_id(item) == ledger_account_id and item.get("available") is not False
    ]
    return len(matches) == 1 and _normalize_name(_name(matches[0])) == _normalize_name("普通預金")


def _bank_pattern_matches(
    conn: sqlite3.Connection,
    normalized: dict[str, str],
    event: Mapping[str, Any],
    masters: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    direct = _find_pattern_matches(conn, normalized)
    family = _bank_description_family(str(event.get("description") or ""))
    if not family:
        return direct
    rows = conn.execute(
        """
        SELECT p.*,
               MAX(o.transaction_date) AS last_occurrence_date,
               'bank_context_official_history' AS match_source,
               0.95 AS confidence
        FROM journal_patterns AS p
        LEFT JOIN journal_pattern_occurrences AS o
          ON o.pattern_hash = p.pattern_hash
        WHERE p.amount_bucket = ?
          AND p.auto_post_eligibility IN ('enabled','limited')
          AND p.failure_count = 0
        GROUP BY p.pattern_hash
        ORDER BY last_occurrence_date DESC, p.pattern_hash
        """,
        (normalized["amount_bucket"],),
    ).fetchall()
    contextual = []
    for raw in rows:
        row = dict(raw)
        if _bank_description_family(str(row.get("description_key") or "")) != family:
            continue
        candidate = _journal_candidate_from_pattern(row, {**normalized, "amount": normalized["amount"]})
        if _rag_connected_side_matches(candidate, normalized, masters):
            contextual.append(row)
    combined = {str(row["pattern_hash"]): dict(row) for row in [*direct, *contextual]}
    matches = list(combined.values())
    shapes = {
        _counterpart_shape(
            _journal_candidate_from_pattern(row, {**normalized, "amount": normalized["amount"]}),
            normalized,
        )
        for row in matches
        if _rag_connected_side_matches(
            _journal_candidate_from_pattern(row, {**normalized, "amount": normalized["amount"]}),
            normalized,
            masters,
        )
    }
    if len(shapes) <= 1:
        return matches
    dated = [row for row in matches if str(row.get("last_occurrence_date") or "")]
    if not dated:
        return matches
    latest = max(str(row["last_occurrence_date"]) for row in dated)
    latest_rows = [row for row in dated if str(row["last_occurrence_date"]) == latest]
    latest_shapes = {
        _counterpart_shape(
            _journal_candidate_from_pattern(row, {**normalized, "amount": normalized["amount"]}),
            normalized,
        )
        for row in latest_rows
    }
    return latest_rows if len(latest_shapes) == 1 else matches


def _bank_description_family(value: str) -> str:
    text = _normalize_name(value)
    if "沖縄公庫貸付金" in text:
        return "okinawa_public_finance_interest"
    if text in {"返済", "元金返済"} or "元金返済" in text:
        return "loan_principal_repayment"
    if "預金機" in text:
        return "cash_deposit"
    return ""


def _unique_rag_shapes(
    matches: Sequence[dict[str, Any]],
    normalized: dict[str, str],
    *,
    masters: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    shapes: dict[tuple[str, str, str], dict[str, Any]] = {}
    event = {**normalized, "amount": normalized["amount"]}
    for row in matches:
        eligibility = str(row.get("auto_post_eligibility") or "")
        if eligibility not in {"limited", "enabled"} or int(row.get("failure_count") or 0):
            continue
        candidate = _journal_candidate_from_pattern(row, event)
        if not _rag_connected_side_matches(candidate, normalized, masters):
            continue
        shape = _counterpart_shape(candidate, event)
        shapes.setdefault(shape, candidate)
    return [shapes[key] for key in sorted(shapes)]


def _rag_connected_side_matches(
    candidate: Mapping[str, Any],
    event: Mapping[str, Any],
    masters: Mapping[str, list[dict[str, Any]]],
) -> bool:
    ledger_account_id = str(event.get("connected_ledger_account_id") or "")
    matches = [
        item
        for item in masters.get("accounts", [])
        if _external_id(item) == ledger_account_id
    ]
    if len(matches) != 1:
        return False
    connected_name = _name(matches[0])
    prefix = "debit" if str(event.get("transaction_side") or "") == "INCOME" else "credit"
    return _normalize_name(str(candidate.get(f"{prefix}_account") or "")) == _normalize_name(
        connected_name
    )


def _matching_related_candidates(
    event: Mapping[str, Any], candidates: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    amount = int(event.get("transaction_value") or event.get("amount") or 0)
    direction = str(event.get("cash_flow_direction") or "")
    text = _text(event.get("description"), event.get("memo"), event.get("counterparty"))
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in candidates:
        if str(item.get("cash_flow_direction") or "") != direction:
            continue
        if int(item.get("amount") or 0) != amount:
            continue
        if int(item.get("open_balance") or 0) < amount:
            continue
        candidate_text = _text(item.get("counterparty"), item.get("remark"), item.get("sub_account"))
        score = 1 + (2 if candidate_text and any(token in text for token in candidate_text.split()) else 0)
        scored.append((score, dict(item)))
    if not scored:
        return []
    top = max(score for score, _item in scored)
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for score, item in scored:
        if score == top:
            shape = (
                str(item.get("account") or ""),
                str(item.get("sub_account") or ""),
                str(item.get("tax_category") or "対象外"),
            )
            unique.setdefault(shape, item)
    return [unique[key] for key in sorted(unique)]


def _candidate_from_related(event: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    return _candidate(event, str(item["account"]), str(item.get("sub_account") or ""), str(item.get("tax_category") or "対象外"), str(item.get("pattern_hash") or ""))


def _candidate(
    event: Mapping[str, Any], account: str, sub_account: str, tax: str, pattern_hash: str
) -> dict[str, Any]:
    side = str(event.get("transaction_side") or "")
    amount = int(event.get("transaction_value") or event.get("amount") or 0)
    connected_account = "普通預金"
    payload = {
        "source_event_type": "bank_transaction",
        "source_type": "bank_transaction",
        "journal_date": str(event.get("transaction_date") or ""),
        "transaction_date": str(event.get("transaction_date") or ""),
        "vendor_name": str(event.get("counterparty") or event.get("description") or "bank transfer"),
        "amount": amount,
        "tax_amount": 0,
        "tax_rate": 0.0,
        "description": str(event.get("description") or event.get("memo") or "bank transfer"),
        "debit_account": connected_account if side == "INCOME" else account,
        "debit_sub_account": "" if side == "INCOME" else sub_account,
        "credit_account": account if side == "INCOME" else connected_account,
        "credit_sub_account": sub_account if side == "INCOME" else "",
        "tax_category": tax,
    }
    if pattern_hash:
        payload["matched_pattern_hash"] = pattern_hash
    return payload


def _master_resolution_reasons(
    candidate: Mapping[str, Any], event: Mapping[str, Any], masters: Mapping[str, list[dict[str, Any]]]
) -> list[str]:
    side = str(event.get("transaction_side") or "")
    prefix = "credit" if side == "INCOME" else "debit"
    account_name = str(candidate.get(f"{prefix}_account") or "")
    account = _unique_named(masters.get("accounts", []), account_name)
    reasons: list[str] = []
    if account is None:
        reasons.append("counterpart_account_master_unresolved")
    sub_name = str(candidate.get(f"{prefix}_sub_account") or "")
    if sub_name and _unique_sub_account(
        masters.get("sub_accounts", []),
        sub_account_name=sub_name,
        account_name=account_name,
        account_id=_external_id(account) if account else None,
    ) is None:
        reasons.append("counterpart_sub_account_master_unresolved")
    if _unique_named(masters.get("taxes", []), str(candidate.get("tax_category") or "")) is None:
        reasons.append("tax_master_unresolved")
    return reasons


def _counterpart_shape(candidate: Mapping[str, Any], event: Mapping[str, Any]) -> tuple[str, str, str]:
    side = str(event.get("transaction_side") or event.get("cash_flow_direction") or "")
    prefix = "credit" if side in {"INCOME", "inflow"} else "debit"
    return (
        str(candidate.get(f"{prefix}_account") or ""),
        str(candidate.get(f"{prefix}_sub_account") or ""),
        str(candidate.get("tax_category") or ""),
    )


def _text(*values: Any) -> str:
    normalized = unicodedata.normalize("NFKC", " ".join(str(value or "") for value in values)).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _counterparty_from_bank_description(value: str) -> str:
    """Remove banking operation noise while preserving the named party."""

    text = unicodedata.normalize("NFKC", value).strip()
    text = re.sub(
        r"^(?:振込(?:入金)?|振替|口座振替|自動振替|入金|出金|引落(?:し)?|送金)\s*[:：\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()
