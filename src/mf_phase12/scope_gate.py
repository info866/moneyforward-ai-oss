from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class VoucherWriteScopeEvidence:
    provider: str
    checked_at: str
    scope_hash: str
    has_voucher_write: bool
    raw_secret_stored: bool

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "checked_at": self.checked_at,
            "scope_hash": self.scope_hash,
            "has_voucher_write": self.has_voucher_write,
            "raw_secret_stored": self.raw_secret_stored,
            "raw_scope_stored": False,
            "raw_secret_value_stored": False,
        }


@dataclass(frozen=True)
class JournalWriteScopeEvidence:
    provider: str
    checked_at: str
    scope_hash: str
    has_journal_write: bool
    raw_secret_stored: bool

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "checked_at": self.checked_at,
            "scope_hash": self.scope_hash,
            "has_journal_write": self.has_journal_write,
            "raw_secret_stored": self.raw_secret_stored,
            "raw_scope_stored": False,
            "raw_secret_value_stored": False,
        }


@dataclass(frozen=True)
class TradePartnerWriteScopeEvidence:
    provider: str
    checked_at: str
    scope_hash: str
    has_trade_partner_write: bool
    raw_secret_stored: bool

    def to_redacted_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "checked_at": self.checked_at,
            "scope_hash": self.scope_hash,
            "has_trade_partner_write": self.has_trade_partner_write,
            "raw_secret_stored": self.raw_secret_stored,
            "raw_scope_stored": False,
            "raw_secret_value_stored": False,
        }


def require_latest_voucher_write_scope(
    conn: sqlite3.Connection,
    *,
    expected_scope_hash: str,
    provider: str = "moneyforward",
) -> VoucherWriteScopeEvidence:
    if not expected_scope_hash:
        raise PermissionError("expected_scope_hash is required")
    try:
        row = conn.execute(
            """
            SELECT provider, checked_at, scope_hash, has_voucher_write, raw_secret_stored
            FROM oauth_scope_checks
            WHERE provider = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise PermissionError("oauth_scope_checks table is unavailable in the local Phase 12 DB") from error
    if row is None:
        raise PermissionError("voucher.write scope has not been checked in the local Phase 12 DB")
    evidence = VoucherWriteScopeEvidence(
        provider=str(row["provider"]),
        checked_at=str(row["checked_at"]),
        scope_hash=str(row["scope_hash"]),
        has_voucher_write=bool(row["has_voucher_write"]),
        raw_secret_stored=bool(row["raw_secret_stored"]),
    )
    if evidence.raw_secret_stored:
        raise PermissionError("scope evidence is invalid because raw_secret_stored is true")
    if not evidence.has_voucher_write:
        raise PermissionError("latest Money Forward scope check does not include voucher.write")
    if evidence.scope_hash != expected_scope_hash:
        raise PermissionError("latest Money Forward scope_hash does not match the approved grant")
    return evidence


def require_latest_trade_partner_write_scope(
    conn: sqlite3.Connection,
    *,
    expected_scope_hash: str,
    provider: str = "moneyforward",
) -> TradePartnerWriteScopeEvidence:
    if not expected_scope_hash:
        raise PermissionError("expected_scope_hash is required")
    try:
        row = conn.execute(
            """
            SELECT provider, checked_at, scope_hash, has_trade_partner_write, raw_secret_stored
            FROM oauth_scope_checks
            WHERE provider = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise PermissionError("oauth_scope_checks table is unavailable in the local Phase 12 DB") from error
    if row is None:
        raise PermissionError("trade_partners.write scope has not been checked in the local Phase 12 DB")
    evidence = TradePartnerWriteScopeEvidence(
        provider=str(row["provider"]),
        checked_at=str(row["checked_at"]),
        scope_hash=str(row["scope_hash"]),
        has_trade_partner_write=bool(row["has_trade_partner_write"]),
        raw_secret_stored=bool(row["raw_secret_stored"]),
    )
    if evidence.raw_secret_stored:
        raise PermissionError("scope evidence is invalid because raw_secret_stored is true")
    if not evidence.has_trade_partner_write:
        raise PermissionError("latest Money Forward scope check does not include trade_partners.write")
    if evidence.scope_hash != expected_scope_hash:
        raise PermissionError("latest Money Forward scope_hash does not match the approved grant")
    return evidence


def require_latest_journal_write_scope(
    conn: sqlite3.Connection,
    *,
    expected_scope_hash: str,
    provider: str = "moneyforward",
) -> JournalWriteScopeEvidence:
    if not expected_scope_hash:
        raise PermissionError("expected_scope_hash is required")
    try:
        row = conn.execute(
            """
            SELECT provider, checked_at, scope_hash, has_journal_write, raw_secret_stored
            FROM oauth_scope_checks
            WHERE provider = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise PermissionError("oauth_scope_checks table is unavailable in the local Phase 12 DB") from error
    if row is None:
        raise PermissionError("journal.write scope has not been checked in the local Phase 12 DB")
    evidence = JournalWriteScopeEvidence(
        provider=str(row["provider"]),
        checked_at=str(row["checked_at"]),
        scope_hash=str(row["scope_hash"]),
        has_journal_write=bool(row["has_journal_write"]),
        raw_secret_stored=bool(row["raw_secret_stored"]),
    )
    if evidence.raw_secret_stored:
        raise PermissionError("scope evidence is invalid because raw_secret_stored is true")
    if not evidence.has_journal_write:
        raise PermissionError("latest Money Forward scope check does not include journal.write")
    if evidence.scope_hash != expected_scope_hash:
        raise PermissionError("latest Money Forward scope_hash does not match the approved grant")
    return evidence
