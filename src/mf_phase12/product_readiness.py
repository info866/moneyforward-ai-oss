from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .classification_resolution_queue import build_classification_resolution_status
from .db import DEFAULT_DB_PATH, Phase12Database
from .drive_processed_readback import PROCESSED_READBACK_EVENT


REQUIRED_TABLES = (
    "sync_manifests",
    "processing_ledger",
    "write_intents",
    "audit_logs",
    "journal_patterns",
    "journal_pattern_occurrences",
    "rag_cards",
    "classification_evaluation_runs",
    "classification_evaluation_items",
    "classification_improvement_candidates",
    "classification_resolution_queue",
    "fixed_rule_candidates",
    "vendor_profiles",
    "post_registration_verifications",
    "voucher_attach_execution_records",
    "voucher_attach_readback_verifications",
    "drive_move_execution_records",
    "drive_move_readback_verifications",
    "drive_move_chain_items",
    "payable_folder_mappings",
    "financial_event_classifications",
    "financial_event_learning_events",
    "correction_diffs",
    "failure_examples",
)


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


def build_product_readiness_status(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    min_sample_required: int = 100,
    min_auto_post_eligible_required: int = 10,
    product_mode: str = "limited_auto",
) -> dict:
    if product_mode not in {"limited_auto", "strict_all_patterns", "workflow_ready", "managed_operations"}:
        raise ValueError(f"unsupported product_mode: {product_mode}")
    db = Phase12Database(db_path)
    path = db.path
    checks: list[ReadinessCheck] = []
    if not path.exists():
        checks.append(ReadinessCheck("db_exists", False, f"missing {path}"))
        return _status(path, checks, {}, latest_evaluation=None)

    checks.append(ReadinessCheck("db_exists", True, str(path)))
    with db.connect_readonly() as conn:
        conn.row_factory = sqlite3.Row
        tables = _table_names(conn)
        for table in REQUIRED_TABLES:
            checks.append(ReadinessCheck(f"table:{table}", table in tables, "present" if table in tables else "missing"))
        if not all(table in tables for table in REQUIRED_TABLES):
            return _status(path, checks, {}, latest_evaluation=None)

        counts = {
            "journal_patterns": _count(conn, "journal_patterns"),
            "journal_pattern_occurrences": _count(conn, "journal_pattern_occurrences"),
            "rag_cards": _count(conn, "rag_cards"),
            "classification_evaluation_runs": _count(conn, "classification_evaluation_runs"),
            "classification_improvement_candidates": _count(conn, "classification_improvement_candidates"),
            "fixed_rule_candidates": _count(conn, "fixed_rule_candidates"),
            "active_fixed_rule_candidates": _active_fixed_rule_count(conn),
            "low_confidence_active_rules": _low_confidence_active_rule_count(conn),
            "vendor_profiles": _count(conn, "vendor_profiles"),
            "low_information_patterns": _low_information_pattern_count(conn),
            "low_information_auto_post_enabled": _low_information_auto_post_enabled_count(conn),
            "limited_auto_post_patterns": _auto_post_eligibility_count(conn, "limited"),
            "enabled_auto_post_patterns": _auto_post_eligibility_count(conn, "enabled"),
            "shadow_auto_post_patterns": _auto_post_eligibility_count(conn, "shadow"),
            "processing_ledger_total": _count(conn, "processing_ledger"),
            "processing_ledger_open_items": _processing_ledger_open_item_count(conn),
            "journal_create_executed_count": _write_intent_count(conn, "journal_create", "executed"),
            "post_registration_passed_count": _post_registration_passed_count(conn),
            "journal_create_verified_joined_count": _journal_create_verified_joined_count(conn),
            "voucher_attach_verified_count": _voucher_attach_verified_count(conn),
            "drive_batch_audit_events": _audit_event_count(conn, "drive_folder_batch_audit"),
            "drive_move_candidate_count": _drive_move_candidate_count(conn),
            "drive_move_verified_count": _drive_move_verified_count(conn),
            "same_chain_journal_voucher_drive_verified_count": _same_chain_journal_voucher_drive_verified_count(conn),
            "payable_folder_mapped_count": _payable_folder_mapped_count(conn),
            "payable_folder_active_candidate_count": _payable_folder_active_candidate_count(conn),
            "financial_event_classification_count": _count(conn, "financial_event_classifications"),
            "financial_event_auto_candidate_count": _financial_event_auto_candidate_count(conn),
            "financial_event_learning_count": _count(conn, "financial_event_learning_events"),
            "correction_diff_count": _count(conn, "correction_diffs"),
            "failure_example_count": _count(conn, "failure_examples"),
        }
        latest_manifest = _latest_manifest(conn, "journal_patterns")
        latest_evaluation = _latest_required_evaluation(conn)
        failed_auto_post_items = _failed_auto_post_candidate_count(conn, latest_evaluation)
        raw_data_stored = _raw_data_stored_count(conn)
        managed_resolution = build_classification_resolution_status(conn)

    checks.extend(_common_checks(latest_manifest, counts, latest_evaluation, min_sample_required, raw_data_stored))
    if product_mode == "limited_auto":
        checks.extend(
            _limited_auto_checks(
                counts,
                latest_evaluation,
                failed_auto_post_items,
                min_auto_post_eligible_required,
            )
        )
    elif product_mode == "strict_all_patterns":
        checks.extend(
            [
                ReadinessCheck(
                    "low_information_auto_post_blocked",
                    counts["low_information_auto_post_enabled"] == 0,
                    str(counts["low_information_auto_post_enabled"]),
                ),
                ReadinessCheck(
                    "normal_threshold_promotion_allowed",
                    bool(latest_evaluation and latest_evaluation["promotion_allowed"] == 1),
                    str(latest_evaluation["promotion_allowed"]) if latest_evaluation else "missing",
                ),
                ReadinessCheck(
                    "normal_threshold_overall_100",
                    bool(latest_evaluation and latest_evaluation["overall_accuracy"] == 1.0),
                    str(latest_evaluation["overall_accuracy"]) if latest_evaluation else "missing",
                ),
                ReadinessCheck(
                    "normal_threshold_classifiable_100",
                    bool(latest_evaluation and latest_evaluation["classifiable_overall_accuracy"] == 1.0),
                    str(latest_evaluation["classifiable_overall_accuracy"]) if latest_evaluation else "missing",
                ),
                ReadinessCheck(
                    "normal_threshold_no_improvement_candidates",
                    bool(latest_evaluation and latest_evaluation["improvement_candidate_count"] == 0),
                    str(latest_evaluation["improvement_candidate_count"]) if latest_evaluation else "missing",
                ),
            ]
        )
    elif product_mode == "workflow_ready":
        checks.extend(_workflow_ready_checks(counts))
    else:
        checks.extend(
            _limited_auto_checks(
                counts,
                latest_evaluation,
                failed_auto_post_items,
                min_auto_post_eligible_required,
            )
        )
        checks.extend(_workflow_ready_checks(counts))
        checks.extend(
            [
                ReadinessCheck(
                    "resolution_queue_all_non_auto_tracked",
                    managed_resolution["missing_tracking_count"] == 0,
                    f"{managed_resolution['tracked_count']} / {managed_resolution['non_auto_count']}",
                ),
                ReadinessCheck(
                    "resolution_queue_no_stale_active_tasks",
                    managed_resolution["stale_active_count"] == 0,
                    str(managed_resolution["stale_active_count"]),
                ),
                ReadinessCheck(
                    "resolution_queue_metadata_complete",
                    managed_resolution["metadata_missing_count"] == 0,
                    str(managed_resolution["metadata_missing_count"]),
                ),
                ReadinessCheck(
                    "resolution_queue_one_task_per_pattern",
                    managed_resolution["duplicate_tracking_count"] == 0,
                    str(managed_resolution["duplicate_tracking_count"]),
                ),
                ReadinessCheck(
                    "resolution_queue_sla_current",
                    managed_resolution["overdue_count"] == 0,
                    str(managed_resolution["overdue_count"]),
                ),
            ]
        )
    status = _status(path, checks, counts, latest_evaluation=latest_evaluation)
    status["product_mode"] = product_mode
    status["classification_resolution"] = managed_resolution
    return status


