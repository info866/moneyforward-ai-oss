"""Synthetic regression: an unknown native link is not proof of a UI candidate."""

from mf_phase12.invoice_accounting_link_probe import _normalize_accrual_item


def test_unconfirmed_native_journal_does_not_imply_ui_candidate():
    match = {
        "billing": {
            "id": "synthetic-billing-1",
            "billing_number": "SYN-001",
            "sales_date": "2026-02-01",
            "partner_name": "Synthetic Customer",
            "total_price": 1100,
            "excise_price": 100,
            "items": [{"name": "Synthetic service", "excise": "ten_percent"}],
        },
        "category_candidates": ["売上高/業務委託・事務委託"],
        "sales_accrual_candidates": [],
    }
    item = _normalize_accrual_item(match)
    assert item["native_linkage_status"] == "registered_journal_not_confirmed"
    assert item["native_source_verified"] is False
    assert item["native_journal_ref_hashes"] == []


from mf_phase12.cli import _sales_invoice_workflow_steps


def test_sales_invoice_three_stage_status_all_verified():
    stages = _sales_invoice_workflow_steps(billing_count=2, verified_count=2)
    assert [s["step"] for s in stages] == [
        "invoice_api", "moneyforward_native_link", "accounting_api_readback"
    ]
    assert [s["status"] for s in stages] == ["verified"] * 3


def test_sales_invoice_three_stage_status_partial_and_unknown():
    partial = _sales_invoice_workflow_steps(billing_count=2, verified_count=1)
    assert [s["status"] for s in partial] == [
        "verified", "needs_attention", "partial"
    ]
    unknown = _sales_invoice_workflow_steps(billing_count=2, verified_count=0)
    assert [s["status"] for s in unknown] == [
        "verified", "needs_attention", "needs_attention"
    ]
