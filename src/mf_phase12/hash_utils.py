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
