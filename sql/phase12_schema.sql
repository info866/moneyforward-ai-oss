PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS office_identity_bindings (
    profile_slug TEXT PRIMARY KEY,
    profile_hash TEXT NOT NULL,
    office_id_hash TEXT NOT NULL UNIQUE,
    legal_name_hash TEXT NOT NULL,
    keychain_service_hash TEXT NOT NULL,
    boundary_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified', 'revoked')),
    raw_office_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_office_id_stored = 0),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    verified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(profile_hash) = 64),
    CHECK (length(office_id_hash) = 64),
    CHECK (length(legal_name_hash) = 64),
    CHECK (length(keychain_service_hash) = 64)
);

CREATE TABLE IF NOT EXISTS oauth_scope_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    provider TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    has_journal_write INTEGER NOT NULL CHECK (has_journal_write IN (0, 1)),
    has_voucher_write INTEGER NOT NULL CHECK (has_voucher_write IN (0, 1)),
    has_trade_partner_write INTEGER NOT NULL DEFAULT 0 CHECK (has_trade_partner_write IN (0, 1)),
    has_transaction_read INTEGER NOT NULL DEFAULT 0 CHECK (has_transaction_read IN (0, 1)),
    has_invoice_data_read INTEGER NOT NULL DEFAULT 0 CHECK (has_invoice_data_read IN (0, 1)),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    synced_from TEXT,
    synced_to TEXT,
    synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'partial')),
    error_message TEXT,
    UNIQUE(source_name, source_kind, source_hash)
);

CREATE TABLE IF NOT EXISTS master_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    item_type TEXT NOT NULL,
    external_id_hash TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    valid_from TEXT,
    valid_to TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_name, item_type, external_id_hash)
);

CREATE TABLE IF NOT EXISTS payable_folder_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_hash TEXT NOT NULL UNIQUE,
    moneyforward_account_name TEXT NOT NULL,
    moneyforward_sub_account_name_hash TEXT NOT NULL,
    moneyforward_sub_account_id_hash TEXT NOT NULL,
    google_drive_parent_folder_id_hash TEXT NOT NULL,
    google_drive_folder_id_hash TEXT NOT NULL,
    google_drive_folder_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'mapped', 'disabled')),
    source_hash TEXT NOT NULL,
    raw_moneyforward_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_moneyforward_id_stored = 0),
    raw_drive_folder_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_drive_folder_id_stored = 0),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS processing_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_drive_file_id_hash TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    evidence_source_type TEXT NOT NULL DEFAULT 'drive'
        CHECK (evidence_source_type IN ('drive', 'invoice_api', 'local_file')),
    candidate_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    processing_status TEXT NOT NULL CHECK (
        processing_status IN (
            'new', 'planned', 'blocked', 'excluded', 'not_applicable', 'journal_created',
            'voucher_attached', 'verified', 'failed'
        )
    ),
    moneyforward_journal_id_hash TEXT,
    moneyforward_voucher_id_hash TEXT,
    dry_run_result_hash TEXT NOT NULL,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(google_drive_file_id_hash, file_hash),
    UNIQUE(candidate_hash, dry_run_result_hash)
);

CREATE INDEX IF NOT EXISTS idx_processing_ledger_status
ON processing_ledger(processing_status, updated_at);

CREATE TABLE IF NOT EXISTS write_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    ledger_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('journal_create', 'journal_update', 'voucher_attach', 'journal_verify')
    ),
    source_file_id_hash TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    dry_run_result_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'blocked', 'executed', 'verified', 'failed')),
    moneyforward_journal_id_hash TEXT,
    moneyforward_voucher_id_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    executed_at TEXT,
    verified_at TEXT,
    UNIQUE(idempotency_key, operation_type),
    UNIQUE(operation_type, source_file_id_hash, candidate_hash),
    FOREIGN KEY(ledger_id) REFERENCES processing_ledger(id)
);

