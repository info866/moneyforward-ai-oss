"""Explicit, immutable company configuration and fail-closed SQLite binding.

No provider credentials are loaded here. Schema v1 hashing stays compatible;
v2 runtime use additionally requires the complete boundary fingerprint column.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .hash_utils import stable_hash

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OFFICE_PROFILES_DIR = ROOT / "config" / "offices"
OFFICE_PROFILE_ENV = "MF_OFFICE_PROFILE"
_CURRENT: ContextVar[OfficeProfile | None] = ContextVar("office_profile", default=None)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DRIVE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_SECRET_KEYS = frozenset({"access_token", "refresh_token", "token", "api_key", "client_id",
    "client_secret", "office_id", "password", "secret", "credential", "credentials",
    "private_key", "webhook", "webhook_url", "authorization", "tokens", "secrets",
    "credential_json", "service_account_key"})


class OfficeProfileError(ValueError):
    """Profile is missing, unsafe, incomplete or inconsistent."""


def normalize_office_name(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            forbidden = {re.sub(r"[^a-z0-9]", "", k) for k in _SECRET_KEYS}
            if normalized in forbidden or normalized.endswith(("token", "clientsecret", "privatekey", "password")):
                raise OfficeProfileError("profile contains a forbidden secret/raw identity field")
            _reject_secret_fields(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secret_fields(child)
    elif isinstance(value, str) and ("-----BEGIN " in value or value.lower().startswith("bearer ")):
        raise OfficeProfileError("profile contains credential material")


@dataclass(frozen=True)
class OfficeProfile:
    schema_version: int
    slug: str
    legal_name: str
    display_name: str
    expected_office_names: tuple[str, ...]
    google_account: str
    moneyforward_keychain_service: str
    moneyforward_db_path: Path
    egov_keychain_service: str = ""
    google_chat_keychain_service: str = ""
    google_chat_keychain_account: str = ""
    google_drive_account: str = ""
    google_drive_display_root_name: str = ""
    google_drive_evidence_root_id: str = ""
    google_drive_evidence_source_folder_ids: tuple[str, ...] = ()
    google_drive_processed_folder_id: str | None = None
    google_drive_temporary_ocr_folder_id: str | None = None
    n8n_credential_namespace: str = ""
    n8n_workflow_namespace: str = ""
    source_path: Path = Path()
    repository_root: Path = ROOT
    configured: bool = False
    accounting_rules: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accounting_rules, Mapping):
            raise OfficeProfileError("accounting_rules must be a mapping")
        _reject_secret_fields(self.accounting_rules)
        object.__setattr__(self, "accounting_rules", _freeze(self.accounting_rules))
        object.__setattr__(self, "expected_office_names", tuple(self.expected_office_names))
        object.__setattr__(self, "google_drive_evidence_source_folder_ids", tuple(self.google_drive_evidence_source_folder_ids))
        object.__setattr__(self, "repository_root", Path(self.repository_root).resolve())
        object.__setattr__(self, "moneyforward_db_path", Path(self.moneyforward_db_path).resolve())

    @property
    def expected_office_name_keys(self) -> frozenset[str]:
        return frozenset(normalize_office_name(n) for n in self.expected_office_names)

    @property
    def profile_hash(self) -> str:
        # Keep this payload and canonical hashing unchanged for schema v1.
        return stable_hash({
            "schema_version": self.schema_version, "slug": self.slug,
            "legal_name": normalize_office_name(self.legal_name),
            "expected_office_names": sorted(normalize_office_name(n) for n in self.expected_office_names),
            "moneyforward_keychain_service": self.moneyforward_keychain_service,
            "moneyforward_db_path": self.moneyforward_db_path.relative_to(self.repository_root).as_posix(),
            "google_chat_keychain_service": self.google_chat_keychain_service,
            "google_drive_evidence_root_id": self.google_drive_evidence_root_id,
            "google_drive_processed_folder_id": self.google_drive_processed_folder_id or "",
        })

    @property
    def boundary_fingerprint(self) -> str:
        return stable_hash({
            "version": 2, "profile_hash": self.profile_hash,
            "configured": self.configured,
            "db_path": str(self.moneyforward_db_path),
            "rag": {"db_path": str(self.moneyforward_db_path), "profile_slug": self.slug},
            "workspace_account": self.google_account, "drive_account": self.google_drive_account,
            "chat_account": self.google_chat_keychain_account,
            "chat_service": self.google_chat_keychain_service,
            "egov_service": self.egov_keychain_service,
            "n8n_credentials": self.n8n_credential_namespace,
            "n8n_workflows": self.n8n_workflow_namespace,
            "drive_root": self.google_drive_evidence_root_id,
            "drive_sources": sorted(self.google_drive_evidence_source_folder_ids),
            "drive_processed": self.google_drive_processed_folder_id,
            "drive_temporary_ocr": self.google_drive_temporary_ocr_folder_id,
            "accounting_rules": _plain(self.accounting_rules),
        })

    def get_setting(self, path: str, default: Any = None) -> Any:
        """Look up dotted accounting rule keys, optionally prefixed accounting_rules."""
        parts = path.split(".")
        if parts[0] == "accounting_rules":
            parts = parts[1:]
        value: Any = self.accounting_rules
        for part in parts:
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value

    def require_configured(self) -> None:
        if self.schema_version != 2 or self.configured is not True:
            raise OfficeProfileError("external I/O requires an explicitly configured schema v2 profile")
        values = (self.slug, self.legal_name, self.moneyforward_keychain_service,
                  self.google_account, self.google_drive_account, self.google_drive_evidence_root_id,
                  *self.expected_office_names, *self.google_drive_evidence_source_folder_ids,
                  self.google_drive_processed_folder_id or "", self.google_drive_temporary_ocr_folder_id or "")
        if any(re.search(r"(?i)(placeholder|replace[_ -]?me|your[_ -]|example|changeme|<|\$\{)", v) for v in values):
            raise OfficeProfileError("example placeholders cannot authorize external I/O")
        if not _SLUG.fullmatch(self.slug) or not self.expected_office_names or not self.google_account or not self.google_drive_account or not self.moneyforward_keychain_service:
            raise OfficeProfileError("profile identity is incomplete")
        if normalize_office_name(self.legal_name) not in self.expected_office_name_keys:
            raise OfficeProfileError("legal name must match an expected office name")
        data_root = self.repository_root / "data" / "phase12" / "offices" / self.slug
        if data_root not in self.moneyforward_db_path.parents or self.moneyforward_db_path.resolve() != self.moneyforward_db_path:
            raise OfficeProfileError("DB path is outside the company data root or changed through a symlink")
        routes = self.google_drive_evidence_source_folder_ids
        outputs = tuple(v for v in (self.google_drive_processed_folder_id, self.google_drive_temporary_ocr_folder_id) if v)
        if (self.google_drive_evidence_root_id not in routes or len(set(routes)) != len(routes)
                or len(set(outputs)) != len(outputs) or set(routes).intersection(outputs)
                or any(not _DRIVE.fullmatch(v) for v in (*routes, *outputs))):
            raise OfficeProfileError("Drive routes are incomplete or overlap")

    def to_redacted_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slug": self.slug,
                "profile_hash": self.profile_hash, "boundary_fingerprint": self.boundary_fingerprint,
                "configured": self.configured, "raw_office_id_stored": False, "raw_secret_stored": False}


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficeProfileError("required profile string is missing")
    return value.strip()


def load_office_profile(slug: str, *, profiles_dir: Path | str | None = None,
                        repository_root: Path | str | None = None) -> OfficeProfile:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise OfficeProfileError("invalid office profile slug")
    root = Path(repository_root or ROOT).resolve()
    directory = Path(profiles_dir).resolve() if profiles_dir is not None else root / "config" / "offices"
    source = directory / f"{slug}.json"
    if source.resolve().parent != directory.resolve():
        raise OfficeProfileError("profile file escapes profiles directory")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfficeProfileError("office profile could not be read") from exc
    if not isinstance(payload, dict):
        raise OfficeProfileError("profile must be a JSON object")
    _reject_secret_fields(payload)
    version = payload.get("schema_version")
    if type(version) is not int or version not in (1, 2) or payload.get("slug") != slug:
        raise OfficeProfileError("invalid schema version or slug mismatch")
    def section(key: str, optional: bool = False) -> dict[str, Any]:
        value = payload.get(key, {} if optional else None)
        if not isinstance(value, dict):
            raise OfficeProfileError(f"{key} must be an object")
        return value
    mf, workspace, drive = section("moneyforward"), section("google_workspace"), section("google_drive")
    chat, egov, n8n = section("google_chat", True), section("egov", True), section("n8n", True)
    names = mf.get("expected_office_names")
    if not isinstance(names, list) or not names:
        raise OfficeProfileError("expected_office_names must be a nonempty list")
    legal_name = _text(payload.get("legal_name"))
    names = tuple(_text(n) for n in names)
    if normalize_office_name(legal_name) not in {normalize_office_name(n) for n in names}:
        raise OfficeProfileError("expected office names must include legal_name")
    relative = Path(_text(mf.get("db_path")))
    db = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or root / "data" / "phase12" / "offices" / slug not in db.parents:
        raise OfficeProfileError("DB must be under data/phase12/offices/<slug>/")
    sources = drive.get("evidence_source_folder_ids", [drive.get("evidence_root_id")])
    if not isinstance(sources, list) or not sources:
        raise OfficeProfileError("Drive source folders must be a nonempty list")
    if type(payload.get("configured", False)) is not bool:
        raise OfficeProfileError("configured must be a boolean")
    profile = OfficeProfile(
        schema_version=version, slug=slug, legal_name=legal_name, display_name=_text(payload.get("display_name")),
        expected_office_names=names, google_account=_text(workspace.get("account")),
        moneyforward_keychain_service=_text(mf.get("keychain_service")), moneyforward_db_path=db,
        google_drive_account=_text(drive.get("account")),
        google_drive_evidence_root_id=_text(drive.get("evidence_root_id")),
        google_drive_evidence_source_folder_ids=tuple(_text(v) for v in sources),
        google_drive_processed_folder_id=_text(drive["processed_folder_id"]) if drive.get("processed_folder_id") else None,
        google_drive_temporary_ocr_folder_id=_text(drive["temporary_ocr_folder_id"]) if drive.get("temporary_ocr_folder_id") else None,
        google_drive_display_root_name=str(drive.get("display_root_name", "")),
        google_chat_keychain_service=_text(chat["keychain_service"]) if chat else "",
        google_chat_keychain_account=_text(chat["keychain_account"]) if chat else "",
        egov_keychain_service=_text(egov["keychain_service"]) if egov else "",
        n8n_credential_namespace=str(n8n.get("credential_namespace", "")),
        n8n_workflow_namespace=str(n8n.get("workflow_namespace", "")),
        source_path=source, repository_root=root, configured=payload.get("configured", False),
        accounting_rules=section("accounting_rules", True))
    if profile.configured:
        profile.require_configured()
    return profile


def get_current_profile() -> OfficeProfile | None:
    return _CURRENT.get()


@contextmanager
def using_office_profile(profile: OfficeProfile) -> Iterator[OfficeProfile]:
    if not isinstance(profile, OfficeProfile):
        raise OfficeProfileError("an explicit OfficeProfile is required")
    token = _CURRENT.set(profile)
    try:
        yield profile
    finally:
        _CURRENT.reset(token)


def resolve_office_profile(value: str | OfficeProfile | None = None, *, required: bool = False,
                           profiles_dir: Path | str | None = None,
                           repository_root: Path | str | None = None) -> OfficeProfile | None:
    if isinstance(value, OfficeProfile):
        return value
    if value is None and get_current_profile() is not None:
        return get_current_profile()
    slug = value if value is not None else os.environ.get(OFFICE_PROFILE_ENV, "")
    if not slug:
        if required:
            raise OfficeProfileError("explicit office profile is required; no default company exists")
        return None
    return load_office_profile(slug, profiles_dir=profiles_dir, repository_root=repository_root)


def get_setting(path: str, default: Any = None) -> Any:
    return resolve_office_profile(required=True).get_setting(path, default)


def resolve_moneyforward_db_path(profile: OfficeProfile | None, explicit_path: Path | str | None = None,
                                 *, external_io: bool = False) -> Path:
    if profile is None:
        if external_io or explicit_path is None:
            raise OfficeProfileError("an explicit profile is required for external DB use")
        return Path(explicit_path).resolve()
    if external_io:
        profile.require_configured()
    expected = profile.moneyforward_db_path
    if expected.resolve() != expected or (explicit_path is not None and Path(explicit_path).resolve() != expected):
        raise OfficeProfileError("DB path does not match the selected profile")
    return expected


def resolve_moneyforward_keychain_service(profile: OfficeProfile | None, explicit_service: str | None = None,
                                          *, external_io: bool = False) -> str:
    if profile is None:
        if external_io or not explicit_service:
            raise OfficeProfileError("an explicit profile is required for external Keychain use")
        return explicit_service
    if external_io:
        profile.require_configured()
    if explicit_service is not None and explicit_service != profile.moneyforward_keychain_service:
        raise OfficeProfileError("Keychain service does not match the selected profile")
    return profile.moneyforward_keychain_service


def require_google_drive_evidence_root(profile: OfficeProfile, folder_id: str) -> None:
    profile.require_configured()
    if folder_id != profile.google_drive_evidence_root_id:
        raise OfficeProfileError("Drive evidence root mismatch")


def require_google_drive_evidence_source_folder(profile: OfficeProfile, folder_id: str) -> None:
    profile.require_configured()
    if folder_id not in profile.google_drive_evidence_source_folder_ids:
        raise OfficeProfileError("Drive source folder mismatch")


def _destination(profile: OfficeProfile, expected: str | None, value: str | None) -> str:
    profile.require_configured()
    if not expected or (value is not None and value != expected):
        raise OfficeProfileError("Drive destination is absent or mismatched")
    return expected


def require_google_drive_processed_folder(profile: OfficeProfile, folder_id: str | None = None) -> str:
    return _destination(profile, profile.google_drive_processed_folder_id, folder_id)


def require_google_drive_temporary_ocr_folder(profile: OfficeProfile, folder_id: str | None = None) -> str:
    return _destination(profile, profile.google_drive_temporary_ocr_folder_id, folder_id)


@dataclass(frozen=True)
class OfficeIdentityBinding:
    profile_slug: str
    profile_hash: str
    office_id: str = field(repr=False)
    office_id_hash: str
    legal_name_hash: str
    keychain_service_hash: str
    boundary_fingerprint: str = ""

    @property
    def binding_hash(self) -> str:
        return stable_hash({key: getattr(self, key) for key in (
            "profile_slug", "profile_hash", "office_id_hash", "legal_name_hash", "keychain_service_hash")})

    def assert_matches(self, profile: OfficeProfile, *, office_id: str | None = None) -> None:
        if self.profile_slug != profile.slug or self.profile_hash != profile.profile_hash:
            raise PermissionError("office identity profile mismatch")
        if self.keychain_service_hash != stable_hash(profile.moneyforward_keychain_service):
            raise PermissionError("office identity Keychain mismatch")
        if self.legal_name_hash not in {stable_hash(n) for n in profile.expected_office_name_keys}:
            raise PermissionError("office identity legal name mismatch")
        if self.office_id and stable_hash(self.office_id) != self.office_id_hash:
            raise PermissionError("office identity hash is inconsistent")
        if office_id is not None and stable_hash(str(office_id)) != self.office_id_hash:
            raise PermissionError("requested office identity mismatch")
        if self.boundary_fingerprint and self.boundary_fingerprint != profile.boundary_fingerprint:
            raise PermissionError("office identity boundary mismatch")

    def to_redacted_dict(self) -> dict[str, Any]:
        return {**{k: getattr(self, k) for k in ("profile_slug", "profile_hash", "office_id_hash",
                "legal_name_hash", "keychain_service_hash", "boundary_fingerprint")},
                "raw_office_id_stored": False, "raw_secret_stored": False}


def require_database_path(conn: sqlite3.Connection, profile: OfficeProfile) -> None:
    databases = conn.execute("PRAGMA database_list").fetchall()
    main = [row for row in databases if row[1] == "main"]
    if (len(main) != 1 or not main[0][2] or Path(main[0][2]).resolve() != profile.moneyforward_db_path
            or any(row[1] not in ("main", "temp") for row in databases)):
        raise PermissionError("connection must point exclusively to the exact profile DB")
    resolve_moneyforward_db_path(profile)


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        cursor = conn.execute("SELECT * FROM office_identity_bindings")
    except sqlite3.DatabaseError as exc:
        raise PermissionError("DB identity schema is missing") from exc
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def require_database_office_identity(conn: sqlite3.Connection, profile: OfficeProfile,
                                     binding: OfficeIdentityBinding | None = None) -> OfficeIdentityBinding:
    require_database_path(conn, profile)
    rows = _rows(conn)
    if len(rows) != 1 or rows[0]["status"] != "verified":
        raise PermissionError("DB must have exactly one verified office binding")
    row = rows[0]
    stored = OfficeIdentityBinding(**{k: str(row[k]) for k in (
        "profile_slug", "profile_hash", "office_id_hash", "legal_name_hash", "keychain_service_hash")},
        office_id="", boundary_fingerprint=str(row.get("boundary_fingerprint") or ""))
    stored.assert_matches(profile)
    if profile.schema_version == 2 and stored.boundary_fingerprint != profile.boundary_fingerprint:
        raise PermissionError("DB requires a matching v2 boundary fingerprint")
    if row.get("raw_office_id_stored", 0) != 0 or row.get("raw_secret_stored", 0) != 0:
        raise PermissionError("DB binding contains prohibited raw identity material")
    if binding is not None:
        binding.assert_matches(profile)
        if not binding.office_id or stored.binding_hash != binding.binding_hash:
            raise PermissionError("live office identity does not match DB binding")
        return binding
    return stored


def bind_database_office_identity(conn: sqlite3.Connection, profile: OfficeProfile,
                                 binding: OfficeIdentityBinding) -> None:
    """Bind an empty initialized DB after authoritative identity validation.

    Existing bindings are read-only and must already match. No table creation,
    migration, refresh, reset or re-binding is performed here.
    """
    profile.require_configured()
    require_database_path(conn, profile)
    binding.assert_matches(profile)
    if not binding.office_id or binding.boundary_fingerprint != profile.boundary_fingerprint:
        raise PermissionError("first binding requires live identity and complete boundary validation")
    if conn.in_transaction:
        raise PermissionError("first binding requires a clean transaction boundary")
    conn.execute("SAVEPOINT tenant_binding")
    try:
        if _rows(conn):
            require_database_office_identity(conn, profile, binding)
        else:
            # SQLite FTS shadow configuration contains engine metadata even when
            # its virtual business table is empty. Check the virtual table itself.
            shadow_tables = {r[1] for r in conn.execute("PRAGMA table_list") if r[2] == "shadow"}
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if table in {"office_identity_bindings", "schema_migrations"} or table.startswith("sqlite_") or table in shadow_tables:
                    continue
                quoted = table.replace('"', '""')
                if conn.execute(f'SELECT 1 FROM "{quoted}" LIMIT 1').fetchone() is not None:
                    raise PermissionError("first binding refuses a nonempty business/RAG DB")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(office_identity_bindings)")}
            if "boundary_fingerprint" not in columns:
                raise PermissionError("schema v2 requires boundary_fingerprint; no automatic migration")
            conn.execute("""INSERT INTO office_identity_bindings
                (profile_slug, profile_hash, office_id_hash, legal_name_hash, keychain_service_hash,
                 boundary_fingerprint, status, raw_office_id_stored, raw_secret_stored)
                VALUES (?, ?, ?, ?, ?, ?, 'verified', 0, 0)""",
                (binding.profile_slug, binding.profile_hash, binding.office_id_hash, binding.legal_name_hash,
                 binding.keychain_service_hash, profile.boundary_fingerprint))
        conn.execute("RELEASE tenant_binding")
    except BaseException:
        conn.execute("ROLLBACK TO tenant_binding")
        conn.execute("RELEASE tenant_binding")
        raise


def current_database_office_binding_hash(conn: sqlite3.Connection) -> str | None:
    rows = _rows(conn)
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["status"] != "verified":
        raise PermissionError("DB has ambiguous or revoked office bindings")
    return stable_hash({k: str(rows[0][k]) for k in (
        "profile_slug", "profile_hash", "office_id_hash", "legal_name_hash", "keychain_service_hash")})
