from __future__ import annotations
from .office_profile import get_current_profile
from .shiwake_run import (
    _setting,
    _office_name,
    _mf_principal,
    _drive_principal,
    _artifact_root,
)
import sqlite3
from typing import Any
from .daily_accounting_control import build_daily_accounting_control
from .execution_policy import build_execution_policy_contract
from .shiwake_run import (
    PARALLEL_WORKFLOWS,
    RUN_STAGES,
    STAFF_LIMIT_SECONDS,
    STAGE_TARGET_SECONDS,
    VETERAN_TARGET_SECONDS,
)
from .supplier_primary_watchlist import supplier_primary_watchlist_status

SHIWAKE_TRIGGER_PHRASES: tuple[str, ...] = (
    "仕分けして",
    "仕訳して",
    "仕分け作業",
    "仕訳作業",
    "Money Forward 仕分け",
    "マネーフォワードで仕分け",
    "普通預金の未仕訳",
    "configured_companyの未仕訳",
)
WORKFLOW_STEPS: tuple[str, ...] = PARALLEL_WORKFLOWS
GOOGLE_CHAT_COMPLETION_FORMAT = (
    "No.<仕訳番号> <相手先> <金額>  <借方科目/補助/> → <貸方科目/補助> 証憑OK"
)


def should_activate_shiwake_autorun(instruction: str) -> bool:
    text = _normalize_text(instruction)
    return any((_normalize_text(phrase) in text for phrase in SHIWAKE_TRIGGER_PHRASES))


