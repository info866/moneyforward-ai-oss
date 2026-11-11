"""Synthetic policy checks: no credentials, network, files of business data or DB."""
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from mf_phase12 import card, drive_batch, drive_policy, financial_event_classification as financial
from mf_phase12 import gmail_recurring_invoice as gmail
from mf_phase12 import skills, supplier_primary_watchlist as supplier
from mf_phase12 import sub_account_request
from mf_phase12.models import Evidence, JournalCandidate
from mf_phase12.office_profile import OfficeProfile, using_office_profile


def profile(slug="tenant-a", rules=None):
    return OfficeProfile(
        schema_version=2, slug=slug, legal_name=f"Synthetic {slug}", display_name=slug,
        expected_office_names=(f"Synthetic {slug}",), google_account=f"{slug}@example.invalid",
        moneyforward_keychain_service=f"test-{slug}", moneyforward_db_path=Path(f"/unused/{slug}.sqlite"),
        google_drive_account="shared@example.invalid", google_drive_evidence_root_id=f"{slug}-root",
        google_drive_evidence_source_folder_ids=(f"{slug}-source",),
        google_drive_processed_folder_id=f"{slug}-processed", accounting_rules=rules or {},
    )


def policy(**values):
    return {"company_slug": "tenant-a", **values}


def evidence(source="tenant-a-source", **values):
    return Evidence(google_drive_file_id="synthetic-file", file_name="synthetic.pdf",
                    file_hash="a" * 64, parent_folder_id=source, **values)


def candidate(**values):
    return replace(JournalCandidate(
        journal_date="2030-01-03", vendor_name="Synthetic Merchant", amount=1200,
        tax_amount=0, tax_rate=0, debit_account="Observed Expense", debit_sub_account=None,
        credit_account="Observed Liability", credit_sub_account=None,
        tax_category="Observed Tax", description="駐車 印紙 飲食 燃料 消耗品",
    ), **values)


def card_rules():
    return {"card_routes": [policy(credit_account="Card Liability", credit_sub_account="Company Card",
                                  ledger_account_id="account-a", ledger_sub_account_id="sub-a")]}


def card_event(**values):
    return {"company_slug": "tenant-a", "source_event_type": "card_transaction",
            "connected_ledger_account_id": "account-a", "connected_ledger_sub_account_id": "sub-a",
            "transaction_side": "EXPENSE", "transaction_date": "2030-01-03",
            "amount": 1200, "counterparty": "Synthetic Merchant", **values}


def masters():
    return {"accounts": [{"id": "account-a", "name": "Card Liability"}],
            "sub_accounts": [{"id": "sub-a", "account_id": "account-a", "name": "Company Card"}]}


def financial_event(**values):
    return {"company_slug": "tenant-a", "source_event_type": "bank_transaction",
            "transaction_date": "2030-01-03", "cash_flow_direction": "inflow",
            "amount": 1200, "description": "Synthetic remittance", "counterparty": "Synthetic Payer", **values}


def fixed_rule():
    return policy(match={"cash_flow_direction": "inflow", "description_contains": ["Synthetic remittance"]},
                  candidate={"debit_account": "Configured Bank", "credit_account": "Configured Receivable",
                             "credit_sub_account": "Configured Customer", "tax_category": "対象外"})


def test_no_active_receipt_rule_without_configuration():
    with using_office_profile(profile()):
        original = candidate()
        assert skills.apply_fixed_rules(original) == original
        assert skills.apply_receipt_usage_rules(original) == original
        assert skills.detect_failure_examples(original) == ()
        assert drive_policy.folder_credit_decision(evidence()) is None
        assert drive_policy.folder_expense_decision(evidence()) is None
        with pytest.raises(ValueError, match="credit route"):
            drive_policy.decide_credit_side(original, evidence())


