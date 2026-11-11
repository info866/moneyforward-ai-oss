from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .journal_batch_dry_run import run_journal_batch_dry_run_from_file
from .journal_batch_router import route_journal_batch_next_actions
from .local_artifacts import require_ignored_untracked_data_path


@dataclass(frozen=True)
class JournalBatchPrepareSummary:
    status: str
    input_json: Path
    output_dir: Path
    dry_run_json: Path
    route_summary_json: Path
    dry_run: dict[str, Any]
    routes: dict[str, Any]

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "input_json": str(self.input_json),
            "output_dir": str(self.output_dir),
            "dry_run_json": str(self.dry_run_json),
            "route_summary_json": str(self.route_summary_json),
            "dry_run": self.dry_run,
            "routes": self.routes,
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
        }


def prepare_journal_batch(
    *,
    input_json: Path,
    output_dir: Path,
    db_path: Path | None = None,
    actor: str = "codex",
) -> JournalBatchPrepareSummary:
    input_json = require_ignored_untracked_data_path(input_json, purpose="journal batch prepare input")
    output_dir = require_ignored_untracked_data_path(output_dir, purpose="journal batch prepare output")
    output_dir.mkdir(parents=True, exist_ok=True)
    dry_run_json = output_dir / "dry_run.local.json"
    dry_run = run_journal_batch_dry_run_from_file(
        input_json=input_json,
        output_json=dry_run_json,
        db_path=db_path,
        actor=actor,
    )
    routes_dir = output_dir / "routes"
    routes = route_journal_batch_next_actions(
        batch_input_json=input_json,
        dry_run_json=dry_run_json,
        output_dir=routes_dir,
        db_path=db_path,
    )
    status = "ready" if dry_run.ready_count == dry_run.input_count and routes.routed_count == 0 else "prepared"
    summary = JournalBatchPrepareSummary(
        status=status,
        input_json=input_json,
        output_dir=output_dir,
        dry_run_json=dry_run_json,
        route_summary_json=routes_dir / "route_summary.local.json",
        dry_run=dry_run.to_redacted_dict(),
        routes=routes.to_redacted_dict(),
    )
    summary_path = output_dir / "prepare_summary.local.json"
    summary_path.write_text(
        json.dumps(summary.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary
