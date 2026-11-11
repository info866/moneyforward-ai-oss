from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, Phase12Database
from .office_profile import resolve_office_profile, require_database_office_identity
from .drive_processed_readback import PROCESSED_READBACK_EVENT


@dataclass(frozen=True)
class SameChainStatus:
    db_path: Path
    chains: list[dict[str, Any]]

    def to_redacted_dict(self) -> dict[str, Any]:
        ready = [chain for chain in self.chains if chain["same_chain_ready"]]
        all_ready = bool(self.chains) and len(ready) == len(self.chains)
        next_actions = _counts(chain["next_action"] for chain in self.chains)
        return {
            "status": "ready" if all_ready else "needs_action",
            "db_path": str(self.db_path),
            "chain_count": len(self.chains),
            "same_chain_ready_count": len(ready),
            "next_action_counts": next_actions,
            "chains": self.chains,
            "raw_drive_file_ids_printed": False,
            "raw_journal_ids_printed": False,
            "raw_voucher_ids_printed": False,
            "raw_secret_stored": False,
        }


def build_same_chain_status(db_path: Path | str = DEFAULT_DB_PATH) -> SameChainStatus:
    db = Phase12Database(db_path)
    path = db.path
    with db.connect_readonly() as conn:
        conn.row_factory = sqlite3.Row
        require_database_office_identity(conn, resolve_office_profile(required=True))
        chains = [_chain_status(conn, row) for row in _ledger_rows(conn)]
    return SameChainStatus(db_path=path, chains=chains)


def status_to_json(status: SameChainStatus) -> str:
    return json.dumps(status.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def same_chain_status_for_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
) -> dict[str, Any] | None:
    """Return one authoritative chain only when the evidence hash is unique."""

    require_database_office_identity(conn, resolve_office_profile(required=True))

    if not str(file_hash).strip():
        return None
    rows = conn.execute(
        """
        SELECT idempotency_key, file_name, processing_status,
               google_drive_file_id_hash, file_hash,
               evidence_source_type, moneyforward_journal_id_hash,
               moneyforward_voucher_id_hash
        FROM processing_ledger
        WHERE file_hash = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (file_hash,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return _chain_status(conn, rows[0])


def _ledger_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT idempotency_key, file_name, processing_status,
               google_drive_file_id_hash, file_hash,
               evidence_source_type, moneyforward_journal_id_hash, moneyforward_voucher_id_hash
        FROM processing_ledger
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()


def _chain_status(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    idempotency_key = str(row["idempotency_key"])
    journal_hash = str(row["moneyforward_journal_id_hash"] or "")
    evidence_source_type = str(row["evidence_source_type"] or "drive")
    drive_completion_required = evidence_source_type == "drive"
    checks = {
        "ledger_verified": str(row["processing_status"]) == "verified",
        "journal_create_verified": _journal_create_verified(conn, idempotency_key, journal_hash),
        "post_registration_verified": _post_registration_verified(conn, idempotency_key, journal_hash),
        "voucher_attach_verified": _voucher_attach_verified(
            conn,
            idempotency_key=idempotency_key,
            journal_hash=journal_hash,
            voucher_hash_present=bool(row["moneyforward_voucher_id_hash"]),
        ),
        "drive_batch_audited": (not drive_completion_required) or _drive_batch_audited(conn, idempotency_key),
        "drive_move_verified": (not drive_completion_required) or _drive_move_verified(conn, idempotency_key),
    }
    same_chain_ready = all(checks.values())
    return {
        "idempotency_key": idempotency_key,
        "file_name": row["file_name"],
        "processing_status": row["processing_status"],
        "evidence_source_type": evidence_source_type,
        "drive_completion_required": drive_completion_required,
        "google_drive_file_id_hash": row["google_drive_file_id_hash"],
        "file_hash": row["file_hash"],
        "moneyforward_journal_id_hash": journal_hash,
        "moneyforward_voucher_id_hash_present": bool(row["moneyforward_voucher_id_hash"]),
        "checks": checks,
        "same_chain_ready": same_chain_ready,
        "next_action": "complete" if same_chain_ready else _next_action(checks),
    }


def _journal_create_verified(conn: sqlite3.Connection, idempotency_key: str, journal_hash: str) -> bool:
    if not journal_hash:
        return False
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM write_intents
            WHERE idempotency_key = ?
              AND operation_type = 'journal_create'
              AND status IN ('executed', 'verified')
              AND moneyforward_journal_id_hash = ?
            LIMIT 1
            """,
            (idempotency_key, journal_hash),
        ).fetchone()
    )


