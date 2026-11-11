#!/usr/bin/env python3
"""Copy a self-contained, unconfigured starter without touching any service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
ROOT_FILES = ("README.md", "README.ja.md", "ROADMAP.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "constraints-test.txt", "constraints-cloud.txt", "docs/release-readiness.json", "examples/README.md", "examples/portable_state_demo.py", "AGENTS.md", "Makefile", "pyproject.toml", ".gitignore",
              "integrations/company-operations/.codex-plugin/plugin.json")
TREE_SUFFIXES = {
    "src": {".py"},
    "sql": {".sql"},
    "tests": {".py", ".json", ".txt", ".sql"},
    "scripts": {".py", ".sh"},
    "docs": {".md"},
    "codex/learning": {".py", ".md"},
    "mcp_servers": {".py"},
    "integrations/company-operations": {".json", ".md"},
}
EXCLUDED_DIRECTORIES = {
    "data", "credentials", "credential", "secrets", "tokens", "exports",
    "downloads", "generated", "reports", "artifacts", "__pycache__",
    "node_modules", "venv", "build", "dist", "tmp", "temp", "logs",
}
EXCLUDED_STEMS = {
    "credential", "credentials", "secret", "secrets", "token", "tokens",
    "access_token", "refresh_token", "client_secret", "service_account",
    "service-account", "oauth_token", "oauth_credentials",
}


def validate_slug(slug: str) -> str:
    if len(slug) > 63 or not SLUG.fullmatch(slug) or slug == "example":
        raise ValueError("slug must be 1-63 lowercase ASCII letters/digits with internal '-' or '_'; 'example' is reserved")
    return slug


def _excluded(name: str) -> bool:
    lower = name.lower()
    return (
        lower.startswith(".")
        or lower in EXCLUDED_DIRECTORIES
        or ".local." in lower
        or Path(lower).stem in EXCLUDED_STEMS
        or lower.endswith((".pem", ".key", ".secret", ".token"))
    )


def _symlink_parent(path: Path, source: Path) -> bool:
    return any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents)


def source_files(source: Path) -> list[Path]:
    """Only deliver code, synthetic test inputs, and operator documentation."""
    selected: list[Path] = []
    for name in (*ROOT_FILES, "config/offices/example.json"):
        path = source / name
        if path.is_symlink() or not path.is_file() or _symlink_parent(path, source):
            raise ValueError(f"required starter file missing or symlinked: {name}")
        selected.append(path)
    for tree, suffixes in TREE_SUFFIXES.items():
        start = source / tree
        if start.is_symlink() or _symlink_parent(start, source):
            raise ValueError(f"symlinked starter tree: {tree}")
        if not start.is_dir():
            if tree == "codex/learning":
                continue
            raise ValueError(f"required starter tree missing: {tree}")
        for directory, subdirs, filenames in os.walk(start, followlinks=False):
            parent = Path(directory)
            for name in subdirs:
                if not _excluded(name) and (parent / name).is_symlink():
                    raise ValueError(f"symlinked starter directory: {(parent / name).relative_to(source)}")
            subdirs[:] = sorted(name for name in subdirs if not _excluded(name))
            for name in sorted(filenames):
                path = parent / name
                if _excluded(name) or path.suffix not in suffixes:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"unsafe starter file: {path.relative_to(source)}")
                selected.append(path)
    return sorted(set(selected))


def company_profile(source: Path, slug: str) -> dict:
    profile = json.loads((source / "config/offices/example.json").read_text(encoding="utf-8"))
    if profile.get("schema_version") != 2 or profile.get("configured") is not False:
        raise ValueError("example profile must be schema 2 and explicitly unconfigured")
    profile["slug"] = slug
    profile["moneyforward"]["db_path"] = f"data/phase12/offices/{slug}/moneyforward_phase12.sqlite"
    profile["moneyforward"]["keychain_service"] = f"moneyforward-{slug}-automation"
    return profile


def bootstrap(slug: str, output: Path, *, source: Path = ROOT) -> Path:
    validate_slug(slug)
    source = source.resolve(strict=True)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("output must not exist; existing directories are never overwritten")
    if ".." in output.parts:
        raise ValueError("output must not contain parent traversal ('..')")
    destination = output.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("output must be outside the source starter")
    if not output.parent.is_dir():
        raise ValueError("output parent must already exist")
    files = source_files(source)
    profile = company_profile(source, slug)
    output.mkdir(mode=0o700)
    for path in files:
        relative = path.relative_to(source)
        if path.is_symlink() or _symlink_parent(path, source):
            raise ValueError(f"starter source changed during copy: {relative}")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    local_profile = output / "config" / "offices" / f"{slug}.json"
    with local_profile.open("x", encoding="utf-8") as handle:
        json.dump(profile, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    local_profile.chmod(0o600)
    # Generated plugin identity is unique per company; its code is still generic.
    for relative in (
        "integrations/company-operations/plugin.json",
        "integrations/company-operations/.codex-plugin/plugin.json",
    ):
        manifest_path = output / relative
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = f"mf-{slug}-operations"
        manifest["description"] = f"Money Forward accounting for independently configured tenant {slug}"
        interface = manifest.get("extensions", {}).get("com.openai", {}).get("interface")
        if interface is None:
            interface = manifest.get("interface")
        if interface is not None:
            interface["displayName"] = f"{slug} Money Forward Operations"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    router_path = output / "integrations/company-operations/skills/router/SKILL.md"
    # Regeneration from a previously copied starter must not retain that
    # previous company's router slug or description.
    router_text = router_path.read_text(encoding="utf-8")
    router_text = router_text.replace("__COMPANY_SLUG__", slug)
    router_text = re.sub(
        r"(?m)^(name: )mf-[a-z][a-z0-9_-]*-accounting-router$",
        lambda match: match.group(1) + f"mf-{slug}-accounting-router",
        router_text,
    )
    router_text = re.sub(
        r"(company slug `)[a-z][a-z0-9_-]*(` only)",
        lambda match: match.group(1) + slug + match.group(2),
        router_text,
    )
    router_path.write_text(router_text, encoding="utf-8")
    return local_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        profile = bootstrap(args.slug, args.output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"bootstrap refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "not_configured", "profile": str(profile),
                      "next": "Read README.md; configure the ignored local profile before doctor or connection setup.",
                      "external_calls": False, "database_created": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
