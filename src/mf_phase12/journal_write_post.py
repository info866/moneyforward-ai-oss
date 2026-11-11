from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager

from .accounting_api import (
    MoneyForwardAccountingClient,
    MoneyForwardRequestRejectedError,
    WriteExecutionGrant,
    extract_moneyforward_journal_id,
)
from .hash_utils import mask_identifier, stable_hash
from .journal_write_grant import _load_object
from .local_artifacts import require_ignored_untracked_data_path
from .transaction_journalize import (
    JOURNAL_CREATE_EXECUTION_CHANNEL,
    TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
    expected_api_action,
    journal_write_request_metadata,
    transaction_journalize_binding_proof,
    verify_transaction_journalize_precondition,
)


class JournalCreateDeliveryUncertainError(RuntimeError):
    """Raised when retrying could duplicate an external journal write."""


@dataclass(frozen=True)
class JournalCreateExecutionDryRun:
    operation_type: str
    execution_channel: str
    expected_api_action: str
    ready_to_post: bool
    executed: bool
    idempotency_key: str
    request_hash: str
    target_hash: str
    approval_hash: str
    scope_hash: str
    body_hash: str
    dry_run_hash: str

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "execution_channel": self.execution_channel,
            "expected_api_action": self.expected_api_action,
            "ready_to_post": self.ready_to_post,
            "executed": self.executed,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "approval_hash": self.approval_hash,
            "scope_hash": self.scope_hash,
            "body_hash": self.body_hash,
            "dry_run_hash": self.dry_run_hash,
            "moneyforward_api_called": False,
            "raw_secret_stored": False,
            "raw_journal_body_stored": False,
            "raw_api_response_stored": False,
        }


@dataclass(frozen=True)
class JournalCreatePostResult:
    operation_type: str
    execution_channel: str
    executed: bool
    already_executed: bool
    idempotency_key: str
    request_hash: str
    target_hash: str
    approval_hash: str
    response_hash: str
    moneyforward_journal_id_hash: str
    moneyforward_journal_id: str
    moneyforward_api_called: bool
    transaction_precondition_hash: str = ""

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "execution_channel": self.execution_channel,
            "executed": self.executed,
            "already_executed": self.already_executed,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "approval_hash": self.approval_hash,
            "response_hash": self.response_hash,
            "moneyforward_journal_id_hash": self.moneyforward_journal_id_hash,
            "moneyforward_api_called": self.moneyforward_api_called,
            "transaction_precondition_hash": self.transaction_precondition_hash,
            "raw_secret_stored": False,
            "raw_journal_id_stored": False,
            "raw_journal_body_stored": False,
            "raw_api_response_stored": False,
        }

    def to_journal_id_dict(self) -> dict[str, Any]:
        return {
            "journal_id": self.moneyforward_journal_id,
            "journal_id_hash": self.moneyforward_journal_id_hash,
            "raw_journal_id_git_tracked": False,
            "storage": "ignored_local_data_phase12_only",
        }


@dataclass(frozen=True)
class _JournalCreateClaim:
    claim_token_hash: str
    already_executed_journal_hash: str = ""


def load_journal_create_execution_inputs(
    *,
    grant_json: Path,
    journal_json: Path,
    conn: sqlite3.Connection,
    office_id: str | None = None,
) -> JournalCreateExecutionDryRun:
    grant_json = require_ignored_untracked_data_path(grant_json, purpose="journal write grant input")
    journal_json = require_ignored_untracked_data_path(journal_json, purpose="journal write request input")
    return dry_run_journal_create_execution(
        grant=_load_object(grant_json, "journal write grant"),
        journal=_load_object(journal_json, "journal write request"),
        conn=conn,
        office_id=office_id,
    )


