from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Protocol

from .hash_utils import mask_identifier, stable_hash
from .models import JournalCandidate
from .office_profile import (
    OfficeIdentityBinding, OfficeProfile, normalize_office_name,
    require_database_office_identity, resolve_office_profile,
    resolve_moneyforward_db_path, resolve_moneyforward_keychain_service,
)
from pathlib import Path


ACCOUNTING_BASE_URL = "https://api-accounting.moneyforward.com"
PAYABLE_GATEWAY_BASE_URL = "https://api.payable.moneyforward.com"
PAYABLE_EXTERNAL_BASE_URL = "https://payable.moneyforward.com"
AUTHORIZE_URL = "https://api.biz.moneyforward.com/authorize"
TOKEN_URL = "https://api.biz.moneyforward.com/token"
PAYABLE_AUTHORIZE_URL = "https://payable.moneyforward.com/oauth/authorize"
PAYABLE_TOKEN_URL = "https://payable.moneyforward.com/oauth/token"
ACCOUNTING_TRANSACTION_READ_SCOPE = "mfc/accounting/transaction.read"
PHASE12_READ_ONLY_SCOPES = (
    "mfc/invoice/data.read",
    "mfc/accounting/offices.read",
    "mfc/accounting/accounts.read",
    "mfc/accounting/departments.read",
    "mfc/accounting/taxes.read",
    "mfc/accounting/journal.read",
    "mfc/accounting/report.read",
    "mfc/accounting/trade_partners.read",
    "mfc/accounting/connected_account.read",
    ACCOUNTING_TRANSACTION_READ_SCOPE,
)
INVOICE_SOURCE_TYPES = frozenset({"invoice", "bill", "purchase_invoice", "received_invoice"})
MONEY_FORWARD_JOURNAL_TYPE = "journal_entry"
INVOICE_KIND_QUALIFIED = "INVOICE_KIND_QUALIFIED"
INVOICE_KIND_NOT_TARGET = "INVOICE_KIND_NOT_TARGET"
INVOICE_KIND_UNQUALIFIED_80 = "INVOICE_KIND_UNQUALIFIED_80"
_INVOICE_KIND_ALIASES = {
    "qualified": INVOICE_KIND_QUALIFIED,
    "not_target": INVOICE_KIND_NOT_TARGET,
    "not-target": INVOICE_KIND_NOT_TARGET,
    "unqualified_80": INVOICE_KIND_UNQUALIFIED_80,
    INVOICE_KIND_QUALIFIED: INVOICE_KIND_QUALIFIED,
    INVOICE_KIND_NOT_TARGET: INVOICE_KIND_NOT_TARGET,
    INVOICE_KIND_UNQUALIFIED_80: INVOICE_KIND_UNQUALIFIED_80,
}

_SAFE_API_ERROR_MESSAGE_REASONS = {
    "Accounting period doesn't exist for the fiscal year.": "accounting_period_missing",
    "Start date is after end date.": "start_date_after_end_date",
    "Given date is not matching any accounting periods.": "date_has_no_accounting_period",
    "Given date is outside the accounting period.": "date_outside_accounting_period",
    "The parameters cannot be provided simultaneously.": "incompatible_parameters",
    "Please specify either start_date or end_date.": "date_parameter_pair_incomplete",
    "Please specify creditor and debitor.": "creditor_or_debitor_missing",
    "Please specify memo of 200 characters or less.": "memo_too_long",
    "The given id does not exist for this office.": "office_master_id_not_found",
    "Journal cannot be moved to a different term.": "journal_term_move_forbidden",
    "The specified account is inactive.": "account_inactive",
}


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]: ...


class MoneyForwardRequestRejectedError(RuntimeError):
    """A deterministic HTTP rejection whose response contains no write result."""

    def __init__(self, *, status: int, payload: bytes):
        self.status = status
        self.response_hash = stable_hash(payload.decode("utf-8", errors="replace"))
        self.error_codes, self.target_fields = _redacted_api_error_details(payload)
        self.error_reasons = _redacted_api_error_reasons(payload)
        code_text = ",".join(self.error_codes) or "unknown_error"
        target_text = ",".join(self.target_fields) or "unknown_target"
        reason_text = ",".join(self.error_reasons) or "unknown_reason"
        super().__init__(
            f"Money Forward API rejected the request: HTTP {status}; "
            f"codes={code_text}; reasons={reason_text}; targets={target_text}; "
            f"response_hash={self.response_hash[:16]}"
        )

    @property
    def reason_code(self) -> str:
        if self.error_reasons:
            return self.error_reasons[0]
        return self.error_codes[0] if self.error_codes else f"http_{self.status}_rejected"


class MoneyForwardReadError(RuntimeError):
    """Redacted, canonical failure for a read-only Money Forward request."""

    reason_code = "moneyforward_read_error"


class MoneyForwardRateLimitError(MoneyForwardReadError):
    reason_code = "http_429_rate_limited"

    def __init__(self, *, retry_after_seconds: float, attempts: int):
        self.status = 429
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        super().__init__(
            "Money Forward read API remained rate limited after bounded retries; "
            f"attempts={attempts}; retry_after_seconds={retry_after_seconds:g}"
        )


class MoneyForwardServerError(MoneyForwardReadError):
    reason_code = "http_5xx_server_error"

    def __init__(self, *, status: int, attempts: int):
        self.status = status
        self.attempts = attempts
        super().__init__(
            "Money Forward read API remained unavailable after bounded retries; "
            f"status={status}; attempts={attempts}"
        )


class MoneyForwardReadTimeoutError(MoneyForwardReadError):
    reason_code = "transport_timeout"

    def __init__(self, *, attempts: int):
        self.attempts = attempts
        super().__init__(
            "Money Forward read API timed out after bounded retries; "
            f"attempts={attempts}"
        )


class MoneyForwardMalformedResponseError(MoneyForwardReadError):
    reason_code = "malformed_json"

    def __init__(self, *, payload: bytes):
        self.response_hash = stable_hash(payload.decode("utf-8", errors="replace"))
        super().__init__(
            "Money Forward read API returned malformed JSON; "
            f"response_hash={self.response_hash[:16]}"
        )


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()


@dataclass(frozen=True)
class TokenRefreshResult:
    access_token: str
    refresh_token: str | None
    scope: tuple[str, ...]
    scope_hash: str
    has_journal_write: bool
    has_voucher_write: bool
    has_trade_partner_write: bool
    has_transaction_read: bool
    has_invoice_data_read: bool


@dataclass(frozen=True)
class WriteExecutionGrant:
    operation_type: str
    approval_hash: str
    scope_hash: str
    target_hash: str

    def assert_allows(self, operation_type: str) -> None:
        if self.operation_type != operation_type:
            raise PermissionError("write grant operation_type does not match")
        if not self.approval_hash or not self.scope_hash or not self.target_hash:
            raise PermissionError("write grant is incomplete")


@dataclass(frozen=True)
class JournalSideMasterIds:
    account_id: str
    sub_account_id: str | None = None
    tax_id: str | None = None
    department_id: str | None = None
    trade_partner_code: str | None = None
    invoice_kind: str | None = None


