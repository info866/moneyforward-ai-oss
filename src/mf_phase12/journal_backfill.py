from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .hash_utils import mask_identifier, stable_hash
from .journal_write_verify import _read_back_gross_value
from .local_artifacts import require_ignored_untracked_data_path
from .sync import _amount, _date_key, _journal_to_pattern_records, _paired_journal_lines


_FULL_JOURNAL_BRANCH_FIELDS = (
    "debit_account",
    "debit_sub_account",
    "debit_tax_category",
    "debit_value",
    "debit_tax_value",
    "debit_gross_value",
    "credit_account",
    "credit_sub_account",
    "credit_tax_category",
    "credit_value",
    "credit_tax_value",
    "credit_gross_value",
)


class JournalReadClient(Protocol):
    def journals(
        self,
        office_id: str | None = None,
        *,
        start_date: str,
        end_date: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict: ...

    def journal(self, journal_id: str, office_id: str | None = None) -> dict: ...


@dataclass(frozen=True)
class JournalBackfillExpected:
    journal_date: str
    amount: int
    debit_account: str
    credit_account: str
    source_file_name: str
    source_file_id_hash: str
    source_file_hash: str
    debit_sub_account: str | None = None
    credit_sub_account: str | None = None
    tax_category: str | None = None
    description_contains: str | None = None
    voucher_file_ids_present: bool = False
    journal_no: str | None = None
    full_journal_branches: tuple[dict[str, Any], ...] = ()
    journal_total: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "JournalBackfillExpected":
        required = [
            "journal_date",
            "amount",
            "debit_account",
            "credit_account",
            "source_file_name",
            "source_file_id_hash",
            "source_file_hash",
        ]
        missing = [key for key in required if payload.get(key) in (None, "")]
        if missing:
            raise ValueError(f"missing required expected fields: {', '.join(missing)}")
        source_file_id_hash = str(payload["source_file_id_hash"])
        if not _is_sha256_marker(source_file_id_hash):
            raise ValueError("source_file_id_hash must be a sha256:<64 hex> value, not a raw Drive ID")
        source_file_hash = str(payload["source_file_hash"])
        if not _is_sha256_marker(source_file_hash):
            raise ValueError("source_file_hash must be a sha256:<64 hex> value")
        full_journal_branches = _parse_full_journal_branches(payload.get("full_journal_branches"))
        journal_total = payload.get("journal_total")
        if full_journal_branches and journal_total in (None, ""):
            raise ValueError("journal_total is required with full_journal_branches")
        return cls(
            journal_date=str(payload["journal_date"])[:10],
            amount=int(payload["amount"]),
            debit_account=str(payload["debit_account"]),
            credit_account=str(payload["credit_account"]),
            source_file_name=str(payload["source_file_name"]),
            source_file_id_hash=source_file_id_hash,
            source_file_hash=source_file_hash,
            debit_sub_account=_optional_str(payload.get("debit_sub_account")),
            credit_sub_account=_optional_str(payload.get("credit_sub_account")),
            tax_category=_optional_str(payload.get("tax_category")),
            description_contains=_optional_str(payload.get("description_contains")),
            voucher_file_ids_present=bool(payload.get("voucher_file_ids_present", False)),
            journal_no=_optional_str(payload.get("journal_no")),
            full_journal_branches=full_journal_branches,
            journal_total=int(journal_total) if journal_total not in (None, "") else None,
        )

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "journal_date": self.journal_date,
            "amount_hash": stable_hash(self.amount),
            "debit_account": self.debit_account,
            "debit_sub_account": self.debit_sub_account,
            "credit_account": self.credit_account,
            "credit_sub_account": self.credit_sub_account,
            "tax_category": self.tax_category,
            "description_contains_hash": stable_hash(self.description_contains) if self.description_contains else None,
            "voucher_file_ids_present": self.voucher_file_ids_present,
            "journal_no_hash": stable_hash(self.journal_no) if self.journal_no else None,
            "source_file_name": self.source_file_name,
            "source_file_id_hash": self.source_file_id_hash,
            "source_file_hash": self.source_file_hash,
            "full_journal_branch_count": len(self.full_journal_branches),
            "full_journal_branches_hash": (
                stable_hash([_redact_expected_branch(branch) for branch in self.full_journal_branches])
                if self.full_journal_branches
                else None
            ),
            "journal_total_hash": stable_hash(self.journal_total) if self.journal_total is not None else None,
        }


