"""Optional company MCP: official reads plus opt-in local workflow metadata.

Requires the cloud extra and a configured, existing, company-bound database.
Local DAILY state is enabled with MF_CLOUD_LOCAL_WORKFLOW_ENABLED=1; it grants
no provider writes, official accounting verification, Cross Audit or delivery.
"""
from __future__ import annotations

import os
import sys
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from datetime import date
import re
from mf_phase12.chatgpt_cloud_contract import (
    requested_workflows, required_tools,
)
from mf_phase12.chatgpt_contract import execution_contract
from mf_phase12.workflow_bridge import WorkflowBridge
from mf_phase12.office_profile import (
    load_office_profile, using_office_profile, require_database_office_identity,
)
from mf_phase12.office_connection_probe import bind_company_accounting_client_from_keychain
from mf_phase12.shiwake_run import build_run_status


def _profile_path() -> Path:
    """No implicit tenant, env-supplied credentials, or profile fallback."""
    raw = os.environ.get("MF_CLOUD_ACCOUNTING_PROFILE", "")
    if not raw or not Path(raw).is_absolute():
        raise PermissionError("MF_CLOUD_ACCOUNTING_PROFILE must be an absolute company profile path")
    path = Path(raw)
    parent = ROOT / "config" / "offices"
    if path.is_symlink() or path.parent.resolve() != parent.resolve():
        raise PermissionError("profile must be a direct non-symlink file in config/offices")
    if not path.is_file() or path.suffix != ".json" or path.name == "example.json":
        raise PermissionError("an existing, non-example company profile is required")
    return path


def _load_profile():
    path = _profile_path()
    profile = load_office_profile(
        path.stem, profiles_dir=path.parent, repository_root=ROOT,
    )
    profile.require_configured()
    return profile


@contextmanager
def _bound_read() -> Iterator[tuple[Any, Any]]:
    """Authenticated office GET and existing DB binding precede all reads."""
    profile = _load_profile()
    with using_office_profile(profile):
        client, _office, _refresh = bind_company_accounting_client_from_keychain(
            write_enabled=False
        )
        # Refresh/binding already validates exactly one permitted official office.
        yield profile, client


RO = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                     idempotent_hint=True, open_world_hint=False)
mcp = MCPServer(
    "Company Money Forward (provider-read-only)",
    instructions=(
        "ChatGPT orchestrates the selected company's accounting. Use this "
        "connector for official reads and opt-in portable local DAILY run state. "
        "Local manifests are caller attestations. Do not claim journal posting, "
        "Cross Audit, or final Google Chat. Those operations require the "
        "company's existing approved run/write connector and authoritative "
        "read-back. Never switch to another company on an identity failure."
    ),
)


@mcp.tool(annotations=RO)
def get_capabilities() -> dict[str, Any]:
    """Static registry is diagnostic only; no external readiness is implied."""
    profile = _load_profile()
    tools = sorted(mcp._tool_manager._tools)
    return {
        "execution_contract": execution_contract(profile.slug),
        "available_tools": tools,
        "company_configured": "not_probed",
        "company_authentication": "not_probed",
        "write_capability": "not_provided",
        "run_completion_capability": "external_company_bridge_required",
        "local_workflow_state_enabled": os.environ.get("MF_CLOUD_LOCAL_WORKFLOW_ENABLED") == "1",
        "local_state_mutation_tools": ["run_begin", "run_status", "run_resume",
                                       "run_record_source_snapshot", "run_record_item"],
        "chatgpt_host_sync": "not_observable_from_server",
    }


@mcp.tool(annotations=RO)
def workflow_preflight(mode: str, workflow: str | None = None) -> dict[str, Any]:
    """Check this company's live read boundary; never claim operational PASS."""
    with _bound_read() as (profile, client):
        client.validate_office_identity()
        requested = [workflow] if workflow is not None else None
        selected = requested_workflows(mode, requested)
        available = set(mcp._tool_manager._tools)
        if os.environ.get("MF_CLOUD_LOCAL_WORKFLOW_ENABLED") != "1":
            available.difference_update({"run_begin", "run_resume",
                                         "run_record_source_snapshot", "run_record_item"})
        operations = {
            name: {
                "status": "available" if not missing else "needs_attention",
                "missing_tools": missing,
            }
            for name, expected in required_tools(mode, requested).items()
            for missing in [[tool for tool in expected if tool not in available]]
        }
        return {
            "mode": mode,
            "workflows": list(selected),
            "local_run_state_only": True,
            "local_run_modes": ["DAILY"],
            "local_metadata_is_official_source_evidence": False,
            "provider_write_capability": "not_provided",
            "operations": operations,
            "production_ready": False,
            "company_boundary": {
                "profile_slug": profile.slug,
                "boundary_fingerprint": profile.boundary_fingerprint,
            },
            "status": "needs_external_run_write_and_notification_tools",
            "accounting_api_connected": True,
            "actual_run_started": False,
        }


@mcp.tool(annotations=RO)
def prepare_run_scope(
    mode: str, run_id: str, accounting_date: str, workflow: str | None = None,
) -> dict[str, Any]:
    """Produce one typed request; NOT an authoritative run_begin read-back."""
    profile = _load_profile()
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run_id must be a printable, path-free identifier")
    try:
        if date.fromisoformat(accounting_date).isoformat() != accounting_date:
            raise ValueError("accounting_date must be YYYY-MM-DD")
    except (TypeError, ValueError) as error:
        raise ValueError("accounting_date must be YYYY-MM-DD") from error
    selected = requested_workflows(mode, [workflow] if workflow else None)
    return {
        "status": "scope_draft", "run_started": False,
        "mode": mode, "run_id": run_id, "accounting_date": accounting_date,
        "company_boundary": {
            "profile_slug": profile.slug, "boundary_fingerprint": profile.boundary_fingerprint,
        },
        "source_scope": {"selected_workflows": list(selected)},
        "production_authorized": False,
    }


@mcp.tool(annotations=RO)
def get_office() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.offices()


@mcp.tool(annotations=RO)
def get_term_settings() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.term_settings()


@mcp.tool(annotations=RO)
def get_accounts() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.accounts()


@mcp.tool(annotations=RO)
def get_sub_accounts() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.sub_accounts()


@mcp.tool(annotations=RO)
def get_taxes() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.taxes()


@mcp.tool(annotations=RO)
def get_trade_partners() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.trade_partners()


@mcp.tool(annotations=RO)
def get_connected_accounts() -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.connected_accounts()


@mcp.tool(annotations=RO)
def get_journals(start_date: str, end_date: str, page: int = 1,
                 per_page: int = 100) -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.journals(start_date=start_date, end_date=end_date,
                               page=page, per_page=per_page)


@mcp.tool(annotations=RO)
def get_journal(journal_id: str) -> dict[str, Any]:
    if not journal_id or len(journal_id) > 150:
        raise ValueError("journal_id is required")
    with _bound_read() as (_profile, client):
        return client.journal(journal_id)


@mcp.tool(annotations=RO)
def get_transactions(start_date: str, end_date: str, page: int = 1,
                     per_page: int = 100, status: str = "none") -> dict[str, Any]:
    if status not in {"none", "registered", "modified", "excluded", "new_voucher_attached"}:
        raise ValueError("unsupported transaction status")
    with _bound_read() as (_profile, client):
        return client.transactions(start_date=start_date, end_date=end_date,
                                   page=page, per_page=per_page,
                                   journalizing_statuses=(status,))


@mcp.tool(annotations=RO)
def get_trial_balance_bs(start_date: str, end_date: str) -> dict[str, Any]:
    with _bound_read() as (_profile, client):
        return client.trial_balance_bs(start_date=start_date, end_date=end_date)


@mcp.tool(annotations=RO)
def get_trial_balance_pl(start_date: str, end_date: str) -> dict[str, Any]:
    from urllib.parse import urlencode
    with _bound_read() as (_profile, client):
        return client._get_json(
            "/api/v3/reports/trial_balance_pl?" + urlencode({
                "start_date": start_date, "end_date": end_date,
            })
        )


# These tools change local metadata (including the company lock file), never
# provider journals or notifications. Explicit opt-in preserves read-only default.
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                        idempotent_hint=False, open_world_hint=False)


def _local_bridge():
    if os.environ.get("MF_CLOUD_LOCAL_WORKFLOW_ENABLED") != "1":
        raise PermissionError("local workflow state is disabled; explicitly enable MF_CLOUD_LOCAL_WORKFLOW_ENABLED")
    return WorkflowBridge(_load_profile())


