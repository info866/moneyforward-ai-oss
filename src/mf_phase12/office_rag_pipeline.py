"""Build company memory from that company's verified official read snapshot."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .accounting_api import MoneyForwardAccountingClient
from .classification_eval import evaluate_journal_pattern_classification
from .classification_improvements import materialize_stable_learned_rules
from .db import Phase12Database
from .financial_event_classification import upsert_company_fixed_accounting_rules
from .office_profile import (
    bind_database_office_identity, resolve_office_profile,
    require_database_office_identity, resolve_moneyforward_db_path,
    resolve_moneyforward_keychain_service,
)
from .sync import sync_accounting_full_read_snapshot


@dataclass(frozen=True)
class OfficeRagBuildSummary:
    db_path: str
    sync_executed: bool
    sync_counts: dict
    fixed_rule_candidates_upserted: int
    rag_cards_upserted: int
    vendor_profiles_upserted: int
    overall_accuracy: float
    classifiable_overall_accuracy: float
    improvement_candidate_count: int
    promotion_allowed: bool
    raw_data_stored: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def build_office_rag(*, db_path: Path | str, client: MoneyForwardAccountingClient | None = None,
                     start_date: str | None = None, end_date: str | None = None,
                     office_id: str | None = None, skip_sync: bool = False,
                     reset_existing_rag: bool = False) -> OfficeRagBuildSummary:
    if reset_existing_rag:
        raise PermissionError("Company memory is preserved; reset is not a starter operation")
    profile = resolve_office_profile(required=True)
    path = resolve_moneyforward_db_path(profile, db_path, external_io=True)
    binding = None
    if client is not None:
        if client.office_profile != profile:
            raise PermissionError("RAG client and active profile differ")
        if not client.keychain_service:
            raise PermissionError("RAG client credential source must be explicit")
        resolve_moneyforward_keychain_service(profile, client.keychain_service, external_io=True)
        resolve_moneyforward_db_path(profile, client.db_path, external_io=True)
        binding = client.validate_office_identity(office_id=office_id)
    if not skip_sync and binding is None:
        raise PermissionError("A verified company client is required for RAG synchronization")
    db = Phase12Database(path)
    if not path.exists():
        if binding is None:
            raise PermissionError("A new company DB requires official identity discovery")
        db.initialize()
        with db.connect() as conn:
            bind_database_office_identity(conn, profile, binding)
    with db.connect() as conn:
        require_database_office_identity(conn, profile, binding)
        sync_counts = {}
        if not skip_sync:
            sync_counts = sync_accounting_full_read_snapshot(conn, client, start_date=start_date,
                end_date=end_date, office_id=binding.office_id)
        learned = materialize_stable_learned_rules(conn)
        upsert_company_fixed_accounting_rules(conn)
        evaluation = evaluate_journal_pattern_classification(conn, prediction_strategy="learned_rules_first")
    return OfficeRagBuildSummary(
        db_path=str(path), sync_executed=not skip_sync, sync_counts=sync_counts,
        fixed_rule_candidates_upserted=learned.fixed_rule_candidates_upserted,
        rag_cards_upserted=learned.rag_cards_upserted, vendor_profiles_upserted=learned.vendor_profiles_upserted,
        overall_accuracy=evaluation.overall_accuracy,
        classifiable_overall_accuracy=evaluation.classifiable_overall_accuracy,
        improvement_candidate_count=evaluation.improvement_candidate_count,
        promotion_allowed=evaluation.promotion_allowed)