def dry_run_journal_create_execution(
    *,
    grant: dict[str, Any],
    journal: dict[str, Any],
    conn: sqlite3.Connection,
    office_id: str | None = None,
) -> JournalCreateExecutionDryRun:
    _require(grant.get("operation_type") == "journal_create", "grant operation_type mismatch")
    _require(grant.get("write_execution_grant_ready") is True, "grant is not ready")
    for field in ("approval_hash", "scope_hash", "target_hash", "request_hash", "idempotency_key"):
        _require(bool(grant.get(field)), f"grant {field} is required")
    _require(grant["target_hash"] == grant["request_hash"], "grant target_hash and request_hash must match")

    metadata = journal_write_request_metadata(journal, office_id=office_id)
    execution_channel = str(metadata["execution_channel"])
    grant_channel = str(grant.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
    _require(grant_channel == execution_channel, "grant execution_channel does not match request body")
    _require(
        str(grant.get("expected_api_action") or expected_api_action(grant_channel))
        == expected_api_action(execution_channel),
        "grant expected_api_action does not match request body",
    )
    _require(metadata["request_hash"] == grant["target_hash"], "journal body request_hash does not match grant target")

    row = conn.execute(
        """
        SELECT status, request_hash
        FROM write_intents
        WHERE idempotency_key = ?
          AND operation_type = 'journal_create'
        """,
        (str(grant["idempotency_key"]),),
    ).fetchone()
    if not row:
        raise ValueError("matching journal_create write_intent is missing")
    if row["status"] == "executed":
        raise ValueError("matching journal_create write_intent is already executed")
    if row["status"] != "planned":
        raise ValueError("matching journal_create write_intent is not planned")
    _require(row["request_hash"] == grant["target_hash"], "write_intent request_hash does not match grant target")

    approval = conn.execute(
        """
        SELECT scope_hash, revoked_at
        FROM approval_records
        WHERE approval_hash = ?
          AND idempotency_key = ?
          AND operation_type = 'journal_create'
        """,
        (str(grant["approval_hash"]), str(grant["idempotency_key"])),
    ).fetchone()
    if not approval:
        raise ValueError("matching SDK approval record is missing")
    if approval["revoked_at"]:
        raise ValueError("matching SDK approval record is revoked")
    _require(approval["scope_hash"] == grant["scope_hash"], "approval scope_hash does not match grant")

    dry_payload: dict[str, Any] = {
        "operation_type": execution_channel,
        "idempotency_key": grant["idempotency_key"],
        "approval_hash": grant["approval_hash"],
        "scope_hash": grant["scope_hash"],
        "target_hash": grant["target_hash"],
        "body_hash": metadata["body_hash"],
    }
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        dry_payload.update(transaction_journalize_binding_proof(grant))
    dry_hash = stable_hash(dry_payload)
    return JournalCreateExecutionDryRun(
        operation_type="journal_create",
        execution_channel=execution_channel,
        expected_api_action=expected_api_action(execution_channel),
        ready_to_post=True,
        executed=False,
        idempotency_key=str(grant["idempotency_key"]),
        request_hash=str(grant["request_hash"]),
        target_hash=str(grant["target_hash"]),
        approval_hash=str(grant["approval_hash"]),
        scope_hash=str(grant["scope_hash"]),
        body_hash=str(metadata["body_hash"]),
        dry_run_hash=dry_hash,
    )


def execute_journal_create_with_client(
    *,
    conn: sqlite3.Connection,
    client: MoneyForwardAccountingClient,
    dry_run: JournalCreateExecutionDryRun,
    grant: dict[str, Any],
    journal: dict[str, Any],
    execution_approval: dict[str, Any],
    office_id: str | None = None,
) -> JournalCreatePostResult:
    _require(dry_run.ready_to_post is True, "dry_run must be ready_to_post")
    _require(dry_run.executed is False, "dry_run must not already be executed")
    require_journal_create_execution_approval_details(
        execution_approval=execution_approval,
        dry_run=dry_run,
        grant=grant,
    )

    existing = _execution_record(conn, dry_run.idempotency_key)
    if existing is not None:
        _require_execution_record_matches(existing, dry_run)
        status = str(existing["status"])
        if status in {"executing", "delivery_uncertain"}:
            raise JournalCreateDeliveryUncertainError(
                "journal delivery is uncertain; automatic re-POST is blocked until authoritative read-back"
            )
        if status in {"executed", "verified"}:
            journal_hash = str(existing["moneyforward_journal_id_hash"] or "")
            _require(bool(journal_hash), "executed journal claim has no journal hash")
            return _already_executed_result(dry_run, journal_hash)

    current = conn.execute(
        """
        SELECT status, moneyforward_journal_id_hash
        FROM write_intents
        WHERE idempotency_key = ? AND operation_type = 'journal_create'
        """,
        (dry_run.idempotency_key,),
    ).fetchone()
    if not current:
        raise ValueError("matching journal_create write_intent is missing")
    if current["status"] in {"executed", "verified"}:
        journal_hash = str(current["moneyforward_journal_id_hash"] or "")
        _require(bool(journal_hash), "executed journal intent has no journal hash")
        return _already_executed_result(dry_run, journal_hash)

    immediate = dry_run_journal_create_execution(grant=grant, journal=journal, conn=conn, office_id=office_id)
    _require(immediate.dry_run_hash == dry_run.dry_run_hash, "immediate dry_run_hash mismatch")
    claim = _claim_execution(conn, dry_run=dry_run, execution_approval=execution_approval)
    if claim.already_executed_journal_hash:
        return _already_executed_result(dry_run, claim.already_executed_journal_hash)

    transaction_precondition_hash = ""
    try:
        if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            transaction_precondition_hash = verify_transaction_journalize_precondition(
                client=client,
                request=journal,
                binding_proof=grant,
                office_id=office_id,
            )
    except BaseException:
        _release_claim_before_delivery(conn, dry_run=dry_run, claim_token_hash=claim.claim_token_hash)
        raise

    api_grant = WriteExecutionGrant(
        operation_type=dry_run.execution_channel,
        approval_hash=dry_run.approval_hash,
        scope_hash=dry_run.scope_hash,
        target_hash=dry_run.target_hash,
    )
    try:
        if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            response = client.journalize_transaction(request=journal, office_id=office_id, grant=api_grant)
        else:
            response = client.create_journal(journal=journal, office_id=office_id, grant=api_grant)
        journal_id = (
            _extract_linked_journal_id(response)
            if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL
            else extract_moneyforward_journal_id(response)
        )
    except MoneyForwardRequestRejectedError:
        _mark_request_rejected(conn, dry_run=dry_run, claim_token_hash=claim.claim_token_hash)
        raise
    except BaseException:
        _mark_delivery_uncertain(
            conn,
            dry_run=dry_run,
            claim_token_hash=claim.claim_token_hash,
            reason_code="transport_or_response_result_unknown",
        )
        raise

    journal_hash = mask_identifier(journal_id)
    try:
        _mark_journal_created(
            conn,
            dry_run=dry_run,
            claim_token_hash=claim.claim_token_hash,
            journal_hash=journal_hash,
        )
    except BaseException:
        _mark_delivery_uncertain(
            conn,
            dry_run=dry_run,
            claim_token_hash=claim.claim_token_hash,
            reason_code="local_persistence_failed_after_success_response",
            journal_hash=journal_hash,
        )
        raise
    return JournalCreatePostResult(
        operation_type="journal_create",
        execution_channel=dry_run.execution_channel,
        executed=True,
        already_executed=False,
        idempotency_key=dry_run.idempotency_key,
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        approval_hash=dry_run.approval_hash,
        response_hash=stable_hash(
            {
                "response": _redact_response(response, journal_hash),
                "transaction_precondition_hash": transaction_precondition_hash,
            }
        ),
        moneyforward_journal_id_hash=journal_hash,
        moneyforward_journal_id=journal_id,
        moneyforward_api_called=True,
        transaction_precondition_hash=transaction_precondition_hash,
    )


def execute_journal_create_with_connection_factory(
    *,
    connect: Callable[[], ContextManager[sqlite3.Connection]],
    client: MoneyForwardAccountingClient,
    dry_run: JournalCreateExecutionDryRun,
    grant: dict[str, Any],
    journal: dict[str, Any],
    execution_approval: dict[str, Any],
    office_id: str | None = None,
) -> JournalCreatePostResult:
    """Execute without holding SQLite open across API traffic."""

    _require(dry_run.ready_to_post is True, "dry_run must be ready_to_post")
    _require(dry_run.executed is False, "dry_run must not already be executed")
    require_journal_create_execution_approval_details(
        execution_approval=execution_approval,
        dry_run=dry_run,
        grant=grant,
    )
    with connect() as conn:
        existing = _execution_record(conn, dry_run.idempotency_key)
        if existing is not None:
            _require_execution_record_matches(existing, dry_run)
            status = str(existing["status"])
            if status in {"executing", "delivery_uncertain"}:
                raise JournalCreateDeliveryUncertainError(
                    "journal delivery is uncertain; automatic re-POST is blocked until authoritative read-back"
                )
            if status in {"executed", "verified"}:
                journal_hash = str(existing["moneyforward_journal_id_hash"] or "")
                _require(bool(journal_hash), "executed journal claim has no journal hash")
                return _already_executed_result(dry_run, journal_hash)
        immediate = dry_run_journal_create_execution(
            grant=grant,
            journal=journal,
            conn=conn,
            office_id=office_id,
        )
        _require(immediate.dry_run_hash == dry_run.dry_run_hash, "immediate dry_run_hash mismatch")
        claim = _claim_execution(
            conn,
            dry_run=dry_run,
            execution_approval=execution_approval,
        )
    if claim.already_executed_journal_hash:
        return _already_executed_result(dry_run, claim.already_executed_journal_hash)

    transaction_precondition_hash = ""
    try:
        if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            transaction_precondition_hash = verify_transaction_journalize_precondition(
                client=client,
                request=journal,
                binding_proof=grant,
                office_id=office_id,
            )
    except BaseException:
        with connect() as conn:
            _release_claim_before_delivery(
                conn,
                dry_run=dry_run,
                claim_token_hash=claim.claim_token_hash,
            )
        raise

    api_grant = WriteExecutionGrant(
        operation_type=dry_run.execution_channel,
        approval_hash=dry_run.approval_hash,
        scope_hash=dry_run.scope_hash,
        target_hash=dry_run.target_hash,
    )
    try:
        if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            response = client.journalize_transaction(
                request=journal,
                office_id=office_id,
                grant=api_grant,
            )
        else:
            response = client.create_journal(
                journal=journal,
                office_id=office_id,
                grant=api_grant,
            )
        journal_id = (
            _extract_linked_journal_id(response)
            if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL
            else extract_moneyforward_journal_id(response)
        )
    except MoneyForwardRequestRejectedError:
        with connect() as conn:
            _mark_request_rejected(
                conn,
                dry_run=dry_run,
                claim_token_hash=claim.claim_token_hash,
            )
        raise
    except BaseException:
        with connect() as conn:
            _mark_delivery_uncertain(
                conn,
                dry_run=dry_run,
                claim_token_hash=claim.claim_token_hash,
                reason_code="transport_or_response_result_unknown",
            )
        raise

    journal_hash = mask_identifier(journal_id)
    with connect() as conn:
        _mark_journal_created(
            conn,
            dry_run=dry_run,
            claim_token_hash=claim.claim_token_hash,
            journal_hash=journal_hash,
        )
    return JournalCreatePostResult(
        operation_type="journal_create",
        execution_channel=dry_run.execution_channel,
        executed=True,
        already_executed=False,
        idempotency_key=dry_run.idempotency_key,
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        approval_hash=dry_run.approval_hash,
        response_hash=stable_hash(
            {
                "response": _redact_response(response, journal_hash),
                "transaction_precondition_hash": transaction_precondition_hash,
            }
        ),
        moneyforward_journal_id_hash=journal_hash,
        moneyforward_journal_id=journal_id,
        moneyforward_api_called=True,
        transaction_precondition_hash=transaction_precondition_hash,
    )


def _extract_linked_journal_id(response: dict[str, Any]) -> str:
    """Never mistake the transaction resource ID for the created journal ID."""

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    journal = response.get("journal") if isinstance(response.get("journal"), dict) else {}
    data_journal = data.get("journal") if isinstance(data.get("journal"), dict) else {}
    for candidate in (
        response.get("journal_id"),
        journal.get("id"),
        data.get("journal_id"),
        data_journal.get("id"),
    ):
        if candidate:
            return str(candidate)
    raise RuntimeError("transaction journalize response did not contain an explicit journal id")


def load_json_file(path: Path) -> dict[str, Any]:
    return _load_object(path, "JSON file")


def require_journal_create_execution_approval_details(
    *,
    execution_approval: dict[str, Any],
    dry_run: JournalCreateExecutionDryRun,
    grant: dict[str, Any],
) -> None:
    _require(execution_approval.get("operation_type") == "journal_create", "execution approval operation mismatch")
    _require(execution_approval.get("approval_hash") == dry_run.approval_hash, "execution approval_hash mismatch")
    _require(execution_approval.get("scope_hash") == dry_run.scope_hash, "execution scope_hash mismatch")
    _require(execution_approval.get("request_hash") == dry_run.request_hash, "execution request_hash mismatch")
    _require(execution_approval.get("target_hash") == dry_run.target_hash, "execution target_hash mismatch")
    _require(execution_approval.get("dry_run_hash") == dry_run.dry_run_hash, "execution dry_run_hash mismatch")
    _require(grant.get("approval_hash") == dry_run.approval_hash, "grant approval_hash mismatch")
    _require(grant.get("scope_hash") == dry_run.scope_hash, "grant scope_hash mismatch")
    _require(
        str(grant.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL) == dry_run.execution_channel,
        "grant execution_channel mismatch",
    )
    action = str(execution_approval.get("expected_api_action") or "")
    rollback = str(execution_approval.get("rollback_method") or "")
    required_action = expected_api_action(dry_run.execution_channel)
    _require(required_action in action, f"execution approval must name {required_action}")
    _require("GET /api/v3/journals/{id}" in action, "execution approval must name journal read-back")
    if dry_run.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        _require(
            "GET /api/v3/transactions" in action,
            "transaction execution approval must name pre/post transaction read-back",
        )
    _require("separate explicit approval" in rollback, "rollback method must require separate explicit approval")
    _require(
        "journal_id" in rollback or "reversal" in rollback or "delete" in rollback,
        "rollback method must mention journal_id, reversal, or delete",
    )


def _execution_record(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT approval_hash, idempotency_key, request_hash, target_hash, dry_run_hash,
               scope_hash, claim_token_hash, execution_channel, status,
               moneyforward_journal_id_hash
        FROM journal_create_execution_records
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()


def _require_execution_record_matches(row: sqlite3.Row, dry_run: JournalCreateExecutionDryRun) -> None:
    for field, expected in (
        ("approval_hash", dry_run.approval_hash),
        ("idempotency_key", dry_run.idempotency_key),
        ("request_hash", dry_run.request_hash),
        ("target_hash", dry_run.target_hash),
        ("dry_run_hash", dry_run.dry_run_hash),
        ("scope_hash", dry_run.scope_hash),
        ("execution_channel", dry_run.execution_channel),
    ):
        _require(str(row[field]) == expected, f"journal execution record {field} mismatch")


def _claim_execution(
    conn: sqlite3.Connection,
    *,
    dry_run: JournalCreateExecutionDryRun,
    execution_approval: dict[str, Any],
) -> _JournalCreateClaim:
    if conn.in_transaction:
        raise RuntimeError("journal execution claim requires a clean committed database transaction")
    claim_token_hash = stable_hash({"claim_token": secrets.token_urlsafe(48)})
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _execution_record(conn, dry_run.idempotency_key)
        if existing is not None:
            _require_execution_record_matches(existing, dry_run)
            status = str(existing["status"])
            if status in {"executing", "delivery_uncertain"}:
                raise JournalCreateDeliveryUncertainError(
                    "journal delivery is uncertain; automatic re-POST is blocked until authoritative read-back"
                )
            if status in {"executed", "verified"}:
                journal_hash = str(existing["moneyforward_journal_id_hash"] or "")
                _require(bool(journal_hash), "executed journal claim has no journal hash")
                conn.commit()
                return _JournalCreateClaim("", journal_hash)
            raise RuntimeError(f"unsupported journal execution status: {status}")
        intent = conn.execute(
            """
            SELECT status, request_hash, moneyforward_journal_id_hash
            FROM write_intents
            WHERE idempotency_key = ? AND operation_type = 'journal_create'
            """,
            (dry_run.idempotency_key,),
        ).fetchone()
        if intent is None:
            raise ValueError("matching journal_create write_intent is missing")
        if intent["status"] in {"executed", "verified"}:
            journal_hash = str(intent["moneyforward_journal_id_hash"] or "")
            _require(bool(journal_hash), "executed journal intent has no journal hash")
            conn.commit()
            return _JournalCreateClaim("", journal_hash)
        _require(intent["status"] == "planned", "journal_create write_intent is not planned")
        _require(intent["request_hash"] == dry_run.request_hash, "journal_create write_intent request mismatch")
        conn.execute(
            """
            INSERT INTO journal_create_execution_records (
                approval_hash, idempotency_key, request_hash, target_hash, dry_run_hash,
                scope_hash, execution_approval_hash, expected_api_action_hash,
                rollback_method_hash, claim_token_hash, execution_channel, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'executing')
            """,
            (
                dry_run.approval_hash,
                dry_run.idempotency_key,
                dry_run.request_hash,
                dry_run.target_hash,
                dry_run.dry_run_hash,
                dry_run.scope_hash,
                stable_hash(execution_approval),
                stable_hash(str(execution_approval.get("expected_api_action") or "")),
                stable_hash(str(execution_approval.get("rollback_method") or "")),
                claim_token_hash,
                dry_run.execution_channel,
            ),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _JournalCreateClaim(claim_token_hash)


def _release_claim_before_delivery(
    conn: sqlite3.Connection,
    *,
    dry_run: JournalCreateExecutionDryRun,
    claim_token_hash: str,
) -> None:
    conn.execute(
        """
        DELETE FROM journal_create_execution_records
        WHERE idempotency_key = ? AND claim_token_hash = ? AND status = 'executing'
        """,
        (dry_run.idempotency_key, claim_token_hash),
    )
    conn.commit()


def _mark_request_rejected(
    conn: sqlite3.Connection,
    *,
    dry_run: JournalCreateExecutionDryRun,
    claim_token_hash: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            DELETE FROM journal_create_execution_records
            WHERE idempotency_key = ? AND claim_token_hash = ? AND status = 'executing'
            """,
            (dry_run.idempotency_key, claim_token_hash),
        )
        conn.execute(
            """
            UPDATE write_intents
            SET status = 'failed'
            WHERE idempotency_key = ? AND operation_type = 'journal_create' AND status = 'planned'
            """,
            (dry_run.idempotency_key,),
        )
        conn.execute(
            """
            UPDATE processing_ledger
            SET processing_status = 'failed',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ? AND processing_status IN ('planned', 'blocked', 'new')
            """,
            (dry_run.idempotency_key,),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _mark_delivery_uncertain(
    conn: sqlite3.Connection,
    *,
    dry_run: JournalCreateExecutionDryRun,
    claim_token_hash: str,
    reason_code: str,
    journal_hash: str = "",
) -> None:
    conn.execute(
        """
        UPDATE journal_create_execution_records
        SET status = 'delivery_uncertain',
            uncertainty_reason_code = ?,
            moneyforward_journal_id_hash = COALESCE(NULLIF(?, ''), moneyforward_journal_id_hash),
            uncertain_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE idempotency_key = ? AND claim_token_hash = ? AND status IN ('executing', 'delivery_uncertain')
        """,
        (reason_code, journal_hash, dry_run.idempotency_key, claim_token_hash),
    )
    conn.commit()


def _mark_journal_created(
    conn: sqlite3.Connection,
    *,
    dry_run: JournalCreateExecutionDryRun,
    claim_token_hash: str,
    journal_hash: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE processing_ledger
            SET processing_status = 'journal_created',
                moneyforward_journal_id_hash = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ?
            """,
            (journal_hash, dry_run.idempotency_key),
        )
        updated = conn.execute(
            """
            UPDATE write_intents
            SET status = 'executed',
                moneyforward_journal_id_hash = ?,
                executed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ? AND operation_type = 'journal_create' AND status = 'planned'
            """,
            (journal_hash, dry_run.idempotency_key),
        )
        _require(updated.rowcount == 1, "journal write_intent changed during execution")
        claim = conn.execute(
            """
            UPDATE journal_create_execution_records
            SET status = 'executed',
                moneyforward_journal_id_hash = ?,
                executed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ? AND claim_token_hash = ? AND status = 'executing'
            """,
            (journal_hash, dry_run.idempotency_key, claim_token_hash),
        )
        _require(claim.rowcount == 1, "journal execution claim changed before persistence")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _already_executed_result(
    dry_run: JournalCreateExecutionDryRun,
    journal_hash: str,
) -> JournalCreatePostResult:
    return JournalCreatePostResult(
        operation_type="journal_create",
        execution_channel=dry_run.execution_channel,
        executed=False,
        already_executed=True,
        idempotency_key=dry_run.idempotency_key,
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        approval_hash=dry_run.approval_hash,
        response_hash=stable_hash({"already_executed": True, "journal_hash": journal_hash}),
        moneyforward_journal_id_hash=journal_hash,
        moneyforward_journal_id="",
        moneyforward_api_called=False,
    )


def _redact_response(response: dict[str, Any], journal_hash: str) -> dict[str, Any]:
    return {
        "keys": sorted(response.keys()),
        "moneyforward_journal_id_hash": journal_hash,
        "raw_response_stored": False,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
