from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date
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
from .drive_policy import company_policies
from .office_profile import get_current_profile


def _name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or item.get("display_name") or item.get("title") or "").strip()


def _external_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("account_id") or item.get("sub_account_id") or "").strip()

def configured_card_route() -> dict[str, Any]:
    routes = company_policies("accounting_rules.card_routes")
    if len(routes) != 1:
        raise ValueError("one explicit current-company card route is required")
    route = routes[0]
    if any(not route.get(key) for key in ("credit_account", "credit_sub_account", "ledger_account_id", "ledger_sub_account_id")):
        raise ValueError("configured card route is incomplete")
    return route


_FIXED_FACT_FIELDS = {
    "amount",
    "cash_flow_direction",
    "connected_account",
    "connected_ledger_account_id",
    "connected_ledger_sub_account_id",
    "date",
    "journal_date",
    "transaction_date",
    "transaction_id",
    "transaction_side",
}
_DECISION_FIELDS = {
    "basis_codes",
    "debit_account",
    "debit_sub_account",
    "credit_account",
    "credit_sub_account",
    "evidence_content_hash",
    "evidence_required",
    "packet_hash",
    "reason_codes",
    "status",
    "tax_category",
}
_BASIS_CODES = {
    "accounting_rule",
    "canonical_evidence",
    "current_card_facts",
    "verified_rag",
}
_REVIEW_REASONS = {
    "candidate_evidence_conflict",
    "candidate_tie_after_current_facts",
    "specific_card_fact_missing",
}


@dataclass(frozen=True)
class CardDecisionBatch:
    items: list[FinancialEventClassification]
    decision_sources: list[str]
    review_reasons: list[list[str]]
    not_ready_reasons: list[list[str]]
    ai_packets: list[dict[str, Any]] = dataclass_field(default_factory=list)
    evidence_required: list[bool] = dataclass_field(default_factory=list)
    selected_evidence_hashes: list[str] = dataclass_field(default_factory=list)

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_local_dict() for item in self.items],
            "decision_sources": self.decision_sources,
            "review_reasons": self.review_reasons,
            "not_ready_reasons": self.not_ready_reasons,
            "ai_packets": self.ai_packets,
            "evidence_required": self.evidence_required,
            "selected_evidence_hashes": self.selected_evidence_hashes,
        }


def is_configured_card_transaction(event: Mapping[str, Any], masters: Mapping[str, list[dict[str, Any]]]) -> bool:
    route = configured_card_route()
    if event.get("company_slug") != get_current_profile().slug:
        return False
    account_id = str(event.get("connected_ledger_account_id") or "")
    sub_id = str(event.get("connected_ledger_sub_account_id") or "")
    if account_id != route["ledger_account_id"] or sub_id != route["ledger_sub_account_id"]:
        return False
    accounts = [item for item in masters.get("accounts", []) if _external_id(item) == account_id and item.get("available") is not False]
    subs = [item for item in masters.get("sub_accounts", []) if _external_id(item) == sub_id and item.get("available") is not False]
    return bool(len(accounts) == 1 and len(subs) == 1
        and _key(_name(accounts[0])) == _key(route["credit_account"])
        and _key(_name(subs[0])) == _key(route["credit_sub_account"])
        and str(subs[0].get("account_id") or "") == account_id)


