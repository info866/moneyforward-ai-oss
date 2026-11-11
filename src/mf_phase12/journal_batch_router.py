from __future__ import annotations

import json
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .db import Phase12Database
from .drive_policy import (
    DIRECT_LEDGER_SOURCE_TYPES,
    FINANCIAL_STATEMENT_SOURCE_TYPES,
    apply_drive_credit_policy,
    decide_credit_side,
    folder_credit_decision,
    resolve_credit_side_master,
)
from .models import Evidence, JournalCandidate
from .office_profile import get_current_profile
from .sub_account_request import build_sub_account_create_candidate_draft
from .trade_partner_request import build_trade_partner_create_request_draft


@dataclass(frozen=True)
class JournalBatchRouteSummary:
    status: str
    input_count: int
    routed_count: int
    route_counts: dict[str, int]
    items: tuple[dict[str, Any], ...]
    output_dir: Path

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "input_count": self.input_count,
            "routed_count": self.routed_count,
            "route_counts": self.route_counts,
            "items": list(self.items),
            "output_dir": str(self.output_dir),
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
        }


def route_journal_batch_next_actions(
    *,
    batch_input_json: Path,
    dry_run_json: Path,
    output_dir: Path,
    db_path: Path | None = None,
) -> JournalBatchRouteSummary:
    batch_input_json = require_ignored_untracked_data_path(batch_input_json, purpose="journal batch router input")
    dry_run_json = require_ignored_untracked_data_path(dry_run_json, purpose="journal batch dry-run summary")
    output_dir = require_ignored_untracked_data_path(output_dir, purpose="journal batch route output")
    batch_payload = json.loads(batch_input_json.read_text(encoding="utf-8"))
    dry_payload = json.loads(dry_run_json.read_text(encoding="utf-8"))
    batch_items = _items_from_batch_input(batch_payload)
    dry_items = dry_payload.get("items") if isinstance(dry_payload, dict) else None
    if not isinstance(dry_items, list):
        raise ValueError("dry-run summary must contain items")
    if len(batch_items) != len(dry_items):
        raise ValueError("batch input item count and dry-run item count differ")
    output_dir.mkdir(parents=True, exist_ok=True)
    routed: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    db = Phase12Database(db_path) if db_path else None
    if db:
        db.initialize()
    with db.connect() if db else nullcontext() as conn:
        for index, dry_item in enumerate(dry_items):
            if not isinstance(dry_item, dict):
                raise ValueError("dry-run item must be an object")
            candidate = _object(batch_items[index].get("candidate"), "candidate")
            evidence = _object(batch_items[index].get("evidence"), "evidence")
            normalized_candidate = _normalized_candidate(
                JournalCandidate(**candidate),
                Evidence(**evidence),
                conn,
            ).to_dict()
            if dry_item.get("candidate_hash") != stable_hash(normalized_candidate):
                raise ValueError(f"candidate_hash mismatch at item {index}")
            for action in dry_item.get("next_actions") or []:
                if action == "run_phase12_trade_partner_request":
                    routed.append(_route_trade_partner(index, normalized_candidate, evidence, dry_item, output_dir))
                    route_counts[action] += 1
                elif action == "run_phase12_sub_account_request":
                    routed.append(_route_sub_account(index, normalized_candidate, evidence, dry_item, output_dir))
                    route_counts[action] += 1
    summary = JournalBatchRouteSummary(
        status="ready" if routed else "no_routes",
        input_count=len(batch_items),
        routed_count=len(routed),
        route_counts=dict(sorted(route_counts.items())),
        items=tuple(routed),
        output_dir=output_dir,
    )
    summary_path = output_dir / "route_summary.local.json"
    summary_path.write_text(
        json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _route_trade_partner(
    index: int,
    candidate: dict[str, Any],
    evidence: dict[str, Any],
    dry_item: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    input_path = output_dir / "trade_partner_inputs" / f"item_{index:04d}.local.json"
    output_path = output_dir / "trade_partner_requests" / f"item_{index:04d}.local.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    partner_name = str(candidate.get("trade_partner_name") or candidate.get("vendor_name") or "").strip()
    invoice_registration_number = _valid_invoice_registration_number(candidate.get("invoice_number"))
    payload = {
        "name": partner_name,
        "invoice_registration_number": invoice_registration_number,
        "candidate_hash": dry_item.get("candidate_hash"),
        "source_file_id_hash": mask_identifier(str(evidence.get("google_drive_file_id") or "")),
        "reason": "batch_route_missing_trade_partner_master",
    }
    input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    draft = build_trade_partner_create_request_draft(input_path, output_path)
    return {
        "index": index,
        "route": "trade_partner_request",
        "input_json": str(input_path),
        "output_json": str(output_path),
        "candidate_hash": dry_item.get("candidate_hash"),
        "request_hash": draft.request_hash,
        "partner_name_hash": draft.partner_name_hash,
        "registration_allowed": False,
    }


def _route_sub_account(
    index: int,
    candidate: dict[str, Any],
    evidence: dict[str, Any],
    dry_item: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    input_path = output_dir / "sub_account_inputs" / f"item_{index:04d}.local.json"
    output_path = output_dir / "sub_account_requests" / f"item_{index:04d}.local.json"
    profile = get_current_profile()
    if profile is None:
        raise ValueError("sub account routing requires an explicit current company profile")
    decision = decide_credit_side(JournalCandidate(**candidate), Evidence(**evidence))
    payload = {
        "company_slug": profile.slug,
        "account_name": decision.credit_account,
        "sub_account_name": decision.credit_sub_account,
        "candidate_hash": dry_item.get("candidate_hash"),
        "source_file_id_hash": mask_identifier(str(evidence.get("google_drive_file_id") or "")),
        "reason": "batch_route_missing_sub_account_master",
    }
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    draft = build_sub_account_create_candidate_draft(input_path, output_path)
    return {
        "index": index,
        "route": "sub_account_create_resume",
        "input_json": str(input_path),
        "output_json": str(output_path),
        "candidate_hash": dry_item.get("candidate_hash"),
        "request_hash": draft.candidate_hash,
        "account_name_hash": draft.account_name_hash,
        "sub_account_name_hash": draft.sub_account_name_hash,
        "registration_allowed": False,
        "user_confirmation_required": False,
        "auto_resume_required": True,
        "next_action": draft.next_action,
    }


def _items_from_batch_input(payload: Any) -> list[dict[str, Any]]:
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("batch input must contain a non-empty items array")
    return [_object(item, "batch item") for item in raw_items]


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _valid_invoice_registration_number(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 14 and text.startswith("T") and text[1:].isdigit():
        return text
    return None


def _normalized_candidate(
    candidate: JournalCandidate,
    evidence: Evidence,
    conn: Any | None,
) -> JournalCandidate:
    candidate = apply_drive_credit_policy(candidate, evidence)
    # Folder ownership is mandatory for person folders, including their
    # temporary subfolders; a card source type cannot bypass it.
    if candidate.source_type in FINANCIAL_STATEMENT_SOURCE_TYPES and folder_credit_decision(evidence) is None:
        return candidate
    if candidate.source_type in DIRECT_LEDGER_SOURCE_TYPES:
        return candidate
    if conn is None:
        return candidate
    decision = resolve_credit_side_master(conn, candidate, evidence)
    if candidate.credit_account == decision.credit_account and candidate.credit_sub_account == decision.credit_sub_account:
        return candidate
    return replace(candidate, credit_account=decision.credit_account, credit_sub_account=decision.credit_sub_account)
