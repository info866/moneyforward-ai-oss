from __future__ import annotations

from .models import Evidence


DRIVE = "drive"
INVOICE_API = "invoice_api"
LOCAL_FILE = "local_file"
EVIDENCE_SOURCE_TYPES = (DRIVE, INVOICE_API, LOCAL_FILE)


def evidence_source_type(evidence: Evidence) -> str:
    return evidence_source_type_from_id(evidence.google_drive_file_id)


def evidence_source_type_from_id(source_id: str) -> str:
    normalized = str(source_id or "").strip().lower()
    if normalized.startswith("invoice-api:"):
        return INVOICE_API
    if normalized.startswith(("local:", "local-sync:")):
        return LOCAL_FILE
    return DRIVE