def test_no_financial_rule_or_vendor_watch_without_configuration(monkeypatch):
    conn = Mock()
    monkeypatch.setattr(financial, "_find_pattern_matches", lambda *_: [])
    with using_office_profile(profile()):
        event = financial_event()
        assert financial._classify_company_fixed_event(event, financial.normalize_financial_event(event)) is None
        assert not financial._classify_event(conn, event).registration_allowed
        assert financial.upsert_company_fixed_accounting_rules(conn) == 0
        assert not financial._configured_card_statement_requires_target_exclusion(card_event(), financial.normalize_financial_event(card_event()))
        with pytest.raises(ValueError, match="card route"):
            card.configured_card_route()
    assert conn.mock_calls == []


def test_configured_receipt_rule_only_applies_to_explicit_match():
    rules = {"receipt_fixed_rules": [policy(description_contains=["Synthetic service"],
                                           candidate={"debit_account": "Configured Expense"})]}
    with using_office_profile(profile(rules=rules)):
        original = candidate(description="Synthetic service")
        assert skills.apply_fixed_rules(original).debit_account == "Configured Expense"
        assert skills.apply_fixed_rules(original).amount == original.amount
        assert skills.apply_fixed_rules(candidate()) == candidate()


def test_fixed_financial_rule_is_scoped_and_preserves_facts():
    with using_office_profile(profile(rules={"financial_event_rules": [fixed_rule()]})):
        event = financial_event()
        result = financial._classify_company_fixed_event(event, financial.normalize_financial_event(event))
        assert result.registration_allowed
        assert result.journal_candidate["amount"] == 1200
        assert result.journal_candidate["journal_date"] == "2030-01-03"
        assert result.journal_candidate["credit_sub_account"] == "Configured Customer"
        wrong_direction = financial_event(cash_flow_direction="outflow")
        assert financial._classify_company_fixed_event(wrong_direction, financial.normalize_financial_event(wrong_direction)) is None
    with using_office_profile(profile("tenant-b", {"financial_event_rules": [fixed_rule()]})):
        with pytest.raises(ValueError, match="company binding"):
            financial._classify_company_fixed_event(event, financial.normalize_financial_event(event))


def test_fixed_rule_cannot_override_amount_and_ambiguous_rules_reject():
    rule = fixed_rule()
    rule["candidate"]["amount"] = 9999
    event = financial_event()
    with using_office_profile(profile(rules={"financial_event_rules": [rule]})):
        with pytest.raises(ValueError, match="authoritative facts"):
            financial._classify_company_fixed_event(event, financial.normalize_financial_event(event))
    with using_office_profile(profile(rules={"financial_event_rules": [fixed_rule(), fixed_rule()]})):
        with pytest.raises(ValueError, match="ambiguous"):
            financial._classify_company_fixed_event(event, financial.normalize_financial_event(event))


def test_drive_rule_uses_own_ids_not_folder_names_or_pdf_text():
    rules = {"folder_credit_rules": [policy(folder_id="tenant-a-source", credit_account="Configured Payable",
                                           credit_sub_account="Configured Owner", kind="personal_payable")]}
    with using_office_profile(profile(rules=rules)):
        own = drive_policy.folder_credit_decision(evidence(parent_folder_name="unrelated"))
        assert own.credit_sub_account == "Configured Owner"
        assert drive_policy.is_personal_payable_decision(own)
        with pytest.raises(ValueError, match="source Drive route"):
            drive_policy.folder_credit_decision(evidence("tenant-b-source", parent_folder_name="Configured Owner"))
    with using_office_profile(profile("tenant-b", rules)):
        with pytest.raises(ValueError, match="company binding"):
            drive_policy.folder_credit_decision(evidence("tenant-b-source"))


@pytest.mark.parametrize("source,destination", [("tenant-b-source", None), ("tenant-a-source", "tenant-b-processed"), ("", None)])
def test_wrong_drive_routes_fail_closed(source, destination):
    with using_office_profile(profile()):
        with pytest.raises(ValueError):
            drive_policy.drive_move_request(evidence(source), destination)


