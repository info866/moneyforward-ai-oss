from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .accounting_api import ACCOUNTING_TRANSACTION_READ_SCOPE, MoneyForwardAccountingClient
from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .drive_policy import company_policies
from .office_profile import get_current_profile, get_setting


TRANSACTIONS_GET_ENDPOINT = "/api/v3/transactions"
TRANSACTION_JOURNALIZE_ENDPOINT = "/api/v3/transactions/journalize"
TRANSACTION_DATE_RANGE_MAX_DAYS = 40


@dataclass(frozen=True)
class ConnectedAccountProbeResult:
    status: str
    accounting_api_connected: bool
    connected_account_count: int
    top_keys: list[str]
    sample_keys: list[str]
    account_hashes: list[str]
    output_json: str | None = None
    normalized_transactions_output_json: str | None = None
    moneyforward_write_called: bool = False
    raw_api_response_stored: bool = False
    unposted_financial_event_api_verified: bool = False
    unposted_financial_event_count: int | None = None
    unposted_counts_by_account_hash: tuple[dict[str, Any], ...] = ()
    unposted_financial_event_api_reason: str = "not_checked"
    public_accounting_api_spec_checked: bool = True
    public_accounting_api_candidate_body_get_endpoint: str = TRANSACTIONS_GET_ENDPOINT
    public_accounting_api_transaction_journalize_endpoint: str = TRANSACTION_JOURNALIZE_ENDPOINT
    transaction_read_scope: str = ACCOUNTING_TRANSACTION_READ_SCOPE
    transaction_journalize_scope: str = "mfc/accounting/journal.write"
    public_accounting_api_endpoint_boundary: str = (
        "GET /api/v3/transactions reads connected-service statements including journalizing status; "
        "POST /api/v3/transactions/journalize creates a journal bound to and consuming one transaction"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounting_api_connected": self.accounting_api_connected,
            "connected_account_count": self.connected_account_count,
            "top_keys": self.top_keys,
            "sample_keys": self.sample_keys,
            "account_hashes": self.account_hashes,
            "output_json": self.output_json,
            "normalized_transactions_output_json": self.normalized_transactions_output_json,
            "moneyforward_write_called": self.moneyforward_write_called,
            "raw_api_response_stored": self.raw_api_response_stored,
            "unposted_financial_event_api_verified": self.unposted_financial_event_api_verified,
            "unposted_financial_event_count": self.unposted_financial_event_count,
            "unposted_counts_by_account_hash": list(self.unposted_counts_by_account_hash),
            "unposted_financial_event_api_reason": self.unposted_financial_event_api_reason,
            "public_accounting_api_spec_checked": self.public_accounting_api_spec_checked,
            "public_accounting_api_candidate_body_get_endpoint": self.public_accounting_api_candidate_body_get_endpoint,
            "public_accounting_api_transaction_journalize_endpoint": self.public_accounting_api_transaction_journalize_endpoint,
            "transaction_read_scope": self.transaction_read_scope,
            "transaction_journalize_scope": self.transaction_journalize_scope,
            "public_accounting_api_endpoint_boundary": self.public_accounting_api_endpoint_boundary,
        }


def run_connected_account_probe(
    *,
    accounting_client: MoneyForwardAccountingClient,
    output_json: Path | None = None,
    office_id: str | None = None,
    include_unposted_transactions: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    normalized_transactions_output_json: Path | None = None,
    max_pages: int = 100,
) -> ConnectedAccountProbeResult:
    profile = _current_profile()
    client_profile = accounting_client.office_profile
    if client_profile is None or client_profile.boundary_fingerprint != profile.boundary_fingerprint:
        raise ValueError("connected account client does not match the current company profile")
    payload = accounting_client.connected_accounts(office_id)
    items = _extract_items(payload)
    top_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    sample_keys = sorted(items[0].keys()) if items and isinstance(items[0], dict) else []

    unposted_items: list[dict[str, Any]] = []
    counts_by_account_hash: tuple[dict[str, Any], ...] = ()
    transaction_api_verified = False
    reason = "transaction_read_endpoint_not_requested"
    normalized_output: Path | None = None
    if include_unposted_transactions:
        if not start_date or not end_date:
            raise ValueError("start_date and end_date are required for the unposted transaction API probe")
        unposted_items = _read_all_unposted_transactions(
            accounting_client,
            office_id=office_id,
            start_date=start_date,
            end_date=end_date,
            max_pages=max_pages,
        )
        transaction_api_verified = True
        reason = "transaction_read_endpoint_verified"
        counts_by_account_hash = _unposted_counts(items, unposted_items)
        if normalized_transactions_output_json is not None:
            normalized_output = _write_normalized_transactions(
                normalized_transactions_output_json,
                connected_accounts=items,
                transactions=unposted_items,
                start_date=start_date,
                end_date=end_date,
            )

    result = ConnectedAccountProbeResult(
        status="complete" if transaction_api_verified else "needs_attention",
        accounting_api_connected=True,
        connected_account_count=len(items),
        top_keys=top_keys,
        sample_keys=sample_keys,
        account_hashes=[stable_hash(item) for item in items[:20]],
        output_json=str(output_json) if output_json else None,
        normalized_transactions_output_json=str(normalized_output) if normalized_output else None,
        unposted_financial_event_api_verified=transaction_api_verified,
        unposted_financial_event_count=len(unposted_items) if transaction_api_verified else None,
        unposted_counts_by_account_hash=counts_by_account_hash,
        unposted_financial_event_api_reason=reason,
    )
    if output_json:
        output_json = require_ignored_untracked_data_path(output_json, purpose="connected account probe output")
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result


