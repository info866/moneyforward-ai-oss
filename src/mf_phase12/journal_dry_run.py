from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Phase12Database
from .local_artifacts import require_ignored_untracked_data_path
from .models import Evidence, JournalCandidate
from .sdk import MoneyForwardDryRunSDK


@dataclass(frozen=True)
class JournalDryRunSummary:
    status: str
    idempotency_key: str
    candidate_hash: str
    dry_run_result_hash: str
    registration_allowed: bool
    next_actions: tuple[str, ...]
    failed_gate_names: tuple[str, ...]
    output_json: Path | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "candidate_hash": self.candidate_hash,
            "dry_run_result_hash": self.dry_run_result_hash,
            "registration_allowed": self.registration_allowed,
            "next_actions": list(self.next_actions),
            "failed_gate_names": list(self.failed_gate_names),
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


def run_journal_dry_run_from_file(
    *,
    input_json: Path,
    output_json: Path | None = None,
    db_path: Path | None = None,
    actor: str = "codex",
) -> JournalDryRunSummary:
    input_json = require_ignored_untracked_data_path(input_json, purpose="journal dry-run input")
    if output_json:
        output_json = require_ignored_untracked_data_path(output_json, purpose="journal dry-run output")
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("journal dry-run input must be an object")
    candidate_payload = payload.get("candidate")
    evidence_payload = payload.get("evidence")
    if not isinstance(candidate_payload, dict) or not isinstance(evidence_payload, dict):
        raise ValueError("journal dry-run input requires candidate and evidence objects")
    candidate = JournalCandidate(**candidate_payload)
    evidence = Evidence(**evidence_payload)
    db = Phase12Database(db_path)
    db.initialize()
    with db.connect() as conn:
        plan = MoneyForwardDryRunSDK(conn, actor=actor).plan_journal_create(evidence, candidate)
    failed_gate_names = tuple(_failed_gate_names(plan.reasons))
    summary = JournalDryRunSummary(
        status="ready" if plan.registration_allowed else "blocked",
        idempotency_key=plan.idempotency_key,
        candidate_hash=plan.candidate_hash,
        dry_run_result_hash=plan.dry_run_result_hash,
        registration_allowed=plan.registration_allowed,
        next_actions=plan.next_actions,
        failed_gate_names=failed_gate_names,
        output_json=output_json,
    )
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return summary


def _failed_gate_names(reasons: tuple[str, ...]) -> list[str]:
    failed: list[str] = []
    for reason in reasons:
        name, _, detail = reason.partition(":")
        if any(marker in detail for marker in ("停止", "未解決", "未登録", "失敗", "古い", "対象外", "不正")):
            failed.append(name)
    return list(dict.fromkeys(failed))
