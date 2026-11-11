from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .daily_accounting_control import require_daily_completion_notification_allowed
from .hash_utils import accounting_run_hash, mask_identifier, stable_hash
from .keychain import KeychainClient
from .local_artifacts import require_ignored_untracked_data_path
from .same_chain_status import same_chain_status_for_file_hash
from .office_profile import resolve_office_profile


DEFAULT_WEBHOOK_ENV = "GOOGLE_CHAT_WEBHOOK_URL"
DEFAULT_WEBHOOK_KEYCHAIN_ACCOUNT = "google_chat_webhook_url"
DELIVERY_BLOCKING_STATUSES = {"dispatching", "sent", "uncertain_failure"}
CLI_SUCCESS_STATUSES = frozenset({"sent", "historical_sent"})
PROCESSED_BATCH_SCOPE = "processed_batch"
AGGREGATE_COMPLETION_SCOPE = "aggregate_completion"


class GoogleChatTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]: ...


class UrllibGoogleChatTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()


@dataclass(frozen=True)
class GoogleChatNotificationPlan:
    status: str
    message_text: str
    message_hash: str
    item_count: int
    blocked_reasons: tuple[str, ...]
    webhook_url_hash: str | None = None
    google_chat_api_called: bool = False
    response_hash: str | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "item_count": self.item_count,
            "message_hash": self.message_hash,
            "message_preview": self.message_text,
            "blocked_reasons": list(self.blocked_reasons),
            "webhook_url_hash": self.webhook_url_hash,
            "webhook_url_stored": False,
            "raw_secret_stored": False,
            "google_chat_api_called": self.google_chat_api_called,
            "response_hash": self.response_hash,
        }


def build_completion_notification_plan(payload: dict[str, Any]) -> GoogleChatNotificationPlan:
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    notification_type = str(payload.get("notification_type") or payload.get("type") or "completion")
    if notification_type in {"pending", "waiting"}:
        failures = _pending_failures(payload, items)
        message = _build_pending_message(payload, items)
    else:
        scope = _notification_scope(payload)
        failures = (
            _aggregate_completion_failures(payload)
            if scope == AGGREGATE_COMPLETION_SCOPE
            else _completion_failures(payload)
        )
        if scope != AGGREGATE_COMPLETION_SCOPE:
            failures.extend(_no_journal_summary_failures(payload))
        if scope == PROCESSED_BATCH_SCOPE:
            failures.extend(_processed_batch_failures(payload, items))
        message = (
            _build_aggregate_completion_message(payload)
            if scope == AGGREGATE_COMPLETION_SCOPE
            else _build_completion_message(payload, items)
        )
    return GoogleChatNotificationPlan(
        status="ready" if not failures else "blocked",
        message_text=message,
        message_hash=stable_hash({"text": message}),
        item_count=len(items),
        blocked_reasons=tuple(failures),
    )


def load_completion_notification_plan(summary_json: Path) -> GoogleChatNotificationPlan:
    return build_completion_notification_plan(load_completion_notification_payload(summary_json))


def load_completion_notification_payload(summary_json: Path) -> dict[str, Any]:
    summary_json = require_ignored_untracked_data_path(summary_json, purpose="Google Chat notification summary")
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("notification summary must be a JSON object")
    return payload


def resolve_webhook_url(
    *,
    env_var: str = DEFAULT_WEBHOOK_ENV,
    keychain_account: str = DEFAULT_WEBHOOK_KEYCHAIN_ACCOUNT,
    keychain_service: str | None = None,
) -> str:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("Google Chat webhook resolution is disabled under pytest")
    profile = resolve_office_profile(required=True)
    profile.require_configured()
    service = profile.google_chat_keychain_service
    account = profile.google_chat_keychain_account
    if not service or not account or (keychain_service is not None and keychain_service != service):
        raise PermissionError("A company-specific notification credential reference is required")
    if keychain_account != DEFAULT_WEBHOOK_KEYCHAIN_ACCOUNT and keychain_account != account:
        raise PermissionError("Notification credential account differs from the company profile")
    return KeychainClient(service).get_secret(account)


