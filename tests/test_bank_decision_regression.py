from __future__ import annotations

import json

from datetime import datetime, timezone

from pathlib import Path

from types import SimpleNamespace

from mf_phase12 import cli

from mf_phase12 import connected_account_probe

from mf_phase12.bank_transfer import decide_bank_transactions, is_bank_transaction

from mf_phase12.db import Phase12Database

from mf_phase12.hash_utils import accounting_run_hash, mask_identifier, stable_hash
import pytest
from mf_phase12.office_profile import using_office_profile
from test_starter_accounting import company, bound, Api

def _first_transaction_ai_decision(packet: dict, **overrides) -> dict:
    return {
        "packet_hash": packet["packet_hash"],
        "status": "decided",
        "counterpart_account": "支払手数料",
        "counterpart_sub_account": "",
        "tax_category": "対象外",
        "basis_codes": ["current_bank_facts", "accounting_rule"],
        **overrides,
    }


@pytest.fixture
def company_profile(tmp_path):
    profile = company(tmp_path, 'bank-tenant')
    bound(profile, Api())
    with using_office_profile(profile):
        yield profile

def _event(
    *, direction: str = "inflow", description: str = "ABC payment", amount: int = 50000
) -> dict:
    side = "INCOME" if direction == "inflow" else "EXPENSE"
    transaction_id = "transaction-1"
    account_id = "connected-1"
    sub_id = "connected-sub-1"
    ledger_id = "bank-account-id"
    ledger_sub_id = "bank-sub-id"
    return {
        "id": transaction_id,
        "source_event_id": transaction_id,
        "transaction_id": transaction_id,
        "source_event_id_hash": mask_identifier(transaction_id),
        "source_event_type": "bank_transaction",
        "source_api": "GET /api/v3/transactions",
        "transaction_date": "2026-08-25",
        "transaction_original_date": "2026-08-25",
        "description": description,
        "counterparty": "ABC",
        "amount": amount,
        "transaction_value": amount,
        "cash_flow_direction": direction,
        "transaction_side": side,
        "journalizing_status": "none",
        "connected_account_id": account_id,
        "connected_sub_account_id": sub_id,
        "connected_ledger_account_id": ledger_id,
        "connected_ledger_sub_account_id": ledger_sub_id,
        "source_row_hash": stable_hash({"row": transaction_id}),
        "transaction_facts_hash": stable_hash({"facts": transaction_id}),
        "connected_account_binding_hash": mask_identifier(f"{account_id}:{sub_id}"),
        "connected_ledger_binding_hash": mask_identifier(
            f"{account_id}:{sub_id}:{ledger_id}:{ledger_sub_id}"
        ),
    }

def _masters() -> dict[str, list[dict]]:
    return {
        "accounts": [
            {"id": "bank-account-id", "name": "普通預金", "available": True},
            {"id": "receivable", "name": "未収入金", "available": True},
            {"id": "payable", "name": "未払金", "available": True},
            {"id": "payroll", "name": "未払費用", "available": True},
            {"id": "fee", "name": "支払手数料", "available": True},
        ],
        "sub_accounts": [
            {
                "id": "abc-sub",
                "name": "ABC",
                "account_id": "receivable",
                "account_name": "未収入金",
                "available": True,
            },
            {
                "id": "vendor-sub",
                "name": "Vendor",
                "account_id": "payable",
                "account_name": "未払金",
                "available": True,
            },
            {
                "id": "employee-sub",
                "name": "Employee",
                "account_id": "payroll",
                "account_name": "未払費用",
                "available": True,
            },
        ],
        "taxes": [{"id": "tax-out", "name": "対象外", "available": True}],
        "trade_partners": [],
    }

def _insert_pattern(conn, *, pattern_hash: str, credit_sub: str = "ABC", debit: str = "普通預金") -> None:
    conn.execute(
        "INSERT INTO journal_patterns "
        "(pattern_hash,vendor_key,description_key,amount_bucket,debit_account,"
        "debit_sub_account,credit_account,credit_sub_account,tax_category,"
        "success_count,failure_count,auto_post_eligibility) "
        "VALUES (?,?,?,?,?,?,?,?,?,3,0,'limited')",
        (
            pattern_hash,
            "ABC",
            "abc payment",
            "mid",
            debit,
            None,
            "未収入金",
            credit_sub,
            "対象外",
        ),
    )

def test_bank_scope_uses_live_ordinary_deposit_binding() -> None:
    bank = _event()
    amazon = {**bank, "connected_ledger_account_id": "fee"}

    assert is_bank_transaction(bank, _masters()) is True
    assert is_bank_transaction(amazon, _masters()) is False

