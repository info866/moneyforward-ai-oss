from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .hash_utils import normalize_sha256_hash, sha256_hash_variants, stable_hash

HEX_HASH_LEN = 64
MASKED_ID_PREFIX = "sha256:"
MASKED_ID_LEN = 71


def record_voucher_attach_execution_approval(
    conn: sqlite3.Connection,
    *,
    grant: dict[str, Any],
    preflight: dict[str, Any],
    dry_run_hash: str,
    moneyforward_journal_id_hash: str,
    execution_approval: dict[str, Any],
) -> str:
    _require(grant.get("operation_type") == "voucher_attach", "grant operation_type must be voucher_attach")
    _require(execution_approval.get("operation_type") == "voucher_attach", "approval operation_type must be voucher_attach")
    for field in ("approval_hash", "request_hash", "target_hash", "scope_hash"):
        _require(str(grant.get(field) or "") == str(execution_approval.get(field) or ""), f"{field} mismatch")
    _require(str(execution_approval.get("dry_run_hash") or "") == dry_run_hash, "dry_run_hash mismatch")
    require_voucher_attach_execution_approval_details(
        execution_approval=execution_approval,
        expected_dry_run_hash=dry_run_hash,
        preflight=preflight,
        grant=grant,
    )
    approval_hash = str(grant["approval_hash"])
    values = {
        "approval_hash": approval_hash,
        "request_hash": str(grant["request_hash"]),
        "target_hash": str(grant["target_hash"]),
        "moneyforward_journal_id_hash": moneyforward_journal_id_hash,
        "dry_run_hash": dry_run_hash,
        "scope_hash": str(grant["scope_hash"]),
        "expected_api_action_hash": stable_hash(str(execution_approval["expected_api_action"])),
        "rollback_plan_hash": stable_hash(str(execution_approval["rollback_method"])),
        "user_approval_hash": stable_hash(str(execution_approval["user_approval"])),
        "requested_file_identity_hash": _requested_file_identity_hash(preflight),
    }
    for field, value in values.items():
        if field == "requested_file_identity_hash":
            continue
        if field == "moneyforward_journal_id_hash":
            _require_masked_identifier(value, field)
        else:
            _require_hash(value, field)
    existing = conn.execute(
        """
        SELECT approval_hash, request_hash, target_hash, moneyforward_journal_id_hash, dry_run_hash, scope_hash,
               expected_api_action_hash, rollback_plan_hash, user_approval_hash,
               requested_file_identity_hash, status
        FROM voucher_attach_execution_records
        WHERE approval_hash = ?
        """,
        (approval_hash,),
    ).fetchone()
    if existing is not None:
        for field, value in values.items():
            if field == "requested_file_identity_hash":
                _require(existing[field] == value, f"conflicting duplicate approval {field}")
            else:
                _require(str(existing[field]) == value, f"conflicting duplicate approval {field}")
        if str(existing["status"]) != "approved":
            raise PermissionError("voucher attach approval has already entered execution lifecycle")
        return approval_hash
    conn.execute(
        """
        INSERT OR IGNORE INTO voucher_attach_execution_records (
            approval_hash, request_hash, target_hash, moneyforward_journal_id_hash, dry_run_hash, scope_hash,
            expected_api_action_hash, rollback_plan_hash, user_approval_hash,
            requested_file_identity_hash, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved')
        """,
        (
            values["approval_hash"],
            values["request_hash"],
            values["target_hash"],
            values["moneyforward_journal_id_hash"],
            values["dry_run_hash"],
            values["scope_hash"],
            values["expected_api_action_hash"],
            values["rollback_plan_hash"],
            values["user_approval_hash"],
            values["requested_file_identity_hash"],
        ),
    )
    conn.commit()
    return approval_hash


