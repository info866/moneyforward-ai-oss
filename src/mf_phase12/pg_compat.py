from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any


DEFAULT_POSTGRES_SCHEMA = "app_moneyforward_phase12"

_NOW_SQL = "to_char(clock_timestamp() at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"

_UPSERT_TARGETS: dict[str, tuple[str, ...]] = {
    "master_items": ("source_name", "item_type", "external_id_hash"),
    "rag_cards": ("card_hash",),
    "classification_evaluation_runs": ("run_hash",),
    "drive_move_execution_records": ("approval_hash",),
    "drive_move_readback_verifications": ("verification_hash",),
    "drive_move_chain_items": ("approval_hash", "idempotency_key"),
}

_NO_ID_TABLES = {"schema_migrations"}


class PostgresCompatRow(dict):
    def __init__(self, columns: Iterable[str], values: Iterable[Any]):
        self._columns = tuple(columns)
        self._values = tuple(values)
        super().__init__(zip(self._columns, self._values, strict=False))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)


@dataclass
class PostgresCompatCursor:
    rows: list[PostgresCompatRow]
    rowcount: int = -1
    lastrowid: int | None = None

    def fetchone(self) -> PostgresCompatRow | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[PostgresCompatRow]:
        rows = self.rows
        self.rows = []
        return rows

    def __iter__(self) -> Iterator[PostgresCompatRow]:
        return iter(self.fetchall())


class PostgresCompatConnection:
    def __init__(self, database_url: str, *, schema: str = DEFAULT_POSTGRES_SCHEMA):
        try:
            import psycopg
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "PostgreSQL support requires psycopg. Install with: python3 -m pip install 'psycopg[binary]>=3.1'"
            ) from error

        self._psycopg = psycopg
        self.database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.schema = schema
        self._conn = psycopg.connect(self.database_url)
        with self._conn.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("SET search_path TO {}, public").format(psycopg.sql.Identifier(self.schema))
            )

    def __enter__(self) -> PostgresCompatConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def executescript(self, sql: str) -> PostgresCompatCursor:
        if "CREATE TABLE" in sql.upper() or "CREATE VIRTUAL TABLE" in sql.upper():
            return PostgresCompatCursor([])
        for statement in (part.strip() for part in sql.split(";")):
            if statement:
                self.execute(statement)
        return PostgresCompatCursor([])

    def executemany(self, sql: str, seq_of_params: Iterable[tuple[Any, ...]]) -> PostgresCompatCursor:
        last_cursor = PostgresCompatCursor([])
        for params in seq_of_params:
            last_cursor = self.execute(sql, params)
        return last_cursor

    def execute(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> PostgresCompatCursor:
        stripped = sql.strip()
        if not stripped:
            return PostgresCompatCursor([])
        handled = self._handle_sqlite_metadata(stripped, params)
        if handled is not None:
            return handled
        if _is_noop_sqlite_ddl(stripped):
            return PostgresCompatCursor([])
        if re.search(r"\bMATCH\s+\?", stripped, flags=re.IGNORECASE):
            raise sqlite3.OperationalError("SQLite FTS MATCH is not available on PostgreSQL")

        translated = _translate_sql(stripped)
        translated = _translate_insert_or_ignore(translated)
        translated = _translate_insert_or_replace(translated)
        translated, wants_lastrowid = _append_returning_id(translated)
        translated = _replace_qmark_placeholders(translated)
        translated = _escape_psycopg_literal_percent(translated)

        try:
            with self._conn.cursor() as cursor:
                cursor.execute(translated, tuple(params) if not isinstance(params, Mapping) else params)
                rows: list[PostgresCompatRow] = []
                lastrowid = None
                if cursor.description is not None:
                    columns = [column.name for column in cursor.description]
                    rows = [PostgresCompatRow(columns, values) for values in cursor.fetchall()]
                    if wants_lastrowid and rows and "id" in rows[0]:
                        lastrowid = int(rows[0]["id"])
                return PostgresCompatCursor(rows=rows, rowcount=cursor.rowcount, lastrowid=lastrowid)
        except self._psycopg.Error as error:
            self._conn.rollback()
            raise sqlite3.OperationalError(str(error)) from error

    def _handle_sqlite_metadata(
        self,
        sql: str,
        params: Iterable[Any] | Mapping[str, Any],
    ) -> PostgresCompatCursor | None:
        upper = " ".join(sql.upper().split())
        if upper == "PRAGMA FOREIGN_KEYS = ON":
            return PostgresCompatCursor([])
        if upper == "PRAGMA DATABASE_LIST":
            return PostgresCompatCursor([PostgresCompatRow(("seq", "name", "file"), (0, "main", ""))])

        pragma_match = re.match(r"PRAGMA\s+table_info\((?P<table>[A-Za-z_][A-Za-z0-9_]*)\)", sql, flags=re.IGNORECASE)
        if pragma_match:
            table_name = pragma_match.group("table")
            return self.execute(
                """
                SELECT ordinal_position - 1 AS cid,
                       column_name AS name,
                       data_type AS type,
                       CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS "notnull",
                       column_default AS dflt_value,
                       0 AS pk
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
                ORDER BY ordinal_position
                """,
                (table_name,),
            )

        if "FROM SQLITE_MASTER" not in upper:
            return None

        if "SELECT SQL" in upper:
            return PostgresCompatCursor(
                [PostgresCompatRow(("sql",), ("source_event_context_key converted_to_rag_card_hash",))]
            )

        table_type = "BASE TABLE" if "TYPE = 'TABLE'" in upper else None
        rows = self.execute(
            """
            SELECT table_name AS name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_type = COALESCE(?, table_type)
              AND table_name NOT LIKE 'sqlite_%'
            ORDER BY table_name
            """,
            (table_type,),
        ).fetchall()

        if "NAME = ?" in upper:
            wanted = tuple(params)[0] if not isinstance(params, Mapping) else params.get("name")
            rows = [row for row in rows if row["name"] == wanted]
        return PostgresCompatCursor(rows)


def _is_noop_sqlite_ddl(sql: str) -> bool:
    upper = sql.upper()
    if upper.startswith("CREATE TABLE IF NOT EXISTS ACCOUNTING_DECISION_CACHE"):
        return False
    return (
        upper.startswith("CREATE TABLE IF NOT EXISTS")
        or upper.startswith("CREATE VIRTUAL TABLE IF NOT EXISTS")
        or upper.startswith("CREATE TRIGGER IF NOT EXISTS")
    )


def _translate_sql(sql: str) -> str:
    translated = sql
    translated = translated.replace("strftime('%Y-%m-%dT%H:%M:%fZ', 'now')", _NOW_SQL)
    translated = re.sub(r"\bexcluded\.", "EXCLUDED.", translated, flags=re.IGNORECASE)
    translated = re.sub(r"MAX\(([^,\n]+),\s*([^)]+)\)", r"GREATEST(\1, \2)", translated)
    return translated


def _translate_insert_or_ignore(sql: str) -> str:
    if not re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE):
        return sql
    translated = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.IGNORECASE)
    if "ON CONFLICT" not in translated.upper():
        translated = f"{translated.rstrip()} ON CONFLICT DO NOTHING"
    return translated