def test_processed_destination_and_batch_readback_gate():
    with using_office_profile(profile()):
        item = drive_batch.DriveBatchItem("synthetic-file", "synthetic.pdf", "a" * 64,
                                        "voucher_attached", False, parent_folder_id="tenant-a-source")
        assert drive_batch.build_drive_batch_decision(item).action == "read_back_verify_before_move"
        verified = replace(item, processing_status="verified", verification_passed=True, idempotency_key="synthetic-chain")
        decision = drive_batch.build_drive_batch_decision(verified)
        assert decision.move_request["destination_folder_id"] == "tenant-a-processed"
        assert decision.move_request["idempotency_key"] == "synthetic-chain"
        assert decision.move_request["rename"] == "false"
    with using_office_profile(replace(profile(), google_drive_processed_folder_id=None)):
        with pytest.raises(ValueError, match="processed Drive route"):
            drive_policy.drive_move_request(evidence())


@pytest.mark.parametrize("changes", [{"company_slug": "tenant-b"}, {"company_slug": ""},
                                    {"connected_ledger_account_id": "account-b"},
                                    {"connected_ledger_sub_account_id": "sub-b"}])
def test_card_wrong_company_or_master_route_rejected_before_db(changes):
    conn = Mock()
    with using_office_profile(profile(rules=card_rules())):
        with pytest.raises(ValueError, match="company route"):
            card.decide_card_transactions([card_event(**changes)], conn=conn, masters=masters())
    assert conn.mock_calls == []


def test_configured_card_route_accepts_exact_live_master_only():
    with using_office_profile(profile(rules=card_rules())):
        assert card.is_configured_card_transaction(card_event(), masters())
        duplicate = masters()
        duplicate["sub_accounts"] *= 2
        assert not card.is_configured_card_transaction(card_event(), duplicate)
        result = card._candidate(card_event(), "Observed Expense", "", "Observed Tax", "")
        assert result["credit_sub_account"] == "Company Card"
        assert result["amount"] == 1200
        received = card._candidate(card_event(transaction_side="INCOME"), "Original Expense", "", "Observed Tax", "")
        assert received["debit_sub_account"] == "Company Card"


def test_supplier_watch_missing_configuration_has_no_db_side_effect():
    conn = Mock()
    item = supplier.SupplierPrimaryWatchItem("purchase", "Synthetic Vendor", "2030-01-03", 1200,
                                            "synthetic-no", "synthetic-journal", True,
                                            "Payable", "Card", "synthetic-card")
    with using_office_profile(profile()):
        with pytest.raises(ValueError, match="supplier watch route"):
            supplier.upsert_supplier_primary_card_duplicate_watch(conn, item)
    assert conn.mock_calls == []


def test_recurring_invoice_has_no_vendor_defaults_and_refuses_foreign_routes():
    with using_office_profile(profile()):
        assert gmail.recurring_vendor_queries() == ()
        assert gmail.recurring_invoice_routes() == ()
        with pytest.raises(ValueError, match="not configured"):
            gmail.build_recurring_invoice_search_plan(start_date="2030-01-01")
    with using_office_profile(profile(rules={"recurring_invoices": {"routes": [policy(
        company_slug="tenant-b", vendor_key="synthetic", route="paper", aliases=["Synthetic Vendor"])]}})):
        with pytest.raises(ValueError, match="company binding"):
            gmail.recurring_invoice_routes()


def test_recurring_invoice_explicit_shared_mailbox_and_query_receipts():
    settings = {"recurring_invoices": {
        "evidence_source_accounts": ["shared@example.invalid"],
        "vendor_queries": [policy(vendor_key="synthetic", display_name="Synthetic Vendor", query="synthetic-invoice",
                                  terms=["Synthetic Vendor"], required=True)],
    }}
    with using_office_profile(profile(rules=settings)):
        plan = gmail.build_recurring_invoice_search_plan(start_date="2030-01-01")
        assert plan.accounts == ("shared@example.invalid",)
        assert len(plan.search_items) == 1
        assert plan.to_redacted_dict()["company_slug"] == "tenant-a"
        assert gmail._validated_query_receipts([], plan) == {}
        with pytest.raises(ValueError, match="exactly"):
            gmail.build_recurring_invoice_search_plan(start_date="2030-01-01", accounts=("foreign@example.invalid",))
        item = plan.search_items[0]
        receipt = {"principal": plan.accounts[0], "accountEmail": plan.accounts[0],
                   "accountHash": item["account_hash"], "vendorKey": item["vendor_key"],
                   "queryHash": item["query_hash"], "status": "completed", "resultCount": 0, "processedCount": 0}
        assert len(gmail._validated_query_receipts([receipt], plan)) == 1
        with pytest.raises(ValueError, match="duplicate"):
            gmail._validated_query_receipts([receipt, receipt], plan)


def test_verified_rag_candidate_and_transaction_binding_remain_authoritative(monkeypatch):
    row = {"debit_account": "Historical Expense", "debit_sub_account": None,
           "credit_account": "Historical Payable", "credit_sub_account": None,
           "tax_category": "対象外", "pattern_hash": "synthetic-pattern", "confidence": 1.0,
           "auto_post_eligibility": "enabled", "match_source": "exact_vendor_description_amount",
           "success_count": 4, "failure_count": 0}
    monkeypatch.setattr(financial, "_find_pattern_matches", lambda *_: [row])
    with using_office_profile(profile()):
        event = financial_event(cash_flow_direction="outflow")
        result = financial._classify_event(Mock(), event)
        assert result.registration_allowed
        assert result.journal_candidate["debit_account"] == "Historical Expense"
        bound = financial._attach_moneyforward_transaction_binding(result, {**event, "transaction_id": "synthetic-id"})
        assert not bound.registration_allowed
        assert "moneyforward_transaction_binding_incomplete" in bound.unresolved_points


@pytest.mark.parametrize("engine", [None, "auto", "python"])
def test_python_classification_does_not_activate_ambient_binary(engine, monkeypatch):
    monkeypatch.setenv("MF_PHASE12_RUST_CORE_BIN", "/untrusted/mf-phase12-core")
    monkeypatch.setattr(financial, "_ensure_tables", lambda *_: None)
    monkeypatch.setattr(financial, "_find_pattern_matches", lambda *_: [])
    def forbidden(*args, **kwargs):
        raise AssertionError("Rust must not be probed for Python or auto classification")
    monkeypatch.setattr(financial, "_classify_financial_events_with_rust", forbidden)
    conn = Mock()
    with using_office_profile(profile()):
        batch = financial.classify_financial_events({"events": [financial_event()]}, conn=conn, engine=engine)
        assert not batch.items[0].registration_allowed
        assert batch.items[0].matched_pattern_source == "none"
        assert financial._resolve_engine(None) == "python"
    assert conn.mock_calls == []


def test_explicit_rust_requires_reviewed_bundle_before_database_use(monkeypatch):
    from mf_phase12 import rust_core
    def unavailable():
        raise FileNotFoundError("No reviewed Rust classifier is bundled; use the Python classifier")
    monkeypatch.setattr(rust_core, "rust_core_command", unavailable)
    conn = Mock()
    with using_office_profile(profile()):
        with pytest.raises(FileNotFoundError, match="reviewed Rust classifier"):
            financial.classify_financial_events([financial_event()], conn=conn, engine="rust")
    assert conn.mock_calls == []


def test_fixed_rule_rag_materialization_is_explicit_repeatable_and_never_purges(monkeypatch):
    conn = Mock()
    manifests = []
    cards_seen = []
    def manifest(connection, source, kind, cards):
        assert connection is conn
        manifests.append((source, kind, cards))
        return "synthetic-manifest"
    def build(connection, cards, source_hash, *, commit):
        assert connection is conn and source_hash == "synthetic-manifest" and commit is False
        cards_seen.append(cards)
        return len(cards)
    monkeypatch.setattr(financial, "upsert_sync_manifest", manifest)
    monkeypatch.setattr(financial, "build_rag_cards", build)
    with using_office_profile(profile(rules={"financial_event_rules": [fixed_rule()]})):
        assert financial.upsert_company_fixed_accounting_rules(conn) == 1
        assert financial.upsert_company_fixed_accounting_rules(conn) == 1
    assert cards_seen[0] == cards_seen[1]
    assert cards_seen[0][0]["card_type"] == "fixed_rule_candidate"
    assert manifests[0] == manifests[1]
    assert not conn.execute.called


