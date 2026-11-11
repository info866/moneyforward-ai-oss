from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounting_api import build_trade_partner_create_request, normalize_invoice_registration_number
from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path


@dataclass(frozen=True)
class TradePartnerCreateRequestDraft:
    input_json: Path
    output_json: Path
    request_hash: str
    partner_name_hash: str
    invoice_registration_number_hash: str
    has_invoice_registration_number: bool
    next_action: str = "approve_and_execute_trade_partner_create_then_refetch_master"

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "ready_for_approval",
            "input_json": str(self.input_json),
            "output_json": str(self.output_json),
            "request_hash": self.request_hash,
            "partner_name_hash": self.partner_name_hash,
            "invoice_registration_number_hash": self.invoice_registration_number_hash,
            "has_invoice_registration_number": self.has_invoice_registration_number,
            "registration_allowed": False,
            "next_action": self.next_action,
            "moneyforward_api_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_name_printed": False,
            "raw_invoice_registration_number_printed": False,
            "raw_api_response_stored": False,
        }


@dataclass(frozen=True)
class TradePartnerCreateGrant:
    request_json: Path
    approval_json: Path
    output_json: Path
    request_hash: str
    approval_hash: str
    scope_hash: str
    dry_run_hash: str

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "grant_ready",
            "operation_type": "trade_partner_create",
            "request_json": str(self.request_json),
            "approval_json": str(self.approval_json),
            "output_json": str(self.output_json),
            "request_hash": self.request_hash,
            "target_hash": self.request_hash,
            "approval_hash": self.approval_hash,
            "scope_hash": self.scope_hash,
            "dry_run_hash": self.dry_run_hash,
            "write_execution_grant_ready": True,
            "registration_allowed": False,
            "next_action": "request_explicit_moneyforward_trade_partner_post_approval_then_refetch_master",
            "moneyforward_api_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_name_printed": False,
            "raw_invoice_registration_number_printed": False,
            "raw_secret_stored": False,
            "raw_api_response_stored": False,
        }


def build_trade_partner_create_request_draft(input_json: Path, output_json: Path) -> TradePartnerCreateRequestDraft:
    input_json = require_ignored_untracked_data_path(input_json, purpose="trade partner request input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="trade partner request output")
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("trade partner request input must be an object")
    name = str(payload.get("name") or "").strip()
    if not name or "TODO" in name or "PLACEHOLDER" in name:
        raise ValueError("name is required")
    raw_invoice_registration_number = payload.get("invoice_registration_number")
    normalized_invoice_registration_number = normalize_invoice_registration_number(
        str(raw_invoice_registration_number) if raw_invoice_registration_number else None
    )
    request = build_trade_partner_create_request(
        name=name,
        invoice_registration_number=normalized_invoice_registration_number,
        corporate_number=payload.get("corporate_number"),
        search_key=payload.get("search_key"),
        available=bool(payload.get("available", True)),
    )
    artifact = {
        "status": "ready_for_approval",
        "operation_type": "trade_partner_create",
        "request": request,
        "source": {
            "candidate_hash": payload.get("candidate_hash"),
            "source_file_id_hash": payload.get("source_file_id_hash"),
            "reason": payload.get("reason") or "missing_trade_partner_master",
        },
        "registration_allowed": False,
        "moneyforward_api_called": False,
        "keychain_accessed": False,
        "oauth_url_opened": False,
        "raw_secret_stored": False,
        "raw_api_response_stored": False,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    invoice_hash = mask_identifier(normalized_invoice_registration_number or "")
    return TradePartnerCreateRequestDraft(
        input_json=input_json,
        output_json=output_json,
        request_hash=stable_hash(request),
        partner_name_hash=mask_identifier(name),
        invoice_registration_number_hash=invoice_hash,
        has_invoice_registration_number=bool(normalized_invoice_registration_number),
    )


def build_trade_partner_create_grant(
    *,
    request_json: Path,
    approval_json: Path,
    output_json: Path,
) -> TradePartnerCreateGrant:
    request_json = require_ignored_untracked_data_path(request_json, purpose="trade partner create request input")
    approval_json = require_ignored_untracked_data_path(approval_json, purpose="trade partner create approval input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="trade partner create grant output")
    request_artifact = _load_object(request_json, "trade partner create request")
    approval = _load_object(approval_json, "trade partner create approval")
    if request_artifact.get("operation_type") != "trade_partner_create":
        raise ValueError("request operation_type must be trade_partner_create")
    request = request_artifact.get("request")
    if not isinstance(request, dict):
        raise ValueError("request artifact missing request object")
    request_hash = stable_hash(request)
    if approval.get("operation_type") != "trade_partner_create":
        raise ValueError("approval operation_type must be trade_partner_create")
    if str(approval.get("request_hash") or "") != request_hash:
        raise ValueError("approval request_hash does not match request artifact")
    scope_hash = str(approval.get("scope_hash") or "")
    if not scope_hash:
        raise ValueError("approval scope_hash is required")
    expected_action = str(approval.get("expected_api_action") or "")
    if "POST /api/v3/trade_partners" not in expected_action or "GET /api/v3/trade_partners" not in expected_action:
        raise ValueError("approval expected_api_action must include POST /api/v3/trade_partners and GET /api/v3/trade_partners")
    rollback = str(approval.get("rollback_method") or "")
    if "separate explicit approval" not in rollback and "別途承認" not in rollback:
        raise ValueError("approval rollback_method must require separate explicit approval")
    user_approval = str(approval.get("user_approval") or "")
    if len(user_approval.strip()) < 12:
        raise ValueError("approval user_approval is too weak")
    approval_hash = stable_hash(
        {
            "operation_type": "trade_partner_create",
            "request_hash": request_hash,
            "scope_hash": scope_hash,
            "expected_api_action": expected_action,
            "rollback_method": rollback,
            "user_approval_hash": stable_hash(user_approval),
        }
    )
    dry_run_hash = stable_hash(
        {
            "operation_type": "trade_partner_create",
            "request_hash": request_hash,
            "approval_hash": approval_hash,
            "scope_hash": scope_hash,
        }
    )
    grant = TradePartnerCreateGrant(
        request_json=request_json,
        approval_json=approval_json,
        output_json=output_json,
        request_hash=request_hash,
        approval_hash=approval_hash,
        scope_hash=scope_hash,
        dry_run_hash=dry_run_hash,
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(grant.to_redacted_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return grant


def _load_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload
