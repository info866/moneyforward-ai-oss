"""Offline canonical runtime and third-company onboarding regression tests."""

from argparse import Namespace
from dataclasses import replace
from datetime import date
import json
from types import SimpleNamespace

import pytest

from mf_phase12 import cli, shiwake_run
from mf_phase12.db import Phase12Database
from mf_phase12.hash_utils import stable_hash, accounting_run_hash
from mf_phase12.office_profile import (
    OfficeProfile,
    OfficeIdentityBinding,
    using_office_profile,
    normalize_office_name,
    require_database_office_identity,
)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    value = OfficeProfile(
        schema_version=2,
        slug="alpha",
        legal_name="Tenant Alpha",
        display_name="Alpha",
        expected_office_names=("Tenant Alpha",),
        google_account="alpha@tenant.test",
        google_drive_account="shared@storage.test",
        moneyforward_keychain_service="mf-alpha",
        moneyforward_db_path=tmp_path / "data/phase12/offices/alpha/ledger.sqlite",
        google_drive_evidence_root_id="alpha_root",
        google_drive_evidence_source_folder_ids=("alpha_root", "alpha_receipts"),
        google_drive_processed_folder_id="alpha_processed",
        repository_root=tmp_path,
        configured=True,
        accounting_rules={
            "identity": {"moneyforward_principal": "alpha@tenant.test"},
            "fiscal_year": {"start_month": 4, "start_day": 1},
        },
    )
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "mf_phase12.office_profile.load_office_profile", lambda *a, **k: value
    )
    return value


def binding(profile):
    return OfficeIdentityBinding(
        profile_slug=profile.slug,
        profile_hash=profile.profile_hash,
        office_id="alpha-office",
        office_id_hash=stable_hash("alpha-office"),
        legal_name_hash=stable_hash(normalize_office_name(profile.legal_name)),
        keychain_service_hash=stable_hash(profile.moneyforward_keychain_service),
        boundary_fingerprint=profile.boundary_fingerprint,
    )


def setup(profile, monkeypatch):
    calls = []

    def discover():
        calls.append("official_read")
        return binding(profile)

    monkeypatch.setattr(
        cli,
        "_read_only_profile_client",
        lambda: SimpleNamespace(validate_office_identity=discover),
    )
    assert cli.main(["--office-profile", "alpha.json", "setup", "--execute"]) == 0
    return calls


