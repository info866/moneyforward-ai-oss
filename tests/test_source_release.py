"""Source-package tests use only generated text and local Git repositories."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_release', ROOT / 'scripts/build_source_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def run(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    run(root, 'init', '-q', '-b', 'main')
    run(root, 'config', 'user.name', 'OSS Maintainers')
    run(root, 'config', 'user.email', 'synthetic@users.noreply.github.com')
    for name in release.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Synthetic distribution file\n')
    (root / '.gitignore').write_text('data/\n.env\n')
    run(root, 'add', '.')
    run(root, 'commit', '-qm', 'Synthetic source')
    return root


def test_build_is_deterministic_and_never_copies_ignored_data(source, tmp_path):
    (source / 'data').mkdir()
    (source / 'data/not-for-distribution.txt').write_text('Synthetic private local state')
    (source / '.env').write_text('SYNTHETIC_LOCAL_SETTING=not-distributed')
    first, second = tmp_path / 'one.tar.gz', tmp_path / 'two.tar.gz'
    a, b = release.build(source, first), release.build(source, second)
    assert a['sha256'] == b['sha256']
    assert first.read_bytes() == second.read_bytes()
    assert a['published'] is False and a['production_ready'] is False
    assert a['provider_operations'] == 0
    with tarfile.open(first) as archive:
        names = archive.getnames()
        assert 'data/not-for-distribution.txt' not in names
        assert '.env' not in names and '.git/config' not in names
        manifest = json.load(archive.extractfile(release.MANIFEST))
        assert manifest['commit'] == run(source, 'rev-parse', 'HEAD').stdout.decode().strip()
    assert release.verify(first)['verified']


@pytest.mark.parametrize('kind', ['tracked', 'untracked', 'staged'])
def test_dirty_checkout_is_not_packaged(source, tmp_path, kind):
    target = source / ('new.txt' if kind == 'untracked' else 'README.md')
    target.write_text('Synthetic modification')
    if kind == 'staged':
        run(source, 'add', 'README.md')
    with pytest.raises(ValueError, match='clean'):
        release.build(source, tmp_path / 'dirty.tar.gz')


def test_existing_output_is_not_overwritten(source, tmp_path):
    target = tmp_path / 'existing.tar.gz'
    target.write_text('Preserve existing file')
    with pytest.raises(ValueError):
        release.build(source, target)
    assert target.read_text() == 'Preserve existing file'


def test_tracked_symlink_is_rejected(source, tmp_path):
    (source / 'link.txt').symlink_to('README.md')
    run(source, 'add', 'link.txt')
    run(source, 'commit', '-qm', 'Synthetic link')
    with pytest.raises(ValueError, match='unapproved'):
        release.build(source, tmp_path / 'link.tar.gz')


def test_unreviewed_binary_is_rejected(source, tmp_path):
    (source / 'binary.dat').write_bytes(b'\xff\x00')
    run(source, 'add', 'binary.dat')
    run(source, 'commit', '-qm', 'Synthetic binary')
    with pytest.raises(ValueError, match='binary'):
        release.build(source, tmp_path / 'binary.tar.gz')


@pytest.mark.parametrize('path', ['../escape', '/absolute', '.env', 'data/example.txt',
                                  'config/offices/synthetic.json', '.github/workflows/test.yml'])
def test_unsafe_paths_are_rejected(path):
    assert not release.safe_path(path)


def test_changed_content_is_detected(source, tmp_path):
    original = tmp_path / 'original.tar.gz'
    release.build(source, original)
    corrupted = tmp_path / 'corrupted.tar.gz'
    with tarfile.open(original) as src, tarfile.open(corrupted, 'w:gz') as out:
        for member in src:
            data = src.extractfile(member).read()
            if member.name == 'README.md':
                data += b'changed'
                member.size = len(data)
            out.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match='checksum'):
        release.verify(corrupted)


def test_duplicate_archive_members_are_rejected(tmp_path):
    path = tmp_path / 'duplicate.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        for _ in range(2):
            info = tarfile.TarInfo('README.md')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='duplicate'):
        release.verify(path)


@pytest.mark.parametrize('fault', ['executable', 'owner', 'timestamp'])
def test_archive_identity_and_mode_metadata_cannot_be_changed(source, tmp_path, fault):
    original = tmp_path / 'mode-original.tar.gz'
    release.build(source, original)
    changed = tmp_path / 'mode-changed.tar.gz'
    with tarfile.open(original) as src, tarfile.open(changed, 'w:gz') as out:
        for member in src:
            data = src.extractfile(member).read()
            if member.name == 'README.md':
                if fault == 'executable':
                    member.mode = 0o755
                elif fault == 'owner':
                    member.uname = 'synthetic-private-name'
                else:
                    member.mtime = 1
            out.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError):
        release.verify(changed)