def build_shiwake_workflow_contract() -> dict[str, Any]:
    execution_policy = build_execution_policy_contract()
    return {
        "status": "ready",
        "workflow_steps": list(WORKFLOW_STEPS),
        "accounting_run_stage_order": list(RUN_STAGES),
        "parallel_workflows": list(PARALLEL_WORKFLOWS),
        "execution_order_authoritative": False,
        "accounting_run_is_completion_authority": False,
        "trigger_phrases": list(SHIWAKE_TRIGGER_PHRASES),
        "minimal_instruction_activates_autorun": True,
        "skill_fixed": True,
        "hook_fixed": True,
        "mcp_read_tools_fixed": True,
        "api_fixed": True,
        "execution_policy": execution_policy,
        "rag_learning_required": True,
        "startup": {
            "single_startup_check": 'make run ARGS="contract"',
            "daily_control_check": "make phase12-daily-accounting-control",
            "no_repeated_full_research": True,
            "classification_scope_gate_cached_until_db_changes": True,
            "ordinary_deposit_unposted_is_in_scope": True,
            "selected_bank_unposted_is_in_scope": True,
        },
        "performance_policy": {
            "veteran_target_seconds": VETERAN_TARGET_SECONDS,
            "staff_limit_seconds": STAFF_LIMIT_SECONDS,
            "stage_target_seconds": dict(STAGE_TARGET_SECONDS),
            "ordinary_batch_human_confirmation_count": 1,
            "target_retry_count": 0,
            "overrun_stops_work": False,
            "quality_gates_unchanged": True,
            "primary_slowdown_causes": [
                "毎回の入口判断を会話でやり直した",
                "Drive/Gmail証憑と銀行API未仕訳を別扱いにした",
                "Money Forward連携明細APIの公開後も旧UI取得経路を標準として残していた",
                "RAGがreadyでも同じ広範囲ゲートを再実行した",
            ],
            "fast_path": [
                "起動時にstartup_source_triageで対象ソースだけを確定",
                "RAG/分類scopeはDB状態確認で済ませ、RAG再構築は同期古さや不足時だけ実行",
                "GET /api/v3/transactionsでconfigured_companyの全連携明細を一度取得し、financial_event_ingest -> classifyへ直結",
                "取引ID・連携口座・仕訳候補を結合し、POST /api/v3/transactions/journalizeを実行",
                "明細status=registered、仕訳本体、transaction_id結合、未仕訳件数を同じAPIでread-back",
                "公式API障害を記録した場合のみMoney Forward UI表コピー/TSV/Computer Useを一時代替にする",
            ],
            "pdca_recovery_fixed": execution_policy["pdca"],
            "normal_batch_passes": [
                "one_startup_preflight",
                "one_complete_manifest_and_classification",
                "one_execution",
                "one_authoritative_readback",
                "one_chat_learning_issue_closure",
            ],
            "prohibited_rework": [
                "per_item_confirmation_for_ordinary_journals",
                "reexecution_of_succeeded_stages",
                "full_rag_rebuild_without_binding_loss_or_empty_memory",
                "repository_full_test_during_accounting_operations",
                "command_by_command_issue_updates",
            ],
        },
        "moneyforward_source_policy": {
            "accounting_api": {
                "journals": "read_write_supported_with_approval",
                "vouchers": "attach_supported_with_approval",
                "masters": "read_supported",
                "connected_service_unposted_candidates": "GET /api/v3/transactions with mfc/accounting/transaction.read",
                "connected_service_direct_journalize": "POST /api/v3/transactions/journalize with mfc/accounting/journal.write and exact candidate binding",
                "connected_service_candidate_body_verification": {
                    "command": "make phase12-connected-account-probe",
                    "public_api_boundary": "GET /api/v3/connected_accounts lists connected services; GET /api/v3/transactions reads statement bodies and journalizing status; POST /api/v3/transactions/journalize creates a journal bound to one statement",
                    "when_api_outage_verified": "record the API error, then use Money Forward UI table copy / TSV / Computer Use temporarily",
                },
            },
            "invoice_from_input": {
                "ui_feature": "自動で仕訳 > 請求書から入力",
                "scope": "our_issued_invoice_sales_ar_and_expected_deposit_candidates",
                "not_for": "received_vendor_invoices_or_receipts",
                "api_data_source": "Money Forward Cloud Invoice API can read invoice/billing/partner data when mfc/invoice/data.read is authorized",
                "candidate_registration_api": "not confirmed as public for accounting UI candidate IDs; use accounting journal API or Computer Use UI fallback after data retrieval",
            },
            "payroll_from_input": {
                "ui_feature": "自動で仕訳 > 給与から入力",
                "official_payroll_api": "can read finalized payrolls, payroll details, payment/deduction/company-burden items, and payroll groups",
                "api_requirement": "requires Money Forward-issued identifier plus office_api_key",
                "preferred_when_identifier_granted": "Payroll API -> payroll RAG fixed rules -> accounting journal API",
                "current_default": "Payroll API is not currently available; use Computer Use UI and wage ledger CSV/PDF payroll statement intake",
                "fallback": "Computer Use UI / wage ledger CSV/PDF payroll statement intake until identifier is granted",
            },
            "computer_use_ui_operator": {
                "workflow_folder": "workflows/moneyforward/INTAKE_EVIDENCE.md",
                "status": "exception_fallback_for_verified_api_outage_or_documented_api_gap",
                "requires_readback": True,
                "ui_change_policy": "when Money Forward UI/UA changes, update the workflow folder and rerun contract/status verification before continuing",
            },
        },
        "google_chat": {
            "completion_and_pending_are_separate": True,
            "blank_line_between_items": True,
            "completion_format": GOOGLE_CHAT_COMPLETION_FORMAT,
            "hidden_fields": ["date", "long_description", "仕訳:", "tax_category"],
            "delivery_ledger_required": True,
            "legacy_summary_can_mark_sent": False,
            "send_after_cross_source_post_audit": True,
            "one_completion_batch_per_accounting_run": True,
        },
        "daily_accounting_control": {
            "parallel_sources": list(PARALLEL_WORKFLOWS),
            "journal_post_requires_all_sources_captured_zero_or_not_applicable": True,
            "post_audit_requires": [
                "duplicate_journal_check",
                "debit_credit_amount_check",
                "account_sub_account_tax_check",
                "source_statement_evidence_reconciliation",
                "processed_folder_readback",
            ],
        },
        "completion_evidence": {
            "accounting_run_status_complete_required": False,
            "all_run_stages_succeeded_required": False,
            "journal_readback_required": True,
            "voucher_readback_or_not_required": True,
            "source_parent_removed": True,
            "office_visible_processed_folder": "<人物>/処理完了/",
            "central_archive_metadata_is_sufficient": False,
        },
        "payroll_rules": {
            "accrual_date": "pay_period_end",
            "payment_due": _setting("payroll.payment_due"),
            "negative_net_handling": "do_not_use_accounts_receivable; offset through withholding/social_insurance liability",
            "personal_raw_amounts_in_rag": False,
            "employee_specific_payable_required": True,
            "aggregate_payroll_liability_prohibited": True,
            "generic_payroll_sub_accounts_prohibited": ["給与", "従業員", "給与一式"],
            "employee_allocation_verification_required_before_write_queue": True,
            "payroll_pattern_auto_post_allowed": True,
            "payroll_auto_post_requires_verified_employee_allocation": True,
            "payroll_auto_post_requires_accrual_readback": True,
            "partial_settlement_requires_exact_employee_binding": True,
        },
        "write_boundary": {
            "journal_write_requires_approval": True,
            "voucher_write_requires_approval": True,
            "google_drive_move_requires_approval": True,
            "read_only_mcp_write_disabled": True,
        },
    }


