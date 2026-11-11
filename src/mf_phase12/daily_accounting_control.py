from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .hash_utils import accounting_run_hash, mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path


SOURCE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("sales", "売上請求書・入金消込", "当社発行請求書、発生仕訳、普通預金入金を確認"),
    ("payroll", "給与・賞与", "給与確定、控除、会社負担、支払予定を確認"),
    ("received_evidence", "受領請求書・領収書", "取引先からの請求書と経費証憑を全件manifest化"),
    ("bank", "銀行・口座振替", "普通預金の入出金、光熱費等の引落しを確認"),
    ("card", "クレジットカード", "カード明細を取得し、領収書・請求書との突合対象を確定"),
    ("tax_and_insurance", "税金・社会保険・労働保険", "申告・納付根拠、実出金、未払・預り金消込を確認"),
)
SOURCE_STATES = frozenset({"captured", "zero_confirmed", "not_applicable", "fallback_required", "unavailable"})
READY_SOURCE_STATES = frozenset({"captured", "zero_confirmed", "not_applicable"})
SOURCE_CAPTURE_METHODS: dict[str, frozenset[str]] = {
    "sales": frozenset({"moneyforward_invoice_api", "moneyforward_accounting_readback"}),
    "payroll": frozenset({"moneyforward_payroll_api", "moneyforward_ui_export", "wage_ledger_export", "moneyforward_accounting_readback"}),
    "received_evidence": frozenset({"drive_full_folder_manifest", "gmail_drive_manifest"}),
    "bank": frozenset(
        {
            "moneyforward_ui_tsv",
            "moneyforward_ui_text_copy",
            "moneyforward_accounting_transactions_api",
            "bank_api_export",
            "computer_use",
            "accounting_ui_export",
        }
    ),
    "card": frozenset(
        {
            "moneyforward_ui_tsv",
            "moneyforward_ui_text_copy",
            "moneyforward_accounting_transactions_api",
            "card_statement_export",
            "computer_use",
            "accounting_ui_export",
        }
    ),
    "tax_and_insurance": frozenset({"egov_and_bank_evidence", "moneyforward_accounting_readback", "tax_payment_evidence"}),
}
SOURCE_COVERAGE_FLAGS = {
    "sales": "current_invoice_queue_covered",
    "payroll": "current_payroll_period_covered",
    "received_evidence": "full_intake_manifest_covered",
    "bank": "unposted_queue_covered",
    "card": "unposted_queue_covered",
    "tax_and_insurance": "current_tax_payment_queue_covered",
}
POST_AUDIT_ORDER: tuple[str, ...] = (
    "duplicate_journal_check",
    "debit_credit_amount_check",
    "account_sub_account_tax_check",
    "source_statement_evidence_reconciliation",
    "processed_folder_readback",
)
POST_AUDIT_STATES = frozenset({"verified", "not_required", "not_started", "failed", "unavailable"})
READY_POST_AUDIT_STATES = frozenset({"verified", "not_required"})

# These are balance-sheet accounts that require a human-readable follow-up
# when their closing balance is non-zero.  They are review candidates, not a
# reason to stop unrelated ready journals.
OPENING_BALANCE_REVIEW_ACCOUNTS: dict[str, str] = {
    "前払金": "prepaid",
    "前払費用": "prepaid",
    "未払金": "payable",
    "未払費用": "payable",
    "未払法人税等": "tax_payable",
    "未払消費税": "tax_payable",
    "未払消費税等": "tax_payable",
    "未収入金": "receivable",
    "売掛金": "receivable",
    "前受金": "deferred_income",
    "前受収益": "deferred_income",
    "仮払金": "suspense",
    "立替金": "suspense",
    "仮受金": "suspense",
    "預り金": "withholding",
    "短期借入金": "borrowing",
    "長期借入金": "borrowing",
}