def _read_all_unposted_transactions(
    client: MoneyForwardAccountingClient,
    *,
    office_id: str | None,
    start_date: str,
    end_date: str,
    max_pages: int,
) -> list[dict[str, Any]]:
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for window_start, window_end in _date_windows(start_date, end_date):
        query_start = window_start
        if window_start == window_end:
            # The API requires start_date < end_date. Expand only the GET
            # window, then discard rows outside the caller's requested day.
            query_start = (date.fromisoformat(window_start) - timedelta(days=40)).isoformat()
        page = 1
        raw_window_item_count = 0
        window_items: list[dict[str, Any]] = []
        expected_total_count: int | None = None
        while True:
            if page > max_pages:
                raise RuntimeError("Money Forward transaction.read exceeds the configured max_pages safety limit")
            payload = client.transactions(
                office_id,
                start_date=query_start,
                end_date=window_end,
                journalizing_statuses=("none",),
                page=page,
                per_page=500,
            )
            page_items = _extract_transactions(payload)
            for item in page_items:
                if item.get("journalizing_status") != "none":
                    raise RuntimeError("Money Forward transaction.read returned a non-unposted row for the none filter")
            raw_window_item_count += len(page_items)
            window_items.extend(
                item
                for item in page_items
                if window_start <= str(item.get("date") or item.get("transaction_date") or "")[:10] <= window_end
            )
            total_pages = _metadata_total_pages(payload)
            total_count = _metadata_total_count(payload)
            if total_pages is None or total_count is None:
                raise RuntimeError("Money Forward transaction.read response is missing pagination metadata")
            if (total_pages == 0) != (total_count == 0):
                raise RuntimeError("Money Forward transaction.read pagination metadata is inconsistent")
            if expected_total_count is None:
                expected_total_count = total_count
            elif total_count is not None and total_count != expected_total_count:
                raise RuntimeError("Money Forward transaction.read metadata.total_count changed during pagination")
            if page >= total_pages:
                break
            page += 1
        if expected_total_count is not None and raw_window_item_count != expected_total_count:
            raise RuntimeError("Money Forward transaction.read rows do not match metadata.total_count")
        for item in window_items:
            transaction_id = str(item.get("id") or "").strip()
            if not transaction_id:
                raise RuntimeError("Money Forward transaction.read returned a row without an ID")
            if transaction_id in seen_ids:
                raise RuntimeError("Money Forward transaction.read returned a duplicate transaction across date windows")
            seen_ids.add(transaction_id)
            result.append(item)
    return result


