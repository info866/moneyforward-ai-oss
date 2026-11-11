from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .db import DEFAULT_DB_PATH
from .product_readiness import build_product_readiness_status


REQUIRED_PHASE12_EVIDENCE_IDS = (
    "EV-P12-READ-001",
    "EV-P12-SHADOW-001",
    "EV-P12-PREFLIGHT-001",
    "EV-P12-APPROVALRECORD-001",
    "EV-P12-GRANT-001",
    "EV-P12-DRYRUN-001",
    "EV-P12-SCOPE-001",
    "EV-P12-READBACK-001",
    "EV-P12-AUDIT-001",
    "EV-P12-GATE-001",
    "EV-P12-JOURNAL-001",
    "EV-P12-APPROVAL-001",
    "EV-P12-APPROVALGEN-001",
    "EV-P12-PREPAREBUNDLE-001",
    "EV-P12-OFFICEONBOARDING-001",
    "EV-P12-DRIVEBATCH-001",
    "EV-P12-DRIVEEXTRACTIONSPREPARE-001",
    "EV-P12-GMAILINVOICEINTAKE-001",
    "EV-P12-GMAILDRIVEUPLOAD-001",
    "EV-P12-EVENTQUEUE-001",
    "EV-P12-PAYABLEFOLDERSYNC-001",
    "EV-P12-PAYABLEFOLDERCREATE-001",
    "EV-P12-INVOICECANDIDATEINGEST-001",
    "EV-P12-FINANCIALEVENTINGEST-001",
    "EV-P12-CARDSTATEMENTEVIDENCEMATCH-001",
    "EV-P12-FINANCIALEVENTRECONCILE-001",
    "EV-P12-FINANCIALEVENTMATCHAPPLY-001",
    "EV-P12-FINANCIALEVENT-001",
    "EV-P12-FINANCIALINVOICEPIPELINE-001",
    "EV-P12-FINANCIALEVENTLEARN-001",
    "EV-P12-JOURNALBACKFILL-001",
    "EV-P12-EVIDENCESTATUS-001",
    "EV-P12-REALPOSTCHECKLIST-001",
    "EV-P12-OAUTHWRITECHECKLIST-001",
    "EV-P12-REALPOSTGATE-001",
    "EV-P12-NEXTACTION-001",
    "EV-P12-INVENTORY-001",
    "EV-P12-REQUESTTEMPLATE-001",
    "EV-P12-REQUESTSTATUS-001",
    "EV-P12-REQUESTPREPARE-001",
    "EV-P12-REQUESTQUEUE-001",
    "EV-P12-REQUESTFILL-001",
    "EV-P12-REQUESTFILLTEMPLATE-001",
    "EV-P12-REQUESTFILLDRAFT-001",
    "EV-P12-REQUESTOAUTHDRAFT-001",
    "EV-P12-REQUESTFILLCHECK-001",
    "EV-P12-REALPOST-001",
    "EV-P12-CLASSIFICATION-001",
    "EV-P12-CLASSIFICATIONSCOPE-001",
    "EV-P12-IMPROVEMENTRAG-001",
    "EV-P12-LEARNEDRULES-001",
    "EV-P12-EVIDENCEFACTS-001",
    "EV-P12-FACTSBATCHNORMALIZE-001",
    "EV-P12-FACTSBATCH-001",
    "EV-P12-FACTSBATCHPREPARE-001",
    "EV-P12-WORKFLOWLOCALREPLAY-001",
    "EV-P12-JOURNALDRYRUN-001",
    "EV-P12-JOURNALBATCHDRYRUN-001",
    "EV-P12-JOURNALBATCHROUTE-001",
    "EV-P12-JOURNALBATCHPREPARE-001",
    "EV-P12-JOURNALWRITEQUEUE-001",
    "EV-P12-JOURNALWRITEAPPROVALDRAFT-001",
    "EV-P12-JOURNALWRITEAPPROVALRECORD-001",
    "EV-P12-JOURNALWRITEGRANT-001",
    "EV-P12-JOURNALWRITEREALPOSTCHECKLIST-001",
    "EV-P12-JOURNALWRITEPOST-001",
    "EV-P12-JOURNALWRITEVERIFY-001",
    "EV-P12-TRADEPARTNERREQ-001",
    "EV-P12-TRADEPARTNERGRANT-001",
    "EV-P12-TRADEPARTNERREALPOSTCHECKLIST-001",
    "EV-P12-TRADEPARTNERPOST-001",
    "EV-P12-SUBACCOUNTREQ-001",
    "EV-P12-N8NPLAN-001",
    "EV-P12-PRODUCTREADINESS-001",
    "EV-P12-PRODUCTREADY-001",
    "EV-P12-RELEASEREADINESS-001",
    "EV-P12-AUTONOMYRUN-001",
)