def build_opening_balance_review(
    payload: dict[str, Any] | None,
    *,
    accounting_date: str,
) -> dict[str, Any]:
    """Summarize non-zero control accounts without exposing raw balances."""

    payload = payload if isinstance(payload, dict) else {}
    end = date.fromisoformat(accounting_date)
    start_year = end.year if end.month >= 2 else end.year - 1
    expected_start = date(start_year, 2, 1).isoformat()
    if str(payload.get("start_date") or "") != expected_start:
        raise ValueError("trial balance start_date does not match the fiscal year")
    if str(payload.get("end_date") or "") != accounting_date:
        raise ValueError("trial balance end_date does not match the accounting date")

    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("trial balance response is malformed")
    closing_index = _trial_balance_closing_index(columns)
    found: dict[str, dict[str, Any]] = {}

    def visit(items: list[Any]) -> None:
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if name in OPENING_BALANCE_REVIEW_ACCOUNTS:
                balance = _trial_balance_decimal(raw.get("values"), closing_index)
                if balance != 0:
                    found[name] = {
                        "account_name": name,
                        "control_kind": OPENING_BALANCE_REVIEW_ACCOUNTS[name],
                        "reason_code": "nonzero_closing_balance_requires_review",
                        "balance_state": "negative" if balance < 0 else "positive",
                    }
            children = raw.get("rows")
            if isinstance(children, list):
                visit(children)

    visit(rows)
    exception_items = [found[name] for name in sorted(found)]
    return {
        "status": "verified",
        "source": "moneyforward_trial_balance_bs",
        "review_period": {"start_date": expected_start, "end_date": accounting_date},
        "review_status": "attention_required" if exception_items else "clear",
        "review_item_count": len(exception_items),
        "exception_items": exception_items,
        "workflow_blocking": False,
        "ready_work_continues": True,
        "raw_amounts_stored": False,
        "raw_ids_stored": False,
    }


def _trial_balance_closing_index(columns: list[Any]) -> int:
    for index, raw in enumerate(columns):
        label = str(raw.get("name") if isinstance(raw, dict) else raw or "").lower()
        if "期末残高" in label or "closing" in label or "end_balance" in label:
            return index
    if len(columns) >= 4:
        return 3
    raise ValueError("trial balance closing balance column is missing")


def _trial_balance_decimal(values: Any, index: int) -> Decimal:
    if not isinstance(values, list) or index >= len(values):
        raise ValueError("trial balance row has no closing balance")
    raw = values[index]
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("amount", raw.get("balance")))
    try:
        return Decimal(str(raw or "0").replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("trial balance closing balance is invalid") from exc


@dataclass(frozen=True)
class DailyAccountingControl:
    status: str
    accounting_date: str
    accounting_run_id_hash: str | None
    control_errors: tuple[str, ...]
    source_states: dict[str, str]
    blocking_sources: tuple[str, ...]
    post_audit_states: dict[str, str]
    blocking_post_audits: tuple[str, ...]
    ordered_phases: tuple[str, ...]
    journal_post_allowed: bool
    completion_notification_allowed: bool
    post_audit_required: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounting_date": self.accounting_date,
            "accounting_run_id_hash": self.accounting_run_id_hash,
            "control_errors": list(self.control_errors),
            "source_states": self.source_states,
            "blocking_sources": list(self.blocking_sources),
            "post_audit_states": self.post_audit_states,
            "blocking_post_audits": list(self.blocking_post_audits),
            "ordered_phases": list(self.ordered_phases),
            "journal_post_allowed": self.journal_post_allowed,
            "completion_notification_allowed": self.completion_notification_allowed,
            "post_audit_required": list(self.post_audit_required),
            "source_count": len(SOURCE_ORDER),
            "raw_amounts_stored": False,
            "raw_ids_stored": False,
        }


def daily_accounting_inventory_template() -> dict[str, Any]:
    return {
        "accounting_date": date.today().isoformat(),
        "accounting_run_id": "",
        "sources": {
            key: {
                "state": "fallback_required",
                "reason_code": "source_inventory_not_captured",
            }
            for key, _label, _purpose in SOURCE_ORDER
        },
        "post_audit": {key: "not_started" for key in POST_AUDIT_ORDER},
    }


