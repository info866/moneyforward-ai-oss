from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .rag import build_rag_cards, upsert_sync_manifest
from .office_profile import get_current_profile, get_setting
from .drive_policy import company_policies


AUTO_ELIGIBLE = {"limited", "enabled"}
CLASSIFICATION_ENGINE_ENV = "MF_PHASE12_CLASSIFICATION_ENGINE"
DEFAULT_CLASSIFICATION_ENGINE = "python"
LEGACY_PYTHON_CLASSIFICATION_ENGINE = "python"
AI_DECISION_POLICY_VERSION = "receipt-decision-v5"
AI_DECISION_MODEL_ID = "gemini-3.5-flash-lite"
AI_DECISION_OUTPUT_FIELDS = frozenset(
    {
        "packet_hash",
        "broad_content",
        "debit_account",
        "debit_sub_account",
        "pattern_hash",
        "confidence",
        "decidable",
        "rationale_codes",
    }
)
@dataclass(frozen=True)
class FinancialEventClassification:
    event_hash: str
    source_event_type: str
    transaction_date: str
    description_key: str
    amount_bucket: str
    cash_flow_direction: str
    account_key_hash: str
    matched_pattern_hash: str | None
    matched_pattern_source: str
    confidence: float
    recommended_action: str
    registration_allowed: bool
    learning_action: str
    unresolved_points: list[str]
    journal_candidate: dict[str, Any] | None

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "event_hash": self.event_hash,
            "source_event_type": self.source_event_type,
            "transaction_date": self.transaction_date,
            "description_key": self.description_key,
            "amount_bucket": self.amount_bucket,
            "cash_flow_direction": self.cash_flow_direction,
            "account_key_hash": self.account_key_hash,
            "matched_pattern_hash": self.matched_pattern_hash,
            "matched_pattern_source": self.matched_pattern_source,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "registration_allowed": self.registration_allowed,
            "learning_action": self.learning_action,
            "unresolved_points": self.unresolved_points,
            "journal_candidate": self.journal_candidate,
        }

    def to_redacted_dict(self) -> dict[str, Any]:
        candidate = None
        if self.journal_candidate:
            candidate = {
                "debit_account": self.journal_candidate.get("debit_account"),
                "debit_sub_account": self.journal_candidate.get("debit_sub_account"),
                "credit_account": self.journal_candidate.get("credit_account"),
                "credit_sub_account": self.journal_candidate.get("credit_sub_account"),
                "tax_category": self.journal_candidate.get("tax_category"),
            }
        return {
            "event_hash": self.event_hash,
            "source_event_type": self.source_event_type,
            "transaction_date": self.transaction_date,
            "description_key": self.description_key,
            "amount_bucket": self.amount_bucket,
            "cash_flow_direction": self.cash_flow_direction,
            "account_key_hash": self.account_key_hash,
            "matched_pattern_hash": self.matched_pattern_hash,
            "matched_pattern_source": self.matched_pattern_source,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "registration_allowed": self.registration_allowed,
            "learning_action": self.learning_action,
            "unresolved_points": self.unresolved_points,
            "journal_candidate": candidate,
        }


@dataclass(frozen=True)
class FinancialEventClassificationBatch:
    items: list[FinancialEventClassification]
    rag_cards_upserted: int
    persisted: bool
    journal_batch_input_path: Path | None = None

    def to_local_dict(self) -> dict[str, Any]:
        journal_batch_items = _journal_batch_items(self.items)
        return {
            "status": "ok",
            "item_count": len(self.items),
            "auto_post_candidate_count": sum(1 for item in self.items if item.registration_allowed),
            "needs_learning_count": sum(1 for item in self.items if item.recommended_action == "queue_for_learning"),
            "review_queue_count": sum(1 for item in self.items if item.recommended_action == "queue_for_review"),
            "rag_cards_upserted": self.rag_cards_upserted,
            "persisted": self.persisted,
            "journal_batch_ready_count": len(journal_batch_items),
            "journal_batch_input_path": str(self.journal_batch_input_path) if self.journal_batch_input_path else "",
            "journal_batch_input": {"items": journal_batch_items},
            "items": [item.to_local_dict() for item in self.items],
            "raw_event_ids_stored": False,
            "raw_amounts_stored_in_db": False,
        }

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "item_count": len(self.items),
            "auto_post_candidate_count": sum(1 for item in self.items if item.registration_allowed),
            "needs_learning_count": sum(1 for item in self.items if item.recommended_action == "queue_for_learning"),
            "review_queue_count": sum(1 for item in self.items if item.recommended_action == "queue_for_review"),
            "rag_cards_upserted": self.rag_cards_upserted,
            "persisted": self.persisted,
            "journal_batch_ready_count": len(_journal_batch_items(self.items)),
            "journal_batch_input_path": str(self.journal_batch_input_path) if self.journal_batch_input_path else "",
            "items": [item.to_redacted_dict() for item in self.items],
            "raw_event_ids_printed": False,
            "raw_amounts_printed": False,
            "raw_secret_stored": False,
        }


@dataclass(frozen=True)
class DecisionValidation:
    status: str
    candidate: dict[str, Any] | None
    reason_codes: tuple[str, ...]


def build_accounting_decision_packet(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the only LLM packet; all fixed accounting facts remain immutable."""

    body = dict(payload)
    body.pop("packet_hash", None)
    body["policy_version"] = AI_DECISION_POLICY_VERSION
    body["model_id"] = AI_DECISION_MODEL_ID
    return {"packet_hash": "sha256:" + stable_hash(body), **body}


def validate_accounting_ai_decision(
    packet: dict[str, Any], raw: Any
) -> DecisionValidation:
    """Fail closed without asking the same model to validate its own answer."""

    expected_hash = str(packet.get("packet_hash") or "")
    body = {key: value for key, value in packet.items() if key != "packet_hash"}
    problems: list[str] = []
    if expected_hash != "sha256:" + stable_hash(body):
        problems.append("packet_hash_mismatch")
    if not isinstance(raw, dict):
        return DecisionValidation("review", None, ("decision_not_object",))
    extra = sorted(set(raw) - AI_DECISION_OUTPUT_FIELDS)
    if extra:
        problems.append("unexpected_fields:" + ",".join(extra))
    if str(raw.get("packet_hash") or "") != expected_hash:
        problems.append("response_packet_hash_mismatch")
    if raw.get("decidable") is not True:
        problems.append("model_not_decidable")

    account = str(raw.get("debit_account") or "").strip()
    sub_account = str(raw.get("debit_sub_account") or "").strip() or None
    pattern_hash = str(raw.get("pattern_hash") or "").strip() or None
    broad_content = str(raw.get("broad_content") or "").strip()
    try:
        confidence = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
        problems.append("confidence_not_numeric")
    allowed_accounts = {str(value).strip() for value in packet.get("allowed_debit_accounts") or []}
    allowed_sub_accounts = {
        str(value).strip() for value in packet.get("allowed_debit_sub_accounts") or []
    }
    allowed_patterns = {
        str(value).strip() for value in packet.get("allowed_pattern_hashes") or []
    }
    if not account or account not in allowed_accounts:
        problems.append("debit_account_not_in_live_master")
    if not broad_content or len(broad_content) > 40 or "\n" in broad_content:
        problems.append("broad_content_invalid")
    current_broad_content = str(packet.get("current_broad_content") or "").strip()
    if current_broad_content and broad_content != current_broad_content:
        problems.append("broad_content_current_facts_mismatch")
    if sub_account is not None and sub_account not in allowed_sub_accounts:
        problems.append("debit_sub_account_not_in_live_master")
    if pattern_hash is not None and pattern_hash not in allowed_patterns:
        problems.append("pattern_hash_not_in_evidence")
    if not 0 <= confidence <= 1:
        problems.append("confidence_out_of_range")
    rationale_codes = raw.get("rationale_codes")
    if not isinstance(rationale_codes, list) or not all(
        isinstance(value, str) for value in rationale_codes
    ):
        problems.append("rationale_codes_invalid")
        rationale_codes = []

    fixed = packet.get("fixed_fields") or {}
    facts = packet.get("canonical_facts") or {}
    if int(fixed.get("amount") or 0) != int(facts.get("amount") or 0):
        problems.append("fixed_amount_mismatch")
    if str(fixed.get("journal_date") or "") != str(facts.get("transaction_date") or ""):
        problems.append("fixed_date_mismatch")
    tax_rows = fixed.get("tax_rows") or []
    if int(fixed.get("tax_amount") or 0) != sum(
        int(row.get("tax_amount") or 0) for row in tax_rows if isinstance(row, dict)
    ):
        problems.append("fixed_tax_mismatch")
    route = packet.get("drive_route") or {}
    if route.get("company_slug") != get_current_profile().slug:
        problems.append("drive_route_company_mismatch")
    if str(fixed.get("credit_sub_account") or "") != str(
        route.get("payment_liability_route") or route.get("credit_sub_account") or ""
    ):
        problems.append("fixed_credit_route_mismatch")
    if not route.get("credit_account") or str(fixed.get("credit_account") or "") != route["credit_account"]:
        problems.append("fixed_credit_account_mismatch")
    live_master = packet.get("live_master") or {}
    if live_master.get("credit_sub_account_unique") is not True:
        problems.append("credit_sub_account_not_unique_in_live_master")
    identity = packet.get("moneyforward_identity") or {}
    office_id_hash = str(identity.get("office_id_hash") or "")
    if identity.get("verified") is not True or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", office_id_hash
    ):
        problems.append("moneyforward_identity_not_verified")

    if problems:
        return DecisionValidation("review", None, tuple(sorted(set(problems))))
    return DecisionValidation(
        "decided",
        {
            "broad_content": broad_content,
            "debit_account": account,
            "debit_sub_account": sub_account,
            "pattern_hash": pattern_hash,
            "confidence": confidence,
        },
        tuple(sorted(rationale_codes)),
    )


def load_accounting_decision_cache(
    conn: Any, packet_hash: str
) -> dict[str, Any] | None:
    _ensure_accounting_decision_cache(conn)
    row = conn.execute(
        "SELECT status, normalized_result_json, reason_codes_json FROM accounting_decision_cache "
        "WHERE packet_hash=? AND model_id=? AND policy_version=?",
        (packet_hash, AI_DECISION_MODEL_ID, AI_DECISION_POLICY_VERSION),
    ).fetchone()
    if row is None:
        return None
    return {
        "packet_hash": packet_hash,
        "status": str(row["status"]),
        "candidate": json.loads(row["normalized_result_json"]) if row["normalized_result_json"] else None,
        "conflicts": json.loads(row["reason_codes_json"]),
        "cache_hit": True,
    }


def store_accounting_decision_cache(
    conn: Any, packet_hash: str, validation: DecisionValidation
) -> None:
    _ensure_accounting_decision_cache(conn)
    conn.execute(
        """
        INSERT INTO accounting_decision_cache(
            packet_hash, model_id, policy_version, status,
            normalized_result_json, reason_codes_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(packet_hash, model_id, policy_version) DO UPDATE SET
            status=excluded.status,
            normalized_result_json=excluded.normalized_result_json,
            reason_codes_json=excluded.reason_codes_json
        """,
        (
            packet_hash,
            AI_DECISION_MODEL_ID,
            AI_DECISION_POLICY_VERSION,
            validation.status,
            json.dumps(validation.candidate, ensure_ascii=False, sort_keys=True) if validation.candidate else None,
            json.dumps(list(validation.reason_codes), ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def _ensure_accounting_decision_cache(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounting_decision_cache (
            packet_hash TEXT NOT NULL,
            model_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('decided', 'review')),
            normalized_result_json TEXT,
            reason_codes_json TEXT NOT NULL,
            raw_document_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_document_stored = 0),
            raw_model_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_model_response_stored = 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY(packet_hash, model_id, policy_version)
        )
        """
    )

def classify_financial_events_from_file(
    *,
    events_json: Path,
    output_json: Path,
    journal_batch_input_json: Path | None = None,
    conn: sqlite3.Connection,
    persist: bool = False,
    engine: str | None = None,
) -> FinancialEventClassificationBatch:
    events_json = require_ignored_untracked_data_path(events_json, purpose="financial events input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="financial event classification output")
    payload = json.loads(events_json.read_text(encoding="utf-8"))
    batch = classify_financial_events(payload, conn=conn, persist=persist, engine=engine)
    if journal_batch_input_json is not None:
        journal_batch_input_json = require_ignored_untracked_data_path(
            journal_batch_input_json,
            purpose="financial event journal batch input output",
        )
        journal_batch_input_json.parent.mkdir(parents=True, exist_ok=True)
        journal_batch_input_json.write_text(
            json.dumps({"items": _journal_batch_items(batch.items)}, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        batch = FinancialEventClassificationBatch(
            items=batch.items,
            rag_cards_upserted=batch.rag_cards_upserted,
            persisted=batch.persisted,
            journal_batch_input_path=journal_batch_input_json,
        )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(batch.to_local_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return batch


def classify_financial_events(
    payload: Any,
    *,
    conn: sqlite3.Connection,
    persist: bool = False,
    engine: str | None = None,
) -> FinancialEventClassificationBatch:
    resolved_engine = _resolve_engine(engine)
    events = _extract_items(payload)
    if resolved_engine == "rust":
        from .rust_core import rust_core_command

        rust_core_command()
        if (any(isinstance(item.get("payroll_resolution"), dict) for item in events)
                or company_policies("accounting_rules.financial_event_rules")
                or company_policies("accounting_rules.supplier_primary_card_routes")):
            raise ValueError("configured company policies require the Python classifier")
        # The adapter must verify a bundled reviewed binary; never probe PATH here.
        return _classify_financial_events_with_rust(payload, conn=conn, persist=persist)
    return _classify_financial_events_with_python_legacy(payload, conn=conn, persist=persist)


def _classify_financial_events_with_python_legacy(
    payload: Any,
    *,
    conn: sqlite3.Connection,
    persist: bool,
) -> FinancialEventClassificationBatch:
    _ensure_tables(conn)
    events = _extract_items(payload)
    classifications = [
        _attach_moneyforward_transaction_binding(_classify_event(conn, event), event)
        for event in events
    ]
    rag_cards_upserted = 0
    if persist:
        _persist_classifications(conn, classifications)
        rag_cards_upserted = _materialize_bank_transaction_rag(conn, classifications)
    return FinancialEventClassificationBatch(
        items=classifications,
        rag_cards_upserted=rag_cards_upserted,
        persisted=persist,
    )


def _resolve_engine(engine: str | None) -> str:
    value = (engine or os.environ.get(CLASSIFICATION_ENGINE_ENV) or DEFAULT_CLASSIFICATION_ENGINE).strip().casefold()
    if value not in {LEGACY_PYTHON_CLASSIFICATION_ENGINE, "rust", "auto"}:
        raise ValueError("classification engine must be one of: python, rust, auto")
    return "python" if value == "auto" else value


def _classify_financial_events_with_rust(
    payload: Any,
    *,
    conn: sqlite3.Connection,
    persist: bool,
) -> FinancialEventClassificationBatch:
    from .rust_core import classify_events_via_rust

    events = _extract_items(payload)
    db_row = conn.execute("PRAGMA database_list").fetchone()
    db_path = db_row["file"] if isinstance(db_row, sqlite3.Row) else db_row[2]
    if not db_path:
        raise RuntimeError("Rust classification requires a file-backed SQLite DB")
    result = classify_events_via_rust(db_path, payload, persist=persist)
    items = [
        FinancialEventClassification(
            event_hash=str(item["event_hash"]),
            source_event_type=str(item["source_event_type"]),
            transaction_date=str(item["transaction_date"]),
            description_key=str(item["description_key"]),
            amount_bucket=str(item["amount_bucket"]),
            cash_flow_direction=str(item["cash_flow_direction"]),
            account_key_hash=str(item["account_key_hash"]),
            matched_pattern_hash=item.get("matched_pattern_hash"),
            matched_pattern_source=str(item["matched_pattern_source"]),
            confidence=float(item["confidence"]),
            recommended_action=str(item["recommended_action"]),
            registration_allowed=bool(item["registration_allowed"]),
            learning_action=str(item["learning_action"]),
            unresolved_points=[str(value) for value in item.get("unresolved_points", [])],
            journal_candidate=item.get("journal_candidate"),
        )
        for item in result.get("items", [])
    ]
    items = [_attach_moneyforward_transaction_binding(item, events[index]) for index, item in enumerate(items)]
    return FinancialEventClassificationBatch(
        items=items,
        rag_cards_upserted=int(result.get("rag_cards_upserted", 0)),
        persisted=bool(result.get("persisted", persist)),
    )


def normalize_financial_event(event: dict[str, Any]) -> dict[str, str]:
    return _normalized_event(event)


def _classify_event(conn: sqlite3.Connection, event: dict[str, Any]) -> FinancialEventClassification:
    profile = get_current_profile()
    if profile is None:
        raise ValueError("explicit current company profile is required")
    if event.get("company_slug", profile.slug) != profile.slug:
        raise ValueError("financial event company binding mismatch")
    normalized = _normalized_event(event)
    verified_evidence = _classify_verified_evidence_resolution(event, normalized)
    if verified_evidence is not None:
        return verified_evidence
    verified_payroll = _classify_verified_payroll_resolution(event, normalized)
    if verified_payroll is not None:
        return verified_payroll
    fixed = _classify_company_fixed_event(event, normalized)
    if fixed is not None:
        return fixed
    if _looks_like_payroll_event(normalized):
        return FinancialEventClassification(
            event_hash=normalized["event_hash"],
            source_event_type=normalized["source_event_type"],
            transaction_date=normalized["transaction_date"],
            description_key=normalized["description_key"],
            amount_bucket=normalized["amount_bucket"],
            cash_flow_direction=normalized["cash_flow_direction"],
            account_key_hash=normalized["account_key_hash"],
            matched_pattern_hash=None,
            matched_pattern_source="payroll_ledger_resolution_required",
            confidence=0.0,
            recommended_action="resolve_payroll_ledger_then_auto_post",
            registration_allowed=False,
            learning_action="run_payroll_ledger_resolver_before_pattern_matching",
            unresolved_points=["verified_payroll_resolution_required"],
            journal_candidate=None,
        )
    if (
        _configured_card_statement_requires_target_exclusion(event, normalized)
        and _has_supplier_primary_match(event)
    ):
        unresolved_points = [
            "configured_supplier_primary_policy",
            "card_statement_target_exclusion_required",
        ]
        return FinancialEventClassification(
            event_hash=normalized["event_hash"],
            source_event_type=normalized["source_event_type"],
            transaction_date=normalized["transaction_date"],
            description_key=normalized["description_key"],
            amount_bucket=normalized["amount_bucket"],
            cash_flow_direction=normalized["cash_flow_direction"],
            account_key_hash=normalized["account_key_hash"],
            matched_pattern_hash=None,
            matched_pattern_source="configured_supplier_primary_policy",
            confidence=1.0,
            recommended_action="queue_for_review",
            registration_allowed=False,
            learning_action="mark_matching_card_statement_target_excluded_after_supplier_primary_journal",
            unresolved_points=unresolved_points,
            journal_candidate=None,
        )
    matches = _find_pattern_matches(conn, normalized)
    if not matches:
        return FinancialEventClassification(
            event_hash=normalized["event_hash"],
            source_event_type=normalized["source_event_type"],
            transaction_date=normalized["transaction_date"],
            description_key=normalized["description_key"],
            amount_bucket=normalized["amount_bucket"],
            cash_flow_direction=normalized["cash_flow_direction"],
            account_key_hash=normalized["account_key_hash"],
            matched_pattern_hash=None,
            matched_pattern_source="none",
            confidence=0.0,
            recommended_action="queue_for_learning",
            registration_allowed=False,
            learning_action="capture_user_resolution_as_new_financial_pattern",
            unresolved_points=["no_past_journal_pattern_match"],
            journal_candidate=None,
        )
    best = matches[0]
    confidence = float(best["confidence"])
    eligibility = str(best["auto_post_eligibility"] or "disabled")
    unresolved_points = []
    if normalized["cash_flow_direction"] == "inflow" and not _has_invoice_or_evidence_match(event):
        unresolved_points.append("inflow_requires_invoice_or_revenue_match")
    stable_description_amount = (
        str(best["match_source"]) == "description_amount"
        and normalized["cash_flow_direction"] == "outflow"
        and eligibility in AUTO_ELIGIBLE
        and int(best["success_count"] or 0) >= 2
        and int(best["failure_count"] or 0) == 0
        and str(best["tax_category"] or "") not in {"", "不明", "unknown_tax"}
    )
    if confidence < 0.95 and not stable_description_amount:
        unresolved_points.append(f"confidence={confidence}")
    if eligibility not in AUTO_ELIGIBLE:
        unresolved_points.append(f"auto_post_eligibility={eligibility}")
    registration_allowed = not unresolved_points
    recommended_action = "auto_post_candidate" if registration_allowed else "queue_for_review"
    learning_action = "reinforce_financial_pattern" if registration_allowed else "confirm_then_reinforce_or_correct_pattern"
    return FinancialEventClassification(
        event_hash=normalized["event_hash"],
        source_event_type=normalized["source_event_type"],
        transaction_date=normalized["transaction_date"],
        description_key=normalized["description_key"],
        amount_bucket=normalized["amount_bucket"],
        cash_flow_direction=normalized["cash_flow_direction"],
        account_key_hash=normalized["account_key_hash"],
        matched_pattern_hash=str(best["pattern_hash"]),
        matched_pattern_source=str(best["match_source"]),
        confidence=confidence,
        recommended_action=recommended_action,
        registration_allowed=registration_allowed,
        learning_action=learning_action,
        unresolved_points=unresolved_points,
        journal_candidate=_journal_candidate_from_pattern(best, normalized),
    )


def _classify_verified_evidence_resolution(
    event: dict[str, Any],
    normalized: dict[str, str],
) -> FinancialEventClassification | None:
    """Authorize one exact card row only from verified PDF facts plus a resolved candidate."""

    resolution = event.get("evidence_resolution")
    if not isinstance(resolution, dict) or resolution.get("status") != "verified":
        return None
    candidate = resolution.get("journal_candidate")
    if not isinstance(candidate, dict):
        raise ValueError("verified evidence resolution requires journal_candidate")
    required_hashes = ("source_file_hash", "extraction_hash", "facts_hash")
    if any(
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(resolution.get(field) or "")) is None
        for field in required_hashes
    ):
        raise ValueError("verified evidence resolution hashes are incomplete")
    if (
        resolution.get("recipient_verified") is not True
        or resolution.get("amount_date_vendor_exact_match") is not True
        or resolution.get("qualified_invoice_readback") is not True
        or str(resolution.get("transaction_date") or "") != normalized["transaction_date"]
        or int(resolution.get("amount") or 0) != int(normalized["amount"])
    ):
        raise ValueError("verified evidence resolution does not match the financial event")
    for field in ("debit_account", "credit_account", "tax_category"):
        if not str(candidate.get(field) or "").strip():
            raise ValueError(f"verified evidence candidate missing {field}")
    if str(candidate.get("tax_category")) in {"", "不明", "unknown_tax"}:
        raise ValueError("verified evidence candidate tax category is unresolved")
    resolved_candidate = {
        **candidate,
        "source_file_hash": resolution["source_file_hash"],
        "evidence_facts_hash": resolution["facts_hash"],
    }
    return FinancialEventClassification(
        event_hash=normalized["event_hash"],
        source_event_type=normalized["source_event_type"],
        transaction_date=normalized["transaction_date"],
        description_key=normalized["description_key"],
        amount_bucket=normalized["amount_bucket"],
        cash_flow_direction=normalized["cash_flow_direction"],
        account_key_hash=normalized["account_key_hash"],
        matched_pattern_hash=str(candidate.get("matched_pattern_hash") or "") or None,
        matched_pattern_source="verified_pdf_evidence_resolution",
        confidence=1.0,
        recommended_action="auto_post_candidate",
        registration_allowed=True,
        learning_action="learn_after_journal_voucher_drive_readback",
        unresolved_points=[],
        journal_candidate=resolved_candidate,
    )


def _matching_company_rule(event: dict[str, Any], normalized: dict[str, str]) -> dict | None:
    matches = []
    for rule in company_policies("accounting_rules.financial_event_rules"):
        condition = rule.get("match", {})
        if not isinstance(condition, dict) or not condition:
            raise ValueError("fixed accounting rule requires explicit match conditions")
        if set(condition) - {"source_event_type", "cash_flow_direction", "description_contains", "vendor_equals"}:
            raise ValueError("unsupported fixed accounting rule condition")
        if condition.get("source_event_type") and condition["source_event_type"] != normalized["source_event_type"]:
            continue
        if condition.get("cash_flow_direction") and condition["cash_flow_direction"] != normalized["cash_flow_direction"]:
            continue
        text = _search_text(_pick(event, "description", "memo"), _pick(event, "counterparty", "vendor_name"))
        aliases = condition.get("description_contains")
        if aliases is not None:
            if not isinstance(aliases, (list, tuple)) or not aliases or any(not str(x).strip() for x in aliases):
                raise ValueError("fixed rule aliases must be nonempty")
            if not any(_search_text(alias) in text for alias in aliases):
                continue
        vendor = condition.get("vendor_equals")
        if vendor and _search_text(vendor) != _search_text(_pick(event, "counterparty", "vendor_name")):
            continue
        matches.append(rule)
    if len(matches) > 1:
        raise ValueError("ambiguous company fixed accounting rule")
    return matches[0] if matches else None


def _classify_company_fixed_event(event: dict[str, Any], normalized: dict[str, str]) -> FinancialEventClassification | None:
    rule = _matching_company_rule(event, normalized)
    if rule is None:
        return None
    fields = rule.get("candidate", {})
    allowed = {"debit_account", "debit_sub_account", "credit_account", "credit_sub_account",
               "tax_category", "vendor_name", "trade_partner_name"}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValueError("fixed accounting rule cannot override authoritative facts")
    if any(not fields.get(key) for key in ("debit_account", "credit_account", "tax_category")):
        raise ValueError("fixed accounting rule is incomplete")
    pattern = {
        **fields, "debit_sub_account": fields.get("debit_sub_account"),
        "credit_sub_account": fields.get("credit_sub_account"), "pattern_hash": stable_hash(rule),
    }
    candidate = _journal_candidate_from_pattern(pattern, normalized)
    candidate.update({key: fields[key] for key in ("vendor_name", "trade_partner_name") if key in fields})
    return FinancialEventClassification(
        event_hash=normalized["event_hash"], source_event_type=normalized["source_event_type"],
        transaction_date=normalized["transaction_date"], description_key=normalized["description_key"],
        amount_bucket=normalized["amount_bucket"], cash_flow_direction=normalized["cash_flow_direction"],
        account_key_hash=normalized["account_key_hash"], matched_pattern_hash=stable_hash(rule),
        matched_pattern_source="company_fixed_accounting_rule", confidence=1.0,
        recommended_action="auto_post_candidate", registration_allowed=True,
        learning_action="reinforce_company_rule_after_readback", unresolved_points=[],
        journal_candidate=candidate,
    )


def upsert_company_fixed_accounting_rules(conn: sqlite3.Connection) -> int:
    """Materialize explicit current-company rules without deleting existing history."""
    rules = company_policies("accounting_rules.financial_event_rules")
    if not rules:
        return 0
    cards = []
    for rule in rules:
        # Direction-aware rules must not enter the unscoped description-key executor.
        fields = rule.get("candidate", {})
        if not rule.get("match") or any(not fields.get(key) for key in ("debit_account", "credit_account", "tax_category")):
            raise ValueError("fixed accounting rule is incomplete")
        cards.append({
            "card_type": "fixed_rule_candidate",
            "title": "Configured company accounting policy",
            "body": json.dumps(rule, ensure_ascii=False, sort_keys=True),
        })
    source_hash = upsert_sync_manifest(conn, "local", "company_fixed_accounting_rules", cards)
    count = build_rag_cards(conn, cards, source_hash, commit=False)
    conn.commit()
    return count


def _classify_verified_payroll_resolution(
    event: dict[str, Any],
    normalized: dict[str, str],
) -> FinancialEventClassification | None:
    resolution = event.get("payroll_resolution")
    if not isinstance(resolution, dict) or resolution.get("status") != "verified":
        return None
    candidate = resolution.get("journal_candidate")
    contract = resolution.get("payroll_contract")
    if not isinstance(candidate, dict) or not isinstance(contract, dict):
        return None
    evidence_hash = str(resolution.get("payroll_evidence_hash") or "")
    source_item = {
        "candidate": candidate,
        "payroll_contract": contract,
        "payroll_evidence_hash": evidence_hash,
        "payroll_period": resolution.get("payroll_period"),
        "payment_date": resolution.get("payment_date"),
        "payroll_workflow_step": resolution.get("payroll_workflow_step"),
    }
    try:
        from .journal_write_queue import _validate_payroll_write_queue_item

        _validate_payroll_write_queue_item(source_item=source_item, item_index=0)
    except ValueError:
        return None
    if (
        str(resolution.get("payroll_workflow_step")) == "settlement"
        and int(candidate.get("amount") or 0) != int(normalized["amount"])
    ):
        return None
    enriched_candidate = {
        **candidate,
        "payroll_contract": contract,
        "payroll_evidence_hash": evidence_hash,
        "payroll_period": str(resolution.get("payroll_period") or ""),
        "payment_date": str(resolution.get("payment_date") or ""),
        "payroll_workflow_step": str(resolution.get("payroll_workflow_step") or ""),
    }
    return FinancialEventClassification(
        event_hash=normalized["event_hash"],
        source_event_type=normalized["source_event_type"],
        transaction_date=normalized["transaction_date"],
        description_key=normalized["description_key"],
        amount_bucket=normalized["amount_bucket"],
        cash_flow_direction=normalized["cash_flow_direction"],
        account_key_hash=normalized["account_key_hash"],
        matched_pattern_hash=None,
        matched_pattern_source="verified_payroll_ledger_resolution",
        confidence=1.0,
        recommended_action="auto_post_candidate",
        registration_allowed=True,
        learning_action="learn_verified_employee_payroll_pattern_after_readback",
        unresolved_points=[],
        journal_candidate=enriched_candidate,
    )


def _looks_like_payroll_event(normalized: dict[str, str]) -> bool:
    text = unicodedata.normalize(
        "NFKC",
        f"{normalized.get('vendor_key', '')} {normalized.get('description_key', '')}",
    ).casefold()
    return any(marker in text for marker in ("給与", "給料", "賞与", "キユウヨ", "キュウヨ"))


def _find_pattern_matches(conn: sqlite3.Connection, event: dict[str, str]) -> list[dict[str, Any]]:
    if event["source_event_type"] in {"receipt", "received_invoice"}:
        broad_content = str(event.get("broad_content") or "")
        tax_category = str(event.get("receipt_tax_category") or "")
        if not broad_content or not tax_category:
            return []
        same_vendor = conn.execute(
            """
            SELECT *
            FROM journal_patterns
            WHERE vendor_key = ?
              AND description_key = ?
              AND amount_bucket = ?
              AND tax_category = ?
              AND auto_post_eligibility IN ('limited', 'enabled')
              AND failure_count = 0
            ORDER BY
              success_count DESC,
              last_seen_at DESC
            LIMIT 50
            """,
            (
                event["vendor_key"],
                broad_content,
                event["amount_bucket"],
                tax_category,
            ),
        ).fetchall()
        candidates = [dict(row) for row in same_vendor]
        if not candidates:
            return []
        shape_support: dict[tuple[str, ...], int] = {}
        for row in candidates:
            shape = (
                str(row.get("debit_account") or ""),
                str(row.get("debit_sub_account") or ""),
                str(row.get("credit_account") or ""),
                str(row.get("credit_sub_account") or ""),
                str(row.get("tax_category") or ""),
            )
            shape_support[shape] = shape_support.get(shape, 0) + max(
                1, int(row.get("success_count") or 0)
            )
        total_support = sum(shape_support.values())
        winning_shape, winning_support = max(
            shape_support.items(), key=lambda item: item[1]
        )
        if total_support < 2 or winning_support / total_support < 0.8:
            return []
        result = []
        for row in candidates:
            shape = tuple(
                str(row.get(field) or "")
                for field in (
                    "debit_account",
                    "debit_sub_account",
                    "credit_account",
                    "credit_sub_account",
                    "tax_category",
                )
            )
            if shape == winning_shape:
                result.append(
                    {
                        **row,
                        "confidence": winning_support / total_support,
                        "match_source": "vendor_broad_content_history",
                    }
                )
        return result
    rows: list[dict[str, Any]] = []
    exact = conn.execute(
        """
        SELECT *, 1.0 AS confidence, 'exact_vendor_description_amount' AS match_source
        FROM journal_patterns
        WHERE vendor_key = ?
          AND description_key = ?
          AND amount_bucket = ?
        ORDER BY
          CASE auto_post_eligibility WHEN 'enabled' THEN 3 WHEN 'limited' THEN 2 WHEN 'shadow' THEN 1 ELSE 0 END DESC,
          success_count DESC,
          failure_count ASC
        LIMIT 5
        """,
        (event["vendor_key"], event["description_key"], event["amount_bucket"]),
    ).fetchall()
    rows.extend(dict(row) for row in exact)
    if rows:
        return rows
    description_amount = conn.execute(
        """
        SELECT *, 0.9 AS confidence, 'description_amount' AS match_source
        FROM journal_patterns
        WHERE description_key = ?
          AND amount_bucket = ?
        ORDER BY
          CASE auto_post_eligibility WHEN 'enabled' THEN 3 WHEN 'limited' THEN 2 WHEN 'shadow' THEN 1 ELSE 0 END DESC,
          success_count DESC,
          failure_count ASC
        LIMIT 5
        """,
        (event["description_key"], event["amount_bucket"]),
    ).fetchall()
    rows.extend(dict(row) for row in description_amount)
    if rows:
        return rows
    description_only = conn.execute(
        """
        SELECT *, 0.75 AS confidence, 'description_only' AS match_source
        FROM journal_patterns
        WHERE description_key = ?
        ORDER BY
          CASE auto_post_eligibility WHEN 'enabled' THEN 3 WHEN 'limited' THEN 2 WHEN 'shadow' THEN 1 ELSE 0 END DESC,
          success_count DESC,
          failure_count ASC
        LIMIT 5
        """,
        (event["description_key"],),
    ).fetchall()
    rows = [dict(row) for row in description_only]
    if rows:
        return rows
    vendor = str(event["vendor_key"] or "").strip().casefold()
    if vendor in {"", "unknown_vendor"} or len(vendor) < 3:
        return []
    tax_category = str(event.get("receipt_tax_category") or "")
    if not tax_category:
        return []
    cross_field = conn.execute(
        """
        SELECT *, 0.85 AS confidence, 'vendor_in_description_amount' AS match_source
        FROM journal_patterns
        WHERE amount_bucket = ?
          AND auto_post_eligibility IN ('limited', 'enabled')
          AND failure_count = 0
          AND tax_category = ?
          AND instr(lower(description_key), ?) > 0
        ORDER BY success_count DESC, last_seen_at DESC
        LIMIT 5
        """,
        (event["amount_bucket"], tax_category, vendor),
    ).fetchall()
    candidates = [dict(row) for row in cross_field]
    shapes = {
        (
            str(row.get("debit_account") or ""),
            str(row.get("debit_sub_account") or ""),
            str(row.get("credit_account") or ""),
            str(row.get("credit_sub_account") or ""),
            str(row.get("tax_category") or ""),
        )
        for row in candidates
    }
    return candidates if len(shapes) == 1 else []


def _journal_candidate_from_pattern(pattern: dict[str, Any], event: dict[str, str]) -> dict[str, Any]:
    tax_rate = _tax_rate(str(pattern["tax_category"]))
    amount = int(event["amount"])
    candidate: dict[str, Any] = {
        "source_event_type": event["source_event_type"],
        "source_type": event["source_event_type"],
        "journal_date": event["transaction_date"],
        "transaction_date": event["transaction_date"],
        "vendor_name": event["vendor_key"],
        "amount": amount,
        "tax_amount": _tax_amount(amount, tax_rate),
        "tax_rate": tax_rate,
        "description_key": event["description_key"],
        "description": event["description_key"],
        "amount_bucket": event["amount_bucket"],
        "debit_account": pattern["debit_account"],
        "debit_sub_account": pattern["debit_sub_account"],
        "credit_account": pattern["credit_account"],
        "credit_sub_account": pattern["credit_sub_account"],
        "tax_category": pattern["tax_category"],
        "matched_pattern_hash": pattern["pattern_hash"],
    }
    transaction_binding = _moneyforward_transaction_binding(event)
    if transaction_binding is not None:
        candidate["moneyforward_transaction_binding"] = transaction_binding
    return candidate


def _has_invoice_or_evidence_match(event: dict[str, Any]) -> bool:
    value = (
        event.get("matched_invoice_or_evidence")
        or event.get("matched_counterpart_hash")
        or event.get("invoice_match_hash")
        or event.get("evidence_match_hash")
    )
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    if value is None:
        return False
    return bool(str(value).strip())


def _configured_card_statement_requires_target_exclusion(event: dict[str, Any], normalized: dict[str, str]) -> bool:
    if normalized["source_event_type"] not in {"card_transaction", "card_statement"}:
        return False
    account = _pick(event, "bank_or_card_account", "account_name", "connected_account_name", "wallet_name")
    vendor = _search_text(_pick(event, "counterparty", "vendor_name"), _pick(event, "description", "memo"))
    return any(
        row.get("card_account_key") == account and account
        and any(_search_text(alias) in vendor for alias in row.get("vendor_aliases", []) if str(alias).strip())
        for row in company_policies("accounting_rules.supplier_primary_card_routes")
    )


def _has_supplier_primary_match(event: dict[str, Any]) -> bool:
    for key in ("matched_supplier_primary_purchase", "supplier_primary_match"):
        match = event.get(key)
        if isinstance(match, dict):
            if match.get("company_slug") != get_current_profile().slug:
                raise ValueError("supplier primary match company binding mismatch")
            if match.get("supplier_registration_readback_verified") is True and match.get("voucher_readback_verified") is True:
                return True
    return False


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _search_text(*values: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold() for value in values)


def _persist_classifications(conn: sqlite3.Connection, classifications: list[FinancialEventClassification]) -> None:
    for item in classifications:
        conn.execute(
            """
            INSERT INTO financial_event_classifications (
                event_hash, source_event_type, transaction_date, description_key,
                amount_bucket, cash_flow_direction, account_key_hash, matched_pattern_hash,
                matched_pattern_source, confidence_score, recommended_action,
                registration_allowed, learning_action, unresolved_points_json,
                result_summary_redacted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_hash) DO UPDATE SET
                matched_pattern_hash = excluded.matched_pattern_hash,
                matched_pattern_source = excluded.matched_pattern_source,
                confidence_score = excluded.confidence_score,
                recommended_action = excluded.recommended_action,
                registration_allowed = excluded.registration_allowed,
                learning_action = excluded.learning_action,
                unresolved_points_json = excluded.unresolved_points_json,
                result_summary_redacted = excluded.result_summary_redacted,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                item.event_hash,
                item.source_event_type,
                item.transaction_date,
                item.description_key,
                item.amount_bucket,
                item.cash_flow_direction,
                item.account_key_hash,
                item.matched_pattern_hash,
                item.matched_pattern_source,
                item.confidence,
                item.recommended_action,
                1 if item.registration_allowed else 0,
                item.learning_action,
                json.dumps(item.unresolved_points, ensure_ascii=False, sort_keys=True),
                _result_summary(item),
            ),
        )
    conn.commit()


def _materialize_bank_transaction_rag(conn: sqlite3.Connection, classifications: list[FinancialEventClassification]) -> int:
    cards = []
    for item in classifications:
        if item.matched_pattern_hash is None:
            continue
        body = " | ".join(
            [
                f"source_event_type={item.source_event_type}",
                f"description={item.description_key}",
                f"amount_bucket={item.amount_bucket}",
                f"match={item.matched_pattern_source}",
                f"confidence={item.confidence}",
                f"action={item.recommended_action}",
            ]
        )
        cards.append(
            {
                "card_type": "bank_transaction_pattern",
                "title": f"{item.source_event_type} {item.description_key}"[:120],
                "body": body,
            }
        )
    if not cards:
        return 0
    source_hash = upsert_sync_manifest(conn, "local", "financial_event_classifications", cards)
    return build_rag_cards(conn, cards, source_hash)


def _result_summary(item: FinancialEventClassification) -> str:
    return " | ".join(
        [
            f"action={item.recommended_action}",
            f"match={item.matched_pattern_source}",
            f"confidence={item.confidence}",
            f"amount_bucket={item.amount_bucket}",
        ]
    )


def _normalized_event(event: dict[str, Any]) -> dict[str, str]:
    description = _pick(event, "description", "memo", "摘要", "remark", "name")
    counterparty = _pick(event, "counterparty", "vendor_name", "partner_name", "payee", "payer")
    account = _pick(event, "bank_or_card_account", "account_name", "connected_account_name", "wallet_name")
    source_event_id = _pick(event, "source_event_id", "id", "transaction_id", "statement_id")
    amount = _amount(event)
    direction = _cash_flow_direction(event)
    transaction_date = _pick(event, "transaction_date", "date", "recorded_on", "posted_at")[:10]
    description_key = _description_key(description)
    vendor_key = _vendor_key(counterparty, description_key)
    broad_content_value = str(event.get("broad_content") or "").strip()
    broad_content = (
        _description_key(broad_content_value) if broad_content_value else ""
    )
    payload_hash = {
        "source_event_id_hash": mask_identifier(source_event_id),
        "source_event_type": _source_event_type(event),
        "transaction_date": transaction_date,
        "description_key": description_key,
        "vendor_key": vendor_key,
        "amount_bucket": _amount_bucket(amount),
        "cash_flow_direction": direction,
        "account_key_hash": mask_identifier(account),
    }
    if broad_content:
        payload_hash["broad_content"] = broad_content
    normalized = {
        **payload_hash,
        "amount": str(amount),
        "event_hash": stable_hash(payload_hash),
    }
    receipt_rates = {
        str(row.get("rate") or "").strip()
        for row in event.get("receipt_tax_rows") or []
        if isinstance(row, dict)
    }
    if len(receipt_rates) == 1:
        normalized["receipt_tax_category"] = {
            "8": "課仕 (軽)8%",
            "10": "課仕 10%",
        }.get(next(iter(receipt_rates)), "")
    for field in (
        "transaction_id",
        "connected_account_id",
        "connected_sub_account_id",
        "connected_account_binding_hash",
        "connected_ledger_account_id",
        "connected_ledger_sub_account_id",
        "connected_ledger_binding_hash",
        "transaction_side",
        "transaction_value",
        "transaction_original_date",
        "transaction_facts_hash",
        "journalizing_status",
        "source_row_hash",
        "source_api",
    ):
        value = event.get(field)
        if value not in (None, ""):
            normalized[field] = str(value)
    return normalized


def _is_moneyforward_transaction_event(event: dict[str, Any]) -> bool:
    return bool(
        str(event.get("transaction_id") or "").strip()
        or str(event.get("source_api") or "").strip() == "GET /api/v3/transactions"
    )


def _moneyforward_transaction_binding(event: dict[str, Any]) -> dict[str, str] | None:
    if not _is_moneyforward_transaction_event(event):
        return None
    transaction_id = str(event.get("transaction_id") or "").strip()
    source_event_id = str(event.get("source_event_id") or event.get("id") or transaction_id).strip()
    connected_account_id = str(event.get("connected_account_id") or "").strip()
    connected_sub_account_id = str(event.get("connected_sub_account_id") or "").strip()
    source_row_hash = str(event.get("source_row_hash") or "").strip()
    account_binding_hash = str(event.get("connected_account_binding_hash") or "").strip()
    ledger_account_id = str(event.get("connected_ledger_account_id") or "").strip()
    ledger_sub_account_id = str(event.get("connected_ledger_sub_account_id") or "").strip()
    ledger_binding_hash = str(event.get("connected_ledger_binding_hash") or "").strip()
    transaction_side = str(event.get("transaction_side") or "").strip()
    transaction_value = str(event.get("transaction_value") or "").strip()
    transaction_original_date = str(event.get("transaction_original_date") or "").strip()
    transaction_facts_hash = str(event.get("transaction_facts_hash") or "").strip()
    journalizing_status = str(event.get("journalizing_status") or "").strip()
    if not transaction_id or source_event_id != transaction_id or not connected_account_id:
        return None
    if not _is_sha256_hex(source_row_hash):
        return None
    if account_binding_hash != mask_identifier(f"{connected_account_id}:{connected_sub_account_id}"):
        return None
    if not ledger_account_id:
        return None
    if ledger_binding_hash != mask_identifier(
        f"{connected_account_id}:{connected_sub_account_id}:{ledger_account_id}:{ledger_sub_account_id}"
    ):
        return None
    if transaction_side not in {"INCOME", "EXPENSE"}:
        return None
    if not transaction_value.isdigit() or int(transaction_value) <= 0:
        return None
    if transaction_original_date != str(event.get("transaction_date") or "")[:10]:
        return None
    if not _is_sha256_hex(transaction_facts_hash) or journalizing_status != "none":
        return None
    return {
        "transaction_id": transaction_id,
        "connected_account_id": connected_account_id,
        "connected_sub_account_id": connected_sub_account_id,
        "connected_account_binding_hash": account_binding_hash,
        "connected_ledger_account_id": ledger_account_id,
        "connected_ledger_sub_account_id": ledger_sub_account_id,
        "connected_ledger_binding_hash": ledger_binding_hash,
        "transaction_side": transaction_side,
        "transaction_value": transaction_value,
        "transaction_original_date": transaction_original_date,
        "transaction_facts_hash": transaction_facts_hash,
        "source_row_hash": source_row_hash,
        "journalizing_status": journalizing_status,
        "source_api": "GET /api/v3/transactions",
    }


def _attach_moneyforward_transaction_binding(
    item: FinancialEventClassification,
    event: dict[str, Any],
) -> FinancialEventClassification:
    if not _is_moneyforward_transaction_event(event):
        return item
    binding = _moneyforward_transaction_binding(event)
    if binding is None:
        unresolved = list(item.unresolved_points)
        if "moneyforward_transaction_binding_incomplete" not in unresolved:
            unresolved.append("moneyforward_transaction_binding_incomplete")
        return replace(
            item,
            registration_allowed=False,
            recommended_action="queue_for_review",
            learning_action="restore_moneyforward_transaction_binding_before_registration",
            unresolved_points=unresolved,
        )
    if item.journal_candidate is None:
        return item
    candidate = dict(item.journal_candidate)
    candidate["moneyforward_transaction_binding"] = binding
    return replace(item, journal_candidate=candidate)


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("events", "transactions", "items", "data", "connected_account_transactions"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _pick(event: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _source_event_type(event: dict[str, Any]) -> str:
    value = _pick(event, "source_event_type", "type", "kind")
    if value:
        return _safe_token(value)
    account = _pick(event, "bank_or_card_account", "account_name", "connected_account_name")
    if any(marker in account.casefold() for marker in ("card", "カード")):
        return "card_transaction"
    return "bank_transaction"


def _description_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?", " ", normalized)
    normalized = re.sub(r"\d{1,2}月(?:分|度)?", " ", normalized)
    normalized = re.sub(r"\d{1,3}(?:,\d{3})+円?", " ", normalized)
    normalized = re.sub(r"\d+円", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:120] or "no_description"


def _vendor_key(counterparty: str, description_key: str) -> str:
    value = counterparty.strip() or description_key
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    return value[:80] if value else "unknown_vendor"


def _amount(event: dict[str, Any]) -> int:
    for key in ("amount", "value", "total_amount", "withdrawal", "deposit", "price"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return abs(int(value))
        if isinstance(value, str):
            text = value.replace(",", "").strip()
            if text.lstrip("-").isdigit():
                return abs(int(text))
    return 0


def _cash_flow_direction(event: dict[str, Any]) -> str:
    explicit = _pick(event, "cash_flow_direction", "direction", "入出金区分")
    if explicit:
        lowered = explicit.casefold()
        if lowered in {"outflow", "withdrawal", "debit", "支出", "出金", "引落", "利用"}:
            return "outflow"
        if lowered in {"inflow", "deposit", "credit", "収入", "入金"}:
            return "inflow"
    if _pick(event, "withdrawal", "出金額", "支払額"):
        return "outflow"
    if _pick(event, "deposit", "入金額"):
        return "inflow"
    for key in ("amount", "value", "total_amount", "price"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            if value < 0:
                return "outflow"
            if value > 0:
                return "inflow"
        if isinstance(value, str):
            text = value.replace(",", "").strip()
            if text.lstrip("-").isdigit():
                number = int(text)
                if number < 0:
                    return "outflow"
                if number > 0:
                    return "inflow"
    return "unknown"


def _tax_rate(tax_category: str) -> float:
    if "10" in tax_category:
        return 0.10
    if "8" in tax_category:
        return 0.08
    return 0.0


def _tax_amount(amount: int, tax_rate: float) -> int:
    if tax_rate <= 0:
        return 0
    return round(amount * tax_rate / (1 + tax_rate))


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


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_一-龯ぁ-んァ-ンー-]+", "_", unicodedata.normalize("NFKC", value)).strip("_")
    return token[:80] or "unknown"


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_event_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_hash TEXT NOT NULL UNIQUE,
            source_event_type TEXT NOT NULL,
            transaction_date TEXT NOT NULL,
            description_key TEXT NOT NULL,
            amount_bucket TEXT NOT NULL,
            cash_flow_direction TEXT NOT NULL DEFAULT 'unknown'
                CHECK (cash_flow_direction IN ('outflow', 'inflow', 'unknown')),
            account_key_hash TEXT NOT NULL,
            matched_pattern_hash TEXT,
            matched_pattern_source TEXT NOT NULL,
            confidence_score REAL NOT NULL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
            recommended_action TEXT NOT NULL CHECK (recommended_action IN ('auto_post_candidate', 'queue_for_review', 'queue_for_learning')),
            registration_allowed INTEGER NOT NULL CHECK (registration_allowed IN (0, 1)),
            learning_action TEXT NOT NULL,
            unresolved_points_json TEXT NOT NULL,
            result_summary_redacted TEXT NOT NULL,
            raw_event_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_event_id_stored = 0),
            raw_amount_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amount_stored = 0),
            raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()


def _journal_batch_items(items: list[FinancialEventClassification]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in items:
        if not item.registration_allowed or not item.journal_candidate:
            continue
        batch_item = {
            "candidate": _journal_batch_candidate(item.journal_candidate),
            "evidence": _financial_event_evidence(item),
        }
        if item.journal_candidate.get("payroll_contract"):
            batch_item.update(
                {
                    "payroll_contract": item.journal_candidate["payroll_contract"],
                    "payroll_evidence_hash": item.journal_candidate["payroll_evidence_hash"],
                    "payroll_period": item.journal_candidate["payroll_period"],
                    "payment_date": item.journal_candidate["payment_date"],
                    "payroll_workflow_step": item.journal_candidate[
                        "payroll_workflow_step"
                    ],
                }
            )
        binding = item.journal_candidate.get("moneyforward_transaction_binding")
        if isinstance(binding, dict):
            batch_item["execution_channel"] = "transaction_journalize"
            batch_item["moneyforward_transaction_binding"] = dict(binding)
        else:
            batch_item["execution_channel"] = "journal_create"
        output.append(batch_item)
    return output


def _journal_batch_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    batch_candidate = {
        "journal_date": candidate["journal_date"],
        "vendor_name": candidate["vendor_name"],
        "amount": candidate["amount"],
        "tax_amount": candidate["tax_amount"],
        "tax_rate": candidate["tax_rate"],
        "debit_account": candidate["debit_account"],
        "debit_sub_account": candidate["debit_sub_account"],
        "credit_account": candidate["credit_account"],
        "credit_sub_account": candidate["credit_sub_account"],
        "tax_category": candidate["tax_category"],
        "description": candidate["description"],
        "source_type": candidate["source_type"],
        "risk_flags": [],
    }
    for key in ("payroll_period", "payment_date", "payroll_workflow_step"):
        if candidate.get(key):
            batch_candidate[key] = candidate[key]
    return batch_candidate


def _financial_event_evidence(item: FinancialEventClassification) -> dict[str, Any]:
    if item.journal_candidate and item.journal_candidate.get("payroll_evidence_hash"):
        file_hash = str(item.journal_candidate["payroll_evidence_hash"])
    else:
        file_hash = f"financial-event:{item.event_hash}"
    return {
        "google_drive_file_id": f"financial-event:{item.event_hash}",
        "file_name": f"{item.source_event_type}-{item.transaction_date}-{item.event_hash[:12]}.json",
        "file_hash": file_hash,
        "exists": True,
        "attachable": True,
        "parent_folder_id": "moneyforward-connected-statements",
        "parent_folder_name": "Money Forward金融機関連携明細",
    }
