#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from argparse import ArgumentParser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SECRET_PATTERNS = (
    re.compile(r"mf_api(?:_pro)?_[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bkey_[A-Za-z0-9_-]{12,}"),
    re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:access|refresh)_token\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bclient_secret\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(
        r"https://chat\.googleapis\.com/v1/spaces/[A-Za-z0-9_-]+/messages\?"
        r"key=[A-Za-z0-9_-]{20,}&token=[A-Za-z0-9_-]{20,}",
        re.I,
    ),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:egov_)?api_key\s*[:=]\s*[\"']?[A-Za-z0-9_-]{20,}", re.I),
    re.compile(r"APIキー\s*[:：=]\s*[A-Za-z0-9_-]{20,}", re.I),
)


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: list[str]
    returncode: int
    duration_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def main() -> int:
    args = _parse_args()
    root = _repo_root()
    started_at = datetime.now(timezone.utc).isoformat()
    db_path = args.db_path or os.environ.get("DB_PATH")
    commands = _build_commands(args.profile, db_path)

    secret_scan = _scan_worktree_for_secrets(root)
    if secret_scan["matches"]:
        results: list[CommandResult] = []
        skipped_commands = [name for name, _command in commands]
    else:
        results, skipped_commands = _run_fail_fast(commands, root)
    git_status = _git_output(root, ["git", "status", "--short"])
    git_diff_stat = _git_output(root, ["git", "diff", "--stat"])
    git_head = _git_output(root, ["git", "rev-parse", "HEAD"]).strip()
    learning = _learning_routing(root)
    safe_promotion = {
        "ready": (
            len(results) == len(commands)
            and all(result.passed for result in results)
            and not secret_scan["matches"]
        ),
        "requires_user_or_external_approval": False,
        "write_api_called": False,
        "moneyforward_api_called": False,
        "keychain_accessed": False,
    }
    rollback_manifest = {
        "head_before_commit": git_head,
        "changed_files": _changed_files(git_status),
        "restore_method": "Use a normal git revert of the next commit if rollback is required. Do not use reset --hard.",
    }

    report = {
        "status": "passed" if safe_promotion["ready"] else "failed",
        "profile": args.profile,
        "db_path": str(db_path) if db_path else "default",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "commands": [asdict(result) for result in results],
        "skipped_commands": skipped_commands,
        "fail_fast": True,
        "git": {
            "head": git_head,
            "status_short": git_status,
            "diff_stat": git_diff_stat,
        },
        "secret_scan": secret_scan,
        "learning_routing": learning,
        "safe_promotion": safe_promotion,
        "rollback_manifest": rollback_manifest,
    }
    report_path = _write_report(root, report)
    summary = {
        "status": report["status"],
        "profile": args.profile,
        "db_path": report["db_path"],
        "report_path": str(report_path.relative_to(root)),
        "failed_commands": [result.name for result in results if not result.passed],
        "skipped_commands": skipped_commands,
        "secret_matches": len(secret_scan["matches"]),
        "changed_files": rollback_manifest["changed_files"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def _parse_args():
    parser = ArgumentParser(description="Run Codex autonomy verification for this repository.")
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default=os.environ.get("CODEX_AUTONOMY_PROFILE", "full"),
        help="fast is for inner-loop PDCA; full is for pre-commit/final verification.",
    )
    parser.add_argument("--db-path", help="Optional Phase 12 office SQLite DB path. Defaults to DB_PATH env or repo default.")
    return parser.parse_args()


def _build_commands(profile: str, db_path: str | None = None) -> list[tuple[str, list[str]]]:
    del db_path
    common = [
        ("compileall", ["python3", "-m", "compileall", "-q", "src", "scripts", "codex/learning"]),
        ("diff_check", ["git", "diff", "--check"]),
        ("audit", ["make", "audit"]),
    ]
    if profile == "fast":
        return common
    return [
        *common,
        ("test", ["make", "test"]),
    ]


def _repo_root() -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Path(completed.stdout.strip())


def _run(name: str, command: list[str], root: Path) -> CommandResult:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "src")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    duration = round(time.monotonic() - started, 3)
    return CommandResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        duration_seconds=duration,
        stdout_tail=_tail(completed.stdout) if completed.returncode else "",
        stderr_tail=_tail(completed.stderr) if completed.returncode else "",
    )