def decide_card_transactions(
    transactions: Sequence[dict[str, Any]],
    *,
    conn: sqlite3.Connection,
    masters: Mapping[str, list[dict[str, Any]]],
    ai_decisions: Mapping[str, Mapping[str, Any]] | None = None,
    evidence_candidates: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    persist: bool = True,
) -> CardDecisionBatch:
    """Resolve debit fields while preserving immutable Money Forward card facts."""

    for event in transactions:
        if not is_configured_card_transaction(event, masters):
            raise ValueError("card event does not match the current company route")
    baseline = classify_financial_events(
        {"events": list(transactions)}, conn=conn, persist=False, engine="python"
    ).items
    decisions: list[FinancialEventClassification] = []
    sources: list[str] = []
    reviews: list[list[str]] = []
    not_ready: list[list[str]] = []
    packets: list[dict[str, Any]] = []
    needs_evidence: list[bool] = []
    selected_hashes: list[str] = []
    supplied = ai_decisions or {}
    evidence_by_event = evidence_candidates or {}

    for event, classified in zip(transactions, baseline, strict=True):
        normalized = normalize_financial_event(event)
        event_hash = str(event.get("source_event_id_hash") or classified.event_hash)
        rag_candidates = _unique_card_rag_candidates(conn, normalized, event)
        supplier_primary = event.get("matched_supplier_primary_purchase")
        if isinstance(supplier_primary, Mapping) and supplier_primary.get("company_slug") != get_current_profile().slug:
            raise ValueError("supplier primary match company binding mismatch")
        if (
            isinstance(supplier_primary, Mapping)
            and supplier_primary.get("company_slug") == get_current_profile().slug
            and supplier_primary.get("supplier_registration_readback_verified") is True
            and supplier_primary.get("voucher_readback_verified") is True
        ):
            reasons = ["supplier_primary_target_exclusion_required"]
            decisions.append(_unresolved(classified, event, reasons, review=False))
            sources.append("verified_supplier_primary_duplicate")
            reviews.append([])
            not_ready.append(reasons)
            needs_evidence.append(False)
            selected_hashes.append("")
            continue
        supplier_adjustment = event.get("supplier_primary_adjustment_match")
        if isinstance(supplier_adjustment, Mapping) and supplier_adjustment.get("company_slug") != get_current_profile().slug:
            raise ValueError("supplier adjustment company binding mismatch")
        if (
            event.get("connected_source_type") == "supplier_primary"
            and str(event.get("transaction_side") or "") == "INCOME"
        ):
            match = supplier_adjustment if isinstance(supplier_adjustment, Mapping) else {}
            if match.get("status") == "unique":
                candidate = _candidate(
                    event,
                    str(match.get("original_expense_account") or ""),
                    str(match.get("original_expense_sub_account") or ""),
                    str(match.get("original_tax_category") or ""),
                    "",
                    invoice_kind=str(match.get("original_invoice_kind") or "") or None,
                )
                master_reasons = _master_reasons(candidate, masters)
                if not master_reasons:
                    resolved = replace(
                        classified,
                        matched_pattern_source="official_supplier_primary_purchase_readback",
                        confidence=1.0,
                        recommended_action="auto_post_candidate",
                        registration_allowed=True,
                        learning_action="learn_after_card_transaction_readback",
                        unresolved_points=[],
                        journal_candidate=candidate,
                    )
                    decisions.append(_attach_moneyforward_transaction_binding(resolved, event))
                    sources.append("official_supplier_primary_purchase_readback")
                    reviews.append([])
                    not_ready.append([])
                    needs_evidence.append(False)
                    selected_hashes.append("")
                    continue
                reasons = master_reasons
            elif match.get("status") == "ambiguous":
                reasons = ["supplier_primary_original_purchase_ambiguous"]
            else:
                reasons = ["supplier_primary_voucher_identity_mismatch"]
            decisions.append(_unresolved(classified, event, reasons, review=True))
            sources.append("supplier_primary_adjustment_review")
            reviews.append(sorted(set(reasons)))
            not_ready.append([])
            needs_evidence.append(False)
            selected_hashes.append("")
            continue

        fast_path = (
            len(rag_candidates) == 1
            and rag_candidates[0].get("_current_facts_exact_match") is True
            and not _master_reasons(rag_candidates[0], masters)
        )
        packet = _decision_packet(
            event,
            masters,
            rag_candidates,
            decision_required=not fast_path,
        )
        packets.append(packet)
        candidate: dict[str, Any] | None = None
        source = ""
        evidence_required = False
        selected_hash = ""
        review_reasons: list[str] = []
        not_ready_reasons: list[str] = []

        if fast_path:
            candidate = dict(rag_candidates[0])
            candidate.pop("_current_facts_exact_match", None)
            source = "verified_rag_current_card_facts"
        else:
            ai_candidate, source, decision_reasons, waiting, evidence_required, selected_hash = (
                _resolve_ai_decision(
                    event,
                    masters,
                    packet=packet,
                    decision=supplied.get(str(packet["packet_hash"])),
                )
            )
            if waiting:
                reasons = sorted({"ai_decision_required", *decision_reasons})
                decisions.append(_unresolved(classified, event, reasons, review=False))
                sources.append("ai_decision_required")
                reviews.append([])
                not_ready.append([])
                needs_evidence.append(evidence_required)
                selected_hashes.append(selected_hash)
                continue
            if ai_candidate is None:
                if source == "ai_card_not_ready":
                    not_ready_reasons = decision_reasons
                else:
                    review_reasons = decision_reasons
            else:
                candidate = ai_candidate
                if len(rag_candidates) == 1:
                    candidate["matched_pattern_hash"] = rag_candidates[0].get(
                        "matched_pattern_hash"
                    )

        if candidate is not None:
            master_reasons = _master_reasons(candidate, masters)
            if master_reasons:
                not_ready_reasons.extend(master_reasons)
                candidate = None

        if candidate is not None and evidence_required:
            candidates = list(evidence_by_event.get(event_hash, ()))
            hashes = {
                str(item.get("content_hash") or "")
                for item in candidates
                if _evidence_matches(event, item)
            }
            hashes.discard("")
            if not hashes:
                not_ready_reasons.append("necessary_evidence_missing")
                candidate = None
            elif not selected_hash and len(hashes) == 1:
                selected_hash = next(iter(hashes))
            elif not selected_hash:
                reasons = ["ai_evidence_selection_required"]
                decisions.append(_unresolved(classified, event, reasons, review=False))
                sources.append("ai_decision_required")
                reviews.append([])
                not_ready.append([])
                needs_evidence.append(True)
                selected_hashes.append("")
                continue
            elif selected_hash not in hashes:
                review_reasons.append("selected_canonical_evidence_conflicts_with_card_facts")
                candidate = None

        if candidate is not None:
            resolved = replace(
                classified,
                matched_pattern_hash=str(candidate.get("matched_pattern_hash") or "") or None,
                matched_pattern_source=source,
                confidence=1.0,
                recommended_action="auto_post_candidate",
                registration_allowed=True,
                learning_action="learn_after_card_transaction_readback",
                unresolved_points=[],
                journal_candidate=candidate,
            )
            resolved = _attach_moneyforward_transaction_binding(resolved, event)
            decisions.append(resolved)
            sources.append(source)
            reviews.append([])
            not_ready.append([])
            needs_evidence.append(evidence_required)
            selected_hashes.append(selected_hash)
            continue

        reasons = sorted(set(review_reasons or not_ready_reasons or ["candidate_not_uniquely_decidable"]))
        decisions.append(_unresolved(classified, event, reasons, review=bool(review_reasons)))
        sources.append(source or ("not_ready" if not_ready_reasons else "review"))
        reviews.append(sorted(set(review_reasons)))
        not_ready.append(sorted(set(not_ready_reasons)))
        needs_evidence.append(evidence_required)
        selected_hashes.append(selected_hash)

    if persist:
        from .financial_event_classification import _persist_classifications

        _persist_classifications(conn, decisions)
    return CardDecisionBatch(
        decisions,
        sources,
        reviews,
        not_ready,
        packets,
        needs_evidence,
        selected_hashes,
    )


