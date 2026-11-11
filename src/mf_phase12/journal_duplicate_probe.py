from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .hash_utils import mask_identifier, stable_hash
from .journal_backfill import _extract_items, _journal_id, _normalized_lines, _unwrap_journal
from .journal_write_verify import _read_back_gross_value
from .local_artifacts import require_ignored_untracked_data_path
from .sync import _amount, _date_key, _paired_journal_lines


class JournalDuplicateReadClient(Protocol):
    def journals(
        self,
        office_id: str | None = None,
        *,
        start_date: str,
        end_date: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict: ...

    def journal(self, journal_id: str, office_id: str | None = None) -> dict: ...


@dataclass(frozen=True)
class JournalDuplicateTarget:
    source_file_hash: str
    journal_date: str
    amount: int
    match_terms: tuple[str, ...]
    expected_debit_account: str | None = None
    expected_debit_sub_account: str | None = None
    expected_credit_account: str | None = None
    expected_credit_sub_account: str | None = None
    search_start_date: str | None = None
    search_end_date: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "JournalDuplicateTarget":
        required = ("source_file_hash", "journal_date", "amount", "match_terms")
        missing = [key for key in required if payload.get(key) in (None, "", [])]
        if missing:
            raise ValueError(f"duplicate target missing: {','.join(missing)}")
        source_hash = str(payload["source_file_hash"])
        if not _is_sha256_marker(source_hash):
            raise ValueError("source_file_hash must be sha256:<64 hex>")
        terms = tuple(str(value).strip() for value in payload["match_terms"] if str(value).strip())
        if not terms:
            raise ValueError("match_terms must contain at least one value")
        amount = int(payload["amount"])
        if amount <= 0:
            raise ValueError("amount must be positive")
        return cls(
            source_file_hash=source_hash,
            journal_date=str(payload["journal_date"])[:10],
            amount=amount,
            match_terms=terms,
            expected_debit_account=_optional_str(payload.get("expected_debit_account")),
            expected_debit_sub_account=_optional_str(payload.get("expected_debit_sub_account")),
            expected_credit_account=_optional_str(payload.get("expected_credit_account")),
            expected_credit_sub_account=_optional_str(payload.get("expected_credit_sub_account")),
            search_start_date=_optional_str(payload.get("search_start_date")),
            search_end_date=_optional_str(payload.get("search_end_date")),
        )


@dataclass(frozen=True)
class JournalDuplicateProbeItem:
    source_file_hash: str
    target_hash: str
    status: str
    exact_match_count: int
    amount_only_match_count: int
    account_policy_match: bool | None
    journal_id_hashes: tuple[str, ...]
    voucher_attached_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_hash": self.source_file_hash,
            "target_hash": self.target_hash,
            "status": self.status,
            "exact_match_count": self.exact_match_count,
            "amount_only_match_count": self.amount_only_match_count,
            "account_policy_match": self.account_policy_match,
            "journal_id_hashes": list(self.journal_id_hashes),
            "voucher_attached_count": self.voucher_attached_count,
            "raw_amount_stored": False,
            "raw_description_stored": False,
            "raw_journal_id_stored": False,
        }


def run_journal_duplicate_probe(
    *,
    client: JournalDuplicateReadClient,
    targets: list[JournalDuplicateTarget],
    output_json: Path,
    office_id: str | None = None,
) -> dict[str, Any]:
    if not targets:
        raise ValueError("at least one duplicate target is required")
    output_path = require_ignored_untracked_data_path(output_json, purpose="journal duplicate probe output")
    search_ranges = {
        (
            target.search_start_date or target.journal_date,
            target.search_end_date or target.journal_date,
        )
        for target in targets
    }
    details_by_range = {
        search_range: _journal_details_for_period(
            client,
            start_date=search_range[0],
            end_date=search_range[1],
            office_id=office_id,
        )
        for search_range in sorted(search_ranges)
    }
    items = [
        _probe_target(
            target,
            details_by_range[
                (
                    target.search_start_date or target.journal_date,
                    target.search_end_date or target.journal_date,
                )
            ],
        )
        for target in targets
    ]
    summary = {
        "status": "ready" if all(item.status != "ambiguous_duplicate" for item in items) else "needs_attention",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "target_count": len(items),
        "not_found_count": sum(item.status == "not_found" for item in items),
        "exact_duplicate_count": sum(item.status == "exact_duplicate" for item in items),
        "conflicting_existing_journal_count": sum(
            item.status == "conflicting_existing_journal" for item in items
        ),
        "ambiguous_duplicate_count": sum(item.status == "ambiguous_duplicate" for item in items),
        "items": [item.to_dict() for item in items],
        "moneyforward_write_called": False,
        "raw_amounts_stored": False,
        "raw_descriptions_stored": False,
        "raw_journal_ids_stored": False,
        "raw_api_response_stored": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def load_journal_duplicate_targets(path: Path) -> list[JournalDuplicateTarget]:
    input_path = require_ignored_untracked_data_path(path, purpose="journal duplicate probe input")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    rows = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("duplicate probe input must contain targets[]")
    return [JournalDuplicateTarget.from_dict(row) for row in rows if isinstance(row, dict)]


def redacted_probe_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "items"}


