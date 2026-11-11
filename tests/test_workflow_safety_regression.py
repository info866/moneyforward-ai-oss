"""Ledger-level safety regression with independent, entirely synthetic fixtures.

Production-mode cases exercise durable-receipt enforcement in an in-memory
SQLite ledger. They do not invoke provider adapters, credentials, or real DBs.
"""

from pathlib import Path
import sqlite3

import pytest

from mf_phase12 import shiwake_run as workflow
from mf_phase12.hash_utils import accounting_run_hash, stable_hash
from mf_phase12.office_profile import OfficeProfile, using_office_profile


DAY = "2026-02-17"
CHECKS = (
    "duplicate_journal_check",
    "debit_credit_amount_check",
    "account_sub_account_tax_check",
    "source_statement_evidence_reconciliation",
    "processed_folder_readback",
)


def digest(value):
    return "sha256:" + stable_hash(value)


@pytest.fixture
def ledger(tmp_path):
    profile = OfficeProfile(
        schema_version=2,
        configured=True,
        slug="synthetic",
        legal_name="Synthetic Ledger Company",
        display_name="Synthetic Ledger",
        expected_office_names=("Synthetic Ledger Company",),
        google_account="ledger@synthetic.invalid",
        google_drive_account="shared@synthetic.invalid",
        moneyforward_keychain_service="synthetic-ledger-credential",
        moneyforward_db_path=tmp_path / "data/phase12/offices/synthetic/ledger.sqlite",
        google_drive_evidence_root_id="synthetic_root",
        google_drive_evidence_source_folder_ids=("synthetic_root",),
        google_drive_processed_folder_id="synthetic_processed",
        repository_root=tmp_path,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = Path(__file__).resolve().parents[1] / "sql/phase12_schema.sql"
    conn.executescript(schema.read_text(encoding="utf-8"))
    with using_office_profile(profile):
        yield conn, profile
    conn.close()
    assert not profile.moneyforward_db_path.exists()


def start(conn, run_id="synthetic-run", mode="production"):
    return workflow.start_run(conn, run_id, accounting_date=DAY, run_mode=mode)


def evidence(stage, **updates):
    result = {
        "status": "verified",
        "evidence_hash": digest({"stage": stage}),
        "coverage_state": "zero_confirmed",
        "all_items_resolved": True,
        "remaining_count": 0,
    }
    if stage == "all_receipt_folders":
        result.update(folder_count=1, manifest_folder_count=1, all_folders_covered=True)
    if stage == "cross_audit":
        result["audit_checks"] = dict.fromkeys(CHECKS, "verified")
    result.update(updates)
    return result


def receipt(profile, stage="sales_invoice", run_id="synthetic-run", **updates):
    principal = (
        profile.google_drive_account
        if stage in {"payable_invoice", "all_receipt_folders"}
        else profile.google_account
    )
    result = {
        **evidence(stage),
        "stage": stage,
        "profile_slug": profile.slug,
        "boundary_fingerprint": profile.boundary_fingerprint,
        "accounting_run_id_hash": "sha256:" + accounting_run_hash(run_id),
        "accounting_date": DAY,
        "identity_readback_verified": True,
        "office_hash": digest({"office": profile.legal_name}),
        "execution_principal_hash": digest({"principal": principal}),
        "source_snapshot_hash": digest({"snapshot": stage}),
        "readback_hash": digest({"readback": stage}),
    }
    result.update(updates)
    return result


def stage_rows(conn, run_id="synthetic-run"):
    return {
        row["stage_key"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM accounting_run_stages WHERE run_id_hash = ?",
            (accounting_run_hash(run_id),),
        )
    }


def test_same_run_start_and_resume_preserve_successful_stages(ledger):
    conn, _ = ledger
    start(conn, mode="synthetic")
    for stage in workflow.RUN_STAGES[:4]:
        workflow.advance_run(conn, "synthetic-run", stage, evidence(stage))
    before = stage_rows(conn)
    transitions = conn.execute(
        "SELECT COUNT(*) FROM accounting_run_transitions"
    ).fetchone()[0]
    start(conn, mode="synthetic")
    assert stage_rows(conn) == before
    assert (
        conn.execute("SELECT COUNT(*) FROM accounting_run_transitions").fetchone()[0]
        == transitions
    )
    workflow.fail_run_stage(conn, "synthetic-run", "bank_transfer", "synthetic timeout")
    resumed = workflow.resume_run(conn, "synthetic-run")
    assert resumed["current_stage"] == "bank_transfer"
    after = stage_rows(conn)
    for stage in workflow.RUN_STAGES[:4]:
        assert after[stage] == before[stage]
        assert after[stage]["attempt_count"] == 1
    with pytest.raises(ValueError, match="already succeeded"):
        workflow.advance_run(
            conn, "synthetic-run", "sales_invoice", evidence("sales_invoice")
        )
    with pytest.raises(ValueError, match="immutable run inputs"):
        workflow.start_run(
            conn, "synthetic-run", accounting_date="2026-02-18", run_mode="synthetic"
        )


def test_deferred_replay_requires_existing_item_and_fresh_same_run_receipt(ledger):
    conn, profile = ledger
    start(conn)
    stage = "sales_invoice"
    pending = receipt(
        profile, remaining_count=1, all_items_resolved=False, coverage_state="captured"
    )
    old_receipt = workflow.record_source_stage_receipt(
        conn, run_id="synthetic-run", stage=stage, receipt=pending
    )
    workflow.advance_run(
        conn,
        "synthetic-run",
        stage,
        evidence(
            stage,
            adapter_receipt_hash=old_receipt,
            remaining_count=1,
            all_items_resolved=False,
            coverage_state="captured",
            items=[{"idempotency_key": "synthetic-event-1", "status": "planned"}],
        ),
    )
    original = dict(conn.execute("SELECT * FROM accounting_run_items").fetchone())
    resolved = evidence(
        stage,
        adapter_receipt_hash=old_receipt,
        items=[
            {
                "existing_item_hash": "sha256:" + original["item_hash"],
                "status": "verified",
            }
        ],
    )
    with pytest.raises(ValueError, match="fresh authoritative receipt"):
        workflow.advance_run(conn, "synthetic-run", stage, resolved)
    fresh = workflow.record_source_stage_receipt(
        conn,
        run_id="synthetic-run",
        stage=stage,
        receipt=receipt(profile, readback_hash=digest("resolved-event-1")),
    )
    resolved["adapter_receipt_hash"] = fresh
    with pytest.raises(ValueError, match="existing unresolved item"):
        workflow.advance_run(
            conn,
            "synthetic-run",
            stage,
            {
                **resolved,
                "items": [
                    {"existing_item_hash": digest("foreign-item"), "status": "verified"}
                ],
            },
        )
    assert (
        dict(conn.execute("SELECT * FROM accounting_run_items").fetchone()) == original
    )
    workflow.advance_run(conn, "synthetic-run", stage, resolved)
    rows = conn.execute("SELECT * FROM accounting_run_items").fetchall()
    assert len(rows) == 1
    assert rows[0]["item_hash"] == original["item_hash"]
    assert rows[0]["idempotency_key_hash"] == original["idempotency_key_hash"]
    assert rows[0]["status"] == "verified"
    with pytest.raises(ValueError, match="already succeeded"):
        workflow.advance_run(conn, "synthetic-run", stage, resolved)


@pytest.mark.parametrize("outcome", ["not_applied", "blocked"])
def test_uncertain_write_cannot_resume_without_durable_readback(ledger, outcome):
    conn, profile = ledger
    start(conn)
    stage = "sales_invoice"
    failed = workflow.fail_run_stage(
        conn,
        "synthetic-run",
        stage,
        "synthetic transport interrupted",
        uncertain_external_write=True,
    )
    assert failed["status"] == "uncertain_external_write"
    with pytest.raises(ValueError, match="read-back"):
        workflow.resume_run(conn, "synthetic-run")
    with pytest.raises(ValueError, match="readback_receipt_hash"):
        workflow.resolve_uncertain_write(
            conn, "synthetic-run", stage, outcome=outcome, readback_evidence={}
        )
    with pytest.raises(ValueError, match="receipt was not found"):
        workflow.resolve_uncertain_write(
            conn,
            "synthetic-run",
            stage,
            outcome=outcome,
            readback_evidence={"readback_receipt_hash": digest("invented")},
        )
    readback = receipt(
        profile,
        outcome=outcome,
        readback_performed=True,
        external_write_absence_verified=outcome == "not_applied",
        manual_resolution_required=outcome == "blocked",
        blocker_hash=digest("manual-review"),
    )
    required_flag = (
        "external_write_absence_verified"
        if outcome == "not_applied"
        else "manual_resolution_required"
    )
    with pytest.raises(ValueError, match="requires"):
        workflow.record_uncertain_write_readback(
            conn,
            run_id="synthetic-run",
            stage=stage,
            outcome=outcome,
            receipt={**readback, required_flag: False},
        )
    proof = workflow.record_uncertain_write_readback(
        conn,
        run_id="synthetic-run",
        stage=stage,
        outcome=outcome,
        receipt=readback,
    )
    result = workflow.resolve_uncertain_write(
        conn,
        "synthetic-run",
        stage,
        outcome=outcome,
        readback_evidence={"readback_receipt_hash": proof},
    )
    assert result["status"] == "failed"
    if outcome == "not_applied":
        assert workflow.resume_run(conn, "synthetic-run")["current_stage"] == stage
    else:
        with pytest.raises(ValueError, match="blocked"):
            workflow.resume_run(conn, "synthetic-run")


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_slug", "other"),
        ("boundary_fingerprint", "0" * 64),
        ("office_hash", digest({"office": "Other Synthetic Company"})),
        ("execution_principal_hash", digest({"principal": "other@synthetic.invalid"})),
        ("accounting_run_id_hash", digest({"accounting_run_id": "other-run"})),
        ("accounting_date", "2026-02-18"),
        ("stage", "bank_transfer"),
        ("source_snapshot_hash", "not-a-hash"),
        ("readback_hash", "not-a-hash"),
        ("identity_readback_verified", False),
    ],
)
def test_source_receipt_rejects_mismatched_tenant_run_and_hash(ledger, field, value):
    conn, profile = ledger
    start(conn)
    with pytest.raises(ValueError):
        workflow.record_source_stage_receipt(
            conn,
            run_id="synthetic-run",
            stage="sales_invoice",
            receipt=receipt(profile, **{field: value}),
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM accounting_run_receipts").fetchone()[0] == 0
    )
    assert stage_rows(conn)["sales_invoice"]["status"] == "pending"


