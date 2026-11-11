from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .classification_scope import build_classification_scope_status
from .db import DEFAULT_DB_PATH, Phase12Database
from .evidence_status import build_phase12_evidence_status
from .product_readiness import build_product_readiness_status


ACTIVE_SCOPE = (
    "moneyforward_accounting_read_sync",
    "office_specific_db_rag",
    "classification_replay_gate",
    "classification_scope_gate",
    "drive_manifest_and_batch_audit",
    "journal_create_write_gate",
    "voucher_attach_api_gate",
    "post_registration_readback",
    "processing_ledger_idempotency",
    "managed_classification_resolution_queue",
)

DEFERRED_SCOPES = (
    {
        "scope": "moneyforward_standard_ai_ocr_direct_api",
        "reason": "official API/MCP direct upload, OCR-result, and candidate endpoints are not currently available",
        "blocks_active_release": False,
    },
    {
        "scope": "live_payroll_api",
        "reason": "Money Forward-issued payroll identifier is not yet granted; local masked fallback is separate",
        "blocks_active_release": False,
    },
    {
        "scope": "n8n_workflow_layer",
        "reason": "user set n8n as last-stage validation and existing SNS n8n environment must stay isolated",
        "blocks_active_release": False,
    },
    {
        "scope": "moneyforward_plus_and_depreciation",
        "reason": "Money Forward Plus-family services and depreciation/fixed-assets are explicitly out of current scope",
        "blocks_active_release": False,
    },
)


@dataclass(frozen=True)
class ReleaseCheck:
    name: str
    passed: bool
    detail: str


def build_active_scope_release_status(
    root: Path,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    root = root.resolve()
    db_path = Phase12Database(db_path).path
    checks: list[ReleaseCheck] = []
    evidence = build_phase12_evidence_status(root, db_path=db_path)
    checks.append(
        ReleaseCheck(
            "phase12_evidence_status_ok",
            evidence["status"] == "ok" and evidence["phase12_completion_allowed"] is True,
            f"{evidence['status']} / completion_allowed={evidence['phase12_completion_allowed']}",
        )
    )

    mode_statuses = {
        mode: build_product_readiness_status(db_path, product_mode=mode)
        for mode in ("limited_auto", "workflow_ready", "managed_operations", "strict_all_patterns")
    }
    for mode in ("limited_auto", "workflow_ready", "managed_operations"):
        status = mode_statuses[mode]
        checks.append(
            ReleaseCheck(
                f"{mode}_ready",
                status["phase12_product_ready"] is True,
                ",".join(status["failed_checks"]) if status["failed_checks"] else "ready",
            )
        )
    strict_quality = mode_statuses["strict_all_patterns"]
    checks.append(
        ReleaseCheck(
            "strict_all_patterns_reported_as_advisory",
            True,
            strict_quality["status"],
        )
    )
    classification_scope = build_classification_scope_status(db_path)
    managed_resolution_ready = mode_statuses["managed_operations"].get("classification_resolution", {}).get(
        "managed_queue_ready"
    ) is True
    safe_or_managed = bool(
        classification_scope.auto_post_scope_ready
        or (
            classification_scope.auto_post_eligible_count > 0
            and classification_scope.auto_post_eligible_accuracy == 1.0
            and managed_resolution_ready
        )
    )
    checks.append(
        ReleaseCheck(
            "classification_scope_safe_or_managed",
            safe_or_managed,
            (
                f"auto={classification_scope.auto_post_eligible_count};"
                f"blocked={classification_scope.blocked_count};"
                f"accuracy={classification_scope.auto_post_eligible_accuracy};"
                f"managed={managed_resolution_ready}"
            ),
        )
    )

    checks.append(
        ReleaseCheck(
            "deferred_scopes_do_not_block_active_release",
            all(item["blocks_active_release"] is False for item in DEFERRED_SCOPES),
            ",".join(item["scope"] for item in DEFERRED_SCOPES),
        )
    )
    checks.append(
        ReleaseCheck(
            "active_scope_defined",
            len(ACTIVE_SCOPE) >= 5,
            ",".join(ACTIVE_SCOPE),
        )
    )

    failed = [check.name for check in checks if not check.passed]
    return {
        "status": "ready" if not failed else "not_ready",
        "active_scope_release_ready": not failed,
        "active_scope": list(ACTIVE_SCOPE),
        "deferred_scopes": list(DEFERRED_SCOPES),
        "failed_checks": failed,
        "checks": [asdict(check) for check in checks],
        "mode_statuses": {
            mode: {
                "status": status["status"],
                "phase12_product_ready": status["phase12_product_ready"],
                "failed_checks": status["failed_checks"],
                "counts": status.get("counts", {}),
            }
            for mode, status in mode_statuses.items()
        },
        "advisory_quality_gates": {
            "strict_all_patterns": {
                "status": strict_quality["status"],
                "phase12_product_ready": strict_quality["phase12_product_ready"],
                "failed_checks": strict_quality["failed_checks"],
                "blocks_active_release": False,
            }
        },
        "classification_scope": classification_scope.to_dict(),
        "evidence_status": {
            "status": evidence["status"],
            "phase12_completion_allowed": evidence["phase12_completion_allowed"],
            "verified_count": evidence["verified_count"],
            "partial_count": evidence["partial_count"],
            "incomplete_count": evidence["incomplete_count"],
            "unknown_count": evidence["unknown_count"],
        },
        "verified_evidence_ids": evidence["verified_evidence_ids"],
        "partial_evidence_ids": evidence["partial_evidence_ids"],
        "incomplete_evidence_ids": evidence["incomplete_evidence_ids"],
        "unknown_evidence_ids": evidence["unknown_evidence_ids"],
        "expected_incomplete_ids": evidence["expected_incomplete_ids"],
        "command_surface_checks": evidence["command_surface_checks"],
        "schema_surface_checks": evidence["schema_surface_checks"],
        "raw_data_stored": False,
    }


def status_to_json(status: dict) -> str:
    return json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)
