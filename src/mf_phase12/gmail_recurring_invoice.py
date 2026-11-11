from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .office_profile import get_current_profile, get_setting
from .drive_policy import company_policies


def accounting_execution_principal() -> str:
    principal = get_current_profile().google_account
    if not principal:
        raise ValueError("accounting execution principal is not configured")
    return principal


def evidence_source_accounts() -> tuple[str, ...]:
    values = get_setting("recurring_invoices.evidence_source_accounts", [])
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("invoice evidence source accounts are not configured")
    accounts = tuple(str(value).strip().casefold() for value in values)
    if any(not account for account in accounts) or len(set(accounts)) != len(accounts):
        raise ValueError("invoice evidence source accounts are invalid")
    return accounts


def recurring_vendor_queries() -> tuple[dict, ...]:
    rows = company_policies("recurring_invoices.vendor_queries")
    for row in rows:
        if any(not row.get(key) for key in ("vendor_key", "display_name", "query", "terms")):
            raise ValueError("recurring vendor query is incomplete")
    return rows


def recurring_invoice_routes() -> tuple[dict, ...]:
    rows = company_policies("recurring_invoices.routes")
    for row in rows:
        if not row.get("vendor_key") or not row.get("route") or not row.get("aliases"):
            raise ValueError("recurring invoice route is incomplete")
        if row.get("source_account") and row["source_account"] not in evidence_source_accounts():
            raise ValueError("recurring invoice route mailbox is not configured")
    return rows


@dataclass(frozen=True)
class RecurringInvoiceExpectation:
    vendor_key: str
    vendor_name: str
    acquisition_route: str
    historical_count: int
    last_seen_at: str | None
    observed_this_month: bool
    lifecycle_status: str = "active"
    acquisition_channel: str = "unknown"
    acquisition_method: str = "unknown"

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "vendor_key": self.vendor_key,
            "vendor_name_hash": mask_identifier(self.vendor_name),
            "acquisition_route": self.acquisition_route,
            "historical_count": self.historical_count,
            "last_seen_at": self.last_seen_at,
            "observed_this_month": self.observed_this_month,
            "lifecycle_status": self.lifecycle_status,
            "acquisition_channel": self.acquisition_channel,
            "acquisition_method": self.acquisition_method,
            "status": "observed" if self.observed_this_month else "missing_expected_invoice",
        }


@dataclass(frozen=True)
class RecurringInvoiceLifecycle:
    vendor_key: str
    lifecycle_status: str
    acquisition_channel: str
    acquisition_method: str
    consecutive_seen: int
    consecutive_missing: int
    last_seen_period: str | None
    evaluated_period: str


def refresh_recurring_invoice_profiles(
    conn: sqlite3.Connection,
    *,
    accounting_date: str,
) -> tuple[RecurringInvoiceLifecycle, ...]:
    """Refresh deterministic monthly lifecycle state from authoritative journal history.

    The current partial month is never counted as a missed month.  Two
    consecutive completed months register a recurring candidate; a mapped
    email/paper source makes it active.  One/two/three missed completed months
    move active vendors to watch/dormant/archived.  A returning vendor must
    again appear in two consecutive months before becoming active.
    """

    target = date.fromisoformat(accounting_date)
    evaluated = _shift_month(target.replace(day=1), -1)
    evaluated_period = evaluated.strftime("%Y-%m")
    rows = conn.execute(
        """
        SELECT p.vendor_key, p.description_key, substr(o.transaction_date, 1, 7) AS period
        FROM journal_pattern_occurrences AS o
        JOIN journal_patterns AS p ON p.pattern_hash = o.pattern_hash
        WHERE o.transaction_date IS NOT NULL
          AND substr(o.transaction_date, 1, 7) <= ?
        """,
        (evaluated_period,),
    ).fetchall()
    months_by_vendor: dict[str, set[str]] = {}
    display_names: dict[str, str] = {}
    for row in rows:
        raw_vendor = str(row["vendor_key"])
        route = _recurring_route(f"{raw_vendor} {row['description_key']}")
        if raw_vendor in {"", "unknown_vendor"} and route is None:
            continue
        vendor_key = str(route["vendor_key"]) if route else f"rag:{stable_hash(raw_vendor)[:16]}"
        months_by_vendor.setdefault(vendor_key, set()).add(str(row["period"]))
        display_names.setdefault(vendor_key, raw_vendor)

    existing = {
        str(row["vendor_key"]): dict(row)
        for row in conn.execute(
            """
            SELECT vendor_key, invoice_lifecycle_status, invoice_acquisition_channel,
                   invoice_acquisition_method, invoice_source_account_hash
            FROM vendor_profiles
            WHERE invoice_lifecycle_status <> 'untracked'
            """
        ).fetchall()
    }
    results: list[RecurringInvoiceLifecycle] = []
    for vendor_key in sorted(set(months_by_vendor) | set(existing)):
        months = months_by_vendor.get(vendor_key, set())
        if not months:
            continue
        latest_period = max(months)
        consecutive_seen = _consecutive_months_ending_at(months, latest_period)
        missing = max(0, _month_distance(latest_period, evaluated_period))
        route = next((item for item in recurring_invoice_routes() if item["vendor_key"] == vendor_key), None)
        prior = existing.get(vendor_key, {})
        channel = str((route or {}).get("channel") or prior.get("invoice_acquisition_channel") or "unknown")
        method = str((route or {}).get("method") or prior.get("invoice_acquisition_method") or "unknown")
        source_account = (route or {}).get("source_account")
        source_hash = mask_identifier(str(source_account)) if source_account else prior.get("invoice_source_account_hash")
        was_tracked = str(prior.get("invoice_lifecycle_status") or "untracked") not in {"untracked", "candidate", "archived"}
        mapped = channel in {"email", "paper"} and method != "unknown"
        if missing >= 3:
            lifecycle = "archived"
        elif missing == 2:
            lifecycle = "dormant" if mapped or was_tracked else "candidate"
        elif missing == 1:
            lifecycle = "watch" if mapped or was_tracked else "candidate"
        elif consecutive_seen >= 2:
            lifecycle = "active" if mapped else "candidate"
        else:
            lifecycle = "candidate"
        conn.execute(
            """
            INSERT INTO vendor_profiles (
                vendor_key, display_name_masked, confidence_score,
                invoice_acquisition_channel, invoice_acquisition_method,
                invoice_source_account_hash, invoice_cadence,
                invoice_lifecycle_status, invoice_consecutive_seen,
                invoice_consecutive_missing, invoice_last_seen_period,
                invoice_last_evaluated_period
            ) VALUES (?, ?, 0.0, ?, ?, ?, 'monthly', ?, ?, ?, ?, ?)
            ON CONFLICT(vendor_key) DO UPDATE SET
                invoice_acquisition_channel = excluded.invoice_acquisition_channel,
                invoice_acquisition_method = excluded.invoice_acquisition_method,
                invoice_source_account_hash = excluded.invoice_source_account_hash,
                invoice_cadence = excluded.invoice_cadence,
                invoice_lifecycle_status = excluded.invoice_lifecycle_status,
                invoice_consecutive_seen = excluded.invoice_consecutive_seen,
                invoice_consecutive_missing = excluded.invoice_consecutive_missing,
                invoice_last_seen_period = excluded.invoice_last_seen_period,
                invoice_last_evaluated_period = excluded.invoice_last_evaluated_period,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                vendor_key,
                mask_identifier(display_names.get(vendor_key, vendor_key)),
                channel,
                method,
                source_hash,
                lifecycle,
                consecutive_seen,
                missing,
                latest_period,
                evaluated_period,
            ),
        )
        results.append(
            RecurringInvoiceLifecycle(
                vendor_key=vendor_key,
                lifecycle_status=lifecycle,
                acquisition_channel=channel,
                acquisition_method=method,
                consecutive_seen=consecutive_seen,
                consecutive_missing=missing,
                last_seen_period=latest_period,
                evaluated_period=evaluated_period,
            )
        )
    conn.commit()
    return tuple(results)


def record_recurring_invoice_acquisition(
    conn: sqlite3.Connection,
    *,
    vendor_key: str,
    period: str,
    evidence_hash: str,
) -> None:
    """Record a redacted GAS or paper receipt without creating another ledger."""

    if len(period) != 7 or period[4] != "-":
        raise ValueError("period must be YYYY-MM")
    normalized_hash = evidence_hash.removeprefix("sha256:")
    if len(normalized_hash) != 64 or any(character not in "0123456789abcdef" for character in normalized_hash):
        raise ValueError("evidence_hash must be a sha256 hash")
    updated = conn.execute(
        """
        UPDATE vendor_profiles
        SET invoice_last_acquired_period = ?,
            invoice_last_acquisition_evidence_hash = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE vendor_key = ?
          AND invoice_lifecycle_status IN ('active', 'watch')
        """,
        (period, evidence_hash, vendor_key),
    )
    if updated.rowcount != 1:
        raise ValueError("recurring invoice vendor is not active")
    conn.commit()


def build_recurring_invoice_expectations(
    conn: sqlite3.Connection,
    *,
    accounting_date: str,
    minimum_history_count: int = 2,
) -> tuple[RecurringInvoiceExpectation, ...]:
    """Build one monthly expectation list from the existing office RAG.

    Money Forward journal history supplies recurrence.  Only a GAS/Drive or
    paper-scan receipt supplies current-month acquisition.  No API or browser
    call is made here.
    """

    del minimum_history_count  # Two consecutive completed months is the fixed rule.
    refresh_recurring_invoice_profiles(conn, accounting_date=accounting_date)
    month_prefix = date.fromisoformat(accounting_date).strftime("%Y-%m")
    rows = conn.execute(
        """
        SELECT vendor_key, invoice_acquisition_channel, invoice_acquisition_method,
               invoice_lifecycle_status, invoice_consecutive_seen,
               invoice_last_seen_period, invoice_last_acquired_period
        FROM vendor_profiles
        WHERE invoice_cadence = 'monthly'
          AND invoice_lifecycle_status IN ('active', 'watch')
        ORDER BY vendor_key
        """,
    ).fetchall()
    expectations: list[RecurringInvoiceExpectation] = []
    for row in rows:
        vendor_key = str(row["vendor_key"])
        route = next((item for item in recurring_invoice_routes() if item["vendor_key"] == vendor_key), None)
        vendor_name = str((route or {}).get("aliases", (vendor_key,))[0])
        acquisition_route = str((route or {}).get("route") or "source_mapping_required")
        aliases = _route_aliases(vendor_key, vendor_name)
        observed = str(row["invoice_last_acquired_period"] or "") == month_prefix or _invoice_observed_this_month(
            conn,
            month_prefix=month_prefix,
            aliases=aliases,
        )
        expectations.append(
            RecurringInvoiceExpectation(
                vendor_key=vendor_key,
                vendor_name=vendor_name,
                acquisition_route=acquisition_route,
                historical_count=int(row["invoice_consecutive_seen"]),
                last_seen_at=str(row["invoice_last_seen_period"] or "") or None,
                observed_this_month=observed,
                lifecycle_status=str(row["invoice_lifecycle_status"]),
                acquisition_channel=str(row["invoice_acquisition_channel"]),
                acquisition_method=str(row["invoice_acquisition_method"]),
            )
        )
    return tuple(sorted(expectations, key=lambda item: item.vendor_key))


def _recurring_route(vendor_name: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", vendor_name).casefold()
    return next(
        (
            route
            for route in recurring_invoice_routes()
            if any(unicodedata.normalize("NFKC", str(alias)).casefold() in normalized for alias in route["aliases"])
        ),
        None,
    )


def _route_aliases(vendor_key: str, vendor_name: str) -> tuple[str, ...]:
    route = next((item for item in recurring_invoice_routes() if item["vendor_key"] == vendor_key), None)
    return (
        tuple(unicodedata.normalize("NFKC", str(alias)).casefold() for alias in route["aliases"])
        if route
        else (unicodedata.normalize("NFKC", vendor_name).casefold(),)
    )


def _shift_month(value: date, delta: int) -> date:
    month_index = value.year * 12 + value.month - 1 + delta
    return date(month_index // 12, month_index % 12 + 1, 1)


def _month_distance(start_period: str, end_period: str) -> int:
    start = date.fromisoformat(f"{start_period}-01")
    end = date.fromisoformat(f"{end_period}-01")
    return (end.year - start.year) * 12 + end.month - start.month


def _consecutive_months_ending_at(months: set[str], last_period: str) -> int:
    current = date.fromisoformat(f"{last_period}-01")
    count = 0
    while current.strftime("%Y-%m") in months:
        count += 1
        current = _shift_month(current, -1)
    return count


def _invoice_observed_this_month(
    conn: sqlite3.Connection,
    *,
    month_prefix: str,
    aliases: tuple[str, ...],
) -> bool:
    # Money Forward registration proves accounting, not invoice acquisition.
    # Current-month acquisition must come from a GAS/Drive or paper-scan receipt.
    ledger_rows = conn.execute(
        """
        SELECT file_name FROM processing_ledger
        WHERE substr(created_at, 1, 7) = ?
        """,
        (month_prefix,),
    ).fetchall()
    return any(
        any(
            alias in unicodedata.normalize("NFKC", str(row["file_name"])).casefold()
            for alias in aliases
        )
        for row in ledger_rows
    )


@dataclass(frozen=True)
class GmailRecurringInvoiceSearchPlan:
    start_date: str
    accounts: tuple[str, ...]
    search_items: tuple[dict[str, Any], ...]

    def to_redacted_dict(self) -> dict[str, Any]:
        evidence_source_account_hashes = [mask_identifier(account) for account in self.accounts]
        return {
            "status": "ready",
            "company_slug": get_current_profile().slug,
            "start_date": self.start_date,
            "accounting_execution_principal_hash": mask_identifier(accounting_execution_principal()),
            "account_hashes": evidence_source_account_hashes,
            "evidence_source_account_hashes": evidence_source_account_hashes,
            "account_count": len(self.accounts),
            "search_items": [
                {
                    "vendor_key": item["vendor_key"],
                    "display_name": item["display_name"],
                    "required": item["required"],
                    "gmail_query": item["gmail_query"],
                    "query_hash": item["query_hash"],
                    "account_hash": item["account_hash"],
                }
                for item in self.search_items
            ],
            "gmail_api_called": False,
            "google_drive_write_called": False,
            "raw_account_emails_printed": False,
            "raw_message_ids_printed": False,
            "raw_secret_stored": False,
        }


@dataclass(frozen=True)
class GmailRecurringInvoiceCoverage:
    status: str
    checked_count: int
    missing_required: tuple[str, ...]
    found_counts: dict[str, int]
    missing_query_receipts: tuple[str, ...]
    unprocessed_query_results: tuple[str, ...]
    query_execution: dict[str, dict[str, Any]]

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounting_execution_principal_hash": mask_identifier(
                accounting_execution_principal()
            ),
            "evidence_source_account_hashes": [
                mask_identifier(account) for account in evidence_source_accounts()
            ],
            "checked_count": self.checked_count,
            "missing_required": list(self.missing_required),
            "found_counts": self.found_counts,
            "missing_query_receipts": list(self.missing_query_receipts),
            "unprocessed_query_results": list(self.unprocessed_query_results),
            "query_receipt_count": sum(
                1 for receipt in self.query_execution.values() if receipt["status"] == "completed"
            ),
            "query_execution": self.query_execution,
            "gmail_api_called": False,
            "google_drive_write_called": False,
            "raw_message_ids_printed": False,
            "raw_secret_stored": False,
        }


def build_recurring_invoice_search_plan(
    *,
    start_date: str,
    accounts: tuple[str, ...] | None = None,
) -> GmailRecurringInvoiceSearchPlan:
    if not start_date:
        raise ValueError("start_date is required")
    accounts = _require_evidence_source_accounts(accounts)
    search_items: list[dict[str, Any]] = []
    after = start_date.replace("-", "/")
    queries = recurring_vendor_queries()
    if not queries:
        raise ValueError("recurring invoice search queries are not configured")
    for account in accounts:
        account_hash = mask_identifier(account)
        for vendor in queries:
            search_items.append(
                _with_query_hash(
                    {
                        "account_email": account,
                        "account_hash": account_hash,
                        "vendor_key": vendor["vendor_key"],
                        "display_name": vendor["display_name"],
                        "required": bool(vendor.get("required", False)),
                        "gmail_query": f'after:{after} {vendor["query"]} (invoice OR 請求書 OR receipt OR 領収書 OR 明細 OR has:attachment) -in:trash -in:spam',
                    }
                )
            )
    return GmailRecurringInvoiceSearchPlan(
        start_date=start_date,
        accounts=accounts,
        search_items=tuple(search_items),
    )


def write_recurring_invoice_search_plan(
    *,
    output_json: Path,
    start_date: str,
    accounts: tuple[str, ...] | None = None,
) -> GmailRecurringInvoiceSearchPlan:
    output_json = require_ignored_untracked_data_path(output_json, purpose="Gmail recurring invoice search plan")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    plan = build_recurring_invoice_search_plan(start_date=start_date, accounts=accounts)
    payload = plan.to_redacted_dict()
    raw_items = [
        {
            key: value
            for key, value in item.items()
            if key in {"account_email", "vendor_key", "display_name", "required", "gmail_query", "query_hash"}
        }
        for item in plan.search_items
    ]
    output_json.write_text(
        json.dumps({**payload, "raw_search_items": raw_items}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return plan


def verify_recurring_invoice_coverage(
    *,
    search_plan_json: Path,
    gmail_messages_json: Path,
) -> GmailRecurringInvoiceCoverage:
    search_plan_json = require_ignored_untracked_data_path(search_plan_json, purpose="Gmail recurring invoice search plan")
    gmail_messages_json = require_ignored_untracked_data_path(gmail_messages_json, purpose="Gmail recurring invoice messages")
    plan_payload = json.loads(search_plan_json.read_text(encoding="utf-8"))
    messages_payload = json.loads(gmail_messages_json.read_text(encoding="utf-8"))
    raw_items = plan_payload.get("raw_search_items") if isinstance(plan_payload, dict) else []
    if not isinstance(plan_payload, dict) or not isinstance(raw_items, list):
        raise ValueError("recurring invoice search plan is malformed")
    if plan_payload.get("company_slug") != get_current_profile().slug:
        raise ValueError("recurring invoice plan company binding mismatch")
    expected_plan = build_recurring_invoice_search_plan(
        start_date=str(plan_payload.get("start_date") or ""),
        accounts=None,
    )
    expected = {
        (
            str(item["account_email"]),
            str(item["vendor_key"]),
            bool(item["required"]),
            str(item["gmail_query"]),
            str(item["query_hash"]),
        )
        for item in expected_plan.search_items
    }
    actual = {
        (
            str(item.get("account_email") or ""),
            str(item.get("vendor_key") or ""),
            bool(item.get("required")),
            str(item.get("gmail_query") or ""),
            str(item.get("query_hash") or ""),
        )
        for item in raw_items
        if isinstance(item, dict)
    }
    if len(raw_items) != len(expected) or actual != expected:
        raise ValueError("recurring invoice search plan does not match the current company canonical plan")
    if plan_payload.get("accounting_execution_principal_hash") != mask_identifier(
        accounting_execution_principal()
    ):
        raise ValueError("recurring invoice plan accounting execution principal is not current company")
    expected_account_hashes = [mask_identifier(account) for account in evidence_source_accounts()]
    if (
        plan_payload.get("account_count") != len(evidence_source_accounts())
        or plan_payload.get("account_hashes") != expected_account_hashes
        or plan_payload.get("evidence_source_account_hashes") != expected_account_hashes
    ):
        raise ValueError("recurring invoice plan evidence source coverage is not canonical")
    messages = _extract_messages(messages_payload)
    message_manifest = [
        (message, _resolve_evidence_source_account(message, context="Gmail message"))
        for message in messages
    ]
    query_receipts = _extract_query_receipts(messages_payload)
    receipts_by_key = _validated_query_receipts(query_receipts, expected_plan)
    found_counts: dict[str, int] = {}
    missing: list[str] = []
    missing_query_receipts: list[str] = []
    unprocessed_query_results: list[str] = []
    query_execution: dict[str, dict[str, Any]] = {}
    required_vendor_observed_counts: dict[str, int] = {}
    for item in raw_items if isinstance(raw_items, list) else []:
        vendor_key = str(item.get("vendor_key") or "")
        account_email = str(item.get("account_email") or "")
        key = f"{vendor_key}:{mask_identifier(account_email)}"
        count = _count_vendor_messages(message_manifest, vendor_key, account_email)
        found_counts[key] = count
        query_hash = str(item.get("query_hash") or "")
        receipt = receipts_by_key.get((account_email, vendor_key, query_hash))
        if receipt is None:
            missing_query_receipts.append(key)
            query_execution[key] = {
                "status": "missing",
                "query_hash": query_hash,
                "result_count": None,
                "processed_count": None,
                "manifest_message_count": count,
            }
        else:
            result_count = int(receipt["result_count"])
            processed_count = receipt.get("processed_count")
            result_processed = (
                result_count == 0
                or count >= result_count
                or (processed_count is not None and int(processed_count) >= result_count)
            )
            if not result_processed:
                unprocessed_query_results.append(key)
            query_execution[key] = {
                "status": "completed",
                "query_hash": query_hash,
                "result_count": result_count,
                "processed_count": processed_count,
                "manifest_message_count": count,
                "result_processing_verified": result_processed,
            }
            if item.get("required") is True:
                required_vendor_observed_counts[vendor_key] = (
                    required_vendor_observed_counts.get(vendor_key, 0)
                    + max(count, result_count if result_processed else 0)
                )
    required_vendor_keys = {
        str(item["vendor_key"])
        for item in expected_plan.search_items
        if item["required"] is True
    }
    missing.extend(
        f"{vendor_key}:aggregate"
        for vendor_key in sorted(required_vendor_keys)
        if required_vendor_observed_counts.get(vendor_key, 0) == 0
    )
    return GmailRecurringInvoiceCoverage(
        status="ready"
        if not missing and not missing_query_receipts and not unprocessed_query_results
        else "blocked",
        checked_count=len(found_counts),
        missing_required=tuple(missing),
        found_counts=found_counts,
        missing_query_receipts=tuple(missing_query_receipts),
        unprocessed_query_results=tuple(unprocessed_query_results),
        query_execution=query_execution,
    )


def _with_query_hash(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "query_hash": stable_hash({
        "company_slug": get_current_profile().slug,
        "account_hash": item["account_hash"],
        "vendor_key": item["vendor_key"],
        "gmail_query": item["gmail_query"],
    })}


def _extract_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("messages", "emails", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                if any(not isinstance(item, dict) for item in value):
                    raise ValueError("Gmail message manifest entries must be objects")
                return value
    if isinstance(payload, list):
        if any(not isinstance(item, dict) for item in payload):
            raise ValueError("Gmail message manifest entries must be objects")
        return payload
    return []


def _extract_query_receipts(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get("queryReceipts")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("GAS Gmail queryReceipts must be a list")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError("Gmail query receipt entries must be objects")
    return value


def _validated_query_receipts(
    receipts: list[dict[str, Any]],
    plan: GmailRecurringInvoiceSearchPlan,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    expected = {
        (str(item["account_email"]), str(item["vendor_key"]), str(item["query_hash"]))
        for item in plan.search_items
    }
    validated: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for receipt in receipts:
        if any(
            not str(receipt.get(field) or "").strip()
            for field in ("principal", "accountEmail", "accountHash", "vendorKey", "queryHash", "status")
        ):
            raise ValueError("GAS Gmail query receipt is missing canonical fields")
        account_email = _resolve_evidence_source_account(receipt, context="Gmail query receipt")
        key = (
            account_email,
            str(receipt["vendorKey"]).strip().casefold(),
            str(receipt["queryHash"]).strip().casefold(),
        )
        if key not in expected:
            raise ValueError("Gmail query receipt does not match the canonical search plan")
        if key in seen:
            raise ValueError("duplicate Gmail query receipt for one account/vendor query")
        seen.add(key)
        if receipt["status"] != "completed":
            continue
        result_count = _non_negative_int(receipt.get("resultCount"), "resultCount")
        processed_count = _non_negative_int(receipt.get("processedCount"), "processedCount")
        validated[key] = {
            "account_email": account_email,
            "vendor_key": key[1],
            "query_hash": key[2],
            "status": "completed",
            "result_count": result_count,
            "processed_count": processed_count,
        }
    return validated


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"GAS Gmail query receipt {field} must be a non-negative integer")
    return value


def _allowed_account_hashes(account_email: str) -> set[str]:
    masked = mask_identifier(account_email)
    digest = masked.removeprefix("sha256:")
    return {masked.casefold(), digest.casefold(), digest[:16].casefold()}


def _resolve_evidence_source_account(item: dict[str, Any], *, context: str) -> str:
    account_values = {
        str(item[key]).strip().casefold()
        for key in ("mailbox", "account_email", "accountEmail", "account", "principal")
        if key in item and item[key] is not None and str(item[key]).strip()
    }
    if len(account_values) > 1:
        raise ValueError(f"{context} mailbox aliases do not match")
    account_email = next(iter(account_values), "")
    account_hash_values = {
        str(item[key]).strip().casefold()
        for key in ("account_hash", "accountHash", "source_mailbox_hash")
        if key in item and item[key] is not None and str(item[key]).strip()
    }
    if account_email and account_email not in evidence_source_accounts():
        raise ValueError(f"{context} source mailbox is not allowed")
    if account_hash_values:
        resolved_hash_accounts: set[str] = set()
        for account_hash in account_hash_values:
            hash_matches = [
                account
                for account in evidence_source_accounts()
                if account_hash in _allowed_account_hashes(account)
            ]
            if len(hash_matches) != 1:
                raise ValueError(f"{context} account hash is not an allowed verified mailbox hash")
            resolved_hash_accounts.add(hash_matches[0])
        if len(resolved_hash_accounts) != 1:
            raise ValueError(f"{context} account hash aliases do not match")
        hash_account = next(iter(resolved_hash_accounts))
        if account_email and account_email != hash_account:
            raise ValueError(f"{context} mailbox and account hash do not match")
        account_email = hash_account
    if not account_email:
        raise ValueError(f"{context} source mailbox provenance is required")
    return account_email


def _count_vendor_messages(
    message_manifest: list[tuple[dict[str, Any], str]],
    vendor_key: str,
    account_email: str,
) -> int:
    matches = [row for row in recurring_vendor_queries() if row["vendor_key"] == vendor_key]
    if len(matches) != 1:
        raise ValueError("vendor query is not uniquely configured")
    vendor_terms = matches[0]["terms"]
    count = 0
    for message, mailbox in message_manifest:
        if account_email and mailbox != account_email:
            continue
        haystack = " ".join(
            str(message.get(key) or "")
            for key in ("from", "from_", "sender", "subject", "snippet", "body")
        ).casefold()
        if any(term.casefold() in haystack for term in vendor_terms):
            count += 1
    return count


def _require_evidence_source_accounts(accounts: tuple[str, ...] | None) -> tuple[str, ...]:
    expected = evidence_source_accounts()
    normalized = expected if accounts is None else tuple(str(account or "").strip().casefold() for account in accounts)
    if len(normalized) != len(expected) or set(normalized) != set(expected):
        raise ValueError("recurring invoice coverage requires exactly the current profile evidence accounts")
    return expected