EXPECTED_INCOMPLETE_IDS: tuple[str, ...] = ()

REQUIRED_COMMAND_SURFACE = {
    "make_target:phase12-voucher-attach-preflight": "phase12-voucher-attach-preflight:",
    "make_target:phase12-voucher-attach-approval-record": "phase12-voucher-attach-approval-record:",
    "make_target:phase12-voucher-attach-grant": "phase12-voucher-attach-grant:",
    "make_target:phase12-voucher-attach-execute-dry-run": "phase12-voucher-attach-execute-dry-run:",
    "make_target:phase12-voucher-attach-prepare-bundle": "phase12-voucher-attach-prepare-bundle:",
    "make_target:phase12-voucher-attach-realpost-checklist": "phase12-voucher-attach-realpost-checklist:",
    "make_target:phase12-realpost-gate": "phase12-realpost-gate:",
    "make_target:phase12-realpost-next-action": "phase12-realpost-next-action:",
    "make_target:phase12-realpost-inventory": "phase12-realpost-inventory:",
    "make_target:phase12-realpost-request-template": "phase12-realpost-request-template:",
    "make_target:phase12-realpost-request-fill-template": "phase12-realpost-request-fill-template:",
    "make_target:phase12-realpost-request-fill-draft": "phase12-realpost-request-fill-draft:",
    "make_target:phase12-realpost-request-oauth-write-approval-draft": "phase12-realpost-request-oauth-write-approval-draft:",
    "make_target:phase12-realpost-request-fill-check": "phase12-realpost-request-fill-check:",
    "make_target:phase12-realpost-request-fill": "phase12-realpost-request-fill:",
    "make_target:phase12-realpost-request-status": "phase12-realpost-request-status:",
    "make_target:phase12-realpost-request-prepare": "phase12-realpost-request-prepare:",
    "make_target:phase12-realpost-request-queue": "phase12-realpost-request-queue:",
    "make_target:phase12-voucher-attach-ready": "phase12-voucher-attach-ready:",
    "make_target:phase12-voucher-attach-post": "phase12-voucher-attach-post:",
    "make_target:phase12-voucher-attach-verify": "phase12-voucher-attach-verify:",
    "make_target:phase12-drive-folder-manifest": "phase12-drive-folder-manifest:",
    "make_target:phase12-drive-folder-audit": "phase12-drive-folder-audit:",
    "make_target:phase12-drive-batch-prepare": "phase12-drive-batch-prepare:",
    "make_target:phase12-drive-extractions-prepare": "phase12-drive-extractions-prepare:",
    "make_target:phase12-gmail-invoice-intake": "phase12-gmail-invoice-intake:",
    "make_target:phase12-gmail-drive-upload": "phase12-gmail-drive-upload:",
    "make_target:phase12-event-queue-build": "phase12-event-queue-build:",
    "make_target:phase12-invoice-candidate-ingest": "phase12-invoice-candidate-ingest:",
    "make_target:phase12-financial-event-ingest": "phase12-financial-event-ingest:",
    "make_target:phase12-financial-event-reconcile": "phase12-financial-event-reconcile:",
    "make_target:phase12-financial-event-apply-matches": "phase12-financial-event-apply-matches:",
    "make_target:phase12-financial-event-classify": "phase12-financial-event-classify:",
    "make_target:phase12-financial-invoice-pipeline": "phase12-financial-invoice-pipeline:",
    "make_target:phase12-card-statement-evidence-match": "phase12-card-statement-evidence-match:",
    "make_target:phase12-financial-event-learn": "phase12-financial-event-learn:",
    "make_target:phase12-drive-move-plan": "phase12-drive-move-plan:",
    "make_target:phase12-drive-move-execute": "phase12-drive-move-execute:",
    "make_target:phase12-drive-move-verify": "phase12-drive-move-verify:",
    "make_target:phase12-payable-folder-sync": "phase12-payable-folder-sync:",
    "make_target:phase12-payable-folder-create": "phase12-payable-folder-create:",
    "make_target:phase12-backfill-journal-readback": "phase12-backfill-journal-readback:",
    "make_target:phase12-voucher-write-scope-gate": "phase12-voucher-write-scope-gate:",
    "make_target:phase12-oauth-write-checklist": "phase12-oauth-write-checklist:",
    "make_target:phase12-shiwake-autorun-contract": "phase12-shiwake-autorun-contract:",
    "make_target:phase12-evidence-status": "phase12-evidence-status:",
    "make_target:phase12-office-onboarding-plan": "phase12-office-onboarding-plan:",
    "make_target:phase12-office-rag-build": "phase12-office-rag-build:",
    "make_target:phase12-office-rag-gate": "phase12-office-rag-gate:",
    "make_target:phase12-classification-eval": "phase12-classification-eval:",
    "make_target:phase12-classification-eval-learned": "phase12-classification-eval-learned:",
    "make_target:phase12-classification-eval-shadow": "phase12-classification-eval-shadow:",
    "make_target:phase12-classification-improvements": "phase12-classification-improvements:",
    "make_target:phase12-classification-scope": "phase12-classification-scope:",
    "make_target:phase12-materialize-learned-rules": "phase12-materialize-learned-rules:",
    "make_target:phase12-evidence-facts-normalize": "phase12-evidence-facts-normalize:",
    "make_target:phase12-facts-batch-normalize": "phase12-facts-batch-normalize:",
    "make_target:phase12-facts-batch-input": "phase12-facts-batch-input:",
    "make_target:phase12-facts-batch-prepare": "phase12-facts-batch-prepare:",
    "make_target:phase12-workflow-local-replay": "phase12-workflow-local-replay:",
    "make_target:phase12-journal-dry-run": "phase12-journal-dry-run:",
    "make_target:phase12-journal-batch-input": "phase12-journal-batch-input:",
    "make_target:phase12-journal-batch-dry-run": "phase12-journal-batch-dry-run:",
    "make_target:phase12-journal-batch-route": "phase12-journal-batch-route:",
    "make_target:phase12-journal-batch-prepare": "phase12-journal-batch-prepare:",
    "make_target:phase12-journal-write-queue": "phase12-journal-write-queue:",
    "make_target:phase12-journal-write-approval-draft": "phase12-journal-write-approval-draft:",
    "make_target:phase12-journal-write-approval-record": "phase12-journal-write-approval-record:",
    "make_target:phase12-journal-write-grant": "phase12-journal-write-grant:",
    "make_target:phase12-journal-write-realpost-checklist": "phase12-journal-write-realpost-checklist:",
    "make_target:phase12-journal-write-post": "phase12-journal-write-post:",
    "make_target:phase12-journal-write-verify": "phase12-journal-write-verify:",
    "make_target:phase12-journal-voucher-prepare": "phase12-journal-voucher-prepare:",
    "make_target:phase12-trade-partner-request": "phase12-trade-partner-request:",
    "make_target:phase12-trade-partner-create-grant": "phase12-trade-partner-create-grant:",
    "make_target:phase12-trade-partner-create-realpost-checklist": "phase12-trade-partner-create-realpost-checklist:",
    "make_target:phase12-trade-partner-create-post": "phase12-trade-partner-create-post:",
    "make_target:phase12-sub-account-request": "phase12-sub-account-request:",
    "make_target:phase12-n8n-workflow-plan": "phase12-n8n-workflow-plan:",
    "make_target:phase12-product-readiness": "phase12-product-readiness:",
    "make_target:phase12-release-readiness": "phase12-release-readiness:",
    "make_target:codex-pdca": "codex-pdca:",
    "make_target:codex-goal-status": "codex-goal-status:",
    "make_target:codex-autonomy-fast": "codex-autonomy-fast:",
    "make_target:codex-autonomy-run": "codex-autonomy-run:",
    "make_verify_uses_journal_id_json": '--journal-id-json "$(JOURNAL_ID_JSON)"',
    "make_verify_uses_voucher_file_ids_json": '--voucher-file-ids-json "$(VOUCHER_FILE_IDS_JSON)"',
}