CREATE TABLE IF NOT EXISTS approval_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_hash TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    approved_by_hash TEXT NOT NULL,
    rollback_plan_hash TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    revoked_at TEXT,
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS journal_create_execution_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_hash TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    dry_run_hash TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    execution_approval_hash TEXT NOT NULL,
    expected_api_action_hash TEXT NOT NULL,
    rollback_method_hash TEXT NOT NULL,
    claim_token_hash TEXT NOT NULL UNIQUE,
    execution_channel TEXT NOT NULL DEFAULT 'journal_create'
        CHECK (execution_channel IN ('journal_create', 'transaction_journalize')),
    status TEXT NOT NULL CHECK (
        status IN ('executing', 'delivery_uncertain', 'executed', 'verified')
    ),
    uncertainty_reason_code TEXT,
    moneyforward_journal_id_hash TEXT,
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_journal_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_id_stored = 0),
    raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0),
    claimed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    uncertain_at TEXT,
    executed_at TEXT,
    verified_at TEXT,
    CHECK (length(approval_hash) = 64),
    CHECK (length(request_hash) = 64),
    CHECK (length(target_hash) = 64),
    CHECK (length(dry_run_hash) = 64),
    CHECK (length(execution_approval_hash) = 64),
    CHECK (length(expected_api_action_hash) = 64),
    CHECK (length(rollback_method_hash) = 64),
    CHECK (length(claim_token_hash) = 64),
    CHECK (
        moneyforward_journal_id_hash IS NULL
        OR (moneyforward_journal_id_hash LIKE 'sha256:%' AND length(moneyforward_journal_id_hash) = 71)
    ),
    FOREIGN KEY(approval_hash) REFERENCES approval_records(approval_hash),
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_journal_create_execution_status
ON journal_create_execution_records(status, claimed_at);

CREATE TABLE IF NOT EXISTS safety_gate_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high', 'blocker')),
    reason_code TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    result_summary_redacted TEXT NOT NULL,
    checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(idempotency_key, gate_name, result_hash),
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS journal_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_hash TEXT NOT NULL UNIQUE,
    vendor_key TEXT NOT NULL,
    description_key TEXT NOT NULL,
    amount_bucket TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    debit_sub_account TEXT,
    credit_account TEXT NOT NULL,
    credit_sub_account TEXT,
    tax_category TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    auto_post_eligibility TEXT NOT NULL DEFAULT 'disabled'
        CHECK (auto_post_eligibility IN ('disabled', 'shadow', 'limited', 'enabled')),
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS journal_pattern_provenance (
    pattern_hash TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'moneyforward_snapshot',
        'verified_journal_learning',
        'payroll_verified_journal_learning',
        'financial_event_learning'
    )),
    source_ref_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (pattern_hash, source_kind),
    FOREIGN KEY (pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS financial_event_learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learning_hash TEXT NOT NULL UNIQUE,
    event_hash TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    source_event_type TEXT NOT NULL,
    cash_flow_direction TEXT NOT NULL
        CHECK (cash_flow_direction IN ('outflow', 'inflow', 'unknown')),
    learning_source TEXT NOT NULL,
    result_summary_redacted TEXT NOT NULL,
    raw_event_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_event_id_stored = 0),
    raw_amount_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amount_stored = 0),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS journal_pattern_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_hash TEXT NOT NULL UNIQUE,
    pattern_hash TEXT NOT NULL,
    moneyforward_journal_id_hash TEXT NOT NULL,
    line_index INTEGER NOT NULL CHECK (line_index >= 0),
    transaction_date TEXT,
    source_range_hash TEXT NOT NULL,
    raw_data_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_data_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS vendor_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_key TEXT NOT NULL UNIQUE,
    display_name_masked TEXT NOT NULL,
    default_debit_account TEXT,
    default_debit_sub_account TEXT,
    default_credit_account TEXT,
    default_credit_sub_account TEXT,
    default_tax_category TEXT,
    invoice_number_required INTEGER NOT NULL DEFAULT 0 CHECK (invoice_number_required IN (0, 1)),
    confidence_score REAL NOT NULL DEFAULT 0.0 CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    invoice_acquisition_channel TEXT NOT NULL DEFAULT 'unknown'
        CHECK (invoice_acquisition_channel IN ('unknown', 'email', 'paper')),
    invoice_acquisition_method TEXT NOT NULL DEFAULT 'unknown'
        CHECK (invoice_acquisition_method IN ('unknown', 'gas_attachment', 'browser_link', 'paper_scan')),
    invoice_source_account_hash TEXT,
    invoice_cadence TEXT NOT NULL DEFAULT 'unknown'
        CHECK (invoice_cadence IN ('unknown', 'monthly', 'quarterly', 'annual')),
    invoice_lifecycle_status TEXT NOT NULL DEFAULT 'untracked'
        CHECK (invoice_lifecycle_status IN ('untracked', 'candidate', 'active', 'watch', 'dormant', 'archived')),
    invoice_consecutive_seen INTEGER NOT NULL DEFAULT 0 CHECK (invoice_consecutive_seen >= 0),
    invoice_consecutive_missing INTEGER NOT NULL DEFAULT 0 CHECK (invoice_consecutive_missing >= 0),
    invoice_last_seen_period TEXT,
    invoice_last_acquired_period TEXT,
    invoice_last_acquisition_evidence_hash TEXT,
    invoice_last_evaluated_period TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS fixed_rule_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_hash TEXT NOT NULL UNIQUE,
    trigger_type TEXT NOT NULL CHECK (
        trigger_type IN (
            'description_key', 'vendor_key', 'vendor_description_key',
            'amount_bucket_key', 'vendor_token_key', 'exact_pattern_key',
            'exact_pattern_date_key', 'occurrence_context_key',
            'source_event_context_key'
        )
    ),
    trigger_key TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    debit_sub_account TEXT,
    credit_account TEXT NOT NULL,
    credit_sub_account TEXT,
    tax_category TEXT NOT NULL,
    support_count INTEGER NOT NULL CHECK (support_count >= 1),
    confidence_score REAL NOT NULL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'shadow', 'approved', 'suspended')),
    raw_data_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_data_stored = 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS failure_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    failure_hash TEXT NOT NULL UNIQUE,
    failure_type TEXT NOT NULL,
    trigger_text TEXT NOT NULL,
    blocked_reason TEXT NOT NULL,
    corrected_rule_json TEXT NOT NULL DEFAULT '{}',
    source_journal_id_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS rag_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_hash TEXT NOT NULL UNIQUE,
    card_type TEXT NOT NULL CHECK (
        card_type IN (
            'vendor_profile', 'journal_pattern', 'failure_example',
            'correction_diff', 'invoice_pattern', 'bank_transaction_pattern',
            'fixed_rule_candidate'
        )
    ),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    embedding_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_cards_fts
USING fts5(title, body, content='rag_cards', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS rag_cards_ai AFTER INSERT ON rag_cards BEGIN
    INSERT INTO rag_cards_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS rag_cards_ad AFTER DELETE ON rag_cards BEGIN
    INSERT INTO rag_cards_fts(rag_cards_fts, rowid, title, body)
    VALUES('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS rag_cards_au AFTER UPDATE ON rag_cards BEGIN
    INSERT INTO rag_cards_fts(rag_cards_fts, rowid, title, body)
    VALUES('delete', old.id, old.title, old.body);
    INSERT INTO rag_cards_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS financial_event_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_hash TEXT NOT NULL UNIQUE,
    source_event_type TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    description_key TEXT NOT NULL,
    amount_bucket TEXT NOT NULL,
    cash_flow_direction TEXT NOT NULL DEFAULT 'unknown'
        CHECK (cash_flow_direction IN ('outflow', 'inflow', 'unknown')),
    account_key_hash TEXT NOT NULL,
    matched_pattern_hash TEXT,
    matched_pattern_source TEXT NOT NULL,
    confidence_score REAL NOT NULL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    recommended_action TEXT NOT NULL
        CHECK (recommended_action IN ('auto_post_candidate', 'queue_for_review', 'queue_for_learning')),
    registration_allowed INTEGER NOT NULL CHECK (registration_allowed IN (0, 1)),
    learning_action TEXT NOT NULL,
    unresolved_points_json TEXT NOT NULL,
    result_summary_redacted TEXT NOT NULL,
    raw_event_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_event_id_stored = 0),
    raw_amount_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amount_stored = 0),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(matched_pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS accounting_decision_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('decided', 'review')),
    normalized_result_json TEXT,
    reason_codes_json TEXT NOT NULL,
    raw_document_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_document_stored = 0),
    raw_model_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_model_response_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(packet_hash, model_id, policy_version)
);

