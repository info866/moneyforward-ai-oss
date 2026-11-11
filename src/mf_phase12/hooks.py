from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .drive_policy import is_invoice_workflow, resolve_credit_side_master
from .execution_policy import validate_execution_channel
from .hash_utils import mask_identifier, stable_hash
from .models import Evidence, JournalCandidate
from .shiwake_workflow_api import should_activate_shiwake_autorun


AUTO_POST_STATES = {"limited", "enabled"}
ROUTE_PATTERN_MIN_SUPPORT = 3


@dataclass(frozen=True)
class HookResult:
    name: str
    passed: bool
    reason: str


class Phase12Hooks:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def execution_channel(
        self,
        *,
        operation: str,
        selected_channel: str,
        public_api_supported: bool = True,
        payroll_identifier_available: bool = False,
        candidate_bound: bool = False,
        simple_single_branch_transaction: bool = True,
    ) -> HookResult:
        try:
            passed, reason = validate_execution_channel(
                operation,
                selected_channel,
                public_api_supported=public_api_supported,
                payroll_identifier_available=payroll_identifier_available,
                candidate_bound=candidate_bound,
                simple_single_branch_transaction=simple_single_branch_transaction,
            )
        except ValueError as exc:
            return HookResult("execution_channel", False, str(exc))
        return HookResult("execution_channel", passed, reason)

    def duplicate(self, evidence: Evidence, candidate_hash: str, dry_run_result_hash: str) -> HookResult:
        file_id_hash = mask_identifier(evidence.google_drive_file_id)
        row = self.conn.execute(
            """
            SELECT idempotency_key FROM processing_ledger
            WHERE processing_status NOT IN ('new', 'unprocessed', 'blocked', 'failed')
              AND (google_drive_file_id_hash = ? OR (candidate_hash = ? AND dry_run_result_hash = ?))
            LIMIT 1
            """,
            (file_id_hash, candidate_hash, dry_run_result_hash),
        ).fetchone()
        if row:
            return HookResult("duplicate", False, "既存のDrive file IDまたは候補hashがあるため二重登録を停止")
        return HookResult("duplicate", True, "重複なし")

    def rag_freshness(self, max_age_hours: int = 24) -> HookResult:
        row = self.conn.execute(
            """
            SELECT synced_at, status
            FROM sync_manifests
            WHERE source_name = 'moneyforward'
              AND source_kind = 'journal_patterns'
            ORDER BY synced_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return HookResult("rag_freshness", False, "journal_patterns RAG同期manifestがない")
        if row["status"] != "ok":
            return HookResult("rag_freshness", False, f"最新journal_patterns RAG同期statusが{row['status']}")
        synced_at = datetime.fromisoformat(row["synced_at"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - synced_at
        if age > timedelta(hours=max_age_hours):
            return HookResult("rag_freshness", False, "RAG同期が古いため自動登録停止")
        return HookResult("rag_freshness", True, "RAGは新しい")

    def amount_tax(self, candidate: JournalCandidate) -> HookResult:
        if candidate.amount <= 0:
            return HookResult("amount_tax", False, "金額が0以下")
        if candidate.tax_amount < 0:
            return HookResult("amount_tax", False, "税額が負数")
        if candidate.tax_category == "対象外" and candidate.tax_amount != 0:
            return HookResult("amount_tax", False, "対象外税区分なのに税額がある")
        if candidate.tax_rate > 0:
            expected = round((candidate.amount - candidate.tax_amount) * candidate.tax_rate)
            if abs(expected - candidate.tax_amount) > 2:
                return HookResult("amount_tax", False, "金額と税額と税率が整合しない")
        return HookResult("amount_tax", True, "金額・税額整合")

    def invoice_number(self, candidate: JournalCandidate) -> HookResult:
        value = candidate.invoice_number
        if not value:
            return HookResult("invoice_number", True, "インボイス番号なし。必須判定は別Hookで扱う")
        if re.fullmatch(r"T\d{13}", value):
            return HookResult("invoice_number", True, "インボイス番号形式OK")
        if re.search(r"(商品|注文|伝票|電話|TEL|No)", value, re.IGNORECASE):
            return HookResult("invoice_number", False, "商品番号・注文番号・電話番号との混同疑い")
        return HookResult("invoice_number", False, "インボイス番号形式不正")

    def master_resolution(self, candidate: JournalCandidate, evidence: Evidence | None = None) -> HookResult:
        if is_invoice_workflow(candidate, evidence):
            if not candidate.trade_partner_code:
                return HookResult("master_resolution", False, "請求書系仕訳のtrade_partner_codeが未解決")
            if candidate.trade_partner_name:
                rows = _active_master_rows(self.conn, "trade_partner", candidate.trade_partner_name)
                if len(rows) != 1:
                    return HookResult(
                        "master_resolution",
                        False,
                        f"取引先:{candidate.trade_partner_name} が一意に解決できない",
                    )
        checks = [
            ("account", candidate.debit_account),
            ("account", candidate.credit_account),
            ("tax", candidate.tax_category),
        ]
        for item_type, name in checks:
            rows = _active_master_rows(self.conn, item_type, name)
            if len(rows) != 1:
                return HookResult("master_resolution", False, f"{item_type}:{name} が一意に解決できない")
        credit_decision = resolve_credit_side_master(self.conn, candidate, evidence)
        if credit_decision.sub_account_creation_required:
            return HookResult(
                "master_resolution",
                False,
                f"貸方補助科目:{credit_decision.credit_sub_account} が未登録。登録候補を作成し、承認後にMoney Forwardマスタへ追加する",
            )
        return HookResult("master_resolution", True, "マスタ一意解決")

    def evidence(self, evidence: Evidence) -> HookResult:
        if not evidence.exists:
            return HookResult("evidence", False, "証憑ファイルが存在しない")
        if not evidence.attachable:
            return HookResult("evidence", False, "証憑ファイルが添付不能")
        return HookResult("evidence", True, "証憑添付前提OK")

    def auto_post_eligibility(self, candidate: JournalCandidate) -> HookResult:
        if _is_user_confirmed_customs_payment(candidate):
            return HookResult("auto_post_eligibility", True, "ユーザー確認済み関税支払ルール")
        if _is_user_confirmed_withholding_tax_special_due_correction(candidate):
            return HookResult("auto_post_eligibility", True, "ユーザー確認済み源泉所得税納期特例補正")
        tax_aliases = sorted(_tax_category_aliases(candidate.tax_category))
        placeholders = ",".join("?" for _ in tax_aliases)
        rows = self.conn.execute(
            f"""
            SELECT auto_post_eligibility
            FROM journal_patterns
            WHERE description_key = ?
              AND debit_account = ?
              AND COALESCE(debit_sub_account, '') = ?
              AND credit_account = ?
              AND COALESCE(credit_sub_account, '') = ?
              AND tax_category IN ({placeholders})
            """,
            (
                candidate.description or "no_description",
                candidate.debit_account,
                candidate.debit_sub_account or "",
                candidate.credit_account,
                candidate.credit_sub_account or "",
                *tax_aliases,
            ),
        ).fetchall()
        if not rows:
            route_row = self.conn.execute(
                f"""
                SELECT SUM(success_count) AS support_count,
                       COUNT(*) AS pattern_count,
                       GROUP_CONCAT(DISTINCT auto_post_eligibility) AS states
                FROM journal_patterns
                WHERE amount_bucket = ?
                  AND debit_account = ?
                  AND COALESCE(debit_sub_account, '') = ?
                  AND credit_account = ?
                  AND COALESCE(credit_sub_account, '') = ?
                  AND tax_category IN ({placeholders})
                  AND auto_post_eligibility IN ('limited', 'enabled')
                """,
                (
                    _amount_bucket(candidate.amount),
                    candidate.debit_account,
                    candidate.debit_sub_account or "",
                    candidate.credit_account,
                    candidate.credit_sub_account or "",
                    *tax_aliases,
                ),
            ).fetchone()
            support_count = int((route_row or {})["support_count"] or 0)
            pattern_count = int((route_row or {})["pattern_count"] or 0)
            if support_count >= ROUTE_PATTERN_MIN_SUPPORT and pattern_count > 0:
                return HookResult(
                    "auto_post_eligibility",
                    True,
                    f"反復済み会計ルートpatternに一致 support={support_count}",
                )
            invoice_history = self._outgoing_invoice_sales_history(candidate)
            if invoice_history >= ROUTE_PATTERN_MIN_SUPPORT:
                return HookResult(
                    "auto_post_eligibility",
                    True,
                    f"同一取引先の検証済み売上履歴に一致 support={invoice_history}",
                )
            return HookResult("auto_post_eligibility", False, "製品ゲートを通過した過去仕訳patternがない")
        states = {row["auto_post_eligibility"] for row in rows}
        if states & AUTO_POST_STATES:
            return HookResult("auto_post_eligibility", True, "限定自動登録patternに一致")
        invoice_history = self._outgoing_invoice_sales_history(candidate)
        if invoice_history >= ROUTE_PATTERN_MIN_SUPPORT:
            return HookResult(
                "auto_post_eligibility",
                True,
                f"同一取引先の検証済み売上履歴に一致 support={invoice_history}",
            )
        return HookResult("auto_post_eligibility", False, f"自動登録対象外pattern: {','.join(sorted(states))}")

    def _outgoing_invoice_sales_history(self, candidate: JournalCandidate) -> int:
        """Allow a new line item only for an evidenced, recurring outgoing-invoice route."""
        if not _is_outgoing_invoice_sales_candidate(candidate):
            return 0
        tax_aliases = sorted(_sales_history_tax_aliases(candidate.tax_category))
        placeholders = ",".join("?" for _ in tax_aliases)
        row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM(success_count), 0) AS support_count
            FROM journal_patterns
            WHERE debit_account = ?
              AND COALESCE(debit_sub_account, '') = ?
              AND credit_account = ?
              AND tax_category IN ({placeholders})
              AND auto_post_eligibility IN ('limited', 'enabled')
            """,
            (
                candidate.debit_account,
                candidate.debit_sub_account or "",
                candidate.credit_account,
                *tax_aliases,
            ),
        ).fetchone()
        return int((row or {})["support_count"] or 0)

    def write_safety(self, candidate: JournalCandidate, prior_results: list[HookResult]) -> HookResult:
        failed = [result.name for result in prior_results if not result.passed]
        if failed:
            return HookResult("write_safety", False, f"失敗Hookあり: {','.join(failed)}")
        if candidate.risk_flags:
            return HookResult("write_safety", False, "未解決risk flagあり")
        return HookResult("write_safety", True, "書き込み安全条件OK")

    def google_chat_readability(self, message_text: str, *, expected_item_count: int) -> HookResult:
        if expected_item_count <= 0:
            return HookResult("google_chat_readability", False, "通知対象件数が0")
        if message_text.count("\n\n") < expected_item_count:
            return HookResult("google_chat_readability", False, "仕訳ごとの空行が不足")
        forbidden = ("仕訳:", "課仕10%", "課税仕入 10%", "2026-")
        leaked = [value for value in forbidden if value in message_text]
        if leaked:
            return HookResult("google_chat_readability", False, f"チャットに出さない情報が含まれる: {','.join(leaked)}")
        return HookResult("google_chat_readability", True, "Google Chat通知形式OK")

    def shiwake_autorun_trigger(self, instruction: str) -> HookResult:
        if should_activate_shiwake_autorun(instruction):
            return HookResult("shiwake_autorun_trigger", True, "仕分け指示を標準自動実行入口へ接続")
        return HookResult("shiwake_autorun_trigger", False, "仕分け起動語がない")

    def rag_learning_closure(self, *, require_payroll: bool = False) -> HookResult:
        required = ["verified_journal_learning"]
        if require_payroll:
            required.append("payroll_verified_journal_learning")
        missing: list[str] = []
        for source_kind in required:
            row = self.conn.execute(
                """
                SELECT status, item_count
                FROM sync_manifests
                WHERE source_name = 'local'
                  AND source_kind = ?
                ORDER BY synced_at DESC, id DESC
                LIMIT 1
                """,
                (source_kind,),
            ).fetchone()
            if not row or row["status"] != "ok" or int(row["item_count"] or 0) <= 0:
                missing.append(source_kind)
        if missing:
            return HookResult("rag_learning_closure", False, "RAG学習未完了: " + ",".join(missing))
        return HookResult("rag_learning_closure", True, "検証済み仕訳/RAG学習完了")