def _translate_insert_or_replace(sql: str) -> str:
    match = re.match(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<columns>.*?)\)\s*VALUES\s*(?P<values>.+)\Z",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql
    table = match.group("table")
    columns = tuple(_clean_identifier(column) for column in match.group("columns").split(","))
    conflict_columns = _UPSERT_TARGETS.get(table)
    translated = f"INSERT INTO {table} ({match.group('columns')}) VALUES {match.group('values')}"
    if not conflict_columns:
        return f"{translated} ON CONFLICT DO NOTHING"
    update_columns = [column for column in columns if column not in conflict_columns and column]
    if not update_columns:
        return f"{translated} ON CONFLICT ({', '.join(conflict_columns)}) DO NOTHING"
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    return f"{translated} ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET {assignments}"


def _append_returning_id(sql: str) -> tuple[str, bool]:
    upper = sql.upper()
    if not upper.startswith("INSERT INTO") or " RETURNING " in upper:
        return sql, False
    table_match = re.match(r"INSERT\s+INTO\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)", sql, flags=re.IGNORECASE)
    if table_match and (table_match.group("table").endswith("_fts") or table_match.group("table") in _NO_ID_TABLES):
        return sql, False
    return f"{sql} RETURNING id", True


def _replace_qmark_placeholders(sql: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double:
            in_single = not in_single
            result.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
        elif char == "?" and not in_single and not in_double:
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _escape_psycopg_literal_percent(sql: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "%":
            next_char = sql[index + 1] if index + 1 < len(sql) else ""
            if next_char in {"s", "b", "t", "%"}:
                result.append(char)
            else:
                result.append("%%")
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _clean_identifier(value: str) -> str:
    return value.strip().strip('"').split()[-1]