@dataclass(frozen=True)
class SingleBranchJournalMasterIds:
    debitor: JournalSideMasterIds
    creditor: JournalSideMasterIds


def build_single_branch_journal_create_request(
    candidate: JournalCandidate,
    master_ids: SingleBranchJournalMasterIds,
    *,
    journal_type: str = MONEY_FORWARD_JOURNAL_TYPE,
    memo: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    if not candidate.journal_date:
        raise ValueError("journal_date is required")
    if candidate.amount <= 0:
        raise ValueError("amount must be positive")
    if not journal_type:
        raise ValueError("journal_type is required")
    if journal_type == "ex_txn":
        raise ValueError("journal_type ex_txn is obsolete; use journal_entry")
    journal: dict[str, Any] = {
        "transaction_date": candidate.journal_date,
        "journal_type": journal_type,
        "branches": [
            {
                "remark": candidate.description,
                "debitor": _journal_side_payload(master_ids.debitor, candidate.amount),
                "creditor": _journal_side_payload(master_ids.creditor, candidate.amount),
            }
        ],
    }
    if memo:
        journal["memo"] = memo
    if tags:
        journal["tags"] = tags
    return journal


def _journal_side_payload(master_ids: JournalSideMasterIds, value: int) -> dict[str, Any]:
    if not master_ids.account_id:
        raise ValueError("account_id is required")
    payload: dict[str, Any] = {"account_id": master_ids.account_id, "value": value}
    for key in ("tax_id", "sub_account_id", "department_id", "trade_partner_code", "invoice_kind"):
        field_value = getattr(master_ids, key)
        if field_value is not None:
            if key == "invoice_kind":
                field_value = normalize_invoice_kind(str(field_value))
            payload[key] = field_value
    return payload


def normalize_invoice_kind(value: str) -> str:
    normalized = _INVOICE_KIND_ALIASES.get(value)
    if not normalized:
        allowed = ", ".join(sorted({INVOICE_KIND_QUALIFIED, INVOICE_KIND_NOT_TARGET, INVOICE_KIND_UNQUALIFIED_80}))
        raise ValueError(f"unsupported invoice_kind {value!r}; allowed: {allowed}")
    return normalized


def normalize_invoice_registration_number(value: str | None) -> str | None:
    if not value:
        return None
    normalized = "".join(ch for ch in str(value).upper() if ch.isdigit() or ch == "T")
    if normalized.startswith("T"):
        normalized = normalized[1:]
    if not normalized:
        return None
    if len(normalized) != 13 or not normalized.isdigit():
        raise ValueError("invoice registration number must be T + 13 digits or 13 digits")
    return normalized


def _redacted_api_error_details(payload: bytes) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract only documented error codes and structural target paths."""
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return (), ()
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list):
        return (), ()
    codes: set[str] = set()
    targets: set[str] = set()
    for item in errors:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if re.fullmatch(r"[a-z0-9_]{1,80}", code):
            codes.add(code)
        message = str(item.get("message") or "")
        for target in re.findall(
            r"(?:Target|target)\s*[:=]?\s*([A-Za-z][A-Za-z0-9_.\[\]-]{0,159})",
            message,
        ):
            targets.add(target)
    return tuple(sorted(codes)), tuple(sorted(targets))


def _redacted_api_error_reasons(payload: bytes) -> tuple[str, ...]:
    """Map only exact, documented Money Forward messages to safe reason codes."""
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list):
        return ()
    reasons = {
        reason
        for item in errors
        if isinstance(item, dict)
        for reason in [_SAFE_API_ERROR_MESSAGE_REASONS.get(str(item.get("message") or ""))]
        if reason is not None
    }
    return tuple(sorted(reasons))


_JOURNAL_CREATE_KEYS = frozenset({"transaction_date", "journal_type", "branches", "memo", "tags"})
_JOURNAL_BRANCH_KEYS = frozenset({"remark", "debitor", "creditor"})
_JOURNAL_SIDE_KEYS = frozenset(
    {"value", "account_id", "tax_id", "sub_account_id", "department_id", "trade_partner_code", "invoice_kind"}
)
_TRANSACTION_JOURNALIZE_KEYS = frozenset(
    {
        "transaction_id",
        "transaction_date",
        "tags",
        "memo",
        "account_id",
        "sub_account_id",
        "department_id",
        "trade_partner_code",
        "tax_id",
        "invoice_kind",
        "remark",
    }
)


def validate_journal_create_request(journal: Mapping[str, Any]) -> None:
    """Fail locally when a journal body differs from the official request schema."""
    if not isinstance(journal, Mapping):
        raise ValueError("journal create body must be an object")
    _reject_unknown_keys(journal, _JOURNAL_CREATE_KEYS, location="journal")
    _require_iso_date(journal.get("transaction_date"), field="journal.transaction_date")
    if journal.get("journal_type") not in {"journal_entry", "adjusting_entry"}:
        raise ValueError("journal.journal_type must be journal_entry or adjusting_entry")
    branches = journal.get("branches")
    if not isinstance(branches, list) or not 1 <= len(branches) <= 300:
        raise ValueError("journal.branches must contain between 1 and 300 rows")
    debit_total = 0
    credit_total = 0
    for index, branch in enumerate(branches):
        if not isinstance(branch, Mapping):
            raise ValueError(f"journal.branches[{index}] must be an object")
        _reject_unknown_keys(branch, _JOURNAL_BRANCH_KEYS, location=f"journal.branches[{index}]")
        if branch.get("debitor") is None and branch.get("creditor") is None:
            raise ValueError(f"journal.branches[{index}] requires a debitor or creditor")
        for side_name in ("debitor", "creditor"):
            side = branch.get(side_name)
            if side is None:
                continue
            if not isinstance(side, Mapping):
                raise ValueError(f"journal.branches[{index}].{side_name} must be an object or null")
            _reject_unknown_keys(side, _JOURNAL_SIDE_KEYS, location=f"journal.branches[{index}].{side_name}")
            if not str(side.get("account_id") or "").strip():
                raise ValueError(f"journal.branches[{index}].{side_name}.account_id is required")
            value = side.get("value")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"journal.branches[{index}].{side_name}.value must be a non-negative integer")
            if side.get("invoice_kind") is not None:
                normalize_invoice_kind(str(side["invoice_kind"]))
            if side_name == "debitor":
                debit_total += value
            else:
                credit_total += value
    if debit_total != credit_total:
        raise ValueError("journal debit and credit totals must match")
    _validate_optional_text(journal.get("memo"), field="journal.memo", max_length=200)
    _validate_tags(journal.get("tags"), field="journal.tags")


def validate_transaction_journalize_request(request: Mapping[str, Any]) -> None:
    if not isinstance(request, Mapping):
        raise ValueError("transaction journalize body must be an object")
    _reject_unknown_keys(request, _TRANSACTION_JOURNALIZE_KEYS, location="transaction_journalize")
    for field in ("transaction_id", "account_id"):
        if not str(request.get(field) or "").strip():
            raise ValueError(f"transaction_journalize.{field} is required")
    if request.get("transaction_date") is not None:
        _require_iso_date(request.get("transaction_date"), field="transaction_journalize.transaction_date")
    _validate_optional_text(request.get("memo"), field="transaction_journalize.memo", max_length=200)
    _validate_optional_text(request.get("remark"), field="transaction_journalize.remark", max_length=200)
    _validate_tags(request.get("tags"), field="transaction_journalize.tags")
    if request.get("invoice_kind") is not None:
        normalize_invoice_kind(str(request["invoice_kind"]))


def _reject_unknown_keys(payload: Mapping[str, Any], allowed: frozenset[str], *, location: str) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ValueError(f"{location} contains unsupported request fields: {','.join(unknown)}")


def _require_iso_date(value: Any, *, field: str) -> None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be YYYY-MM-DD")


def _validate_optional_text(value: Any, *, field: str, max_length: int) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > max_length):
        raise ValueError(f"{field} must be a string of at most {max_length} characters")


def _validate_tags(value: Any, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(tag, str) or len(tag) > 20 for tag in value):
        raise ValueError(f"{field} must be an array of strings of at most 20 characters")


class MoneyForwardOAuthClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: Transport | None = None,
        *,
        authorize_url: str = AUTHORIZE_URL,
        token_url: str = TOKEN_URL,
        client_auth_style: str = "basic",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.transport = transport or UrllibTransport()
        self.authorize_url = authorize_url
        self.token_url = token_url
        if client_auth_style not in {"basic", "form"}:
            raise ValueError("client_auth_style must be basic or form")
        self.client_auth_style = client_auth_style

    def refresh_access_token(self, refresh_token: str) -> TokenRefreshResult:
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_auth_style == "form":
            form.update({"client_id": self.client_id, "client_secret": self.client_secret})
        else:
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        body = urllib.parse.urlencode(form).encode()
        status, _headers, payload = self.transport.request(
            "POST",
            self.token_url,
            headers=headers,
            body=body,
        )
        if status != 200:
            raise RuntimeError(f"token refresh failed: HTTP {status}")
        data = json.loads(payload.decode())
        scope = tuple(sorted(data.get("scope", "").split()))
        return TokenRefreshResult(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            scope=scope,
            scope_hash=stable_hash(scope),
            has_journal_write="mfc/accounting/journal.write" in scope,
            has_voucher_write="mfc/accounting/voucher.write" in scope,
            has_trade_partner_write="mfc/accounting/trade_partners.write" in scope,
            has_transaction_read=ACCOUNTING_TRANSACTION_READ_SCOPE in scope,
            has_invoice_data_read="mfc/invoice/data.read" in scope,
        )

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...],
        state: str,
        code_challenge: str | None = None,
    ) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
        }
        if code_challenge:
            query["code_challenge"] = code_challenge
            query["code_challenge_method"] = "S256"
        return f"{self.authorize_url}?{urllib.parse.urlencode(query)}"

    def exchange_authorization_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenRefreshResult:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            form["code_verifier"] = code_verifier
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_auth_style == "form":
            form.update({"client_id": self.client_id, "client_secret": self.client_secret})
        else:
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        body = urllib.parse.urlencode(form).encode()
        status, _headers, payload = self.transport.request(
            "POST",
            self.token_url,
            headers=headers,
            body=body,
        )
        if status != 200:
            raise RuntimeError(f"authorization code exchange failed: HTTP {status}")
        data = json.loads(payload.decode())
        scope = tuple(sorted(data.get("scope", "").split()))
        return TokenRefreshResult(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            scope=scope,
            scope_hash=stable_hash(scope),
            has_journal_write="mfc/accounting/journal.write" in scope,
            has_voucher_write="mfc/accounting/voucher.write" in scope,
            has_trade_partner_write="mfc/accounting/trade_partners.write" in scope,
            has_transaction_read=ACCOUNTING_TRANSACTION_READ_SCOPE in scope,
            has_invoice_data_read="mfc/invoice/data.read" in scope,
        )


def record_scope_check(conn: sqlite3.Connection, provider: str, result: TokenRefreshResult) -> None:
    conn.execute(
        """
        INSERT INTO oauth_scope_checks (
            provider, scope_hash, has_journal_write, has_voucher_write,
            has_trade_partner_write, has_transaction_read, has_invoice_data_read, raw_secret_stored
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            provider,
            result.scope_hash,
            1 if result.has_journal_write else 0,
            1 if result.has_voucher_write else 0,
            1 if result.has_trade_partner_write else 0,
            1 if result.has_transaction_read else 0,
            1 if result.has_invoice_data_read else 0,
        ),
    )
    conn.commit()