REQUIRED_SCHEMA_SURFACE = {
    "schema:table:journal_pattern_occurrences": "CREATE TABLE IF NOT EXISTS journal_pattern_occurrences",
    "schema:table:voucher_attach_execution_records": "CREATE TABLE IF NOT EXISTS voucher_attach_execution_records",
    "schema:table:voucher_attach_readback_verifications": "CREATE TABLE IF NOT EXISTS voucher_attach_readback_verifications",
    "schema:table:drive_move_execution_records": "CREATE TABLE IF NOT EXISTS drive_move_execution_records",
    "schema:table:drive_move_readback_verifications": "CREATE TABLE IF NOT EXISTS drive_move_readback_verifications",
    "schema:table:payable_folder_mappings": "CREATE TABLE IF NOT EXISTS payable_folder_mappings",
    "schema:table:financial_event_classifications": "CREATE TABLE IF NOT EXISTS financial_event_classifications",
    "schema:table:classification_evaluation_runs": "CREATE TABLE IF NOT EXISTS classification_evaluation_runs",
    "schema:table:classification_evaluation_items": "CREATE TABLE IF NOT EXISTS classification_evaluation_items",
    "schema:table:classification_improvement_candidates": "CREATE TABLE IF NOT EXISTS classification_improvement_candidates",
    "schema:table:fixed_rule_candidates": "CREATE TABLE IF NOT EXISTS fixed_rule_candidates",
    "schema:classification_prediction_strategy": "prediction_strategy TEXT NOT NULL DEFAULT 'nearest_pattern'",
    "schema:classification_learned_rule_threshold": "learned_rule_confidence_threshold REAL NOT NULL DEFAULT 0.8",
    "schema:classification_prediction_source": "prediction_source TEXT NOT NULL DEFAULT 'matched_nearest_pattern'",
    "schema:classification_classifiable_sample": "classifiable_sample_count INTEGER NOT NULL DEFAULT 0",
    "schema:classification_classifiable_accuracy": "classifiable_overall_accuracy REAL NOT NULL DEFAULT 0.0",
    "schema:classification_auto_post_eligible_count": "auto_post_eligible_count INTEGER NOT NULL DEFAULT 0",
    "schema:classification_auto_post_eligible_accuracy": "auto_post_eligible_accuracy REAL NOT NULL DEFAULT 0.0",
    "schema:classification_item_classifiable": "is_classifiable INTEGER NOT NULL DEFAULT 1",
    "schema:classification_item_auto_post_candidate": "auto_post_candidate INTEGER NOT NULL DEFAULT 0",
    "schema:classification_failure_reason_code": "failure_reason_code TEXT NOT NULL DEFAULT ''",
    "schema:classification_promotion_allowed": "promotion_allowed INTEGER NOT NULL DEFAULT 0 CHECK (promotion_allowed IN (0, 1))",
    "schema:classification_block_reason": "block_reason_code TEXT NOT NULL DEFAULT ''",
    "schema:execution_raw_secret_check": "raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0)",
    "schema:execution_raw_journal_check": "raw_journal_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_id_stored = 0)",
    "schema:execution_raw_voucher_check": "raw_voucher_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_voucher_file_ids_stored = 0)",
    "schema:execution_raw_api_response_check": "raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0)",
}

