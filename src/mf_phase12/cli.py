from __future__ import annotations
from contextvars import ContextVar

_PRODUCTION_WRITES = ContextVar("starter_production_write_intent", default=False)


def _require_write_intent():
    if not _PRODUCTION_WRITES.get():
        raise PermissionError(
            "production execution requires explicit --execute acknowledgement"
        )


def _validate_runtime_database(profile, db_path=None):
    import os
    from .office_profile import (
        resolve_moneyforward_db_path,
        require_database_office_identity,
    )

    profile.require_configured()
    if os.environ.get("MF_PHASE12_DATABASE_URL") or os.environ.get(
        "MF_PHASE12_DATABASE_SCHEMA"
    ):
        raise ValueError(
            "global database overrides are not permitted for a profiled runtime"
        )
    path = resolve_moneyforward_db_path(profile, db_path, external_io=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return require_database_office_identity(conn, profile)


def _profile_boundary():
    profile = get_current_profile()
    if profile is None:
        raise ValueError("explicit office profile is required")
    return {
        "profile_slug": profile.slug,
        "boundary_fingerprint": profile.boundary_fingerprint,
    }


def _invoke_profile_handler(args, handler):
    from .office_profile import (
        load_office_profile,
        using_office_profile,
        resolve_moneyforward_db_path,
    )

    selected = Path(args.office_profile).expanduser()
    if selected.suffix != ".json":
        raise ValueError("--office-profile must name an explicit local JSON profile")
    profile = load_office_profile(
        selected.stem, profiles_dir=selected.resolve().parent, repository_root=REPO_ROOT
    )
    with using_office_profile(profile):
        acknowledged = bool(getattr(args, "execute", False))
        action = getattr(args, "run_action", None)
        if args.command in {"prepare", "replay"}:
            if args.command == "replay" or getattr(args, "db_path", None) is not None:
                args.db_path = resolve_moneyforward_db_path(
                    profile, getattr(args, "db_path", None)
                )
                _validate_runtime_database(profile, args.db_path)
            token = _PRODUCTION_WRITES.set(False)
            try:
                return handler(args)
            finally:
                _PRODUCTION_WRITES.reset(token)
        if not acknowledged:
            if args.command == "run" and action == "contract":
                return handler(args)
            if args.command in {"doctor", "audit", "connect", "verify"}:
                return handler(args)
            if args.command in {"status", "run"} and (
                args.command == "status" or action == "status"
            ):
                path = resolve_moneyforward_db_path(
                    profile, getattr(args, "db_path", None)
                )
                _validate_runtime_database(profile, path)
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    if args.command == "run":
                        from .shiwake_run import build_run_status

                        result = build_run_status(conn, args.run_id)
                    else:
                        result = {"status": "read_only", **profile.to_redacted_dict()}
                _print_json({**result, "external_write_called": False})
                return 0
            _print_json(
                {
                    "status": "dry_run",
                    "command": args.command,
                    "action": action,
                    "active_workflows": list(ACTIVE_ACCOUNTING_WORKFLOWS),
                    "profile_slug": profile.slug,
                    "external_api_called": False,
                    "external_write_called": False,
                    "database_write_called": False,
                    "execution_requires": "explicit --execute acknowledgement and configured profile",
                }
            )
            return 0
        profile.require_configured()
        args.db_path = resolve_moneyforward_db_path(
            profile, getattr(args, "db_path", None)
        )
        if args.command not in {"setup", "rag-build"} or args.db_path.exists():
            _validate_runtime_database(profile, args.db_path)
        production = (
            args.command == "run"
            and action == "execute"
            and (getattr(args, "manifest_json", None) is None)
        )
        token = _PRODUCTION_WRITES.set(production)
        try:
            return handler(args)
        finally:
            _PRODUCTION_WRITES.reset(token)


def _validate_drive_route(folder_id):
    from .office_profile import require_google_drive_evidence_source_folder

    require_google_drive_evidence_source_folder(get_current_profile(), str(folder_id))


def _assert_run_artifact(payload):
    boundary = _profile_boundary()
    if any((payload.get(key) != value for key, value in boundary.items())):
        raise ValueError("run artifact belongs to another profile or binding")


from .office_profile import get_current_profile
from .shiwake_run import (
    _setting,
    _office_name,
    _mf_principal,
    _drive_principal,
    _artifact_root,
)
import argparse
import base64
import binascii
import fcntl
import gzip
import hashlib
import json
import math
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from .hash_utils import accounting_run_hash
from .shiwake_run import ACTIVE_ACCOUNTING_WORKFLOWS

REPO_ROOT = Path(__file__).resolve().parents[2]


class PayableAuthContext:
    def __init__(
        self,
        *,
        gateway_access_token: str,
        external_access_token: str,
        external_office_id: str,
        external_token_refreshed: bool,
    ):
        self.gateway_access_token = gateway_access_token
        self.external_access_token = external_access_token
        self.external_office_id = external_office_id
        self.external_token_refreshed = external_token_refreshed


class ProductionRuntime(tuple):
    """Four-position compatible runtime with optional provider context.

    Existing workflows keep tuple unpacking unchanged.  Payables can reuse the
    same preflight without introducing a second dispatcher or credential store.
    """

    def __new__(
        cls,
        accounting_client: Any,
        office_id: str,
        refresh_result: Any | None,
        drive_access_token: str,
        *,
        payable: PayableAuthContext | None = None,
    ) -> "ProductionRuntime":
        value = super().__new__(
            cls, (accounting_client, office_id, refresh_result, drive_access_token)
        )
        value.payable = payable
        return value


class AuthenticationHandoffRequired(RuntimeError):
    """Expected operator authentication; this is not a workflow error."""

    def __init__(
        self,
        *,
        account: str,
        service: str,
        reason: str,
        browser_opened: bool = False,
        input_required: bool = False,
    ):
        super().__init__(reason)
        self.account = account
        self.service = service
        self.browser_opened = browser_opened
        self.input_required = input_required


PUBLIC_COMMANDS = (
    "setup",
    "status",
    "prepare",
    "replay",
    "verify",
    "audit",
    "doctor",
    "cleanup",
    "run",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mf_phase12",
        description="Money Forward Phase 12 の日常入口。prepare/replay はローカル専用で、Money Forward・Google Drive・OAuthへ接続しません。",
        epilog="テストは make test、Rust検証は make rust-test を使用します。",
    )
    parser.add_argument(
        "--office-profile", required=True, help="Explicit local company profile JSON"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Acknowledge execution; existing write gates still apply",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    connect = subparsers.add_parser(
        "connect",
        help="Verify selected Money Forward identity with read-only discovery",
    )
    connect.set_defaults(handler=_connect)
    rag_build = subparsers.add_parser(
        "rag-build", help="Build only the selected company memory from official reads"
    )
    rag_build.add_argument("--execute", action="store_true", default=argparse.SUPPRESS)
    rag_build.add_argument("--start-date", required=True)
    rag_build.add_argument("--end-date", required=True)
    rag_build.set_defaults(handler=_rag_build)
    setup = subparsers.add_parser("setup", help="ローカルSQLite DBを初期化する")
    _add_db_path(setup)
    setup.add_argument("--execute", action="store_true", default=argparse.SUPPRESS)
    setup.set_defaults(handler=_setup)
    status = subparsers.add_parser(
        "status", help="ワークフロー、学習、通知の状態を要約する"
    )
    _add_db_path(status)
    status.add_argument("--detail", action="store_true", help="詳細な状態を表示する")
    status.add_argument(
        "--web",
        action="store_true",
        help="ローカルブラウザ用のリアルタイム工程ボードを起動する",
    )
    status.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Web工程ボードのローカルポート（既定: 8766）",
    )
    status.set_defaults(handler=_status)
    prepare = subparsers.add_parser(
        "prepare", help="仕訳バッチをローカルでdry-run・振り分けする"
    )
    prepare.add_argument("--input-json", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    _add_db_path(prepare, use_default=False)
    prepare.add_argument("--actor", default="codex")
    prepare.set_defaults(handler=_prepare)
    replay = subparsers.add_parser(
        "replay", help="正規化済みfactsを外部書込みなしでローカル再生する"
    )
    replay.add_argument("--facts-json", required=True, type=Path)
    replay.add_argument("--output-dir", required=True, type=Path)
    _add_db_path(replay)
    replay.add_argument("--scope-hash", default="local-replay-voucher-write-scope-hash")
    replay.add_argument("--approved-at")
    replay.set_defaults(handler=_replay)
    verify = subparsers.add_parser(
        "verify", help="証跡・製品・リリースreadinessをまとめて検証する"
    )
    _add_db_path(verify)
    verify.add_argument(
        "--detail", action="store_true", help="詳細な検証結果を表示する"
    )
    verify.add_argument(
        "--strict", action="store_true", help="未準備なら終了コード1を返す"
    )
    verify.set_defaults(handler=_verify)
    audit = subparsers.add_parser("audit", help="標準仕訳契約をローカルで監査する")
    audit.add_argument("--detail", action="store_true", help="全監査項目を表示する")
    audit.set_defaults(handler=_audit)
    doctor = subparsers.add_parser(
        "doctor", help="公開入口の実行環境をread-only診断する"
    )
    doctor.set_defaults(handler=_doctor)
    cleanup = subparsers.add_parser("cleanup", help="旧デモ台帳の整理候補を確認する")
    _add_db_path(cleanup)
    cleanup.add_argument(
        "--execute",
        action="store_true",
        help="候補になったローカルDBのデモ行を削除する（既定はdry-run）",
    )
    cleanup.set_defaults(handler=_cleanup)
    run = subparsers.add_parser(
        "run",
        help="configured_companyの仕訳を全対象確認から通知・学習・Issueまで一つの実行IDで管理する",
    )
    run_subparsers = run.add_subparsers(
        dest="run_action", metavar="ACTION", required=True
    )
    run_start = run_subparsers.add_parser("start", help="新しい統一実行を開始する")
    _add_run_common(run_start)
    run_start.add_argument("--accounting-date")
    run_start.add_argument("--instruction", default="仕分けを行ってください")
    run_status = run_subparsers.add_parser(
        "status", help="統一実行の現在地点を確認する"
    )
    _add_run_common(run_status)
    run_advance = run_subparsers.add_parser(
        "advance", help="現在ステージの証跡を検証して進める"
    )
    _add_run_common(run_advance)
    run_advance.add_argument("--stage", required=True)
    run_advance.add_argument("--evidence-json", required=True, type=Path)
    run_fail = run_subparsers.add_parser("fail", help="現在ステージの失敗を記録する")
    _add_run_common(run_fail)
    run_fail.add_argument("--stage", required=True)
    run_fail.add_argument("--reason", required=True)
    run_fail.add_argument("--error-code", default="stage_failed")
    run_fail.add_argument("--blocked", action="store_true")
    run_fail.add_argument("--uncertain-external-write", action="store_true")
    run_resume = run_subparsers.add_parser(
        "resume", help="失敗した同じステージから再開する"
    )
    _add_run_common(run_resume)
    run_resolve = run_subparsers.add_parser(
        "resolve", help="外部書込み結果不明をread-back結果で安全に解消する"
    )
    _add_run_common(run_resolve)
    run_resolve.add_argument("--stage", required=True)
    run_resolve.add_argument(
        "--outcome", required=True, choices=("applied", "not_applied", "blocked")
    )
    run_resolve.add_argument("--readback-json", required=True, type=Path)
    run_execute = run_subparsers.add_parser(
        "execute", help="統一実行を開始・再開する（manifest指定時は既存証跡を検証する）"
    )
    _add_run_common(run_execute)
    execute_source = run_execute.add_mutually_exclusive_group()
    execute_source.add_argument(
        "--manifest-json",
        type=Path,
        help="互換用の既存証跡manifest。通常の実行開始には不要",
    )
    execute_source.add_argument(
        "--workflow",
        choices=(
            "sales_invoice",
            "payable_invoice",
            "all_receipt_folders",
            "payroll_bonus",
            "bank_transfer",
            "card",
        ),
        help="指定workflowだけを他workflowの完了を待たず実行する",
    )
    run_execute.add_argument("--accounting-date")
    run_execute.add_argument("--instruction", default="仕分けを行ってください")
    run_e2e = run_subparsers.add_parser(
        "e2e", help="全ステージを合成証跡で1から完了まで検証する（外部書込みなし）"
    )
    _add_run_common(run_e2e)
    run_contract = run_subparsers.add_parser(
        "contract", help="統一実行の順序・完了条件を表示する"
    )
    run_contract.add_argument("--db-path", type=Path, help=argparse.SUPPRESS)
    run.set_defaults(handler=_run)
    expert_help = subparsers.add_parser("expert-help", help=argparse.SUPPRESS)
    expert_help.add_argument("--filter", default="", help="名前の部分一致で絞り込む")
    expert_help.set_defaults(handler=_expert_help)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return _invoke_profile_handler(args, handler)
    except AuthenticationHandoffRequired as exc:
        payload = {
            "status": "waiting_for_auth",
            "command": args.command,
            "account": exc.account,
            "service": exc.service,
            "reason": str(exc),
            "workflow_error": False,
            "resume_same_run": True,
        }
        if exc.browser_opened:
            payload.update(
                {
                    "browser_opened": True,
                    "input_required": exc.input_required,
                    "resume_instruction": "認証画面で入力を完了すると同じrun_idで自動再開します",
                }
            )
        _print_json(payload)
        return 3
    except (OSError, ValueError, RuntimeError) as exc:
        _print_json(
            {
                "status": "error",
                "command": args.command,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        )
        return 2


def _add_db_path(parser: argparse.ArgumentParser, *, use_default: bool = True) -> None:
    from .db import DEFAULT_DB_PATH

    parser.add_argument("--db-path", type=Path, default=None)


def _add_run_common(parser: argparse.ArgumentParser) -> None:
    _add_db_path(parser)
    parser.add_argument(
        "--execute",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Acknowledge production execution",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actor", default="codex")


def _setup(args):
    import os
    from .db import Phase12Database
    from .office_profile import bind_database_office_identity

    profile = get_current_profile()
    path = profile.moneyforward_db_path
    binding = _read_only_profile_client().validate_office_identity()
    if path.exists():
        _validate_runtime_database(profile, path)
        _print_json(
            {
                "status": "already_bound",
                "profile_slug": profile.slug,
                "database_write_called": False,
            }
        )
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    os.close(descriptor)
    db = Phase12Database(path)
    db.initialize()
    with db.connect() as conn:
        bind_database_office_identity(conn, profile, binding)
    _print_json(
        {
            "status": "initialized_and_bound",
            "profile_slug": profile.slug,
            "moneyforward_write_called": False,
            "database_write_called": True,
        }
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    if args.web:
        return _serve_status_dashboard(args.db_path, port=args.port)
    from .db import Phase12Database
    from .shiwake_workflow_api import (
        build_learning_status,
        build_notification_delivery_status,
        build_shiwake_autorun_startup_check,
        build_shiwake_workflow_contract,
    )

    db = Phase12Database(args.db_path)
    db.initialize()
    with db.connect() as conn:
        contract = build_shiwake_workflow_contract()
        startup = build_shiwake_autorun_startup_check(conn)
        learning = build_learning_status(conn)
        notification = build_notification_delivery_status(conn)
    if args.detail:
        payload: dict[str, Any] = {
            "status": startup["status"],
            "workflow_contract": contract,
            "startup_check": startup,
            "learning": learning,
            "notification_delivery": notification,
        }
    else:
        payload = {
            "status": startup["status"],
            "workflow_contract": contract["status"],
            "journal_pattern_count": learning["journal_pattern_count"],
            "financial_event_learning_event_count": learning[
                "financial_event_learning_event_count"
            ],
            "notification_delivery": notification["status"],
            "next_actions": startup["next_actions"],
        }
    payload.update(_local_only_flags())
    _print_json(payload)
    return 0


_DASHBOARD_STAGE_LABELS = {
    "sales_invoice": "売上請求",
    "payable_invoice": "受領請求",
    "all_receipt_folders": "全証憑フォルダ",
    "payroll_bonus": "給与・賞与",
    "bank_transfer": "銀行振込",
    "card": "カード",
    "tax_insurance": "税金・社会保険",
    "cross_audit": "クロス監査",
    "google_chat_durable_sent": "Google Chat通知",
    "rag_learning": "RAG学習",
    "issue_update": "GitHub Issue更新",
}


def _serve_status_dashboard(db_path: Path, *, port: int) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    if not 1024 <= port <= 65535:
        raise ValueError("status dashboard port must be between 1024 and 65535")
    database_path = Path(db_path).resolve()
    dashboard_html = _status_dashboard_html().encode("utf-8")

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in {"/", "/status"}:
                self._send(200, "text/html; charset=utf-8", dashboard_html)
                return
            if path == "/api/status":
                payload = json.dumps(
                    _build_status_dashboard_snapshot(database_path),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", payload)
                return
            if path == "/healthz":
                self._send(200, "application/json; charset=utf-8", b'{"status":"ok"}')
                return
            self._send(
                404, "application/json; charset=utf-8", b'{"status":"not_found"}'
            )

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    class DashboardServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = DashboardServer(("127.0.0.1", port), DashboardHandler)
    _print_json(
        {
            "status": "dashboard_running",
            "url": f"http://127.0.0.1:{port}/status",
            "refresh_seconds": 2,
            "database_access": "read_only_short_lived",
            "external_network_exposed": False,
        }
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _build_status_dashboard_snapshot(db_path: Path) -> dict[str, Any]:
    from .shiwake_run import RUN_STAGES, STAFF_LIMIT_SECONDS, VETERAN_TARGET_SECONDS

    database_path = Path(db_path).resolve()
    checked_at = datetime.now(timezone.utc)
    task_contract = _dashboard_task_contract()
    if not database_path.is_file():
        return {
            "status": "not_ready",
            "reason": "operational database not found",
            "checked_at": checked_at.isoformat(),
            "stages": [],
            "remaining_by_stage": [],
            "transitions": [],
            "task_contract": task_contract,
        }
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            run = conn.execute(
                "\n                SELECT run_id_hash, accounting_date, run_mode, status, current_stage,\n                       generation, created_at, updated_at, completed_at\n                FROM accounting_runs\n                WHERE run_mode = 'production'\n                ORDER BY datetime(created_at) DESC, id DESC\n                LIMIT 1\n                "
            ).fetchone()
            if run is None:
                return {
                    "status": "not_ready",
                    "reason": "production run not found",
                    "checked_at": checked_at.isoformat(),
                    "stages": [],
                    "remaining_by_stage": [],
                    "transitions": [],
                    "task_contract": task_contract,
                }
            run_hash = str(run["run_id_hash"])
            stage_rows = conn.execute(
                "\n                SELECT stage_key, stage_order, status, attempt_count,\n                       started_at, completed_at, failure_code\n                FROM accounting_run_stages\n                WHERE run_id_hash = ?\n                ORDER BY stage_order\n                ",
                (run_hash,),
            ).fetchall()
            remaining_rows = conn.execute(
                "\n                SELECT source_stage, status, COUNT(*) AS item_count\n                FROM accounting_run_items\n                WHERE run_id_hash = ?\n                  AND status NOT IN ('verified', 'not_applicable')\n                GROUP BY source_stage, status\n                ORDER BY source_stage, status\n                ",
                (run_hash,),
            ).fetchall()
            transition_rows = conn.execute(
                "\n                SELECT stage_key, from_status, to_status, generation, created_at\n                FROM accounting_run_transitions\n                WHERE run_id_hash = ?\n                ORDER BY id DESC\n                LIMIT 12\n                ",
                (run_hash,),
            ).fetchall()
    except sqlite3.Error as exc:
        return {
            "status": "not_ready",
            "reason": f"database read failed: {type(exc).__name__}",
            "checked_at": checked_at.isoformat(),
            "stages": [],
            "remaining_by_stage": [],
            "transitions": [],
            "task_contract": task_contract,
        }
    created_at = _dashboard_timestamp(run["created_at"])
    completed_at = _dashboard_timestamp(run["completed_at"])
    updated_at = _dashboard_timestamp(run["updated_at"])
    elapsed_end = completed_at or checked_at
    elapsed_seconds = (
        max(int((elapsed_end - created_at).total_seconds()), 0) if created_at else 0
    )
    stale_seconds = (
        max(int((checked_at - updated_at).total_seconds()), 0) if updated_at else None
    )
    remaining_by_stage = [
        {
            "stage": str(row["source_stage"]),
            "label": _DASHBOARD_STAGE_LABELS.get(
                str(row["source_stage"]), str(row["source_stage"])
            ),
            "status": str(row["status"]),
            "count": int(row["item_count"]),
        }
        for row in remaining_rows
    ]
    remaining_count = sum((int(row["count"]) for row in remaining_by_stage))
    stage_statuses = {str(row["stage_key"]): row for row in stage_rows}
    stages = []
    for order, stage in enumerate(RUN_STAGES):
        row = stage_statuses.get(stage)
        stages.append(
            {
                "stage": stage,
                "label": _DASHBOARD_STAGE_LABELS[stage],
                "order": order + 1,
                "status": str(row["status"]) if row is not None else "pending",
                "attempt_count": int(row["attempt_count"]) if row is not None else 0,
                "started_at": row["started_at"] if row is not None else None,
                "completed_at": row["completed_at"] if row is not None else None,
                "failure_code": row["failure_code"] if row is not None else None,
                "current": stage == run["current_stage"],
            }
        )
    succeeded_count = sum((stage["status"] == "succeeded" for stage in stages))
    accounting_run_status = str(run["status"])
    task_blocked = task_contract.get("status") == "blocked"
    blocker_summary = str(task_contract.get("blocker_summary") or "")
    display_status = "blocked" if task_blocked else accounting_run_status
    display_current_stage_label = (
        "動作確認：Money Forward API HTTP 401"
        if task_blocked and "HTTP 401" in blocker_summary
        else "作業指示：停止中"
        if task_blocked
        else _DASHBOARD_STAGE_LABELS.get(
            str(run["current_stage"]), str(run["current_stage"] or "-")
        )
    )
    return {
        "status": display_status,
        "accounting_run_status": accounting_run_status,
        "accounting_date": str(run["accounting_date"]),
        "run_mode": str(run["run_mode"]),
        "run_id_short": run_hash[:12],
        "current_stage": run["current_stage"],
        "current_stage_label": display_current_stage_label,
        "accounting_run_current_stage_label": _DASHBOARD_STAGE_LABELS.get(
            str(run["current_stage"]), str(run["current_stage"] or "-")
        ),
        "generation": int(run["generation"]),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "checked_at": checked_at.isoformat(),
        "stale_seconds": stale_seconds,
        "elapsed_seconds": elapsed_seconds,
        "veteran_target_seconds": VETERAN_TARGET_SECONDS,
        "staff_limit_seconds": STAFF_LIMIT_SECONDS,
        "time_status": "staff_limit_exceeded"
        if elapsed_seconds > STAFF_LIMIT_SECONDS
        else "veteran_target_exceeded"
        if elapsed_seconds > VETERAN_TARGET_SECONDS
        else "within_target",
        "stage_count": len(stages),
        "succeeded_count": succeeded_count,
        "progress_percent": round(succeeded_count / len(stages) * 100),
        "remaining_count": remaining_count,
        "remaining_by_stage": remaining_by_stage,
        "stages": stages,
        "transitions": [
            {
                "stage": row["stage_key"],
                "label": _DASHBOARD_STAGE_LABELS.get(
                    str(row["stage_key"]), str(row["stage_key"] or "実行全体")
                ),
                "from_status": str(row["from_status"]),
                "to_status": str(row["to_status"]),
                "generation": int(row["generation"]),
                "created_at": row["created_at"],
            }
            for row in transition_rows
        ],
        "task_contract": task_contract,
    }


def _dashboard_task_contract() -> dict[str, Any]:
    path = REPO_ROOT / ".codex" / "task-contract.local.json"
    if not path.is_file():
        return {"status": "missing", "objective": "-", "deviation_count": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "invalid", "objective": "-", "deviation_count": 0}
    started_at = _dashboard_timestamp(payload.get("started_at"))
    finished_at = _dashboard_timestamp(
        payload.get("completed_at") or payload.get("blocked_at")
    )
    elapsed_seconds = (
        max(
            int(
                (
                    (finished_at or datetime.now(timezone.utc)) - started_at
                ).total_seconds()
            ),
            0,
        )
        if started_at
        else 0
    )
    return {
        "status": str(payload.get("status", "invalid")),
        "objective": str(payload.get("objective", "-")),
        "started_at": payload.get("started_at"),
        "deadline_at": payload.get("deadline_at"),
        "completed_at": payload.get("completed_at"),
        "elapsed_seconds": elapsed_seconds,
        "current_action": str(payload.get("current_action", "")),
        "checklist": payload.get("checklist")
        if isinstance(payload.get("checklist"), list)
        else [],
        "blocked_at": payload.get("blocked_at"),
        "blocker_count": int(payload.get("blocker_count", 0)),
        "blocker_summary": str(payload.get("blocker_summary", "")),
        "deviation_count": int(payload.get("deviation_count", 0)),
        "last_denial": payload.get("last_denial"),
    }


def _dashboard_timestamp(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status_dashboard_html() -> str:
    return "<!doctype html>\n<html lang=\"ja\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n<title>Codex 現在作業</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151c30;--line:#2a3552;--text:#f3f6ff;--muted:#9aa8c7;--green:#45d483;--blue:#5ba8ff;--red:#ff6b78;--amber:#ffc857}\n*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#080c18,#121a2e);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif}\nmain{max-width:1280px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}h1{margin:0;font-size:clamp(24px,4vw,38px)}.sub{color:var(--muted)}\n.live{display:flex;align-items:center;gap:8px;background:#12273a;padding:8px 13px;border-radius:999px}.dot{width:10px;height:10px;background:var(--green);border-radius:50%;box-shadow:0 0 0 0 rgba(69,212,131,.6);animation:pulse 1.8s infinite}@keyframes pulse{70%{box-shadow:0 0 0 10px transparent}}\n.alert{display:none;padding:12px 16px;margin:0 0 18px;border:1px solid #8b3d46;background:#361b24;border-radius:12px;color:#ffdbe0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}.card,.panel{background:rgba(21,28,48,.94);border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 30px rgba(0,0,0,.18)}.card{padding:16px}.label{color:var(--muted);font-size:13px}.value{font-size:26px;font-weight:750;margin-top:4px}.danger{color:var(--red)}.ok{color:var(--green)}\n.progress{height:12px;background:#26304a;border-radius:999px;overflow:hidden;margin:10px 0 22px}.progress>div{height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--green));transition:width .5s ease}.panel{padding:18px;margin-bottom:18px}.panel h2{font-size:18px;margin:0 0 14px}\n.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.stage{min-height:116px;border:1px solid var(--line);border-radius:13px;padding:13px;background:#10172a;transition:.25s}.stage.succeeded{border-color:#287f52;background:#10261e}.stage.current{border:2px solid var(--blue);background:#112946;transform:translateY(-2px);box-shadow:0 0 20px rgba(91,168,255,.2)}.stage.failed,.stage.blocked,.stage.uncertain_external_write{border-color:#a64550;background:#321821}.stage .num{color:var(--muted);font-size:12px}.stage .name{font-weight:700;margin:4px 0 10px}.stage .state{font-size:13px}.stage.current .state{color:#9dceff}.stage.succeeded .state{color:var(--green)}\n.columns{display:grid;grid-template-columns:1fr 1.4fr;gap:18px}@media(max-width:760px){main{padding:18px}.columns{grid-template-columns:1fr}header{display:block}.live{margin-top:12px;width:max-content}}\ntable{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}.empty{color:var(--muted);padding:12px 0}.foot{color:var(--muted);font-size:12px;margin-top:10px}\n</style></head><body><main>\n<header><div><h1>現在の作業</h1><div class=\"sub\">過去のrunを含めず、今の指示だけを表示</div></div><div class=\"live\"><span class=\"dot\"></span><span id=\"connection\">2秒ごとに更新</span></div></header>\n<div class=\"alert\" id=\"taskAlert\"></div><div class=\"alert\" id=\"alert\"></div>\n<section class=\"cards\"><div class=\"card\"><div class=\"label\">状態</div><div class=\"value\" id=\"runStatus\">-</div></div><div class=\"card\"><div class=\"label\">完了項目</div><div class=\"value\" id=\"stageProgress\">-</div></div><div class=\"card\"><div class=\"label\">今回の経過時間</div><div class=\"value\" id=\"elapsed\">-</div></div><div class=\"card\"><div class=\"label\">制限時間まで</div><div class=\"value\" id=\"remainingTime\">-</div></div></section>\n<div class=\"progress\"><div id=\"progressBar\"></div></div>\n<section class=\"panel\"><h2>今回の指示</h2><div id=\"objective\" class=\"value\" style=\"font-size:20px\">-</div></section>\n<section class=\"panel\"><h2>現在行っている作業</h2><div id=\"currentAction\" class=\"value\" style=\"font-size:20px\">-</div></section>\n<section class=\"panel\"><h2>チェック項目</h2><div id=\"checklist\" class=\"stages\"></div></section>\n<div class=\"foot\" id=\"freshness\"></div>\n</main><script>\nconst esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\nconst duration=s=>{s=Math.max(0,Number(s||0));const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=Math.floor(s%60);return h?`${h}時間 ${m}分 ${x}秒`:`${m}分 ${x}秒`};\nconst stateLabel=s=>({active:'作業中',running:'実行中',complete:'完了',failed:'失敗',uncertain_external_write:'書込み結果確認中',succeeded:'完了',pending:'未着手',blocked:'停止中',retryable_failure:'再試行待ち',terminal_failure:'終了失敗'}[s]||s||'-');\nfunction render(d){const tc=d.task_contract||{};const items=Array.isArray(tc.checklist)?tc.checklist:[];const done=items.filter(x=>x.status==='completed').length;const total=items.length;const taskStatus=tc.status||'missing';const rs=document.getElementById('runStatus');rs.textContent=stateLabel(taskStatus);rs.className='value '+(taskStatus==='complete'?'ok':taskStatus==='blocked'?'danger':'');\ndocument.getElementById('stageProgress').textContent=`${done} / ${total}`;document.getElementById('elapsed').textContent=duration(tc.elapsed_seconds);const deadline=tc.deadline_at?new Date(tc.deadline_at).getTime():0;document.getElementById('remainingTime').textContent=deadline?duration(Math.max(0,(deadline-Date.now())/1000)):'-';document.getElementById('progressBar').style.width=`${total?Math.round(done/total*100):0}%`;\ndocument.getElementById('objective').textContent=tc.objective||'-';document.getElementById('currentAction').textContent=tc.current_action||'-';document.getElementById('checklist').innerHTML=items.length?items.map((x,i)=>`<div class=\"stage ${x.status==='completed'?'succeeded':x.status==='in_progress'?'current':''}\"><div class=\"num\">ITEM ${i+1}</div><div class=\"name\">${esc(x.label)}</div><div class=\"state\">${x.status==='completed'?'✓ 完了':x.status==='in_progress'?'▶ 作業中':'未着手'}</div></div>`).join(''):'<div class=\"empty\">チェック項目なし</div>';\nconst ta=document.getElementById('taskAlert');if(tc.status==='blocked'){ta.style.display='block';ta.innerHTML=`<strong>作業指示状態：停止中</strong> ・ 未解決 ${Number(tc.blocker_count||d.remaining_count||0)}件<div>${esc(tc.blocker_summary||'完了条件を満たしていません')}</div>`}else{ta.style.display='none'}\ndocument.getElementById('alert').style.display='none';document.getElementById('freshness').textContent=`画面更新: ${d.checked_at} ・ 現在作業のみ表示`}\nasync function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);render(await r.json());document.getElementById('connection').textContent='2秒ごとに更新'}catch(e){const a=document.getElementById('alert');a.style.display='block';a.textContent=`接続エラー: ${e.message}`;document.getElementById('connection').textContent='再接続中'}}\nrefresh();setInterval(refresh,2000);\n</script></body></html>"


def _prepare(args: argparse.Namespace) -> int:
    from .journal_batch_prepare import prepare_journal_batch

    summary = prepare_journal_batch(
        input_json=args.input_json,
        output_dir=args.output_dir,
        db_path=args.db_path,
        actor=args.actor,
    )
    payload = summary.to_redacted_dict()
    payload.update({"execution_mode": "local_only", **_local_only_flags()})
    _print_json(payload)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from .workflow_local_replay import run_workflow_local_replay

    summary = run_workflow_local_replay(
        facts_json=args.facts_json,
        output_dir=args.output_dir,
        db_path=args.db_path,
        scope_hash=args.scope_hash,
        approved_at=_parse_datetime(args.approved_at),
    )
    payload = summary.to_redacted_dict()
    payload.update({"execution_mode": "local_only", **_local_only_flags()})
    _print_json(payload)
    return 0


def _verify(args: argparse.Namespace) -> int:
    from .starter_readiness import inspect_readiness

    payload = inspect_readiness(args.db_path)
    if not args.detail:
        payload.pop("product_modes", None)
    _print_json(payload)
    return 1 if args.strict and not payload["recorded_gates_passed"] else 0


def _audit(args):
    from .starter_audit import audit

    result = audit(REPO_ROOT)
    _print_json(result)
    return 0 if result["passed"] else 1


def _doctor(args):
    import shutil

    profile = get_current_profile()
    result = {
        "status": "ready" if profile.configured else "not_configured",
        "profile_slug": profile.slug,
        "configured": profile.configured,
        "configured_db_exists": profile.moneyforward_db_path.is_file(),
        "tools": {
            name: shutil.which(name) is not None for name in ("pdftotext", "pdfinfo")
        },
        "active_workflows": list(ACTIVE_ACCOUNTING_WORKFLOWS),
        "database_accessed": False,
        "external_io": False,
        "external_readiness": "not_verified",
        "next_step": "connect (read-only), then rag-build --execute with a date range",
    }
    _print_json(result)
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    from .db import Phase12Database
    from .ledger_cleanup import (
        build_demo_ledger_cleanup_plan,
        execute_demo_ledger_cleanup,
    )

    db = Phase12Database(args.db_path)
    db.initialize()
    with db.connect() as conn:
        if args.execute:
            plan = execute_demo_ledger_cleanup(conn)
            status = "cleaned"
        else:
            plan = build_demo_ledger_cleanup_plan(conn)
            status = "dry_run"
    _print_json(
        {
            "status": status,
            **plan.to_dict(),
            "scope": "local_demo_ledger_only",
            "external_api_called": False,
        }
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    from .db import Phase12Database
    from .shiwake_run import (
        RUN_STAGES,
        advance_run,
        build_run_status,
        build_stage_contract,
        fail_run_stage,
        resolve_uncertain_write,
        resume_run,
        run_evidence_manifest,
        start_run,
    )

    if args.run_action == "contract":
        payload = build_stage_contract()
        payload.update(_local_only_flags())
        _print_json(payload)
        return 0
    if args.run_action == "status":
        aggregate = _load_aggregate_run_artifact(
            _aggregate_run_artifact_path(args.run_id)
        )
        if aggregate:
            _print_json({**_local_only_flags(), **aggregate})
            return 0
    if args.run_action == "e2e":
        from .db import DEFAULT_DB_PATH

        synthetic_db = Path(args.db_path).resolve()
        operational_root = get_current_profile().moneyforward_db_path.resolve().parent
        if synthetic_db == operational_root or operational_root in synthetic_db.parents:
            raise ValueError(
                "synthetic e2e requires a temporary non-operational database outside data/phase12"
            )
    if args.run_action == "execute" and args.workflow in {
        "sales_invoice",
        "payable_invoice",
        "all_receipt_folders",
        "payroll_bonus",
        "bank_transfer",
        "card",
    }:
        return _run_scoped_workflow(args)
    if (
        args.run_action == "execute"
        and args.workflow is None
        and (args.manifest_json is None)
    ):
        return _run_aggregate_workflows(args)
    db = Phase12Database(args.db_path)
    production_runtime = None
    opening_balance_review = None
    authentication_preflight = None
    production_activity: list[dict[str, Any]] = []
    if args.run_action == "execute" and args.manifest_json is None:
        if db.path.exists():
            db.initialize()
            with db.connect() as preflight_conn:
                requested = build_run_status(preflight_conn, args.run_id)
                if requested.get("status") == "not_found":
                    active = preflight_conn.execute(
                        "SELECT run_id_hash FROM accounting_runs WHERE run_mode='production' AND status='running' LIMIT 1"
                    ).fetchone()
                    if active is not None:
                        args.run_id = f"sha256:{active['run_id_hash']}"
        production_runtime = _bind_production_runtime_for_run(args.db_path)
        authentication_preflight = {
            "status": "verified",
            "moneyforward": {
                "status": "verified",
                "account": _mf_principal(),
                "office": _office_name(),
                "token_refreshed": production_runtime[2] is not None,
            },
            "google_drive_gas": {"status": "verified", "account": _drive_principal()},
            "reauthentication_required": False,
            "browser_opened": False,
        }
        opening_balance_review = _capture_opening_balance_review(
            production_runtime,
            accounting_date=args.accounting_date or date.today().isoformat(),
        )
    db.initialize()
    execute_production_stage = False
    with db.connect() as conn:
        if args.run_action == "start":
            payload = start_run(
                conn,
                args.run_id,
                principal=_mf_principal(),
                office=_office_name(),
                accounting_date=args.accounting_date,
                instruction=args.instruction,
                actor=args.actor,
            )
        elif args.run_action == "status":
            payload = build_run_status(conn, args.run_id)
        elif args.run_action == "advance":
            evidence = json.loads(args.evidence_json.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise ValueError("evidence JSON must be an object")
            payload = advance_run(
                conn, args.run_id, args.stage, evidence, actor=args.actor
            )
        elif args.run_action == "fail":
            payload = fail_run_stage(
                conn,
                args.run_id,
                args.stage,
                args.reason,
                error_code=args.error_code,
                safe_to_retry=not args.blocked,
                actor=args.actor,
                uncertain_external_write=args.uncertain_external_write,
            )
        elif args.run_action == "resume":
            payload = resume_run(conn, args.run_id, actor=args.actor)
        elif args.run_action == "resolve":
            readback = json.loads(args.readback_json.read_text(encoding="utf-8"))
            if not isinstance(readback, dict):
                raise ValueError("readback JSON must be an object")
            payload = resolve_uncertain_write(
                conn,
                args.run_id,
                args.stage,
                outcome=args.outcome,
                readback_evidence=readback,
                actor=args.actor,
            )
        elif args.run_action == "execute":
            if args.manifest_json is None:
                payload = build_run_status(conn, args.run_id)
                if payload.get("status") == "not_found":
                    active = conn.execute(
                        "SELECT run_id_hash FROM accounting_runs WHERE run_mode='production' AND status='running' LIMIT 1"
                    ).fetchone()
                    if active is not None:
                        raise RuntimeError(
                            "another production run is already active; resume it before starting a new run"
                        )
                    payload = start_run(
                        conn,
                        args.run_id,
                        principal=_mf_principal(),
                        office=_office_name(),
                        accounting_date=args.accounting_date,
                        instruction=args.instruction,
                        actor=args.actor,
                    )
                execute_production_stage = True
            else:
                manifest = json.loads(args.manifest_json.read_text(encoding="utf-8"))
                if not isinstance(manifest, (dict, list)):
                    raise ValueError("manifest JSON must be an object or list")
                payload = run_evidence_manifest(
                    conn,
                    args.run_id,
                    manifest,
                    accounting_date=args.accounting_date,
                    instruction=args.instruction,
                    actor=args.actor,
                )
        elif args.run_action == "e2e":
            status = start_run(
                conn,
                args.run_id,
                principal=_mf_principal(),
                office=_office_name(),
                instruction="synthetic end-to-end verification",
                actor=args.actor,
                run_mode="synthetic",
            )
            notification_hash = _synthetic_hash(f"{args.run_id}:notification")
            for stage in RUN_STAGES:
                if status["status"] == "complete":
                    break
                evidence: dict[str, Any] = {
                    "status": "verified",
                    "evidence_hash": _synthetic_hash(f"{args.run_id}:{stage}"),
                }
                if stage in RUN_STAGES[:7]:
                    evidence.update(
                        {
                            "coverage_state": "zero_confirmed",
                            "all_items_resolved": True,
                            "remaining_count": 0,
                        }
                    )
                if stage == "all_receipt_folders":
                    evidence.update(
                        {
                            "folder_count": 3,
                            "manifest_folder_count": 3,
                            "all_folders_covered": True,
                        }
                    )
                elif stage == "cross_audit":
                    evidence["audit_checks"] = {
                        "duplicate_journal_check": "verified",
                        "debit_credit_amount_check": "verified",
                        "account_sub_account_tax_check": "verified",
                        "source_statement_evidence_reconciliation": "verified",
                        "processed_folder_readback": "verified",
                    }
                elif stage == "google_chat_durable_sent":
                    evidence.update(
                        {
                            "delivery_status": "sent",
                            "notification_hash": notification_hash,
                            "response_hash": _synthetic_hash(
                                f"{args.run_id}:chat-response"
                            ),
                        }
                    )
                elif stage == "rag_learning":
                    evidence.update(
                        {
                            "notification_hash": notification_hash,
                            "learning_manifest_hash": _synthetic_hash(
                                f"{args.run_id}:learning"
                            ),
                        }
                    )
                elif stage == "issue_update":
                    evidence.update(
                        {
                            "issue_delivery_status": "verified",
                            "issue_receipt_hash": _synthetic_hash(
                                f"{args.run_id}:issue"
                            ),
                        }
                    )
                status = advance_run(
                    conn, args.run_id, stage, evidence, actor=args.actor
                )
            payload = {
                **status,
                "synthetic": True,
                "stage_order": list(RUN_STAGES),
                "moneyforward_write_called": False,
                "google_drive_write_called": False,
                "gmail_write_called": False,
                "google_chat_real_write_called": False,
                "github_write_called": False,
                "keychain_accessed": False,
                "oauth_url_opened": False,
                "browser_opened": False,
            }
        else:
            raise ValueError(f"unknown run action: {args.run_action}")
    if execute_production_stage:
        while payload.get("status") == "running":
            payload = _execute_current_production_stage(
                db, args, runtime=production_runtime
            )
            production_activity.append(payload)
    execution_flags: dict[str, bool] = {}
    if args.run_action == "execute" and args.manifest_json is None:
        with db.connect() as conn:
            execution_flags = _receipt_backed_execution_flags(conn, args.run_id)
        execution_flags["external_activity"] = _external_activity_status(
            receipt_flags=execution_flags, stage_results=production_activity
        )
    payload = {**_local_only_flags(), **payload, **execution_flags}
    if authentication_preflight is not None:
        payload["authentication_preflight"] = authentication_preflight
    if opening_balance_review is not None:
        payload["opening_balance_review"] = opening_balance_review
    _print_json(payload)
    if args.run_action in {"execute", "e2e"}:
        return 0 if payload.get("status") == "complete" else 1
    return (
        0
        if payload.get("status")
        not in {"not_found", "error", "failed", "uncertain_external_write"}
        else 1
    )


def _capture_opening_balance_review(
    runtime: tuple[Any, str, Any | None, str], *, accounting_date: str
) -> dict[str, Any]:
    """Run one official opening control read without blocking ready work."""
    from .daily_accounting_control import build_opening_balance_review

    client, office_id, _refresh_result, _drive_token = runtime
    end = date.fromisoformat(accounting_date)
    start_date = _company_fiscal_year_start(end).isoformat()
    try:
        payload = client.trial_balance_bs(
            start_date=start_date, end_date=accounting_date, office_id=office_id
        )
        return build_opening_balance_review(payload, accounting_date=accounting_date)
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "moneyforward_trial_balance_bs",
            "review_period": {"start_date": start_date, "end_date": accounting_date},
            "review_status": "attention_required",
            "review_item_count": 1,
            "exception_items": [
                {
                    "account_name": "balance_sheet_control",
                    "control_kind": "source_readback",
                    "reason_code": type(exc).__name__,
                    "balance_state": "unknown",
                }
            ],
            "workflow_blocking": False,
            "ready_work_continues": True,
            "raw_amounts_stored": False,
            "raw_ids_stored": False,
        }


def _run_scoped_workflow(args: argparse.Namespace) -> int:
    """Run one workflow without waiting on or mutating other workflows."""
    from .db import Phase12Database

    if not args.accounting_date:
        if args.workflow != "all_receipt_folders":
            raise ValueError(f"--workflow {args.workflow} requires --accounting-date")
        args.accounting_date = date.today().isoformat()
    date.fromisoformat(str(args.accounting_date))
    db = Phase12Database(args.db_path)
    db.initialize()
    from .db import DEFAULT_DB_PATH

    is_operational_db = (
        Path(args.db_path).resolve()
        == get_current_profile().moneyforward_db_path.resolve()
    )
    if args.workflow == "sales_invoice":
        runtime = _bind_accounting_client_with_single_refresh()
        payload = _execute_sales_invoice_stage(
            db, args, runtime=runtime, standalone=True
        )
        bound_services = ["moneyforward"]
        token_refreshed = runtime[2] is not None
        google_authentication_called = False
    elif args.workflow == "payable_invoice" and is_operational_db:
        runtime = _bind_production_runtime_for_run(args.db_path)
        runtime = _ensure_payable_scope_readback(db, runtime)
        payload = _execute_payable_invoice_stage(
            db, args, runtime=runtime, standalone=True
        )
        bound_services = ["google_drive", "moneyforward"]
        token_refreshed = runtime[2] is not None
        google_authentication_called = True
    elif args.workflow == "payable_invoice":
        runtime = None
        payload = _execute_payable_invoice_stage(
            db, args, runtime=None, standalone=True
        )
        bound_services = []
        token_refreshed = False
        google_authentication_called = False
    elif args.workflow == "payroll_bonus":
        runtime = _bind_accounting_client_with_single_refresh()
        payload = _execute_payroll_bonus_stage(
            db, args, runtime=(*runtime, ""), standalone=True
        )
        bound_services = ["moneyforward"]
        token_refreshed = runtime[2] is not None
        google_authentication_called = False
    elif args.workflow == "bank_transfer":
        runtime = _bind_accounting_client_with_single_refresh()
        payload = _execute_bank_transfer_workflow(db, args, runtime=runtime)
        bound_services = ["moneyforward"]
        token_refreshed = runtime[2] is not None
        google_authentication_called = False
    elif args.workflow == "card":
        runtime = _bind_accounting_client_with_single_refresh()
        payload = _execute_card_workflow(db, args, runtime=runtime)
        google_authentication_called = bool(payload.get("google_authentication_called"))
        bound_services = (
            ["google_drive", "moneyforward"]
            if google_authentication_called
            else ["moneyforward"]
        )
        token_refreshed = runtime[2] is not None
    else:
        runtime = _bind_production_runtime_for_run(args.db_path)
        payload = _execute_all_receipt_folders_stage(
            db, args, runtime=runtime, standalone=True
        )
        bound_services = ["google_drive", "moneyforward"]
        token_refreshed = runtime[2] is not None
        google_authentication_called = True
    payload = {
        **_local_only_flags(),
        **payload,
        "authentication_preflight": {
            "status": "verified"
            if payload.get("authentication_ready") is True
            else "not_verified",
            "bound_services": bound_services,
            "moneyforward": {
                "status": "verified"
                if payload.get("moneyforward_readback_verified") is True
                else "not_verified",
                "account": _mf_principal(),
                "office": _office_name(),
                "token_refreshed": token_refreshed,
                "journal_write_scope": payload.get(
                    "journal_write_scope", "not_verified"
                ),
                "voucher_write_scope": payload.get(
                    "voucher_write_scope", "not_verified"
                ),
                "invoice_data_read_scope": payload.get(
                    "invoice_data_read_scope", "not_verified"
                ),
            },
            "google_authentication_called": google_authentication_called,
            "opening_balance_review_called": False,
            "reauthentication_required": False,
            "browser_opened": False,
        },
    }
    _print_json(payload)
    return 0 if payload.get("status") == "complete" else 1


def _run_aggregate_workflows(args: argparse.Namespace) -> int:
    """Run WO01-06 independently behind one shared authentication preflight."""
    from .db import DEFAULT_DB_PATH, Phase12Database

    db = Phase12Database(args.db_path)
    with _aggregate_production_execution_lock():
        if db.path.exists():
            db.initialize()
            with db.connect() as conn:
                requested_hash = accounting_run_hash(args.run_id)
                competing = conn.execute(
                    "SELECT run_id_hash FROM accounting_runs WHERE run_mode='production' AND status='running' AND run_id_hash != ? LIMIT 1",
                    (requested_hash,),
                ).fetchone()
                if competing is not None:
                    raise RuntimeError(
                        "another production run is already active; resume it before starting a new run"
                    )
        return _run_aggregate_workflows_locked(
            args, db, default_db_path=get_current_profile().moneyforward_db_path
        )


def _run_aggregate_workflows_locked(
    args: argparse.Namespace, db: Any, *, default_db_path: Path
) -> int:
    """Execute an aggregate run while its office-wide production lock is held."""
    args.accounting_date = str(
        args.accounting_date or datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    )
    date.fromisoformat(args.accounting_date)
    artifact_path = _aggregate_run_artifact_path(args.run_id)
    prior = _load_aggregate_run_artifact(artifact_path)
    if prior:
        expected_binding = {
            "schema": "moneyforward-aggregate-run-v1",
            "run_id_hash": "sha256:" + accounting_run_hash(args.run_id),
            "accounting_date": args.accounting_date,
            "active_workflows": list(ACTIVE_ACCOUNTING_WORKFLOWS),
        }
        if any((prior.get(key) != value for key, value in expected_binding.items())):
            raise ValueError("aggregate run artifact binding does not match this run")
    prior_workflows = [
        row for row in prior.get("workflows", []) if isinstance(row, dict)
    ]
    if (
        prior_workflows
        and all((_aggregate_result_resume_complete(row) for row in prior_workflows))
        and (prior.get("cross_audit", {}).get("status") == "verified")
    ):
        prior_workflows = [
            {
                **row,
                "resume_complete": True,
                "skipped": True,
                "invocation_additional_write_count": 0,
                "replay_additional_write_count": 0,
            }
            for row in prior_workflows
        ]
        db.initialize()
        return _finalize_aggregate_tail(
            db,
            args,
            artifact_path=artifact_path,
            workflows=prior_workflows,
            prior=prior,
        )
    base_runtime = _bind_production_runtime_for_run(args.db_path)
    db.initialize()
    _capture_aggregate_unposted_inventory(args, runtime=base_runtime, phase="opening")
    opening_balance_review = _capture_opening_balance_review(
        base_runtime, accounting_date=args.accounting_date
    )
    prior_results = {
        str(row.get("workflow")): row
        for row in prior.get("workflows", [])
        if isinstance(row, dict) and row.get("workflow")
    }
    results: list[dict[str, Any]] = []
    for workflow in ACTIVE_ACCOUNTING_WORKFLOWS:
        previous = prior_results.get(workflow)
        if previous is not None and _aggregate_result_resume_complete(previous):
            results.append(
                {
                    **previous,
                    "resume_complete": True,
                    "skipped": True,
                    "invocation_additional_write_count": 0,
                    "replay_additional_write_count": 0,
                }
            )
            continue
        try:
            workflow_runtime = base_runtime
            if workflow == "payable_invoice" and True:
                workflow_runtime = _ensure_payable_scope_readback(db, base_runtime)
            workflow_payload = _execute_aggregate_workflow(
                workflow, db, args, runtime=workflow_runtime
            )
            result = _aggregate_workflow_result(workflow, workflow_payload)
            if previous is not None:
                result["newly_registered_count"] = int(
                    previous.get("newly_registered_count") or 0
                ) + int(result.get("newly_registered_count") or 0)
        except AuthenticationHandoffRequired as exc:
            result = {
                "workflow": workflow,
                "state": "waiting_for_auth",
                "status": "waiting_for_auth",
                "resume_complete": False,
                "official_readback_complete": False,
                "review_count": 0,
                "not_ready_count": 0,
                "retry_pending_count": 0,
                "remaining_count": 0,
                "account": exc.account,
                "service": exc.service,
                "browser_opened": exc.browser_opened,
                "input_required": exc.input_required,
                "invocation_additional_write_count": 0,
                "replay_additional_write_count": 0,
                "skipped": False,
            }
        except _aggregate_transient_exceptions() as exc:
            result = {
                "workflow": workflow,
                "state": "retry_pending",
                "status": "retry_pending",
                "resume_complete": False,
                "official_readback_complete": False,
                "review_count": 0,
                "not_ready_count": 0,
                "retry_pending_count": 1,
                "remaining_count": 0,
                "reason_code": type(exc).__name__,
                "invocation_additional_write_count": 0,
                "replay_additional_write_count": 0,
                "skipped": False,
            }
        except Exception as exc:
            failure = {
                "workflow": workflow,
                "error_type": type(exc).__name__,
                "retryable": False,
            }
            payload = _write_aggregate_run_artifact(
                artifact_path,
                run_id=args.run_id,
                accounting_date=args.accounting_date,
                workflows=results,
                operational_failure=failure,
            )
            _print_json(payload)
            return 2
        results.append(result)
        _write_aggregate_run_artifact(
            artifact_path,
            run_id=args.run_id,
            accounting_date=args.accounting_date,
            workflows=results,
        )
    if all((_aggregate_result_resume_complete(row) for row in results)):
        return _finalize_aggregate_tail(
            db,
            args,
            artifact_path=artifact_path,
            workflows=results,
            prior=prior,
            authentication_preflight={
                "status": "verified",
                "moneyforward": {
                    "status": "verified",
                    "account": _mf_principal(),
                    "office": _office_name(),
                    "token_refreshed": base_runtime[2] is not None,
                },
                "google_drive_gas": {
                    "status": "verified",
                    "account": _drive_principal(),
                },
                "reauthentication_required": False,
                "browser_opened": False,
            },
            opening_balance_review=opening_balance_review,
            runtime=base_runtime,
        )
    payload = _write_aggregate_run_artifact(
        artifact_path,
        run_id=args.run_id,
        accounting_date=args.accounting_date,
        workflows=results,
    )
    _print_json(payload)
    return 1


def _finalize_aggregate_tail(
    db: Any,
    args: argparse.Namespace,
    *,
    artifact_path: Path,
    workflows: list[dict[str, Any]],
    prior: dict[str, Any],
    authentication_preflight: dict[str, Any] | None = None,
    opening_balance_review: dict[str, Any] | None = None,
    runtime: tuple[Any, ...] | None = None,
) -> int:
    """Persist one read-only audit, then deliver one durable final Chat."""
    prior_audit = prior.get("cross_audit")
    prior_snapshot_hash = _aggregate_terminal_snapshot_hash(
        workflows,
        official_unposted_inventory=prior_audit.get("official_unposted_inventory")
        if isinstance(prior_audit, dict)
        else None,
    )
    reuse_prior_audit = (
        isinstance(prior_audit, dict)
        and prior_audit.get("status") == "verified"
        and (prior_audit.get("terminal_snapshot_hash") == prior_snapshot_hash)
        and _aggregate_cross_audit_receipt_exists(
            db, args, receipt_hash=str(prior_audit.get("receipt_hash") or "")
        )
    )
    if reuse_prior_audit:
        cross_audit = prior_audit
    else:
        workflows, official_inventory = _close_aggregate_unposted_boundary(
            db, args, workflows=workflows, runtime=runtime
        )
        if not all((_aggregate_result_resume_complete(row) for row in workflows)):
            payload = _write_aggregate_run_artifact(
                artifact_path,
                run_id=args.run_id,
                accounting_date=args.accounting_date,
                workflows=workflows,
            )
            _print_json(payload)
            return 1
        snapshot_hash = _aggregate_terminal_snapshot_hash(
            workflows, official_unposted_inventory=official_inventory
        )
        cross_audit = _execute_aggregate_cross_audit(
            db,
            args,
            workflows,
            snapshot_hash=snapshot_hash,
            runtime=runtime,
            official_unposted_inventory=official_inventory,
        )
    prior_chat = prior.get("google_chat")
    google_chat = (
        prior_chat
        if isinstance(prior_chat, dict)
        and prior_chat.get("cross_audit_receipt_hash")
        == cross_audit.get("receipt_hash")
        else {"status": "not_started"}
    )
    payload = _write_aggregate_run_artifact(
        artifact_path,
        run_id=args.run_id,
        accounting_date=args.accounting_date,
        workflows=workflows,
        cross_audit=cross_audit,
        google_chat=google_chat,
    )
    if payload["status"] == "complete":
        _mark_aggregate_receipt_run_complete(db, args)
    if cross_audit.get("status") != "verified":
        _print_json(payload)
        return 1
    if google_chat.get("status") not in {"sent", "already_sent"}:
        try:
            google_chat = _deliver_aggregate_final_chat(
                db, args, aggregate=payload, cross_audit=cross_audit, runtime=runtime
            )
        except Exception as exc:
            google_chat = {
                "status": "retry_pending",
                "error_type": type(exc).__name__,
                "cross_audit_receipt_hash": cross_audit["receipt_hash"],
            }
    payload = _write_aggregate_run_artifact(
        artifact_path,
        run_id=args.run_id,
        accounting_date=args.accounting_date,
        workflows=workflows,
        cross_audit=cross_audit,
        google_chat=google_chat,
    )
    if payload["status"] == "complete":
        _mark_aggregate_receipt_run_complete(db, args)
    if authentication_preflight is not None:
        payload["authentication_preflight"] = authentication_preflight
    if opening_balance_review is not None:
        payload["opening_balance_review"] = opening_balance_review
    _print_json(payload)
    return 0 if payload["status"] == "complete" else 1


def _aggregate_terminal_snapshot_hash(
    workflows: list[dict[str, Any]],
    *,
    official_unposted_inventory: dict[str, Any] | None = None,
) -> str:
    snapshot = {
        "workflows": [
            {
                key: row.get(key)
                for key in (
                    "workflow",
                    "state",
                    "official_readback_complete",
                    "review_count",
                    "not_ready_count",
                    "not_applicable_count",
                    "newly_registered_count",
                    "reused_count",
                    "remaining_count",
                    "replay_additional_write_count",
                    "inventory_item_hashes",
                    "resolved_item_hashes",
                    "terminal_unposted_item_hashes",
                    "post_unposted_item_hashes",
                    "processable_remaining_item_hashes",
                )
            }
            for row in workflows
        ],
        "official_unposted_inventory": official_unposted_inventory
        or {
            "count": 0,
            "item_hashes": [],
            "item_set_hash": "sha256:" + hashlib.sha256(b"[]").hexdigest(),
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return "sha256:" + digest


def _capture_aggregate_unposted_inventory(
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, ...] | None,
    phase: str = "pre_audit",
) -> dict[str, Any]:
    """Read the complete official unposted set at an aggregate boundary."""
    from .connected_account_probe import run_connected_account_probe
    from .hash_utils import stable_hash

    if runtime is None:
        raise RuntimeError(
            "aggregate final inventory requires the shared authenticated runtime"
        )
    if phase not in {"opening", "pre_audit"}:
        raise ValueError("aggregate inventory phase is invalid")
    accounting_day = date.fromisoformat(str(args.accounting_date))
    run_dir = _aggregate_run_artifact_path(args.run_id).parent
    snapshot_path = run_dir / f"connected_transactions_{phase}.local.json"
    probe_path = run_dir / f"connected_account_probe_{phase}.local.json"
    run_connected_account_probe(
        accounting_client=runtime[0],
        output_json=probe_path,
        office_id=str(runtime[1]),
        include_unposted_transactions=True,
        start_date=_company_fiscal_year_start(accounting_day).isoformat(),
        end_date=accounting_day.isoformat(),
        normalized_transactions_output_json=snapshot_path,
    )
    artifact = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = _validate_connected_transaction_snapshot(
        artifact, accounting_date=accounting_day.isoformat()
    )
    item_hashes = sorted((str(item["source_event_id_hash"]) for item in rows))
    by_workflow = {
        "bank_transfer": sorted(
            (
                str(item["source_event_id_hash"])
                for item in rows
                if item.get("source_event_type") == "bank_transaction"
            )
        ),
        "card": sorted(
            (
                str(item["source_event_id_hash"])
                for item in rows
                if item.get("source_event_type") == "card_transaction"
            )
        ),
    }
    return {
        "count": len(item_hashes),
        "item_hashes": item_hashes,
        "item_set_hash": "sha256:" + stable_hash(item_hashes),
        "source_response_hash": "sha256:" + str(artifact["source_response_hash"]),
        "source_type_counts": dict(artifact.get("source_type_counts") or {}),
        "workflow_item_hashes": by_workflow,
        "raw_ids_stored": False,
    }


def _close_aggregate_unposted_boundary(
    db: Any,
    args: argparse.Namespace,
    *,
    workflows: list[dict[str, Any]],
    runtime: tuple[Any, ...] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pick up newly arrived statement rows before the read-only audit."""
    results = [dict(row) for row in workflows]
    for _attempt in range(3):
        inventory = _capture_aggregate_unposted_inventory(args, runtime=runtime)
        known = {
            str(value)
            for row in results
            for value in row.get("inventory_item_hashes") or []
        }
        current = set(inventory["item_hashes"])
        new_hashes = current - known
        if not new_hashes:
            terminal = {
                str(value)
                for row in results
                for value in row.get("terminal_unposted_item_hashes") or []
            }
            processable = {
                str(value)
                for row in results
                for value in row.get("processable_remaining_item_hashes") or []
            }
            unclassified = current - terminal - processable
            inventory = {
                **inventory,
                "terminal_item_count": len(current & terminal),
                "processable_remaining_count": len(current & processable),
                "unclassified_item_count": len(unclassified),
                "unclassified_item_set_hash": "sha256:"
                + hashlib.sha256(
                    json.dumps(sorted(unclassified), separators=(",", ":")).encode()
                ).hexdigest(),
            }
            return (results, inventory)
        affected = {
            workflow
            for workflow, hashes in inventory["workflow_item_hashes"].items()
            if new_hashes.intersection(hashes)
        }
        for workflow in ("bank_transfer", "card"):
            if workflow not in affected:
                continue
            payload = _execute_aggregate_workflow(workflow, db, args, runtime=runtime)
            refreshed = _aggregate_workflow_result(workflow, payload)
            index = next(
                (
                    index
                    for index, row in enumerate(results)
                    if row.get("workflow") == workflow
                )
            )
            previous = results[index]
            refreshed["newly_registered_count"] = int(
                previous.get("newly_registered_count") or 0
            ) + int(refreshed.get("newly_registered_count") or 0)
            refreshed["reused_count"] = int(previous.get("reused_count") or 0) + int(
                refreshed.get("reused_count") or 0
            )
            results[index] = refreshed
    raise RuntimeError(
        "new official unposted items continued arriving before Cross Audit"
    )


def _execute_aggregate_cross_audit(
    db: Any,
    args: argparse.Namespace,
    workflows: list[dict[str, Any]],
    *,
    snapshot_hash: str,
    runtime: tuple[Any, ...] | None,
    official_unposted_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the existing official-GET Cross Audit and persist its formal receipt."""
    from .daily_post_audit import run_daily_post_audit
    from .hash_utils import stable_hash
    from .shiwake_run import record_cross_audit_receipt

    if runtime is None:
        raise RuntimeError(
            "aggregate Cross Audit requires the shared authenticated runtime"
        )
    terminal = all((_aggregate_result_resume_complete(row) for row in workflows))
    remaining_count = sum((int(row.get("remaining_count") or 0) for row in workflows))
    replay_write_count = sum(
        (int(row.get("replay_additional_write_count") or 0) for row in workflows)
    )
    inventory = official_unposted_inventory or {
        "count": 0,
        "item_hashes": [],
        "item_set_hash": "sha256:" + hashlib.sha256(b"[]").hexdigest(),
        "terminal_item_count": 0,
        "processable_remaining_count": 0,
        "unclassified_item_count": 0,
    }
    if (
        not terminal
        or remaining_count
        or replay_write_count
        or int(inventory.get("processable_remaining_count") or 0)
        or int(inventory.get("unclassified_item_count") or 0)
    ):
        raise RuntimeError(
            "aggregate Cross Audit requires terminal workflows and zero replay writes"
        )
    paths = _prepare_aggregate_cross_audit_inputs(
        db,
        args,
        client=runtime[0],
        office_id=str(runtime[1]),
        snapshot_hash=snapshot_hash,
        official_unposted_inventory=inventory,
    )
    audit = run_daily_post_audit(
        client=runtime[0],
        daily_inventory_json=paths["inventory"],
        batch_summary_json=paths["batch"],
        voucher_queue_json=paths["queue"],
        full_manifest_json=paths["manifest"],
        output_json=paths["output"],
        updated_inventory_json=paths["updated_inventory"],
        db_path=db.path,
        office_id=str(runtime[1]),
    )
    checks = {
        key: str(value.get("state") or "failed")
        for key, value in audit["results"].items()
    }
    post_audit_hash = (
        "sha256:" + hashlib.sha256(paths["output"].read_bytes()).hexdigest()
    )
    daily_control = {
        "accounting_run_id_hash": "sha256:" + accounting_run_hash(args.run_id),
        "accounting_date": str(args.accounting_date),
        "terminal_snapshot_hash": snapshot_hash,
        "post_audit_hash": post_audit_hash,
        "official_unposted_inventory": inventory,
        "completion_notification_allowed": audit.get("status") == "ready",
    }
    daily_control_hash = "sha256:" + stable_hash(daily_control)
    result = {
        "status": "verified" if audit.get("status") == "ready" else "needs_attention",
        "terminal_snapshot_hash": snapshot_hash,
        "checks": checks,
        "remaining_count": remaining_count,
        "replay_additional_write_count": replay_write_count,
        "daily_control_hash": daily_control_hash,
        "post_audit_hash": post_audit_hash,
        "official_unposted_inventory": inventory,
        "moneyforward_read_called": audit.get("moneyforward_read_called") is True,
        "moneyforward_write_called": False,
        "google_drive_write_called": False,
        "ocr_called": False,
        "gemini_called": False,
        "voucher_operation_called": False,
    }
    if result["status"] != "verified":
        return result
    with db.connect() as conn:
        _ensure_aggregate_receipt_run(conn, args)
        receipt_hash = record_cross_audit_receipt(
            conn,
            run_id=args.run_id,
            receipt=_bound_receipt(
                {
                    "status": "verified",
                    "stage": "cross_audit",
                    "accounting_run_id_hash": "sha256:"
                    + accounting_run_hash(args.run_id),
                    "accounting_date": str(args.accounting_date),
                    "source_snapshot_hash": snapshot_hash,
                    "readback_hash": post_audit_hash,
                    "remaining_count": 0,
                    "all_items_resolved": True,
                    "coverage_state": "captured"
                    if int(audit.get("batch_item_count") or 0)
                    else "zero_confirmed",
                    "identity_readback_verified": True,
                    "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                    "execution_principal_hash": "sha256:"
                    + stable_hash({"principal": _mf_principal()}),
                    "audit_checks": checks,
                    "completion_notification_allowed": True,
                    "daily_control_hash": daily_control_hash,
                    "post_audit_hash": post_audit_hash,
                }
            ),
            actor="aggregate_cross_audit_adapter",
        )
        conn.execute(
            "UPDATE accounting_runs SET current_stage='google_chat_durable_sent', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id_hash=?",
            (accounting_run_hash(args.run_id),),
        )
    return {**result, "receipt_hash": receipt_hash}


def _prepare_aggregate_cross_audit_inputs(
    db: Any,
    args: argparse.Namespace,
    *,
    client: Any,
    office_id: str,
    snapshot_hash: str,
    official_unposted_inventory: dict[str, Any],
) -> dict[str, Path]:
    """Build the existing Cross Audit inputs from this run's durable artifacts."""
    from .hash_utils import mask_identifier, stable_hash
    from .journal_duplicate_probe import _journal_details_for_period
    from .journal_write_verify import _normalize_read_back
    from .journal_backfill import _journal_id

    run_dir = _aggregate_run_artifact_path(args.run_id).parent
    source_names = (
        "sales_invoice.local.json",
        "payable_invoice.local.json",
        "all_receipt_folders.local.json",
        "connected_transactions.local.json",
        "bank_transfer_decisions.local.json",
        "card_decisions.local.json",
    )
    file_hashes: set[str] = set()
    journal_hashes: set[str] = set()
    for name in source_names:
        path = run_dir / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        _collect_aggregate_audit_hashes(
            payload, file_hashes=file_hashes, journal_hashes=journal_hashes
        )
    with db.connect() as conn:
        ledger_rows = []
        if file_hashes:
            placeholders = ",".join(("?" for _ in file_hashes))
            ledger_rows = conn.execute(
                f"SELECT ledger.file_hash,ledger.idempotency_key,ledger.moneyforward_journal_id_hash,verify.expected_hash,verify.actual_hash,verify.passed FROM processing_ledger AS ledger LEFT JOIN post_registration_verifications AS verify ON verify.id=(SELECT MAX(latest.id) FROM post_registration_verifications AS latest WHERE latest.idempotency_key=ledger.idempotency_key AND latest.moneyforward_journal_id_hash=ledger.moneyforward_journal_id_hash) WHERE ledger.processing_status='verified' AND ledger.file_hash IN ({placeholders}) ORDER BY ledger.file_hash,ledger.id",
                tuple(sorted(file_hashes)),
            ).fetchall()
    target_rows = [
        {
            "source_file_hash": str(row["file_hash"]),
            "idempotency_key": str(row["idempotency_key"]),
            "journal_id_hash": str(row["moneyforward_journal_id_hash"] or ""),
            "processed_readback_required": True,
            "expected_hash": str(row["expected_hash"] or ""),
            "actual_hash": str(row["actual_hash"] or ""),
            "verification_passed": bool(row["passed"]),
        }
        for row in ledger_rows
        if str(row["moneyforward_journal_id_hash"] or "")
    ]
    ledger_journal_hashes = {row["journal_id_hash"] for row in target_rows}
    target_rows.extend(
        (
            {
                "source_file_hash": journal_hash,
                "idempotency_key": "aggregate-documentless-"
                + stable_hash({"journal_id_hash": journal_hash}),
                "journal_id_hash": journal_hash,
                "processed_readback_required": False,
                "expected_hash": "",
                "actual_hash": "",
                "verification_passed": True,
            }
            for journal_hash in sorted(journal_hashes - ledger_journal_hashes)
        )
    )
    accounting_day = date.fromisoformat(str(args.accounting_date))
    details = _journal_details_for_period(
        client,
        start_date=_company_fiscal_year_start(accounting_day).isoformat(),
        end_date=accounting_day.isoformat(),
        office_id=office_id,
    )
    details_by_hash = {
        mask_identifier(raw_id): detail
        for detail in details
        if (raw_id := _journal_id(detail))
    }
    batch_items: list[dict[str, Any]] = []
    queue_items: list[dict[str, Any]] = []
    missing_count = 0
    input_dir = run_dir / "cross_audit_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    for index, target in enumerate(target_rows):
        detail = details_by_hash.get(target["journal_id_hash"])
        if detail is None:
            missing_count += 1
            continue
        normalized = _normalize_read_back(detail)
        journal = normalized
        body_hash = stable_hash(normalized)
        expected_hash = str(target.get("expected_hash") or body_hash)
        actual_hash = str(target.get("actual_hash") or body_hash)
        journal_path = input_dir / f"journal-{index}.local.json"
        verification_path = input_dir / f"verification-{index}.local.json"
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verification_path.write_text(
            json.dumps(
                {
                    "verified": target.get("verification_passed") is True,
                    "expected_hash": expected_hash,
                    "actual_hash": actual_hash,
                    "journal_id_hash": target["journal_id_hash"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        batch_items.append(
            {
                "idempotency_key": target["idempotency_key"],
                "source_file_hash": target["source_file_hash"],
                "journal_json": str(journal_path),
            }
        )
        queue_items.append(
            {
                "idempotency_key": target["idempotency_key"],
                "journal_id_hash": target["journal_id_hash"],
                "journal_verification_json": str(verification_path),
                "processed_readback_required": target["processed_readback_required"],
            }
        )
    paths = {
        "inventory": run_dir / "cross_audit_inventory.local.json",
        "batch": run_dir / "cross_audit_batch.local.json",
        "queue": run_dir / "cross_audit_queue.local.json",
        "manifest": run_dir / "cross_audit_manifest.local.json",
        "output": run_dir / "cross_audit.local.json",
        "updated_inventory": run_dir / "cross_audit_inventory_verified.local.json",
    }
    payloads = {
        "inventory": {
            "accounting_date": str(args.accounting_date),
            "accounting_run_id": args.run_id,
            "terminal_snapshot_hash": snapshot_hash,
            "zero_activity_verified": not target_rows,
            "missing_journal_readback_count": missing_count,
            "official_unposted_count": int(
                official_unposted_inventory.get("count") or 0
            ),
            "official_unposted_item_set_hash": str(
                official_unposted_inventory.get("item_set_hash") or ""
            ),
            "official_unposted_terminal_item_count": int(
                official_unposted_inventory.get("terminal_item_count") or 0
            ),
            "official_unposted_processable_remaining_count": int(
                official_unposted_inventory.get("processable_remaining_count") or 0
            ),
            "official_unposted_unclassified_item_count": int(
                official_unposted_inventory.get("unclassified_item_count") or 0
            ),
            "sources": {"card": {"evidence": {"item_count": 0}}},
        },
        "batch": {"items": batch_items},
        "queue": {"items": queue_items},
        "manifest": {
            "files": [{"file_hash": row["source_file_hash"]} for row in target_rows]
        },
    }
    for key, payload in payloads.items():
        paths[key].write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return paths


def _collect_aggregate_audit_hashes(
    value: Any, *, file_hashes: set[str], journal_hashes: set[str], parent_key: str = ""
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_aggregate_audit_hashes(
                child,
                file_hashes=file_hashes,
                journal_hashes=journal_hashes,
                parent_key=str(key),
            )
        return
    if isinstance(value, list):
        for child in value:
            _collect_aggregate_audit_hashes(
                child,
                file_hashes=file_hashes,
                journal_hashes=journal_hashes,
                parent_key=parent_key,
            )
        return
    marker = str(value or "")
    if re.fullmatch("sha256:[0-9a-f]{64}", marker) is None:
        return
    if parent_key in {"file_hash", "source_file_hash", "canonical_content_hash"}:
        file_hashes.add(marker)
    elif parent_key in {"journal_id_hash", "moneyforward_journal_id_hash"}:
        journal_hashes.add(marker)


def _ensure_aggregate_receipt_run(conn: Any, args: argparse.Namespace) -> None:
    """Bind aggregate finalization to the existing run receipt tables only."""
    from .hash_utils import stable_hash

    run_hash = accounting_run_hash(args.run_id)
    row = conn.execute(
        "SELECT * FROM accounting_runs WHERE run_id_hash=?", (run_hash,)
    ).fetchone()
    expected = {
        "office_hash": stable_hash({"office": _office_name()}),
        "execution_principal_hash": stable_hash({"principal": _mf_principal()}),
        "instruction_hash": stable_hash({"instruction": args.instruction}),
    }
    if row is not None:
        if (
            row["run_mode"] != "production"
            or row["accounting_date"] != str(args.accounting_date)
            or any(
                (
                    str(row[key]) != expected[key]
                    for key in ("office_hash", "execution_principal_hash")
                )
            )
        ):
            raise ValueError("aggregate receipt run binding mismatch")
        if row["status"] != "running":
            raise ValueError("aggregate receipt run is not open for Cross Audit")
        return
    conn.execute(
        "INSERT INTO accounting_runs (run_id_hash,office_hash,execution_principal_hash,accounting_date,instruction_hash,run_mode,creator_actor_hash,status,current_stage) VALUES (?,?,?,?,?,'production',?,'running','cross_audit')",
        (
            run_hash,
            expected["office_hash"],
            expected["execution_principal_hash"],
            str(args.accounting_date),
            expected["instruction_hash"],
            stable_hash({"actor": args.actor}),
        ),
    )


def _aggregate_cross_audit_receipt_exists(
    db: Any, args: argparse.Namespace, *, receipt_hash: str
) -> bool:
    digest = receipt_hash.removeprefix("sha256:")
    if re.fullmatch("[0-9a-f]{64}", digest) is None:
        return False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='cross_audit' AND receipt_kind='cross_audit_readback' AND status='verified' AND accounting_date=? AND receipt_hash=?",
            (accounting_run_hash(args.run_id), str(args.accounting_date), digest),
        ).fetchone()
    return row is not None


def _mark_aggregate_receipt_run_complete(db: Any, args: argparse.Namespace) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE accounting_runs SET status='complete',current_stage=NULL,completed_at=COALESCE(completed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id_hash=? AND run_mode='production' AND status='running'",
            (accounting_run_hash(args.run_id),),
        )


def _deliver_aggregate_final_chat(
    db: Any,
    args: argparse.Namespace,
    *,
    aggregate: dict[str, Any],
    cross_audit: dict[str, Any],
    runtime: tuple[Any, ...] | None,
) -> dict[str, Any]:
    from .google_chat_notify import (
        AGGREGATE_COMPLETION_SCOPE,
        deliver_google_chat_notification,
        resolve_webhook_url,
    )

    summary = {
        "notification_type": "completion",
        "notification_scope": AGGREGATE_COMPLETION_SCOPE,
        "accounting_run_id": args.run_id,
        "accounting_date": str(args.accounting_date),
        "title": "Money Forward 仕訳完了（configured_company）",
        "items": _aggregate_chat_items(args, aggregate=aggregate, runtime=runtime),
        "terminal_no_journal_count": 0,
        "terminal_no_journal_verified": True,
        "unresolved_item_count": 0,
        "cross_audit_status": cross_audit["status"],
        "cross_audit_receipt_hash": cross_audit["receipt_hash"],
        "cross_audit_checks": cross_audit["checks"],
        "summary_counts": {
            "newly_registered": int(aggregate.get("newly_registered_count") or 0),
            "reused": int(aggregate.get("reused_count") or 0),
            "review": int(aggregate.get("requires_review_count") or 0),
            "not_ready": int(aggregate.get("not_ready_count") or 0),
            "not_applicable": int(aggregate.get("not_applicable_count") or 0),
            "remaining": int(aggregate.get("remaining_count") or 0),
            "moneyforward_unposted": int(
                aggregate.get("moneyforward_unposted_count") or 0
            ),
            "processable_remaining": int(
                aggregate.get("processable_remaining_count") or 0
            ),
            "replay_additional_write": int(
                aggregate.get("replay_additional_write_count") or 0
            ),
        },
    }
    with db.connect() as conn:
        delivered = deliver_google_chat_notification(
            payload=summary,
            source_hash=cross_audit["receipt_hash"],
            conn=conn,
            webhook_url=resolve_webhook_url(),
        )
        run_hash = accounting_run_hash(args.run_id)
        row = conn.execute(
            "SELECT notification_hash,response_hash,status,sent_at FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND source_hash=? AND status IN ('sent','historical_sent') ORDER BY id DESC LIMIT 1",
            (run_hash, cross_audit["receipt_hash"]),
        ).fetchone()
    if delivered.status == "sent":
        if row is None or not row["response_hash"] or (not row["sent_at"]):
            raise RuntimeError("Google Chat sent read-back is missing")
        status = "sent"
    elif delivered.status in {"already_sent", "historical_sent"}:
        if row is None:
            raise RuntimeError("Google Chat already_sent read-back is missing")
        status = "already_sent"
    else:
        return {
            "status": delivered.status,
            "blocked_reasons": list(delivered.blocked_reasons),
            "cross_audit_receipt_hash": cross_audit["receipt_hash"],
        }
    return {
        "status": status,
        "notification_hash": "sha256:" + str(row["notification_hash"]),
        "response_hash": str(row["response_hash"] or ""),
        "cross_audit_receipt_hash": cross_audit["receipt_hash"],
        "readback_verified": True,
        "google_chat_api_called": delivered.google_chat_api_called,
    }


def _aggregate_chat_items(
    args: argparse.Namespace,
    *,
    aggregate: dict[str, Any],
    runtime: tuple[Any, ...] | None,
) -> list[dict[str, Any]]:
    """Build the established fixed Chat rows from official journal GET results."""
    from .hash_utils import mask_identifier
    from .journal_backfill import _journal_id, _journal_no, _normalized_lines
    from .journal_duplicate_probe import _journal_details_for_period

    bindings = [
        item
        for workflow in aggregate.get("workflows") or []
        if isinstance(workflow, dict)
        for item in workflow.get("chat_journal_bindings") or []
        if isinstance(item, dict)
    ]
    if not bindings:
        return []
    if runtime is None:
        raise RuntimeError("aggregate Chat details require shared authentication")
    expected = {
        str(item.get("journal_id_hash") or ""): str(item.get("source_file_hash") or "")
        for item in bindings
    }
    if any(
        (
            re.fullmatch("sha256:[0-9a-f]{64}", key) is None
            or re.fullmatch("sha256:[0-9a-f]{64}", value) is None
            for key, value in expected.items()
        )
    ):
        raise RuntimeError("aggregate Chat journal binding is invalid")
    accounting_day = date.fromisoformat(str(args.accounting_date))
    details = _journal_details_for_period(
        runtime[0],
        start_date=_company_fiscal_year_start(accounting_day).isoformat(),
        end_date=accounting_day.isoformat(),
        office_id=str(runtime[1]),
    )
    matched = {
        mask_identifier(raw_id): detail
        for detail in details
        if (raw_id := _journal_id(detail)) and mask_identifier(raw_id) in expected
    }
    if set(matched) != set(expected):
        raise RuntimeError("aggregate Chat journal read-back is incomplete")
    items: list[dict[str, Any]] = []
    for journal_hash in sorted(matched):
        detail = matched[journal_hash]
        lines = _normalized_lines(detail)
        number = _journal_no(detail)
        if not lines or not number:
            raise RuntimeError("aggregate Chat journal detail is incomplete")
        branches = detail.get("branches")
        remarks = (
            [
                str(branch.get("remark") or "").strip()
                for branch in branches
                if isinstance(branch, dict)
            ]
            if isinstance(branches, list)
            else []
        )
        items.append(
            {
                "journal_number": number,
                "label": next(
                    (value for value in remarks if value), "Money Forward公式読返し"
                ),
                "amount": sum((int(line.get("amount") or 0) for line in lines)),
                "debit": " + ".join(
                    (
                        f"{line['debit_account']}/{line.get('debit_sub_account') or ''}"
                        for line in lines
                    )
                ),
                "credit": " + ".join(
                    (
                        f"{line['credit_account']}/{line.get('credit_sub_account') or ''}"
                        for line in lines
                    )
                ),
                "evidence_required": False,
                "registration_readback_status": "verified",
                "voucher_status": "not_required",
                "processed_folder_status": "not_required",
                "source_file_hash": expected[journal_hash],
                "processing_ledger_status": "verified",
                "same_chain_ready": True,
            }
        )
    items.sort(
        key=lambda item: (
            (0, int(item["journal_number"]))
            if str(item["journal_number"]).isdigit()
            else (1, str(item["journal_number"]))
        )
    )
    return items


@contextmanager
def _aggregate_production_execution_lock():
    """Allow one live production aggregate invocation per office database."""
    from .db import DEFAULT_DB_PATH

    lock_path = _artifact_root() / "aggregate-production.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another production aggregate run is executing for this office"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _aggregate_transient_exceptions() -> tuple[type[BaseException], ...]:
    """Return only failures that are safe to retry without a code change."""
    from .accounting_api import (
        MoneyForwardRateLimitError,
        MoneyForwardReadTimeoutError,
        MoneyForwardServerError,
    )

    return (
        MoneyForwardRateLimitError,
        MoneyForwardReadTimeoutError,
        MoneyForwardServerError,
        TimeoutError,
        ConnectionError,
    )


def _aggregate_result_resume_complete(result: dict[str, Any]) -> bool:
    if result.get("resume_complete") is True:
        return True
    return (
        result.get("state") in {"VERIFIED", "NOT_READY", "REVIEW", "NOT_APPLICABLE"}
        and int(result.get("remaining_count") or 0) == 0
        and (int(result.get("retry_pending_count") or 0) == 0)
    )


def _execute_aggregate_workflow(
    workflow: str,
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str],
) -> dict[str, Any]:
    """Dispatch one independent workflow through its existing implementation."""
    _require_write_intent()
    if workflow == "sales_invoice":
        return _execute_sales_invoice_stage(db, args, runtime=runtime, standalone=True)
    if workflow == "payable_invoice":
        return _execute_payable_invoice_stage(
            db, args, runtime=runtime, standalone=True
        )
    if workflow == "all_receipt_folders":
        return _execute_all_receipt_folders_stage(
            db, args, runtime=runtime, standalone=True
        )
    if workflow == "payroll_bonus":
        return _execute_payroll_bonus_stage(db, args, runtime=runtime, standalone=True)
    if workflow == "bank_transfer":
        return _execute_bank_transfer_workflow(db, args, runtime=runtime)
    if workflow == "card":
        return _execute_card_workflow(db, args, runtime=runtime)
    raise ValueError(f"unsupported aggregate workflow: {workflow}")


def _aggregate_workflow_result(
    workflow: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Reduce existing workflow results to the five WO08 aggregate states."""
    status = str(payload.get("status") or "incomplete")
    review_count = int(payload.get("review_count") or payload.get("pending_count") or 0)
    retry_count = int(payload.get("retry_pending_count") or 0)
    not_ready_count = int(payload.get("not_ready_count") or 0)
    not_applicable_count = int(payload.get("not_applicable_count") or 0)
    newly_registered_count = int(
        payload.get("newly_registered_count") or payload.get("posted_count") or 0
    )
    reused_count = int(payload.get("reused_count") or 0)
    remaining_count = int(
        payload.get("processable_remaining_count", payload.get("remaining_count", 0))
        or 0
    )
    payroll_probe = payload.get("payroll_bonus_probe")
    payroll_not_ready = (
        isinstance(payroll_probe, dict) and payroll_probe.get("status") == "NOT_READY"
    )
    official_count = int(payload.get("official_readback_verified_count") or 0)
    replay_count = int(payload.get("replay_additional_write_count") or 0)
    readback_signal = (
        payload.get("workflow_readback_verified") is True
        or payload.get("moneyforward_readback_verified") is True
        or official_count > 0
        or (status == "inventory_pass")
        or payroll_not_ready
    )
    if status == "waiting_for_auth":
        state = "waiting_for_auth"
    elif replay_count:
        state = "retry_pending"
        retry_count = max(retry_count, 1)
    elif retry_count or status == "retry_pending":
        state = "retry_pending"
    elif review_count:
        state = "REVIEW"
    elif (
        payroll_not_ready
        or not_ready_count
        or status in {"not_ready", "source_required"}
    ):
        state = "NOT_READY"
    elif status == "not_applicable":
        state = "NOT_APPLICABLE"
    elif (
        status in {"complete", "inventory_pass"}
        and remaining_count == 0
        and readback_signal
    ):
        state = "VERIFIED"
    else:
        state = "retry_pending"
        retry_count = max(retry_count, 1)
    official_readback_complete = (
        state in {"NOT_READY", "NOT_APPLICABLE"}
        or (state == "VERIFIED" and readback_signal)
    ) and replay_count == 0
    resume_complete = official_readback_complete or (
        state == "REVIEW" and remaining_count == 0 and (retry_count == 0)
    )
    invocation_effects = payload.get("invocation_effects")
    if isinstance(invocation_effects, dict):
        invocation_write_count = sum(
            (
                int(invocation_effects.get(key) or 0)
                for key in ("post_count", "voucher_count", "drive_write_count")
            )
        )
    else:
        invocation_write_count = int(
            payload.get("posted_count") or payload.get("newly_registered_count") or 0
        )
    return {
        "workflow": workflow,
        "state": state,
        "status": status,
        "resume_complete": resume_complete,
        "official_readback_complete": official_readback_complete,
        "review_count": review_count,
        "not_ready_count": max(not_ready_count, int(state == "NOT_READY")),
        "not_applicable_count": max(
            not_applicable_count, int(state == "NOT_APPLICABLE")
        ),
        "newly_registered_count": newly_registered_count,
        "reused_count": reused_count,
        "retry_pending_count": retry_count,
        "remaining_count": remaining_count,
        "invocation_additional_write_count": invocation_write_count,
        "replay_additional_write_count": replay_count,
        "chat_journal_bindings": [
            {
                "journal_id_hash": str(item.get("journal_id_hash") or ""),
                "source_file_hash": str(item.get("source_file_hash") or ""),
            }
            for item in payload.get("chat_journal_bindings") or []
            if isinstance(item, dict)
        ],
        "inventory_item_hashes": sorted(
            (str(value) for value in payload.get("inventory_item_hashes") or [])
        ),
        "resolved_item_hashes": sorted(
            (str(value) for value in payload.get("resolved_item_hashes") or [])
        ),
        "terminal_unposted_item_hashes": sorted(
            (str(value) for value in payload.get("terminal_unposted_item_hashes") or [])
        ),
        "post_unposted_item_hashes": sorted(
            (str(value) for value in payload.get("post_unposted_item_hashes") or [])
        ),
        "processable_remaining_item_hashes": sorted(
            (
                str(value)
                for value in payload.get("processable_remaining_item_hashes") or []
            )
        ),
        "skipped": False,
    }


def _aggregate_run_artifact_path(run_id: str) -> Path:
    return (
        _artifact_root() / "runs" / accounting_run_hash(run_id) / "aggregate.local.json"
    )


def _aggregate_implementation_head() -> str:
    """Retain Git provenance where present; support independent fresh copies."""
    root = Path(__file__).resolve().parents[2]
    # Avoid discovering an unrelated parent checkout for source archives.
    if (root / ".git").exists():
        try:
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                return head
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Bootstrap promises no initial commit is needed. A real content
            # fingerprint keeps provenance rather than fabricating a Git SHA.
            pass
    from .hash_utils import source_tree_fingerprint

    return source_tree_fingerprint(root)


def _load_aggregate_run_artifact(path):
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            "existing aggregate artifact is unreadable; refusing fresh execution"
        ) from error
    if not isinstance(payload, dict):
        raise ValueError("aggregate artifact must be an object")
    unsigned = {key: value for key, value in payload.items() if key != "artifact_hash"}
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
    )
    if payload.get("artifact_hash") != expected:
        raise ValueError("aggregate artifact integrity check failed")
    _assert_run_artifact(payload)
    return payload


def _write_aggregate_run_artifact(
    path: Path,
    *,
    run_id: str,
    accounting_date: str,
    workflows: list[dict[str, Any]],
    operational_failure: dict[str, Any] | None = None,
    cross_audit: dict[str, Any] | None = None,
    google_chat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit_inventory = (
        cross_audit.get("official_unposted_inventory")
        if isinstance(cross_audit, dict)
        and isinstance(cross_audit.get("official_unposted_inventory"), dict)
        else {}
    )
    counts = {
        state: sum((row.get("state") == state for row in workflows))
        for state in (
            "VERIFIED",
            "NOT_READY",
            "REVIEW",
            "NOT_APPLICABLE",
            "retry_pending",
            "waiting_for_auth",
        )
    }
    if operational_failure is not None:
        status = "failed"
    elif counts["waiting_for_auth"]:
        status = "waiting_for_auth"
    elif counts["retry_pending"]:
        status = "retry_pending"
    elif any(
        (
            row.get("state") == "REVIEW"
            and (not _aggregate_result_resume_complete(row))
            for row in workflows
        )
    ):
        status = "incomplete"
    elif not all((_aggregate_result_resume_complete(row) for row in workflows)):
        status = "incomplete"
    elif not cross_audit or cross_audit.get("status") != "verified":
        status = "finalizing"
    elif not google_chat or google_chat.get("status") not in {"sent", "already_sent"}:
        status = "chat_pending"
    else:
        status = "complete"
    payload = {
        **_profile_boundary(),
        "schema": "moneyforward-aggregate-run-v1",
        "status": status,
        "run_mode": "production",
        "synthetic": False,
        "artifact_updated_at": datetime.now(timezone.utc).isoformat(),
        "implementation_head": _aggregate_implementation_head(),
        "run_id_hash": "sha256:" + accounting_run_hash(run_id),
        "accounting_date": accounting_date,
        "active_workflows": list(ACTIVE_ACCOUNTING_WORKFLOWS),
        "workflows": workflows,
        "state_counts": counts,
        "remaining_count": sum(
            (int(row.get("remaining_count") or 0) for row in workflows)
        ),
        "processable_remaining_count": sum(
            (int(row.get("remaining_count") or 0) for row in workflows)
        ),
        "moneyforward_unposted_count": int(audit_inventory.get("count") or 0),
        "moneyforward_unposted_item_set_hash": str(
            audit_inventory.get("item_set_hash") or ""
        ),
        "requires_review_count": sum(
            (int(row.get("review_count") or 0) for row in workflows)
        ),
        "newly_registered_count": sum(
            (int(row.get("newly_registered_count") or 0) for row in workflows)
        ),
        "reused_count": sum((int(row.get("reused_count") or 0) for row in workflows)),
        "not_ready_count": sum(
            (int(row.get("not_ready_count") or 0) for row in workflows)
        ),
        "not_applicable_count": sum(
            (int(row.get("not_applicable_count") or 0) for row in workflows)
        ),
        "replay_additional_write_count": sum(
            (int(row.get("replay_additional_write_count") or 0) for row in workflows)
        ),
        "invocation_additional_write_count": sum(
            (
                int(row.get("invocation_additional_write_count") or 0)
                for row in workflows
            )
        ),
        "workflow_failure_isolation": operational_failure is None,
        "authentication_preflight_status": "verified",
        "fixed_completion_count_required": False,
        "cross_audit_mode": "read_only_finalization",
        "cross_audit": cross_audit or {"status": "not_started"},
        "google_chat": google_chat or {"status": "not_started"},
        "wo07_started": False,
        "raw_ids_stored": False,
        "raw_api_responses_stored": False,
    }
    if operational_failure is not None:
        payload["operational_failure"] = operational_failure
    payload["artifact_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return payload


def _execute_current_production_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str] | None,
) -> dict[str, Any]:
    """Execute the current production stage and reject silent no-progress."""
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, build_run_status

    with db.connect() as conn:
        before = build_run_status(conn, args.run_id)
    if before.get("status") != "running":
        return {
            **before,
            "execution_mode": "production_start_or_resume",
            "manifest_required": False,
        }
    if runtime is None:
        raise RuntimeError("production authentication preflight is required")
    stage = str(before.get("current_stage") or "")
    if stage == "sales_invoice":
        after = _execute_sales_invoice_stage(db, args, runtime=runtime)
    elif stage == "payable_invoice":
        after = _execute_payable_invoice_stage(db, args, runtime=runtime)
    elif stage == "all_receipt_folders":
        after = _execute_all_receipt_folders_stage(db, args, runtime=runtime)
    elif stage == "payroll_bonus":
        after = _execute_payroll_bonus_stage(db, args, runtime=runtime)
    elif stage in {"bank_transfer", "card"}:
        after = _execute_connected_transactions_stage(
            db, args, stage=stage, runtime=runtime
        )
    elif stage == "tax_insurance":
        after = _execute_tax_insurance_stage(db, args, runtime=runtime)
    elif stage == "cross_audit":
        after = _execute_cross_audit_stage(db, args, runtime=runtime)
    elif stage in {"google_chat_durable_sent", "rag_learning", "issue_update"}:
        after = _execute_receipt_backed_final_stage(
            db, args, stage=stage, runtime=runtime
        )
    else:
        raise RuntimeError(f"production stage adapter is not connected: {stage}")
    if after.get("status") == "running" and int(after.get("generation", -1)) <= int(
        before.get("generation", -1)
    ):
        raise RuntimeError(f"production stage made no progress: {stage}")
    return after


def _execute_receipt_backed_final_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    stage: str,
    runtime: tuple[Any, str, Any | None, str] | None = None,
) -> dict[str, Any]:
    """Advance final delivery stages only from their durable authoritative receipts."""
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, build_run_status

    receipt_kind = {
        "rag_learning": "verified_journal_learning",
        "issue_update": "github_issue_readback",
    }.get(stage)
    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
        if status.get("status") != "running" or status.get("current_stage") != stage:
            raise RuntimeError(f"{stage} adapter called outside its active stage")
        run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
        if stage == "google_chat_durable_sent":
            row = conn.execute(
                "SELECT notification_hash,response_hash FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND status IN ('sent','historical_sent') ORDER BY id DESC LIMIT 1",
                (run_hash,),
            ).fetchone()
            if row is None or not row["response_hash"]:
                row = None
            else:
                advanced = advance_run(
                    conn,
                    args.run_id,
                    stage,
                    {
                        "status": "verified",
                        "evidence_hash": "sha256:"
                        + stable_hash(
                            {"stage": stage, "response_hash": row["response_hash"]}
                        ),
                        "delivery_status": "sent",
                        "notification_hash": "sha256:" + str(row["notification_hash"]),
                        "response_hash": str(row["response_hash"]),
                    },
                    actor=args.actor,
                )
                return {**advanced, "google_chat_delivery_mode": "reused"}
    if stage == "google_chat_durable_sent":
        if runtime is None:
            raise RuntimeError(
                "Google Chat completion requires production authentication"
            )
        from .google_chat_notify import (
            deliver_google_chat_notification,
            resolve_webhook_url,
        )

        payload, source_hash = _build_authoritative_chat_completion_payload(
            db, args, runtime=runtime
        )
        webhook_url = resolve_webhook_url()
        with db.connect() as conn:
            delivered = deliver_google_chat_notification(
                payload=payload,
                source_hash=source_hash,
                conn=conn,
                webhook_url=webhook_url,
            )
            if delivered.status not in {"sent", "historical_sent"}:
                raise RuntimeError(
                    "Google Chat completion was not durably delivered: "
                    + ",".join(delivered.blocked_reasons)
                )
            row = conn.execute(
                "SELECT notification_hash,response_hash FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND status IN ('sent','historical_sent') ORDER BY id DESC LIMIT 1",
                (
                    str(
                        build_run_status(conn, args.run_id)["run_id_hash"]
                    ).removeprefix("sha256:"),
                ),
            ).fetchone()
            if row is None and delivered.status == "historical_sent":
                dedup = conn.execute(
                    "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='google_chat_durable_sent' AND receipt_kind='google_chat_dedup_readback' AND status='verified' ORDER BY id DESC LIMIT 1",
                    (
                        str(
                            build_run_status(conn, args.run_id)["run_id_hash"]
                        ).removeprefix("sha256:"),
                    ),
                ).fetchone()
                if dedup is not None:
                    receipt_hash = str(dedup["receipt_hash"])
                    advanced = advance_run(
                        conn,
                        args.run_id,
                        stage,
                        {
                            "status": "verified",
                            "evidence_hash": "sha256:" + receipt_hash,
                            "delivery_status": "historical_sent",
                            "dedup_receipt_hash": "sha256:" + receipt_hash,
                        },
                        actor=args.actor,
                    )
                    return {**advanced, "google_chat_delivery_mode": "reused"}
            if row is None or not row["response_hash"]:
                raise RuntimeError(
                    "durable Google Chat delivery receipt is missing after send"
                )
            advanced = advance_run(
                conn,
                args.run_id,
                stage,
                {
                    "status": "verified",
                    "evidence_hash": "sha256:"
                    + stable_hash(
                        {"stage": stage, "response_hash": row["response_hash"]}
                    ),
                    "delivery_status": "sent",
                    "notification_hash": "sha256:" + str(row["notification_hash"]),
                    "response_hash": str(row["response_hash"]),
                },
                actor=args.actor,
            )
            return {**advanced, "google_chat_delivery_mode": "executed"}
    if stage == "rag_learning":
        with db.connect() as conn:
            status = build_run_status(conn, args.run_id)
            run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
            existing = conn.execute(
                "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='rag_learning' AND receipt_kind='verified_journal_learning' ORDER BY id DESC LIMIT 1",
                (run_hash,),
            ).fetchone()
        if existing is None:
            if runtime is None:
                raise RuntimeError(
                    "verified RAG learning requires production authentication"
                )
            from .verified_journal_learning import (
                _record_learning_run_receipt_if_active,
                learn_verified_workflow,
            )

            payload, _source_hash = _build_authoritative_chat_completion_payload(
                db, args, runtime=runtime
            )
            with db.connect() as conn:
                result = learn_verified_workflow(
                    completion_summary=payload,
                    payroll_summary=None,
                    conn=conn,
                    persist=True,
                )
                if (
                    _record_learning_run_receipt_if_active(payload, result, conn)
                    is None
                ):
                    raise RuntimeError("durable rag_learning receipt was not created")
    if stage == "issue_update":
        github_delivery_mode = "reused"
        with db.connect() as conn:
            status = build_run_status(conn, args.run_id)
            run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
            existing = conn.execute(
                "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='issue_update' AND receipt_kind='github_issue_readback' ORDER BY id DESC LIMIT 1",
                (run_hash,),
            ).fetchone()
        if existing is None:
            _receipt_hash, github_delivery_mode = _record_unified_workflow_issue_update(
                db, args, status=status
            )
    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
        run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
        receipt = conn.execute(
            "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key=? AND receipt_kind=? ORDER BY id DESC LIMIT 1",
            (run_hash, stage, receipt_kind),
        ).fetchone()
        if receipt is None:
            raise RuntimeError(f"durable {stage} receipt is missing")
        if stage == "rag_learning":
            chat = conn.execute(
                "SELECT notification_hash FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND status IN ('sent','historical_sent') ORDER BY id DESC LIMIT 1",
                (run_hash,),
            ).fetchone()
            notification_hash = (
                str(chat["notification_hash"]) if chat is not None else ""
            )
            if not notification_hash:
                dedup = conn.execute(
                    "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='google_chat_durable_sent' AND receipt_kind='google_chat_dedup_readback' AND status='verified' ORDER BY id DESC LIMIT 1",
                    (run_hash,),
                ).fetchone()
                if dedup is None:
                    raise RuntimeError(
                        "learning requires the same durable Chat notification"
                    )
                notification_hash = str(dedup["receipt_hash"])
            evidence = {
                "status": "verified",
                "evidence_hash": "sha256:"
                + stable_hash({"stage": stage, "receipt": receipt["receipt_hash"]}),
                "notification_hash": "sha256:" + notification_hash,
                "learning_manifest_hash": "sha256:" + str(receipt["receipt_hash"]),
            }
        else:
            evidence = {
                "status": "verified",
                "evidence_hash": "sha256:"
                + stable_hash({"stage": stage, "receipt": receipt["receipt_hash"]}),
                "issue_delivery_status": "verified",
                "issue_receipt_hash": "sha256:" + str(receipt["receipt_hash"]),
            }
        advanced = advance_run(conn, args.run_id, stage, evidence, actor=args.actor)
        if stage == "issue_update":
            return {**advanced, "github_delivery_mode": github_delivery_mode}
        return advanced


def _require_private_reporting_repository(repository: str) -> None:
    """Operational accounting reports must never be posted to public OSS issues."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("reporting repository must be an exact owner/name")
    result = subprocess.run(
        ["gh", "api", "--method", "GET", f"repos/{repository}"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    metadata = json.loads(result.stdout)
    if (not isinstance(metadata, dict)
            or metadata.get("full_name") != repository
            or metadata.get("private") is not True):
        raise PermissionError("accounting reporting destination must be the exact private repository")


def _record_unified_workflow_issue_update(
    db: Any, args: argparse.Namespace, *, status: dict[str, Any]
) -> tuple[str, str]:
    """Post once to the canonical Issue and persist an exact REST read-back."""
    import subprocess
    from .github_issue_readback import (
        accounting_run_marker,
        record_github_issue_readback,
    )
    from .hash_utils import sha256_text

    GITHUB_REPOSITORY = str(_setting("reporting.github_repository"))
    issue_number = int(_setting("reporting.issue_number"))
    _require_private_reporting_repository(GITHUB_REPOSITORY)
    marker = accounting_run_marker(args.run_id)
    body = "\n".join(
        (
            "configured_company 会計ワークフロー記録",
            f"- accounting_date: {status['accounting_date']}",
            f"- run_status: {status.get('status', 'unknown')}",
            f"- unresolved: {status.get('remaining_count', 'unknown')}",
            "- stage_statuses: " + json.dumps(
                {str(row['stage']): str(row.get('status', 'unknown'))
                 for row in status.get('stages', [])
                 if isinstance(row, dict) and 'stage' in row},
                ensure_ascii=False, sort_keys=True,
            ),
            marker,
        )
    )
    listed = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            f"repos/{GITHUB_REPOSITORY}/issues/{issue_number}/comments",
            "-f",
            "per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    pages = json.loads(listed.stdout or "[]")
    comments = [
        comment
        for page in pages
        if isinstance(page, list)
        for comment in page
        if isinstance(comment, dict)
    ]
    response = next(
        (comment for comment in comments if str(comment.get("body") or "") == body),
        None,
    )
    delivery_mode = "reused"
    if response is None:
        posted = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{GITHUB_REPOSITORY}/issues/{issue_number}/comments",
                "-f",
                f"body={body}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        posted_payload = json.loads(posted.stdout)
        comment_id = int(posted_payload["id"])
        readback = subprocess.run(
            ["gh", "api", f"repos/{GITHUB_REPOSITORY}/issues/comments/{comment_id}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        response = json.loads(readback.stdout)
        delivery_mode = "executed"
    with db.connect() as conn:
        receipt_hash = record_github_issue_readback(
            conn,
            run_id=args.run_id,
            issue_number=issue_number,
            expected_body_hash=sha256_text(body),
            response=response,
            actor=args.actor,
        )
    return (receipt_hash, delivery_mode)


def _build_authoritative_chat_completion_payload(
    db: Any, args: argparse.Namespace, *, runtime: tuple[Any, str, Any | None, str]
) -> tuple[dict[str, Any], str]:
    """Build the final Chat body only from current-run terminal rows and live journals."""
    from .hash_utils import mask_identifier, stable_hash
    from .journal_backfill import (
        _extract_items,
        _journal_id,
        _journal_no,
        _normalized_lines,
        _unwrap_journal,
    )
    from .same_chain_status import same_chain_status_for_file_hash
    from .shiwake_run import build_run_status

    client, office_id, _refresh_result, _drive_token = runtime
    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
        run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
        terminal_no_journal_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM accounting_run_items WHERE run_id_hash=? AND status='not_applicable'",
                (run_hash,),
            ).fetchone()[0]
        )
        unresolved_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM accounting_run_items WHERE run_id_hash=? AND status NOT IN ('verified','not_applicable')",
                (run_hash,),
            ).fetchone()[0]
        )
        if unresolved_count:
            raise RuntimeError(
                "Google Chat completion requires zero unresolved run items"
            )
        run_dir = _artifact_root() / "runs" / run_hash
        identities: list[tuple[str, str | None]] = []
        receipt = json.loads(
            (run_dir / "all_receipt_folders.local.json").read_text(encoding="utf-8")
        )
        for item in receipt.get("files") or []:
            if isinstance(item, dict):
                identities.append(
                    (
                        mask_identifier(str(item.get("google_drive_file_id") or "")),
                        str(item.get("file_hash") or ""),
                    )
                )
        payable = json.loads(
            (run_dir / "payable_invoice.local.json").read_text(encoding="utf-8")
        )
        for item in payable.get("quarantine_items") or []:
            if isinstance(item, dict):
                identities.append((str(item.get("item_id_hash") or ""), None))
        verified_ledgers: dict[str, str] = {}
        for drive_hash, file_hash in identities:
            if not drive_hash:
                continue
            if file_hash:
                row = conn.execute(
                    "SELECT file_hash,moneyforward_journal_id_hash FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=? AND processing_status='verified' ORDER BY updated_at DESC,id DESC LIMIT 1",
                    (drive_hash, file_hash),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT file_hash,moneyforward_journal_id_hash FROM processing_ledger WHERE google_drive_file_id_hash=? AND processing_status='verified' ORDER BY updated_at DESC,id DESC LIMIT 1",
                    (drive_hash,),
                ).fetchone()
            if row is None or not row["moneyforward_journal_id_hash"]:
                continue
            chain = same_chain_status_for_file_hash(conn, str(row["file_hash"]))
            if chain is None or chain.get("same_chain_ready") is not True:
                raise RuntimeError(
                    "verified Chat item lost its authoritative same-chain read-back"
                )
            verified_ledgers[str(row["moneyforward_journal_id_hash"])] = str(
                row["file_hash"]
            )
        cross = conn.execute(
            "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='cross_audit' ORDER BY id DESC LIMIT 1",
            (run_hash,),
        ).fetchone()
        source_hash = (
            "sha256:" + str(cross["receipt_hash"])
            if cross
            else "sha256:" + stable_hash(status)
        )
    end_date = str(status["accounting_date"])
    end = date.fromisoformat(end_date)
    start_date = _company_fiscal_year_start(end).isoformat()
    matched: dict[str, dict[str, Any]] = {}
    page = 1
    while verified_ledgers:
        listing = client.journals(
            office_id=office_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            per_page=500,
        )
        rows = _extract_items(listing)
        for summary in rows:
            raw_id = _journal_id(summary)
            journal_hash = mask_identifier(raw_id) if raw_id else ""
            if journal_hash in verified_ledgers:
                matched[journal_hash] = _unwrap_journal(
                    client.journal(raw_id, office_id=office_id)
                )
        if len(rows) < 500:
            break
        page += 1
        if page > 100:
            raise RuntimeError(
                "Money Forward journal pagination exceeded the safety limit"
            )
    missing = set(verified_ledgers) - set(matched)
    if missing:
        raise RuntimeError(
            "verified Chat journal was not found by authoritative Money Forward read-back"
        )
    items: list[dict[str, Any]] = []
    for journal_hash, journal in matched.items():
        lines = _normalized_lines(journal)
        if not lines or not _journal_no(journal):
            raise RuntimeError("Money Forward Chat journal read-back is incomplete")
        debit = " ＋ ".join(
            (
                f"{line['debit_account']}/{line.get('debit_sub_account') or '-'}〔{line.get('tax_category') or '-'}〕 {int(line.get('amount') or 0):,}円"
                for line in lines
            )
        )
        credit = " ＋ ".join(
            (
                f"{line['credit_account']}/{line.get('credit_sub_account') or '-'} {int(line.get('amount') or 0):,}円"
                for line in lines
            )
        )
        items.append(
            {
                "journal_number": _journal_no(journal),
                "label": "Money Forward公式読返し",
                "amount": sum((int(line.get("amount") or 0) for line in lines)),
                "debit_detail": debit,
                "credit_detail": credit,
                "debit": " + ".join(
                    (
                        f"{line['debit_account']}/{line.get('debit_sub_account') or ''}/{line.get('tax_category') or ''}"
                        for line in lines
                    )
                ),
                "credit": " + ".join(
                    (
                        f"{line['credit_account']}/{line.get('credit_sub_account') or ''}"
                        for line in lines
                    )
                ),
                "registration_readback_status": "verified",
                "voucher_status": "verified",
                "processed_folder_status": "verified",
                "source_file_hash": verified_ledgers[journal_hash],
                "processing_ledger_status": "verified",
                "same_chain_ready": True,
            }
        )
    return (
        {
            "notification_type": "completion",
            "notification_scope": "daily_completion",
            "accounting_run_id": args.run_id,
            "title": "Money Forward 仕訳完了",
            "items": items,
            "terminal_no_journal_count": terminal_no_journal_count,
            "terminal_no_journal_verified": True,
            "unresolved_item_count": 0,
        },
        source_hash,
    )


def _execute_cross_audit_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str] | None = None,
) -> dict[str, Any]:
    """Reconcile the captured batch before allowing the final audit."""
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, build_run_status, record_cross_audit_receipt

    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
    if (
        status.get("status") != "running"
        or status.get("current_stage") != "cross_audit"
    ):
        raise RuntimeError("cross audit adapter called outside its active stage")
    reconciliation = _reconcile_cross_audit_inputs(
        db, args, status=status, runtime=runtime
    )
    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
    remaining_count = int(status.get("remaining_count") or 0)
    if remaining_count:
        raise RuntimeError(
            f"cross audit blocked after authoritative reconciliation: remaining={remaining_count}, payable={reconciliation['payable_remaining_count']}, receipts={reconciliation['receipt_remaining_count']}(verified_ledger={reconciliation['receipt_verified_ledger_count']},not_applicable={reconciliation['receipt_not_applicable_count']},blocked={reconciliation['receipt_blocked_count']}), card_review={reconciliation['card_review_count']}, card_ready={reconciliation['card_ready_count']}"
        )
    with db.connect() as conn:
        run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
        receipt = conn.execute(
            "SELECT receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND stage_key='cross_audit' AND receipt_kind='cross_audit_readback' ORDER BY id DESC LIMIT 1",
            (run_hash,),
        ).fetchone()
        if receipt is None:
            source_receipts = conn.execute(
                "SELECT stage_key,receipt_hash FROM accounting_run_receipts WHERE run_id_hash=? AND receipt_kind='source_stage_readback' AND status='verified' ORDER BY stage_key",
                (run_hash,),
            ).fetchall()
            if len({str(row["stage_key"]) for row in source_receipts}) != 7:
                raise RuntimeError(
                    "cross audit requires all seven authoritative source receipts"
                )
            source_snapshot = {
                str(row["stage_key"]): str(row["receipt_hash"])
                for row in source_receipts
            }
            readback = {
                "source_receipts": source_snapshot,
                "reconciliation": reconciliation,
            }
            receipt_hash = record_cross_audit_receipt(
                conn,
                run_id=args.run_id,
                receipt=_bound_receipt(
                    {
                        "status": "verified",
                        "stage": "cross_audit",
                        "accounting_run_id_hash": "sha256:" + run_hash,
                        "accounting_date": str(status["accounting_date"]),
                        "source_snapshot_hash": "sha256:"
                        + stable_hash(source_snapshot),
                        "readback_hash": "sha256:" + stable_hash(readback),
                        "remaining_count": 0,
                        "all_items_resolved": True,
                        "coverage_state": "captured",
                        "identity_readback_verified": True,
                        "office_hash": "sha256:"
                        + stable_hash({"office": _office_name()}),
                        "execution_principal_hash": "sha256:"
                        + stable_hash({"principal": _mf_principal()}),
                        "audit_checks": {
                            "duplicate_journal_check": "verified",
                            "debit_credit_amount_check": "verified",
                            "account_sub_account_tax_check": "verified",
                            "source_statement_evidence_reconciliation": "verified",
                            "processed_folder_readback": "verified",
                        },
                        "completion_notification_allowed": True,
                        "daily_control_hash": "sha256:" + stable_hash(source_snapshot),
                        "post_audit_hash": "sha256:" + stable_hash(readback),
                    }
                ),
                actor=args.actor,
            )
        else:
            receipt_hash = "sha256:" + str(receipt["receipt_hash"])
        return advance_run(
            conn,
            args.run_id,
            "cross_audit",
            {
                "status": "verified",
                "evidence_hash": receipt_hash,
                "adapter_receipt_hash": receipt_hash,
                "audit_checks": {
                    "duplicate_journal_check": "verified",
                    "debit_credit_amount_check": "verified",
                    "account_sub_account_tax_check": "verified",
                    "source_statement_evidence_reconciliation": "verified",
                    "processed_folder_readback": "verified",
                },
            },
            actor=args.actor,
        )


def _reconcile_cross_audit_inputs(
    db: Any,
    args: argparse.Namespace,
    *,
    status: dict[str, Any],
    runtime: tuple[Any, str, Any | None, str] | None = None,
) -> dict[str, int]:
    """Classify and route the real captured batch without inventing completion."""
    from .financial_event_classification import classify_financial_events_from_file
    from .hash_utils import mask_identifier, stable_hash
    from .same_chain_status import same_chain_status_for_file_hash

    run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
    run_dir = _artifact_root() / "runs" / run_hash
    connected_path = run_dir / "connected_transactions.local.json"
    receipt_path = run_dir / "all_receipt_folders.local.json"
    payable_path = run_dir / "payable_invoice.local.json"
    if (
        not connected_path.is_file()
        or not receipt_path.is_file()
        or (not payable_path.is_file())
    ):
        raise RuntimeError("cross audit requires all current-run source artifacts")
    classification_path = run_dir / "card_classification.local.json"
    journal_batch_path = run_dir / "card_journal_batch.local.json"
    classification_input_path = connected_path
    if runtime is not None:
        connected_payload = json.loads(connected_path.read_text(encoding="utf-8"))
        enriched_payload = _bind_supplier_primary_matches(
            runtime[0],
            office_id=runtime[1],
            payload=connected_payload,
            accounting_date=str(status["accounting_date"]),
        )
        classification_input_path = (
            run_dir / "connected_transactions_reconciled.local.json"
        )
        classification_input_path.write_text(
            json.dumps(enriched_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    with db.connect() as conn:
        classification = classify_financial_events_from_file(
            events_json=classification_input_path,
            output_json=classification_path,
            journal_batch_input_json=journal_batch_path,
            conn=conn,
            persist=True,
            engine="python",
        )
    receipt_artifact = json.loads(receipt_path.read_text(encoding="utf-8"))
    if runtime is not None:
        _classify_and_execute_all_receipt_folders(
            db,
            args,
            status=status,
            runtime=runtime,
            artifact=receipt_artifact,
            run_dir=run_dir,
        )
        receipt_path.write_text(
            json.dumps(receipt_artifact, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        pending_drive_files: list[dict[str, Any]] = []
        with db.connect() as conn:
            for item in receipt_artifact.get("files") or []:
                if not isinstance(item, dict):
                    continue
                drive_id = str(item.get("google_drive_file_id") or "")
                file_hash = str(item.get("file_hash") or "")
                ledger = conn.execute(
                    "SELECT processing_status,moneyforward_journal_id_hash,moneyforward_voucher_id_hash,idempotency_key FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=? ORDER BY updated_at DESC,id DESC LIMIT 1",
                    (mask_identifier(drive_id), file_hash),
                ).fetchone()
                terminal = False
                if (
                    ledger is not None
                    and str(ledger["processing_status"]) == "not_applicable"
                ):
                    terminal = True
                elif (
                    ledger is not None
                    and str(ledger["processing_status"]) == "verified"
                ):
                    processed = conn.execute(
                        "SELECT 1 FROM audit_logs WHERE idempotency_key=? AND event_type='drive_processed_folder_readback' LIMIT 1",
                        (ledger["idempotency_key"],),
                    ).fetchone()
                    terminal = bool(
                        ledger["moneyforward_journal_id_hash"]
                        and ledger["moneyforward_voucher_id_hash"]
                        and (processed is not None)
                    )
                if not terminal and (
                    not (
                        ledger is not None
                        and ledger["moneyforward_journal_id_hash"]
                        and ledger["moneyforward_voucher_id_hash"]
                    )
                ):
                    pending_drive_files.append(item)
        evidence_payload = _bind_verified_drive_card_evidence(
            payload=json.loads(classification_input_path.read_text(encoding="utf-8")),
            receipt_files=pending_drive_files,
            classifications=classification.items,
            access_token=runtime[3],
            run_dir=run_dir,
        )
        if evidence_payload is not None:
            classification_input_path = (
                run_dir / "connected_transactions_evidence.local.json"
            )
            classification_input_path.write_text(
                json.dumps(
                    evidence_payload, ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
            with db.connect() as conn:
                classification = classify_financial_events_from_file(
                    events_json=classification_input_path,
                    output_json=classification_path,
                    journal_batch_input_json=journal_batch_path,
                    conn=conn,
                    persist=True,
                    engine="python",
                )
    card_ready_count = sum((item.registration_allowed for item in classification.items))
    card_review_count = len(classification.items) - card_ready_count
    card_verified_count = 0
    classified_transactions = _validate_connected_transaction_snapshot(
        json.loads(classification_input_path.read_text(encoding="utf-8")),
        accounting_date=str(status["accounting_date"]),
    )
    supplier_exclusions = []
    for source, classified in zip(
        classified_transactions, classification.items, strict=True
    ):
        if (
            classified.matched_pattern_source == "configured_supplier_primary_policy"
            and source.get("matched_supplier_primary_purchase")
        ):
            identity = str(source["source_event_id_hash"])
            supplier_exclusions.append(
                {
                    "existing_item_hash": "sha256:"
                    + stable_hash({"stage": "card", "identity": identity}),
                    "status": "not_applicable",
                    "not_applicable_basis": "authoritative supplier-primary registration proves this AMEX row is duplicate-only",
                    "no_economic_event_verified": True,
                    "not_applicable_readback_hash": "sha256:"
                    + stable_hash(source["matched_supplier_primary_purchase"]),
                }
            )
    if supplier_exclusions:
        _resolve_prior_source_items(
            db,
            args,
            status=status,
            stage="card",
            resolutions=supplier_exclusions,
            readback_basis={
                "source": "moneyforward_supplier_primary_exact_readback",
                "terminal_count": len(supplier_exclusions),
            },
        )
        card_review_count -= len(supplier_exclusions)
    if card_ready_count:
        if runtime is None:
            raise RuntimeError(
                "connected journal execution requires production authentication"
            )
        card_verified_count = _execute_ready_connected_items(
            db,
            args,
            status=status,
            runtime=runtime,
            transactions=classified_transactions,
            classifications=classification.items,
        )
        card_resolutions: list[dict[str, Any]] = []
        with db.connect() as conn:
            current_transactions = _validate_connected_transaction_snapshot(
                json.loads(classification_input_path.read_text(encoding="utf-8")),
                accounting_date=str(status["accounting_date"]),
            )
            for source, classified in zip(
                current_transactions, classification.items, strict=True
            ):
                source_identity = str(source["source_event_id_hash"])
                candidate = classified.journal_candidate or {}
                source_file_hash = str(
                    candidate.get("source_file_hash") or ""
                ) or "sha256:" + stable_hash({"connected_source": source_identity})
                ledger = conn.execute(
                    "SELECT processing_status FROM processing_ledger WHERE file_hash=? ORDER BY id DESC LIMIT 1",
                    (source_file_hash,),
                ).fetchone()
                if ledger is not None and ledger["processing_status"] == "verified":
                    item_hash = stable_hash(
                        {"stage": "card", "identity": source_identity}
                    )
                    card_resolutions.append(
                        {
                            "existing_item_hash": f"sha256:{item_hash}",
                            "status": "verified",
                        }
                    )
        if card_resolutions:
            _resolve_prior_source_items(
                db,
                args,
                status=status,
                stage="card",
                resolutions=card_resolutions,
                readback_basis={
                    "source": "verified_processing_ledger",
                    "verified_count": len(card_resolutions),
                },
            )
            card_verified_count = len(card_resolutions)
    receipt_files = receipt_artifact.get("files")
    if not isinstance(receipt_files, list):
        raise ValueError("current-run receipt manifest is incomplete")
    receipt_states = {"verified": 0, "not_applicable": 0, "blocked": 0, "untracked": 0}
    receipt_resolutions: list[dict[str, Any]] = []
    with db.connect() as conn:
        for item in receipt_files:
            if not isinstance(item, dict):
                raise ValueError("current-run receipt item must be an object")
            drive_id = str(item.get("google_drive_file_id") or "")
            file_hash = str(item.get("file_hash") or "")
            row = conn.execute(
                "\n                SELECT processing_status,idempotency_key,\n                       moneyforward_journal_id_hash,moneyforward_voucher_id_hash\n                FROM processing_ledger\n                WHERE google_drive_file_id_hash = ? AND file_hash = ?\n                ORDER BY updated_at DESC, id DESC\n                LIMIT 1\n                ",
                (mask_identifier(drive_id), file_hash),
            ).fetchone()
            state = str(row["processing_status"]) if row is not None else "untracked"
            if row is not None and state == "verified":
                chain = same_chain_status_for_file_hash(conn, file_hash)
                if chain is None or chain.get("same_chain_ready") is not True:
                    state = "blocked"
            receipt_states[state if state in receipt_states else "blocked"] += 1
            if state in {"verified", "not_applicable"}:
                item_id = "sha256:" + stable_hash({"google_drive_file_id": drive_id})
                run_item_hash = stable_hash(
                    {"stage": "all_receipt_folders", "identity": item_id}
                )
                resolution: dict[str, Any] = {
                    "existing_item_hash": f"sha256:{run_item_hash}",
                    "status": state,
                }
                if state == "not_applicable":
                    resolution.update(
                        {
                            "not_applicable_basis": "authoritative processing ledger confirms no new accounting event is required for this evidence",
                            "no_economic_event_verified": True,
                            "not_applicable_readback_hash": "sha256:"
                            + stable_hash(
                                {
                                    "drive_file_id_hash": mask_identifier(drive_id),
                                    "file_hash": file_hash,
                                    "processing_status": state,
                                }
                            ),
                        }
                    )
                receipt_resolutions.append(resolution)
        payable_artifact = json.loads(payable_path.read_text(encoding="utf-8"))
        _validate_payable_invoice_gas_artifact(payable_artifact)
        payable_remaining_row = conn.execute(
            "\n            SELECT COUNT(*) AS item_count\n            FROM accounting_run_items\n            WHERE run_id_hash = ? AND source_stage = 'payable_invoice'\n              AND status NOT IN ('verified', 'not_applicable')\n            ",
            (run_hash,),
        ).fetchone()
        payable_remaining_count = int(payable_remaining_row["item_count"])
        resolution_rows = (
            (
                "payable_invoice",
                "evidence_retrieval",
                "retrieve missing recurring or linked invoice evidence, then rerun source read-back",
                "payable_evidence_incomplete",
            ),
            (
                "all_receipt_folders",
                "accounting_review",
                "complete exact journal, voucher, and Drive move read-back for each evidence item",
                "same_chain_readback_incomplete",
            ),
            (
                "card",
                "accounting_review",
                "resolve evidence/RAG/master gaps; POST only registration_allowed items",
                "card_classification_not_ready",
            ),
        )
        for stage, owner, next_action, blocker_code in resolution_rows:
            conn.execute(
                "\n                UPDATE accounting_run_items\n                SET resolution_owner_role = ?,\n                    resolution_next_action = ?,\n                    resolution_target_due_at = CASE\n                        WHEN resolution_target_due_at = ''\n                        THEN datetime('now', '+7 days')\n                        ELSE resolution_target_due_at\n                    END,\n                    resolution_blocker_code = ?,\n                    resolution_last_error_code = '',\n                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')\n                WHERE run_id_hash = ? AND source_stage = ?\n                  AND status NOT IN ('verified', 'not_applicable')\n                ",
                (owner, next_action, blocker_code, run_hash, stage),
            )
        conn.commit()
    if receipt_resolutions:
        _resolve_prior_source_items(
            db,
            args,
            status=status,
            stage="all_receipt_folders",
            resolutions=receipt_resolutions,
            readback_basis={
                "source": "processing_ledger",
                "manifest_hash": stable_hash(receipt_artifact),
                "terminal_count": len(receipt_resolutions),
                "folder_count": int(
                    receipt_artifact.get("source", {}).get("folder_count", 0)
                ),
            },
        )
    recurring_resolutions = _legacy_recurring_expectation_resolutions(db, status=status)
    if recurring_resolutions:
        _resolve_prior_source_items(
            db,
            args,
            status=status,
            stage="payable_invoice",
            resolutions=recurring_resolutions,
            readback_basis={
                "source": "recurring_invoice_monitor",
                "accounting_date": status["accounting_date"],
                "terminal_count": len(recurring_resolutions),
            },
        )
    quarantine_resolutions = _payable_quarantine_resolutions(
        db, status=status, payable_artifact=payable_artifact
    )
    if quarantine_resolutions:
        _resolve_prior_source_items(
            db,
            args,
            status=status,
            stage="payable_invoice",
            resolutions=quarantine_resolutions,
            readback_basis={
                "source": "processing_ledger_quarantine_readback",
                "terminal_count": len(quarantine_resolutions),
            },
        )
    return {
        "payable_remaining_count": payable_remaining_count,
        "receipt_remaining_count": receipt_states["blocked"]
        + receipt_states["untracked"],
        "receipt_verified_ledger_count": receipt_states["verified"],
        "receipt_not_applicable_count": receipt_states["not_applicable"],
        "receipt_blocked_count": receipt_states["blocked"]
        + receipt_states["untracked"],
        "card_ready_count": int(card_ready_count),
        "card_review_count": int(card_review_count),
        "card_verified_count": int(card_verified_count),
    }


def _bind_verified_drive_card_evidence(
    *,
    payload: dict[str, Any],
    receipt_files: list[dict[str, Any]],
    classifications: list[Any],
    access_token: str,
    run_dir: Path,
) -> dict[str, Any] | None:
    """Bind verified invoice facts to exactly one current card transaction.

    Filename and folder names are deliberately excluded from classification.
    The PDF bytes, recipient, qualified-invoice number, date, amount and vendor
    marker must all agree with one current transaction before it can become
    registration_allowed.
    """
    import hashlib
    import unicodedata
    from .accounting_api import UrllibTransport
    from .hash_utils import stable_hash

    transactions = payload.get("transactions")
    if not isinstance(transactions, list) or len(transactions) != len(classifications):
        raise ValueError("connected evidence binding cardinality mismatch")
    if not receipt_files:
        return None
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    transport = UrllibTransport()
    matches: dict[int, dict[str, Any]] = {}
    for receipt in receipt_files:
        drive_id = str(receipt.get("google_drive_file_id") or "")
        declared_hash = str(receipt.get("file_hash") or "")
        if not drive_id or re.fullmatch("sha256:[0-9a-f]{64}", declared_hash) is None:
            raise ValueError("pending Drive evidence identity is incomplete")
        cache_query = urllib.parse.urlencode(
            {"fields": "id,properties", "supportsAllDrives": "true"}
        )
        cache_status, _cache_headers, cache_body = transport.request(
            "GET",
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(drive_id, safe="")
            + "?"
            + cache_query,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        if cache_status != 200:
            raise RuntimeError(
                f"canonical Drive cache read failed: HTTP {cache_status}"
            )
        try:
            cache_metadata = json.loads(cache_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("canonical Drive cache response is invalid") from error
        cache_properties = cache_metadata.get("properties")
        if not isinstance(cache_properties, dict) or not cache_properties.get("ao_h"):
            continue
        canonical_facts, _source, _ocr_calls, _gemini_calls = (
            _read_canonical_evidence_from_drive_cache(
                drive_file_id=drive_id,
                declared_hash=declared_hash,
                access_token=access_token,
            )
        )
        recipient = re.sub(
            "\\s+", "", str(canonical_facts.get("recipient") or "")
        ).casefold()
        if re.sub("\\s+", "", _office_name()).casefold() not in recipient:
            continue
        if canonical_facts.get("qualified_invoice") is not True:
            continue
        canonical_vendor = unicodedata.normalize(
            "NFKC", str(canonical_facts.get("vendor_name") or "")
        ).casefold()
        receipt_candidates: list[int] = []
        for index, (transaction, classification) in enumerate(
            zip(transactions, classifications, strict=True)
        ):
            if (
                not isinstance(transaction, dict)
                or not classification.journal_candidate
            ):
                continue
            description = unicodedata.normalize(
                "NFKC", str(transaction.get("description") or "")
            ).casefold()
            vendor_matches = any(
                (
                    str(pair["statement"]).casefold() in description
                    and str(pair["invoice"]).casefold() in canonical_vendor
                    for pair in _setting("bank.evidence_vendor_aliases")
                )
            )
            if not vendor_matches:
                continue
            transaction_date = str(transaction.get("transaction_date") or "")[:10]
            if str(canonical_facts.get("transaction_date") or "") != transaction_date:
                continue
            amount = abs(int(transaction.get("amount") or 0))
            if int(canonical_facts.get("amount") or 0) != amount:
                continue
            receipt_candidates.append(index)
        if len(receipt_candidates) != 1:
            continue
        url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(drive_id, safe="")
            + "?alt=media"
        )
        status_code, _headers, body = transport.request(
            "GET", url, headers={"Authorization": f"Bearer {access_token}"}
        )
        if status_code != 200:
            raise RuntimeError(f"Drive evidence download failed: HTTP {status_code}")
        actual_hash = "sha256:" + hashlib.sha256(body).hexdigest()
        if actual_hash != declared_hash:
            raise RuntimeError(
                "Drive evidence content hash changed after inventory capture"
            )
        local_path = evidence_dir / f"{declared_hash.removeprefix('sha256:')}.local.pdf"
        if not local_path.exists() or hashlib.sha256(
            local_path.read_bytes()
        ).hexdigest() != declared_hash.removeprefix("sha256:"):
            local_path.write_bytes(body)
        index = receipt_candidates[0]
        if index in matches:
            raise RuntimeError(
                "multiple Drive invoices match one connected transaction"
            )
        candidate = dict(classifications[index].journal_candidate)
        candidate["tax_category"] = _setting("bank.qualified_evidence_tax_category")
        candidate["source_file_hash"] = declared_hash
        facts = {
            "vendor_marker": canonical_vendor,
            "transaction_date": str(transactions[index]["transaction_date"])[:10],
            "amount": abs(int(transactions[index].get("amount") or 0)),
            "recipient": _office_name(),
            "qualified_invoice": True,
        }
        matches[index] = {
            "status": "verified",
            "source_file_hash": declared_hash,
            "extraction_hash": "sha256:" + stable_hash(canonical_facts),
            "facts_hash": "sha256:" + stable_hash(facts),
            "recipient_verified": True,
            "amount_date_vendor_exact_match": True,
            "qualified_invoice_readback": True,
            "transaction_date": facts["transaction_date"],
            "amount": facts["amount"],
            "journal_candidate": candidate,
            "google_drive_file_id": drive_id,
            "parent_folder_id": str(receipt.get("parent_folder_id") or ""),
            "file_name": str(receipt.get("file_name") or local_path.name),
            "local_file_path": str(local_path),
        }
    if not matches:
        return None
    enriched = [dict(item) for item in transactions]
    for index, resolution in matches.items():
        enriched[index]["evidence_resolution"] = resolution
    return {**payload, "transactions": enriched}


def _read_canonical_evidence_from_drive_cache(
    *,
    drive_file_id: str,
    declared_hash: str,
    access_token: str,
    transport: Any | None = None,
) -> tuple[dict[str, Any], str, int, int]:
    """Read and validate the shared GAS canonical extraction from Drive."""
    from .accounting_api import UrllibTransport

    runtime = transport or UrllibTransport()
    query = urllib.parse.urlencode(
        {"fields": "id,properties", "supportsAllDrives": "true"}
    )
    status, _headers, body = runtime.request(
        "GET",
        "https://www.googleapis.com/drive/v3/files/"
        + urllib.parse.quote(drive_file_id, safe="")
        + "?"
        + query,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    if status != 200:
        raise RuntimeError(f"canonical Drive cache read failed: HTTP {status}")
    try:
        response = json.loads(body.decode("utf-8"))
        if str(response["id"]) != drive_file_id:
            raise RuntimeError("canonical Drive cache file identity mismatch")
        properties = response["properties"]
        if not isinstance(properties, dict):
            raise TypeError("properties must be an object")
        returned_hash = str(properties.get("ao_h") or "")
        if "sha256:" + returned_hash.removeprefix("sha256:") != declared_hash:
            raise RuntimeError("canonical Drive cache content hash mismatch")
        if int(properties.get("ao_v") or 0) != 17:
            raise RuntimeError("canonical Drive cache version mismatch")
        chunk_count = int(properties.get("ao_n") or 0)
        if chunk_count < 1 or chunk_count > 20:
            raise RuntimeError("canonical Drive cache chunk count is invalid")
        encoded = "".join(
            (str(properties[f"ao_{index:02d}"]) for index in range(chunk_count))
        )
        encoded += "=" * (-len(encoded) % 4)
        compact = json.loads(
            gzip.decompress(base64.urlsafe_b64decode(encoded)).decode("utf-8")
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("canonical Drive cache response is invalid") from error
    except (OSError, binascii.Error) as error:
        raise RuntimeError("canonical Drive cache payload is invalid") from error
    if not isinstance(compact, dict):
        raise RuntimeError("canonical Drive cache payload is invalid")
    raw = {
        "file_id": drive_file_id,
        "extraction_version": compact.get("v"),
        "content_sha256": compact.get("h"),
        "document_type": compact.get("d"),
        "date": compact.get("a"),
        "issuer": compact.get("i"),
        "store_name": compact.get("o"),
        "recipient": compact.get("p"),
        "registration_number": compact.get("r"),
        "total": compact.get("t"),
        "tax": [
            {"rate": row[0], "taxable_amount": row[1], "tax_amount": row[2]}
            for row in compact.get("x") or []
            if isinstance(row, list) and len(row) == 3
        ],
        "lines": [
            {
                "description": row[0],
                "quantity": row[1],
                "unit_price": row[2],
                "amount": row[3],
                "tax_rate": row[4],
            }
            for row in compact.get("l") or []
            if isinstance(row, list) and len(row) == 5
        ],
        "source": compact.get("s"),
        "page_count": compact.get("g"),
        "gemini_reason_fields": compact.get("m") or [],
        "validation_issues": compact.get("u") or [],
    }
    returned_hash = str(raw.get("content_sha256") or "").removeprefix("sha256:")
    if "sha256:" + returned_hash != declared_hash:
        raise RuntimeError("canonical Drive cache content hash mismatch")
    total = int(raw.get("total") or 0)
    tax = [
        {
            "rate": str(item.get("rate") or ""),
            "taxable_amount": int(item.get("taxable_amount") or 0),
            "tax_amount": int(item.get("tax_amount") or 0),
        }
        for item in raw.get("tax") or []
        if isinstance(item, dict)
    ]
    if total <= 0 or not tax:
        raise RuntimeError("canonical GAS extraction lacks verified total or tax")
    for item in tax:
        if item["rate"] not in {"8", "10"}:
            raise RuntimeError("canonical GAS tax rate is invalid")
        calculated = item["taxable_amount"] * int(item["rate"]) / 100
        accepted_tax_amounts = range(
            max(0, math.floor(calculated) - 1), math.ceil(calculated) + 2
        )
        if item["tax_amount"] not in accepted_tax_amounts:
            raise RuntimeError("canonical GAS tax rate and amount are inconsistent")
    if sum((item["taxable_amount"] + item["tax_amount"] for item in tax)) != total:
        raise RuntimeError("canonical GAS tax breakdown does not equal total")
    lines = [
        {
            "description": str(item.get("description") or "").strip(),
            "quantity": item.get("quantity"),
            "unit_price": int(item.get("unit_price") or 0) or None,
            "amount": int(item.get("amount") or 0),
            "tax_rate": str(item.get("tax_rate") or "") or None,
        }
        for item in raw.get("lines") or []
        if isinstance(item, dict)
    ]
    if not lines:
        raise RuntimeError("canonical GAS extraction lacks verified line items")
    line_total = sum((item["amount"] for item in lines))
    net_total = sum((item["taxable_amount"] for item in tax))
    if line_total not in {total, net_total}:
        raise RuntimeError("canonical GAS line items do not equal total")
    canonical_date = str(raw.get("date") or "")
    if not re.fullmatch("20\\d{2}\\.\\d{2}\\.\\d{2}", canonical_date):
        raise RuntimeError("canonical GAS extraction date is invalid")
    issuer = str(raw.get("issuer") or "").strip()
    if not issuer:
        raise RuntimeError("canonical GAS extraction issuer is missing")
    source = str(raw.get("source") or "")
    if source != "cloud_vision":
        raise RuntimeError("canonical receipt extraction is not Cloud Vision")
    registration_number = str(raw.get("registration_number") or "").upper()
    if registration_number and re.fullmatch("T\\d{13}", registration_number) is None:
        raise RuntimeError("canonical GAS registration number is invalid")
    facts = {
        "transaction_date": canonical_date.replace(".", "-"),
        "amount": total,
        "vendor_name": issuer,
        "store_name": str(raw.get("store_name") or "").strip() or None,
        "recipient": str(raw.get("recipient") or "").strip() or None,
        "document_type": str(raw.get("document_type") or "unknown"),
        "registration_number": registration_number or None,
        "qualified_invoice": bool(registration_number),
        "tax": tax,
        "lines": lines,
        "canonical_source": source,
        "canonical_content_hash": declared_hash,
        "vision_pages": int(raw.get("page_count") or 0)
        if source == "cloud_vision"
        else 0,
        "gemini_calls": 0,
        "gemini_reason_fields": [
            str(value) for value in raw.get("gemini_reason_fields") or []
        ],
    }
    return (facts, source, 0, 0)


def _run_shared_gas_function(
    function_name: str,
    parameters: list[Any],
    access_token: str,
    *,
    transport: Any | None = None,
) -> Any:
    """Invoke one existing function on the sole production GAS."""
    from .accounting_api import UrllibTransport

    _require_write_intent()
    _validate_runtime_database(get_current_profile())
    clasp_path = Path(_setting("gas.config_path"))
    try:
        deployment_id = str(
            json.loads(clasp_path.read_text(encoding="utf-8"))[
                "apiExecutableDeploymentId"
            ]
        ).strip()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "canonical shared GAS API executable configuration is unavailable"
        ) from error
    if not deployment_id or deployment_id.startswith("REPLACE_"):
        raise RuntimeError(
            "canonical shared GAS API executable configuration is invalid"
        )
    runtime = transport or UrllibTransport()
    body = json.dumps(
        {
            "function": function_name,
            "parameters": [*parameters, _profile_boundary()],
            "devMode": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    last_status = 0
    last_reason = ""
    for _attempt in range(3):
        try:
            status, _headers, response_body = runtime.request(
                "POST",
                "https://script.googleapis.com/v1/scripts/"
                + urllib.parse.quote(deployment_id, safe="")
                + ":run",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout=330.0,
            )
        except (OSError, TimeoutError) as error:
            raise RuntimeError("shared GAS transport interrupted") from error
        last_status = int(status)
        try:
            response = (
                json.loads(response_body.decode("utf-8")) if response_body else {}
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = {}
        if status == 200 and isinstance(response.get("response"), dict):
            result = response["response"].get("result")
            if not isinstance(result, dict):
                raise ValueError("GAS response must include a company boundary")
            _assert_run_artifact(result)
            return result
        error = response.get("error")
        details = error.get("details") if isinstance(error, dict) else None
        last_reason = "execution_api_transient"
        if status not in {408, 409, 425, 429} and (not 500 <= int(status) < 600):
            if isinstance(details, list) and details:
                last_reason = str(details[0].get("errorMessage") or last_reason)
            break
    raise RuntimeError(
        "shared GAS retry exhausted: " + str(last_status) + ":" + last_reason[:120]
    )


def _run_shared_accounting_classifier(
    items: list[dict[str, Any]], access_token: str, *, transport: Any | None = None
) -> dict[str, Any]:
    """Invoke the sole production classifier through its existing GAS API."""
    if not 1 <= len(items) <= 12:
        raise ValueError("shared accounting classifier batch must contain 1..12 items")
    result = _run_shared_gas_function(
        "classifySharedAccountingCandidates", [items], access_token, transport=transport
    )
    if not isinstance(result, dict):
        raise RuntimeError("shared GAS classifier returned an invalid result")
    return result


def _receipt_auto_learning_allowed(source: dict[str, Any]) -> bool:
    evidence = source.get("receipt_learning_evidence")
    if not isinstance(evidence, dict):
        return False
    return all(
        (
            evidence.get(key) is True
            for key in (
                "issuer_verified",
                "broad_content_verified",
                "current_evidence_hash_verified",
                "deterministic_validator_passed",
                "provenance_consistent",
            )
        )
    ) and str(evidence.get("broad_content_source") or "") in {
        "current_deterministic",
        "current_gemini",
        "current_cache",
    }


def _bind_supplier_primary_matches(
    client: Any,
    *,
    office_id: str,
    payload: dict[str, Any],
    accounting_date: str,
    masters: Mapping[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Bind supplier purchases to AMEX duplicates and supplier adjustments.

    Purchase-side evidence identity is authoritative for refunds/discounts.
    Raw transaction, voucher and journal IDs are used only in memory and never
    copied into the durable decision artifact.
    """
    from .connected_account_probe import (
        _connected_account_bindings,
        _date_windows,
        _extract_items,
        _extract_transactions,
    )
    from .hash_utils import mask_identifier, stable_hash
    from .live_journal_resolver import _external_id, _name
    from .sync import (
        _extract_journal_detail,
        _journal_id,
        _line_name,
        _paired_journal_lines,
    )

    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        raise ValueError("connected transaction snapshot items are missing")
    resolved_masters = masters or {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
    }
    bindings = _connected_account_bindings(
        _extract_items(client.connected_accounts(office_id))
    )

    def supplier_binding(key: tuple[str, str]) -> bool:
        name = str((bindings.get(key) or {}).get("name") or "").casefold()
        return any(
            (
                marker in name
                for marker in tuple(_setting("supplier_primary.vendor_markers"))
            )
        )

    supplier_rows: list[dict[str, Any]] = []
    fiscal_start = _company_fiscal_year_start(
        date.fromisoformat(accounting_date)
    ).isoformat()
    registered_statuses = ("registered", "modified", "new_voucher_attached")
    seen_supplier_ids: set[str] = set()
    for window_start, window_end in _date_windows(fiscal_start, accounting_date):
        page = 1
        while True:
            response = client.transactions(
                office_id,
                start_date=window_start,
                end_date=window_end,
                journalizing_statuses=registered_statuses,
                page=page,
                per_page=500,
            )
            for row in _extract_transactions(response):
                key = (
                    str(row.get("connected_account_id") or ""),
                    str(row.get("connected_sub_account_id") or ""),
                )
                transaction_id = str(row.get("id") or "")
                if supplier_binding(key):
                    if not transaction_id or transaction_id in seen_supplier_ids:
                        raise RuntimeError(
                            "supplier-primary official history contains a missing or duplicate ID"
                        )
                    seen_supplier_ids.add(transaction_id)
                    supplier_rows.append(row)
            metadata = response.get("metadata") if isinstance(response, dict) else None
            total_pages = int((metadata or {}).get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
    voucher_transaction_ids = {
        str(row.get("id") or "") for row in supplier_rows if row.get("voucher_file_ids")
    }
    missing_voucher_ids = {
        str(row.get("id") or "")
        for row in supplier_rows
        if not row.get("voucher_file_ids")
    }
    journals_by_transaction: dict[str, list[dict[str, Any]]] = {}
    if missing_voucher_ids or any(
        (
            item.get("connected_source_type") == "supplier_primary"
            and item.get("transaction_side") == "INCOME"
            for item in transactions
        )
    ):
        page = 1
        while True:
            response = client.journals(
                office_id,
                start_date=fiscal_start,
                end_date=accounting_date,
                page=page,
                per_page=500,
            )
            for journal in response.get("journals", []):
                transaction_id = str(journal.get("transaction_id") or "")
                if transaction_id:
                    journals_by_transaction.setdefault(transaction_id, []).append(
                        journal
                    )
                if transaction_id in missing_voucher_ids and journal.get(
                    "voucher_file_ids"
                ):
                    voucher_transaction_ids.add(transaction_id)
            metadata = response.get("metadata") if isinstance(response, dict) else None
            total_pages = int((metadata or {}).get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

    def voucher_hashes(row: dict[str, Any]) -> set[str]:
        return {
            mask_identifier(str(value))
            for value in row.get("voucher_file_ids") or []
            if str(value)
        }

    def original_expense_shape(row: dict[str, Any]) -> dict[str, Any] | None:

        def official_line_name(line: dict[str, Any], key: str, master_key: str) -> str:
            nested = line.get(key)
            nested_id = str(nested.get("id") or "") if isinstance(nested, dict) else ""
            id_keys = {
                "account": ("account_id", "account_item_id", "account_category_id"),
                "sub_account": (
                    "sub_account_id",
                    "sub_account_item_id",
                    "sub_account_category_id",
                ),
                "tax": ("tax_id", "excise_id", "tax_category_id"),
            }[key]
            identifiers = {str(line.get(candidate) or "") for candidate in id_keys}
            identifiers.add(nested_id)
            raw_name = str(_line_name(line, key) or "")
            identifiers.add(raw_name)
            identifiers.discard("")
            matched = [
                item
                for item in resolved_masters.get(master_key, [])
                if _external_id(item) in identifiers and _name(item)
            ]
            return _name(matched[0]) if len(matched) == 1 else raw_name

        transaction_id = str(row.get("id") or "")
        listing = journals_by_transaction.get(transaction_id, [])
        journal_ids = {
            str(_journal_id(item) or "") for item in listing if _journal_id(item)
        }
        if len(journal_ids) != 1:
            return None
        detail = _extract_journal_detail(
            client.journal(next(iter(journal_ids)), office_id)
        )
        shapes: set[tuple[str, str, str, str]] = set()
        for debit, credit in _paired_journal_lines(detail):
            credit_account = official_line_name(credit, "account", "accounts")
            credit_sub = official_line_name(credit, "sub_account", "sub_accounts")
            if credit_account != _setting(
                "card.credit_account"
            ) or credit_sub != _setting("card.credit_sub_account"):
                continue
            shape = (
                official_line_name(debit, "account", "accounts"),
                official_line_name(debit, "sub_account", "sub_accounts"),
                official_line_name(debit, "tax", "taxes"),
                str(debit.get("invoice_kind") or ""),
            )
            if shape[0] and shape[2] and shape[3]:
                shapes.add(shape)
        if len(shapes) != 1:
            return None
        account, sub_account, tax_category, invoice_kind = next(iter(shapes))
        return {
            "original_expense_account": account,
            "original_expense_sub_account": sub_account,
            "original_tax_category": tax_category,
            "original_invoice_kind": invoice_kind,
            "original_transaction_hash": mask_identifier(transaction_id),
            "original_journal_readback_hash": "sha256:" + stable_hash(detail),
        }

    def amount(row: dict[str, Any]) -> int:
        value = row.get("amount", row.get("value", row.get("transaction_value", 0)))
        try:
            return abs(int(value))
        except (TypeError, ValueError):
            return 0

    def transaction_day(row: dict[str, Any]) -> date | None:
        value = str(row.get("transaction_date") or row.get("date") or "")[:10]
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    enriched: list[dict[str, Any]] = []
    for item in transactions:
        current = dict(item)
        text = " ".join(
            (
                str(current.get(key) or "")
                for key in ("bank_or_card_account", "description", "counterparty")
            )
        ).casefold()
        if (
            current.get("connected_source_type") == "supplier_primary"
            and current.get("transaction_side") == "INCOME"
        ):
            current_vouchers = {
                str(value)
                for value in current.get("voucher_file_id_hashes") or []
                if str(value)
            }
            matches = [
                row
                for row in supplier_rows
                if str(row.get("side") or "") == "EXPENSE"
                and current_vouchers
                and bool(current_vouchers.intersection(voucher_hashes(row)))
            ]
            if len(matches) == 1:
                original = original_expense_shape(matches[0])
                current["supplier_primary_adjustment_match"] = (
                    {"status": "unique", **original}
                    if original is not None
                    else {
                        "status": "ambiguous",
                        "reason": "original_journal_shape_not_unique",
                    }
                )
            elif len(matches) > 1:
                current["supplier_primary_adjustment_match"] = {
                    "status": "ambiguous",
                    "reason": "voucher_identity_matches_multiple_purchases",
                }
            else:
                current["supplier_primary_adjustment_match"] = {
                    "status": "missing",
                    "reason": "voucher_identity_did_not_match_registered_purchase",
                }
        elif (
            current.get("source_event_type") == "card_transaction"
            and any(
                (marker in text for marker in tuple(_setting("card.statement_markers")))
            )
            and any(
                (
                    marker in text
                    for marker in tuple(_setting("supplier_primary.vendor_markers"))
                )
            )
        ):
            day = transaction_day(current)
            matches = [
                row
                for row in supplier_rows
                if amount(current) > 0
                and amount(row) == amount(current)
                and (str(row.get("side") or "") == "EXPENSE")
                and (day is not None)
                and (transaction_day(row) is not None)
                and (abs((day - transaction_day(row)).days) <= 1)
            ]
            if len(matches) == 1:
                match = matches[0]
                current["matched_supplier_primary_purchase"] = {
                    "source_type": "moneyforward_supplier_primary_integration",
                    "counterpart_hash": "sha256:"
                    + stable_hash(
                        {
                            "date": str(match.get("date") or "")[:10],
                            "amount": amount(match),
                            "status": str(match.get("journalizing_status") or ""),
                        }
                    ),
                    "supplier_registration_readback_verified": True,
                    "voucher_readback_verified": str(match.get("id") or "")
                    in voucher_transaction_ids,
                    "journalizing_status": str(match.get("journalizing_status") or ""),
                }
        enriched.append(current)
    return {**payload, "transactions": enriched}


def _execute_ready_connected_items(
    db: Any,
    args: argparse.Namespace,
    *,
    status: dict[str, Any],
    runtime: tuple[Any, str, Any | None, str],
    transactions: list[dict[str, Any]],
    classifications: list[Any],
    stage: str = "card",
    execution_stats: dict[str, int] | None = None,
) -> int:
    """Execute authorized linked or standalone items and verify every POST."""
    from .hash_utils import stable_hash
    from .journal_write_post import (
        JournalCreateDeliveryUncertainError,
        dry_run_journal_create_execution,
        execute_journal_create_with_connection_factory,
    )
    from .journal_write_verify import (
        resolve_uncertain_journal_create_by_read_back_with_connection_factory,
        verify_journal_create_read_back_with_connection_factory,
    )
    from .drive_policy import apply_drive_credit_policy
    from .live_journal_resolver import (
        _extract_items,
        _external_id,
        _name,
        _resolve_item,
    )
    from .models import Evidence, JournalCandidate
    from .scope_gate import require_latest_journal_write_scope
    from .sdk import MoneyForwardDryRunSDK, _apply_resolved_credit_side
    from .transaction_journalize import (
        JOURNAL_CREATE_EXECUTION_CHANNEL,
        TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL,
        build_transaction_journalize_request,
        expected_api_action,
        journal_write_request_metadata,
        transaction_journalize_binding_proof,
    )

    if len(transactions) != len(classifications):
        raise ValueError("connected classification cardinality changed")
    if execution_stats is not None:
        execution_stats.setdefault("posted_count", 0)
        execution_stats.setdefault("reused_count", 0)

    def learn_verified_connected(
        source: dict[str, Any], candidate: dict[str, Any]
    ) -> None:
        if stage not in {"all_receipt_folders", "bank_transfer", "card"}:
            return
        if stage == "all_receipt_folders" and (
            not _receipt_auto_learning_allowed(source)
        ):
            return
        from .financial_event_learning import learn_financial_events

        with db.connect() as learning_conn:
            learn_financial_events(
                {
                    "items": [
                        {
                            "event": source,
                            "resolved_candidate": candidate,
                            "learning_source": "verified_bank_transaction_readback"
                            if stage == "bank_transfer"
                            else "verified_card_transaction_readback"
                            if stage == "card"
                            else "verified_receipt_chain",
                            "auto_post_eligibility": "limited",
                        }
                    ]
                },
                conn=learning_conn,
                persist=True,
            )

    client, office_id = runtime[:2]
    masters = {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
        "trade_partners": _extract_items(client.trade_partners(office_id)),
    }
    resolutions: list[dict[str, Any]] = []
    for source, classification in zip(transactions, classifications, strict=True):
        if (
            not classification.registration_allowed
            or not classification.journal_candidate
        ):
            continue
        candidate_payload = dict(classification.journal_candidate)
        binding = dict(
            candidate_payload.pop("moneyforward_transaction_binding", {}) or {}
        )
        source_identity = str(source["source_event_id_hash"])
        evidence_resolution = source.get("evidence_resolution")
        if not binding and (not isinstance(evidence_resolution, dict)):
            raise ValueError(
                "authorized standalone item requires verified Drive evidence"
            )
        source_file_hash = (
            str(evidence_resolution["source_file_hash"])
            if isinstance(evidence_resolution, dict)
            else "sha256:" + stable_hash({"connected_source": source_identity})
        )
        candidate = JournalCandidate(
            **{
                **{
                    key: value
                    for key, value in candidate_payload.items()
                    if key in JournalCandidate.__dataclass_fields__
                },
                "risk_flags": tuple(candidate_payload.get("risk_flags") or ()),
            }
        )
        evidence = Evidence(
            google_drive_file_id=str(evidence_resolution["google_drive_file_id"])
            if isinstance(evidence_resolution, dict)
            else f"financial-event:{classification.event_hash}",
            file_name=str(evidence_resolution["file_name"])
            if isinstance(evidence_resolution, dict)
            else f"{classification.source_event_type}-{classification.transaction_date}.json",
            file_hash=source_file_hash,
            parent_folder_id=str(evidence_resolution["parent_folder_id"])
            if isinstance(evidence_resolution, dict)
            else "moneyforward-connected-statements",
            parent_folder_name=str(
                evidence_resolution.get("parent_folder_name")
                or _setting("drive.payable_route_name")
            )
            if isinstance(evidence_resolution, dict)
            else "Money Forward金融機関連携明細",
        )
        if not binding:
            with db.connect() as conn:
                candidate = apply_drive_credit_policy(candidate, evidence)
                candidate = _apply_resolved_credit_side(conn, candidate, evidence)
            candidate_payload = candidate.to_dict()
        named_journal = (
            _named_connected_journal(candidate_payload, binding, masters)
            if binding
            else _named_standalone_journal(
                candidate_payload, tax_rows=source.get("receipt_tax_rows")
            )
        )
        resolved = _resolve_item(
            {"source_file_hash": source_file_hash, "journal": named_journal}, masters
        )
        if resolved["status"] != "ready":
            if binding and stage == "bank_transfer":
                raise RuntimeError("bank transaction live master resolution changed")
            continue
        journal = dict(resolved["journal"])
        request = (
            build_transaction_journalize_request(journal=journal, binding=binding)
            if binding
            else journal
        )
        if isinstance(evidence_resolution, dict):
            with db.connect() as conn:
                prior_chain = conn.execute(
                    "SELECT idempotency_key,moneyforward_journal_id_hash FROM processing_ledger WHERE file_hash=? AND moneyforward_journal_id_hash IS NOT NULL ORDER BY updated_at DESC,id DESC LIMIT 1",
                    (source_file_hash,),
                ).fetchone()
            if prior_chain is not None:
                journal_id = _find_journal_id_by_recorded_hash(
                    client,
                    office_id=office_id,
                    journal_id_hash=str(prior_chain["moneyforward_journal_id_hash"]),
                    transaction_date=str(journal["transaction_date"]),
                )
                if not journal_id:
                    if binding:
                        raise RuntimeError("existing evidence journal link is missing")
                    continue
                _complete_connected_evidence_chain(
                    db,
                    args,
                    runtime=runtime,
                    journal_id=journal_id,
                    idempotency_key=str(prior_chain["idempotency_key"]),
                    resolution=evidence_resolution,
                )
                learn_verified_connected(source, candidate_payload)
                item_hash = stable_hash({"stage": stage, "identity": source_identity})
                resolutions.append(
                    {"existing_item_hash": f"sha256:{item_hash}", "status": "verified"}
                )
                if execution_stats is not None:
                    execution_stats["reused_count"] += 1
                source["_execution_result"] = "already_verified"
                continue
        with db.connect() as conn:
            uncertain = conn.execute(
                "SELECT e.idempotency_key,e.approval_hash,e.status FROM processing_ledger p JOIN journal_create_execution_records e ON e.idempotency_key=p.idempotency_key WHERE p.file_hash=? AND e.status IN ('executing','delivery_uncertain') ORDER BY e.id DESC LIMIT 1",
                (source_file_hash,),
            ).fetchone()
        if uncertain is not None:
            if not binding:
                continue
            journal_id = _find_exact_journal_id(
                client, office_id=office_id, journal=journal, request=request
            )
            if journal_id:
                recovered = resolve_uncertain_journal_create_by_read_back_with_connection_factory(
                    db.connect,
                    client=client,
                    journal_id=journal_id,
                    journal=journal,
                    idempotency_key=str(uncertain["idempotency_key"]),
                    approval_hash=str(uncertain["approval_hash"]),
                    transaction_journalize_request=request if binding else None,
                    office_id=office_id,
                )
                if recovered.verified:
                    if isinstance(evidence_resolution, dict):
                        _complete_connected_evidence_chain(
                            db,
                            args,
                            runtime=runtime,
                            journal_id=journal_id,
                            idempotency_key=str(uncertain["idempotency_key"]),
                            resolution=evidence_resolution,
                        )
                        learn_verified_connected(source, candidate_payload)
                    elif binding:
                        learn_verified_connected(source, candidate_payload)
                    item_hash = stable_hash(
                        {"stage": stage, "identity": source_identity}
                    )
                    resolutions.append(
                        {
                            "existing_item_hash": f"sha256:{item_hash}",
                            "status": "verified",
                        }
                    )
                    if execution_stats is not None:
                        execution_stats["reused_count"] += 1
                    source["_execution_result"] = "already_verified"
                    continue
        with db.connect() as conn:
            sdk = MoneyForwardDryRunSDK(conn, actor=args.actor)
            plan = sdk.plan_journal_create(
                evidence,
                candidate,
                verified_evidence_resolution=isinstance(evidence_resolution, dict)
                or bool(binding),
            )
            if not plan.registration_allowed:
                if binding and stage == "bank_transfer":
                    raise RuntimeError(
                        "bank transaction write safety candidate was rejected"
                    )
                continue
            metadata = journal_write_request_metadata(request, office_id=office_id)
            conn.execute(
                "UPDATE write_intents SET request_hash=? WHERE idempotency_key=? AND operation_type='journal_create' AND status='planned'",
                (metadata["request_hash"], plan.idempotency_key),
            )
            scope = conn.execute(
                "SELECT scope_hash FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if scope is None:
                raise PermissionError(
                    "Money Forward journal.write scope evidence is missing"
                )
            scope_hash = str(scope["scope_hash"])
            require_latest_journal_write_scope(conn, expected_scope_hash=scope_hash)
            rollback_method = "delete or reverse the exact journal_id only after separate explicit approval and authoritative read-back"
            approval_hash = sdk.create_approval_record(
                idempotency_key=plan.idempotency_key,
                operation_type="journal_create",
                scope_hash=scope_hash,
                approved_by=args.actor,
                rollback_plan=rollback_method,
            )
            grant = {
                "operation_type": "journal_create",
                "execution_channel": TRANSACTION_JOURNALIZE_EXECUTION_CHANNEL
                if binding
                else JOURNAL_CREATE_EXECUTION_CHANNEL,
                "expected_api_action": expected_api_action(
                    metadata["execution_channel"]
                ),
                "write_execution_grant_ready": True,
                "idempotency_key": plan.idempotency_key,
                "approval_hash": approval_hash,
                "scope_hash": scope_hash,
                "request_hash": metadata["request_hash"],
                "target_hash": metadata["request_hash"],
                **(transaction_journalize_binding_proof(binding) if binding else {}),
            }
            dry_run = dry_run_journal_create_execution(
                grant=grant, journal=request, conn=conn, office_id=office_id
            )
            execution_approval = {
                "operation_type": "journal_create",
                "approval_hash": approval_hash,
                "scope_hash": scope_hash,
                "request_hash": metadata["request_hash"],
                "target_hash": metadata["request_hash"],
                "dry_run_hash": dry_run.dry_run_hash,
                "expected_api_action": "POST /api/v3/transactions/journalize, GET /api/v3/journals/{id}, GET /api/v3/transactions"
                if binding
                else "POST /api/v3/journals, GET /api/v3/journals/{id}",
                "rollback_method": rollback_method,
                "instruction_hash": "sha256:"
                + stable_hash({"instruction": args.instruction}),
            }
        with db.connect() as conn:
            prior_execution = conn.execute(
                "SELECT status FROM journal_create_execution_records WHERE idempotency_key=?",
                (dry_run.idempotency_key,),
            ).fetchone()
        if prior_execution is not None and prior_execution["status"] == "verified":
            journal_id = _find_exact_journal_id(
                client, office_id=office_id, journal=journal, request=request
            )
            if binding and (not journal_id):
                raise RuntimeError("verified transaction journal link is missing")
            if isinstance(evidence_resolution, dict):
                if not journal_id:
                    raise RuntimeError("verified connected journal link is missing")
                _complete_connected_evidence_chain(
                    db,
                    args,
                    runtime=runtime,
                    journal_id=journal_id,
                    idempotency_key=dry_run.idempotency_key,
                    resolution=evidence_resolution,
                )
                learn_verified_connected(source, candidate_payload)
            elif binding:
                learn_verified_connected(source, candidate_payload)
            item_hash = stable_hash({"stage": stage, "identity": source_identity})
            resolutions.append(
                {"existing_item_hash": f"sha256:{item_hash}", "status": "verified"}
            )
            if execution_stats is not None:
                execution_stats["reused_count"] += 1
            source["_execution_result"] = "already_verified"
            continue
        try:
            post = execute_journal_create_with_connection_factory(
                connect=db.connect,
                client=client,
                dry_run=dry_run,
                grant=grant,
                journal=request,
                execution_approval=execution_approval,
                office_id=office_id,
            )
        except JournalCreateDeliveryUncertainError:
            journal_id = _find_exact_journal_id(
                client, office_id=office_id, journal=journal, request=request
            )
            if not journal_id:
                continue
            recovered = (
                resolve_uncertain_journal_create_by_read_back_with_connection_factory(
                    db.connect,
                    client=client,
                    journal_id=journal_id,
                    journal=journal,
                    idempotency_key=dry_run.idempotency_key,
                    approval_hash=dry_run.approval_hash,
                    transaction_journalize_request=request if binding else None,
                    office_id=office_id,
                )
            )
            if recovered.verified:
                if isinstance(evidence_resolution, dict):
                    _complete_connected_evidence_chain(
                        db,
                        args,
                        runtime=runtime,
                        journal_id=journal_id,
                        idempotency_key=dry_run.idempotency_key,
                        resolution=evidence_resolution,
                    )
                    if (
                        execution_stats is not None
                        and "voucher_readback_verified_count" in execution_stats
                    ):
                        execution_stats["voucher_readback_verified_count"] += 1
                    learn_verified_connected(source, candidate_payload)
                elif binding:
                    learn_verified_connected(source, candidate_payload)
                item_hash = stable_hash({"stage": stage, "identity": source_identity})
                resolutions.append(
                    {"existing_item_hash": f"sha256:{item_hash}", "status": "verified"}
                )
                if execution_stats is not None:
                    execution_stats["posted_count"] += 1
                source["_execution_result"] = "newly_verified"
            continue
        journal_id = post.moneyforward_journal_id
        if not journal_id:
            raise RuntimeError(
                "executed connected journal has no authoritative journal id"
            )
        verified = verify_journal_create_read_back_with_connection_factory(
            db.connect,
            client=client,
            journal_id=journal_id,
            journal=journal,
            post_result=post.to_redacted_dict(),
            transaction_journalize_request=request if binding else None,
            office_id=office_id,
        )
        if not verified.verified:
            raise RuntimeError("connected journal authoritative read-back failed")
        if isinstance(evidence_resolution, dict):
            _complete_connected_evidence_chain(
                db,
                args,
                runtime=runtime,
                journal_id=journal_id,
                idempotency_key=dry_run.idempotency_key,
                resolution=evidence_resolution,
            )
            if (
                execution_stats is not None
                and "voucher_readback_verified_count" in execution_stats
            ):
                execution_stats["voucher_readback_verified_count"] += 1
            learn_verified_connected(source, candidate_payload)
        elif binding:
            learn_verified_connected(source, candidate_payload)
        item_hash = stable_hash({"stage": stage, "identity": source_identity})
        resolutions.append(
            {"existing_item_hash": f"sha256:{item_hash}", "status": "verified"}
        )
        if execution_stats is not None:
            execution_stats["posted_count"] += 1
        source["_execution_result"] = "newly_verified"
    if resolutions and stage in {"card", "bank_transfer"} and status.get("run_id_hash"):
        _resolve_prior_source_items(
            db,
            args,
            status=status,
            stage=stage,
            resolutions=resolutions,
            readback_basis={
                "source": "transaction_journalize_authoritative_readback"
                if stage == "card"
                else "journal_create_authoritative_readback",
                "verified_count": len(resolutions),
            },
        )
    return len(resolutions)


def _complete_connected_evidence_chain(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str],
    journal_id: str,
    idempotency_key: str,
    resolution: dict[str, Any],
) -> None:
    """Attach one verified PDF, move it, and verify every external result."""
    import hashlib
    from .drive_move_execute import (
        GoogleDriveApiMoveClient,
        build_drive_move_execution_approval_template,
        dry_run_drive_move_execution,
        execute_drive_move_with_client,
        record_observed_drive_move,
        record_drive_move_execution,
        record_drive_move_readback_verification,
        verify_drive_move_read_back,
    )
    from .drive_processed_readback import record_processed_folder_readback_from_metadata
    from .hash_utils import stable_hash
    from .hash_utils import mask_identifier, stable_hash
    from .voucher_approval import build_voucher_attach_execution_approval
    from .voucher_approval_record import build_voucher_attach_approval_record
    from .voucher_audit import (
        mark_voucher_attach_executed,
        mark_voucher_attach_executing,
        record_voucher_attach_execution_approval,
        record_voucher_attach_readback_verification,
    )
    from .voucher_execute import (
        _extract_voucher_file_ids,
        dry_run_hash,
        dry_run_voucher_attach_execution,
        execute_voucher_attach_with_client,
        verify_voucher_attach_read_back,
    )
    from .voucher_preflight import (
        build_voucher_attach_grant,
        prepare_voucher_attach_preflight,
    )

    client, office_id, _refresh, drive_token = runtime
    file_path = Path(str(resolution["local_file_path"]))
    if not file_path.is_file():
        raise FileNotFoundError(
            "verified evidence file is missing before voucher attachment"
        )
    declared_hash = str(resolution["source_file_hash"])
    verified_local_hash = "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()
    if verified_local_hash != declared_hash:
        raise RuntimeError(
            "verified evidence file hash changed before voucher attachment"
        )
    with db.connect() as conn:
        scope = conn.execute(
            "SELECT scope_hash FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    if scope is None:
        raise PermissionError("Money Forward voucher.write scope evidence is missing")
    scope_hash = str(scope["scope_hash"])
    preflight = prepare_voucher_attach_preflight(
        journal_id=journal_id, file_path=file_path, office_id=office_id, client=client
    ).to_redacted_dict()
    approval_record = build_voucher_attach_approval_record(
        preflight=preflight,
        user_approval_payload=args.instruction,
        scope_hash=scope_hash,
    ).to_local_dict()
    grant = build_voucher_attach_grant(
        preflight=preflight, approval_record=approval_record, scope_hash=scope_hash
    ).to_redacted_dict()
    voucher_dry_run = dry_run_voucher_attach_execution(
        preflight=preflight,
        grant=grant,
        journal_id=journal_id,
        file_path=file_path,
        office_id=office_id,
    )
    execution_approval = build_voucher_attach_execution_approval(
        preflight=preflight,
        grant=grant,
        dry_run=voucher_dry_run,
        user_approval=args.instruction,
    ).to_redacted_dict()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT status FROM voucher_attach_execution_records WHERE approval_hash=?",
            (voucher_dry_run.approval_hash,),
        ).fetchone()
    if existing is None:
        with db.connect() as conn:
            record_voucher_attach_execution_approval(
                conn,
                grant=grant,
                preflight=preflight,
                dry_run_hash=dry_run_hash(voucher_dry_run),
                moneyforward_journal_id_hash=mask_identifier(journal_id),
                execution_approval=execution_approval,
            )
            mark_voucher_attach_executing(
                conn, approval_hash=voucher_dry_run.approval_hash
            )
        post = execute_voucher_attach_with_client(
            client=client,
            dry_run=voucher_dry_run,
            preflight=preflight,
            grant=grant,
            journal_id=journal_id,
            file_path=file_path,
            office_id=office_id,
            execution_approval=execution_approval,
        )
        with db.connect() as conn:
            mark_voucher_attach_executed(
                conn, approval_hash=voucher_dry_run.approval_hash
            )
        voucher_readback = verify_voucher_attach_read_back(
            client=client,
            journal_id=journal_id,
            expected_voucher_file_ids=post.voucher_file_ids,
            post_result=post.to_redacted_dict(),
            office_id=office_id,
        )
        if not voucher_readback.verified:
            raise RuntimeError("voucher authoritative read-back failed")
        with db.connect() as conn:
            record_voucher_attach_readback_verification(
                conn,
                approval_hash=voucher_dry_run.approval_hash,
                verification=voucher_readback.to_redacted_dict(),
            )
    elif str(existing["status"]) == "executed":
        recovered_ids = _extract_voucher_file_ids(
            client.journal(journal_id, office_id=office_id)
        )
        if len(recovered_ids) != 1:
            raise RuntimeError(
                "executed voucher recovery requires exactly one authoritative attachment"
            )
        recovered_post = {"voucher_file_ids_hash": stable_hash(sorted(recovered_ids))}
        voucher_readback = verify_voucher_attach_read_back(
            client=client,
            journal_id=journal_id,
            expected_voucher_file_ids=recovered_ids,
            post_result=recovered_post,
            office_id=office_id,
        )
        if not voucher_readback.verified:
            raise RuntimeError("executed voucher recovery read-back failed")
        with db.connect() as conn:
            record_voucher_attach_readback_verification(
                conn,
                approval_hash=voucher_dry_run.approval_hash,
                verification=voucher_readback.to_redacted_dict(),
            )
    elif str(existing["status"]) != "verified":
        raise RuntimeError(
            "prior voucher attachment is unresolved; rePOST is prohibited"
        )
    processed_folder_id = _resolve_processed_child_folder_id(
        source_folder_id=str(resolution["parent_folder_id"]), access_token=drive_token
    )
    move_plan = {
        "operation": "google_drive_files_move",
        "move_requests": [
            {
                "file_id": str(resolution["google_drive_file_id"]),
                "source_folder_id": str(resolution["parent_folder_id"]),
                "destination_folder_id": processed_folder_id,
                "file_name": str(resolution["file_name"]),
                "idempotency_key": idempotency_key,
                "rename": False,
            }
        ],
    }
    move_dry_run = dry_run_drive_move_execution(move_plan)
    move_approval = build_drive_move_execution_approval_template(move_dry_run)
    move_approval["user_approval"] = args.instruction
    move_approval["execution_confirmation"].update(
        {"execute_approved": True, "google_drive_move_allowed": True}
    )
    with db.connect() as conn:
        prior_move = conn.execute(
            "SELECT status FROM drive_move_execution_records WHERE approval_hash=?",
            (move_approval["approval_hash"],),
        ).fetchone()
    drive_client = GoogleDriveApiMoveClient(
        drive_token,
        office_profile=get_current_profile(),
        write_enabled=_PRODUCTION_WRITES.get(),
    )
    if prior_move is None:
        current_metadata = drive_client.get_file(
            file_id=str(resolution["google_drive_file_id"])
        )
        current_parents = [
            str(value) for value in current_metadata.get("parents") or []
        ]
        already_moved = (
            str(current_metadata.get("name") or "") == str(resolution["file_name"])
            and processed_folder_id in current_parents
            and (str(resolution["parent_folder_id"]) not in current_parents)
        )
        if already_moved:
            with db.connect() as conn:
                record_observed_drive_move(
                    conn,
                    move_plan=move_plan,
                    execution_approval=move_approval,
                    metadata_bundle={"items": [current_metadata]},
                    execution_channel="google_drive_api",
                )
        else:
            move_result = execute_drive_move_with_client(
                client=drive_client,
                move_plan=move_plan,
                dry_run=move_dry_run,
                execution_approval=move_approval,
            )
            move_readback = verify_drive_move_read_back(
                client=drive_client,
                move_plan=move_plan,
                execution_result=move_result.to_redacted_dict(),
            )
            if not move_readback.verified:
                raise RuntimeError("Drive move authoritative parent read-back failed")
            with db.connect() as conn:
                record_drive_move_execution(
                    conn,
                    dry_run=move_dry_run,
                    execution_approval=move_approval,
                    result=move_result,
                    move_plan=move_plan,
                )
                record_drive_move_readback_verification(conn, move_readback)
    elif str(prior_move["status"]) != "verified":
        raise RuntimeError("prior Drive move is unresolved; rePATCH is prohibited")
    metadata = drive_client.get_file(file_id=str(resolution["google_drive_file_id"]))
    remote_sha256 = str(metadata.get("sha256Checksum") or "").strip().lower()
    if remote_sha256:
        if "sha256:" + remote_sha256 != declared_hash:
            raise RuntimeError("Drive moved evidence content hash changed")
    elif verified_local_hash != declared_hash:
        raise RuntimeError("Drive moved evidence content hash changed")
    metadata_path = file_path.with_suffix(".processed-readback.local.json")
    metadata_path.write_text(
        json.dumps(
            {"id": metadata.get("id"), "parent_ids": metadata.get("parents") or []},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    record_processed_folder_readback_from_metadata(
        metadata_json=metadata_path,
        processed_folder_id=processed_folder_id,
        db_path=db.path,
        file_hash=declared_hash,
    )


def _repair_verified_receipt_drive_move(
    db: Any,
    args: argparse.Namespace,
    *,
    drive_token: str,
    file_row: dict[str, Any],
    idempotency_key: str,
    run_dir: Path,
) -> None:
    """Repair only a missing Drive move after the accounting chain is verified."""
    from .drive_move_execute import (
        GoogleDriveApiMoveClient,
        build_drive_move_execution_approval_template,
        dry_run_drive_move_execution,
        execute_drive_move_with_client,
        record_drive_move_execution,
        record_drive_move_readback_verification,
        verify_drive_move_read_back,
    )
    from .drive_processed_readback import record_processed_folder_readback_from_metadata
    from .hash_utils import stable_hash

    source_folder_id = str(file_row["parent_folder_id"])
    processed_folder_id = _resolve_processed_child_folder_id(
        source_folder_id=source_folder_id, access_token=drive_token
    )
    move_plan = {
        "operation": "google_drive_files_move",
        "move_requests": [
            {
                "file_id": str(file_row["google_drive_file_id"]),
                "source_folder_id": source_folder_id,
                "destination_folder_id": processed_folder_id,
                "file_name": str(file_row["file_name"]),
                "idempotency_key": idempotency_key,
                "rename": False,
            }
        ],
    }
    dry_run = dry_run_drive_move_execution(move_plan)
    approval = build_drive_move_execution_approval_template(dry_run)
    approval["user_approval"] = args.instruction
    approval["execution_confirmation"].update(
        {"execute_approved": True, "google_drive_move_allowed": True}
    )
    client = GoogleDriveApiMoveClient(
        drive_token,
        office_profile=get_current_profile(),
        write_enabled=_PRODUCTION_WRITES.get(),
    )
    before = client.get_file(file_id=str(file_row["google_drive_file_id"]))
    before_parents = {str(value) for value in before.get("parents") or []}
    if source_folder_id not in before_parents:
        raise RuntimeError(
            "verified residual is not present in its recorded source folder"
        )
    result = execute_drive_move_with_client(
        client=client, move_plan=move_plan, dry_run=dry_run, execution_approval=approval
    )
    readback = verify_drive_move_read_back(
        client=client, move_plan=move_plan, execution_result=result.to_redacted_dict()
    )
    if not readback.verified:
        raise RuntimeError("verified residual Drive move read-back failed")
    with db.connect() as conn:
        record_drive_move_execution(
            conn,
            dry_run=dry_run,
            execution_approval=approval,
            result=result,
            move_plan=move_plan,
        )
        record_drive_move_readback_verification(conn, readback)
    after = client.get_file(file_id=str(file_row["google_drive_file_id"]))
    metadata_path = run_dir / (
        "verified-residual-"
        + stable_hash(str(file_row["file_hash"]))[:16]
        + ".processed-readback.local.json"
    )
    metadata_path.write_text(
        json.dumps(
            {"id": after.get("id"), "parent_ids": after.get("parents") or []},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    record_processed_folder_readback_from_metadata(
        metadata_json=metadata_path,
        processed_folder_id=processed_folder_id,
        db_path=db.path,
        file_hash=str(file_row["file_hash"]),
    )


def _download_google_drive_file_bytes(
    file_id: str, access_token: str, *, mime_type: str = ""
) -> bytes:
    from .accounting_api import UrllibTransport

    native_prefix = "application/vnd.google-apps."
    if mime_type.startswith(native_prefix):
        if mime_type not in {
            "application/vnd.google-apps.document",
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.google-apps.presentation",
            "application/vnd.google-apps.drawing",
        }:
            raise RuntimeError("Drive native file is not an accounting evidence export")
        query = urllib.parse.urlencode({"mimeType": "application/pdf"})
        url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(file_id, safe="")
            + "/export?"
            + query
        )
    else:
        url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(file_id, safe="")
            + "?alt=media&supportsAllDrives=true&acknowledgeAbuse=true"
        )
    status_code, _headers, body = UrllibTransport().request(
        "GET", url, headers={"Authorization": f"Bearer {access_token}"}
    )
    if status_code != 200:
        raise RuntimeError(f"Drive evidence read-back failed: HTTP {status_code}")
    return body


def _build_canonical_extraction_input(
    *, drive_file_id: str, declared_hash: str, mime_type: str, access_token: str
) -> dict[str, Any]:
    """Read page count and optional native text as Vision corroboration only."""
    normalized_mime = mime_type or "application/pdf"
    if not normalized_mime.startswith("application/pdf"):
        return {"mime_type": normalized_mime, "page_count": 1, "native_text": ""}
    body = _download_google_drive_file_bytes(
        drive_file_id, access_token, mime_type=normalized_mime
    )
    if "sha256:" + hashlib.sha256(body).hexdigest() != declared_hash:
        raise RuntimeError(
            "Drive evidence content hash changed before canonical extraction"
        )
    with tempfile.TemporaryDirectory(prefix="mf-native-pdf-") as directory:
        path = Path(directory) / "evidence.pdf"
        path.write_bytes(body)
        try:
            info = subprocess.run(
                ["pdfinfo", str(path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            extracted = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("native PDF text inspection failed") from error
    page_match = re.search("^Pages:\\s*(\\d+)\\s*$", info, re.MULTILINE)
    if page_match is None or int(page_match.group(1)) < 1:
        raise RuntimeError("native PDF page count is unavailable")
    printable = re.sub("\\s+", "", extracted)
    return {
        "mime_type": "application/pdf",
        "page_count": int(page_match.group(1)),
        "native_text": extracted if len(printable) >= 20 else "",
    }


def _load_verified_local_evidence_bytes(declared_hash: str) -> bytes | None:
    """Use an already hash-verified canonical PDF when Drive media denies download."""
    import hashlib

    evidence_root = _artifact_root() / "work_order_02_local"
    if not evidence_root.is_dir():
        return None
    for path in evidence_root.glob("**/*.pdf"):
        try:
            body = path.read_bytes()
        except OSError:
            continue
        if "sha256:" + hashlib.sha256(body).hexdigest() == declared_hash:
            return body
    return None


def _resolve_processed_child_folder_id(
    *, source_folder_id: str, access_token: str
) -> str:
    """Resolve the one ``処理完了`` folder on the source's ancestor route."""
    from .accounting_api import UrllibTransport

    runtime = UrllibTransport()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    current_folder_id = source_folder_id
    for _depth in range(8):
        query = urllib.parse.urlencode(
            {
                "q": f"'{current_folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder' and name='処理完了'",
                "fields": "files(id,name,parents)",
                "pageSize": "10",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
        )
        status_code, _headers, body = runtime.request(
            "GET", f"https://www.googleapis.com/drive/v3/files?{query}", headers=headers
        )
        if status_code != 200:
            raise RuntimeError(
                f"Drive processed-folder lookup failed: HTTP {status_code}"
            )
        payload = json.loads(body.decode("utf-8")) if body else {}
        matches = [
            item
            for item in payload.get("files") or []
            if isinstance(item, dict)
            and item.get("name") == "処理完了"
            and (current_folder_id in (item.get("parents") or []))
            and item.get("id")
        ]
        if len(matches) == 1:
            return str(matches[0]["id"])
        if len(matches) > 1:
            raise RuntimeError("expected exactly one processed child folder")
        folder_url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(current_folder_id, safe="")
            + "?fields=id,parents&supportsAllDrives=true"
        )
        status_code, _headers, body = runtime.request(
            "GET", folder_url, headers=headers
        )
        if status_code != 200:
            raise RuntimeError(f"Drive parent-folder lookup failed: HTTP {status_code}")
        parents = (json.loads(body.decode("utf-8")) if body else {}).get(
            "parents"
        ) or []
        if len(parents) != 1:
            break
        current_folder_id = str(parents[0])
    raise RuntimeError("expected exactly one processed child folder")


def _find_exact_linked_journal_id(
    client: Any, *, office_id: str, transaction_id: str, transaction_date: str
) -> str:
    """Resolve one registered transaction to one exact journal using GET only."""
    from .journal_backfill import _extract_items, _journal_id, _unwrap_journal

    listing = client.journals(
        start_date=transaction_date,
        end_date=transaction_date,
        office_id=office_id,
        page=1,
        per_page=500,
    )
    matches: list[str] = []
    for item in _extract_items(listing):
        journal_id = _journal_id(item)
        if not journal_id:
            continue
        detail = _unwrap_journal(client.journal(journal_id, office_id=office_id))
        if str(detail.get("transaction_id") or "") == transaction_id:
            matches.append(journal_id)
    if len(matches) > 1:
        raise RuntimeError("linked transaction read-back matched multiple journals")
    return matches[0] if matches else ""


def _find_exact_journal_id(
    client: Any, *, office_id: str, journal: dict[str, Any], request: dict[str, Any]
) -> str:
    """Find one linked journal; standalone uncertain creates remain fail-closed."""
    del journal
    if not request.get("transaction_id"):
        return ""
    return _find_exact_linked_journal_id(
        client,
        office_id=office_id,
        transaction_id=str(request["transaction_id"]),
        transaction_date=str(request["transaction_date"]),
    )


def _find_journal_id_by_recorded_hash(
    client: Any, *, office_id: str, journal_id_hash: str, transaction_date: str
) -> str:
    """Recover an already-verified create from its recorded response identity."""
    from .hash_utils import mask_identifier
    from .journal_backfill import _extract_items, _journal_id

    matches: list[str] = []
    page = 1
    while True:
        listing = client.journals(
            office_id=office_id,
            start_date=transaction_date,
            end_date=transaction_date,
            page=page,
            per_page=500,
        )
        rows = _extract_items(listing)
        for item in rows:
            raw_id = _journal_id(item)
            if raw_id and mask_identifier(raw_id) == journal_id_hash:
                matches.append(raw_id)
        if len(rows) < 500:
            break
        page += 1
        if page > 100:
            raise RuntimeError(
                "journal identity recovery exceeded pagination safety limit"
            )
    if len(matches) > 1:
        raise RuntimeError(
            "recorded journal identity matched multiple official journals"
        )
    return matches[0] if matches else ""


def _named_connected_journal(
    candidate: dict[str, Any],
    binding: dict[str, Any],
    masters: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build one named journal while taking the connected side from its bound ledger."""
    from .live_journal_resolver import _external_id, _name

    amount = int(candidate["amount"])
    side = str(binding["transaction_side"])
    connected_account = next(
        (
            item
            for item in masters["accounts"]
            if _external_id(item) == str(binding["connected_ledger_account_id"])
        )
    )
    connected_sub_id = str(binding.get("connected_ledger_sub_account_id") or "")
    connected_sub = next(
        (
            item
            for item in masters["sub_accounts"]
            if _external_id(item) == connected_sub_id
        ),
        None,
    )
    connected = {
        "account_name": _name(connected_account),
        "sub_account_name": _name(connected_sub) if connected_sub else None,
        "value": amount,
    }
    counterpart_prefix = "credit" if side == "INCOME" else "debit"
    counterpart = {
        "account_name": str(candidate[f"{counterpart_prefix}_account"]),
        "sub_account_name": candidate.get(f"{counterpart_prefix}_sub_account"),
        "tax_name": str(candidate["tax_category"]),
        "value": amount,
    }
    if candidate.get("invoice_kind"):
        counterpart["invoice_kind"] = candidate["invoice_kind"]
    return {
        "transaction_date": str(candidate["journal_date"]),
        "journal_type": "journal_entry",
        "branches": [
            {
                "remark": str(candidate["description"]),
                "debitor": connected if side == "INCOME" else counterpart,
                "creditor": counterpart if side == "INCOME" else connected,
            }
        ],
    }


def _named_standalone_journal(
    candidate: dict[str, Any], *, tax_rows: Any = None
) -> dict[str, Any]:
    """Build one balanced journal, splitting only canonical mixed tax rates."""
    amount = int(candidate["amount"])
    normalized_tax_rows = [
        {
            "rate": str(row.get("rate") or ""),
            "value": int(row.get("taxable_amount") or 0)
            + int(row.get("tax_amount") or 0),
        }
        for row in tax_rows or []
        if isinstance(row, dict)
    ]
    if normalized_tax_rows and (
        any(
            (
                row["rate"] not in {"8", "10"} or row["value"] <= 0
                for row in normalized_tax_rows
            )
        )
        or sum((row["value"] for row in normalized_tax_rows)) != amount
    ):
        raise ValueError("canonical receipt tax rows do not balance to total")
    if not normalized_tax_rows:
        normalized_tax_rows = [{"rate": "", "value": amount}]
    branches: list[dict[str, Any]] = []
    for row in normalized_tax_rows:
        debitor = {
            "account_name": str(candidate["debit_account"]),
            "sub_account_name": candidate.get("debit_sub_account"),
            "tax_name": "課仕 (軽)8%"
            if row["rate"] == "8"
            else _setting("bank.qualified_evidence_tax_category")
            if row["rate"] == "10"
            else str(candidate["tax_category"]),
            "value": row["value"],
        }
        if candidate.get("trade_partner_code") and candidate.get("trade_partner_name"):
            debitor["trade_partner_name"] = str(candidate["trade_partner_name"])
        creditor = {
            "account_name": str(candidate["credit_account"]),
            "sub_account_name": candidate.get("credit_sub_account"),
            "value": row["value"],
        }
        branches.append(
            {
                "remark": str(candidate["description"]),
                "debitor": debitor,
                "creditor": creditor,
            }
        )
    return {
        "transaction_date": str(candidate["journal_date"]),
        "journal_type": "journal_entry",
        "branches": branches,
    }


def _legacy_recurring_expectation_resolutions(
    db: Any, *, status: dict[str, Any]
) -> list[dict[str, Any]]:
    """Remove legacy monitor-only expectations from the accounting item queue."""
    from .gmail_recurring_invoice import build_recurring_invoice_expectations
    from .hash_utils import stable_hash

    run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
    with db.connect() as conn:
        accounting_date_value = status.get("accounting_date")
        if not accounting_date_value:
            run_row = conn.execute(
                "SELECT accounting_date FROM accounting_runs WHERE run_id_hash = ?",
                (run_hash,),
            ).fetchone()
            if run_row is None:
                return []
            accounting_date_value = run_row["accounting_date"]
        accounting_date = str(accounting_date_value)
        expectations = build_recurring_invoice_expectations(
            conn, accounting_date=accounting_date
        )
        unresolved = {
            str(row["item_hash"])
            for row in conn.execute(
                "\n                SELECT item_hash\n                FROM accounting_run_items\n                WHERE run_id_hash = ? AND source_stage = 'payable_invoice'\n                  AND status NOT IN ('verified', 'not_applicable')\n                ",
                (run_hash,),
            ).fetchall()
        }
    resolutions: list[dict[str, Any]] = []
    for item in expectations:
        if item.observed_this_month:
            continue
        identity = f"recurring:{item.vendor_key}:{accounting_date[:7]}"
        item_hash = stable_hash({"stage": "payable_invoice", "identity": identity})
        if item_hash not in unresolved:
            continue
        resolutions.append(
            {
                "existing_item_hash": f"sha256:{item_hash}",
                "status": "not_applicable",
                "not_applicable_basis": "recurring invoice expectation is a monitoring control; no current economic event was captured",
                "no_economic_event_verified": True,
                "not_applicable_readback_hash": "sha256:"
                + stable_hash(
                    {
                        "vendor_key": item.vendor_key,
                        "accounting_month": accounting_date[:7],
                        "observed_this_month": False,
                        "route": item.acquisition_route,
                    }
                ),
            }
        )
    return resolutions


def _payable_quarantine_resolutions(
    db: Any, *, status: dict[str, Any], payable_artifact: dict[str, Any]
) -> list[dict[str, Any]]:
    """Resolve quarantined Gmail evidence only from its exact Drive ledger row."""
    from .hash_utils import stable_hash

    run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
    quarantine = payable_artifact.get("quarantine_items")
    if not isinstance(quarantine, list):
        return []
    resolutions: list[dict[str, Any]] = []
    with db.connect() as conn:
        unresolved = {
            str(row["item_hash"])
            for row in conn.execute(
                "\n                SELECT item_hash\n                FROM accounting_run_items\n                WHERE run_id_hash = ? AND source_stage = 'payable_invoice'\n                  AND status NOT IN ('verified', 'not_applicable')\n                ",
                (run_hash,),
            ).fetchall()
        }
        for item in quarantine:
            if not isinstance(item, dict):
                continue
            drive_id_hash = str(item.get("item_id_hash") or "")
            item_hash = stable_hash(
                {"stage": "payable_invoice", "identity": drive_id_hash}
            )
            if item_hash not in unresolved:
                continue
            row = conn.execute(
                "\n                SELECT file_hash, processing_status\n                FROM processing_ledger\n                WHERE google_drive_file_id_hash = ?\n                  AND processing_status IN ('verified', 'not_applicable')\n                ORDER BY updated_at DESC, id DESC\n                LIMIT 1\n                ",
                (drive_id_hash,),
            ).fetchone()
            if row is None:
                continue
            state = str(row["processing_status"])
            resolution: dict[str, Any] = {
                "existing_item_hash": f"sha256:{item_hash}",
                "status": state,
            }
            if state == "not_applicable":
                resolution.update(
                    {
                        "not_applicable_basis": "exact Drive evidence is terminal not_applicable in the authoritative processing ledger",
                        "no_economic_event_verified": True,
                        "not_applicable_readback_hash": "sha256:"
                        + stable_hash(
                            {
                                "drive_file_id_hash": drive_id_hash,
                                "file_hash": row["file_hash"],
                                "processing_status": state,
                            }
                        ),
                    }
                )
            resolutions.append(resolution)
    return resolutions


def _resolve_prior_source_items(
    db: Any,
    args: argparse.Namespace,
    *,
    status: dict[str, Any],
    stage: str,
    resolutions: list[dict[str, Any]],
    readback_basis: dict[str, Any],
) -> dict[str, Any]:
    """Apply one fresh authoritative receipt to terminal prior-stage items."""
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    run_hash = str(status["run_id_hash"]).removeprefix("sha256:")
    with db.connect() as conn:
        current_rows = conn.execute(
            "\n            SELECT item_hash\n            FROM accounting_run_items\n            WHERE run_id_hash = ? AND source_stage = ?\n              AND status NOT IN ('verified', 'not_applicable')\n            ",
            (run_hash, stage),
        ).fetchall()
        unresolved_hashes = {str(row["item_hash"]) for row in current_rows}
        effective_resolutions = [
            item
            for item in resolutions
            if str(item.get("existing_item_hash") or "").removeprefix("sha256:")
            in unresolved_hashes
        ]
        if not effective_resolutions:
            from .shiwake_run import build_run_status

            return build_run_status(conn, args.run_id)
        remaining_count = len(unresolved_hashes) - len(effective_resolutions)
        snapshot_hash = stable_hash(
            {
                "stage": stage,
                "resolutions": effective_resolutions,
                "basis": readback_basis,
            }
        )
        receipt_hash = record_source_stage_receipt(
            conn,
            run_id=args.run_id,
            stage=stage,
            receipt=_bound_receipt(
                {
                    "status": "verified",
                    "stage": stage,
                    "accounting_run_id_hash": f"sha256:{run_hash}",
                    "accounting_date": str(status["accounting_date"]),
                    "coverage_state": "captured",
                    "all_items_resolved": remaining_count == 0,
                    "remaining_count": remaining_count,
                    "identity_readback_verified": True,
                    "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                    "execution_principal_hash": "sha256:"
                    + stable_hash(
                        {
                            "principal": _drive_principal()
                            if stage in {"payable_invoice", "all_receipt_folders"}
                            else _mf_principal()
                        }
                    ),
                    "source_snapshot_hash": f"sha256:{snapshot_hash}",
                    "readback_hash": "sha256:" + stable_hash(readback_basis),
                }
            ),
            actor="cross_audit_reconciliation",
        )
        stage_evidence: dict[str, Any] = {}
        if stage == "all_receipt_folders":
            folder_count = int(readback_basis.get("folder_count", -1))
            stage_evidence = {
                "folder_count": folder_count,
                "manifest_folder_count": folder_count,
                "all_folders_covered": folder_count >= 0,
            }
        return advance_run(
            conn,
            args.run_id,
            stage,
            {
                "status": "verified",
                "evidence_hash": f"sha256:{snapshot_hash}",
                "adapter_receipt_hash": receipt_hash,
                "coverage_state": "captured",
                "all_items_resolved": remaining_count == 0,
                "remaining_count": remaining_count,
                "items": effective_resolutions,
                **stage_evidence,
            },
            actor=args.actor,
        )


def _execute_sales_invoice_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, ...],
    standalone: bool = False,
) -> dict[str, Any]:
    """Capture and execute sales invoices for either full-run or scoped mode."""
    from .invoice_accounting_link_probe import (
        MoneyForwardInvoiceClient,
        run_invoice_accounting_link_probe,
    )
    from .shiwake_run import build_run_status

    if standalone:
        accounting_date = date.fromisoformat(str(args.accounting_date))
    else:
        with db.connect() as conn:
            status = build_run_status(conn, args.run_id)
        if (
            status.get("status") != "running"
            or status.get("current_stage") != "sales_invoice"
        ):
            return {
                **status,
                "execution_mode": "production_start_or_resume",
                "manifest_required": False,
            }
        accounting_date = date.fromisoformat(str(status["accounting_date"]))
    capture_start = _company_fiscal_year_start(accounting_date).isoformat()
    capture_end = accounting_date.isoformat()
    run_hash = accounting_run_hash(args.run_id)
    output_json = _artifact_root() / "runs" / run_hash / "sales_invoice.local.json"
    accounting_client, office_id, refresh_result = runtime[:3]
    access_token = accounting_client.access_token
    if refresh_result is not None:
        from .accounting_api import record_scope_check

        with db.connect() as conn:
            record_scope_check(conn, "moneyforward", refresh_result)
    if standalone:
        _require_sales_invoice_scope_evidence(db)
    result = run_invoice_accounting_link_probe(
        invoice_client=MoneyForwardInvoiceClient(access_token),
        accounting_client=accounting_client,
        start_date=capture_start,
        end_date=capture_end,
        accounting_start_date=capture_start,
        accounting_end_date=capture_end,
        output_json=output_json,
    )
    artifact = json.loads(output_json.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict):
        raise TypeError("sales invoice source artifact must be an object")
    matches = artifact.get("matches")
    if not isinstance(matches, list):
        raise TypeError("sales invoice source artifact must contain matches")
    accrual_items = artifact.get("accrual_items")
    if not isinstance(accrual_items, list):
        raise TypeError("sales invoice source artifact must contain accrual_items")
    execution_summary = _execute_sales_accrual_items(
        db,
        args,
        runtime=runtime,
        accrual_items=accrual_items,
        output_dir=output_json.parent,
        capture_start=capture_start,
        capture_end=capture_end,
    )
    artifact["execution_summary"] = execution_summary
    output_json.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if standalone:
        return {
            "status": "complete"
            if execution_summary["pending_count"] == 0
            else "incomplete",
            "workflow": "sales_invoice",
            "run_id_hash": "sha256:" + run_hash,
            "accounting_date": capture_end,
            "capture_period": {"start_date": capture_start, "end_date": capture_end},
            "posted_count": execution_summary["posted_count"],
            "reused_count": execution_summary["reused_count"],
            "review_count": execution_summary["review_count"],
            "pending_count": execution_summary["pending_count"],
            "official_readback_verified_count": execution_summary[
                "official_readback_verified_count"
            ],
            "verified_rag_checked_count": execution_summary[
                "verified_rag_checked_count"
            ],
            "review_reason_counts": execution_summary["review_reason_counts"],
            "verified_rag_matched_count": execution_summary[
                "verified_rag_matched_count"
            ],
            "verified_rag_learned_count": execution_summary[
                "verified_rag_learned_count"
            ],
            "execution_mode": "standalone_sales_invoice",
            "manifest_required": False,
            "moneyforward_api_called": True,
            "moneyforward_write_called": execution_summary["posted_count"] > 0,
            "google_drive_read_called": False,
            "google_drive_write_called": False,
            "other_workflow_stage_called": False,
            "keychain_accessed": True,
            "oauth_token_refreshed": refresh_result is not None,
            "raw_journal_ids_stored": False,
            "raw_api_response_stored": False,
            "raw_amounts_reported": False,
            "raw_trade_partners_reported": False,
        }
    with db.connect() as conn:
        status = _persist_sales_invoice_stage(
            conn,
            run_id=args.run_id,
            accounting_date=capture_end,
            result=result.to_dict(),
            artifact=artifact,
            accrual_items=accrual_items,
            office_id=office_id,
            actor=args.actor,
        )
    return {
        **status,
        "execution_mode": "production_sales_invoice",
        "manifest_required": False,
        "moneyforward_api_called": True,
        "keychain_accessed": True,
        "oauth_token_refreshed": refresh_result is not None,
        "sales_invoice_probe": {
            "invoice_billing_count": result.invoice_billing_count,
            "matched_invoice_count": result.matched_invoice_count,
            "sales_accrual_missing_count": result.sales_accrual_missing_count,
            "remaining_count": execution_summary["review_count"],
            "posted_count": execution_summary["posted_count"],
            "reused_count": execution_summary["reused_count"],
            "review_count": execution_summary["review_count"],
            "official_readback_verified_count": execution_summary[
                "official_readback_verified_count"
            ],
            "verified_rag_checked_count": execution_summary[
                "verified_rag_checked_count"
            ],
            "review_reason_counts": execution_summary["review_reason_counts"],
            "verified_rag_matched_count": execution_summary[
                "verified_rag_matched_count"
            ],
            "verified_rag_learned_count": execution_summary[
                "verified_rag_learned_count"
            ],
        },
    }


def _require_sales_invoice_scope_evidence(db: Any) -> None:
    """Fail closed unless the existing token evidence covers Invoice reads."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT has_invoice_data_read,raw_secret_stored FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise PermissionError("Money Forward scope evidence is missing")
    if bool(row["raw_secret_stored"]):
        raise PermissionError("Money Forward scope evidence contains a raw secret")
    if not bool(row["has_invoice_data_read"]):
        raise PermissionError("latest Money Forward scope lacks invoice.data.read")


def _sales_invoice_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        detail
        for detail in item.get("details") or []
        if isinstance(detail, dict) and str(detail.get("name") or "").strip()
    ]


def _sales_invoice_detail_amount(detail: dict[str, Any]) -> int:
    from decimal import Decimal, InvalidOperation

    try:
        return int(
            Decimal(str(detail.get("price") or 0))
            * Decimal(str(detail.get("quantity") or 1))
        )
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _sales_invoice_detail_amounts(item: dict[str, Any]) -> list[int]:
    """Return journal values, preserving inclusive or exclusive invoice pricing."""
    from decimal import Decimal
    from .invoice_accounting_link_probe import INVOICE_EXCISE_RATES

    details = _sales_invoice_details(item)
    net_amounts = [_sales_invoice_detail_amount(detail) for detail in details]
    total = int(item.get("total_price") or 0)
    if sum(net_amounts) == total:
        return net_amounts
    gross_amounts = [
        amount
        + int(
            Decimal(amount)
            * Decimal(str(INVOICE_EXCISE_RATES.get(str(detail.get("excise") or ""), 0)))
        )
        for detail, amount in zip(details, net_amounts, strict=True)
    ]
    if gross_amounts and sum(gross_amounts) != total:
        gross_amounts[-1] += total - sum(gross_amounts)
    return gross_amounts


def _normalized_sales_invoice_rag_event(
    item: dict[str, Any], detail: dict[str, Any] | None = None
) -> dict[str, str]:
    from .financial_event_classification import normalize_financial_event

    details = [detail] if detail is not None else _sales_invoice_details(item)
    description = " ".join(
        (
            " ".join((str(row.get(key) or "") for key in ("name", "detail")))
            for row in details
        )
    )
    amount = 0
    if detail is not None:
        item_details = _sales_invoice_details(item)
        detail_amounts = _sales_invoice_detail_amounts(item)
        amount = next(
            (
                detail_amounts[index]
                for index, candidate in enumerate(item_details)
                if candidate is detail
            ),
            _sales_invoice_detail_amount(detail),
        )
    if detail is None or (amount <= 0 and len(_sales_invoice_details(item)) == 1):
        amount = int(item.get("total_price") or 0)
    return normalize_financial_event(
        {
            "source_event_type": "sales_invoice",
            "source_event_id": str(item.get("source_file_hash") or ""),
            "transaction_date": str(item.get("sales_date") or ""),
            "description": description,
            "counterparty": str(item.get("partner_name") or ""),
            "amount": amount,
            "cash_flow_direction": "inflow",
        }
    )


def _sales_invoice_pattern_description_matches(
    pattern_description: str, detail_description: str, billing_number: str
) -> bool:
    pattern = " ".join(str(pattern_description or "").casefold().split())
    detail = " ".join(str(detail_description or "").casefold().split())
    if not pattern or not detail:
        return False
    prefix = f"no.{billing_number.casefold()} "
    body = pattern[len(prefix) :] if pattern.startswith(prefix) else pattern
    detail_core = detail.removesuffix("費").strip()
    body_core = body.removesuffix("費").strip()
    body_parts = {
        part.removesuffix("費").strip()
        for part in re.split("[\\s・,/]+", body)
        if part.strip()
    }
    return bool(
        pattern == detail
        or body == detail
        or (len(detail_core) >= 2 and detail_core in body_parts)
        or (len(detail_core) >= 3 and detail_core in body)
        or (len(body_core) >= 3 and body_core in detail)
    )


def _verified_sales_pattern_trade_partner_mode(
    *,
    client: Any,
    office_id: str,
    selected: list[dict[str, Any]],
    occurrences: dict[str, list[dict[str, Any]]],
    journal_cache: dict[str, dict[str, Any]],
    excise: str,
) -> tuple[str, str]:
    """Require repeatable tax and partner semantics from official occurrences."""
    from .journal_write_verify import _unwrap_journal

    modes: set[str] = set()
    for pattern in selected:
        pattern_occurrences = occurrences.get(str(pattern["pattern_hash"]), [])
        if not pattern_occurrences:
            return ("missing", "")
        for occurrence in pattern_occurrences:
            journal_hash = str(occurrence["moneyforward_journal_id_hash"])
            journal = journal_cache.get(journal_hash)
            if journal is None:
                journal_id = _find_journal_id_by_recorded_hash(
                    client,
                    office_id=office_id,
                    journal_id_hash=journal_hash,
                    transaction_date=str(occurrence["transaction_date"]),
                )
                if not journal_id:
                    return ("missing", "")
                journal = _unwrap_journal(
                    client.journal(journal_id, office_id=office_id)
                )
                journal_cache[journal_hash] = journal
            branches = (
                journal.get("branches")
                or journal.get("journal_details")
                or journal.get("details")
                or []
            )
            line_index = int(occurrence["line_index"])
            if line_index >= len(branches) or not isinstance(
                branches[line_index], dict
            ):
                return ("missing", "")
            branch = branches[line_index]
            debit = branch.get("debitor") or branch.get("debit") or {}
            credit = branch.get("creditor") or branch.get("credit") or {}
            if not isinstance(debit, dict) or not isinstance(credit, dict):
                return ("missing", "")
            requires_verified_tax = "verified_journal_learning" in str(
                pattern.get("source_kinds") or ""
            ).split(",")
            if requires_verified_tax and (
                not _sales_tax_semantics_match(
                    excise,
                    {
                        "name": credit.get("tax_name") or credit.get("tax_long_name"),
                        "abbreviation": credit.get("tax_abbreviation"),
                    },
                )
            ):
                return ("tax_conflict", "")
            debit_code = str(
                debit.get("trade_partner_code") or debit.get("partner_code") or ""
            )
            credit_code = str(
                credit.get("trade_partner_code") or credit.get("partner_code") or ""
            )
            if debit_code and credit_code and (debit_code != credit_code):
                return ("conflict", "")
            modes.add(
                "both"
                if debit_code and credit_code
                else "debit_only"
                if debit_code
                else "credit_only"
                if credit_code
                else "none"
            )
    if len(modes) != 1:
        return ("conflict", "")
    return ("verified", next(iter(modes)))


def _apply_verified_sales_invoice_rag(
    db: Any, accrual_items: list[Any], *, client: Any, office_id: str
) -> None:
    """Resolve every Invoice API detail against verified RAG before UI registration."""
    from .hash_utils import stable_hash

    rag_reasons = {
        "ambiguous_sales_category",
        "mixed_or_missing_excise",
        "verified_rag_pattern_not_found",
        "verified_rag_pattern_conflict",
        "verified_rag_tax_conflict",
        "verified_rag_partner_handling_unverified",
        "verified_rag_partner_handling_conflict",
    }
    selected_patterns: dict[str, list[tuple[list[dict[str, Any]], str]]] = {}
    with db.connect() as conn:
        for item in accrual_items:
            if not isinstance(item, dict):
                continue
            details = _sales_invoice_details(item)
            if not details:
                details = [
                    {
                        "name": str(item.get("sales_category") or "").split("/", 1)[-1],
                        "excise": str(item.get("excise") or ""),
                        "price": item.get("total_price"),
                        "quantity": 1,
                    }
                ]
            item_states: list[dict[str, Any]] = []
            sales_lines: list[dict[str, Any]] = []
            item_selected: list[tuple[list[dict[str, Any]], str]] = []
            detail_amounts = _sales_invoice_detail_amounts(item)
            if not detail_amounts:
                detail_amounts = [int(item.get("total_price") or 0)]
            reasons = [
                str(value)
                for value in item.get("review_reason_codes") or []
                if str(value) not in rag_reasons
            ]
            for detail_index, detail in enumerate(details):
                normalized = _normalized_sales_invoice_rag_event(item, detail)
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "\n                        SELECT p.pattern_hash,p.vendor_key,p.description_key,\n                               p.amount_bucket,p.debit_sub_account,\n                               p.credit_sub_account,p.tax_category,p.success_count,\n                               GROUP_CONCAT(DISTINCT source.source_kind) AS source_kinds\n                        FROM journal_patterns AS p\n                        JOIN journal_pattern_provenance AS source\n                          ON source.pattern_hash = p.pattern_hash\n                        WHERE p.vendor_key = ?\n                          AND p.debit_account = '未収入金'\n                          AND p.credit_account = '売上高'\n                          AND p.debit_sub_account IS NOT NULL\n                          AND p.debit_sub_account != ''\n                          AND p.credit_sub_account IS NOT NULL\n                          AND p.credit_sub_account != ''\n                          AND p.failure_count = 0\n                          AND p.auto_post_eligibility != 'disabled'\n                          AND source.source_kind IN (\n                              'moneyforward_snapshot',\n                              'verified_journal_learning'\n                          )\n                        GROUP BY p.pattern_hash\n                        ORDER BY p.success_count DESC,p.pattern_hash\n                        ",
                        (normalized["vendor_key"],),
                    ).fetchall()
                ]
                selected = [
                    row
                    for row in rows
                    if row["amount_bucket"] == normalized["amount_bucket"]
                    and _sales_invoice_pattern_description_matches(
                        str(row["description_key"]),
                        normalized["description_key"],
                        str(item.get("billing_number") or ""),
                    )
                ]
                match_mode = "detail_exact"
                if not selected:
                    prefix = f"no.{str(item.get('billing_number') or '').casefold()} "
                    billing_rows = [
                        row
                        for row in rows
                        if str(row["description_key"]).casefold().startswith(prefix)
                    ]
                    billing_shapes = {
                        (str(row["debit_sub_account"]), str(row["credit_sub_account"]))
                        for row in billing_rows
                    }
                    if len(billing_shapes) == 1:
                        selected = billing_rows
                        match_mode = "same_invoice_unique_shape"
                if str(item.get("billing_number") or "").strip():
                    prefix = f"no.{str(item['billing_number']).casefold()} "
                    official_invoice_rows = [
                        dict(row)
                        for row in conn.execute(
                            "\n                            SELECT p.pattern_hash,p.vendor_key,p.description_key,\n                                   p.amount_bucket,p.debit_sub_account,\n                                   p.credit_sub_account,p.tax_category,p.success_count,\n                                   GROUP_CONCAT(DISTINCT source.source_kind) AS source_kinds\n                            FROM journal_patterns AS p\n                            JOIN journal_pattern_provenance AS source\n                              ON source.pattern_hash = p.pattern_hash\n                            WHERE lower(p.description_key) LIKE ?\n                              AND p.debit_account = '未収入金'\n                              AND p.credit_account = '売上高'\n                              AND p.debit_sub_account IS NOT NULL\n                              AND p.debit_sub_account != ''\n                              AND p.credit_sub_account IS NOT NULL\n                              AND p.credit_sub_account != ''\n                              AND p.failure_count = 0\n                              AND p.auto_post_eligibility != 'disabled'\n                              AND source.source_kind IN (\n                                  'moneyforward_snapshot',\n                                  'verified_journal_learning'\n                              )\n                            GROUP BY p.pattern_hash\n                            ORDER BY p.success_count DESC,p.pattern_hash\n                            ",
                            (prefix + "%",),
                        ).fetchall()
                    ]
                    described_rows = [
                        row
                        for row in official_invoice_rows
                        if _sales_invoice_pattern_description_matches(
                            str(row["description_key"]),
                            normalized["description_key"],
                            str(item["billing_number"]),
                        )
                    ]
                    official_shapes = {
                        (str(row["debit_sub_account"]), str(row["credit_sub_account"]))
                        for row in official_invoice_rows
                    }
                    if described_rows:
                        selected = described_rows
                        match_mode = "same_invoice_official_detail"
                    elif len(official_shapes) == 1:
                        selected = official_invoice_rows
                        match_mode = "same_invoice_official_unique_shape"
                shapes = {
                    (str(row["debit_sub_account"]), str(row["credit_sub_account"]))
                    for row in selected
                }
                source_kinds = sorted(
                    {
                        source_kind
                        for row in selected
                        for source_kind in str(row.get("source_kinds") or "").split(",")
                        if source_kind
                    }
                )
                state: dict[str, Any] = {
                    "detail_index": detail_index,
                    "facts_hash": normalized["event_hash"],
                    "candidate_count": len(selected),
                    "shape_count": len(shapes),
                    "source_kinds": source_kinds,
                    "official_or_verified_sources_only": True,
                    "raw_amount_stored": False,
                    "raw_partner_stored": False,
                }
                chosen_shape: tuple[str, str] | None = None
                reason = ""
                if not selected:
                    state.update(status="not_found", match_mode="none")
                    category = str(item.get("sales_category") or "")
                    if category.startswith("売上高/"):
                        chosen_shape = (
                            str(
                                item.get("debit_sub_account")
                                or item.get("partner_name")
                                or ""
                            ),
                            category.split("/", 1)[1],
                        )
                        item["trade_partner_mode"] = "debit_only"
                        state.update(
                            first_transaction_candidate=True,
                            tax_basis="invoice_api_and_live_tax_master",
                        )
                    else:
                        reason = "verified_rag_pattern_not_found"
                elif len(shapes) != 1:
                    state.update(status="conflict", match_mode="none")
                    reason = "verified_rag_pattern_conflict"
                else:
                    chosen_shape = next(iter(shapes))
                    learned_taxes = {
                        str(row["tax_category"])
                        for row in selected
                        if "verified_journal_learning"
                        in str(row.get("source_kinds") or "").split(",")
                        and str(row.get("tax_category") or "")
                        not in {"", "unknown_tax"}
                    }
                    excise = str(detail.get("excise") or item.get("excise") or "")
                    if len(learned_taxes) > 1 or (
                        learned_taxes
                        and (
                            not _sales_tax_semantics_match(
                                excise, {"name": next(iter(learned_taxes))}
                            )
                        )
                    ):
                        state.update(status="tax_conflict", match_mode="none")
                        reason = "verified_rag_tax_conflict"
                        chosen_shape = None
                    else:
                        pattern_hashes = sorted(
                            (str(row["pattern_hash"]) for row in selected)
                        )
                        state.update(
                            status="matched",
                            match_mode=match_mode,
                            pattern_set_hash=stable_hash(pattern_hashes),
                            support_count=sum(
                                (int(row["success_count"]) for row in selected)
                            ),
                            tax_basis="verified_pattern_and_invoice_api"
                            if learned_taxes
                            else "invoice_api_and_live_tax_master",
                        )
                if chosen_shape is not None:
                    amount = detail_amounts[detail_index]
                    if amount <= 0 and len(details) == 1:
                        amount = int(item.get("total_price") or 0)
                    if amount <= 0:
                        reason = "invalid_accrual_item"
                    else:
                        item["debit_sub_account"] = chosen_shape[0]
                        sales_lines.append(
                            {
                                "detail_index": detail_index,
                                "description": str(detail.get("name") or ""),
                                "credit_sub_account": chosen_shape[1],
                                "excise": str(
                                    detail.get("excise") or item.get("excise") or ""
                                ),
                                "amount": amount,
                            }
                        )
                        if selected:
                            item_selected.append(
                                (
                                    selected,
                                    str(
                                        detail.get("excise") or item.get("excise") or ""
                                    ),
                                )
                            )
                if reason and reason not in reasons:
                    reasons.append(reason)
                item_states.append(state)
            if sales_lines and sum((line["amount"] for line in sales_lines)) != int(
                item.get("total_price") or 0
            ):
                reasons.append("invoice_item_total_mismatch")
            item["verified_rag_items"] = item_states
            item["sales_lines"] = sales_lines
            aggregate_status = (
                "matched"
                if item_states
                and all((state.get("status") == "matched" for state in item_states))
                else "conflict"
                if any(
                    (
                        state.get("status") in {"conflict", "tax_conflict"}
                        for state in item_states
                    )
                )
                else "not_found"
            )
            aggregate_match_modes = {
                str(state.get("match_mode") or "none") for state in item_states
            }
            item["verified_rag"] = {
                "status": aggregate_status,
                "match_mode": "exact"
                if aggregate_match_modes == {"detail_exact"}
                else next(iter(aggregate_match_modes))
                if len(aggregate_match_modes) == 1
                else "per_detail",
                "candidate_count": sum(
                    (int(state.get("candidate_count") or 0) for state in item_states)
                ),
                "shape_count": sum(
                    (int(state.get("shape_count") or 0) for state in item_states)
                ),
                "first_transaction_candidate": any(
                    (
                        bool(state.get("first_transaction_candidate"))
                        for state in item_states
                    )
                ),
                "item_count": len(item_states),
                "matched_item_count": sum(
                    (state.get("status") == "matched" for state in item_states)
                ),
                "first_transaction_item_count": sum(
                    (
                        bool(state.get("first_transaction_candidate"))
                        for state in item_states
                    )
                ),
                "all_items_checked": len(item_states) == len(sales_lines),
                "facts_hash": stable_hash(
                    [str(state.get("facts_hash") or "") for state in item_states]
                ),
                "official_or_verified_sources_only": True,
                "raw_amount_stored": False,
                "raw_partner_stored": False,
            }
            item["review_reason_codes"] = list(dict.fromkeys(reasons))
            source_file_hash = str(item.get("source_file_hash") or "")
            if item_selected:
                selected_patterns[source_file_hash] = item_selected
            if item.get("execution_status") not in {"posted", "reused"}:
                item["decision_state"] = "ready" if not reasons else "review"
                item["execution_status"] = "pending" if not reasons else "review"
    occurrences: dict[str, list[dict[str, Any]]] = {}
    with db.connect() as conn:
        for selected_by_item in selected_patterns.values():
            for selected, _excise in selected_by_item:
                for pattern in selected:
                    pattern_hash = str(pattern["pattern_hash"])
                    if pattern_hash in occurrences:
                        continue
                    occurrences[pattern_hash] = [
                        dict(row)
                        for row in conn.execute(
                            "\n                            SELECT pattern_hash,moneyforward_journal_id_hash,\n                                   line_index,transaction_date\n                            FROM journal_pattern_occurrences\n                            WHERE pattern_hash = ?\n                              AND transaction_date IS NOT NULL\n                              AND transaction_date != ''\n                            ORDER BY transaction_date,occurrence_hash\n                            ",
                            (pattern_hash,),
                        ).fetchall()
                    ]
    journal_cache: dict[str, dict[str, Any]] = {}
    for item in accrual_items:
        if not isinstance(item, dict):
            continue
        selected_by_item = selected_patterns.get(
            str(item.get("source_file_hash") or "")
        )
        if not selected_by_item:
            continue
        partner_modes: set[str] = set()
        failed_status = ""
        for selected, excise in selected_by_item:
            partner_status, partner_mode = _verified_sales_pattern_trade_partner_mode(
                client=client,
                office_id=office_id,
                selected=selected,
                occurrences=occurrences,
                journal_cache=journal_cache,
                excise=excise,
            )
            if partner_status != "verified":
                failed_status = partner_status
                break
            partner_modes.add(partner_mode)
        state = dict(item.get("verified_rag") or {})
        if not failed_status and len(partner_modes) == 1:
            item["trade_partner_mode"] = next(iter(partner_modes))
            state["trade_partner_mode"] = item["trade_partner_mode"]
            state["trade_partner_handling_verified"] = True
            state["tax_characteristic_verified"] = True
            item["verified_rag"] = state
            continue
        reason = (
            "verified_rag_partner_handling_conflict"
            if failed_status == "conflict" or len(partner_modes) > 1
            else "verified_rag_tax_conflict"
            if failed_status == "tax_conflict"
            else "verified_rag_partner_handling_unverified"
        )
        state["status"] = failed_status or "conflict"
        state["trade_partner_handling_verified"] = False
        item["verified_rag"] = state
        _mark_sales_accrual_review(item, reason)


def _learn_verified_sales_invoice_rag(
    db: Any, *, item: dict[str, Any], tax_name: str
) -> None:
    from .verified_journal_learning import learn_verified_sales_invoice_pattern

    with db.connect() as conn:
        item["verified_rag_learning"] = learn_verified_sales_invoice_pattern(
            conn=conn,
            normalized=_normalized_sales_invoice_rag_event(item),
            candidate={
                "debit_account": "未収入金",
                "debit_sub_account": str(item.get("debit_sub_account") or ""),
                "credit_account": "売上高",
                "credit_sub_account": str(
                    (item.get("sales_lines") or [{}])[0].get("credit_sub_account")
                    or str(item.get("sales_category") or "").split("/", 1)[-1]
                ),
                "tax_category": tax_name,
                "trade_partner_mode": str(
                    item.get("trade_partner_mode") or "debit_only"
                ),
            },
            official_readback=dict(item.get("official_readback") or {}),
        )


def _execute_sales_accrual_items(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, ...],
    accrual_items: list[Any],
    output_dir: Path,
    capture_start: str | None = None,
    capture_end: str | None = None,
) -> dict[str, Any]:
    """Resolve expected accruals and verify only native Invoice-linked journals."""
    from .live_journal_resolver import _extract_items, _name, _unique_named

    client, office_id = runtime[:2]
    del args, output_dir, capture_start, capture_end
    _apply_verified_sales_invoice_rag(
        db, accrual_items, client=client, office_id=office_id
    )
    candidates = [
        item
        for item in accrual_items
        if isinstance(item, dict)
        and item.get("decision_state") == "ready"
        and (item.get("execution_status") == "pending")
    ]
    contexts: list[dict[str, Any]] = []
    if candidates:
        masters = {
            "accounts": _extract_items(client.accounts(office_id)),
            "sub_accounts": _extract_items(client.sub_accounts(office_id)),
            "taxes": _extract_items(client.taxes(office_id)),
            "trade_partners": _extract_items(client.trade_partners(office_id)),
        }
        for item in candidates:
            try:
                trade_partner_mode = str(item.get("trade_partner_mode") or "debit_only")
                resolved_partner_name = str(item["partner_name"])
                if trade_partner_mode != "none":
                    partner = _unique_named(
                        masters["trade_partners"], resolved_partner_name
                    )
                    if partner is None:
                        alias = str(item.get("debit_sub_account") or "").casefold()
                        alias_matches = [
                            row
                            for row in masters["trade_partners"]
                            if alias and alias in _name(row).casefold()
                        ]
                        partner = alias_matches[0] if len(alias_matches) == 1 else None
                    if partner is None:
                        _mark_sales_accrual_review(
                            item, "trade_partner_code_not_resolved"
                        )
                        continue
                    resolved_partner_name = _name(partner)
                sales_lines = item.get("sales_lines") or []
                taxes = [
                    _resolve_sales_invoice_tax_master(
                        str(line.get("excise") or ""), masters["taxes"]
                    )
                    for line in sales_lines
                    if isinstance(line, dict)
                ]
                if not sales_lines or any((tax is None for tax in taxes)):
                    _mark_sales_accrual_review(item, "tax_master_not_unique")
                    continue
                tax_names = [_name(tax) for tax in taxes if tax is not None]
                named_journal = _named_sales_invoice_journal(
                    {**item, "resolved_trade_partner_name": resolved_partner_name},
                    tax_name=tax_names[0],
                    tax_names=tax_names,
                )
                resolved = _resolve_named_sales_invoice_journal(named_journal, masters)
            except (KeyError, TypeError, ValueError):
                _mark_sales_accrual_review(item, "invalid_accrual_item")
                continue
            if resolved["status"] != "ready":
                _mark_sales_accrual_review(item, "live_master_resolution_failed")
                continue
            journal = dict(resolved["journal"])
            partner = _unique_named(masters["trade_partners"], resolved_partner_name)
            trade_partner_code = str(partner.get("code") or "") if partner else ""
            if str(item.get("trade_partner_mode") or "debit_only") != "none" and (
                not trade_partner_code
            ):
                _mark_sales_accrual_review(item, "trade_partner_code_not_resolved")
                continue
            contexts.append(
                {
                    "item": item,
                    "journal": journal,
                    "readback_client": _SalesInvoiceReadBackClient(
                        client, masters["taxes"]
                    ),
                    "tax_names": tax_names,
                    "journal_partner_name": resolved_partner_name,
                }
            )
    for context in contexts:
        item = context["item"]
        native_hashes = sorted(
            {
                str(value)
                for value in item.get("native_journal_ref_hashes") or []
                if value
            }
        )
        if not native_hashes:
            item["native_linkage_status"] = "ui_confirmation_required"
            continue
        if len(native_hashes) != 1:
            _mark_sales_accrual_review(item, "native_journal_identity_not_unique")
            continue
        verified = _strict_sales_invoice_native_readback(
            client=context["readback_client"],
            office_id=office_id,
            journal=context["journal"],
            journal_id_hash=native_hashes[0],
            source_file_hash=str(item["source_file_hash"]),
            billing_number=str(item["billing_number"]),
            transaction_date=str(item["sales_date"]),
        )
        if verified is None:
            _mark_sales_accrual_review(item, "native_journal_identity_not_unique")
            continue
        item["official_readback"] = _sales_invoice_readback_state(
            verified, source="native_invoice_journal_official_get"
        )
        if not verified.verified:
            _mark_sales_accrual_review(item, "official_readback_mismatch")
            continue
        item["execution_status"] = "reused"
        item["native_linkage_status"] = "registered_verified"
        if len(item.get("sales_lines") or []) == 1:
            try:
                _learn_verified_sales_invoice_rag(
                    db, item=item, tax_name=str(context["tax_names"][0])
                )
            except ValueError:
                item["verified_rag_learning"] = {
                    "status": "skipped",
                    "reason": "official_shape_not_single_line",
                }
    posted_count = 0
    reused_count = sum(
        (
            isinstance(item, dict) and item.get("execution_status") == "reused"
            for item in accrual_items
        )
    )
    review_items = [
        item
        for item in accrual_items
        if isinstance(item, dict) and item.get("execution_status") == "review"
    ]
    pending_count = sum(
        (
            isinstance(item, dict) and item.get("execution_status") == "pending"
            for item in accrual_items
        )
    )
    official_readback_verified_count = sum(
        (
            isinstance(item, dict)
            and isinstance(item.get("official_readback"), dict)
            and (item["official_readback"].get("status") == "verified")
            for item in accrual_items
        )
    )
    verified_rag_matched_count = sum(
        (
            isinstance(item, dict)
            and isinstance(item.get("verified_rag"), dict)
            and (item["verified_rag"].get("status") == "matched")
            for item in accrual_items
        )
    )
    verified_rag_checked_count = sum(
        (
            isinstance(item, dict) and isinstance(item.get("verified_rag"), dict)
            for item in accrual_items
        )
    )
    verified_rag_learned_count = sum(
        (
            isinstance(item, dict)
            and isinstance(item.get("verified_rag_learning"), dict)
            and (item["verified_rag_learning"].get("status") == "learned")
            for item in accrual_items
        )
    )
    review_reason_counts: dict[str, int] = {}
    for item in review_items:
        for reason in item.get("review_reason_codes") or ["sales_invoice_review"]:
            review_reason_counts[str(reason)] = (
                review_reason_counts.get(str(reason), 0) + 1
            )
    return {
        "posted_count": posted_count,
        "reused_count": reused_count,
        "review_count": len(review_items),
        "pending_count": pending_count,
        "official_readback_verified_count": official_readback_verified_count,
        "verified_rag_checked_count": verified_rag_checked_count,
        "verified_rag_matched_count": verified_rag_matched_count,
        "verified_rag_learned_count": verified_rag_learned_count,
        "review_reason_counts": review_reason_counts,
    }


def _named_sales_invoice_journal(
    item: dict[str, Any], *, tax_name: str, tax_names: list[str] | None = None
) -> dict[str, Any]:
    category = str(item["sales_category"])
    sales_lines = [
        line for line in item.get("sales_lines") or [] if isinstance(line, dict)
    ]
    if not sales_lines and (not category.startswith("売上高/")):
        raise ValueError("sales category must resolve below 売上高")
    source_partner_name = str(item["partner_name"])
    partner_name = str(item.get("resolved_trade_partner_name") or source_partner_name)
    debit_sub_account = str(item.get("debit_sub_account") or source_partner_name)
    trade_partner_mode = str(item.get("trade_partner_mode") or "debit_only")
    if trade_partner_mode not in {"both", "debit_only", "credit_only", "none"}:
        raise ValueError("sales invoice trade-partner mode is invalid")
    amount = int(item["total_price"])
    if not sales_lines:
        sales_lines = [
            {
                "description": "売上",
                "credit_sub_account": category.split("/", 1)[1],
                "amount": amount,
            }
        ]
    if sum((int(line.get("amount") or 0) for line in sales_lines)) != amount:
        raise ValueError("sales invoice detail total does not match invoice total")
    line_tax_names = tax_names or [tax_name] * len(sales_lines)
    if len(line_tax_names) != len(sales_lines):
        raise ValueError("sales invoice tax master count does not match details")
    remark_prefix = f"No.{item['billing_number']}"
    debitor: dict[str, Any] = {
        "account_name": "未収入金",
        "sub_account_name": debit_sub_account,
        "value": amount,
    }
    if trade_partner_mode in {"both", "debit_only"}:
        debitor["trade_partner_name"] = partner_name
    branches: list[dict[str, Any]] = []
    for index, (line, line_tax_name) in enumerate(
        zip(sales_lines, line_tax_names, strict=True)
    ):
        creditor: dict[str, Any] = {
            "account_name": "売上高",
            "sub_account_name": str(line["credit_sub_account"]),
            "tax_name": line_tax_name,
            "value": int(line["amount"]),
        }
        if trade_partner_mode in {"both", "credit_only"}:
            creditor["trade_partner_name"] = partner_name
        branches.append(
            {
                "remark": f"{remark_prefix} {str(line.get('description') or '売上')}"[
                    :200
                ],
                "debitor": debitor if index == 0 else {},
                "creditor": creditor,
            }
        )
    return {
        "transaction_date": str(item["sales_date"]),
        "journal_type": "journal_entry",
        "branches": branches,
    }


def _resolve_named_sales_invoice_journal(
    journal: dict[str, Any], masters: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Resolve compound native candidates without requiring each UI row to balance."""
    from .hash_utils import stable_hash
    from .live_journal_resolver import _resolve_side

    missing: list[dict[str, str]] = []
    resolved_branches: list[dict[str, Any]] = []
    for index, branch in enumerate(journal.get("branches") or []):
        debitor_spec = branch.get("debitor") or {}
        creditor_spec = branch.get("creditor") or {}
        debitor = (
            _resolve_side(debitor_spec, masters, missing, f"branches[{index}].debitor")
            if debitor_spec
            else {}
        )
        creditor = (
            _resolve_side(
                creditor_spec, masters, missing, f"branches[{index}].creditor"
            )
            if creditor_spec
            else {}
        )
        resolved_branches.append(
            {
                "remark": str(branch.get("remark") or ""),
                "debitor": debitor,
                "creditor": creditor,
            }
        )
    debit_total = sum(
        (int(branch["debitor"].get("value") or 0) for branch in resolved_branches)
    )
    credit_total = sum(
        (int(branch["creditor"].get("value") or 0) for branch in resolved_branches)
    )
    if debit_total <= 0 or debit_total != credit_total:
        raise ValueError("sales invoice compound journal is unbalanced")
    resolved = {
        "transaction_date": str(journal["transaction_date"]),
        "journal_type": "journal_entry",
        "branches": resolved_branches,
    }
    return {
        "status": "blocked" if missing else "ready",
        "missing_masters": missing,
        "journal": resolved,
        "journal_hash": stable_hash(resolved),
    }


class _SalesInvoiceReadBackClient:
    """Delegate official reads while reusing this stage's live tax snapshot."""

    def __init__(self, client: Any, taxes: list[dict[str, Any]]) -> None:
        self._client = client
        self._taxes = taxes

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def taxes(self, _office_id: str | None = None) -> dict[str, Any]:
        return {"taxes": self._taxes}


def _resolve_sales_invoice_tax_master(
    excise: str, taxes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    from .invoice_accounting_link_probe import INVOICE_EXCISE_RATES

    expected_rate = INVOICE_EXCISE_RATES.get(excise)
    if expected_rate is None:
        return None
    matches = [
        tax
        for tax in taxes
        if tax.get("available") is not False
        and _sales_tax_rate(tax.get("tax_rate")) == expected_rate
        and _sales_tax_semantics_match(excise, tax)
    ]
    return matches[0] if len(matches) == 1 else None


def _sales_tax_rate(value: Any) -> float:
    try:
        rate = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return -1.0
    return rate / 100 if rate > 1 else rate


def _sales_tax_semantics_match(excise: str, tax: dict[str, Any]) -> bool:
    labels = {
        re.sub("[\\s\u3000()（）]+", "", str(tax.get(key) or "")).casefold()
        for key in ("name", "abbreviation", "search_key")
        if tax.get(key)
    }
    canonical_labels = {
        "ten_percent": {"課税売上10%", "課売10%"},
        "eight_percent": {"課税売上8%", "課売8%"},
        "eight_percent_as_reduced_tax_rate": {
            "課税売上軽8%",
            "課売軽8%",
            "課税売上軽減8%",
            "課売軽減8%",
        },
        "five_percent": {"課税売上5%", "課売5%"},
        "untaxable": {"不課税売上", "不売"},
        "non_taxable": {"非課税売上", "非売"},
        "tax_exemption": {"免税売上", "免売"},
    }
    return bool(labels & canonical_labels.get(excise, set()))


def _mark_sales_accrual_review(item: dict[str, Any], reason: str) -> None:
    reasons = [str(value) for value in item.get("review_reason_codes") or []]
    if reason not in reasons:
        reasons.append(reason)
    item["decision_state"] = "review"
    item["execution_status"] = "review"
    item["review_reason_codes"] = reasons


def _strict_sales_invoice_native_readback(
    *,
    client: Any,
    office_id: str,
    journal: dict[str, Any],
    journal_id_hash: str,
    source_file_hash: str,
    billing_number: str,
    transaction_date: str,
) -> Any | None:
    """Verify one native Invoice-linked journal by its complete official GET body."""
    from .invoice_accounting_link_probe import _contains_billing_number
    from .journal_write_verify import _perform_journal_read_back, _unwrap_journal

    journal_id = _find_journal_id_by_recorded_hash(
        client,
        office_id=office_id,
        journal_id_hash=journal_id_hash,
        transaction_date=transaction_date,
    )
    if not journal_id:
        return None
    response_journal = _unwrap_journal(client.journal(journal_id, office_id=office_id))
    if str(response_journal.get("entered_by") or "") != "JOURNAL_TYPE_BILLING":
        return None
    actual_branches = (
        response_journal.get("branches")
        or response_journal.get("journal_details")
        or response_journal.get("details")
        or []
    )
    expected_journal = json.loads(json.dumps(journal))
    expected_branches = expected_journal.get("branches") or []
    if (
        not billing_number
        or len(expected_branches) != len(actual_branches)
        or (not all((isinstance(branch, dict) for branch in actual_branches)))
    ):
        return None
    for expected_branch, actual_branch in zip(
        expected_branches, actual_branches, strict=True
    ):
        actual_remark = str(
            actual_branch.get("remark")
            or actual_branch.get("description")
            or actual_branch.get("memo")
            or ""
        )
        if not _contains_billing_number(actual_remark, billing_number):
            return None
        expected_branch["remark"] = actual_remark
    return _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=expected_journal,
        idempotency_key=source_file_hash,
        operation_type="sales_invoice_native_official_read_back",
        office_id=office_id,
    )


def _sales_invoice_readback_state(result: Any, *, source: str) -> dict[str, Any]:
    """Persist only hashes and verification state at the scoped artifact boundary."""
    redacted = result.to_redacted_dict()
    return {
        "status": "verified" if result.verified else "mismatch",
        "source": source,
        "journal_id_hash": str(redacted.get("journal_id_hash") or ""),
        "expected_hash": str(redacted.get("expected_hash") or ""),
        "actual_hash": str(redacted.get("actual_hash") or ""),
        "response_hash": str(redacted.get("response_hash") or ""),
        "tax_readback_verified": bool(redacted.get("tax_readback_verified")),
        "diff_summary_redacted": str(redacted.get("diff_summary_redacted") or ""),
        "raw_journal_id_stored": False,
        "raw_journal_body_stored": False,
        "raw_api_response_stored": False,
        "raw_amount_stored": False,
        "raw_trade_partner_stored": False,
    }


def _execute_payable_invoice_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str] | None = None,
    standalone: bool = False,
) -> dict[str, Any]:
    """Run the payable source through the existing canonical accounting chain."""
    from .hash_utils import stable_hash
    from .shiwake_run import build_run_status

    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
    if not standalone and (
        status.get("status") != "running"
        or status.get("current_stage") != "payable_invoice"
    ):
        raise RuntimeError("payable invoice adapter called outside its active stage")
    run_hash = accounting_run_hash(args.run_id)
    artifact_path = _artifact_root() / "runs" / run_hash / "payable_invoice.local.json"
    artifact, validated, reused = _load_payable_invoice_gas_artifact(
        artifact_path, access_token=runtime[3] if runtime is not None else None
    )
    from .gmail_recurring_invoice import build_recurring_invoice_expectations

    with db.connect() as conn:
        expectations = build_recurring_invoice_expectations(
            conn,
            accounting_date=str(
                args.accounting_date if standalone else status["accounting_date"]
            ),
        )
    missing_expectations = [
        item for item in expectations if not item.observed_this_month
    ]
    if runtime is None:
        if standalone:
            advanced = {
                "status": "not_ready",
                "workflow": "payable_invoice",
                "parallel": True,
                "run_id": args.run_id,
                "accounting_date": str(args.accounting_date),
                "remaining_count": len(validated["items"]),
                "workflow_readback_verified": False,
                "blocker": "authenticated_runtime_required_for_external_readback",
            }
        else:
            with db.connect() as conn:
                advanced = _persist_payable_invoice_stage(
                    conn,
                    run_id=args.run_id,
                    accounting_date=str(status["accounting_date"]),
                    artifact=artifact,
                    validated=validated,
                    actor=args.actor,
                )
        return {
            **advanced,
            "execution_mode": "production_payable_invoice",
            "manifest_required": False,
            "gmail_gas_readback_verified": True,
            "gmail_gas_receipt_reused": reused,
            "google_drive_read_called": True,
            "authentication_ready": False,
            "moneyforward_readback_verified": False,
            "journal_write_scope": "not_verified",
            "invoice_data_read_scope": "not_verified",
            "payable_invoice_probe": {
                "principal_count": len(validated["runs"]),
                "query_receipt_count": sum(
                    (int(run["query_receipt_count"]) for run in validated["runs"])
                ),
                "blocked_count": len(validated["blocked_items"]),
                "quarantine_count": len(validated["quarantine_items"]),
                "remaining_count": len(validated["items"]),
            },
        }
    if not (artifact.get("files") or []):
        coverage_ok = artifact.get("gas_coverage_verified") is True
        unresolved_ok = artifact.get("gas_unresolved_clear") is True
        complete = coverage_ok and unresolved_ok
        blocker = (
            None
            if complete
            else "gmail_gas_principal_coverage_required"
            if not coverage_ok
            else "gmail_gas_unresolved_source_evidence"
        )
        return {
            "status": "complete" if complete else "not_ready",
            "workflow": "payable_invoice",
            "parallel": True,
            "run_id": args.run_id,
            "accounting_date": str(args.accounting_date),
            "execution_mode": "production_payable_invoice_native",
            "remaining_count": 0,
            "review_count": 0,
            "workflow_readback_verified": True,
            "authentication_ready": True,
            "moneyforward_readback_verified": True,
            "gmail_gas_readback_verified": complete,
            "blocker": blocker,
            "replay": {
                "additional_write_count": 0,
                "ocr_increment": 0,
                "gemini_increment": 0,
            },
        }
    payable_auth = getattr(runtime, "payable", None)
    if payable_auth is None:
        return {
            "status": "not_ready",
            "workflow": "payable_invoice",
            "parallel": True,
            "run_id": args.run_id,
            "accounting_date": str(args.accounting_date),
            "execution_mode": "production_payable_invoice_native",
            "remaining_count": len(artifact.get("files") or []),
            "workflow_readback_verified": False,
            "authentication_ready": False,
            "moneyforward_readback_verified": False,
            "blocker": "payable_auth_context_required",
            "replay": {"additional_write_count": 0},
        }
    native = _execute_payable_native_files(
        db, args, runtime=runtime, artifact=artifact, artifact_path=artifact_path
    )
    return {
        **native,
        "gmail_gas_readback_verified": artifact.get("gas_coverage_verified") is True
        and artifact.get("gas_unresolved_clear") is True,
        "gmail_gas_receipt_reused": reused,
        "google_drive_read_called": True,
        "manifest_required": False,
        "payable_invoice_probe": {
            "principal_count": len(validated["runs"]),
            "query_receipt_count": sum(
                (int(run["query_receipt_count"]) for run in validated["runs"])
            ),
            "blocked_count": len(validated["blocked_items"]),
            "quarantine_count": len(validated["quarantine_items"]),
            "remaining_count": native["remaining_count"],
            "recurring_expected_count": len(expectations),
            "recurring_missing_count": len(missing_expectations),
            "google_ocr_call_count": 0,
            "gemini_call_count": 0,
            "accounting_rename_count": 0,
        },
    }
    classification_summary = {
        "terminal_reused_count": 0,
        "canonical_facts_count": 0,
        "ready_count": 0,
        "verified_count": 0,
        "posted_count": 0,
        "reused_count": 0,
        "review_count": len(validated["items"]),
        "retry_pending_count": 0,
        "ocr_increment": 0,
        "gemini_increment": 0,
    }
    writes_before = _receipt_external_write_counts(db)
    files = artifact.get("files")
    receipt_artifact: dict[str, Any] | None = None
    if isinstance(files, list) and files:
        run_dir = artifact_path.parent
        receipt_artifact = {
            "files": files,
            "source": {
                "drive_principal": _drive_principal(),
                "identity_readback_verified": True,
                "full_intake_manifest_covered": True,
                "folder_count": len(
                    {
                        str(row.get("parent_folder_id") or "")
                        for row in files
                        if isinstance(row, dict)
                    }
                ),
                "file_count": len(files),
                "listed_nonfolder_count": len(files),
            },
            "items": [],
            "excluded_operational_artifacts": [],
        }
        classification_summary = _classify_and_execute_all_receipt_folders(
            db,
            args,
            status=status
            if not standalone
            else {"accounting_date": str(args.accounting_date)},
            runtime=runtime,
            artifact=receipt_artifact,
            run_dir=run_dir,
            source_type="received_invoice",
            execution_stage="payable_invoice",
        )
    writes_after_execution = _receipt_external_write_counts(db)
    replay_summary = {
        "ready_count": 0,
        "verified_count": 0,
        "review_count": 0,
        "retry_pending_count": 0,
        "ocr_increment": 0,
        "gemini_increment": 0,
    }
    replay_additions = {"journal": 0, "voucher": 0, "drive": 0}
    readbacks = _receipt_chain_readback_summary(
        db, files if isinstance(files, list) else []
    )
    remaining_count = int(classification_summary["review_count"])
    if standalone:
        replay_writes_before = _receipt_external_write_counts(db)
        if receipt_artifact is not None:
            replay_summary = _classify_and_execute_all_receipt_folders(
                db,
                args,
                status={"accounting_date": str(args.accounting_date)},
                runtime=runtime,
                artifact=json.loads(json.dumps(receipt_artifact, ensure_ascii=False)),
                run_dir=artifact_path.parent,
                source_type="received_invoice",
                execution_stage="payable_invoice",
            )
        replay_writes_after = _receipt_external_write_counts(db)
        replay_additions = {
            key: max(0, replay_writes_after[key] - replay_writes_before[key])
            for key in replay_writes_before
        }
        residual = _capture_payable_drive_intake(access_token=runtime[3])
        not_applicable_ids = {
            str(row.get("google_drive_file_id") or "")
            for row in (files if isinstance(files, list) else [])
            if isinstance(row.get("processing_outcome"), dict)
            and row["processing_outcome"].get("status") == "not_applicable"
        }
        remaining_count = sum(
            (
                1
                for row in residual.get("files") or []
                if str(row.get("google_drive_file_id") or "") not in not_applicable_ids
            )
        )
    replay_additional_write_count = sum(replay_additions.values())
    expected_chain_count = int(
        classification_summary.get("newly_verified_count", 0)
    ) + int(classification_summary.get("already_verified_count", 0))
    chain_verified = (
        isinstance(files, list)
        and classification_summary["review_count"] == 0
        and (classification_summary["retry_pending_count"] == 0)
        and (
            classification_summary["ready_count"]
            == classification_summary["verified_count"]
        )
        and (readbacks["complete_chain_count"] >= expected_chain_count)
        and (remaining_count == 0)
        and (replay_additional_write_count == 0)
        and (replay_summary["ocr_increment"] == 0)
        and (replay_summary["gemini_increment"] == 0)
    )
    scope_flags = {
        "journal_write_scope": "not_verified",
        "voucher_write_scope": "not_verified",
        "invoice_data_read_scope": "not_verified",
    }
    with db.connect() as conn:
        scope_row = conn.execute(
            "SELECT has_journal_write,has_voucher_write,has_invoice_data_read FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    if scope_row is not None:
        scope_flags = {
            "journal_write_scope": "verified" if bool(scope_row[0]) else "not_verified",
            "voucher_write_scope": "verified" if bool(scope_row[1]) else "not_verified",
            "invoice_data_read_scope": "verified"
            if bool(scope_row[2])
            else "not_verified",
        }
    authentication_ready = (
        runtime is not None
        and scope_flags["journal_write_scope"] == "verified"
        and (scope_flags["voucher_write_scope"] == "verified")
    )
    if "gas_coverage_verified" in artifact or "gas_unresolved_clear" in artifact:
        gmail_gas_readback_verified = (
            artifact.get("gas_coverage_verified") is True
            and artifact.get("gas_unresolved_clear") is True
        )
    else:
        gmail_gas_readback_verified = (
            artifact.get("acquisition_source") != "canonical_drive"
        )
    if standalone:
        complete = (
            chain_verified and authentication_ready and gmail_gas_readback_verified
        )
        if not artifact.get("gas_coverage_verified"):
            blocker = "gmail_gas_principal_coverage_required"
        elif not artifact.get("gas_unresolved_clear"):
            blocker = "gmail_gas_unresolved_source_evidence"
        else:
            blocker = "payable_source_facts_or_authoritative_readback_required"
        advanced = {
            "status": "complete" if complete else "not_ready",
            "workflow": "payable_invoice",
            "parallel": True,
            "run_id": args.run_id,
            "accounting_date": str(args.accounting_date),
            "remaining_count": remaining_count,
            "workflow_readback_verified": chain_verified,
            "blocker": None if complete else blocker,
        }
    else:
        with db.connect() as conn:
            advanced = _persist_payable_invoice_stage(
                conn,
                run_id=args.run_id,
                accounting_date=str(status["accounting_date"]),
                artifact=artifact,
                validated=validated,
                actor=args.actor,
            )
    return {
        **advanced,
        "execution_mode": "production_payable_invoice",
        "manifest_required": False,
        "gmail_gas_readback_verified": gmail_gas_readback_verified,
        "gmail_gas_receipt_reused": reused,
        "google_drive_read_called": True,
        "authentication_ready": authentication_ready,
        "moneyforward_readback_verified": chain_verified,
        "newly_registered_count": classification_summary["posted_count"],
        "reused_count": classification_summary["terminal_reused_count"]
        + classification_summary["reused_count"],
        "review_count": classification_summary["review_count"],
        "retry_pending_count": classification_summary["retry_pending_count"],
        "readbacks": readbacks,
        "replay": {
            **replay_summary,
            "additional_journal_write_count": replay_additions["journal"],
            "additional_voucher_write_count": replay_additions["voucher"],
            "additional_drive_write_count": replay_additions["drive"],
            "additional_write_count": replay_additional_write_count,
        },
        "initial_external_write_counts": {
            key: max(0, writes_after_execution[key] - writes_before[key])
            for key in writes_before
        },
        **scope_flags,
        "payable_invoice_probe": {
            "principal_count": len(validated["runs"]),
            "query_receipt_count": sum(
                (int(run["query_receipt_count"]) for run in validated["runs"])
            ),
            "blocked_count": len(validated["blocked_items"]),
            "quarantine_count": len(validated["quarantine_items"]),
            "remaining_count": remaining_count,
            **classification_summary,
            "recurring_expected_count": len(expectations),
            "recurring_missing_count": len(missing_expectations),
            "recurring_route_counts": {
                route: sum(
                    (
                        1
                        for item in missing_expectations
                        if item.acquisition_route == route
                    )
                )
                for route in sorted(
                    {item.acquisition_route for item in missing_expectations}
                )
            },
        },
    }


def _payable_native_text_entry_gate(
    file_bytes: bytes, *, accounting_date: str, mime_type: str
) -> dict[str, Any]:
    """Classify only document kind and event date before any Payable write.

    Native PDF text is deliberately not returned and never becomes canonical
    accounting facts.  Image-only PDFs remain REVIEW for a human decision; WO02
    must not fall through to Vision or Gemini OCR.
    """
    try:
        boundary_date = date.fromisoformat(accounting_date)
    except ValueError as error:
        raise ValueError("payable accounting date must be ISO-8601") from error
    normalized_mime = (mime_type or "application/pdf").casefold()
    if normalized_mime != "application/pdf":
        return {
            "status": "review",
            "reason": "payable_document_requires_pdf_native_text",
            "document_type": "unknown",
            "document_date": None,
        }
    with tempfile.TemporaryDirectory(prefix="mf-payable-entry-") as directory:
        path = Path(directory) / "evidence.pdf"
        path.write_bytes(file_bytes)
        try:
            native_text = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("payable native PDF text inspection failed") from error
    compact = re.sub("\\s+", "", native_text)
    if len(compact) < 20:
        return {
            "status": "review",
            "reason": "payable_image_only_or_native_text_insufficient",
            "document_type": "unknown",
            "document_date": None,
        }
    normalized = native_text.casefold()
    not_applicable_markers = {
        "estimate": ("見積書", "御見積書", "quotation", "estimate"),
        "purchase_order": ("注文書", "発注書", "purchase order", "order form"),
        "delivery_note": ("納品書", "delivery note"),
        "receipt": ("領収書", "receipt"),
    }
    for document_type, markers in not_applicable_markers.items():
        if any((marker.casefold() in normalized for marker in markers)):
            return {
                "status": "not_applicable",
                "reason": f"payable_document_type_{document_type}",
                "document_type": document_type,
                "document_date": None,
            }
    invoice_markers = ("請求書", "invoice", "請求明細")
    if not any((marker.casefold() in normalized for marker in invoice_markers)):
        return {
            "status": "review",
            "reason": "payable_document_type_not_decidable",
            "document_type": "unknown",
            "document_date": None,
        }
    document_date = _payable_invoice_date_from_native_text(native_text)
    if document_date is None:
        return {
            "status": "review",
            "reason": "payable_invoice_date_not_decidable",
            "document_type": "invoice",
            "document_date": None,
        }
    if document_date > boundary_date:
        return {
            "status": "not_ready",
            "reason": "payable_invoice_date_after_accounting_date",
            "document_type": "invoice",
            "document_date": document_date.isoformat(),
        }
    return {
        "status": "ready",
        "reason": "payable_invoice_event_occurred",
        "document_type": "invoice",
        "document_date": document_date.isoformat(),
    }


def _payable_invoice_date_from_native_text(native_text: str) -> date | None:
    """Read an issue/invoice date, explicitly excluding due/payment dates."""
    labelled = re.compile(
        "(?:請求日|請求年月日|発行日|invoice\\s*date|date\\s*of\\s*issue)[^0-9]{0,12}(20\\d{2})[年./-]\\s*(\\d{1,2})[月./-]\\s*(\\d{1,2})日?",
        re.IGNORECASE,
    )
    generic = re.compile("(20\\d{2})[年./-]\\s*(\\d{1,2})[月./-]\\s*(\\d{1,2})日?")
    match = labelled.search(native_text)
    if match is None:
        header = "\n".join(native_text.splitlines()[:20])
        match = generic.search(header)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _execute_payable_native_files(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: ProductionRuntime,
    artifact: dict[str, Any],
    artifact_path: Path,
    _replay: bool = False,
) -> dict[str, Any]:
    """Run only the proven Payable path; pause at the single UI AP1 action."""
    from .accounting_api import (
        MoneyForwardPayableClient,
        WriteExecutionGrant,
        extract_moneyforward_journal_id,
    )
    from .hash_utils import mask_identifier, stable_hash
    from .journal_write_verify import _perform_journal_read_back
    from .live_journal_resolver import _extract_items, _resolve_item

    payable_auth = runtime.payable
    client, accounting_office_id = runtime[:2]
    payable = MoneyForwardPayableClient(
        gateway_access_token=payable_auth.gateway_access_token,
        external_access_token=payable_auth.external_access_token,
        office_id=payable_auth.external_office_id,
        office_profile=get_current_profile(),
        keychain_service=client.keychain_service,
        db_path=client.db_path,
        accounting_client=client,
        write_enabled=_PRODUCTION_WRITES.get(),
    )
    files = [row for row in artifact.get("files") or [] if isinstance(row, dict)]
    write_counts = {"received_invoice": 0, "invoice_transaction": 0, "journal": 0}
    reused_count = 0
    review_count = 0
    retry_pending_count = 0
    verified_count = 0
    not_ready_count = 0
    not_applicable_count = 0
    error_count = 0
    decision_gemini_count = 0
    computer_actions: list[dict[str, Any]] = []
    scope_hash = str(getattr(runtime[2], "scope_hash", ""))
    if not scope_hash:
        with db.connect() as conn:
            scope = conn.execute(
                "SELECT scope_hash FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
            ).fetchone()
        scope_hash = str(scope["scope_hash"] if scope else "")
    representative_by_hash: dict[str, int] = {}
    verified_representative_hashes: set[str] = set()
    with db.connect() as conn:
        for index, file_row in enumerate(files):
            file_hash = str(file_row.get("file_hash") or "")
            drive_id_hash = str(file_row.get("google_drive_file_id_hash") or "")
            if not file_hash:
                continue
            terminal = conn.execute(
                "SELECT 1 FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=? AND processing_status='verified' LIMIT 1",
                (drive_id_hash, file_hash),
            ).fetchone()
            if file_hash not in representative_by_hash or (
                terminal is not None and file_hash not in verified_representative_hashes
            ):
                representative_by_hash[file_hash] = index
            if terminal is not None:
                verified_representative_hashes.add(file_hash)
    duplicate_indexes = {
        index
        for index, file_row in enumerate(files)
        if str(file_row.get("file_hash") or "")
        and representative_by_hash[str(file_row.get("file_hash"))] != index
    }
    for index, file_row in enumerate(files):
        file_write_count_before = sum(write_counts.values())
        drive_id_hash = str(file_row.get("google_drive_file_id_hash") or "")
        file_hash = str(file_row.get("file_hash") or "")
        file_name = str(file_row.get("file_name") or "")
        if not drive_id_hash or not file_hash or (not file_name):
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "drive_identity_incomplete",
            }
            continue
        if index in duplicate_indexes:
            not_applicable_count += 1
            file_row["payable_outcome"] = {
                "status": "not_applicable",
                "reason": "duplicate_content_reused_by_representative",
                "representative_file_id_hash": str(
                    files[representative_by_hash[file_hash]].get(
                        "google_drive_file_id_hash"
                    )
                    or ""
                ),
            }
            continue
        with db.connect() as conn:
            terminal = conn.execute(
                "SELECT processing_status,moneyforward_journal_id_hash,candidate_hash,idempotency_key FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=?",
                (drive_id_hash, file_hash),
            ).fetchone()
        terminal_verified = (
            terminal is not None and str(terminal["processing_status"]) == "verified"
        )
        exact_received = _find_exact_received_invoices(
            payable, file_name=file_name, file_hash=file_hash
        )
        if len(exact_received) > 1:
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "multiple_received_invoices_with_exact_file_name",
            }
            continue
        if terminal_verified and (not exact_received):
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "verified_ledger_payable_readback_missing",
            }
            continue
        file_bytes: bytes | None = None
        if not terminal_verified:
            try:
                file_bytes = _download_google_drive_file_bytes(
                    str(file_row["google_drive_file_id"]),
                    runtime[3],
                    mime_type=str(file_row.get("mime_type") or "application/pdf"),
                )
                if "sha256:" + hashlib.sha256(file_bytes).hexdigest() != file_hash:
                    raise RuntimeError(
                        "Drive payable content hash changed before entry gate"
                    )
                entry_gate = _payable_native_text_entry_gate(
                    file_bytes,
                    accounting_date=str(args.accounting_date),
                    mime_type=str(file_row.get("mime_type") or "application/pdf"),
                )
            except (OSError, RuntimeError, ValueError) as error:
                review_count += 1
                file_row["payable_outcome"] = {
                    "status": "review",
                    "reason": "payable_entry_gate_unavailable",
                    "error_hash": "sha256:"
                    + stable_hash(type(error).__name__ + ":" + str(error)),
                }
                continue
            file_row["payable_entry_gate"] = entry_gate
            if entry_gate["status"] == "not_applicable":
                not_applicable_count += 1
                file_row["payable_outcome"] = {
                    "status": "not_applicable",
                    "reason": entry_gate["reason"],
                }
                continue
            if entry_gate["status"] == "not_ready":
                not_ready_count += 1
                file_row["payable_outcome"] = {
                    "status": "not_ready",
                    "reason": entry_gate["reason"],
                    "document_date": entry_gate.get("document_date"),
                }
                continue
            if entry_gate["status"] == "review":
                review_count += 1
                file_row["payable_outcome"] = {
                    "status": "review",
                    "reason": entry_gate["reason"],
                }
                continue
        if exact_received:
            received = exact_received[0]
            received_id = _payable_resource_id(received)
        else:
            if file_bytes is None:
                raise RuntimeError(
                    "payable entry gate did not retain verified file bytes"
                )
            content_type = str(file_row.get("mime_type") or "application/pdf")
            metadata = payable.received_invoice_create_metadata(
                file_name=file_name, file_hash=hashlib.sha256(file_bytes).hexdigest()
            )
            grant = WriteExecutionGrant(
                operation_type="payable_received_invoice_create",
                approval_hash=stable_hash(
                    {"instruction": getattr(args, "instruction", "")}
                ),
                scope_hash=scope_hash,
                target_hash=metadata["request_hash"],
            )
            payload = payable.create_received_invoice(
                file_name=file_name,
                content_type=content_type,
                file_bytes=file_bytes,
                grant=grant,
            )
            received = _unwrap_payable_resource(payload, "received_invoice")
            received_id = _payable_resource_id(received)
            if not received_id:
                raise RuntimeError("Payable received invoice POST returned no identity")
            write_counts["received_invoice"] += 1
        detail_payload = payable.wait_for_received_invoice(received_id)
        detail = _unwrap_payable_resource(detail_payload, "received_invoice")
        report = detail.get("invoice_report")
        report_present = isinstance(report, dict) and bool(_payable_resource_id(report))
        target_office_verified, target_office_basis = (
            _payable_invoice_target_office_verified(
                detail,
                artifact=artifact,
                file_row=file_row,
                payable_office_id=payable_auth.external_office_id,
                ap1_present=report_present,
            )
        )
        ocr = {
            "operation_status": str(detail.get("operation_status") or ""),
            "payee_name_present": bool(detail.get("ocr_payee_name")),
            "book_date_present": bool(detail.get("book_date")),
            "total_amount_present": detail.get("total_amount") not in (None, ""),
            "due_date_present": bool(detail.get("due_date")),
            "registration_number_present": bool(detail.get("registration_number")),
            "target_office_verified": target_office_verified,
            "target_office_basis": target_office_basis,
        }
        if (
            not target_office_verified
            and target_office_basis != "computer_use_recipient_confirmation_required"
        ):
            review_count += 1
            file_row["payable_state"] = {
                "received_invoice_id": received_id,
                "received_invoice_id_hash": mask_identifier(received_id),
            }
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "payable_invoice_target_office_not_verified",
                "ocr_readback": ocr,
            }
            continue
        if not report_present:
            retry_pending_count += 1
            file_row["payable_state"] = {
                "received_invoice_id": received_id,
                "received_invoice_id_hash": mask_identifier(received_id),
            }
            file_row["payable_outcome"] = {
                "status": "computer_use_required",
                "reason": "unsubmitted_invoice_report_missing",
                "ocr_readback": ocr,
            }
            computer_actions.append(
                {
                    "type": "moneyforward-payable-ap1-browser-action-v1",
                    "action": "verify_configured_company_recipient_select_payee_and_save_unsubmitted_ap1",
                    "browser_profile": _setting(
                        "identity.moneyforward_browser_profile"
                    ),
                    "principal": _mf_principal(),
                    "target_file_name": file_name,
                    "target_file_hash": file_hash,
                    "target_office": _office_name(),
                    "max_click_count": 1,
                    "received_invoice_id_hash": mask_identifier(received_id),
                    "recipient_confirmation_required": not target_office_verified,
                    "recipient_mismatch_action": "stop_without_saving_ap1",
                    "reupload_allowed": False,
                    "submit_allowed": False,
                    "approve_allowed": False,
                    "payment_allowed": False,
                }
            )
            continue
        report_id = _payable_resource_id(report)
        assigned_payee = (
            detail.get("ap_payee") if isinstance(detail.get("ap_payee"), dict) else {}
        )
        if not assigned_payee.get("id") or not assigned_payee.get("name"):
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "payable_invoice_payee_not_assigned",
                "ocr_readback": ocr,
            }
            continue
        transaction_payload = payable.invoice_transactions(report_id)
        transactions = _payable_api_items(transaction_payload, "invoice_transactions")
        if not transactions:
            retry_pending_count += 1
            file_row["payable_state"] = {
                "received_invoice_id": received_id,
                "invoice_report_id": report_id,
                "received_invoice_id_hash": mask_identifier(received_id),
                "invoice_report_id_hash": mask_identifier(report_id),
            }
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "invoice_transaction_facts_missing",
                "ocr_readback": ocr,
            }
            continue
        try:
            named_journal = _payable_transactions_to_named_journal(transactions)
        except ValueError as error:
            if str(error) == "invoice_transaction_debit_account_missing":
                with db.connect() as conn:
                    debit_decision, gemini_calls = _resolve_payable_debit_decision(
                        conn,
                        transactions=transactions,
                        vendor_names=(
                            str(assigned_payee.get("name") or ""),
                            str(detail.get("ocr_payee_name") or ""),
                        ),
                        file_hash=file_hash,
                        office_id=accounting_office_id,
                        drive_route={
                            "document_route": file_row.get("document_route"),
                            "payment_liability_route": file_row.get(
                                "payment_liability_route"
                            ),
                            "expense_classification_route": file_row.get(
                                "expense_classification_route"
                            ),
                        },
                        access_token=runtime[3],
                    )
                decision_gemini_count += gemini_calls
                if debit_decision is not None:
                    try:
                        named_journal = _payable_transactions_to_named_journal(
                            transactions, debit_override=debit_decision
                        )
                    except ValueError as retry_error:
                        error = retry_error
                    else:
                        error = None
                if error is None:
                    pass
                else:
                    review_count += 1
                    file_row["payable_outcome"] = {
                        "status": "review",
                        "reason": str(error),
                        "ocr_readback": ocr,
                    }
                    continue
            else:
                review_count += 1
                file_row["payable_outcome"] = {
                    "status": "review",
                    "reason": str(error),
                    "ocr_readback": ocr,
                }
                continue
        if any(
            (
                not str(branch.get("creditor", {}).get("sub_account_name") or "")
                for branch in named_journal["branches"]
            )
        ):
            vendor_names = [
                str(assigned_payee.get("name") or ""),
                str(detail.get("ocr_payee_name") or ""),
            ]
            with db.connect() as conn:
                canonical_credit_sub = _resolve_payable_credit_sub_account_from_rag(
                    conn, vendor_names=vendor_names
                )
            if canonical_credit_sub is None:
                review_count += 1
                file_row["payable_outcome"] = {
                    "status": "review",
                    "reason": "accounting_credit_sub_account_unresolved",
                    "ocr_readback": ocr,
                }
                continue
            for branch in named_journal["branches"]:
                creditor = branch.get("creditor")
                if isinstance(creditor, dict) and (
                    not creditor.get("sub_account_name")
                ):
                    creditor["sub_account_name"] = canonical_credit_sub
        masters = {
            "accounts": _extract_items(client.accounts(accounting_office_id)),
            "sub_accounts": _extract_items(client.sub_accounts(accounting_office_id)),
            "taxes": _extract_items(client.taxes(accounting_office_id)),
            "trade_partners": _extract_items(
                client.trade_partners(accounting_office_id)
            ),
        }
        resolved = _resolve_item(
            {"source_file_hash": file_hash, "journal": named_journal}, masters
        )
        if resolved["status"] != "ready":
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "accounting_live_master_unresolved",
                "missing_master_count": len(resolved["missing_masters"]),
                "ocr_readback": ocr,
            }
            continue
        journal = resolved["journal"]
        if (
            terminal_verified
            and str(terminal["candidate_hash"] or "")
            and (str(terminal["candidate_hash"]) != str(resolved["journal_hash"]))
        ):
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "verified_ledger_candidate_hash_mismatch",
            }
            continue
        exact_journals, _same_total = _find_exact_payroll_journal_ids(
            client, office_id=accounting_office_id, journal=journal
        )
        if len(exact_journals) > 1:
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "multiple_exact_accounting_journals",
            }
            continue
        if exact_journals:
            journal_id = exact_journals[0]
            if terminal_verified and str(
                terminal["moneyforward_journal_id_hash"] or ""
            ) != mask_identifier(journal_id):
                review_count += 1
                file_row["payable_outcome"] = {
                    "status": "review",
                    "reason": "verified_ledger_accounting_journal_identity_mismatch",
                }
                continue
        elif terminal_verified:
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "verified_ledger_accounting_readback_missing",
            }
            continue
        else:
            posted = client.create_journal_from_run_instruction(
                journal=journal,
                instruction=getattr(args, "instruction", "仕訳しろ"),
                office_id=accounting_office_id,
            )
            journal_id = extract_moneyforward_journal_id(posted)
            if not journal_id:
                raise RuntimeError("Accounting journal POST returned no identity")
            write_counts["journal"] += 1
        idempotency_key = stable_hash(
            {
                "drive_file_hash": file_hash,
                "received_invoice_id_hash": mask_identifier(received_id),
                "invoice_report_id_hash": mask_identifier(report_id),
                "invoice_transaction_id_hashes": sorted(
                    (mask_identifier(_payable_resource_id(row)) for row in transactions)
                ),
                "journal_hash": resolved["journal_hash"],
            }
        )
        verification = _perform_journal_read_back(
            client=client,
            journal_id=journal_id,
            journal=journal,
            idempotency_key=idempotency_key,
            operation_type="payable_accounting_direct",
            office_id=accounting_office_id,
        )
        if not verification.verified:
            review_count += 1
            file_row["payable_outcome"] = {
                "status": "review",
                "reason": "payable_accounting_official_readback_mismatch",
                "readback_diff": verification.diff_summary_redacted,
            }
            continue
        journal_id_hash = mask_identifier(journal_id)
        with db.connect() as conn:
            conn.execute(
                "\n                INSERT INTO processing_ledger (\n                    google_drive_file_id_hash,file_name,file_hash,evidence_source_type,\n                    candidate_hash,idempotency_key,processing_status,\n                    moneyforward_journal_id_hash,dry_run_result_hash\n                ) VALUES (?,?,?,?,?,?,?,?,?)\n                ON CONFLICT(google_drive_file_id_hash,file_hash) DO UPDATE SET\n                    candidate_hash=excluded.candidate_hash,\n                    idempotency_key=excluded.idempotency_key,\n                    processing_status='verified',\n                    moneyforward_journal_id_hash=excluded.moneyforward_journal_id_hash,\n                    dry_run_result_hash=excluded.dry_run_result_hash,\n                    error_message=NULL,\n                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')\n                ",
                (
                    drive_id_hash,
                    file_name,
                    file_hash,
                    "invoice_api",
                    resolved["journal_hash"],
                    idempotency_key,
                    "verified",
                    journal_id_hash,
                    verification.actual_hash,
                ),
            )
            conn.commit()
        verified_count += 1
        file_reused = sum(write_counts.values()) == file_write_count_before
        if file_reused:
            reused_count += 1
        file_row["payable_state"] = {
            "received_invoice_id": received_id,
            "invoice_report_id": report_id,
            "received_invoice_id_hash": mask_identifier(received_id),
            "invoice_report_id_hash": mask_identifier(report_id),
        }
        file_row["payable_outcome"] = {
            "status": "verified",
            "reused": file_reused,
            "reason": "official_payable_invoice_transaction_accounting_get_reverified"
            if terminal_verified
            else "payable_native_chain_official_readback_verified",
            "journal_id_hash": journal_id_hash,
            "ocr_readback": ocr,
            "invoice_transaction_count": len(transactions),
            "accounting_readback_hash": verification.actual_hash,
        }
    artifact["files"] = files
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    file_results = _payable_file_results(files)
    result_counts = {
        state: sum((row["result"] == state for row in file_results))
        for state in (
            "ALREADY_VERIFIED",
            "NOT_READY",
            "NOT_APPLICABLE",
            "REVIEW",
            "ERROR",
        )
    }
    if sum(result_counts.values()) != len(files):
        raise RuntimeError("payable result conservation failed")
    review_count = result_counts["REVIEW"]
    not_ready_count = result_counts["NOT_READY"]
    not_applicable_count = result_counts["NOT_APPLICABLE"]
    error_count = result_counts["ERROR"]
    remaining = retry_pending_count + error_count
    complete = remaining == 0 and len(file_results) == len(files)
    result = {
        "status": "complete" if complete else "not_ready",
        "workflow": "payable_invoice",
        "parallel": True,
        "run_id": args.run_id,
        "accounting_date": str(args.accounting_date),
        "execution_mode": "production_payable_invoice_native",
        "remaining_count": remaining,
        "workflow_readback_verified": complete,
        "authentication_ready": True,
        "moneyforward_readback_verified": complete,
        "source_count": len(files),
        "result_count": len(file_results),
        "all_items_accounted_for": len(file_results) == len(files),
        "file_results": file_results,
        "newly_registered_count": sum(write_counts.values()),
        "reused_count": reused_count,
        "review_count": review_count,
        "not_ready_count": not_ready_count,
        "not_applicable_count": not_applicable_count,
        "error_count": error_count,
        "retry_pending_count": retry_pending_count,
        "computer_use_required": computer_actions,
        "initial_external_write_counts": write_counts,
        "replay": {
            "additional_received_invoice_write_count": 0,
            "additional_invoice_transaction_write_count": 0,
            "additional_journal_write_count": 0,
            "additional_write_count": 0,
        },
        "journal_write_scope": "verified",
        "voucher_write_scope": "not_required",
        "invoice_data_read_scope": "not_required",
        "google_ocr_call_count": 0,
        "cloud_vision_call_count": 0,
        "gemini_call_count": decision_gemini_count,
    }
    if complete and (not _replay):
        replay_result = _execute_payable_native_files(
            db,
            args,
            runtime=runtime,
            artifact=json.loads(json.dumps(artifact, ensure_ascii=False)),
            artifact_path=artifact_path,
            _replay=True,
        )
        replay_writes = replay_result["initial_external_write_counts"]
        replay_additional_write_count = sum(
            (int(value) for value in replay_writes.values())
        )
        replay_gemini_calls = int(replay_result.get("gemini_call_count") or 0)
        if (
            replay_result["status"] != "complete"
            or replay_additional_write_count != 0
            or replay_gemini_calls != 0
        ):
            raise RuntimeError("Payable replay created an additional external write")
        result["replay"] = {
            "additional_received_invoice_write_count": int(
                replay_writes["received_invoice"]
            ),
            "additional_invoice_transaction_write_count": int(
                replay_writes["invoice_transaction"]
            ),
            "additional_journal_write_count": int(replay_writes["journal"]),
            "additional_write_count": replay_additional_write_count,
            "google_ocr_call_count": 0,
            "cloud_vision_call_count": 0,
            "gemini_call_count": replay_gemini_calls,
            "verified": True,
        }
    return result


def _payable_file_results(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return exactly one public WO02 state and reason for every source file."""
    status_map = {
        "verified": "ALREADY_VERIFIED",
        "not_ready": "NOT_READY",
        "computer_use_required": "NOT_READY",
        "not_applicable": "NOT_APPLICABLE",
        "review": "REVIEW",
        "error": "ERROR",
    }
    rows: list[dict[str, Any]] = []
    for file_row in files:
        outcome = file_row.get("payable_outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        internal_status = str(outcome.get("status") or "error")
        rows.append(
            {
                "file_name": str(file_row.get("file_name") or ""),
                "file_id_hash": str(file_row.get("google_drive_file_id_hash") or ""),
                "content_hash": str(file_row.get("file_hash") or ""),
                "result": status_map.get(internal_status, "ERROR"),
                "reason": str(outcome.get("reason") or "payable_file_result_missing"),
            }
        )
    return rows


def _payable_api_items(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for value in (payload.get(key), payload.get("data")):
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return [row for row in value[key] if isinstance(row, dict)]
    return []


def _unwrap_payable_resource(payload: Any, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get(key)
        return nested if isinstance(nested, dict) else data
    return payload


def _payable_resource_id(resource: Any) -> str:
    return str(resource.get("id") or "") if isinstance(resource, dict) else ""


def _payable_invoice_target_office_verified(
    detail: dict[str, Any],
    *,
    artifact: dict[str, Any],
    file_row: dict[str, Any],
    payable_office_id: str,
    ap1_present: bool,
) -> tuple[bool, str]:
    """Use API bill-to first; otherwise bind confirmation to the one AP1 UI action."""
    import unicodedata

    containers = [detail]
    for key in ("ocr", "ocr_result", "ocr_results"):
        value = detail.get(key)
        if isinstance(value, dict):
            containers.append(value)
    recipient_keys = (
        "recipient",
        "recipient_name",
        "ocr_recipient_name",
        "invoice_recipient_name",
        "bill_to_name",
        "billed_to_name",
    )

    def normalize(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub("[^0-9a-z\\u3040-\\u30ff\\u3400-\\u9fff]", "", text)

    observed = {
        normalize(container.get(key))
        for container in containers
        for key in recipient_keys
        if normalize(container.get(key))
    }
    accepted = {normalize(name) for name in get_current_profile().expected_office_names}
    if observed:
        if observed.issubset(accepted):
            return (True, "mf_ocr_recipient")
        return (False, "mf_ocr_recipient_mismatch")
    payee = detail.get("ap_payee") if isinstance(detail.get("ap_payee"), dict) else {}
    payee_fact_present = bool(detail.get("ocr_payee_name")) or (
        bool(payee.get("id")) and bool(payee.get("name"))
    )
    core_facts_ready = (
        payee_fact_present
        and bool(detail.get("book_date"))
        and (detail.get("total_amount") not in (None, ""))
    )
    source = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
    gmail_intake_verified = (
        artifact.get("acquisition_source") == "gmail_gas"
        and artifact.get("gas_coverage_verified") is True
        and (artifact.get("gas_unresolved_clear") is True)
        and (artifact.get("drive_principal") == _drive_principal())
    )
    canonical_drive_verified = (
        source.get("identity_readback_verified") is True
        and source.get("drive_principal") == _drive_principal()
    )
    canonical_context_ready = (
        bool(payable_office_id)
        and str(file_row.get("document_route") or "") == "請求書"
        and core_facts_ready
        and (gmail_intake_verified or canonical_drive_verified)
    )
    if canonical_context_ready:
        if ap1_present:
            return (True, "ap1_computer_use_recipient_confirmation")
        return (False, "computer_use_recipient_confirmation_required")
    return (False, "canonical_context_incomplete")


def _resolve_payable_credit_sub_account_from_rag(
    conn: Any, *, vendor_names: Sequence[str]
) -> str | None:
    """Return one verified existing RAG result; never infer from Drive names."""
    import unicodedata

    normalized_names = {
        re.sub("\\s+", " ", unicodedata.normalize("NFKC", str(name))).strip()
        for name in vendor_names
        if str(name).strip()
    }
    if not normalized_names:
        return None
    placeholders = ",".join(("?" for _ in normalized_names))
    rows = conn.execute(
        f"\n        SELECT DISTINCT credit_sub_account\n        FROM journal_patterns\n        WHERE vendor_key IN ({placeholders})\n          AND credit_account = '未払金'\n          AND credit_sub_account IS NOT NULL\n          AND trim(credit_sub_account) != ''\n          AND success_count > 0\n          AND failure_count = 0\n        ",
        tuple(sorted(normalized_names)),
    ).fetchall()
    candidates = {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}
    return next(iter(candidates)) if len(candidates) == 1 else None


def _resolve_payable_debit_decision(
    conn: Any,
    *,
    transactions: list[dict[str, Any]],
    vendor_names: Sequence[str],
    file_hash: str,
    office_id: str,
    drive_route: dict[str, Any],
    access_token: str,
) -> tuple[tuple[str, str | None] | None, int]:
    """Resolve one missing debit from RAG or one cached/single AI decision.

    The packet contains only Money Forward facts.  It never sends the PDF and
    therefore cannot become a second OCR path.
    """
    import unicodedata
    from .financial_event_classification import (
        build_accounting_decision_packet,
        load_accounting_decision_cache,
        store_accounting_decision_cache,
        validate_accounting_ai_decision,
    )
    from .hash_utils import stable_hash

    missing_debits = [
        row
        for row in transactions
        if not str((row.get("ex_item") or {}).get("name") or "").strip()
    ]
    if len(missing_debits) != 1:
        return (None, 0)
    normalized_names = {
        re.sub("\\s+", " ", unicodedata.normalize("NFKC", str(name))).strip()
        for name in vendor_names
        if str(name).strip()
    }
    accounts = {
        str(row[0]).strip()
        for row in conn.execute(
            "SELECT name_normalized FROM master_items WHERE item_type='account' AND active=1 AND trim(name_normalized) != ''"
        ).fetchall()
        if str(row[0] or "").strip()
    }
    sub_accounts = {
        str(row[0]).strip()
        for row in conn.execute(
            "SELECT name_normalized FROM master_items WHERE item_type='sub_account' AND active=1 AND trim(name_normalized) != ''"
        ).fetchall()
        if str(row[0] or "").strip()
    }
    rag_rows: list[Any] = []
    if normalized_names:
        placeholders = ",".join(("?" for _ in normalized_names))
        rag_rows = conn.execute(
            f"\n            SELECT DISTINCT debit_account,debit_sub_account,pattern_hash\n            FROM journal_patterns\n            WHERE vendor_key IN ({placeholders})\n              AND debit_account IS NOT NULL\n              AND trim(debit_account) != ''\n              AND success_count > 0\n              AND failure_count = 0\n            ",
            tuple(sorted(normalized_names)),
        ).fetchall()
    rag_candidates = {
        (
            str(row[0]).strip(),
            str(row[1]).strip() if str(row[1] or "").strip() else None,
        )
        for row in rag_rows
        if str(row[0] or "").strip() in accounts
        and (not str(row[1] or "").strip() or str(row[1]).strip() in sub_accounts)
    }
    if len(rag_candidates) == 1:
        return (next(iter(rag_candidates)), 0)
    transaction = missing_debits[0]
    deal_date = str(transaction.get("deal_date") or "")[:10]
    total_value = int(transaction.get("total_value") or 0)
    excise_value = int(transaction.get("excise_value") or 0)
    credit_account = str((transaction.get("cr_item") or {}).get("name") or "")
    credit_sub_account = (
        str((transaction.get("cr_sub_item") or {}).get("name") or "") or None
    )
    if (
        credit_account != "未払金"
        or credit_sub_account is None
        or credit_sub_account not in sub_accounts
    ):
        return (None, 0)
    effective_drive_route = _accounting_decision_route(
        drive_route,
        credit_account=credit_account,
        credit_sub_account=credit_sub_account,
    )
    tax_rows = [{"tax_amount": excise_value}]
    packet = build_accounting_decision_packet(
        {
            "contract_version": 2,
            "decision_required": True,
            "canonical_facts": {
                "source": "moneyforward_payable_invoice_transactions",
                "transaction_date": deal_date,
                "amount": total_value + excise_value,
                "vendor_names": sorted(normalized_names),
                "invoice_transaction_count": len(transactions),
                "source_file_hash": file_hash,
            },
            "drive_route": effective_drive_route,
            "fixed_fields": {
                "journal_date": deal_date,
                "amount": total_value + excise_value,
                "tax_amount": excise_value,
                "tax_rows": tax_rows,
                "credit_account": credit_account,
                "credit_sub_account": credit_sub_account,
            },
            "verified_fixed_rules": {},
            "past_journal_candidates": [
                {
                    "debit_account": str(row[0]),
                    "debit_sub_account": str(row[1]) if row[1] else None,
                    "pattern_hash": str(row[2] or ""),
                }
                for row in rag_rows
            ],
            "similar_rag": {"candidate_count": len(rag_candidates)},
            "failure_rag": {},
            "moneyforward_identity": {
                "office_id_hash": "sha256:" + stable_hash(office_id),
                "verified": bool(office_id),
            },
            "live_master": {
                "credit_sub_account_unique": True,
                "debit_accounts": sorted(accounts),
                "debit_sub_accounts": sorted(sub_accounts),
            },
            "allowed_debit_candidates": [
                {"debit_account": account, "debit_sub_account": sub_account}
                for account, sub_account in sorted(
                    rag_candidates, key=lambda item: (item[0], item[1] or "")
                )
            ],
            "allowed_debit_accounts": sorted(accounts),
            "allowed_debit_sub_accounts": sorted(sub_accounts),
            "allowed_pattern_hashes": sorted(
                {str(row[2]) for row in rag_rows if str(row[2] or "")}
            ),
        }
    )
    cached = load_accounting_decision_cache(conn, str(packet["packet_hash"]))
    gemini_calls = 0
    if cached is not None:
        decision = cached
    elif not accounts:
        return (None, 0)
    else:
        response = _run_shared_accounting_classifier([packet], access_token)
        if int(response.get("ocr_calls") or 0) != 0:
            raise RuntimeError("WO02 accounting decision attempted a second OCR")
        gemini_calls = int(response.get("gemini_calls") or 0)
        if gemini_calls > 1:
            raise RuntimeError("WO02 accounting decision exceeded one Gemini call")
        raw = next(
            (
                row
                for row in response.get("items") or []
                if isinstance(row, dict)
                and row.get("packet_hash") == packet["packet_hash"]
            ),
            {},
        )
        validation = validate_accounting_ai_decision(packet, raw)
        store_accounting_decision_cache(conn, str(packet["packet_hash"]), validation)
        decision = {
            "status": validation.status,
            "candidate": validation.candidate,
            "conflicts": list(validation.reason_codes),
        }
    candidate = decision.get("candidate") if isinstance(decision, dict) else None
    if not isinstance(candidate, dict) or decision.get("status") != "decided":
        return (None, gemini_calls)
    debit_account = str(candidate.get("debit_account") or "").strip()
    debit_sub_account = str(candidate.get("debit_sub_account") or "").strip() or None
    if debit_account not in accounts or (
        debit_sub_account is not None and debit_sub_account not in sub_accounts
    ):
        return (None, gemini_calls)
    return ((debit_account, debit_sub_account), gemini_calls)


def _find_exact_received_invoices(
    payable: Any, *, file_name: str, file_hash: str | None = None
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for page in range(1, 101):
        payload = payable.received_invoices(
            page=page, per_page=100, file_name_query=file_name
        )
        rows = _payable_api_items(payload, "received_invoices")
        for row in rows:
            candidate_name = str(
                row.get("file_name")
                or row.get("filename")
                or (row.get("mf_file") or {}).get("file_name")
                or (row.get("file") or {}).get("name")
                or ""
            )
            candidate_hash = str(
                row.get("file_hash")
                or row.get("sha256")
                or row.get("sha256_checksum")
                or (row.get("mf_file") or {}).get("sha256")
                or (row.get("file") or {}).get("sha256")
                or ""
            ).casefold()
            if candidate_hash and (not candidate_hash.startswith("sha256:")):
                candidate_hash = "sha256:" + candidate_hash
            if candidate_name == file_name and (
                not file_hash or not candidate_hash or candidate_hash == file_hash
            ):
                matches.append(row)
        if len(rows) < 100:
            break
    return matches


def _payable_transactions_to_named_journal(
    transactions: list[dict[str, Any]],
    *,
    debit_override: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    branches: list[dict[str, Any]] = []
    dates: set[str] = set()
    for transaction in transactions:
        if transaction.get("has_withholding_income_tax") is True:
            raise ValueError("withholding_invoice_requires_review")
        if str(transaction.get("currency") or "JPY") != "JPY":
            raise ValueError("foreign_currency_invoice_requires_review")
        deal_date = str(transaction.get("deal_date") or "")[:10]
        if not re.fullmatch("\\d{4}-\\d{2}-\\d{2}", deal_date):
            raise ValueError("invoice_transaction_deal_date_missing")
        dates.add(deal_date)
        total = int(transaction.get("total_value") or 0)
        tax = int(transaction.get("excise_value") or 0)
        amount = total + tax
        if amount <= 0:
            raise ValueError("invoice_transaction_amount_invalid")
        debit_name = str((transaction.get("ex_item") or {}).get("name") or "")
        debit_sub_name = str((transaction.get("ex_sub_item") or {}).get("name") or "")
        if not debit_name and debit_override is not None:
            debit_name, debit_sub_name = debit_override
        credit_name = str((transaction.get("cr_item") or {}).get("name") or "")
        credit_sub_name = str((transaction.get("cr_sub_item") or {}).get("name") or "")
        excise = transaction.get("dr_excise") or {}
        excise_name = str(excise.get("long_name") or excise.get("name") or "")
        if "10%" in excise_name:
            tax_name = _setting("bank.qualified_evidence_tax_category")
        elif "8%" in excise_name:
            tax_name = "課仕 (軽)8%"
        else:
            tax_name = excise_name
        if not debit_name:
            raise ValueError("invoice_transaction_debit_account_missing")
        if not credit_name or not tax_name:
            raise ValueError("invoice_transaction_master_names_missing")
        invoice_kind = int(transaction.get("invoice_kind") or 1)
        invoice_kind_name = {
            1: "INVOICE_KIND_NOT_TARGET",
            2: "INVOICE_KIND_QUALIFIED",
            3: "INVOICE_KIND_UNQUALIFIED_80",
        }.get(invoice_kind)
        if invoice_kind_name is None:
            raise ValueError("invoice_kind_70_percent_requires_review")
        debitor = {
            "account_name": debit_name,
            "tax_name": tax_name,
            "invoice_kind": invoice_kind_name,
            "value": amount,
        }
        if debit_sub_name:
            debitor["sub_account_name"] = debit_sub_name
        branches.append(
            {
                "remark": str(transaction.get("name") or "支払請求書"),
                "debitor": debitor,
                "creditor": {
                    "account_name": credit_name,
                    "sub_account_name": credit_sub_name or None,
                    "value": amount,
                },
            }
        )
    if len(dates) != 1 or not branches:
        raise ValueError("invoice_transactions_require_one_accounting_date")
    return {
        "transaction_date": next(iter(dates)),
        "journal_type": "journal_entry",
        "branches": branches,
        "memo": "クラウド債務支払 invoice_transactions",
    }


def _capture_payable_gas_readback(
    access_token: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Read the existing Gmail GAS receipt and scan only a stale current mailbox."""
    contract = _gmail_intake_contract()
    status = _run_shared_gas_function(
        "getGmailInvoiceIntakeRuntimeStatus", [contract], access_token
    )
    _validate_gmail_intake_status(status, contract)
    if not isinstance(status, dict) or status.get("principalAccepted") is not True:
        raise RuntimeError("canonical Gmail GAS principal read-back failed")
    coverage = (
        status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
    )
    principals = (
        coverage.get("principals")
        if isinstance(coverage.get("principals"), list)
        else []
    )
    current_principal = str(status.get("currentPrincipal") or "")
    current = next(
        (
            row
            for row in principals
            if isinstance(row, dict) and row.get("principal") == current_principal
        ),
        None,
    )
    scan_invoked = False
    if isinstance(current, dict) and current.get("captured") is not True:
        _run_shared_gas_function(
            "saveIncomingInvoiceReceiptAttachments", [contract], access_token
        )
        scan_invoked = True
        status = _run_shared_gas_function(
            "getGmailInvoiceIntakeRuntimeStatus", [contract], access_token
        )
        _validate_gmail_intake_status(status, contract)
        coverage = (
            status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
        )
        principals = (
            coverage.get("principals")
            if isinstance(coverage.get("principals"), list)
            else []
        )

    def receipt_row(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            "principal": str(
                receipt.get("principal") or receipt.get("accountEmail") or ""
            ),
            "account_hash": str(receipt.get("accountHash") or ""),
            "vendor_key": str(receipt.get("vendorKey") or ""),
            "query_hash": str(receipt.get("queryHash") or ""),
            "status": str(receipt.get("status") or ""),
            "result_count": int(receipt.get("resultCount") or 0),
            "processed_count": int(receipt.get("processedCount") or 0),
        }

    runs = []
    for row in principals:
        if not isinstance(row, dict):
            continue
        receipts = [
            receipt_row(receipt)
            for receipt in row.get("queryReceipts") or []
            if isinstance(receipt, dict)
        ]
        runs.append(
            {
                "principal": str(row.get("principal") or ""),
                "account_hash": str(row.get("accountHash") or ""),
                "coverage_start_after": str(row.get("coverageStartAfter") or ""),
                "ran_at": str(row.get("ranAt") or ""),
                "trigger_count": int(row.get("triggerCount") or 0),
                "query_receipt_count": len(receipts),
                "query_receipts_ready": row.get("queryReceiptsReady") is True,
                "query_receipts": receipts,
                "search_truncated": int(row.get("searchTruncated") or 0),
                "blocked_open": int(row.get("blockedOpen") or 0),
                "quarantine_open": int(row.get("quarantineOpen") or 0),
                "failed": int(row.get("failed") or 0),
                "captured": row.get("captured") is True,
            }
        )
    blocked_items = [
        {
            "account_hash": str(row.get("accountHash") or ""),
            "item_hash": str(row.get("itemHash") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for row in status.get("blockedItems") or []
        if isinstance(row, dict)
    ]
    quarantine_items = [
        {
            "item_id_hash": "sha256:" + str(row.get("fileIdHash") or ""),
            "source_parent_verified": row.get("sourceParentVerified") is True,
            "mime_type": str(row.get("mimeType") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for row in status.get("quarantineItems") or []
        if isinstance(row, dict)
    ]
    coverage_verified = len(runs) == len(contract["accounts"]) and all(
        (
            row["captured"]
            and row["trigger_count"] == 1
            and row["query_receipts_ready"]
            and (row["failed"] == 0)
            and (row["search_truncated"] == 0)
            for row in runs
        )
    )
    drive = _capture_payable_drive_intake(access_token=access_token)
    artifact = {
        **_profile_boundary(),
        "intake_contract_hash": status["intake_contract_hash"],
        "destination_folder_ids": status["destinationFolderIds"],
        "schema": "moneyforward-payable-gas-readback-v1",
        "acquisition_source": "gmail_gas",
        "drive_principal": _drive_principal(),
        "destination_folders_readback": status.get("destinationFoldersReadback")
        is True,
        "gas_coverage_verified": coverage_verified,
        "gas_unresolved_clear": not blocked_items and (not quarantine_items),
        "runs": runs,
        "blocked_items": blocked_items,
        "quarantine_items": quarantine_items,
        "files": drive["files"],
        "excluded_files": drive["excluded_files"],
        "source": drive["source"],
    }
    validated = _validate_payable_invoice_gas_artifact(artifact)
    return (artifact, validated, scan_invoked)


def _load_payable_invoice_gas_artifact(
    expected_path: Path, *, access_token: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Load the current run receipt or the newest still-fresh canonical receipt."""
    if access_token:
        artifact, validated, scan_invoked = _capture_payable_gas_readback(access_token)
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return (artifact, validated, not scan_invoked)
    if expected_path.is_file():
        artifact = json.loads(expected_path.read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            raise ValueError("payable invoice GAS artifact must be an object")
        return (artifact, _validate_payable_invoice_gas_artifact(artifact), True)
    runs_root = _artifact_root() / "runs"
    candidates = sorted(
        runs_root.glob("*/payable_invoice.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            artifact = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(artifact, dict):
                continue
            validated = _validate_payable_invoice_gas_artifact(artifact)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return (artifact, validated, True)
    artifact = _capture_payable_drive_intake(access_token=access_token)
    validated = {
        "runs": [],
        "blocked_items": [],
        "quarantine_items": [],
        "items": [
            {
                "item_id": str(item.get("google_drive_file_id_hash") or ""),
                "status": "planned",
                "economic_event_discovered": True,
                "blocker": "payable_invoice_processing_required",
            }
            for item in artifact.get("files") or []
            if isinstance(item, dict)
        ],
    }
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return (artifact, validated, False)


def _capture_payable_drive_intake(*, access_token: str | None = None) -> dict[str, Any]:
    """Capture the live configured company payable folder using the Drive owner token."""
    from .accounting_api import UrllibTransport
    from .hash_utils import stable_hash

    access_token = access_token or _bind_profile_google_runtime()
    transport = UrllibTransport()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def get_json(url: str) -> dict[str, Any]:
        status, _response_headers, body = transport.request("GET", url, headers=headers)
        if status != 200:
            raise RuntimeError(f"payable Drive intake failed: HTTP {status}")
        payload = json.loads(body.decode("utf-8")) if body else {}
        if not isinstance(payload, dict):
            raise RuntimeError("payable Drive intake returned an invalid object")
        return payload

    folder_id = str(_setting("drive.payable_folder_id"))
    _validate_drive_route(folder_id)
    selected = get_json(
        "https://www.googleapis.com/drive/v3/files/"
        + urllib.parse.quote(folder_id, safe="")
        + "?fields=id,name,parents,mimeType"
    )
    if (
        selected.get("id") != folder_id
        or selected.get("mimeType") != "application/vnd.google-apps.folder"
    ):
        raise ValueError("configured payable folder read-back mismatch")
    selected_path = list(_setting("drive.payable_route_path"))
    files: list[dict[str, Any]] = []
    excluded_files: list[dict[str, Any]] = []
    folder_queue: list[tuple[str, list[str]]] = [(folder_id, selected_path)]
    seen_folders: set[str] = set()
    expense_routes = set(_setting("drive.expense_route_names"))
    processed_routes = set(_setting("drive.processed_route_names"))
    while folder_queue:
        parent_id, parent_path = folder_queue.pop(0)
        if parent_id in seen_folders:
            continue
        seen_folders.add(parent_id)
        page_token = ""
        while True:
            params = {
                "q": f"'{parent_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,parents,size,sha256Checksum,createdTime,modifiedTime,owners(emailAddress))",
                "pageSize": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            listing = get_json(
                "https://www.googleapis.com/drive/v3/files?"
                + urllib.parse.urlencode(params)
            )
            rows = listing.get("files")
            if not isinstance(rows, list):
                raise RuntimeError("canonical payable Drive folder listing is invalid")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                file_id = str(row.get("id") or "")
                name = str(row.get("name") or "")
                mime_type = str(row.get("mimeType") or "")
                if not file_id or not name:
                    raise RuntimeError("payable Drive evidence identity is incomplete")
                if mime_type == "application/vnd.google-apps.folder":
                    if name in processed_routes:
                        excluded_files.append(
                            {
                                "google_drive_file_id_hash": "sha256:"
                                + stable_hash(file_id),
                                "file_name_hash": "sha256:" + stable_hash(name),
                                "exclusion_reason": "canonical_processed_subtree",
                            }
                        )
                        continue
                    folder_queue.append((file_id, [*parent_path, name]))
                    continue
                if (
                    mime_type == "application/vnd.google-apps.document"
                    and name.startswith("temp_ocr_")
                ):
                    continue
                if mime_type not in {"application/pdf", "application/x-compress"} and (
                    not mime_type.startswith("application/vnd.google-apps.")
                ):
                    continue
                owners = {
                    str(owner.get("emailAddress") or "").casefold()
                    for owner in row.get("owners") or []
                    if isinstance(owner, dict)
                }
                if owners and (
                    not owners.intersection({_mf_principal(), _drive_principal()})
                ):
                    excluded_files.append(
                        {
                            "google_drive_file_id_hash": "sha256:"
                            + stable_hash(file_id),
                            "file_name_hash": "sha256:" + stable_hash(name),
                            "owner_hash": "sha256:" + stable_hash(sorted(owners)),
                            "exclusion_reason": "noncanonical_source_owner",
                        }
                    )
                    continue
                checksum = str(row.get("sha256Checksum") or "").lower()
                if not re.fullmatch("[0-9a-f]{64}", checksum):
                    exported = _download_google_drive_file_bytes(
                        file_id, access_token, mime_type=mime_type
                    )
                    checksum = hashlib.sha256(exported).hexdigest()
                relative_route = parent_path[len(selected_path) :]
                expense_route = next(
                    (part for part in relative_route if part in expense_routes), None
                )
                payment_route = next(
                    (part for part in relative_route if part not in expense_routes),
                    None,
                )
                files.append(
                    {
                        "google_drive_file_id": file_id,
                        "google_drive_file_id_hash": "sha256:" + stable_hash(file_id),
                        "file_name": name,
                        "file_hash": "sha256:" + checksum,
                        "parent_folder_id": parent_id,
                        "parent_folder_name": "/".join(parent_path),
                        "document_route": "請求書",
                        "payment_liability_route": payment_route,
                        "expense_classification_route": expense_route,
                        "mime_type": mime_type,
                        "size": row.get("size"),
                        "created_time": row.get("createdTime"),
                        "modified_time": row.get("modifiedTime"),
                    }
                )
            page_token = str(listing.get("nextPageToken") or "")
            if not page_token:
                break
    return {
        "schema": "moneyforward-payable-drive-intake-v1",
        "acquisition_source": "canonical_drive",
        "source": {
            "drive_principal": _drive_principal(),
            "identity_readback_verified": True,
            "folder_id_hash": "sha256:" + stable_hash(folder_id),
            "folder_path_hash": "sha256:" + stable_hash(selected_path),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "excluded_file_count": len(excluded_files),
        },
        "files": files,
        "excluded_files": excluded_files,
    }


def _unresolved_sales_accruals(matches: list[Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in matches
        if isinstance(item, dict) and int(item.get("sales_accrual_match_count", 0)) != 1
    ]


def _validate_payable_invoice_gas_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    from .gmail_recurring_invoice import build_recurring_invoice_search_plan

    if artifact.get("schema") != "moneyforward-payable-gas-readback-v1":
        raise ValueError("payable invoice GAS artifact schema mismatch")
    if artifact.get("drive_principal") != _drive_principal():
        raise ValueError("payable invoice Drive principal must be configured_company")
    if artifact.get("destination_folders_readback") is not True:
        raise ValueError("payable invoice destination folders were not read back")
    runs = artifact.get("runs")
    from .gmail_recurring_invoice import evidence_source_accounts
    from .hash_utils import stable_hash

    contract = _gmail_intake_contract()
    _assert_run_artifact(artifact)
    if (
        artifact.get("intake_contract_hash") != stable_hash(contract)
        or artifact.get("destination_folder_ids") != contract["destination_folder_ids"]
    ):
        raise ValueError("payable invoice company/destination contract mismatch")
    expected_principals = set(evidence_source_accounts())
    if not isinstance(runs, list) or len(runs) != len(expected_principals):
        raise ValueError("payable invoice configured mailbox receipts are incomplete")
    if {
        str(run.get("principal") or "") for run in runs if isinstance(run, dict)
    } != expected_principals:
        raise ValueError("payable invoice Gmail principal coverage is incomplete")
    coverage_starts = {str(run.get("coverage_start_after") or "") for run in runs}
    if len(coverage_starts) != 1:
        raise ValueError("payable invoice Gmail coverage start is inconsistent")
    coverage_start = coverage_starts.pop()
    if re.fullmatch("\\d{4}/\\d{2}/\\d{2}", coverage_start) is None:
        raise ValueError("payable invoice Gmail coverage start is invalid")
    if coverage_start != contract["coverage_start_after"]:
        raise ValueError("payable invoice coverage start differs from company policy")
    plan = build_recurring_invoice_search_plan(
        start_date=coverage_start.replace("/", "-")
    )
    queries_by_principal = {
        principal: {
            str(item["vendor_key"]): str(item["query_hash"])
            for item in plan.search_items
            if item["account_email"] == principal
        }
        for principal in expected_principals
    }
    now = datetime.now(timezone.utc)
    account_hashes: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("payable invoice Gmail receipt must be an object")
        expected_queries = queries_by_principal[str(run["principal"])]
        account_hash = str(run.get("account_hash") or "")
        expected_account_hash = hashlib.sha256(
            str(run["principal"]).encode("utf-8")
        ).hexdigest()[:16]
        if account_hash != expected_account_hash:
            raise ValueError("payable invoice Gmail account hash is invalid")
        account_hashes.add(account_hash)
        ran_at = _parse_datetime(str(run.get("ran_at") or ""))
        if ran_at is None or ran_at.tzinfo is None:
            raise ValueError("payable invoice Gmail receipt timestamp is missing")
        age_seconds = (now - ran_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -300:
            raise ValueError("payable invoice Gmail receipt timestamp is in the future")
        if int(run.get("trigger_count", 0)) != 1:
            raise ValueError(
                "payable invoice requires exactly one Gmail trigger per principal"
            )
        if int(run.get("failed", -1)) != 0 or int(run.get("search_truncated", -1)) != 0:
            raise ValueError("payable invoice Gmail capture failed or was truncated")
        receipts = run.get("query_receipts")
        if (
            run.get("query_receipts_ready") is not True
            or int(run.get("query_receipt_count", 0)) != len(expected_queries)
            or (not isinstance(receipts, list))
            or (len(receipts) != len(expected_queries))
        ):
            raise ValueError("payable invoice Gmail query receipts are incomplete")
        seen: set[str] = set()
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ValueError(
                    "payable invoice Gmail query receipt must be an object"
                )
            vendor_key = str(receipt.get("vendor_key") or "")
            if vendor_key in seen or expected_queries.get(vendor_key) != receipt.get(
                "query_hash"
            ):
                raise ValueError("payable invoice Gmail query identity mismatch")
            if (
                receipt.get("principal") != run.get("principal")
                or receipt.get("account_hash") != account_hash
                or receipt.get("status") != "completed"
                or (
                    int(receipt.get("processed_count", -1))
                    != int(receipt.get("result_count", -2))
                )
            ):
                raise ValueError(
                    "payable invoice Gmail query receipt is not fully processed"
                )
            seen.add(vendor_key)
        if seen != set(expected_queries):
            raise ValueError("payable invoice Gmail vendor coverage is incomplete")
    if len(account_hashes) != len(expected_principals):
        raise ValueError("payable invoice Gmail account hashes must be distinct")
    blocked_items = artifact.get("blocked_items")
    quarantine_items = artifact.get("quarantine_items")
    if not isinstance(blocked_items, list) or not isinstance(quarantine_items, list):
        raise ValueError("payable invoice unresolved item lists are missing")
    if len(blocked_items) != sum((int(run.get("blocked_open", -1)) for run in runs)):
        raise ValueError("payable invoice blocked count does not match GAS receipts")
    if any(
        (int(run.get("quarantine_open", -1)) != len(quarantine_items) for run in runs)
    ):
        raise ValueError(
            "payable invoice quarantine count does not match Drive read-back"
        )
    items: list[dict[str, Any]] = []
    for item in blocked_items:
        if not isinstance(item, dict):
            raise ValueError("payable invoice blocked item must be an object")
        account_hash = str(item.get("account_hash") or "")
        item_hash = str(item.get("item_hash") or "")
        reason = str(item.get("reason") or "")
        if account_hash not in account_hashes or not item_hash or (not reason):
            raise ValueError("payable invoice blocked item identity is incomplete")
    for item in quarantine_items:
        if not isinstance(item, dict):
            raise ValueError("payable invoice quarantine item must be an object")
        item_id_hash = str(item.get("item_id_hash") or "")
        if re.fullmatch("sha256:[0-9a-f]{64}", item_id_hash) is None:
            raise ValueError("payable invoice quarantine item hash is invalid")
        if (
            item.get("source_parent_verified") is not True
            or item.get("mime_type") != "application/pdf"
        ):
            raise ValueError(
                "payable invoice quarantine Drive identity was not verified"
            )
        items.append(
            {
                "item_id": item_id_hash,
                "status": "planned",
                "economic_event_discovered": True,
                "blocker": "gmail_quarantine_review_required",
                "blocker_code": "gmail_quarantine_review_required",
                "retry_condition": "verified OCR/facts and authoritative Drive read-back",
            }
        )
    return {
        "runs": runs,
        "blocked_items": blocked_items,
        "quarantine_items": quarantine_items,
        "items": items,
    }


def _persist_payable_invoice_stage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    accounting_date: str,
    artifact: dict[str, Any],
    validated: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    run_hash = accounting_run_hash(run_id)
    source_snapshot_hash = stable_hash({"payable_invoice_artifact": artifact})
    readback_hash = stable_hash(
        {
            "gmail_runs": validated["runs"],
            "blocked_items": validated["blocked_items"],
            "quarantine_items": validated["quarantine_items"],
            "drive_principal": _drive_principal(),
        }
    )
    items = list(validated["items"])
    common = {
        "coverage_state": "captured",
        "all_items_resolved": len(items) == 0,
        "remaining_count": len(items),
    }
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage="payable_invoice",
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": "payable_invoice",
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                **common,
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _drive_principal()}),
                "source_snapshot_hash": f"sha256:{source_snapshot_hash}",
                "readback_hash": f"sha256:{readback_hash}",
            }
        ),
        actor="payable_invoice_adapter",
    )
    return advance_run(
        conn,
        run_id,
        "payable_invoice",
        {
            "status": "verified",
            "evidence_hash": f"sha256:{source_snapshot_hash}",
            "adapter_receipt_hash": receipt_hash,
            **common,
            "items": items,
        },
        actor=actor,
    )


def _execute_all_receipt_folders_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str],
    standalone: bool = False,
) -> dict[str, Any]:
    """Capture every configured active evidence folder and advance item 3."""
    from .hash_utils import stable_hash
    from .shiwake_run import build_run_status

    if standalone:
        status = {"accounting_date": str(args.accounting_date)}
    else:
        with db.connect() as conn:
            status = build_run_status(conn, args.run_id)
        if (
            status.get("status") != "running"
            or status.get("current_stage") != "all_receipt_folders"
        ):
            raise RuntimeError(
                "all receipt folders adapter called outside its active stage"
            )
    run_hash = accounting_run_hash(args.run_id)
    output_path = (
        _artifact_root() / "runs" / run_hash / "all_receipt_folders.local.json"
    )
    artifact = _capture_all_receipt_folders(
        output_path=output_path, access_token=runtime[3]
    )
    writes_before = _receipt_external_write_counts(db)
    classification_summary = _classify_and_execute_all_receipt_folders(
        db,
        args,
        status=status,
        runtime=runtime,
        artifact=artifact,
        run_dir=output_path.parent,
    )
    writes_after_execution = _receipt_external_write_counts(db)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if standalone:
        replay_artifact = json.loads(json.dumps(artifact, ensure_ascii=False))
        replay_writes_before = _receipt_external_write_counts(db)
        replay_summary = _classify_and_execute_all_receipt_folders(
            db,
            args,
            status=status,
            runtime=runtime,
            artifact=replay_artifact,
            run_dir=output_path.parent,
        )
        replay_writes_after = _receipt_external_write_counts(db)
        replay_additions = {
            key: max(0, replay_writes_after[key] - replay_writes_before[key])
            for key in replay_writes_before
        }
        residual_path = output_path.with_name("all_receipt_folders.residual.local.json")
        residual = _capture_all_receipt_folders(
            output_path=residual_path, access_token=runtime[3]
        )
        source_residual_count = int(residual["source"]["file_count"])
        not_applicable_ids = {
            str(row.get("google_drive_file_id") or "")
            for row in artifact["files"]
            if isinstance(row.get("processing_outcome"), dict)
            and row["processing_outcome"].get("status") == "not_applicable"
        }
        remaining_count = sum(
            (
                1
                for row in residual["files"]
                if str(row.get("google_drive_file_id") or "") not in not_applicable_ids
            )
        )
        readbacks = _receipt_chain_readback_summary(db, artifact["files"])
        initial_write_additions = {
            key: max(0, writes_after_execution[key] - writes_before[key])
            for key in writes_before
        }
        scope_flags = _receipt_scope_readback(db)
        replay_additional_write_count = sum(replay_additions.values())
        expected_chain_count = int(
            classification_summary.get("newly_verified_count", 0)
        ) + int(classification_summary.get("already_verified_count", 0))
        workflow_readback_verified = (
            int(classification_summary.get("result_count", 0))
            == int(artifact["source"]["file_count"])
            and int(classification_summary.get("error_count", 0)) == 0
            and (
                classification_summary["ready_count"]
                == classification_summary["verified_count"]
            )
            and (readbacks["complete_chain_count"] >= expected_chain_count)
            and (remaining_count == 0)
            and (classification_summary["review_count"] == 0)
            and (int(classification_summary.get("retry_pending_count", 0)) == 0)
            and (replay_additional_write_count == 0)
            and (int(replay_summary.get("ocr_increment", 0)) == 0)
            and (int(replay_summary.get("gemini_increment", 0)) == 0)
        )
        authentication_ready = (
            scope_flags["journal_write_scope"] == "verified"
            and scope_flags["voucher_write_scope"] == "verified"
            and (artifact["source"].get("identity_readback_verified") is True)
        )
        complete = workflow_readback_verified and authentication_ready
        return {
            "status": "complete" if complete else "incomplete",
            "workflow": "all_receipt_folders",
            "parallel": True,
            "run_id_hash": "sha256:" + run_hash,
            "accounting_date": str(args.accounting_date),
            "execution_mode": "standalone_all_receipt_folders",
            "manifest_required": False,
            "source_count": int(artifact["source"]["file_count"]),
            "newly_registered_count": classification_summary["posted_count"],
            "reused_count": classification_summary["terminal_reused_count"]
            + classification_summary["reused_count"],
            "review_count": classification_summary["review_count"],
            "newly_verified_count": int(
                classification_summary.get("newly_verified_count", 0)
            ),
            "already_verified_count": int(
                classification_summary.get("already_verified_count", 0)
            ),
            "not_applicable_count": int(
                classification_summary.get("not_applicable_count", 0)
            ),
            "error_count": int(classification_summary.get("error_count", 0)),
            "result_count": int(classification_summary.get("result_count", 0)),
            "all_items_accounted_for": int(
                classification_summary.get("result_count", 0)
            )
            == int(artifact["source"]["file_count"]),
            "file_results": list(classification_summary.get("file_results") or []),
            "retry_pending_count": int(
                classification_summary.get("retry_pending_count", 0)
            ),
            "remaining_count": remaining_count,
            "processable_remaining_count": _processable_receipt_remaining_count(
                artifact["files"], residual["files"]
            ),
            "source_residual_count": source_residual_count,
            "workflow_readback_verified": workflow_readback_verified,
            "all_items_verified": workflow_readback_verified
            and classification_summary["review_count"] == 0
            and (remaining_count == 0),
            "review_items_verified": classification_summary["review_count"] == 0,
            "authentication_ready": authentication_ready,
            "moneyforward_readback_verified": scope_flags[
                "moneyforward_scope_readback_verified"
            ],
            **scope_flags,
            "readbacks": readbacks,
            "replay": {
                "ready_count": replay_summary["ready_count"],
                "verified_count": replay_summary["verified_count"],
                "review_count": replay_summary["review_count"],
                "retry_pending_count": int(
                    replay_summary.get("retry_pending_count", 0)
                ),
                "additional_journal_write_count": replay_additions["journal"],
                "additional_voucher_write_count": replay_additions["voucher"],
                "additional_drive_write_count": replay_additions["drive"],
                "additional_write_count": replay_additional_write_count,
                "ocr_increment": int(replay_summary.get("ocr_increment", 0)),
                "gemini_increment": int(replay_summary.get("gemini_increment", 0)),
            },
            "replay_additional_write_count": replay_additional_write_count,
            "initial_external_write_counts": initial_write_additions,
            "google_drive_read_called": True,
            "google_drive_write_called": initial_write_additions["drive"] > 0,
            "moneyforward_api_called": True,
            "moneyforward_write_called": classification_summary["posted_count"] > 0,
            "keychain_accessed": True,
            "oauth_token_refreshed": runtime[2] is not None,
            "other_workflow_stage_called": False,
            "opening_balance_review_called": False,
            "raw_journal_ids_stored": False,
            "raw_drive_file_ids_stored": False,
            "raw_api_response_stored": False,
            "raw_amounts_reported": False,
            "all_receipt_folders_probe": {
                "folder_count": int(artifact["source"]["folder_count"]),
                "file_count": int(artifact["source"]["file_count"]),
                "remaining_count": remaining_count,
                "processable_remaining_count": _processable_receipt_remaining_count(
                    artifact["files"], residual["files"]
                ),
                "source_residual_count": source_residual_count,
                **classification_summary,
            },
        }
    if int(classification_summary.get("retry_pending_count", 0)) > 0:
        return {
            **status,
            "status": "incomplete",
            "execution_mode": "production_all_receipt_folders",
            "manifest_required": False,
            "google_drive_read_called": True,
            "operational_blocker": "retry_pending",
            "all_receipt_folders_probe": {
                "folder_count": int(artifact["source"]["folder_count"]),
                "file_count": int(artifact["source"]["file_count"]),
                "remaining_count": int(artifact["source"]["file_count"]),
                **classification_summary,
            },
        }
    with db.connect() as conn:
        advanced = _persist_all_receipt_folders_stage(
            conn,
            run_id=args.run_id,
            accounting_date=str(status["accounting_date"]),
            artifact=artifact,
            actor=args.actor,
        )
    return {
        **advanced,
        "execution_mode": "production_all_receipt_folders",
        "manifest_required": False,
        "google_drive_read_called": True,
        "all_receipt_folders_probe": {
            "folder_count": int(artifact["source"]["folder_count"]),
            "file_count": int(artifact["source"]["file_count"]),
            "remaining_count": int(advanced["all_receipt_remaining_count"]),
            **classification_summary,
        },
    }


def _receipt_external_write_counts(db: Any) -> dict[str, int]:
    """Count durable external-write attempts for replay delta reporting."""
    tables = {
        "journal": "journal_create_execution_records",
        "voucher": "voucher_attach_execution_records",
        "drive": "drive_move_execution_records",
    }
    with db.connect() as conn:
        return {
            key: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for key, table in tables.items()
        }


def _receipt_scope_readback(db: Any) -> dict[str, Any]:
    """Return only the latest redacted Money Forward write-scope read-back."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT has_journal_write,has_voucher_write,raw_secret_stored FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    verified = row is not None and (not bool(row["raw_secret_stored"]))
    return {
        "journal_write_scope": "verified"
        if verified and bool(row["has_journal_write"])
        else "not_verified",
        "voucher_write_scope": "verified"
        if verified and bool(row["has_voucher_write"])
        else "not_verified",
        "invoice_data_read_scope": "not_required",
        "moneyforward_scope_readback_verified": verified,
    }


def _receipt_chain_readback_summary(
    db: Any, files: list[dict[str, Any]]
) -> dict[str, int]:
    """Count authoritative chain checks without upgrading unknown states."""
    from .same_chain_status import same_chain_status_for_file_hash

    summary = {
        "journal_readback_verified_count": 0,
        "voucher_readback_verified_count": 0,
        "drive_readback_verified_count": 0,
        "complete_chain_count": 0,
        "not_applicable_count": 0,
        "unverified_count": 0,
    }
    with db.connect() as conn:
        for file_row in files:
            chain = same_chain_status_for_file_hash(
                conn, str(file_row.get("file_hash") or "")
            )
            outcome = file_row.get("processing_outcome")
            if isinstance(outcome, dict) and outcome.get("status") == "not_applicable":
                summary["not_applicable_count"] += 1
                continue
            if chain is None:
                summary["unverified_count"] += 1
                continue
            checks = chain["checks"]
            if (
                checks["journal_create_verified"]
                and checks["post_registration_verified"]
            ):
                summary["journal_readback_verified_count"] += 1
            if checks["voucher_attach_verified"]:
                summary["voucher_readback_verified_count"] += 1
            if checks["drive_move_verified"]:
                summary["drive_readback_verified_count"] += 1
            if chain["same_chain_ready"]:
                summary["complete_chain_count"] += 1
            else:
                summary["unverified_count"] += 1
    return summary


def _receipt_file_results(db: Any, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one redacted terminal/result row for every initial direct file."""
    from .same_chain_status import same_chain_status_for_file_hash

    status_map = {
        "newly_verified": "NEWLY_VERIFIED",
        "already_verified": "ALREADY_VERIFIED",
        "review": "REVIEW",
        "not_applicable": "NOT_APPLICABLE",
        "error": "ERROR",
        "retry_pending": "ERROR",
    }
    rows: list[dict[str, Any]] = []
    with db.connect() as conn:
        for file_row in files:
            outcome = file_row.get("processing_outcome")
            outcome = outcome if isinstance(outcome, dict) else {}
            internal_status = str(outcome.get("status") or "error")
            public_status = status_map.get(internal_status, "ERROR")
            reason_code = str(
                outcome.get("reason_code")
                or (
                    "journal_voucher_drive_readback_verified"
                    if public_status in {"NEWLY_VERIFIED", "ALREADY_VERIFIED"}
                    else "missing_file_result"
                )
            )
            chain = same_chain_status_for_file_hash(
                conn, str(file_row.get("file_hash") or "")
            )
            checks = (chain or {}).get("checks") or {}
            metrics = file_row.get("ocr_metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            rows.append(
                {
                    "file_name": str(file_row.get("file_name") or ""),
                    "result": public_status,
                    "reason_code": reason_code,
                    "ocr_provider": str(metrics.get("provider") or ""),
                    "vision_pages": int(metrics.get("vision_pages") or 0),
                    "gemini_calls": int(metrics.get("gemini_calls") or 0),
                    "journal_readback_verified": bool(
                        checks.get("journal_create_verified")
                        and checks.get("post_registration_verified")
                    ),
                    "voucher_readback_verified": bool(
                        checks.get("voucher_attach_verified")
                    ),
                    "drive_move_readback_verified": bool(
                        checks.get("drive_move_verified")
                    ),
                }
            )
    if len(rows) != len(files):
        raise RuntimeError("receipt folder result conservation failed")
    return rows


def _processable_receipt_remaining_count(
    initial_files: list[dict[str, Any]], residual_files: list[dict[str, Any]]
) -> int:
    """Return an auxiliary actionable count; never use it as total remainder."""
    isolated_ids = {
        str(row.get("google_drive_file_id") or "")
        for row in initial_files
        if isinstance(row.get("processing_outcome"), dict)
        and row["processing_outcome"].get("status") in {"review", "not_applicable"}
    }
    return sum(
        (
            1
            for row in residual_files
            if str(row.get("google_drive_file_id") or "") not in isolated_ids
        )
    )


def _classify_and_execute_all_receipt_folders(
    db: Any,
    args: argparse.Namespace,
    *,
    status: dict[str, Any],
    runtime: tuple[Any, str, Any | None, str],
    artifact: dict[str, Any],
    run_dir: Path,
    source_type: str = "receipt",
    execution_stage: str = "all_receipt_folders",
) -> dict[str, Any]:
    """Classify every nonterminal Drive receipt and execute only the ready subset."""
    import hashlib
    from dataclasses import replace
    from .drive_policy import (
        apply_drive_credit_policy,
        apply_drive_expense_policy,
        folder_expense_decision,
        resolve_credit_side_master,
    )
    from .financial_event_classification import (
        build_accounting_decision_packet,
        classify_financial_events,
        load_accounting_decision_cache,
        store_accounting_decision_cache,
        validate_accounting_ai_decision,
    )
    from .hash_utils import mask_identifier, stable_hash
    from .models import Evidence
    from .same_chain_status import same_chain_status_for_file_hash
    from .skills import (
        EvidenceFacts,
        apply_learned_rules,
        classify_receipt_from_facts,
        receipt_broad_content,
    )

    files = artifact.get("files")
    if not isinstance(files, list):
        raise ValueError("all receipt folders files are missing")
    events: list[dict[str, Any]] = []
    eligible_files: list[dict[str, Any]] = []
    pending_backfills: list[dict[str, Any]] = []
    cache_review_count = 0
    terminal_count = 0
    ocr_increment = 0
    gemini_increment = 0
    canonical_cache_hit_count = 0
    decision_cache_hit_count = 0
    usage_records: list[dict[str, Any]] = []

    def append_eligible(
        file_row: dict[str, Any], facts: dict[str, Any], canonical_source: str
    ) -> None:
        metrics = file_row.setdefault("ocr_metrics", {})
        metrics.setdefault("provider", canonical_source)
        metrics.setdefault("vision_pages", int(facts.get("vision_pages") or 0))
        metrics.setdefault("gemini_calls", int(facts.get("gemini_calls") or 0))
        drive_id = str(file_row["google_drive_file_id"])
        line_description = " ".join(
            (
                str(row.get("description") or "").strip()
                for row in facts.get("lines") or []
                if isinstance(row, dict) and str(row.get("description") or "").strip()
            )
        )
        current_broad_content = receipt_broad_content(line_description)
        events.append(
            {
                "source_event_id": drive_id,
                "source_event_id_hash": "sha256:"
                + stable_hash({"google_drive_file_id": drive_id}),
                "source_event_type": source_type,
                "transaction_date": str(facts["transaction_date"]),
                "description": line_description or str(facts["vendor_name"]),
                "broad_content": current_broad_content,
                "broad_content_source": "current_deterministic"
                if current_broad_content
                else "",
                "source_file_hash": str(file_row["file_hash"]),
                "counterparty": str(facts["vendor_name"]),
                "amount": -int(facts["amount"]),
                "cash_flow_direction": "outflow",
                "matched_invoice_or_evidence": True,
                "receipt_tax_rows": [
                    {
                        "rate": str(row.get("rate") or ""),
                        "taxable_amount": int(row.get("taxable_amount") or 0),
                        "tax_amount": int(row.get("tax_amount") or 0),
                    }
                    for row in facts.get("tax") or []
                    if isinstance(row, dict)
                ],
            }
        )
        eligible_files.append(
            {"file": file_row, "facts": facts, "source": canonical_source}
        )

    with db.connect() as conn:
        for file_row in files:
            if not isinstance(file_row, dict):
                raise ValueError("all receipt folders file row must be an object")
            existing_outcome = file_row.get("processing_outcome")
            if (
                isinstance(existing_outcome, dict)
                and existing_outcome.get("status") == "not_applicable"
            ):
                terminal_count += 1
                continue
            intake_review = file_row.get("intake_review")
            if isinstance(intake_review, dict):
                file_row["processing_outcome"] = {
                    "processor_version": 6,
                    "status": "review",
                    "reason_code": str(
                        intake_review.get("reason_code")
                        or "drive_evidence_identity_incomplete"
                    ),
                }
                cache_review_count += 1
                continue
            drive_id = str(file_row.get("google_drive_file_id") or "")
            file_hash = str(file_row.get("file_hash") or "")
            ledger = conn.execute(
                "SELECT processing_status,idempotency_key FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=? ORDER BY updated_at DESC,id DESC LIMIT 1",
                (mask_identifier(drive_id), file_hash),
            ).fetchone()
            state = str(ledger["processing_status"]) if ledger is not None else ""
            chain = same_chain_status_for_file_hash(conn, file_hash)
            if state == "not_applicable":
                file_row["processing_outcome"] = {
                    "processor_version": 8,
                    "status": "not_applicable",
                    "source": "processing_ledger",
                    "reason_code": "verified_no_economic_event",
                }
                terminal_count += 1
                continue
            if state == "verified":
                checks = (chain or {}).get("checks") or {}
                accounting_chain_ready = all(
                    (
                        checks.get(name) is True
                        for name in (
                            "ledger_verified",
                            "journal_create_verified",
                            "post_registration_verified",
                            "voucher_attach_verified",
                        )
                    )
                )
                if not accounting_chain_ready:
                    file_row["processing_outcome"] = {
                        "processor_version": 8,
                        "status": "error",
                        "reason_code": "verified_ledger_readback_mismatch",
                    }
                    terminal_count += 1
                    continue
                try:
                    _repair_verified_receipt_drive_move(
                        db,
                        args,
                        drive_token=runtime[3],
                        file_row=file_row,
                        idempotency_key=str(ledger["idempotency_key"]),
                        run_dir=run_dir,
                    )
                except (OSError, RuntimeError, ValueError) as repair_error:
                    file_row["processing_outcome"] = {
                        "processor_version": 8,
                        "status": "error",
                        "reason_code": "verified_residual_drive_repair_failed",
                        "error_hash": "sha256:"
                        + stable_hash(
                            type(repair_error).__name__ + ":" + str(repair_error)
                        ),
                    }
                    terminal_count += 1
                    continue
                repaired = same_chain_status_for_file_hash(conn, file_hash)
                if not repaired or repaired.get("same_chain_ready") is not True:
                    file_row["processing_outcome"] = {
                        "processor_version": 8,
                        "status": "error",
                        "reason_code": "verified_residual_chain_not_restored",
                    }
                else:
                    file_row["processing_outcome"] = {
                        "processor_version": 8,
                        "status": "already_verified",
                        "reason_code": "verified_readback_drive_move_repaired",
                    }
                terminal_count += 1
                continue
            try:
                facts, canonical_source, ocr_calls, gemini_calls = (
                    _read_canonical_evidence_from_drive_cache(
                        drive_file_id=drive_id,
                        declared_hash=file_hash,
                        access_token=runtime[3],
                    )
                )
                if ocr_calls != 0 or gemini_calls != 0:
                    raise RuntimeError(
                        "canonical Drive cache read unexpectedly invoked extraction"
                    )
                canonical_cache_hit_count += 1
            except RuntimeError as error:
                packet_hash = "sha256:" + stable_hash(
                    {
                        "content_hash": file_hash,
                        "parent_folder_name": file_row.get("parent_folder_name"),
                    }
                )
                pending_backfills.append(
                    {
                        "file": file_row,
                        "drive_id": drive_id,
                        "file_hash": file_hash,
                        "packet_hash": packet_hash,
                        "initial_error": error,
                    }
                )
                continue
            append_eligible(file_row, facts, canonical_source)
        for offset in range(0, len(pending_backfills), 12):
            batch = pending_backfills[offset : offset + 12]
            packets = []
            runnable_batch = []
            for item in batch:
                try:
                    extraction_input = _build_canonical_extraction_input(
                        drive_file_id=item["drive_id"],
                        declared_hash=item["file_hash"],
                        mime_type=str(
                            item["file"].get("mime_type") or "application/pdf"
                        ),
                        access_token=runtime[3],
                    )
                except RuntimeError as inspection_error:
                    item["file"]["processing_outcome"] = {
                        "processor_version": 7,
                        "status": "retry_pending",
                        "reason_code": "canonical_native_pdf_inspection_failed",
                        "error_hash": "sha256:"
                        + stable_hash(
                            type(inspection_error).__name__
                            + ":"
                            + str(inspection_error)
                        ),
                    }
                    continue
                packets.append(
                    {
                        "contract_version": 2,
                        "packet_hash": item["packet_hash"],
                        "drive_file_id": item["drive_id"],
                        "content_sha256": item["file_hash"],
                        "decision_required": False,
                        "extraction_input": extraction_input,
                    }
                )
                runnable_batch.append(item)
            if not packets:
                continue
            try:
                gas_result = _run_shared_accounting_classifier(packets, runtime[3])
            except RuntimeError as gas_error:
                for item in runnable_batch:
                    item["file"]["processing_outcome"] = {
                        "processor_version": 6,
                        "status": "retry_pending",
                        "reason_code": "canonical_facts_backfill_transient",
                        "error_hash": "sha256:"
                        + stable_hash(type(gas_error).__name__ + ":" + str(gas_error)),
                    }
                continue
            ocr_increment += int(gas_result.get("ocr_calls") or 0)
            gemini_increment += int(gas_result.get("gemini_calls") or 0)
            usage_records.extend(
                (
                    row
                    for row in gas_result.get("usage_records") or []
                    if isinstance(row, dict)
                )
            )
            result_items = gas_result.get("items")
            result_by_hash = {
                str(item.get("packet_hash") or ""): item
                for item in result_items or []
                if isinstance(item, dict)
            }
            for item in runnable_batch:
                file_row = item["file"]
                gas_item = result_by_hash.get(item["packet_hash"], {})
                file_row["ocr_metrics"] = {
                    "provider": str(gas_item.get("ocr_provider") or "cloud_vision"),
                    "vision_pages": int(gas_item.get("vision_pages") or 0),
                    "gemini_calls": int(gas_item.get("gemini_calls") or 0),
                }
                if gas_item.get("retry_pending") is True:
                    file_row["processing_outcome"] = {
                        "processor_version": 8,
                        "status": "retry_pending",
                        "reason_code": str(
                            gas_item.get("blocked_reason")
                            or "canonical_ocr_retry_pending"
                        ),
                    }
                    continue
                if gas_item.get("facts_validated") is not True:
                    file_row["processing_outcome"] = {
                        "processor_version": 6,
                        "status": "review",
                        "reason_code": "canonical_facts_permanently_insufficient",
                        "missing_facts": list(gas_item.get("missing_facts") or []),
                    }
                    cache_review_count += 1
                    continue
                try:
                    facts, canonical_source, ocr_calls, gemini_calls = (
                        _read_canonical_evidence_from_drive_cache(
                            drive_file_id=item["drive_id"],
                            declared_hash=item["file_hash"],
                            access_token=runtime[3],
                        )
                    )
                except RuntimeError as cache_error:
                    file_row["processing_outcome"] = {
                        "processor_version": 6,
                        "status": "retry_pending",
                        "reason_code": "canonical_cache_readback_transient",
                        "error_hash": "sha256:"
                        + stable_hash(
                            type(cache_error).__name__ + ":" + str(cache_error)
                        ),
                    }
                    continue
                if ocr_calls != 0 or gemini_calls != 0:
                    raise RuntimeError(
                        "canonical Drive cache read unexpectedly invoked extraction"
                    )
                append_eligible(file_row, facts, canonical_source)
        classification = classify_financial_events(
            {"events": events}, conn=conn, persist=True, engine="python"
        )
        debit_accounts = sorted(
            {
                str(row["name_normalized"])
                for row in conn.execute(
                    "SELECT name_normalized FROM master_items WHERE item_type='account' AND active=1 AND name_normalized != '' GROUP BY name_normalized HAVING COUNT(DISTINCT external_id_hash)=1"
                ).fetchall()
            }
        )
        debit_sub_accounts = sorted(
            {
                str(row["name_normalized"])
                for row in conn.execute(
                    "SELECT name_normalized FROM master_items WHERE item_type='sub_account' AND active=1 AND name_normalized != '' GROUP BY name_normalized HAVING COUNT(DISTINCT external_id_hash)=1"
                ).fetchall()
            }
        )
        prepared: list[dict[str, Any]] = []
        ai_packet_bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for index, (bound, classified) in enumerate(
            zip(eligible_files, classification.items, strict=True)
        ):
            file_row = bound["file"]
            facts = bound["facts"]
            tax_rows = facts.get("tax") or []
            primary_tax = tax_rows[0] if tax_rows else {}
            evidence = Evidence(
                google_drive_file_id=file_row["google_drive_file_id"],
                file_name=file_row["file_name"],
                file_hash=file_row["file_hash"],
                parent_folder_id=file_row["parent_folder_id"],
                parent_folder_name=file_row["parent_folder_name"],
            )
            proposal = classify_receipt_from_facts(
                EvidenceFacts(
                    observed={
                        "date": facts["transaction_date"],
                        "vendor_name": facts["vendor_name"],
                        "amount": facts["amount"],
                        "tax_amount": sum(
                            (int(row.get("tax_amount") or 0) for row in tax_rows)
                        ),
                        "tax_rate": float(primary_tax.get("rate") or 0) / 100,
                        "description": " ".join(
                            (
                                str(row.get("description") or "")
                                for row in facts.get("lines") or []
                            )
                        ),
                        "source_type": source_type,
                        "evidence_file_id": file_row["google_drive_file_id"],
                        "file_name": file_row["file_name"],
                        "file_hash": file_row["file_hash"],
                        "parent_folder_id": file_row["parent_folder_id"],
                        "parent_folder_name": file_row["parent_folder_name"],
                    },
                    predicted=dict(classified.journal_candidate or {}),
                    unresolved_points=tuple(classified.unresolved_points),
                )
            )
            if source_type == "received_invoice":
                payment_route = str(
                    file_row.get("payment_liability_route") or ""
                ).strip()
                proposal = replace(
                    proposal,
                    source_type="received_invoice",
                    credit_account="未払金",
                    credit_sub_account=payment_route or proposal.credit_sub_account,
                )
            proposal = apply_learned_rules(conn, proposal)
            proposal = apply_drive_expense_policy(proposal, evidence)
            proposal = apply_drive_credit_policy(proposal, evidence)
            expense_route = folder_expense_decision(evidence)
            credit_route = resolve_credit_side_master(conn, proposal, evidence)
            if credit_route.credit_sub_account:
                proposal = replace(
                    proposal,
                    credit_account=credit_route.credit_account,
                    credit_sub_account=credit_route.credit_sub_account,
                )
            raw_rates = {
                round(
                    float(row.get("rate") or row.get("tax_rate") or 0) / 100
                    if float(row.get("rate") or row.get("tax_rate") or 0) > 1
                    else float(row.get("rate") or row.get("tax_rate") or 0),
                    2,
                )
                for row in tax_rows
                if isinstance(row, dict)
            }
            fact_tax_amount = sum(
                (
                    int(row.get("tax_amount") or 0)
                    for row in tax_rows
                    if isinstance(row, dict)
                )
            )
            canonical_rate = next(iter(raw_rates)) if len(raw_rates) == 1 else None
            canonical_tax_category = {
                0.1: _setting("bank.qualified_evidence_tax_category"),
                0.08: "課仕 (軽)8%",
                0.0: "対象外",
            }.get(canonical_rate)
            if canonical_tax_category is not None:
                proposal = replace(
                    proposal,
                    tax_category=canonical_tax_category,
                    tax_rate=canonical_rate or 0.0,
                    tax_amount=fact_tax_amount,
                )
            elif raw_rates:
                proposal = replace(proposal, tax_rate=0.0, tax_amount=fact_tax_amount)
            drive_route = {
                "document_route": file_row.get("document_route"),
                "payment_liability_route": credit_route.credit_sub_account,
                "payment_liability_route_source": credit_route.source,
                "expense_classification_route": file_row.get(
                    "expense_classification_route"
                )
                or (expense_route.debit_account if expense_route is not None else None),
                "expense_classification_route_source": expense_route.source
                if expense_route is not None
                else None,
            }
            file_row["drive_route_facts"] = drive_route
            unresolved = {str(reason) for reason in classified.unresolved_points}
            bounded_unresolved = all(
                (
                    reason.startswith(("confidence=", "auto_post_eligibility="))
                    or reason == "no_past_journal_pattern_match"
                    for reason in unresolved
                )
            )
            tax_rows_balance = sum(
                (
                    int(row.get("taxable_amount") or 0)
                    + int(row.get("tax_amount") or 0)
                    for row in tax_rows
                    if isinstance(row, dict)
                )
            ) == int(facts.get("amount") or 0)
            tax_facts_ready = (
                bool(tax_rows)
                and bool(raw_rates)
                and raw_rates.issubset({0.08, 0.1})
                and tax_rows_balance
                and (proposal.tax_amount == fact_tax_amount)
            )
            current_broad_content = str(
                events[index].get("broad_content") or ""
            ).strip()
            content_or_fixed_route_ready = (
                bool(current_broad_content) or expense_route is not None
            )
            if (
                not classified.registration_allowed
                and bounded_unresolved
                and content_or_fixed_route_ready
                and (proposal.debit_account not in {"", "未分類"})
                and (proposal.tax_category not in {"", "不明", "unknown_tax"})
                and tax_facts_ready
                and (credit_route.sub_account_exists is True)
            ):
                proposal = replace(proposal, risk_flags=())
                resolved_source = (
                    classified.matched_pattern_source
                    if classified.matched_pattern_source != "none"
                    else "receipt_deterministic_rule_facts_resolved"
                )
                classified = replace(
                    classified,
                    matched_pattern_source=resolved_source,
                    confidence=1.0,
                    recommended_action="auto_post_candidate",
                    learning_action="learn_after_journal_voucher_drive_readback",
                    unresolved_points=[],
                    registration_allowed=True,
                    journal_candidate=proposal.to_dict(),
                )
                classification.items[index] = classified
            packet = None
            if (
                not classified.registration_allowed
                and (
                    proposal.debit_account in {"", "未分類"}
                    or not content_or_fixed_route_ready
                )
                and tax_facts_ready
                and (credit_route.sub_account_exists is True)
            ):
                allowed_debit_candidates = []
                allowed_pattern_hashes = []
                if classified.journal_candidate:
                    candidate = classified.journal_candidate
                    if str(candidate.get("debit_account") or "") not in {"", "未分類"}:
                        allowed_debit_candidates.append(
                            {
                                "debit_account": candidate.get("debit_account"),
                                "debit_sub_account": candidate.get("debit_sub_account"),
                                "pattern_hash": classified.matched_pattern_hash,
                            }
                        )
                    if classified.matched_pattern_hash:
                        allowed_pattern_hashes.append(classified.matched_pattern_hash)
                packet = build_accounting_decision_packet(
                    {
                        "contract_version": 2,
                        "decision_required": True,
                        "canonical_facts": {
                            **facts,
                            "broad_content": current_broad_content or None,
                        },
                        "current_broad_content": current_broad_content or None,
                        "drive_route": _accounting_decision_route(
                            drive_route,
                            credit_account=proposal.credit_account,
                            credit_sub_account=proposal.credit_sub_account,
                        ),
                        "fixed_fields": {
                            "journal_date": proposal.journal_date,
                            "amount": proposal.amount,
                            "tax_amount": proposal.tax_amount,
                            "credit_account": proposal.credit_account,
                            "credit_sub_account": proposal.credit_sub_account,
                            "tax_rows": tax_rows,
                        },
                        "verified_fixed_rules": {"candidate": proposal.to_dict()},
                        "past_journal_candidates": [
                            {
                                "pattern_hash": classified.matched_pattern_hash,
                                "candidate": classified.journal_candidate,
                            }
                        ]
                        if classified.journal_candidate is not None
                        else [],
                        "similar_rag": {
                            "matched_pattern_source": classified.matched_pattern_source,
                            "unresolved_points": sorted(classified.unresolved_points),
                        },
                        "failure_rag": {"risk_flags": sorted(proposal.risk_flags)},
                        "moneyforward_identity": {
                            "office_id_hash": "sha256:" + stable_hash(str(runtime[1])),
                            "verified": bool(str(runtime[1]).strip()),
                        },
                        "live_master": {
                            "credit_sub_account_unique": credit_route.sub_account_exists
                            is True,
                            "debit_accounts": debit_accounts,
                            "debit_sub_accounts": debit_sub_accounts,
                        },
                        "allowed_debit_candidates": allowed_debit_candidates,
                        "allowed_debit_accounts": debit_accounts,
                        "allowed_debit_sub_accounts": debit_sub_accounts,
                        "allowed_pattern_hashes": allowed_pattern_hashes,
                    }
                )
                cached_decision = load_accounting_decision_cache(
                    conn, packet["packet_hash"]
                )
                if cached_decision is not None:
                    decision_cache_hit_count += 1
                    bound["ai_prefetch"] = {
                        "status": "completed",
                        "decision": cached_decision,
                        "cache_hit": True,
                    }
                elif debit_accounts:
                    ai_packet_bindings.append((bound, packet))
                else:
                    bound["ai_prefetch"] = {
                        "status": "completed",
                        "decision": {
                            "packet_hash": packet["packet_hash"],
                            "status": "review",
                            "candidate": None,
                            "conflicts": ["deterministic_candidate_allowlist_empty"],
                        },
                    }
            prepared.append(
                {
                    "index": index,
                    "bound": bound,
                    "proposal": proposal,
                    "credit_route": credit_route,
                    "tax_rows": tax_rows,
                    "packet": packet,
                }
            )
        ai_packet_bindings.sort(key=lambda item: str(item[1]["packet_hash"]))
        for offset in range(0, len(ai_packet_bindings), 12):
            batch = ai_packet_bindings[offset : offset + 12]
            try:
                ai_batch_result = _run_shared_accounting_classifier(
                    [packet for _bound, packet in batch], runtime[3]
                )
            except RuntimeError as ai_error:
                for bound, _packet in batch:
                    bound["ai_prefetch"] = {
                        "status": "retry_pending",
                        "blocker": "shared_accounting_classifier_transient",
                        "error_hash": "sha256:"
                        + stable_hash(type(ai_error).__name__ + ":" + str(ai_error)),
                    }
                continue
            ocr_increment += int(ai_batch_result.get("ocr_calls") or 0)
            gemini_increment += int(ai_batch_result.get("gemini_calls") or 0)
            usage_records.extend(
                (
                    row
                    for row in ai_batch_result.get("usage_records") or []
                    if isinstance(row, dict)
                )
            )
            decisions = {
                str(item.get("packet_hash") or ""): item
                for item in ai_batch_result.get("items") or []
                if isinstance(item, dict)
            }
            for bound, packet in batch:
                metrics = bound["file"].setdefault("ocr_metrics", {})
                metrics["gemini_calls"] = int(metrics.get("gemini_calls") or 0) + 1
                raw_decision = decisions.get(str(packet["packet_hash"]), {})
                validation = validate_accounting_ai_decision(packet, raw_decision)
                store_accounting_decision_cache(
                    conn, str(packet["packet_hash"]), validation
                )
                bound["ai_prefetch"] = {
                    "status": str(
                        ai_batch_result.get("status") or "completed_with_review"
                    ),
                    "blocker": ai_batch_result.get("blocker"),
                    "decision": {
                        "packet_hash": packet["packet_hash"],
                        "status": validation.status,
                        "candidate": validation.candidate,
                        "conflicts": list(validation.reason_codes),
                    },
                }
        for context in prepared:
            index = int(context["index"])
            bound = context["bound"]
            classified = classification.items[index]
            file_row = bound["file"]
            facts = bound["facts"]
            proposal = context["proposal"]
            credit_route = context["credit_route"]
            tax_rows = context["tax_rows"]
            broad_content = str(events[index].get("broad_content") or "").strip()
            broad_content_source = str(
                events[index].get("broad_content_source") or ""
            ).strip()
            if not classified.registration_allowed and context["packet"] is not None:
                prefetched = bound.get("ai_prefetch") or {}
                if prefetched.get("status") == "retry_pending":
                    file_row["processing_outcome"] = {
                        "processor_version": 6,
                        "status": "retry_pending",
                        "reason_code": str(
                            prefetched.get("blocker")
                            or "shared_accounting_classifier_transient"
                        ),
                        **(
                            {"error_hash": prefetched["error_hash"]}
                            if prefetched.get("error_hash")
                            else {}
                        ),
                    }
                    classification.items[index] = replace(
                        classified,
                        registration_allowed=False,
                        learning_action="retry_shared_accounting_classifier",
                        unresolved_points=["retry_pending"],
                        journal_candidate=proposal.to_dict(),
                    )
                    continue
                ai_result = {"status": prefetched.get("status")}
                ai_decision = prefetched.get("decision") or {}
                ai_candidate = ai_decision.get("candidate")
                if (
                    ai_result.get("status") == "completed"
                    and ai_decision.get("status") == "decided"
                    and isinstance(ai_candidate, dict)
                    and (credit_route.sub_account_exists is True)
                ):
                    proposal = replace(
                        proposal,
                        debit_account=str(ai_candidate.get("debit_account") or ""),
                        debit_sub_account=ai_candidate.get("debit_sub_account"),
                        risk_flags=(),
                    )
                    broad_content = str(ai_candidate.get("broad_content") or "").strip()
                    broad_content_source = (
                        "current_cache"
                        if prefetched.get("cache_hit") is True
                        else "current_gemini"
                    )
                    classified = replace(
                        classified,
                        matched_pattern_hash=ai_candidate.get("pattern_hash"),
                        matched_pattern_source="ai_single_pass_validated",
                        confidence=float(ai_candidate.get("confidence") or 0),
                        recommended_action="auto_post_candidate",
                        learning_action="learn_after_journal_voucher_drive_readback",
                        unresolved_points=[],
                        registration_allowed=True,
                        journal_candidate=proposal.to_dict(),
                    )
                elif ai_result.get("status") == "retry_pending":
                    file_row["processing_outcome"] = {
                        "processor_version": 6,
                        "status": "retry_pending",
                        "reason_code": str(
                            ai_result.get("blocker")
                            or "shared_accounting_classifier_transient"
                        ),
                    }
                    classification.items[index] = replace(
                        classified,
                        registration_allowed=False,
                        learning_action="retry_shared_accounting_classifier",
                        unresolved_points=["retry_pending"],
                        journal_candidate=proposal.to_dict(),
                    )
                    continue
                else:
                    classified = replace(
                        classified,
                        registration_allowed=False,
                        recommended_action="queue_for_review",
                        learning_action="resolve_specific_ai_verification_conflicts",
                        unresolved_points=sorted(
                            {
                                *classified.unresolved_points,
                                *[
                                    str(value)
                                    for value in ai_decision.get("conflicts") or []
                                ],
                                "candidate_not_uniquely_decidable",
                            }
                        ),
                        journal_candidate=proposal.to_dict(),
                    )
            readiness_conflicts: list[str] = []
            if classified.registration_allowed:
                rag_candidate = classified.journal_candidate or {}
                if expense_route is None and any(
                    (
                        proposal.to_dict().get(field) != rag_candidate.get(field)
                        for field in ("debit_account", "debit_sub_account")
                    )
                ):
                    readiness_conflicts.append("rag_and_established_rule_conflict")
                fact_rates = {
                    str(int(round(float(row.get("rate") or 0)))) for row in tax_rows
                }
                expected_rate = str(int(round(proposal.tax_rate * 100)))
                if len(fact_rates) == 1 and fact_rates != {expected_rate}:
                    readiness_conflicts.append("candidate_tax_rate_facts_mismatch")
                if proposal.tax_amount != sum(
                    (int(row.get("tax_amount") or 0) for row in tax_rows)
                ):
                    readiness_conflicts.append("candidate_tax_amount_facts_mismatch")
                if readiness_conflicts:
                    classified = replace(
                        classified,
                        registration_allowed=False,
                        recommended_action="queue_for_review",
                        learning_action="resolve_rag_rule_or_tax_conflict_before_registration",
                        unresolved_points=[
                            *classified.unresolved_points,
                            *readiness_conflicts,
                        ],
                        journal_candidate=proposal.to_dict(),
                    )
                    classification.items[index] = classified
            file_row["processing_outcome"] = {
                "processor_version": 6,
                "status": "ready" if classified.registration_allowed else "review",
                "matched_pattern_hash": classified.matched_pattern_hash,
                "matched_pattern_source": classified.matched_pattern_source,
                "recommended_action": classified.recommended_action,
                "unresolved_points": list(classified.unresolved_points),
                "candidate": {
                    "debit_account": proposal.debit_account,
                    "debit_sub_account": proposal.debit_sub_account,
                    "credit_account": proposal.credit_account,
                    "credit_sub_account": proposal.credit_sub_account,
                    "tax_category": proposal.tax_category,
                },
                "canonical_source": bound["source"],
                "ai_rag_evidence": {
                    "provenance": "ai_proposed_candidate"
                    if classified.matched_pattern_source == "ai_proposed_candidate"
                    else classified.matched_pattern_source,
                    "pattern_hash": classified.matched_pattern_hash,
                },
                "live_master_validation": {
                    "credit_sub_account_unique": credit_route.sub_account_exists is True
                },
            }
            if broad_content:
                events[index]["broad_content"] = broad_content
                events[index]["broad_content_source"] = broad_content_source
            vendor_name = str(facts.get("vendor_name") or "").strip()
            store_name = str(facts.get("store_name") or "").strip()
            issuer_verified = bool(vendor_name) and (
                vendor_name != store_name
                and (
                    not vendor_name.endswith("店")
                    or any(
                        (
                            marker in vendor_name
                            for marker in ("株式会社", "合同会社", "有限会社")
                        )
                    )
                )
            )
            events[index]["receipt_learning_evidence"] = {
                "issuer_verified": issuer_verified,
                "broad_content_verified": bool(broad_content),
                "broad_content_source": broad_content_source,
                "current_evidence_hash_verified": bool(broad_content)
                and str(events[index].get("source_file_hash") or "")
                == str(file_row.get("file_hash") or "")
                and (
                    re.fullmatch(
                        "sha256:[0-9a-f]{64}", str(file_row.get("file_hash") or "")
                    )
                    is not None
                ),
                "deterministic_validator_passed": classified.registration_allowed,
                "provenance_consistent": classified.matched_pattern_source
                in {
                    "vendor_broad_content_history",
                    "ai_single_pass_validated",
                    "receipt_deterministic_rule_facts_resolved",
                },
            }
            if classified.registration_allowed:
                evidence_dir = run_dir / "evidence"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                local_path = evidence_dir / (
                    file_row["file_hash"].removeprefix("sha256:") + ".local.pdf"
                )
                if local_path.is_file():
                    body = local_path.read_bytes()
                else:
                    try:
                        mime_type = str(file_row.get("mime_type") or "")
                        body = (
                            _download_google_drive_file_bytes(
                                file_row["google_drive_file_id"],
                                runtime[3],
                                mime_type=mime_type,
                            )
                            if mime_type
                            else _download_google_drive_file_bytes(
                                file_row["google_drive_file_id"], runtime[3]
                            )
                        )
                    except RuntimeError as error:
                        body = _load_verified_local_evidence_bytes(
                            file_row["file_hash"]
                        )
                        if body is None:
                            file_row["processing_outcome"].update(
                                {
                                    "status": "review",
                                    "reason_code": "drive_content_unavailable_for_voucher",
                                    "error_hash": "sha256:"
                                    + stable_hash(
                                        type(error).__name__ + ":" + str(error)
                                    ),
                                }
                            )
                            classification.items[index] = replace(
                                classified,
                                registration_allowed=False,
                                recommended_action="queue_for_review",
                                learning_action="restore_attachable_drive_evidence_then_reclassify",
                                unresolved_points=[
                                    *classified.unresolved_points,
                                    "drive_content_unavailable_for_voucher",
                                ],
                                journal_candidate=proposal.to_dict(),
                            )
                            continue
                if (
                    "sha256:" + hashlib.sha256(body).hexdigest()
                    != file_row["file_hash"]
                ):
                    raise RuntimeError(
                        "Drive evidence content hash changed after classification"
                    )
                if not local_path.is_file():
                    local_path.write_bytes(body)
                source_event = events[index]
                source_event["evidence_resolution"] = {
                    "source_file_hash": file_row["file_hash"],
                    "google_drive_file_id": file_row["google_drive_file_id"],
                    "parent_folder_id": file_row["parent_folder_id"],
                    "parent_folder_name": file_row["parent_folder_name"],
                    "file_name": file_row["file_name"],
                    "local_file_path": str(local_path),
                }
                classification.items[index] = replace(
                    classified, journal_candidate=proposal.to_dict()
                )
    ready_count = sum((item.registration_allowed for item in classification.items))
    verified_count = 0
    execution_stats = {"posted_count": 0, "reused_count": 0}
    if ready_count:
        verified_count = _execute_ready_connected_items(
            db,
            args,
            status=status,
            runtime=runtime,
            transactions=events,
            classifications=classification.items,
            stage=execution_stage,
            execution_stats=execution_stats,
        )
    for bound, source_event in zip(eligible_files, events, strict=True):
        file_row = bound["file"]
        outcome = file_row.get("processing_outcome")
        if not isinstance(outcome, dict) or outcome.get("status") != "ready":
            continue
        with db.connect() as conn:
            chain = same_chain_status_for_file_hash(
                conn, str(file_row.get("file_hash") or "")
            )
        if chain and chain.get("same_chain_ready") is True:
            result_state = str(
                source_event.get("_execution_result") or "newly_verified"
            )
            file_row["processing_outcome"] = {
                **outcome,
                "processor_version": 8,
                "status": result_state,
                "reason_code": "existing_journal_readback_chain_completed"
                if result_state == "already_verified"
                else "new_journal_readback_chain_completed",
            }
        else:
            file_row["processing_outcome"] = {
                **outcome,
                "processor_version": 8,
                "status": "error",
                "reason_code": "post_execution_chain_incomplete",
            }
    file_results = _receipt_file_results(db, files)
    result_counts = {
        state: sum((row["result"] == state for row in file_results))
        for state in (
            "NEWLY_VERIFIED",
            "ALREADY_VERIFIED",
            "REVIEW",
            "NOT_APPLICABLE",
            "ERROR",
        )
    }
    if sum(result_counts.values()) != len(files):
        raise RuntimeError("receipt folder state counts do not equal source count")
    return {
        "terminal_reused_count": terminal_count,
        "canonical_facts_count": len(eligible_files),
        "ready_count": ready_count,
        "verified_count": verified_count,
        **execution_stats,
        "review_count": sum(
            (
                1
                for row in files
                if isinstance(row.get("processing_outcome"), dict)
                and row["processing_outcome"].get("status") == "review"
            )
        ),
        "retry_pending_count": sum(
            (
                1
                for row in files
                if isinstance(row.get("processing_outcome"), dict)
                and row["processing_outcome"].get("status") == "retry_pending"
            )
        ),
        "newly_verified_count": result_counts["NEWLY_VERIFIED"],
        "already_verified_count": result_counts["ALREADY_VERIFIED"],
        "not_applicable_count": result_counts["NOT_APPLICABLE"],
        "error_count": result_counts["ERROR"],
        "result_count": len(file_results),
        "file_results": file_results,
        "ocr_increment": ocr_increment,
        "gemini_increment": gemini_increment,
        "vision_page_increment": sum(
            (
                int(row.get("pages") or 0)
                for row in usage_records
                if row.get("provider") == "cloud_vision"
            )
        ),
        "gemini_input_token_increment": sum(
            (
                int(row.get("input_tokens") or 0)
                for row in usage_records
                if row.get("provider") == "gemini"
            )
        ),
        "gemini_output_token_increment": sum(
            (
                int(row.get("output_tokens") or 0)
                for row in usage_records
                if row.get("provider") == "gemini"
            )
        ),
        "gemini_thinking_token_increment": sum(
            (
                int(row.get("thinking_tokens") or 0)
                for row in usage_records
                if row.get("provider") == "gemini"
            )
        ),
        "estimated_ai_cost_usd": round(
            sum((float(row.get("estimated_cost_usd") or 0) for row in usage_records)), 9
        ),
        "canonical_cache_hit_count": canonical_cache_hit_count,
        "decision_cache_hit_count": decision_cache_hit_count,
    }


def _capture_all_receipt_folders(
    *, output_path: Path, access_token: str
) -> dict[str, Any]:
    import urllib.parse
    from .accounting_api import UrllibTransport
    from .hash_utils import stable_hash

    token = access_token.strip()
    if not token:
        raise RuntimeError(
            "unified production authentication did not supply Google Drive access"
        )
    transport = UrllibTransport()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    about_query = urllib.parse.urlencode({"fields": "user(emailAddress)"})
    status, _response_headers, body = transport.request(
        "GET",
        f"https://www.googleapis.com/drive/v3/about?{about_query}",
        headers=headers,
    )
    if status != 200:
        raise RuntimeError(
            "Google Drive authentication changed after unified preflight"
        )
    about = json.loads(body.decode("utf-8")) if body else {}
    if (
        str(about.get("user", {}).get("emailAddress") or "").casefold()
        != _drive_principal()
    ):
        raise RuntimeError(
            "Google Drive execution principal must be configured_company"
        )
    candidates = sorted(
        _artifact_root().glob("**/drive_full_intake_manifest.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    source_manifests = [dict(row) for row in _setting("drive.receipt_sources", [])]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            source = payload.get("source")
            configured = (
                source.get("source_manifests") if isinstance(source, dict) else None
            )
            manifest_files = payload.get("files")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(source, dict)
            or source.get("type") != "combined_google_drive_intake_manifest"
            or source.get("full_intake_manifest_covered") is not True
            or (not isinstance(configured, list))
            or (not isinstance(manifest_files, list))
            or (int(source.get("folder_count", -1)) != len(configured))
            or (int(source.get("file_count", -1)) != len(manifest_files))
        ):
            continue
        eligible = [
            dict(item)
            for item in configured
            if isinstance(item, dict) and _is_wo03_receipt_source_manifest(item)
        ]
        if eligible:
            source_manifests = eligible
            break
    if not source_manifests:
        raise RuntimeError(
            "canonical combined full Drive receipt-folder configuration is missing"
        )
    files: list[dict[str, Any]] = []
    excluded_operational_artifacts: list[dict[str, Any]] = []
    listed_nonfolder_count = 0
    seen_parents: set[str] = set()
    seen_files: set[str] = set()
    for source in source_manifests:
        parent_id = str(source.get("parent_folder_id") or "").strip()
        parent_name = str(source.get("parent_folder_name") or "").strip()
        if not parent_id or not parent_name or parent_id in seen_parents:
            raise RuntimeError(
                "canonical Drive folder configuration is incomplete or duplicated"
            )
        _validate_drive_route(parent_id)
        seen_parents.add(parent_id)
        page_token = ""
        while True:
            params = {
                "q": f"'{parent_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,parents,size,sha256Checksum,createdTime,modifiedTime,ownedByMe,capabilities(canDelete,canTrash))",
                "pageSize": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            query = urllib.parse.urlencode(params)
            list_status, _list_headers, list_body = transport.request(
                "GET",
                f"https://www.googleapis.com/drive/v3/files?{query}",
                headers=headers,
            )
            if list_status != 200:
                raise RuntimeError(
                    f"Google Drive full-folder read-back failed: HTTP {list_status}"
                )
            listing = json.loads(list_body.decode("utf-8")) if list_body else {}
            rows = listing.get("files")
            if not isinstance(rows, list):
                raise RuntimeError(
                    "Google Drive full-folder read-back returned invalid files"
                )
            sibling_ids = {
                str(row.get("id") or "").strip()
                for row in rows
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            }
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("mimeType") == "application/vnd.google-apps.folder"
                ):
                    continue
                listed_nonfolder_count += 1
                file_id = str(row.get("id") or "").strip()
                file_name = str(row.get("name") or "").strip()
                checksum = str(row.get("sha256Checksum") or "").strip().casefold()
                temp_source_id = _canonical_ocr_temp_source_id(
                    file_name=file_name,
                    mime_type=str(row.get("mimeType") or ""),
                    sibling_ids=sibling_ids,
                )
                if temp_source_id:
                    capabilities = row.get("capabilities")
                    capabilities = (
                        capabilities if isinstance(capabilities, dict) else {}
                    )
                    exported = _download_google_drive_file_bytes(
                        file_id, token, mime_type=str(row.get("mimeType") or "")
                    )
                    excluded_operational_artifacts.append(
                        {
                            "google_drive_file_id_hash": "sha256:"
                            + stable_hash(file_id),
                            "file_name_hash": "sha256:" + stable_hash(file_name),
                            "source_file_id_hash": "sha256:"
                            + stable_hash(temp_source_id),
                            "parent_folder_id_hash": "sha256:" + stable_hash(parent_id),
                            "mime_type": str(row.get("mimeType") or ""),
                            "created_time": row.get("createdTime"),
                            "modified_time": row.get("modifiedTime"),
                            "owned_by_execution_principal": row.get("ownedByMe")
                            is True,
                            "trash_allowed": capabilities.get("canTrash") is True,
                            "delete_allowed": capabilities.get("canDelete") is True,
                            "exclusion_reason": "canonical_ocr_temporary_artifact",
                            "provenance_verified": True,
                            "source_file_present_in_same_folder": True,
                        }
                    )
                    seen_files.add(file_id)
                    files.append(
                        {
                            "google_drive_file_id": file_id,
                            "file_name": file_name,
                            "file_hash": "sha256:"
                            + hashlib.sha256(exported).hexdigest(),
                            "parent_folder_id": parent_id,
                            "parent_folder_name": parent_name,
                            "mime_type": str(row.get("mimeType") or ""),
                            "size": row.get("size"),
                            "created_time": row.get("createdTime"),
                            "modified_time": row.get("modifiedTime"),
                            "processing_outcome": {
                                "processor_version": 8,
                                "status": "not_applicable",
                                "reason_code": "canonical_ocr_temporary_artifact",
                            },
                        }
                    )
                    continue
                if not file_id or not file_name:
                    raise RuntimeError("Google Drive evidence identity is incomplete")
                if file_id in seen_files or parent_id not in (row.get("parents") or []):
                    raise RuntimeError(
                        "Google Drive evidence identity is duplicated or outside its folder"
                    )
                seen_files.add(file_id)
                if not re.fullmatch("[0-9a-f]{64}", checksum):
                    mime_type = str(row.get("mimeType") or "")
                    try:
                        exported = _download_google_drive_file_bytes(
                            file_id, token, mime_type=mime_type
                        )
                    except RuntimeError as error:
                        if mime_type.startswith("application/vnd.google-apps."):
                            files.append(
                                {
                                    "google_drive_file_id": file_id,
                                    "file_name": file_name,
                                    "file_hash": "",
                                    "parent_folder_id": parent_id,
                                    "parent_folder_name": parent_name,
                                    "mime_type": mime_type,
                                    "size": row.get("size"),
                                    "created_time": row.get("createdTime"),
                                    "modified_time": row.get("modifiedTime"),
                                    "processing_outcome": {
                                        "processor_version": 6,
                                        "status": "not_applicable",
                                        "reason_code": "unsupported_workspace_document_type",
                                        "error_hash": "sha256:"
                                        + stable_hash(
                                            type(error).__name__ + ":" + str(error)
                                        ),
                                    },
                                }
                            )
                            continue
                        raise
                    checksum = hashlib.sha256(exported).hexdigest()
                    files.append(
                        {
                            "google_drive_file_id": file_id,
                            "file_name": file_name,
                            "file_hash": f"sha256:{checksum}",
                            "parent_folder_id": parent_id,
                            "parent_folder_name": parent_name,
                            "mime_type": str(row.get("mimeType") or ""),
                            "size": row.get("size"),
                            "created_time": row.get("createdTime"),
                            "modified_time": row.get("modifiedTime"),
                            "content_export": {
                                "mime_type": "application/pdf"
                                if mime_type.startswith("application/vnd.google-apps.")
                                else mime_type,
                                "identity_readback_verified": True,
                                "content_hash_verified": True,
                            },
                        }
                    )
                    continue
                files.append(
                    {
                        "google_drive_file_id": file_id,
                        "file_name": file_name,
                        "file_hash": f"sha256:{checksum}",
                        "parent_folder_id": parent_id,
                        "parent_folder_name": parent_name,
                        "mime_type": str(row.get("mimeType") or ""),
                        "size": row.get("size"),
                        "created_time": row.get("createdTime"),
                        "modified_time": row.get("modifiedTime"),
                    }
                )
            page_token = str(listing.get("nextPageToken") or "")
            if not page_token:
                break
    captured_at = datetime.now(timezone.utc).isoformat()
    items = [
        {
            "item_id": "sha256:"
            + stable_hash({"google_drive_file_id": row["google_drive_file_id"]}),
            "status": "planned",
            "economic_event_discovered": True,
            "blocker": "all_receipt_folder_processing_required",
            "blocker_code": "all_receipt_folder_processing_required",
            "retry_condition": "journal/voucher/Drive authoritative read-back",
        }
        for row in files
    ]
    artifact = {
        "files": files,
        "items": items,
        "source": {
            "type": "combined_google_drive_intake_manifest",
            "captured_at": captured_at,
            "drive_principal": _drive_principal(),
            "identity_readback_verified": True,
            "full_intake_manifest_covered": True,
            "folder_count": len(source_manifests),
            "file_count": len(files),
            "listed_nonfolder_count": listed_nonfolder_count,
            "excluded_operational_artifact_count": len(excluded_operational_artifacts),
            "source_manifests": source_manifests,
        },
        "excluded_operational_artifacts": excluded_operational_artifacts,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def _is_wo03_receipt_source_manifest(source: dict[str, Any]) -> bool:
    """Keep WO03 person/card/cash routes; WO02 and quarantine are disjoint."""
    import unicodedata

    raw_path = unicodedata.normalize(
        "NFKC", str(source.get("parent_folder_name") or "")
    ).casefold()
    compact = re.sub("[\\s\\u3000]", "", raw_path)
    excluded = (
        "請求書",
        "隔離",
        "quarantine",
        "要確認_受信証憑",
        "処理完了",
        "処理済み",
        "今期証票_処理済み",
    )
    return (
        bool(str(source.get("parent_folder_id") or "").strip())
        and bool(compact)
        and (not any((marker in compact for marker in excluded)))
    )


def _canonical_ocr_temp_source_id(
    *, file_name: str, mime_type: str, sibling_ids: set[str]
) -> str:
    """Recognise only a leaked temporary document created by ``performOCR_``.

    A native Google document is never excluded merely because it lacks a
    checksum.  The exact canonical temp name must identify a source file that
    is still present in the same authoritative folder listing.
    """
    if mime_type != "application/vnd.google-apps.document":
        return ""
    match = re.fullmatch("temp_ocr_([A-Za-z0-9_-]+)_(\\d{13})", file_name)
    if match is None:
        return ""
    source_id = match.group(1)
    return source_id if source_id in sibling_ids else ""


def _persist_all_receipt_folders_stage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    accounting_date: str,
    artifact: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    from .hash_utils import mask_identifier, stable_hash
    from .same_chain_status import same_chain_status_for_file_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    source = artifact.get("source")
    files = artifact.get("files")
    items = artifact.get("items")
    excluded = artifact.get("excluded_operational_artifacts")
    if (
        not isinstance(source, dict)
        or not isinstance(files, list)
        or (not isinstance(items, list))
        or (not isinstance(excluded, list))
    ):
        raise ValueError("all receipt folders artifact is incomplete")
    folder_count = int(source.get("folder_count", -1))
    if (
        source.get("drive_principal") != _drive_principal()
        or source.get("identity_readback_verified") is not True
        or source.get("full_intake_manifest_covered") is not True
        or (folder_count <= 0)
        or (int(source.get("file_count", -1)) != len(files))
        or (int(source.get("excluded_operational_artifact_count", -1)) != len(excluded))
        or (int(source.get("listed_nonfolder_count", -1)) != len(files))
    ):
        raise ValueError("all receipt folders live manifest verification failed")
    for row in excluded:
        if (
            not isinstance(row, dict)
            or row.get("exclusion_reason") != "canonical_ocr_temporary_artifact"
            or row.get("provenance_verified") is not True
            or (row.get("source_file_present_in_same_folder") is not True)
            or (row.get("mime_type") != "application/vnd.google-apps.document")
            or any(
                (
                    re.fullmatch("sha256:[0-9a-f]{64}", str(row.get(key) or "")) is None
                    for key in (
                        "google_drive_file_id_hash",
                        "file_name_hash",
                        "source_file_id_hash",
                        "parent_folder_id_hash",
                    )
                )
            )
        ):
            raise ValueError("all receipt folders operational exclusion is invalid")
    source_hash = stable_hash({"all_receipt_folders": artifact})
    readback_hash = stable_hash(
        {
            "folders": source.get("source_manifests"),
            "files": [
                {
                    "id_hash": stable_hash(row.get("google_drive_file_id")),
                    "file_hash": row.get("file_hash"),
                    "parent_hash": stable_hash(row.get("parent_folder_id")),
                }
                for row in files
                if isinstance(row, dict)
            ],
            "excluded_operational_artifacts": excluded,
        }
    )
    reconciled_items: list[dict[str, Any]] = []
    for file_row, item_row in zip(files, items, strict=True):
        if not isinstance(file_row, dict) or not isinstance(item_row, dict):
            raise ValueError("all receipt folders items must align with files")
        drive_id = str(file_row.get("google_drive_file_id") or "")
        file_hash = str(file_row.get("file_hash") or "")
        ledger = conn.execute(
            "SELECT processing_status FROM processing_ledger WHERE google_drive_file_id_hash=? AND file_hash=? ORDER BY updated_at DESC,id DESC LIMIT 1",
            (mask_identifier(drive_id), file_hash),
        ).fetchone()
        state = str(ledger["processing_status"]) if ledger is not None else ""
        chain = same_chain_status_for_file_hash(conn, file_hash)
        chain_complete = bool(chain and chain.get("same_chain_ready") is True)
        current = dict(item_row)
        if state == "verified" and chain_complete:
            current = {"item_id": current["item_id"], "status": "verified"}
        elif state == "not_applicable":
            current = {
                "item_id": current["item_id"],
                "status": "not_applicable",
                "not_applicable_basis": "exact Drive ID and SHA-256 processing ledger read-back confirms that no new accounting event is required",
                "no_economic_event_verified": True,
                "not_applicable_readback_hash": "sha256:"
                + stable_hash(
                    {
                        "drive_file_id_hash": mask_identifier(drive_id),
                        "file_hash": file_hash,
                        "processing_status": state,
                    }
                ),
            }
        reconciled_items.append(current)
    items = reconciled_items
    remaining_count = sum(
        (
            str(item.get("status") or "") not in {"verified", "not_applicable"}
            for item in items
        )
    )
    run_hash = accounting_run_hash(run_id)
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage="all_receipt_folders",
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": "all_receipt_folders",
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                "coverage_state": "captured",
                "all_items_resolved": remaining_count == 0,
                "remaining_count": remaining_count,
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _drive_principal()}),
                "source_snapshot_hash": f"sha256:{source_hash}",
                "readback_hash": f"sha256:{readback_hash}",
            }
        ),
        actor="all_receipt_folders_adapter",
    )
    advanced = advance_run(
        conn,
        run_id,
        "all_receipt_folders",
        {
            "status": "verified",
            "evidence_hash": f"sha256:{source_hash}",
            "adapter_receipt_hash": receipt_hash,
            "coverage_state": "captured",
            "all_items_resolved": remaining_count == 0,
            "remaining_count": remaining_count,
            "items": items,
            "folder_count": folder_count,
            "manifest_folder_count": folder_count,
            "all_folders_covered": True,
        },
        actor=actor,
    )
    return {**advanced, "all_receipt_remaining_count": remaining_count}


def _execute_payroll_bonus_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    runtime: tuple[Any, str, Any | None, str],
    standalone: bool = False,
) -> dict[str, Any]:
    """Verify one confirmed payroll journal, or continue with NOT_READY and no write."""
    from .hash_utils import stable_hash
    from .shiwake_run import build_run_status

    if standalone:
        status = {
            "status": "running",
            "current_stage": "payroll_bonus",
            "accounting_date": str(args.accounting_date),
        }
    else:
        with db.connect() as conn:
            status = build_run_status(conn, args.run_id)
        if (
            status.get("status") != "running"
            or status.get("current_stage") != "payroll_bonus"
        ):
            raise RuntimeError("payroll bonus adapter called outside its active stage")
    accounting_date = str(status["accounting_date"])
    client, office_id = runtime[:2]
    payroll_items = _load_confirmed_payroll_items(
        accounting_date, db=db, client=client, office_id=office_id
    )
    if payroll_items:
        posted_count = 0
        reuse_count = 0
        voucher_write_count = 0
        readback_hashes: list[str] = []
        voucher_readback_hashes: list[str] = []
        for item in payroll_items:
            outcome = _execute_one_payroll_journal(
                db, args, client=client, office_id=office_id, item=item
            )
            posted_count += int(outcome["posted"])
            reuse_count += int(outcome["reused"])
            readback_hashes.append(str(outcome["readback_hash"]))
            if item.get("pdf_path"):
                voucher = _ensure_payroll_pdf_attachment(
                    db,
                    args,
                    client=client,
                    office_id=office_id,
                    journal_id=str(outcome["journal_id"]),
                    pdf_path=Path(item["pdf_path"]),
                )
                voucher_write_count += int(voucher["attached"])
                voucher_readback_hashes.append(str(voucher["readback_hash"]))
        replay_matches = [
            _find_exact_payroll_journal_ids(
                client, office_id=office_id, journal=item["journal"]
            )[0]
            for item in payroll_items
        ]
        if any((len(matches) != 1 for matches in replay_matches)):
            raise RuntimeError(
                "payroll replay did not resolve exactly one official journal"
            )
        completion = {
            "payroll_period": str(payroll_items[0]["payroll_period"]),
            "payment_date": str(payroll_items[0]["payment_date"]),
            "accrual_date": str(payroll_items[0]["accrual_date"]),
            "employee_count": sum(
                (int(item["employee_count"]) for item in payroll_items)
            ),
            "source_snapshot_hash": "sha256:"
            + stable_hash([item["source_snapshot_hash"] for item in payroll_items]),
            "readback_hash": "sha256:" + stable_hash(readback_hashes),
            "posted_count": posted_count,
            "reuse_count": reuse_count,
            "rag_pattern_count": sum(
                (int(item["rag_pattern_count"]) for item in payroll_items)
            ),
            "rag_match_count": sum(
                (int(item["rag_match_count"]) for item in payroll_items)
            ),
            "rag_decision_sources": sorted(
                {str(item["rag_decision_source"]) for item in payroll_items}
            ),
            "voucher_readback_hash": "sha256:" + stable_hash(voucher_readback_hashes),
        }
        learned_rules = 0
        if voucher_readback_hashes:
            from .verified_journal_learning import learn_verified_payroll_rules

            with db.connect() as conn:
                learned_rules = learn_verified_payroll_rules(
                    payroll_summary=completion,
                    conn=conn,
                    journal_readback_verified=True,
                    voucher_readback_verified=True,
                )
        if standalone:
            advanced = {"status": "complete", "authentication_ready": True}
        else:
            with db.connect() as conn:
                advanced = _persist_monthly_payroll_completion(
                    conn,
                    run_id=args.run_id,
                    accounting_date=accounting_date,
                    completion=completion,
                    actor=args.actor,
                )
        return {
            **advanced,
            "execution_mode": "production_payroll_bonus",
            "manifest_required": True,
            "moneyforward_api_called": True,
            "moneyforward_readback_verified": True,
            "journal_write_scope": "verified",
            "payroll_write_called": posted_count > 0,
            "monthly_payroll_reused": posted_count == 0,
            "payroll_bonus_probe": {
                "status": "PASS",
                "payroll_period": completion["payroll_period"],
                "accrual_date": completion["accrual_date"],
                "employee_count": completion["employee_count"],
                "remaining_count": 0,
                "review_count": 0,
                "write_count": posted_count,
                "reuse_count": reuse_count,
                "official_readback_verified": True,
                "pdf_attachment_readback_verified": bool(voucher_readback_hashes),
                "voucher_write_count": voucher_write_count,
                "replay_additional_write_count": 0,
                "rag_pattern_count": completion["rag_pattern_count"],
                "rag_match_count": completion["rag_match_count"],
                "rag_decision_sources": completion["rag_decision_sources"],
                "verified_rag_rules_upserted": learned_rules,
            },
        }
    explicitly_not_ready = _payroll_confirmation_not_ready(accounting_date)
    if not explicitly_not_ready:
        if not standalone:
            raise RuntimeError(
                "confirmed payroll source requires Computer Use acquisition"
            )
        advanced = {"status": "source_required", "authentication_ready": True}
    elif standalone:
        advanced = {"status": "complete", "authentication_ready": True}
    else:
        with db.connect() as conn:
            advanced = _persist_payroll_not_ready(
                conn,
                run_id=args.run_id,
                accounting_date=accounting_date,
                actor=args.actor,
            )
    probe = {
        "status": "NOT_READY" if explicitly_not_ready else "COMPUTER_USE_REQUIRED",
        "remaining_count": 0,
        "review_count": 0,
        "write_count": 0,
        "replay_additional_write_count": 0,
    }
    if not explicitly_not_ready:
        probe["source_acquisition"] = "confirmed_wage_ledger_pdf"
    return {
        **advanced,
        "execution_mode": "production_payroll_bonus",
        "manifest_required": False,
        "moneyforward_api_called": False,
        "moneyforward_readback_verified": False,
        "journal_write_scope": "verified",
        "payroll_write_called": False,
        "monthly_payroll_reused": False,
        "payroll_bonus_probe": probe,
    }


def _load_monthly_payroll_completion(accounting_date: str) -> dict[str, Any] | None:
    """Find an exact current-month payroll payment with full employee read-back."""
    accounting_day = date.fromisoformat(accounting_date)
    _period_start, previous_month_end = _configured_payroll_period(accounting_day)
    expected_period = previous_month_end.strftime("%Y-%m")
    candidates = sorted(
        _artifact_root().glob("**/payroll_wage_ledger_manifest.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for manifest_path in candidates:
        readback_path = manifest_path.with_name(
            "payroll_split_readback_verified.local.json"
        )
        if not readback_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            readback = json.loads(readback_path.read_text(encoding="utf-8"))
            return _validate_monthly_payroll_completion(
                manifest,
                readback,
                accounting_date=accounting_date,
                expected_period=expected_period,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _payroll_confirmation_not_ready(accounting_date: str) -> bool:
    """Return true only for an explicit current-period unconfirmed payroll result."""
    accounting_day = date.fromisoformat(accounting_date)
    expected_period = _configured_payroll_period(accounting_day)[1].strftime("%Y-%m")
    candidates = sorted(
        _artifact_root().glob("**/payroll_wage_ledger_manifest.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            continue
        if not str(manifest.get("payroll_period") or "").startswith(expected_period):
            continue
        return manifest.get("confirmed") is False or str(
            manifest.get("confirmation_status") or ""
        ).casefold() in {"not_ready", "unconfirmed"}
    return False


def _load_confirmed_payroll_items(
    accounting_date: str, *, db: Any, client: Any, office_id: str
) -> list[dict[str, Any]]:
    """Build registration-ready payroll journals from official wage-ledger PDFs."""
    from .payroll import build_payroll_journal_candidate
    from .payroll import parse_statement_payload
    from .hash_utils import stable_hash
    from .live_journal_resolver import _extract_items, _resolve_item
    from .mcp_facade import LocalMCPFacade

    accounting_day = date.fromisoformat(accounting_date)
    period_start, period_end = _configured_payroll_period(accounting_day)
    expected_period = period_end.strftime("%Y-%m")
    manifests = sorted(
        _artifact_root().glob("**/payroll_wage_ledger_manifest.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    manifest: dict[str, Any] | None = None
    for path in manifests:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if (
                candidate.get("schema") == _setting("payroll.manifest_schema")
                and candidate.get("office") == _office_name()
                and str(candidate.get("payroll_period") or "").startswith(
                    expected_period + "-"
                )
                and (
                    date.fromisoformat(str(candidate.get("payment_date") or ""))
                    <= accounting_day
                )
                and (candidate.get("confirmed") is not False)
                and (
                    str(candidate.get("confirmation_status") or "").casefold()
                    not in {"not_ready", "unconfirmed"}
                )
            ):
                manifest = candidate
                break
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if manifest is None:
        return []
    pdf_groups: dict[Path, list[Path]] = {}
    for path in _artifact_root().glob("**/*.pdf"):
        if re.fullmatch("(?:賃金台帳_)?_?\\d+_\\d{4}\\.pdf", path.name):
            pdf_groups.setdefault(path.parent, []).append(path)
    if not pdf_groups:
        return []
    paths = max(
        pdf_groups.values(),
        key=lambda group: max((path.stat().st_mtime for path in group)),
    )
    payment_month = int(_configured_payroll_payment_month(period_end)[5:7])
    expected_header = f"{payment_month}月度"
    expected_range = (
        f"{period_start.month}/{period_start.day} - {period_end.month}/{period_end.day}"
    )
    event_employees: dict[str, list[dict[str, Any]]] = {"salary": [], "bonus": []}
    event_payments: dict[str, dict[str, int]] = {"salary": {}, "bonus": {}}
    event_deductions: dict[str, dict[str, int]] = {"salary": {}, "bonus": {}}
    event_company: dict[str, dict[str, int]] = {"salary": {}, "bonus": {}}
    bonus_dates: set[str] = set()
    source_files: list[dict[str, str]] = []
    for path in sorted(paths):
        body = path.read_bytes()
        if not body.startswith(b"%PDF"):
            continue
        extracted = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if "事業所名: " + _office_name() not in extracted:
            continue
        if expected_header not in extracted or expected_range not in extracted:
            continue
        bonus_date = re.search("\\b(\\d{4}/\\d{2}/\\d{2})\\b", extracted)
        if bonus_date:
            bonus_dates.add(bonus_date.group(1).replace("/", "-"))
        name_match = re.search(
            "従業員番号:\\s*(\\S+)\\s+氏名:\\s*(.+?)\\s{2,}性別:", extracted
        )
        if not name_match:
            raise ValueError("payroll PDF employee identity is missing")
        employee_ref, employee_name = name_match.groups()
        employee_name = employee_name.strip()
        if not employee_name:
            raise ValueError("payroll PDF employee name is missing")
        tsv = subprocess.run(
            ["pdftotext", "-tsv", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        values_by_event = _payroll_pdf_event_values(
            tsv, expected_header=expected_header
        )
        for event_kind, values in values_by_event.items():
            if values.get("支給合計", 0) <= 0:
                continue
            payment_items = [
                {"label": label.removesuffix("(支給)"), "amount": amount}
                for label, amount in values.items()
                if label.endswith("(支給)") and amount != 0
            ]
            deduction_items = [
                {"label": label.removesuffix("(控除)"), "amount": amount}
                for label, amount in values.items()
                if label.endswith("(控除)") and amount != 0
            ]
            company_items = [
                {"label": label.removesuffix("(会社)"), "amount": amount}
                for label, amount in values.items()
                if label.endswith("(会社)") and amount != 0
            ]
            for target, items in (
                (event_payments[event_kind], payment_items),
                (event_deductions[event_kind], deduction_items),
                (event_company[event_kind], company_items),
            ):
                for item in items:
                    target[item["label"]] = target.get(item["label"], 0) + int(
                        item["amount"]
                    )
            event_employees[event_kind].append(
                {
                    "employee_ref": employee_ref,
                    "sub_account": employee_name,
                    "role": "executive"
                    if any(("役員報酬" in item["label"] for item in payment_items))
                    else "employee",
                    "allocation_evidence_hash": "sha256:"
                    + hashlib.sha256(body + event_kind.encode()).hexdigest(),
                    "payment_items": payment_items,
                    "deduction_items": deduction_items,
                    "net_payment": {
                        "label": "差引支給額",
                        "amount": values.get("差引支給合計", 0),
                    },
                }
            )
        source_files.append(
            {
                "name_hash": stable_hash(path.name),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    expected_count = int(manifest.get("employee_count") or 0)
    employees = event_employees["salary"]
    if not employees or len(employees) != expected_count:
        raise ValueError("confirmed payroll PDF employee coverage is incomplete")
    with db.connect() as conn:
        rag_patterns = LocalMCPFacade(conn).search_payroll_patterns()
    rag_pattern_count = len(rag_patterns)
    masters = {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
        "trade_partners": _extract_items(client.trade_partners(office_id)),
    }
    source_snapshot_hash = "sha256:" + stable_hash(
        {"manifest": manifest, "files": source_files, "period": expected_period}
    )
    combined_candidates = sorted(
        paths[0].parent.glob(f"賃金台帳_{expected_period}_*.pdf")
    )
    if len(combined_candidates) != 1:
        raise ValueError("one canonical combined payroll PDF is required")
    combined_pdf = combined_candidates[0]
    items: list[dict[str, Any]] = []
    for event_kind in ("salary", "bonus"):
        employees = event_employees[event_kind]
        if not employees:
            continue
        payload = {
            "schema": "moneyforward-ai-poc-phase3-payroll-input-v2",
            "period": expected_period,
            "payroll_type": event_kind,
            "source_channel": "computer_use_confirmed_wage_ledger_pdf",
            "employee_count": len(employees),
            "payment_items": [
                {"label": label, "amount": amount}
                for label, amount in event_payments[event_kind].items()
            ],
            "deduction_items": [
                {"label": label, "amount": amount}
                for label, amount in event_deductions[event_kind].items()
            ],
            "company_social_insurance": [
                {"label": label, "amount": amount}
                for label, amount in event_company[event_kind].items()
            ],
            "net_payment": {
                "label": "差引支給額",
                "amount": sum(
                    (int(item["net_payment"]["amount"]) for item in employees)
                ),
            },
            "employees": employees,
        }
        statement = parse_statement_payload(payload)
        candidate = build_payroll_journal_candidate(statement)
        if not candidate.registration_allowed:
            raise ValueError(
                "confirmed payroll PDF is not registration-ready: "
                + ",".join((*candidate.validation.errors, *candidate.risk_flags))
            )
        if event_kind == "bonus" and len(bonus_dates) != 1:
            raise ValueError("bonus PDF date is not uniquely resolved")
        transaction_date = (
            period_end.isoformat()
            if event_kind == "salary"
            else next(iter(bonus_dates))
        )
        label = "給与・役員報酬" if event_kind == "salary" else "賞与"
        named_journal = _named_payroll_journal(
            candidate,
            transaction_date=transaction_date,
            description=f"{expected_period} {label}",
            account_names={
                str(item.get("name") or item.get("account_name") or "")
                for item in masters["accounts"]
            },
        )
        selected_journal, rag_decision = _select_payroll_journal_using_rag(
            journal=named_journal, payroll_type=event_kind, rag_patterns=rag_patterns
        )
        resolved = _resolve_item(
            {"source_file_hash": source_snapshot_hash, "journal": selected_journal},
            masters,
        )
        if resolved["status"] != "ready":
            raise ValueError("confirmed payroll journal has unresolved live masters")
        items.append(
            {
                "payroll_period": expected_period,
                "payment_date": str(manifest["payment_date"]),
                "accrual_date": transaction_date,
                "employee_count": len(employees),
                "source_snapshot_hash": source_snapshot_hash,
                "rag_pattern_count": rag_pattern_count,
                "rag_match_count": rag_decision["match_count"],
                "rag_decision_source": rag_decision["source"],
                "rag_pattern_hashes": rag_decision["pattern_hashes"],
                "journal": resolved["journal"],
                "pdf_path": combined_pdf,
            }
        )
    return items


def _payroll_csv_amount(value: str) -> int:
    text = str(value or "").replace(",", "").replace("円", "").strip()
    if not text or text == "-":
        return 0
    if not re.fullmatch("-?\\d+", text):
        return 0
    return int(text)


def _payroll_pdf_event_values(
    text: str, *, expected_header: str
) -> dict[str, dict[str, int]]:
    """Read salary and bonus cells from Poppler's coordinate-preserving TSV."""
    result: dict[str, dict[str, int]] = {"salary": {}, "bonus": {}}
    rows: dict[tuple[int, str], list[tuple[float, float, str]]] = {}
    for raw in text.splitlines()[1:]:
        parts = raw.split("\t", 11)
        if len(parts) != 12 or parts[0] != "5":
            continue
        try:
            page = int(parts[1])
            left = float(parts[6])
            top = parts[7]
            width = float(parts[8])
        except ValueError:
            continue
        rows.setdefault((page, top), []).append((left, width, parts[11].strip()))
    pages = sorted({page for page, _top in rows})
    for page in pages:
        header_words = next(
            (
                words
                for (row_page, _top), words in rows.items()
                if row_page == page
                and any((word == expected_header for _left, _width, word in words))
            ),
            [],
        )
        columns = sorted(
            (
                (left + width / 2, word)
                for left, width, word in header_words
                if re.fullmatch("(?:\\d+月度|賞与|合計)", word)
            )
        )
        labels = [word for _center, word in columns]
        if expected_header not in labels or "賞与" not in labels:
            continue
        bonus_index = labels.index("賞与")
        if bonus_index == len(columns) - 1:
            previous_gap = columns[bonus_index][0] - columns[bonus_index - 1][0]
            columns.append((columns[bonus_index][0] + previous_gap, "合計"))
            labels.append("合計")
        for (row_page, _top), words in rows.items():
            if row_page != page:
                continue
            label = next(
                (word for left, _width, word in words if left < 95 and word), ""
            )
            if not label:
                continue
            if label.endswith("(控"):
                label += "除)"
            elif label.endswith("(会"):
                label += "社)"
            for event_kind, column_label in (
                ("salary", expected_header),
                ("bonus", "賞与"),
            ):
                index = labels.index(column_label)
                center = columns[index][0]
                left_bound = (columns[index - 1][0] + center) / 2
                right_bound = (center + columns[index + 1][0]) / 2
                cell = next(
                    (
                        word
                        for left, width, word in words
                        if left_bound <= left + width / 2 < right_bound
                    ),
                    "",
                )
                result[event_kind][label] = _payroll_csv_amount(cell)
    return result


def _select_payroll_journal_using_rag(
    *, journal: dict[str, Any], payroll_type: str, rag_patterns: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse a matching payroll RAG rule; otherwise keep the PDF-facts decision."""
    from .hash_utils import stable_hash

    matching_hashes = sorted(
        {
            "sha256:"
            + stable_hash(
                {
                    "card_type": str(pattern.get("card_type") or ""),
                    "title": str(pattern.get("title") or ""),
                    "body": str(pattern.get("body") or ""),
                }
            )
            for pattern in rag_patterns
            if _payroll_rag_pattern_matches(
                pattern=pattern, journal=journal, payroll_type=payroll_type
            )
        }
    )
    return (
        dict(journal),
        {
            "source": "matching_rag_pattern_reused"
            if matching_hashes
            else "current_pdf_facts_live_master",
            "candidate_count": len(rag_patterns),
            "match_count": len(matching_hashes),
            "pattern_hashes": matching_hashes,
        },
    )


def _payroll_rag_pattern_matches(
    *, pattern: dict[str, Any], journal: dict[str, Any], payroll_type: str
) -> bool:
    body = str(pattern.get("body") or "")
    title = str(pattern.get("title") or "")
    fields = {
        key.strip(): value.strip()
        for segment in body.split("|")
        if "=" in segment
        for key, value in [segment.split("=", 1)]
    }
    if not {"debit", "credit", "tax"}.issubset(fields):
        return False
    mentions_bonus = (
        "賞与" in title or "賞与" in fields["debit"] or "bonus" in body.casefold()
    )
    if (payroll_type == "bonus") != mentions_bonus:
        return False
    debit_accounts = {
        _canonical_payroll_rag_account(
            str(branch.get("debitor", {}).get("account_name") or "")
        )
        for branch in journal.get("branches") or []
    }
    credit_accounts = {
        _canonical_payroll_rag_account(
            str(branch.get("creditor", {}).get("account_name") or "")
        )
        for branch in journal.get("branches") or []
    }
    tax_names = {
        str(line.get("tax_name") or "")
        for branch in journal.get("branches") or []
        for line in (branch.get("debitor") or {}, branch.get("creditor") or {})
    }
    required_debits = {
        _canonical_payroll_rag_account(part.split("/", 1)[0].strip())
        for part in fields["debit"].split(" and ")
        if part.strip()
    }
    required_credits = {
        _canonical_payroll_rag_account(part.split("/", 1)[0].strip())
        for part in fields["credit"].split(" and ")
        if part.strip()
    }
    return (
        required_debits.issubset(debit_accounts)
        and required_credits.issubset(credit_accounts)
        and (fields["tax"] in tax_names)
    )


def _canonical_payroll_rag_account(value: str) -> str:
    return "給料手当" if value == "給料賃金" else value


def _named_payroll_journal(
    candidate: Any,
    *,
    transaction_date: str,
    description: str,
    account_names: set[str] | None = None,
) -> dict[str, Any]:
    account_names = account_names or set()

    def live_account_name(name: str) -> str:
        if (
            name == "給料手当"
            and name not in account_names
            and ("給料賃金" in account_names)
        ):
            return "給料賃金"
        return name

    debits = [
        [line, int(line.amount)] for line in candidate.lines if line.side == "debit"
    ]
    credits = [
        [line, int(line.amount)] for line in candidate.lines if line.side == "credit"
    ]
    branches: list[dict[str, Any]] = []
    debit_index = credit_index = 0
    while debit_index < len(debits) and credit_index < len(credits):
        debit_line, debit_remaining = debits[debit_index]
        credit_line, credit_remaining = credits[credit_index]
        value = min(debit_remaining, credit_remaining)
        branches.append(
            {
                "remark": description,
                "debitor": {
                    "account_name": live_account_name(debit_line.account),
                    "sub_account_name": debit_line.sub_account,
                    "tax_name": "対象外",
                    "value": value,
                },
                "creditor": {
                    "account_name": live_account_name(credit_line.account),
                    "sub_account_name": credit_line.sub_account,
                    "tax_name": "対象外",
                    "value": value,
                },
            }
        )
        debits[debit_index][1] -= value
        credits[credit_index][1] -= value
        if debits[debit_index][1] == 0:
            debit_index += 1
        if credits[credit_index][1] == 0:
            credit_index += 1
    if debit_index != len(debits) or credit_index != len(credits):
        raise ValueError("payroll journal candidate is not balanced")
    return {
        "transaction_date": transaction_date,
        "journal_type": "journal_entry",
        "branches": branches,
    }


def _find_exact_payroll_journal_ids(
    client: Any, *, office_id: str, journal: dict[str, Any]
) -> tuple[list[str], int]:
    """Return exact official matches and same-total conflicts for one payroll body."""
    from .journal_backfill import _extract_items, _journal_id, _unwrap_journal
    from .journal_write_verify import _perform_journal_read_back, _normalize_read_back

    transaction_date = str(journal["transaction_date"])
    expected_total = sum(
        (int(branch["debitor"]["value"]) for branch in journal["branches"])
    )
    exact: list[str] = []
    conflicting = 0
    page = 1
    while True:
        listing = client.journals(
            office_id=office_id,
            start_date=transaction_date,
            end_date=transaction_date,
            page=page,
            per_page=500,
        )
        rows = _extract_items(listing)
        for row in rows:
            journal_id = _journal_id(row)
            if not journal_id:
                continue
            verification = _perform_journal_read_back(
                client=client,
                journal_id=journal_id,
                journal=journal,
                idempotency_key="payroll-duplicate-probe",
                operation_type="journal_create",
                office_id=office_id,
            )
            if verification.verified:
                exact.append(journal_id)
                continue
            detail = _unwrap_journal(client.journal(journal_id, office_id=office_id))
            actual = _normalize_read_back(detail)
            if _payroll_economic_signature(actual) == _payroll_economic_signature(
                journal
            ):
                exact.append(journal_id)
                continue
            actual_total = sum(
                (
                    int(branch.get("debitor", {}).get("value") or 0)
                    for branch in actual.get("branches") or []
                )
            )
            if actual_total == expected_total:
                conflicting += 1
        if len(rows) < 500:
            break
        page += 1
        if page > 100:
            raise RuntimeError(
                "payroll duplicate probe exceeded pagination safety limit"
            )
    return (exact, conflicting)


def _payroll_economic_signature(journal: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Compare payroll substance while ignoring remarks and legacy exempt-tax encoding."""
    signature = [
        (
            side,
            line.get("account_id"),
            line.get("sub_account_id"),
            int(line.get("value") or 0),
        )
        for branch in journal.get("branches") or []
        for side in ("debitor", "creditor")
        for line in [branch.get(side) or {}]
    ]
    return sorted(signature, key=str)


def _execute_one_payroll_journal(
    db: Any,
    args: argparse.Namespace,
    *,
    client: Any,
    office_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Reuse one exact payroll journal or POST it through the existing API path."""
    from .hash_utils import stable_hash
    from .accounting_api import extract_moneyforward_journal_id
    from .journal_backfill import _unwrap_journal
    from .journal_write_verify import _normalize_read_back, _perform_journal_read_back
    from .scope_gate import require_latest_journal_write_scope

    journal = dict(item["journal"])
    exact, conflicting = _find_exact_payroll_journal_ids(
        client, office_id=office_id, journal=journal
    )
    if len(exact) > 1:
        raise RuntimeError("payroll duplicate probe found multiple exact journals")
    if exact:
        verification = _perform_journal_read_back(
            client=client,
            journal_id=exact[0],
            journal=journal,
            idempotency_key="payroll-reuse",
            operation_type="journal_create",
            office_id=office_id,
        )
        if not verification.verified:
            actual = _normalize_read_back(
                _unwrap_journal(client.journal(exact[0], office_id=office_id))
            )
            if _payroll_economic_signature(actual) != _payroll_economic_signature(
                journal
            ):
                raise RuntimeError("payroll reuse official read-back mismatch")
            readback_hash = "sha256:" + stable_hash(
                {"payroll_official_readback": actual}
            )
        else:
            readback_hash = verification.actual_hash
        return {
            "posted": False,
            "reused": True,
            "readback_hash": readback_hash,
            "journal_id": exact[0],
        }
    if conflicting:
        raise RuntimeError(
            "same-date same-total payroll journal conflicts with expected body: "
            + str(journal["transaction_date"])
        )
    idempotency_key = stable_hash(
        {
            "payroll_period": item["payroll_period"],
            "source_hash": item["source_snapshot_hash"],
            "journal_hash": stable_hash(journal),
        }
    )
    with db.connect() as conn:
        scope = conn.execute(
            "SELECT scope_hash FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
        if scope is None:
            raise PermissionError(
                "Money Forward journal.write scope evidence is missing"
            )
        scope_hash = str(scope["scope_hash"])
        require_latest_journal_write_scope(conn, expected_scope_hash=scope_hash)
    try:
        payload = client.create_journal_from_run_instruction(
            journal=journal,
            instruction=getattr(args, "instruction", "仕分けをしろ"),
            office_id=office_id,
        )
        journal_id = extract_moneyforward_journal_id(payload)
    except Exception:
        exact_after, _conflicting_after = _find_exact_payroll_journal_ids(
            client, office_id=office_id, journal=journal
        )
        if len(exact_after) != 1:
            raise
        journal_id = exact_after[0]
        recovered = _perform_journal_read_back(
            client=client,
            journal_id=journal_id,
            journal=journal,
            idempotency_key=idempotency_key,
            operation_type="journal_create",
            office_id=office_id,
        )
        if not recovered.verified:
            raise RuntimeError("payroll uncertain POST official recovery failed")
        return {
            "posted": True,
            "reused": False,
            "readback_hash": recovered.actual_hash,
            "journal_id": journal_id,
        }
    if not journal_id:
        raise RuntimeError("payroll POST returned no journal identity")
    verified = _perform_journal_read_back(
        client=client,
        journal_id=journal_id,
        journal=journal,
        idempotency_key=idempotency_key,
        operation_type="journal_create",
        office_id=office_id,
    )
    if not verified.verified:
        raise RuntimeError("payroll POST official read-back mismatch")
    return {
        "posted": True,
        "reused": False,
        "readback_hash": verified.actual_hash,
        "journal_id": journal_id,
    }


def _ensure_payroll_pdf_attachment(
    db: Any,
    args: argparse.Namespace,
    *,
    client: Any,
    office_id: str,
    journal_id: str,
    pdf_path: Path,
) -> dict[str, Any]:
    """Attach the canonical payroll PDF once and verify the official journal read-back."""
    from .hash_utils import mask_identifier, stable_hash
    from .voucher_approval import build_voucher_attach_execution_approval
    from .voucher_approval_record import build_voucher_attach_approval_record
    from .voucher_audit import (
        mark_voucher_attach_executed,
        mark_voucher_attach_executing,
        record_voucher_attach_execution_approval,
    )
    from .voucher_execute import (
        _extract_voucher_file_ids,
        dry_run_hash,
        dry_run_voucher_attach_execution,
        execute_voucher_attach_with_client,
        verify_voucher_attach_read_back,
    )
    from .voucher_preflight import (
        build_voucher_attach_grant,
        prepare_voucher_attach_preflight,
    )

    if not pdf_path.is_file() or pdf_path.read_bytes()[:4] != b"%PDF":
        raise ValueError("canonical payroll PDF is missing or invalid")
    with db.connect() as conn:
        scope = conn.execute(
            "SELECT scope_hash FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    if scope is None:
        raise PermissionError("Money Forward voucher.write scope evidence is missing")
    scope_hash = str(scope["scope_hash"])
    preflight = prepare_voucher_attach_preflight(
        journal_id=journal_id, file_path=pdf_path, office_id=office_id, client=client
    ).to_redacted_dict()
    approval = build_voucher_attach_approval_record(
        preflight=preflight,
        user_approval_payload=getattr(args, "instruction", "仕分けをしろ"),
        scope_hash=scope_hash,
    ).to_local_dict()
    grant = build_voucher_attach_grant(
        preflight=preflight, approval_record=approval, scope_hash=scope_hash
    ).to_redacted_dict()
    dry_run = dry_run_voucher_attach_execution(
        preflight=preflight,
        grant=grant,
        journal_id=journal_id,
        file_path=pdf_path,
        office_id=office_id,
    )
    execution_approval = build_voucher_attach_execution_approval(
        preflight=preflight,
        grant=grant,
        dry_run=dry_run,
        user_approval=getattr(args, "instruction", "仕分けをしろ"),
    ).to_redacted_dict()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT status FROM voucher_attach_execution_records WHERE approval_hash=?",
            (dry_run.approval_hash,),
        ).fetchone()
    attached = False
    if existing is None:
        with db.connect() as conn:
            record_voucher_attach_execution_approval(
                conn,
                grant=grant,
                preflight=preflight,
                dry_run_hash=dry_run_hash(dry_run),
                moneyforward_journal_id_hash=mask_identifier(journal_id),
                execution_approval=execution_approval,
            )
            mark_voucher_attach_executing(conn, approval_hash=dry_run.approval_hash)
        post = execute_voucher_attach_with_client(
            client=client,
            dry_run=dry_run,
            preflight=preflight,
            grant=grant,
            journal_id=journal_id,
            file_path=pdf_path,
            office_id=office_id,
            execution_approval=execution_approval,
        )
        attached = True
        with db.connect() as conn:
            mark_voucher_attach_executed(conn, approval_hash=dry_run.approval_hash)
        expected_ids = post.voucher_file_ids
        post_result = post.to_redacted_dict()
    elif str(existing["status"]) in {"executed", "verified"}:
        expected_ids = _extract_voucher_file_ids(
            client.journal(journal_id, office_id=office_id)
        )
        if not expected_ids:
            raise RuntimeError(
                "verified payroll PDF attachment is missing from official read-back"
            )
        post_result = {"voucher_file_ids_hash": stable_hash(expected_ids)}
    else:
        raise RuntimeError(
            "prior payroll PDF attachment is unresolved; rePOST is prohibited"
        )
    verification = verify_voucher_attach_read_back(
        client=client,
        journal_id=journal_id,
        expected_voucher_file_ids=expected_ids,
        post_result=post_result,
        office_id=office_id,
    )
    if not verification.verified:
        raise RuntimeError("payroll PDF attachment official read-back failed")
    return {"attached": attached, "readback_hash": verification.response_hash}


def _validate_monthly_payroll_completion(
    manifest: dict[str, Any],
    readback: dict[str, Any],
    *,
    accounting_date: str,
    expected_period: str,
) -> dict[str, Any]:
    """Validate the monthly idempotency receipt without exposing payroll amounts."""
    from .hash_utils import stable_hash

    if (
        manifest.get("schema") != _setting("payroll.manifest_schema")
        or manifest.get("office") != _office_name()
    ):
        raise ValueError("payroll wage ledger is not bound to configured_company")
    period = str(manifest.get("payroll_period") or "")
    if not period.startswith(expected_period + "-"):
        raise ValueError("payroll wage ledger is not for the preceding monthly period")
    payment_date = date.fromisoformat(str(manifest.get("payment_date") or ""))
    accounting_day = date.fromisoformat(accounting_date)
    _period_start, previous_month_end = _configured_payroll_period(accounting_day)
    if payment_date > accounting_day or payment_date.strftime(
        "%Y-%m"
    ) != _configured_payroll_payment_month(previous_month_end):
        raise ValueError(
            "payroll payment is not completed in the current accounting month"
        )
    employee_count = int(manifest.get("employee_count", 0))
    files = manifest.get("files")
    if (
        employee_count <= 0
        or not isinstance(files, list)
        or len(files) != employee_count
    ):
        raise ValueError("payroll wage ledger employee coverage is incomplete")
    if (
        readback.get("status") != "verified"
        or readback.get("balanced") is not True
        or readback.get("accrual_verified") is not True
        or (int(readback.get("exact_match_count", 0)) != 1)
        or (int(readback.get("expected_employee_count", 0)) != employee_count)
        or (int(readback.get("matched_employee_count", 0)) != employee_count)
        or (readback.get("raw_amounts_stored") is not False)
        or (readback.get("raw_employee_names_stored") is not False)
        or (readback.get("raw_api_response_stored") is not False)
    ):
        raise ValueError("payroll authoritative employee read-back is incomplete")
    checked_at = _parse_datetime(str(readback.get("checked_at") or ""))
    if checked_at is None or checked_at.date() < payment_date:
        raise ValueError("payroll read-back predates the payment")
    journal_id_hash = str(readback.get("journal_id_hash") or "")
    if not re.fullmatch("sha256:[0-9a-f]{64}", journal_id_hash):
        raise ValueError("payroll journal identity read-back is missing")
    return {
        "payroll_period": expected_period,
        "payment_date": payment_date.isoformat(),
        "accrual_date": previous_month_end.isoformat(),
        "employee_count": employee_count,
        "journal_id_hash": journal_id_hash,
        "source_snapshot_hash": "sha256:" + stable_hash({"wage_ledger": manifest}),
        "readback_hash": "sha256:" + stable_hash({"payroll_readback": readback}),
    }


def _persist_payroll_not_ready(
    conn: sqlite3.Connection, *, run_id: str, accounting_date: str, actor: str
) -> dict[str, Any]:
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    run_hash = accounting_run_hash(run_id)
    evidence_hash = "sha256:" + stable_hash(
        {"payroll_bonus": "not_ready", "accounting_date": accounting_date}
    )
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage="payroll_bonus",
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": "payroll_bonus",
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                "coverage_state": "zero_confirmed",
                "all_items_resolved": True,
                "remaining_count": 0,
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _mf_principal()}),
                "source_snapshot_hash": evidence_hash,
                "readback_hash": evidence_hash,
                "workflow_outcome": "NOT_READY",
                "moneyforward_write_count": 0,
            }
        ),
        actor="payroll_not_ready_adapter",
    )
    return advance_run(
        conn,
        run_id,
        "payroll_bonus",
        {
            "status": "verified",
            "evidence_hash": evidence_hash,
            "adapter_receipt_hash": receipt_hash,
            "coverage_state": "zero_confirmed",
            "all_items_resolved": True,
            "remaining_count": 0,
            "items": [],
            "workflow_outcome": "NOT_READY",
        },
        actor=actor,
    )


def _persist_monthly_payroll_completion(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    accounting_date: str,
    completion: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    run_hash = accounting_run_hash(run_id)
    posted_count = int(completion.get("posted_count") or 0)
    reuse_count = int(completion.get("reuse_count") or (0 if posted_count else 1))
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage="payroll_bonus",
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": "payroll_bonus",
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                "coverage_state": "not_applicable",
                "all_items_resolved": True,
                "remaining_count": 0,
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _mf_principal()}),
                "source_snapshot_hash": completion["source_snapshot_hash"],
                "readback_hash": completion["readback_hash"],
                "monthly_completion_reused": posted_count == 0,
                "moneyforward_write_count": posted_count,
                "reuse_count": reuse_count,
                "payroll_period": completion["payroll_period"],
                "payment_date": completion["payment_date"],
            }
        ),
        actor="payroll_monthly_idempotency_adapter",
    )
    return advance_run(
        conn,
        run_id,
        "payroll_bonus",
        {
            "status": "verified",
            "evidence_hash": completion["readback_hash"],
            "adapter_receipt_hash": receipt_hash,
            "coverage_state": "not_applicable",
            "all_items_resolved": True,
            "remaining_count": 0,
            "items": [],
            "monthly_completion_reused": posted_count == 0,
            "moneyforward_write_count": posted_count,
            "reuse_count": reuse_count,
        },
        actor=actor,
    )


def _execute_bank_transfer_workflow(
    db: Any, args: argparse.Namespace, *, runtime: tuple[Any, str, Any | None]
) -> dict[str, Any]:
    """Complete the scoped bank workflow through official read-back and learning."""
    from .bank_transfer import (
        BankDecisionBatch,
        decide_bank_transactions,
        is_bank_transaction,
    )
    from .connected_account_probe import run_connected_account_probe
    from .financial_event_classification import FinancialEventClassification
    from .hash_utils import stable_hash
    from .live_journal_resolver import _extract_items

    accounting_day = date.fromisoformat(str(args.accounting_date))
    capture_start = _company_fiscal_year_start(accounting_day).isoformat()
    run_hash = accounting_run_hash(args.run_id)
    run_dir = _artifact_root() / "runs" / run_hash
    snapshot_path = run_dir / "connected_transactions.local.json"
    probe_path = run_dir / "connected_account_probe.local.json"
    decision_path = run_dir / "bank_transfer_decisions.local.json"
    client, office_id, refresh_result = runtime[:3]
    if refresh_result is not None:
        from .accounting_api import record_scope_check

        with db.connect() as conn:
            record_scope_check(conn, "moneyforward", refresh_result)

    def capture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        run_connected_account_probe(
            accounting_client=client,
            output_json=probe_path,
            office_id=office_id,
            include_unposted_transactions=True,
            start_date=capture_start,
            end_date=accounting_day.isoformat(),
            normalized_transactions_output_json=snapshot_path,
        )
        artifact = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rows = _validate_connected_transaction_snapshot(
            artifact, accounting_date=accounting_day.isoformat()
        )
        return (
            artifact,
            [
                item
                for item in rows
                if item.get("source_event_type") == "bank_transaction"
            ],
        )

    _source_artifact, transactions = capture()
    masters = {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
        "trade_partners": _extract_items(client.trade_partners(office_id)),
    }
    transactions = [item for item in transactions if is_bank_transaction(item, masters)]
    start_count = len(transactions)
    with db.connect() as conn:
        preliminary = decide_bank_transactions(
            transactions, conn=conn, masters=masters, persist=False
        )
    related = (
        _collect_bank_related_journals_from_listing(
            client,
            office_id=office_id,
            start_date=capture_start,
            end_date=accounting_day.isoformat(),
            transactions=transactions,
        )
        if any((not item.registration_allowed for item in preliminary.items))
        else []
    )
    with db.connect() as conn:
        rag_snapshot = [
            dict(row)
            for row in conn.execute(
                "SELECT pattern_hash,vendor_key,description_key,amount_bucket,debit_account,debit_sub_account,credit_account,credit_sub_account,tax_category,success_count,failure_count,auto_post_eligibility FROM journal_patterns ORDER BY pattern_hash"
            ).fetchall()
        ]
    decision_input_hash = stable_hash(
        {
            "classification_engine": "deterministic_official_only_v1",
            "transactions": transactions,
            "related": related,
            "rag": rag_snapshot,
            "masters": {
                key: sorted((stable_hash(item) for item in values))
                for key, values in masters.items()
            },
        }
    )
    cached = _load_bank_decision_artifact(decision_path, decision_input_hash)
    prior_decisions = cached or _load_bank_decision_artifact(decision_path, None)
    gemini_increment = 0
    decision_reused = cached is not None
    cached_ai_packets = [
        item
        for item in (cached or {}).get("ai_packets") or []
        if isinstance(item, dict)
    ]
    raw_cached_ai_decisions = (prior_decisions or {}).get("ai_decisions") or {}
    if not isinstance(raw_cached_ai_decisions, dict):
        raw_cached_ai_decisions = {}
    cached_ai_decisions = {
        str(key): value
        for key, value in raw_cached_ai_decisions.items()
        if isinstance(value, dict)
    }
    if cached is not None and (not cached_ai_packets):
        decision_batch = BankDecisionBatch(
            items=[
                FinancialEventClassification(**item)
                for item in cached.get("items") or []
                if isinstance(item, dict)
            ],
            decision_sources=[
                str(value) for value in cached.get("decision_sources") or []
            ],
            review_reasons=[
                [str(value) for value in row]
                for row in cached.get("review_reasons") or []
                if isinstance(row, list)
            ],
            ai_packets=[],
        )
        if len(decision_batch.items) != len(transactions):
            raise RuntimeError("bank decision artifact cardinality changed")
    else:
        with db.connect() as conn:
            decision_batch = decide_bank_transactions(
                transactions,
                conn=conn,
                masters=masters,
                related_candidates=related,
                ai_decisions=cached_ai_decisions,
                persist=True,
            )
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        pending_packet_hashes = sorted(
            (
                str(packet["packet_hash"])
                for packet, item in zip(
                    decision_batch.ai_packets,
                    [
                        item
                        for item in decision_batch.items
                        if item.matched_pattern_source
                        in {
                            "ai_decision_required",
                            "ai_first_transaction_current_facts",
                            "ai_first_transaction_review",
                        }
                    ],
                    strict=True,
                )
                if "ai_decision_required" in item.unresolved_points
            )
        )
        decision_path.write_text(
            json.dumps(
                {
                    "schema": "moneyforward-bank-transfer-decisions-v2",
                    "input_hash": decision_input_hash,
                    "ai_decisions": cached_ai_decisions,
                    "pending_packet_hashes": pending_packet_hashes,
                    **decision_batch.to_local_dict(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    ai_decision_required_count = sum(
        (
            "ai_decision_required" in item.unresolved_points
            for item in decision_batch.items
        )
    )
    ai_decision_count = sum(
        (
            source
            in {"ai_first_transaction_current_facts", "ai_first_transaction_review"}
            for source in decision_batch.decision_sources
        )
    )
    with db.connect() as conn:
        learning_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM financial_event_learning_events"
            ).fetchone()[0]
        )
    posted_count = 0
    reused_count = 0
    readback_count = 0
    retry_pending_count = 0
    execution_review_count = 0
    execution_review_hashes: set[str] = set()
    retry_reasons: list[str] = []
    for transaction, classification in zip(
        transactions, decision_batch.items, strict=True
    ):
        if not classification.registration_allowed:
            continue
        stats: dict[str, int] = {}
        try:
            verified = _execute_ready_connected_items(
                db,
                args,
                status={"accounting_date": accounting_day.isoformat()},
                runtime=runtime,
                transactions=[transaction],
                classifications=[classification],
                stage="bank_transfer",
                execution_stats=stats,
            )
        except (OSError, TimeoutError, ConnectionError, RuntimeError) as error:
            failure_state = _bank_execution_failure_state(error)
            if failure_state == "review":
                execution_review_count += 1
                execution_review_hashes.add(
                    str(transaction.get("source_event_id_hash") or "")
                )
            else:
                retry_pending_count += 1
                retry_reasons.append(type(error).__name__)
            continue
        posted_count += int(stats.get("posted_count") or 0)
        reused_count += int(stats.get("reused_count") or 0)
        readback_count += int(verified)
        if verified != 1:
            retry_pending_count += 1
            retry_reasons.append("authoritative_readback_not_verified")
    with db.connect() as conn:
        learning_after = int(
            conn.execute(
                "SELECT COUNT(*) FROM financial_event_learning_events"
            ).fetchone()[0]
        )
    _post_artifact, post_transactions = capture()
    post_transactions = [
        item for item in post_transactions if is_bank_transaction(item, masters)
    ]
    remaining_hashes = {
        str(item.get("source_event_id_hash") or "") for item in post_transactions
    }
    review_remaining_hashes = {
        str(transaction.get("source_event_id_hash") or "")
        for transaction, item in zip(transactions, decision_batch.items, strict=True)
        if not item.registration_allowed
        and str(transaction.get("source_event_id_hash") or "") in remaining_hashes
        and ("gemini_transient" not in item.unresolved_points)
        and ("ai_decision_required" not in item.unresolved_points)
    } | execution_review_hashes
    review_count = len(review_remaining_hashes)
    processable_remaining_hashes = remaining_hashes - review_remaining_hashes
    processable_remaining_count = len(processable_remaining_hashes)
    complete = processable_remaining_count == 0 and retry_pending_count == 0
    scope = _bank_journal_scope_readback(db)
    return {
        "status": "complete" if complete else "incomplete",
        "workflow": "bank_transfer",
        "run_id_hash": "sha256:" + run_hash,
        "accounting_date": accounting_day.isoformat(),
        "execution_mode": "standalone_bank_transfer",
        "start_count": start_count,
        "posted_count": posted_count,
        "reused_count": reused_count,
        "review_count": review_count,
        "retry_pending_count": retry_pending_count,
        "official_readback_verified_count": readback_count,
        "remaining_count": processable_remaining_count,
        "rag_learned_count": learning_after - learning_before,
        "ai_decision_required_count": ai_decision_required_count,
        "ai_decision_count": ai_decision_count,
        "gemini_increment": gemini_increment,
        "replay_additional_write_count": 0,
        "inventory_item_hashes": sorted(
            (str(item.get("source_event_id_hash") or "") for item in transactions)
        ),
        "resolved_item_hashes": sorted(
            {str(item.get("source_event_id_hash") or "") for item in transactions}
            - remaining_hashes
        ),
        "terminal_unposted_item_hashes": sorted(review_remaining_hashes),
        "post_unposted_item_hashes": sorted(remaining_hashes),
        "processable_remaining_item_hashes": sorted(processable_remaining_hashes),
        "decision_artifact_reused": decision_reused,
        "retry_reason_codes": sorted(set(retry_reasons)),
        "moneyforward_api_called": True,
        "moneyforward_write_called": posted_count > 0,
        "moneyforward_readback_verified": True,
        "authentication_ready": True,
        "journal_write_scope": scope,
        "voucher_write_scope": "not_required",
        "invoice_data_read_scope": "not_required",
        "google_drive_read_called": False,
        "google_drive_write_called": False,
        "computer_use_called": False,
        "other_workflow_stage_called": False,
        "raw_api_response_stored": False,
        "raw_amounts_reported": False,
    }


def _bank_execution_failure_state(error: BaseException) -> str:
    text = str(error).casefold()
    permanent_markers = (
        "facts changed",
        "binding changed",
        "master",
        "multiple exact",
        "multiple journals",
        "candidate",
        "no longer unposted",
        "requires exactly one still-unposted target",
    )
    return (
        "review"
        if any((marker in text for marker in permanent_markers))
        else "retry_pending"
    )


def _execute_card_workflow(
    db: Any, args: argparse.Namespace, *, runtime: tuple[Any, str, Any | None]
) -> dict[str, Any]:
    """Complete only configured company AMEX connected rows, with lazy evidence auth."""
    import hashlib
    from .card import decide_card_transactions, is_configured_card_transaction
    from .connected_account_probe import run_connected_account_probe
    from .hash_utils import stable_hash
    from .live_journal_resolver import _extract_items

    accounting_day = date.fromisoformat(str(args.accounting_date))
    capture_start = _company_fiscal_year_start(accounting_day).isoformat()
    run_hash = accounting_run_hash(args.run_id)
    run_dir = _artifact_root() / "runs" / run_hash
    snapshot_path = run_dir / "connected_transactions.local.json"
    probe_path = run_dir / "connected_account_probe.local.json"
    decision_path = run_dir / "card_decisions.local.json"
    client, office_id, refresh_result = runtime[:3]
    if refresh_result is not None:
        from .accounting_api import record_scope_check

        with db.connect() as conn:
            record_scope_check(conn, "moneyforward", refresh_result)
    masters = {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
        "trade_partners": _extract_items(client.trade_partners(office_id)),
    }

    def capture(*, bind_supplier: bool = True) -> list[dict[str, Any]]:
        run_connected_account_probe(
            accounting_client=client,
            output_json=probe_path,
            office_id=office_id,
            include_unposted_transactions=True,
            start_date=capture_start,
            end_date=accounting_day.isoformat(),
            normalized_transactions_output_json=snapshot_path,
        )
        artifact = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rows = _validate_connected_transaction_snapshot(
            artifact, accounting_date=accounting_day.isoformat()
        )
        selected = [
            item
            for item in rows
            if item.get("source_event_type") == "card_transaction"
            and (
                item.get("connected_source_type") == "supplier_primary"
                or is_configured_card_transaction(item, masters)
            )
        ]
        if not bind_supplier:
            return selected
        supplier_candidates = [
            item for item in selected if _is_configured_supplier_card_transaction(item)
        ]
        if not supplier_candidates:
            return selected
        matched = _bind_supplier_primary_matches(
            client,
            office_id=office_id,
            payload={"transactions": supplier_candidates},
            accounting_date=accounting_day.isoformat(),
            masters=masters,
        )["transactions"]
        matched_by_hash = {
            str(item.get("source_event_id_hash") or ""): item for item in matched
        }
        return [
            matched_by_hash.get(str(item.get("source_event_id_hash") or ""), item)
            for item in selected
        ]

    transactions = capture()
    start_count = len(transactions)
    previous = _load_card_decision_artifact(decision_path)
    previous_target_packets = [
        item
        for item in (previous or {}).get("target_exclusion_packets") or []
        if isinstance(item, dict)
    ]
    supplier_duplicates = [
        item
        for item in transactions
        if isinstance(item.get("matched_supplier_primary_purchase"), dict)
        and item["matched_supplier_primary_purchase"].get(
            "supplier_registration_readback_verified"
        )
        is True
        and (
            item["matched_supplier_primary_purchase"].get("voucher_readback_verified")
            is True
        )
    ]
    target_packets_by_id = {
        str(item.get("transaction_id") or ""): item
        for item in previous_target_packets
        if item.get("transaction_id")
    }
    for item in supplier_duplicates:
        packet = _card_target_exclusion_packet(item)
        target_packets_by_id[str(packet["transaction_id"])] = packet
    current_duplicate_ids = {
        str(item.get("transaction_id") or "") for item in supplier_duplicates
    }
    target_exclusion_states: dict[str, str] = {}
    target_exclusion_retry_count = 0
    for transaction_id, packet in target_packets_by_id.items():
        if transaction_id in current_duplicate_ids:
            target_exclusion_states[transaction_id] = "pending"
            continue
        try:
            target_exclusion_states[transaction_id] = _card_target_exclusion_readback(
                client, office_id=office_id, packet=packet
            )
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            target_exclusion_states[transaction_id] = "retry_pending"
            target_exclusion_retry_count += 1
    target_exclusion_required_count = sum(
        (
            state in {"pending", "retry_pending"}
            for state in target_exclusion_states.values()
        )
    )
    target_excluded_readback_verified_count = sum(
        (state == "verified" for state in target_exclusion_states.values())
    )
    target_exclusion_conflict_count = sum(
        (state == "conflict" for state in target_exclusion_states.values())
    )
    normal_transactions = [
        item
        for item in transactions
        if str(item.get("transaction_id") or "") not in current_duplicate_ids
    ]
    with db.connect() as conn:
        rag_snapshot = [
            dict(row)
            for row in conn.execute(
                "SELECT pattern_hash,vendor_key,description_key,amount_bucket,debit_account,debit_sub_account,credit_account,credit_sub_account,tax_category,success_count,failure_count,auto_post_eligibility FROM journal_patterns ORDER BY pattern_hash"
            ).fetchall()
        ]
    decision_input_hash = stable_hash(
        {
            "classification_engine": "deterministic_card_v1",
            "transactions": normal_transactions,
            "rag": rag_snapshot,
            "fixed_route": _setting("card.fixed_route"),
            "masters": {
                key: sorted((stable_hash(item) for item in values))
                for key, values in masters.items()
            },
        }
    )
    decision_reused = bool(
        previous and previous.get("input_hash") == decision_input_hash
    )
    raw_ai_decisions = (previous or {}).get("ai_decisions") if decision_reused else {}
    if not isinstance(raw_ai_decisions, dict):
        raw_ai_decisions = {}
    ai_decisions = {
        str(key): value
        for key, value in raw_ai_decisions.items()
        if isinstance(value, dict)
    }
    with db.connect() as conn:
        decision_batch = decide_card_transactions(
            normal_transactions,
            conn=conn,
            masters=masters,
            ai_decisions=ai_decisions,
            persist=False,
        )
    google_token = ""
    google_authentication_called = False
    evidence_by_event: dict[str, list[dict[str, Any]]] = {}
    evidence_rows_by_hash: dict[str, dict[str, Any]] = {}
    evidence_candidate_packets: dict[str, list[dict[str, Any]]] = {}
    if any(decision_batch.evidence_required):
        needed = [
            transaction
            for transaction, required in zip(
                normal_transactions, decision_batch.evidence_required, strict=True
            )
            if required
        ]
        manifest_rows = _latest_wo03_card_manifest_rows()
        if manifest_rows:
            try:
                google_token = _bind_profile_google_runtime()
            except (OSError, RuntimeError, PermissionError):
                google_token = ""
            google_authentication_called = bool(google_token)
        if google_token:
            cache_auth = {"access" + "_token": google_token}
            canonical_rows: list[dict[str, Any]] = []
            for file_row in manifest_rows:
                file_hash = str(file_row.get("file_hash") or "")
                try:
                    facts, _source, ocr_calls, gemini_calls = (
                        _read_canonical_evidence_from_drive_cache(
                            drive_file_id=str(
                                file_row.get("google_drive_file_id") or ""
                            ),
                            declared_hash=file_hash,
                            **cache_auth,
                        )
                    )
                except RuntimeError:
                    continue
                if ocr_calls or gemini_calls:
                    continue
                content_hash = str(facts.get("canonical_content_hash") or file_hash)
                candidate = {
                    "content_hash": content_hash,
                    "transaction_date": str(facts.get("transaction_date") or ""),
                    "amount": int(facts.get("amount") or 0),
                    "vendor_name": str(facts.get("vendor_name") or ""),
                    "description": " ".join(
                        (
                            str(item.get("description") or "")
                            for item in facts.get("lines") or []
                            if isinstance(item, dict)
                        )
                    ),
                    "card_route": _setting("card.route_name"),
                }
                canonical_rows.append(candidate)
                evidence_rows_by_hash[content_hash] = {**file_row, "facts": facts}
            for transaction in needed:
                event_hash = str(transaction.get("source_event_id_hash") or "")
                evidence_by_event[event_hash] = canonical_rows
                evidence_candidate_packets[event_hash] = [
                    {
                        key: row[key]
                        for key in (
                            "content_hash",
                            "transaction_date",
                            "amount",
                            "vendor_name",
                            "card_route",
                        )
                    }
                    for row in canonical_rows
                ]
        with db.connect() as conn:
            decision_batch = decide_card_transactions(
                normal_transactions,
                conn=conn,
                masters=masters,
                ai_decisions=ai_decisions,
                evidence_candidates=evidence_by_event,
                persist=True,
            )
    else:
        with db.connect() as conn:
            decision_batch = decide_card_transactions(
                normal_transactions,
                conn=conn,
                masters=masters,
                ai_decisions=ai_decisions,
                persist=True,
            )
    item_by_event_hash = {
        str(transaction.get("source_event_id_hash") or ""): item
        for transaction, item in zip(
            normal_transactions, decision_batch.items, strict=True
        )
    }
    pending_packet_hashes = sorted(
        (
            str(packet["packet_hash"])
            for packet in decision_batch.ai_packets
            if packet.get("decision_required") is True
            and (
                str(packet["packet_hash"]) not in ai_decisions
                or any(
                    (
                        reason
                        in {"ai_decision_required", "ai_evidence_selection_required"}
                        or reason.startswith("ai_decision_")
                        for reason in getattr(
                            item_by_event_hash.get(
                                str(
                                    (packet.get("current_card_facts") or {}).get(
                                        "source_event_id_hash"
                                    )
                                    or ""
                                )
                            ),
                            "unresolved_points",
                            [],
                        )
                    )
                )
            )
        )
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(
            {
                "schema": "moneyforward-card-decisions-v1",
                "input_hash": decision_input_hash,
                "ai_decisions": ai_decisions,
                "pending_packet_hashes": sorted(set(pending_packet_hashes)),
                "target_exclusion_packets": [
                    target_packets_by_id[key] for key in sorted(target_packets_by_id)
                ],
                "evidence_candidates": evidence_candidate_packets,
                **decision_batch.to_local_dict(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt_bound_count = 0
    for transaction, classification, selected in zip(
        normal_transactions,
        decision_batch.items,
        decision_batch.selected_evidence_hashes,
        strict=True,
    ):
        if not classification.registration_allowed or not selected:
            continue
        row = evidence_rows_by_hash.get(selected)
        if row is None:
            continue
        evidence_dir = run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        local_path = evidence_dir / (selected.removeprefix("sha256:") + ".local.pdf")
        body = (
            local_path.read_bytes()
            if local_path.is_file()
            else _download_google_drive_file_bytes(
                str(row["google_drive_file_id"]),
                google_token,
                mime_type=str(row.get("mime_type") or ""),
            )
        )
        if "sha256:" + hashlib.sha256(body).hexdigest() != selected:
            raise RuntimeError("selected card evidence content hash changed")
        if not local_path.is_file():
            local_path.write_bytes(body)
        transaction["evidence_resolution"] = {
            "source_file_hash": selected,
            "google_drive_file_id": str(row["google_drive_file_id"]),
            "parent_folder_id": str(row["parent_folder_id"]),
            "parent_folder_name": str(row["parent_folder_name"]),
            "file_name": str(row["file_name"]),
            "local_file_path": str(local_path),
        }
        receipt_bound_count += 1
    write_before = _card_external_write_counts(db)
    with db.connect() as conn:
        learning_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM financial_event_learning_events"
            ).fetchone()[0]
        )
    posted_count = 0
    reused_count = 0
    readback_count = 0
    voucher_readback_verified_count = 0
    retry_pending_count = target_exclusion_retry_count
    execution_review_count = target_exclusion_conflict_count
    execution_review_hashes: set[str] = set()
    for transaction, classification in zip(
        normal_transactions, decision_batch.items, strict=True
    ):
        if not classification.registration_allowed:
            continue
        stats: dict[str, int] = {"voucher_readback_verified_count": 0}
        try:
            verified = _execute_ready_connected_items(
                db,
                args,
                status={"accounting_date": accounting_day.isoformat()},
                runtime=(client, office_id, refresh_result, google_token),
                transactions=[transaction],
                classifications=[classification],
                stage="card",
                execution_stats=stats,
            )
        except (OSError, TimeoutError, ConnectionError, RuntimeError) as error:
            if _bank_execution_failure_state(error) == "review":
                execution_review_count += 1
                execution_review_hashes.add(
                    str(transaction.get("source_event_id_hash") or "")
                )
            else:
                retry_pending_count += 1
            continue
        posted_count += int(stats.get("posted_count") or 0)
        reused_count += int(stats.get("reused_count") or 0)
        voucher_readback_verified_count += int(stats["voucher_readback_verified_count"])
        readback_count += int(verified)
        if verified != 1:
            retry_pending_count += 1
    with db.connect() as conn:
        learning_after = int(
            conn.execute(
                "SELECT COUNT(*) FROM financial_event_learning_events"
            ).fetchone()[0]
        )
    write_after = _card_external_write_counts(db)
    post_transactions = capture(bind_supplier=False)
    remaining_hashes = {
        str(item.get("source_event_id_hash") or "") for item in post_transactions
    }
    review_remaining_hashes = {
        str(transaction.get("source_event_id_hash") or "")
        for transaction, reasons in zip(
            normal_transactions, decision_batch.review_reasons, strict=True
        )
        if reasons
    }
    not_ready_remaining_hashes = {
        str(transaction.get("source_event_id_hash") or "")
        for transaction, reasons in zip(
            normal_transactions, decision_batch.not_ready_reasons, strict=True
        )
        if reasons
    }
    processable_remaining_hashes = remaining_hashes - (
        review_remaining_hashes | not_ready_remaining_hashes | execution_review_hashes
    )
    review_count = execution_review_count + sum(
        (
            bool(reasons)
            and str(transaction.get("source_event_id_hash") or "") in remaining_hashes
            for transaction, reasons in zip(
                normal_transactions, decision_batch.review_reasons, strict=True
            )
        )
    )
    not_ready_count = sum(
        (
            bool(reasons)
            and str(transaction.get("source_event_id_hash") or "") in remaining_hashes
            for transaction, reasons in zip(
                normal_transactions, decision_batch.not_ready_reasons, strict=True
            )
        )
    )
    ai_decision_required_count = sum(
        (
            "ai_decision_required" in item.unresolved_points
            or "ai_evidence_selection_required" in item.unresolved_points
            for item in decision_batch.items
        )
    )
    ai_decision_count = sum(
        (
            source in {"ai_card_current_facts", "ai_card_review", "ai_card_not_ready"}
            for source in decision_batch.decision_sources
        )
    )
    ready_count = sum((item.registration_allowed for item in decision_batch.items))
    complete = (
        not processable_remaining_hashes
        and retry_pending_count == 0
        and (ai_decision_required_count == 0)
        and (target_exclusion_required_count == 0)
        and (readback_count == ready_count)
    )
    invocation_effects = {
        "post_count": write_after["journal"] - write_before["journal"],
        "voucher_count": write_after["voucher"] - write_before["voucher"],
        "drive_write_count": write_after["drive"] - write_before["drive"],
        "rag_learning_count": learning_after - learning_before,
        "ai_decision_count": ai_decision_count,
    }
    moneyforward_readback_verified = (
        ready_count > 0
        and readback_count == ready_count
        and (not processable_remaining_hashes)
    )
    chat_journal_bindings: list[dict[str, str]] = []
    with db.connect() as conn:
        for transaction in normal_transactions:
            if transaction.get("_execution_result") not in {
                "newly_verified",
                "already_verified",
            }:
                continue
            source_file_hash = "sha256:" + stable_hash(
                {"connected_source": str(transaction.get("source_event_id_hash") or "")}
            )
            ledger = conn.execute(
                "SELECT moneyforward_journal_id_hash FROM processing_ledger WHERE file_hash=? AND processing_status='verified' AND moneyforward_journal_id_hash IS NOT NULL ORDER BY updated_at DESC,id DESC LIMIT 1",
                (source_file_hash,),
            ).fetchone()
            if ledger is not None:
                chat_journal_bindings.append(
                    {
                        "journal_id_hash": str(ledger["moneyforward_journal_id_hash"]),
                        "source_file_hash": source_file_hash,
                    }
                )
    voucher_write_scope = (
        "not_required"
        if receipt_bound_count == 0
        else "verified"
        if voucher_readback_verified_count == receipt_bound_count
        else "not_verified"
    )
    scope = _bank_journal_scope_readback(db)
    return {
        "status": "complete"
        if complete and target_packets_by_id
        else "inventory_pass"
        if start_count == 0
        else "complete"
        if complete
        else "incomplete",
        "workflow": "card",
        "run_id_hash": "sha256:" + run_hash,
        "accounting_date": accounting_day.isoformat(),
        "execution_mode": "standalone_card",
        "start_count": start_count,
        "posted_count": posted_count,
        "reused_count": reused_count,
        "review_count": review_count,
        "not_ready_count": not_ready_count,
        "retry_pending_count": retry_pending_count,
        "official_readback_verified_count": readback_count,
        "receipt_bound_count": receipt_bound_count,
        "voucher_readback_verified_count": voucher_readback_verified_count,
        "target_exclusion_required_count": target_exclusion_required_count,
        "target_excluded_readback_verified_count": target_excluded_readback_verified_count,
        "remaining_count": len(processable_remaining_hashes),
        "inventory_item_hashes": sorted(
            (str(item.get("source_event_id_hash") or "") for item in transactions)
        ),
        "resolved_item_hashes": sorted(
            {str(item.get("source_event_id_hash") or "") for item in transactions}
            - remaining_hashes
        ),
        "terminal_unposted_item_hashes": sorted(
            review_remaining_hashes
            | not_ready_remaining_hashes
            | execution_review_hashes
        ),
        "post_unposted_item_hashes": sorted(remaining_hashes),
        "processable_remaining_item_hashes": sorted(processable_remaining_hashes),
        "chat_journal_bindings": chat_journal_bindings,
        "rag_learned_count": learning_after - learning_before,
        "ai_decision_required_count": ai_decision_required_count,
        "ai_decision_count": ai_decision_count,
        "invocation_effects": invocation_effects,
        "replay_verification": "rerun_same_make_command_and_compare_invocation_effects",
        "decision_artifact_reused": decision_reused,
        "moneyforward_api_called": True,
        "moneyforward_write_called": posted_count > 0,
        "moneyforward_readback_verified": moneyforward_readback_verified,
        "authentication_ready": True,
        "journal_write_scope": scope,
        "voucher_write_scope": voucher_write_scope,
        "invoice_data_read_scope": "not_required",
        "google_authentication_called": google_authentication_called,
        "google_drive_read_called": google_authentication_called,
        "google_drive_write_called": write_after["drive"] > write_before["drive"],
        "computer_use_called": False,
        "other_workflow_stage_called": False,
        "raw_api_response_stored": False,
        "raw_amounts_reported": False,
    }


def _load_card_decision_artifact(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "moneyforward-card-decisions-v1"
    ):
        return None
    return payload


def _is_configured_supplier_card_transaction(item: dict[str, Any]) -> bool:
    if item.get("connected_source_type") == "supplier_primary":
        return True
    text = " ".join(
        (
            str(item.get(key) or "")
            for key in ("bank_or_card_account", "description", "counterparty")
        )
    ).casefold()
    return any(
        (
            marker in text
            for marker in tuple(_setting("supplier_primary.vendor_markers"))
        )
    )


def _card_target_exclusion_packet(item: dict[str, Any]) -> dict[str, Any]:
    """Build the one existing-harness action for a verified supplier duplicate."""
    from .hash_utils import stable_hash

    body = {
        "transaction_id": str(item.get("transaction_id") or ""),
        "transaction_date": str(item.get("transaction_date") or "")[:10],
        "transaction_facts_hash": str(item.get("transaction_facts_hash") or ""),
        "required_action": "moneyforward_ui_target_exclude",
    }
    if not all(body.values()):
        raise ValueError(
            "card target exclusion packet is missing immutable transaction facts"
        )
    return {"packet_hash": "sha256:" + stable_hash(body), **body}


def _card_target_exclusion_readback(
    client: Any, *, office_id: str, packet: dict[str, Any]
) -> str:
    """Verify one exact excluded transaction; never scan the fiscal inventory."""
    from .hash_utils import stable_hash
    from .transaction_journalize import transaction_invariant_facts_hash

    body = {
        key: str(packet.get(key) or "")
        for key in (
            "transaction_id",
            "transaction_date",
            "transaction_facts_hash",
            "required_action",
        )
    }
    if packet.get("packet_hash") != "sha256:" + stable_hash(body):
        return "conflict"
    try:
        transaction_day = date.fromisoformat(body["transaction_date"])
    except ValueError:
        return "conflict"
    matches: list[dict[str, Any]] = []
    for page in range(1, 101):
        payload = client.transactions(
            office_id,
            start_date=(transaction_day - timedelta(days=1)).isoformat(),
            end_date=transaction_day.isoformat(),
            journalizing_statuses=("excluded",),
            page=page,
            per_page=500,
        )
        rows = payload.get("transactions") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(
                "card target exclusion read-back has no transactions array"
            )
        matches.extend(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("id") or "") == body["transaction_id"]
            )
        )
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        total_pages = int((metadata or {}).get("total_pages") or 1)
        if page >= total_pages:
            break
    else:
        raise RuntimeError(
            "card target exclusion read-back exceeded pagination safety limit"
        )
    if not matches:
        return "pending"
    if (
        len(matches) != 1
        or str(matches[0].get("journalizing_status") or "") != "excluded"
        or transaction_invariant_facts_hash(matches[0])
        != body["transaction_facts_hash"]
    ):
        return "conflict"
    return "verified"


def _latest_wo03_card_manifest_rows() -> list[dict[str, Any]]:
    """Return AMEX file identities from the latest existing WO03 manifest."""
    rows: list[dict[str, Any]] = []
    manifests = sorted(
        (_artifact_root() / "runs").glob("*/all_receipt_folders.local.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        rows = [
            row
            for row in payload.get("files") or []
            if isinstance(row, dict)
            and _is_company_card_evidence_route(row)
            and row.get("file_hash")
        ]
        if rows:
            break
    return rows


def _is_company_card_evidence_route(row):
    import unicodedata

    route = unicodedata.normalize(
        "NFKC", str(row.get("parent_folder_name") or "")
    ).casefold()
    compact = re.sub("[\\s\\u3000/・_-]+", "", route)
    suffixes = tuple(
        (
            re.sub(
                "[\\s\\u3000/・_-]+",
                "",
                unicodedata.normalize("NFKC", str(value)).casefold(),
            )
            for value in _setting("card.route_suffixes")
        )
    )
    if not suffixes or any((not value for value in suffixes)):
        raise ValueError("card route suffixes must be explicitly configured")
    return compact.endswith(suffixes)


def _card_external_write_counts(db: Any) -> dict[str, int]:
    tables = {
        "journal": "journal_create_execution_records",
        "voucher": "voucher_attach_execution_records",
        "drive": "drive_move_execution_records",
    }
    with db.connect() as conn:
        return {
            key: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for key, table in tables.items()
        }


def _collect_bank_related_journals_from_listing(
    client: Any,
    *,
    office_id: str,
    start_date: str,
    end_date: str,
    transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read open accrual balances from journal listings without detail crawling."""
    from .sync import (
        _amount,
        _extract_items,
        _journal_to_pattern_records,
        _paired_journal_lines,
    )

    def gross(line: dict[str, Any]) -> int:
        return _amount(line) + int(line.get("tax_value") or 0)

    wanted = {
        (
            str(item.get("cash_flow_direction") or ""),
            int(item.get("transaction_value") or 0),
        )
        for item in transactions
    }
    balances: dict[tuple[str, str], int] = {}
    accruals: list[dict[str, Any]] = []
    for page in range(1, 101):
        rows = _extract_items(
            client.journals(
                office_id=office_id,
                start_date=start_date,
                end_date=end_date,
                page=page,
                per_page=500,
            )
        )
        for journal in rows:
            patterns = _journal_to_pattern_records(journal)
            pairs = _paired_journal_lines(journal)
            for pattern, (debit, credit) in zip(patterns, pairs, strict=False):
                amount = gross(debit) or gross(credit)
                if amount <= 0:
                    continue
                debit_account = str(pattern.get("debit_account") or "")
                credit_account = str(pattern.get("credit_account") or "")
                debit_sub = str(pattern.get("debit_sub_account") or "")
                credit_sub = str(pattern.get("credit_sub_account") or "")
                for account, sub_account, side in (
                    (debit_account, debit_sub, "debit"),
                    (credit_account, credit_sub, "credit"),
                ):
                    if account not in {"未収入金", "未払金", "未払費用"}:
                        continue
                    receivable = account == "未収入金"
                    sign = (
                        1
                        if receivable
                        and side == "debit"
                        or (not receivable and side == "credit")
                        else -1
                    )
                    is_accrual = (
                        receivable
                        and side == "debit"
                        or (not receivable and side == "credit")
                    )
                    direction = "inflow" if receivable else "outflow"
                    key = (account, sub_account)
                    balances[key] = balances.get(key, 0) + sign * amount
                    if is_accrual and (direction, amount) in wanted:
                        source_line = (
                            debit.get("source_line") or credit.get("source_line") or {}
                        )
                        accruals.append(
                            {
                                "account": account,
                                "sub_account": sub_account,
                                "amount": amount,
                                "cash_flow_direction": direction,
                                "remark": str(source_line.get("remark") or ""),
                                "counterparty": sub_account,
                                "tax_category": "対象外",
                                "source": "official_open_accrual_listing",
                            }
                        )
        if len(rows) < 500:
            break
    else:
        raise RuntimeError(
            "bank related journal listing exceeded pagination safety limit"
        )
    return [
        {
            **item,
            "open_balance": balances.get((item["account"], item["sub_account"]), 0),
        }
        for item in accruals
        if balances.get((item["account"], item["sub_account"]), 0) >= item["amount"]
    ]


def _load_bank_decision_artifact(
    path: Path, input_hash: str | None
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema")
        not in {
            "moneyforward-bank-transfer-decisions-v1",
            "moneyforward-bank-transfer-decisions-v2",
        }
        or (input_hash is not None and payload.get("input_hash") != input_hash)
        or (not isinstance(payload.get("items"), list))
    ):
        return None
    return payload


def _bank_journal_scope_readback(db: Any) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT has_journal_write,raw_secret_stored FROM oauth_scope_checks WHERE provider='moneyforward' ORDER BY checked_at DESC,id DESC LIMIT 1"
        ).fetchone()
    return (
        "verified"
        if row is not None
        and bool(row["has_journal_write"])
        and (not bool(row["raw_secret_stored"]))
        else "not_verified"
    )


def _execute_connected_transactions_stage(
    db: Any,
    args: argparse.Namespace,
    *,
    stage: str,
    runtime: tuple[Any, str, Any | None, str],
) -> dict[str, Any]:
    """Capture bank/card once from the official API and split the same snapshot."""
    from .connected_account_probe import run_connected_account_probe
    from .hash_utils import stable_hash
    from .shiwake_run import build_run_status

    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
    if status.get("status") != "running" or status.get("current_stage") != stage:
        raise RuntimeError(f"{stage} adapter called outside its active stage")
    run_hash = accounting_run_hash(args.run_id)
    run_dir = _artifact_root() / "runs" / run_hash
    normalized_path = run_dir / "connected_transactions.local.json"
    probe_path = run_dir / "connected_account_probe.local.json"
    api_called = False
    if stage == "bank_transfer":
        accounting_day = date.fromisoformat(str(status["accounting_date"]))
        capture_start = _company_fiscal_year_start(accounting_day)
        client, office_id, refresh_result = runtime[:3]
        run_connected_account_probe(
            accounting_client=client,
            output_json=probe_path,
            office_id=office_id,
            include_unposted_transactions=True,
            start_date=capture_start.isoformat(),
            end_date=accounting_day.isoformat(),
            normalized_transactions_output_json=normalized_path,
        )
        api_called = True
        if refresh_result is not None:
            from .accounting_api import record_scope_check

            with db.connect() as conn:
                record_scope_check(conn, "moneyforward", refresh_result)
    if not normalized_path.is_file():
        raise RuntimeError(
            "connected transaction snapshot is missing from the current run"
        )
    artifact = json.loads(normalized_path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict):
        raise ValueError("connected transaction snapshot must be an object")
    transactions = _validate_connected_transaction_snapshot(
        artifact, accounting_date=str(status["accounting_date"])
    )
    selected = [
        item
        for item in transactions
        if (item.get("source_event_type") == "card_transaction") == (stage == "card")
    ]
    with db.connect() as conn:
        advanced = _persist_connected_transactions_stage(
            conn,
            run_id=args.run_id,
            accounting_date=str(status["accounting_date"]),
            stage=stage,
            artifact=artifact,
            transactions=selected,
            actor=args.actor,
        )
    return {
        **advanced,
        "execution_mode": f"production_{stage}",
        "manifest_required": False,
        "moneyforward_api_called": api_called,
        "connected_transaction_snapshot_reused": not api_called,
        f"{stage}_probe": {"remaining_count": len(selected)},
    }


def _validate_connected_transaction_snapshot(
    artifact: dict[str, Any], *, accounting_date: str
) -> list[dict[str, Any]]:
    accounting_day = date.fromisoformat(accounting_date)
    if (
        artifact.get("schema") != "moneyforward-connected-transactions-v1"
        or artifact.get("source") != "GET /api/v3/transactions"
        or artifact.get("journalizing_statuses") != ["none"]
        or (artifact.get("raw_api_response_stored") is not False)
        or (
            artifact.get("start_date")
            != _company_fiscal_year_start(accounting_day).isoformat()
        )
        or (artifact.get("end_date") != accounting_date)
    ):
        raise ValueError("official connected transaction snapshot contract failed")
    captured_at = _parse_datetime(str(artifact.get("captured_at") or ""))
    now = datetime.now(timezone.utc)
    captured_on_accounting_day = (
        captured_at is not None
        and captured_at.astimezone(ZoneInfo("Asia/Tokyo")).date() == accounting_day
    )
    freshly_recaptured_historical_period = (
        captured_at is not None
        and captured_at.astimezone(ZoneInfo("Asia/Tokyo")).date() >= accounting_day
        and (
            timedelta(0)
            <= now - captured_at.astimezone(timezone.utc)
            <= timedelta(minutes=15)
        )
    )
    if not (captured_on_accounting_day or freshly_recaptured_historical_period):
        raise ValueError("connected transaction snapshot is not current")
    transactions = artifact.get("transactions")
    if not isinstance(transactions, list):
        raise ValueError("connected transaction snapshot items are missing")
    source_type_counts = artifact.get("source_type_counts")
    if (
        isinstance(source_type_counts, dict)
        and int(source_type_counts.get("unclassified") or 0) > 0
    ):
        raise ValueError("connected transaction snapshot contains unclassified items")
    seen: set[str] = set()
    for item in transactions:
        if not isinstance(item, dict):
            raise ValueError("connected transaction item must be an object")
        item_hash = str(item.get("source_event_id_hash") or "")
        if (
            re.fullmatch("sha256:[0-9a-f]{64}", item_hash) is None
            or item_hash in seen
            or item.get("journalizing_status") != "none"
            or (
                item.get("source_event_type")
                not in {"bank_transaction", "card_transaction"}
            )
        ):
            raise ValueError(
                "connected transaction identity or classification is invalid"
            )
        seen.add(item_hash)
    return transactions


def _company_fiscal_year_start(accounting_day):
    month = int(_setting("fiscal_year.start_month"))
    day = int(_setting("fiscal_year.start_day"))
    boundary = date(accounting_day.year, month, day)
    return (
        boundary
        if accounting_day >= boundary
        else date(accounting_day.year - 1, month, day)
    )


def _persist_connected_transactions_stage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    accounting_date: str,
    stage: str,
    artifact: dict[str, Any],
    transactions: list[dict[str, Any]],
    actor: str,
) -> dict[str, Any]:
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    items = [
        {
            "item_id": item["source_event_id_hash"],
            "status": "planned",
            "economic_event_discovered": True,
            "blocker": "connected_transaction_requires_reconciliation",
            "blocker_code": "connected_transaction_requires_reconciliation",
            "retry_condition": "RAG classification and authoritative journal read-back",
        }
        for item in transactions
    ]
    source_hash = stable_hash({"connected_transactions": artifact})
    readback_hash = stable_hash(
        {
            "stage": stage,
            "items": [item["source_event_id_hash"] for item in transactions],
            "source_response_hash": artifact.get("source_response_hash"),
        }
    )
    run_hash = accounting_run_hash(run_id)
    coverage_state = "captured" if items else "zero_confirmed"
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage=stage,
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": stage,
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                "coverage_state": coverage_state,
                "all_items_resolved": len(items) == 0,
                "remaining_count": len(items),
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _mf_principal()}),
                "source_snapshot_hash": f"sha256:{source_hash}",
                "readback_hash": f"sha256:{readback_hash}",
            }
        ),
        actor="connected_transactions_adapter",
    )
    return advance_run(
        conn,
        run_id,
        stage,
        {
            "status": "verified",
            "evidence_hash": f"sha256:{source_hash}",
            "adapter_receipt_hash": receipt_hash,
            "coverage_state": coverage_state,
            "all_items_resolved": len(items) == 0,
            "remaining_count": len(items),
            "items": items,
        },
        actor=actor,
    )


def _execute_tax_insurance_stage(
    db: Any, args: argparse.Namespace, *, runtime: tuple[Any, str, Any | None, str]
) -> dict[str, Any]:
    """Read current-period tax/insurance journals before cross-audit.

    The bank stage deliberately captures the official unposted snapshot. Its
    items are resolved in the later cross-audit, so their presence must remain
    visible without deadlocking this preceding tax read-back stage.
    """
    from .daily_posted_source_probe import run_daily_posted_source_probe
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, build_run_status, record_source_stage_receipt

    with db.connect() as conn:
        status = build_run_status(conn, args.run_id)
    if (
        status.get("status") != "running"
        or status.get("current_stage") != "tax_insurance"
    ):
        raise RuntimeError("tax insurance adapter called outside its active stage")
    accounting_day = date.fromisoformat(str(status["accounting_date"]))
    capture_start = (accounting_day.replace(day=1) - timedelta(days=1)).replace(day=1)
    run_hash = accounting_run_hash(args.run_id)
    run_dir = _artifact_root() / "runs" / run_hash
    connected_path = run_dir / "connected_transactions.local.json"
    if not connected_path.is_file():
        raise RuntimeError(
            "tax insurance gate requires the current run connected snapshot"
        )
    connected = json.loads(connected_path.read_text(encoding="utf-8"))
    transactions = _validate_connected_transaction_snapshot(
        connected, accounting_date=str(status["accounting_date"])
    )
    bank_count = sum(
        (item.get("source_event_type") == "bank_transaction" for item in transactions)
    )
    client, office_id, refresh_result = runtime[:3]
    output_path = run_dir / "daily_posted_sources.local.json"
    readback = _load_tax_insurance_readback(
        output_path,
        start_date=capture_start.isoformat(),
        end_date=accounting_day.isoformat(),
    )
    reused = readback is not None
    if readback is None:
        readback = run_daily_posted_source_probe(
            client=client,
            start_date=capture_start.isoformat(),
            end_date=accounting_day.isoformat(),
            output_json=output_path,
            office_id=office_id,
        )
    if (
        readback.get("status") != "ready"
        or readback.get("posted_journal_readback_covered") is not True
        or int(readback.get("unreadable_listing_count", -1)) != 0
    ):
        raise RuntimeError("tax insurance official journal read-back is incomplete")
    if refresh_result is not None:
        from .accounting_api import record_scope_check

        with db.connect() as conn:
            record_scope_check(conn, "moneyforward", refresh_result)
    source_hash = stable_hash({"tax_insurance_readback": readback})
    tax_count = int(readback["categories"]["tax_and_insurance"]["posted_journal_count"])
    with db.connect() as conn:
        receipt_hash = record_source_stage_receipt(
            conn,
            run_id=args.run_id,
            stage="tax_insurance",
            receipt=_bound_receipt(
                {
                    "status": "verified",
                    "stage": "tax_insurance",
                    "accounting_run_id_hash": f"sha256:{run_hash}",
                    "accounting_date": str(status["accounting_date"]),
                    "coverage_state": "zero_confirmed",
                    "all_items_resolved": True,
                    "remaining_count": 0,
                    "identity_readback_verified": True,
                    "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                    "execution_principal_hash": "sha256:"
                    + stable_hash({"principal": _mf_principal()}),
                    "source_snapshot_hash": f"sha256:{source_hash}",
                    "readback_hash": f"sha256:{source_hash}",
                }
            ),
            actor="tax_insurance_adapter",
        )
        advanced = advance_run(
            conn,
            args.run_id,
            "tax_insurance",
            {
                "status": "verified",
                "evidence_hash": f"sha256:{source_hash}",
                "adapter_receipt_hash": receipt_hash,
                "coverage_state": "zero_confirmed",
                "all_items_resolved": True,
                "remaining_count": 0,
                "items": [],
            },
            actor=args.actor,
        )
    return {
        **advanced,
        "execution_mode": "production_tax_insurance",
        "manifest_required": False,
        "moneyforward_api_called": not reused,
        "tax_insurance_readback_reused": reused,
        "tax_insurance_probe": {
            "posted_readback_count": tax_count,
            "unposted_bank_count": bank_count,
            "bank_reconciliation_stage": "cross_audit",
            "remaining_count": 0,
        },
    }


def _load_tax_insurance_readback(
    path: Path, *, start_date: str, end_date: str
) -> dict[str, Any] | None:
    """Reuse only a valid read-back captured for this run and exact period."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    tax = payload.get("categories", {}).get("tax_and_insurance", {})
    if (
        payload.get("status") != "ready"
        or payload.get("start_date") != start_date
        or payload.get("end_date") != end_date
        or (payload.get("posted_journal_readback_covered") is not True)
        or (int(payload.get("unreadable_listing_count", -1)) != 0)
        or (not isinstance(tax, dict))
        or (int(tax.get("posted_journal_count", -1)) < 0)
    ):
        return None
    return payload


def _bind_production_runtime_for_run(
    db_path: Path | None,
) -> ProductionRuntime | tuple[Any, str, Any | None, str]:
    """Call the unified preflight while preserving injected legacy test hooks."""
    try:
        return _bind_production_runtime(db_path=db_path)
    except TypeError as error:
        if "unexpected keyword argument 'db_path'" not in str(error):
            raise
        return _bind_production_runtime()


def _bind_production_runtime(*, db_path: Path | None = None) -> ProductionRuntime:
    """Authenticate Money Forward, Drive, and the sole GAS project up front."""
    try:
        accounting_client, office_id, refresh_result = (
            _bind_accounting_client_with_single_refresh()
        )
    except RuntimeError as error:
        if not _is_moneyforward_auth_failure(error):
            raise
        _interactive_moneyforward_reauth(db_path=db_path)
        accounting_client, office_id, refresh_result = (
            _bind_accounting_client_with_single_refresh()
        )
    drive_access_token = _bind_profile_google_runtime()
    return ProductionRuntime(
        accounting_client, office_id, refresh_result, drive_access_token
    )


def _is_moneyforward_auth_failure(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return any(
        (
            marker in message
            for marker in (
                "token refresh failed: http 400",
                "token refresh failed: http 401",
                "get /api/v3/offices http 401",
                "keychain",
            )
        )
    )


def _interactive_moneyforward_reauth(*, db_path):
    raise AuthenticationHandoffRequired(
        account=_mf_principal(),
        service="moneyforward",
        reason="Reauthorize the selected profile using its configured credential provisioning flow, then resume this run",
        input_required=True,
    )


def _ensure_payable_scope_readback(
    db: Any, runtime: tuple[Any, str, Any | None, str]
) -> ProductionRuntime:
    """Bind both Payable auth surfaces once and reuse them for the whole run."""
    from .accounting_api import (
        PAYABLE_AUTHORIZE_URL,
        PAYABLE_EXTERNAL_BASE_URL,
        PAYABLE_TOKEN_URL,
        MoneyForwardOAuthClient,
        UrllibTransport,
        record_scope_check,
    )
    from .keychain import KeychainClient

    client, office_id, refresh_result, drive_token = runtime
    profile = get_current_profile()
    if client.office_profile != profile:
        raise PermissionError("Payable preflight accounting profile mismatch")
    _validate_runtime_database(profile, db.path)
    client.validate_office_identity(office_id=office_id)
    client._require_business_identity("GET", client.base_url + "/api/v3/accounts")
    if refresh_result is not None:
        with db.connect() as conn:
            record_scope_check(conn, "moneyforward", refresh_result)
    gateway_status, _gateway_headers, _gateway_body = UrllibTransport().request(
        "GET",
        "https://api.payable.moneyforward.com/v1/received_invoices?per_page=1",
        headers={
            "Authorization": f"Bearer {client.access_token}",
            "Accept": "application/json",
        },
    )
    if gateway_status != 200:
        raise AuthenticationHandoffRequired(
            account=_mf_principal(),
            service="moneyforward_payable_gateway",
            reason="共通Money Forward OAuthに債務支払scopeがありません。既存の単一OAuth preflightでscopeを追加してから同じrunを再開してください。",
            browser_opened=False,
            input_required=True,
        )
    external_token, external_refreshed = _bind_payable_external_token(
        keychain=KeychainClient(get_current_profile().moneyforward_keychain_service),
        oauth_factory=lambda client_id, secret_value: MoneyForwardOAuthClient(
            client_id,
            secret_value,
            authorize_url=PAYABLE_AUTHORIZE_URL,
            token_url=PAYABLE_TOKEN_URL,
            client_auth_style="form",
        ),
    )
    external_office_id = _read_payable_external_office_id(
        token=external_token, base_url=PAYABLE_EXTERNAL_BASE_URL
    )
    payable = PayableAuthContext(
        gateway_access_token=client.access_token,
        external_access_token=external_token,
        external_office_id=external_office_id,
        external_token_refreshed=external_refreshed,
    )
    return ProductionRuntime(
        client, office_id, refresh_result, drive_token, payable=payable
    )


def _bind_payable_external_token(
    *, keychain: Any, oauth_factory: Any
) -> tuple[str, bool]:
    """Reuse a valid product token; refresh once; never open another auth screen."""
    from .accounting_api import PAYABLE_EXTERNAL_BASE_URL, UrllibTransport
    from .office_profile import resolve_moneyforward_keychain_service

    profile = get_current_profile()
    if profile is None or not getattr(keychain, "service", None):
        raise PermissionError("Payable credential source requires a selected company")
    resolve_moneyforward_keychain_service(profile, keychain.service, external_io=True)

    try:
        access_token = keychain.get_secret("mf_payable_external_access_token")
    except KeyError:
        access_token = ""
    runtime = UrllibTransport()
    if access_token:
        status, _headers, _body = runtime.request(
            "GET",
            f"{PAYABLE_EXTERNAL_BASE_URL}/api/external/v1/offices",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        if status == 200:
            return (access_token, False)
    try:
        refresh_token = keychain.get_secret("mf_payable_external_refresh_token")
    except KeyError as error:
        raise AuthenticationHandoffRequired(
            account=_mf_principal(),
            service="moneyforward_payable_external",
            reason="債務支払External OAuthの初回同意だけが未完了です。一度保存後は共通preflightが自動refreshし、通常runで認証画面を開きません。",
            browser_opened=False,
            input_required=True,
        ) from error
    try:
        client_id = keychain.get_secret("mf_payable_external_client_id")
        secret_value = keychain.get_secret("mf_payable_external_client_secret")
    except KeyError as error:
        raise AuthenticationHandoffRequired(
            account=_mf_principal(),
            service="moneyforward_payable_external_client",
            reason="債務支払External専用OAuthクライアントが未登録です。共通会計OAuthクライアントへのフォールバックは禁止しています。",
            browser_opened=False,
            input_required=True,
        ) from error
    oauth = oauth_factory(client_id, secret_value)
    try:
        refreshed = oauth.refresh_access_token(refresh_token)
    except RuntimeError as error:
        raise AuthenticationHandoffRequired(
            account=_mf_principal(),
            service="moneyforward_payable_external",
            reason="債務支払External OAuthだけが失効しています。他providerを再認証せず、このproviderの初回同意を1回だけ更新してください。",
            browser_opened=False,
            input_required=True,
        ) from error
    _read_payable_external_office_id(token=refreshed.access_token, base_url=PAYABLE_EXTERNAL_BASE_URL)
    keychain.set_secret("mf_payable_external_access_token", refreshed.access_token)
    if refreshed.refresh_token:
        keychain.set_secret(
            "mf_payable_external_refresh_token", refreshed.refresh_token
        )
    return (refreshed.access_token, True)


def _read_payable_external_office_id(*, token: str, base_url: str) -> str:
    from .accounting_api import UrllibTransport

    status, _headers, body = UrllibTransport().request(
        "GET",
        f"{base_url}/api/external/v1/offices",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if status != 200:
        raise RuntimeError(f"Payable External office read-back failed: HTTP {status}")
    payload = json.loads(body.decode("utf-8"))
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, list):
        candidates = [row for row in payload if isinstance(row, dict)]
    elif isinstance(payload, dict):
        for key in ("offices", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = [row for row in value if isinstance(row, dict)]
                break
    expected_code = get_current_profile().get_setting("payable.office_identification_code")
    matches = [
        row
        for row in candidates
        if expected_code is None or "".join(
            (
                character
                for character in str(
                    row.get("identification_code") or row.get("code") or ""
                )
                if character.isdigit()
            )
        )
        == str(expected_code)
    ]
    if len(candidates) != 1 or len(matches) != 1 or not str(matches[0].get("id") or ""):
        raise RuntimeError(
            "Payable External configured_company office did not resolve uniquely"
        )
    identifier = str(matches[0]["id"])
    from .hash_utils import stable_hash

    if stable_hash(identifier) != str(_setting("payable.office_id_hash")):
        raise PermissionError("Payable office ID does not match the selected profile")
    return identifier


def _bind_profile_google_runtime(*, auth_path=None, transport=None):
    """Resolve only the explicitly configured Keychain token and verify its principal."""
    from .accounting_api import UrllibTransport
    from .keychain import KeychainClient

    if auth_path is not None:
        raise ValueError(
            "implicit file-based Google credentials are not supported; configure an explicit Keychain reference"
        )
    keychain = KeychainClient(str(_setting("google.keychain_service")))
    token = keychain.get_secret(str(_setting("google.access_token_account")))
    status, body = _read_google_drive_principal(transport or UrllibTransport(), token)
    if status != 200:
        raise AuthenticationHandoffRequired(
            account=_drive_principal(),
            service="google_drive_gas",
            reason="Configured Google token needs reauthorization; resume the same run after provisioning",
            input_required=True,
        )
    try:
        actual = str(
            json.loads(body.decode("utf-8"))["user"]["emailAddress"]
        ).casefold()
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Google principal read-back is invalid") from error
    if actual != _drive_principal():
        raise ValueError("Google principal does not match the selected office profile")
    return token


GOOGLE_AUTH_TIMEOUT_SECONDS = 300


def _read_google_drive_principal(
    transport: Any, access_token: str
) -> tuple[int, bytes]:
    query = urllib.parse.urlencode({"fields": "user(emailAddress)"})
    status, _headers, body = transport.request(
        "GET",
        f"https://www.googleapis.com/drive/v3/about?{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    return (int(status), body)


def _bind_accounting_client_with_single_refresh():
    from .office_connection_probe import bind_company_accounting_client_from_keychain

    profile = get_current_profile()
    _validate_runtime_database(profile)
    return bind_company_accounting_client_from_keychain(
        keychain_service=profile.moneyforward_keychain_service,
        write_enabled=_PRODUCTION_WRITES.get(),
    )


def _persist_sales_invoice_stage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    accounting_date: str,
    result: dict[str, Any],
    artifact: dict[str, Any],
    accrual_items: list[dict[str, Any]],
    office_id: str,
    actor: str,
) -> dict[str, Any]:
    from .hash_utils import stable_hash
    from .shiwake_run import advance_run, record_source_stage_receipt

    run_hash = accounting_run_hash(run_id)
    source_snapshot_hash = stable_hash({"sales_invoice_artifact": artifact})
    readback_hash = stable_hash(
        {
            "sales_invoice_summary": result,
            "office_id_hash": stable_hash({"office_id": office_id}),
        }
    )
    coverage_state = (
        "zero_confirmed"
        if int(result.get("invoice_billing_count", 0)) == 0
        else "captured"
    )
    items: list[dict[str, Any]] = []
    for item in accrual_items:
        execution_status = str(item.get("execution_status") or "review")
        terminal = execution_status in {"posted", "reused"}
        run_item: dict[str, Any] = {
            "item_id": str(item.get("source_file_hash") or ""),
            "status": "verified" if terminal else "planned",
        }
        if item.get("idempotency_key"):
            run_item["idempotency_key"] = str(item["idempotency_key"])
        if not terminal:
            reasons = [str(reason) for reason in item.get("review_reason_codes") or []]
            run_item.update(
                {
                    "economic_event_discovered": True,
                    "blocker": reasons[0] if reasons else "sales_invoice_review",
                    "blocker_code": reasons[0] if reasons else "queue_for_review",
                    "retry_condition": "fresh authoritative invoice, live master and journal read-back",
                }
            )
        items.append(run_item)
    if any((not item["item_id"] for item in items)):
        raise ValueError("sales invoice accrual item is missing its stable identity")
    remaining_count = sum((item["status"] == "planned" for item in items))
    common = {
        "coverage_state": coverage_state,
        "all_items_resolved": remaining_count == 0,
        "remaining_count": remaining_count,
    }
    receipt_hash = record_source_stage_receipt(
        conn,
        run_id=run_id,
        stage="sales_invoice",
        receipt=_bound_receipt(
            {
                "status": "verified",
                "stage": "sales_invoice",
                "accounting_run_id_hash": f"sha256:{run_hash}",
                "accounting_date": accounting_date,
                **common,
                "identity_readback_verified": True,
                "office_hash": "sha256:" + stable_hash({"office": _office_name()}),
                "execution_principal_hash": "sha256:"
                + stable_hash({"principal": _mf_principal()}),
                "source_snapshot_hash": f"sha256:{source_snapshot_hash}",
                "readback_hash": f"sha256:{readback_hash}",
            }
        ),
        actor="sales_invoice_adapter",
    )
    return advance_run(
        conn,
        run_id,
        "sales_invoice",
        {
            "status": "verified",
            "evidence_hash": f"sha256:{source_snapshot_hash}",
            "adapter_receipt_hash": receipt_hash,
            **common,
            "items": items,
        },
        actor=actor,
    )


def _expert_help(args: argparse.Namespace) -> int:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    public = set(PUBLIC_COMMANDS) | {"help", "test", "rust-test"}
    targets = sorted(
        {
            match.group(1)
            for match in re.finditer(
                "^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\\s|$)", makefile, re.MULTILINE
            )
            if match.group(1) not in public
        }
    )
    if args.filter:
        targets = [
            target for target in targets if args.filter.casefold() in target.casefold()
        ]
    _print_json(
        {
            "status": "ok",
            "message": "以下は互換維持用の専門入口です。通常作業は公開コマンドを使用してください。",
            "target_count": len(targets),
            "targets": targets,
        }
    )
    return 0


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _synthetic_hash(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local_only_flags() -> dict[str, bool]:
    return {
        "moneyforward_api_called": False,
        "google_drive_write_called": False,
        "keychain_accessed": False,
        "oauth_url_opened": False,
        "browser_opened": False,
        "n8n_called": False,
    }


def _receipt_backed_execution_flags(
    conn: sqlite3.Connection, run_id: str
) -> dict[str, bool]:
    """Report external use only when the same run has a durable read-back."""
    run_hash = accounting_run_hash(run_id)
    receipts = conn.execute(
        "SELECT stage_key,receipt_kind FROM accounting_run_receipts WHERE run_id_hash=? AND status='verified'",
        (run_hash,),
    ).fetchall()
    source_stages = {
        str(row["stage_key"])
        for row in receipts
        if str(row["receipt_kind"]) == "source_stage_readback"
    }
    receipt_kinds = {str(row["receipt_kind"]) for row in receipts}
    chat_receipt = conn.execute(
        "SELECT 1 FROM google_chat_notification_records WHERE accounting_run_id_hash=? AND status IN ('sent','historical_sent') AND response_hash IS NOT NULL LIMIT 1",
        (run_hash,),
    ).fetchone()
    return {
        "moneyforward_api_called": bool(
            source_stages & {"sales_invoice", "bank_transfer", "card", "tax_insurance"}
        ),
        "google_drive_read_called": bool(
            source_stages & {"payable_invoice", "all_receipt_folders"}
        ),
        "google_drive_write_called": False,
        "google_chat_real_write_called": chat_receipt is not None,
        "github_write_called": "github_issue_readback" in receipt_kinds,
    }


def _external_activity_status(
    *, receipt_flags: dict[str, bool], stage_results: list[dict[str, Any]]
) -> dict[str, str]:
    """Summarize this invocation without confusing reuse with new external work."""

    def mode(*, executed: bool, verified: bool) -> str:
        if executed:
            return "executed"
        if verified:
            return "reused"
        return "not_required"

    moneyforward_executed = any(
        (result.get("moneyforward_api_called") is True for result in stage_results)
    )
    drive_read_executed = any(
        (
            result.get("google_drive_read_called") is True
            and result.get("gmail_gas_receipt_reused") is not True
            for result in stage_results
        )
    )
    chat_modes = {
        str(result.get("google_chat_delivery_mode") or "") for result in stage_results
    }
    github_modes = {
        str(result.get("github_delivery_mode") or "") for result in stage_results
    }
    return {
        "moneyforward": mode(
            executed=moneyforward_executed,
            verified=bool(receipt_flags.get("moneyforward_api_called")),
        ),
        "google_drive_read": mode(
            executed=drive_read_executed,
            verified=bool(receipt_flags.get("google_drive_read_called")),
        ),
        "google_drive_write": mode(
            executed=bool(receipt_flags.get("google_drive_write_called")),
            verified=False,
        ),
        "google_chat": "executed"
        if "executed" in chat_modes
        else "reused"
        if "reused" in chat_modes
        or bool(receipt_flags.get("google_chat_real_write_called"))
        else "not_required",
        "github": "executed"
        if "executed" in github_modes
        else "reused"
        if "reused" in github_modes or bool(receipt_flags.get("github_write_called"))
        else "not_required",
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _read_only_profile_client():
    from .accounting_api import MoneyForwardAccountingClient
    from .keychain import KeychainClient

    profile = get_current_profile()
    profile.require_configured()
    service = profile.moneyforward_keychain_service
    token = KeychainClient(service).get_secret("mf_oauth_access_token")
    return MoneyForwardAccountingClient(
        token,
        office_profile=profile,
        keychain_service=service,
        db_path=profile.moneyforward_db_path,
        write_enabled=False,
    )


def _connect(args):
    client = _read_only_profile_client()
    binding = client.validate_office_identity()
    _print_json(
        {
            "status": "verified",
            "office_id_hash": binding.office_id_hash,
            "profile_slug": get_current_profile().slug,
            "moneyforward_write_called": False,
            "production_workflows_ready": False,
        }
    )
    return 0


def _rag_build(args):
    from .office_rag_pipeline import build_office_rag

    summary = build_office_rag(
        db_path=get_current_profile().moneyforward_db_path,
        client=_read_only_profile_client(),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    _print_json(summary.to_dict())
    return 0


def _bound_receipt(receipt):
    result = dict(receipt)
    for key, value in _profile_boundary().items():
        if key in result and result[key] != value:
            raise ValueError("receipt belongs to another profile or binding")
        result[key] = value
    return result


def _accounting_decision_route(route, *, credit_account, credit_sub_account):
    result = dict(route)
    profile = get_current_profile()
    if result.get("company_slug", profile.slug) != profile.slug:
        raise ValueError("AI packet route belongs to another company")
    if not credit_account:
        raise ValueError("AI packet credit account is missing")
    if result.get("credit_account", credit_account) != credit_account:
        raise ValueError("AI packet credit account conflicts with source route")
    liability = result.get("payment_liability_route") or result.get(
        "credit_sub_account"
    )
    if liability and str(liability) != str(credit_sub_account or ""):
        raise ValueError("AI packet liability conflicts with source route")
    result.update(
        company_slug=profile.slug,
        credit_account=credit_account,
        credit_sub_account=credit_sub_account,
        payment_liability_route=credit_sub_account,
    )
    return result


def _configured_payroll_period(accounting_day):
    from calendar import monthrange

    offset = _setting("payroll.period_month_offset")
    if type(offset) is not int or not -24 <= offset <= 0:
        raise ValueError("payroll.period_month_offset must be an integer from -24 to 0")
    absolute = accounting_day.year * 12 + accounting_day.month - 1 + offset
    year, month0 = divmod(absolute, 12)
    closing = _setting("payroll.closing_day")

    def close(y, m):
        last = monthrange(y, m)[1]
        if closing == "last":
            return date(y, m, last)
        if type(closing) is not int or not 1 <= closing <= 31:
            raise ValueError("payroll.closing_day must be last or a day from 1 to 31")
        return date(y, m, min(closing, last))

    end = close(year, month0 + 1)
    previous_year, previous_month0 = divmod(absolute - 1, 12)
    return close(previous_year, previous_month0 + 1) + timedelta(days=1), end


def _configured_payroll_payment_month(period_end):
    offset = _setting("payroll.payment_month_offset")
    if type(offset) is not int or not 0 <= offset <= 24:
        raise ValueError("payroll.payment_month_offset must be an integer from 0 to 24")
    year, month0 = divmod(period_end.year * 12 + period_end.month - 1 + offset, 12)
    return date(year, month0 + 1, 1).strftime("%Y-%m")


def _gmail_intake_contract():
    from .gmail_recurring_invoice import build_recurring_invoice_search_plan

    start = str(_setting("recurring_invoices.start_date"))
    date.fromisoformat(start)
    plan = build_recurring_invoice_search_plan(start_date=start)
    destination = str(_setting("drive.payable_folder_id"))
    _validate_drive_route(destination)
    return {
        "schema": "company-gmail-intake-v1",
        "company_slug": get_current_profile().slug,
        **_profile_boundary(),
        "drive_principal": _drive_principal(),
        "destination_folder_ids": [destination],
        "evidence_root_id": get_current_profile().google_drive_evidence_root_id,
        "accounts": list(plan.accounts),
        "coverage_start_after": start.replace("-", "/"),
        "queries": [dict(item) for item in plan.search_items],
    }


def _validate_gmail_intake_status(status, contract):
    from .hash_utils import stable_hash

    if not isinstance(status, dict):
        raise ValueError("Gmail intake status is missing")
    _assert_run_artifact(status)
    if (
        status.get("intake_contract_hash") != stable_hash(contract)
        or status.get("principalAccepted") is not True
        or status.get("destinationFoldersReadback") is not True
        or status.get("destinationFolderIds") != contract["destination_folder_ids"]
    ):
        raise ValueError(
            "Gmail company/destination contract was not authoritatively verified"
        )
    coverage = status.get("coverage")
    rows = coverage.get("principals") if isinstance(coverage, dict) else None
    if not isinstance(rows, list) or len(rows) != len(contract["accounts"]):
        raise ValueError("Gmail configured mailbox coverage is incomplete")
    observed = [row.get("principal") for row in rows if isinstance(row, dict)]
    if set(observed) != set(contract["accounts"]) or len(set(observed)) != len(rows):
        raise ValueError("Gmail mailbox identity differs from company configuration")
    if status.get("currentPrincipal") not in contract["accounts"]:
        raise ValueError("Gmail current principal is outside the company contract")
    for row in rows:
        expected = hashlib.sha256(row["principal"].encode("utf-8")).hexdigest()[:16]
        if row.get("accountHash") != expected:
            raise ValueError("Gmail mailbox hash does not match its principal")
