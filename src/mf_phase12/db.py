from __future__ import annotations

import sqlite3
import os
from pathlib import Path
from typing import Iterable

from .pg_compat import DEFAULT_POSTGRES_SCHEMA, PostgresCompatConnection
from .hash_utils import stable_hash
from .journal_pattern_provenance import backfill_verified_journal_learning_provenance
from .office_profile import resolve_office_profile, resolve_moneyforward_db_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = None
LEGACY_WORKSPACE_DB_PATH = None
DEFAULT_SCHEMA_PATH = ROOT / "sql" / "phase12_schema.sql"
DB_PATH_ENV = "MF_PHASE12_DB_PATH"
DATABASE_URL_ENV = "MF_PHASE12_DATABASE_URL"
DATABASE_SCHEMA_ENV = "MF_PHASE12_DATABASE_SCHEMA"


class _ManagedSQLiteConnection(sqlite3.Connection):
    """Commit or roll back, then release the SQLite file handle on context exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Phase12Database:
    def __init__(
        self,
        path: Path | str | None = None,
        schema_path: Path | str = DEFAULT_SCHEMA_PATH,
        *,
        database_url: str | None = None,
        database_schema: str | None = None,
    ):
        path_value = str(path) if path is not None else ""
        path_is_postgres_url = path_value.startswith(("postgresql://", "postgresql+psycopg://"))
        # An explicit local path must never be redirected by an ambient DSN.
        self.database_url = database_url or (path_value if path_is_postgres_url else None)
        self.database_schema = database_schema or os.environ.get(DATABASE_SCHEMA_ENV) or DEFAULT_POSTGRES_SCHEMA
        if self.database_url:
            raise ValueError("This starter uses one profile-bound SQLite database; PostgreSQL requires a reviewed adapter")
        profile = resolve_office_profile(required=False)
        self.path = resolve_moneyforward_db_path(profile, path, external_io=profile is not None)
        self.schema_path = Path(schema_path)

    @property
    def is_postgres(self) -> bool:
        return bool(self.database_url)

    def connect(self) -> sqlite3.Connection | PostgresCompatConnection:
        if self.database_url:
            return PostgresCompatConnection(self.database_url, schema=self.database_schema)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, factory=_ManagedSQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def connect_readonly(self) -> sqlite3.Connection:
        """Inspect an existing DB without creating it or running migrations."""
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
            factory=_ManagedSQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def initialize(self) -> None:
        if self.database_url:
            with self.connect() as conn:
                required = {
                    "schema_migrations",
                    "journal_patterns",
                    "rag_cards",
                    "financial_event_classifications",
                    "accounting_runs",
                    "accounting_run_stages",
                    "accounting_run_items",
                    "accounting_run_transitions",
                    "accounting_run_receipts",
                }
                missing = required - self.table_names()
                if missing:
                    raise RuntimeError(
                        f"PostgreSQL schema {self.database_schema!r} is missing Phase12 tables: {sorted(missing)}"
                    )
                conn.execute(
                    """
                    INSERT INTO schema_migrations(version)
                    VALUES ('phase12_postgres_runtime')
                    ON CONFLICT(version) DO NOTHING
                    """
                )
            return
        schema = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema)
            self._migrate(conn)

    def table_names(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {row["name"] for row in rows}

    def execute_many(self, sql: str, params: Iterable[tuple]) -> None:
        with self.connect() as conn:
            conn.executemany(sql, params)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "processing_ledger" in tables:
            self._migrate_processing_ledger_evidence_source_type(conn)
            self._add_column_if_missing(
                conn,
                "processing_ledger",
                "evidence_source_type",
                (
                    "TEXT NOT NULL DEFAULT 'drive' "
                    "CHECK (evidence_source_type IN ('drive', 'invoice_api', 'local_file'))"
                ),
            )
        if "voucher_attach_execution_records" in tables:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(voucher_attach_execution_records)").fetchall()
            }
            if "moneyforward_journal_id_hash" not in columns:
                self._add_column_if_missing(
                    conn,
                    "voucher_attach_execution_records",
                    "moneyforward_journal_id_hash",
                    "TEXT NOT NULL DEFAULT 'sha256:unknown'",
                )
            self._add_column_if_missing(
                conn,
                "voucher_attach_execution_records",
                "requested_file_identity_hash",
                "TEXT",
            )
        if "oauth_scope_checks" in tables:
            self._add_column_if_missing(
                conn,
                "oauth_scope_checks",
                "has_trade_partner_write",
                "INTEGER NOT NULL DEFAULT 0 CHECK (has_trade_partner_write IN (0, 1))",
            )
            self._add_column_if_missing(
                conn,
                "oauth_scope_checks",
                "has_transaction_read",
                "INTEGER NOT NULL DEFAULT 0 CHECK (has_transaction_read IN (0, 1))",
            )
            self._add_column_if_missing(
                conn,
                "oauth_scope_checks",
                "has_invoice_data_read",
                "INTEGER NOT NULL DEFAULT 0 CHECK (has_invoice_data_read IN (0, 1))",
            )
        if "classification_evaluation_runs" in tables:
            self._migrate_classification_evaluation_runs(conn)
        if "classification_evaluation_items" in tables:
            self._migrate_classification_evaluation_items(conn)
        if "journal_pattern_occurrences" not in tables:
            conn.execute(
                """
                CREATE TABLE journal_pattern_occurrences (
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
                )
                """
            )
        conn.execute(
            """
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
            )
            """
        )
        if "drive_move_execution_records" not in tables:
            conn.execute(
                """
                CREATE TABLE drive_move_execution_records (
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
                )
                """
            )
        if "drive_move_readback_verifications" not in tables:
            conn.execute(
                """
                CREATE TABLE drive_move_readback_verifications (
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
                )
                """
            )
        if "drive_move_chain_items" not in tables:
            conn.execute(
                """
                CREATE TABLE drive_move_chain_items (
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
                )
                """
            )
        if "payable_folder_mappings" not in tables:
            conn.execute(
                """
                CREATE TABLE payable_folder_mappings (
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
                )
                """
            )
        if "financial_event_classifications" not in tables:
            conn.execute(
                """
                CREATE TABLE financial_event_classifications (
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
                )
                """
            )
        else:
            self._add_column_if_missing(
                conn,
                    "financial_event_classifications",
                    "cash_flow_direction",
                    (
                        "TEXT NOT NULL DEFAULT 'unknown' "
                        "CHECK (cash_flow_direction IN ('outflow', 'inflow', 'unknown'))"
                    ),
                )
        if "financial_event_learning_events" not in tables:
            conn.execute(
                """
                CREATE TABLE financial_event_learning_events (
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
                )
                """
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO journal_pattern_provenance (
                pattern_hash, source_kind, source_ref_hash
            )
            SELECT pattern_hash, 'moneyforward_snapshot', MIN(source_range_hash)
            FROM journal_pattern_occurrences
            GROUP BY pattern_hash
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO journal_pattern_provenance (
                pattern_hash, source_kind, source_ref_hash
            )
            SELECT pattern_hash, 'financial_event_learning', learning_hash
            FROM financial_event_learning_events
            """
        )
        backfill_verified_journal_learning_provenance(conn)
        if "supplier_primary_card_duplicate_watchlist" not in tables:
            conn.execute(
                """
                CREATE TABLE supplier_primary_card_duplicate_watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_hash TEXT NOT NULL UNIQUE,
                    supplier_source_type TEXT NOT NULL,
                    vendor_key TEXT NOT NULL,
                    transaction_date TEXT NOT NULL,
                    amount_bucket TEXT NOT NULL,
                    source_journal_number TEXT NOT NULL,
                    moneyforward_journal_id_hash TEXT NOT NULL,
                    voucher_verified INTEGER NOT NULL CHECK (voucher_verified IN (0, 1)),
                    credit_account TEXT NOT NULL,
                    credit_sub_account TEXT NOT NULL,
                    expected_card_account_key TEXT NOT NULL,
                    expected_action TEXT NOT NULL
                        CHECK (expected_action IN ('target_exclude_card_statement')),
                    status TEXT NOT NULL
                        CHECK (status IN ('awaiting_card_statement', 'target_exclusion_ready', 'target_excluded_verified', 'cancelled')),
                    raw_journal_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_journal_id_stored = 0),
                    raw_statement_id_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_statement_id_stored = 0),
                    raw_amount_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_amount_stored = 0),
                    raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_secret_stored = 0),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
        if "classification_improvement_candidates" in tables:
            candidate_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(classification_improvement_candidates)").fetchall()
            }
            if "converted_to_rag_card_hash" not in candidate_columns:
                conn.execute(
                    """
                    ALTER TABLE classification_improvement_candidates
                    ADD COLUMN converted_to_rag_card_hash TEXT
                    """
                )
        if "fixed_rule_candidates" not in tables:
            conn.execute(
                """
                CREATE TABLE fixed_rule_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_hash TEXT NOT NULL UNIQUE,
                    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('description_key', 'vendor_key', 'vendor_description_key', 'amount_bucket_key', 'vendor_token_key', 'exact_pattern_key', 'exact_pattern_date_key', 'occurrence_context_key', 'source_event_context_key')),
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
                )
                """
            )
        else:
            self._migrate_fixed_rule_candidates(conn)
        if "google_chat_notification_records" in tables:
            self._add_column_if_missing(
                conn,
                "google_chat_notification_records",
                "accounting_run_id_hash",
                "TEXT",
            )
        if "google_chat_notification_items" in tables:
            self._add_column_if_missing(
                conn,
                "google_chat_notification_items",
                "accounting_run_id_hash",
                "TEXT",
            )
        if "accounting_runs" in tables:
            self._add_column_if_missing(
                conn,
                "accounting_runs",
                "run_mode",
                "TEXT NOT NULL DEFAULT 'production' CHECK (run_mode IN ('production', 'synthetic'))",
            )
            self._add_column_if_missing(
                conn,
                "accounting_runs",
                "creator_actor_hash",
                "TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'",
            )
        if "accounting_run_items" in tables:
            for column, definition in (
                ("resolution_owner_role", "TEXT NOT NULL DEFAULT ''"),
                ("resolution_next_action", "TEXT NOT NULL DEFAULT ''"),
                ("resolution_target_due_at", "TEXT NOT NULL DEFAULT ''"),
                (
                    "resolution_attempt_count",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (resolution_attempt_count >= 0)",
                ),
                ("resolution_blocker_code", "TEXT NOT NULL DEFAULT ''"),
                ("resolution_last_error_code", "TEXT NOT NULL DEFAULT ''"),
                ("resolution_last_attempt_at", "TEXT"),
            ):
                self._add_column_if_missing(conn, "accounting_run_items", column, definition)
        if "accounting_run_receipts" in tables:
            self._migrate_accounting_run_receipts(conn)
        if "vendor_profiles" in tables:
            self._migrate_vendor_profiles(conn)

    def _migrate_fixed_rule_candidates(self, conn: sqlite3.Connection) -> None:
        create_sql = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'fixed_rule_candidates'
            """
        ).fetchone()["sql"]
        if "source_event_context_key" in create_sql:
            return
        conn.execute("ALTER TABLE fixed_rule_candidates RENAME TO fixed_rule_candidates_old")
        conn.execute(
            """
            CREATE TABLE fixed_rule_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_hash TEXT NOT NULL UNIQUE,
                trigger_type TEXT NOT NULL CHECK (trigger_type IN ('description_key', 'vendor_key', 'vendor_description_key', 'amount_bucket_key', 'vendor_token_key', 'exact_pattern_key', 'exact_pattern_date_key', 'occurrence_context_key', 'source_event_context_key')),
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO fixed_rule_candidates (
                id, rule_hash, trigger_type, trigger_key, debit_account,
                debit_sub_account, credit_account, credit_sub_account,
                tax_category, support_count, confidence_score, status,
                raw_data_stored, updated_at
            )
            SELECT id, rule_hash, trigger_type, trigger_key, debit_account,
                   debit_sub_account, credit_account, credit_sub_account,
                   tax_category, support_count, confidence_score, status,
                   raw_data_stored, updated_at
            FROM fixed_rule_candidates_old
            """
        )
        conn.execute("DROP TABLE fixed_rule_candidates_old")

    def _migrate_processing_ledger_evidence_source_type(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(processing_ledger)").fetchall()
        }
        if "evidence_source_type" not in columns:
            return
        create_row = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'processing_ledger'
            """
        ).fetchone()
        create_sql = str(create_row["sql"] or "") if create_row else ""
        if "local_file" in create_sql and "'not_applicable'" in create_sql:
            return

        baseline_violations = {
            tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
        }
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        conn.commit()
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                CREATE TABLE processing_ledger_new (
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
                )
                """
            )
            conn.execute(
                """
                INSERT INTO processing_ledger_new (
                    id, google_drive_file_id_hash, file_name, file_hash,
                    evidence_source_type, candidate_hash, idempotency_key,
                    processing_status, moneyforward_journal_id_hash,
                    moneyforward_voucher_id_hash, dry_run_result_hash,
                    error_message, retry_count, created_at, updated_at
                )
                SELECT id, google_drive_file_id_hash, file_name, file_hash,
                       evidence_source_type, candidate_hash, idempotency_key,
                       processing_status, moneyforward_journal_id_hash,
                       moneyforward_voucher_id_hash, dry_run_result_hash,
                       error_message, retry_count, created_at, updated_at
                FROM processing_ledger
                """
            )
            conn.execute("DROP TABLE processing_ledger")
            conn.execute("ALTER TABLE processing_ledger_new RENAME TO processing_ledger")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processing_ledger_status
                ON processing_ledger(processing_status, updated_at)
                """
            )
            migrated_violations = {
                tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            }
            if migrated_violations != baseline_violations:
                raise RuntimeError(
                    "processing_ledger evidence source migration changed foreign key violations"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if foreign_keys_enabled:
                conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_classification_evaluation_runs(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(classification_evaluation_runs)").fetchall()
        }
        additions = {
            "prediction_strategy": (
                "TEXT NOT NULL DEFAULT 'nearest_pattern' "
                "CHECK (prediction_strategy IN ('nearest_pattern', 'learned_rules_first'))"
            ),
            "learned_rule_confidence_threshold": (
                "REAL NOT NULL DEFAULT 0.8 "
                "CHECK (learned_rule_confidence_threshold >= 0.0 AND learned_rule_confidence_threshold <= 1.0)"
            ),
            "classifiable_sample_count": "INTEGER NOT NULL DEFAULT 0 CHECK (classifiable_sample_count >= 0)",
            "classifiable_overall_accuracy": (
                "REAL NOT NULL DEFAULT 0.0 "
                "CHECK (classifiable_overall_accuracy >= 0.0 AND classifiable_overall_accuracy <= 1.0)"
            ),
            "auto_post_eligible_count": "INTEGER NOT NULL DEFAULT 0 CHECK (auto_post_eligible_count >= 0)",
            "auto_post_eligible_accuracy": (
                "REAL NOT NULL DEFAULT 0.0 "
                "CHECK (auto_post_eligible_accuracy >= 0.0 AND auto_post_eligible_accuracy <= 1.0)"
            ),
            "promotion_allowed": "INTEGER NOT NULL DEFAULT 0 CHECK (promotion_allowed IN (0, 1))",
            "block_reason_code": "TEXT NOT NULL DEFAULT ''",
            "min_sample_required": "INTEGER NOT NULL DEFAULT 100 CHECK (min_sample_required >= 1)",
            "thresholds_hash": "TEXT NOT NULL DEFAULT ''",
            "unknown_pattern_count": "INTEGER NOT NULL DEFAULT 0 CHECK (unknown_pattern_count >= 0)",
            "weak_match_count": "INTEGER NOT NULL DEFAULT 0 CHECK (weak_match_count >= 0)",
            "improvement_candidate_count": "INTEGER NOT NULL DEFAULT 0 CHECK (improvement_candidate_count >= 0)",
        }
        for column, definition in additions.items():
            if column not in columns:
                self._add_column_if_missing(conn, "classification_evaluation_runs", column, definition)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "classification_improvement_candidates" not in tables:
            conn.execute(
                """
                CREATE TABLE classification_improvement_candidates (
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
                )
                """
            )
        if "classification_resolution_queue" not in tables:
            conn.execute(
                """
                CREATE TABLE classification_resolution_queue (
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
                    owner_role TEXT NOT NULL DEFAULT 'evidence_retrieval' CHECK (owner_role IN (
                        'evidence_retrieval', 'accounting_correction', 'accounting_review'
                    )),
                    priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 4),
                    target_due_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+14 days')),
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
                )
                """
            )
        self._migrate_classification_resolution_queue(conn)

    def _migrate_classification_resolution_queue(self, conn: sqlite3.Connection) -> None:
        additions = {
            "owner_role": (
                "TEXT NOT NULL DEFAULT 'evidence_retrieval' "
                "CHECK (owner_role IN ('evidence_retrieval', 'accounting_correction', 'accounting_review'))"
            ),
            "priority": "INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 4)",
            "target_due_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)",
            "last_attempt_at": "TEXT",
            "last_error_code": "TEXT NOT NULL DEFAULT ''",
            "escalation_state": (
                "TEXT NOT NULL DEFAULT 'normal' "
                "CHECK (escalation_state IN ('normal', 'overdue', 'escalated'))"
            ),
        }
        for column, definition in additions.items():
            self._add_column_if_missing(conn, "classification_resolution_queue", column, definition)
        self._canonicalize_classification_resolution_queue(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_classification_resolution_queue_source_pattern
            ON classification_resolution_queue(source_pattern_hash)
            """
        )

    def _canonicalize_classification_resolution_queue(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id, source_pattern_hash, resolution_hash, resolution_state, updated_at
            FROM classification_resolution_queue
            ORDER BY source_pattern_hash, updated_at DESC, id DESC
            """
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["source_pattern_hash"]), []).append(row)
        terminal_states = {"resolved", "not_applicable"}
        for source_pattern_hash, candidates in grouped.items():
            chosen = max(
                candidates,
                key=lambda row: (
                    str(row["resolution_state"]) in terminal_states,
                    str(row["updated_at"]),
                    int(row["id"]),
                ),
            )
            for row in candidates:
                if int(row["id"]) != int(chosen["id"]):
                    conn.execute("DELETE FROM classification_resolution_queue WHERE id = ?", (row["id"],))
            canonical_hash = stable_hash({"source_pattern_hash": source_pattern_hash})
            if str(chosen["resolution_hash"]) != canonical_hash:
                conn.execute(
                    "UPDATE classification_resolution_queue SET resolution_hash = ? WHERE id = ?",
                    (canonical_hash, chosen["id"]),
                )

    def _migrate_classification_evaluation_items(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(classification_evaluation_items)").fetchall()
        }
        additions = {
            "prediction_source": "TEXT NOT NULL DEFAULT 'matched_nearest_pattern'",
            "is_classifiable": "INTEGER NOT NULL DEFAULT 1 CHECK (is_classifiable IN (0, 1))",
            "auto_post_candidate": "INTEGER NOT NULL DEFAULT 0 CHECK (auto_post_candidate IN (0, 1))",
            "failure_reason_code": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if column not in columns:
                self._add_column_if_missing(conn, "classification_evaluation_items", column, definition)

    def _migrate_vendor_profiles(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(vendor_profiles)").fetchall()
        }
        additions = {
            "default_credit_account": "TEXT",
            "default_credit_sub_account": "TEXT",
            "invoice_acquisition_channel": "TEXT NOT NULL DEFAULT 'unknown' CHECK (invoice_acquisition_channel IN ('unknown', 'email', 'paper'))",
            "invoice_acquisition_method": "TEXT NOT NULL DEFAULT 'unknown' CHECK (invoice_acquisition_method IN ('unknown', 'gas_attachment', 'browser_link', 'paper_scan'))",
            "invoice_source_account_hash": "TEXT",
            "invoice_cadence": "TEXT NOT NULL DEFAULT 'unknown' CHECK (invoice_cadence IN ('unknown', 'monthly', 'quarterly', 'annual'))",
            "invoice_lifecycle_status": "TEXT NOT NULL DEFAULT 'untracked' CHECK (invoice_lifecycle_status IN ('untracked', 'candidate', 'active', 'watch', 'dormant', 'archived'))",
            "invoice_consecutive_seen": "INTEGER NOT NULL DEFAULT 0 CHECK (invoice_consecutive_seen >= 0)",
            "invoice_consecutive_missing": "INTEGER NOT NULL DEFAULT 0 CHECK (invoice_consecutive_missing >= 0)",
            "invoice_last_seen_period": "TEXT",
            "invoice_last_acquired_period": "TEXT",
            "invoice_last_acquisition_evidence_hash": "TEXT",
            "invoice_last_evaluated_period": "TEXT",
        }
        for column, definition in additions.items():
            if column not in columns:
                self._add_column_if_missing(conn, "vendor_profiles", column, definition)

    def _migrate_accounting_run_receipts(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'accounting_run_receipts'
            """
        ).fetchone()
        table_sql = str(row["sql"] or "") if row is not None else ""
        if (
            row is None
            or (
                "source_stage_readback" in table_sql
                and "cross_audit_readback" in table_sql
                and "uncertain_not_applied_readback" in table_sql
                and "uncertain_blocked_readback" in table_sql
                and "accounting_date TEXT NOT NULL" in table_sql
            )
        ):
            return
        conn.execute(
            """
            CREATE TABLE accounting_run_receipts_new (
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO accounting_run_receipts_new (
                id, receipt_hash, run_id_hash, stage_key, receipt_kind,
                status, accounting_date, source_hash, actor_hash, created_at
            )
            SELECT r.id, r.receipt_hash, r.run_id_hash, r.stage_key, r.receipt_kind,
                   r.status, a.accounting_date, r.source_hash, r.actor_hash, r.created_at
            FROM accounting_run_receipts AS r
            JOIN accounting_runs AS a ON a.run_id_hash = r.run_id_hash
            """
        )
        conn.execute("DROP TABLE accounting_run_receipts")
        conn.execute(
            "ALTER TABLE accounting_run_receipts_new RENAME TO accounting_run_receipts"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_accounting_run_receipts_run
            ON accounting_run_receipts(run_id_hash, stage_key, receipt_kind)
            """
        )

    def _add_column_if_missing(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error).lower():
                raise