def _date_windows(start_date: str, end_date: str) -> tuple[tuple[str, str], ...]:
    try:
        cursor = date.fromisoformat(start_date)
        final = date.fromisoformat(end_date)
    except ValueError as error:
        raise ValueError("transaction probe dates must be YYYY-MM-DD") from error
    if final < cursor:
        raise ValueError("transaction probe end_date must be on or after start_date")
    windows: list[tuple[str, str]] = []
    while cursor <= final:
        # The live transaction endpoint rejects longer periods even though the
        # public client contract historically allowed a fiscal-year request.
        # Keep adjacent windows disjoint while using the largest accepted
        # start/end distance.
        window_end = min(cursor + timedelta(days=TRANSACTION_DATE_RANGE_MAX_DAYS), final)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _write_normalized_transactions(
    output_json: Path,
    *,
    connected_accounts: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> Path:
    output_json = require_ignored_untracked_data_path(
        output_json,
        purpose="Money Forward normalized connected transactions output",
    )
    account_bindings = _connected_account_bindings(connected_accounts)
    normalized = [_normalize_transaction(item, account_bindings=account_bindings) for item in transactions]
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    local_payload = {
        "company_slug": _current_profile().slug,
        "profile_slug": _current_profile().slug,
        "boundary_fingerprint": _current_profile().boundary_fingerprint,
        "schema": "moneyforward-connected-transactions-v1",
        "source": "GET /api/v3/transactions",
        "captured_at": captured_at,
        "start_date": start_date,
        "end_date": end_date,
        "journalizing_statuses": ["none"],
        "transactions": normalized,
        "transaction_item_hashes": sorted(
            str(item["source_event_id_hash"]) for item in normalized
        ),
        "source_type_counts": {
            source_type: sum(
                item.get("connected_source_type") == source_type for item in normalized
            )
            for source_type in ("bank", "card", "supplier_primary", "unclassified")
        },
        "source_response_hash": stable_hash(transactions),
        "raw_api_response_stored": False,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(local_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_json


def _normalize_transaction(
    item: dict[str, Any],
    *,
    account_bindings: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    profile = _current_profile()
    for field in ("company_slug", "profile_slug"):
        if field in item and item[field] != profile.slug:
            raise ValueError("connected transaction company binding mismatch")
    if "boundary_fingerprint" in item and item["boundary_fingerprint"] != profile.boundary_fingerprint:
        raise ValueError("connected transaction profile binding mismatch")
    transaction_id = str(item.get("id") or "").strip()
    connected_account_id = str(item.get("connected_account_id") or "").strip()
    connected_sub_account_id = str(item.get("connected_sub_account_id") or "").strip()
    if not transaction_id or not connected_account_id:
        raise RuntimeError("Money Forward transaction.read returned a row without required IDs")
    side = str(item.get("side") or "").strip()
    if side not in {"INCOME", "EXPENSE"}:
        raise RuntimeError("Money Forward transaction.read returned an unsupported side")
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("Money Forward transaction.read returned an invalid value")
    account_binding = account_bindings.get((connected_account_id, connected_sub_account_id)) or account_bindings.get(
        (connected_account_id, ""),
        {},
    )
    account_name = str(account_binding.get("name") or "")
    ledger_account_id = str(account_binding.get("account_id") or "").strip()
    ledger_sub_account_id = str(account_binding.get("sub_account_id") or "").strip()
    connected_source_type = _connected_source_type(
        account_name, ledger_account_id=ledger_account_id,
        ledger_sub_account_id=ledger_sub_account_id,
    )
    transaction_facts = {
        "transaction_id": transaction_id,
        "date": str(item.get("date") or "")[:10],
        "value": value,
        "side": side,
        "content": str(item.get("content") or "").strip(),
        "memo": str(item.get("memo") or "").strip(),
        "connected_account_id": connected_account_id,
        "connected_sub_account_id": connected_sub_account_id,
        "voucher_file_id_hashes": sorted(
            mask_identifier(str(value))
            for value in item.get("voucher_file_ids") or []
            if str(value)
        ),
    }
    normalized = {
        "company_slug": profile.slug,
        "id": transaction_id,
        "source_event_id": transaction_id,
        "transaction_id": transaction_id,
        "source_api": "GET /api/v3/transactions",
        "source_event_type": "card_transaction" if connected_source_type in {"card", "supplier_primary"} else "bank_transaction",
        "connected_source_type": connected_source_type,
        "bank_or_card_account": account_name,
        "transaction_date": str(item.get("date") or "")[:10],
        "description": str(item.get("content") or "").strip(),
        "memo": str(item.get("memo") or "").strip(),
        "amount": value,
        "cash_flow_direction": "inflow" if side == "INCOME" else "outflow",
        "transaction_side": side,
        "transaction_value": value,
        "transaction_original_date": str(item.get("date") or "")[:10],
        "journalizing_status": "none",
        "connected_account_id": connected_account_id,
        "connected_sub_account_id": connected_sub_account_id or None,
        "connected_ledger_account_id": ledger_account_id or None,
        "connected_ledger_sub_account_id": ledger_sub_account_id or None,
        "voucher_file_count": len(item.get("voucher_file_ids") or []),
        "voucher_file_id_hashes": transaction_facts["voucher_file_id_hashes"],
        "source_row_hash": stable_hash(item),
        "transaction_facts_hash": stable_hash(transaction_facts),
        "source_event_id_hash": mask_identifier(transaction_id),
        "connected_account_binding_hash": _account_binding_hash(connected_account_id, connected_sub_account_id),
        "connected_ledger_binding_hash": _ledger_binding_hash(
            connected_account_id,
            connected_sub_account_id,
            ledger_account_id,
            ledger_sub_account_id,
        ),
    }
    return {key: value for key, value in normalized.items() if value not in ("", None)}


def _unposted_counts(
    connected_accounts: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    known_bindings = set(_connected_account_bindings(connected_accounts))
    counts: dict[str, int] = {
        _account_binding_hash(account_id, sub_account_id): 0
        for account_id, sub_account_id in known_bindings
    }
    for item in transactions:
        account_id = str(item.get("connected_account_id") or "").strip()
        sub_account_id = str(item.get("connected_sub_account_id") or "").strip()
        key = _account_binding_hash(account_id, sub_account_id)
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        {"account_binding_hash": key, "unposted_count": count}
        for key, count in sorted(counts.items())
    )


def _connected_account_bindings(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        account_id = str(item.get("id") or "").strip()
        account_name = str(item.get("name") or "").strip()
        if not account_id:
            continue
        sub_accounts = item.get("connected_sub_accounts")
        if isinstance(sub_accounts, list) and sub_accounts:
            for sub in sub_accounts:
                if not isinstance(sub, dict):
                    continue
                sub_id = str(sub.get("id") or "").strip()
                if sub_id:
                    sub_name = str(sub.get("name") or "").strip()
                    result[(account_id, sub_id)] = {
                        "name": " / ".join(value for value in (account_name, sub_name) if value),
                        "account_id": str(sub.get("account_id") or "").strip(),
                        "sub_account_id": str(sub.get("sub_account_id") or "").strip(),
                    }
        else:
            result[(account_id, "")] = {
                "name": account_name,
                "account_id": str(item.get("account_id") or "").strip(),
                "sub_account_id": str(item.get("sub_account_id") or "").strip(),
            }
    return result


def _connected_account_names(items: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Backward-compatible display-name view used by older callers/tests."""
    return {key: value["name"] for key, value in _connected_account_bindings(items).items()}


def _account_binding_hash(account_id: str, sub_account_id: str) -> str:
    return mask_identifier(f"{account_id}:{sub_account_id}")


def _ledger_binding_hash(
    connected_account_id: str,
    connected_sub_account_id: str,
    ledger_account_id: str,
    ledger_sub_account_id: str,
) -> str:
    return mask_identifier(
        f"{connected_account_id}:{connected_sub_account_id}:{ledger_account_id}:{ledger_sub_account_id}"
    )


def _source_event_type(account_name: str) -> str:
    if _connected_source_type(account_name) in {"card", "supplier_primary"}:
        return "card_transaction"
    return "bank_transaction"


def _current_profile():
    profile = get_current_profile()
    if profile is None:
        raise ValueError("connected account processing requires an explicit current company profile")
    return profile


def _configured_markers(path: str) -> tuple[str, ...]:
    """Frozen profiles expose arrays as tuples; neither form implies a default rule."""
    values = get_setting(path, ())
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{path} must be a list of nonempty strings")
    return tuple(unicodedata.normalize("NFKC", value).strip().casefold() for value in values)


def _connected_source_type(
    account_name: str, *, ledger_account_id: str = "", ledger_sub_account_id: str = ""
) -> str:
    _current_profile()
    lowered = unicodedata.normalize("NFKC", account_name).casefold()
    if any(marker in lowered for marker in _configured_markers("supplier_primary.vendor_markers")):
        return "supplier_primary"
    routes = company_policies("accounting_rules.card_routes")
    matched_routes = [
        route for route in routes
        if ledger_account_id and ledger_sub_account_id
        and route.get("ledger_account_id") == ledger_account_id
        and route.get("ledger_sub_account_id") == ledger_sub_account_id
    ]
    if len(matched_routes) > 1:
        raise ValueError("ambiguous configured connected card route")
    if matched_routes:
        return "card"
    if any(marker in lowered for marker in (*_configured_markers("card.statement_markers"), "card", "カード")):
        return "card"
    if account_name.strip():
        return "bank"
    return "unclassified"


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("connected_accounts", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_transactions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get("transactions")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise RuntimeError("Money Forward transaction.read response has no transactions array")


def _metadata_total_pages(payload: Any) -> int | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        return None
    value = payload["metadata"].get("total_pages")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Money Forward transaction.read metadata.total_pages is invalid") from error
    if parsed < 0:
        raise RuntimeError("Money Forward transaction.read metadata.total_pages is invalid")
    return parsed

def _metadata_total_count(payload: Any) -> int | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        return None
    value = payload["metadata"].get("total_count")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Money Forward transaction.read metadata.total_count is invalid") from error
    if parsed < 0:
        raise RuntimeError("Money Forward transaction.read metadata.total_count is invalid")
    return parsed