def _limited_auto_checks(
    counts: dict,
    latest_evaluation: dict | None,
    failed_auto_post_items: int,
    min_auto_post_eligible_required: int,
) -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            "limited_auto_post_min_candidates",
            bool(latest_evaluation and latest_evaluation["auto_post_eligible_count"] >= min_auto_post_eligible_required),
            (
                f"{latest_evaluation['auto_post_eligible_count']} / {min_auto_post_eligible_required}"
                if latest_evaluation
                else "missing"
            ),
        ),
        ReadinessCheck(
            "limited_auto_post_accuracy_100",
            bool(latest_evaluation and latest_evaluation["auto_post_eligible_accuracy"] == 1.0),
            str(latest_evaluation["auto_post_eligible_accuracy"]) if latest_evaluation else "missing",
        ),
        ReadinessCheck(
            "limited_auto_failed_items_zero",
            failed_auto_post_items == 0,
            str(failed_auto_post_items),
        ),
        ReadinessCheck(
            "low_information_auto_post_blocked",
            counts["low_information_auto_post_enabled"] == 0,
            str(counts["low_information_auto_post_enabled"]),
        ),
    ]


def _common_checks(
    latest_manifest: sqlite3.Row | None,
    counts: dict,
    latest_evaluation: dict | None,
    min_sample_required: int,
    raw_data_stored: int,
) -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            "journal_sync_manifest_ok",
            bool(latest_manifest and latest_manifest["status"] == "ok" and latest_manifest["item_count"] > 0),
            json.dumps(dict(latest_manifest), ensure_ascii=False, sort_keys=True) if latest_manifest else "missing",
        ),
        ReadinessCheck(
            "journal_patterns_min_sample",
            counts["journal_patterns"] >= min_sample_required,
            f"{counts['journal_patterns']} / {min_sample_required}",
        ),
        ReadinessCheck(
            "normal_threshold_evaluation_exists",
            latest_evaluation is not None,
            json.dumps(latest_evaluation or {}, ensure_ascii=False, sort_keys=True),
        ),
        ReadinessCheck(
            "no_low_confidence_active_rules",
            counts["low_confidence_active_rules"] == 0,
            str(counts["low_confidence_active_rules"]),
        ),
        ReadinessCheck("raw_data_not_stored", raw_data_stored == 0, str(raw_data_stored)),
        ReadinessCheck(
            "payable_folder_mapping_ready",
            counts["payable_folder_mapped_count"] > 0 and counts["payable_folder_active_candidate_count"] == 0,
            (
                f"mapped={counts['payable_folder_mapped_count']};"
                f"candidate={counts['payable_folder_active_candidate_count']}"
            ),
        ),
        ReadinessCheck(
            "financial_event_classification_ready",
            counts["financial_event_classification_count"] > 0 and counts["financial_event_auto_candidate_count"] > 0,
            (
                f"classified={counts['financial_event_classification_count']};"
                f"auto={counts['financial_event_auto_candidate_count']}"
            ),
        ),
        ReadinessCheck(
            "financial_event_learning_loop_ready",
            counts["financial_event_learning_count"] > 0,
            str(counts["financial_event_learning_count"]),
        ),
        ReadinessCheck(
            "failure_learning_material_ready",
            counts["failure_example_count"] > 0 or counts["correction_diff_count"] > 0,
            f"failure_examples={counts['failure_example_count']};correction_diffs={counts['correction_diff_count']}",
        ),
    ]