REQUIRED_FILES = (
    "scripts/phase12_voucher_attach_prepare.py",
    "scripts/phase12_drive_folder_manifest.py",
    "scripts/phase12_drive_folder_audit.py",
    "scripts/phase12_drive_extractions_prepare.py",
    "scripts/phase12_gmail_invoice_intake.py",
    "scripts/phase12_gmail_drive_upload.py",
    "scripts/phase12_event_queue_build.py",
    "scripts/phase12_invoice_candidate_ingest.py",
    "scripts/phase12_financial_event_ingest.py",
    "scripts/phase12_financial_event_reconcile.py",
    "scripts/phase12_financial_event_apply_matches.py",
    "scripts/phase12_financial_event_classify.py",
    "scripts/phase12_financial_invoice_pipeline.py",
    "scripts/phase12_card_statement_evidence_match.py",
    "scripts/phase12_payable_folder_sync.py",
    "scripts/phase12_payable_folder_create.py",
    "scripts/phase12_backfill_journal_readback.py",
    "scripts/phase12_voucher_attach_realpost_checklist.py",
    "scripts/phase12_realpost_gate.py",
    "scripts/phase12_realpost_next_action.py",
    "scripts/phase12_realpost_inventory.py",
    "scripts/phase12_realpost_request_template.py",
    "scripts/phase12_realpost_request_fill_template.py",
    "scripts/phase12_realpost_request_fill_draft.py",
    "scripts/phase12_realpost_request_oauth_write_approval_draft.py",
    "scripts/phase12_realpost_request_fill_check.py",
    "scripts/phase12_realpost_request_fill.py",
    "scripts/phase12_realpost_request_status.py",
    "scripts/phase12_realpost_request_prepare.py",
    "scripts/phase12_realpost_request_queue.py",
    "scripts/phase12_voucher_attach_ready.py",
    "scripts/phase12_voucher_attach_post.py",
    "scripts/phase12_voucher_attach_verify.py",
    "scripts/phase12_voucher_write_scope_gate.py",
    "scripts/phase12_oauth_write_checklist.py",
    "scripts/phase12_shiwake_autorun_contract.py",
    ".githooks/pre-commit",
    "scripts/phase12_office_onboarding_plan.py",
    "scripts/phase12_office_rag_build.py",
    "scripts/phase12_office_rag_gate.py",
    "scripts/phase12_classification_eval.py",
    "scripts/phase12_classification_scope.py",
    "scripts/phase12_classification_improvements.py",
    "scripts/phase12_materialize_learned_rules.py",
    "scripts/phase12_evidence_facts_normalize.py",
    "scripts/phase12_facts_batch_normalize.py",
    "scripts/phase12_facts_batch_input.py",
    "scripts/phase12_facts_batch_prepare.py",
    "scripts/phase12_workflow_local_replay.py",
    "scripts/phase12_journal_dry_run.py",
    "scripts/phase12_journal_batch_input.py",
    "scripts/phase12_journal_batch_dry_run.py",
    "scripts/phase12_journal_batch_route.py",
    "scripts/phase12_journal_batch_prepare.py",
    "scripts/phase12_journal_write_queue.py",
    "scripts/phase12_journal_write_approval_draft.py",
    "scripts/phase12_journal_write_approval_record.py",
    "scripts/phase12_journal_write_grant.py",
    "scripts/phase12_journal_write_realpost_checklist.py",
    "scripts/phase12_journal_write_post.py",
    "scripts/phase12_journal_write_verify.py",
    "scripts/phase12_journal_voucher_prepare.py",
    "scripts/phase12_trade_partner_request.py",
    "scripts/phase12_sub_account_request.py",
    "scripts/phase12_n8n_workflow_plan.py",
    "scripts/phase12_product_readiness.py",
    "scripts/phase12_release_readiness.py",
    "codex/learning/codex_goal_status.py",
    "codex/learning/codex_autonomy_run.py",
    "tests/test_phase12_accounting_api.py",
    "src/mf_phase12/office_rag_pipeline.py",
    "src/mf_phase12/office_onboarding.py",
    "src/mf_phase12/drive_manifest.py",
    "src/mf_phase12/drive_batch.py",
    "src/mf_phase12/drive_extractions_prepare.py",
    "src/mf_phase12/gmail_invoice_intake.py",
    "src/mf_phase12/gmail_drive_upload.py",
    "src/mf_phase12/event_queue.py",
    "src/mf_phase12/invoice_candidate_ingest.py",
    "src/mf_phase12/financial_event_ingest.py",
    "src/mf_phase12/financial_event_reconcile.py",
    "src/mf_phase12/financial_event_match_apply.py",
    "src/mf_phase12/financial_event_classification.py",
    "src/mf_phase12/financial_invoice_pipeline.py",
    "src/mf_phase12/card_statement_evidence_match.py",
    "src/mf_phase12/payable_folder_sync.py",
    "src/mf_phase12/journal_backfill.py",
    "src/mf_phase12/classification_eval.py",
    "src/mf_phase12/classification_scope.py",
    "src/mf_phase12/classification_improvements.py",
    "src/mf_phase12/evidence_facts_normalize.py",
    "src/mf_phase12/facts_batch_normalize.py",
    "src/mf_phase12/facts_batch_input.py",
    "src/mf_phase12/facts_batch_prepare.py",
    "src/mf_phase12/workflow_local_replay.py",
    "src/mf_phase12/journal_dry_run.py",
    "src/mf_phase12/journal_batch_input.py",
    "src/mf_phase12/journal_batch_dry_run.py",
    "src/mf_phase12/journal_batch_router.py",
    "src/mf_phase12/journal_batch_prepare.py",
    "src/mf_phase12/journal_write_queue.py",
    "src/mf_phase12/journal_write_approval_draft.py",
    "src/mf_phase12/journal_write_approval_record.py",
    "src/mf_phase12/journal_write_grant.py",
    "src/mf_phase12/journal_write_realpost_checklist.py",
    "src/mf_phase12/journal_write_post.py",
    "src/mf_phase12/journal_write_verify.py",
    "src/mf_phase12/journal_voucher_handoff.py",
    "src/mf_phase12/trade_partner_request.py",
    "src/mf_phase12/sub_account_request.py",
    "src/mf_phase12/n8n_workflow_plan.py",
    "src/mf_phase12/product_readiness.py",
    "src/mf_phase12/release_readiness.py",
    "src/mf_phase12/voucher_realpost_checklist.py",
    "src/mf_phase12/oauth_write_checklist.py",
    "src/mf_phase12/shiwake_autorun_contract.py",
    "src/mf_phase12/realpost_gate.py",
    "src/mf_phase12/realpost_next_action.py",
    "src/mf_phase12/realpost_inventory.py",
    "src/mf_phase12/realpost_request.py",
)


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    target: str
    method: str
    result: str
    commit_or_issue: str
    state: str