def _unique_card_rag_candidates(
    conn: sqlite3.Connection,
    normalized: dict[str, str],
    event: Mapping[str, Any],
) -> list[dict[str, Any]]:
    shapes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _find_pattern_matches(conn, normalized):
        if str(row.get("auto_post_eligibility") or "") not in {"enabled", "limited"}:
            continue
        if int(row.get("failure_count") or 0):
            continue
        raw = _journal_candidate_from_pattern(
            row, {**normalized, "amount": normalized["amount"]}
        )
        counterpart_prefix = (
            "credit"
            if str(event.get("transaction_side") or "") == "INCOME"
            else "debit"
        )
        candidate = _candidate(
            event,
            str(raw.get(f"{counterpart_prefix}_account") or ""),
            str(raw.get(f"{counterpart_prefix}_sub_account") or ""),
            str(raw.get("tax_category") or ""),
            str(raw.get("matched_pattern_hash") or row.get("pattern_hash") or ""),
        )
        shape = (
            _key(str(candidate[f"{counterpart_prefix}_account"])),
            _key(str(candidate.get(f"{counterpart_prefix}_sub_account") or "")),
            _key(str(candidate["tax_category"])),
        )
        candidate["_current_facts_exact_match"] = (
            str(row.get("match_source") or "")
            == "exact_vendor_description_amount"
        )
        shapes.setdefault(shape, candidate)
    return [shapes[key] for key in sorted(shapes)]


