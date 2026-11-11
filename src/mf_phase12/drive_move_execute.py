from __future__ import annotations

import json
import sqlite3
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .accounting_api import Transport, UrllibTransport
from .hash_utils import stable_hash
from .office_profile import (
    resolve_office_profile, require_google_drive_evidence_source_folder,
    require_google_drive_processed_folder,
)


GOOGLE_DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3"


class DriveMoveClient(Protocol):
    def move_file(self, *, file_id: str, source_folder_id: str, destination_folder_id: str) -> dict[str, Any]: ...

    def get_file(self, *, file_id: str) -> dict[str, Any]: ...


class _DriveBoundTransport:
    def __init__(self, client, delegate):
        self.client = client
        self.delegate = delegate

    def request(self, method, url, **kwargs):
        client = self.client
        profile = client.office_profile
        profile.require_configured()
        authorizations = [v for k, v in (kwargs.get('headers') or {}).items() if str(k).lower() == 'authorization']
        if authorizations != [f'Bearer {client.access_token}']:
            raise PermissionError('Drive request credential differs from the bound client')
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(client.base_url)
        if parsed.fragment or parsed.username or parsed.password or kwargs.get('body') is not None:
            raise PermissionError('Drive operation must be a canonical metadata read or parent-only move')
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise PermissionError('Drive request origin differs from configured service')
        if method == 'GET' and parsed.path == base.path + '/about':
            return self.delegate.request(method, url, **kwargs)
        prefix = base.path + '/files/'
        if not parsed.path.startswith(prefix):
            raise PermissionError('Unsupported Drive operation')
        file_id = urllib.parse.unquote(parsed.path[len(prefix):])
        if re.fullmatch(r'[A-Za-z0-9_-]+', file_id) is None:
            raise PermissionError('Drive file identifier is not canonical')
        state = (client.access_token, client.office_profile, client.write_enabled, client.base_url)
        if method == 'PATCH':
            if not client.write_enabled:
                raise PermissionError('Drive connection is read-only')
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if (set(query) != {'addParents', 'removeParents', 'fields', 'supportsAllDrives'}
                    or any(len(values) != 1 for values in query.values())
                    or query['fields'] != ['id,name,parents'] or query['supportsAllDrives'] != ['true']):
                raise PermissionError('Drive parent move parameters must be exact and unique')
            source = query.get('removeParents', [''])[0]
            destination = query.get('addParents', [''])[0]
            require_google_drive_evidence_source_folder(profile, source)
            require_google_drive_processed_folder(profile, destination)
            if client.get_file(file_id=file_id).get('parents') != [source]:
                raise PermissionError('Drive file has an ambiguous source route')
        elif method == 'GET':
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {'fields', 'supportsAllDrives', 'includeItemsFromAllDrives'}:
                raise PermissionError('Only bounded Drive metadata reads are supported')
            client._verify_actor()
        else:
            raise PermissionError('Unsupported Drive operation')
        if state != (client.access_token, client.office_profile, client.write_enabled, client.base_url):
            raise PermissionError('Drive session changed during identity verification')
        response = self.delegate.request(method, url, **kwargs)
        if method == 'GET' and response[0] == 200:
            result = json.loads(response[2])
            permitted = {*profile.google_drive_evidence_source_folder_ids, profile.google_drive_processed_folder_id}
            parents = result.get('parents', [])
            if result.get('id') != file_id or len(parents) != 1 or parents[0] not in permitted:
                raise PermissionError('Drive file parents are outside or ambiguous for this company')
        return response


class GoogleDriveApiMoveClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = GOOGLE_DRIVE_BASE_URL,
        transport: Transport | None = None,
        office_profile=None,
        write_enabled: bool = False,
    ):
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.office_profile = resolve_office_profile(office_profile, required=True)
        self.write_enabled = write_enabled
        self.transport = _DriveBoundTransport(self, transport or UrllibTransport())

    def _verify_actor(self) -> None:
        self.office_profile.require_configured()
        status, _, body = self.transport.request("GET", f"{self.base_url}/about?fields=user(emailAddress)",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"})
        if status != 200 or json.loads(body).get("user", {}).get("emailAddress") != self.office_profile.google_drive_account:
            raise PermissionError("Authenticated Drive account differs from this company profile")

    def move_file(self, *, file_id: str, source_folder_id: str, destination_folder_id: str) -> dict[str, Any]:
        require_google_drive_evidence_source_folder(self.office_profile, source_folder_id)
        require_google_drive_processed_folder(self.office_profile, destination_folder_id)
        if not self.write_enabled:
            raise PermissionError("Drive client is read-only without explicit run authorization")
        query = urllib.parse.urlencode(
            {
                "addParents": destination_folder_id,
                "removeParents": source_folder_id,
                "fields": "id,name,parents",
                "supportsAllDrives": "true",
            }
        )
        url = f"{self.base_url}/files/{urllib.parse.quote(file_id, safe='')}?{query}"
        status, _headers, body = self.transport.request(
            "PATCH",
            url,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        if status not in {200, 201}:
            detail = ""
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
                error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(error, dict):
                    detail = "; error_reason=" + ",".join(
                        str(item.get("reason") or "")
                        for item in error.get("errors") or []
                        if isinstance(item, dict) and item.get("reason")
                    )
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise RuntimeError(f"Google Drive move failed: HTTP {status}{detail}")
        return json.loads(body.decode("utf-8")) if body else {}

    def get_file(self, *, file_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "fields": "id,name,parents,size,sha256Checksum",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
        )
        url = f"{self.base_url}/files/{urllib.parse.quote(file_id, safe='')}?{query}"
        status, _headers, body = self.transport.request(
            "GET",
            url,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"Google Drive file read-back failed: HTTP {status}")
        result = json.loads(body.decode("utf-8")) if body else {}
        permitted = set(self.office_profile.google_drive_evidence_source_folder_ids)
        permitted.add(self.office_profile.google_drive_processed_folder_id)
        if len(result.get("parents", [])) != 1 or result["parents"][0] not in permitted:
            raise PermissionError("Drive file metadata is outside the configured company routes")
        return result


@dataclass(frozen=True)
class DriveMoveDryRun:
    operation_type: str
    ready_to_execute: bool
    executed: bool
    request_hash: str
    target_hash: str
    move_request_count: int

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "ready_to_execute": self.ready_to_execute,
            "executed": self.executed,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "move_request_count": self.move_request_count,
            "google_drive_api_called": False,
            "raw_drive_file_ids_stored": False,
            "raw_secret_stored": False,
        }


@dataclass(frozen=True)
class DriveMoveExecutionResult:
    operation_type: str
    executed: bool
    approval_hash: str
    request_hash: str
    target_hash: str
    moved_count: int
    response_hash: str
    google_drive_api_called: bool

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "executed": self.executed,
            "approval_hash": self.approval_hash,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "moved_count": self.moved_count,
            "response_hash": self.response_hash,
            "google_drive_api_called": self.google_drive_api_called,
            "raw_drive_file_ids_stored": False,
            "raw_secret_stored": False,
            "raw_api_response_stored": False,
        }


@dataclass(frozen=True)
class DriveMoveReadBackVerification:
    operation_type: str
    verified: bool
    approval_hash: str
    request_hash: str
    target_hash: str
    moved_count: int
    verified_count: int
    response_hash: str
    google_drive_api_called: bool

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "verified": self.verified,
            "approval_hash": self.approval_hash,
            "request_hash": self.request_hash,
            "target_hash": self.target_hash,
            "moved_count": self.moved_count,
            "verified_count": self.verified_count,
            "response_hash": self.response_hash,
            "google_drive_api_called": self.google_drive_api_called,
            "raw_drive_file_ids_stored": False,
            "raw_secret_stored": False,
            "raw_api_response_stored": False,
        }


def load_drive_move_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Drive move plan must be a JSON object")
    if payload.get("operation") != "google_drive_files_move":
        raise ValueError("Drive move plan operation must be google_drive_files_move")
    return payload


def dry_run_drive_move_execution(move_plan: dict[str, Any]) -> DriveMoveDryRun:
    requests = _validated_move_requests(move_plan)
    redacted = [_redact_request(request) for request in requests]
    return DriveMoveDryRun(
        operation_type="google_drive_files_move",
        ready_to_execute=True,
        executed=False,
        request_hash=stable_hash({"operation": "google_drive_files_move", "requests": redacted}),
        target_hash=stable_hash(
            [
                {
                    "source_folder_id_hash": stable_hash(request["source_folder_id"]),
                    "destination_folder_id_hash": stable_hash(request["destination_folder_id"]),
                }
                for request in requests
            ]
        ),
        move_request_count=len(requests),
    )


def drive_move_dry_run_hash(dry_run: DriveMoveDryRun) -> str:
    return stable_hash(dry_run.to_redacted_dict())