def build_daily_accounting_control(payload: dict[str, Any] | None) -> DailyAccountingControl:
    payload = payload if isinstance(payload, dict) else {}
    accounting_date = str(payload.get("accounting_date") or "").strip()
    accounting_run_id = str(payload.get("accounting_run_id") or "").strip()
    control_errors: list[str] = []
    try:
        date.fromisoformat(accounting_date)
    except ValueError:
        control_errors.append("accounting_date_missing_or_invalid")
    if not accounting_run_id:
        control_errors.append("accounting_run_id_missing")
    accounting_run_id_hash = (
        "sha256:" + accounting_run_hash(accounting_run_id)
        if accounting_run_id
        else None
    )
    sources = payload.get("sources")
    sources = sources if isinstance(sources, dict) else {}
    post_audit = payload.get("post_audit")
    post_audit = post_audit if isinstance(post_audit, dict) else {}
    source_states: dict[str, str] = {}
    blocking: list[str] = []
    for source_key, _label, _purpose in SOURCE_ORDER:
        raw = sources.get(source_key)
        state = str(raw.get("state") if isinstance(raw, dict) else raw or "fallback_required").strip()
        if state not in SOURCE_STATES:
            state = "unavailable"
        source_states[source_key] = state
        if state not in READY_SOURCE_STATES:
            blocking.append(source_key)
        elif isinstance(raw, dict):
            evidence_errors = _validate_source_evidence(
                source_key=source_key,
                state=state,
                source_payload=raw,
                accounting_date=accounting_date,
            )
            if evidence_errors:
                control_errors.extend(evidence_errors)
                blocking.append(source_key)
        else:
            control_errors.append(f"{source_key}_evidence_missing")
            blocking.append(source_key)
    blocking = list(dict.fromkeys(blocking))
    journal_post_allowed = not blocking and not control_errors
    post_audit_states: dict[str, str] = {}
    blocking_post_audits: list[str] = []
    for audit_key in POST_AUDIT_ORDER:
        state = str(post_audit.get(audit_key) or "not_started").strip()
        raw_audit = post_audit.get(audit_key)
        if isinstance(raw_audit, dict):
            state = str(raw_audit.get("state") or "not_started").strip()
        if state not in POST_AUDIT_STATES:
            state = "unavailable"
        post_audit_states[audit_key] = state
        if state not in READY_POST_AUDIT_STATES:
            blocking_post_audits.append(audit_key)
        elif not isinstance(raw_audit, dict):
            control_errors.append(f"{audit_key}_evidence_missing")
            blocking_post_audits.append(audit_key)
        else:
            audit_errors = _validate_post_audit_evidence(
                audit_key=audit_key,
                state=state,
                audit_payload=raw_audit,
                accounting_date=accounting_date,
            )
            if audit_errors:
                control_errors.extend(audit_errors)
                blocking_post_audits.append(audit_key)
    completion_notification_allowed = journal_post_allowed and not blocking_post_audits
    if not journal_post_allowed:
        status = "source_inventory_incomplete"
    elif not completion_notification_allowed:
        status = "ready_for_reconciliation"
    else:
        status = "ready_for_completion_notification"
    return DailyAccountingControl(
        status=status,
        accounting_date=accounting_date,
        accounting_run_id_hash=accounting_run_id_hash,
        control_errors=tuple(control_errors),
        source_states=source_states,
        blocking_sources=tuple(blocking),
        post_audit_states=post_audit_states,
        blocking_post_audits=tuple(blocking_post_audits),
        ordered_phases=(
            "source_inventory",
            "sales_and_payroll_accrual_review",
            "evidence_and_financial_statement_reconciliation",
            "tax_and_insurance_settlement_review",
            "journal_post_and_readback",
            "voucher_move_and_readback",
            "cross_source_post_audit",
            "single_batch_completion_notification",
            "learning_and_issue_update",
        ),
        journal_post_allowed=journal_post_allowed,
        completion_notification_allowed=completion_notification_allowed,
        post_audit_required=POST_AUDIT_ORDER,
    )


def require_daily_journal_post_allowed(payload: dict[str, Any] | None) -> DailyAccountingControl:
    control = build_daily_accounting_control(payload)
    if not control.journal_post_allowed:
        reasons = ", ".join((*control.control_errors, *control.blocking_sources))
        raise SystemExit(f"journal POST blocked: daily source inventory is incomplete ({reasons})")
    return control