def test_generic_supplier_duplicate_policy_requires_configured_vendor_and_exact_card():
    rule = policy(card_account_key="synthetic-card", vendor_aliases=["Synthetic Supplier"])
    event = card_event(bank_or_card_account="synthetic-card", counterparty="Synthetic Supplier")
    with using_office_profile(profile(rules={"supplier_primary_card_routes": [rule]})):
        assert financial._configured_card_statement_requires_target_exclusion(event, financial.normalize_financial_event(event))
        wrong = {**event, "bank_or_card_account": "foreign-card"}
        assert not financial._configured_card_statement_requires_target_exclusion(wrong, financial.normalize_financial_event(wrong))
        other_vendor = {**event, "counterparty": "Unconfigured Supplier"}
        assert not financial._configured_card_statement_requires_target_exclusion(other_vendor, financial.normalize_financial_event(other_vendor))
        matched = {"company_slug": "tenant-a", "supplier_registration_readback_verified": True, "voucher_readback_verified": True}
        assert financial._has_supplier_primary_match({"matched_supplier_primary_purchase": matched})
        with pytest.raises(ValueError, match="company binding"):
            financial._has_supplier_primary_match({"matched_supplier_primary_purchase": {**matched, "company_slug": "tenant-b"}})


def test_sub_account_draft_uses_profile_identity_and_rejects_other_company(monkeypatch):
    input_path, output_path = Mock(), Mock()
    monkeypatch.setattr(sub_account_request, "require_ignored_untracked_data_path", lambda path, **_: path)
    payload = {"company_slug": "tenant-a", "account_name": "Synthetic Account", "sub_account_name": "Synthetic Sub"}
    input_path.read_text.return_value = json.dumps(payload)
    with using_office_profile(profile()):
        draft = sub_account_request.build_sub_account_create_candidate_draft(input_path, output_path)
        written = json.loads(output_path.write_text.call_args.args[0])
        assert written["company_slug"] == "tenant-a"
        assert written["required_tenant"] == "Synthetic tenant-a"
        assert written["execution_principal"] == "tenant-a@example.invalid"
        assert written["registration_allowed"] is False
        assert draft.next_action == "create_in_authenticated_company_then_refetch_master_and_resume_batch"
    output_path.reset_mock()
    with using_office_profile(profile("tenant-b")):
        with pytest.raises(ValueError, match="company binding"):
            sub_account_request.build_sub_account_create_candidate_draft(input_path, output_path)
    assert output_path.mock_calls == []


def test_expense_policy_rejects_foreign_route_even_without_rules():
    with using_office_profile(profile()):
        with pytest.raises(ValueError, match="source Drive route"):
            drive_policy.folder_expense_decision(evidence("tenant-b-source"))


def test_financial_statement_without_drive_evidence_preserves_authoritative_credit():
    with using_office_profile(profile()):
        original = candidate(source_type="card_statement")
        assert drive_policy.apply_drive_credit_policy(original) == original


def test_card_ai_packet_is_bound_to_company_even_when_account_names_match():
    settings = card_rules()
    with using_office_profile(profile(rules=settings)):
        packet_a = card._decision_packet(card_event(), masters(), [], decision_required=True)
    settings["card_routes"][0]["company_slug"] = "tenant-b"
    with using_office_profile(profile("tenant-b", settings)):
        packet_b = card._decision_packet(card_event(company_slug="tenant-b"), masters(), [], decision_required=True)
        assert packet_a["packet_hash"] != packet_b["packet_hash"]
        result = card._resolve_ai_decision(card_event(company_slug="tenant-b"), masters(), packet=packet_b,
                                           decision={"packet_hash": packet_a["packet_hash"]})
        assert result[0] is None
        assert "ai_decision_packet_hash_mismatch" in result[2]
