from __future__ import annotations

import json
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .hash_utils import mask_identifier
from .journal_backfill import _extract_items, _journal_id, _unwrap_journal
from .local_artifacts import require_ignored_untracked_data_path


class DailyPostedSourceReadClient(Protocol):
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


SOURCE_TERMS: dict[str, tuple[str, ...]] = {
    "sales": ("売上高", "売掛金", "未収入金"),
    "payroll": ("給料手当", "給与", "賞与", "役員報酬"),
    "bank": ("普通預金", "当座預金", "定期預金", "貯蓄預金"),
    "card": ("アメリカンエキスプレス", "アメリカン・エキスプレス", "アメックス", "amex", "クレジットカード"),
    "tax_and_insurance": (
        "租税公課",
        "法定福利費",
        "社会保険",
        "労働保険",
        "雇用保険",
        "健康保険",
        "厚生年金",
    ),
}


def run_daily_posted_source_probe(
    *,
    client: DailyPostedSourceReadClient,
    start_date: str,
    end_date: str,
    output_json: Path,
    office_id: str | None = None,
    per_page: int = 100,
    max_pages: int = 100,
) -> dict[str, Any]:
    _validate_date_range(start_date, end_date)
    output_path = require_ignored_untracked_data_path(
        output_json,
        purpose="daily posted source probe output",
    )
    listing = _fetch_all_journals(
        client,
        office_id=office_id,
        start_date=start_date,
        end_date=end_date,
        per_page=per_page,
        max_pages=max_pages,
    )
    category_hashes = {key: [] for key in SOURCE_TERMS}
    unclassified_hashes: list[str] = []
    unreadable_listing_count = 0
    seen_ids: set[str] = set()

    for listed in listing:
        journal_id = _journal_id(listed)
        if not journal_id:
            unreadable_listing_count += 1
            continue
        if journal_id in seen_ids:
            continue
        seen_ids.add(journal_id)
        detail = _unwrap_journal(client.journal(journal_id, office_id))
        if not detail:
            raise RuntimeError("Money Forward journal detail read-back returned no journal")
        journal_hash = mask_identifier(journal_id)
        normalized_text = _normalize_text(" ".join(_walk_text(detail)))
        matched = False
        for source_key, terms in SOURCE_TERMS.items():
            if any(_normalize_text(term) in normalized_text for term in terms):
                category_hashes[source_key].append(journal_hash)
                matched = True
        if not matched:
            unclassified_hashes.append(journal_hash)

    categories = {
        key: {
            "posted_journal_count": len(hashes),
            "journal_id_hashes": sorted(set(hashes)),
        }
        for key, hashes in category_hashes.items()
    }
    result = {
        "status": "ready" if unreadable_listing_count == 0 else "needs_attention",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "listed_journal_count": len(listing),
        "detail_readback_count": len(seen_ids),
        "unreadable_listing_count": unreadable_listing_count,
        "categories": categories,
        "unclassified_journal_count": len(unclassified_hashes),
        "unclassified_journal_id_hashes": sorted(set(unclassified_hashes)),
        "posted_journal_readback_covered": unreadable_listing_count == 0,
        "unposted_queue_covered": False,
        "proves_current_unposted_zero": False,
        "moneyforward_write_called": False,
        "raw_amounts_stored": False,
        "raw_descriptions_stored": False,
        "raw_journal_ids_stored": False,
        "raw_api_response_stored": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def redacted_posted_source_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "checked_at": result["checked_at"],
        "start_date": result["start_date"],
        "end_date": result["end_date"],
        "listed_journal_count": result["listed_journal_count"],
        "detail_readback_count": result["detail_readback_count"],
        "unreadable_listing_count": result["unreadable_listing_count"],
        "category_counts": {
            key: value["posted_journal_count"]
            for key, value in result["categories"].items()
        },
        "unclassified_journal_count": result["unclassified_journal_count"],
        "posted_journal_readback_covered": result["posted_journal_readback_covered"],
        "unposted_queue_covered": False,
        "moneyforward_write_called": False,
        "raw_data_stored": False,
    }


def _fetch_all_journals(
    client: DailyPostedSourceReadClient,
    *,
    office_id: str | None,
    start_date: str,
    end_date: str,
    per_page: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    journals: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = client.journals(
            office_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            per_page=per_page,
        )
        items = _extract_items(payload)
        journals.extend(items)
        if len(items) < per_page:
            return journals
    raise RuntimeError("Money Forward journal pagination exceeded the configured page limit")


def _validate_date_range(start_date: str, end_date: str) -> None:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("start_date and end_date must be ISO dates") from exc
    if start > end:
        raise ValueError("start_date must not be after end_date")


def _walk_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for child in value.values():
            values.extend(_walk_text(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_walk_text(child))
        return values
    return []


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace(" ", "")
