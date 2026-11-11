"""Static checks for a standalone starter; never opens accounting databases."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def audit(root: Path = ROOT) -> dict:
    errors = []
    modules = {path.stem for path in (root / 'src/mf_phase12').glob('*.py')}
    for path in sorted((root / 'src').rglob('*.py')):
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                if path.parent.name == 'mf_phase12' and node.module.split('.')[0] not in modules:
                    errors.append(f'{path.relative_to(root)}:{node.lineno}: missing local dependency {node.module}')
            if isinstance(node, ast.ImportFrom) and not node.level and node.module and node.module.startswith('mf_'):
                module_path = root / 'src' / Path(*node.module.split('.'))
                if not module_path.with_suffix('.py').exists() and not (module_path / '__init__.py').exists():
                    errors.append(f'{path.relative_to(root)}:{node.lineno}: missing bundled dependency {node.module}')
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if re.fullmatch(r'[A-Za-z0-9_-]{25,50}', text) and any(c.isdigit() for c in text) and any(c.isupper() for c in text) and any(c.islower() for c in text):
                    errors.append(f'{path.relative_to(root)}:{node.lineno}: possible embedded external identifier')
    for path in (root / 'config/offices').glob('*.json'):
        if path.name != 'example.json':
            result = subprocess.run(['git', 'ls-files', '--error-unmatch', str(path.relative_to(root))], cwd=root, capture_output=True)
            if result.returncode == 0:
                errors.append(f'{path.relative_to(root)}: company configuration must remain local')
    return {'passed': not errors, 'errors': errors, 'database_accessed': False, 'external_io': False}



EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|mf_api_prd_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
REQUIRED_MATERIALS = ("LICENSE", "README.md", "README.ja.md", "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "pyproject.toml", "Makefile", "src", "tests", "docs/SETUP.md", "docs/RELEASE_GATES.md")


def text_findings(text: str, location: str, deny_terms: tuple[str, ...] = ()) -> list[dict]:
    """Return locations/types only. Never echo a private value in the audit output."""
    findings = []
    for m in EMAIL.finditer(text):
        domain = m.group().rsplit('@', 1)[1].lower()
        if domain in {'example.com', 'example.net', 'example.org', 'users.noreply.github.com'} or domain.endswith(('.test', '.invalid', '.example')):
            continue
        findings.append({'kind': 'nonpublic_email', 'location': location, 'line': text[:m.start()].count('\n') + 1})
    for kind, pattern in [('secret', SECRET), ('local_user_path', PRIVATE_PATH)]:
        for m in pattern.finditer(text):
            findings.append({'kind': kind, 'location': location, 'line': text[:m.start()].count('\n') + 1})
    for term in deny_terms:
        if not term:
            raise ValueError('Private deny terms must not be empty')
        for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
            findings.append({'kind': 'private_deny_term', 'location': location, 'line': text[:m.start()].count('\n') + 1})
    return findings


def publication_content_audit(root: Path, deny_terms: tuple[str, ...] = ()) -> dict:
    """Screen every reachable commit/blob and index. Not a business-ready certificate."""
    def git(*args: str) -> bytes:
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout
    findings = []
    missing = [x for x in REQUIRED_MATERIALS if not (root / x).exists()]
    commits = git('rev-list', '--all').decode().splitlines()
    if not commits:
        findings.append({'kind': 'history_missing', 'location': 'git', 'line': 0})
    for sha in commits:
        commit = git('cat-file', 'commit', sha).decode('utf-8')
        findings.extend(text_findings(commit, 'commit:' + sha[:12], deny_terms))
        for line in commit.splitlines():
            if line.startswith(('author ', 'committer ')):
                name = line.split(' ', 1)[1].rsplit('<', 1)[0].strip()
                if name not in {'OSS Maintainers', 'GitHub'} and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
                    findings.append({'kind': 'author_identity_review', 'location': 'commit:' + sha[:12], 'line': 0})
    seen = set()
    objects = git('rev-list', '--objects', '--all').decode().splitlines()
    index = git('ls-files', '-s').decode().splitlines()
    for line in index:
        mode, sha, tail = line.split(' ', 2)
        objects.append(sha + ' ' + tail.split('\t', 1)[-1])
        if mode == '120000':
            findings.append({'kind': 'symlink_review_required', 'location': tail.split('\t', 1)[-1], 'line': 0})
        if mode == '160000':
            findings.append({'kind': 'unreviewed_submodule', 'location': tail.split('\t', 1)[-1], 'line': 0})
    for item in objects:
        sha, _, path = item.partition(' ')
        if sha in seen or git('cat-file', '-t', sha).strip() != b'blob':
            continue
        seen.add(sha)
        raw = git('cat-file', 'blob', sha)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            findings.append({'kind': 'binary_review_required', 'location': path, 'line': 0})
            continue
        findings.extend(text_findings(text, 'blob:' + sha[:12] + ':' + path, deny_terms))
        if path.startswith('.github/workflows/'):
            findings.append({'kind': 'actions_workflow_prohibited', 'location': path, 'line': 0})
        if path.startswith('config/offices/') and path != 'config/offices/example.json':
            findings.append({'kind': 'company_profile_tracked', 'location': path, 'line': 0})
    return {'passed': not findings and not missing, 'commits_scanned': len(commits),
            'unique_blobs_scanned': len(seen), 'findings': findings, 'missing_materials': missing,
            'limits': 'Pattern screening plus static dependency checks. Manual review, GitHub surfaces, licensing, live E2E and owner approval remain independent release gates.',
            'business_readiness_certified': False, 'external_io': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='Also screen all reachable Git history/index and required materials')
    parser.add_argument('--denylist', type=Path, help='Private JSON string array outside this checkout; never committed')
    args = parser.parse_args()
    terms = ()
    if args.denylist:
        path = args.denylist.resolve(strict=True)
        if ROOT.resolve() == path or ROOT.resolve() in path.parents:
            raise SystemExit('Private denylist must remain outside the repository')
        values = json.loads(path.read_text())
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise SystemExit('Private denylist must be a JSON string array')
        terms = tuple(values)
    result = audit()
    if args.history:
        result['publication_content'] = publication_content_audit(ROOT, terms)
        result['passed'] = result['passed'] and result['publication_content']['passed']
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
