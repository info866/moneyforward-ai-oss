from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounting_api import WriteExecutionGrant
from .db import Phase12Database
from .hash_utils import stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .transaction_journalize import (
    JOURNAL_CREATE_EXECUTION_CHANNEL,
    TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
    expected_api_action,
    journal_write_request_metadata,
    transaction_journalize_binding_proof,
)


@dataclass(frozen=True)
class JournalWriteGrantSummary:
    status: str
    approval_record_json: Path
    journal_json: Path
    output_json: Path
    idempotency_key: str
    approval_hash: str
    scope_hash: str
    target_hash: str
    execution_channel: str
    expected_api_action: str
    body_hash: str
    dry_run_hash: str
    source_row_hash: str
    transaction_facts_hash: str
    connected_account_binding_hash: str
    connected_ledger_binding_hash: str
    checked_items: tuple[str, ...]

    def to_redacted_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "approval_record_json": str(self.approval_record_json),
            "journal_json": str(self.journal_json),
            "output_json": str(self.output_json),
            "operation_type": "journal_create",
            "execution_channel": self.execution_channel,
            "expected_api_action": self.expected_api_action,
            "idempotency_key": self.idempotency_key,
            "approval_hash": self.approval_hash,
            "scope_hash": self.scope_hash,
            "target_hash": self.target_hash,
            "request_hash": self.target_hash,
            "body_hash": self.body_hash,
            "dry_run_hash": self.dry_run_hash,
            "checked_items": list(self.checked_items),
            "write_execution_grant_ready": True,
            "journal_post_allowed": False,
            "executed": False,
            "next_action": "request_explicit_moneyforward_journal_post_approval_for_this_grant",
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_journal_body_printed": False,
            "raw_api_response_stored": False,
        }
        if self.execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
            result.update(
                {
                    "source_row_hash": self.source_row_hash,
                    "transaction_facts_hash": self.transaction_facts_hash,
                    "connected_account_binding_hash": self.connected_account_binding_hash,
                    "connected_ledger_binding_hash": self.connected_ledger_binding_hash,
                }
            )
        return result


def build_journal_write_grant(
    *,
    approval_record_json: Path,
    journal_json: Path,
    output_json: Path,
    db_path: Path | None = None,
    office_id: str | None = None,
) -> JournalWriteGrantSummary:
    approval_record_json = require_ignored_untracked_data_path(
        approval_record_json, purpose="journal write approval record input"
    )
    journal_json = require_ignored_untracked_data_path(journal_json, purpose="journal create body input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="journal write grant output")
    approval_record = _load_object(approval_record_json, "journal write approval record")
    journal = _load_object(journal_json, "journal create body")
    checked = ["approval_record_loaded", "journal_body_loaded"]

    idempotency_key = _required_string(approval_record, "idempotency_key")
    approval_hash = _required_string(approval_record, "approval_hash")
    target_request_hash = _required_string(approval_record, "target_request_hash")
    if approval_record.get("status") != "approval_record_created":
        raise ValueError("journal write approval record is not created")
    checked.append("approval_record_status_created")

    metadata = journal_write_request_metadata(journal, office_id=office_id)
    if metadata["request_hash"] != target_request_hash:
        raise ValueError("journal body request_hash does not match approval target_request_hash")
    checked.append("journal_body_matches_approval_target")

    db = Phase12Database(db_path)
    db.initialize()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT scope_hash, revoked_at
            FROM approval_records
            WHERE approval_hash = ?
              AND idempotency_key = ?
              AND operation_type = 'journal_create'
            """,
            (approval_hash, idempotency_key),
        ).fetchone()
        if not row:
            raise ValueError("matching SDK approval record is missing")
        if row["revoked_at"]:
            raise ValueError("matching SDK approval record is revoked")
        scope_hash = str(row["scope_hash"])
        checked.extend(["sdk_approval_record_present", "sdk_approval_record_not_revoked"])
        intent = conn.execute(
            """
            SELECT status, request_hash
            FROM write_intents
            WHERE idempotency_key = ?
              AND operation_type = 'journal_create'
            """,
            (idempotency_key,),
        ).fetchone()
        if not intent:
            raise ValueError("matching journal_create write_intent is missing")
        if intent["status"] != "planned":
            raise ValueError("matching journal_create write_intent is not planned")
        if intent["request_hash"] != target_request_hash:
            raise ValueError("write_intent request_hash does not match approval target_request_hash")
        checked.extend(["write_intent_present", "write_intent_planned", "write_intent_target_matches"])

    execution_channel = str(metadata["execution_channel"])
    approval_channel = str(approval_record.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
    if approval_channel != execution_channel:
        raise ValueError("journal write approval execution_channel does not match request body")
    approval_action = str(approval_record.get("expected_api_action") or expected_api_action(approval_channel))
    if approval_action != expected_api_action(execution_channel):
        raise ValueError("journal write approval endpoint does not match request body")
    checked.append("execution_channel_matches_approval")
    binding_proof = {
        "source_row_hash": "",
        "transaction_facts_hash": "",
        "connected_account_binding_hash": "",
        "connected_ledger_binding_hash": "",
    }
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        binding_proof = transaction_journalize_binding_proof(approval_record)
        checked.append("transaction_binding_proof_matches_approval")
    grant = WriteExecutionGrant(
        operation_type=execution_channel,
        approval_hash=approval_hash,
        scope_hash=scope_hash,
        target_hash=target_request_hash,
    )
    grant.assert_allows(execution_channel)
    checked.append("write_execution_grant_constructed")
    dry_run_payload = {
        "operation_type": execution_channel,
        "idempotency_key": idempotency_key,
        "approval_hash": approval_hash,
        "scope_hash": scope_hash,
        "target_hash": target_request_hash,
        "body_hash": metadata["body_hash"],
    }
    if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
        dry_run_payload.update(binding_proof)
    dry_run_hash = stable_hash(dry_run_payload)
    summary = JournalWriteGrantSummary(
        status="grant_ready",
        approval_record_json=approval_record_json,
        journal_json=journal_json,
        output_json=output_json,
        idempotency_key=idempotency_key,
        approval_hash=approval_hash,
        scope_hash=scope_hash,
            target_hash=target_request_hash,
        execution_channel=execution_channel,
        expected_api_action=expected_api_action(execution_channel),
        body_hash=str(metadata["body_hash"]),
        dry_run_hash=dry_run_hash,
        source_row_hash=binding_proof["source_row_hash"],
        transaction_facts_hash=binding_proof["transaction_facts_hash"],
        connected_account_binding_hash=binding_proof["connected_account_binding_hash"],
        connected_ledger_binding_hash=binding_proof["connected_ledger_binding_hash"],
        checked_items=tuple(checked),
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _load_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field) or "")
    if not value:
        raise ValueError(f"journal write approval record missing {field}")
    return value