def _workflow_ready_checks(counts: dict) -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            "journal_create_executed",
            counts["journal_create_executed_count"] > 0,
            str(counts["journal_create_executed_count"]),
        ),
        ReadinessCheck(
            "post_registration_verification_passed",
            counts["post_registration_passed_count"] > 0,
            str(counts["post_registration_passed_count"]),
        ),
        ReadinessCheck(
            "journal_create_verified_joined",
            counts["journal_create_verified_joined_count"] > 0,
            str(counts["journal_create_verified_joined_count"]),
        ),
        ReadinessCheck(
            "voucher_attach_verified",
            counts["voucher_attach_verified_count"] > 0,
            str(counts["voucher_attach_verified_count"]),
        ),
        ReadinessCheck(
            "drive_batch_audit_persisted",
            counts["drive_batch_audit_events"] > 0,
            str(counts["drive_batch_audit_events"]),
        ),
        ReadinessCheck(
            "drive_move_readback_verified_for_candidates",
            counts["drive_move_candidate_count"] == 0
            or counts["drive_move_verified_count"] >= counts["drive_move_candidate_count"],
            f"{counts['drive_move_verified_count']} / {counts['drive_move_candidate_count']}",
        ),
        ReadinessCheck(
            "same_chain_journal_voucher_drive_verified",
            counts["same_chain_journal_voucher_drive_verified_count"] > 0,
            str(counts["same_chain_journal_voucher_drive_verified_count"]),
        ),
        ReadinessCheck(
            "processing_ledger_no_open_items",
            counts["processing_ledger_open_items"] == 0,
            str(counts["processing_ledger_open_items"]),
        ),
    ]


def status_to_json(status: dict) -> str:
    return json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)


def _status(
    path: Path,
    checks: list[ReadinessCheck],
    counts: dict,
    *,
    latest_evaluation: dict | None,
) -> dict:
    failed = [check.name for check in checks if not check.passed]
    return {
        "status": "ready" if not failed else "not_ready",
        "phase12_product_ready": not failed,
        "db_path": str(path),
        "counts": counts,
        "latest_required_evaluation": latest_evaluation or {},
        "failed_checks": failed,
        "checks": [asdict(check) for check in checks],
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _low_information_pattern_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM journal_patterns
            WHERE description_key = 'no_description'
               OR amount_bucket = 'unknown'
               OR debit_account = 'unknown_debit_account'
               OR credit_account = 'unknown_credit_account'
               OR tax_category = 'unknown_tax'
            """
        ).fetchone()[0]
    )


def _low_information_auto_post_enabled_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM journal_patterns
            WHERE auto_post_eligibility IN ('limited', 'enabled')
              AND (
                   description_key = 'no_description'
                OR amount_bucket = 'unknown'
                OR debit_account = 'unknown_debit_account'
                OR credit_account = 'unknown_credit_account'
                OR tax_category = 'unknown_tax'
              )
            """
        ).fetchone()[0]
    )


def _auto_post_eligibility_count(conn: sqlite3.Connection, eligibility: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM journal_patterns WHERE auto_post_eligibility = ?",
            (eligibility,),
        ).fetchone()[0]
    )


def _payable_folder_mapped_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM payable_folder_mappings
            WHERE status = 'mapped'
              AND google_drive_folder_id_hash <> ''
            """
        ).fetchone()[0]
    )


def _payable_folder_active_candidate_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM payable_folder_mappings WHERE status = 'candidate'"
        ).fetchone()[0]
    )


def _financial_event_auto_candidate_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM financial_event_classifications
            WHERE recommended_action = 'auto_post_candidate'
              AND registration_allowed = 1
            """
        ).fetchone()[0]
    )


def _processing_ledger_open_item_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM processing_ledger
            WHERE processing_status IN (
                'new', 'planned', 'blocked', 'journal_created',
                'voucher_attached', 'failed'
            )
            """
        ).fetchone()[0]
    )


def _write_intent_count(conn: sqlite3.Connection, operation_type: str, status: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM write_intents
            WHERE operation_type = ? AND status = ?
            """,
            (operation_type, status),
        ).fetchone()[0]
    )


def _post_registration_passed_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM post_registration_verifications
            WHERE passed = 1
            """
        ).fetchone()[0]
    )


def _journal_create_verified_joined_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT intent.idempotency_key)
            FROM write_intents AS intent
            JOIN post_registration_verifications AS verification
              ON verification.idempotency_key = intent.idempotency_key
             AND verification.moneyforward_journal_id_hash = intent.moneyforward_journal_id_hash
             AND verification.passed = 1
            WHERE intent.operation_type = 'journal_create'
              AND intent.status IN ('executed', 'verified')
              AND intent.moneyforward_journal_id_hash IS NOT NULL
            """
        ).fetchone()[0]
    )


def _voucher_attach_verified_count(conn: sqlite3.Connection) -> int:
    api_post_verified = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT exec.moneyforward_journal_id_hash)
            FROM voucher_attach_execution_records AS exec
            JOIN voucher_attach_readback_verifications AS verify
              ON verify.approval_hash = exec.approval_hash
             AND verify.passed = 1
             AND verify.raw_secret_stored = 0
             AND verify.raw_journal_id_stored = 0
             AND verify.raw_voucher_file_ids_stored = 0
             AND verify.raw_api_response_stored = 0
            WHERE exec.status = 'verified'
              AND exec.raw_secret_stored = 0
              AND exec.raw_journal_id_stored = 0
              AND exec.raw_voucher_file_ids_stored = 0
              AND exec.raw_api_response_stored = 0
            """
        ).fetchone()[0]
    )
    readback_verified = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM processing_ledger AS ledger
            JOIN post_registration_verifications AS verify
              ON verify.idempotency_key = ledger.idempotency_key
             AND verify.moneyforward_journal_id_hash = ledger.moneyforward_journal_id_hash
             AND verify.passed = 1
             AND verify.voucher_file_ids_hash IS NOT NULL
             AND verify.voucher_file_ids_hash != ''
            WHERE ledger.processing_status = 'verified'
              AND ledger.moneyforward_journal_id_hash IS NOT NULL
              AND ledger.moneyforward_voucher_id_hash IS NOT NULL
              AND ledger.moneyforward_voucher_id_hash != ''
            """
        ).fetchone()[0]
    )
    return max(api_post_verified, readback_verified)


def _audit_event_count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_logs
            WHERE event_type = ?
            """,
            (event_type,),
        ).fetchone()[0]
    )


def _drive_move_verified_count(conn: sqlite3.Connection) -> int:
    executed = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM drive_move_execution_records
            WHERE status = 'verified'
              AND raw_secret_stored = 0
              AND raw_drive_file_ids_stored = 0
              AND raw_api_response_stored = 0
            """
        ).fetchone()[0]
    )
    read_back = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM drive_move_readback_verifications
            WHERE passed = 1
              AND raw_secret_stored = 0
              AND raw_drive_file_ids_stored = 0
              AND raw_api_response_stored = 0
            """
        ).fetchone()[0]
    )
    processed_readback = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT idempotency_key)
            FROM audit_logs
            WHERE event_type = ?
              AND idempotency_key IS NOT NULL
            """,
            (PROCESSED_READBACK_EVENT,),
        ).fetchone()[0]
    )
    return max(min(executed, read_back), processed_readback)


def _drive_move_candidate_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT ledger.idempotency_key)
            FROM processing_ledger AS ledger
            JOIN audit_logs AS audit
              ON audit.idempotency_key = ledger.idempotency_key
             AND audit.event_type = 'drive_folder_batch_audit'
            WHERE ledger.processing_status = 'verified'
            """
        ).fetchone()[0]
    )


def _same_chain_journal_voucher_drive_verified_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT ledger.idempotency_key)
            FROM processing_ledger AS ledger
            JOIN write_intents AS intent
              ON intent.idempotency_key = ledger.idempotency_key
             AND intent.operation_type = 'journal_create'
             AND intent.status IN ('executed', 'verified')
             AND intent.moneyforward_journal_id_hash = ledger.moneyforward_journal_id_hash
            JOIN post_registration_verifications AS post_verify
             ON post_verify.idempotency_key = ledger.idempotency_key
             AND post_verify.moneyforward_journal_id_hash = intent.moneyforward_journal_id_hash
             AND post_verify.passed = 1
            LEFT JOIN audit_logs AS audit
              ON audit.idempotency_key = ledger.idempotency_key
             AND audit.event_type = 'drive_folder_batch_audit'
            WHERE ledger.processing_status = 'verified'
              AND ledger.moneyforward_journal_id_hash IS NOT NULL
              AND ledger.moneyforward_voucher_id_hash IS NOT NULL
              AND ledger.moneyforward_voucher_id_hash != ''
              AND post_verify.voucher_file_ids_hash IS NOT NULL
              AND post_verify.voucher_file_ids_hash != ''
              AND (
                ledger.evidence_source_type IN ('invoice_api', 'local_file')
                OR (
                  audit.id IS NOT NULL
                  AND (
                    EXISTS (
                  SELECT 1
                  FROM drive_move_chain_items AS chain
                  JOIN drive_move_execution_records AS drive_exec
                    ON drive_exec.approval_hash = chain.approval_hash
                   AND drive_exec.request_hash = chain.request_hash
                   AND drive_exec.target_hash = chain.target_hash
                   AND drive_exec.status = 'verified'
                   AND drive_exec.raw_secret_stored = 0
                   AND drive_exec.raw_drive_file_ids_stored = 0
                   AND drive_exec.raw_api_response_stored = 0
                  JOIN drive_move_readback_verifications AS drive_verify
                    ON drive_verify.approval_hash = drive_exec.approval_hash
                   AND drive_verify.request_hash = drive_exec.request_hash
                   AND drive_verify.target_hash = drive_exec.target_hash
                   AND drive_verify.passed = 1
                   AND drive_verify.raw_secret_stored = 0
                   AND drive_verify.raw_drive_file_ids_stored = 0
                   AND drive_verify.raw_api_response_stored = 0
                  WHERE chain.idempotency_key = ledger.idempotency_key
                    AND chain.raw_secret_stored = 0
                    AND chain.raw_drive_file_ids_stored = 0
                    )
                    OR EXISTS (
                  SELECT 1
                  FROM audit_logs AS processed
                  WHERE processed.idempotency_key = ledger.idempotency_key
                    AND processed.event_type = ?
                    )
                  )
                )
              )
            """
            ,
            (PROCESSED_READBACK_EVENT,),
        ).fetchone()[0]
    )