@dataclass(frozen=True)
class CommandSurfaceCheck:
    name: str
    passed: bool
    detail: str


def parse_phase12_evidence_rows(markdown: str) -> list[EvidenceRow]:
    rows: list[EvidenceRow] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("| `EV-P12-"):
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) != 5:
            continue
        evidence_id = _strip_code(parts[0])
        rows.append(
            EvidenceRow(
                evidence_id=evidence_id,
                target=parts[1],
                method=parts[2],
                result=parts[3],
                commit_or_issue=parts[4],
                state=classify_evidence_state(parts[3]),
            )
        )
    return rows


def classify_evidence_state(result: str) -> str:
    unfinished_real = r"(実API.*未実行|実POST.*未実行|実write.*未実行)"
    partial_proof = r"(mock|local|ローカル|passed|拒否|確認|生成|通る)"
    if re.search(partial_proof + r".*" + unfinished_real, result):
        return "partial"
    if re.search(unfinished_real + r".*" + partial_proof, result):
        return "partial"
    if "検知できることを確認" in result:
        return "verified"
    if re.search(r"未実行|未完了|未揃い|禁止", result):
        return "incomplete"
    if re.search(r"確認|passed|通る|OK|成功|取得|生成|拒否|raw_data_stored|保存不可|registration_allowed|件", result):
        return "verified"
    return "unknown"


