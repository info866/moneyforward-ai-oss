from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .drive_policy import drive_move_allowed_for_status, drive_move_request, require_source_route
from .hash_utils import mask_identifier, normalize_sha256_hash, sha256_hash_variants
from .models import Evidence


@dataclass(frozen=True)
class DriveBatchItem:
    google_drive_file_id: str
    file_name: str
    file_hash: str
    processing_status: str
    verification_passed: bool
    parent_folder_id: str | None = None
    parent_folder_name: str | None = None
    moneyforward_journal_no: str | None = None
    voucher_attached: bool = False
    idempotency_key: str | None = None
    intake_block_reason: str | None = None


@dataclass(frozen=True)
class DriveBatchDecision:
    file_name: str
    file_id_hash: str
    processing_status: str
    action: str
    reason: str
    move_request: dict[str, str] | None = None
    moneyforward_journal_no: str | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "file_id_hash": self.file_id_hash,
            "processing_status": self.processing_status,
            "action": self.action,
            "reason": self.reason,
            "move_request": self._redacted_move_request(),
            "moneyforward_journal_no_hash": stable_hash(self.moneyforward_journal_no) if self.moneyforward_journal_no else None,
        }

    def _redacted_move_request(self) -> dict[str, str] | None:
        if self.move_request is None:
            return None
        redacted = {
            "file_id_hash": stable_hash(self.move_request["file_id"]),
            "source_folder_id_hash": stable_hash(self.move_request.get("source_folder_id", "")),
            "destination_folder_id_hash": stable_hash(self.move_request["destination_folder_id"]),
            "file_name": self.move_request["file_name"],
            "rename": self.move_request["rename"],
        }
        if self.move_request.get("idempotency_key"):
            redacted["idempotency_key_hash"] = stable_hash(self.move_request["idempotency_key"])
        return redacted


@dataclass(frozen=True)
class DriveBatchPersistResult:
    input_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_count": self.input_count,
            "inserted_count": self.inserted_count,
            "updated_count": self.updated_count,
            "unchanged_count": self.unchanged_count,
            "summary": self.summary,
            "raw_drive_file_ids_stored": False,
            "raw_moneyforward_ids_stored": False,
        }


@dataclass(frozen=True)
class DriveBatchPlan:
    input_count: int
    summary: dict[str, int]
    ordered_actions: list[str]
    next_action: str
    completion_blockers: list[str]
    decisions: list[DriveBatchDecision]

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "input_count": self.input_count,
            "summary": self.summary,
            "ordered_actions": self.ordered_actions,
            "next_action": self.next_action,
            "completion_blockers": self.completion_blockers,
            "move_request_count": self.summary.get("move_to_processed", 0),
            "read_back_required_count": self.summary.get("read_back_verify_before_move", 0),
            "journal_candidate_count": self.summary.get("create_journal_candidate", 0),
            "blocked_count": self.summary.get("blocked", 0),
            "decisions": [decision.to_redacted_dict() for decision in self.decisions],
            "raw_drive_file_ids_stored": False,
            "raw_moneyforward_ids_stored": False,
        }


@dataclass(frozen=True)
class DriveMoveExecutionPlan:
    move_requests: list[dict[str, str]]
    skipped: list[DriveBatchDecision]

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.move_requests else "empty",
            "operation": "google_drive_files_move",
            "move_request_count": len(self.move_requests),
            "move_requests": self.move_requests,
            "preserve_file_name": True,
            "rename_allowed": False,
            "requires_user_approval_before_execute": True,
            "raw_drive_file_ids_stored_in_ignored_local_json": bool(self.move_requests),
            "google_drive_write_called": False,
        }

    def to_redacted_dict(self) -> dict[str, Any]:
        redacted_requests = [
            _redacted_move_request(request)
            for request in self.move_requests
        ]
        return {
            "status": "ready" if self.move_requests else "empty",
            "operation": "google_drive_files_move",
            "move_request_count": len(self.move_requests),
            "move_requests": redacted_requests,
            "skipped_count": len(self.skipped),
            "skipped_summary": _summary(self.skipped),
            "preserve_file_name": True,
            "rename_allowed": False,
            "requires_user_approval_before_execute": True,
            "raw_drive_file_ids_printed": False,
            "raw_drive_file_ids_stored_in_ignored_local_json": bool(self.move_requests),
            "raw_idempotency_keys_printed": False,
            "google_drive_write_called": False,
        }