def post_google_chat_notification(
    *,
    plan: GoogleChatNotificationPlan,
    webhook_url: str,
    transport: GoogleChatTransport | None = None,
) -> GoogleChatNotificationPlan:
    if plan.status != "ready":
        raise SystemExit("Google Chat notification is blocked; complete workflow verification first")
    if not webhook_url.startswith("https://chat.googleapis.com/"):
        raise SystemExit("Google Chat webhook URL must be a chat.googleapis.com webhook")
    if os.environ.get("PYTEST_CURRENT_TEST") and transport is None:
        raise RuntimeError("real Google Chat transport is disabled under pytest")
    client = transport or UrllibGoogleChatTransport()
    body = json.dumps({"text": plan.message_text}, ensure_ascii=False).encode()
    status, _headers, payload = client.request(
        "POST",
        webhook_url,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        body=body,
    )
    if status not in (200, 201):
        raise RuntimeError(f"Google Chat webhook failed: HTTP {status}")
    response_hash = mask_identifier(payload.decode(errors="replace"))
    return GoogleChatNotificationPlan(
        status="sent",
        message_text=plan.message_text,
        message_hash=plan.message_hash,
        item_count=plan.item_count,
        blocked_reasons=(),
        webhook_url_hash=mask_identifier(webhook_url),
        google_chat_api_called=True,
        response_hash=response_hash,
    )


def deliver_google_chat_notification(
    *,
    payload: dict[str, Any],
    source_hash: str,
    conn: sqlite3.Connection,
    webhook_url: str,
    transport: GoogleChatTransport | None = None,
) -> GoogleChatNotificationPlan:
    """Send only items never recorded as sent, and leave uncertain sends blocked."""
    _require_production_completion_stage(payload=payload, conn=conn)
    if requires_daily_completion_control(payload) and not _active_production_cross_audit_succeeded(
        payload=payload,
        conn=conn,
    ):
        require_daily_completion_notification_allowed(payload)
    accounting_run_id_hash = _accounting_run_id_hash(payload)
    plan, item_hashes, notification_type = _plan_unsent_items(payload=payload, conn=conn)
    if plan.status != "ready":
        if plan.status == "already_sent":
            receipt_hash = _record_chat_dedup_receipt_if_active(
                payload=payload,
                plan=plan,
                conn=conn,
            )
            if receipt_hash:
                return replace(
                    plan,
                    status="historical_sent",
                    blocked_reasons=(),
                    response_hash=receipt_hash,
                )
        return plan
    notification_hash = stable_hash(
        {
            "notification_type": notification_type,
            "message_hash": plan.message_hash,
            "source_hash": source_hash,
            "item_hashes": item_hashes,
            "accounting_run_id_hash": accounting_run_id_hash,
        }
    )
    _reserve_notification(
        conn=conn,
        notification_hash=notification_hash,
        notification_type=notification_type,
        message_hash=plan.message_hash,
        source_hash=source_hash,
        item_hashes=item_hashes,
        accounting_run_id_hash=accounting_run_id_hash,
    )
    conn.commit()
    try:
        sent = post_google_chat_notification(plan=plan, webhook_url=webhook_url, transport=transport)
    except Exception as error:
        _mark_notification_uncertain(
            conn=conn,
            notification_hash=notification_hash,
            failure_hash=stable_hash({"error_type": type(error).__name__, "message": str(error)}),
        )
        conn.commit()
        raise
    _mark_notification_sent(
        conn=conn,
        notification_hash=notification_hash,
        response_hash=sent.response_hash or stable_hash("empty_google_chat_response"),
    )
    conn.commit()
    return sent


def _active_production_cross_audit_succeeded(
    *,
    payload: dict[str, Any],
    conn: sqlite3.Connection,
) -> bool:
    """Use the canonical run receipt instead of re-supplying daily-control JSON."""

    run_hash = _accounting_run_id_hash(payload)
    if run_hash is None:
        return False
    row = conn.execute(
        """
        SELECT r.status, r.current_stage, r.run_mode, s.status AS cross_audit_status
        FROM accounting_runs AS r
        LEFT JOIN accounting_run_stages AS s
          ON s.run_id_hash = r.run_id_hash AND s.stage_key = 'cross_audit'
        WHERE r.run_id_hash = ?
        """,
        (run_hash,),
    ).fetchone()
    return bool(
        row is not None
        and row["run_mode"] == "production"
        and row["status"] == "running"
        and row["current_stage"] == "google_chat_durable_sent"
        and row["cross_audit_status"] == "succeeded"
    )


def _record_chat_dedup_receipt_if_active(
    *,
    payload: dict[str, Any],
    plan: GoogleChatNotificationPlan,
    conn: sqlite3.Connection,
) -> str | None:
    accounting_run_id = str(payload.get("accounting_run_id") or "").strip()
    accounting_run_id_hash = _accounting_run_id_hash(payload)
    if not accounting_run_id or not accounting_run_id_hash:
        return None
    row = conn.execute(
        """
        SELECT current_stage, run_mode
        FROM accounting_runs
        WHERE run_id_hash = ?
        """,
        (accounting_run_id_hash,),
    ).fetchone()
    if row is None or row["current_stage"] != "google_chat_durable_sent":
        return None
    if row["run_mode"] != "production":
        return None
    from .shiwake_run import _record_run_receipt

    return _record_run_receipt(
        conn,
        run_id=accounting_run_id,
        stage="google_chat_durable_sent",
        receipt_kind="google_chat_dedup_readback",
        source_hash="sha256:"
        + stable_hash(
            {
                "accounting_run_id_hash": accounting_run_id_hash,
                "already_sent_message_hash": plan.message_hash,
                "blocked_reasons": list(plan.blocked_reasons),
            }
        ),
        actor="google_chat_notification",
    )