CREATE TABLE IF NOT EXISTS shadow_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_hash TEXT NOT NULL UNIQUE,
    pattern_hash TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    predicted_json TEXT NOT NULL,
    account_match INTEGER NOT NULL CHECK (account_match IN (0, 1)),
    sub_account_match INTEGER NOT NULL CHECK (sub_account_match IN (0, 1)),
    tax_match INTEGER NOT NULL CHECK (tax_match IN (0, 1)),
    amount_match INTEGER NOT NULL CHECK (amount_match IN (0, 1)),
    partner_match INTEGER NOT NULL CHECK (partner_match IN (0, 1)),
    description_quality REAL NOT NULL CHECK (description_quality >= 0.0 AND description_quality <= 1.0),
    evaluated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS shadow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_hash TEXT NOT NULL UNIQUE,
    source_manifest_id INTEGER,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
    FOREIGN KEY(source_manifest_id) REFERENCES sync_manifests(id)
);

CREATE TABLE IF NOT EXISTS shadow_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_hash TEXT NOT NULL,
    source_journal_ref_hash TEXT NOT NULL,
    prediction_hash TEXT NOT NULL,
    actual_hash TEXT NOT NULL,
    account_match INTEGER NOT NULL CHECK (account_match IN (0, 1)),
    sub_account_match INTEGER NOT NULL CHECK (sub_account_match IN (0, 1)),
    tax_match INTEGER NOT NULL CHECK (tax_match IN (0, 1)),
    amount_match INTEGER NOT NULL CHECK (amount_match IN (0, 1)),
    partner_match INTEGER NOT NULL CHECK (partner_match IN (0, 1)),
    description_quality_score REAL NOT NULL CHECK (description_quality_score >= 0.0 AND description_quality_score <= 1.0),
    voucher_attach_match INTEGER NOT NULL CHECK (voucher_attach_match IN (0, 1)),
    overall_passed INTEGER NOT NULL CHECK (overall_passed IN (0, 1)),
    FOREIGN KEY(run_hash) REFERENCES shadow_runs(run_hash)
);

