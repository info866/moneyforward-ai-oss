from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .office_profile import get_current_profile


@dataclass(frozen=True)
class SubAccountCreateCandidateDraft:
    input_json: Path
    output_json: Path
    candidate_hash: str
    account_name_hash: str
    sub_account_name_hash: str
    next_action: str = "create_in_authenticated_company_then_refetch_master_and_resume_batch"

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": "creation_and_resume_required",
            "input_json": str(self.input_json),
            "output_json": str(self.output_json),
            "candidate_hash": self.candidate_hash,
            "account_name_hash": self.account_name_hash,
            "sub_account_name_hash": self.sub_account_name_hash,
            "api_create_available": False,
            "registration_allowed": False,
            "user_confirmation_required": False,
            "auto_resume_required": True,
            "next_action": self.next_action,
            "moneyforward_api_called": False,
            "keychain_accessed": False,
            "oauth_url_opened": False,
            "raw_secret_stored": False,
            "raw_name_printed": False,
            "raw_api_response_stored": False,
        }


def build_sub_account_create_candidate_draft(input_json: Path, output_json: Path) -> SubAccountCreateCandidateDraft:
    input_json = require_ignored_untracked_data_path(input_json, purpose="sub account candidate input")
    output_json = require_ignored_untracked_data_path(output_json, purpose="sub account candidate output")
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sub account candidate input must be an object")
    account_name = _required_name(payload.get("account_name"), "account_name")
    sub_account_name = _required_name(payload.get("sub_account_name"), "sub_account_name")
    profile = get_current_profile()
    if not profile.google_account or not profile.legal_name:
        raise ValueError("company profile identity is incomplete")
    if payload.get("company_slug") != profile.slug:
        raise ValueError("sub account request company binding mismatch")
    candidate = {
        "company_slug": profile.slug,
        "status": "creation_and_resume_required",
        "operation_type": "sub_account_create_candidate",
        "account_name": account_name,
        "sub_account_name": sub_account_name,
        "reason": payload.get("reason") or "missing_sub_account_master",
        "source": {
            "candidate_hash": payload.get("candidate_hash"),
            "source_file_id_hash": payload.get("source_file_id_hash"),
        },
        "api_create_available": False,
        "registration_allowed": False,
        "execution_principal": get_current_profile().google_account,
        "required_tenant": get_current_profile().legal_name,
        "fallback": "verified_authenticated_ui",
        "user_confirmation_required": False,
        "auto_resume_required": True,
        "completion_readback": "sub_account_present_in_refetched_moneyforward_master",
        "moneyforward_api_called": False,
        "keychain_accessed": False,
        "oauth_url_opened": False,
        "raw_secret_stored": False,
        "raw_api_response_stored": False,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SubAccountCreateCandidateDraft(
        input_json=input_json,
        output_json=output_json,
        candidate_hash=stable_hash(candidate),
        account_name_hash=mask_identifier(account_name),
        sub_account_name_hash=mask_identifier(sub_account_name),
    )


def _required_name(value: object, field_name: str) -> str:
    name = str(value or "").strip()
    if not name or "TODO" in name or "PLACEHOLDER" in name:
        raise ValueError(f"{field_name} is required")
    return name