def test_default_execute_is_no_io_dry_run(profile, capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_bind_production_runtime_for_run",
        lambda *a: pytest.fail("unexpected authentication"),
    )
    assert (
        cli.main(["--office-profile", "alpha.json", "run", "execute", "--run-id", "r1"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dry_run"
    assert len(result["active_workflows"]) == 6
    assert result["database_write_called"] is False
    assert not profile.moneyforward_db_path.exists()


def test_third_company_setup_binds_only_after_discovery(profile, monkeypatch):
    assert setup(profile, monkeypatch) == ["official_read"]
    with (
        using_office_profile(profile),
        Phase12Database(profile.moneyforward_db_path).connect() as conn,
    ):
        assert require_database_office_identity(
            conn, profile
        ).office_id_hash == stable_hash("alpha-office")
    assert cli._PRODUCTION_WRITES.get() is False


@pytest.mark.parametrize("case", ["matching", "foreign", "missing_configuration"])
def test_issue_readback_uses_only_current_company_repository(profile, monkeypatch, case):
    from mf_phase12.github_issue_readback import accounting_run_marker, record_github_issue_readback
    from mf_phase12.hash_utils import sha256_text

    rules = dict(profile.accounting_rules)
    if case != "missing_configuration":
        rules["reporting"] = {"github_repository": "tenant-alpha/accounting"}
    profile = replace(profile, accounting_rules=rules)
    monkeypatch.setattr("mf_phase12.office_profile.load_office_profile", lambda *a, **k: profile)
    setup(profile, monkeypatch)
    body = accounting_run_marker("issue-run")
    repo = "tenant-beta/accounting" if case == "foreign" else "tenant-alpha/accounting"
    response = {"id": 42, "body": body, "created_at": "2026-04-01T00:00:00Z",
        "issue_url": f"https://api.github.com/repos/{repo}/issues/1",
        "url": f"https://api.github.com/repos/{repo}/issues/comments/42",
        "html_url": f"https://github.com/{repo}/issues/1#issuecomment-42"}
    with using_office_profile(profile), Phase12Database(profile.moneyforward_db_path).connect() as conn:
        shiwake_run.start_run(conn, "issue-run", accounting_date="2026-04-01")
        if case == "matching":
            assert record_github_issue_readback(conn, run_id="issue-run", issue_number=1,
                expected_body_hash=sha256_text(body), response=response)
        else:
            with pytest.raises(ValueError):
                record_github_issue_readback(conn, run_id="issue-run", issue_number=1,
                    expected_body_hash=sha256_text(body), response=response)
            assert conn.execute("SELECT COUNT(*) FROM accounting_run_receipts").fetchone()[0] == 0


def test_failed_identity_discovery_creates_no_db(profile, monkeypatch):
    def reject():
        raise PermissionError("wrong office")

    monkeypatch.setattr(
        cli,
        "_read_only_profile_client",
        lambda: SimpleNamespace(validate_office_identity=reject),
    )
    assert cli.main(["--office-profile", "alpha.json", "setup", "--execute"]) == 2
    assert not profile.moneyforward_db_path.exists()


def test_existing_setup_does_not_initialize_again(profile, monkeypatch):
    setup(profile, monkeypatch)
    monkeypatch.setattr(
        Phase12Database,
        "initialize",
        lambda self: pytest.fail("existing DB initialized"),
    )
    assert cli.main(["--office-profile", "alpha.json", "setup", "--execute"]) == 0


def test_connect_never_creates_db(profile, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_read_only_profile_client",
        lambda: SimpleNamespace(validate_office_identity=lambda: binding(profile)),
    )
    assert cli.main(["--office-profile", "alpha.json", "connect"]) == 0
    assert not profile.moneyforward_db_path.exists()


@pytest.mark.parametrize("command", ["prepare", "replay"])
def test_local_commands_dispatch_without_production_intent(
    profile, monkeypatch, command
):
    setup(profile, monkeypatch)
    calls = []

    def handler(args):
        calls.append(args.command)
        assert not cli._PRODUCTION_WRITES.get()
        return 0

    args = Namespace(
        office_profile="alpha.json",
        command=command,
        db_path=profile.moneyforward_db_path,
    )
    assert cli._invoke_profile_handler(args, handler) == 0
    assert calls == [command]


def test_rag_build_calls_company_pipeline_with_read_only_client(profile, monkeypatch):
    calls = []
    client = object()
    monkeypatch.setattr(cli, "_read_only_profile_client", lambda: client)

    def build(**kwargs):
        calls.append(kwargs)
        assert cli._PRODUCTION_WRITES.get() is False
        return SimpleNamespace(to_dict=lambda: {"status": "ready"})

    monkeypatch.setattr("mf_phase12.office_rag_pipeline.build_office_rag", build)
    assert (
        cli.main(
            [
                "--office-profile",
                "alpha.json",
                "rag-build",
                "--execute",
                "--start-date",
                "2026-01-01",
                "--end-date",
                "2026-02-01",
            ]
        )
        == 0
    )
    assert calls[0]["db_path"] == profile.moneyforward_db_path
    assert calls[0]["client"] is client


@pytest.mark.parametrize(
    "workflow,function",
    [
        ("sales_invoice", "_execute_sales_invoice_stage"),
        ("payable_invoice", "_execute_payable_invoice_stage"),
        ("all_receipt_folders", "_execute_all_receipt_folders_stage"),
        ("payroll_bonus", "_execute_payroll_bonus_stage"),
        ("bank_transfer", "_execute_bank_transfer_workflow"),
        ("card", "_execute_card_workflow"),
    ],
)
def test_all_six_canonical_dispatch_paths_retained(monkeypatch, workflow, function):
    calls = []
    monkeypatch.setattr(
        cli, function, lambda *a, **k: calls.append(k) or {"status": "verified"}
    )
    with pytest.raises(PermissionError):
        cli._execute_aggregate_workflow(workflow, object(), Namespace(), runtime=())
    token = cli._PRODUCTION_WRITES.set(True)
    try:
        assert (
            cli._execute_aggregate_workflow(
                workflow, object(), Namespace(), runtime=()
            )["status"]
            == "verified"
        )
    finally:
        cli._PRODUCTION_WRITES.reset(token)
    assert len(calls) == 1


def test_profile_switch_changes_identity_and_fiscal_year(profile):
    second = replace(
        profile,
        slug="beta",
        legal_name="Tenant Beta",
        google_account="beta@tenant.test",
        expected_office_names=("Tenant Beta",),
        moneyforward_db_path=profile.repository_root
        / "data/phase12/offices/beta/ledger.sqlite",
        accounting_rules={"identity": {"moneyforward_principal": "beta@tenant.test"}},
    )
    with using_office_profile(profile):
        assert shiwake_run._mf_principal() == "alpha@tenant.test"
        assert cli._company_fiscal_year_start(date(2026, 3, 31)) == date(2025, 4, 1)
        with using_office_profile(second):
            assert shiwake_run._mf_principal() == "beta@tenant.test"
            with pytest.raises(ValueError, match="not configured"):
                cli._company_fiscal_year_start(date(2026, 3, 31))
        assert shiwake_run._office_name() == "Tenant Alpha"


def test_aggregate_artifact_rejects_changed_route_and_corruption(profile, tmp_path):
    path = profile.moneyforward_db_path.parent / "runs/r1/aggregate.local.json"
    with using_office_profile(profile):
        cli._write_aggregate_run_artifact(
            path, run_id="r1", accounting_date="2026-04-01", workflows=[]
        )
        assert (
            cli._load_aggregate_run_artifact(path)["boundary_fingerprint"]
            == profile.boundary_fingerprint
        )
    with using_office_profile(
        replace(profile, google_drive_processed_folder_id="changed_output")
    ):
        with pytest.raises(ValueError, match="another profile"):
            cli._load_aggregate_run_artifact(path)
    path.write_text("{broken", encoding="utf-8")
    with (
        using_office_profile(profile),
        pytest.raises(ValueError, match="refusing fresh"),
    ):
        cli._load_aggregate_run_artifact(path)


def test_run_ledger_rejects_foreign_principal_on_resume(profile, monkeypatch):
    setup(profile, monkeypatch)
    with (
        using_office_profile(profile),
        Phase12Database(profile.moneyforward_db_path).connect() as conn,
    ):
        shiwake_run.start_run(conn, "r1", accounting_date="2026-04-01")
        shiwake_run.fail_run_stage(
            conn, "r1", "sales_invoice", "retry", safe_to_retry=True
        )
        altered = replace(
            profile,
            google_account="other@tenant.test",
            accounting_rules={
                "identity": {"moneyforward_principal": "other@tenant.test"}
            },
        )
        with (
            using_office_profile(altered),
            pytest.raises(ValueError, match="another company"),
        ):
            shiwake_run.resume_run(conn, "r1")


def test_cross_audit_requires_all_five_checks_and_profile_fingerprint(
    profile, monkeypatch
):
    setup(profile, monkeypatch)
    digest = "sha256:" + "a" * 64
    with (
        using_office_profile(profile),
        Phase12Database(profile.moneyforward_db_path).connect() as conn,
    ):
        shiwake_run.start_run(conn, "r1", accounting_date="2026-04-01")
        receipt = cli._bound_receipt(
            {
                "status": "verified",
                "stage": "cross_audit",
                "accounting_run_id_hash": accounting_run_hash("r1"),
                "accounting_date": "2026-04-01",
                "source_snapshot_hash": digest,
                "readback_hash": digest,
                "remaining_count": 0,
                "all_items_resolved": True,
                "coverage_state": "captured",
                "identity_readback_verified": True,
                "office_hash": stable_hash({"office": profile.legal_name}),
                "execution_principal_hash": stable_hash(
                    {"principal": "alpha@tenant.test"}
                ),
                "daily_control_hash": digest,
                "post_audit_hash": digest,
                "completion_notification_allowed": True,
            }
        )
        with pytest.raises(ValueError, match="five"):
            shiwake_run.record_cross_audit_receipt(conn, run_id="r1", receipt=receipt)
        receipt["audit_checks"] = {
            key: "verified"
            for key in (
                "duplicate_journal_check",
                "debit_credit_amount_check",
                "account_sub_account_tax_check",
                "source_statement_evidence_reconciliation",
                "processed_folder_readback",
            )
        }
        receipt["boundary_fingerprint"] = "b" * 64
        with pytest.raises(ValueError, match="fingerprint"):
            shiwake_run.record_cross_audit_receipt(conn, run_id="r1", receipt=receipt)
        receipt["boundary_fingerprint"] = profile.boundary_fingerprint
        first = shiwake_run.record_cross_audit_receipt(
            conn, run_id="r1", receipt=receipt
        )
        assert (
            shiwake_run.record_cross_audit_receipt(conn, run_id="r1", receipt=receipt)
            == first
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM accounting_run_receipts").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("state", ["VERIFIED", "REVIEW", "NOT_READY", "NOT_APPLICABLE"])
def test_terminal_items_are_skipped_on_replay(state):
    assert cli._aggregate_result_resume_complete({"state": state, "remaining_count": 0})
    assert not cli._aggregate_result_resume_complete(
        {"state": state, "remaining_count": 1}
    )


def test_wrong_drive_source_rejected_before_business_io(profile):
    with using_office_profile(profile), pytest.raises(ValueError, match="Drive source"):
        cli._validate_drive_route("beta_receipts")


def test_google_binding_has_no_implicit_global_credential_fallback(profile):
    with (
        using_office_profile(profile),
        pytest.raises(ValueError, match="not configured"),
    ):
        cli._bind_profile_google_runtime()


def test_payable_document_entry_gate_preserves_future_invoice_not_ready(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="請求書 Invoice\n請求日 2026-12-01\n合計金額 1000 円\n"
        ),
    )
    result = cli._payable_native_text_entry_gate(
        file_bytes=b"synthetic pdf",
        mime_type="application/pdf",
        accounting_date="2026-04-01",
    )
    assert result["status"] == "not_ready"


def test_principal_has_one_canonical_source(profile):
    single = replace(profile, accounting_rules={})
    with using_office_profile(single):
        assert shiwake_run._mf_principal() == single.google_account
    conflict = replace(
        profile,
        accounting_rules={"identity": {"moneyforward_principal": "other@tenant.test"}},
    )
    with using_office_profile(conflict), pytest.raises(ValueError, match="conflicts"):
        shiwake_run._mf_principal()


@pytest.mark.parametrize("wrong_boundary", [False, True])
def test_gas_passes_boundary_and_requires_matching_response(
    profile, tmp_path, monkeypatch, wrong_boundary
):
    config = tmp_path / "gas-config.json"
    config.write_text(json.dumps({"apiExecutableDeploymentId": "synthetic-deployment"}))
    selected = replace(profile, accounting_rules={"gas": {"config_path": str(config)}})
    requests = []

    class Transport:
        def request(self, method, url, **kwargs):
            requests.append(json.loads(kwargs["body"]))
            boundary = cli._profile_boundary()
            if wrong_boundary:
                boundary["boundary_fingerprint"] = "wrong"
            return (
                200,
                {},
                json.dumps(
                    {"response": {"result": {**boundary, "status": "verified"}}}
                ).encode(),
            )

    monkeypatch.setattr(cli, "_validate_runtime_database", lambda *a: None)
    with using_office_profile(selected):
        token = cli._PRODUCTION_WRITES.set(True)
        try:
            if wrong_boundary:
                with pytest.raises(ValueError, match="another profile"):
                    cli._run_shared_gas_function(
                        "classifySharedAccountingCandidates",
                        [[{"fact": "synthetic"}]],
                        "synthetic",
                        transport=Transport(),
                    )
            else:
                assert (
                    cli._run_shared_gas_function(
                        "classifySharedAccountingCandidates",
                        [[]],
                        "synthetic",
                        transport=Transport(),
                    )["status"]
                    == "verified"
                )
        finally:
            cli._PRODUCTION_WRITES.reset(token)
    assert (
        requests[0]["parameters"][-1]["boundary_fingerprint"]
        == selected.boundary_fingerprint
    )


def test_chat_only_resume_reuses_cross_audit_and_never_dispatches_workflows(
    profile, monkeypatch
):
    workflows = [
        {
            "workflow": name,
            "state": "VERIFIED",
            "remaining_count": 0,
            "resume_complete": True,
            "official_readback_complete": True,
        }
        for name in cli.ACTIVE_ACCOUNTING_WORKFLOWS
    ]
    audit = {
        "status": "verified",
        "receipt_hash": "sha256:" + "a" * 64,
        "terminal_snapshot_hash": cli._aggregate_terminal_snapshot_hash(workflows),
    }
    args = Namespace(run_id="chat-resume", accounting_date="2026-04-01")
    path = profile.moneyforward_db_path.parent / "runs/chat/aggregate.local.json"
    deliveries = []

    def delivery(*a, **k):
        deliveries.append(1)
        if len(deliveries) == 1:
            raise TimeoutError("synthetic notification interruption")
        return {"status": "sent", "cross_audit_receipt_hash": audit["receipt_hash"]}

    monkeypatch.setattr(
        cli, "_aggregate_cross_audit_receipt_exists", lambda *a, **k: True
    )
    monkeypatch.setattr(cli, "_mark_aggregate_receipt_run_complete", lambda *a: None)
    monkeypatch.setattr(cli, "_deliver_aggregate_final_chat", delivery)
    monkeypatch.setattr(
        cli,
        "_execute_aggregate_cross_audit",
        lambda *a, **k: pytest.fail("audit repeated"),
    )
    monkeypatch.setattr(
        cli,
        "_execute_aggregate_workflow",
        lambda *a, **k: pytest.fail("workflow repeated"),
    )
    monkeypatch.setattr(
        cli,
        "_bind_production_runtime_for_run",
        lambda *a: pytest.fail("preflight repeated"),
    )
    with using_office_profile(profile):
        assert (
            cli._finalize_aggregate_tail(
                object(),
                args,
                artifact_path=path,
                workflows=workflows,
                prior={"cross_audit": audit},
            )
            == 1
        )
        prior = cli._load_aggregate_run_artifact(path)
        assert prior["status"] == "chat_pending"
        assert (
            cli._finalize_aggregate_tail(
                object(), args, artifact_path=path, workflows=workflows, prior=prior
            )
            == 0
        )
        assert cli._load_aggregate_run_artifact(path)["status"] == "complete"
    assert len(deliveries) == 2


def payroll_profile(profile):
    rules = {
        "credit_net_account_default": "Payroll liability",
        "allowed_net_credit_accounts": ["Payroll liability"],
        "debit_salary_account": "Salary expense",
        "debit_executive_salary_account": "Officer expense",
        "debit_bonus_account": "Bonus expense",
        "debit_legal_welfare_account": "Employer expense",
        "credit_withholding_account": "Withholding liability",
        "credit_unknown_deduction_subaccount": "Unclassified withholding",
        "credit_company_si_account": "Employer liability",
        "debit_company_social_insurance_subaccount": "Employer social expense",
        "credit_company_social_insurance_subaccount": "Employer social payable",
        "debit_company_labor_insurance_subaccount": "Employer labor expense",
        "credit_company_labor_insurance_subaccount": "Employer labor payable",
        "deduction_subaccount_rules": [["Tax", "Tax payable"]],
        "required_deduction_subaccounts": ["Tax payable"],
    }
    return replace(profile, accounting_rules={"payroll": {"rules": rules}})


def payroll_payload(kind="salary", role="employee"):
    return {
        "schema": "moneyforward-ai-poc-phase3-payroll-input-v2",
        "period": "2026-04",
        "payroll_type": kind,
        "employee_count": 1,
        "payment_items": [{"label": "Pay", "amount": 1000}],
        "deduction_items": [{"label": "Tax", "amount": 100}],
        "net_payment": {"label": "Net", "amount": 900},
        "company_social_insurance": [{"label": "Social", "amount": 50}],
        "employees": [
            {
                "employee_ref": "worker-a",
                "sub_account": "Worker A",
                "role": role,
                "allocation_evidence_hash": "sha256:" + "a" * 64,
                "payment_items": [{"label": "Pay", "amount": 1000}],
                "deduction_items": [{"label": "Tax", "amount": 100}],
                "net_payment": {"label": "Net", "amount": 900},
            }
        ],
    }


@pytest.mark.parametrize(
    "kind,role,expected",
    [
        ("salary", "employee", "Salary expense"),
        ("salary", "executive", "Officer expense"),
        ("bonus", "employee", "Bonus expense"),
    ],
)
def test_real_payroll_parser_and_builder_preserve_allocations(
    profile, kind, role, expected
):
    from mf_phase12.payroll import (
        parse_statement_payload,
        build_payroll_journal_candidate,
    )

    with using_office_profile(payroll_profile(profile)):
        candidate = build_payroll_journal_candidate(
            parse_statement_payload(payroll_payload(kind, role))
        )
    assert candidate.registration_allowed is True
    assert candidate.debit_total == candidate.credit_total == 1050
    assert any(
        line.account == expected and line.amount == 1000 for line in candidate.lines
    )
    assert any(
        line.account == "Payroll liability"
        and line.sub_account == "Worker A"
        and line.amount == 900
        for line in candidate.lines
    )


def test_real_payroll_mismatched_employee_totals_never_register(profile):
    from mf_phase12.payroll import (
        parse_statement_payload,
        build_payroll_journal_candidate,
    )

    payload = payroll_payload()
    payload["employees"][0]["net_payment"]["amount"] = 800
    with using_office_profile(payroll_profile(profile)):
        candidate = build_payroll_journal_candidate(parse_statement_payload(payload))
    assert candidate.registration_allowed is False
    assert candidate.validation.errors


def test_real_payroll_aggregate_only_never_becomes_postable(profile):
    from mf_phase12.payroll import (
        parse_statement_payload,
        build_payroll_journal_candidate,
    )

    payload = payroll_payload()
    payload.pop("employees")
    payload["schema"] = "moneyforward-ai-poc-phase3-payroll-input-v1"
    with using_office_profile(payroll_profile(profile)):
        candidate = build_payroll_journal_candidate(parse_statement_payload(payload))
    assert candidate.registration_allowed is False


def test_payroll_without_company_mappings_fails_closed(profile):
    from mf_phase12.payroll import parse_statement_payload

    with (
        using_office_profile(profile),
        pytest.raises(ValueError, match="payroll.rules"),
    ):
        parse_statement_payload(payroll_payload())


def gmail_profile(profile, count):
    return replace(
        profile,
        accounting_rules={
            "drive": {"payable_folder_id": "alpha_receipts"},
            "recurring_invoices": {
                "start_date": "2026-04-01",
                "evidence_source_accounts": [
                    f"inbox{i}@tenant.test" for i in range(count)
                ],
                "vendor_queries": [
                    {
                        "company_slug": "alpha",
                        "vendor_key": "supplier",
                        "display_name": "Supplier",
                        "query": "from:supplier.test",
                        "terms": ["Supplier"],
                    }
                ],
            },
        },
    )


def gmail_status(contract, *, captured=True):
    from datetime import datetime, timezone
    import hashlib

    rows = []
    for account in contract["accounts"]:
        account_hash = hashlib.sha256(account.encode()).hexdigest()[:16]
        receipts = [
            {
                "principal": account,
                "accountHash": account_hash,
                "vendorKey": query["vendor_key"],
                "queryHash": query["query_hash"],
                "status": "completed",
                "resultCount": 1,
                "processedCount": 1,
            }
            for query in contract["queries"]
            if query["account_email"] == account
        ]
        rows.append(
            {
                "principal": account,
                "accountHash": account_hash,
                "captured": captured,
                "coverageStartAfter": contract["coverage_start_after"],
                "ranAt": datetime.now(timezone.utc).isoformat(),
                "triggerCount": 1,
                "queryReceiptsReady": True,
                "queryReceipts": receipts,
                "failed": 0,
                "searchTruncated": 0,
                "blockedOpen": 0,
                "quarantineOpen": 0,
            }
        )
    return {
        "profile_slug": contract["profile_slug"],
        "boundary_fingerprint": contract["boundary_fingerprint"],
        "intake_contract_hash": stable_hash(contract),
        "principalAccepted": True,
        "destinationFoldersReadback": True,
        "destinationFolderIds": contract["destination_folder_ids"],
        "currentPrincipal": contract["accounts"][0],
        "coverage": {"principals": rows},
        "blockedItems": [],
        "quarantineItems": [],
    }


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("stale", [False, True])
def test_actual_gmail_capture_contract_and_per_mailbox_query_validation(
    profile, monkeypatch, count, stale
):
    selected = gmail_profile(profile, count)
    calls = []
    with using_office_profile(selected):
        contract = cli._gmail_intake_contract()

        def gas(name, params, token):
            assert params == [contract]  # Never [] for attachment-saving.
            assert token == "synthetic"
            calls.append(name)
            return gmail_status(contract, captured=not stale or len(calls) > 1)

        monkeypatch.setattr(cli, "_run_shared_gas_function", gas)
        monkeypatch.setattr(
            cli,
            "_capture_payable_drive_intake",
            lambda **k: {
                "files": [],
                "excluded_files": [],
                "source": {"file_count": 0},
            },
        )
        artifact, validated, invoked = cli._capture_payable_gas_readback("synthetic")
        assert len(validated["runs"]) == count
        assert artifact["gas_coverage_verified"] is True
        assert invoked is stale
        assert calls == (
            [
                "getGmailInvoiceIntakeRuntimeStatus",
                "saveIncomingInvoiceReceiptAttachments",
                "getGmailInvoiceIntakeRuntimeStatus",
            ]
            if stale
            else ["getGmailInvoiceIntakeRuntimeStatus"]
        )
        if count > 1:
            artifact["runs"][1]["query_receipts"][0]["query_hash"] = artifact["runs"][
                0
            ]["query_receipts"][0]["query_hash"]
            with pytest.raises(ValueError, match="query identity"):
                cli._validate_payable_invoice_gas_artifact(artifact)


@pytest.mark.parametrize(
    "fault", ["destination", "contract", "profile", "accounts", "hash", "readback"]
)
def test_gmail_refuses_attachment_save_before_complete_boundary_readback(
    profile, monkeypatch, fault
):
    with using_office_profile(gmail_profile(profile, 3)):
        contract = cli._gmail_intake_contract()
        status = gmail_status(contract, captured=False)
        if fault == "destination":
            status["destinationFolderIds"] = ["foreign_destination"]
        elif fault == "contract":
            status["intake_contract_hash"] = "bad"
        elif fault == "profile":
            status["profile_slug"] = "beta"
        elif fault == "accounts":
            status["coverage"]["principals"].pop()
        elif fault == "hash":
            status["coverage"]["principals"][1]["accountHash"] = status["coverage"][
                "principals"
            ][0]["accountHash"]
        else:
            status["destinationFoldersReadback"] = False
        calls = []

        def gas(name, params, token):
            calls.append(name)
            assert name == "getGmailInvoiceIntakeRuntimeStatus"
            return status

        monkeypatch.setattr(cli, "_run_shared_gas_function", gas)
        monkeypatch.setattr(
            cli,
            "_capture_payable_drive_intake",
            lambda **k: pytest.fail("unsafe continuation"),
        )
        with pytest.raises(ValueError):
            cli._capture_payable_gas_readback("synthetic")
        assert calls == ["getGmailInvoiceIntakeRuntimeStatus"]


def test_actual_payable_ai_decision_route_and_cache_replay(profile, monkeypatch):
    setup(profile, monkeypatch)
    packets = []

    def classifier(items, token):
        packet = items[0]
        packets.append(packet)
        assert packet["drive_route"]["company_slug"] == "alpha"
        assert packet["drive_route"]["credit_account"] == "未払金"
        assert packet["drive_route"]["payment_liability_route"] == "Supplier"
        return {
            "ocr_calls": 0,
            "gemini_calls": 1,
            "items": [
                {
                    "packet_hash": packet["packet_hash"],
                    "decidable": True,
                    "debit_account": "Supplies",
                    "broad_content": "Supplies",
                    "confidence": 1,
                    "rationale_codes": ["live_master"],
                }
            ],
        }

    monkeypatch.setattr(cli, "_run_shared_accounting_classifier", classifier)
    with (
        using_office_profile(profile),
        Phase12Database(profile.moneyforward_db_path).connect() as conn,
    ):
        for kind, name in [("account", "Supplies"), ("sub_account", "Supplier")]:
            conn.execute(
                "INSERT INTO master_items(source_name,item_type,external_id_hash,name_normalized) VALUES(?,?,?,?)",
                ("synthetic", kind, stable_hash(name), name),
            )
        kwargs = {
            "transactions": [
                {
                    "deal_date": "2026-04-30",
                    "total_value": 1000,
                    "excise_value": 100,
                    "cr_item": {"name": "未払金"},
                    "cr_sub_item": {"name": "Supplier"},
                }
            ],
            "vendor_names": ["Supplier"],
            "file_hash": stable_hash("synthetic file"),
            "office_id": "alpha-office",
            "drive_route": {"document_route": "invoice"},
            "access_token": "synthetic",
        }
        assert cli._resolve_payable_debit_decision(conn, **kwargs) == (
            ("Supplies", None),
            1,
        )
        assert cli._resolve_payable_debit_decision(conn, **kwargs) == (
            ("Supplies", None),
            0,
        )
        assert len(packets) == 1  # Real persisted cache prevents repeat AI/OCR.
        kwargs["drive_route"] = {"company_slug": "beta"}
        with pytest.raises(ValueError, match="another company"):
            cli._resolve_payable_debit_decision(conn, **kwargs)
        assert len(packets) == 1


@pytest.mark.parametrize(
    "offset,closing,expected",
    [
        (-1, "last", (date(2026, 8, 1), date(2026, 8, 31))),
        (0, 20, (date(2026, 8, 21), date(2026, 9, 20))),
        (-7, 31, (date(2026, 2, 1), date(2026, 2, 28))),
    ],
)
def test_company_payroll_period_policy(profile, offset, closing, expected):
    selected = replace(
        profile,
        accounting_rules={
            "payroll": {
                "period_month_offset": offset,
                "closing_day": closing,
                "payment_month_offset": 2,
            }
        },
    )
    with using_office_profile(selected):
        assert cli._configured_payroll_period(date(2026, 9, 11)) == expected
        assert cli._configured_payroll_payment_month(date(2026, 11, 20)) == "2027-01"


def test_company_calendar_policies_never_fabricated(profile):
    with using_office_profile(profile):
        assert cli._company_fiscal_year_start(date(2026, 3, 31)) == date(2025, 4, 1)
        with pytest.raises(ValueError, match="period_month_offset"):
            cli._configured_payroll_period(date(2026, 9, 11))
        with pytest.raises(ValueError, match="payment_month_offset"):
            cli._configured_payroll_payment_month(date(2026, 9, 20))


@pytest.mark.parametrize("observed", ["alpha-payable", "foreign-payable"])
def test_cli_payable_office_uses_configured_hash_of_live_identity(
    profile, monkeypatch, observed
):
    from mf_phase12 import accounting_api

    selected = replace(
        profile,
        accounting_rules={
            "payable": {
                "office_id_hash": stable_hash("alpha-payable"),
                "office_identification_code": "123",
            }
        },
    )
    monkeypatch.setattr(
        accounting_api.UrllibTransport,
        "request",
        lambda *a, **k: (
            200,
            {},
            json.dumps({"offices": [{"id": observed, "code": "123"}]}).encode(),
        ),
    )
    with using_office_profile(selected):
        if observed == "alpha-payable":
            assert (
                cli._read_payable_external_office_id(
                    token="synthetic", base_url="https://example.test"
                )
                == observed
            )
        else:
            with pytest.raises(PermissionError, match="office ID"):
                cli._read_payable_external_office_id(
                    token="synthetic", base_url="https://example.test"
                )


@pytest.mark.parametrize("write_enabled", [False, True])
def test_cli_constructs_single_guarded_payable_adapter(
    profile, monkeypatch, write_enabled
):
    from mf_phase12 import accounting_api

    accounting = accounting_api.MoneyForwardAccountingClient(
        "synthetic-gateway",
        office_profile=profile,
        keychain_service=profile.moneyforward_keychain_service,
        db_path=profile.moneyforward_db_path,
        write_enabled=write_enabled,
    )
    runtime = cli.ProductionRuntime(
        accounting,
        "alpha-office",
        None,
        "synthetic-drive",
        payable=cli.PayableAuthContext(
            gateway_access_token=accounting.access_token,
            external_access_token="synthetic-external",
            external_office_id="alpha-payable",
            external_token_refreshed=False,
        ),
    )
    original = accounting_api.MoneyForwardPayableClient
    created = []

    class ConstructionChecked(Exception):
        pass

    def check_constructor(**kwargs):
        client = original(**kwargs)
        assert client.office_profile is profile
        assert client.accounting_client is accounting
        assert client.keychain_service == accounting.keychain_service
        assert client.db_path == accounting.db_path
        assert client.write_enabled is write_enabled
        assert client.gateway_access_token == accounting.access_token
        assert type(client._transport) is accounting_api.UrllibTransport
        created.append(client)
        raise ConstructionChecked

    monkeypatch.setattr(accounting_api, "MoneyForwardPayableClient", check_constructor)
    with using_office_profile(profile):
        intent = cli._PRODUCTION_WRITES.set(write_enabled)
        try:
            with pytest.raises(ConstructionChecked):
                cli._execute_payable_native_files(
                    None,
                    Namespace(),
                    runtime=runtime,
                    artifact={},
                    artifact_path=profile.repository_root / "synthetic.json",
                )
        finally:
            cli._PRODUCTION_WRITES.reset(intent)
    assert len(created) == 1
    assert not hasattr(cli, "_BoundPayableTransport")


def test_receipt_route_passes_real_accounting_packet_validator(profile):
    from mf_phase12.financial_event_classification import (
        build_accounting_decision_packet,
        validate_accounting_ai_decision,
    )

    with using_office_profile(profile):
        route = cli._accounting_decision_route(
            {"document_route": "receipt"},
            credit_account="Card liability",
            credit_sub_account="Corporate card",
        )
        packet = build_accounting_decision_packet(
            {
                "canonical_facts": {"amount": 1100, "transaction_date": "2026-09-01"},
                "drive_route": route,
                "fixed_fields": {
                    "amount": 1100,
                    "journal_date": "2026-09-01",
                    "tax_amount": 100,
                    "tax_rows": [{"tax_amount": 100}],
                    "credit_account": "Card liability",
                    "credit_sub_account": "Corporate card",
                },
                "allowed_debit_accounts": ["Supplies"],
                "live_master": {"credit_sub_account_unique": True},
                "moneyforward_identity": {
                    "verified": True,
                    "office_id_hash": "sha256:" + stable_hash("alpha-office"),
                },
            }
        )
        decision = {
            "packet_hash": packet["packet_hash"],
            "decidable": True,
            "debit_account": "Supplies",
            "broad_content": "Supplies",
            "confidence": 1,
            "rationale_codes": [],
        }
        result = validate_accounting_ai_decision(packet, decision)
        assert result.status == "decided", result.reason_codes
        for conflict in [
            {"company_slug": "beta"},
            {"credit_account": "Foreign liability"},
            {"payment_liability_route": "Foreign card"},
        ]:
            with pytest.raises(ValueError):
                cli._accounting_decision_route(
                    conflict,
                    credit_account="Card liability",
                    credit_sub_account="Corporate card",
                )
