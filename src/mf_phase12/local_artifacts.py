from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LOCAL_ARTIFACT_BASE = ROOT / "data" / "phase12"


def require_ignored_untracked_data_path(path: Path, *, purpose: str) -> Path:
    from .office_profile import resolve_office_profile
    profile = resolve_office_profile(required=True)
    profile.require_configured()
    resolved = path.resolve()
    base = (profile.repository_root / "data" / "phase12" / "offices" / profile.slug).resolve()
    depth = len(resolved.parts) - len(base.parts)
    ancestor = resolved
    for _ in range(max(0, depth)):
        ancestor = ancestor.parent
    try:
        inside_base = depth >= 0 and ancestor.samefile(base)
    except OSError:
        inside_base = False
    if not inside_base:
        raise SystemExit(f"{purpose} must stay under the selected company's ignored data directory")
    relative = Path(*resolved.parts[len(ROOT.resolve().parts) :])
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", str(relative)],
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked.stdout.strip():
        raise SystemExit(f"{purpose} must be untracked")
    ignored = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "--", str(relative)],
        check=False,
    )
    if ignored.returncode != 0:
        raise SystemExit(f"{purpose} must be ignored by git")
    return path


def load_journal_id(raw_journal_id: str | None, journal_id_json: Path | None, *, allow_raw: bool = False) -> str:
    if raw_journal_id and journal_id_json:
        raise SystemExit("use either --journal-id or --journal-id-json, not both")
    if journal_id_json:
        require_ignored_untracked_data_path(journal_id_json, purpose="journal_id input")
        payload: Any = json.loads(Path(journal_id_json).read_text(encoding="utf-8"))
        value = payload.get("journal_id") if isinstance(payload, dict) else payload
        if not isinstance(value, str) or not value:
            raise SystemExit("journal_id JSON must contain a non-empty string or {\"journal_id\": \"...\"}")
        return value
    if raw_journal_id:
        if not allow_raw:
            raise SystemExit("raw --journal-id is refused; use --journal-id-json under ignored data/phase12/")
        return raw_journal_id
    raise SystemExit("expected --journal-id-json or --journal-id")