def _active_fixed_rule_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM fixed_rule_candidates WHERE status IN ('shadow', 'approved')"
        ).fetchone()[0]
    )


def _low_confidence_active_rule_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fixed_rule_candidates
            WHERE status IN ('shadow', 'approved')
              AND confidence_score < 0.8
            """
        ).fetchone()[0]
    )


def _latest_manifest(conn: sqlite3.Connection, source_kind: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT source_kind, item_count, status, synced_from, synced_to, synced_at
        FROM sync_manifests
        WHERE source_name = 'moneyforward' AND source_kind = ?
        ORDER BY synced_at DESC, id DESC
        LIMIT 1
        """,
        (source_kind,),
    ).fetchone()


def _latest_required_evaluation(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT run_hash, prediction_strategy, learned_rule_confidence_threshold, sample_count,
               classifiable_sample_count, overall_accuracy, classifiable_overall_accuracy,
               auto_post_eligible_count, auto_post_eligible_accuracy,
               promotion_allowed, block_reason_code, unknown_pattern_count,
               improvement_candidate_count, raw_data_stored, created_at
        FROM classification_evaluation_runs
        WHERE prediction_strategy = 'learned_rules_first'
          AND learned_rule_confidence_threshold >= 0.8
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def _raw_data_stored_count(conn: sqlite3.Connection) -> int:
    total = 0
    raw_checks = {
        "classification_evaluation_runs": ("raw_data_stored",),
        "classification_evaluation_items": ("raw_data_stored",),
        "classification_improvement_candidates": ("raw_data_stored",),
        "fixed_rule_candidates": ("raw_data_stored",),
        "journal_pattern_occurrences": ("raw_data_stored",),
        "oauth_scope_checks": ("raw_secret_stored",),
        "voucher_attach_execution_records": (
            "raw_secret_stored",
            "raw_journal_id_stored",
            "raw_voucher_file_ids_stored",
            "raw_api_response_stored",
        ),
        "voucher_attach_readback_verifications": (
            "raw_secret_stored",
            "raw_journal_id_stored",
            "raw_voucher_file_ids_stored",
            "raw_api_response_stored",
        ),
        "drive_move_execution_records": (
            "raw_secret_stored",
            "raw_drive_file_ids_stored",
            "raw_api_response_stored",
        ),
        "drive_move_readback_verifications": (
            "raw_secret_stored",
            "raw_drive_file_ids_stored",
            "raw_api_response_stored",
        ),
        "drive_move_chain_items": (
            "raw_secret_stored",
            "raw_drive_file_ids_stored",
        ),
    }
    tables = _table_names(conn)
    for table, columns in raw_checks.items():
        if table not in tables:
            continue
        table_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column in columns:
            if column in table_columns:
                total += int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} != 0").fetchone()[0])
    return total


def _failed_auto_post_candidate_count(conn: sqlite3.Connection, latest_evaluation: dict | None) -> int:
    if not latest_evaluation:
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM classification_evaluation_items
            WHERE run_hash = ?
              AND auto_post_candidate = 1
              AND overall_passed = 0
            """,
            (latest_evaluation["run_hash"],),
        ).fetchone()[0]
    )