def _redacted_move_request(request: dict[str, str]) -> dict[str, str]:
    redacted = {
                "file_id_hash": stable_hash(request["file_id"]),
                "source_folder_id_hash": stable_hash(request.get("source_folder_id", "")),
                "destination_folder_id_hash": stable_hash(request["destination_folder_id"]),
                "file_name": request["file_name"],
                "rename": request["rename"],
            }
    if request.get("idempotency_key"):
        redacted["idempotency_key_hash"] = stable_hash(request["idempotency_key"])
    return redacted


@dataclass(frozen=True)
class JournalCandidateEvidenceBundle:
    items: list[dict[str, Any]]

    def to_local_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.items else "empty",
            "item_count": len(self.items),
            "items": self.items,
            "next_step": "fill candidate objects, then run phase12-journal-batch-prepare",
            "raw_drive_file_ids_stored_in_ignored_local_json": bool(self.items),
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
        }

    def to_redacted_dict(self) -> dict[str, Any]:
        redacted_items = [
            {
                "index": item["index"],
                "file_name": item["evidence"]["file_name"],
                "file_id_hash": mask_identifier(item["evidence"]["google_drive_file_id"]),
                "file_hash": item["evidence"]["file_hash"],
                "parent_folder_id_hash": stable_hash(item["evidence"].get("parent_folder_id") or ""),
                "parent_folder_name": item["evidence"].get("parent_folder_name"),
                "evidence_type": item["evidence_type"],
                "suggested_source_type": item["suggested_source_type"],
                "candidate_required": item["candidate_required"],
            }
            for item in self.items
        ]
        return {
            "status": "ready" if self.items else "empty",
            "item_count": len(self.items),
            "items": redacted_items,
            "next_step": "fill candidate objects, then run phase12-journal-batch-prepare",
            "raw_drive_file_ids_printed": False,
            "raw_drive_file_ids_stored_in_ignored_local_json": bool(self.items),
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
        }


def build_drive_batch_plan(
    items: list[DriveBatchItem], *, destination_folder_id: str | None = None
) -> DriveBatchPlan:
    decisions = build_drive_batch_decisions(items, destination_folder_id=destination_folder_id)
    summary = _summary(decisions)
    ordered_actions = [action for action in _ACTION_PRIORITY if summary.get(action, 0) > 0]
    return DriveBatchPlan(
        input_count=len(items),
        summary=summary,
        ordered_actions=ordered_actions,
        next_action=_next_batch_action(summary),
        completion_blockers=_completion_blockers(summary),
        decisions=decisions,
    )


def build_drive_batch_decisions(
    items: list[DriveBatchItem], *, destination_folder_id: str | None = None
) -> list[DriveBatchDecision]:
    return [
        build_drive_batch_decision(item, destination_folder_id=destination_folder_id)
        for item in items
    ]


def build_drive_move_execution_plan(
    items: list[DriveBatchItem], *, destination_folder_id: str | None = None
) -> DriveMoveExecutionPlan:
    decisions = build_drive_batch_decisions(items, destination_folder_id=destination_folder_id)
    move_requests = [decision.move_request for decision in decisions if decision.action == "move_to_processed"]
    return DriveMoveExecutionPlan(
        move_requests=[request for request in move_requests if request is not None],
        skipped=[decision for decision in decisions if decision.action != "move_to_processed"],
    )


