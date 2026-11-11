from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path


@dataclass(frozen=True)
class JournalBatchInputBuildResult:
    status: str
    input_count: int
    output_json: Path
    items: tuple[dict[str, Any], ...]

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "input_count": self.input_count,
            "output_json": str(self.output_json),
            "items": list(self.items),
            "moneyforward_api_called": False,
            "google_drive_write_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_candidate_printed": False,
            "raw_evidence_file_id_printed": False,
            "raw_api_response_stored": False,
        }


def build_journal_batch_input_from_evidence_bundle(
    *,
    evidence_bundle_json: Path,
    output_json: Path,
) -> JournalBatchInputBuildResult:
    evidence_bundle_json = require_ignored_untracked_data_path(
        evidence_bundle_json,
        purpose="journal candidate evidence bundle input",
    )
    output_json = require_ignored_untracked_data_path(output_json, purpose="journal batch input output")
    payload = json.loads(evidence_bundle_json.read_text(encoding="utf-8"))
    raw_items = _items_from_bundle(payload)
    output_items: list[dict[str, Any]] = []
    redacted_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        evidence = _evidence_with_defaults(_object(raw_item.get("evidence"), f"items[{index}].evidence"))
        candidate = _object(raw_item.get("candidate"), f"items[{index}].candidate")
        normalized_candidate = _candidate_with_source_type(candidate, raw_item)
        output_items.append({"candidate": normalized_candidate, "evidence": evidence})
        redacted_items.append(
            {
                "index": index,
                "candidate_hash": stable_hash(normalized_candidate),
                "file_name": evidence.get("file_name"),
                "file_id_hash": mask_identifier(str(evidence.get("google_drive_file_id") or "")),
                "file_hash": evidence.get("file_hash"),
                "local_file_path_present": bool(evidence.get("local_file_path")),
                "local_file_path_hash": stable_hash({"local_file_path": evidence.get("local_file_path")})
                if evidence.get("local_file_path")
                else "",
                "parent_folder_id_hash": mask_identifier(str(evidence.get("parent_folder_id") or "")),
                "parent_folder_name": evidence.get("parent_folder_name"),
                "source_type": normalized_candidate.get("source_type"),
                "evidence_type": raw_item.get("evidence_type"),
            }
        )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps({"items": output_items}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return JournalBatchInputBuildResult(
        status="ready" if output_items else "empty",
        input_count=len(output_items),
        output_json=output_json,
        items=tuple(redacted_items),
    )


def _items_from_bundle(payload: Any) -> list[dict[str, Any]]:
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("journal candidate evidence bundle requires a non-empty items array")
    return [_object(item, "journal candidate evidence item") for item in raw_items]


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a filled object")
    return dict(value)


def _candidate_with_source_type(candidate: dict[str, Any], raw_item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    source_type = str(normalized.get("source_type") or "").strip()
    if not source_type:
        suggested = str(raw_item.get("suggested_source_type") or "").strip()
        normalized["source_type"] = suggested or "receipt"
    return normalized


def _evidence_with_defaults(evidence: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(evidence)
    normalized.setdefault("exists", True)
    normalized.setdefault("attachable", True)
    return normalized
