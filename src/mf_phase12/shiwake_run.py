from __future__ import annotations
from .office_profile import get_current_profile


def _setting(path, default=None):
    from .office_profile import resolve_office_profile

    value = resolve_office_profile(required=True).get_setting(path, default)
    if value is None or value == "":
        raise ValueError("office setting is not configured: " + path)
    return value


def _office_name():
    from .office_profile import resolve_office_profile

    return resolve_office_profile(required=True).legal_name


def _mf_principal():
    from .office_profile import resolve_office_profile

    profile = resolve_office_profile(required=True)
    value = str(profile.google_account).strip().casefold()
    configured = profile.get_setting("identity.moneyforward_principal")
    if configured is not None and str(configured).strip().casefold() != value:
        raise ValueError("Money Forward principal conflicts with the profile account")
    if not value:
        raise ValueError("Money Forward principal is not configured")
    return value


def _drive_principal():
    from .office_profile import resolve_office_profile

    value = (
        str(resolve_office_profile(required=True).google_drive_account)
        .strip()
        .casefold()
    )
    if not value:
        raise ValueError("Drive principal is not configured")
    return value


def _artifact_root():
    from .office_profile import resolve_office_profile

    profile = resolve_office_profile(required=True)
    expected = profile.moneyforward_db_path.parent.resolve()
    configured = profile.get_setting("runtime.artifact_root")
    if configured is not None and Path(configured).expanduser().resolve() != expected:
        raise ValueError("artifact root must match the selected company DB directory")
    return expected


def _source_stage_principals():
    return {
        stage: _drive_principal()
        if stage in {"payable_invoice", "all_receipt_folders"}
        else _mf_principal()
        for stage in _SOURCE_STAGE_MAP
    }


import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from .hash_utils import accounting_run_hash, stable_hash

RUN_STAGES = (
    "sales_invoice",
    "payable_invoice",
    "all_receipt_folders",
    "payroll_bonus",
    "bank_transfer",
    "card",
    "tax_insurance",
    "cross_audit",
    "google_chat_durable_sent",
    "rag_learning",
    "issue_update",
)
ACTIVE_ACCOUNTING_WORKFLOWS = RUN_STAGES[:6]
PARALLEL_WORKFLOWS = ACTIVE_ACCOUNTING_WORKFLOWS
VETERAN_TARGET_SECONDS = 15 * 60
STAFF_LIMIT_SECONDS = 30 * 60
STAGE_TARGET_SECONDS = {
    "sales_invoice": 60,
    "payable_invoice": 90,
    "all_receipt_folders": 180,
    "payroll_bonus": 90,
    "bank_transfer": 90,
    "card": 90,
    "tax_insurance": 60,
    "cross_audit": 120,
    "google_chat_durable_sent": 30,
    "rag_learning": 30,
    "issue_update": 60,
}
_SHA256_RE = re.compile("^(?:sha256:)?([0-9a-f]{64})$", re.IGNORECASE)
_FAILURE_STATUS_MAP = {
    "retryable_failure": "failed",
    "blocked": "failed",
    "terminal_failure": "failed",
}
_SOURCE_STAGE_MAP = {
    "sales_invoice": "sales",
    "payable_invoice": "received_evidence",
    "all_receipt_folders": "received_evidence",
    "payroll_bonus": "payroll",
    "bank_transfer": "bank",
    "card": "card",
    "tax_insurance": "tax_and_insurance",
}
_REPO_ROOT = Path(__file__).resolve().parents[2]


def build_stage_contract() -> dict[str, Any]:
    return {
        "status": "ready",
        "office": _office_name(),
        "execution_principal": _mf_principal(),
        "source_principal_map": dict(_source_stage_principals()),
        "stage_order": list(RUN_STAGES),
        "parallel_workflows": list(PARALLEL_WORKFLOWS),
        "execution_order_authoritative": False,
        "completion_requires": [
            "each requested workflow completes its own verified ready items",
            "cross-source audit remains read-only and item-scoped",
            "workflow-specific verified learning follows that workflow read-back",
            "production stages use authoritative artifacts or durable ledgers",
        ],
        "resume_policy": "retry_failed_stage_only",
        "uncertain_external_write_policy": "read_back_before_retry",
        "performance_target": {
            "veteran_target_seconds": VETERAN_TARGET_SECONDS,
            "staff_limit_seconds": STAFF_LIMIT_SECONDS,
            "stage_target_seconds": dict(STAGE_TARGET_SECONDS),
            "ordinary_batch_human_confirmation_count": 1,
            "target_retry_count": 0,
            "overrun_policy": "continue_to_completion_and_report_bottleneck",
            "quality_gates_unchanged": True,
        },
        "small_batch_fast_path": {
            "one_complete_manifest": True,
            "one_prepare_and_reconciliation_pass": True,
            "one_execution_pass": True,
            "one_readback_pass": True,
            "one_final_chat_and_issue_boundary": True,
            "per_item_confirmation_prohibited_for_ordinary_journals": True,
            "successful_stage_reexecution_prohibited": True,
            "full_rag_rebuild_each_run": False,
            "repository_full_test_during_accounting_run": False,
        },
        "production_receipt_policy": {
            "source_and_cross_audit": "dedicated authoritative adapter receipt bound to the same run and accounting date",
            "google_chat": "same-run sent ledger or dedup read-back receipt",
            "rag_learning": "verified_journal_learning run receipt",
            "issue_update": "GitHub API read-back run receipt",
            "payroll_extra": [
                "wage_ledger_readback",
                "employee_specific_liability_allocation",
                "accrual_readback",
                "settlement_readback",
            ],
        },
    }


def start_run(
    conn: sqlite3.Connection,
    run_id: str,
    principal: str = None,
    office: str = None,
    accounting_date: str | None = None,
    instruction: str = "仕分けを行ってください",
    actor: str = "codex",
    run_mode: str = "production",
) -> dict[str, Any]:
    office = office if office is not None else _office_name()
    principal = principal if principal is not None else _mf_principal()
    run_id = _require_text(run_id, "run_id")
    principal = _validate_principal(principal)
    if office != _office_name():
        raise ValueError("office does not match the selected company")
    instruction = _require_text(instruction, "instruction")
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    if run_mode not in {"production", "synthetic"}:
        raise ValueError("run_mode must be production or synthetic")
    if run_mode == "synthetic":
        _require_non_operational_synthetic_database(conn)
    run_hash = accounting_run_hash(run_id)
    existing = conn.execute(
        "SELECT * FROM accounting_runs WHERE run_id_hash = ?", (run_hash,)
    ).fetchone()
    if existing is not None:
        expected = (
            stable_hash({"office": office}),
            stable_hash({"principal": principal.casefold()}),
            stable_hash({"instruction": instruction}),
        )
        actual = (
            existing["office_hash"],
            existing["execution_principal_hash"],
            existing["instruction_hash"],
        )
        if (
            actual != expected
            or existing["run_mode"] != run_mode
            or (
                accounting_date is not None
                and existing["accounting_date"] != accounting_date
            )
        ):
            raise ValueError(
                "run_id is already bound to different immutable run inputs"
            )
        return build_run_status(conn, run_id)
    accounting_date = accounting_date or date.today().isoformat()
    try:
        date.fromisoformat(accounting_date)
    except ValueError as exc:
        raise ValueError("accounting_date must be ISO YYYY-MM-DD") from exc
    first_stage_started_at = _utc_now_iso()
    with _atomic(conn):
        inserted = conn.execute(
            "\n            INSERT OR IGNORE INTO accounting_runs (\n                run_id_hash,\n                office_hash,\n                execution_principal_hash,\n                accounting_date,\n                instruction_hash,\n                run_mode,\n                creator_actor_hash,\n                status,\n                current_stage\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)\n            ",
            (
                run_hash,
                stable_hash({"office": office}),
                stable_hash({"principal": principal}),
                accounting_date,
                stable_hash({"instruction": instruction}),
                run_mode,
                actor_hash,
                RUN_STAGES[0],
            ),
        )
        if inserted.rowcount == 1:
            conn.executemany(
                "\n                INSERT INTO accounting_run_stages (\n                    run_id_hash, stage_key, stage_order, status, started_at\n                ) VALUES (?, ?, ?, 'pending', ?)\n                ",
                [
                    (
                        run_hash,
                        stage,
                        index,
                        first_stage_started_at if index == 0 else None,
                    )
                    for index, stage in enumerate(RUN_STAGES)
                ],
            )
            _record_transition(
                conn,
                run_hash=run_hash,
                stage=RUN_STAGES[0],
                from_status="not_started",
                to_status="running",
                generation=0,
                actor_hash=actor_hash,
                evidence_hash=None,
            )
        else:
            concurrent = conn.execute(
                "SELECT * FROM accounting_runs WHERE run_id_hash = ?", (run_hash,)
            ).fetchone()
            if concurrent is None:
                raise RuntimeError("concurrent run start could not be read back")
            expected = (
                stable_hash({"office": office}),
                stable_hash({"principal": principal}),
                stable_hash({"instruction": instruction}),
                accounting_date,
                run_mode,
            )
            actual = (
                concurrent["office_hash"],
                concurrent["execution_principal_hash"],
                concurrent["instruction_hash"],
                concurrent["accounting_date"],
                concurrent["run_mode"],
            )
            if actual != expected:
                raise ValueError(
                    "run_id is already bound to different immutable run inputs"
                )
    return build_run_status(conn, run_id)