def write_drive_move_execution_plan(
    items: list[DriveBatchItem], output_path: Path, *, destination_folder_id: str | None = None
) -> DriveMoveExecutionPlan:
    plan = build_drive_move_execution_plan(items, destination_folder_id=destination_folder_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(plan.to_local_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return plan


def build_journal_candidate_evidence_bundle(items: list[DriveBatchItem]) -> JournalCandidateEvidenceBundle:
    decisions = build_drive_batch_decisions(items)
    bundle_items: list[dict[str, Any]] = []
    for index, (item, decision) in enumerate(zip(items, decisions)):
        if decision.action != "create_journal_candidate":
            continue
        evidence_type = classify_evidence_type(item)
        bundle_items.append(
            {
                "index": index,
                "evidence": {
                    "google_drive_file_id": item.google_drive_file_id,
                    "file_name": item.file_name,
                    "file_hash": item.file_hash,
                    "parent_folder_id": item.parent_folder_id,
                    "parent_folder_name": item.parent_folder_name,
                },
                "evidence_type": evidence_type,
                "suggested_source_type": _source_type_for_evidence_type(evidence_type),
                "candidate_required": True,
                "candidate": None,
            }
        )
    return JournalCandidateEvidenceBundle(bundle_items)


def write_journal_candidate_evidence_bundle(
    items: list[DriveBatchItem],
    output_path: Path,
) -> JournalCandidateEvidenceBundle:
    plan = build_journal_candidate_evidence_bundle(items)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(plan.to_local_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return plan


def build_drive_batch_decision(
    item: DriveBatchItem, *, destination_folder_id: str | None = None
) -> DriveBatchDecision:
    require_source_route(Evidence(
        google_drive_file_id=item.google_drive_file_id, file_name=item.file_name,
        file_hash=item.file_hash, parent_folder_id=item.parent_folder_id,
        parent_folder_name=item.parent_folder_name,
    ))
    if item.processing_status == "not_applicable":
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status="not_applicable",
            action="record_no_journal_required",
            reason=str(item.intake_block_reason or "verified_no_journal_required"),
            moneyforward_journal_no=None,
        )

    if str(item.intake_block_reason or "").startswith(
        "verified_no_company_financial_event"
    ):
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status="not_applicable",
            action="record_no_journal_required",
            reason=str(item.intake_block_reason),
            moneyforward_journal_no=None,
        )

    if str(item.intake_block_reason or "").startswith(
        "external_entity_not_current_company"
    ):
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status="blocked",
            action="resolve_entity_relationship",
            reason="entity_relationship_requires_full_facts_and_rag",
            moneyforward_journal_no=None,
        )

    if drive_move_allowed_for_status(item.processing_status, item.verification_passed):
        evidence = Evidence(
            google_drive_file_id=item.google_drive_file_id,
            file_name=item.file_name,
            file_hash=item.file_hash,
            parent_folder_id=item.parent_folder_id,
            parent_folder_name=item.parent_folder_name,
        )
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status=item.processing_status,
            action="move_to_processed",
            reason="journal_created_voucher_attached_read_back_verified",
            move_request=_move_request_with_chain(
                evidence,
                item.idempotency_key,
                destination_folder_id=destination_folder_id,
            ),
            moneyforward_journal_no=item.moneyforward_journal_no,
        )

    if item.processing_status in {"unprocessed", "new"}:
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status=item.processing_status,
            action="create_journal_candidate",
            reason="not_registered_yet",
            moneyforward_journal_no=item.moneyforward_journal_no,
        )

    if item.processing_status in {"journal_created", "voucher_attached"} or item.voucher_attached:
        return DriveBatchDecision(
            file_name=item.file_name,
            file_id_hash=stable_hash(item.google_drive_file_id),
            processing_status=item.processing_status,
            action="read_back_verify_before_move",
            reason="moneyforward_read_back_not_verified",
            moneyforward_journal_no=item.moneyforward_journal_no,
        )

    return DriveBatchDecision(
        file_name=item.file_name,
        file_id_hash=stable_hash(item.google_drive_file_id),
        processing_status=item.processing_status,
        action="blocked",
        reason="unknown_or_error_status",
        moneyforward_journal_no=item.moneyforward_journal_no,
    )


def classify_evidence_type(item: DriveBatchItem) -> str:
    text = f"{item.parent_folder_name or ''} {item.file_name}"
    if "請求書" in text:
        return "invoice"
    if any(marker in text for marker in ("カード", "立替")):
        return "card_or_reimbursement"
    if any(marker in text for marker in ("給与", "賞与", "勤怠")):
        return "payroll_or_special"
    return "general_receipt"


def _source_type_for_evidence_type(evidence_type: str) -> str:
    if evidence_type == "invoice":
        return "invoice"
    if evidence_type == "payroll_or_special":
        return "special"
    return "receipt"