def _post_registration_verified(conn: sqlite3.Connection, idempotency_key: str, journal_hash: str) -> bool:
    if not journal_hash:
        return False
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM post_registration_verifications
            WHERE idempotency_key = ?
              AND moneyforward_journal_id_hash = ?
              AND passed = 1
            LIMIT 1
            """,
            (idempotency_key, journal_hash),
        ).fetchone()
    )


def _voucher_attach_verified(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    journal_hash: str,
    voucher_hash_present: bool,
) -> bool:
    if not journal_hash:
        return False
    api_post_verified = bool(
        conn.execute(
            """
            SELECT 1
            FROM voucher_attach_execution_records AS exec
            JOIN voucher_attach_readback_verifications AS verify
              ON verify.approval_hash = exec.approval_hash
             AND verify.passed = 1
             AND verify.raw_secret_stored = 0
             AND verify.raw_journal_id_stored = 0
             AND verify.raw_voucher_file_ids_stored = 0
             AND verify.raw_api_response_stored = 0
            WHERE exec.moneyforward_journal_id_hash = ?
              AND exec.status = 'verified'
              AND exec.raw_secret_stored = 0
              AND exec.raw_journal_id_stored = 0
              AND exec.raw_voucher_file_ids_stored = 0
              AND exec.raw_api_response_stored = 0
            LIMIT 1
            """,
            (journal_hash,),
        ).fetchone()
    )
    if api_post_verified:
        return True
    if not voucher_hash_present:
        return False
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM post_registration_verifications
            WHERE idempotency_key = ?
              AND moneyforward_journal_id_hash = ?
              AND passed = 1
              AND voucher_file_ids_hash IS NOT NULL
              AND voucher_file_ids_hash != ''
            LIMIT 1
            """,
            (idempotency_key, journal_hash),
        ).fetchone()
    )


def _drive_batch_audited(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM audit_logs
            WHERE idempotency_key = ?
              AND event_type = 'drive_folder_batch_audit'
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
    )


def _drive_move_verified(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    drive_move_verified = bool(
        conn.execute(
            """
            SELECT 1
            FROM drive_move_chain_items AS chain
            JOIN drive_move_execution_records AS exec
              ON exec.approval_hash = chain.approval_hash
             AND exec.request_hash = chain.request_hash
             AND exec.target_hash = chain.target_hash
             AND exec.status = 'verified'
             AND exec.raw_secret_stored = 0
             AND exec.raw_drive_file_ids_stored = 0
             AND exec.raw_api_response_stored = 0
            JOIN drive_move_readback_verifications AS verify
              ON verify.approval_hash = exec.approval_hash
             AND verify.request_hash = exec.request_hash
             AND verify.target_hash = exec.target_hash
             AND verify.passed = 1
             AND verify.raw_secret_stored = 0
             AND verify.raw_drive_file_ids_stored = 0
             AND verify.raw_api_response_stored = 0
            WHERE chain.idempotency_key = ?
              AND chain.raw_secret_stored = 0
              AND chain.raw_drive_file_ids_stored = 0
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
    )
    if drive_move_verified:
        return True
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM audit_logs
            WHERE idempotency_key = ?
              AND event_type = ?
            LIMIT 1
            """,
            (idempotency_key, PROCESSED_READBACK_EVENT),
        ).fetchone()
    )


def _next_action(checks: dict[str, bool]) -> str:
    order = (
        ("ledger_verified", "verify_or_backfill_processing_ledger"),
        ("journal_create_verified", "run_journal_post_and_readback_or_backfill"),
        ("post_registration_verified", "run_journal_readback_verify"),
        ("voucher_attach_verified", "run_voucher_attach_and_readback"),
        ("drive_batch_audited", "run_drive_batch_prepare_or_audit"),
        ("drive_move_verified", "run_drive_move_execute_and_verify"),
    )
    for key, action in order:
        if not checks[key]:
            return action
    return "complete"


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts
