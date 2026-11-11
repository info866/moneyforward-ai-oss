"""Synthetic execution-boundary fixtures shared by the demo and acceptance tests.

No pytest dependency, credential lookup, external transport, or persistent service.
The source facts and planned ledger are explicit fixture inputs, not evidence of
OCR, classification, master synchronization, or source-document verification.
Only the SDK approval and bundled POST/GET verification code advance execution.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlsplit

from mf_phase12.accounting_api import (
    JournalSideMasterIds,
    MoneyForwardAccountingClient,
    SingleBranchJournalMasterIds,
    build_single_branch_journal_create_request,
)
from mf_phase12.db import Phase12Database
from mf_phase12.hash_utils import mask_identifier, stable_hash
from mf_phase12.journal_write_post import (
    JournalCreateExecutionDryRun,
    dry_run_journal_create_execution,
    execute_journal_create_with_connection_factory,
)
from mf_phase12.journal_write_verify import (
    resolve_uncertain_journal_create_by_read_back_with_connection_factory,
    verify_journal_create_read_back_with_connection_factory,
)
from mf_phase12.models import JournalCandidate
from mf_phase12.office_profile import (
    OfficeProfile,
    bind_database_office_identity,
    using_office_profile,
)
from mf_phase12.sdk import MoneyForwardDryRunSDK


@dataclass(frozen=True)
class SourceLine:
    account: str
    tax_id: str
    net: int
    tax: int
    rate: float
    description: str


# Invented here; never derived from a real journal, document, or company.
SOURCE_LINES = (
    SourceLine("synthetic-supplies", "synthetic-tax-10", 1000, 100, .10, "Synthetic stationery"),
    SourceLine("synthetic-food", "synthetic-tax-8", 2000, 160, .08, "Synthetic food"),
)
OFFICE_ID = "synthetic-office"
LEGAL_NAME = "Synthetic Accounting Fixture"
ROLLBACK = "reverse the exact journal_id only after separate explicit approval"


def journal_request(lines: tuple[SourceLine, ...]) -> dict[str, Any]:
    """Construct each branch through the bundled first-party request builder."""
    branches = []
    for line in lines:
        candidate = JournalCandidate(
            journal_date="2026-01-15", vendor_name="Synthetic Supplier",
            amount=line.net + line.tax, tax_amount=line.tax, tax_rate=line.rate,
            debit_account=line.account, debit_sub_account=None,
            credit_account="synthetic-bank", credit_sub_account=None,
            tax_category=line.tax_id, description=line.description,
        )
        built = build_single_branch_journal_create_request(
            candidate,
            SingleBranchJournalMasterIds(
                debitor=JournalSideMasterIds(account_id=line.account, tax_id=line.tax_id),
                creditor=JournalSideMasterIds(account_id="synthetic-bank", tax_id="synthetic-tax-exempt"),
            ),
        )
        branches.extend(built["branches"])
    return {"transaction_date": "2026-01-15", "journal_type": "journal_entry", "branches": branches}


class SyntheticJournalTransport:
    """Protocol-conforming in-memory provider; every unknown route fails closed.

    POST stores a copy and computes net/tax GET fields using a synthetic rate
    table. GET returns that committed copy, never the caller's expected object.
    A timeout occurs AFTER committing exactly one mutation. The test operator
    can discover its ID through the synthetic journal-list GET after restart;
    production journal discovery and idempotency are not simulated guarantees.
    """

    def __init__(self, *, timeout_after_commit: bool = False):
        self._token = token_urlsafe(32)  # ephemeral fake bearer, never persisted
        self.office_name = LEGAL_NAME
        self.timeout_after_commit = timeout_after_commit
        self.journals: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.post_attempts = 0
        self.mutations = 0

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api-accounting.moneyforward.com":
            raise AssertionError("unexpected synthetic origin")
        if (headers or {}).get("Authorization") != f"Bearer {self._token}":
            raise PermissionError("synthetic authorization mismatch")
        path = parsed.path
        self.calls.append((method, path))
        status = 200
        if method == "GET" and path == "/api/v3/offices":
            response = {"data": [{"id": OFFICE_ID, "name": self.office_name}]}
        elif method == "POST" and path == "/api/v3/journals":
            self.post_attempts += 1
            submitted = json.loads(body)["journal"]
            stored = deepcopy(submitted)
            for branch in stored["branches"]:
                for side_name in ("debitor", "creditor"):
                    side = branch[side_name]
                    percent = {"synthetic-tax-10": 10, "synthetic-tax-8": 8,
                               "synthetic-tax-exempt": 0}[side["tax_id"]]
                    gross = side["value"]
                    tax = gross * percent // (100 + percent)
                    side.update(value=gross - tax, tax_value=tax)
            journal_id = f"synthetic-journal-{len(self.journals) + 1}"
            stored["id"] = journal_id
            self.journals[journal_id] = stored
            self.mutations += 1
            if self.timeout_after_commit:
                self.timeout_after_commit = False
                raise TimeoutError("synthetic timeout after committed journal")
            status, response = 201, {"id": journal_id}
        elif method == "GET" and path == "/api/v3/journals":
            response = {"data": list(deepcopy(self.journals).values())}
        elif method == "GET" and path.startswith("/api/v3/journals/"):
            response = {"journal": deepcopy(self.journals[path.rsplit("/", 1)[1]])}
        elif method == "GET" and path == "/api/v3/term_settings":
            response = {"data": [{"start_date": "2026-01-01", "end_date": "2026-12-31",
                                   "tax_method": "INDIVIDUAL_ALLOCATION"}]}
        elif method == "GET" and path == "/api/v3/taxes":
            response = {"data": [{"id": "synthetic-tax-10", "name": "課税仕入 10%"},
                                   {"id": "synthetic-tax-8", "name": "課税仕入 (軽)8%"},
                                   {"id": "synthetic-tax-exempt", "name": "対象外"}]}
        else:
            raise AssertionError("unexpected synthetic method or route")
        return status, {}, json.dumps(response).encode()


@dataclass
class AccountingScenario:
    profile: OfficeProfile
    provider: SyntheticJournalTransport
    db: Phase12Database
    client: MoneyForwardAccountingClient
    journal: dict[str, Any]
    grant: dict[str, Any]
    dry_run: JournalCreateExecutionDryRun
    execution_approval: dict[str, Any]

    @property
    def expected_tax_values(self):
        # Independent synthetic source oracle; not copied from provider GET.
        return [{"debitor": line.tax, "creditor": 0}
                for line in SOURCE_LINES[:len(self.journal["branches"])]]

    def restart(self) -> None:
        """New DB facade and client; all SQLite handles already closed per call."""
        with using_office_profile(self.profile):
            self.db = Phase12Database(self.profile.moneyforward_db_path)
        self.client = new_client(self.profile, self.provider)

    def execute(self):
        return execute_journal_create_with_connection_factory(
            connect=self.db.connect, client=self.client, dry_run=self.dry_run,
            grant=self.grant, journal=self.journal,
            execution_approval=self.execution_approval, office_id=OFFICE_ID,
        )

    def verify(self, post):
        return verify_journal_create_read_back_with_connection_factory(
            self.db.connect, client=self.client, journal_id=post.moneyforward_journal_id,
            journal=self.journal, post_result=post.to_redacted_dict(), office_id=OFFICE_ID,
            expected_tax_values=self.expected_tax_values,
        )

    def reconcile(self, journal_id):
        return resolve_uncertain_journal_create_by_read_back_with_connection_factory(
            self.db.connect, client=self.client, journal_id=journal_id,
            journal=self.journal, idempotency_key=self.dry_run.idempotency_key,
            approval_hash=self.dry_run.approval_hash, office_id=OFFICE_ID,
            expected_tax_values=self.expected_tax_values,
        )

    def states(self):
        with self.db.connect() as conn:
            return tuple(conn.execute(sql, (self.dry_run.idempotency_key,)).fetchone()[0]
                         for sql in (
                             "SELECT status FROM write_intents WHERE idempotency_key=?",
                             "SELECT status FROM journal_create_execution_records WHERE idempotency_key=?",
                             "SELECT processing_status FROM processing_ledger WHERE idempotency_key=?",
                         ))


def new_client(profile, provider):
    # A service NAME is required by the identity guard; no Keychain code runs.
    return MoneyForwardAccountingClient(
        provider._token, office_profile=profile, transport=provider,
        keychain_service=profile.moneyforward_keychain_service,
        db_path=profile.moneyforward_db_path, write_enabled=True,
    )


def prepare_scenario(root: Path, *, multiline=False, timeout_after_commit=False):
    """Seed a synthetic approved request at the journal-execution boundary.

    Planned source/intent rows are explicit prerequisites. Never seed executed,
    verified, execution-claim, or read-back rows. Approval is created by the real
    SDK, and dry-run validation binds it to the exact request before any POST.
    CLI file-based grant generation and SDK source planning are outside scope.
    """
    profile = OfficeProfile(
        schema_version=2, configured=True, slug="synthetic-accounting",
        legal_name=LEGAL_NAME, display_name=LEGAL_NAME, expected_office_names=(LEGAL_NAME,),
        google_account="fixture@synthetic.invalid", google_drive_account="fixture@synthetic.invalid",
        moneyforward_keychain_service="unused-synthetic-accounting",
        moneyforward_db_path=root / "data/phase12/offices/synthetic-accounting/ledger.sqlite",
        repository_root=root, google_drive_evidence_root_id="synthetic_evidence_root",
        google_drive_evidence_source_folder_ids=("synthetic_evidence_root",),
        google_drive_processed_folder_id="synthetic_processed_folder",
    )
    provider = SyntheticJournalTransport(timeout_after_commit=timeout_after_commit)
    client = new_client(profile, provider)
    journal = journal_request(SOURCE_LINES if multiline else SOURCE_LINES[:1])
    metadata = client.create_journal_request_metadata(journal=journal, office_id=OFFICE_ID)
    request_hash = metadata["request_hash"]
    source_hash = stable_hash({"synthetic_document": "mixed" if multiline else "single"})
    idem = stable_hash({"source": source_hash, "request": request_hash})
    with using_office_profile(profile):
        db = Phase12Database(profile.moneyforward_db_path)
        db.initialize()
        with db.connect() as conn:
            bind_database_office_identity(conn, profile, client.validate_office_identity())
            ledger = conn.execute(
                "INSERT INTO processing_ledger "
                "(google_drive_file_id_hash,file_name,file_hash,candidate_hash,idempotency_key,"
                "processing_status,dry_run_result_hash) VALUES (?,?,?,?,?,'planned',?)",
                (mask_identifier("synthetic-document"), "synthetic-source.txt", source_hash,
                 stable_hash(journal), idem, stable_hash({"synthetic_prerequisite": source_hash})),
            )
            conn.execute(
                "INSERT INTO write_intents "
                "(idempotency_key,ledger_id,operation_type,source_file_id_hash,candidate_hash,"
                "dry_run_result_hash,plan_hash,request_hash,status) "
                "VALUES (?,?,'journal_create',?,?,?,?,?,'planned')",
                (idem, ledger.lastrowid, mask_identifier("synthetic-document"), stable_hash(journal),
                 stable_hash({"synthetic_prerequisite": source_hash}), stable_hash({"plan": idem}), request_hash),
            )
            approval_hash = MoneyForwardDryRunSDK(conn, actor="synthetic-owner").create_approval_record(
                idempotency_key=idem, operation_type="journal_create",
                scope_hash=stable_hash("synthetic-journal-write-scope"),
                approved_by="synthetic-owner", rollback_plan=ROLLBACK,
            )
            grant = dict(
                operation_type="journal_create", execution_channel="journal_create",
                expected_api_action="POST /api/v3/journals", write_execution_grant_ready=True,
                idempotency_key=idem, approval_hash=approval_hash,
                scope_hash=stable_hash("synthetic-journal-write-scope"),
                request_hash=request_hash, target_hash=request_hash,
            )
            dry_run = dry_run_journal_create_execution(grant=grant, journal=journal, conn=conn, office_id=OFFICE_ID)
    approval = dict(
        operation_type="journal_create", approval_hash=approval_hash,
        scope_hash=dry_run.scope_hash, request_hash=request_hash, target_hash=request_hash,
        dry_run_hash=dry_run.dry_run_hash,
        expected_api_action="POST /api/v3/journals then GET /api/v3/journals/{id}",
        rollback_method=ROLLBACK,
    )
    return AccountingScenario(profile, provider, db, client, journal, grant, dry_run, approval)