CREATE TABLE IF NOT EXISTS classification_evaluation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_hash TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    prediction_strategy TEXT NOT NULL DEFAULT 'nearest_pattern'
        CHECK (prediction_strategy IN ('nearest_pattern', 'learned_rules_first')),
    learned_rule_confidence_threshold REAL NOT NULL DEFAULT 0.8
        CHECK (learned_rule_confidence_threshold >= 0.0 AND learned_rule_confidence_threshold <= 1.0),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    classifiable_sample_count INTEGER NOT NULL DEFAULT 0 CHECK (classifiable_sample_count >= 0),
    account_accuracy REAL NOT NULL CHECK (account_accuracy >= 0.0 AND account_accuracy <= 1.0),
    sub_account_accuracy REAL NOT NULL CHECK (sub_account_accuracy >= 0.0 AND sub_account_accuracy <= 1.0),
    tax_accuracy REAL NOT NULL CHECK (tax_accuracy >= 0.0 AND tax_accuracy <= 1.0),
    overall_accuracy REAL NOT NULL CHECK (overall_accuracy >= 0.0 AND overall_accuracy <= 1.0),
    classifiable_overall_accuracy REAL NOT NULL DEFAULT 0.0
        CHECK (classifiable_overall_accuracy >= 0.0 AND classifiable_overall_accuracy <= 1.0),
    auto_post_eligible_count INTEGER NOT NULL DEFAULT 0 CHECK (auto_post_eligible_count >= 0),
    auto_post_eligible_accuracy REAL NOT NULL DEFAULT 0.0
        CHECK (auto_post_eligible_accuracy >= 0.0 AND auto_post_eligible_accuracy <= 1.0),
    status TEXT NOT NULL CHECK (status IN ('ok', 'insufficient_data', 'failed')),
    promotion_allowed INTEGER NOT NULL DEFAULT 0 CHECK (promotion_allowed IN (0, 1)),
    block_reason_code TEXT NOT NULL DEFAULT '',
    min_sample_required INTEGER NOT NULL DEFAULT 100 CHECK (min_sample_required >= 1),
    thresholds_hash TEXT NOT NULL DEFAULT '',
    unknown_pattern_count INTEGER NOT NULL DEFAULT 0 CHECK (unknown_pattern_count >= 0),
    weak_match_count INTEGER NOT NULL DEFAULT 0 CHECK (weak_match_count >= 0),
    improvement_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (improvement_candidate_count >= 0),
    raw_data_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_data_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS classification_evaluation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_hash TEXT NOT NULL,
    source_pattern_hash TEXT NOT NULL,
    predicted_pattern_hash TEXT,
    prediction_source TEXT NOT NULL DEFAULT 'matched_nearest_pattern',
    is_classifiable INTEGER NOT NULL DEFAULT 1 CHECK (is_classifiable IN (0, 1)),
    auto_post_candidate INTEGER NOT NULL DEFAULT 0 CHECK (auto_post_candidate IN (0, 1)),
    feature_match_score INTEGER NOT NULL CHECK (feature_match_score >= 0),
    account_match INTEGER NOT NULL CHECK (account_match IN (0, 1)),
    sub_account_match INTEGER NOT NULL CHECK (sub_account_match IN (0, 1)),
    tax_match INTEGER NOT NULL CHECK (tax_match IN (0, 1)),
    overall_passed INTEGER NOT NULL CHECK (overall_passed IN (0, 1)),
    failure_reason_code TEXT NOT NULL DEFAULT '',
    reason_redacted TEXT NOT NULL,
    raw_data_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_data_stored = 0),
    FOREIGN KEY(run_hash) REFERENCES classification_evaluation_runs(run_hash),
    FOREIGN KEY(source_pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS classification_improvement_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_hash TEXT NOT NULL UNIQUE,
    source_pattern_hash TEXT NOT NULL,
    predicted_pattern_hash TEXT,
    issue_type TEXT NOT NULL CHECK (
        issue_type IN (
            'account_mismatch', 'sub_account_mismatch', 'tax_mismatch',
            'weak_match_passed', 'no_comparable_past_pattern'
        )
    ),
    feature_match_score INTEGER NOT NULL CHECK (feature_match_score >= 0),
    blocking_reason TEXT NOT NULL,
    suggested_action TEXT NOT NULL CHECK (
        suggested_action IN (
            'add_failure_rag', 'add_vendor_profile',
            'add_fixed_rule', 'collect_more_examples'
        )
    ),
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    converted_to_rag_card_hash TEXT,
    raw_data_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_data_stored = 0),
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(source_pattern_hash) REFERENCES journal_patterns(pattern_hash)
);

CREATE TABLE IF NOT EXISTS classification_resolution_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_hash TEXT NOT NULL UNIQUE,
    source_pattern_hash TEXT NOT NULL,
    last_evaluation_run_hash TEXT NOT NULL,
    resolution_tag TEXT NOT NULL CHECK (resolution_tag IN (
        'needs_source_evidence',
        'account_correction_review',
        'sub_account_correction_review',
        'tax_correction_review',
        'manual_classification_review'
    )),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN (
        'open', 'evidence_requested', 'evidence_collected', 'correction_review',
        'correction_draft_ready', 'correction_posted', 'resolved', 'not_applicable'
    )),
    next_action TEXT NOT NULL,
    owner_role TEXT NOT NULL CHECK (owner_role IN (
        'evidence_retrieval', 'accounting_correction', 'accounting_review'
    )),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 4),
    target_due_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    escalation_state TEXT NOT NULL DEFAULT 'normal' CHECK (escalation_state IN ('normal', 'overdue', 'escalated')),
    correction_required INTEGER NOT NULL CHECK (correction_required IN (0, 1)),
    source_evidence_required INTEGER NOT NULL CHECK (source_evidence_required IN (0, 1)),
    occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(source_pattern_hash) REFERENCES journal_patterns(pattern_hash),
    FOREIGN KEY(last_evaluation_run_hash) REFERENCES classification_evaluation_runs(run_hash)
);

