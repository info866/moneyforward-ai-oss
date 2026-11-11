from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Protocol

from .drive_policy import (
    DIRECT_LEDGER_SOURCE_TYPES,
    FINANCIAL_STATEMENT_SOURCE_TYPES,
    apply_drive_credit_policy,
    folder_credit_decision,
    is_invoice_workflow,
    resolve_credit_side_master,
)
from .evidence_source import DRIVE, INVOICE_API, evidence_source_type
from .hash_utils import mask_identifier, normalize_sha256_hash, stable_hash
from .hooks import HookResult, Phase12Hooks, candidate_hash
from .models import Evidence, JournalCandidate


@dataclass(frozen=True)
class DryRunPlan:
    idempotency_key: str
    candidate_hash: str
    dry_run_result_hash: str
    registration_allowed: bool
    reasons: tuple[str, ...]
    next_actions: tuple[str, ...] = ()


class JournalWriteClient(Protocol):
    def create_journal(self, plan: DryRunPlan) -> str: ...


class MoneyForwardDryRunSDK:
    """Local deterministic SDK facade.

    It never calls Money Forward by itself. It prepares and verifies plans so
    write-capable API code can be added without changing the safety contract.
    """

    def __init__(self, conn: sqlite3.Connection, actor: str = "codex"):
        self.conn = conn
        self.actor = actor

    def plan_journal_create(
        self,
        evidence: Evidence,
        candidate: JournalCandidate,
        *,
        verified_evidence_resolution: bool = False,
        authoritative_invoice_source: bool = False,
    ) -> DryRunPlan:
        evidence = replace(evidence, file_hash=normalize_sha256_hash(evidence.file_hash))
        source_type = evidence_source_type(evidence)
        if authoritative_invoice_source and source_type != INVOICE_API:
            raise ValueError(
                "authoritative_invoice_source requires Money Forward Invoice API evidence"
            )
        if not authoritative_invoice_source:
            candidate = apply_drive_credit_policy(candidate, evidence)
            candidate = _apply_resolved_credit_side(self.conn, candidate, evidence)
        c_hash = candidate_hash(candidate)
        dry_payload = {"candidate": candidate.to_dict(), "evidence_file_hash": evidence.file_hash}
        dry_hash = stable_hash(dry_payload)
        idem = stable_hash(
            {
                "source_file_id": mask_identifier(evidence.google_drive_file_id),
                "file_hash": evidence.file_hash,
                "candidate_hash": c_hash,
                "dry_run_result_hash": dry_hash,
            }
        )
        source_file_id_hash = mask_identifier(evidence.google_drive_file_id)
        resumable_plan = self.conn.execute(
            """
            SELECT p.idempotency_key
            FROM processing_ledger p
            WHERE p.google_drive_file_id_hash=? AND p.file_hash=?
              AND p.candidate_hash=? AND p.dry_run_result_hash=?
              AND p.processing_status='planned'
              AND NOT EXISTS (
                  SELECT 1 FROM journal_create_execution_records e
                  WHERE e.idempotency_key=p.idempotency_key
              )
            ORDER BY p.updated_at DESC,p.id DESC LIMIT 1
            """,
            (source_file_id_hash, evidence.file_hash, c_hash, dry_hash),
        ).fetchone()

        hooks = Phase12Hooks(self.conn)
        duplicate = hooks.duplicate(evidence, c_hash, dry_hash)
        if resumable_plan is not None:
            duplicate = HookResult(
                "duplicate",
                True,
                "exact durable planned item resumed before external claim",
            )
        auto_post = (
            HookResult(
                "auto_post_eligibility",
                True,
                (
                    "official Invoice API facts and live accounting masters are authoritative"
                    if authoritative_invoice_source
                    else "verified PDF facts exactly match one current financial event"
                ),
            )
            if verified_evidence_resolution
            else hooks.auto_post_eligibility(candidate)
        )
        rag_freshness = (
            HookResult(
                "rag_freshness",
                True,
                "official Invoice API facts do not depend on a local RAG snapshot",
            )
            if authoritative_invoice_source or verified_evidence_resolution
            else hooks.rag_freshness()
        )
        master_resolution = (
            HookResult(
                "master_resolution",
                True,
                "accounts, sub-accounts, tax and trade partner were resolved from live masters",
            )
            if authoritative_invoice_source or verified_evidence_resolution
            else hooks.master_resolution(candidate, evidence)
        )
        evidence_gate = (
            HookResult(
                "evidence",
                True,
                "official Invoice API billing and line-item GETs are the accounting source",
            )
            if authoritative_invoice_source
            else hooks.evidence(evidence)
        )
        results = [
            duplicate,
            rag_freshness,
            hooks.amount_tax(candidate),
            hooks.invoice_number(candidate),
            master_resolution,
            evidence_gate,
            auto_post,
        ]
        results.append(hooks.write_safety(candidate, results))
        allowed = all(result.passed for result in results)
        reasons = tuple(f"{result.name}:{result.reason}" for result in results)
        next_actions = _blocked_next_actions(candidate, evidence, results)

        ledger_row = self.conn.execute(
            "SELECT id, processing_status, evidence_source_type FROM processing_ledger WHERE idempotency_key = ?", (idem,)
        ).fetchone()
        if ledger_row and source_type != DRIVE and ledger_row["evidence_source_type"] == DRIVE:
            # Legacy rows classified every non-invoice source as Drive. Correct
            # only that one-way legacy state; never rewrite one explicit source
            # type into another.
            self.conn.execute(
                "UPDATE processing_ledger SET evidence_source_type = ? WHERE id = ?",
                (source_type, ledger_row["id"]),
            )
            ledger_row = self.conn.execute(
                "SELECT id, processing_status, evidence_source_type FROM processing_ledger WHERE id = ?",
                (ledger_row["id"],),
            ).fetchone()
        # A policy/master/RAG update can make the exact same candidate eligible
        # after a prior local-only blocked plan. Replace only before a write has
        # happened; executed or verified chains are never reopened.
        if ledger_row and ledger_row["processing_status"] in {"new", "blocked", "failed"}:
            self._replace_reclassifiable_ledger(
                ledger_id=int(ledger_row["id"]),
                candidate_hash=c_hash,
                processing_status="planned" if allowed else "blocked",
                dry_run_result_hash=dry_hash,
            )
            ledger_row = self.conn.execute(
                "SELECT id, processing_status, evidence_source_type FROM processing_ledger WHERE id = ?", (ledger_row["id"],)
            ).fetchone()
        if ledger_row is None:
            source_ledger = self.conn.execute(
                """
                SELECT id, idempotency_key, processing_status,
                       candidate_hash, dry_run_result_hash
                FROM processing_ledger
                WHERE google_drive_file_id_hash = ? AND file_hash = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (source_file_id_hash, evidence.file_hash),
            ).fetchone()
            source_has_execution = bool(
                source_ledger
                and self.conn.execute(
                    "SELECT 1 FROM journal_create_execution_records WHERE idempotency_key=?",
                    (source_ledger["idempotency_key"],),
                ).fetchone()
            )
            if source_ledger and (
                source_ledger["processing_status"] in {"new", "blocked", "failed"}
                or (
                    source_ledger["processing_status"] == "planned"
                    and not source_has_execution
                )
            ):
                self._replace_reclassifiable_ledger(
                    ledger_id=int(source_ledger["id"]),
                    candidate_hash=c_hash,
                    processing_status="planned" if allowed else "blocked",
                    dry_run_result_hash=dry_hash,
                )
                # An evidence chain keeps its original idempotency key so all prior
                # blocked-plan audit records remain referentially intact.
                idem = str(source_ledger["idempotency_key"])
                ledger_row = self.conn.execute("SELECT id FROM processing_ledger WHERE id = ?", (source_ledger["id"],)).fetchone()
            elif (
                source_ledger
                and source_ledger["processing_status"] == "planned"
                and source_ledger["candidate_hash"] == c_hash
                and source_ledger["dry_run_result_hash"] == dry_hash
                and self.conn.execute(
                    "SELECT 1 FROM journal_create_execution_records WHERE idempotency_key=?",
                    (source_ledger["idempotency_key"],),
                ).fetchone()
                is None
            ):
                # A crash after planning but before the external claim must
                # resume the exact same durable item instead of creating a
                # second identity or silently skipping the ready transaction.
                idem = str(source_ledger["idempotency_key"])
                ledger_row = self.conn.execute(
                    "SELECT id,processing_status,evidence_source_type FROM processing_ledger WHERE id=?",
                    (source_ledger["id"],),
                ).fetchone()
            elif source_ledger:
                self.conn.commit()
                return DryRunPlan(idem, c_hash, dry_hash, False, reasons, next_actions)
            else:
                self.conn.execute(
                    """
                    INSERT INTO processing_ledger (
                        google_drive_file_id_hash, file_name, file_hash, evidence_source_type, candidate_hash,
                        idempotency_key, processing_status, dry_run_result_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_file_id_hash,
                        evidence.file_name,
                        evidence.file_hash,
                        source_type,
                        c_hash,
                        idem,
                        "planned" if allowed else "blocked",
                        dry_hash,
                    ),
                )
                ledger_row = self.conn.execute(
                    "SELECT id, processing_status, evidence_source_type FROM processing_ledger WHERE idempotency_key = ?", (idem,)
                ).fetchone()
        if ledger_row is None:
            raise RuntimeError("processing ledger is missing after dry-run planning")
        ledger_id = ledger_row["id"]
        plan_hash = stable_hash(
            {
                "operation_type": "journal_create",
                "idempotency_key": idem,
                "reasons": reasons,
                "next_actions": next_actions,
            }
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO write_intents (
                idempotency_key, ledger_id, operation_type, source_file_id_hash,
                candidate_hash, dry_run_result_hash, plan_hash, request_hash, status
            ) VALUES (?, ?, 'journal_create', ?, ?, ?, ?, ?, ?)
            """,
            (
                idem,
                ledger_id,
                source_file_id_hash,
                c_hash,
                dry_hash,
                plan_hash,
                stable_hash(dry_payload),
                "planned" if allowed else "blocked",
            ),
        )
        for result in results:
            self._record_gate_result(idem, result.name, result.passed, result.reason)
        self._audit(
            "plan_journal_create",
            idem,
            dry_payload,
            {"allowed": allowed, "reasons": reasons, "next_actions": next_actions},
            True,
        )
        self.conn.commit()
        return DryRunPlan(idem, c_hash, dry_hash, allowed, reasons, next_actions)

    def _replace_reclassifiable_ledger(
        self,
        *,
        ledger_id: int,
        candidate_hash: str,
        processing_status: str,
        dry_run_result_hash: str,
    ) -> None:
        """Replace a blocked/failed plan only before any Money Forward write exists."""
        row = self.conn.execute("SELECT idempotency_key FROM processing_ledger WHERE id = ?", (ledger_id,)).fetchone()
        if row is None:
            raise RuntimeError("reclassifiable processing ledger is missing")
        self.conn.execute("DELETE FROM safety_gate_results WHERE idempotency_key = ?", (row["idempotency_key"],))
        self.conn.execute(
            "DELETE FROM write_intents WHERE ledger_id = ? AND status IN ('planned', 'blocked', 'failed')",
            (ledger_id,),
        )
        self.conn.execute(
            """
            UPDATE processing_ledger
            SET candidate_hash = ?, processing_status = ?,
                dry_run_result_hash = ?, error_message = NULL, retry_count = retry_count + 1,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND processing_status IN ('new', 'planned', 'blocked', 'failed')
            """,
            (candidate_hash, processing_status, dry_run_result_hash, ledger_id),
        )

    def create_approval_record(
        self,
        *,
        idempotency_key: str,
        operation_type: str,
        scope_hash: str,
        approved_by: str,
        rollback_plan: str,
    ) -> str:
        approval_hash = stable_hash(
            {
                "idempotency_key": idempotency_key,
                "operation_type": operation_type,
                "scope_hash": scope_hash,
                "approved_by_hash": mask_identifier(approved_by),
                "rollback_plan_hash": stable_hash(rollback_plan),
            }
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO approval_records (
                approval_hash, idempotency_key, operation_type, scope_hash,
                approved_by_hash, rollback_plan_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                approval_hash,
                idempotency_key,
                operation_type,
                scope_hash,
                mask_identifier(approved_by),
                stable_hash(rollback_plan),
            ),
        )
        self._audit(
            "create_approval_record",
            idempotency_key,
            {"operation_type": operation_type, "scope_hash": scope_hash},
            {"approval_hash": approval_hash},
            True,
        )
        self.conn.commit()
        return approval_hash

    def execute_approved_journal_create(
        self,
        plan: DryRunPlan,
        *,
        approval_hash: str | None = None,
        write_client: JournalWriteClient | None = None,
    ) -> str:
        if not approval_hash:
            raise PermissionError("write execution requires an approval record")
        if not plan.registration_allowed:
            raise PermissionError("blocked dry-run plan cannot be executed")
        approval = self.conn.execute(
            """
            SELECT * FROM approval_records
            WHERE approval_hash = ? AND idempotency_key = ?
              AND operation_type = 'journal_create' AND revoked_at IS NULL
            """,
            (approval_hash, plan.idempotency_key),
        ).fetchone()
        if not approval:
            raise PermissionError("approval record does not match this journal_create plan")
        intent = self.conn.execute(
            """
            SELECT status, moneyforward_journal_id_hash FROM write_intents
            WHERE idempotency_key = ? AND operation_type = 'journal_create'
            """,
            (plan.idempotency_key,),
        ).fetchone()
        if not intent:
            raise RuntimeError("missing journal_create write intent")
        if intent["status"] == "executed":
            return intent["moneyforward_journal_id_hash"]
        if write_client is None:
            raise RuntimeError("real Money Forward write client is not attached")
        journal_id = write_client.create_journal(plan)
        self.mark_journal_created(plan.idempotency_key, journal_id)
        self._audit(
            "execute_approved_journal_create",
            plan.idempotency_key,
            {"idempotency_key": plan.idempotency_key, "approval_hash": approval_hash},
            {"moneyforward_journal_id_hash": mask_identifier(journal_id)},
            False,
        )
        self.conn.commit()
        return mask_identifier(journal_id)

    def mark_journal_created(self, idempotency_key: str, moneyforward_journal_id: str) -> None:
        journal_hash = mask_identifier(moneyforward_journal_id)
        current = self.conn.execute(
            "SELECT moneyforward_journal_id_hash FROM processing_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not current:
            raise KeyError("unknown idempotency_key")
        if current["moneyforward_journal_id_hash"] and current["moneyforward_journal_id_hash"] != journal_hash:
            raise RuntimeError("journal id already recorded for this idempotency_key")
        self.conn.execute(
            """
            UPDATE processing_ledger
            SET processing_status = 'journal_created',
                moneyforward_journal_id_hash = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ?
            """,
            (journal_hash, idempotency_key),
        )
        self.conn.execute(
            """
            UPDATE write_intents
            SET status = 'executed',
                moneyforward_journal_id_hash = ?,
                executed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ? AND operation_type = 'journal_create'
            """,
            (journal_hash, idempotency_key),
        )
        self._audit(
            "mark_journal_created",
            idempotency_key,
            {"idempotency_key": idempotency_key},
            {"moneyforward_journal_id_hash": journal_hash},
            True,
        )
        self.conn.commit()

    def plan_voucher_attach(self, idempotency_key: str, evidence: Evidence) -> str:
        row = self.conn.execute(
            "SELECT id, moneyforward_journal_id_hash FROM processing_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not row:
            raise KeyError("unknown idempotency_key")
        if not row["moneyforward_journal_id_hash"]:
            raise RuntimeError("journal must be created before voucher attach retry planning")
        request = {
            "operation_type": "voucher_attach",
            "idempotency_key": idempotency_key,
            "file_hash": evidence.file_hash,
            "journal_id_hash": row["moneyforward_journal_id_hash"],
        }
        plan_hash = stable_hash(request)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO write_intents (
                idempotency_key, ledger_id, operation_type, source_file_id_hash,
                candidate_hash, dry_run_result_hash, plan_hash, request_hash, status,
                moneyforward_journal_id_hash
            )
            SELECT idempotency_key, id, 'voucher_attach', google_drive_file_id_hash,
                   candidate_hash, dry_run_result_hash, ?, ?, 'planned',
                   moneyforward_journal_id_hash
            FROM processing_ledger
            WHERE idempotency_key = ?
            """,
            (plan_hash, stable_hash(request), idempotency_key),
        )
        self._audit("plan_voucher_attach", idempotency_key, request, {"plan_hash": plan_hash}, True)
        self.conn.commit()
        return plan_hash

    def verify_journal(self, expected: dict, actual: dict) -> bool:
        keys = ["journal_date", "amount", "tax_amount", "debit_account", "credit_account", "voucher_file_ids"]
        ok = all(expected.get(key) == actual.get(key) for key in keys)
        idem = expected.get("idempotency_key")
        if idem:
            self.conn.execute(
                """
                INSERT INTO post_registration_verifications (
                    idempotency_key, moneyforward_journal_id_hash, expected_hash,
                    actual_hash, voucher_file_ids_hash, passed, diff_summary_redacted
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idem,
                    expected.get("moneyforward_journal_id_hash", "sha256:unknown"),
                    stable_hash(expected),
                    stable_hash(actual),
                    stable_hash(actual.get("voucher_file_ids", [])),
                    1 if ok else 0,
                    "一致" if ok else "登録後再取得結果が期待値と一致しない",
                ),
            )
            if ok:
                self._mark_processing_verified_if_complete(
                    idempotency_key=idem,
                    moneyforward_journal_id_hash=str(expected.get("moneyforward_journal_id_hash") or ""),
                )
        self._audit("verify_journal", None, expected, {"ok": ok}, True)
        self.conn.commit()
        return ok

    def _mark_processing_verified_if_complete(
        self,
        *,
        idempotency_key: str,
        moneyforward_journal_id_hash: str,
    ) -> None:
        if not moneyforward_journal_id_hash:
            return
        row = self.conn.execute(
            """
            SELECT 1
            FROM voucher_attach_readback_verifications
            WHERE journal_id_hash = ? AND passed = 1
            LIMIT 1
            """,
            (moneyforward_journal_id_hash,),
        ).fetchone()
        if row is None:
            return
        self.conn.execute(
            """
            UPDATE processing_ledger
            SET processing_status = 'verified',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE idempotency_key = ?
              AND moneyforward_journal_id_hash = ?
              AND processing_status IN ('journal_created', 'voucher_attached', 'verified')
            """,
            (idempotency_key, moneyforward_journal_id_hash),
        )

    def _record_gate_result(self, idempotency_key: str, name: str, passed: bool, reason: str) -> None:
        result_hash = stable_hash({"name": name, "passed": passed, "reason": reason})
        self.conn.execute(
            """
            INSERT OR IGNORE INTO safety_gate_results (
                idempotency_key, gate_name, passed, risk_level,
                reason_code, result_hash, result_summary_redacted
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                name,
                1 if passed else 0,
                "low" if passed else "blocker",
                "passed" if passed else "blocked",
                result_hash,
                reason,
            ),
        )

    def _audit(self, event_type: str, idempotency_key: str | None, request: dict, response: dict, dry_run: bool) -> None:
        request_hash = stable_hash(request)
        response_hash = stable_hash(response)
        event_hash = stable_hash(
            {
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "response_hash": response_hash,
                "dry_run": dry_run,
            }
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO audit_logs (
                event_hash, event_type, idempotency_key, request_hash, response_hash,
                actor, dry_run, rollback_plan
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_hash,
                event_type,
                idempotency_key,
                request_hash,
                response_hash,
                self.actor,
                1 if dry_run else 0,
                "No local rollback needed for dry-run; real writes require Money Forward ID specific rollback.",
            ),
        )