@dataclass(frozen=True)
class JournalBackfillResult:
    status: str
    idempotency_key: str
    journal_id_hash: str
    matched_line_hash: str
    voucher_file_ids_hash: str
    raw_journal_id_stored: bool = False
    raw_voucher_file_ids_stored: bool = False
    moneyforward_write_performed: bool = False
    journal_id_output_written: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "journal_id_hash": self.journal_id_hash,
            "matched_line_hash": self.matched_line_hash,
            "voucher_file_ids_hash": self.voucher_file_ids_hash,
            "raw_journal_id_stored": self.raw_journal_id_stored,
            "raw_voucher_file_ids_stored": self.raw_voucher_file_ids_stored,
            "moneyforward_write_performed": self.moneyforward_write_performed,
            "journal_id_output_written": self.journal_id_output_written,
        }


def backfill_journal_create_from_readback(
    conn: sqlite3.Connection,
    client: JournalReadClient,
    expected: JournalBackfillExpected,
    *,
    office_id: str | None = None,
    journal_id_output_json: Path | None = None,
) -> JournalBackfillResult:
    matched = capture_unique_matching_journal(client, expected, office_id=office_id)
    return persist_captured_journal_backfill(
        conn,
        matched,
        expected,
        journal_id_output_json=journal_id_output_json,
    )


def capture_unique_matching_journal(
    client: JournalReadClient,
    expected: JournalBackfillExpected,
    *,
    office_id: str | None = None,
) -> dict[str, Any]:
    """Perform the remote GET phase without holding an SQLite connection."""

    return _find_unique_matching_journal(client, expected, office_id=office_id)


