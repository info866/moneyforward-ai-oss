"""Offline regressions for complete accounting journal inventory (no API I/O)."""
from __future__ import annotations

import pytest

from mf_phase12.sync import _fetch_all_journals


class SyntheticJournalClient:
    def __init__(self, pages):
        self.pages = pages
        self.called_pages = []

    def journals(self, office_id=None, *, start_date, end_date, page, per_page):
        assert office_id == "synthetic-office"
        assert (start_date, end_date) == ("2026-01-01", "2026-01-31")
        assert per_page == 2
        self.called_pages.append(page)
        return self.pages[page]


def row(value):
    return {"id": "synthetic-journal-" + value, "transaction_date": "2026-01-15"}


def listing(items, *, pages=None, count=None):
    payload = {"journals": items}
    if pages is not None:
        payload["metadata"] = {"total_pages": pages, "total_count": count}
    return payload


def scan(pages, *, max_pages=10):
    client = SyntheticJournalClient(pages)
    try:
        result = _fetch_all_journals(
            client, "synthetic-office", start_date="2026-01-01",
            end_date="2026-01-31", per_page=2, max_pages=max_pages,
        )
        return result, client.called_pages
    except Exception:
        # No external calls; expose attempted page numbers through the client in
        # any debugging session, but never reveal business record identifiers.
        raise


def test_complete_two_page_inventory_matches_authoritative_metadata():
    rows, calls = scan({
        1: listing([row("a"), row("b")], pages=2, count=3),
        2: listing([row("c")], pages=2, count=3),
    })
    assert [r["id"] for r in rows] == [
        "synthetic-journal-a", "synthetic-journal-b", "synthetic-journal-c",
    ]
    assert calls == [1, 2]


def test_complete_two_page_inventory_without_metadata_uses_short_page():
    rows, calls = scan({
        1: listing([row("a"), row("b")]),
        2: listing([row("c")]),
    })
    assert len(rows) == 3 and calls == [1, 2]


@pytest.mark.parametrize("bad_pages", [
    {1: listing([row("a"), row("b")], pages=2, count=3),
     2: listing([row("a")], pages=2, count=3)},
    {1: listing([row("a"), row("a")], pages=1, count=2)},
])
def test_duplicate_journal_ids_fail_closed(bad_pages):
    with pytest.raises(RuntimeError, match="duplicate journal"):
        scan(bad_pages)


def test_missing_last_page_cannot_pass_as_completed():
    with pytest.raises(RuntimeError, match="incomplete|count|missing|empty"):
        scan({
            1: listing([row("a"), row("b")], pages=3, count=4),
            2: listing([], pages=3, count=4),
        })


def test_max_page_exhaustion_must_never_return_partial_snapshot():
    with pytest.raises(RuntimeError, match="page limit|incomplete"):
        scan({1: listing([row("a"), row("b")]),
              2: listing([row("c"), row("d")])}, max_pages=2)


@pytest.mark.parametrize("payload", [
    {},
    {"journals": "not-an-array"},
    {"journals": [row("a"), "not-an-object"]},
    {"journals": [{"transaction_date": "2026-01-15"}]},
    {"journals": [row("a")], "metadata": {"total_pages": "bad", "total_count": 1}},
    {"journals": [row("a")], "metadata": {"total_pages": True, "total_count": 1}},
    {"journals": [row("a")], "metadata": {"total_pages": 1, "total_count": -1}},
    {"journals": [row("a")], "metadata": {"total_pages": 1}},
])
def test_malformed_pages_or_missing_identity_are_rejected(payload):
    with pytest.raises((RuntimeError, ValueError), match="journal|pagination|metadata|invalid"):
        scan({1: payload})


def test_metadata_count_mismatch_is_not_a_complete_snapshot():
    with pytest.raises(RuntimeError, match="count|incomplete"):
        scan({1: listing([row("a"), row("b")], pages=1, count=3)})


def test_metadata_changes_during_pagination_are_rejected():
    with pytest.raises(RuntimeError, match="metadata.*changed|pagination"):
        scan({
            1: listing([row("a"), row("b")], pages=2, count=3),
            2: listing([row("c")], pages=2, count=4),
        })


def test_excess_rows_on_one_page_are_rejected():
    with pytest.raises(RuntimeError, match="page|per_page"):
        scan({1: listing([row("a"), row("b"), row("c")], pages=1, count=3)})