def execute_drive_move_with_client(
    *,
    client: DriveMoveClient,
    move_plan: dict[str, Any],
    dry_run: DriveMoveDryRun,
    execution_approval: dict[str, Any],
) -> DriveMoveExecutionResult:
    _require(dry_run.ready_to_execute is True, "dry_run must be ready_to_execute")
    _require(dry_run.executed is False, "dry_run must not already be executed")
    _require_drive_move_execution_approval(execution_approval, dry_run)
    requests = _validated_move_requests(move_plan)
    immediate = dry_run_drive_move_execution(move_plan)
    _require(drive_move_dry_run_hash(immediate) == drive_move_dry_run_hash(dry_run), "immediate dry_run_hash mismatch")
    responses = [
        client.move_file(
            file_id=request["file_id"],
            source_folder_id=request["source_folder_id"],
            destination_folder_id=request["destination_folder_id"],
        )
        for request in requests
    ]
    return DriveMoveExecutionResult(
        operation_type="google_drive_files_move",
        executed=True,
        approval_hash=str(execution_approval["approval_hash"]),
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        moved_count=len(responses),
        response_hash=stable_hash([_redact_response(response) for response in responses]),
        google_drive_api_called=True,
    )


def verify_drive_move_read_back(
    *,
    client: DriveMoveClient,
    move_plan: dict[str, Any],
    execution_result: dict[str, Any],
) -> DriveMoveReadBackVerification:
    requests = _validated_move_requests(move_plan)
    dry_run = dry_run_drive_move_execution(move_plan)
    _require(execution_result.get("operation_type") == "google_drive_files_move", "execution result operation mismatch")
    _require(execution_result.get("executed") is True, "execution result must be executed")
    _require(bool(execution_result.get("approval_hash")), "execution result approval_hash is required")
    _require(execution_result.get("request_hash") == dry_run.request_hash, "execution result request_hash mismatch")
    _require(execution_result.get("target_hash") == dry_run.target_hash, "execution result target_hash mismatch")
    _require(int(execution_result.get("moved_count") or -1) == len(requests), "execution moved_count mismatch")
    read_backs = []
    verified_count = 0
    for request in requests:
        response = client.get_file(file_id=request["file_id"])
        parents = [str(parent) for parent in response.get("parents") or []]
        destination_present = request["destination_folder_id"] in parents
        source_removed = request["source_folder_id"] not in parents
        if destination_present and source_removed:
            verified_count += 1
        read_backs.append(
            {
                "file_id_hash": stable_hash(request["file_id"]),
                "destination_present": destination_present,
                "source_removed": source_removed,
                "parents_hash": stable_hash(parents),
            }
        )
    return DriveMoveReadBackVerification(
        operation_type="google_drive_files_move_read_back",
        verified=verified_count == len(requests),
        approval_hash=str(execution_result["approval_hash"]),
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        moved_count=len(requests),
        verified_count=verified_count,
        response_hash=stable_hash(read_backs),
        google_drive_api_called=True,
    )