def persist_captured_journal_backfill(
    conn: sqlite3.Connection,
    matched: dict[str, Any],
    expected: JournalBackfillExpected,
    *,
    journal_id_output_json: Path | None = None,
) -> JournalBackfillResult:
    """Persist one already-captured authoritative journal in a short transaction."""

    journal_id_output_path = (
        require_ignored_untracked_data_path(
            journal_id_output_json,
            purpose="existing journal id handoff output",
        )
        if journal_id_output_json is not None
        else None
    )
    journal_id = _journal_id(matched)
    if not journal_id:
        raise RuntimeError("matched journal does not contain an id")
    if journal_id_output_path is not None:
        _require_compatible_journal_id_output(journal_id_output_path, journal_id)
    journal_id_hash = mask_identifier(journal_id)
    normalized = _matching_line(matched, expected)
    full_journal = _verified_full_journal(matched, expected)
    voucher_file_ids = _voucher_file_ids(matched)
    if expected.voucher_file_ids_present and not voucher_file_ids:
        raise RuntimeError("matched journal does not contain voucher_file_ids")

    expected_redacted = expected.to_redacted_dict()
    actual_redacted = _redacted_actual(
        normalized,
        matched,
        expected,
        voucher_file_ids,
        full_journal=full_journal,
    )
    idempotency_key = stable_hash(
        {
            "purpose": "journal_create_readback_backfill",
            "journal_id_hash": journal_id_hash,
            "expected_hash": stable_hash(expected_redacted),
        }
    )
    candidate_hash = stable_hash(expected_redacted)
    dry_run_result_hash = stable_hash(actual_redacted)
    voucher_hash = stable_hash(voucher_file_ids)
    conn.execute(
        """
        INSERT INTO processing_ledger (
            google_drive_file_id_hash, file_name, file_hash, candidate_hash,
            idempotency_key, processing_status, moneyforward_journal_id_hash,
            moneyforward_voucher_id_hash, dry_run_result_hash
        ) VALUES (?, ?, ?, ?, ?, 'verified', ?, ?, ?)
        ON CONFLICT(google_drive_file_id_hash, file_hash) DO UPDATE SET
            processing_status = 'verified',
            moneyforward_journal_id_hash = excluded.moneyforward_journal_id_hash,
            moneyforward_voucher_id_hash = excluded.moneyforward_voucher_id_hash,
            dry_run_result_hash = excluded.dry_run_result_hash,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (
            expected.source_file_id_hash,
            expected.source_file_name,
            expected.source_file_hash,
            candidate_hash,
            idempotency_key,
            journal_id_hash,
            voucher_hash if voucher_file_ids else None,
            dry_run_result_hash,
        ),
    )
    ledger = conn.execute(
        "SELECT id, idempotency_key FROM processing_ledger WHERE google_drive_file_id_hash = ? AND file_hash = ?",
        (expected.source_file_id_hash, expected.source_file_hash),
    ).fetchone()
    final_idempotency_key = str(ledger["idempotency_key"])
    conn.execute(
        """
        INSERT INTO write_intents (
            idempotency_key, ledger_id, operation_type, source_file_id_hash,
            candidate_hash, dry_run_result_hash, plan_hash, request_hash,
            status, moneyforward_journal_id_hash
        ) VALUES (?, ?, 'journal_create', ?, ?, ?, ?, ?, 'executed', ?)
        ON CONFLICT(idempotency_key, operation_type) DO UPDATE SET
            status = 'executed',
            moneyforward_journal_id_hash = excluded.moneyforward_journal_id_hash,
            executed_at = COALESCE(write_intents.executed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (
            final_idempotency_key,
            int(ledger["id"]),
            expected.source_file_id_hash,
            candidate_hash,
            dry_run_result_hash,
            stable_hash({"backfill": "journal_create", "expected": expected_redacted}),
            stable_hash({"method": "GET", "journal_id_hash": journal_id_hash}),
            journal_id_hash,
        ),
    )
    conn.execute(
        """
        INSERT INTO post_registration_verifications (
            idempotency_key, moneyforward_journal_id_hash, expected_hash,
            actual_hash, voucher_file_ids_hash, passed, diff_summary_redacted
        ) VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (
            final_idempotency_key,
            journal_id_hash,
            stable_hash(expected_redacted),
            stable_hash(actual_redacted),
            voucher_hash,
            (
                "read-only backfill matched every Money Forward journal branch and balanced total"
                if expected.full_journal_branches
                else "read-only backfill matched Money Forward journal"
            ),
        ),
    )
    _insert_backfill_audit(conn, final_idempotency_key, expected_redacted, actual_redacted)
    conn.commit()
    if journal_id_output_path is not None:
        _write_journal_id_output(journal_id_output_path, journal_id)
    return JournalBackfillResult(
        status="verified",
        idempotency_key=final_idempotency_key,
        journal_id_hash=journal_id_hash,
        matched_line_hash=stable_hash(normalized),
        voucher_file_ids_hash=voucher_hash,
        journal_id_output_written=journal_id_output_json is not None,
    )


def _require_compatible_journal_id_output(path: Path, journal_id: str) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing journal id handoff is not valid JSON") from exc
    existing_journal_id = payload.get("journal_id") if isinstance(payload, dict) else None
    if not isinstance(existing_journal_id, str) or not existing_journal_id:
        raise ValueError("existing journal id handoff is missing journal_id")
    if stable_hash(existing_journal_id) != stable_hash(journal_id):
        raise ValueError("existing journal id handoff targets a different journal")


def _write_journal_id_output(output_path: Path, journal_id: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.write_text(
        json.dumps({"journal_id": journal_id}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)


def _find_unique_matching_journal(
    client: JournalReadClient,
    expected: JournalBackfillExpected,
    *,
    office_id: str | None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for journal in _iter_journals_for_date(client, expected.journal_date, office_id=office_id):
        journal_id = _journal_id(journal)
        detail = _unwrap_journal(client.journal(journal_id, office_id)) if journal_id else _unwrap_journal(journal)
        if _journal_matches(detail, expected):
            candidates.append(detail)
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one matching journal, found {len(candidates)}")
    return candidates[0]


def _iter_journals_for_date(
    client: JournalReadClient,
    journal_date: str,
    *,
    office_id: str | None,
    per_page: int = 100,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = client.journals(
            office_id,
            start_date=journal_date,
            end_date=journal_date,
            page=page,
            per_page=per_page,
        )
        page_items = _extract_items(payload)
        items.extend(page_items)
        if len(page_items) < per_page:
            return items
    raise RuntimeError("journal read-back pagination exceeded max_pages")


def _journal_matches(journal: dict[str, Any], expected: JournalBackfillExpected) -> bool:
    if _date_key(journal) != expected.journal_date:
        return False
    if expected.journal_no and _journal_no(journal) != expected.journal_no:
        return False
    if expected.description_contains and expected.description_contains not in _journal_text(journal):
        return False
    try:
        _matching_line(journal, expected)
        _verified_full_journal(journal, expected)
    except RuntimeError:
        return False
    if expected.voucher_file_ids_present and not _voucher_file_ids(journal):
        return False
    return True


def _matching_line(journal: dict[str, Any], expected: JournalBackfillExpected) -> dict[str, Any]:
    matches = [
        line
        for line in _normalized_lines(journal)
        if _line_matches(line, expected)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one matching journal line, found {len(matches)}")
    return matches[0]


def _normalized_lines(journal: dict[str, Any]) -> list[dict[str, Any]]:
    records = _journal_to_pattern_records(journal)
    paired = _paired_journal_lines(journal)
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        debit, _credit = paired[index] if index < len(paired) else ({}, {})
        normalized.append(
            {
                "journal_date": _date_key(journal),
                "amount": _read_back_gross_value(debit, _amount(debit) or _amount(journal)),
                "debit_account": record["debit_account"],
                "debit_sub_account": record["debit_sub_account"],
                "credit_account": record["credit_account"],
                "credit_sub_account": record["credit_sub_account"],
                "tax_category": record["tax_category"],
            }
        )
    return normalized


def _line_matches(line: dict[str, Any], expected: JournalBackfillExpected) -> bool:
    checks = [
        line.get("journal_date") == expected.journal_date,
        int(line.get("amount") or 0) == expected.amount,
        line.get("debit_account") == expected.debit_account,
        line.get("credit_account") == expected.credit_account,
    ]
    if expected.debit_sub_account is not None:
        checks.append(line.get("debit_sub_account") == expected.debit_sub_account)
    if expected.credit_sub_account is not None:
        checks.append(line.get("credit_sub_account") == expected.credit_sub_account)
    if expected.tax_category is not None:
        checks.append(line.get("tax_category") == expected.tax_category)
    return all(checks)


def _redacted_actual(
    line: dict[str, Any],
    journal: dict[str, Any],
    expected: JournalBackfillExpected,
    voucher_file_ids: list[str],
    *,
    full_journal: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        **line,
        "amount_hash": stable_hash(line.get("amount")),
        "amount": "<redacted>",
        "journal_id_hash": mask_identifier(_journal_id(journal)),
        "journal_no_hash": stable_hash(_journal_no(journal)) if _journal_no(journal) else None,
        "description_contains_hash": stable_hash(expected.description_contains) if expected.description_contains else None,
        "voucher_file_ids_hash": stable_hash(voucher_file_ids),
        "voucher_file_ids_present": bool(voucher_file_ids),
        "raw_voucher_file_ids_stored": False,
    }
    if full_journal is not None:
        payload.update(
            {
                "full_journal_verified": True,
                "full_journal_branch_count": full_journal["branch_count"],
                "full_journal_debit_total_hash": stable_hash(full_journal["debit_total"]),
                "full_journal_credit_total_hash": stable_hash(full_journal["credit_total"]),
                "full_journal_branches_hash": stable_hash(full_journal["branches_redacted"]),
                "raw_branch_amounts_stored": False,
                "raw_branch_remarks_stored": False,
            }
        )
    return payload


def _verified_full_journal(
    journal: dict[str, Any],
    expected: JournalBackfillExpected,
) -> dict[str, Any] | None:
    if not expected.full_journal_branches:
        return None
    raw_branches = journal.get("branches")
    if not isinstance(raw_branches, list):
        raise RuntimeError("matched journal does not contain branches")
    actual = [_normalize_full_journal_branch(branch) for branch in raw_branches]
    if len(actual) != len(expected.full_journal_branches):
        raise RuntimeError(
            f"expected {len(expected.full_journal_branches)} full journal branches, found {len(actual)}"
        )
    for index, (actual_branch, expected_branch) in enumerate(
        zip(actual, expected.full_journal_branches, strict=True)
    ):
        if not _full_journal_branch_matches(actual_branch, expected_branch):
            raise RuntimeError(f"full journal branch {index} did not match")
    debit_total = sum(int(branch["debit_gross_value"] or 0) for branch in actual)
    credit_total = sum(int(branch["credit_gross_value"] or 0) for branch in actual)
    if debit_total != expected.journal_total or credit_total != expected.journal_total:
        raise RuntimeError("full journal debit/credit total did not match")
    return {
        "branch_count": len(actual),
        "debit_total": debit_total,
        "credit_total": credit_total,
        "branches_redacted": [_redact_actual_branch(branch) for branch in actual],
    }


def _normalize_full_journal_branch(branch: Any) -> dict[str, Any]:
    if not isinstance(branch, dict):
        raise RuntimeError("journal branch must be an object")
    debit = branch.get("debitor") if isinstance(branch.get("debitor"), dict) else {}
    credit = branch.get("creditor") if isinstance(branch.get("creditor"), dict) else {}
    debit_value = int(debit.get("value") or 0)
    debit_tax_value = int(debit.get("tax_value") or 0)
    credit_value = int(credit.get("value") or 0)
    credit_tax_value = int(credit.get("tax_value") or 0)
    return {
        "debit_account": _optional_str(debit.get("account_name")),
        "debit_sub_account": _optional_str(debit.get("sub_account_name")),
        "debit_tax_category": _optional_str(debit.get("tax_name")),
        "debit_value": debit_value,
        "debit_tax_value": debit_tax_value,
        "debit_gross_value": int(_read_back_gross_value(debit, debit_value) or 0),
        "credit_account": _optional_str(credit.get("account_name")),
        "credit_sub_account": _optional_str(credit.get("sub_account_name")),
        "credit_tax_category": _optional_str(credit.get("tax_name")),
        "credit_value": credit_value,
        "credit_tax_value": credit_tax_value,
        "credit_gross_value": int(_read_back_gross_value(credit, credit_value) or 0),
        "remark": str(branch.get("remark") or ""),
    }


def _full_journal_branch_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key in _FULL_JOURNAL_BRANCH_FIELDS:
        if actual.get(key) != expected.get(key):
            return False
    remark_contains = str(expected.get("remark_contains") or "")
    return not remark_contains or remark_contains in str(actual.get("remark") or "")


def _parse_full_journal_branches(value: Any) -> tuple[dict[str, Any], ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not value:
        raise ValueError("full_journal_branches must be a non-empty list")
    parsed: list[dict[str, Any]] = []
    for index, branch in enumerate(value):
        if not isinstance(branch, dict):
            raise ValueError(f"full_journal_branches[{index}] must be an object")
        missing = [key for key in _FULL_JOURNAL_BRANCH_FIELDS if key not in branch]
        if missing:
            raise ValueError(
                f"full_journal_branches[{index}] missing fields: {', '.join(missing)}"
            )
        parsed.append(
            {
                **{key: branch.get(key) for key in _FULL_JOURNAL_BRANCH_FIELDS},
                "remark_contains": str(branch.get("remark_contains") or ""),
            }
        )
    return tuple(parsed)


def _redact_expected_branch(branch: dict[str, Any]) -> dict[str, Any]:
    return {
        key: stable_hash(branch.get(key)) if key.endswith("_value") else branch.get(key)
        for key in _FULL_JOURNAL_BRANCH_FIELDS
    } | {
        "remark_contains_hash": (
            stable_hash(branch.get("remark_contains")) if branch.get("remark_contains") else None
        )
    }


def _redact_actual_branch(branch: dict[str, Any]) -> dict[str, Any]:
    return {
        key: stable_hash(branch.get(key)) if key.endswith("_value") else branch.get(key)
        for key in _FULL_JOURNAL_BRANCH_FIELDS
    } | {"remark_hash": stable_hash(branch.get("remark") or "")}


def _insert_backfill_audit(
    conn: sqlite3.Connection,
    idempotency_key: str,
    expected_redacted: dict[str, Any],
    actual_redacted: dict[str, Any],
) -> None:
    request_hash = stable_hash(expected_redacted)
    response_hash = stable_hash(actual_redacted)
    event_hash = stable_hash(
        {
            "event_type": "journal_create_readback_backfill",
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "response_hash": response_hash,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_logs (
            event_hash, event_type, idempotency_key, request_hash,
            response_hash, actor, dry_run, rollback_plan
        ) VALUES (?, 'journal_create_readback_backfill', ?, ?, ?, 'codex', 1, ?)
        """,
        (
            event_hash,
            idempotency_key,
            request_hash,
            response_hash,
            "Read-only backfill from Money Forward GET. No external write was performed.",
        ),
    )


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    payload = _unwrap_journal(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "journals"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _unwrap_journal(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("journal", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
    return payload if isinstance(payload, dict) else {}


def _journal_id(journal: dict[str, Any]) -> str:
    for key in ("id", "journal_id"):
        value = journal.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _journal_no(journal: dict[str, Any]) -> str | None:
    for key in ("no", "number", "journal_no", "journal_number"):
        value = journal.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _voucher_file_ids(journal: dict[str, Any]) -> list[str]:
    values = journal.get("voucher_file_ids")
    if isinstance(values, list):
        return [str(value) for value in values if value not in (None, "")]
    return []


def _journal_text(journal: dict[str, Any]) -> str:
    return " ".join(_walk_text(journal))


def _walk_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for child in value.values():
            texts.extend(_walk_text(child))
        return texts
    if isinstance(value, list):
        texts: list[str] = []
        for child in value:
            texts.extend(_walk_text(child))
        return texts
    return []


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _is_sha256_marker(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest.lower())
