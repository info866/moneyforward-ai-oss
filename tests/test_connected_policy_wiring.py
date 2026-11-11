"""Real producer/consumer regressions with synthetic artifacts and no service/DB use."""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from types import MappingProxyType

import pytest

from mf_phase12 import connected_account_probe as probe
from mf_phase12 import journal_batch_router as router
from mf_phase12 import local_artifacts
from mf_phase12.card import is_configured_card_transaction
from mf_phase12.financial_event_classification import _moneyforward_transaction_binding
from mf_phase12.hash_utils import stable_hash
from mf_phase12.models import Evidence, JournalCandidate
from mf_phase12.office_profile import OfficeProfile, using_office_profile
from mf_phase12.sub_account_request import build_sub_account_create_candidate_draft


def rules(slug="tenant-a"):
    return {
        "card_routes": [{"company_slug": slug, "credit_account": "Synthetic Liability",
                         "credit_sub_account": "Synthetic Purchases", "ledger_account_id": "ledger-a",
                         "ledger_sub_account_id": "ledger-sub-a"}],
        "folder_credit_rules": [{"company_slug": slug, "folder_id": f"synthetic-source-{slug}",
                                 "credit_account": "Synthetic Liability", "credit_sub_account": "Synthetic Purchases"}],
    }


@pytest.fixture
def company(tmp_path, monkeypatch):
    # Exercise the real ignored/untracked-path guard in a disposable repository.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("data/\n")
    monkeypatch.setattr(local_artifacts, "ROOT", tmp_path)
    slug = "tenant-a"
    data = tmp_path / "data" / "phase12" / "offices" / slug
    data.mkdir(parents=True)
    return OfficeProfile(
        schema_version=2, configured=True, slug=slug, legal_name="Synthetic Tenant A",
        display_name="Synthetic Tenant A", expected_office_names=("Synthetic Tenant A",),
        google_account="tenant-a@synthetic.invalid", moneyforward_keychain_service="synthetic-mf-a",
        moneyforward_db_path=data / "never-opened.sqlite", repository_root=tmp_path,
        google_drive_account="shared@synthetic.invalid", google_drive_evidence_root_id=f"synthetic-source-{slug}",
        google_drive_evidence_source_folder_ids=(f"synthetic-source-{slug}",),
        google_drive_processed_folder_id="synthetic-processed-tenant-a", accounting_rules=rules(),
    )


def raw_transaction(**changes):
    return {"id": "synthetic-transaction", "connected_account_id": "connected-a",
            "connected_sub_account_id": "connected-sub-a", "date": "2030-01-03",
            "value": 1200, "side": "EXPENSE", "content": "Synthetic purchase",
            "journalizing_status": "none", **changes}


def connected_accounts(name="Synthetic Purchases"):
    return [{"id": "connected-a", "name": name, "connected_sub_accounts": [
        {"id": "connected-sub-a", "account_id": "ledger-a", "sub_account_id": "ledger-sub-a"}
    ]}]


def masters():
    return {"accounts": [{"id": "ledger-a", "name": "Synthetic Liability"}],
            "sub_accounts": [{"id": "ledger-sub-a", "account_id": "ledger-a", "name": "Synthetic Purchases"}]}


class SyntheticClient:
    def __init__(self, profile):
        self.office_profile = profile
        self.calls = []

    def connected_accounts(self, office_id):
        self.calls.append(("connected_accounts", office_id))
        return {"data": connected_accounts()}

    def transactions(self, office_id, **kwargs):
        self.calls.append(("transactions", office_id, kwargs))
        return {"transactions": [raw_transaction()], "metadata": {"total_pages": 1, "total_count": 1}}


def test_probe_artifact_flows_into_real_card_selector_with_binding(company):
    client = SyntheticClient(company)
    output = company.moneyforward_db_path.parent / "snapshot.local.json"
    with using_office_profile(company):
        result = probe.run_connected_account_probe(
            accounting_client=client, office_id="synthetic-office", include_unposted_transactions=True,
            start_date="2030-01-01", end_date="2030-01-03", normalized_transactions_output_json=output,
        )
        artifact = json.loads(output.read_text())
        event = artifact["transactions"][0]
        assert result.status == "complete" and result.unposted_financial_event_count == 1
        assert artifact["profile_slug"] == artifact["company_slug"] == company.slug
        assert artifact["boundary_fingerprint"] == company.boundary_fingerprint
        assert event["company_slug"] == company.slug
        assert event["source_event_type"] == "card_transaction"
        assert event["connected_source_type"] == "card"
        assert is_configured_card_transaction(event, masters())
        assert _moneyforward_transaction_binding(event)["transaction_id"] == "synthetic-transaction"
        assert event["amount"] == 1200 and event["transaction_date"] == "2030-01-03"
        assert event["source_row_hash"] == stable_hash(raw_transaction())
        again = probe._normalize_transaction(raw_transaction(), account_bindings=probe._connected_account_bindings(connected_accounts()))
        assert again == event
        assert not is_configured_card_transaction({**event, "company_slug": "tenant-b"}, masters())
    assert not company.moneyforward_db_path.exists()


def test_foreign_client_rejected_before_provider_read(company):
    client = SyntheticClient(replace(company, google_account="foreign@synthetic.invalid"))
    with using_office_profile(company), pytest.raises(ValueError, match="client does not match"):
        probe.run_connected_account_probe(accounting_client=client)
    assert client.calls == []