def _decision_packet(
    event: Mapping[str, Any],
    masters: Mapping[str, list[dict[str, Any]]],
    rag_candidates: Sequence[dict[str, Any]],
    *,
    decision_required: bool,
) -> dict[str, Any]:
    facts = {
        "transaction_id": str(event.get("transaction_id") or ""),
        "source_event_id_hash": str(event.get("source_event_id_hash") or ""),
        "transaction_facts_hash": str(event.get("transaction_facts_hash") or ""),
        "transaction_date": str(event.get("transaction_date") or ""),
        "description": str(event.get("description") or ""),
        "counterparty": str(event.get("counterparty") or ""),
        "amount": int(event.get("transaction_value") or event.get("amount") or 0),
        "cash_flow_direction": str(event.get("cash_flow_direction") or ""),
        "transaction_side": str(event.get("transaction_side") or ""),
        "connected_account": f"{configured_card_route()['credit_account']}/{configured_card_route()['credit_sub_account']}",
    }
    counterpart_prefix = "credit" if facts["transaction_side"] == "INCOME" else "debit"
    body = {
        "company_slug": get_current_profile().slug,
        "configured_card_route": configured_card_route(),
        "decision_required": decision_required,
        "decision_kind": "card_counterpart_classification",
        "current_card_facts": facts,
        "verified_rag_candidates": [
            {
                key: item.get(key)
                for key in (
                    f"{counterpart_prefix}_account",
                    f"{counterpart_prefix}_sub_account",
                    "tax_category",
                )
            }
            for item in rag_candidates
        ],
        "live_master": {
            "accounts": sorted({_name(item) for item in masters.get("accounts", []) if _name(item)}),
            "sub_accounts": sorted({_name(item) for item in masters.get("sub_accounts", []) if _name(item)}),
            "taxes": sorted({_name(item) for item in masters.get("taxes", []) if _name(item)}),
        },
        "fixed_connected_side": {
            "side": "debit" if facts["transaction_side"] == "INCOME" else "credit",
            "account": configured_card_route()["credit_account"],
            "sub_account": configured_card_route()["credit_sub_account"],
        },
        "allowed_decision_fields": [
            f"{counterpart_prefix}_account",
            f"{counterpart_prefix}_sub_account",
            "tax_category",
            "basis_codes",
            "evidence_required",
        ],
        "allowed_basis_codes": sorted(_BASIS_CODES),
        "allowed_review_reason_codes": sorted(_REVIEW_REASONS),
    }
    return {"packet_hash": "sha256:" + stable_hash(body), **body}