def _blocked_next_actions(candidate: JournalCandidate, evidence: Evidence, results: list) -> tuple[str, ...]:
    if not is_invoice_workflow(candidate, evidence):
        return ()
    actions: list[str] = []
    for result in results:
        if result.name != "master_resolution" or result.passed:
            continue
        if "trade_partner_code" in result.reason or result.reason.startswith("取引先:"):
            actions.append("run_phase12_trade_partner_request")
        if "貸方補助科目" in result.reason:
            actions.append("run_phase12_sub_account_request")
    return tuple(dict.fromkeys(actions))


def _apply_resolved_credit_side(
    conn: sqlite3.Connection,
    candidate: JournalCandidate,
    evidence: Evidence,
) -> JournalCandidate:
    # A card/financial source normally retains the statement-side credit.  A
    # person-owned Drive path is an explicit exception and must stay payable to
    # that person even after the evidence enters a temporary pending folder.
    if candidate.source_type in FINANCIAL_STATEMENT_SOURCE_TYPES and folder_credit_decision(evidence) is None:
        return candidate
    if candidate.source_type in DIRECT_LEDGER_SOURCE_TYPES:
        return candidate
    decision = resolve_credit_side_master(conn, candidate, evidence)
    if candidate.credit_account == decision.credit_account and candidate.credit_sub_account == decision.credit_sub_account:
        return candidate
    return replace(candidate, credit_account=decision.credit_account, credit_sub_account=decision.credit_sub_account)
