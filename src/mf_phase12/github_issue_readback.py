from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

from .hash_utils import accounting_run_hash, sha256_text, stable_hash
from .shiwake_run import _record_verified_run_receipt
from .office_profile import get_setting


_API_ORIGIN = "https://api.github.com"
_WEB_ORIGIN = "https://github.com"
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$", re.IGNORECASE)


def accounting_run_marker(run_id: str) -> str:
    """Return the exact machine marker required in the durable Issue comment."""

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    run_hash = accounting_run_hash(run_id)
    return f"<!-- accounting_run_id_hash: sha256:{run_hash} -->"


def record_github_issue_readback(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    issue_number: int,
    expected_body_hash: str,
    response: Mapping[str, Any],
    actor: str = "github-issue-readback-adapter",
) -> str:
    """Validate a GitHub REST comment read-back, then persist its run receipt."""

    repository = get_setting("reporting.github_repository")
    if (not isinstance(repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
            or any(part in {".", ".."} for part in repository.split("/"))):
        raise ValueError("Company reporting.github_repository must be configured")

    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    if not isinstance(response, Mapping):
        raise ValueError("GitHub Issue read-back response must be an object")

    body = response.get("body")
    if not isinstance(body, str):
        raise ValueError("GitHub Issue read-back body is required")
    body_hash = sha256_text(body)
    if body_hash != _parse_hash(expected_body_hash, "expected_body_hash"):
        raise ValueError("GitHub Issue comment body hash does not match the expected exact body")

    marker = accounting_run_marker(run_id)
    if marker not in body:
        raise ValueError("GitHub Issue comment is missing the exact accounting run hash marker")

    comment_id = response.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
        raise ValueError("GitHub Issue comment id must be a positive integer")

    issue_url = _required_text(response.get("issue_url"), "issue_url")
    comment_url = _required_text(response.get("url"), "comment url")
    html_url = _required_text(response.get("html_url"), "comment html_url")
    expected_issue_url = f"{_API_ORIGIN}/repos/{repository}/issues/{issue_number}"
    expected_comment_url = f"{_API_ORIGIN}/repos/{repository}/issues/comments/{comment_id}"
    expected_html_url = (
        f"{_WEB_ORIGIN}/{repository}/issues/{issue_number}"
        f"#issuecomment-{comment_id}"
    )
    if issue_url != expected_issue_url:
        raise ValueError("GitHub Issue read-back repository or issue number does not match")
    if comment_url != expected_comment_url:
        raise ValueError("GitHub Issue comment URL or id does not match")
    if html_url != expected_html_url:
        raise ValueError("GitHub Issue HTML URL, issue number, or id does not match")
    _require_https_url(comment_url, hostname="api.github.com")
    _require_https_url(html_url, hostname="github.com")

    created_at = _parse_created_at(response.get("created_at"))
    run_hash = accounting_run_hash(run_id)
    source_hash = stable_hash(
        {
            "repository": repository,
            "issue_number": issue_number,
            "accounting_run_id_hash": run_hash,
            "body_hash": body_hash,
            "comment_id": comment_id,
            "comment_url": comment_url,
            "html_url": html_url,
            "created_at": created_at,
        }
    )
    return _record_verified_run_receipt(
        conn,
        run_id=run_id,
        stage="issue_update",
        receipt_kind="github_issue_readback",
        source_hash=f"sha256:{source_hash}",
        actor=actor,
    )


def _parse_hash(value: Any, label: str) -> str:
    match = _SHA256_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise ValueError(f"{label} must be sha256:<64 hex>")
    return match.group(1).lower()


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _require_https_url(value: str, *, hostname: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != hostname or parsed.username or parsed.password:
        raise ValueError(f"GitHub URL must use exact HTTPS host {hostname}")


def _parse_created_at(value: Any) -> str:
    text = _required_text(value, "created_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    return parsed.isoformat()