def build_phase12_evidence_status(
    root: Path,
    *,
    db_path: Path | str | None = None,
) -> dict:
    work_order = root / "docs" / "poc" / "moneyforward_ai_poc_work_order.md"
    makefile = root / "Makefile"
    workflow_db = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    markdown = work_order.read_text(encoding="utf-8")
    rows = parse_phase12_evidence_rows(markdown)
    by_id = {row.evidence_id: row for row in rows}

    missing_ids = [evidence_id for evidence_id in REQUIRED_PHASE12_EVIDENCE_IDS if evidence_id not in by_id]
    incomplete_ids = [row.evidence_id for row in rows if row.state == "incomplete"]
    partial_ids = [row.evidence_id for row in rows if row.state == "partial"]
    unknown_ids = [row.evidence_id for row in rows if row.state == "unknown"]
    verified_ids = [row.evidence_id for row in rows if row.state == "verified"]
    unexpected_completed = [
        evidence_id
        for evidence_id in EXPECTED_INCOMPLETE_IDS
        if evidence_id in by_id and by_id[evidence_id].state != "incomplete"
    ]

    command_checks = check_command_surface(root, makefile)
    schema_checks = check_schema_surface(root / "sql" / "phase12_schema.sql")
    workflow_status = build_product_readiness_status(workflow_db, product_mode="workflow_ready")
    failure_reasons = []
    if missing_ids:
        failure_reasons.append("required evidence IDs are missing")
    if unknown_ids:
        failure_reasons.append("some evidence rows have unknown state")
    if incomplete_ids:
        failure_reasons.append("some evidence rows are incomplete")
    if unexpected_completed:
        failure_reasons.append("expected incomplete evidence is no longer marked incomplete")
    if any(not check.passed for check in command_checks + schema_checks):
        failure_reasons.append("required command/schema surface is missing")
    if not workflow_status["phase12_product_ready"]:
        failure_reasons.append("workflow_ready gate is not ready")

    ledger_consistent = not failure_reasons
    completion_blocker = ""
    if not ledger_consistent or incomplete_ids:
        completion_blocker = "Phase 12 has missing, unknown, incomplete, invalid, or workflow-not-ready evidence"
    return {
        "status": "ok" if ledger_consistent else "needs_attention",
        "phase12_completion_allowed": ledger_consistent and not incomplete_ids,
        "completion_blocker": completion_blocker,
        "work_order": str(work_order.relative_to(root)),
        "evidence_count": len(rows),
        "verified_count": len(verified_ids),
        "partial_count": len(partial_ids),
        "incomplete_count": len(incomplete_ids),
        "unknown_count": len(unknown_ids),
        "missing_required_evidence_ids": missing_ids,
        "verified_evidence_ids": verified_ids,
        "partial_evidence_ids": partial_ids,
        "incomplete_evidence_ids": incomplete_ids,
        "unknown_evidence_ids": unknown_ids,
        "expected_incomplete_ids": list(EXPECTED_INCOMPLETE_IDS),
        "unexpected_completed_expected_incomplete_ids": unexpected_completed,
        "command_surface_checks": [asdict(check) for check in command_checks],
        "schema_surface_checks": [asdict(check) for check in schema_checks],
        "workflow_readiness": {
            "status": workflow_status["status"],
            "product_mode": workflow_status.get("product_mode", "workflow_ready"),
            "phase12_product_ready": workflow_status["phase12_product_ready"],
            "failed_checks": workflow_status["failed_checks"],
            "counts": workflow_status["counts"],
        },
        "failure_reasons": failure_reasons,
    }