def build_run_status(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run_hash = accounting_run_hash(_require_text(run_id, "run_id"))
    run = conn.execute(
        "SELECT * FROM accounting_runs WHERE run_id_hash = ?", (run_hash,)
    ).fetchone()
    if run is None:
        return {
            "status": "not_found",
            "run_id_hash": f"sha256:{run_hash}",
            "current_stage": None,
            "failed_stage": None,
            "stages": [],
        }
    _validate_run_identity(run)
    rows = conn.execute(
        "\n        SELECT stage_key, stage_order, status, attempt_count, output_hash,\n               binding_hash, failure_code, failure_hash, started_at, completed_at\n        FROM accounting_run_stages\n        WHERE run_id_hash = ?\n        ORDER BY stage_order\n        ",
        (run_hash,),
    ).fetchall()
    remaining_rows = conn.execute(
        "\n        SELECT source_stage, COUNT(*) AS remaining_count\n        FROM accounting_run_items\n        WHERE run_id_hash = ? AND status NOT IN ('verified', 'not_applicable')\n        GROUP BY source_stage\n        ",
        (run_hash,),
    ).fetchall()
    remaining_by_stage = {
        str(row["source_stage"]): int(row["remaining_count"]) for row in remaining_rows
    }
    item_status_rows = conn.execute(
        "\n        SELECT status, COUNT(*) AS item_count\n        FROM accounting_run_items\n        WHERE run_id_hash = ?\n        GROUP BY status\n        ",
        (run_hash,),
    ).fetchall()
    item_status_counts = {
        str(row["status"]): int(row["item_count"]) for row in item_status_rows
    }
    now = datetime.now(timezone.utc)
    stages = []
    for row in rows:
        stored_status = str(row["status"])
        stage_remaining = remaining_by_stage.get(str(row["stage_key"]), 0)
        visible_status = _FAILURE_STATUS_MAP.get(stored_status, stored_status)
        if stored_status == "succeeded" and stage_remaining > 0:
            visible_status = "captured_pending_registration"
        stage_elapsed_seconds = _elapsed_seconds(
            row["started_at"], row["completed_at"], now=now
        )
        stage_target_seconds = STAGE_TARGET_SECONDS[str(row["stage_key"])]
        stages.append(
            {
                "stage": row["stage_key"],
                "order": row["stage_order"],
                "status": visible_status,
                "attempt_count": row["attempt_count"],
                "evidence_hash": _marked_hash(row["output_hash"]),
                "binding_hash": _marked_hash(row["binding_hash"]),
                "failure_code": row["failure_code"],
                "failure_hash": _marked_hash(row["failure_hash"]),
                "safe_to_retry": stored_status == "retryable_failure",
                "requires_readback": stored_status == "uncertain_external_write",
                "elapsed_seconds": stage_elapsed_seconds,
                "target_seconds": stage_target_seconds,
                "time_status": _time_status(
                    stage_elapsed_seconds, stage_target_seconds
                ),
                "remaining_count": stage_remaining,
            }
        )
    status = str(run["status"])
    current_stage = run["current_stage"]
    failed_row = next(
        (row for row in stages if row["stage"] == run["last_failed_stage"]), None
    )
    elapsed_seconds = (
        _elapsed_seconds(run["created_at"], run["completed_at"], now=now) or 0
    )
    retry_count = sum((max(int(row["attempt_count"]) - 1, 0) for row in rows))
    performance_status = _time_status(elapsed_seconds, VETERAN_TARGET_SECONDS)
    if elapsed_seconds > STAFF_LIMIT_SECONDS:
        performance_status = "staff_limit_exceeded"
    return {
        "status": status,
        "run_id_hash": f"sha256:{run_hash}",
        "accounting_date": run["accounting_date"],
        "run_mode": run["run_mode"],
        "synthetic": run["run_mode"] == "synthetic",
        "current_stage": current_stage,
        "failed_stage": run["last_failed_stage"],
        "last_error_code": run["last_error_code"],
        "generation": run["generation"],
        "remaining_count": sum(remaining_by_stage.values()),
        "requires_review_count": item_status_counts.get("planned", 0),
        "blocked_count": item_status_counts.get("failed", 0),
        "unprocessed_count": sum(remaining_by_stage.values()),
        "stages": stages,
        "performance": {
            "elapsed_seconds": elapsed_seconds,
            "veteran_target_seconds": VETERAN_TARGET_SECONDS,
            "staff_limit_seconds": STAFF_LIMIT_SECONDS,
            "time_status": performance_status,
            "target_overrun_seconds": max(elapsed_seconds - VETERAN_TARGET_SECONDS, 0),
            "staff_limit_overrun_seconds": max(
                elapsed_seconds - STAFF_LIMIT_SECONDS, 0
            ),
            "retry_count": retry_count,
            "human_confirmation_count": 1,
            "quality_gates_unchanged": True,
            "overrun_stops_work": False,
        },
        "fast_path": {
            "eligible": status not in {"failed", "uncertain_external_write"}
            and retry_count == 0
            and (elapsed_seconds <= STAFF_LIMIT_SECONDS),
            "single_manifest_required": True,
            "successful_stages_reused": True,
            "ordinary_batch_additional_confirmation_required": False,
            "full_rag_rebuild_each_run": False,
            "repository_full_test_during_accounting_run": False,
        },
        "next_action": _next_action(
            status,
            current_stage,
            run["last_failed_stage"],
            bool(failed_row and failed_row["safe_to_retry"]),
            remaining_by_stage.get(str(current_stage), 0) if current_stage else 0,
        ),
        "raw_ids_stored": False,
        "raw_amounts_stored": False,
    }


def advance_run(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    evidence: Mapping[str, Any],
    actor: str = "codex",
) -> dict[str, Any]:
    stage = _validate_stage(stage)
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    with _atomic(conn):
        run_hash, run = _get_run(conn, run_id)
        if run["status"] != "running":
            raise ValueError(
                "run must be running; resume the failed stage or read back an uncertain write first"
            )
        resolving_deferred = _is_prior_deferred_source_stage(
            conn, run_hash=run_hash, run=run, stage=stage
        )
        if not resolving_deferred:
            stage_row = conn.execute(
                "SELECT status FROM accounting_run_stages WHERE run_id_hash=? AND stage_key=?",
                (run_hash, stage),
            ).fetchone()
            if stage_row is None:
                raise ValueError(f"unknown stage in run: {stage!r}")
            if stage_row["status"] == "succeeded":
                raise ValueError(f"stage already succeeded: {stage!r}")
        evidence_dict = dict(evidence)
        remaining_count = _source_remaining_count(stage, evidence_dict)
        prepared_items = _prepare_items(
            stage,
            evidence_dict.get("items"),
            allow_unresolved=remaining_count > 0 and (not resolving_deferred),
        )
        evidence_hash, binding_hash = _validate_evidence(
            conn, run_hash, stage, evidence_dict, run_mode=str(run["run_mode"])
        )
        if resolving_deferred:
            if not prepared_items:
                raise ValueError(
                    "deferred source resolution requires at least one resolved item"
                )
            previous = conn.execute(
                "\n                SELECT binding_hash\n                FROM accounting_run_stages\n                WHERE run_id_hash = ? AND stage_key = ? AND status = 'succeeded'\n                ",
                (run_hash, stage),
            ).fetchone()
            if previous is None:
                raise ValueError(
                    "deferred source resolution requires a succeeded prior stage"
                )
            if binding_hash is None or previous["binding_hash"] == binding_hash:
                raise ValueError(
                    "deferred source resolution requires a fresh authoritative receipt"
                )
            _record_prepared_items(
                conn, run_hash, stage, prepared_items, existing_unresolved_only=True
            )
            unresolved = conn.execute(
                "\n                SELECT COUNT(*) AS count\n                FROM accounting_run_items\n                WHERE run_id_hash = ? AND source_stage = ?\n                  AND status NOT IN ('verified', 'not_applicable')\n                ",
                (run_hash, stage),
            ).fetchone()
            actual_remaining = int(unresolved["count"]) if unresolved is not None else 0
            if actual_remaining != remaining_count:
                raise ValueError(
                    "remaining_count must match the prior source stage unresolved items"
                )
            stage_update = conn.execute(
                "\n                UPDATE accounting_run_stages\n                SET input_hash = ?,\n                    output_hash = ?,\n                    binding_hash = ?,\n                    failure_code = NULL,\n                    failure_hash = NULL,\n                    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),\n                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                WHERE run_id_hash = ? AND stage_key = ? AND status = 'succeeded'\n                  AND binding_hash IS ?\n                ",
                (
                    evidence_hash,
                    evidence_hash,
                    binding_hash,
                    run_hash,
                    stage,
                    previous["binding_hash"],
                ),
            )
            if stage_update.rowcount != 1:
                raise RuntimeError(
                    f"concurrent deferred source resolution rejected: {stage}"
                )
            return build_run_status(conn, run_id)
        defer_unresolved = (
            remaining_count > 0
            and run["run_mode"] == "production"
            and (stage in _SOURCE_STAGE_MAP)
        )
        if remaining_count > 0:
            unresolved_item_hashes = sorted(
                (
                    item_hash
                    for item_hash, item_status, _, _ in prepared_items
                    if item_status not in {"verified", "not_applicable"}
                )
            )
            if len(unresolved_item_hashes) != remaining_count:
                raise ValueError(
                    "remaining_count must equal the enumerated unresolved run items"
                )
            delta_hash = stable_hash(
                {"stage": stage, "remaining_item_hashes": unresolved_item_hashes}
            )
            previous = conn.execute(
                "\n                SELECT output_hash\n                FROM accounting_run_stages\n                WHERE run_id_hash = ? AND stage_key = ?\n                ",
                (run_hash, stage),
            ).fetchone()
            no_progress = previous is not None and previous["output_hash"] == delta_hash
            if not defer_unresolved:
                _record_prepared_items(conn, run_hash, stage, prepared_items)
                return _record_source_delta(
                    conn,
                    run_id=run_id,
                    run_hash=run_hash,
                    run=run,
                    stage=stage,
                    evidence_hash=evidence_hash,
                    binding_hash=binding_hash,
                    delta_hash=delta_hash,
                    actor_hash=actor_hash,
                    no_progress=no_progress,
                )
        now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        stage_update = conn.execute(
            f"\n            UPDATE accounting_run_stages\n            SET status = 'succeeded',\n                attempt_count = attempt_count + 1,\n                input_hash = ?,\n                output_hash = ?,\n                binding_hash = ?,\n                failure_code = NULL,\n                failure_hash = NULL,\n                started_at = COALESCE(started_at, {now_sql}),\n                completed_at = {now_sql},\n                updated_at = {now_sql}\n            WHERE run_id_hash = ? AND stage_key = ?\n              AND status IN ('pending', 'running')\n            ",
            (evidence_hash, evidence_hash, binding_hash, run_hash, stage),
        )
        if stage_update.rowcount != 1:
            raise RuntimeError(
                f"concurrent or duplicate stage transition rejected: {stage}"
            )
        _record_prepared_items(conn, run_hash, stage, prepared_items)
        unresolved_stage_items = conn.execute(
            "\n            SELECT COUNT(*) AS count\n            FROM accounting_run_items\n            WHERE run_id_hash = ? AND source_stage = ?\n              AND status NOT IN ('verified', 'not_applicable')\n            ",
            (run_hash, stage),
        ).fetchone()
        if (
            not defer_unresolved
            and unresolved_stage_items is not None
            and (int(unresolved_stage_items["count"]) > 0)
        ):
            raise ValueError(
                "remaining source items must be resolved before stage completion"
            )
        pending = conn.execute(
            "\n            SELECT stage_key FROM accounting_run_stages\n            WHERE run_id_hash=? AND status IN ('pending','running')\n            ORDER BY stage_order\n            LIMIT 1\n            ",
            (run_hash,),
        ).fetchone()
        if pending is None:
            unresolved = conn.execute(
                "\n                SELECT COUNT(*) AS count\n                FROM accounting_run_items\n                WHERE run_id_hash = ? AND status NOT IN ('verified', 'not_applicable')\n                ",
                (run_hash,),
            ).fetchone()
            if unresolved is not None and int(unresolved["count"]) > 0:
                raise ValueError(
                    "run completion is blocked by unresolved accounting run items"
                )
            target_status = "complete"
            current_stage = None
            completed_sql = now_sql
        else:
            target_status = "running"
            current_stage = str(pending["stage_key"])
            completed_sql = "NULL"
            conn.execute(
                f"\n                UPDATE accounting_run_stages\n                SET started_at = COALESCE(started_at, {now_sql}),\n                    updated_at = {now_sql}\n                WHERE run_id_hash = ? AND stage_key = ? AND status = 'pending'\n                ",
                (run_hash, current_stage),
            )
        run_update = conn.execute(
            f"\n            UPDATE accounting_runs\n            SET status = ?,\n                current_stage = ?,\n                last_failed_stage = NULL,\n                last_error_code = NULL,\n                last_error_hash = NULL,\n                generation = generation + 1,\n                completed_at = {completed_sql},\n                updated_at = {now_sql}\n            WHERE run_id_hash = ?\n              AND status = 'running'\n              AND generation = ?\n            ",
            (target_status, current_stage, run_hash, int(run["generation"])),
        )
        if run_update.rowcount != 1:
            raise RuntimeError(f"concurrent run transition rejected: {stage}")
        _record_transition(
            conn,
            run_hash=run_hash,
            stage=stage,
            from_status="running",
            to_status=target_status,
            generation=int(run["generation"]) + 1,
            actor_hash=actor_hash,
            evidence_hash=evidence_hash,
        )
    return build_run_status(conn, run_id)


def _record_source_delta(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    run_hash: str,
    run: sqlite3.Row,
    stage: str,
    evidence_hash: str,
    binding_hash: str | None,
    delta_hash: str,
    actor_hash: str,
    no_progress: bool,
) -> dict[str, Any]:
    """Keep one source stage active, or stop when its remaining set is unchanged."""
    now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    stage_status = "blocked" if no_progress else "running"
    run_status = "failed" if no_progress else "running"
    failure_code = "no_progress" if no_progress else None
    stage_update = conn.execute(
        f"\n        UPDATE accounting_run_stages\n        SET status = ?,\n            attempt_count = attempt_count + 1,\n            input_hash = ?,\n            output_hash = ?,\n            binding_hash = ?,\n            failure_code = ?,\n            failure_hash = ?,\n            started_at = COALESCE(started_at, {now_sql}),\n            completed_at = NULL,\n            updated_at = {now_sql}\n        WHERE run_id_hash = ? AND stage_key = ?\n          AND status IN ('pending', 'running')\n        ",
        (
            stage_status,
            evidence_hash,
            delta_hash,
            binding_hash,
            failure_code,
            delta_hash if no_progress else None,
            run_hash,
            stage,
        ),
    )
    if stage_update.rowcount != 1:
        raise RuntimeError(f"concurrent or duplicate source delta rejected: {stage}")
    run_update = conn.execute(
        f"\n        UPDATE accounting_runs\n        SET status = ?,\n            current_stage = ?,\n            last_failed_stage = ?,\n            last_error_code = ?,\n            last_error_hash = ?,\n            generation = generation + 1,\n            updated_at = {now_sql}\n        WHERE run_id_hash = ? AND status = 'running'\n          AND current_stage = ? AND generation = ?\n        ",
        (
            run_status,
            stage,
            stage if no_progress else None,
            failure_code,
            delta_hash if no_progress else None,
            run_hash,
            stage,
            int(run["generation"]),
        ),
    )
    if run_update.rowcount != 1:
        raise RuntimeError(f"concurrent source delta transition rejected: {stage}")
    _record_transition(
        conn,
        run_hash=run_hash,
        stage=stage,
        from_status="running",
        to_status=run_status,
        generation=int(run["generation"]) + 1,
        actor_hash=actor_hash,
        evidence_hash=delta_hash,
    )
    return build_run_status(conn, run_id)


def fail_run_stage(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    reason: str,
    error_code: str = "stage_failed",
    safe_to_retry: bool = True,
    actor: str = "codex",
    *,
    uncertain_external_write: bool = False,
) -> dict[str, Any]:
    stage = _validate_stage(stage)
    reason = _require_text(reason, "reason")
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    with _atomic(conn):
        run_hash, run = _get_run(conn, run_id)
        if run["status"] != "running":
            raise ValueError("only a running run can record a new stage failure")
        stage_row = conn.execute(
            "SELECT status FROM accounting_run_stages WHERE run_id_hash=? AND stage_key=?",
            (run_hash, stage),
        ).fetchone()
        if stage_row is None:
            raise ValueError(f"unknown stage: {stage!r}")
        stage_status = (
            "uncertain_external_write"
            if uncertain_external_write
            else "retryable_failure"
            if safe_to_retry
            else "blocked"
        )
        run_status = (
            "uncertain_external_write" if uncertain_external_write else "failed"
        )
        failure_hash = stable_hash(
            {
                "run_id_hash": run_hash,
                "stage": stage,
                "error_code": error_code,
                "reason": reason,
            }
        )
        now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        stage_update = conn.execute(
            f"\n            UPDATE accounting_run_stages\n            SET status = ?,\n                attempt_count = attempt_count + 1,\n                failure_code = ?,\n                failure_hash = ?,\n                started_at = COALESCE(started_at, {now_sql}),\n                updated_at = {now_sql}\n            WHERE run_id_hash = ? AND stage_key = ?\n              AND status IN ('pending', 'running')\n            ",
            (stage_status, error_code, failure_hash, run_hash, stage),
        )
        if stage_update.rowcount != 1:
            raise RuntimeError(
                f"concurrent or duplicate failure transition rejected: {stage}"
            )
        run_update = conn.execute(
            f"\n            UPDATE accounting_runs\n            SET status = ?,\n                last_failed_stage = ?,\n                last_error_code = ?,\n                last_error_hash = ?,\n                generation = generation + 1,\n                updated_at = {now_sql}\n            WHERE run_id_hash = ?\n              AND status = 'running'\n              AND current_stage = ?\n              AND generation = ?\n            ",
            (
                run_status,
                stage,
                error_code,
                failure_hash,
                run_hash,
                stage,
                int(run["generation"]),
            ),
        )
        if run_update.rowcount != 1:
            raise RuntimeError(f"concurrent run failure transition rejected: {stage}")
        _record_transition(
            conn,
            run_hash=run_hash,
            stage=stage,
            from_status="running",
            to_status=run_status,
            generation=int(run["generation"]) + 1,
            actor_hash=actor_hash,
            evidence_hash=failure_hash,
        )
    return build_run_status(conn, run_id)


def resume_run(
    conn: sqlite3.Connection, run_id: str, actor: str = "codex"
) -> dict[str, Any]:
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    with _atomic(conn):
        run_hash, run = _get_run(conn, run_id)
        if run["status"] == "running":
            return build_run_status(conn, run_id)
        if run["status"] == "uncertain_external_write":
            raise ValueError(
                "uncertain external write requires read-back evidence before retry"
            )
        if run["status"] != "failed" or not run["last_failed_stage"]:
            raise ValueError("only a failed run can be resumed")
        stage = str(run["last_failed_stage"])
        stage_update = conn.execute(
            "\n            UPDATE accounting_run_stages\n            SET status = 'pending',\n                failure_code = NULL,\n                failure_hash = NULL,\n                completed_at = NULL,\n                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n            WHERE run_id_hash = ? AND stage_key = ?\n              AND status = 'retryable_failure'\n            ",
            (run_hash, stage),
        )
        if stage_update.rowcount != 1:
            raise ValueError(
                "failed stage is blocked and cannot be retried automatically"
            )
        run_update = conn.execute(
            "\n            UPDATE accounting_runs\n            SET status = 'running',\n                current_stage = ?,\n                last_failed_stage = NULL,\n                last_error_code = NULL,\n                last_error_hash = NULL,\n                generation = generation + 1,\n                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n            WHERE run_id_hash = ? AND status = 'failed' AND generation = ?\n            ",
            (stage, run_hash, int(run["generation"])),
        )
        if run_update.rowcount != 1:
            raise RuntimeError("concurrent run resume rejected")
        _record_transition(
            conn,
            run_hash=run_hash,
            stage=stage,
            from_status="failed",
            to_status="running",
            generation=int(run["generation"]) + 1,
            actor_hash=actor_hash,
            evidence_hash=None,
        )
    return build_run_status(conn, run_id)


def resolve_uncertain_write(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    *,
    outcome: str,
    readback_evidence: Mapping[str, Any],
    actor: str = "codex",
) -> dict[str, Any]:
    """Resolve an unknown external write without ever performing a blind retry.

    ``applied`` requires the same authoritative stage evidence as a normal
    success. ``not_applied`` makes the stage retryable. ``blocked`` records a
    terminal manual-resolution state.
    """
    stage = _validate_stage(stage)
    if outcome not in {"applied", "not_applied", "blocked"}:
        raise ValueError(
            "uncertain write outcome must be applied, not_applied, or blocked"
        )
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    if outcome == "applied":
        with _atomic(conn):
            run_hash, run = _get_run(conn, run_id)
            if run["status"] != "uncertain_external_write":
                raise ValueError("run is not waiting for read-back at this stage")
            prepared_items = _prepare_items(stage, readback_evidence.get("items"))
            evidence_hash, binding_hash = _validate_evidence(
                conn,
                run_hash,
                stage,
                dict(readback_evidence),
                run_mode=str(run["run_mode"]),
            )
            stage_update = conn.execute(
                "\n                UPDATE accounting_run_stages\n                SET status = 'succeeded',\n                    input_hash = ?,\n                    output_hash = ?,\n                    binding_hash = ?,\n                    failure_code = NULL,\n                    failure_hash = NULL,\n                    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),\n                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                WHERE run_id_hash = ? AND stage_key = ?\n                  AND status = 'uncertain_external_write'\n                ",
                (evidence_hash, evidence_hash, binding_hash, run_hash, stage),
            )
            if stage_update.rowcount != 1:
                raise RuntimeError("concurrent uncertain-write resolution rejected")
            _record_prepared_items(conn, run_hash, stage, prepared_items)
            pending = conn.execute(
                "SELECT stage_key FROM accounting_run_stages WHERE run_id_hash=? AND status IN ('pending','running') ORDER BY stage_order LIMIT 1",
                (run_hash,),
            ).fetchone()
            next_stage = str(pending["stage_key"]) if pending is not None else None
            if next_stage is None:
                unresolved = conn.execute(
                    "\n                    SELECT COUNT(*) AS count\n                    FROM accounting_run_items\n                    WHERE run_id_hash = ? AND status NOT IN ('verified', 'not_applicable')\n                    ",
                    (run_hash,),
                ).fetchone()
                if unresolved is not None and int(unresolved["count"]) > 0:
                    raise ValueError(
                        "run completion is blocked by unresolved accounting run items"
                    )
            target_status = "complete" if next_stage is None else "running"
            if next_stage is not None:
                conn.execute(
                    "\n                    UPDATE accounting_run_stages\n                    SET started_at = COALESCE(\n                            started_at,\n                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                        ),\n                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                    WHERE run_id_hash = ? AND stage_key = ? AND status = 'pending'\n                    ",
                    (run_hash, next_stage),
                )
            run_update = conn.execute(
                "\n                UPDATE accounting_runs\n                SET status = ?,\n                    current_stage = ?,\n                    last_failed_stage = NULL,\n                    last_error_code = NULL,\n                    last_error_hash = NULL,\n                    generation = generation + 1,\n                    completed_at = CASE WHEN ? = 'complete'\n                        THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') ELSE NULL END,\n                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                WHERE run_id_hash = ? AND status = 'uncertain_external_write'\n                  AND current_stage = ? AND generation = ?\n                ",
                (
                    target_status,
                    next_stage,
                    target_status,
                    run_hash,
                    stage,
                    int(run["generation"]),
                ),
            )
            if run_update.rowcount != 1:
                raise RuntimeError("concurrent uncertain run resolution rejected")
            _record_transition(
                conn,
                run_hash=run_hash,
                stage=stage,
                from_status="uncertain_external_write",
                to_status=target_status,
                generation=int(run["generation"]) + 1,
                actor_hash=actor_hash,
                evidence_hash=evidence_hash,
            )
        return build_run_status(conn, run_id)
    with _atomic(conn):
        run_hash, run = _get_run(conn, run_id)
        if run["status"] != "uncertain_external_write":
            raise ValueError("run is not waiting for read-back at this stage")
        readback = dict(readback_evidence)
        if run["run_mode"] == "production":
            readback_receipt_hash = _parse_hash(
                readback.get("readback_receipt_hash"), "readback_receipt_hash"
            )
            _require_run_receipt(
                conn,
                run_hash=run_hash,
                stage=stage,
                receipt_kind=f"uncertain_{outcome}_readback",
                receipt_hash=readback_receipt_hash,
                accounting_date=str(run["accounting_date"]),
            )
        else:
            if (
                readback.get("status") != "verified"
                or readback.get("outcome") != outcome
                or readback.get("readback_performed") is not True
            ):
                raise ValueError(
                    "synthetic uncertain-write resolution still requires verified read-back"
                )
            readback_receipt_hash = _parse_hash(
                readback.get("readback_hash"), "readback_hash"
            )
        reason = str(readback.get("reason") or f"readback_{outcome}")
        stage_status = "retryable_failure" if outcome == "not_applied" else "blocked"
        run_status = "failed"
        failure_hash = stable_hash(
            {
                "run_id_hash": run_hash,
                "stage": stage,
                "outcome": outcome,
                "readback_receipt_hash": readback_receipt_hash,
                "reason": reason,
            }
        )
        stage_update = conn.execute(
            "\n            UPDATE accounting_run_stages\n            SET status = ?, failure_code = ?, failure_hash = ?,\n                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n            WHERE run_id_hash = ? AND stage_key = ?\n              AND status = 'uncertain_external_write'\n            ",
            (stage_status, f"readback_{outcome}", failure_hash, run_hash, stage),
        )
        if stage_update.rowcount != 1:
            raise RuntimeError("concurrent uncertain stage resolution rejected")
        run_update = conn.execute(
            "\n            UPDATE accounting_runs\n            SET status = ?, last_error_code = ?, last_error_hash = ?,\n                generation = generation + 1,\n                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n            WHERE run_id_hash = ? AND status = 'uncertain_external_write'\n              AND current_stage = ? AND generation = ?\n            ",
            (
                run_status,
                f"readback_{outcome}",
                failure_hash,
                run_hash,
                stage,
                int(run["generation"]),
            ),
        )
        if run_update.rowcount != 1:
            raise RuntimeError("concurrent uncertain run resolution rejected")
        _record_transition(
            conn,
            run_hash=run_hash,
            stage=stage,
            from_status="uncertain_external_write",
            to_status=run_status,
            generation=int(run["generation"]) + 1,
            actor_hash=actor_hash,
            evidence_hash=failure_hash,
        )
    return build_run_status(conn, run_id)


def run_evidence_manifest(
    conn: sqlite3.Connection,
    run_id: str,
    manifest: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    accounting_date: str | None = None,
    instruction: str = "仕分けを行ってください",
    actor: str = "codex",
    run_mode: str = "production",
) -> dict[str, Any]:
    status = build_run_status(conn, run_id)
    if status["status"] == "not_found":
        status = start_run(
            conn,
            run_id,
            accounting_date=accounting_date,
            instruction=instruction,
            actor=actor,
            run_mode=run_mode,
        )
    else:
        start_run(
            conn,
            run_id,
            accounting_date=accounting_date,
            instruction=instruction,
            actor=actor,
            run_mode=run_mode,
        )
    if status["status"] != "running":
        raise ValueError("manifest execution requires a running run")
    if isinstance(manifest, Mapping):
        evidence_by_stage = dict(manifest)
    else:
        evidence_by_stage = {}
        for item in manifest:
            if not isinstance(item, Mapping):
                raise ValueError("each manifest item must be an object")
            stage = str(item.get("stage") or "")
            if stage in evidence_by_stage:
                raise ValueError(f"duplicate manifest stage: {stage}")
            evidence = item.get("evidence", item)
            evidence_by_stage[stage] = evidence
    unknown = sorted(set(evidence_by_stage) - set(RUN_STAGES))
    if unknown:
        raise ValueError(f"unknown manifest stages: {', '.join(unknown)}")
    for stage in evidence_by_stage:
        try:
            status = advance_run(
                conn, run_id, stage, evidence_by_stage[stage], actor=actor
            )
        except (KeyError, TypeError, ValueError) as exc:
            fail_run_stage(
                conn,
                run_id,
                stage,
                reason=str(exc),
                error_code="invalid_stage_evidence",
                safe_to_retry=True,
                actor=actor,
            )
            return build_run_status(conn, run_id)
        if status["status"] != "running":
            return status
    return status


def _validate_evidence(
    conn: sqlite3.Connection,
    run_hash: str,
    stage: str,
    evidence: dict[str, Any],
    *,
    run_mode: str,
) -> tuple[str, str | None]:
    if evidence.get("status") != "verified":
        raise ValueError(f"{stage} evidence status must be verified")
    evidence_hash = _parse_hash(evidence.get("evidence_hash"), "evidence_hash")
    binding_hash: str | None = None
    if stage in RUN_STAGES[:7]:
        if evidence.get("coverage_state") not in {
            "captured",
            "zero_confirmed",
            "not_applicable",
        }:
            raise ValueError(f"{stage} requires a complete coverage_state")
        remaining_count = _source_remaining_count(stage, evidence)
        expected_resolved = remaining_count == 0
        if evidence.get("all_items_resolved") is not expected_resolved:
            raise ValueError(f"{stage} all_items_resolved must match remaining_count")
    if run_mode == "production":
        evidence_hash, binding_hash = _validate_production_evidence(
            conn, run_hash, stage, evidence
        )
    if stage == "all_receipt_folders":
        folder_count = int(evidence.get("folder_count", -1))
        manifest_count = int(evidence.get("manifest_folder_count", -2))
        if (
            not evidence.get("all_folders_covered")
            or folder_count < 0
            or folder_count != manifest_count
        ):
            raise ValueError(
                "all receipt folders require a complete full-folder manifest"
            )
    elif stage == "cross_audit":
        required = {
            "duplicate_journal_check",
            "debit_credit_amount_check",
            "account_sub_account_tax_check",
            "source_statement_evidence_reconciliation",
            "processed_folder_readback",
        }
        checks = evidence.get("audit_checks")
        if not isinstance(checks, Mapping) or any(
            (checks.get(check) != "verified" for check in required)
        ):
            raise ValueError("cross audit requires all five read-back checks")
    elif stage == "google_chat_durable_sent":
        if evidence.get("delivery_status") not in {
            "sent",
            "historical_sent",
            "already_sent",
        }:
            raise ValueError(
                "Google Chat completion notification must have a durable sent receipt"
            )
        if (
            run_mode == "production"
            and evidence.get("delivery_status") in {"already_sent", "historical_sent"}
            and evidence.get("dedup_receipt_hash")
        ):
            notification_hash = binding_hash
        else:
            notification_hash = _parse_hash(
                evidence.get("notification_hash"), "notification_hash"
            )
            _parse_hash(evidence.get("response_hash"), "response_hash")
            if binding_hash is None:
                binding_hash = notification_hash
    elif stage == "rag_learning":
        notification_hash = _parse_hash(
            evidence.get("notification_hash"), "notification_hash"
        )
        _parse_hash(evidence.get("learning_manifest_hash"), "learning_manifest_hash")
        sent = _stage_binding_hash(conn, run_hash, "google_chat_durable_sent")
        if sent is None or notification_hash != sent:
            raise ValueError(
                "notification hash binding does not match the same-run durable receipt"
            )
        if binding_hash is None:
            binding_hash = notification_hash
    elif stage == "issue_update":
        if evidence.get("issue_delivery_status") != "verified":
            raise ValueError("issue update requires a verified durable issue receipt")
        issue_receipt_hash = _parse_hash(
            evidence.get("issue_receipt_hash"), "issue_receipt_hash"
        )
        if binding_hash is None:
            binding_hash = issue_receipt_hash
        if _stage_binding_hash(conn, run_hash, "google_chat_durable_sent") is None:
            raise ValueError("issue receipt requires the same-run durable notification")
        if _stage_binding_hash(conn, run_hash, "rag_learning") is None:
            raise ValueError("issue receipt requires same-run RAG learning")
    return (evidence_hash, binding_hash)


def _source_remaining_count(stage: str, evidence: Mapping[str, Any]) -> int:
    if stage not in _SOURCE_STAGE_MAP:
        return 0
    try:
        remaining_count = int(evidence.get("remaining_count", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{stage} remaining_count must be a non-negative integer"
        ) from exc
    if remaining_count < 0:
        raise ValueError(f"{stage} remaining_count must be a non-negative integer")
    return remaining_count


def _validate_production_evidence(
    conn: sqlite3.Connection, run_hash: str, stage: str, evidence: dict[str, Any]
) -> tuple[str, str | None]:
    run = conn.execute(
        "SELECT accounting_date FROM accounting_runs WHERE run_id_hash = ?", (run_hash,)
    ).fetchone()
    if run is None:
        raise ValueError("accounting run not found for production evidence")
    accounting_date = str(run["accounting_date"])
    if stage in _SOURCE_STAGE_MAP:
        receipt_hash = _parse_hash(
            evidence.get("adapter_receipt_hash"), "adapter_receipt_hash"
        )
        _require_run_receipt(
            conn,
            run_hash=run_hash,
            stage=stage,
            receipt_kind="source_stage_readback",
            receipt_hash=receipt_hash,
            accounting_date=accounting_date,
        )
        return (receipt_hash, receipt_hash)
    if stage == "cross_audit":
        receipt_hash = _parse_hash(
            evidence.get("adapter_receipt_hash"), "adapter_receipt_hash"
        )
        _require_run_receipt(
            conn,
            run_hash=run_hash,
            stage=stage,
            receipt_kind="cross_audit_readback",
            receipt_hash=receipt_hash,
            accounting_date=accounting_date,
        )
        return (receipt_hash, receipt_hash)
    if stage == "google_chat_durable_sent":
        if evidence.get("delivery_status") in {
            "already_sent",
            "historical_sent",
        } and evidence.get("dedup_receipt_hash"):
            receipt_hash = _parse_hash(
                evidence.get("dedup_receipt_hash"), "dedup_receipt_hash"
            )
            _require_run_receipt(
                conn,
                run_hash=run_hash,
                stage=stage,
                receipt_kind="google_chat_dedup_readback",
                receipt_hash=receipt_hash,
                accounting_date=accounting_date,
            )
            return (receipt_hash, receipt_hash)
        notification_hash = _parse_hash(
            evidence.get("notification_hash"), "notification_hash"
        )
        response_hash = _parse_hash(evidence.get("response_hash"), "response_hash")
        row = conn.execute(
            "\n            SELECT notification_hash, response_hash\n            FROM google_chat_notification_records\n            WHERE notification_hash = ?\n              AND accounting_run_id_hash = ?\n              AND notification_type = 'completion'\n              AND status IN ('sent', 'historical_sent')\n              AND sent_at IS NOT NULL\n              AND (status = 'historical_sent' OR response_hash IS NOT NULL)\n            ",
            (notification_hash, run_hash),
        ).fetchone()
        if row is None:
            raise ValueError("same-run durable Google Chat sent record was not found")
        if row["response_hash"]:
            stored_response_hash = _parse_hash(
                row["response_hash"], "stored_response_hash"
            )
            if stored_response_hash != response_hash:
                raise ValueError(
                    "Google Chat response hash does not match durable record"
                )
        return (notification_hash, notification_hash)
    if stage == "rag_learning":
        receipt_hash = _parse_hash(
            evidence.get("learning_manifest_hash"), "learning_manifest_hash"
        )
        _require_run_receipt(
            conn,
            run_hash=run_hash,
            stage=stage,
            receipt_kind="verified_journal_learning",
            receipt_hash=receipt_hash,
            accounting_date=accounting_date,
        )
        return (
            receipt_hash,
            _parse_hash(evidence.get("notification_hash"), "notification_hash"),
        )
    if stage == "issue_update":
        receipt_hash = _parse_hash(
            evidence.get("issue_receipt_hash"), "issue_receipt_hash"
        )
        _require_run_receipt(
            conn,
            run_hash=run_hash,
            stage=stage,
            receipt_kind="github_issue_readback",
            receipt_hash=receipt_hash,
            accounting_date=accounting_date,
        )
        return (receipt_hash, receipt_hash)
    raise ValueError(f"production evidence adapter is missing for stage: {stage}")


def _prepare_items(
    stage: str, items: Any, *, allow_unresolved: bool = False
) -> list[tuple[str, str, str | None, bool]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    prepared: list[tuple[str, str, str | None, bool]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each run item must be an object")
        status = str(item.get("status") or "verified")
        if status not in {"planned", "verified", "not_applicable", "failed"}:
            raise ValueError(f"invalid run item status: {status}")
        if not allow_unresolved and status not in {"verified", "not_applicable"}:
            raise ValueError(
                "a succeeded stage cannot contain planned or failed run items"
            )
        if status == "not_applicable":
            discovered_event_fields = (
                "economic_event_discovered",
                "blocker",
                "blocker_code",
                "retry_condition",
            )
            if any(
                (
                    item.get(field) not in (None, "", [])
                    for field in discovered_event_fields
                )
            ):
                raise ValueError(
                    "a discovered economic event cannot be not_applicable; keep it unresolved"
                )
            if not str(item.get("not_applicable_basis") or "").strip():
                raise ValueError("not_applicable requires a documented basis")
            if item.get("no_economic_event_verified") is not True:
                raise ValueError(
                    "not_applicable requires no_economic_event_verified=true"
                )
            _parse_hash(
                item.get("not_applicable_readback_hash"), "not_applicable_readback_hash"
            )
        existing_item_hash = item.get("existing_item_hash")
        if existing_item_hash not in (None, ""):
            if any(
                (
                    item.get(field) not in (None, "")
                    for field in ("idempotency_key", "item_id", "item_hash")
                )
            ):
                raise ValueError(
                    "existing_item_hash cannot be combined with another item identity"
                )
            item_hash = _parse_hash(existing_item_hash, "existing_item_hash")
            references_existing = True
        else:
            immutable_identity = (
                item.get("idempotency_key")
                or item.get("item_id")
                or item.get("item_hash")
            )
            if immutable_identity:
                item_hash = stable_hash(
                    {"stage": stage, "identity": immutable_identity}
                )
            else:
                immutable_item = {
                    key: value
                    for key, value in dict(item).items()
                    if key not in {"status", "result", "updated_at", "completed_at"}
                }
                item_hash = stable_hash({"stage": stage, "item": immutable_item})
            references_existing = False
        idempotency_hash = (
            stable_hash({"idempotency_key": item["idempotency_key"]})
            if item.get("idempotency_key")
            else None
        )
        prepared.append((item_hash, status, idempotency_hash, references_existing))
    return prepared


def _record_prepared_items(
    conn: sqlite3.Connection,
    run_hash: str,
    stage: str,
    prepared: Sequence[tuple[str, str, str | None, bool]],
    *,
    existing_unresolved_only: bool = False,
) -> None:
    for item_hash, status, idempotency_hash, references_existing in prepared:
        if existing_unresolved_only and (not references_existing):
            raise ValueError(
                "prior deferred source resolution requires existing_item_hash"
            )
        if references_existing:
            if status not in {"verified", "not_applicable"}:
                if existing_unresolved_only:
                    raise ValueError(
                        "deferred source items can only advance to a terminal status"
                    )
            updated = conn.execute(
                "\n                UPDATE accounting_run_items\n                SET status = ?,\n                    idempotency_key_hash = COALESCE(idempotency_key_hash, ?),\n                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                WHERE run_id_hash = ? AND item_hash = ? AND source_stage = ?\n                  AND status NOT IN ('verified', 'not_applicable')\n                ",
                (status, idempotency_hash, run_hash, item_hash, stage),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    "existing_item_hash must match an existing unresolved item in the source stage"
                )
            if status in {"verified", "not_applicable"}:
                _clear_item_resolution_metadata(conn, run_hash, item_hash)
            continue
        conn.execute(
            "\n            INSERT INTO accounting_run_items (\n                run_id_hash, item_hash, source_stage, status, idempotency_key_hash\n            ) VALUES (?, ?, ?, ?, ?)\n            ON CONFLICT(run_id_hash, item_hash) DO UPDATE SET\n                status = excluded.status,\n                idempotency_key_hash = excluded.idempotency_key_hash,\n                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n            ",
            (run_hash, item_hash, stage, status, idempotency_hash),
        )
        if status in {"verified", "not_applicable"}:
            _clear_item_resolution_metadata(conn, run_hash, item_hash)


def _clear_item_resolution_metadata(
    conn: sqlite3.Connection, run_hash: str, item_hash: str
) -> None:
    """Remove active blocker fields after an item reaches a terminal state."""
    conn.execute(
        "\n        UPDATE accounting_run_items\n        SET resolution_owner_role = '',\n            resolution_next_action = '',\n            resolution_target_due_at = '',\n            resolution_blocker_code = '',\n            resolution_last_error_code = '',\n            resolution_last_attempt_at = NULL\n        WHERE run_id_hash = ? AND item_hash = ?\n          AND status IN ('verified', 'not_applicable')\n        ",
        (run_hash, item_hash),
    )


def record_source_stage_receipt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    receipt: Mapping[str, Any],
    actor: str = "source_adapter",
) -> str:
    """Persist a source adapter read-back bound to one production run/date."""
    stage = _validate_stage(stage)
    if stage not in _SOURCE_STAGE_MAP:
        raise ValueError("source adapter receipt is allowed only for a source stage")
    run_hash, run = _get_run(conn, run_id)
    if run["run_mode"] != "production" or run["status"] != "running":
        raise ValueError("source adapter receipts require a running production run")
    receipt_dict = dict(receipt)
    _validate_receipt_profile(receipt_dict)
    _validate_stage_receipt(
        receipt_dict,
        run_hash=run_hash,
        stage=stage,
        accounting_date=str(run["accounting_date"]),
    )
    if stage == "payroll_bonus":
        _validate_payroll_receipt(
            receipt_dict, accounting_date=str(run["accounting_date"])
        )
    source_digest = stable_hash(
        {
            "adapter_receipt": receipt_dict,
            "run_id_hash": run_hash,
            "accounting_date": run["accounting_date"],
        }
    )
    return _record_verified_run_receipt(
        conn,
        run_id=run_id,
        stage=stage,
        receipt_kind="source_stage_readback",
        source_hash=f"sha256:{source_digest}",
        actor=actor,
    )


def record_cross_audit_receipt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    receipt: Mapping[str, Any],
    actor: str = "cross_audit_adapter",
) -> str:
    """Persist the authoritative five-check cross-source audit read-back."""
    stage = "cross_audit"
    run_hash, run = _get_run(conn, run_id)
    if run["run_mode"] != "production" or run["status"] != "running":
        raise ValueError("cross-audit receipts require a running production run")
    receipt_dict = dict(receipt)
    _validate_receipt_profile(receipt_dict)
    _validate_stage_receipt(
        receipt_dict,
        run_hash=run_hash,
        stage=stage,
        accounting_date=str(run["accounting_date"]),
    )
    required = {
        "duplicate_journal_check",
        "debit_credit_amount_check",
        "account_sub_account_tax_check",
        "source_statement_evidence_reconciliation",
        "processed_folder_readback",
    }
    checks = receipt_dict.get("audit_checks")
    if not isinstance(checks, Mapping) or any(
        (checks.get(check) != "verified" for check in required)
    ):
        raise ValueError("cross-audit adapter must verify all five read-back checks")
    if receipt_dict.get("completion_notification_allowed") is not True:
        raise ValueError(
            "cross-audit adapter has not authorized completion notification"
        )
    _parse_hash(receipt_dict.get("daily_control_hash"), "daily_control_hash")
    _parse_hash(receipt_dict.get("post_audit_hash"), "post_audit_hash")
    source_digest = stable_hash(
        {
            "adapter_receipt": receipt_dict,
            "run_id_hash": run_hash,
            "accounting_date": run["accounting_date"],
        }
    )
    return _record_verified_run_receipt(
        conn,
        run_id=run_id,
        stage=stage,
        receipt_kind="cross_audit_readback",
        source_hash=f"sha256:{source_digest}",
        actor=actor,
    )


def record_uncertain_write_readback(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    outcome: str,
    receipt: Mapping[str, Any],
    actor: str = "write_readback_adapter",
) -> str:
    """Persist read-back proving an uncertain write absent or blocked."""
    stage = _validate_stage(stage)
    if outcome not in {"not_applied", "blocked"}:
        raise ValueError("uncertain read-back outcome must be not_applied or blocked")
    run_hash, run = _get_run(conn, run_id)
    if run["run_mode"] != "production":
        raise ValueError("uncertain write receipts require a production run")
    if run["status"] != "uncertain_external_write":
        raise ValueError(
            "run is not waiting for uncertain-write read-back at this stage"
        )
    receipt_dict = dict(receipt)
    _validate_receipt_profile(receipt_dict)
    if (
        receipt_dict.get("status") != "verified"
        or receipt_dict.get("outcome") != outcome
    ):
        raise ValueError(
            "uncertain write adapter receipt must verify the exact outcome"
        )
    if (
        _parse_hash(
            receipt_dict.get("accounting_run_id_hash"), "accounting_run_id_hash"
        )
        != run_hash
    ):
        raise ValueError("uncertain write receipt run binding mismatch")
    if receipt_dict.get("accounting_date") != run["accounting_date"]:
        raise ValueError("uncertain write receipt accounting date mismatch")
    if receipt_dict.get("readback_performed") is not True:
        raise ValueError("uncertain write receipt requires authoritative read-back")
    if _parse_hash(receipt_dict.get("office_hash"), "office_hash") != stable_hash(
        {"office": _office_name()}
    ):
        raise ValueError("uncertain write receipt office is not configured_company")
    if _parse_hash(
        receipt_dict.get("execution_principal_hash"), "execution_principal_hash"
    ) != stable_hash({"principal": _mf_principal()}):
        raise ValueError(
            "uncertain write receipt principal is not info@configured_company"
        )
    _parse_hash(receipt_dict.get("readback_hash"), "readback_hash")
    if outcome == "not_applied":
        if receipt_dict.get("external_write_absence_verified") is not True:
            raise ValueError(
                "not_applied requires verified absence of the external write"
            )
    else:
        if receipt_dict.get("manual_resolution_required") is not True:
            raise ValueError("blocked requires a verified manual-resolution boundary")
        _parse_hash(receipt_dict.get("blocker_hash"), "blocker_hash")
    source_digest = stable_hash(
        {
            "adapter_receipt": receipt_dict,
            "run_id_hash": run_hash,
            "stage": stage,
            "accounting_date": run["accounting_date"],
        }
    )
    return _record_verified_run_receipt(
        conn,
        run_id=run_id,
        stage=stage,
        receipt_kind=f"uncertain_{outcome}_readback",
        source_hash=f"sha256:{source_digest}",
        actor=actor,
    )


def _record_run_receipt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    receipt_kind: str,
    source_hash: str,
    actor: str = "codex",
) -> str:
    """Persist an integration-private receipt after authoritative read-back.

    This function does not perform the external action.  The Chat adapter uses
    its dedicated delivery ledger; verified learning and GitHub read-back
    adapters use this small, redacted receipt registry.  It is intentionally
    private so callers cannot treat arbitrary summary JSON as a public receipt
    registration API.
    """
    stage = _validate_stage(stage)
    allowed = {
        *(
            (source_stage, "source_stage_readback")
            for source_stage in _SOURCE_STAGE_MAP
        ),
        ("cross_audit", "cross_audit_readback"),
        ("google_chat_durable_sent", "google_chat_dedup_readback"),
        ("rag_learning", "verified_journal_learning"),
        ("issue_update", "github_issue_readback"),
    }
    if receipt_kind in {"uncertain_not_applied_readback", "uncertain_blocked_readback"}:
        allowed.add((stage, receipt_kind))
    if (stage, receipt_kind) not in allowed:
        raise ValueError("unsupported accounting run receipt kind")
    source_digest = _parse_hash(source_hash, "source_hash")
    actor_hash = stable_hash({"actor": _require_text(actor, "actor")})
    with _atomic(conn):
        run_hash, run = _get_run(conn, run_id)
        if run["run_mode"] != "production":
            raise ValueError("durable external receipts are for production runs only")
        prior_deferred_receipt = (
            receipt_kind == "source_stage_readback"
            and _is_prior_deferred_source_stage(
                conn, run_hash=run_hash, run=run, stage=stage
            )
        )
        receipt_hash = stable_hash(
            {
                "run_id_hash": run_hash,
                "accounting_date": run["accounting_date"],
                "stage": stage,
                "receipt_kind": receipt_kind,
                "source_hash": source_digest,
                "boundary_fingerprint": get_current_profile().boundary_fingerprint,
            }
        )
        conn.execute(
            "\n            INSERT INTO accounting_run_receipts (\n                receipt_hash, run_id_hash, stage_key, receipt_kind,\n                status, accounting_date, source_hash, actor_hash\n            ) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?)\n            ON CONFLICT(receipt_hash) DO NOTHING\n            ",
            (
                receipt_hash,
                run_hash,
                stage,
                receipt_kind,
                run["accounting_date"],
                source_digest,
                actor_hash,
            ),
        )
        return f"sha256:{receipt_hash}"


def _record_verified_run_receipt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    receipt_kind: str,
    source_hash: str,
    actor: str,
) -> str:
    """Private integration boundary for adapters that performed read-back."""
    return _record_run_receipt(
        conn,
        run_id=run_id,
        stage=stage,
        receipt_kind=receipt_kind,
        source_hash=source_hash,
        actor=actor,
    )


