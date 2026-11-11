from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUST_CORE_BIN_ENV = "MF_PHASE12_RUST_CORE_BIN"


def rust_core_command() -> list[str]:
    release_binary = ROOT / "target" / "release" / "mf_phase12_core"
    if (ROOT / "Cargo.toml").is_file() and release_binary.is_file():
        return [str(release_binary)]
    raise FileNotFoundError("No reviewed Rust classifier is bundled; use the Python classifier")


def rust_core_available() -> bool:
    try:
        run_rust_core(["mask-identifier", "probe"], expect_json=False)
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError):
        return False
    return True


def run_rust_core(args: list[str], payload: Any | None = None, *, expect_json: bool = True) -> Any:
    input_text = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    result = subprocess.run(
        [*rust_core_command(), *args],
        input=input_text,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "mf_phase12_core failed")
    stdout = result.stdout.strip()
    if expect_json:
        return json.loads(stdout)
    return stdout


def stable_hash_via_rust(payload: Any) -> str:
    return str(run_rust_core(["hash-json"], payload, expect_json=False))


def mask_identifier_via_rust(value: str) -> str:
    return str(run_rust_core(["mask-identifier", value], expect_json=False))


def classify_event_via_rust(db_path: Path | str, event: dict[str, Any]) -> dict[str, Any]:
    return dict(run_rust_core(["classify-event", "--db-path", str(db_path)], event))


def classify_events_via_rust(db_path: Path | str, payload: Any, *, persist: bool = False) -> dict[str, Any]:
    args = ["classify-events", "--db-path", str(db_path)]
    if persist:
        args.append("--persist")
    return dict(run_rust_core(args, payload))


def classify_events_file_via_rust(
    *,
    db_path: Path | str,
    events_json: Path | str,
    output_json: Path | str,
    journal_batch_input_json: Path | str | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    args = [
        "classify-events-file",
        "--db-path",
        str(db_path),
        "--events-json",
        str(events_json),
        "--output-json",
        str(output_json),
    ]
    if journal_batch_input_json is not None:
        args.extend(["--journal-batch-input-json", str(journal_batch_input_json)])
    if persist:
        args.append("--persist")
    return dict(run_rust_core(args))
