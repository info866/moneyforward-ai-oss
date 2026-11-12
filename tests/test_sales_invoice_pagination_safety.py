"""Fail-closed source inventory: synthetic inputs, no provider/network/credentials."""

import pytest

from mf_phase12.invoice_accounting_link_probe import (
    _fetch_accounting_journals,
    _fetch_paged,
)


def invoice_row(i):
    return {"id": f"synthetic-invoice-{i}"}


def invoice_page(rows, *, total_pages=None, total_count=None, page=None):
    payload = {"data": rows}
    if total_pages is not None:
        payload["pagination"] = {
            "total_pages": total_pages,
            "total_count": total_count,
            "current_page": page,
        }
    return payload


def test_invoice_pagination_complete_two_pages_with_metadata():
    called = []
    pages = {
        1: invoice_page([invoice_row(1), invoice_row(2)], total_pages=2, total_count=3, page=1),
        2: invoice_page([invoice_row(3)], total_pages=2, total_count=3, page=2),
    }
    result = _fetch_paged(lambda page: (called.append(page), pages[page])[1], max_pages=2, per_page=2)
    assert [r["id"] for r in result] == [r["id"] for r in (invoice_row(1), invoice_row(2), invoice_row(3))]
    assert called == [1, 2]


def test_invoice_pagination_unconfirmed_full_page_cap_must_fail():
    pages = {page: invoice_page([invoice_row(page * 100 + i) for i in range(100)]) for page in (1, 2)}
    with pytest.raises(RuntimeError, match="incomplete|page limit"):
        _fetch_paged(pages.__getitem__, max_pages=2)


@pytest.mark.parametrize("payload", [
    {},
    {"data": "invalid"},
    {"data": [invoice_row(1), "invalid"]},
    {"data": [{"name": "missing id"}]},
])
def test_invoice_pagination_invalid_page_or_identity_must_fail(payload):
    with pytest.raises(RuntimeError, match="page|identity|rows|invalid"):
        _fetch_paged(lambda _page: payload, max_pages=2)


def test_invoice_pagination_duplicate_id_must_fail():
    pages = {1: invoice_page([invoice_row(1)]), 2: invoice_page([])}
    with pytest.raises(RuntimeError, match="duplicate"):
        _fetch_paged(lambda p: {"data": [invoice_row(1)], "pagination": {
            "total_pages": 2, "total_count": 2, "current_page": p
        }} if p == 2 else {"data": [invoice_row(1)], "pagination": {
            "total_pages": 2, "total_count": 2, "current_page": 1
        }}, max_pages=2)


def test_invoice_pagination_wrong_metadata_count_must_fail():
    with pytest.raises(RuntimeError, match="count|incomplete|pagination"):
        _fetch_paged(lambda _page: invoice_page([invoice_row(1)], total_pages=1, total_count=2, page=1), max_pages=2)


def test_invoice_pagination_later_http_400_is_not_end_of_list():
    def fetch(page):
        if page == 1:
            return invoice_page([invoice_row(i) for i in range(100)])
        raise RuntimeError("Money Forward Invoice API failed HTTP 400")
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _fetch_paged(fetch, max_pages=2)


class SyntheticAccountingClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def journals(self, office_id=None, *, start_date, end_date, page, per_page):
        assert office_id is None
        assert (start_date, end_date, per_page) == ("2026-01-01", "2026-01-31", 2)
        self.calls.append(page)
        return self.pages[page]


def accounting_page(rows, *, count=None, pages=None):
    result = {"data": rows}
    if count is not None:
        result["metadata"] = {"total_pages": pages, "total_count": count}
    return result


def journal(i):
    return {"id": f"synthetic-journal-{i}", "transaction_date": "2026-01-15"}


def scan(pages, *, max_pages=2):
    client = SyntheticAccountingClient(pages)
    results = _fetch_accounting_journals(accounting_client=client, start_date="2026-01-01", end_date="2026-01-31", max_pages=max_pages, per_page=2)
    return results, client.calls


def test_journal_pagination_two_pages_valid():
    results, calls = scan({1: accounting_page([journal(1), journal(2)], count=3, pages=2), 2: accounting_page([journal(3)], count=3, pages=2)})
    assert len(results) == 3 and calls == [1, 2]