def _validate_stage_receipt(
    receipt: dict[str, Any], *, run_hash: str, stage: str, accounting_date: str
) -> None:
    if receipt.get("status") != "verified":
        raise ValueError(f"{stage} authoritative receipt must be verified")
    if receipt.get("stage") != stage:
        raise ValueError(f"{stage} authoritative receipt stage mismatch")
    receipt_run_hash = _parse_hash(
        receipt.get("accounting_run_id_hash"), "accounting_run_id_hash"
    )
    if receipt_run_hash != run_hash:
        raise ValueError(f"{stage} authoritative receipt run binding mismatch")
    if receipt.get("accounting_date") != accounting_date:
        raise ValueError(f"{stage} authoritative receipt accounting date mismatch")
    _parse_hash(receipt.get("source_snapshot_hash"), "source_snapshot_hash")
    _parse_hash(receipt.get("readback_hash"), "readback_hash")
    remaining_count = int(receipt.get("remaining_count", -1))
    if remaining_count < 0:
        raise ValueError(f"{stage} receipt remaining_count must be non-negative")
    if stage not in _SOURCE_STAGE_MAP and remaining_count != 0:
        raise ValueError(f"{stage} receipt must have zero remaining items")
    expected_resolved = remaining_count == 0
    if receipt.get("all_items_resolved") is not expected_resolved:
        raise ValueError(
            f"{stage} receipt all_items_resolved must match remaining_count"
        )
    if receipt.get("coverage_state") not in {
        "captured",
        "zero_confirmed",
        "not_applicable",
    }:
        raise ValueError(f"{stage} receipt coverage is incomplete")
    if receipt.get("identity_readback_verified") is not True:
        raise ValueError(
            f"{stage} receipt requires configured company identity read-back"
        )
    if _parse_hash(receipt.get("office_hash"), "office_hash") != stable_hash(
        {"office": _office_name()}
    ):
        raise ValueError(f"{stage} receipt office is not configured company")
    expected_principal = _source_stage_principals().get(stage, _mf_principal())
    if _parse_hash(
        receipt.get("execution_principal_hash"), "execution_principal_hash"
    ) != stable_hash({"principal": expected_principal}):
        raise ValueError(f"{stage} receipt principal is not {expected_principal}")