def _office_resources(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("offices", "office", "data"):
        if key in payload:
            return _office_resources(payload[key])
    if any(key in payload for key in ("id", "office_id", "code")):
        return [payload]
    return []


class _OfficeBoundTransport:
    """Guard every accounting request, including direct POST transport calls."""

    def __init__(self, client: Any, delegate: Transport):
        self.client = client
        self.delegate = delegate

    def request(self, method: str, url: str, **kwargs: Any):
        if self.client.office_profile is None:
            raise PermissionError("Money Forward access requires an explicit office profile")
        self.client.office_profile.require_configured()
        authorizations = [v for k, v in (kwargs.get("headers") or {}).items() if str(k).lower() == "authorization"]
        if authorizations != [f"Bearer {self.client.access_token}"]:
            raise PermissionError("Request authorization differs from the verified client")
        parsed = urllib.parse.urlsplit(url)
        expected = urllib.parse.urlsplit(self.client.base_url)
        if parsed.fragment or parsed.username or parsed.password or any(
            part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/")
        ):
            raise PermissionError("Accounting request URL is not canonical")
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise PermissionError("Accounting requests cannot leave the configured API origin")
        if not (method.upper() == "GET" and parsed.path == "/api/v3/offices"):
            state = (self.client.access_token, self.client.office_profile, self.client.keychain_service,
                     self.client.db_path, self.client.write_enabled, self.client.base_url)
            self.client._require_business_identity(method, url)
            if state != (self.client.access_token, self.client.office_profile, self.client.keychain_service,
                         self.client.db_path, self.client.write_enabled, self.client.base_url):
                raise PermissionError("Accounting session changed during identity verification")
        return self.delegate.request(method, url, **kwargs)


class MoneyForwardAccountingClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = ACCOUNTING_BASE_URL,
        transport: Transport | None = None,
        use_office_path: bool = False,
        office_profile: OfficeProfile | None = None,
        keychain_service: str | None = None,
        db_path: Path | str | None = None,
        write_enabled: bool = False,
    ):
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self._transport = transport or UrllibTransport()
        self.transport = _OfficeBoundTransport(self, self._transport)
        self.use_office_path = use_office_path
        self.office_profile = office_profile or resolve_office_profile(required=False)
        self.keychain_service = keychain_service
        self.db_path = db_path
        self.write_enabled = write_enabled
        self.office_identity_binding: OfficeIdentityBinding | None = None

    def offices(self) -> dict[str, Any]:
        return self._get_json("/api/v3/offices")

    def validate_office_identity(self, *, office_id: str | None = None) -> OfficeIdentityBinding:
        profile = self.office_profile
        if profile is None:
            raise PermissionError("An explicit office profile is required")
        profile.require_configured()
        offices = _office_resources(self.offices())
        # The current-office API cannot safely disambiguate a multi-office token.
        if len(offices) != 1:
            raise PermissionError("The authorization must resolve to exactly one office")
        item = offices[0]
        name = str(item.get("name") or item.get("office_name") or item.get("display_name") or "")
        identifier = str(item.get("id") or item.get("office_id") or item.get("code") or "")
        if not identifier or normalize_office_name(name) not in profile.expected_office_name_keys:
            raise PermissionError("The authorization does not match the configured legal entity")
        if office_id is not None and str(office_id) != identifier:
            raise PermissionError("The requested office does not match the authorization")
        binding = OfficeIdentityBinding(
            profile_slug=profile.slug, profile_hash=profile.profile_hash,
            office_id=identifier, office_id_hash=stable_hash(identifier),
            legal_name_hash=stable_hash(normalize_office_name(name)),
            keychain_service_hash=stable_hash(profile.moneyforward_keychain_service),
            boundary_fingerprint=profile.boundary_fingerprint,
        )
        binding.assert_matches(profile, office_id=office_id)
        self.office_identity_binding = binding
        return binding

    def _require_business_identity(self, method: str, url: str) -> None:
        profile = self.office_profile
        if profile is None:
            raise PermissionError("Business I/O requires an explicit office profile")
        if not self.keychain_service:
            raise PermissionError("The credential source Keychain service must be explicit")
        resolve_moneyforward_keychain_service(profile, self.keychain_service, external_io=True)
        requested_id = None
        parts = urllib.parse.urlsplit(url).path.split("/")
        if len(parts) > 5 and parts[1:4] == ["api", "v3", "offices"]:
            requested_id = urllib.parse.unquote(parts[4])
        binding = self.validate_office_identity(office_id=requested_id)
        path = resolve_moneyforward_db_path(profile, self.db_path, external_io=True)
        if not path.is_file():
            raise PermissionError("Business I/O requires an existing bound company database")
        # No initialize(), migrations, directory creation, or binding update here.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            require_database_office_identity(conn, profile, binding)
        if method.upper() != "GET" and not self.write_enabled:
            raise PermissionError("This connection is read-only; writes require explicit run authorization")

    def term_settings(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("term_settings", office_id))

    def trial_balance_bs(
        self,
        *,
        start_date: str,
        end_date: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Read the official balance-sheet trial balance for one period."""

        query = urllib.parse.urlencode(
            {"start_date": start_date, "end_date": end_date}
        )
        return self._get_json(
            f"{self._path('reports/trial_balance_bs', office_id)}?{query}"
        )

    def accounts(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("accounts", office_id))

    def sub_accounts(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("sub_accounts", office_id))

    def taxes(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("taxes", office_id))

    def trade_partners(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("trade_partners", office_id))

    def resolve_trade_partner_code(self, name: str, office_id: str | None = None) -> str:
        partners = _resource_items(self.trade_partners(office_id))
        matches = [
            partner
            for partner in partners
            if str(partner.get("name") or "") == name
            or str(partner.get("name") or "").replace("株式会社", "").strip()
            == name.replace("株式会社", "").strip()
        ]
        if len(matches) != 1:
            raise LookupError(f"trade partner {name} resolved to {len(matches)} candidates")
        code = str(matches[0].get("code") or "")
        if not code:
            raise LookupError(f"trade partner {name} has no code")
        return code

    def create_trade_partner(
        self,
        *,
        name: str,
        invoice_registration_number: str | None = None,
        corporate_number: str | None = None,
        search_key: str | None = None,
        available: bool = True,
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("trade partner create requires a WriteExecutionGrant")
        grant.assert_allows("trade_partner_create")
        body = build_trade_partner_create_request(
            name=name,
            invoice_registration_number=invoice_registration_number,
            corporate_number=corporate_number,
            search_key=search_key,
            available=available,
        )
        status, _headers, payload = self.transport.request(
            "POST",
            f"{self.base_url}{self._path('trade_partners', office_id)}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(body, ensure_ascii=False).encode(),
        )
        if status not in (200, 201):
            error_codes, error_paths = _redacted_api_error_details(payload)
            detail = ""
            if error_codes or error_paths:
                detail = f"; error_codes={','.join(error_codes)}; error_paths={','.join(error_paths)}"
            raise RuntimeError(
                f"Money Forward API failed: POST /api/v3/trade_partners HTTP {status}{detail}"
            )
        return json.loads(payload.decode())

    def ensure_trade_partner_code(
        self,
        *,
        name: str,
        invoice_registration_number: str | None = None,
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> str:
        try:
            return self.resolve_trade_partner_code(name, office_id)
        except LookupError:
            if grant is None:
                raise
        self.create_trade_partner(
            name=name,
            invoice_registration_number=invoice_registration_number,
            office_id=office_id,
            grant=grant,
        )
        return self.resolve_trade_partner_code(name, office_id)

    def update_journal(
        self,
        *,
        journal_id: str,
        journal: dict[str, Any],
        expected_before_body_hash: str,
        expected_office_hash: str,
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("journal update requires a WriteExecutionGrant")
        grant.assert_allows("journal_update")
        metadata = self.update_journal_request_metadata(
            journal_id=journal_id,
            journal=journal,
            expected_before_body_hash=expected_before_body_hash,
            expected_office_hash=expected_office_hash,
            office_id=office_id,
        )
        if grant.target_hash != metadata["request_hash"]:
            raise PermissionError("journal update target hash mismatch")
        current = self.journal(journal_id, office_id=office_id)
        current_journal = _unwrap_single_resource(current, "journal")
        if stable_hash(current_journal) != expected_before_body_hash:
            raise RuntimeError("journal changed after update approval; PUT was not sent")
        status, _headers, payload = self.transport.request(
            "PUT",
            f"{self.base_url}{self._path('journals', office_id)}/{urllib.parse.quote(journal_id)}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps({"journal": journal}, ensure_ascii=False).encode(),
        )
        if status != 200:
            raise RuntimeError(f"Money Forward API failed: PUT /api/v3/journals/{{id}} HTTP {status}")
        return json.loads(payload.decode())

    def update_journal_request_metadata(
        self,
        *,
        journal_id: str,
        journal: dict[str, Any],
        expected_before_body_hash: str,
        expected_office_hash: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind a journal update grant to one journal and one replacement body."""
        if not journal_id:
            raise ValueError("journal_id is required")
        if not expected_before_body_hash:
            raise ValueError("expected_before_body_hash is required")
        if not expected_office_hash:
            raise ValueError("expected_office_hash is required")
        body_hash = stable_hash({"journal": journal})
        request_shape = {
            "method": "PUT",
            "path": f"{self._metadata_path('journals', office_id)}/{{id}}",
            "journal_id_hash": mask_identifier(journal_id),
            "before_body_hash": expected_before_body_hash,
            "body_hash": body_hash,
            "expected_office_hash": expected_office_hash,
        }
        return {
            "request_hash": stable_hash(request_shape),
            "body_hash": body_hash,
            "request_shape": request_shape,
        }

    def create_journal_request_metadata(
        self,
        *,
        journal: dict[str, Any],
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Return deterministic metadata for a journal create request without exposing the body."""
        validate_journal_create_request(journal)
        body_hash = stable_hash({"journal": journal})
        request_shape = {
            "method": "POST",
            "path": self._metadata_path("journals", office_id),
            "body_hash": body_hash,
        }
        return {"request_hash": stable_hash(request_shape), "body_hash": body_hash, "request_shape": request_shape}

    def create_journal(
        self,
        *,
        journal: dict[str, Any],
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("journal create requires a WriteExecutionGrant")
        grant.assert_allows("journal_create")
        metadata = self.create_journal_request_metadata(journal=journal, office_id=office_id)
        if grant.target_hash != metadata["request_hash"]:
            raise PermissionError("journal create target hash mismatch")
        return self._post_journal(journal=journal, office_id=office_id)

    def create_journal_from_run_instruction(
        self,
        *,
        journal: dict[str, Any],
        instruction: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """POST a normal-run journal when the user's shiwake instruction authorizes it."""

        normalized = "".join(str(instruction or "").split())
        if "仕分" not in normalized and "仕訳" not in normalized:
            raise PermissionError("normal journal create requires a shiwake instruction")
        validate_journal_create_request(journal)
        return self._post_journal(journal=journal, office_id=office_id)

    def _post_journal(
        self,
        *,
        journal: dict[str, Any],
        office_id: str | None = None,
    ) -> dict[str, Any]:
        status, _headers, payload = self.transport.request(
            "POST",
            f"{self.base_url}{self._path('journals', office_id)}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps({"journal": journal}, ensure_ascii=False).encode(),
        )
        if status == 400:
            raise MoneyForwardRequestRejectedError(status=status, payload=payload)
        if status not in (200, 201):
            raise RuntimeError(f"Money Forward API failed: POST /api/v3/journals HTTP {status}")
        return json.loads(payload.decode())

    def departments(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("departments", office_id))

    def connected_accounts(self, office_id: str | None = None) -> dict[str, Any]:
        return self._get_json(self._path("connected_accounts", office_id))

    def transactions(
        self,
        office_id: str | None = None,
        *,
        start_date: str,
        end_date: str,
        connected_account_id: str | None = None,
        connected_sub_account_id: str | None = None,
        journalizing_statuses: tuple[str, ...] = (),
        order: str = "desc",
        page: int = 1,
        per_page: int = 500,
    ) -> dict[str, Any]:
        """Read connected-service transactions through the official API."""
        _require_iso_date(start_date, field="transactions.start_date")
        _require_iso_date(end_date, field="transactions.end_date")
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            raise ValueError("transactions.end_date must be on or after start_date")
        if (end - start).days > 40:
            raise ValueError("transactions date range must be 40 days or less")
        if connected_account_id and connected_sub_account_id:
            raise ValueError("connected_account_id and connected_sub_account_id are mutually exclusive")
        allowed_statuses = {"excluded", "none", "registered", "modified", "new_voucher_attached"}
        if any(status not in allowed_statuses for status in journalizing_statuses):
            raise ValueError("transactions journalizing_statuses contains an unsupported value")
        if order not in {"asc", "desc"}:
            raise ValueError("transactions.order must be asc or desc")
        if page < 1 or not 10 <= per_page <= 500:
            raise ValueError("transactions page/per_page are outside the official API range")
        query_items: list[tuple[str, Any]] = [("start_date", start_date), ("end_date", end_date)]
        if connected_account_id:
            query_items.append(("connected_account_id", connected_account_id))
        if connected_sub_account_id:
            query_items.append(("connected_sub_account_id", connected_sub_account_id))
        query_items.extend(("journalizing_statuses", status) for status in journalizing_statuses)
        query_items.extend((("order", order), ("page", page), ("per_page", per_page)))
        query = urllib.parse.urlencode(query_items, doseq=True)
        return self._get_json(f"{self._path('transactions', office_id)}?{query}")

    def transaction_journalize_request_metadata(
        self,
        *,
        request: dict[str, Any],
        office_id: str | None = None,
    ) -> dict[str, Any]:
        validate_transaction_journalize_request(request)
        body_hash = stable_hash(request)
        request_shape = {
            "method": "POST",
            "path": self._metadata_path("transactions/journalize", office_id),
            "body_hash": body_hash,
        }
        return {"request_hash": stable_hash(request_shape), "body_hash": body_hash, "request_shape": request_shape}

    def journalize_transaction(
        self,
        *,
        request: dict[str, Any],
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("transaction journalize requires a WriteExecutionGrant")
        grant.assert_allows("transaction_journalize")
        metadata = self.transaction_journalize_request_metadata(request=request, office_id=office_id)
        if grant.target_hash != metadata["request_hash"]:
            raise PermissionError("transaction journalize target hash mismatch")
        status, _headers, payload = self.transport.request(
            "POST",
            f"{self.base_url}{self._path('transactions/journalize', office_id)}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(request, ensure_ascii=False).encode(),
        )
        if status == 400:
            raise MoneyForwardRequestRejectedError(status=status, payload=payload)
        if status not in (200, 201):
            raise RuntimeError(f"Money Forward API failed: POST /api/v3/transactions/journalize HTTP {status}")
        return json.loads(payload.decode())

    def journals(
        self,
        office_id: str | None = None,
        *,
        start_date: str,
        end_date: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict:
        query = urllib.parse.urlencode(
            {"start_date": start_date, "end_date": end_date, "page": page, "per_page": per_page}
        )
        return self._get_json(f"{self._path('journals', office_id)}?{query}")

    def journal(self, journal_id: str, office_id: str | None = None) -> dict:
        return self._get_json(f"{self._path('journals', office_id)}/{urllib.parse.quote(journal_id)}")

    def journal_or_none(self, journal_id: str, office_id: str | None = None) -> dict[str, Any] | None:
        path = f"{self._path('journals', office_id)}/{urllib.parse.quote(journal_id)}"
        status, _headers, payload = self.transport.request(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"Money Forward API failed: GET {path} HTTP {status}")
        return json.loads(payload.decode())

    def delete_journal_request_metadata(
        self,
        *,
        journal_id: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Return deterministic metadata for a journal delete without exposing the raw ID."""
        if not journal_id:
            raise ValueError("journal_id is required")
        request_shape = {
            "method": "DELETE",
            "path": f"{self._metadata_path('journals', office_id)}/{{id}}",
            "journal_id_hash": mask_identifier(journal_id),
        }
        return {"request_hash": stable_hash(request_shape), "request_shape": request_shape}

    def delete_journal(
        self,
        *,
        journal_id: str,
        office_id: str | None = None,
        grant: WriteExecutionGrant | None = None,
    ) -> None:
        if grant is None:
            raise PermissionError("journal delete requires a WriteExecutionGrant")
        grant.assert_allows("journal_delete")
        metadata = self.delete_journal_request_metadata(journal_id=journal_id, office_id=office_id)
        if grant.target_hash != metadata["request_hash"]:
            raise PermissionError("journal delete target hash mismatch")
        status, _headers, _payload = self.transport.request(
            "DELETE",
            f"{self.base_url}{self._path('journals', office_id)}/{urllib.parse.quote(journal_id)}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        if status not in (200, 204):
            raise RuntimeError(f"Money Forward API failed: DELETE /api/v3/journals/{{id}} HTTP {status}")

    def attach_voucher_request_metadata(
        self,
        *,
        office_id: str,
        journal_id: str,
        file_name: str,
        content_type: str,
        file_hash: str,
    ) -> dict[str, Any]:
        """Return deterministic metadata for a write request without reading file bytes."""
        request_shape = {
            "method": "POST",
            "path": self._metadata_path("vouchers", office_id),
            "journal_id_hash": mask_identifier(journal_id),
            "file_name": file_name,
            "content_type": content_type,
            "file_hash": file_hash,
        }
        return {"request_hash": stable_hash(request_shape), "request_shape": request_shape}

    def attach_voucher(
        self,
        *,
        office_id: str,
        journal_id: str,
        file_name: str,
        content_type: str,
        file_bytes: bytes,
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("voucher attach requires a WriteExecutionGrant")
        grant.assert_allows("voucher_attach")
        body = json.dumps(
            {
                "journal_id": journal_id,
                "voucher_files": [
                    {
                        "file_name": file_name,
                        "file_data": base64.b64encode(file_bytes).decode(),
                    }
                ],
            },
            ensure_ascii=False,
        ).encode()
        status, _headers, payload = self.transport.request(
            "POST",
            f"{self.base_url}{self._path('vouchers', office_id)}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
        )
        if status not in (200, 201):
            raise RuntimeError(f"Money Forward API failed: POST /api/v3/vouchers HTTP {status}")
        return json.loads(payload.decode())

    def _metadata_path(self, resource: str, office_id: str | None = None) -> str:
        """Render local request metadata without credentials or identity I/O.

        Metadata is not authorization. Actual operations still use _path and
        the guarded transport, with fresh authenticated identity and DB checks.
        """
        if self.use_office_path:
            if not office_id:
                raise ValueError("office_id is required when use_office_path=True")
            return f"/api/v3/offices/{urllib.parse.quote(office_id, safe='')}/{resource}"
        return f"/api/v3/{resource}"

    def _path(self, resource: str, office_id: str | None = None) -> str:
        if office_id is not None:
            if self.office_identity_binding is None:
                self.validate_office_identity(office_id=office_id)
            elif self.office_identity_binding.office_id != str(office_id):
                raise PermissionError("Requested office differs from the bound office")
        if self.use_office_path:
            if not office_id:
                raise ValueError("office_id is required when use_office_path=True")
            return f"/api/v3/offices/{urllib.parse.quote(office_id)}/{resource}"
        return f"/api/v3/{resource}"

    def _get_json(self, path: str) -> dict[str, Any]:
        status = 0
        headers: dict[str, str] = {}
        payload = b""
        attempts = 5
        for attempt in range(attempts):
            try:
                status, headers, payload = self.transport.request(
                    "GET",
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                )
            except (TimeoutError, socket.timeout) as error:
                if attempt == attempts - 1:
                    raise MoneyForwardReadTimeoutError(attempts=attempts) from error
                time.sleep(_read_retry_delay_seconds({}, attempt))
                continue
            retryable_status = status == 429 or 500 <= status <= 599
            if not retryable_status or attempt == attempts - 1:
                break
            time.sleep(_read_retry_delay_seconds(headers, attempt))
        if status == 429:
            raise MoneyForwardRateLimitError(
                retry_after_seconds=_read_retry_delay_seconds(headers, attempts - 1),
                attempts=attempts,
            )
        if 500 <= status <= 599:
            raise MoneyForwardServerError(status=status, attempts=attempts)
        if status != 200:
            raise RuntimeError(f"Money Forward API failed: GET {path} HTTP {status}")
        try:
            return json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MoneyForwardMalformedResponseError(payload=payload) from error


class _PayableBoundTransport:
    """Apply company and write guards even to direct public transport calls."""

    def __init__(self, client: Any, delegate: Transport):
        self.client = client
        self.delegate = delegate

    def request(self, method: str, url: str, **kwargs: Any):
        client = self.client
        if method != "GET" and client.write_enabled is not True:
            raise PermissionError("Payable connection is read-only without explicit run authorization")
        parsed = urllib.parse.urlsplit(url)
        gateway = urllib.parse.urlsplit(client.gateway_base_url)
        external = urllib.parse.urlsplit(client.external_base_url)
        origin = (parsed.scheme, parsed.netloc)
        if (parsed.fragment or parsed.username or parsed.password
                or any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/"))):
            raise PermissionError("Payable request URL is not canonical")
        if origin == (gateway.scheme, gateway.netloc) and parsed.path.startswith(gateway.path + "/v1/"):
            token = client.gateway_access_token
        elif origin == (external.scheme, external.netloc):
            expected_prefix = external.path + "/api/external/v1/offices/" + urllib.parse.quote(client.office_id, safe="") + "/"
            if not parsed.path.startswith(expected_prefix):
                raise PermissionError("Payable external request office path mismatch")
            token = client.external_access_token
        else:
            raise PermissionError("Payable request origin or route mismatch")
        headers = kwargs.get("headers") or {}
        authorizations = [v for k, v in headers.items() if str(k).lower() == "authorization"]
        if authorizations != [f"Bearer {token}"]:
            raise PermissionError("Payable request credential differs from the validated surface")
        state = client._boundary_state()
        client.validate_office_identity()
        if state != client._boundary_state():
            raise PermissionError("Payable session changed during identity verification")
        return self.delegate.request(method, url, **kwargs)


class MoneyForwardPayableClient:
    """Two explicitly bound Payable surfaces, read-only by default.

    The starter supports the gateway credential shared with its Accounting
    client; a standalone gateway JWT has no verified office adapter here and is
    refused. External OAuth identity is read back independently. An office ID
    supplied by the caller is only a routing assertion, never identity proof.
    """

    def __init__(
        self,
        *,
        gateway_access_token: str,
        external_access_token: str,
        office_id: str,
        gateway_base_url: str = PAYABLE_GATEWAY_BASE_URL,
        external_base_url: str = PAYABLE_EXTERNAL_BASE_URL,
        transport: Transport | None = None,
        office_profile: OfficeProfile | None = None,
        keychain_service: str | None = None,
        db_path: Path | str | None = None,
        accounting_client: MoneyForwardAccountingClient | None = None,
        write_enabled: bool = False,
    ):
        if not gateway_access_token or not external_access_token or not office_id:
            raise ValueError("gateway token, external token, and office_id are required")
        self.gateway_access_token = gateway_access_token
        self.external_access_token = external_access_token
        self.office_id = office_id
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.external_base_url = external_base_url.rstrip("/")
        self.office_profile = office_profile or resolve_office_profile(required=False)
        # This is the actual source of both Payable credentials, supplied by the
        # credential loader. Never infer it from a profile to disguise a mismatch.
        self.keychain_service = keychain_service
        self.db_path = db_path
        self.accounting_client = accounting_client
        self.write_enabled = write_enabled
        self._transport = transport or UrllibTransport()
        self.transport = _PayableBoundTransport(self, self._transport)

    def _boundary_state(self) -> tuple[Any, ...]:
        accounting = self.accounting_client
        return (self.office_profile, self.keychain_service, self.db_path, self.office_id,
                self.gateway_access_token, self.external_access_token, self.write_enabled,
                self.gateway_base_url, self.external_base_url, accounting,
                getattr(accounting, "access_token", None), getattr(accounting, "office_profile", None),
                getattr(accounting, "keychain_service", None), getattr(accounting, "db_path", None))

    def validate_office_identity(self) -> str:
        """Read back both authenticated identities and require the bound DB.

        Raw identifiers and credentials stay in memory; this function opens
        SQLite read-only and never initializes, migrates or refreshes a binding.
        """
        profile = self.office_profile
        if profile is None:
            raise PermissionError("Payable requires an explicit company profile")
        profile.require_configured()
        if not self.keychain_service:
            raise PermissionError("Payable credential-source Keychain service must be explicit")
        resolve_moneyforward_keychain_service(profile, self.keychain_service, external_io=True)
        path = resolve_moneyforward_db_path(profile, self.db_path, external_io=True)
        expected_hash = profile.get_setting("payable.office_id_hash")
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise PermissionError("Payable requires a configured external office identity hash")
        if not isinstance(self.office_id, str) or stable_hash(self.office_id) != expected_hash:
            raise PermissionError("Caller-supplied Payable office differs from the configured identity")
        accounting = self.accounting_client
        if accounting is None or accounting.office_profile is None:
            raise PermissionError("Payable gateway requires a verified Accounting client")
        if accounting.office_profile.boundary_fingerprint != profile.boundary_fingerprint:
            raise PermissionError("Payable and Accounting profiles differ")
        if accounting.keychain_service != self.keychain_service:
            raise PermissionError("Payable and Accounting credential-source Keychain services differ")
        resolve_moneyforward_db_path(profile, accounting.db_path, external_io=True)
        # Equality links the credential source only. Authorization still requires
        # fresh official offices readback and its verified database binding.
        if self.gateway_access_token != accounting.access_token:
            raise PermissionError("Standalone or mismatched gateway credentials have no verified office adapter")
        if not path.is_file():
            raise PermissionError("Payable requires an existing bound company database")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            require_database_office_identity(conn, profile)
            binding = accounting.validate_office_identity()
            require_database_office_identity(conn, profile, binding)
            status, _, body = self._transport.request(
                "GET", f"{self.external_base_url}/api/external/v1/offices",
                headers={"Authorization": f"Bearer {self.external_access_token}", "Accept": "application/json"},
            )
            if status != 200:
                raise PermissionError("Payable external office identity readback failed")
            try:
                offices = _office_resources(json.loads(body.decode("utf-8")))
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                raise PermissionError("Payable external office identity response is malformed") from exc
            if (len(offices) != 1 or type(offices[0].get("id")) not in (str, int)
                    or not str(offices[0]["id"]).strip()):
                raise PermissionError("Payable external authorization must resolve to exactly one office")
            observed = str(offices[0]["id"])
            if stable_hash(observed) != expected_hash or observed != self.office_id:
                raise PermissionError("Authenticated Payable office differs from the configured company")
            expected_code = profile.get_setting("payable.office_identification_code")
            if expected_code is not None:
                observed_code = "".join(ch for ch in str(offices[0].get("identification_code") or offices[0].get("code") or "") if ch.isdigit())
                if not str(expected_code) or observed_code != str(expected_code):
                    raise PermissionError("Authenticated Payable office identification code mismatch")
            # Reject revocation/profile drift during identity readback as well.
            require_database_office_identity(conn, profile, binding)
            return observed
        finally:
            conn.close()

    def received_invoices(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        file_name_query: str | None = None,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= per_page <= 100:
            raise ValueError("received invoice page/per_page are outside the official range")
        query: list[tuple[str, Any]] = [("page", page), ("per_page", per_page)]
        if file_name_query:
            query.append(("searchable_number_or_filename_cont", file_name_query))
        return self._request_json(
            "GET",
            f"/v1/received_invoices?{urllib.parse.urlencode(query)}",
            surface="gateway",
        )

    def received_invoice(self, received_invoice_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/received_invoices/{urllib.parse.quote(received_invoice_id, safe='')}",
            surface="gateway",
        )

    def counterparties(self, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode({"page": page, "per_page": per_page})
        return self._request_json(
            "GET",
            f"/v1/counterparties?{query}",
            surface="gateway",
        )

    def payees(self, counterparty_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/counterparties/{urllib.parse.quote(counterparty_id, safe='')}/payees",
            surface="gateway",
        )

    def wait_for_received_invoice(
        self,
        received_invoice_id: str,
        *,
        max_attempts: int = 12,
        poll_interval_seconds: float = 5.0,
        sleeper: Any = time.sleep,
    ) -> dict[str, Any]:
        if max_attempts < 1 or poll_interval_seconds < 0:
            raise ValueError("OCR polling bounds are invalid")
        latest: dict[str, Any] = {}
        for attempt in range(max_attempts):
            latest = self.received_invoice(received_invoice_id)
            invoice = latest.get("received_invoice") if isinstance(latest.get("received_invoice"), dict) else latest
            if str(invoice.get("operation_status") or "") != "ocr_processing":
                return latest
            if attempt < max_attempts - 1:
                sleeper(poll_interval_seconds)
        raise TimeoutError("Payable OCR did not leave ocr_processing before the bounded deadline")

    def received_invoice_create_metadata(
        self,
        *,
        file_name: str,
        file_hash: str,
        payee_id: str | None = None,
        receipt_type: str = "e_doc",
    ) -> dict[str, Any]:
        if not file_name or not file_hash:
            raise ValueError("file_name and file_hash are required")
        if receipt_type not in {"e_doc", "paper"}:
            raise ValueError("receipt_type must be e_doc or paper")
        shape = {
            "method": "POST",
            "path": "/v1/received_invoices",
            "file_name": file_name,
            "file_hash": file_hash,
            "payee_id_hash": mask_identifier(payee_id) if payee_id else None,
            "receipt_type": receipt_type,
        }
        return {"request_hash": stable_hash(shape), "request_shape": shape}

    def create_received_invoice(
        self,
        *,
        file_name: str,
        content_type: str,
        file_bytes: bytes,
        payee_id: str | None = None,
        receipt_type: str = "e_doc",
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("received invoice create requires a WriteExecutionGrant")
        grant.assert_allows("payable_received_invoice_create")
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        metadata = self.received_invoice_create_metadata(
            file_name=file_name,
            file_hash=file_hash,
            payee_id=payee_id,
            receipt_type=receipt_type,
        )
        if grant.target_hash != metadata["request_hash"]:
            raise PermissionError("received invoice create target hash mismatch")
        boundary = f"mf-phase12-{file_hash[:24]}"
        parts = [_multipart_field(boundary, "invoice[receipt_type]", receipt_type)]
        if payee_id:
            parts.append(_multipart_field(boundary, "invoice[ap_payee_id]", payee_id))
        parts.extend(
            [
                _multipart_file(boundary, "invoice[file]", file_name, content_type, file_bytes),
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return self._request_json(
            "POST",
            "/v1/received_invoices",
            surface="gateway",
            body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
            expected_statuses=(200, 201),
        )

    def invoice_report(self, invoice_report_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._external_path(f"invoice_reports/{urllib.parse.quote(invoice_report_id, safe='')}"),
            surface="external",
        )

    def invoice_transactions(self, invoice_report_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._external_path(
                f"invoice_reports/{urllib.parse.quote(invoice_report_id, safe='')}/invoice_transactions"
            ),
            surface="external",
        )

    def expense_items(self) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._external_path("ex_items"),
            surface="external",
        )

    def excises(self) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._external_path("excises"),
            surface="external",
        )

    def create_invoice_transaction(
        self,
        *,
        invoice_report_id: str,
        transaction: Mapping[str, Any],
        grant: WriteExecutionGrant | None = None,
    ) -> dict[str, Any]:
        if grant is None:
            raise PermissionError("invoice transaction create requires a WriteExecutionGrant")
        grant.assert_allows("payable_invoice_transaction_create")
        path = self._external_path(
            f"invoice_reports/{urllib.parse.quote(invoice_report_id, safe='')}/invoice_transactions"
        )
        body_object = {"ap_invoice_transaction": dict(transaction)}
        request_hash = stable_hash(
            {
                "method": "POST",
                "path": path,
                "invoice_report_id_hash": mask_identifier(invoice_report_id),
                "body_hash": stable_hash(body_object),
            }
        )
        if grant.target_hash != request_hash:
            raise PermissionError("invoice transaction create target hash mismatch")
        return self._request_json(
            "POST",
            path,
            surface="external",
            body=json.dumps(body_object, ensure_ascii=False).encode(),
            content_type="application/json",
            expected_statuses=(200, 201),
        )

    def ex_journals_by_invoice_transactions(
        self,
        *,
        recognized_at_from: str,
        recognized_at_to: str,
        page: int = 1,
    ) -> dict[str, Any]:
        _require_iso_date(recognized_at_from, field="recognized_at_from")
        _require_iso_date(recognized_at_to, field="recognized_at_to")
        query = urllib.parse.urlencode(
            {
                "page": page,
                "query_object[recognized_at_from]": recognized_at_from,
                "query_object[recognized_at_to]": recognized_at_to,
            }
        )
        return self._request_json(
            "GET",
            f"{self._external_path('ex_journals_by_invoice_transactions')}?{query}",
            surface="external",
        )

    def _external_path(self, resource: str) -> str:
        office = urllib.parse.quote(self.office_id, safe="")
        return f"/api/external/v1/offices/{office}/{resource}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        surface: str,
        body: bytes | None = None,
        content_type: str | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        if surface == "gateway":
            base_url, token = self.gateway_base_url, self.gateway_access_token
        elif surface == "external":
            base_url, token = self.external_base_url, self.external_access_token
        else:
            raise ValueError("surface must be gateway or external")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        status, _headers, payload = self.transport.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            body=body,
        )
        if status not in expected_statuses:
            raise RuntimeError(f"Money Forward Payable API failed: {method} {path} HTTP {status}")
        try:
            decoded = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MoneyForwardMalformedResponseError(payload=payload) from error
        if not isinstance(decoded, dict):
            raise MoneyForwardMalformedResponseError(payload=payload)
        return decoded


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def _multipart_file(
    boundary: str,
    name: str,
    file_name: str,
    content_type: str,
    file_bytes: bytes,
) -> bytes:
    safe_name = file_name.replace('"', "")
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + file_bytes + b"\r\n"


def _read_retry_delay_seconds(headers: Mapping[str, str], attempt: int) -> float:
    retry_after = next(
        (value for key, value in headers.items() if str(key).casefold() == "retry-after"),
        None,
    )
    if retry_after is not None:
        try:
            return max(0.0, min(float(retry_after), 60.0))
        except (TypeError, ValueError):
            pass
    return float(min(2**attempt, 16))


class MoneyForwardJournalWriteClient:
    def __init__(
        self,
        accounting_client: MoneyForwardAccountingClient,
        *,
        journal: dict[str, Any],
        office_id: str | None = None,
        grant: WriteExecutionGrant,
    ):
        self.accounting_client = accounting_client
        self.journal = journal
        self.office_id = office_id
        self.grant = grant

    def create_journal(self, _plan: Any) -> str:
        response = self.accounting_client.create_journal(
            journal=self.journal,
            office_id=self.office_id,
            grant=self.grant,
        )
        return extract_moneyforward_journal_id(response)


def _unwrap_single_resource(response: dict[str, Any], resource: str) -> dict[str, Any]:
    nested = response.get(resource)
    if isinstance(nested, dict):
        return nested
    data = response.get("data")
    if isinstance(data, dict):
        nested = data.get(resource)
        if isinstance(nested, dict):
            return nested
        return data
    return response


def extract_moneyforward_journal_id(response: dict[str, Any]) -> str:
    candidates = [
        response.get("id"),
        (response.get("data") or {}).get("id") if isinstance(response.get("data"), dict) else None,
        (response.get("journal") or {}).get("id") if isinstance(response.get("journal"), dict) else None,
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise RuntimeError("Money Forward journal create response did not contain a journal id")


def _resource_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "accounts", "sub_accounts", "taxes", "trade_partners", "departments", "journals"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in data.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def build_trade_partner_create_request(
    *,
    name: str,
    invoice_registration_number: str | None = None,
    corporate_number: str | None = None,
    search_key: str | None = None,
    available: bool = True,
) -> dict[str, Any]:
    if not name:
        raise ValueError("trade partner name is required")
    partner: dict[str, Any] = {"name": name, "available": available}
    if search_key is not None:
        partner["search_key"] = search_key
    normalized_invoice = normalize_invoice_registration_number(invoice_registration_number)
    if normalized_invoice:
        partner["invoice_registration_number"] = normalized_invoice
    normalized_corporate = normalize_invoice_registration_number(corporate_number)
    if normalized_corporate:
        partner["corporate_number"] = normalized_corporate
    return {"trade_partners": [partner]}