def require_voucher_attach_execution_approval_details(
    *,
    execution_approval: dict[str, Any],
    expected_dry_run_hash: str,
    preflight: dict[str, Any] | None = None,
    grant: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    _require(bool(execution_approval.get("user_approval")), "user_approval is required")
    _require_hash(expected_dry_run_hash, "dry_run_hash")
    expected_api_action = str(execution_approval.get("expected_api_action", ""))
    rollback_method = str(execution_approval.get("rollback_method", ""))
    if preflight is not None:
        _require(expected_api_action == str(preflight.get("expected_api_action") or ""), "execution expected_api_action must match preflight")
        _require(rollback_method == str(preflight.get("rollback_method") or ""), "execution rollback_method must match preflight")
    if grant is not None:
        _require_hash(str(grant.get("approval_record_hash") or ""), "approval_record_hash")
        _require(
            str(execution_approval.get("approval_record_hash") or "") == str(grant.get("approval_record_hash") or ""),
            "execution approval_record_hash mismatch",
        )
    _require("POST /api/v3/vouchers" in expected_api_action, "expected API action mismatch")
    _require("GET /api/v3/journals/{id}" in expected_api_action, "expected read-back action mismatch")
    _require("voucher_file_id" in rollback_method, "rollback method mismatch")
    _require("separate explicit approval" in rollback_method, "rollback must require separate explicit approval")
    approved_at = _parse_utc_timestamp(str(execution_approval.get("approved_at") or ""), "approved_at")
    expires_at = _parse_utc_timestamp(str(execution_approval.get("expires_at") or ""), "expires_at")
    reference_now = now or datetime.now(timezone.utc)
    _require(approved_at <= expires_at, "approval expires before approved_at")
    _require(reference_now < expires_at, "execution approval is expired")
    confirmation = execution_approval.get("execution_confirmation")
    _require(isinstance(confirmation, dict), "execution_confirmation is required")
    expected_fields = {
        "operation_type": "voucher_attach",
        "approval_hash": execution_approval.get("approval_hash"),
        "request_hash": execution_approval.get("request_hash"),
        "target_hash": execution_approval.get("target_hash"),
        "scope_hash": execution_approval.get("scope_hash"),
        "approval_record_hash": execution_approval.get("approval_record_hash"),
        "dry_run_hash": expected_dry_run_hash,
        "expected_api_action": expected_api_action,
        "rollback_method": rollback_method,
    }
    for field, expected in expected_fields.items():
        _require(str(confirmation.get(field) or "") == str(expected or ""), f"execution_confirmation {field} mismatch")
    for field in (
        "execute_approved",
        "moneyforward_post_allowed",
        "read_back_required",
        "rollback_requires_separate_approval",
    ):
        _require(confirmation.get(field) is True, f"execution_confirmation {field} must be true")
    for field in (
        "raw_secret_stored",
        "raw_journal_id_stored",
        "raw_voucher_file_ids_stored",
        "raw_api_response_stored",
    ):
        _require(confirmation.get(field) is False, f"execution_confirmation {field} must be false")


def mark_voucher_attach_executing(conn: sqlite3.Connection, *, approval_hash: str) -> None:
    _require_hash(approval_hash, "approval_hash")
    row = conn.execute(
        "SELECT approval_hash, status FROM voucher_attach_execution_records WHERE approval_hash = ?",
        (approval_hash,),
    ).fetchone()
    if row is None:
        raise KeyError("voucher attach approval record not found")
    if row["status"] not in ("approved", "executing"):
        raise PermissionError("voucher attach execution cannot be started after terminal status")
    conn.execute(
        """
        UPDATE voucher_attach_execution_records
        SET status = 'executing'
        WHERE approval_hash = ? AND status IN ('approved', 'executing')
        """,
        (approval_hash,),
    )
    conn.commit()


def mark_voucher_attach_executed(conn: sqlite3.Connection, *, approval_hash: str) -> None:
    _require_hash(approval_hash, "approval_hash")
    row = conn.execute(
        "SELECT approval_hash, status FROM voucher_attach_execution_records WHERE approval_hash = ?",
        (approval_hash,),
    ).fetchone()
    if row is None:
        raise KeyError("voucher attach approval record not found")
    if row["status"] not in ("executing", "executed"):
        raise PermissionError("voucher attach execution cannot be marked after terminal status")
    conn.execute(
        """
        UPDATE voucher_attach_execution_records
        SET status = 'executed',
            executed_at = COALESCE(executed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE approval_hash = ? AND status IN ('executing', 'executed')
        """,
        (approval_hash,),
    )
    conn.commit()


def mark_voucher_attach_failed(conn: sqlite3.Connection, *, approval_hash: str) -> None:
    _require_hash(approval_hash, "approval_hash")
    row = conn.execute(
        "SELECT approval_hash, status FROM voucher_attach_execution_records WHERE approval_hash = ?",
        (approval_hash,),
    ).fetchone()
    if row is None:
        raise KeyError("voucher attach approval record not found")
    if row["status"] in ("executed", "verified", "revoked"):
        raise PermissionError("voucher attach failure cannot overwrite terminal status")
    conn.execute(
        """
        UPDATE voucher_attach_execution_records
        SET status = 'failed',
            executed_at = COALESCE(executed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE approval_hash = ? AND status IN ('approved', 'executing', 'failed')
        """,
        (approval_hash,),
    )
    conn.commit()


def require_voucher_attach_ready_for_readback(
    conn: sqlite3.Connection,
    *,
    approval_hash: str,
    moneyforward_journal_id_hash: str,
) -> None:
    _require_hash(approval_hash, "approval_hash")
    _require_masked_identifier(moneyforward_journal_id_hash, "moneyforward_journal_id_hash")
    row = conn.execute(
        "SELECT status, moneyforward_journal_id_hash FROM voucher_attach_execution_records WHERE approval_hash = ?",
        (approval_hash,),
    ).fetchone()
    if row is None:
        raise KeyError("voucher attach approval record not found")
    if row["status"] not in {"executed", "verified"}:
        raise PermissionError("voucher attach read-back requires executed status")
    if row["moneyforward_journal_id_hash"] != moneyforward_journal_id_hash:
        raise PermissionError("voucher attach read-back journal_id does not match approved target")


def record_voucher_attach_readback_verification(
    conn: sqlite3.Connection,
    *,
    approval_hash: str,
    verification: dict[str, Any],
) -> str:
    _require_hash(approval_hash, "approval_hash")
    row = conn.execute(
        """
        SELECT approval_hash, status, requested_file_identity_hash
        FROM voucher_attach_execution_records
        WHERE approval_hash = ?
        """,
        (approval_hash,),
    ).fetchone()
    if row is None:
        raise KeyError("voucher attach approval record not found")
    _require(verification.get("operation_type") == "voucher_attach_read_back", "verification operation_type mismatch")
    for field in (
        "journal_id_hash",
        "expected_voucher_file_ids_hash",
        "actual_voucher_file_ids_hash",
        "post_result_voucher_file_ids_hash",
        "response_hash",
    ):
        _require(bool(verification.get(field)), f"{field} is required")
    _require_masked_identifier(str(verification["journal_id_hash"]), "journal_id_hash")
    for field in (
        "expected_voucher_file_ids_hash",
        "actual_voucher_file_ids_hash",
        "post_result_voucher_file_ids_hash",
        "response_hash",
    ):
        _require_hash(str(verification[field]), field)
    verified = bool(verification.get("verified"))
    verification_hash = stable_hash(
        {
            "approval_hash": approval_hash,
            "journal_id_hash": verification["journal_id_hash"],
            "expected_voucher_file_ids_hash": verification["expected_voucher_file_ids_hash"],
            "actual_voucher_file_ids_hash": verification["actual_voucher_file_ids_hash"],
            "post_result_voucher_file_ids_hash": verification["post_result_voucher_file_ids_hash"],
            "response_hash": verification["response_hash"],
            "verified": verified,
        }
    )
    if verified:
        ledger_row = _resolve_processing_ledger_row(
            conn,
            moneyforward_journal_id_hash=str(verification["journal_id_hash"]),
            requested_file_identity_hash=row["requested_file_identity_hash"],
        )
        _require_ledger_journal_compatible(
            ledger_row,
            moneyforward_journal_id_hash=str(verification["journal_id_hash"]),
        )
    existing = conn.execute(
        """
        SELECT verification_hash, passed FROM voucher_attach_readback_verifications
        WHERE approval_hash = ?
        """,
        (approval_hash,),
    ).fetchone()
    if existing is not None:
        _require(str(existing["verification_hash"]) == verification_hash, "conflicting read-back verification")
        _require(bool(existing["passed"]) == verified, "conflicting read-back status")
        if verified:
            _mark_processing_ledger_after_voucher_readback(
                conn,
                moneyforward_journal_id_hash=str(verification["journal_id_hash"]),
                moneyforward_voucher_id_hash=str(verification["post_result_voucher_file_ids_hash"]),
                requested_file_identity_hash=row["requested_file_identity_hash"],
            )
            conn.commit()
        return verification_hash
    if row["status"] != "executed":
        raise PermissionError("voucher attach read-back requires executed status")
    conn.execute(
        """
        INSERT INTO voucher_attach_readback_verifications (
            verification_hash, approval_hash, journal_id_hash,
            expected_voucher_file_ids_hash, actual_voucher_file_ids_hash,
            post_result_voucher_file_ids_hash, response_hash, passed,
            diff_summary_redacted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_hash,
            approval_hash,
            str(verification["journal_id_hash"]),
            str(verification["expected_voucher_file_ids_hash"]),
            str(verification["actual_voucher_file_ids_hash"]),
            str(verification["post_result_voucher_file_ids_hash"]),
            str(verification["response_hash"]),
            1 if verified else 0,
            "voucher_file_ids一致" if verified else "voucher_file_idsが一致しない",
        ),
    )
    conn.execute(
        """
        UPDATE voucher_attach_execution_records
        SET status = CASE
                WHEN status = 'executed' THEN ?
                ELSE status
            END,
            verified_at = COALESCE(verified_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE approval_hash = ? AND status = 'executed'
        """,
        ("verified" if verified else "failed", approval_hash),
    )
    if verified:
        _mark_processing_ledger_after_voucher_readback(
            conn,
            moneyforward_journal_id_hash=str(verification["journal_id_hash"]),
            moneyforward_voucher_id_hash=str(verification["post_result_voucher_file_ids_hash"]),
            requested_file_identity_hash=row["requested_file_identity_hash"],
        )
    conn.commit()
    return verification_hash


def _mark_processing_ledger_after_voucher_readback(
    conn: sqlite3.Connection,
    *,
    moneyforward_journal_id_hash: str,
    moneyforward_voucher_id_hash: str,
    requested_file_identity_hash: str | None,
) -> None:
    _require_masked_identifier(moneyforward_journal_id_hash, "moneyforward_journal_id_hash")
    _require_hash(moneyforward_voucher_id_hash, "moneyforward_voucher_id_hash")
    # Voucher read-back is not terminal for Drive evidence. The immutable
    # evidence still has to be moved and read back from the processed folder.
    next_status = "voucher_attached"
    ledger_row = _resolve_processing_ledger_row(
        conn,
        moneyforward_journal_id_hash=moneyforward_journal_id_hash,
        requested_file_identity_hash=requested_file_identity_hash,
    )
    if ledger_row is None:
        return
    _require_ledger_journal_compatible(
        ledger_row,
        moneyforward_journal_id_hash=moneyforward_journal_id_hash,
    )
    conn.execute(
        """
        UPDATE processing_ledger
        SET processing_status = ?,
            moneyforward_journal_id_hash = ?,
            moneyforward_voucher_id_hash = COALESCE(moneyforward_voucher_id_hash, ?),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (
            next_status,
            moneyforward_journal_id_hash,
            moneyforward_voucher_id_hash,
            ledger_row["id"],
        ),
    )
    conn.execute(
        """
        UPDATE write_intents
        SET status = 'verified',
            verified_at = COALESCE(verified_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE operation_type = 'voucher_attach'
          AND moneyforward_journal_id_hash = ?
          AND idempotency_key = ?
          AND status IN ('planned', 'executed', 'verified')
        """,
        (moneyforward_journal_id_hash, ledger_row["idempotency_key"]),
    )


def repair_verified_voucher_processing_ledger_binding(
    conn: sqlite3.Connection,
    *,
    approval_hash: str,
    file_hash: str,
) -> None:
    """Bind an already verified voucher read-back to exactly one evidence row.

    This is a local reconciliation only.  It never calls Money Forward and never
    permits a second voucher POST.
    """

    _require_hash(approval_hash, "approval_hash")
    identity_hash = normalize_sha256_hash(file_hash)
    _require_canonical_file_hash(identity_hash, "file_hash")
    row = conn.execute(
        """
        SELECT execution.status, execution.moneyforward_journal_id_hash,
               execution.requested_file_identity_hash,
               verification.post_result_voucher_file_ids_hash,
               verification.passed
        FROM voucher_attach_execution_records AS execution
        JOIN voucher_attach_readback_verifications AS verification
          ON verification.approval_hash = execution.approval_hash
        WHERE execution.approval_hash = ?
        """,
        (approval_hash,),
    ).fetchone()
    if row is None or str(row["status"]) != "verified" or not bool(row["passed"]):
        raise PermissionError("voucher processing-ledger repair requires verified read-back")
    existing_identity = str(row["requested_file_identity_hash"] or "")
    if existing_identity and normalize_sha256_hash(existing_identity) != identity_hash:
        raise ValueError("voucher approval is already bound to a different evidence file")
    ledger_row = _resolve_processing_ledger_row(
        conn,
        moneyforward_journal_id_hash=str(row["moneyforward_journal_id_hash"]),
        requested_file_identity_hash=identity_hash,
    )
    _require_ledger_journal_compatible(
        ledger_row,
        moneyforward_journal_id_hash=str(row["moneyforward_journal_id_hash"]),
    )
    conn.execute(
        """
        UPDATE voucher_attach_execution_records
        SET requested_file_identity_hash = ?
        WHERE approval_hash = ? AND requested_file_identity_hash IS NULL
        """,
        (identity_hash, approval_hash),
    )
    _mark_processing_ledger_after_voucher_readback(
        conn,
        moneyforward_journal_id_hash=str(row["moneyforward_journal_id_hash"]),
        moneyforward_voucher_id_hash=str(row["post_result_voucher_file_ids_hash"]),
        requested_file_identity_hash=identity_hash,
    )
    conn.commit()


def _resolve_processing_ledger_row(
    conn: sqlite3.Connection,
    *,
    moneyforward_journal_id_hash: str,
    requested_file_identity_hash: str | None,
) -> sqlite3.Row | None:
    if requested_file_identity_hash:
        normalized = normalize_sha256_hash(str(requested_file_identity_hash))
        _require_canonical_file_hash(normalized, "requested_file_identity_hash")
        variants = sha256_hash_variants(normalized)
        placeholders = ",".join("?" for _ in variants)
        rows = conn.execute(
            f"""
            SELECT id, idempotency_key, moneyforward_journal_id_hash, processing_status
            FROM processing_ledger
            WHERE file_hash IN ({placeholders})
            ORDER BY id
            """,
            variants,
        ).fetchall()
        if not rows:
            raise KeyError("voucher evidence file is missing from processing ledger")
        exact = [
            row
            for row in rows
            if str(row["moneyforward_journal_id_hash"] or "")
            == moneyforward_journal_id_hash
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError("voucher evidence file has multiple exact journal bindings")
        unbound = [row for row in rows if not row["moneyforward_journal_id_hash"]]
        if len(unbound) == 1:
            return unbound[0]
        raise ValueError("voucher evidence file identity is ambiguous in processing ledger")

    rows = conn.execute(
        """
        SELECT id, idempotency_key, moneyforward_journal_id_hash
        FROM processing_ledger
        WHERE moneyforward_journal_id_hash = ?
        ORDER BY id
        """,
        (moneyforward_journal_id_hash,),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("legacy voucher read-back is ambiguous without evidence file identity")
    return rows[0] if rows else None


def _require_ledger_journal_compatible(
    ledger_row: sqlite3.Row | None,
    *,
    moneyforward_journal_id_hash: str,
) -> None:
    if ledger_row is None:
        return
    current_journal_hash = str(ledger_row["moneyforward_journal_id_hash"] or "")
    if current_journal_hash and current_journal_hash != moneyforward_journal_id_hash:
        raise ValueError("voucher evidence is already bound to a different journal")


def _requested_file_identity_hash(preflight: dict[str, Any]) -> str | None:
    file_hash = str(preflight.get("file_hash") or "")
    if not file_hash:
        return None
    normalized = normalize_sha256_hash(file_hash)
    _require_canonical_file_hash(normalized, "preflight file_hash")
    return normalized


def _require_canonical_file_hash(value: str, field: str) -> None:
    _require(value.startswith(MASKED_ID_PREFIX), f"{field} must be a canonical sha256 hash")
    _require_hash(value.removeprefix(MASKED_ID_PREFIX), field)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_hash(value: str, field: str) -> None:
    _require(len(value) == HEX_HASH_LEN and all(char in "0123456789abcdef" for char in value), f"{field} must be a sha256 hex hash")


def _require_masked_identifier(value: str, field: str) -> None:
    _require(value.startswith(MASKED_ID_PREFIX) and len(value) == MASKED_ID_LEN, f"{field} must be a masked sha256 identifier")
    _require_hash(value.removeprefix(MASKED_ID_PREFIX), field)


def _parse_utc_timestamp(value: str, field: str) -> datetime:
    _require(bool(value), f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    _require(parsed.tzinfo is not None, f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)
