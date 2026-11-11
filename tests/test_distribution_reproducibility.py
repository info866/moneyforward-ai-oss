"""Distribution regressions: no credentials, network or original checkout."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mf_phase12 import cli
from mf_phase12.hash_utils import source_tree_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def source_tree(root):
    (root / "src/mf_phase12").mkdir(parents=True)
    (root / "src/mf_phase12/cli.py").write_text("# synthetic implementation\n")
    (root / "sql").mkdir()
    (root / "sql/schema.sql").write_text("-- synthetic schema\n")
    return root


def test_source_fingerprint_is_portable_and_ignores_local_company_data(tmp_path):
    first, second = source_tree(tmp_path / "first"), source_tree(tmp_path / "second")
    expected = source_tree_fingerprint(first)
    assert expected.startswith("source-sha256:")
    assert expected == source_tree_fingerprint(second)
    (first / "config").mkdir()
    (first / "config/company.json").write_text('{"synthetic_private": true}')
    assert source_tree_fingerprint(first) == expected
    (first / "src/mf_phase12/cli.py").write_text("# changed implementation\n")
    assert source_tree_fingerprint(first) != expected


def test_source_fingerprint_changes_when_schema_changes(tmp_path):
    root = source_tree(tmp_path)
    before = source_tree_fingerprint(root)
    (root / "sql/schema.sql").write_text("-- changed schema\n")
    assert source_tree_fingerprint(root) != before


def test_missing_source_cannot_produce_a_fabricated_identity(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        source_tree_fingerprint(tmp_path)


def test_source_symlink_cannot_read_outside_distribution(tmp_path):
    root = source_tree(tmp_path / "project")
    external = tmp_path / "external.py"
    external.write_text("# synthetic, must not be read\n")
    (root / "src/mf_phase12/unsafe.py").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        source_tree_fingerprint(root)


def test_fresh_git_init_without_commit_has_real_source_provenance(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(cli, "__file__", str(root / "src/mf_phase12/cli.py"))
    assert cli._aggregate_implementation_head() == source_tree_fingerprint(root)


def test_source_archive_does_not_use_an_unrelated_parent_git_head(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    monkeypatch.setattr(cli, "__file__", str(root / "src/mf_phase12/cli.py"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("no local .git"))
    assert cli._aggregate_implementation_head() == source_tree_fingerprint(root)


def test_generated_copy_includes_examples_and_machine_release_gates(tmp_path):
    spec = importlib.util.spec_from_file_location("distribution_bootstrap", ROOT / "scripts/bootstrap_company.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = tmp_path / "generated"
    module.bootstrap("synthetic", dest)
    for name in ("examples/README.md", "docs/release-readiness.json"):
        assert (dest / name).read_bytes() == (ROOT / name).read_bytes()
    readiness = json.loads((dest / "docs/release-readiness.json").read_text())
    assert readiness["publication_authorized"] is False
    assert readiness["visibility"] == "private"
    assert readiness["source_repository_visibility"] == "public"
    assert readiness["source_publication"]["public_repository_visible"] is True
    assert readiness["source_publication"]["upstream_original_public_visibility_authorized"] is True
    assert readiness["source_publication"]["authorization_does_not_extend_to_generated_company_checkouts"] is True
    assert readiness["production_activation"]["status"] == "BLOCKED"
    assert readiness["production_activation"]["live_e2e_verified"] is False
    assert readiness["source_publication"]["public_visibility_authorized"] is False
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    result = subprocess.run(
        [sys.executable, "-c", "import sys;sys.path.insert(0,'src');from mf_phase12.cli import _aggregate_implementation_head;print(_aggregate_implementation_head())"],
        cwd=dest, check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == source_tree_fingerprint(dest)


def test_demo_runs_from_independent_generated_copy(tmp_path):
    spec = importlib.util.spec_from_file_location("demo_bootstrap", ROOT / "scripts/bootstrap_company.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = tmp_path / 'copy'
    module.bootstrap('synthetic-demo-copy', dest)
    result = subprocess.run([sys.executable, 'examples/portable_state_demo.py'],
        cwd=dest, check=True, capture_output=True, text=True, timeout=20)
    facts = json.loads(result.stdout)
    assert facts['evidence_level'] == 'OFFLINE_SYNTHETIC'
    assert facts['run_count'] == 1
    assert facts['same_request_reused'] and facts['same_decision_reused']
    assert facts['cache_rebuilt_from_bound_db']
    assert facts['provider_operations'] == 0
    assert facts['accounting_completed'] is False
    assert facts['production_ready'] is False


def test_bootstrap_includes_all_accounting_execution_materials(tmp_path):
    import importlib.util
    import sys
    import subprocess
    spec = importlib.util.spec_from_file_location('distribution_accounting_bootstrap', ROOT / 'scripts/bootstrap_company.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = tmp_path / 'accounting-copy'
    module.bootstrap('synthetic-copy', dest)
    for name in ('examples/accounting_fixtures.py', 'examples/accounting_pipeline_demo.py', 'tests/test_accounting_pipeline_e2e.py'):
        assert (dest / name).read_bytes() == (ROOT / name).read_bytes()
    result = subprocess.run([sys.executable, str(dest / 'examples/accounting_pipeline_demo.py')],
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    import json
    assert json.loads(result.stdout)['replay_extra_mutations'] == 0
