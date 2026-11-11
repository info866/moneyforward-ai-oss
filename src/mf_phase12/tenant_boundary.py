"""Preflight for business I/O. Only client identity readback may perform I/O.

Provider principals must come from the caller's authenticated provider session,
never from user input or environment. RAG is always in the bound accounting DB.
Call again when the client/session/route changes; a result is not a bearer grant.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Protocol

from .office_profile import (
    OfficeIdentityBinding, OfficeProfile, OfficeProfileError,
    bind_database_office_identity, require_database_office_identity, require_database_path,
    require_google_drive_evidence_root, require_google_drive_evidence_source_folder,
    require_google_drive_processed_folder, require_google_drive_temporary_ocr_folder,
    resolve_moneyforward_db_path, resolve_moneyforward_keychain_service, resolve_office_profile,
)


class IdentityClient(Protocol):
    office_profile: OfficeProfile | None
    # Credential-source provenance supplied by the credential loader, not copied
    # from profile configuration to make a mismatched client appear valid.
    keychain_service: str

    def validate_office_identity(self, *, office_id: str | None = None) -> OfficeIdentityBinding: ...


@dataclass(frozen=True)
class TenantBoundary:
    profile_slug: str
    boundary_fingerprint: str
    binding: OfficeIdentityBinding
    db_path: Path
    rag_db_path: Path


def validate_tenant_boundary(
    profile: OfficeProfile | str | None = None, *, conn: sqlite3.Connection,
    client: IdentityClient, keychain_service: str, provider_principals: Mapping[str, str],
    drive_routes: Mapping[str, str], db_path: Path | str | None = None,
    rag_db_path: Path | str | None = None, office_id: str | None = None,
    allow_new_binding: bool = False,
) -> TenantBoundary:
    """Validate every local boundary, then authoritative token offices readback.

    Routes: evidence_root / evidence_source / processed / temporary_ocr.
    Principals: google_workspace / google_drive. A Google Chat Keychain account
    is a credential storage label, not an authenticated provider principal.
    No environment switch can disable a check or enable first-time binding.
    """
    selected = resolve_office_profile(profile, required=True)
    selected.require_configured()
    expected_db = resolve_moneyforward_db_path(selected, db_path, external_io=True)
    resolve_moneyforward_keychain_service(selected, keychain_service, external_io=True)
    if getattr(client, "keychain_service", None) != keychain_service:
        raise PermissionError("client credential Keychain source is missing or mismatched")
    require_database_path(conn, selected)
    if rag_db_path is not None and Path(rag_db_path).resolve() != expected_db:
        raise PermissionError("RAG must live within the bound company DB")
    expected_principals = {"google_workspace": selected.google_account, "google_drive": selected.google_drive_account}
    if dict(provider_principals) != expected_principals:
        raise PermissionError("authenticated provider principals do not match the profile")
    guards = {"evidence_root": require_google_drive_evidence_root,
              "evidence_source": require_google_drive_evidence_source_folder,
              "processed": require_google_drive_processed_folder,
              "temporary_ocr": require_google_drive_temporary_ocr_folder}
    if not drive_routes or set(drive_routes) - guards.keys():
        raise OfficeProfileError("explicit supported Drive routes are required")
    for role, folder in drive_routes.items():
        if not isinstance(folder, str) or not folder:
            raise OfficeProfileError("Drive route must be explicit")
        guards[role](selected, folder)
    if client.office_profile is None or client.office_profile.boundary_fingerprint != selected.boundary_fingerprint:
        raise PermissionError("client profile does not match selected boundary")
    if not allow_new_binding:
        require_database_office_identity(conn, selected)
    # This must call the authoritative offices endpoint, not compare token strings
    # or reuse an unverified cached binding. Business API calls follow this function.
    live = client.validate_office_identity(office_id=office_id)
    if not isinstance(live, OfficeIdentityBinding) or not live.office_id:
        raise PermissionError("authoritative offices readback did not return a live binding")
    live.assert_matches(selected, office_id=office_id)
    live = replace(live, boundary_fingerprint=selected.boundary_fingerprint)
    if allow_new_binding:
        bind_database_office_identity(conn, selected, live)
    require_database_office_identity(conn, selected, live)
    return TenantBoundary(selected.slug, selected.boundary_fingerprint, live, expected_db, expected_db)
