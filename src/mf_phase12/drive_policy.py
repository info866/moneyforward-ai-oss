from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from .hash_utils import mask_identifier
from .models import Evidence, JournalCandidate
from .office_profile import get_current_profile, get_setting


def company_policies(path: str) -> tuple[dict, ...]:
    """Read explicit policies for this profile; reject foreign or unscoped rows."""
    rows = get_setting(path, [])
    if not isinstance(rows, (list, tuple)):
        raise ValueError(f"{path} must be a list")
    profile = get_current_profile()
    if profile is None:
        raise ValueError("explicit current company profile is required")
    slug = profile.slug
    if any(not isinstance(row, Mapping) or row.get("company_slug") != slug for row in rows):
        raise ValueError(f"{path} company binding mismatch")
    return tuple(_plain_policy(row) for row in rows)


def _plain_policy(value):
    if isinstance(value, Mapping):
        return {key: _plain_policy(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_policy(child) for child in value]
    return value


def require_source_route(evidence: Evidence | None) -> None:
    profile = get_current_profile()
    if profile is None:
        raise ValueError("explicit current company profile is required")
    allowed = set(profile.google_drive_evidence_source_folder_ids)
    if not evidence or not evidence.parent_folder_id or evidence.parent_folder_id not in allowed:
        raise ValueError("source Drive route is not configured for the current company")


def default_credit_account() -> str:
    return str(get_setting("accounting_rules.default_credit_account", "") or "")


INVOICE_SOURCE_TYPES = frozenset({"invoice", "bill", "purchase_invoice", "received_invoice"})
FINANCIAL_STATEMENT_SOURCE_TYPES = frozenset({"bank_transaction", "card_transaction", "card_statement"})
DIRECT_LEDGER_SOURCE_TYPES = frozenset(
    {
        "withholding_tax_correction",
        "payroll_wage_ledger",
        "social_insurance_reclassification",
    }
)


@dataclass(frozen=True)
class FolderCreditRule:
    folder_name_contains: str
    credit_account: str
    credit_sub_account: str
    folder_id: str | None = None


@dataclass(frozen=True)
class CreditSideDecision:
    credit_account: str
    credit_sub_account: str | None
    source: str
    sub_account_exists: bool | None = None
    sub_account_creation_required: bool = False


@dataclass(frozen=True)
class ExpenseSideDecision:
    debit_account: str
    debit_sub_account: str | None
    source: str


def processed_evidence_folder() -> dict[str, str]:
    profile = get_current_profile()
    if profile is None:
        raise ValueError("explicit current company profile is required")
    destination = profile.google_drive_processed_folder_id
    if not destination:
        raise ValueError("processed Drive route is not configured")
    return {
        "folder_id": destination,
        "purpose": "processed_evidence",
        "move_after_status": "journal_created_and_voucher_attached_and_read_back_verified",
        "preserve_file_name": "true",
    }


def drive_move_request(evidence: Evidence, destination_folder_id: str | None = None) -> dict[str, str]:
    require_source_route(evidence)
    configured = processed_evidence_folder()["folder_id"]
    if destination_folder_id is not None and destination_folder_id != configured:
        raise ValueError("destination Drive route does not belong to the current company")
    return {
        "file_id": evidence.google_drive_file_id,
        "source_folder_id": evidence.parent_folder_id,
        "destination_folder_id": configured,
        "file_name": evidence.file_name,
        "rename": "false",
    }


def apply_drive_credit_policy(candidate: JournalCandidate, evidence: Evidence | None = None) -> JournalCandidate:
    if candidate.source_type in DIRECT_LEDGER_SOURCE_TYPES:
        return candidate
    if candidate.source_type in FINANCIAL_STATEMENT_SOURCE_TYPES and evidence is None:
        return candidate
    decision = decide_credit_side(candidate, evidence)
    # A person-owned source folder is the accounting authority.  A payment method
    # (including a card statement) may be used for duplicate checks, but may never
    # replace the payable owner selected from the source hierarchy.
    if candidate.source_type in FINANCIAL_STATEMENT_SOURCE_TYPES and decision.source != "source_folder_rule":
        return candidate
    return JournalCandidate(
        journal_date=candidate.journal_date,
        vendor_name=candidate.vendor_name,
        amount=candidate.amount,
        tax_amount=candidate.tax_amount,
        tax_rate=candidate.tax_rate,
        debit_account=candidate.debit_account,
        debit_sub_account=candidate.debit_sub_account,
        credit_account=decision.credit_account,
        credit_sub_account=decision.credit_sub_account,
        tax_category=candidate.tax_category,
        description=candidate.description,
        invoice_number=candidate.invoice_number,
        trade_partner_code=candidate.trade_partner_code,
        trade_partner_name=candidate.trade_partner_name,
        source_type=candidate.source_type,
        risk_flags=candidate.risk_flags,
    )


def folder_expense_decision(evidence: Evidence | None = None) -> ExpenseSideDecision | None:
    """Resolve only an explicitly configured canonical expense route."""

    if evidence is not None:
        require_source_route(evidence)
    rules = company_policies("accounting_rules.folder_expense_rules")
    if not rules:
        return None
    require_source_route(evidence)
    matches = [rule for rule in rules if rule.get("folder_id") == evidence.parent_folder_id]
    if len(matches) > 1:
        raise ValueError("ambiguous expense route")
    if not matches:
        return None
    rule = matches[0]
    if not rule.get("debit_account"):
        raise ValueError("expense route account is missing")
    return ExpenseSideDecision(rule["debit_account"], rule.get("debit_sub_account"), "source_folder_expense_route")


def apply_drive_expense_policy(
    candidate: JournalCandidate,
    evidence: Evidence | None = None,
) -> JournalCandidate:
    """Apply an explicit debit route while preserving evidence-derived details."""

    decision = folder_expense_decision(evidence)
    if decision is None:
        return candidate
    return JournalCandidate(
        journal_date=candidate.journal_date,
        vendor_name=candidate.vendor_name,
        amount=candidate.amount,
        tax_amount=candidate.tax_amount,
        tax_rate=candidate.tax_rate,
        debit_account=decision.debit_account,
        debit_sub_account=decision.debit_sub_account or candidate.debit_sub_account,
        credit_account=candidate.credit_account,
        credit_sub_account=candidate.credit_sub_account,
        tax_category=candidate.tax_category,
        description=candidate.description,
        invoice_number=candidate.invoice_number,
        trade_partner_code=candidate.trade_partner_code,
        trade_partner_name=candidate.trade_partner_name,
        source_type=candidate.source_type,
        risk_flags=candidate.risk_flags,
    )


def decide_credit_side(candidate: JournalCandidate, evidence: Evidence | None = None) -> CreditSideDecision:
    decision = folder_credit_decision(evidence)
    if decision is not None:
        return decision
    if is_invoice_workflow(candidate, evidence):
        account = default_credit_account()
        if account and candidate.vendor_name:
            return CreditSideDecision(
                candidate.credit_account or account,
                candidate.credit_sub_account or candidate.vendor_name,
                "invoice_candidate_rag_credit_sub_account" if candidate.credit_sub_account else "invoice_counterparty",
            )
    raise ValueError("credit route is not configured for the current company")


def source_folder_context(evidence: Evidence | None = None) -> str:
    """Return all locally available source hierarchy, not only the immediate parent."""
    if evidence is None:
        return ""
    return "\n".join(
        value
        for value in (str(evidence.parent_folder_name or ""), str(evidence.local_file_path or ""))
        if value
    )


def source_folder_components(evidence: Evidence | None = None) -> tuple[str, ...]:
    """Return folder components only, excluding arbitrary evidence text/file names."""
    if evidence is None:
        return ()
    components: list[str] = []
    parent_name = str(evidence.parent_folder_name or "")
    components.extend(part.strip() for part in parent_name.replace("\\", "/").split("/") if part.strip())
    local_path = str(evidence.local_file_path or "")
    if local_path:
        local_parts = [part.strip() for part in local_path.replace("\\", "/").split("/") if part.strip()]
        # Evidence paths end in a file name.  The owner must come from an actual
        # directory component, never from the PDF name or its OCR/body text.
        components.extend(local_parts[:-1])
    return tuple(dict.fromkeys(components))


def folder_credit_decision(evidence: Evidence | None = None) -> CreditSideDecision | None:
    if evidence is None:
        return None
    require_source_route(evidence)
    rules = company_policies("accounting_rules.folder_credit_rules")
    matches = [rule for rule in rules if rule.get("folder_id") == evidence.parent_folder_id]
    if len(matches) > 1:
        raise ValueError("ambiguous credit route")
    if not matches:
        return None
    rule = matches[0]
    if not rule.get("credit_account") or not rule.get("credit_sub_account"):
        raise ValueError("credit route account is incomplete")
    return CreditSideDecision(rule["credit_account"], rule["credit_sub_account"], "source_folder_rule")


def _decision_has_route_kind(decision: CreditSideDecision | None, kind: str) -> bool:
    return bool(decision and any(
        row.get("kind") == kind
        and row.get("credit_account") == decision.credit_account
        and row.get("credit_sub_account") == decision.credit_sub_account
        for row in company_policies("accounting_rules.folder_credit_rules")
    ))


def is_personal_payable_decision(decision: CreditSideDecision | None) -> bool:
    return _decision_has_route_kind(decision, "personal_payable")


def is_configured_card_decision(decision: CreditSideDecision | None) -> bool:
    return _decision_has_route_kind(decision, "card")


def is_petty_cash_decision(decision: CreditSideDecision | None) -> bool:
    return _decision_has_route_kind(decision, "petty_cash")


def resolve_credit_side_master(
    conn: sqlite3.Connection,
    candidate: JournalCandidate,
    evidence: Evidence | None = None,
) -> CreditSideDecision:
    direct_decision = folder_credit_decision(evidence)
    if direct_decision is not None:
        exists = credit_sub_account_exists(conn, direct_decision.credit_sub_account)
        return CreditSideDecision(
            direct_decision.credit_account,
            direct_decision.credit_sub_account,
            direct_decision.source,
            sub_account_exists=exists,
            sub_account_creation_required=not exists,
        )
    mapped_decision = resolve_credit_side_from_folder_mapping(conn, evidence)
    if mapped_decision is not None and not is_invoice_workflow(candidate, evidence):
        exists = credit_sub_account_exists(conn, mapped_decision.credit_sub_account)
        return CreditSideDecision(
            mapped_decision.credit_account,
            mapped_decision.credit_sub_account,
            mapped_decision.source,
            sub_account_exists=exists,
            sub_account_creation_required=False,
        )
    decision = decide_credit_side(candidate, evidence)
    if not decision.credit_sub_account:
        return CreditSideDecision(
            decision.credit_account,
            None,
            decision.source,
            sub_account_exists=None,
            sub_account_creation_required=False,
        )
    exists = credit_sub_account_exists(conn, decision.credit_sub_account)
    return CreditSideDecision(
        decision.credit_account,
        decision.credit_sub_account,
        decision.source,
        sub_account_exists=exists,
        sub_account_creation_required=not exists and is_invoice_workflow(candidate, evidence),
    )


def resolve_credit_side_from_folder_mapping(
    conn: sqlite3.Connection,
    evidence: Evidence | None = None,
) -> CreditSideDecision | None:
    if evidence is None or not evidence.parent_folder_id:
        return None
    require_source_route(evidence)
    if not default_credit_account():
        return None
    if not _table_exists(conn, "payable_folder_mappings"):
        return None
    row = conn.execute(
        """
        SELECT google_drive_folder_name
        FROM payable_folder_mappings
        WHERE moneyforward_account_name = ?
          AND google_drive_folder_id_hash = ?
          AND status = 'mapped'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (default_credit_account(), mask_identifier(evidence.parent_folder_id)),
    ).fetchone()
    if row is None:
        return None
    folder_name = str(row["google_drive_folder_name"] or "").strip()
    if not folder_name:
        return None
    return CreditSideDecision(default_credit_account(), folder_name, "payable_folder_mapping")


def credit_sub_account_exists(conn: sqlite3.Connection, name: str | None) -> bool:
    if not name:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM master_items
        WHERE item_type = 'sub_account'
          AND name_normalized = ?
          AND active = 1
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    return row is not None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def drive_move_allowed_for_status(processing_status: str, verification_passed: bool) -> bool:
    return processing_status in {"verified"} and verification_passed


def is_invoice_workflow(candidate: JournalCandidate, evidence: Evidence | None = None) -> bool:
    if candidate.source_type not in INVOICE_SOURCE_TYPES:
        return False
    if evidence is not None:
        require_source_route(evidence)
    return bool(get_setting("accounting_rules.invoice_counterparty_credit", False))
