from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .financial_event_classification import normalize_financial_event
from .hash_utils import mask_identifier, stable_hash
from .drive_policy import company_policies


@dataclass(frozen=True)
class SupplierPrimaryWatchItem:
    supplier_source_type: str
    vendor_name: str
    transaction_date: str
    amount: int
    source_journal_number: str
    moneyforward_journal_id: str
    voucher_verified: bool
    credit_account: str
    credit_sub_account: str
    expected_card_account_key: str
    status: str = "awaiting_card_statement"


def upsert_supplier_primary_card_duplicate_watch(
    conn: sqlite3.Connection,
    item: SupplierPrimaryWatchItem,
) -> str:
    routes = company_policies("accounting_rules.supplier_primary_card_routes")
    if not any(
        route.get("supplier_source_type") == item.supplier_source_type
        and route.get("card_account_key") == item.expected_card_account_key
        and route.get("credit_account") == item.credit_account
        and route.get("credit_sub_account") == item.credit_sub_account
        and item.vendor_name.casefold() in {
            str(alias).casefold() for alias in route.get("vendor_aliases", []) if str(alias).strip()
        }
        for route in routes
    ):
        raise ValueError("supplier watch route is not configured for the current company")
    normalized = normalize_financial_event(
        {
            "source_event_type": item.supplier_source_type,
            "transaction_date": item.transaction_date,
            "description": item.vendor_name,
            "counterparty": item.vendor_name,
            "amount": item.amount,
            "cash_flow_direction": "outflow",
        }
    )
    watch_hash = stable_hash(
        {
            "supplier_source_type": item.supplier_source_type,
            "vendor_key": normalized["vendor_key"],
            "transaction_date": item.transaction_date,
            "amount_bucket": normalized["amount_bucket"],
            "source_journal_number": item.source_journal_number,
            "moneyforward_journal_id_hash": mask_identifier(item.moneyforward_journal_id),
            "expected_card_account_key": item.expected_card_account_key,
        }
    )
    conn.execute(
        """
        INSERT INTO supplier_primary_card_duplicate_watchlist (
            watch_hash, supplier_source_type, vendor_key, transaction_date,
            amount_bucket, source_journal_number, moneyforward_journal_id_hash,
            voucher_verified, credit_account, credit_sub_account,
            expected_card_account_key, expected_action, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'target_exclude_card_statement', ?)
        ON CONFLICT(watch_hash) DO UPDATE SET
            voucher_verified = excluded.voucher_verified,
            credit_account = excluded.credit_account,
            credit_sub_account = excluded.credit_sub_account,
            expected_card_account_key = excluded.expected_card_account_key,
            status = excluded.status,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (
            watch_hash,
            item.supplier_source_type,
            normalized["vendor_key"],
            item.transaction_date,
            normalized["amount_bucket"],
            item.source_journal_number,
            mask_identifier(item.moneyforward_journal_id),
            1 if item.voucher_verified else 0,
            item.credit_account,
            item.credit_sub_account,
            item.expected_card_account_key,
            item.status,
        ),
    )
    return watch_hash


def supplier_primary_watchlist_status(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM supplier_primary_card_duplicate_watchlist
        GROUP BY status
        ORDER BY status
        """
    ).fetchall()
    return {
        "status": "ready",
        "watch_count_by_status": {str(row["status"]): int(row["count"]) for row in rows},
        "expected_action": "target_exclude_card_statement",
        "raw_journal_ids_stored": False,
        "raw_statement_ids_stored": False,
        "raw_amounts_stored": False,
        "raw_secret_stored": False,
    }
