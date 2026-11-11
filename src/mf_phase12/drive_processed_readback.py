from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, Phase12Database
from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path


PROCESSED_READBACK_EVENT = "drive_processed_folder_readback"


@dataclass(frozen=True)
class DriveProcessedReadbackResult:
    status: str
    idempotency_key: str
    ledger_match_strategy: str
    file_id_hash: str
    processed_folder_id_hash: str
    parent_folder_hashes: list[str]
    audit_event_hash: str
    batch_audit_event_hash: str
    google_drive_write_called: bool = False
    raw_drive_file_ids_stored: bool = False
    raw_api_response_stored: bool = False

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "ledger_match_strategy": self.ledger_match_strategy,
            "file_id_hash": self.file_id_hash,
            "processed_folder_id_hash": self.processed_folder_id_hash,
            "parent_folder_hashes": self.parent_folder_hashes,
            "audit_event_hash": self.audit_event_hash,
            "batch_audit_event_hash": self.batch_audit_event_hash,
            "google_drive_write_called": self.google_drive_write_called,
            "raw_drive_file_ids_stored": self.raw_drive_file_ids_stored,
            "raw_api_response_stored": self.raw_api_response_stored,
        }


def record_processed_folder_readback_from_metadata(
    *,
    metadata_json: Path,
    processed_folder_id: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    file_hash: str | None = None,
) -> DriveProcessedReadbackResult:
    metadata_path = require_ignored_untracked_data_path(metadata_json, purpose="Drive processed read-back metadata")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    file_id = _metadata_file_id(metadata)
    parent_ids = _metadata_parent_ids(metadata)
    if processed_folder_id not in parent_ids:
        raise RuntimeError("Drive file is not in the expected processed folder")
    db = Phase12Database(Path(db_path))
    db.initialize()
    with db.connect() as conn:
        conn.row_factory = sqlite3.Row
        ledger, ledger_match_strategy = _find_verified_ledger(
            conn,
            file_id_hashes=_file_id_hashes(file_id),
            file_hash=file_hash,
        )
        result = _record_readback(
            conn,
            ledger,
            ledger_match_strategy=ledger_match_strategy,
            file_id=file_id,
            processed_folder_id=processed_folder_id,
            parent_ids=parent_ids,
        )
        conn.commit()
    return result


def _metadata_file_id(metadata: dict[str, Any]) -> str:
    file_id = metadata.get("id") or metadata.get("fileId")
    if not isinstance(file_id, str) or not file_id.strip():
        raise ValueError("metadata JSON must contain id")
    return file_id


def _metadata_parent_ids(metadata: dict[str, Any]) -> list[str]:
    values = metadata.get("parent_ids") or metadata.get("parents")
    if not isinstance(values, list) or not values:
        raise ValueError("metadata JSON must contain parent_ids or parents")
    parents = [str(value) for value in values if str(value).strip()]
    if not parents:
        raise ValueError("metadata parent list is empty")
    return parents


def _find_verified_ledger(
    conn: sqlite3.Connection,
    *,
    file_id_hashes: list[str],
    file_hash: str | None,
) -> tuple[sqlite3.Row, str]:
    if not file_id_hashes:
        raise ValueError("file_id_hashes is required")
    placeholders = ", ".join("?" for _ in file_id_hashes)
    params: tuple[Any, ...]
    file_hash_clause = ""
    if file_hash:
        file_hash_clause = "AND file_hash = ?"
        params = (*file_id_hashes, file_hash)
    else:
        params = tuple(file_id_hashes)
    rows = conn.execute(
        f"""
        SELECT id, idempotency_key, google_drive_file_id_hash, file_hash,
               processing_status, moneyforward_journal_id_hash, moneyforward_voucher_id_hash
        FROM processing_ledger
        WHERE google_drive_file_id_hash IN ({placeholders})
          {file_hash_clause}
          AND processing_status IN ('voucher_attached', 'verified')
          AND moneyforward_journal_id_hash IS NOT NULL
          AND moneyforward_voucher_id_hash IS NOT NULL
        ORDER BY updated_at DESC, id DESC
        """,
        params,
    ).fetchall()
    if len(rows) == 1:
        return rows[0], "drive_file_id"
    if len(rows) > 1:
        raise RuntimeError(f"expected exactly one verified ledger row, found {len(rows)}")

    if not file_hash:
        raise RuntimeError("expected exactly one verified ledger row, found 0")

    # Drive sync clients can materialize a moved file as a new Drive file ID.
    # Only fall back when the observed ID is absent from every verified ledger
    # row and a single immutable content hash identifies the completed chain.
    conflicting_id_rows = conn.execute(
        f"""
        SELECT id
        FROM processing_ledger
        WHERE google_drive_file_id_hash IN ({placeholders})
          AND processing_status IN ('voucher_attached', 'verified')
        LIMIT 2
        """,
        tuple(file_id_hashes),
    ).fetchall()
    if conflicting_id_rows:
        raise RuntimeError("processed Drive metadata ID conflicts with an existing verified ledger row")
    fallback_rows = conn.execute(
        """
        SELECT id, idempotency_key, google_drive_file_id_hash, file_hash,
               processing_status, moneyforward_journal_id_hash, moneyforward_voucher_id_hash
        FROM processing_ledger
        WHERE file_hash = ?
          AND processing_status IN ('voucher_attached', 'verified')
          AND moneyforward_journal_id_hash IS NOT NULL
          AND moneyforward_voucher_id_hash IS NOT NULL
        ORDER BY updated_at DESC, id DESC
        """,
        (file_hash,),
    ).fetchall()
    if len(fallback_rows) != 1:
        raise RuntimeError(f"expected exactly one verified ledger row by content hash, found {len(fallback_rows)}")
    return fallback_rows[0], "content_hash_after_drive_id_change"