@pytest.mark.parametrize("field,value", [("company_slug", "tenant-b"), ("profile_slug", "tenant-b"),
                                       ("boundary_fingerprint", "foreign-binding")])
def test_normalizer_never_relabels_foreign_input(company, field, value):
    with using_office_profile(company), pytest.raises(ValueError, match="binding mismatch"):
        probe._normalize_transaction(raw_transaction(**{field: value}), account_bindings={})


def test_no_context_cannot_label_a_transaction():
    with pytest.raises(ValueError, match="explicit current company"):
        probe._normalize_transaction(raw_transaction(), account_bindings={})


def test_supplier_matching_only_from_frozen_current_profile_settings(company):
    configured = replace(company, accounting_rules={**rules(), "supplier_primary": {"vendor_markers": ["Synthetic Supplier"]}})
    assert isinstance(configured.accounting_rules, MappingProxyType)
    assert isinstance(configured.accounting_rules["supplier_primary"]["vendor_markers"], tuple)
    with using_office_profile(company):
        assert probe._connected_source_type("Synthetic Supplier") == "bank"
    with using_office_profile(configured):
        assert probe._connected_source_type("ＳＹＮＴＨＥＴＩＣ Supplier / Purchases") == "supplier_primary"
        assert probe._source_event_type("Synthetic Supplier") == "card_transaction"
        assert probe._connected_source_type("Unconfigured Supplier") == "bank"
        event = probe._normalize_transaction(raw_transaction(), account_bindings=probe._connected_account_bindings(connected_accounts("Synthetic Supplier")))
        assert event["connected_source_type"] == "supplier_primary"
        assert event["source_event_type"] == "card_transaction"
        assert event["company_slug"] == company.slug
    with using_office_profile(company):
        assert probe._connected_source_type("Synthetic Supplier") == "bank"


@pytest.mark.parametrize("markers", ["Synthetic", [""], [None], {"vendor": "Synthetic"}])
def test_invalid_supplier_marker_config_is_not_a_wildcard(company, markers):
    configured = replace(company, accounting_rules={"supplier_primary": {"vendor_markers": markers}})
    with using_office_profile(configured), pytest.raises(ValueError, match="nonempty strings"):
        probe._connected_source_type("Synthetic Supplier")


def test_foreign_or_ambiguous_card_rules_fail_closed(company):
    for config, reason in [(rules("tenant-b"), "company binding mismatch"),
                           ({"card_routes": rules()["card_routes"] * 2}, "ambiguous")]:
        with using_office_profile(replace(company, accounting_rules=config)), pytest.raises(ValueError, match=reason):
            probe._normalize_transaction(raw_transaction(), account_bindings=probe._connected_account_bindings(connected_accounts()))


def test_full_batch_router_emits_company_bound_subaccount_request(company):
    data = company.moneyforward_db_path.parent
    batch_input, dry_input, output = data / "batch.local.json", data / "dry.local.json", data / "routed"
    candidate = JournalCandidate("2030-01-03", "Synthetic Vendor", 1200, 0, 0.0,
                                 "Synthetic Expense", None, "Synthetic Liability", "Synthetic Purchases",
                                 "対象外", "Synthetic purchase")
    evidence = Evidence("synthetic-file", "synthetic.pdf", "a" * 64,
                        parent_folder_id=company.google_drive_evidence_root_id)
    batch_input.write_text(json.dumps({"items": [{"candidate": candidate.to_dict(), "evidence": asdict(evidence)}]}))
    dry_input.write_text(json.dumps({"items": [{"candidate_hash": stable_hash(candidate.to_dict()),
                                               "next_actions": ["run_phase12_sub_account_request"]}]}))
    with using_office_profile(company):
        result = router.route_journal_batch_next_actions(batch_input_json=batch_input, dry_run_json=dry_input, output_dir=output)
        assert result.status == "ready" and result.routed_count == 1
        request = json.loads((output / "sub_account_requests/item_0000.local.json").read_text())
        assert request["company_slug"] == company.slug
        assert request["execution_principal"] == company.google_account
        assert request["required_tenant"] == company.legal_name
        assert request["registration_allowed"] is False
        assert result.items[0]["auto_resume_required"] is True
        forged_input = data / "foreign-request.local.json"
        source = json.loads((output / "sub_account_inputs/item_0000.local.json").read_text())
        forged_input.write_text(json.dumps({**source, "company_slug": "tenant-b"}))
        with pytest.raises(ValueError, match="company binding mismatch"):
            build_sub_account_create_candidate_draft(forged_input, data / "must-not-exist.local.json")
    assert not (data / "must-not-exist.local.json").exists()
    assert not company.moneyforward_db_path.exists()


def test_subaccount_router_rejects_foreign_drive_source_before_output(company):
    candidate = JournalCandidate("2030-01-03", "Synthetic Vendor", 1200, 0, 0.0,
                                 "Synthetic Expense", None, "Synthetic Liability", "Synthetic Purchases",
                                 "対象外", "Synthetic purchase")
    evidence = Evidence("synthetic-file", "synthetic.pdf", "a" * 64, parent_folder_id="foreign-source")
    output = company.moneyforward_db_path.parent / "must-not-exist"
    with using_office_profile(company), pytest.raises(ValueError, match="source Drive route"):
        router._route_sub_account(0, candidate.to_dict(), asdict(evidence), {}, output)
    assert not output.exists()