def require_daily_completion_notification_allowed(payload: dict[str, Any]) -> DailyAccountingControl:
    raw_control = payload.get("daily_accounting_control")
    if not isinstance(raw_control, dict) and payload.get("daily_accounting_control_path"):
        control_path = require_ignored_untracked_data_path(
            Path(str(payload["daily_accounting_control_path"])),
            purpose="Google Chat daily accounting control input",
        )
        try:
            loaded_control = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit("completion notification daily_accounting_control_path is unreadable") from exc
        raw_control = loaded_control if isinstance(loaded_control, dict) else None
    if not isinstance(raw_control, dict):
        raise SystemExit(
            "completion notification requires daily_accounting_control "
            "or daily_accounting_control_path with source and post-audit evidence"
        )
    if not str(payload.get("accounting_run_id") or "").strip():
        raise SystemExit("completion notification requires accounting_run_id for batch-level duplicate control")
    if str(raw_control.get("accounting_run_id") or "").strip() != str(payload["accounting_run_id"]).strip():
        raise SystemExit("completion notification accounting_run_id must match daily_accounting_control")
    control = build_daily_accounting_control(raw_control)
    if not control.journal_post_allowed:
        reasons = ", ".join((*control.control_errors, *control.blocking_sources))
        raise SystemExit(f"completion notification blocked: daily source inventory is incomplete ({reasons})")
    if not control.completion_notification_allowed:
        audits = ", ".join(control.blocking_post_audits)
        raise SystemExit(f"completion notification blocked: cross-source post audit is incomplete ({audits})")
    return control