def _validate_payroll_receipt(receipt: dict[str, Any], *, accounting_date: str) -> None:
    if receipt.get("coverage_state") != "captured":
        return
    required = (
        "wage_ledger_readback_verified",
        "employee_allocation_verified",
        "accrual_readback_verified",
        "settlement_readback_verified",
    )
    if any((receipt.get(key) is not True for key in required)):
        raise ValueError(
            "payroll requires wage ledger, employee allocation, accrual and settlement read-back"
        )
    if receipt.get("generic_liability_sub_account_used") is not False:
        raise ValueError(
            "payroll cannot use generic 給与/従業員 liability sub-accounts"
        )
    payroll_period = _require_text(receipt.get("payroll_period"), "payroll_period")
    if not re.fullmatch("\\d{4}-\\d{2}", payroll_period):
        raise ValueError("payroll_period must be YYYY-MM")
    payment_date = _require_text(receipt.get("payment_date"), "payment_date")
    if payment_date != accounting_date:
        raise ValueError("payroll payment date must match the accounting run date")
    accounting_day = date.fromisoformat(accounting_date)
    previous_month = (accounting_day.replace(day=1) - timedelta(days=1)).strftime(
        "%Y-%m"
    )
    if payroll_period != previous_month:
        raise ValueError("payroll period must be the month before the payment date")
    for key in (
        "wage_ledger_readback_hash",
        "employee_allocation_hash",
        "accrual_readback_hash",
        "settlement_readback_hash",
    ):
        _parse_hash(receipt.get(key), key)
    employees = receipt.get("employees")
    if not isinstance(employees, list) or not employees:
        raise ValueError(
            "captured payroll requires employee-specific liability bindings"
        )
    for employee in employees:
        if not isinstance(employee, Mapping):
            raise ValueError("payroll employee binding must be an object")
        _parse_hash(employee.get("employee_hash"), "employee_hash")
        _parse_hash(
            employee.get("payable_sub_account_hash"), "payable_sub_account_hash"
        )
        for key in ("allocation_hash", "accrual_entry_hash", "settlement_entry_hash"):
            _parse_hash(employee.get(key), key)
        if employee.get("allocation_verified") is not True:
            raise ValueError("every payroll employee allocation must be verified")
        if employee.get("accrual_readback_verified") is not True:
            raise ValueError("every payroll employee accrual must be read back")
        if employee.get("settlement_readback_verified") is not True:
            raise ValueError("every payroll employee settlement must be read back")


