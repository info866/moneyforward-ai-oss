"""Delivery tests use only synthetic files and temporary, independent copies."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_delivery", ROOT / "scripts/bootstrap_company.py")
assert SPEC is not None and SPEC.loader is not None
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    # Exercise the actual deliverable, isolated from concurrent workspace changes.
    for path in delivery.source_files(ROOT):
        target = source / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return source


def _put(root, name, content="synthetic only\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _clean_env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MF_", "PYTHON"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.mark.parametrize("slug", ["", "../escape", "a/b", "a\\b", "/absolute", "A", "a.b",
                                      "a b", "-a", "a-", "a__b", "example", "a" * 64, "法人", "a\n"])
def test_invalid_slug_is_rejected_before_output(tmp_path, slug):
    destination = tmp_path / "new"
    with pytest.raises(ValueError, match="slug"):
        delivery.bootstrap(slug, destination, source=ROOT)
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
def test_existing_destination_is_never_overwritten(tmp_path, kind):
    destination = tmp_path / "existing"
    if kind == "directory":
        destination.mkdir()
        _put(destination, "keep.txt", "keep")
    elif kind == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError):
        delivery.bootstrap("acme", destination, source=ROOT)
    if kind == "directory":
        assert (destination / "keep.txt").read_text() == "keep"
    elif kind == "file":
        assert destination.read_text() == "keep"
    else:
        assert destination.is_symlink()


def test_output_traversal_and_recursive_destination_are_refused(tmp_path):
    with pytest.raises(ValueError, match="traversal"):
        delivery.bootstrap("acme", tmp_path / "missing" / ".." / "new", source=ROOT)
    with pytest.raises(ValueError, match="outside"):
        delivery.bootstrap("acme", ROOT / "recursive-output", source=ROOT)


def test_bootstrap_has_no_database_network_or_subprocess_effects(source, tmp_path, monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("bootstrap must not use services, commands, or a database")
    monkeypatch.setattr(sqlite3, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(subprocess, "run", prohibited)
    target = tmp_path / "company"
    profile = delivery.bootstrap("acme", target, source=source)
    payload = json.loads(profile.read_text())
    assert profile == target / "config/offices/acme.json"
    assert payload["schema_version"] == 2
    assert payload["configured"] is False
    assert payload["slug"] == "acme"
    assert payload["moneyforward"]["db_path"] == "data/phase12/offices/acme/moneyforward_phase12.sqlite"
    assert payload["moneyforward"]["keychain_service"] == "moneyforward-acme-automation"
    assert profile.stat().st_mode & 0o777 == 0o600
    assert not (target / "data").exists()
    assert not (target / ".git").exists()
    assert not list(target.rglob("*.sqlite"))


def test_copy_excludes_runtime_credentials_local_profiles_and_metadata(source, tmp_path):
    excluded = [".git/config", ".github/workflows/ci.yml", "data/company.sqlite",
                "config/offices/previous.json", "config/offices/previous.local.json",
                "scripts/.env", "scripts/credentials.json", "scripts/client_secret.txt",
                "tests/data/ledger.json", "tests/credentials/account.json",
                "tests/fixtures/service_account.json", "tests/fixtures/report.pdf",
                "tests/fixtures/result.local.json", "src/mf_phase12/__pycache__/cache.pyc",
                "docs/reports/customer.md", "codex/learning/secrets/key.txt"]
    for name in excluded:
        _put(source, name)
    _put(source, "tests/fixtures/synthetic.json", '{"fictional": true}')
    _put(source, "codex/learning/codex_autonomy_run.py", "# local verification runner\n")
    target = tmp_path / "company"
    delivery.bootstrap("acme", target, source=source)
    for name in excluded:
        assert not (target / name).exists(), name
    assert (target / "tests/fixtures/synthetic.json").is_file()
    assert (target / "codex/learning/codex_autonomy_run.py").is_file()
    assert sorted(path.name for path in (target / "config/offices").iterdir()) == ["acme.json", "example.json"]


@pytest.mark.parametrize("kind", ["file", "directory", "config_parent"])
def test_selected_symlinks_are_refused_before_copy(source, tmp_path, kind):
    external = _put(tmp_path, "outside.py", "PRIVATE_SENTINEL = True\n")
    if kind == "file":
        (source / "src/mf_phase12/unsafe.py").symlink_to(external)
    elif kind == "directory":
        (source / "src/unsafe").symlink_to(tmp_path)
    else:
        saved = source / "original-config"
        (source / "config").rename(saved)
        (source / "config").symlink_to(saved)
    target = tmp_path / "company"
    with pytest.raises(ValueError, match="symlink|unsafe"):
        delivery.bootstrap("acme", target, source=source)
    assert not target.exists()


def test_copy_imports_and_bootstraps_again_without_source_repository(source, tmp_path):
    first = tmp_path / "first"
    delivery.bootstrap("acme", first, source=source)
    # Imports run in an isolated interpreter with only the copy's source added.
    code = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
import mf_phase12
from mf_phase12 import accounting_api, hash_utils, office_profile
assert Path(mf_phase12.__file__).resolve().is_relative_to(Path.cwd())
profile = office_profile.load_office_profile('acme', repository_root=Path.cwd())
assert not profile.configured
try:
    profile.require_configured()
except office_profile.OfficeProfileError:
    pass
else:
    raise AssertionError('unconfigured starter must reject external I/O')
assert len(hash_utils.stable_hash({'synthetic': True})) == 64
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=first,
                            env=_clean_env(), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    doctor_code = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
from mf_phase12.cli import main
raise SystemExit(main(['--office-profile', 'config/offices/acme.json', 'doctor']))
"""
    doctor = subprocess.run([sys.executable, "-I", "-B", "-c", doctor_code], cwd=first,
                            env=_clean_env(), text=True, capture_output=True)
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["status"] == "not_configured"
    assert not (first / "data").exists()
    source.rename(tmp_path / "source-unavailable")
    second = tmp_path / "second"
    result = subprocess.run([sys.executable, "-I", "-B", "scripts/bootstrap_company.py",
                             "--slug", "bravo", "--output", str(second)],
                            cwd=first, env=_clean_env(), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "not_configured"
    assert (second / "config/offices/bravo.json").is_file()
    assert not (second / "config/offices/acme.json").exists()
    assert not (second / "data").exists()


def test_all_selected_files_are_copied_byte_for_byte(source, tmp_path):
    target = tmp_path / "company"
    executable = _put(source, "scripts/local-helper.sh", "#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    delivery.bootstrap("new-company", target, source=source)
    expected_customizations = {
        "integrations/company-operations/plugin.json",
        "integrations/company-operations/.codex-plugin/plugin.json",
        "integrations/company-operations/skills/router/SKILL.md",
    }
    for path in delivery.source_files(source):
        relative = path.relative_to(source).as_posix()
        copied = target / relative
        if relative in expected_customizations:
            assert copied.read_bytes() != path.read_bytes()
        else:
            assert copied.read_bytes() == path.read_bytes()
    assert (target / "scripts/local-helper.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("change", [{"configured": True}, {"schema_version": 1}])
def test_bootstrap_refuses_an_unsafe_example_before_creating_output(source, tmp_path, change):
    example = source / "config/offices/example.json"
    payload = json.loads(example.read_text())
    payload.update(change)
    example.write_text(json.dumps(payload))
    target = tmp_path / "company"
    with pytest.raises(ValueError, match="schema 2"):
        delivery.bootstrap("acme", target, source=source)
    assert not target.exists()


def test_ignore_rules_exclude_local_profile_without_hiding_example(source, tmp_path):
    target = tmp_path / "company"
    delivery.bootstrap("acme", target, source=source)
    # A temporary Git repo tests the actual ignore rules; bootstrap creates none.
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True, capture_output=True)
    result = subprocess.run(["git", "check-ignore", "config/offices/acme.json",
                             "config/offices/example.json"], cwd=target, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["config/offices/acme.json"]
    profile_path = target / "config/offices/acme.json"
    payload = json.loads(profile_path.read_text())
    payload.update(configured=True, legal_name="Synthetic Acme", display_name="Synthetic Acme")
    payload["moneyforward"]["expected_office_names"] = ["Synthetic Acme"]
    payload["google_workspace"]["account"] = "acme@synthetic.invalid"
    payload["google_drive"] = {
        "account": "shared@synthetic.invalid",
        "display_root_name": "Synthetic Evidence",
        "evidence_root_id": "synthetic_acme_root",
        "evidence_source_folder_ids": ["synthetic_acme_root"],
        "processed_folder_id": "synthetic_acme_processed",
        "temporary_ocr_folder_id": "synthetic_acme_ocr",
    }
    profile_path.write_text(json.dumps(payload))
    artifact = _put(target, "data/phase12/offices/acme/batch.local.json", '{"synthetic": true}')
    foreign = _put(target, "data/phase12/offices/bravo/batch.local.json", '{"synthetic": "foreign"}')
    code = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
from mf_phase12.local_artifacts import require_ignored_untracked_data_path
from mf_phase12.office_profile import load_office_profile, using_office_profile
profile = load_office_profile('acme', repository_root=Path.cwd())
profile.require_configured()
path = Path('data/phase12/offices/acme/batch.local.json')
with using_office_profile(profile):
    assert require_ignored_untracked_data_path(path, purpose='synthetic bootstrap check') == path
    try:
        require_ignored_untracked_data_path(Path('data/phase12/offices/bravo/batch.local.json'),
                                           purpose='cross-company rejection check')
    except SystemExit as error:
        assert "selected company" in str(error)
    else:
        raise AssertionError('another company path was accepted')
"""
    checked = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=target,
                             env=_clean_env(), text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(artifact.read_text()) == {"synthetic": True}
    assert json.loads(foreign.read_text()) == {"synthetic": "foreign"}
    assert not list((target / "data").rglob("*.sqlite"))


def test_documented_global_execute_position_is_accepted_without_execution():
    from mf_phase12.cli import build_parser

    common = ["--office-profile", "config/offices/acme.json", "--execute"]
    setup = build_parser().parse_args([*common, "setup"])
    assert setup.command == "setup" and setup.execute is True
    rag = build_parser().parse_args([*common, "rag-build", "--start-date", "2026-01-01",
                                    "--end-date", "2026-01-31"])
    assert rag.command == "rag-build" and rag.execute is True
    prepare = build_parser().parse_args(["--office-profile", "config/offices/acme.json", "prepare",
                                        "--input-json", "data/phase12/offices/acme/batch.local.json",
                                        "--output-dir", "data/phase12/offices/acme/dry-run"])
    assert prepare.command == "prepare" and not prepare.execute


def test_bootstrap_cli_rejects_repeated_output(source, tmp_path):
    target = tmp_path / "company"
    command = [sys.executable, "-I", "-B", str(source / "scripts/bootstrap_company.py"),
               "--slug", "acme", "--output", str(target)]
    first = subprocess.run(command, env=_clean_env(), text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    before = (target / "config/offices/acme.json").read_bytes()
    repeated = subprocess.run(command, env=_clean_env(), text=True, capture_output=True)
    assert repeated.returncode == 2
    assert "must not exist" in repeated.stderr
    assert (target / "config/offices/acme.json").read_bytes() == before
