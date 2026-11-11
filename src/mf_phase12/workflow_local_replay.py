from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Phase12Database
from .drive_batch import DriveBatchItem, build_journal_candidate_evidence_bundle
from .hash_utils import mask_identifier, normalize_sha256_hash, stable_hash
from .journal_batch_input import build_journal_batch_input_from_evidence_bundle
from .journal_batch_prepare import prepare_journal_batch
from .journal_voucher_handoff import build_voucher_prepare_from_journal_queue
from .journal_write_queue import build_journal_write_queue
from .local_artifacts import require_ignored_untracked_data_path
from .models import Evidence
from .skills import EvidenceFacts, classify_receipt_from_facts


@dataclass(frozen=True)
class WorkflowLocalReplaySummary:
    status: str
    facts_json: Path
    output_dir: Path
    db_path: Path
    evidence_chain_hash: str
    candidate_hash: str
    idempotency_key: str
    paths: dict[str, Path]
    batch_prepare_status: str
    queued_count: int
    voucher_dry_run_ready: bool

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "facts_json": str(self.facts_json),
            "output_dir": str(self.output_dir),
            "db_path": str(self.db_path),
            "evidence_chain_hash": self.evidence_chain_hash,
            "candidate_hash": self.candidate_hash,
            "idempotency_key": self.idempotency_key,
            "paths": {key: str(value) for key, value in self.paths.items()},
            "batch_prepare_status": self.batch_prepare_status,
            "queued_count": self.queued_count,
            "voucher_dry_run_ready": self.voucher_dry_run_ready,
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "browser_opened": False,
            "n8n_called": False,
            "raw_secret_stored": False,
            "raw_drive_file_id_printed": False,
            "raw_local_file_path_printed": False,
            "raw_journal_id_printed": False,
            "raw_api_response_stored": False,
        }