def stable_hash(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def item_from_manifest_row(row: dict[str, Any]) -> DriveBatchItem:
    return DriveBatchItem(
        google_drive_file_id=str(row["google_drive_file_id"]),
        file_name=str(row["file_name"]),
        file_hash=normalize_sha256_hash(str(row.get("file_hash") or "")),
        processing_status=str(row.get("processing_status") or "unprocessed"),
        verification_passed=bool(row.get("verification_passed", False)),
        parent_folder_id=row.get("parent_folder_id"),
        parent_folder_name=row.get("parent_folder_name"),
        moneyforward_journal_no=row.get("moneyforward_journal_no"),
        voucher_attached=bool(row.get("voucher_attached", False)),
        idempotency_key=row.get("idempotency_key"),
        intake_block_reason=row.get("intake_block_reason"),
    )


def _move_request_with_chain(
    evidence: Evidence,
    idempotency_key: str | None,
    *,
    destination_folder_id: str | None = None,
) -> dict[str, str]:
    request = (
        drive_move_request(evidence, destination_folder_id)
        if destination_folder_id
        else drive_move_request(evidence)
    )
    if idempotency_key:
        request["idempotency_key"] = idempotency_key
    return request


def persist_drive_batch_audit(conn: sqlite3.Connection, items: list[DriveBatchItem]) -> DriveBatchPersistResult:
    decisions = build_drive_batch_decisions(items)
    inserted = 0
    updated = 0
    unchanged = 0
    for item, decision in zip(items, decisions):
        result = _persist_drive_batch_item(conn, item, decision)
        if result == "inserted":
            inserted += 1
        elif result == "updated":
            updated += 1
        else:
            unchanged += 1
    _insert_batch_summary_audit_log(conn, items, decisions)
    conn.commit()
    return DriveBatchPersistResult(
        input_count=len(items),
        inserted_count=inserted,
        updated_count=updated,
        unchanged_count=unchanged,
        summary=_summary(decisions),
    )


def _persist_drive_batch_item(
    conn: sqlite3.Connection,
    item: DriveBatchItem,
    decision: DriveBatchDecision,
) -> str:
    file_id_hash = mask_identifier(item.google_drive_file_id)
    file_hash = normalize_sha256_hash(item.file_hash)
    candidate_hash = stable_hash(
        {
            "file_id_hash": file_id_hash,
            "file_hash": file_hash,
            "file_name_hash": stable_hash(item.file_name),
            "batch_action": decision.action,
        }
    )
    dry_run_hash = stable_hash(decision.to_redacted_dict())
    idempotency_key = stable_hash(
        {
            "file_id_hash": file_id_hash,
            "file_hash": file_hash,
            "candidate_hash": candidate_hash,
            "dry_run_result_hash": dry_run_hash,
            "purpose": "drive_folder_batch_audit",
        }
    )
    ledger_status = _ledger_status_for_decision(decision)
    hash_variants = sha256_hash_variants(file_hash)
    placeholders = ",".join("?" for _ in hash_variants)
    existing = conn.execute(
        f"""
        SELECT id, idempotency_key, processing_status, moneyforward_journal_id_hash,
               moneyforward_voucher_id_hash, dry_run_result_hash
        FROM processing_ledger
        WHERE google_drive_file_id_hash = ? AND file_hash IN ({placeholders})
        ORDER BY CASE WHEN file_hash = ? THEN 0 ELSE 1 END, updated_at DESC, id DESC
        LIMIT 1
        """,
        (file_id_hash, *hash_variants, file_hash),
    ).fetchone()
    journal_hash = mask_identifier(item.moneyforward_journal_no) if item.moneyforward_journal_no else None
    voucher_hash = stable_hash({"voucher_attached": True, "file_id_hash": file_id_hash}) if item.voucher_attached else None
    if existing is None:
        conn.execute(
            """
            INSERT INTO processing_ledger (
                google_drive_file_id_hash, file_name, file_hash, candidate_hash,
                idempotency_key, processing_status, moneyforward_journal_id_hash,
                moneyforward_voucher_id_hash, dry_run_result_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id_hash,
                item.file_name,
                file_hash,
                candidate_hash,
                idempotency_key,
                ledger_status,
                journal_hash,
                voucher_hash,
                dry_run_hash,
            ),
        )
        _insert_batch_audit_log(conn, idempotency_key, item, decision)
        return "inserted"

    audit_idempotency_key = existing["idempotency_key"]
    next_status = _max_status(existing["processing_status"], ledger_status)
    next_journal_hash = existing["moneyforward_journal_id_hash"] or journal_hash
    next_voucher_hash = existing["moneyforward_voucher_id_hash"] or voucher_hash
    changed = any(
        [
            existing["processing_status"] != next_status,
            existing["moneyforward_journal_id_hash"] != next_journal_hash,
            existing["moneyforward_voucher_id_hash"] != next_voucher_hash,
            existing["dry_run_result_hash"] != dry_run_hash,
        ]
    )
    if not changed:
        _insert_batch_audit_log(conn, audit_idempotency_key, item, decision)
        return "unchanged"
    conn.execute(
        """
        UPDATE processing_ledger
        SET file_name = ?,
            file_hash = ?,
            candidate_hash = ?,
            processing_status = ?,
            moneyforward_journal_id_hash = ?,
            moneyforward_voucher_id_hash = ?,
            dry_run_result_hash = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (
            item.file_name,
            file_hash,
            candidate_hash,
            next_status,
            next_journal_hash,
            next_voucher_hash,
            dry_run_hash,
            existing["id"],
        ),
    )
    _insert_batch_audit_log(conn, audit_idempotency_key, item, decision)
    return "updated"


def _insert_batch_audit_log(
    conn: sqlite3.Connection,
    idempotency_key: str,
    item: DriveBatchItem,
    decision: DriveBatchDecision,
) -> None:
    request = {
        "file_id_hash": mask_identifier(item.google_drive_file_id),
        "file_hash": normalize_sha256_hash(item.file_hash),
        "file_name_hash": stable_hash(item.file_name),
        "processing_status": item.processing_status,
        "verification_passed": item.verification_passed,
    }
    response = decision.to_redacted_dict()
    event_hash = stable_hash(
        {
            "event_type": "drive_folder_batch_audit",
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
        ) VALUES (?, 'drive_folder_batch_audit', ?, ?, ?, 'codex', 1, ?)
        """,
        (
            event_hash,
            idempotency_key,
            stable_hash(request),
            stable_hash(response),
            "No Money Forward or Google Drive write is performed by batch audit persistence.",
        ),
    )


def _insert_batch_summary_audit_log(
    conn: sqlite3.Connection,
    items: list[DriveBatchItem],
    decisions: list[DriveBatchDecision],
) -> None:
    request = {
        "input_count": len(items),
        "file_id_hashes": [mask_identifier(item.google_drive_file_id) for item in items],
        "file_hashes": [item.file_hash for item in items],
    }
    response = {
        "summary": _summary(decisions),
        "next_action": _next_batch_action(_summary(decisions)),
        "completion_blockers": _completion_blockers(_summary(decisions)),
    }
    event_hash = stable_hash(
        {
            "event_type": "drive_folder_batch_audit",
            "request_hash": stable_hash(request),
            "response_hash": stable_hash(response),
            "scope": "folder_summary",
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_logs (
            event_hash, event_type, idempotency_key, request_hash, response_hash,
            actor, dry_run, rollback_plan
        ) VALUES (?, 'drive_folder_batch_audit', NULL, ?, ?, 'codex', 1, ?)
        """,
        (
            event_hash,
            stable_hash(request),
            stable_hash(response),
            "Folder-level batch audit summary. No Money Forward or Google Drive write is performed.",
        ),
    )


def _ledger_status_for_decision(decision: DriveBatchDecision) -> str:
    if decision.action == "record_no_journal_required":
        return "not_applicable"
    if decision.action == "move_to_processed":
        return "verified"
    if decision.action == "read_back_verify_before_move":
        return "voucher_attached"
    if decision.action == "create_journal_candidate":
        return "new"
    return "blocked"


def _max_status(current: str, incoming: str) -> str:
    return current if _STATUS_RANK[current] >= _STATUS_RANK[incoming] else incoming


def _summary(decisions: list[DriveBatchDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return counts


def _next_batch_action(summary: dict[str, int]) -> str:
    if summary.get("resolve_entity_relationship", 0) > 0:
        return "resolve_entity_relationship_with_full_facts_and_rag"
    if summary.get("blocked", 0) > 0:
        return "resolve_blocked_items"
    if summary.get("read_back_verify_before_move", 0) > 0:
        return "run_moneyforward_read_back_for_attached_items"
    if summary.get("move_to_processed", 0) > 0:
        return "move_verified_files_to_processed_folder"
    if summary.get("create_journal_candidate", 0) > 0:
        return "create_journal_candidates_in_batch"
    return "no_files_to_process"


def _completion_blockers(summary: dict[str, int]) -> list[str]:
    blockers: list[str] = []
    if summary.get("resolve_entity_relationship", 0) > 0:
        blockers.append("entity_relationship_unresolved")
    if summary.get("blocked", 0) > 0:
        blockers.append("blocked_items_present")
    if summary.get("read_back_verify_before_move", 0) > 0:
        blockers.append("moneyforward_read_back_required")
    if summary.get("create_journal_candidate", 0) > 0:
        blockers.append("unregistered_journal_candidates_present")
    return blockers


_ACTION_PRIORITY = [
    "resolve_entity_relationship",
    "blocked",
    "read_back_verify_before_move",
    "move_to_processed",
    "create_journal_candidate",
]


_STATUS_RANK = {
    "new": 10,
    "planned": 20,
    "blocked": 25,
    "failed": 30,
    "journal_created": 40,
    "voucher_attached": 50,
    "excluded": 60,
    "not_applicable": 65,
    "verified": 70,
}