def _resolve_ai_decision(
    event: Mapping[str, Any],
    masters: Mapping[str, list[dict[str, Any]]],
    *,
    packet: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str, list[str], bool, bool, str]:
    if decision is None:
        return None, "ai_decision_required", [], True, False, ""
    if str(decision.get("packet_hash") or "") != str(packet["packet_hash"]):
        return None, "ai_decision_required", ["ai_decision_packet_hash_mismatch"], True, False, ""
    counterpart_prefix = (
        "credit" if str(event.get("transaction_side") or "") == "INCOME" else "debit"
    )
    fixed_prefix = "debit" if counterpart_prefix == "credit" else "credit"
    if _FIXED_FACT_FIELDS.intersection(decision) or {
        f"{fixed_prefix}_account",
        f"{fixed_prefix}_sub_account",
    }.intersection(decision):
        return None, "ai_decision_required", ["ai_fixed_fact_override_attempt"], True, False, ""
    if set(decision).difference(_DECISION_FIELDS):
        return None, "ai_decision_required", ["ai_decision_field_not_allowed"], True, False, ""
    status = str(decision.get("status") or "")
    if status == "review":
        reasons = {str(value) for value in decision.get("reason_codes") or []}
        if not reasons or not reasons.issubset(_REVIEW_REASONS):
            return None, "ai_decision_required", ["ai_review_reason_invalid"], True, False, ""
        return None, "ai_card_review", sorted(reasons), False, False, ""
    if status != "decided":
        return None, "ai_decision_required", ["ai_decision_status_invalid"], True, False, ""
    basis = {str(value) for value in decision.get("basis_codes") or []}
    if not basis or not basis.issubset(_BASIS_CODES) or "current_card_facts" not in basis:
        return None, "ai_decision_required", ["ai_decision_basis_missing"], True, False, ""
    account = str(decision.get(f"{counterpart_prefix}_account") or "").strip()
    sub = str(decision.get(f"{counterpart_prefix}_sub_account") or "").strip()
    tax = str(decision.get("tax_category") or "").strip()
    if not account or not tax:
        return None, "ai_decision_required", ["ai_decision_fields_missing"], True, False, ""
    evidence_required = decision.get("evidence_required") is True
    selected = str(decision.get("evidence_content_hash") or "")
    candidate = _candidate(event, account, sub, tax, "")
    rag = packet.get("verified_rag_candidates")
    if isinstance(rag, Sequence) and len(rag) == 1 and isinstance(rag[0], Mapping):
        if "verified_rag" not in basis:
            return (
                None,
                "ai_decision_required",
                ["ai_decision_basis_missing"],
                True,
                evidence_required,
                selected,
            )
        expected = (
            _key(str(rag[0].get(f"{counterpart_prefix}_account") or "")),
            _key(str(rag[0].get(f"{counterpart_prefix}_sub_account") or "")),
            _key(str(rag[0].get("tax_category") or "")),
        )
        actual = (_key(account), _key(sub), _key(tax))
        if actual != expected:
            return (
                None,
                "ai_decision_required",
                ["ai_decision_rag_candidate_mismatch"],
                True,
                evidence_required,
                selected,
            )
    reasons = _master_reasons(candidate, masters)
    if reasons:
        return None, "ai_card_not_ready", reasons, False, evidence_required, selected
    return candidate, "ai_card_current_facts", [], False, evidence_required, selected


def _candidate(
    event: Mapping[str, Any],
    counterpart_account: str,
    counterpart_sub: str,
    tax: str,
    pattern_hash: str,
    *,
    invoice_kind: str | None = None,
) -> dict[str, Any]:
    inflow = str(event.get("transaction_side") or "") == "INCOME"
    candidate = {
        "journal_date": str(event.get("transaction_date") or ""),
        "vendor_name": str(event.get("counterparty") or event.get("description") or ""),
        "amount": int(event.get("transaction_value") or event.get("amount") or 0),
        "tax_amount": 0,
        "tax_rate": 0.0,
        "debit_account": configured_card_route()["credit_account"] if inflow else counterpart_account,
        "debit_sub_account": configured_card_route()["credit_sub_account"] if inflow else counterpart_sub or None,
        "credit_account": counterpart_account if inflow else configured_card_route()["credit_account"],
        "credit_sub_account": (
            counterpart_sub or None if inflow else configured_card_route()["credit_sub_account"]
        ),
        "tax_category": tax,
        "description": str(event.get("description") or ""),
        "source_type": "card_transaction",
        "risk_flags": [],
        "matched_pattern_hash": pattern_hash or None,
    }
    if invoice_kind:
        candidate["invoice_kind"] = invoice_kind
    return candidate