def test_verified_single_rag_candidate_becomes_ready(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event()
    with db.connect() as conn:
        _insert_pattern(conn, pattern_hash="pattern-1")
        result = decide_bank_transactions([event], conn=conn, masters=_masters())

    item = result.items[0]
    assert item.registration_allowed is True
    assert item.matched_pattern_source == "verified_rag_current_facts"
    assert item.journal_candidate["credit_account"] == "未収入金"
    assert item.journal_candidate["credit_sub_account"] == "ABC"
    binding = item.journal_candidate["moneyforward_transaction_binding"]
    assert binding["transaction_id"] == event["transaction_id"]
    assert binding["transaction_value"] == "50000"
    assert binding["transaction_side"] == "INCOME"

def test_multiple_rag_candidates_are_uniquely_resolved_by_open_related_journal(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event()
    masters = _masters()
    masters["sub_accounts"].append(
        {"id": "other-sub", "name": "Other", "account_id": "receivable", "account_name": "未収入金", "available": True}
    )
    with db.connect() as conn:
        _insert_pattern(conn, pattern_hash="pattern-1", credit_sub="ABC")
        _insert_pattern(conn, pattern_hash="pattern-2", credit_sub="Other")
        result = decide_bank_transactions(
            [event],
            conn=conn,
            masters=masters,
            related_candidates=[
                {
                    "cash_flow_direction": "inflow",
                    "amount": 50000,
                    "open_balance": 50000,
                    "account": "未収入金",
                    "sub_account": "ABC",
                    "counterparty": "ABC",
                    "tax_category": "対象外",
                    "source": "official_sales_receivable_balance",
                }
            ],
        )

    assert result.items[0].registration_allowed is True
    assert result.items[0].matched_pattern_source == "official_sales_receivable_balance"
    assert result.items[0].journal_candidate["credit_sub_account"] == "ABC"

def test_zero_rag_first_transaction_waits_for_ai_without_review(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="first bank fee")
    with db.connect() as conn:
        result = decide_bank_transactions([event], conn=conn, masters=_masters())

    assert result.items[0].registration_allowed is False
    assert result.items[0].recommended_action == "queue_for_learning"
    assert result.items[0].unresolved_points == ["ai_decision_required"]
    assert result.review_reasons == [[]]
    assert len(result.ai_packets) == 1
    assert ("未収入金", "ABC") in result.ai_packets[0]["live_master"]["sub_accounts"]

def test_zero_rag_first_transaction_ai_decision_becomes_ready(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="first bank fee")
    with db.connect() as conn:
        pending = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), persist=False
        )
        packet = pending.ai_packets[0]
        result = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            ai_decisions={packet["packet_hash"]: _first_transaction_ai_decision(packet)},
            persist=False,
        )

    item = result.items[0]
    assert item.registration_allowed is True
    assert item.matched_pattern_source == "ai_first_transaction_current_facts"
    assert item.journal_candidate["debit_account"] == "支払手数料"
    assert item.journal_candidate["credit_account"] == "普通預金"
    assert item.journal_candidate["moneyforward_transaction_binding"]["transaction_value"] == "50000"

def test_first_transaction_ai_review_requires_specific_reason(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="unknown first payment")
    with db.connect() as conn:
        pending = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), persist=False
        )
        packet = pending.ai_packets[0]
        result = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            ai_decisions={
                packet["packet_hash"]: {
                    "packet_hash": packet["packet_hash"],
                    "status": "review",
                    "reason_codes": ["necessary_evidence_missing"],
                }
            },
            persist=False,
        )
        invalid_reason = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            ai_decisions={
                packet["packet_hash"]: {
                    "packet_hash": packet["packet_hash"],
                    "status": "review",
                    "reason_codes": ["no_verified_rag_candidate"],
                }
            },
            persist=False,
        )

    assert result.items[0].registration_allowed is False
    assert result.items[0].recommended_action == "queue_for_review"
    assert result.items[0].unresolved_points == ["necessary_evidence_missing"]
    assert invalid_reason.items[0].recommended_action == "queue_for_learning"
    assert invalid_reason.items[0].unresolved_points == [
        "ai_decision_required",
        "ai_review_reason_invalid",
    ]

def test_first_transaction_ai_cannot_override_facts_or_bypass_live_master(
    tmp_path: Path, company_profile,
) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="first bank fee")
    with db.connect() as conn:
        pending = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), persist=False
        )
        packet = pending.ai_packets[0]
        fixed_override = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            ai_decisions={
                packet["packet_hash"]: _first_transaction_ai_decision(
                    packet, amount=1
                )
            },
            persist=False,
        )
        missing_master = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            ai_decisions={
                packet["packet_hash"]: _first_transaction_ai_decision(
                    packet, counterpart_account="Not In Master"
                )
            },
            persist=False,
        )

    assert fixed_override.items[0].recommended_action == "queue_for_learning"
    assert fixed_override.items[0].unresolved_points == [
        "ai_decision_required",
        "ai_fixed_fact_override_attempt",
    ]
    assert missing_master.items[0].unresolved_points == [
        "counterpart_account_master_unresolved"
    ]