def candidate_hash(candidate: JournalCandidate) -> str:
    return stable_hash(candidate.to_dict())


def _active_master_rows(conn: sqlite3.Connection, item_type: str, name: str) -> list[sqlite3.Row]:
    exact_name = _master_comparison_name(name)
    exact_rows = _master_rows_for_comparison_names(conn, item_type, [exact_name])
    if exact_rows or item_type != "tax":
        return _prefer_moneyforward_rows(exact_rows)
    comparison_names = sorted(
        {_master_comparison_name(alias) for alias in _tax_category_aliases(name)} - {exact_name}
    )
    return _prefer_moneyforward_rows(_master_rows_for_comparison_names(conn, item_type, comparison_names))


def _master_rows_for_comparison_names(
    conn: sqlite3.Connection,
    item_type: str,
    comparison_names: list[str],
) -> list[sqlite3.Row]:
    if not comparison_names:
        return []
    placeholders = ",".join("?" for _ in comparison_names)
    return conn.execute(
        f"""
        SELECT id, source_name
        FROM master_items
        WHERE item_type = ?
          AND REPLACE(REPLACE(name_normalized, ' ', ''), '　', '') IN ({placeholders})
          AND active = 1
        """,
        (item_type, *comparison_names),
    ).fetchall()


def _prefer_moneyforward_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    moneyforward_rows = [row for row in rows if row["source_name"] == "moneyforward"]
    return moneyforward_rows or rows