def _require_run_receipt(
    conn: sqlite3.Connection,
    *,
    run_hash: str,
    stage: str,
    receipt_kind: str,
    receipt_hash: str,
    accounting_date: str,
) -> None:
    row = conn.execute(
        "\n        SELECT receipt_hash\n        FROM accounting_run_receipts\n        WHERE receipt_hash = ?\n          AND run_id_hash = ?\n          AND stage_key = ?\n          AND receipt_kind = ?\n          AND status = 'verified'\n          AND accounting_date = ?\n        ",
        (receipt_hash, run_hash, stage, receipt_kind, accounting_date),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"authoritative {receipt_kind} receipt was not found for this run"
        )


def _record_transition(
    conn: sqlite3.Connection,
    *,
    run_hash: str,
    stage: str | None,
    from_status: str,
    to_status: str,
    generation: int,
    actor_hash: str,
    evidence_hash: str | None,
) -> None:
    transition_hash = stable_hash(
        {
            "run_id_hash": run_hash,
            "stage": stage,
            "from_status": from_status,
            "to_status": to_status,
            "generation": generation,
            "actor_hash": actor_hash,
            "evidence_hash": evidence_hash,
        }
    )
    conn.execute(
        "\n        INSERT INTO accounting_run_transitions (\n            transition_hash, run_id_hash, stage_key, from_status, to_status,\n            generation, actor_hash, evidence_hash\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n        ",
        (
            transition_hash,
            run_hash,
            stage,
            from_status,
            to_status,
            generation,
            actor_hash,
            evidence_hash,
        ),
    )


