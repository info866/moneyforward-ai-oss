from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import stable_hash
from .local_artifacts import load_journal_id, require_ignored_untracked_data_path
from .voucher_prepare import (
    VoucherAttachPrepareBundle,
    approval_record_hash_from_payload,
    build_voucher_attach_prepare_bundle,
    dry_run_hash_from_payload,
)


@dataclass(frozen=True)
class JournalVoucherPrepareSummary:
    status: str
    queue_json: Path
    journal_id_json: Path
    output_dir: Path
    idempotency_key: str
    item_index: int
    file_name: str
    file_hash: str
    local_file_path_hash: str
    bundle: VoucherAttachPrepareBundle
    paths: dict[str, Path]

    def to_redacted_dict(self) -> dict[str, Any]:
        summary = self.bundle.to_redacted_summary()
        summary.update(
            {
                "status": self.status,
                "queue_json": str(self.queue_json),
                "journal_id_json": str(self.journal_id_json),
                "output_dir": str(self.output_dir),
                "idempotency_key": self.idempotency_key,
                "item_index": self.item_index,
                "file_name": self.file_name,
                "file_hash": self.file_hash,
                "local_file_path_hash": self.local_file_path_hash,
                "preflight_path": str(self.paths["preflight"]),
                "approval_record_path": str(self.paths["approval_record"]),
                "grant_path": str(self.paths["grant"]),
                "dry_run_path": str(self.paths["dry_run"]),
                "execution_approval_path": str(self.paths["execution_approval"]),
                "raw_local_file_path_printed": False,
                "raw_drive_file_id_printed": False,
                "raw_journal_id_printed": False,
            }
        )
        return summary


def build_voucher_prepare_from_journal_queue(
    *,
    queue_json: Path,
    journal_id_json: Path,
    scope_hash: str,
    approval_record_user_approval_json: Path,
    execution_user_approval_json: Path,
    output_dir: Path,
    idempotency_key: str | None = None,
    item_index: int | None = None,
    office_id: str = "",
    approved_at: datetime | None = None,
    ttl_minutes: int = 30,
) -> JournalVoucherPrepareSummary:
    queue_json = require_ignored_untracked_data_path(queue_json, purpose="journal queue voucher handoff input")
    journal_id_json = require_ignored_untracked_data_path(journal_id_json, purpose="journal_id input")
    approval_record_user_approval_json = require_ignored_untracked_data_path(
        approval_record_user_approval_json,
        purpose="voucher approval record user approval input",
    )
    execution_user_approval_json = require_ignored_untracked_data_path(
        execution_user_approval_json,
        purpose="voucher execution user approval input",
    )
    output_dir = require_ignored_untracked_data_path(output_dir, purpose="journal queue voucher handoff output")
    queue_payload = _load_object(queue_json, "journal write queue")
    item = _select_queue_item(queue_payload, idempotency_key=idempotency_key, item_index=item_index)
    handoff = _handoff_from_item(item)
    file_path = Path(str(handoff["local_file_path"]))
    if not file_path.is_file():
        raise FileNotFoundError(f"voucher handoff local file not found: {file_path}")
    journal_id = load_journal_id(None, journal_id_json)
    bundle = build_voucher_attach_prepare_bundle(
        journal_id=journal_id,
        file_path=file_path,
        scope_hash=scope_hash,
        approval_record_user_approval_payload=_load_json(approval_record_user_approval_json),
        execution_user_approval_payload=_load_json(execution_user_approval_json),
        office_id=office_id,
        approved_at=approved_at,
        ttl_minutes=ttl_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = _write_bundle(output_dir, bundle)
    return JournalVoucherPrepareSummary(
        status="ok",
        queue_json=queue_json,
        journal_id_json=journal_id_json,
        output_dir=output_dir,
        idempotency_key=str(item["idempotency_key"]),
        item_index=int(item.get("index") or 0),
        file_name=str(handoff.get("file_name") or file_path.name),
        file_hash=str(handoff.get("file_hash") or ""),
        local_file_path_hash=stable_hash({"local_file_path": str(file_path)}),
        bundle=bundle,
        paths=paths,
    )


def _select_queue_item(payload: dict[str, Any], *, idempotency_key: str | None, item_index: int | None) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("journal write queue must contain items")
    if idempotency_key:
        matches = [item for item in items if isinstance(item, dict) and item.get("idempotency_key") == idempotency_key]
    elif item_index is not None:
        matches = [item for item in items if isinstance(item, dict) and _item_index(item) == item_index]
    else:
        matches = [item for item in items if isinstance(item, dict)]
    if len(matches) != 1:
        raise ValueError("journal write queue handoff requires exactly one selected item")
    return matches[0]


def _item_index(item: dict[str, Any]) -> int:
    value = item.get("index")
    if value in (None, ""):
        return -1
    return int(value)


def _handoff_from_item(item: dict[str, Any]) -> dict[str, Any]:
    handoff = item.get("attachment_handoff")
    if not isinstance(handoff, dict):
        raise ValueError("queue item is missing attachment_handoff")
    if handoff.get("voucher_prepare_ready") is not True:
        raise ValueError("queue item attachment_handoff is not voucher_prepare_ready")
    local_file_path = str(handoff.get("local_file_path") or "")
    if not local_file_path:
        raise ValueError("queue item attachment_handoff is missing local_file_path")
    return handoff


def _write_bundle(output_dir: Path, bundle: VoucherAttachPrepareBundle) -> dict[str, Path]:
    paths = {
        "preflight": output_dir / "preflight" / f"{bundle.preflight['request_hash']}.json",
        "approval_record": output_dir / "approval_records" / f"{approval_record_hash_from_payload(bundle.approval_record)}.json",
        "grant": output_dir / "grants" / f"{bundle.grant['approval_hash']}.json",
        "dry_run": output_dir / "dry_runs" / f"{dry_run_hash_from_payload(bundle.dry_run)}.json",
        "execution_approval": output_dir / "execution_approvals" / f"{bundle.execution_approval['dry_run_hash']}.json",
    }
    payloads = {
        "preflight": bundle.preflight,
        "approval_record": bundle.approval_record,
        "grant": bundle.grant,
        "dry_run": bundle.dry_run,
        "execution_approval": bundle.execution_approval,
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payloads[key], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths


def _load_object(path: Path, label: str) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