def test_shared_drive_principal_receipt_is_company_bound_and_run_specific(ledger):
    conn, profile = ledger
    start(conn)
    stage = "payable_invoice"
    source = receipt(profile, stage)
    with pytest.raises(ValueError, match="principal"):
        workflow.record_source_stage_receipt(
            conn,
            run_id="synthetic-run",
            stage=stage,
            receipt={
                **source,
                "execution_principal_hash": digest(
                    {"principal": profile.google_account}
                ),
            },
        )
    proof = workflow.record_source_stage_receipt(
        conn, run_id="synthetic-run", stage=stage, receipt=source
    )
    assert (
        workflow.record_source_stage_receipt(
            conn, run_id="synthetic-run", stage=stage, receipt=source
        )
        == proof
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM accounting_run_receipts").fetchone()[0] == 1
    )
    start(conn, run_id="second-run")
    with pytest.raises(ValueError, match="receipt was not found"):
        workflow.advance_run(
            conn, "second-run", stage, evidence(stage, adapter_receipt_hash=proof)
        )
    assert stage_rows(conn, "second-run")[stage]["status"] == "pending"
    workflow.advance_run(
        conn, "synthetic-run", stage, evidence(stage, adapter_receipt_hash=proof)
    )
    assert stage_rows(conn)[stage]["status"] == "succeeded"