def _require_production_completion_stage(
    *,
    payload: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """Bind a production completion send to the active post-audit run stage."""
    if _notification_type(payload) != "completion":
        return
    accounting_run_id_hash = _accounting_run_id_hash(payload)
    if accounting_run_id_hash is None:
        return
    run = conn.execute(
        """
        SELECT status, current_stage, run_mode
        FROM accounting_runs
        WHERE run_id_hash = ?
        """,
        (accounting_run_id_hash,),
    ).fetchone()
    if _notification_scope(payload) == AGGREGATE_COMPLETION_SCOPE:
        if run is None or run["run_mode"] != "production":
            raise SystemExit(
                "aggregate completion notification requires a production accounting run"
            )
        receipt_hash = str(payload.get("cross_audit_receipt_hash") or "").removeprefix(
            "sha256:"
        )
        receipt = conn.execute(
            "SELECT 1 FROM accounting_run_receipts WHERE run_id_hash=? "
            "AND stage_key='cross_audit' AND receipt_kind='cross_audit_readback' "
            "AND status='verified' AND receipt_hash=?",
            (accounting_run_id_hash, receipt_hash),
        ).fetchone()
        if receipt is None:
            raise SystemExit(
                "aggregate completion notification requires the same-run formal "
                "cross_audit_readback receipt"
            )
        return
    if run is None or run["run_mode"] != "production":
        return
    if run["status"] != "running" or run["current_stage"] != "google_chat_durable_sent":
        raise SystemExit(
            "production completion notification requires the active "
            "google_chat_durable_sent accounting run stage"
        )
    cross_audit = conn.execute(
        """
        SELECT status
        FROM accounting_run_stages
        WHERE run_id_hash = ? AND stage_key = 'cross_audit'
        """,
        (accounting_run_id_hash,),
    ).fetchone()
    if cross_audit is None or cross_audit["status"] != "succeeded":
        raise SystemExit(
            "production completion notification requires succeeded cross_audit"
        )
    if _notification_scope(payload) != PROCESSED_BATCH_SCOPE:
        return
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit("production completion notification requires processed items")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SystemExit(f"production completion item {index} is invalid")
        source_hash = str(item.get("source_file_hash") or "").strip()
        chain = same_chain_status_for_file_hash(conn, source_hash)
        if chain is None:
            raise SystemExit(
                f"production completion item {index} has no unique authoritative same-chain read-back"
            )
        documentless_readback_ready = (
            item.get("evidence_required") is False
            and chain.get("drive_completion_required") is False
            and all(
                chain.get("checks", {}).get(check) is True
                for check in (
                    "ledger_verified",
                    "journal_create_verified",
                    "post_registration_verified",
                )
            )
        )
        if chain["same_chain_ready"] is not True and not documentless_readback_ready:
            raise SystemExit(
                f"production completion item {index} authoritative same-chain read-back is incomplete"
            )


def redeliver_sent_google_chat_notification(
    *,
    payload: dict[str, Any],
    source_hash: str,
    resend_key: str,
    conn: sqlite3.Connection,
    webhook_url: str,
    transport: GoogleChatTransport | None = None,
    include_unsent_items: bool = False,
) -> GoogleChatNotificationPlan:
    """Explicitly resend a verified notification once after a user reports a delivery defect."""
    if not resend_key.strip():
        raise SystemExit("Google Chat resend requires a non-empty resend key")
    if requires_daily_completion_control(payload):
        require_daily_completion_notification_allowed(payload)
    plan = build_completion_notification_plan(payload)
    if plan.status != "ready":
        return plan
    notification_type = _notification_type(payload)
    accounting_run_id_hash = _accounting_run_id_hash(payload)
    items = payload.get("items")
    item_hashes = _notification_item_hashes(
        notification_type,
        items,
        accounting_run_id_hash=accounting_run_id_hash,
        notification_scope=_notification_scope(payload),
    )
    if not isinstance(items, list) or len(item_hashes) != len(items):
        return GoogleChatNotificationPlan(
            status="blocked",
            message_text="",
            message_hash=stable_hash({"blocked": "resend_item_identity_invalid"}),
            item_count=0,
            blocked_reasons=("resend_item_identity_invalid",),
        )
    existing = _existing_item_statuses(conn, item_hashes)
    if any(existing.get(item_hash) == "uncertain_failure" for item_hash in item_hashes):
        return GoogleChatNotificationPlan(
            status="blocked",
            message_text="",
            message_hash=stable_hash({"blocked": "uncertain_delivery", "items": item_hashes}),
            item_count=0,
            blocked_reasons=("previous_delivery_uncertain_manual_reconciliation_required",),
        )
    if not include_unsent_items and any(
        existing.get(item_hash) != "sent" for item_hash in item_hashes
    ):
        return GoogleChatNotificationPlan(
            status="blocked",
            message_text="",
            message_hash=stable_hash({"blocked": "resend_requires_prior_sent", "items": item_hashes}),
            item_count=0,
            blocked_reasons=("resend_requires_all_items_previously_sent",),
        )
    notification_hash = stable_hash(
        {
            "notification_type": notification_type,
            "message_hash": plan.message_hash,
            "source_hash": source_hash,
            "item_hashes": item_hashes,
            "accounting_run_id_hash": accounting_run_id_hash,
            "explicit_resend_key": stable_hash(resend_key),
        }
    )
    previous = conn.execute(
        "SELECT status FROM google_chat_notification_records WHERE notification_hash = ?",
        (notification_hash,),
    ).fetchone()
    if previous is not None:
        status = str(previous["status"])
        if status == "sent":
            return GoogleChatNotificationPlan(
                status="already_sent",
                message_text="",
                message_hash=stable_hash({"already_resent": notification_hash}),
                item_count=0,
                blocked_reasons=("explicit_resend_already_completed",),
            )
        return GoogleChatNotificationPlan(
            status="blocked",
            message_text="",
            message_hash=stable_hash({"blocked": "resend_delivery_uncertain", "notification": notification_hash}),
            item_count=0,
            blocked_reasons=("explicit_resend_delivery_requires_manual_reconciliation",),
        )
    _insert_notification_rows(
        conn=conn,
        notification_hash=notification_hash,
        notification_type=notification_type,
        message_hash=plan.message_hash,
        source_hash=source_hash,
        item_hashes=item_hashes,
        accounting_run_id_hash=accounting_run_id_hash,
        status="dispatching",
        response_hash=None,
    )
    conn.commit()
    try:
        sent = post_google_chat_notification(plan=plan, webhook_url=webhook_url, transport=transport)
    except Exception as error:
        _mark_notification_uncertain(
            conn=conn,
            notification_hash=notification_hash,
            failure_hash=stable_hash({"error_type": type(error).__name__, "message": str(error)}),
        )
        conn.commit()
        raise
    _mark_notification_sent(
        conn=conn,
        notification_hash=notification_hash,
        response_hash=sent.response_hash or stable_hash("empty_google_chat_response"),
    )
    conn.commit()
    return sent


def plan_google_chat_delivery(*, payload: dict[str, Any], conn: sqlite3.Connection) -> GoogleChatNotificationPlan:
    """Return the sendable subset after durable duplicate suppression."""
    plan, _item_hashes, _notification_type_value = _plan_unsent_items(payload=payload, conn=conn)
    return plan


def record_existing_google_chat_notification(
    *,
    payload: dict[str, Any],
    source_hash: str,
    conn: sqlite3.Connection,
) -> GoogleChatNotificationPlan:
    del payload, source_hash, conn
    raise SystemExit(
        "legacy notification summaries cannot be marked sent without a durable delivery receipt; "
        "record the item as delivery-uncertain and reconcile the destination first"
    )


def dry_run_google_chat_notification(plan: GoogleChatNotificationPlan, webhook_url: str | None = None) -> dict[str, Any]:
    result = plan.to_redacted_dict()
    result["dry_run"] = True
    result["notification_allowed"] = plan.status == "ready"
    result["webhook_url_hash"] = mask_identifier(webhook_url) if webhook_url else None
    return result


def _plan_unsent_items(
    *,
    payload: dict[str, Any],
    conn: sqlite3.Connection,
) -> tuple[GoogleChatNotificationPlan, list[str], str]:
    notification_type = _notification_type(payload)
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    aggregate_completion = (
        _notification_scope(payload) == AGGREGATE_COMPLETION_SCOPE
        and _notification_type(payload) == "completion"
    )
    aggregate_only = aggregate_completion or (
        not items
        and _notification_type(payload) == "completion"
        and int(payload.get("terminal_no_journal_count") or 0) > 0
        and payload.get("terminal_no_journal_verified") is True
        and int(payload.get("unresolved_item_count") or 0) == 0
    )
    if aggregate_only:
        identity = (
            {
                "notification_type": "completion",
                "notification_scope": AGGREGATE_COMPLETION_SCOPE,
                "accounting_run_id_hash": _accounting_run_id_hash(payload),
                "cross_audit_receipt_hash": payload.get("cross_audit_receipt_hash"),
            }
            if aggregate_completion
            else {
                "notification_type": "completion",
                "notification_scope": _notification_scope(payload),
                "accounting_run_id_hash": _accounting_run_id_hash(payload),
                "terminal_no_journal_count": int(payload["terminal_no_journal_count"]),
            }
        )
        item_hashes = [
            stable_hash(identity)
        ]
        existing = _existing_item_statuses(conn, item_hashes)
        if existing.get(item_hashes[0]) == "uncertain_failure":
            return (
                GoogleChatNotificationPlan(
                    status="blocked",
                    message_text="",
                    message_hash=stable_hash({"blocked": "uncertain_delivery", "items": item_hashes}),
                    item_count=0,
                    blocked_reasons=("previous_delivery_uncertain_manual_reconciliation_required",),
                ),
                [],
                notification_type,
            )
        if item_hashes[0] in existing:
            return (
                GoogleChatNotificationPlan(
                    status="already_sent",
                    message_text="",
                    message_hash=stable_hash({"already_sent": item_hashes}),
                    item_count=0,
                    blocked_reasons=("all_items_already_recorded",),
                ),
                [],
                notification_type,
            )
        return build_completion_notification_plan(payload), item_hashes, notification_type
    item_hashes = _notification_item_hashes(
        notification_type,
        items,
        accounting_run_id_hash=_accounting_run_id_hash(payload),
        notification_scope=_notification_scope(payload),
    )
    if len(item_hashes) != len(items):
        return build_completion_notification_plan(payload), item_hashes, notification_type
    existing = _existing_item_statuses(conn, item_hashes)
    uncertain = [item_hash for item_hash in item_hashes if existing.get(item_hash) == "uncertain_failure"]
    if uncertain:
        return (
            GoogleChatNotificationPlan(
                status="blocked",
                message_text="",
                message_hash=stable_hash({"blocked": "uncertain_delivery", "items": uncertain}),
                item_count=0,
                blocked_reasons=("previous_delivery_uncertain_manual_reconciliation_required",),
            ),
            [],
            notification_type,
        )
    unsent_pairs = [
        (item, item_hash)
        for item, item_hash in zip(items, item_hashes)
        if item_hash not in existing
    ]
    if not unsent_pairs:
        return (
            GoogleChatNotificationPlan(
                status="already_sent",
                message_text="",
                message_hash=stable_hash({"already_sent": item_hashes}),
                item_count=0,
                blocked_reasons=("all_items_already_recorded",),
            ),
            [],
            notification_type,
        )
    unsent_payload = dict(payload)
    unsent_payload["items"] = [item for item, _item_hash in unsent_pairs]
    return build_completion_notification_plan(unsent_payload), [item_hash for _item, item_hash in unsent_pairs], notification_type


def _notification_type(payload: dict[str, Any]) -> str:
    value = str(payload.get("notification_type") or payload.get("type") or "completion")
    return "pending" if value in {"pending", "waiting"} else "completion"


def _notification_scope(payload: dict[str, Any]) -> str:
    value = str(payload.get("notification_scope") or "daily_completion").strip()
    if value in {PROCESSED_BATCH_SCOPE, AGGREGATE_COMPLETION_SCOPE}:
        return value
    return "daily_completion"


def requires_daily_completion_control(payload: dict[str, Any]) -> bool:
    """Keep the daily close gate while allowing already-verified item reporting."""
    return _notification_type(payload) == "completion" and _notification_scope(payload) not in {
        PROCESSED_BATCH_SCOPE,
        AGGREGATE_COMPLETION_SCOPE,
    }


def _accounting_run_id_hash(payload: dict[str, Any]) -> str | None:
    accounting_run_id = str(payload.get("accounting_run_id") or "").strip()
    if not accounting_run_id:
        return None
    return accounting_run_hash(accounting_run_id)


def _notification_item_hashes(
    notification_type: str,
    items: Any,
    *,
    accounting_run_id_hash: str | None = None,
    notification_scope: str = "daily_completion",
) -> list[str]:
    # The durable run binding is stored in its own column.  Do not mix it into
    # the item identity: the same journal must remain deduplicated even if an
    # operator accidentally includes it in a later accounting run.
    del accounting_run_id_hash
    if not isinstance(items, list):
        return []
    item_hashes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if notification_type == "completion":
            journal_number = str(item.get("journal_number") or "").strip()
            if not journal_number:
                continue
            identity: dict[str, Any] = {
                "notification_type": notification_type,
                "notification_scope": notification_scope,
                "journal_number": journal_number,
            }
        else:
            identity = {
                "notification_type": notification_type,
                "notification_scope": notification_scope,
                "notification_key": str(item.get("notification_key") or "").strip(),
                "label": _item_label(item),
                "amount": _format_amount(item.get("amount")),
                "reason": str(item.get("reason") or item.get("pending_reason") or "").strip(),
            }
        item_hashes.append(stable_hash(identity))
    return item_hashes


def _existing_item_statuses(conn: sqlite3.Connection, item_hashes: list[str]) -> dict[str, str]:
    if not item_hashes:
        return {}
    placeholders = ",".join("?" for _ in item_hashes)
    rows = conn.execute(
        f"SELECT item_hash, status FROM google_chat_notification_items WHERE item_hash IN ({placeholders})",
        item_hashes,
    ).fetchall()
    return {
        str(row["item_hash"]): str(row["status"])
        for row in rows
        if str(row["status"]) in DELIVERY_BLOCKING_STATUSES
    }


def _reserve_notification(
    *,
    conn: sqlite3.Connection,
    notification_hash: str,
    notification_type: str,
    message_hash: str,
    source_hash: str,
    item_hashes: list[str],
    accounting_run_id_hash: str | None,
) -> None:
    _insert_notification_rows(
        conn=conn,
        notification_hash=notification_hash,
        notification_type=notification_type,
        message_hash=message_hash,
        source_hash=source_hash,
        item_hashes=item_hashes,
        accounting_run_id_hash=accounting_run_id_hash,
        status="dispatching",
        response_hash=None,
    )


def _insert_notification_rows(
    *,
    conn: sqlite3.Connection,
    notification_hash: str,
    notification_type: str,
    message_hash: str,
    source_hash: str,
    item_hashes: list[str],
    accounting_run_id_hash: str | None,
    status: str,
    response_hash: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO google_chat_notification_records (
            notification_hash, notification_type, message_hash, source_hash,
            item_count, status, response_hash, accounting_run_id_hash, sent_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                  CASE WHEN ? IN ('sent', 'historical_sent')
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END)
        """,
        (
            notification_hash,
            notification_type,
            message_hash,
            source_hash,
            len(item_hashes),
            status,
            response_hash,
            accounting_run_id_hash,
            status,
        ),
    )
    for item_hash in item_hashes:
        conn.execute(
            """
            INSERT OR IGNORE INTO google_chat_notification_items (
                item_hash, notification_hash, notification_type, status,
                source_hash, accounting_run_id_hash, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?,
                      CASE WHEN ? IN ('sent', 'historical_sent')
                           THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END)
            """,
            (
                item_hash,
                notification_hash,
                notification_type,
                status,
                source_hash,
                accounting_run_id_hash,
                status,
            ),
        )


def _mark_notification_sent(*, conn: sqlite3.Connection, notification_hash: str, response_hash: str) -> None:
    conn.execute(
        """
        UPDATE google_chat_notification_records
        SET status = 'sent', response_hash = ?, failure_hash = NULL,
            sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE notification_hash = ? AND status = 'dispatching'
        """,
        (response_hash, notification_hash),
    )
    conn.execute(
        """
        UPDATE google_chat_notification_items
        SET status = 'sent', sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE notification_hash = ? AND status = 'dispatching'
        """,
        (notification_hash,),
    )


def _mark_notification_uncertain(*, conn: sqlite3.Connection, notification_hash: str, failure_hash: str) -> None:
    conn.execute(
        """
        UPDATE google_chat_notification_records
        SET status = 'uncertain_failure', failure_hash = ?
        WHERE notification_hash = ? AND status = 'dispatching'
        """,
        (failure_hash, notification_hash),
    )
    conn.execute(
        """
        UPDATE google_chat_notification_items
        SET status = 'uncertain_failure'
        WHERE notification_hash = ? AND status = 'dispatching'
        """,
        (notification_hash,),
    )


def _completion_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        try:
            terminal_count = int(payload.get("terminal_no_journal_count") or 0)
            unresolved_count = int(payload.get("unresolved_item_count") or 0)
        except (TypeError, ValueError):
            return ["items_missing"]
        if (
            terminal_count > 0
            and payload.get("terminal_no_journal_verified") is True
            and unresolved_count == 0
        ):
            return []
        return ["items_missing"]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append(f"item_{index}_not_object")
            continue
        if not str(item.get("journal_number") or "").strip():
            failures.append(f"item_{index}_journal_number_missing")
        readback = str(
            item.get("registration_readback_status")
            or item.get("moneyforward_registration_readback")
            or item.get("readback_status")
            or ""
        )
        if readback not in {"verified", "passed"}:
            failures.append(f"item_{index}_journal_readback_not_verified")
        evidence_required = item.get("evidence_required", True) is not False
        voucher_status = str(item.get("voucher_status") or "")
        if evidence_required and voucher_status != "verified":
            failures.append(f"item_{index}_voucher_not_verified")
        processed_status = str(item.get("processed_folder_status") or "")
        if processed_status not in {"verified", "not_required"}:
            failures.append(f"item_{index}_processed_folder_not_verified")
    return failures


def _aggregate_completion_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not str(payload.get("accounting_run_id") or "").strip():
        failures.append("accounting_run_id_missing")
    receipt_hash = str(payload.get("cross_audit_receipt_hash") or "")
    if not _is_sha256(receipt_hash):
        failures.append("cross_audit_receipt_missing_or_invalid")
    if payload.get("cross_audit_status") != "verified":
        failures.append("cross_audit_not_verified")
    counts = payload.get("summary_counts")
    if not isinstance(counts, dict):
        failures.append("summary_counts_missing")
        return failures
    parsed_counts: dict[str, int] = {}
    for key in (
        "newly_registered",
        "reused",
        "review",
        "not_ready",
        "not_applicable",
        "remaining",
        "replay_additional_write",
    ):
        try:
            value = int(counts.get(key, -1))
        except (TypeError, ValueError):
            value = -1
        parsed_counts[key] = value
        if value < 0:
            failures.append(f"summary_{key}_invalid")
    if parsed_counts["remaining"] != 0:
        failures.append("processable_remaining_present")
    if parsed_counts["replay_additional_write"] != 0:
        failures.append("replay_additional_write_present")
    return failures


def _no_journal_summary_failures(payload: dict[str, Any]) -> list[str]:
    count = payload.get("terminal_no_journal_count", 0)
    try:
        terminal_count = int(count)
    except (TypeError, ValueError):
        return ["terminal_no_journal_count_invalid"]
    failures: list[str] = []
    if terminal_count < 0:
        failures.append("terminal_no_journal_count_invalid")
    if terminal_count and payload.get("terminal_no_journal_verified") is not True:
        failures.append("terminal_no_journal_not_verified")
    try:
        unresolved_count = int(payload.get("unresolved_item_count") or 0)
    except (TypeError, ValueError):
        failures.append("unresolved_item_count_invalid")
        return failures
    if unresolved_count != 0:
        failures.append("unresolved_items_present")
    return failures


def _processed_batch_failures(payload: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if not str(payload.get("accounting_run_id") or "").strip():
        failures.append("accounting_run_id_missing")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        source_hash = str(item.get("source_file_hash") or "").strip()
        if not _is_sha256(source_hash):
            failures.append(f"item_{index}_source_file_hash_missing_or_invalid")
        if str(item.get("processing_ledger_status") or "") != "verified":
            failures.append(f"item_{index}_processing_ledger_not_verified")
        if item.get("same_chain_ready") is not True:
            failures.append(f"item_{index}_same_chain_not_ready")
        if not str(item.get("debit_detail") or "").strip():
            failures.append(f"item_{index}_debit_detail_missing")
        if not str(item.get("credit_detail") or "").strip():
            failures.append(f"item_{index}_credit_detail_missing")
    return failures


def _is_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:].lower())


def _pending_failures(payload: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if not items:
        return ["items_missing"]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append(f"item_{index}_not_object")
            continue
        if not _item_label(item):
            failures.append(f"item_{index}_label_missing")
        if not str(item.get("reason") or item.get("pending_reason") or "").strip():
            failures.append(f"item_{index}_reason_missing")
    return failures


def _build_completion_message(payload: dict[str, Any], items: list[dict[str, Any]]) -> str:
    default_title = (
        "Money Forward 仕訳処理済み"
        if _notification_scope(payload) == PROCESSED_BATCH_SCOPE
        else "Money Forward 仕訳完了"
    )
    title = str(payload.get("title") or default_title)
    lines = [title]
    for item in items:
        number = item.get("journal_number")
        amount = _format_amount(item.get("amount"))
        label = _item_label(item)
        journal_text = _journal_text(item)
        evidence_required = item.get("evidence_required", True) is not False
        if not evidence_required:
            voucher = "証憑不要"
        else:
            voucher = "証憑OK" if str(item.get("voucher_status") or "") == "verified" else "証憑要確認"
        debit_detail = str(item.get("debit_detail") or "").strip()
        credit_detail = str(item.get("credit_detail") or "").strip()
        if debit_detail and credit_detail:
            lines.extend(
                [
                    "",
                    f"No.{number} {label} {amount}",
                    f"借方: {debit_detail}",
                    f"貸方: {credit_detail}",
                    f"証憑: {voucher}",
                ]
            )
        else:
            suffix = f"  {journal_text} {voucher}" if journal_text else f" {voucher}"
            lines.extend(["", f"No.{number} {label} {amount}{suffix}"])
    terminal_count = int(payload.get("terminal_no_journal_count") or 0)
    if terminal_count:
        lines.extend(["", f"仕訳不要（内容・取引関係確認済み） {terminal_count}件"])
    if payload.get("unresolved_item_count") == 0:
        lines.extend(["", "未処理 0件"])
    return "\n".join(lines)


def _build_aggregate_completion_message(payload: dict[str, Any]) -> str:
    counts = payload.get("summary_counts")
    counts = counts if isinstance(counts, dict) else {}
    audit_checks = payload.get("cross_audit_checks")
    audit_checks = audit_checks if isinstance(audit_checks, dict) else {}
    verified_checks = sum(value == "verified" for value in audit_checks.values())
    items = payload.get("items")
    items = items if isinstance(items, list) else []
    fixed_rows = _build_completion_message(
        {**payload, "unresolved_item_count": None}, items
    )
    return "\n".join(
        (
            fixed_rows,
            "",
            f"新規登録 {int(counts.get('newly_registered') or 0)}件 / "
            f"reuse {int(counts.get('reused') or 0)}件",
            f"REVIEW {int(counts.get('review') or 0)}件 / "
            f"NOT_READY {int(counts.get('not_ready') or 0)}件 / "
            f"NOT_APPLICABLE {int(counts.get('not_applicable') or 0)}件",
            f"Money Forward未仕訳残 {int(counts.get('moneyforward_unposted') or 0)}件 / "
            f"処理可能残件 {int(counts.get('processable_remaining') or 0)}件 / "
            f"REVIEW {int(counts.get('review') or 0)}件",
            f"Cross Audit {payload.get('cross_audit_status') or 'not_verified'} "
            f"({verified_checks}/{len(audit_checks)} checks) / "
            f"replay追加write {int(counts.get('replay_additional_write') or 0)}件",
            "",
            f"未処理 {max(int(counts.get('moneyforward_unposted') or 0), int(counts.get('processable_remaining') or 0), int(counts.get('review') or 0))}件",
        )
    )


def _build_pending_message(payload: dict[str, Any], items: list[dict[str, Any]]) -> str:
    title = str(payload.get("title") or "Money Forward 仕訳待機")
    lines = [title]
    for item in items:
        amount = _format_amount(item.get("amount"))
        label = _item_label(item)
        reason = str(item.get("reason") or item.get("pending_reason") or "確認待ち")
        next_action = str(item.get("next_action") or "").strip()
        journal_text = _journal_text(item)
        suffix = f"  {journal_text}" if journal_text else ""
        lines.extend(["", f"{label} {amount}{suffix}"])
        lines.append(f"待機: {reason}")
        if next_action:
            lines.append(f"次: {next_action}")
    return "\n".join(lines)


def _format_amount(value: Any) -> str:
    try:
        return f"{int(value):,}円"
    except Exception:
        return str(value or "")


def _item_label(item: dict[str, Any]) -> str:
    for key in ("label", "vendor_name", "counterparty", "evidence", "description"):
        value = str(item.get(key) or "").strip()
        if value:
            return _shorten(value, limit=28)
    return ""


def _journal_text(item: dict[str, Any]) -> str:
    summary = str(item.get("journal_summary") or "").strip()
    if summary:
        return _compact_journal_summary(summary)
    debit = str(item.get("debit") or item.get("debit_account") or "").strip()
    credit = str(item.get("credit") or item.get("credit_account") or "").strip()
    if not debit or not credit:
        return ""
    return _compact_journal_summary(f"{debit} → {credit}")


def _compact_journal_summary(value: str) -> str:
    if "→" not in value:
        return _compact_account_text(value)
    debit, credit = value.split("→", 1)
    return f"{_strip_tax_from_debit(_compact_account_text(debit))} → {_compact_account_text(credit)}"


def _compact_account_text(value: str) -> str:
    compact = " ".join(value.split())
    replacements = {
        "課税仕入 10%": "課仕10%",
        "課税仕入10%": "課仕10%",
        "課仕 10%": "課仕10%",
        "課仕 (軽)8%": "軽8%",
        "課仕8%": "軽8%",
        "一人1万円以下": "1万円以下",
    }
    for before, after in replacements.items():
        compact = compact.replace(before, after)
    return compact


def _strip_tax_from_debit(value: str) -> str:
    compact = value.strip()
    tax_tokens = [
        "(課仕10%+軽8%)",
        "(課仕10%＋軽8%)",
        "/課仕10%",
        "/課税仕入10%",
        "/課税仕入 10%",
        "/課仕 10%",
        "/軽8%",
        "/課仕(軽)8%",
        "/課仕 (軽)8%",
    ]
    for token in tax_tokens:
        compact = compact.replace(token, "/")
    while "//" in compact:
        compact = compact.replace("//", "/")
    return compact


def _shorten(value: str, *, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