CREATE TABLE IF NOT EXISTS promotion_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('shadow_only', 'limited_auto', 'production_auto', 'suspended')),
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    post_registration_modified_count INTEGER NOT NULL DEFAULT 0 CHECK (post_registration_modified_count >= 0),
    last_shadow_score REAL NOT NULL DEFAULT 0.0 CHECK (last_shadow_score >= 0.0 AND last_shadow_score <= 1.0),
    eligible_since TEXT,
    suspended_at TEXT,
    suspend_reason TEXT
);

CREATE TABLE IF NOT EXISTS post_registration_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    moneyforward_journal_id_hash TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    actual_hash TEXT NOT NULL,
    voucher_file_ids_hash TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    diff_summary_redacted TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS voucher_attach_execution_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_hash TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    moneyforward_journal_id_hash TEXT NOT NULL DEFAULT 'sha256:unknown',
    dry_run_hash TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    expected_api_action_hash TEXT NOT NULL,
    rollback_plan_hash TEXT NOT NULL,
    user_approval_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('approved', 'executing', 'executed', 'verified', 'failed', 'revoked')),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_journal_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_id_stored = 0),
    raw_voucher_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_voucher_file_ids_stored = 0),
    raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    executed_at TEXT,
    verified_at TEXT,
    revoked_at TEXT,
    requested_file_identity_hash TEXT,
    CHECK (length(approval_hash) = 64),
    CHECK (length(request_hash) = 64),
    CHECK (length(target_hash) = 64),
    CHECK (moneyforward_journal_id_hash = 'sha256:unknown' OR (moneyforward_journal_id_hash LIKE 'sha256:%' AND length(moneyforward_journal_id_hash) = 71)),
    CHECK (length(dry_run_hash) = 64),
    CHECK (length(scope_hash) = 64),
    CHECK (length(expected_api_action_hash) = 64),
    CHECK (length(rollback_plan_hash) = 64),
    CHECK (length(user_approval_hash) = 64)
);

CREATE TABLE IF NOT EXISTS voucher_attach_readback_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_hash TEXT NOT NULL UNIQUE,
    approval_hash TEXT NOT NULL UNIQUE,
    journal_id_hash TEXT NOT NULL,
    expected_voucher_file_ids_hash TEXT NOT NULL,
    actual_voucher_file_ids_hash TEXT NOT NULL,
    post_result_voucher_file_ids_hash TEXT NOT NULL,
    response_hash TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    diff_summary_redacted TEXT NOT NULL,
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_journal_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_id_stored = 0),
    raw_voucher_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_voucher_file_ids_stored = 0),
    raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0),
    verified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(verification_hash) = 64),
    CHECK (length(approval_hash) = 64),
    CHECK (journal_id_hash LIKE 'sha256:%' AND length(journal_id_hash) = 71),
    CHECK (length(expected_voucher_file_ids_hash) = 64),
    CHECK (length(actual_voucher_file_ids_hash) = 64),
    CHECK (length(post_result_voucher_file_ids_hash) = 64),
    CHECK (length(response_hash) = 64),
    FOREIGN KEY(approval_hash) REFERENCES voucher_attach_execution_records(approval_hash)
);

