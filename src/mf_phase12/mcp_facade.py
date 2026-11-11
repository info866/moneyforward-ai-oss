from __future__ import annotations

import sqlite3

from .execution_policy import build_execution_policy_contract, select_execution_channel
from .rag import search_rag
from .shiwake_workflow_api import (
    build_learning_status,
    build_shiwake_autorun_startup_check,
    build_shiwake_workflow_contract,
)
from .supplier_primary_watchlist import supplier_primary_watchlist_status
from .shiwake_run import build_run_status, build_stage_contract


class LocalMCPFacade:
    """MCP-shaped local facade used to test tool boundaries before a real MCP server."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def search_journal_patterns(self, query: str) -> list[dict]:
        return search_rag(self.conn, query)

    def get_rag_status(self) -> dict:
        row = self.conn.execute(
            "SELECT synced_at, status, item_count FROM sync_manifests ORDER BY synced_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else {"status": "missing"}

    def get_shiwake_workflow_contract(self) -> dict:
        return build_shiwake_workflow_contract()

    def get_shiwake_autorun_startup_check(self) -> dict:
        return build_shiwake_autorun_startup_check(self.conn)

    def get_shiwake_execution_policy(self, operation: str | None = None) -> dict:
        if operation:
            return select_execution_channel(operation).to_dict()
        return build_execution_policy_contract()

    def get_shiwake_run_status(self, run_id: str) -> dict:
        return build_run_status(self.conn, run_id)

    def get_shiwake_stage_contract(self) -> dict:
        return build_stage_contract()

    def get_learning_status(self) -> dict:
        return build_learning_status(self.conn)

    def get_supplier_primary_watchlist_status(self) -> dict:
        return supplier_primary_watchlist_status(self.conn)

    def search_payroll_patterns(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT card_type, title, body
            FROM rag_cards
            WHERE body LIKE '%source=payroll_verified_journal_learning%'
            ORDER BY updated_at DESC, id DESC
            LIMIT 10
            """
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]
        matches = search_rag(self.conn, "給与", limit=10)
        if not matches:
            matches = search_rag(self.conn, "payroll", limit=10)
        return matches

    def execute_approved_journal_create(self, *_args, **_kwargs) -> None:
        raise PermissionError("execute is intentionally unavailable in local MCP dry-run facade")
