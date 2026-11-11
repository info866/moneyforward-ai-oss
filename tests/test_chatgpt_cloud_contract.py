"""Cloud-run scope checks use synthetic responses, never production services."""
import pytest

from mf_phase12.chatgpt_cloud_contract import (
    DAILY_WORKFLOWS, execution_contract, requested_workflows, required_tools, verify_run_scope,
)


def _run():
    return {"profile_slug": "alpha", "project_mode": "DAILY",
            "run_id": "synthetic-run", "workflows": ["sales_invoice"]}


def test_cloud_policy_requires_external_proof():
    contract = execution_contract("alpha")
    assert contract["orchestrator"] == "chatgpt"
    assert contract["production_ready"] is False
    assert contract["backend_and_oauth_verified"] is False
    assert contract["payroll_in_daily"] is False
    assert contract["monthly_accounting_writes"] is False
    assert contract["modes"]["DAILY"] == list(DAILY_WORKFLOWS)


@pytest.mark.parametrize("selection", [
    None, [], ["sales_invoice", "card"], ["sales_invoice", "sales_invoice"],
    ["payroll_bonus"], ["tax_insurance"], [123],
])
def test_daily_requires_exactly_one_valid_workflow(selection):
    with pytest.raises(ValueError):
        requested_workflows("DAILY", selection)


def test_separate_payroll_monthly_modes():
    assert requested_workflows("DAILY", ["card"]) == ("card",)
    assert requested_workflows("PAYROLL") == ("payroll_bonus",)
    assert len(requested_workflows("MONTHLY_CLOSE")) == 6
    with pytest.raises(ValueError):
        requested_workflows("PAYROLL", ["sales_invoice"])
    with pytest.raises(ValueError):
        requested_workflows("MONTHLY_CLOSE", ["card"])


def test_matching_scope_never_authorizes_company_io():
    result = verify_run_scope("alpha", "DAILY", ["sales_invoice"], _run())
    assert result["status"] == "scope_verified_only"
    assert result["office_and_auth_verified"] is False
    assert result["business_write_authorized"] is False


@pytest.mark.parametrize("changes", [
    {"profile_slug": "other"},
    {"project_mode": "PAYROLL"},
    {"run_id": ""},
    {"workflows": ["sales_invoice", "card"]},
    {"workflows": []},
    {"workflows": None},
])
def test_mismatched_scope_fails_closed(changes):
    response = {**_run(), **changes}
    with pytest.raises(PermissionError):
        verify_run_scope("alpha", "DAILY", ["sales_invoice"], response)


def test_per_workflow_capability_inventory():
    sales = required_tools("DAILY", ["sales_invoice"])["sales_invoice"]
    bank = required_tools("DAILY", ["bank_transfer"])["bank_transfer"]
    assert "invoice_get" in sales and "get_transactions" not in sales
    assert "get_transactions" in bank and "invoice_get" not in bank
    assert "cross_audit" in sales and "final_google_chat_send" in bank


def test_bootstrap_copies_cloud_policy_and_own_code(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location("starter_bootstrap", root / "scripts/bootstrap_company.py")
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    dest = tmp_path / "tenant-alpha"
    profile = module.bootstrap("alpha", dest)
    assert profile.is_file()
    assert (dest / "docs/skills/chatgpt_project_shiwake.md").is_file()
    assert (dest / "src/mf_phase12/chatgpt_cloud_contract.py").is_file()
    assert not (dest / "data").exists()