@pytest.mark.parametrize("check", CHECKS)
def test_cross_audit_requires_every_check_and_authoritative_receipt(ledger, check):
    conn, profile = ledger
    start(conn)
    for source in workflow.ACTIVE_ACCOUNTING_WORKFLOWS:
        source_proof = workflow.record_source_stage_receipt(
            conn,
            run_id="synthetic-run",
            stage=source,
            receipt=receipt(profile, source),
        )
        workflow.advance_run(
            conn,
            "synthetic-run",
            source,
            evidence(source, adapter_receipt_hash=source_proof),
        )
    stage = "cross_audit"
    valid = receipt(
        profile,
        stage,
        completion_notification_allowed=True,
        daily_control_hash=digest("daily-control"),
        post_audit_hash=digest("post-audit"),
    )
    invalid = {**valid, "audit_checks": {**valid["audit_checks"], check: "pending"}}
    with pytest.raises(ValueError, match="all five"):
        workflow.record_cross_audit_receipt(
            conn, run_id="synthetic-run", receipt=invalid
        )
    with pytest.raises(ValueError, match="completion notification"):
        workflow.record_cross_audit_receipt(
            conn,
            run_id="synthetic-run",
            receipt={**valid, "completion_notification_allowed": False},
        )
    with pytest.raises(ValueError, match="receipt was not found"):
        workflow.advance_run(
            conn,
            "synthetic-run",
            stage,
            evidence(stage, adapter_receipt_hash=digest("invented")),
        )
    assert stage_rows(conn)[stage]["status"] == "pending"
    proof = workflow.record_cross_audit_receipt(
        conn, run_id="synthetic-run", receipt=valid
    )
    with pytest.raises(ValueError, match="all five"):
        workflow.advance_run(
            conn,
            "synthetic-run",
            stage,
            evidence(
                stage,
                adapter_receipt_hash=proof,
                audit_checks=invalid["audit_checks"],
            ),
        )
    workflow.advance_run(
        conn, "synthetic-run", stage, evidence(stage, adapter_receipt_hash=proof)
    )
    assert stage_rows(conn)[stage]["status"] == "succeeded"
    assert (
        conn.execute("SELECT status FROM accounting_runs").fetchone()[0] != "complete"
    )
