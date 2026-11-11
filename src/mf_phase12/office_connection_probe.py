"""Read-only identity discovery for the selected company."""
from __future__ import annotations

from typing import Any
from dataclasses import asdict, dataclass
from pathlib import Path
import json

from .accounting_api import MoneyForwardAccountingClient, MoneyForwardOAuthClient
from .keychain import KeychainClient
from .office_profile import resolve_office_profile, resolve_moneyforward_keychain_service
from .hash_utils import stable_hash
from .local_artifacts import require_ignored_untracked_data_path


@dataclass(frozen=True)
class OfficeConnectionProbeResult:
    status: str
    accounting_api_connected: bool
    target_office_verified: bool
    office_count: int
    matched_office_count: int
    expected_office_hash: str
    matched_office_id_hash: str | None
    output_json: str | None = None
    moneyforward_write_called: bool = False
    raw_api_response_stored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_office_connection_probe(*, accounting_client: MoneyForwardAccountingClient,
        expected_office_name: str, output_json: Path | None = None) -> OfficeConnectionProbeResult:
    resolve_expected_office_id(accounting_client, expected_office_name=expected_office_name)
    binding = accounting_client.office_identity_binding
    result = OfficeConnectionProbeResult('ready', True, True, 1, 1,
        stable_hash(expected_office_name), binding.office_id_hash,
        str(output_json) if output_json else None)
    if output_json:
        path = require_ignored_untracked_data_path(output_json, purpose='office connection readback')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False) + '\n')
    return result


def resolve_expected_office_id(accounting_client: MoneyForwardAccountingClient, *, expected_office_name: str | None = None) -> str:
    profile = accounting_client.office_profile
    if profile is None:
        raise PermissionError("An office profile is required")
    if expected_office_name is not None and expected_office_name not in profile.expected_office_names:
        raise PermissionError("The requested legal entity is outside this profile")
    return accounting_client.validate_office_identity().office_id


def bind_company_accounting_client(access_token: str, *, keychain_service: str, write_enabled: bool = False) -> tuple[MoneyForwardAccountingClient, str]:
    profile = resolve_office_profile(required=True)
    resolve_moneyforward_keychain_service(profile, keychain_service, external_io=True)
    client = MoneyForwardAccountingClient(access_token, office_profile=profile,
        keychain_service=keychain_service, db_path=profile.moneyforward_db_path,
        write_enabled=write_enabled)
    return client, resolve_expected_office_id(client)


def bind_company_accounting_client_from_keychain(*, keychain_service: str | None = None, write_enabled: bool = False) -> tuple[MoneyForwardAccountingClient, str, Any | None]:
    profile = resolve_office_profile(required=True)
    service = resolve_moneyforward_keychain_service(profile, keychain_service, external_io=True)
    keychain = KeychainClient(service)
    token = keychain.get_secret("mf_oauth_access_token")
    try:
        client, identifier = bind_company_accounting_client(token, keychain_service=service, write_enabled=write_enabled)
        return client, identifier, None
    except RuntimeError as error:
        if "GET /api/v3/offices HTTP 401" not in str(error):
            raise
    oauth = MoneyForwardOAuthClient(keychain.get_secret("mf_oauth_client_id"), keychain.get_secret("mf_oauth_client_secret"))
    refreshed = oauth.refresh_access_token(keychain.get_secret("mf_oauth_refresh_token"))
    client, identifier = bind_company_accounting_client(refreshed.access_token, keychain_service=service, write_enabled=write_enabled)
    # Store rotation only after authoritative office verification.
    keychain.set_secret("mf_oauth_access_token", refreshed.access_token)
    if refreshed.refresh_token:
        keychain.set_secret("mf_oauth_refresh_token", refreshed.refresh_token)
    return client, identifier, refreshed
