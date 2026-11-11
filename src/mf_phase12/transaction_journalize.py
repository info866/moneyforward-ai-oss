from __future__ import annotations

import sqlite3
import re
from datetime import date, timedelta
from typing import Any, Mapping

from .accounting_api import (
    MoneyForwardAccountingClient,
    validate_journal_create_request,
    validate_transaction_journalize_request,
)
from .hash_utils import mask_identifier, stable_hash


TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL = "transaction_journalize"
TRANSACTION_JOURNALIZE_API_ACTION = "POST /api/v3/transactions/journalize"
JOURNAL_CREATE_EXECUTION_CHANNEL = "journal_create"
JOURNAL_CREATE_API_ACTION = "POST /api/v3/journals"
TRANSACTION_BINDING_PROOF_FIELDS = (
    "source_row_hash",
    "transaction_facts_hash",
    "connected_account_binding_hash",
    "connected_ledger_binding_hash",
)


def build_transaction_journalize_request(
    *,
    journal: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert one exact journal into the official linked-transaction request.

    Money Forward supplies the connected bank/card side from the statement.
    The API request therefore contains only the opposite side.  This function
    proves that the omitted side, amount, direction, date, and ledger mapping
    exactly match the previously captured transaction before returning a body.
    Compound/split journals are intentionally rejected.
    """
    validate_journal_create_request(journal)
    branches = journal.get("branches")
    if not isinstance(branches, list) or len(branches) != 1:
        raise ValueError("transaction journalize supports exactly one journal branch")
    branch = branches[0]
    if not isinstance(branch, Mapping):
        raise ValueError("transaction journalize branch must be an object")

    transaction_id = _required_string(binding, "transaction_id")
    connected_account_id = _required_string(binding, "connected_account_id")
    connected_sub_account_id = str(binding.get("connected_sub_account_id") or "").strip()
    ledger_account_id = _required_string(binding, "connected_ledger_account_id")
    ledger_sub_account_id = str(binding.get("connected_ledger_sub_account_id") or "").strip()
    side = _required_string(binding, "transaction_side")
    if side not in {"INCOME", "EXPENSE"}:
        raise ValueError("transaction binding side must be INCOME or EXPENSE")
    value = _positive_int(binding.get("transaction_value"), "transaction binding value")
    original_date = _required_string(binding, "transaction_original_date")
    if str(journal.get("transaction_date") or "") != original_date:
        raise ValueError("transaction journal date must match the original linked-statement date")
    if str(binding.get("journalizing_status") or "") != "none":
        raise ValueError("transaction binding must still be unposted")
    expected_account_binding_hash = mask_identifier(f"{connected_account_id}:{connected_sub_account_id}")
    if str(binding.get("connected_account_binding_hash") or "") != expected_account_binding_hash:
        raise ValueError("transaction connected-account binding hash mismatch")
    expected_ledger_binding_hash = mask_identifier(
        f"{connected_account_id}:{connected_sub_account_id}:{ledger_account_id}:{ledger_sub_account_id}"
    )
    if str(binding.get("connected_ledger_binding_hash") or "") != expected_ledger_binding_hash:
        raise ValueError("transaction connected-ledger binding hash mismatch")
    for field in ("source_row_hash", "transaction_facts_hash"):
        if not _is_sha256_hex(str(binding.get(field) or "")):
            raise ValueError(f"transaction binding {field} must be a SHA-256 hash")

    connected_name = "debitor" if side == "INCOME" else "creditor"
    counterpart_name = "creditor" if side == "INCOME" else "debitor"
    connected = _required_object(branch, connected_name)
    counterpart = _required_object(branch, counterpart_name)
    if _positive_int(connected.get("value"), f"journal {connected_name}.value") != value:
        raise ValueError("linked-statement amount does not match the journal connected side")
    if _positive_int(counterpart.get("value"), f"journal {counterpart_name}.value") != value:
        raise ValueError("linked-statement amount does not match the journal counterpart side")
    if str(connected.get("account_id") or "") != ledger_account_id:
        raise ValueError("journal connected-side account does not match the linked-service ledger mapping")
    if str(connected.get("sub_account_id") or "") != ledger_sub_account_id:
        raise ValueError("journal connected-side sub-account does not match the linked-service ledger mapping")

    request: dict[str, Any] = {
        "transaction_id": transaction_id,
        "transaction_date": original_date,
        "account_id": _required_string(counterpart, "account_id"),
    }
    for key in ("sub_account_id", "department_id", "trade_partner_code", "tax_id", "invoice_kind"):
        field_value = counterpart.get(key)
        if field_value not in (None, ""):
            request[key] = field_value
    remark = branch.get("remark")
    if remark not in (None, ""):
        request["remark"] = remark
    for key in ("memo", "tags"):
        field_value = journal.get(key)
        if field_value not in (None, "", []):
            request[key] = field_value
    validate_transaction_journalize_request(request)
    return request


def validate_transaction_journalize_candidate_mapping(
    *,
    conn: sqlite3.Connection,
    journal: Mapping[str, Any],
    binding: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    """Prove that raw master IDs still represent the classified candidate."""
    request = build_transaction_journalize_request(journal=journal, binding=binding)
    side = _required_string(binding, "transaction_side")
    value = _positive_int(binding.get("transaction_value"), "transaction binding value")
    if _positive_int(candidate.get("amount"), "journal candidate amount") != value:
        raise ValueError("transaction journalize candidate amount does not match the linked statement")
    if str(candidate.get("journal_date") or "") != str(binding.get("transaction_original_date") or ""):
        raise ValueError("transaction journalize candidate date does not match the linked statement")
    branch = journal["branches"][0]
    if str(branch.get("remark") or "") != str(candidate.get("description") or ""):
        raise ValueError("transaction journalize journal remark does not match the classified candidate")

    connected_name = "debitor" if side == "INCOME" else "creditor"
    counterpart_name = "creditor" if side == "INCOME" else "debitor"
    connected = _required_object(branch, connected_name)
    counterpart = _required_object(branch, counterpart_name)
    candidate_connected_prefix = "debit" if side == "INCOME" else "credit"
    candidate_counterpart_prefix = "credit" if side == "INCOME" else "debit"
    _require_master_id_match(
        conn,
        item_type="account",
        expected_name=_required_string(candidate, f"{candidate_connected_prefix}_account"),
        raw_id=_required_string(connected, "account_id"),
    )
    _require_optional_master_id_match(
        conn,
        item_type="sub_account",
        expected_name=str(candidate.get(f"{candidate_connected_prefix}_sub_account") or ""),
        raw_id=str(connected.get("sub_account_id") or ""),
    )
    _require_master_id_match(
        conn,
        item_type="account",
        expected_name=_required_string(candidate, f"{candidate_counterpart_prefix}_account"),
        raw_id=_required_string(counterpart, "account_id"),
    )
    _require_optional_master_id_match(
        conn,
        item_type="sub_account",
        expected_name=str(candidate.get(f"{candidate_counterpart_prefix}_sub_account") or ""),
        raw_id=str(counterpart.get("sub_account_id") or ""),
    )
    tax_name = str(candidate.get("tax_category") or "")
    tax_id = str(request.get("tax_id") or "")
    if tax_name or tax_id:
        _require_master_id_match(
            conn,
            item_type="tax",
            expected_name=tax_name,
            raw_id=tax_id,
        )
    trade_partner_name = str(candidate.get("trade_partner_name") or "").strip()
    candidate_trade_partner_code = str(candidate.get("trade_partner_code") or "").strip()
    request_trade_partner_code = str(request.get("trade_partner_code") or "").strip()
    if trade_partner_name or candidate_trade_partner_code or request_trade_partner_code:
        if not trade_partner_name or not candidate_trade_partner_code or not request_trade_partner_code:
            raise ValueError("transaction journalize trade_partner candidate/master presence mismatch")
        if candidate_trade_partner_code != request_trade_partner_code:
            raise ValueError("transaction journalize trade_partner code does not match the classified candidate")
        _require_master_id_match(
            conn,
            item_type="trade_partner",
            expected_name=trade_partner_name,
            raw_id=request_trade_partner_code,
        )


def transaction_journalize_binding_proof(binding: Mapping[str, Any]) -> dict[str, str]:
    """Return the redacted proof that must survive approval through live POST."""
    proof = {
        field: str(binding.get(field) or "").strip()
        for field in TRANSACTION_BINDING_PROOF_FIELDS
    }
    for field in ("source_row_hash", "transaction_facts_hash"):
        if not _is_sha256_hex(proof[field]):
            raise ValueError(f"transaction binding {field} must be a SHA-256 hash")
    for field in ("connected_account_binding_hash", "connected_ledger_binding_hash"):
        if not _is_mask_identifier_hash(proof[field]):
            raise ValueError(f"transaction binding {field} must be a masked SHA-256 hash")
    return proof


def verify_transaction_journalize_precondition(
    *,
    client: MoneyForwardAccountingClient,
    request: Mapping[str, Any],
    binding_proof: Mapping[str, Any],
    office_id: str | None = None,
    max_pages: int = 100,
) -> str:
    """Re-read the exact unposted row and ledger binding immediately before POST.

    This closes the race between the original inventory/classification and the
    write.  Any status, amount, direction, description, account, sub-account,
    or ledger-master change fails closed before an execution claim is created.
    """
    validate_transaction_journalize_request(request)
    proof = transaction_journalize_binding_proof(binding_proof)
    if max_pages < 1:
        raise ValueError("transaction precondition max_pages must be positive")
    transaction_id = _required_string(request, "transaction_id")
    transaction_date = _required_string(request, "transaction_date")
    matches: list[dict[str, Any]] = []
    response_hashes: list[str] = []
    page = 1
    # Money Forward rejects an equal start/end date with HTTP 400. Read a
    # two-day window and still bind the exact target by immutable ID and facts.
    query_start_date = (date.fromisoformat(transaction_date) - timedelta(days=40)).isoformat()
    while True:
        payload = client.transactions(
            office_id,
            start_date=query_start_date,
            end_date=transaction_date,
            journalizing_statuses=("none",),
            page=page,
            per_page=500,
        )
        response_hashes.append(stable_hash(payload))
        items = payload.get("transactions") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise PermissionError("transaction precondition read-back has no transactions array")
        matches.extend(
            item for item in items if isinstance(item, dict) and str(item.get("id") or "") == transaction_id
        )
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
        total_pages = (
            _nonnegative_int(metadata.get("total_pages"), "transaction metadata.total_pages")
            if isinstance(metadata, Mapping) and metadata.get("total_pages") is not None
            else None
        )
        if total_pages is not None:
            if page >= total_pages:
                break
        elif len(items) < 500:
            break
        page += 1
        if page > max_pages:
            raise PermissionError("transaction precondition exceeds the page safety limit")
    if len(matches) != 1:
        raise PermissionError("transaction precondition requires exactly one still-unposted target")
    transaction = matches[0]
    if str(transaction.get("journalizing_status") or "") != "none":
        raise PermissionError("transaction precondition target is no longer unposted")
    if transaction_invariant_facts_hash(transaction) != proof["transaction_facts_hash"]:
        raise PermissionError("transaction precondition facts changed after approval")
    connected_account_id = str(transaction.get("connected_account_id") or "").strip()
    connected_sub_account_id = str(transaction.get("connected_sub_account_id") or "").strip()
    if mask_identifier(f"{connected_account_id}:{connected_sub_account_id}") != proof[
        "connected_account_binding_hash"
    ]:
        raise PermissionError("transaction precondition connected-account binding changed")

    connected_payload = client.connected_accounts(office_id)
    connected_items = _extract_connected_account_items(connected_payload)
    ledger_account_id, ledger_sub_account_id = _resolve_connected_ledger_ids(
        connected_items,
        connected_account_id=connected_account_id,
        connected_sub_account_id=connected_sub_account_id,
    )
    ledger_hash = mask_identifier(
        f"{connected_account_id}:{connected_sub_account_id}:{ledger_account_id}:{ledger_sub_account_id}"
    )
    if ledger_hash != proof["connected_ledger_binding_hash"]:
        raise PermissionError("transaction precondition connected-ledger binding changed")
    return stable_hash(
        {
            "transaction_facts_hash": proof["transaction_facts_hash"],
            "connected_account_binding_hash": proof["connected_account_binding_hash"],
            "connected_ledger_binding_hash": proof["connected_ledger_binding_hash"],
            "response_hashes": response_hashes,
            "connected_accounts_response_hash": stable_hash(connected_payload),
        }
    )


def _require_optional_master_id_match(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    expected_name: str,
    raw_id: str,
) -> None:
    if not expected_name and not raw_id:
        return
    if not expected_name or not raw_id:
        raise ValueError(f"transaction journalize {item_type} candidate/master presence mismatch")
    _require_master_id_match(conn, item_type=item_type, expected_name=expected_name, raw_id=raw_id)


def _require_master_id_match(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    expected_name: str,
    raw_id: str,
) -> None:
    if not expected_name or not raw_id:
        raise ValueError(f"transaction journalize {item_type} mapping is incomplete")
    rows = conn.execute(
        """
        SELECT name_normalized
        FROM master_items
        WHERE item_type = ?
          AND external_id_hash = ?
          AND active = 1
        """,
        (item_type, mask_identifier(raw_id)),
    ).fetchall()
    expected_stored_name = re.sub(r"\d{4,}", "####", re.sub(r"T\d{13}", "T*************", expected_name))[:120]
    if len(rows) != 1 or str(rows[0]["name_normalized"] or "") != expected_stored_name:
        raise ValueError(f"transaction journalize {item_type} master ID does not match the classified candidate")


def transaction_invariant_facts_hash(transaction: Mapping[str, Any]) -> str:
    """Hash fields that must not change between pre-write and read-back."""
    return stable_hash(
        {
            "transaction_id": str(transaction.get("id") or transaction.get("transaction_id") or "").strip(),
            "date": str(transaction.get("date") or transaction.get("transaction_original_date") or "")[:10],
            "value": transaction.get("value", transaction.get("transaction_value")),
            "side": str(transaction.get("side") or transaction.get("transaction_side") or "").strip(),
            "content": str(transaction.get("content") or transaction.get("description") or "").strip(),
            "memo": str(transaction.get("memo") or "").strip(),
            "connected_account_id": str(transaction.get("connected_account_id") or "").strip(),
            "connected_sub_account_id": str(transaction.get("connected_sub_account_id") or "").strip(),
            "voucher_file_id_hashes": sorted(
                mask_identifier(str(value))
                for value in transaction.get("voucher_file_ids") or []
                if str(value)
            )
            if transaction.get("voucher_file_ids") is not None
            else sorted(str(value) for value in transaction.get("voucher_file_id_hashes") or []),
        }
    )


def _extract_connected_account_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, Mapping):
        for key in ("connected_accounts", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise PermissionError("connected-account precondition read-back has no account list")


def _resolve_connected_ledger_ids(
    items: list[dict[str, Any]],
    *,
    connected_account_id: str,
    connected_sub_account_id: str,
) -> tuple[str, str]:
    account_matches = [item for item in items if str(item.get("id") or "") == connected_account_id]
    if len(account_matches) != 1:
        raise PermissionError("connected-account precondition requires exactly one account mapping")
    account = account_matches[0]
    if connected_sub_account_id:
        sub_accounts = account.get("connected_sub_accounts")
        if not isinstance(sub_accounts, list):
            raise PermissionError("connected-account precondition has no sub-account mapping")
        sub_matches = [
            item
            for item in sub_accounts
            if isinstance(item, dict) and str(item.get("id") or "") == connected_sub_account_id
        ]
        if len(sub_matches) != 1:
            raise PermissionError("connected-account precondition requires exactly one sub-account mapping")
        mapping = sub_matches[0]
    else:
        mapping = account
    ledger_account_id = str(mapping.get("account_id") or "").strip()
    ledger_sub_account_id = str(mapping.get("sub_account_id") or "").strip()
    if not ledger_account_id:
        raise PermissionError("connected-account precondition ledger account is unresolved")
    return ledger_account_id, ledger_sub_account_id


def journal_write_execution_channel(request: Mapping[str, Any]) -> str:
    has_transaction_id = bool(str(request.get("transaction_id") or "").strip())
    has_journal_shape = any(key in request for key in ("journal_type", "branches"))
    if has_transaction_id and has_journal_shape:
        raise ValueError("journal write request mixes journal-create and transaction-journalize shapes")
    if has_transaction_id:
        validate_transaction_journalize_request(request)
        return TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL
    validate_journal_create_request(request)
    return JOURNAL_CREATE_EXECUTION_CHANNEL


def journal_write_request_metadata(
    request: dict[str, Any],
    *,
    office_id: str | None = None,
) -> dict[str, Any]:
    """Return endpoint-bound metadata for either supported journal-create path."""
    channel = journal_write_execution_channel(request)
    client = MoneyForwardAccountingClient("METADATA_ONLY_TOKEN_NOT_USED")
    if channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        metadata = client.transaction_journalize_request_metadata(request=request, office_id=office_id)
    else:
        metadata = client.create_journal_request_metadata(journal=request, office_id=office_id)
    return {
        **metadata,
        "execution_channel": channel,
        "expected_api_action": expected_api_action(channel),
    }


def expected_api_action(execution_channel: str) -> str:
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        return TRANSACTION_JOURNALIZE_API_ACTION
    if execution_channel == JOURNAL_CREATE_EXECUTION_CHANNEL:
        return JOURNAL_CREATE_API_ACTION
    raise ValueError(f"unsupported journal execution channel: {execution_channel}")


def _required_object(payload: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"transaction journalize {field} must be an object")
    return value


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(f"transaction journalize {field} is required")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PermissionError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise PermissionError(f"{field} must be a non-negative integer") from error
    if parsed < 0:
        raise PermissionError(f"{field} must be a non-negative integer")
    return parsed


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _is_mask_identifier_hash(value: str) -> bool:
    return value.startswith("sha256:") and _is_sha256_hex(value.removeprefix("sha256:"))
