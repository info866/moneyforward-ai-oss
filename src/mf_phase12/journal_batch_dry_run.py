from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Phase12Database
from .journal_dry_run import _failed_gate_names
from .local_artifacts import require_ignored_untracked_data_path
from .models import Evidence, JournalCandidate
from .sdk import MoneyForwardDryRunSDK
from .transaction_journalize import (
    JOURNAL_CREATE_API_ACTION,
    JOURNAL_CREATE_EXECUTION_CHANNEL,
    TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
    build_transaction_journalize_request,
    journal_write_request_metadata,
    transaction_journalize_binding_proof,
    validate_transaction_journalize_candidate_mapping,
)


@dataclass(frozen=True)
class JournalBatchDryRunItem:
    index: int
    status: str
    idempotency_key: str
    candidate_hash: str
    dry_run_result_hash: str
    registration_allowed: bool
    execution_channel: str
    expected_api_action: str
    write_request_json: str
    source_row_hash: str
    transaction_facts_hash: str
    connected_account_binding_hash: str
    connected_ledger_binding_hash: str
    next_actions: tuple[str, ...]
    failed_gate_names: tuple[str, ...]

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "candidate_hash": self.candidate_hash,
            "dry_run_result_hash": self.dry_run_result_hash,
            "registration_allowed": self.registration_allowed,
            "execution_channel": self.execution_channel,
            "expected_api_action": self.expected_api_action,
            "write_request_json": self.write_request_json,
            "source_row_hash": self.source_row_hash,
            "transaction_facts_hash": self.transaction_facts_hash,
            "connected_account_binding_hash": self.connected_account_binding_hash,
            "connected_ledger_binding_hash": self.connected_ledger_binding_hash,
            "next_actions": list(self.next_actions),
            "failed_gate_names": list(self.failed_gate_names),
        }


@dataclass(frozen=True)
class JournalBatchDryRunSummary:
    status: str
    input_count: int
    ready_count: int
    blocked_count: int
    next_action_counts: dict[str, int]
    failed_gate_counts: dict[str, int]
    items: tuple[JournalBatchDryRunItem, ...]
    output_json: Path | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "input_count": self.input_count,
            "ready_count": self.ready_count,
            "blocked_count": self.blocked_count,
            "next_action_counts": self.next_action_counts,
            "failed_gate_counts": self.failed_gate_counts,
            "items": [item.to_redacted_dict() for item in self.items],
            "output_json": str(self.output_json) if self.output_json else "",
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
        }


