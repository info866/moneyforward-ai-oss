"""OFFLINE_SYNTHETIC acceptance of bundled journal execution, not live MF E2E.

Starts from an explicit synthetic planned/authorized journal request. Does not
claim source intake, OCR/classification, masters sync, attachments, or DAILY close.
There are no patches to approval, identity, POST, readback, or validation code.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

# examples is checkout content, not an installed library or pytest fixture plugin.
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_accounting_fixtures", ROOT / "examples/accounting_fixtures.py")
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
prepare_scenario = fixtures.prepare_scenario
OFFICE_ID = fixtures.OFFICE_ID

from mf_phase12.accounting_api import WriteExecutionGrant
from mf_phase12.hash_utils import mask_identifier
from mf_phase12.journal_write_post import (
    JournalCreateDeliveryUncertainError,
    dry_run_journal_create_execution,
)


def assert_journal_facts(scenario, journal_id, multiline):
    """Independent literal oracle: real lines, net/tax/gross and account/tax IDs."""
    actual = scenario.client.journal(journal_id, office_id=OFFICE_ID)["journal"]
    assert actual["transaction_date"] == "2026-01-15"
    assert actual["journal_type"] == "journal_entry"
    expected = [
        ("Synthetic stationery", "synthetic-supplies", "synthetic-tax-10", 1000, 100, 1100),
    ]
    if multiline:
        expected.append(("Synthetic food", "synthetic-food", "synthetic-tax-8", 2000, 160, 2160))
    assert len(actual["branches"]) == len(expected)
    for branch, submitted, (remark, debit, tax_id, net, tax, gross) in zip(
        actual["branches"], scenario.journal["branches"], expected, strict=True,
    ):
        assert branch == {
            "remark": remark,
            "debitor": {"account_id": debit, "tax_id": tax_id, "value": net, "tax_value": tax},
            "creditor": {"account_id": "synthetic-bank", "tax_id": "synthetic-tax-exempt",
                         "value": gross, "tax_value": 0},
        }
        assert submitted == {
            "remark": remark,
            "debitor": {"account_id": debit, "tax_id": tax_id, "value": gross},
            "creditor": {"account_id": "synthetic-bank", "tax_id": "synthetic-tax-exempt", "value": gross},
        }
    assert sum(b["debitor"]["value"] + b["debitor"]["tax_value"] for b in actual["branches"]) == (
        3260 if multiline else 1100
    )
    assert sum(b["debitor"]["tax_value"] for b in actual["branches"]) == (260 if multiline else 100)


def assert_verification_record(scenario, *, passed):
    with scenario.db.connect() as conn:
        rows = conn.execute(
            "SELECT passed,expected_hash,actual_hash FROM post_registration_verifications "
            "WHERE idempotency_key=? ORDER BY id", (scenario.dry_run.idempotency_key,),
        ).fetchall()
    assert rows
    assert rows[-1]["passed"] == int(passed)
    assert (rows[-1]["expected_hash"] == rows[-1]["actual_hash"]) is passed


@pytest.mark.parametrize("multiline", [False, True], ids=["single-line", "mixed-tax-multi-line"])
def test_request_post_readback_restart_and_replay(tmp_path, multiline):
    scenario = prepare_scenario(tmp_path, multiline=multiline)
    with scenario.db.connect() as conn:
        source = conn.execute("SELECT * FROM processing_ledger").fetchone()
        intent = conn.execute("SELECT * FROM write_intents").fetchone()
        assert source["file_hash"] and source["google_drive_file_id_hash"]
        assert intent["ledger_id"] == source["id"]
        assert intent["source_file_id_hash"] == source["google_drive_file_id_hash"]
        assert intent["request_hash"] == scenario.dry_run.request_hash
        assert conn.execute("SELECT COUNT(*) FROM journal_create_execution_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM post_registration_verifications").fetchone()[0] == 0
    post = scenario.execute()
    assert post.executed and not post.already_executed
    assert scenario.states() == ("executed", "executed", "journal_created")
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
    old_client, old_db = scenario.client, scenario.db
    scenario.restart()  # all handles released, new instance after committed POST
    assert scenario.client is not old_client and scenario.db is not old_db
    assert scenario.client.office_identity_binding is None  # no cached authorization
    assert_journal_facts(scenario, post.moneyforward_journal_id, multiline)
    result = scenario.verify(post)
    assert result.verified and result.tax_readback_verified and result.tax_amounts_verified
    assert result.tax_method == "INDIVIDUAL_ALLOCATION"
    assert_verification_record(scenario, passed=True)
    # A journal GET alone cannot finish the normal source/voucher workflow.
    assert scenario.states() == ("verified", "verified", "journal_created")
    with scenario.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM voucher_attach_readback_verifications").fetchone()[0] == 0
    for _ in range(2):
        scenario.restart()
        before = list(scenario.provider.calls)
        replay = scenario.execute()
        assert replay.already_executed and not replay.executed and not replay.moneyforward_api_called
        assert replay.moneyforward_journal_id_hash == mask_identifier(post.moneyforward_journal_id)
        assert scenario.provider.calls == before
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
    with scenario.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM write_intents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM journal_create_execution_records").fetchone()[0] == 1
    assert ("GET", "/api/v3/term_settings") in scenario.provider.calls
    assert ("GET", "/api/v3/taxes") in scenario.provider.calls


@pytest.mark.parametrize("fault", ["amount", "tax-category", "account", "missing-line", "date"])
def test_wrong_official_readback_is_rejected(tmp_path, fault):
    scenario = prepare_scenario(tmp_path, multiline=True)
    post = scenario.execute()
    stored = scenario.provider.journals[post.moneyforward_journal_id]
    if fault == "amount":
        stored["branches"][1]["debitor"]["value"] += 1
    elif fault == "tax-category":
        stored["branches"][1]["debitor"]["tax_id"] = "synthetic-tax-10"
    elif fault == "account":
        stored["branches"][1]["debitor"]["account_id"] = "synthetic-wrong-account"
    elif fault == "missing-line":
        stored["branches"].pop()
    else:
        stored["transaction_date"] = "2026-01-16"
    scenario.restart()
    result = scenario.verify(post)
    assert not result.verified
    assert_verification_record(scenario, passed=False)
    assert scenario.states() == ("executed", "executed", "journal_created")
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1


@pytest.mark.parametrize("fault", ["missing-record", "revoked-record", "missing-execution-approval"])
def test_missing_or_revoked_approval_prevents_post(tmp_path, fault):
    scenario = prepare_scenario(tmp_path)
    if fault == "missing-execution-approval":
        scenario.execution_approval = {}
        with pytest.raises(ValueError, match="execution approval operation mismatch"):
            scenario.execute()
    else:
        with scenario.db.connect() as conn:
            if fault == "missing-record":
                conn.execute("DELETE FROM approval_records")
            else:
                conn.execute("UPDATE approval_records SET revoked_at='2026-01-15T00:00:00Z'")
        with pytest.raises(ValueError, match="approval record is (missing|revoked)"):
            scenario.execute()
    assert scenario.provider.mutations == scenario.provider.post_attempts == 0
    with scenario.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_create_execution_records").fetchone()[0] == 0


@pytest.mark.parametrize("multiline", [False, True])
def test_timeout_after_commit_restart_blind_retry_blocked_and_get_reconciliation(tmp_path, multiline):
    scenario = prepare_scenario(tmp_path, multiline=multiline, timeout_after_commit=True)
    with pytest.raises(TimeoutError, match="after committed journal"):
        scenario.execute()
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
    scenario.restart()
    before = list(scenario.provider.calls)
    with pytest.raises(JournalCreateDeliveryUncertainError, match="automatic re-POST is blocked"):
        scenario.execute()
    assert scenario.provider.calls == before
    # Synthetic operator discovers the committed ID through the client's GET.
    observed = scenario.client.journals(start_date="2026-01-15", end_date="2026-01-15")
    assert len(observed["data"]) == 1
    journal_id = observed["data"][0]["id"]
    assert_journal_facts(scenario, journal_id, multiline)
    original = deepcopy(scenario.provider.journals[journal_id])
    scenario.provider.journals[journal_id]["branches"][0]["debitor"]["value"] += 1
    assert not scenario.reconcile(journal_id).verified
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    assert_verification_record(scenario, passed=False)
    with pytest.raises(JournalCreateDeliveryUncertainError):
        scenario.execute()
    scenario.provider.journals[journal_id] = original
    scenario.restart()
    recovered = scenario.reconcile(journal_id)
    assert recovered.verified and recovered.tax_readback_verified
    assert_verification_record(scenario, passed=True)
    assert scenario.states() == ("verified", "verified", "verified")
    scenario.restart()
    assert scenario.execute().already_executed
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
    assert all(method == "GET" for method, _ in scenario.provider.calls[len(before):])


@pytest.mark.parametrize("fault", ["unbalanced", "negative", "unknown-field", "changed-approved-body"])
def test_unsafe_request_is_rejected_without_mutation(tmp_path, fault):
    scenario = prepare_scenario(tmp_path)
    bad = deepcopy(scenario.journal)
    if fault == "unbalanced":
        bad["branches"][0]["debitor"]["value"] += 1
    elif fault == "negative":
        bad["branches"][0]["debitor"]["value"] = -1
    elif fault == "unknown-field":
        bad["unapproved_field"] = "synthetic"
    else:
        bad["branches"][0]["remark"] = "Changed after approval"
    scenario.journal = bad
    with pytest.raises(ValueError):
        scenario.execute()
    assert scenario.provider.post_attempts == scenario.provider.mutations == 0


def test_wrong_office_and_read_only_client_preserve_guards(tmp_path):
    scenario = prepare_scenario(tmp_path)
    grant = WriteExecutionGrant("journal_create", scenario.dry_run.approval_hash,
                                scenario.dry_run.scope_hash, scenario.dry_run.target_hash)
    scenario.provider.office_name = "Synthetic Other Tenant"
    with pytest.raises(PermissionError, match="legal entity"):
        scenario.client.create_journal(journal=scenario.journal, office_id=OFFICE_ID, grant=grant)
    scenario.provider.office_name = fixtures.LEGAL_NAME
    scenario.client.write_enabled = False
    with pytest.raises(PermissionError, match="read-only"):
        scenario.client.create_journal(journal=scenario.journal, office_id=OFFICE_ID, grant=grant)
    assert scenario.provider.post_attempts == scenario.provider.mutations == 0


def test_request_hash_binding_rejects_substitution(tmp_path):
    scenario = prepare_scenario(tmp_path)
    substituted = deepcopy(scenario.journal)
    substituted["branches"][0]["debitor"]["tax_id"] = "synthetic-tax-8"
    with scenario.db.connect() as conn, pytest.raises(ValueError, match="request_hash does not match grant target"):
        dry_run_journal_create_execution(conn=conn, grant=scenario.grant, journal=substituted, office_id=OFFICE_ID)
    assert scenario.provider.post_attempts == 0


def test_core_defect_wrong_tax_amount_same_gross_must_be_rejected(tmp_path):
    scenario = prepare_scenario(tmp_path)
    post = scenario.execute()
    debit = scenario.provider.journals[post.moneyforward_journal_id]["branches"][0]["debitor"]
    debit.update(value=1001, tax_value=99)  # expected net=1000, tax=100, gross=1100
    result = scenario.verify(post)
    assert not result.verified


def test_demo_is_executable_without_pytest_and_prints_only_counts_flags(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/accounting_pipeline_demo.py")],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert not result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "evidence_level": "OFFLINE_SYNTHETIC", "verified_journals": 3,
        "synthetic_post_attempts": 3, "synthetic_provider_mutations": 3,
        "replay_extra_mutations": 0, "blind_retry_blocked": True,
        "uncertain_reconciled": True, "resumed_new_instances": True,
        "live_api_exercised": False, "accounting_workflow_completed": False,
        "production_ready": False,
    }


def test_nonzero_tax_without_independent_source_witness_is_not_verified(tmp_path):
    from mf_phase12.journal_write_verify import verify_journal_create_read_back_with_connection_factory
    scenario = prepare_scenario(tmp_path)
    post = scenario.execute()
    result = verify_journal_create_read_back_with_connection_factory(
        scenario.db.connect, client=scenario.client, journal_id=post.moneyforward_journal_id,
        journal=scenario.journal, post_result=post.to_redacted_dict(), office_id=OFFICE_ID)
    assert not result.verified
    assert not result.tax_amounts_verified
    assert scenario.states() == ("executed", "executed", "journal_created")


@pytest.mark.parametrize("values", [[], [{"debitor": 100}],
    [{"debitor": True, "creditor": 0}], [{"debitor": "NaN", "creditor": 0}],
    [{"debitor": 99, "creditor": 0}], [{"debitor": "Infinity", "creditor": 0}]])
def test_invalid_source_tax_witness_never_verifies(tmp_path, values):
    from mf_phase12.journal_write_verify import verify_journal_create_read_back_with_connection_factory
    scenario = prepare_scenario(tmp_path)
    post = scenario.execute()
    result = verify_journal_create_read_back_with_connection_factory(
        scenario.db.connect, client=scenario.client, journal_id=post.moneyforward_journal_id,
        journal=scenario.journal, post_result=post.to_redacted_dict(), office_id=OFFICE_ID,
        expected_tax_values=values)
    assert not result.verified and not result.tax_amounts_verified
    assert scenario.provider.mutations == 1


def test_wrong_tax_split_cannot_clear_uncertain_delivery(tmp_path):
    scenario = prepare_scenario(tmp_path, timeout_after_commit=True)
    with pytest.raises(TimeoutError):
        scenario.execute()
    scenario.provider.journals["synthetic-journal-1"]["branches"][0]["debitor"].update(
        value=1001, tax_value=99)
    assert not scenario.reconcile("synthetic-journal-1").verified
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    with pytest.raises(JournalCreateDeliveryUncertainError):
        scenario.execute()
    assert scenario.provider.mutations == 1


def test_uncertain_unknown_journal_get_must_stay_blocked_after_restart(tmp_path):
    """A nonexistent official journal ID never permits another POST."""
    scenario = prepare_scenario(tmp_path, timeout_after_commit=True)
    with pytest.raises(TimeoutError):
        scenario.execute()
    scenario.restart()
    # The caller has no independently matched journal ID; a guessed ID is invalid.
    with pytest.raises(KeyError):
        scenario.reconcile("synthetic-journal-not-found")
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    scenario.restart()
    with pytest.raises(JournalCreateDeliveryUncertainError):
        scenario.execute()
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1


def test_uncertain_changed_approved_request_fails_before_get(tmp_path):
    """Recovered GET must be bound to the exact request hash of the original claim."""
    scenario = prepare_scenario(tmp_path, timeout_after_commit=True)
    with pytest.raises(TimeoutError):
        scenario.execute()
    scenario.restart()
    altered = deepcopy(scenario.journal)
    altered["branches"][0]["remark"] = "Synthetic changed after approval"
    scenario.journal = altered
    before = len(scenario.provider.calls)
    with pytest.raises(ValueError, match="request hash mismatch"):
        scenario.reconcile("synthetic-journal-1")
    assert len(scenario.provider.calls) == before
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    assert scenario.provider.mutations == 1


def test_uncertain_wrong_office_cannot_be_reconciled(tmp_path):
    """A stale/mismatched office fails without marking the write VERIFIED."""
    scenario = prepare_scenario(tmp_path, timeout_after_commit=True)
    with pytest.raises(TimeoutError):
        scenario.execute()
    scenario.restart()
    scenario.provider.office_name = "Synthetic Different Office"
    with pytest.raises(PermissionError, match="legal entity"):
        scenario.reconcile("synthetic-journal-1")
    assert scenario.states() == ("planned", "delivery_uncertain", "planned")
    assert scenario.provider.mutations == 1


def test_concurrent_replay_during_inflight_post_only_one_mutation(tmp_path):
    """A second worker must not POST while the first worker holds the same claim."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    scenario = prepare_scenario(tmp_path)
    entered = Event()
    resume = Event()
    original_request = scenario.provider.request

    def paused_request(method, url, *, headers=None, body=None, timeout=30.0):
        if method == "POST" and url.endswith("/api/v3/journals"):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("synthetic worker was not released")
        return original_request(method, url, headers=headers, body=body, timeout=timeout)

    scenario.provider.request = paused_request
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scenario.execute)
        assert entered.wait(5), "first worker never reached synthetic POST"
        try:
            with pytest.raises(JournalCreateDeliveryUncertainError):
                pool.submit(scenario.execute).result(timeout=5)
        finally:
            resume.set()
        initial = first.result(timeout=5)
    assert initial.executed
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
    scenario.restart()
    replay = scenario.execute()
    assert replay.already_executed and not replay.moneyforward_api_called
    assert scenario.provider.post_attempts == scenario.provider.mutations == 1
