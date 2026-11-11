from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, ContextManager

from .accounting_api import MoneyForwardAccountingClient
from .hash_utils import mask_identifier, stable_hash
from .journal_write_post import load_json_file
from .local_artifacts import load_journal_id, require_ignored_untracked_data_path
from .transaction_journalize import (
    JOURNAL_CREATE_EXECUTION_CHANNEL,
    TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
    journal_write_request_metadata,
)


@dataclass(frozen=True)
class JournalCreateReadBackVerification:
    operation_type: str
    execution_channel: str
    verified: bool
    idempotency_key: str
    journal_id_hash: str
    expected_hash: str
    actual_hash: str
    voucher_file_ids_hash: str
    response_hash: str
    diff_summary_redacted: str
    moneyforward_api_called: bool
    transaction_link_verified: bool
    transaction_status: str
    tax_readback_verified: bool
    tax_method: str
    journal_number: str | None = None
    tax_amounts_verified: bool = False

    def to_redacted_dict(self) -> dict[str, Any]:
        payload = {
            "operation_type": self.operation_type,
            "execution_channel": self.execution_channel,
            "verified": self.verified,
            "idempotency_key": self.idempotency_key,
            "journal_id_hash": self.journal_id_hash,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "voucher_file_ids_hash": self.voucher_file_ids_hash,
            "response_hash": self.response_hash,
            "diff_summary_redacted": self.diff_summary_redacted,
            "moneyforward_api_called": self.moneyforward_api_called,
            "transaction_link_verified": self.transaction_link_verified,
            "transaction_status": self.transaction_status,
            "tax_readback_verified": self.tax_readback_verified,
            "tax_amounts_verified": self.tax_amounts_verified,
            "tax_method": self.tax_method,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_journal_body_stored": False,
            "raw_api_response_stored": False,
        }
        # A journal number is a display identifier, not the immutable API ID.
        # It is needed for the one-time completion notification identity.
        if self.journal_number:
            payload["journal_number"] = self.journal_number
        return payload