def _run_fail_fast(
    commands: list[tuple[str, list[str]]],
    root: Path,
) -> tuple[list[CommandResult], list[str]]:
    results: list[CommandResult] = []
    for index, (name, command) in enumerate(commands):
        result = _run(name, command, root)
        results.append(result)
        if not result.passed:
            return results, [pending_name for pending_name, _ in commands[index + 1 :]]
    return results, []


def _git_output(root: Path, command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _changed_files(status_short: str) -> list[str]:
    files: list[str] = []
    for line in status_short.splitlines():
        if not line.strip():
            continue
        files.append(line[3:].strip())
    return files


def _scan_worktree_for_secrets(root: Path) -> dict:
    matches: list[dict[str, object]] = []
    for scope, command in (
        ("unstaged", ["git", "diff", "--", ".", ":(exclude)data"]),
        ("staged", ["git", "diff", "--cached", "--", ".", ":(exclude)data"]),
    ):
        diff = _git_output(root, command)
        for line_no, line in enumerate(diff.splitlines(), start=1):
            if not line.startswith("+") or line.startswith("+++"):
                continue
            _append_secret_match(matches, line, scope=scope, line_no=line_no)

    untracked = _git_output(root, ["git", "ls-files", "--others", "--exclude-standard"])
    for relative in untracked.splitlines():
        relative = relative.strip()
        if not relative or relative == "data" or relative.startswith("data/"):
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, start=1):
            _append_secret_match(matches, line, scope=f"untracked:{relative}", line_no=line_no)
    return {
        "scope": "staged, unstaged, and untracked worktree files excluding ignored data/",
        "matches": matches,
        "raw_secret_values_recorded": False,
    }


def _append_secret_match(
    matches: list[dict[str, object]],
    line: str,
    *,
    scope: str,
    line_no: int,
) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(line):
            if _is_safe_secret_code_reference(pattern, line):
                continue
            matches.append({"scope": scope, "line": line_no, "pattern": pattern.pattern})
            return


def _is_safe_secret_code_reference(pattern: re.Pattern[str], line: str) -> bool:
    """Exclude variable flow from literal-secret findings without hiding literals."""
    if "(?:access|refresh)_token" in pattern.pattern:
        return bool(
            re.search(
                r"\b(?:access|refresh)_token\s*=\s*(?:"
                r"(?:access|refresh)_token\b|"
                r"[A-Za-z_]\w*\.(?:access|refresh)_token\b|"
                r"[A-Za-z_]\w*\.get_secret\s*\(|"
                r"[A-Za-z_]\w*\s*\()",
                line,
                re.I,
            )
        )
    if "client_secret" in pattern.pattern:
        return bool(
            re.search(
                r"\bclient_secret\s*=\s*(?:client_secret\b|_collect_credentials\s*\()",
                line,
                re.I,
            )
        )
    if "api_key" in pattern.pattern:
        return bool(
            re.search(
                r"\b(?:egov_)?api_key\s*=\s*(?:api_key|egov_api_key)\b",
                line,
                re.I,
            )
        )
    return False


def _learning_routing(root: Path) -> dict:
    candidates = []
    for relative in (
        "pending_rules.md",
        "codex/learning/pending_rules.md",
        "docs/pending_rules.md",
        ".claude/pending_rules.md",
    ):
        path = root / relative
        if path.exists():
            candidates.append(relative)
    return {
        "pending_rule_files_found": candidates,
        "route": "No automatic AGENTS/Skill/Hook promotion is performed by this runner.",
        "manual_review_required": bool(candidates),
    }


def _write_report(root: Path, report: dict) -> Path:
    output_dir = root / "data" / "codex" / "autonomy_reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"codex_autonomy_run_{timestamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_path


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


if __name__ == "__main__":
    sys.exit(main())