def _probe_target(target: JournalDuplicateTarget, journals: list[dict[str, Any]]) -> JournalDuplicateProbeItem:
    terms = tuple(filter(None, (_normalize_text(term) for term in target.match_terms)))
    date_matches = [
        journal
        for journal in journals
        if _date_key(journal) == target.journal_date
    ]
    amount_matches = [
        journal for journal in date_matches if _journal_total(journal) == target.amount
    ]
    term_matches = [
        journal
        for journal in journals
        if any(term in _normalize_text(_journal_text(journal)) for term in terms)
    ]
    exact = [journal for journal in amount_matches if journal in term_matches]
    account_policy_match: bool | None = None
    status = "not_found"
    if len(exact) == 1:
        account_policy_match = _matches_account_policy(exact[0], target)
        status = "exact_duplicate" if account_policy_match else "conflicting_existing_journal"
    elif len(exact) > 1:
        status = "ambiguous_duplicate"
    elif len(term_matches) == 1:
        # The invoice identity exists, but date and/or amount differs.  Treat
        # this as a conflict so a scoped replay can never POST a second copy.
        status = "conflicting_existing_journal"
    elif len(term_matches) > 1:
        status = "ambiguous_duplicate"
    journal_hashes = tuple(mask_identifier(_journal_id(journal)) for journal in exact if _journal_id(journal))
    return JournalDuplicateProbeItem(
        source_file_hash=target.source_file_hash,
        target_hash=stable_hash(
            {
                "source_file_hash": target.source_file_hash,
                "journal_date": target.journal_date,
                "search_period": (
                    target.search_start_date or target.journal_date,
                    target.search_end_date or target.journal_date,
                ),
                "amount": target.amount,
                "terms": terms,
                "expected_accounts": (
                    target.expected_debit_account,
                    target.expected_debit_sub_account,
                    target.expected_credit_account,
                    target.expected_credit_sub_account,
                ),
            }
        ),
        status=status,
        exact_match_count=len(exact),
        amount_only_match_count=max(0, len(amount_matches) - len(exact)),
        account_policy_match=account_policy_match,
        journal_id_hashes=journal_hashes,
        voucher_attached_count=sum(bool(journal.get("voucher_file_ids")) for journal in exact),
    )


def _journal_details_for_date(
    client: JournalDuplicateReadClient,
    journal_date: str,
    *,
    office_id: str | None,
    per_page: int = 100,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    return _journal_details_for_period(
        client,
        start_date=journal_date,
        end_date=journal_date,
        office_id=office_id,
        per_page=per_page,
        max_pages=max_pages,
    )


def _journal_details_for_period(
    client: JournalDuplicateReadClient,
    *,
    start_date: str,
    end_date: str,
    office_id: str | None,
    per_page: int = 100,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = client.journals(
            office_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            per_page=per_page,
        )
        rows = _extract_items(payload)
        for row in rows:
            journal_id = _journal_id(row)
            details.append(_unwrap_journal(client.journal(journal_id, office_id)) if journal_id else _unwrap_journal(row))
        if len(rows) < per_page:
            return details
    raise RuntimeError("journal duplicate probe pagination exceeded max_pages")


def _journal_total(journal: dict[str, Any]) -> int:
    values = [
        int(_read_back_gross_value(debit, _amount(debit) or 0) or 0)
        for debit, _credit in _paired_journal_lines(journal)
    ]
    if values:
        return sum(values)
    return int(_amount(journal) or 0)


def _matches_account_policy(journal: dict[str, Any], target: JournalDuplicateTarget) -> bool:
    expected = (
        target.expected_debit_account,
        target.expected_debit_sub_account,
        target.expected_credit_account,
        target.expected_credit_sub_account,
    )
    if not any(expected):
        return True
    for line in _normalized_lines(journal):
        checks = (
            target.expected_debit_account is None or line.get("debit_account") == target.expected_debit_account,
            target.expected_debit_sub_account is None
            or line.get("debit_sub_account") == target.expected_debit_sub_account,
            target.expected_credit_account is None or line.get("credit_account") == target.expected_credit_account,
            target.expected_credit_sub_account is None
            or line.get("credit_sub_account") == target.expected_credit_sub_account,
        )
        if all(checks):
            return True
    return False


def _journal_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_journal_text(child) for child in value.values())
    if isinstance(value, list):
        return " ".join(_journal_text(child) for child in value)
    return ""


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _is_sha256_marker(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest.lower())