def run_journal_batch_dry_run_from_file(
    *,
    input_json: Path,
    output_json: Path | None = None,
    db_path: Path | None = None,
    actor: str = "codex",
) -> JournalBatchDryRunSummary:
    input_json = require_ignored_untracked_data_path(input_json, purpose="journal batch dry-run input")
    if output_json:
        output_json = require_ignored_untracked_data_path(output_json, purpose="journal batch dry-run output")
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    raw_items = _load_items(payload)
    db = Phase12Database(db_path)
    db.initialize()
    items: list[JournalBatchDryRunItem] = []
    with db.connect() as conn:
        sdk = MoneyForwardDryRunSDK(conn, actor=actor)
        for index, raw_item in enumerate(raw_items):
            candidate_payload = raw_item.get("candidate")
            evidence_payload = raw_item.get("evidence")
            if not isinstance(candidate_payload, dict) or not isinstance(evidence_payload, dict):
                raise ValueError("each journal batch dry-run item requires candidate and evidence objects")
            plan = sdk.plan_journal_create(Evidence(**evidence_payload), JournalCandidate(**candidate_payload))
            journal_payload = raw_item.get("journal")
            execution_channel = str(raw_item.get("execution_channel") or JOURNAL_CREATE_EXECUTION_CHANNEL)
            expected_api_action = JOURNAL_CREATE_API_ACTION
            write_request_json = ""
            binding_proof = {
                "source_row_hash": "",
                "transaction_facts_hash": "",
                "connected_account_binding_hash": "",
                "connected_ledger_binding_hash": "",
            }
            request_payload: dict[str, Any] | None = None
            if execution_channel == TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL:
                if not isinstance(journal_payload, dict):
                    raise ValueError(
                        "transaction_journalize item requires an exact one-branch journal with resolved master IDs"
                    )
                binding = raw_item.get("moneyforward_transaction_binding")
                if not isinstance(binding, dict):
                    raise ValueError("transaction_journalize item requires moneyforward_transaction_binding")
                validate_transaction_journalize_candidate_mapping(
                    conn=conn,
                    journal=journal_payload,
                    binding=binding,
                    candidate=candidate_payload,
                )
                binding_proof = transaction_journalize_binding_proof(binding)
                request_payload = build_transaction_journalize_request(journal=journal_payload, binding=binding)
                metadata = journal_write_request_metadata(
                    request_payload,
                    office_id=str(raw_item.get("office_id") or "") or None,
                )
                expected_api_action = str(metadata["expected_api_action"])
                if output_json is not None:
                    request_dir = output_json.parent / "transaction_journalize_requests"
                    request_dir.mkdir(parents=True, exist_ok=True)
                    request_path = request_dir / f"item_{index:04d}.local.json"
                    request_path.write_text(
                        json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    write_request_json = str(request_path)
                conn.execute(
                    """
                    UPDATE write_intents
                    SET request_hash = ?
                    WHERE idempotency_key = ?
                      AND operation_type = 'journal_create'
                    """,
                    (metadata["request_hash"], plan.idempotency_key),
                )
            elif execution_channel != JOURNAL_CREATE_EXECUTION_CHANNEL:
                raise ValueError(f"unsupported journal execution channel: {execution_channel}")
            if journal_payload is not None:
                if not isinstance(journal_payload, dict):
                    raise ValueError("journal batch dry-run journal must be an object")
                if execution_channel == JOURNAL_CREATE_EXECUTION_CHANNEL:
                    metadata = journal_write_request_metadata(
                        journal_payload,
                        office_id=str(raw_item.get("office_id") or "") or None,
                    )
                    conn.execute(
                        """
                        UPDATE write_intents
                        SET request_hash = ?
                        WHERE idempotency_key = ?
                          AND operation_type = 'journal_create'
                        """,
                        (metadata["request_hash"], plan.idempotency_key),
                    )
            failed_gate_names = tuple(_failed_gate_names(plan.reasons))
            items.append(
                JournalBatchDryRunItem(
                    index=index,
                    status="ready" if plan.registration_allowed else "blocked",
                    idempotency_key=plan.idempotency_key,
                    candidate_hash=plan.candidate_hash,
                    dry_run_result_hash=plan.dry_run_result_hash,
                    registration_allowed=plan.registration_allowed,
                    execution_channel=execution_channel,
                    expected_api_action=expected_api_action,
                    write_request_json=write_request_json,
                    source_row_hash=binding_proof["source_row_hash"],
                    transaction_facts_hash=binding_proof["transaction_facts_hash"],
                    connected_account_binding_hash=binding_proof["connected_account_binding_hash"],
                    connected_ledger_binding_hash=binding_proof["connected_ledger_binding_hash"],
                    next_actions=plan.next_actions,
                    failed_gate_names=failed_gate_names,
                )
            )
    ready_count = sum(1 for item in items if item.registration_allowed)
    blocked_count = len(items) - ready_count
    next_action_counts: Counter[str] = Counter()
    failed_gate_counts: Counter[str] = Counter()
    for item in items:
        next_action_counts.update(item.next_actions)
        failed_gate_counts.update(item.failed_gate_names)
    summary = JournalBatchDryRunSummary(
        status="ready" if blocked_count == 0 else "blocked",
        input_count=len(items),
        ready_count=ready_count,
        blocked_count=blocked_count,
        next_action_counts=dict(sorted(next_action_counts.items())),
        failed_gate_counts=dict(sorted(failed_gate_counts.items())),
        items=tuple(items),
        output_json=output_json,
    )
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return summary


def _load_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = payload.get("items")
    else:
        raw_items = payload
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("journal batch dry-run input requires a non-empty items array")
    if not all(isinstance(item, dict) for item in raw_items):
        raise ValueError("journal batch dry-run items must be objects")
    return raw_items