CREATE TABLE IF NOT EXISTS drive_move_execution_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_hash TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    dry_run_hash TEXT NOT NULL,
    expected_api_action_hash TEXT NOT NULL,
    rollback_plan_hash TEXT NOT NULL,
    user_approval_hash TEXT NOT NULL,
    moved_count INTEGER NOT NULL CHECK (moved_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('approved', 'executing', 'executed', 'verified', 'failed', 'revoked')),
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_drive_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_drive_file_ids_stored = 0),
    raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    executed_at TEXT,
    verified_at TEXT,
    revoked_at TEXT,
    CHECK (length(approval_hash) = 64),
    CHECK (length(request_hash) = 64),
    CHECK (length(target_hash) = 64),
    CHECK (length(dry_run_hash) = 64),
    CHECK (length(expected_api_action_hash) = 64),
    CHECK (length(rollback_plan_hash) = 64),
    CHECK (length(user_approval_hash) = 64)
);

CREATE TABLE IF NOT EXISTS drive_move_readback_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_hash TEXT NOT NULL UNIQUE,
    approval_hash TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    expected_moved_count INTEGER NOT NULL CHECK (expected_moved_count >= 0),
    verified_count INTEGER NOT NULL CHECK (verified_count >= 0),
    response_hash TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    diff_summary_redacted TEXT NOT NULL,
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_drive_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_drive_file_ids_stored = 0),
    raw_api_response_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_api_response_stored = 0),
    verified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(verification_hash) = 64),
    CHECK (length(approval_hash) = 64),
    CHECK (length(request_hash) = 64),
    CHECK (length(target_hash) = 64),
    CHECK (length(response_hash) = 64),
    FOREIGN KEY(approval_hash) REFERENCES drive_move_execution_records(approval_hash)
);

CREATE TABLE IF NOT EXISTS drive_move_chain_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_drive_file_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_drive_file_ids_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(approval_hash, idempotency_key),
    FOREIGN KEY(approval_hash) REFERENCES drive_move_execution_records(approval_hash),
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS correction_diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT,
    moneyforward_journal_id_hash TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    diff_summary_redacted TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    converted_to_rag_card_hash TEXT,
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_hash TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL,
    response_hash TEXT,
    actor TEXT NOT NULL,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    rollback_plan TEXT NOT NULL,
    moneyforward_journal_id_hash TEXT,
    moneyforward_voucher_id_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY(idempotency_key) REFERENCES processing_ledger(idempotency_key)
);

CREATE TABLE IF NOT EXISTS accounting_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id_hash TEXT NOT NULL UNIQUE,
    office_hash TEXT NOT NULL,
    execution_principal_hash TEXT NOT NULL,
    accounting_date TEXT NOT NULL,
    instruction_hash TEXT NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'production' CHECK (run_mode IN ('production', 'synthetic')),
    creator_actor_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'running',
        'failed',
        'uncertain_external_write',
        'complete'
    )),
    current_stage TEXT,
    last_failed_stage TEXT,
    last_error_code TEXT,
    last_error_hash TEXT,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    raw_ids_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_ids_stored = 0),
    raw_amounts_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amounts_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    CHECK (length(run_id_hash) = 64),
    CHECK (length(office_hash) = 64),
    CHECK (length(execution_principal_hash) = 64),
    CHECK (length(instruction_hash) = 64),
    CHECK (length(creator_actor_hash) = 64)
);

CREATE TABLE IF NOT EXISTS accounting_run_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id_hash TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    stage_order INTEGER NOT NULL CHECK (stage_order >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'pending',
        'running',
        'succeeded',
        'retryable_failure',
        'blocked',
        'uncertain_external_write',
        'terminal_failure'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    input_hash TEXT,
    output_hash TEXT,
    binding_hash TEXT,
    failure_code TEXT,
    failure_hash TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(run_id_hash, stage_key),
    CHECK (input_hash IS NULL OR length(input_hash) = 64),
    CHECK (output_hash IS NULL OR length(output_hash) = 64),
    CHECK (binding_hash IS NULL OR length(binding_hash) = 64),
    CHECK (failure_hash IS NULL OR length(failure_hash) = 64),
    FOREIGN KEY(run_id_hash) REFERENCES accounting_runs(run_id_hash)
);

CREATE INDEX IF NOT EXISTS idx_accounting_run_stages_status
ON accounting_run_stages(run_id_hash, status, stage_order);

