from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_hash(data: Any) -> str:
    return sha256_text(canonical_json(data))


def accounting_run_hash(run_reference: str) -> str:
    """Resolve a raw run ID or the durable ``sha256:<digest>`` run reference."""

    text = str(run_reference or "").strip()
    if not text:
        raise ValueError("run_id is required")
    if text.startswith("sha256:") and _SHA256_RE.fullmatch(text.removeprefix("sha256:")):
        return text.removeprefix("sha256:").lower()
    return stable_hash({"accounting_run_id": text})


def mask_identifier(value: str) -> str:
    if not value:
        return ""
    digest = sha256_text(value)
    return f"sha256:{digest}"


def normalize_sha256_hash(value: str) -> str:
    """Return the canonical marker for a real SHA-256, preserving test/legacy tokens.

    Evidence facts historically used the bare 64-hex digest while Drive manifests
    used ``sha256:<digest>``.  Normalizing only syntactically valid SHA-256 values
    avoids changing legacy opaque hashes while giving the processing ledger one
    stable identity for real evidence files.
    """

    text = str(value or "").strip()
    digest = text.removeprefix("sha256:")
    if _SHA256_RE.fullmatch(digest):
        return f"sha256:{digest.lower()}"
    return text


def sha256_hash_variants(value: str) -> tuple[str, ...]:
    """Return canonical and legacy spellings used to read an existing ledger."""

    normalized = normalize_sha256_hash(value)
    if normalized.startswith("sha256:") and _SHA256_RE.fullmatch(normalized.removeprefix("sha256:")):
        return (normalized, normalized.removeprefix("sha256:"))
    return (normalized,)


def source_tree_fingerprint(root) -> str:
    """Identify distributed implementation bytes without requiring Git history.

    Only portable code/schema is read. Local company profiles, credentials,
    runtime databases and absolute paths are never part of this identity.
    This is provenance, not business authorization or an integrity signature.
    """
    from pathlib import Path

    root = Path(root).resolve(strict=True)
    entries = []
    for directory, suffixes in (("src", {".py"}), ("sql", {".sql"}),
                                ("mcp_servers", {".py"})):
        base = root / directory
        if base.is_symlink():
            raise ValueError("implementation tree must not be symlinked")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in suffixes or "__pycache__" in path.parts:
                continue
            if path.is_symlink() or any(parent.is_symlink()
                                       for parent in path.parents if parent != root and root in parent.parents):
                raise ValueError("implementation source must not be symlinked")
            if path.is_file():
                entries.append({"path": path.relative_to(root).as_posix(),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not entries:
        raise ValueError("implementation source tree is missing")
    return "source-sha256:" + stable_hash(entries)