def _file_id_hashes(file_id: str) -> list[str]:
    # Current records use mask_identifier; early local backfills used sha256:<stable_hash(raw_id)>.
    legacy_hash = f"sha256:{stable_hash(file_id)}"
    hashes = [mask_identifier(file_id), legacy_hash]
    return list(dict.fromkeys(hashes))


def _record_readback(
    conn: sqlite3.Connection,
    ledger: sqlite3.Row,
    *,
    ledger_match_strategy: str,
    file_id: str,
    processed_folder_id: str,
    parent_ids: list[str],
) -> DriveProcessedReadbackResult:
    idempotency_key = str(ledger["idempotency_key"])
    request = {
        "file_id_hash": mask_identifier(file_id),
        "ledger_file_id_hash": str(ledger["google_drive_file_id_hash"]),
        "ledger_match_strategy": ledger_match_strategy,
        "processed_folder_id_hash": mask_identifier(processed_folder_id),
        "idempotency_key": idempotency_key,
    }
    response = {
        "parent_folder_hashes": [mask_identifier(parent_id) for parent_id in sorted(parent_ids)],
        "processed_folder_present": True,
        "raw_drive_file_ids_stored": False,
    }
    audit_event_hash = stable_hash(
        {
            "event_type": PROCESSED_READBACK_EVENT,
            "idempotency_key": idempotency_key,
            "request_hash": stable_hash(request),
            "response_hash": stable_hash(response),
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_logs (
            event_hash, event_type, idempotency_key, request_hash, response_hash,
            actor, dry_run, rollback_plan
        ) VALUES (?, ?, ?, ?, ?, 'codex', 1, ?)
        """,
        (
            audit_event_hash,
            PROCESSED_READBACK_EVENT,
            idempotency_key,
            stable_hash(request),
            stable_hash(response),
            "Read-only Google Drive metadata confirmed the file is in the processed folder.",
        ),
    )
    conn.execute(
        """
        UPDATE processing_ledger
        SET processing_status = 'verified',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
          AND processing_status IN ('voucher_attached', 'verified')
          AND moneyforward_journal_id_hash IS NOT NULL
          AND moneyforward_voucher_id_hash IS NOT NULL
        """,
        (ledger["id"],),
    )
    batch_audit_event_hash = stable_hash(
        {
            "event_type": "drive_folder_batch_audit",
            "idempotency_key": idempotency_key,
            "request_hash": stable_hash(request),
            "response_hash": stable_hash({"processed_readback": response}),
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_logs (
            event_hash, event_type, idempotency_key, request_hash, response_hash,
            actor, dry_run, rollback_plan
        ) VALUES (?, 'drive_folder_batch_audit', ?, ?, ?, 'codex', 1, ?)
        """,
        (
            batch_audit_event_hash,
            idempotency_key,
            stable_hash(request),
            stable_hash({"processed_readback": response}),
            "Read-only Drive processed-folder proof; no Google Drive write was performed.",
        ),
    )
    return DriveProcessedReadbackResult(
        status="verified",
        idempotency_key=idempotency_key,
        ledger_match_strategy=ledger_match_strategy,
        file_id_hash=mask_identifier(file_id),
        processed_folder_id_hash=mask_identifier(processed_folder_id),
        parent_folder_hashes=response["parent_folder_hashes"],
        audit_event_hash=audit_event_hash,
        batch_audit_event_hash=batch_audit_event_hash,
    )