@pytest.mark.parametrize("pages", [
    {1: accounting_page([journal(1), journal(2)]), 2: accounting_page([journal(3), journal(4)])},
    {1: accounting_page([journal(1), journal(2)], count=3, pages=2), 2: accounting_page([journal(1)], count=3, pages=2)},
    {1: accounting_page([journal(1), {"transaction_date": "2026-01-15"}])},
    {1: {"unrecognized": [journal(1)]}},
    {1: accounting_page([journal(1), journal(2)], count=3, pages=2), 2: accounting_page([], count=3, pages=2)},
])
def test_journal_pagination_partial_duplicate_or_malformed_must_fail(pages):
    with pytest.raises(RuntimeError, match="journal|page|pagination|incomplete|duplicate"):
        scan(pages)


def test_invoice_next_page_metadata_valid_two_page_navigation():
    pages = {
        1: {"data": [invoice_row(1)], "pagination": {"next_page": 2}},
        2: {"data": [invoice_row(2)], "pagination": {"next_page": None}},
    }
    result = _fetch_paged(pages.__getitem__, max_pages=2, per_page=2)
    assert [x["id"] for x in result] == ["synthetic-invoice-1", "synthetic-invoice-2"]


def test_invoice_next_page_metadata_cannot_silently_truncate():
    pages = {
        1: {"data": [invoice_row(1)], "pagination": {"next_page": 2}},
        2: {"data": [invoice_row(2)], "pagination": {"next_page": 3}},
    }
    with pytest.raises(RuntimeError, match="incomplete|page limit"):
        _fetch_paged(pages.__getitem__, max_pages=2, per_page=2)


def test_invoice_line_items_without_ids_are_not_silently_removed():
    pages = {1: {"data": [{"name": "synthetic item"}], "pagination": {"next_page": None}}}
    assert _fetch_paged(pages.__getitem__, max_pages=2, require_ids=False) == [
        {"name": "synthetic item"}
    ]

class SyntheticInvoiceSource:
    def __init__(self, *, bad_billings=False):
        self.bad_billings = bad_billings

    def partners(self, *, page, per_page):
        return invoice_page(
            [{"id": "synthetic-partner", "name": "Synthetic Customer"}],
            total_pages=1, total_count=1, page=page,
        )

    def billings(self, *, page, per_page):
        return invoice_page(
            [{
                "id": "synthetic-billing", "billing_number": "SYN-2026",
                "billing_date": "2026/01/15", "sales_date": "2026/01/15",
                "partner_id": "synthetic-partner", "total_price": 1100,
                "excise_price": 100,
            }],
            total_pages=1,
            total_count=2 if self.bad_billings else 1,
            page=page,
        )

    def billing(self, billing_id):
        assert billing_id == "synthetic-billing"
        return {"data": [{"id": billing_id}]}

    def billing_items(self, billing_id, *, page, per_page):
        assert billing_id == "synthetic-billing"
        return invoice_page(
            [{"name": "Synthetic service", "excise": "ten_percent"}],
            total_pages=1, total_count=1, page=page,
        )


class EmptyAccountingSource:
    def __init__(self):
        self.calls = 0

    def journals(self, office_id=None, *, start_date, end_date, page, per_page):
        self.calls += 1
        return accounting_page([], count=0, pages=0)


def test_sales_probe_can_read_synthetic_invoice_without_generic_journal_post():
    from mf_phase12.invoice_accounting_link_probe import run_invoice_accounting_link_probe

    accounting = EmptyAccountingSource()
    result = run_invoice_accounting_link_probe(
        invoice_client=SyntheticInvoiceSource(),
        accounting_client=accounting,
        start_date="2026-01-01", end_date="2026-01-31",
        max_pages=2, per_page=2,
    )
    assert result.invoice_billing_count == 1
    assert result.accounting_journal_count == 0
    assert result.moneyforward_write_called is False
    assert result.status == "needs_action"
    assert accounting.calls == 1


def test_sales_probe_invalid_invoice_inventory_stops_before_accounting_calls():
    from mf_phase12.invoice_accounting_link_probe import run_invoice_accounting_link_probe

    accounting = EmptyAccountingSource()
    with pytest.raises(RuntimeError, match="incomplete total_count"):
        run_invoice_accounting_link_probe(
            invoice_client=SyntheticInvoiceSource(bad_billings=True),
            accounting_client=accounting,
            start_date="2026-01-01", end_date="2026-01-31",
            max_pages=2, per_page=2,
        )
    assert accounting.calls == 0
