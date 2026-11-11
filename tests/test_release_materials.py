"""Offline release hygiene; intentionally synthetic values only."""
import importlib.util
from pathlib import Path
import subprocess

from mf_phase12.starter_audit import text_findings

ROOT = Path(__file__).resolve().parents[1]


def test_private_email_is_detected_without_echoing_value():
    address = 'operator' + '@' + 'not-public-domain' + '.com'
    findings = text_findings('x\n' + address, 'example')
    assert findings == [{'kind': 'nonpublic_email', 'location': 'example', 'line': 2}]
    assert address not in str(findings)


def test_reserved_synthetic_email_and_github_noreply_are_allowed():
    assert not text_findings('alpha@tenant.test user@example.invalid user@example.com 42+alias@users.noreply.github.com', 'synthetic')


def test_local_user_path_is_detected():
    text = '/' + 'Users' + '/' + 'synthetic-operator' + '/project'
    assert text_findings(text, 'sample')[0]['kind'] == 'local_user_path'


def test_private_company_denylist_reports_location_only():
    findings = text_findings('Synthetic Confidential Entity', 'sample', ('Confidential Entity',))
    assert findings[0]['kind'] == 'private_deny_term'
    assert 'Confidential Entity' not in str(findings)


def test_token_detector_does_not_echo_simulated_token():
    token = 'gh' + 'p_' + ('A' * 36)
    findings = text_findings(token, 'synthetic')
    assert findings[0]['kind'] == 'secret'
    assert token not in str(findings)


def test_bootstrap_preserves_license_and_public_documents(tmp_path):
    spec = importlib.util.spec_from_file_location('release_bootstrap', ROOT / 'scripts/bootstrap_company.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = tmp_path / 'independent-copy'
    module.bootstrap('synthetic', dest)
    for name in ['LICENSE', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md', 'README.ja.md']:
        assert (dest / name).read_bytes() == (ROOT / name).read_bytes()


def test_no_actions_workflow_is_distributed():
    assert not list((ROOT / '.github/workflows').glob('*'))


def test_example_remains_unconfigured():
    import json
    example = json.loads((ROOT / 'config/offices/example.json').read_text())
    assert example['configured'] is False
    assert example['accounting_rules'] == {}


def test_local_profile_is_ignored_and_license_is_not(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
    ignored = subprocess.run(['git', 'check-ignore', 'config/offices/synthetic.json'], cwd=tmp_path, capture_output=True)
    assert ignored.returncode == 0
    visible = subprocess.run(['git', 'check-ignore', 'LICENSE'], cwd=tmp_path, capture_output=True)
    assert visible.returncode == 1


def test_short_japanese_identity_terms_are_not_silently_ignored():
    assert text_findings("仮社の設定", "synthetic", ("仮社",))[0]["kind"] == "private_deny_term"


def test_empty_deny_term_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        text_findings("synthetic", "example", ("",))
