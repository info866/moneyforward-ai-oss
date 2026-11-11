"""Provider-neutral ChatGPT orchestration policy; pure and offline.

This is NOT a Money Forward connector, credential verifier, run ledger or
production authorization. Each company supplies its own existing adapters.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

DAILY_WORKFLOWS = (
    "sales_invoice", "payable_invoice", "all_receipt_folders", "bank_transfer", "card",
)
MODE_WORKFLOWS = {
    "DAILY": DAILY_WORKFLOWS,
    "PAYROLL": ("payroll_bonus",),
    "MONTHLY_CLOSE": DAILY_WORKFLOWS + ("payroll_bonus",),
}
DEFERRED_WORKFLOWS = ("tax_insurance",)
_COMPANY_SLUG = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")


def _company(company_slug: str) -> str:
    if not isinstance(company_slug, str) or not _COMPANY_SLUG.fullmatch(company_slug):
        raise ValueError("valid explicit company_slug is required")
    return company_slug


def requested_workflows(mode: str, selected_workflows: Sequence[str] | None = None) -> tuple[str, ...]:
    """Resolve the scope *before* touching a company service.

    DAILY requires exactly one workflow per new run. PAYROLL and MONTHLY_CLOSE
    are separate modes and never get silently folded into DAILY.
    """
    if mode not in MODE_WORKFLOWS:
        raise ValueError("unsupported cloud accounting mode")
    allowed = MODE_WORKFLOWS[mode]
    if selected_workflows is None:
        if mode == "DAILY":
            raise ValueError("DAILY requires exactly one selected workflow per run")
        return allowed
    if isinstance(selected_workflows, (str, bytes)) or not isinstance(selected_workflows, (tuple, list)):
        raise ValueError("selected_workflows must be a list or tuple")
    selected = tuple(selected_workflows)
    if not selected or any(not isinstance(w, str) for w in selected) or len(set(selected)) != len(selected) or any(w not in allowed for w in selected):
        raise ValueError("invalid or duplicated workflow selection")
    if mode == "DAILY" and len(selected) != 1:
        raise ValueError("DAILY requires exactly one selected workflow per run")
    if mode != "DAILY" and set(selected) != set(allowed):
        raise ValueError("non-DAILY mode must retain its complete authorized scope")
    return selected


def execution_contract(company_slug: str) -> dict[str, Any]:
    """Advertise a safe target contract, never claim external readiness."""
    return {
        "company_slug": _company(company_slug),
        "orchestrator": "chatgpt",
        "modes": {name: list(workflows) for name, workflows in MODE_WORKFLOWS.items()},
        "daily_run_scope": "one_workflow_per_independent_run",
        "payroll_in_daily": False,
        "monthly_accounting_writes": False,
        "deferred_workflows": list(DEFERRED_WORKFLOWS),
        "notification": "one_final_google_chat_per_verified_run",
        "resume": "same_run_no_duplicate_writes_or_notification",
        "identity": "official_office_plus_authenticated_principal_plus_company_db_rag_drive",
        "optional_metadata": "diagnostic_only_not_a_global_freeze",
        "local_cli": "maintenance_and_tests_only",
        "backend_and_oauth_verified": False,
        "production_ready": False,
    }


def verify_run_scope(
    company_slug: str, mode: str, selected_workflows: Sequence[str] | None,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed on a mismatched run. Scope is NOT office/OAuth attestation.

    Use only after a trusted, company-bound MCP has independently validated
    the live provider, DB, RAG and Drive bindings. No business write is granted
    by this helper, even when the response scope matches.
    """
    company = _company(company_slug)
    selected = requested_workflows(mode, selected_workflows)
    if not isinstance(response, Mapping):
        raise PermissionError("missing authoritative run response")
    if response.get("profile_slug") != company or response.get("project_mode") != mode:
        raise PermissionError("company or mode mismatch")
    run_id = response.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise PermissionError("run id missing")
    returned = response.get("workflows")
    if isinstance(returned, Mapping):
        actual = tuple(returned.keys())
    elif isinstance(returned, list) and all(isinstance(w, str) for w in returned):
        actual = tuple(returned)
    else:
        raise PermissionError("run scope is missing or ambiguous")
    if len(actual) != len(set(actual)) or set(actual) != set(selected):
        raise PermissionError("run scope mismatch; no business write")
    return {
        "status": "scope_verified_only",
        "company_slug": company,
        "project_mode": mode,
        "workflows": list(actual),
        "run_id": run_id,
        "office_and_auth_verified": False,
        "business_write_authorized": False,
    }


def required_tools(mode: str, selected_workflows: Sequence[str] | None = None) -> dict[str, list[str]]:
    """Capability inventory. Missing unrelated tools must not freeze every job."""
    selected = requested_workflows(mode, selected_workflows)
    common = ("get_office", "workflow_preflight", "run_begin", "run_status", "run_resume")
    final = ("cross_audit", "final_google_chat_send")
    result: dict[str, list[str]] = {}
    for workflow in selected:
        names = list(common + final)
        if mode == "MONTHLY_CLOSE":
            names += ["get_journals", "get_trial_balance_bs", "get_trial_balance_pl"]
        elif workflow in ("bank_transfer", "card"):
            names += ["get_transactions", "get_connected_accounts"]
        elif workflow == "sales_invoice":
            names += ["invoice_list", "invoice_get", "invoice_accounting_link_status"]
        elif workflow == "payroll_bonus":
            names += ["payroll_readiness", "payroll_wage_ledger"]
        result[workflow] = list(dict.fromkeys(names))
    return result