def test_tied_related_candidates_and_master_unresolved_are_review_only(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event()
    related = [
        {
            "cash_flow_direction": "inflow",
            "amount": 50000,
            "open_balance": 50000,
            "account": "未収入金",
            "sub_account": name,
            "counterparty": "ABC",
            "tax_category": "対象外",
        }
        for name in ("ABC", "Missing")
    ]
    with db.connect() as conn:
        result = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), related_candidates=related
        )

    assert result.items[0].registration_allowed is False
    assert "related_journal_candidates_tied" in result.items[0].unresolved_points

def test_direction_or_amount_conflict_does_not_select_related_candidate(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event()
    with db.connect() as conn:
        result = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            related_candidates=[
                {
                    "cash_flow_direction": "outflow",
                    "amount": 49999,
                    "open_balance": 50000,
                    "account": "未払金",
                    "sub_account": "Vendor",
                    "tax_category": "対象外",
                }
            ],
        )

    assert result.items[0].registration_allowed is False
    assert result.items[0].unresolved_points == ["ai_decision_required"]

def test_invoice_payment_and_payroll_payment_clear_their_open_liability(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    cases = (("未払金", "Vendor"), ("未払費用", "Employee"))
    with db.connect() as conn:
        for account, sub_account in cases:
            event = _event(direction="outflow", description=sub_account)
            result = decide_bank_transactions(
                [event],
                conn=conn,
                masters=_masters(),
                related_candidates=[
                    {
                        "cash_flow_direction": "outflow",
                        "amount": 50000,
                        "open_balance": 50000,
                        "account": account,
                        "sub_account": sub_account,
                        "counterparty": sub_account,
                        "tax_category": "対象外",
                        "source": "official_open_liability",
                    }
                ],
                persist=False,
            )
            assert result.items[0].registration_allowed is True
            assert result.items[0].journal_candidate["debit_account"] == account
            assert result.items[0].journal_candidate["debit_sub_account"] == sub_account

def test_already_settled_related_journal_is_not_a_ready_candidate(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="Vendor")
    with db.connect() as conn:
        result = decide_bank_transactions(
            [event],
            conn=conn,
            masters=_masters(),
            related_candidates=[
                {
                    "cash_flow_direction": "outflow",
                    "amount": 50000,
                    "open_balance": 0,
                    "account": "未払金",
                    "sub_account": "Vendor",
                    "counterparty": "Vendor",
                    "tax_category": "対象外",
                }
            ],
            persist=False,
        )

    assert result.items[0].registration_allowed is False
    assert result.items[0].unresolved_points == ["ai_decision_required"]

def test_same_inputs_are_deterministic_ten_times_and_skip_ai_when_rag_is_unique(tmp_path: Path, company_profile) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event()
    with db.connect() as conn:
        _insert_pattern(conn, pattern_hash="pattern-1")
        results = [
            decide_bank_transactions(
                [event], conn=conn, masters=_masters(), persist=False
            ).to_local_dict()
            for _ in range(10)
        ]

    assert all(result == results[0] for result in results)

def test_first_transaction_ai_artifact_is_deterministic_ten_times(
    tmp_path: Path, company_profile,
) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="first bank fee")
    with db.connect() as conn:
        pending = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), persist=False
        )
        packet = pending.ai_packets[0]
        decisions = {
            packet["packet_hash"]: _first_transaction_ai_decision(packet)
        }
        results = [
            decide_bank_transactions(
                [event],
                conn=conn,
                masters=_masters(),
                ai_decisions=decisions,
                persist=False,
            ).to_local_dict()
            for _ in range(10)
        ]

    assert all(result == results[0] for result in results)

def test_first_transaction_ai_decision_is_invalidated_when_live_master_changes(
    tmp_path: Path, company_profile,
) -> None:
    db = Phase12Database(company_profile.moneyforward_db_path)
    db.initialize()
    event = _event(direction="outflow", description="first bank fee")
    with db.connect() as conn:
        pending = decide_bank_transactions(
            [event], conn=conn, masters=_masters(), persist=False
        )
        old_packet = pending.ai_packets[0]
        masters = _masters()
        masters["accounts"].append(
            {"id": "other", "name": "Other Account", "available": True}
        )
        changed = decide_bank_transactions(
            [event],
            conn=conn,
            masters=masters,
            ai_decisions={
                old_packet["packet_hash"]: _first_transaction_ai_decision(
                    old_packet
                )
            },
            persist=False,
        )

    assert changed.ai_packets[0]["packet_hash"] != old_packet["packet_hash"]
    assert changed.items[0].unresolved_points == ["ai_decision_required"]