def _invoke_local(operation: str, *args: Any) -> dict[str, Any]:
    """Preserve actionable *redacted* failures through real MCP SDK dispatch.

    Plain ValueError/PermissionError becomes an opaque UnexpectedToolError in
    the SDK. Do not return success-shaped JSON, nor expose arbitrary exception
    messages that could contain a local profile path or company identity.
    """
    try:
        return getattr(_local_bridge(), operation)(*args)
    except (ValueError, PermissionError, sqlite3.Error, OSError, RuntimeError) as error:
        message = str(error)
        if isinstance(error, RuntimeError) and "another production aggregate run is executing" in message:
            reason = "company_run_busy: retry the same retained request_id after the current local operation finishes"
        elif "local workflow state is disabled" in message:
            reason = "local_state_disabled: enable local workflow metadata explicitly; no provider writes are enabled"
        elif "outside recorded run scope" in message or "workflow scope mismatch" in message:
            reason = "workflow_outside_run_scope: use the workflow bound to this run"
        elif "official reconciliation required" in message:
            reason = "official_reconciliation_required: do not retry an uncertain provider write"
        elif "VERIFIED unsupported" in message:
            reason = "VERIFIED_requires_official_readback: local decisions cannot certify accounting completion"
        elif "integrity" in message:
            reason = "local_state_integrity_failed: inspect the existing bound ledger; do not reset it"
        elif isinstance(error, sqlite3.OperationalError):
            reason = "existing_company_database_required: configure and bind this company database before local run use"
        elif isinstance(error, PermissionError):
            reason = "company_boundary_not_verified: check this company's profile and existing database binding"
        else:
            reason = "local_run_request_rejected: verify required inputs, existing run, source snapshot and mode; no provider write occurred"
        raise ToolError(reason) from None


@mcp.tool(annotations=LOCAL)
def run_begin(instruction: str, accounting_date: str, source_scope: dict[str, Any],
              request_id: str, mode: str = "DAILY") -> dict[str, Any]:
    """Opt-in LOCAL state mutation; one DAILY workflow, no provider writes."""
    return _invoke_local("run_begin", instruction, accounting_date, source_scope, mode, request_id)


@mcp.tool(annotations=LOCAL)
def run_status(run_id: str) -> dict[str, Any]:
    """Portable local run status; lock only, no live identity attestation."""
    if os.environ.get("MF_CLOUD_LOCAL_WORKFLOW_ENABLED") == "1":
        return _invoke_local("run_status", run_id)
    # Preserve the original authenticated read-only historical ledger route.
    with _bound_read() as (profile, client):
        with sqlite3.connect(profile.moneyforward_db_path.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            require_database_office_identity(conn, profile, client.office_identity_binding)
            status = build_run_status(conn, run_id)
        return {**status, "company_boundary": {
                    "profile_slug": profile.slug,
                    "boundary_fingerprint": profile.boundary_fingerprint},
                "authoritative_cloud_run_scope": False,
                "write_or_completion_authorized": False}


@mcp.tool(annotations=LOCAL)
def run_resume(run_id: str) -> dict[str, Any]:
    """Return pending work only; never execute or retry provider writes."""
    return _invoke_local("run_resume", run_id)


@mcp.tool(annotations=LOCAL)
def run_record_source_snapshot(run_id: str, workflow: str,
                               source_identity_hashes: list[str], coverage_hash: str) -> dict[str, Any]:
    """Persist caller discovery attestation, not official source verification."""
    return _invoke_local("run_record_source_snapshot", run_id, workflow, source_identity_hashes, coverage_hash)


@mcp.tool(annotations=LOCAL)
def run_record_item(run_id: str, workflow: str, source_identity_hash: str,
                    state: str, decision: dict[str, Any], current_facts_hash: str) -> dict[str, Any]:
    """Record local exception/decision. Accounting VERIFIED is unsupported."""
    return _invoke_local("run_record_item", run_id, workflow, source_identity_hash,
                         state, decision, current_facts_hash)


if __name__ == "__main__":
    _load_profile()  # refuse to open a listener for an unconfigured tenant
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.environ.get("MF_CLOUD_ACCOUNTING_PORT", "8766")),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