def _master_reasons(
    candidate: Mapping[str, Any], masters: Mapping[str, list[dict[str, Any]]]
) -> list[str]:
    reasons: list[str] = []
    for candidate_field, item_type, source in (
        ("debit_account", "account", "accounts"),
        ("credit_account", "account", "accounts"),
        ("tax_category", "tax", "taxes"),
    ):
        name = str(candidate.get(candidate_field) or "")
        matches = [item for item in masters.get(source, []) if _key(_name(item)) == _key(name)]
        if len(matches) != 1:
            reasons.append(f"{item_type}_master_unresolved:{candidate_field}")
    for prefix in ("debit", "credit"):
        sub = str(candidate.get(f"{prefix}_sub_account") or "")
        if not sub:
            continue
        account = str(candidate.get(f"{prefix}_account") or "")
        account_ids = {
            _external_id(item)
            for item in masters.get("accounts", [])
            if _key(_name(item)) == _key(account)
        }
        matches = [
            item
            for item in masters.get("sub_accounts", [])
            if _key(_name(item)) == _key(sub)
            and (not item.get("account_id") or str(item.get("account_id")) in account_ids)
        ]
        if len(matches) != 1:
            reasons.append(f"sub_account_master_unresolved:{prefix}_sub_account")
    return sorted(set(reasons))


def _evidence_matches(event: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    if item.get("company_slug") != get_current_profile().slug:
        return False
    amount = int(event.get("transaction_value") or event.get("amount") or 0)
    if int(item.get("amount") or 0) != amount:
        return False
    try:
        delta = abs(
            (
                date.fromisoformat(str(event.get("transaction_date") or "")[:10])
                - date.fromisoformat(str(item.get("transaction_date") or "")[:10])
            ).days
        )
    except ValueError:
        return False
    if delta > 3 or _key(str(item.get("card_route") or "")) != _key(configured_card_route()["credit_sub_account"]):
        return False
    merchant = _key(" ".join(str(event.get(key) or "") for key in ("description", "counterparty")))
    evidence_text = _key(" ".join(str(item.get(key) or "") for key in ("vendor_name", "description")))
    if not merchant or not evidence_text:
        return False
    if merchant in evidence_text or evidence_text in merchant:
        return True
    merchant_tokens = {
        _key(value)
        for value in re.split(r"\s+", " ".join(str(event.get(key) or "") for key in ("description", "counterparty")))
        if _key(value)
    }
    evidence_tokens = {
        _key(value)
        for value in re.split(r"\s+", " ".join(str(item.get(key) or "") for key in ("vendor_name", "description")))
        if _key(value)
    }
    return bool(merchant_tokens.intersection(evidence_tokens))


def _unresolved(
    classified: FinancialEventClassification,
    event: Mapping[str, Any],
    reasons: list[str],
    *,
    review: bool,
) -> FinancialEventClassification:
    item = replace(
        classified,
        matched_pattern_source="card_review" if review else "ai_decision_required",
        confidence=0.0,
        recommended_action="queue_for_review" if review else "queue_for_learning",
        registration_allowed=False,
        learning_action="resolve_card_exception" if review else "await_existing_ai_harness_decision",
        unresolved_points=sorted(set(reasons)),
        journal_candidate=None,
    )
    return _attach_moneyforward_transaction_binding(item, event)


def _key(value: str) -> str:
    return re.sub(r"[\s\u3000/・_-]+", "", unicodedata.normalize("NFKC", value)).casefold()