def build_shiwake_autorun_startup_check(
    conn: sqlite3.Connection,
    *,
    connected_account_probe: dict[str, Any] | None = None,
    unposted_financial_event_count: int | None = None,
    daily_source_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = build_shiwake_workflow_contract()
    bank_counts = _bank_income_pattern_counts(conn)
    learning = build_learning_status(conn)
    supplier_primary_watchlist = supplier_primary_watchlist_status(conn)
    notification = build_notification_delivery_status(conn)
    daily_control = build_daily_accounting_control(daily_source_inventory)
    office_binding = _office_binding_status(conn)
    active_run_count = int(
        conn.execute(
            "SELECT COUNT(*) AS count FROM accounting_runs WHERE status != 'complete'"
        ).fetchone()["count"]
    )
    connected_api_verified = bool(
        connected_account_probe
        and connected_account_probe.get("unposted_financial_event_api_verified") is True
    )
    connected_probe_reason = "not_provided"
    if connected_account_probe:
        connected_probe_reason = str(
            connected_account_probe.get("unposted_financial_event_api_reason")
            or connected_account_probe.get("reason")
            or connected_account_probe.get("status")
            or "unknown"
        )
    next_actions: list[str] = []
    if not daily_control.journal_post_allowed:
        next_actions.append(
            "売上請求書・支払請求書・全領収書・給与/賞与・銀行・カード・税金/保険を順に棚卸しし、取得済み/0件確認/対象外根拠ありになるまで仕訳POST・完了通知を行わない"
        )
    if unposted_financial_event_count is None and (not connected_api_verified):
        next_actions.append(
            "mfc/accounting/transaction.readを確認し、GET /api/v3/transactionsでconfigured_companyの未仕訳明細本文と件数を取得してから完了判定する"
        )
    elif (unposted_financial_event_count or 0) > 0:
        if connected_api_verified:
            next_actions.append(
                "Money Forward未仕訳明細をfinancial_event_ingest -> classifyへ渡し、取引IDを結合したPOST /api/v3/transactions/journalizeで処理する"
            )
        else:
            next_actions.append(
                "API読み取り未確認のため、まずGET /api/v3/transactionsで対象行とtransaction_idを再取得し、画面の見た目だけで仕訳しない"
            )
    if bank_counts["limited_bank_income_patterns"] <= 0:
        next_actions.append(
            "普通預金入金消込RAGが不足しているため、過去仕訳同期またはlearningを先に実行する"
        )
    if bank_counts["selected_bank_income_patterns"] <= 0:
        next_actions.append(
            "configured_company入金消込RAGが不足しているため、configured_companyの過去仕訳patternを同期する"
        )
    if notification["delivery_uncertain_count"] > 0:
        next_actions.append(
            "Google Chat送達不明をIssueで宛先照合するまで自動再送しない"
        )
    rag_refresh_required = (
        not office_binding["verified"] or learning["journal_pattern_count"] <= 0
    )
    fast_path_blockers: list[str] = []
    if not office_binding["verified"]:
        fast_path_blockers.append("configured_company_office_binding_not_verified")
    if learning["journal_pattern_count"] <= 0:
        fast_path_blockers.append("journal_pattern_memory_empty")
    return {
        "status": "needs_attention" if next_actions else "ready",
        "trigger_ready": True,
        "minimal_instruction_activates_autorun": contract[
            "minimal_instruction_activates_autorun"
        ],
        "startup_check": contract["startup"],
        "ordinary_deposit_unposted_in_scope": True,
        "selected_bank_unposted_in_scope": True,
        "unposted_financial_event_count": unposted_financial_event_count,
        "unposted_financial_event_discovery_verified": connected_api_verified,
        "bank_income_rag": bank_counts,
        "connected_service_unposted_api": {
            "verified": connected_api_verified,
            "reason": connected_probe_reason,
            "verification_command": "make phase12-connected-account-probe",
            "candidate_body_get_endpoint": connected_account_probe.get(
                "public_accounting_api_candidate_body_get_endpoint"
            )
            if connected_account_probe
            else "not_checked",
            "journalize_endpoint": "/api/v3/transactions/journalize",
            "required_read_scope": "mfc/accounting/transaction.read",
            "required_write_scope": "mfc/accounting/journal.write",
            "fallback": "moneyforward_ui_tsv_or_computer_use_after_verified_api_outage",
            "fallback_is_standard_workflow": False,
        },
        "invoice_and_payroll_api_policy": contract["moneyforward_source_policy"],
        "daily_accounting_control": daily_control.to_dict(),
        "supplier_primary_card_duplicate_watchlist": supplier_primary_watchlist,
        "notification_delivery": notification,
        "performance_policy": contract["performance_policy"],
        "fast_path": {
            "profile": "normal_small_batch",
            "preflight_ready": not fast_path_blockers,
            "blockers": fast_path_blockers,
            "active_incomplete_run_count": active_run_count,
            "resume_existing_run_before_rediscovery": active_run_count > 0,
            "ordinary_batch_additional_confirmation_required": False,
            "one_complete_manifest_required": True,
            "full_rag_rebuild_required": rag_refresh_required,
            "incremental_rag_learning_preferred": not rag_refresh_required,
            "repository_full_test_required_during_accounting_run": False,
            "quality_gates_unchanged": True,
            "historical_maintenance_non_blocking": {
                "google_chat_delivery_uncertain_count": notification[
                    "delivery_uncertain_count"
                ],
                "policy": "do_not_block_current_ready_batch",
            },
        },
        "office_identity_binding": office_binding,
        "next_actions": next_actions,
        "learning_status": {
            "journal_pattern_count": learning["journal_pattern_count"],
            "financial_event_learning_event_count": learning[
                "financial_event_learning_event_count"
            ],
            "raw_amounts_printed": learning["raw_amounts_printed"],
            "raw_secret_stored": learning["raw_secret_stored"],
        },
        "raw_amounts_printed": False,
        "raw_ids_printed": False,
        "moneyforward_write_called": False,
        "google_drive_write_called": False,
    }


def build_notification_delivery_status(conn: sqlite3.Connection) -> dict[str, Any]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'google_chat_notification_records'"
    ).fetchone()
    if not table:
        return {
            "status": "needs_attention",
            "reason": "delivery_ledger_missing",
            "delivery_uncertain_count": 0,
            "sent_count": 0,
        }
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM google_chat_notification_records GROUP BY status"
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    uncertain = counts.get("uncertain_failure", 0)
    dispatching = counts.get("dispatching", 0)
    return {
        "status": "needs_attention" if uncertain or dispatching else "ready",
        "delivery_uncertain_count": uncertain,
        "dispatching_count": dispatching,
        "sent_count": counts.get("sent", 0),
        "legacy_summary_can_mark_sent": False,
        "raw_message_stored": False,
        "raw_secret_stored": False,
    }