def run_workflow_local_replay(
    *,
    facts_json: Path,
    output_dir: Path,
    db_path: Path,
    scope_hash: str = "local-replay-voucher-write-scope-hash",
    approved_at: datetime | None = None,
) -> WorkflowLocalReplaySummary:
    facts_json = require_ignored_untracked_data_path(facts_json, purpose="workflow local replay facts input")
    output_dir = require_ignored_untracked_data_path(output_dir, purpose="workflow local replay output")
    db_path = require_ignored_untracked_data_path(db_path, purpose="workflow local replay DB")
    output_dir.mkdir(parents=True, exist_ok=True)
    Phase12Database(db_path).initialize()

    payload = _load_object(facts_json, "workflow local replay facts")
    facts = _facts_from_payload(payload)
    candidate = classify_receipt_from_facts(facts)
    observed = dict(facts.observed)
    local_file_path = Path(str(observed.get("local_file_path") or ""))
    if not local_file_path.is_file():
        raise FileNotFoundError(f"local evidence file not found: {local_file_path}")
    file_hash = normalize_sha256_hash(_sha256_file(local_file_path))
    expected_hash = normalize_sha256_hash(str(observed.get("file_hash") or ""))
    if expected_hash and expected_hash != file_hash:
        raise ValueError("local evidence file hash does not match facts")
    observed["file_hash"] = file_hash

    evidence = Evidence(
        google_drive_file_id=str(observed["evidence_file_id"]),
        file_name=str(observed.get("file_name") or local_file_path.name),
        file_hash=file_hash,
        parent_folder_id=observed.get("parent_folder_id"),
        parent_folder_name=observed.get("parent_folder_name"),
        local_file_path=str(local_file_path),
    )
    drive_item = DriveBatchItem(
        google_drive_file_id=evidence.google_drive_file_id,
        file_name=evidence.file_name,
        file_hash=evidence.file_hash,
        processing_status="new",
        verification_passed=False,
        parent_folder_id=evidence.parent_folder_id,
        parent_folder_name=evidence.parent_folder_name,
    )
    evidence_bundle_json = output_dir / "journal_candidate_evidence.local.json"
    bundle = build_journal_candidate_evidence_bundle([drive_item]).to_local_dict()
    if bundle["item_count"] != 1:
        raise ValueError("workflow local replay expected exactly one journal candidate item")
    bundle["items"][0]["evidence"]["local_file_path"] = str(local_file_path)
    bundle["items"][0]["candidate"] = candidate.to_dict()
    evidence_bundle_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    batch_input_json = output_dir / "journal_batch_input.local.json"
    batch_input = build_journal_batch_input_from_evidence_bundle(
        evidence_bundle_json=evidence_bundle_json,
        output_json=batch_input_json,
    )
    batch_prepare_dir = output_dir / "journal_batch_prepared"
    batch_prepare = prepare_journal_batch(input_json=batch_input_json, output_dir=batch_prepare_dir, db_path=db_path)

    queue_json = output_dir / "journal_write_queue.local.json"
    queue = build_journal_write_queue(
        prepare_summary_json=batch_prepare_dir / "prepare_summary.local.json",
        output_json=queue_json,
        db_path=db_path,
        require_attachment_handoff_ready=True,
    )
    if queue.queued_count != 1:
        raise ValueError(f"workflow local replay expected one queued item, got {queue.queued_count}")

    journal_id_json = output_dir / "journal_id.local.json"
    local_journal_id = f"LOCAL_REPLAY_JOURNAL_{stable_hash({'evidence': evidence.google_drive_file_id})[:12]}"
    journal_id_json.write_text(
        json.dumps(
            {
                "journal_id": local_journal_id,
                "journal_id_hash": mask_identifier(local_journal_id),
                "storage": "ignored_local_data_phase12_only",
                "raw_journal_id_git_tracked": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    approval_record_user_json = output_dir / "voucher_approval_record_user.local.json"
    approval_record_user_json.write_text(
        json.dumps({"user_approval": "local replay approval record only; no external POST"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    execution_user_json = output_dir / "voucher_execution_user.local.json"
    execution_user_json.write_text(
        json.dumps({"user_approval": "local replay execution approval only; no external POST"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    voucher_prepare = build_voucher_prepare_from_journal_queue(
        queue_json=queue_json,
        journal_id_json=journal_id_json,
        scope_hash=scope_hash,
        approval_record_user_approval_json=approval_record_user_json,
        execution_user_approval_json=execution_user_json,
        output_dir=output_dir / "voucher_prepare_from_queue",
        approved_at=approved_at,
    )
    summary = WorkflowLocalReplaySummary(
        status="ready",
        facts_json=facts_json,
        output_dir=output_dir,
        db_path=db_path,
        evidence_chain_hash=stable_hash(
            {
                "file_hash": evidence.file_hash,
                "file_id_hash": mask_identifier(evidence.google_drive_file_id),
                "candidate_hash": stable_hash(candidate.to_dict()),
                "queue_idempotency_key": queue.items[0]["idempotency_key"],
            }
        ),
        candidate_hash=stable_hash(candidate.to_dict()),
        idempotency_key=str(queue.items[0]["idempotency_key"]),
        paths={
            "evidence_bundle_json": evidence_bundle_json,
            "batch_input_json": batch_input_json,
            "batch_prepare_summary_json": batch_prepare_dir / "prepare_summary.local.json",
            "queue_json": queue_json,
            "journal_id_json": journal_id_json,
            "voucher_prepare_dir": voucher_prepare.output_dir,
        },
        batch_prepare_status=batch_prepare.status,
        queued_count=queue.queued_count,
        voucher_dry_run_ready=bool(voucher_prepare.bundle.dry_run["ready_to_post"]),
    )
    (output_dir / "workflow_local_replay_summary.local.json").write_text(
        json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _facts_from_payload(payload: dict[str, Any]) -> EvidenceFacts:
    return EvidenceFacts(
        observed=_object(payload.get("observed"), "observed facts"),
        predicted=_object(payload.get("predicted"), "predicted facts"),
        unresolved_points=tuple(str(item) for item in payload.get("unresolved_points") or ()),
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _object(payload, label)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a filled object")
    return dict(value)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