def _require_non_operational_synthetic_database(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA database_list").fetchall()
    main_path = ""
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if name == "main":
            main_path = str(row["file"] if isinstance(row, sqlite3.Row) else row[2])
            break
    if not main_path:
        return
    database_path = Path(main_path).resolve()
    operational_root = _artifact_root().resolve()
    if database_path == operational_root or operational_root in database_path.parents:
        raise ValueError(
            "synthetic runs require a temporary non-operational database outside data/phase12"
        )


@contextmanager
def _atomic(conn: sqlite3.Connection):
    savepoint = "accounting_run_transition"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def _get_run(conn: sqlite3.Connection, run_id: str) -> tuple[str, sqlite3.Row]:
    run_hash = accounting_run_hash(_require_text(run_id, "run_id"))
    row = conn.execute(
        "SELECT * FROM accounting_runs WHERE run_id_hash = ?", (run_hash,)
    ).fetchone()
    if row is None:
        raise LookupError("accounting run not found")
    _validate_run_identity(row)
    return (run_hash, row)


def _stage_binding_hash(
    conn: sqlite3.Connection, run_hash: str, stage: str
) -> str | None:
    row = conn.execute(
        "\n        SELECT binding_hash FROM accounting_run_stages\n        WHERE run_id_hash = ? AND stage_key = ? AND status = 'succeeded'\n        ",
        (run_hash, stage),
    ).fetchone()
    return str(row["binding_hash"]) if row is not None and row["binding_hash"] else None


def _is_prior_deferred_source_stage(
    conn: sqlite3.Connection, *, run_hash: str, run: sqlite3.Row, stage: str
) -> bool:
    if (
        run["run_mode"] != "production"
        or run["status"] != "running"
        or stage not in _SOURCE_STAGE_MAP
    ):
        return False
    row = conn.execute(
        "\n        SELECT 1\n        FROM accounting_run_stages AS stage_state\n        WHERE stage_state.run_id_hash = ?\n          AND stage_state.stage_key = ?\n          AND stage_state.status = 'succeeded'\n          AND EXISTS (\n              SELECT 1\n              FROM accounting_run_items AS item\n              WHERE item.run_id_hash = stage_state.run_id_hash\n                AND item.source_stage = stage_state.stage_key\n                AND item.status NOT IN ('verified', 'not_applicable')\n          )\n        ",
        (run_hash, stage),
    ).fetchone()
    return row is not None


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _elapsed_seconds(
    started_at: Any, completed_at: Any, *, now: datetime
) -> int | None:
    if not started_at:
        return None
    started = _parse_utc_timestamp(str(started_at))
    finished = _parse_utc_timestamp(str(completed_at)) if completed_at else now
    return max(int((finished - started).total_seconds()), 0)


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _time_status(elapsed_seconds: int | None, target_seconds: int) -> str:
    if elapsed_seconds is None:
        return "not_started"
    return "within_target" if elapsed_seconds <= target_seconds else "target_exceeded"


def _next_action(
    status: str,
    current_stage: str | None,
    failed_stage: str | None,
    safe_to_retry: bool,
    remaining_count: int = 0,
) -> str:
    if status == "complete":
        return "none"
    if status == "uncertain_external_write":
        return f"read_back_{failed_stage or current_stage}"
    if status == "failed":
        prefix = "resume" if safe_to_retry else "manual_resolution"
        return f"{prefix}_{failed_stage or current_stage}"
    if current_stage and remaining_count > 0:
        return f"execute_remaining_{current_stage}"
    return f"execute_{current_stage}" if current_stage else "inspect_run"


def _validate_stage(stage: str) -> str:
    if stage not in RUN_STAGES:
        raise ValueError(f"unknown stage: {stage}")
    return stage


def _validate_principal(principal: str) -> str:
    principal = _require_text(principal, "principal").casefold()
    if principal != _mf_principal():
        raise ValueError("execution principal does not match the selected company")
    return principal


def _parse_hash(value: Any, field: str) -> str:
    match = _SHA256_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"{field} must be sha256:<64 hex>")
    return match.group(1).lower()


def _marked_hash(value: Any) -> str | None:
    return f"sha256:{value}" if value else None


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _validate_run_identity(row):
    if row["office_hash"] != stable_hash({"office": _office_name()}) or row[
        "execution_principal_hash"
    ] != stable_hash({"principal": _mf_principal()}):
        raise ValueError("run belongs to another company or execution principal")


def _validate_receipt_profile(receipt):
    profile = get_current_profile()
    if (
        profile is None
        or receipt.get("profile_slug") != profile.slug
        or receipt.get("boundary_fingerprint") != profile.boundary_fingerprint
    ):
        raise ValueError("receipt profile boundary fingerprint mismatch")