def build_learning_status(conn: sqlite3.Connection) -> dict[str, Any]:
    manifests = conn.execute(
        "\n        SELECT source_name, source_kind, status, item_count, synced_at\n        FROM sync_manifests\n        WHERE source_kind IN (\n            'verified_journal_learning',\n            'payroll_verified_journal_learning',\n            'financial_event_learning'\n        )\n        ORDER BY synced_at DESC, id DESC\n        "
    ).fetchall()
    card_counts = conn.execute(
        "\n        SELECT card_type, COUNT(*) AS count\n        FROM rag_cards\n        GROUP BY card_type\n        ORDER BY card_type\n        "
    ).fetchall()
    pattern_count = conn.execute(
        "SELECT COUNT(*) AS count FROM journal_patterns"
    ).fetchone()["count"]
    learning_event_count = conn.execute(
        "SELECT COUNT(*) AS count FROM financial_event_learning_events"
    ).fetchone()["count"]
    return {
        "status": "ready",
        "journal_pattern_count": int(pattern_count),
        "rag_card_counts": {
            str(row["card_type"]): int(row["count"]) for row in card_counts
        },
        "financial_event_learning_event_count": int(learning_event_count),
        "learning_manifests": [dict(row) for row in manifests],
        "raw_amounts_printed": False,
        "raw_secret_stored": False,
    }


def _bank_income_pattern_counts(conn):
    debit = str(_setting("bank.income_debit_account"))
    credit = str(_setting("bank.income_credit_account"))
    subaccount = str(_setting("bank.income_sub_account"))
    base = "SELECT COUNT(*) AS count FROM journal_patterns WHERE debit_account=? AND credit_account=? AND auto_post_eligibility IN ('limited','enabled')"
    total = conn.execute(base, (debit, credit)).fetchone()["count"]
    selected = conn.execute(
        base + " AND COALESCE(debit_sub_account, '') = ?", (debit, credit, subaccount)
    ).fetchone()["count"]
    return {
        "limited_bank_income_patterns": int(total),
        "selected_bank_income_patterns": int(selected),
    }


def _office_binding_status(conn):
    from .office_profile import require_database_office_identity

    profile = get_current_profile()
    binding = require_database_office_identity(conn, profile)
    return {
        "verified": True,
        "binding_count": 1,
        "profile": profile.slug,
        "binding_hash": binding.binding_hash,
        "raw_office_id_stored": False,
        "raw_secret_stored": False,
    }


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())