def _master_comparison_name(value: str) -> str:
    return re.sub(r"[\s　]+", "", value or "")


def _tax_category_aliases(tax_category: str) -> set[str]:
    aliases = {tax_category}
    if tax_category == "課税仕入 10%":
        aliases.add("課仕 10%")
    if tax_category == "課仕 10%":
        aliases.add("課税仕入 10%")
    if tax_category == "課税仕入 (軽)8%":
        aliases.add("課仕 (軽)8%")
    if tax_category == "課仕 (軽)8%":
        aliases.add("課税仕入 (軽)8%")
    return aliases


def _sales_history_tax_aliases(tax_category: str) -> set[str]:
    aliases = _tax_category_aliases(tax_category)
    if tax_category in {"課税売上 10%", "課売 10%", "課税売上 8%", "課売 8%"}:
        # Historical sync stored the debit-side tax (対象外) for some sales
        # journals. This alias is restricted to the outgoing-sales history check.
        aliases.add("対象外")
    return aliases


def _is_outgoing_invoice_sales_candidate(candidate: JournalCandidate) -> bool:
    return (
        candidate.source_type == "invoice"
        and candidate.debit_account == "未収入金"
        and candidate.credit_account == "売上高"
        and bool(candidate.debit_sub_account)
        and bool(candidate.trade_partner_code)
    )