CREATE TABLE IF NOT EXISTS accounting_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id_hash TEXT NOT NULL,
    item_hash TEXT NOT NULL,
    source_stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'verified', 'not_applicable', 'failed')),
    idempotency_key_hash TEXT,
    resolution_owner_role TEXT NOT NULL DEFAULT '',
    resolution_next_action TEXT NOT NULL DEFAULT '',
    resolution_target_due_at TEXT NOT NULL DEFAULT '',
    resolution_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (resolution_attempt_count >= 0),
    resolution_blocker_code TEXT NOT NULL DEFAULT '',
    resolution_last_error_code TEXT NOT NULL DEFAULT '',
    resolution_last_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(run_id_hash, item_hash),
    CHECK (length(item_hash) = 64),
    CHECK (idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64),
    FOREIGN KEY(run_id_hash) REFERENCES accounting_runs(run_id_hash)
);

CREATE INDEX IF NOT EXISTS idx_accounting_run_items_status
ON accounting_run_items(run_id_hash, status, source_stage);

CREATE TABLE IF NOT EXISTS accounting_run_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transition_hash TEXT NOT NULL UNIQUE,
    run_id_hash TEXT NOT NULL,
    stage_key TEXT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    actor_hash TEXT NOT NULL,
    evidence_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(transition_hash) = 64),
    CHECK (length(actor_hash) = 64),
    CHECK (evidence_hash IS NULL OR length(evidence_hash) = 64),
    FOREIGN KEY(run_id_hash) REFERENCES accounting_runs(run_id_hash)
);

CREATE INDEX IF NOT EXISTS idx_accounting_run_transitions_run
ON accounting_run_transitions(run_id_hash, id);

CREATE TABLE IF NOT EXISTS accounting_run_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_hash TEXT NOT NULL UNIQUE,
    run_id_hash TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    receipt_kind TEXT NOT NULL CHECK (receipt_kind IN (
        'source_stage_readback',
        'cross_audit_readback',
        'uncertain_not_applied_readback',
        'uncertain_blocked_readback',
        'google_chat_dedup_readback',
        'verified_journal_learning',
        'github_issue_readback'
    )),
    status TEXT NOT NULL CHECK (status = 'verified'),
    accounting_date TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    actor_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(receipt_hash) = 64),
    CHECK (length(source_hash) = 64),
    CHECK (length(actor_hash) = 64),
    FOREIGN KEY(run_id_hash) REFERENCES accounting_runs(run_id_hash)
);

CREATE INDEX IF NOT EXISTS idx_accounting_run_receipts_run
ON accounting_run_receipts(run_id_hash, stage_key, receipt_kind);

CREATE TABLE IF NOT EXISTS google_chat_notification_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_hash TEXT NOT NULL UNIQUE,
    notification_type TEXT NOT NULL CHECK (notification_type IN ('completion', 'pending')),
    message_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    status TEXT NOT NULL CHECK (status IN ('dispatching', 'sent', 'historical_sent', 'uncertain_failure')),
    response_hash TEXT,
    failure_hash TEXT,
    accounting_run_id_hash TEXT,
    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
    raw_message_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_message_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    CHECK (accounting_run_id_hash IS NULL OR length(accounting_run_id_hash) = 64)
);

CREATE TABLE IF NOT EXISTS google_chat_notification_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_hash TEXT NOT NULL UNIQUE,
    notification_hash TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (notification_type IN ('completion', 'pending')),
    status TEXT NOT NULL CHECK (status IN ('dispatching', 'sent', 'historical_sent', 'uncertain_failure')),
    source_hash TEXT NOT NULL,
    accounting_run_id_hash TEXT,
    raw_journal_number_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_number_stored = 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    CHECK (accounting_run_id_hash IS NULL OR length(accounting_run_id_hash) = 64),
    FOREIGN KEY(notification_hash) REFERENCES google_chat_notification_records(notification_hash)
);

CREATE INDEX IF NOT EXISTS idx_google_chat_notification_items_status
ON google_chat_notification_items(status, notification_type);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('phase12_v1');
INSERT OR IGNORE INTO schema_migrations(version) VALUES ('phase12_accounting_run_v2');