def record_observed_drive_move(
    conn: sqlite3.Connection,
    *,
    move_plan: dict[str, Any],
    execution_approval: dict[str, Any],
    metadata_bundle: dict[str, Any],
    execution_channel: str,
) -> tuple[DriveMoveExecutionResult, DriveMoveReadBackVerification]:
    """Record a move performed by an approved Drive connector or sync client.

    Some authenticated Drive surfaces perform the write outside this process.
    The move is only accepted when a fresh metadata bundle proves every planned
    file has the destination parent and no longer has the source parent.
    """
    allowed_channels = {"google_drive_api", "google_drive_connector", "google_drive_sync_client"}
    _require(execution_channel in allowed_channels, "unsupported Drive execution channel")
    requests = _validated_move_requests(move_plan)
    dry_run = dry_run_drive_move_execution(move_plan)
    _require_drive_move_execution_approval(execution_approval, dry_run)
    items = metadata_bundle.get("items")
    _require(isinstance(items, list), "metadata bundle items must be a list")
    by_file_id: dict[str, dict[str, Any]] = {}
    for item in items:
        _require(isinstance(item, dict), "metadata bundle item must be an object")
        file_id = str(item.get("id") or item.get("fileId") or "")
        _require(bool(file_id), "metadata bundle item id is required")
        _require(file_id not in by_file_id, "metadata bundle contains a duplicate file id")
        by_file_id[file_id] = item
    _require(set(by_file_id) == {request["file_id"] for request in requests}, "metadata bundle file set mismatch")

    observations: list[dict[str, Any]] = []
    for request in requests:
        item = by_file_id[request["file_id"]]
        name = str(item.get("name") or item.get("title") or "")
        parents_value = item.get("parent_ids") or item.get("parents")
        _require(name == request["file_name"], "metadata file name does not match the approved move plan")
        _require(isinstance(parents_value, list), "metadata parents must be a list")
        parents = [str(parent) for parent in parents_value if str(parent)]
        destination_present = request["destination_folder_id"] in parents
        source_removed = request["source_folder_id"] not in parents
        _require(destination_present and source_removed, "Drive move read-back did not match the approved parent change")
        observations.append(
            {
                "file_id_hash": stable_hash(request["file_id"]),
                "file_name_hash": stable_hash(name),
                "destination_present": destination_present,
                "source_removed": source_removed,
                "parents_hash": stable_hash(sorted(parents)),
            }
        )

    execution = DriveMoveExecutionResult(
        operation_type="google_drive_files_move",
        executed=True,
        approval_hash=str(execution_approval["approval_hash"]),
        request_hash=dry_run.request_hash,
        target_hash=dry_run.target_hash,
        moved_count=len(requests),
        response_hash=stable_hash({"execution_channel": execution_channel, "observations": observations}),
        google_drive_api_called=True,
    )
    verification = DriveMoveReadBackVerification(
        operation_type="google_drive_files_move_read_back",
        verified=True,
        approval_hash=execution.approval_hash,
        request_hash=execution.request_hash,
        target_hash=execution.target_hash,
        moved_count=len(requests),
        verified_count=len(requests),
        response_hash=stable_hash({"read_back": observations}),
        google_drive_api_called=True,
    )
    record_drive_move_execution(
        conn,
        dry_run=dry_run,
        execution_approval=execution_approval,
        result=execution,
        move_plan=move_plan,
    )
    record_drive_move_readback_verification(conn, verification)
    return execution, verification


def build_drive_move_execution_approval_template(dry_run: DriveMoveDryRun) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "operation_type": "google_drive_files_move",
        "approval_hash": stable_hash(
            {
                "operation_type": "google_drive_files_move",
                "request_hash": dry_run.request_hash,
                "target_hash": dry_run.target_hash,
                "dry_run_hash": drive_move_dry_run_hash(dry_run),
            }
        ),
        "request_hash": dry_run.request_hash,
        "target_hash": dry_run.target_hash,
        "dry_run_hash": drive_move_dry_run_hash(dry_run),
        "move_request_count": dry_run.move_request_count,
        "user_approval": "",
        "expected_api_action": (
            "PATCH https://www.googleapis.com/drive/v3/files/{fileId}"
            "?addParents={destination_folder_id}&removeParents={source_folder_id}"
        ),
        "rollback_method": "Move each file back to its original source folder only after separate explicit approval.",
        "approved_at": now,
        "execution_confirmation": {
            "operation_type": "google_drive_files_move",
            "request_hash": dry_run.request_hash,
            "target_hash": dry_run.target_hash,
            "dry_run_hash": drive_move_dry_run_hash(dry_run),
            "execute_approved": False,
            "google_drive_move_allowed": False,
            "rename_allowed": False,
            "rollback_requires_separate_approval": True,
        },
    }


def _require_drive_move_execution_approval(approval: dict[str, Any], dry_run: DriveMoveDryRun) -> None:
    _require(approval.get("operation_type") == "google_drive_files_move", "approval operation_type mismatch")
    _require(approval.get("approval_hash") == build_drive_move_execution_approval_template(dry_run)["approval_hash"], "approval approval_hash mismatch")
    _require(approval.get("request_hash") == dry_run.request_hash, "approval request_hash mismatch")
    _require(approval.get("target_hash") == dry_run.target_hash, "approval target_hash mismatch")
    _require(approval.get("dry_run_hash") == drive_move_dry_run_hash(dry_run), "approval dry_run_hash mismatch")
    _require(bool(str(approval.get("user_approval") or "").strip()), "approval user_approval is required")
    _require("separate explicit approval" in str(approval.get("rollback_method") or ""), "rollback must require separate explicit approval")
    confirmation = approval.get("execution_confirmation")
    _require(isinstance(confirmation, dict), "execution_confirmation is required")
    _require(confirmation.get("execute_approved") is True, "execution_confirmation execute_approved must be true")
    _require(confirmation.get("google_drive_move_allowed") is True, "google_drive_move_allowed must be true")
    _require(confirmation.get("rename_allowed") is False, "rename_allowed must be false")
    _require(confirmation.get("rollback_requires_separate_approval") is True, "rollback_requires_separate_approval must be true")


def record_drive_move_execution(
    conn: sqlite3.Connection,
    *,
    dry_run: DriveMoveDryRun,
    execution_approval: dict[str, Any],
    result: DriveMoveExecutionResult,
    move_plan: dict[str, Any] | None = None,
) -> str:
    _require_drive_move_execution_approval(execution_approval, dry_run)
    _require(result.executed is True, "result must be executed")
    _require(result.approval_hash == execution_approval["approval_hash"], "result approval_hash mismatch")
    conn.execute(
        """
        INSERT OR REPLACE INTO drive_move_execution_records (
            approval_hash, request_hash, target_hash, dry_run_hash,
            expected_api_action_hash, rollback_plan_hash, user_approval_hash,
            moved_count, status, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'executed', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (
            result.approval_hash,
            result.request_hash,
            result.target_hash,
            drive_move_dry_run_hash(dry_run),
            stable_hash(execution_approval.get("expected_api_action") or ""),
            stable_hash(execution_approval.get("rollback_method") or ""),
            stable_hash(execution_approval.get("user_approval") or ""),
            result.moved_count,
        ),
    )
    if move_plan is not None:
        _record_drive_move_chain_items(conn, result.approval_hash, move_plan, result)
    conn.commit()
    return result.approval_hash


def record_drive_move_readback_verification(
    conn: sqlite3.Connection,
    verification: DriveMoveReadBackVerification,
) -> str:
    verification_hash = stable_hash(verification.to_redacted_dict())
    conn.execute(
        """
        INSERT OR REPLACE INTO drive_move_readback_verifications (
            verification_hash, approval_hash, request_hash, target_hash,
            expected_moved_count, verified_count, response_hash, passed,
            diff_summary_redacted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_hash,
            verification.approval_hash,
            verification.request_hash,
            verification.target_hash,
            verification.moved_count,
            verification.verified_count,
            verification.response_hash,
            1 if verification.verified else 0,
            "一致" if verification.verified else "parents mismatch",
        ),
    )
    if verification.verified:
        conn.execute(
            """
            UPDATE drive_move_execution_records
            SET status = 'verified',
                verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE approval_hash = ?
            """,
            (verification.approval_hash,),
        )
    conn.commit()
    return verification_hash


def _record_drive_move_chain_items(
    conn: sqlite3.Connection,
    approval_hash: str,
    move_plan: dict[str, Any],
    result: DriveMoveExecutionResult,
) -> None:
    for request in _validated_move_requests(move_plan):
        idempotency_key = request.get("idempotency_key")
        if not idempotency_key:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO drive_move_chain_items (
                approval_hash, idempotency_key, request_hash, target_hash
            ) VALUES (?, ?, ?, ?)
            """,
            (
                approval_hash,
                idempotency_key,
                result.request_hash,
                result.target_hash,
            ),
        )


def _validated_move_requests(move_plan: dict[str, Any]) -> list[dict[str, str]]:
    requests = move_plan.get("move_requests")
    if not isinstance(requests, list):
        raise ValueError("move_requests must be a list")
    validated: list[dict[str, str]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            raise ValueError(f"move_requests[{index}] must be an object")
        normalized = {
            key: str(request.get(key) or "")
            for key in ("file_id", "source_folder_id", "destination_folder_id", "file_name", "idempotency_key")
        }
        # Move plans are JSON, so the natural representation is boolean false.
        # Accept it as well as the legacy string to prevent an otherwise valid
        # no-rename move from failing before the official API call.
        normalized["rename"] = str(request.get("rename")).lower()
        for field in ("file_id", "source_folder_id", "destination_folder_id", "file_name"):
            if not normalized[field]:
                raise ValueError(f"move_requests[{index}] {field} is required")
        if normalized["rename"] != "false":
            raise ValueError(f"move_requests[{index}] rename must be false")
        validated.append(normalized)
    return validated


def _redact_request(request: dict[str, str]) -> dict[str, str]:
    redacted = {
        "file_id_hash": stable_hash(request["file_id"]),
        "source_folder_id_hash": stable_hash(request["source_folder_id"]),
        "destination_folder_id_hash": stable_hash(request["destination_folder_id"]),
        "file_name": request["file_name"],
        "rename": request["rename"],
    }
    if request.get("idempotency_key"):
        redacted["idempotency_key_hash"] = stable_hash(request["idempotency_key"])
    return redacted


def _redact_response(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(response.keys()),
        "id_hash": stable_hash(str(response.get("id") or "")),
        "parents_hash": stable_hash(response.get("parents") or []),
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