def _is_user_confirmed_customs_payment(candidate: JournalCandidate) -> bool:
    if candidate.source_type != "customs_payment":
        return False
    if "日本郵便" not in candidate.vendor_name:
        return False
    if candidate.credit_account != "未払金":
        return False
    if candidate.debit_account not in {"支払手数料", "租税公課", "仮払消費税"}:
        return False
    return candidate.tax_category in {
        "課税仕入 10%",
        "課仕 10%",
        "対象外",
        "輸入仕入-消費税額 7.8%",
        "輸入仕入-地方消費税額 2.2%",
    }


def _is_user_confirmed_withholding_tax_special_due_correction(candidate: JournalCandidate) -> bool:
    marker_text = " ".join(
        str(value or "")
        for value in (
            candidate.vendor_name,
            candidate.description,
            " ".join(candidate.risk_flags),
        )
    )
    return (
        candidate.source_type == "withholding_tax_correction"
        and candidate.debit_account == "未収入金"
        and candidate.debit_sub_account is None
        and candidate.credit_account == "預り金"
        and candidate.credit_sub_account == "所得税"
        and candidate.tax_category == "対象外"
        and candidate.tax_amount == 0
        and candidate.tax_rate == 0
        and "源泉所得税" in marker_text
        and "納期特例" in marker_text
        and "ユーザー確認済み" in marker_text
    )


def _amount_bucket(amount: int) -> str:
    if amount <= 0:
        return "unknown"
    if amount < 1_000:
        return "micro"
    if amount < 5_000:
        return "small"
    if amount < 10_000:
        return "small_mid"
    if amount < 50_000:
        return "mid"
    if amount < 100_000:
        return "large_mid"
    return "100000+"