def build_daily_accounting_control_from_file(*, inventory_json: Path, output_json: Path) -> DailyAccountingControl:
    inventory_json = require_ignored_untracked_data_path(inventory_json, purpose="daily accounting source inventory")
    output_json = require_ignored_untracked_data_path(output_json, purpose="daily accounting control output")
    payload = json.loads(inventory_json.read_text(encoding="utf-8"))
    control = build_daily_accounting_control(payload if isinstance(payload, dict) else None)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(control.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return control


def _validate_source_evidence(
    *,
    source_key: str,
    state: str,
    source_payload: dict[str, Any],
    accounting_date: str,
) -> list[str]:
    evidence = source_payload.get("evidence")
    if not isinstance(evidence, dict):
        return [f"{source_key}_evidence_missing"]
    errors: list[str] = []
    method = str(evidence.get("method") or "").strip()
    if method not in SOURCE_CAPTURE_METHODS[source_key]:
        errors.append(f"{source_key}_capture_method_invalid")
    coverage = str(evidence.get("coverage") or "").strip()
    expected_coverage = {
        "captured": "full_current_queue",
        "zero_confirmed": "verified_empty",
        "not_applicable": "documented_not_applicable",
    }[state]
    if coverage != expected_coverage:
        errors.append(f"{source_key}_coverage_invalid")
    if _artifact_local_date(evidence.get("captured_at")) != accounting_date:
        errors.append(f"{source_key}_capture_not_current_date")
    try:
        item_count = int(evidence.get("item_count"))
    except (TypeError, ValueError):
        item_count = -1
    if item_count < 0 or state in {"zero_confirmed", "not_applicable"} and item_count != 0:
        errors.append(f"{source_key}_item_count_invalid")
    coverage_flag = SOURCE_COVERAGE_FLAGS[source_key]
    if state != "not_applicable" and evidence.get(coverage_flag) is not True:
        errors.append(f"{source_key}_{coverage_flag}_missing")
    if state == "not_applicable" and not str(evidence.get("not_applicable_basis") or "").strip():
        errors.append(f"{source_key}_not_applicable_basis_missing")
    artifact_path_value = str(evidence.get("artifact_path") or "").strip()
    artifact_hash = str(evidence.get("artifact_hash") or "").strip()
    if not artifact_path_value or not _is_sha256_marker(artifact_hash):
        errors.append(f"{source_key}_artifact_reference_invalid")
        return errors
    try:
        artifact_path = require_ignored_untracked_data_path(
            Path(artifact_path_value), purpose=f"{source_key} daily source evidence"
        )
        if not artifact_path.is_file():
            raise FileNotFoundError(artifact_path)
        artifact_bytes = artifact_path.read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
        if actual_hash != artifact_hash:
            errors.append(f"{source_key}_artifact_hash_mismatch")
        else:
            try:
                artifact_payload = json.loads(artifact_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append(f"{source_key}_artifact_json_invalid")
            else:
                errors.extend(
                    _validate_source_artifact_semantics(
                        source_key=source_key,
                        state=state,
                        method=method,
                        item_count=item_count,
                        accounting_date=accounting_date,
                        artifact_payload=artifact_payload,
                    )
                )
    except BaseException:
        errors.append(f"{source_key}_artifact_unavailable")
    return errors


def _validate_source_artifact_semantics(
    *,
    source_key: str,
    state: str,
    method: str,
    item_count: int,
    accounting_date: str,
    artifact_payload: Any,
) -> list[str]:
    if not isinstance(artifact_payload, dict):
        return [f"{source_key}_artifact_schema_invalid"]
    if method == "moneyforward_invoice_api" and source_key == "sales":
        return _validate_invoice_api_artifact(
            state=state,
            item_count=item_count,
            accounting_date=accounting_date,
            payload=artifact_payload,
        )
    if method == "drive_full_folder_manifest" and source_key == "received_evidence":
        return _validate_drive_manifest_artifact(
            state=state,
            item_count=item_count,
            accounting_date=accounting_date,
            payload=artifact_payload,
        )
    if method == "moneyforward_ui_text_copy" and source_key in {"bank", "card"}:
        return _validate_moneyforward_ui_artifact(
            source_key=source_key,
            state=state,
            item_count=item_count,
            accounting_date=accounting_date,
            payload=artifact_payload,
        )
    if method == "moneyforward_accounting_transactions_api" and source_key in {"bank", "card"}:
        return _validate_moneyforward_transactions_api_artifact(
            source_key=source_key,
            state=state,
            item_count=item_count,
            accounting_date=accounting_date,
            payload=artifact_payload,
        )
    if method == "moneyforward_accounting_readback" and source_key in {"payroll", "tax_and_insurance"}:
        return _validate_accounting_readback_artifact(
            source_key=source_key,
            state=state,
            item_count=item_count,
            accounting_date=accounting_date,
            payload=artifact_payload,
        )
    return []


def _validate_invoice_api_artifact(
    *, state: str, item_count: int, accounting_date: str, payload: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    matches = payload.get("matches")
    summary = payload.get("summary")
    if not isinstance(matches, list) or not isinstance(summary, dict):
        return ["sales_artifact_schema_invalid"]
    if len(matches) != item_count or summary.get("invoice_billing_count") != item_count:
        errors.append("sales_artifact_item_count_mismatch")
    if str(summary.get("end_date") or "") != accounting_date:
        errors.append("sales_artifact_not_current_date")
    if state == "captured" and any(
        not isinstance(row, dict)
        or row.get("matched") is not True
        or int(row.get("sales_accrual_match_count") or 0) < 1
        for row in matches
    ):
        errors.append("sales_artifact_accrual_readback_incomplete")
    return errors


def _validate_drive_manifest_artifact(
    *, state: str, item_count: int, accounting_date: str, payload: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    source = payload.get("source")
    files = payload.get("files")
    if not isinstance(source, dict) or not isinstance(files, list):
        return ["received_evidence_artifact_schema_invalid"]
    if source.get("full_intake_manifest_covered") is not True:
        errors.append("received_evidence_artifact_full_coverage_missing")
    if source.get("file_count") != item_count or len(files) != item_count:
        errors.append("received_evidence_artifact_item_count_mismatch")
    if _artifact_local_date(source.get("captured_at")) != accounting_date:
        errors.append("received_evidence_artifact_not_current_date")
    if any(
        not isinstance(row, dict)
        or not _is_sha256_marker(str(row.get("file_hash") or ""))
        or not str(row.get("file_name") or "").strip()
        or not str(row.get("google_drive_file_id") or "").strip()
        or not str(row.get("parent_folder_id") or "").strip()
        for row in files
    ):
        errors.append("received_evidence_artifact_file_identity_incomplete")
    if state == "zero_confirmed" and files:
        errors.append("received_evidence_artifact_not_empty")
    return errors


def _validate_moneyforward_ui_artifact(
    *,
    source_key: str,
    state: str,
    item_count: int,
    accounting_date: str,
    payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    capture = payload.get("capture")
    transactions = payload.get("transactions")
    if not isinstance(capture, dict) or not isinstance(transactions, list):
        return [f"{source_key}_artifact_schema_invalid"]
    if capture.get("full_unposted_queue_covered") is not True or capture.get("service_filter") != "all":
        errors.append(f"{source_key}_artifact_full_coverage_missing")
    if capture.get("transaction_count") != len(transactions):
        errors.append(f"{source_key}_artifact_total_count_mismatch")
    if capture.get("source_types_fully_classified") is not True:
        errors.append(f"{source_key}_artifact_source_classification_incomplete")
    source_counts = capture.get("source_type_counts")
    if not isinstance(source_counts, dict):
        errors.append(f"{source_key}_artifact_source_counts_missing")
        relevant_count = -1
    elif source_key == "bank":
        relevant_count = int(source_counts.get("bank") or 0)
    else:
        relevant_count = int(source_counts.get("card") or 0) + int(source_counts.get("supplier_primary") or 0)
    if relevant_count != item_count:
        errors.append(f"{source_key}_artifact_item_count_mismatch")
    if _artifact_local_date(capture.get("captured_at")) != accounting_date:
        errors.append(f"{source_key}_artifact_not_current_date")
    if state == "zero_confirmed" and relevant_count != 0:
        errors.append(f"{source_key}_artifact_not_empty")
    return errors


def _validate_moneyforward_transactions_api_artifact(
    *,
    source_key: str,
    state: str,
    item_count: int,
    accounting_date: str,
    payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    transactions = payload.get("transactions")
    if payload.get("schema") != "moneyforward-connected-transactions-v1" or not isinstance(transactions, list):
        return [f"{source_key}_artifact_schema_invalid"]
    if payload.get("source") != "GET /api/v3/transactions":
        errors.append(f"{source_key}_artifact_source_invalid")
    if payload.get("journalizing_statuses") != ["none"] or any(
        not isinstance(row, dict) or row.get("journalizing_status") != "none" for row in transactions
    ):
        errors.append(f"{source_key}_artifact_status_invalid")
    if payload.get("raw_api_response_stored") is not False:
        errors.append(f"{source_key}_artifact_raw_api_response_invalid")
    if not _is_sha256_hex(str(payload.get("source_response_hash") or "")):
        errors.append(f"{source_key}_artifact_response_identity_incomplete")

    try:
        current_date = date.fromisoformat(accounting_date)
        range_covered = (
            date.fromisoformat(str(payload.get("start_date") or ""))
            <= current_date
            <= date.fromisoformat(str(payload.get("end_date") or ""))
        )
    except ValueError:
        range_covered = False
    if not range_covered or _artifact_local_date(payload.get("captured_at")) != accounting_date:
        errors.append(f"{source_key}_artifact_not_current_date")

    source_counts = {"bank": 0, "card": 0}
    identity_incomplete = False
    classification_incomplete = False
    transaction_ids: set[str] = set()
    for row in transactions:
        if not isinstance(row, dict):
            identity_incomplete = True
            classification_incomplete = True
            continue
        identity_values = [str(row.get(key) or "").strip() for key in ("id", "source_event_id", "transaction_id")]
        transaction_id = identity_values[0]
        connected_account_id = str(row.get("connected_account_id") or "").strip()
        connected_sub_account_id = str(row.get("connected_sub_account_id") or "").strip()
        ledger_account_id = str(row.get("connected_ledger_account_id") or "").strip()
        ledger_sub_account_id = str(row.get("connected_ledger_sub_account_id") or "").strip()
        if (
            not transaction_id
            or len(set(identity_values)) != 1
            or transaction_id in transaction_ids
            or row.get("source_api") != "GET /api/v3/transactions"
            or not str(row.get("bank_or_card_account") or "").strip()
            or not connected_account_id
            or not ledger_account_id
            or row.get("source_event_id_hash") != mask_identifier(transaction_id)
            or row.get("connected_account_binding_hash")
            != mask_identifier(f"{connected_account_id}:{connected_sub_account_id}")
            or row.get("connected_ledger_binding_hash")
            != mask_identifier(
                f"{connected_account_id}:{connected_sub_account_id}:{ledger_account_id}:{ledger_sub_account_id}"
            )
            or not _is_sha256_hex(str(row.get("source_row_hash") or ""))
            or not _is_sha256_hex(str(row.get("transaction_facts_hash") or ""))
        ):
            identity_incomplete = True
        else:
            transaction_ids.add(transaction_id)

        try:
            transaction_date = date.fromisoformat(str(row.get("transaction_date") or ""))
            transaction_in_range = (
                date.fromisoformat(str(payload.get("start_date") or ""))
                <= transaction_date
                <= date.fromisoformat(str(payload.get("end_date") or ""))
            )
        except ValueError:
            transaction_in_range = False
        if not transaction_in_range:
            identity_incomplete = True

        source_type = _official_transaction_source_type(row)
        if source_type is None:
            classification_incomplete = True
        else:
            source_counts[source_type] += 1

    if identity_incomplete:
        errors.append(f"{source_key}_artifact_transaction_identity_incomplete")
    if classification_incomplete:
        errors.append(f"{source_key}_artifact_source_classification_incomplete")
    relevant_count = source_counts[source_key]
    if relevant_count != item_count:
        errors.append(f"{source_key}_artifact_item_count_mismatch")
    if state == "zero_confirmed" and relevant_count != 0:
        errors.append(f"{source_key}_artifact_not_empty")
    return errors


def _official_transaction_source_type(row: dict[str, Any]) -> str | None:
    account_name = str(row.get("bank_or_card_account") or "").strip().casefold()
    explicit_type = str(row.get("source_event_type") or "").strip()
    if not account_name or explicit_type not in {"bank_transaction", "card_transaction"}:
        return None
    account_type = (
        "card"
        if any(marker in account_name for marker in ("card", "カード", "amex", "visa", "master", "jcb"))
        else "bank"
    )
    expected_type = f"{account_type}_transaction"
    return account_type if explicit_type == expected_type else None


def _validate_accounting_readback_artifact(
    *,
    source_key: str,
    state: str,
    item_count: int,
    accounting_date: str,
    payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    categories = payload.get("categories")
    category = categories.get(source_key) if isinstance(categories, dict) else None
    if payload.get("posted_journal_readback_covered") is not True or not isinstance(category, dict):
        return [f"{source_key}_artifact_readback_incomplete"]
    if category.get("posted_journal_count") != item_count:
        errors.append(f"{source_key}_artifact_item_count_mismatch")
    try:
        range_covered = (
            date.fromisoformat(str(payload.get("start_date")))
            <= date.fromisoformat(accounting_date)
            <= date.fromisoformat(str(payload.get("end_date")))
        )
    except ValueError:
        range_covered = False
    if not range_covered or _artifact_local_date(payload.get("checked_at")) != accounting_date:
        errors.append(f"{source_key}_artifact_not_current_date")
    if state == "zero_confirmed" and item_count != 0:
        errors.append(f"{source_key}_artifact_not_empty")
    return errors


def _artifact_local_date(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo("Asia/Tokyo"))
        return parsed.date().isoformat()
    except ValueError:
        return ""


def _is_sha256_marker(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest.lower())


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def _validate_post_audit_evidence(
    *,
    audit_key: str,
    state: str,
    audit_payload: dict[str, Any],
    accounting_date: str,
) -> list[str]:
    evidence = audit_payload.get("evidence")
    if not isinstance(evidence, dict):
        return [f"{audit_key}_evidence_missing"]
    artifact_path_value = str(evidence.get("artifact_path") or "").strip()
    artifact_hash = str(evidence.get("artifact_hash") or "").strip()
    result_hash = str(evidence.get("result_hash") or "").strip()
    if not artifact_path_value or not _is_sha256_marker(artifact_hash) or not _is_sha256_marker(result_hash):
        return [f"{audit_key}_evidence_reference_invalid"]
    try:
        artifact_path = require_ignored_untracked_data_path(
            Path(artifact_path_value), purpose=f"{audit_key} post-audit evidence"
        )
        artifact_bytes = artifact_path.read_bytes()
    except BaseException:
        return [f"{audit_key}_evidence_unavailable"]
    actual_artifact_hash = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    if actual_artifact_hash != artifact_hash:
        return [f"{audit_key}_evidence_hash_mismatch"]
    try:
        artifact = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [f"{audit_key}_evidence_json_invalid"]
    if not isinstance(artifact, dict) or str(artifact.get("accounting_date") or "") != accounting_date:
        return [f"{audit_key}_evidence_date_mismatch"]
    results = artifact.get("results")
    result = results.get(audit_key) if isinstance(results, dict) else None
    if not isinstance(result, dict) or str(result.get("state") or "") != state:
        return [f"{audit_key}_evidence_state_mismatch"]
    expected_result_hash = "sha256:" + stable_hash(
        {key: value for key, value in result.items() if key != "result_hash"}
    )
    if result_hash != expected_result_hash or str(result.get("result_hash") or "") != expected_result_hash:
        return [f"{audit_key}_result_hash_mismatch"]
    if state == "verified" and result.get("passed") is not True:
        return [f"{audit_key}_evidence_not_passed"]
    return []
