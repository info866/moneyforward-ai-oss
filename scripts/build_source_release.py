#!/usr/bin/env python3
"""Build/verify a deterministic, clean-commit source archive. Never publishes."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mf_phase12.starter_audit import text_findings  # noqa: E402

MANIFEST = 'RELEASE_MANIFEST.json'
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
REQUIRED = {'LICENSE', 'README.md', 'README.ja.md', 'CONTRIBUTING.md',
            'SECURITY.md', 'pyproject.toml', 'Makefile', 'docs/SETUP.md',
            'docs/RELEASE_GATES.md', 'docs/PRODUCTION_GATES.md'}


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True).stdout


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


def safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    forbidden = {'data', 'credentials', 'secrets', 'logs', 'exports',
                 'downloads', 'node_modules', '.git', '.venv', '__pycache__'}
    return (bool(name) and '\\' not in name and ':' not in name and not path.is_absolute() and str(path) == name
            and not set(path.parts) & (forbidden | {'.', '..'})
            and not name.startswith('.github/workflows/')
            and not any(p.startswith('.env') or '.local.' in p for p in path.parts)
            and not name.endswith(('.sqlite', '.sqlite3', '.db', '.log', '.pem', '.key'))
            and not (name.startswith('config/offices/') and name != 'config/offices/example.json'))


def build(root: Path, output: Path) -> dict:
    root = root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('output already exists; choose a new path')
    if git(root, 'status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('source must be a clean committed checkout')
    commit = git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()
    tree = git(root, 'rev-parse', 'HEAD^{tree}').decode().strip()
    entries, payloads = [], {}
    total = 0
    for entry in git(root, 'ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode('utf-8')
        if kind != 'blob' or mode not in {'100644', '100755'} or not safe_path(name) or name == MANIFEST:
            raise ValueError('unapproved source path or object type')
        size = int(git(root, 'cat-file', '-s', oid))
        total += size
        if size > MAX_FILE or total > MAX_TOTAL:
            raise ValueError('source exceeds reviewed archive limits')
        content = git(root, 'cat-file', 'blob', oid)
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ValueError('binary file requires separate disclosure review') from error
        if text_findings(text, name):
            raise ValueError('disclosure screening failed (values suppressed)')
        payloads[name] = content
        entries.append({'path': name, 'sha256': hashlib.sha256(content).hexdigest(),
                        'bytes': len(content), 'mode': mode})
    if not REQUIRED <= payloads.keys():
        raise ValueError('required distribution materials missing')
    manifest = {'schema': 'source-release-manifest-v1', 'commit': commit, 'tree': tree,
                'evidence_level': 'SOURCE_SNAPSHOT_NOT_LIVE_ACCEPTANCE',
                'files': sorted(entries, key=lambda x: x['path'])}
    payloads[MANIFEST] = canonical(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            with gzip.GzipFile(fileobj=tmp, mode='wb', filename='', mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                    for name, data in sorted(payloads.items()):
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        member.mode = 0o755 if next((e['mode'] for e in entries if e['path'] == name), '100644') == '100755' else 0o644
                        member.mtime = member.uid = member.gid = 0
                        member.uname = member.gname = ''
                        archive.addfile(member, io.BytesIO(data))
            tmp.flush()
            verified = verify(temporary)
            # Exclusive create prevents a racing build from overwriting a file.
            with output.open('xb') as final:
                final.write(temporary.read_bytes())
        finally:
            temporary.unlink(missing_ok=True)
    return {**verified, 'archive': output.name, 'published': False}


def verify(path: Path) -> dict:
    payloads, modes, total = {}, {}, 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            if not member.isfile() or not safe_path(member.name) or member.name in payloads:
                raise ValueError('unsafe or duplicate archive member')
            if member.mode not in (0o644, 0o755) or member.uid != 0 or member.gid != 0 or member.uname or member.gname or member.mtime != 0:
                raise ValueError('unreviewed archive identity or mode metadata')
            modes[member.name] = member.mode
            total += member.size
            if member.size > MAX_FILE or total > MAX_TOTAL:
                raise ValueError('archive exceeds review limits')
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError('missing member content')
            payloads[member.name] = handle.read(MAX_FILE + 1)
    if MANIFEST not in payloads:
        raise ValueError('missing release manifest')
    manifest = json.loads(payloads.pop(MANIFEST))
    if manifest.get('schema') != 'source-release-manifest-v1':
        raise ValueError('unrecognized manifest schema')
    if not re.fullmatch('[a-f0-9]{40,64}', manifest.get('commit', '')):
        raise ValueError('invalid commit identifier')
    if not re.fullmatch('[a-f0-9]{40,64}', manifest.get('tree', '')):
        raise ValueError('invalid source tree identifier')
    entries = manifest.get('files')
    if not isinstance(entries, list):
        raise ValueError('missing file inventory')
    names = [item['path'] for item in entries]
    if len(names) != len(set(names)) or set(names) != set(payloads) or not REQUIRED <= set(names):
        raise ValueError('manifest file set mismatch')
    for item in entries:
        if item['mode'] not in {'100644', '100755'} or modes[item['path']] != int(item['mode'], 8) & 0o777:
            raise ValueError('source mode mismatch')
        raw = payloads[item['path']]
        if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('source content checksum mismatch')
        if text_findings(raw.decode('utf-8'), item['path']):
            raise ValueError('archive disclosure screening failed')
    return {'verified': True, 'commit': manifest['commit'], 'tree': manifest['tree'],
            'files': len(entries), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'production_ready': False, 'provider_operations': 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--verify', type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.verify) if args.verify else build(ROOT, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError, UnicodeError,
            KeyError, TypeError, tarfile.TarError) as error:
        print(json.dumps({'verified': False, 'error_type': type(error).__name__,
                          'action': 'inspect local source; no publication occurred'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