def verify_journal_create_read_back(
    conn: sqlite3.Connection,
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    journal: dict[str, Any],
    post_result: dict[str, Any],
    transaction_journalize_request: dict[str, Any] | None = None,
    office_id: str | None = None,
    expected_tax_values: list[dict[str, Any]] | None = None,
) -> JournalCreateReadBackVerification:
    _require(bool(journal_id), "journal_id is required")
    _validate_post_result(post_result)
    execution_channel = str(post_result.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
    expected_transaction_id = ""
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        if transaction_journalize_request is None:
            raise ValueError("transaction journalize verification requires the exact ignored-local request body")
        metadata = journal_write_request_metadata(transaction_journalize_request, office_id=office_id)
        _require(
            metadata["execution_channel"] == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
            "transaction verification request is not transaction_journalize",
        )
        _require(metadata["request_hash"] == post_result.get("request_hash"), "transaction request hash mismatch")
        expected_transaction_id = str(transaction_journalize_request.get("transaction_id") or "")
    journal_id_hash = mask_identifier(journal_id)
    _require(post_result["moneyforward_journal_id_hash"] == journal_id_hash, "journal_id hash does not match post result")
    idempotency_key = str(post_result["idempotency_key"])
    row = conn.execute(
        """
        SELECT status, moneyforward_journal_id_hash
        FROM write_intents
        WHERE idempotency_key = ?
          AND operation_type = 'journal_create'
        """,
        (idempotency_key,),
    ).fetchone()
    if not row:
        raise ValueError("matching journal_create write_intent is missing")
    if row["status"] not in {"executed", "verified"}:
        raise ValueError("journal_create write_intent is not executed")
    _require(row["moneyforward_journal_id_hash"] == journal_id_hash, "write_intent journal hash mismatch")
    execution = conn.execute(
        """
        SELECT status, moneyforward_journal_id_hash
        FROM journal_create_execution_records
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()
    if execution is not None:
        if execution["status"] not in {"executed", "verified"}:
            raise ValueError("journal execution record requires uncertain read-back reconciliation")
        _require(
            execution["moneyforward_journal_id_hash"] == journal_id_hash,
            "journal execution record journal hash mismatch",
        )

    result = _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=journal,
        idempotency_key=idempotency_key,
        operation_type="journal_create_read_back",
        office_id=office_id,
        execution_channel=execution_channel,
        expected_transaction_id=expected_transaction_id,
        expected_tax_values=expected_tax_values,
    )
    if result.verified and expected_transaction_id:
        transaction_verified, transaction_status, transaction_response_hash = _verify_linked_transaction_read_back(
            client=client,
            transaction_id=expected_transaction_id,
            transaction_date=str(transaction_journalize_request.get("transaction_date") or journal.get("transaction_date") or ""),
            office_id=office_id,
        )
        result = replace(
            result,
            verified=transaction_verified,
            transaction_link_verified=transaction_verified,
            transaction_status=transaction_status,
            response_hash=stable_hash(
                {
                    "journal_response_hash": result.response_hash,
                    "transaction_response_hash": transaction_response_hash,
                }
            ),
            diff_summary_redacted=(
                result.diff_summary_redacted
                if transaction_verified
                else "journal matched but linked transaction did not reach one authoritative posted status"
            ),
        )
    _insert_post_registration_verification(conn, result)
    if result.verified:
        conn.execute(
            """
            UPDATE write_intents
            SET status = 'verified',
                verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ?
              AND operation_type = 'journal_create'
            """,
            (result.idempotency_key,),
        )
        if execution is not None:
            conn.execute(
                """
                UPDATE journal_create_execution_records
                SET status = 'verified',
                    verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                  AND status IN ('executed', 'verified')
                """,
                (result.idempotency_key,),
            )
        if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            conn.execute(
                """
                UPDATE processing_ledger
                SET processing_status = 'verified',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                  AND processing_status IN ('journal_created', 'verified')
                """,
                (result.idempotency_key,),
            )
    conn.commit()
    return result


def verify_journal_create_read_back_with_connection_factory(
    connect: Callable[[], ContextManager[sqlite3.Connection]],
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    journal: dict[str, Any],
    post_result: dict[str, Any],
    transaction_journalize_request: dict[str, Any] | None = None,
    office_id: str | None = None,
    expected_tax_values: list[dict[str, Any]] | None = None,
) -> JournalCreateReadBackVerification:
    """Verify without holding SQLite open across authoritative GETs."""

    _require(bool(journal_id), "journal_id is required")
    _validate_post_result(post_result)
    execution_channel = str(
        post_result.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL
    )
    expected_transaction_id = ""
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        if transaction_journalize_request is None:
            raise ValueError("transaction journalize verification requires the exact ignored-local request body")
        metadata = journal_write_request_metadata(
            transaction_journalize_request,
            office_id=office_id,
        )
        _require(
            metadata["request_hash"] == post_result.get("request_hash"),
            "transaction request hash mismatch",
        )
        expected_transaction_id = str(transaction_journalize_request.get("transaction_id") or "")
    journal_id_hash = mask_identifier(journal_id)
    _require(
        post_result["moneyforward_journal_id_hash"] == journal_id_hash,
        "journal_id hash does not match post result",
    )
    idempotency_key = str(post_result["idempotency_key"])
    with connect() as conn:
        row = conn.execute(
            "SELECT status, moneyforward_journal_id_hash FROM write_intents "
            "WHERE idempotency_key=? AND operation_type='journal_create'",
            (idempotency_key,),
        ).fetchone()
        if not row or row["status"] not in {"executed", "verified"}:
            raise ValueError("journal_create write_intent is not executed")
        _require(row["moneyforward_journal_id_hash"] == journal_id_hash, "write_intent journal hash mismatch")
        execution = conn.execute(
            "SELECT status, moneyforward_journal_id_hash FROM journal_create_execution_records "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if execution is not None:
            _require(execution["status"] in {"executed", "verified"}, "journal execution record requires uncertain read-back reconciliation")
            _require(execution["moneyforward_journal_id_hash"] == journal_id_hash, "journal execution record journal hash mismatch")

    result = _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=journal,
        idempotency_key=idempotency_key,
        operation_type="journal_create_read_back",
        office_id=office_id,
        execution_channel=execution_channel,
        expected_transaction_id=expected_transaction_id,
        expected_tax_values=expected_tax_values,
    )
    if result.verified and expected_transaction_id:
        transaction_verified, transaction_status, transaction_response_hash = (
            _verify_linked_transaction_read_back(
                client=client,
                transaction_id=expected_transaction_id,
                transaction_date=str(
                    (transaction_journalize_request or {}).get("transaction_date")
                    or journal.get("transaction_date")
                    or ""
                ),
                office_id=office_id,
            )
        )
        result = replace(
            result,
            verified=transaction_verified,
            transaction_link_verified=transaction_verified,
            transaction_status=transaction_status,
            response_hash=stable_hash(
                {
                    "journal_response_hash": result.response_hash,
                    "transaction_response_hash": transaction_response_hash,
                }
            ),
            diff_summary_redacted=(
                result.diff_summary_redacted
                if transaction_verified
                else "journal matched but linked transaction did not reach one authoritative posted status"
            ),
        )
    with connect() as conn:
        _insert_post_registration_verification(conn, result)
        if result.verified:
            conn.execute(
                "UPDATE write_intents SET status='verified', "
                "verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE idempotency_key=? AND operation_type='journal_create'",
                (result.idempotency_key,),
            )
            conn.execute(
                "UPDATE journal_create_execution_records SET status='verified', "
                "verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE idempotency_key=? AND status IN ('executed','verified')",
                (result.idempotency_key,),
            )
            if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
                conn.execute(
                    "UPDATE processing_ledger SET processing_status='verified', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE idempotency_key=? AND processing_status IN ('journal_created','verified')",
                    (result.idempotency_key,),
                )
        conn.commit()
    return result


def resolve_uncertain_journal_create_by_read_back(
    conn: sqlite3.Connection,
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    journal: dict[str, Any],
    idempotency_key: str,
    approval_hash: str,
    transaction_journalize_request: dict[str, Any] | None = None,
    office_id: str | None = None,
    expected_tax_values: list[dict[str, Any]] | None = None,
) -> JournalCreateReadBackVerification:
    """Resolve a claimed/uncertain create only through an exact GET read-back.

    This is deliberately the sole path that can advance an abandoned
    ``executing``/``delivery_uncertain`` record. A mismatch is recorded but
    leaves the execution blocked, so it never authorizes an automatic re-POST.
    """
    _require(bool(journal_id), "journal_id is required")
    _require(bool(idempotency_key), "idempotency_key is required")
    _require(bool(approval_hash), "approval_hash is required")
    write_request = transaction_journalize_request or journal
    metadata = journal_write_request_metadata(write_request, office_id=office_id)
    execution_channel = str(metadata["execution_channel"])
    expected_transaction_id = ""
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        expected_transaction_id = str(write_request.get("transaction_id") or "")
        _require(bool(expected_transaction_id), "transaction_id is required for uncertain linked read-back")
    execution = conn.execute(
        """
        SELECT approval_hash, status, request_hash, target_hash,
               moneyforward_journal_id_hash
        FROM journal_create_execution_records
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()
    if execution is None:
        raise ValueError("journal execution record is missing")
    _require(execution["approval_hash"] == approval_hash, "journal execution approval hash mismatch")
    if execution["status"] not in {"executing", "delivery_uncertain"}:
        raise ValueError("journal execution record is not delivery-uncertain")
    _require(execution["request_hash"] == metadata["request_hash"], "journal read-back request hash mismatch")
    _require(execution["target_hash"] == metadata["request_hash"], "journal read-back target hash mismatch")
    journal_id_hash = mask_identifier(journal_id)
    recorded_journal_hash = str(execution["moneyforward_journal_id_hash"] or "")
    if recorded_journal_hash:
        _require(recorded_journal_hash == journal_id_hash, "uncertain execution journal hash mismatch")
    intent = conn.execute(
        """
        SELECT status, request_hash
        FROM write_intents
        WHERE idempotency_key = ? AND operation_type = 'journal_create'
        """,
        (idempotency_key,),
    ).fetchone()
    if intent is None:
        raise ValueError("matching journal_create write_intent is missing")
    _require(intent["status"] == "planned", "uncertain journal write_intent is not planned")
    _require(intent["request_hash"] == metadata["request_hash"], "uncertain journal write_intent request mismatch")

    # An old/orphaned executing state becomes explicitly uncertain before the
    # GET. No write retry is possible while either state exists.
    if execution["status"] == "executing":
        conn.execute(
            """
            UPDATE journal_create_execution_records
            SET status = 'delivery_uncertain',
                uncertainty_reason_code = 'orphaned_executing_requires_readback',
                uncertain_at = COALESCE(uncertain_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            WHERE idempotency_key = ? AND status = 'executing'
            """,
            (idempotency_key,),
        )
        conn.commit()

    result = _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=journal,
        idempotency_key=idempotency_key,
        operation_type="journal_create_uncertain_read_back",
        office_id=office_id,
        execution_channel=execution_channel,
        expected_transaction_id=expected_transaction_id,
        expected_tax_values=expected_tax_values,
    )
    if result.verified and expected_transaction_id:
        transaction_verified, transaction_status, transaction_response_hash = _verify_linked_transaction_read_back(
            client=client,
            transaction_id=expected_transaction_id,
            transaction_date=str(write_request.get("transaction_date") or journal.get("transaction_date") or ""),
            office_id=office_id,
        )
        result = replace(
            result,
            verified=transaction_verified,
            transaction_link_verified=transaction_verified,
            transaction_status=transaction_status,
            response_hash=stable_hash(
                {
                    "journal_response_hash": result.response_hash,
                    "transaction_response_hash": transaction_response_hash,
                }
            ),
            diff_summary_redacted=(
                result.diff_summary_redacted
                if transaction_verified
                else "journal matched but linked transaction did not reach one authoritative posted status"
            ),
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        _insert_post_registration_verification(conn, result)
        if result.verified:
            execution_update = conn.execute(
                """
                UPDATE journal_create_execution_records
                SET status = 'verified',
                    uncertainty_reason_code = NULL,
                    moneyforward_journal_id_hash = ?,
                    executed_at = COALESCE(executed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                  AND approval_hash = ?
                  AND status = 'delivery_uncertain'
                """,
                (journal_id_hash, idempotency_key, approval_hash),
            )
            if execution_update.rowcount != 1:
                raise RuntimeError("uncertain journal execution read-back transition failed")
            intent_update = conn.execute(
                """
                UPDATE write_intents
                SET status = 'verified',
                    moneyforward_journal_id_hash = ?,
                    executed_at = COALESCE(executed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                  AND operation_type = 'journal_create'
                  AND status = 'planned'
                """,
                (journal_id_hash, idempotency_key),
            )
            if intent_update.rowcount != 1:
                raise RuntimeError("uncertain journal write_intent read-back transition failed")
            ledger_update = conn.execute(
                """
                UPDATE processing_ledger
                SET processing_status = 'journal_created',
                    moneyforward_journal_id_hash = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                  AND processing_status = 'planned'
                  AND moneyforward_journal_id_hash IS NULL
                """,
                (journal_id_hash, idempotency_key),
            )
            if ledger_update.rowcount != 1:
                raise RuntimeError("uncertain processing ledger read-back transition failed")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return result


def resolve_uncertain_journal_create_by_read_back_with_connection_factory(
    connect: Callable[[], ContextManager[sqlite3.Connection]],
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    journal: dict[str, Any],
    idempotency_key: str,
    approval_hash: str,
    transaction_journalize_request: dict[str, Any] | None = None,
    office_id: str | None = None,
    expected_tax_values: list[dict[str, Any]] | None = None,
) -> JournalCreateReadBackVerification:
    """GET-only recovery with no SQLite handle open during API traffic."""

    write_request = transaction_journalize_request or journal
    metadata = journal_write_request_metadata(write_request, office_id=office_id)
    execution_channel = str(metadata["execution_channel"])
    expected_transaction_id = str(write_request.get("transaction_id") or "")
    _require(bool(journal_id), "uncertain journal read-back identity is required")
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        _require(bool(expected_transaction_id), "uncertain linked read-back identity is required")
    with connect() as conn:
        execution = conn.execute(
            "SELECT approval_hash,status,request_hash,target_hash,moneyforward_journal_id_hash "
            "FROM journal_create_execution_records WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if execution is None:
            raise ValueError("journal execution record is missing")
        _require(execution["approval_hash"] == approval_hash, "journal execution approval hash mismatch")
        _require(execution["status"] in {"executing", "delivery_uncertain"}, "journal execution record is not delivery-uncertain")
        _require(execution["request_hash"] == metadata["request_hash"], "journal read-back request hash mismatch")
        _require(execution["target_hash"] == metadata["request_hash"], "journal read-back target hash mismatch")
        if execution["moneyforward_journal_id_hash"]:
            _require(execution["moneyforward_journal_id_hash"] == mask_identifier(journal_id), "uncertain execution journal hash mismatch")

    result = _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=journal,
        idempotency_key=idempotency_key,
        operation_type="journal_create_uncertain_read_back",
        office_id=office_id,
        execution_channel=execution_channel,
        expected_transaction_id=expected_transaction_id,
        expected_tax_values=expected_tax_values,
    )
    if result.verified and expected_transaction_id:
        transaction_verified, transaction_status, transaction_response_hash = (
            _verify_linked_transaction_read_back(
                client=client,
                transaction_id=expected_transaction_id,
                transaction_date=str(write_request.get("transaction_date") or journal.get("transaction_date") or ""),
                office_id=office_id,
            )
        )
        result = replace(
            result,
            verified=transaction_verified,
            transaction_link_verified=transaction_verified,
            transaction_status=transaction_status,
            response_hash=stable_hash({"journal_response_hash": result.response_hash, "transaction_response_hash": transaction_response_hash}),
            diff_summary_redacted=(result.diff_summary_redacted if transaction_verified else "journal matched but linked transaction was not authoritatively registered"),
        )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _insert_post_registration_verification(conn, result)
            if result.verified:
                journal_hash = mask_identifier(journal_id)
                execution_update = conn.execute(
                    "UPDATE journal_create_execution_records SET status='verified',uncertainty_reason_code=NULL,moneyforward_journal_id_hash=?,executed_at=COALESCE(executed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE idempotency_key=? AND approval_hash=? AND status IN ('executing','delivery_uncertain')",
                    (journal_hash, idempotency_key, approval_hash),
                )
                if execution_update.rowcount != 1:
                    raise RuntimeError("uncertain journal execution read-back transition failed")
                intent_update = conn.execute(
                    "UPDATE write_intents SET status='verified',moneyforward_journal_id_hash=?,executed_at=COALESCE(executed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE idempotency_key=? AND operation_type='journal_create' AND status='planned'",
                    (journal_hash, idempotency_key),
                )
                if intent_update.rowcount != 1:
                    raise RuntimeError("uncertain journal write_intent read-back transition failed")
                ledger_update = conn.execute(
                    "UPDATE processing_ledger SET processing_status='verified',moneyforward_journal_id_hash=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE idempotency_key=? AND processing_status='planned'",
                    (journal_hash, idempotency_key),
                )
                if ledger_update.rowcount != 1:
                    raise RuntimeError("uncertain processing ledger read-back transition failed")
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return result


def _perform_journal_read_back(
    *,
    client: MoneyForwardAccountingClient,
    journal_id: str,
    journal: dict[str, Any],
    idempotency_key: str,
    operation_type: str,
    office_id: str | None,
    execution_channel: str = JOURNAL_CREATE_EXECUTION_CHANNEL,
    expected_transaction_id: str = "",
    expected_tax_values: list[dict[str, Any]] | None = None,
) -> JournalCreateReadBackVerification:
    journal_id_hash = mask_identifier(journal_id)
    response = client.journal(journal_id, office_id=office_id)
    response_journal = _unwrap_journal(response)
    actual = _normalize_read_back(response_journal)
    transaction_date = str(journal.get("transaction_date") or journal.get("date") or "")[:10]
    tax_method, tax_names_by_id = _read_tax_context(
        client,
        transaction_date=transaction_date,
        office_id=office_id,
    )
    transaction_request_only = (
        execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL
        and bool(journal.get("transaction_id"))
        and not journal.get("branches")
    )
    if transaction_request_only:
        journal_matches = _transaction_journalize_request_matches_read_back(journal, response_journal)
        tax_matches = journal_matches
        expected = {
            "transaction_date": str(journal.get("transaction_date") or "")[:10],
            "transaction_id_hash": mask_identifier(str(journal.get("transaction_id") or "")),
            "selected_side_hash": stable_hash(
                {
                    key: journal.get(key)
                    for key in ("account_id", "sub_account_id", "tax_id", "invoice_kind", "remark")
                    if journal.get(key) not in (None, "")
                }
            ),
        }
    else:
        tax_matches = _tax_read_back_matches(
            journal,
            response_journal,
            tax_method=tax_method,
            tax_names_by_id=tax_names_by_id,
        )
        expected = _align_expected_to_actual(
            _normalize_expected_journal(journal),
            actual,
            allow_missing_tax_id=tax_matches,
        )
        journal_matches = actual == expected
    response_transaction_id = str(response_journal.get("transaction_id") or "")
    link_matches = not expected_transaction_id or response_transaction_id == expected_transaction_id
    amount_matches, expected_amounts, actual_amounts = _tax_amount_read_back(
        response_journal, expected_tax_values
    )
    # Gross equality and a tax ID cannot establish the source net/tax split.
    # Expected tax values are verification-only facts, NOT provider POST fields.
    if expected_tax_values is not None or not amount_matches:
        expected["tax_values"] = expected_amounts
        actual["tax_values"] = actual_amounts
    verified = journal_matches and tax_matches and link_matches and amount_matches
    if not journal_matches and transaction_request_only:
        diff = "transaction journalize read-back did not match the bound request side"
    elif not journal_matches:
        diff = _diff_summary(expected, actual)
    elif not amount_matches:
        diff = "independent source tax amounts missing, invalid or different from read-back"
    elif not tax_matches:
        diff = "journal body matched except tax identity read-back did not match current term policy"
    elif not link_matches:
        diff = "journal body matched but transaction_id linkage did not match"
    else:
        diff = "read-back matched journal create body"
    voucher_ids = _extract_voucher_file_ids(response_journal)
    return JournalCreateReadBackVerification(
        operation_type=operation_type,
        execution_channel=execution_channel,
        verified=verified,
        idempotency_key=idempotency_key,
        journal_id_hash=journal_id_hash,
        expected_hash=stable_hash(expected),
        actual_hash=stable_hash(actual),
        voucher_file_ids_hash=stable_hash(voucher_ids),
        response_hash=stable_hash(
            {
                "keys": sorted(response.keys()),
                "actual_hash": stable_hash(actual),
                "tax_method": tax_method,
                "tax_readback_verified": tax_matches and amount_matches,
            }
        ),
        diff_summary_redacted=diff,
        moneyforward_api_called=True,
        transaction_link_verified=link_matches if expected_transaction_id else False,
        transaction_status="journal_linked" if expected_transaction_id and link_matches else "not_applicable",
        tax_readback_verified=tax_matches and amount_matches,
        tax_method=tax_method,
        journal_number=_extract_journal_number(response_journal),
        tax_amounts_verified=expected_tax_values is not None and amount_matches,
    )


def _verify_linked_transaction_read_back(
    *,
    client: MoneyForwardAccountingClient,
    transaction_id: str,
    transaction_date: str,
    office_id: str | None,
) -> tuple[bool, str, str]:
    _require(bool(transaction_id), "transaction_id is required for linked read-back")
    _require(bool(transaction_date), "transaction date is required for linked read-back")
    posted_statuses = ("registered", "modified", "new_voucher_attached")
    matches: list[dict[str, Any]] = []
    page = 1
    response_hashes: list[str] = []
    query_start_date = (date.fromisoformat(transaction_date[:10]) - timedelta(days=40)).isoformat()
    while True:
        payload = client.transactions(
            office_id,
            start_date=query_start_date,
            end_date=transaction_date,
            journalizing_statuses=posted_statuses,
            page=page,
            per_page=500,
        )
        response_hashes.append(stable_hash(payload))
        items = payload.get("transactions") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("linked transaction read-back has no transactions array")
        matches.extend(
            item for item in items if isinstance(item, dict) and str(item.get("id") or "") == transaction_id
        )
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        total_pages = int(metadata.get("total_pages") or 0) if isinstance(metadata, dict) else 0
        if page >= total_pages or (not total_pages and len(items) < 500):
            break
        page += 1
        if page > 100:
            raise RuntimeError("linked transaction read-back exceeds the 100-page safety limit")
    if len(matches) != 1:
        return False, "missing_or_duplicate", stable_hash(response_hashes)
    status = str(matches[0].get("journalizing_status") or "")
    return status in posted_statuses, status, stable_hash(response_hashes)


def _insert_post_registration_verification(
    conn: sqlite3.Connection,
    result: JournalCreateReadBackVerification,
) -> None:
    conn.execute(
        """
        INSERT INTO post_registration_verifications (
            idempotency_key, moneyforward_journal_id_hash, expected_hash,
            actual_hash, voucher_file_ids_hash, passed, diff_summary_redacted
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.idempotency_key,
            result.journal_id_hash,
            result.expected_hash,
            result.actual_hash,
            result.voucher_file_ids_hash,
            1 if result.verified else 0,
            result.diff_summary_redacted,
        ),
    )


def load_journal_verify_inputs(
    *,
    journal_id_json: Path,
    journal_json: Path,
    post_result_json: Path,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    require_ignored_untracked_data_path(journal_id_json, purpose="journal_id input")
    require_ignored_untracked_data_path(journal_json, purpose="journal create body input")
    require_ignored_untracked_data_path(post_result_json, purpose="journal post result input")
    journal_id = load_journal_id(None, journal_id_json)
    journal = load_json_file(journal_json)
    post_result = load_json_file(post_result_json)
    return journal_id, journal, post_result


def _validate_post_result(post_result: dict[str, Any]) -> None:
    forbidden = {"journal_id", "moneyforward_journal_id", "raw_api_response", "api_response", "response"}
    present = sorted(forbidden.intersection(post_result))
    if present:
        raise ValueError(f"post result contains forbidden raw fields: {', '.join(present)}")
    for key, value in post_result.items():
        if key.startswith("raw_") and value is not False:
            raise ValueError(f"post result {key} must be false")
    required = ("operation_type", "executed", "idempotency_key", "moneyforward_journal_id_hash")
    missing = [key for key in required if post_result.get(key) in ("", None)]
    if missing:
        raise ValueError(f"post result missing fields: {', '.join(missing)}")
    _require(post_result["operation_type"] == "journal_create", "post result operation_type mismatch")
    _require(post_result["executed"] is True, "post result is not executed")
    execution_channel = str(post_result.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
    _require(
        execution_channel in {JOURNAL_CREATE_EXECUTION_CHANNEL, TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL},
        "post result execution_channel mismatch",
    )
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        _require(bool(post_result.get("request_hash")), "direct post result request_hash is required")


def _tax_amount_read_back(actual_journal, expected_tax_values):
    """Compare source-approved per-branch tax amounts without inferring a tax rate.

    Pass [{"debitor": source_tax, "creditor": source_tax}, ...] from the
    independently approved evidence/plan, never copied from this GET response.
    Nonzero provider tax amounts without that source witness fail closed.
    No tax calculation, rounding policy or tenant defaults are invented here.
    """
    from decimal import Decimal, InvalidOperation

    def number(value):
        if isinstance(value, bool) or value in (None, ""):
            raise ValueError("tax amount not explicit")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("invalid tax amount") from error
        if not result.is_finite():
            raise ValueError("non-finite tax amount")
        return format(result.normalize(), "f")

    branches = (actual_journal.get("branches") or actual_journal.get("journal_details")
                or actual_journal.get("details") or [])
    observed = []
    try:
        for branch in branches:
            pair = {}
            for name, alias in (("debitor", "debit"), ("creditor", "credit")):
                side = branch.get(name) or branch.get(alias) or {}
                value = side.get("tax_value")
                pair[name] = number(value) if value not in (None, "") else None
            observed.append(pair)
        if expected_tax_values is None:
            safe_without_amount_claim = all(v in (None, "0") for row in observed for v in row.values())
            return safe_without_amount_claim, "independent_source_tax_values_required", observed
        if not isinstance(expected_tax_values, list) or len(expected_tax_values) != len(observed):
            return False, "invalid_source_tax_shape", observed
        expected = []
        for row in expected_tax_values:
            if not isinstance(row, dict) or set(row) != {"debitor", "creditor"}:
                return False, "invalid_source_tax_shape", observed
            expected.append({name: number(row[name]) for name in ("debitor", "creditor")})
        return expected == observed, expected, observed
    except (ValueError, TypeError, AttributeError):
        return False, "invalid_tax_amount", observed


def _normalize_expected_journal(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_date": str(journal.get("transaction_date") or journal.get("date") or "")[:10],
        "journal_type": str(journal.get("journal_type") or ""),
        "branches": [_normalize_expected_branch(branch) for branch in journal.get("branches", [])],
    }


def _transaction_journalize_request_matches_read_back(
    request: dict[str, Any], response_journal: dict[str, Any]
) -> bool:
    if str(response_journal.get("transaction_id") or "") != str(request.get("transaction_id") or ""):
        return False
    if str(response_journal.get("transaction_date") or "")[:10] != str(request.get("transaction_date") or "")[:10]:
        return False
    branches = response_journal.get("branches") or []
    if len(branches) != 1 or not isinstance(branches[0], dict):
        return False
    branch = branches[0]
    if str(branch.get("remark") or "") != str(request.get("remark") or ""):
        return False
    required = {
        key: request.get(key)
        for key in ("account_id", "sub_account_id", "tax_id", "invoice_kind")
        if request.get(key) not in (None, "")
    }
    for side_name in ("debitor", "creditor"):
        side = branch.get(side_name)
        if isinstance(side, dict) and all(str(side.get(key) or "") == str(value) for key, value in required.items()):
            return True
    return False


def _normalize_expected_branch(branch: dict[str, Any]) -> dict[str, Any]:
    return {
        "remark": str(branch.get("remark") or branch.get("description") or ""),
        "debitor": _normalize_expected_side(branch.get("debitor") or {}),
        "creditor": _normalize_expected_side(branch.get("creditor") or {}),
    }


def _normalize_expected_side(side: dict[str, Any]) -> dict[str, Any]:
    keys = ("account_id", "sub_account_id", "tax_id", "department_id", "trade_partner_code", "value")
    normalized = {key: side.get(key) for key in keys if side.get(key) not in (None, "")}
    if "value" in normalized:
        normalized["value"] = _normalize_number(normalized["value"])
    return normalized


def _normalize_read_back(journal: dict[str, Any]) -> dict[str, Any]:
    branches = journal.get("branches") or journal.get("journal_details") or journal.get("details") or []
    return {
        "transaction_date": str(journal.get("transaction_date") or journal.get("date") or "")[:10],
        "journal_type": str(journal.get("journal_type") or ""),
        "branches": [_normalize_actual_branch(branch) for branch in branches],
    }


def _normalize_actual_branch(branch: dict[str, Any]) -> dict[str, Any]:
    return {
        "remark": str(branch.get("remark") or branch.get("description") or branch.get("memo") or ""),
        "debitor": _normalize_actual_side(branch.get("debitor") or branch.get("debit") or branch),
        "creditor": _normalize_actual_side(branch.get("creditor") or branch.get("credit") or branch),
    }


def _normalize_actual_side(side: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "account_id": ("account_id", "account_item_id", "accountId"),
        "sub_account_id": ("sub_account_id", "sub_account_item_id", "subAccountId"),
        "tax_id": ("tax_id", "taxId"),
        "department_id": ("department_id", "departmentId"),
        "trade_partner_code": ("trade_partner_code", "partner_code"),
        "value": ("value", "amount"),
    }
    normalized: dict[str, Any] = {}
    for key, candidates in mapping.items():
        value = _first_present(side, candidates)
        if value not in (None, ""):
            if key == "tax_id" and _is_target_exempt_tax(side):
                continue
            if key == "value":
                value = _read_back_gross_value(side, value)
            normalized[key] = value
    return normalized


def _align_expected_to_actual(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    allow_missing_tax_id: bool,
) -> dict[str, Any]:
    aligned = json.loads(json.dumps(expected))
    expected_branches = aligned.get("branches") or []
    actual_branches = actual.get("branches") or []
    for expected_branch, actual_branch in zip(expected_branches, actual_branches):
        for side_name in ("debitor", "creditor"):
            expected_side = expected_branch.get(side_name) or {}
            actual_side = actual_branch.get(side_name) or {}
            if allow_missing_tax_id and "tax_id" in expected_side and "tax_id" not in actual_side:
                expected_side.pop("tax_id", None)
    return aligned


def _read_tax_context(
    client: MoneyForwardAccountingClient,
    *,
    transaction_date: str,
    office_id: str | None,
) -> tuple[str, dict[str, str]]:
    tax_method = "UNKNOWN"
    term_reader = getattr(client, "term_settings", None)
    if callable(term_reader):
        try:
            payload = term_reader(office_id)
            tax_method = _tax_method_for_date(payload, transaction_date)
        except Exception:
            tax_method = "UNKNOWN"
    tax_names_by_id: dict[str, str] = {}
    tax_reader = getattr(client, "taxes", None)
    if callable(tax_reader):
        try:
            tax_names_by_id = _tax_names_by_id(tax_reader(office_id))
        except Exception:
            tax_names_by_id = {}
    return tax_method, tax_names_by_id


def _tax_read_back_matches(
    expected_journal: dict[str, Any],
    actual_journal: dict[str, Any],
    *,
    tax_method: str,
    tax_names_by_id: dict[str, str],
) -> bool:
    expected_branches = expected_journal.get("branches") or []
    actual_branches = (
        actual_journal.get("branches")
        or actual_journal.get("journal_details")
        or actual_journal.get("details")
        or []
    )
    if len(expected_branches) != len(actual_branches):
        return False
    for expected_branch, actual_branch in zip(expected_branches, actual_branches):
        if not isinstance(expected_branch, dict) or not isinstance(actual_branch, dict):
            return False
        for side_name, alias in (("debitor", "debit"), ("creditor", "credit")):
            expected_side = expected_branch.get(side_name) or {}
            actual_side = actual_branch.get(side_name) or actual_branch.get(alias) or {}
            expected_tax_id = str(expected_side.get("tax_id") or "")
            if not expected_tax_id:
                continue
            if tax_method == "FREE":
                continue
            actual_tax_id = str(actual_side.get("tax_id") or actual_side.get("taxId") or "")
            if actual_tax_id and actual_tax_id == expected_tax_id:
                continue
            expected_tax_name = tax_names_by_id.get(expected_tax_id, "")
            actual_tax_name = str(actual_side.get("tax_name") or actual_side.get("tax_long_name") or "")
            if actual_tax_id and not actual_tax_name:
                actual_tax_name = tax_names_by_id.get(actual_tax_id, "")
            if not expected_tax_name or _normalize_tax_name(expected_tax_name) != _normalize_tax_name(actual_tax_name):
                return False
    return True


def _tax_method_for_date(payload: Any, transaction_date: str) -> str:
    try:
        target = date.fromisoformat(transaction_date)
    except ValueError:
        return "UNKNOWN"
    for item in _nested_dicts(payload):
        try:
            start = date.fromisoformat(str(item.get("start_date") or ""))
            end = date.fromisoformat(str(item.get("end_date") or ""))
        except ValueError:
            continue
        if start <= target <= end:
            method = str(item.get("tax_method") or "UNKNOWN")
            return method if method in {"FREE", "SIMPLE", "PROPORTIONAL_ALLOCATION", "INDIVIDUAL_ALLOCATION"} else "UNKNOWN"
    return "UNKNOWN"


def _tax_names_by_id(payload: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _nested_dicts(payload):
        tax_id = str(item.get("id") or item.get("tax_id") or "")
        tax_name = str(item.get("name") or item.get("tax_name") or item.get("long_name") or "")
        if tax_id and tax_name:
            result[tax_id] = tax_name
    return result


def _nested_dicts(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        result.append(payload)
        for value in payload.values():
            result.extend(_nested_dicts(value))
    elif isinstance(payload, list):
        for value in payload:
            result.extend(_nested_dicts(value))
    return result


def _normalize_tax_name(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _is_target_exempt_tax(side: dict[str, Any]) -> bool:
    tax_name = str(side.get("tax_name") or side.get("tax_long_name") or "")
    return tax_name == "対象外"


def _read_back_gross_value(side: dict[str, Any], value: Any) -> Any:
    amount = _normalize_number(value)
    tax_value = _normalize_number(side.get("tax_value"))
    if tax_value in (None, ""):
        return amount
    try:
        if float(tax_value) == 0:
            return amount
        return _normalize_number(float(amount) + float(tax_value))
    except (TypeError, ValueError):
        return amount


def _normalize_number(value: Any) -> Any:
    if value in (None, ""):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        stripped = value.replace(",", "").strip()
        if not stripped:
            return value
        try:
            parsed = float(stripped)
        except ValueError:
            return value
        return int(parsed) if parsed.is_integer() else parsed
    return value


def _unwrap_journal(response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("data"), dict):
        return response["data"]
    if isinstance(response.get("journal"), dict):
        return response["journal"]
    return response


def _extract_voucher_file_ids(journal: dict[str, Any]) -> list[str]:
    values = journal.get("voucher_file_ids") or []
    if not isinstance(values, list):
        return []
    return sorted(str(value.get("file_id") or value.get("id") or value) if isinstance(value, dict) else str(value) for value in values)


def _extract_journal_number(journal: dict[str, Any]) -> str | None:
    for key in ("journal_number", "number", "display_number"):
        value = str(journal.get(key) or "").strip()
        if value:
            return value
    return None


def _diff_summary(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    changed = [key for key in ("transaction_date", "journal_type", "branches") if expected.get(key) != actual.get(key)]
    return "read-back mismatch fields: " + ",".join(changed)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