def check_command_surface(root: Path, makefile: Path) -> list[CommandSurfaceCheck]:
    makefile_text = makefile.read_text(encoding="utf-8") if makefile.exists() else ""
    checks: list[CommandSurfaceCheck] = []
    for name, needle in REQUIRED_COMMAND_SURFACE.items():
        checks.append(
            CommandSurfaceCheck(
                name=name,
                passed=needle in makefile_text,
                detail="present" if needle in makefile_text else f"missing {needle}",
            )
        )
    voucher_post_line = _make_target_command_line(makefile_text, "phase12-voucher-attach-post")
    voucher_post_gate_ok = (
        "$(if $(EXECUTE_APPROVED),--execute-approved)" in voucher_post_line
        and not voucher_post_line.rstrip().endswith("--execute-approved")
    )
    checks.append(
        CommandSurfaceCheck(
            name="make_target:phase12-voucher-attach-post_execute_gate_conditional",
            passed=voucher_post_gate_ok,
            detail="conditional" if voucher_post_gate_ok else "missing conditional EXECUTE_APPROVED gate",
        )
    )
    journal_queue_line = _make_target_command_line(makefile_text, "phase12-journal-write-queue")
    queue_handoff_gate_ok = "$(if $(REQUIRE_ATTACHMENT_HANDOFF),--require-attachment-handoff-ready)" in journal_queue_line
    checks.append(
        CommandSurfaceCheck(
            name="make_target:phase12-journal-write-queue_attachment_handoff_gate",
            passed=queue_handoff_gate_ok,
            detail="conditional" if queue_handoff_gate_ok else "missing conditional attachment handoff gate",
        )
    )
    for relative in REQUIRED_FILES:
        path = root / relative
        checks.append(
            CommandSurfaceCheck(
                name=f"file:{relative}",
                passed=path.exists(),
                detail="present" if path.exists() else "missing",
            )
        )
    return checks


def _make_target_command_line(makefile_text: str, target: str) -> str:
    lines = makefile_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"{target}:":
            for candidate in lines[index + 1 :]:
                if candidate and not candidate.startswith(("\t", " ")):
                    return ""
                if candidate.strip():
                    return candidate.strip()
    return ""


def check_schema_surface(schema_path: Path) -> list[CommandSurfaceCheck]:
    schema_text = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""
    checks: list[CommandSurfaceCheck] = []
    for name, needle in REQUIRED_SCHEMA_SURFACE.items():
        checks.append(
            CommandSurfaceCheck(
                name=name,
                passed=needle in schema_text,
                detail="present" if needle in schema_text else f"missing {needle}",
            )
        )
    return checks


def status_to_json(status: dict) -> str:
    return json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)


def _strip_code(value: str) -> str:
    return value.strip().strip("`")
