"""Exercise trusted scratch export against actual filesystem objects."""
import io
import os
from pathlib import Path
import tarfile

import pytest

from core.verifier_runtime import (
    AgenticVerifierExecutor, AgenticVerifierInfrastructureError,
)
from test_verifier_isolation import _RecordingVM, _runtime_trace


def local_exporter(tmp_path):
    class LocalTransfer(_RecordingVM):
        def fetch_file(self, path, max_bytes=None):
            assert max_bytes is None
            return Path(path).read_bytes(), ''

        def run_command(self, command, **kwargs):
            import shlex
            parts = shlex.split(command)
            assert parts[:3] == ['rm', '-f', '--']
            Path(parts[3]).unlink(missing_ok=True)
            return ''

    executor = AgenticVerifierExecutor(LocalTransfer())
    root = tmp_path / 'scratch'
    root.mkdir()
    executor._workspace = str(root)
    executor._desktop_uid = os.getuid()
    executor._desktop_gid = os.getgid()

    def trusted_helper(lang, code, *, timeout, as_desktop=True):
        assert as_desktop is False
        try:
            exec(compile(code, '<trusted-export>', 'exec'), {})
            return _runtime_trace('exported', 0)
        except OSError as exc:
            return _runtime_trace(f'{type(exc).__name__}: {exc}', 1)

    executor._run_in_private_mount_namespace = trusted_helper
    return executor, root


def test_export_preserves_regular_bytes_without_following_links(tmp_path):
    executor, root = local_exporter(tmp_path)
    (root / 'nested').mkdir()
    content = b'\x00binary evidence\xff\n' * 100
    (root / 'nested/evidence').write_bytes(content)
    os.link(root / 'nested/evidence', root / 'alias')
    (root / 'empty').mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'not part of scratch')
    (root / 'escape').symlink_to(outside)
    (root / 'escape_dir').symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / 'fifo')
    blob = executor._export_regular_tree(str(root))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert set(tf.getnames()) == {'nested', 'nested/evidence', 'alias', 'empty'}
        for name in ['nested/evidence', 'alias']:
            assert tf.getmember(name).isfile()
            assert tf.extractfile(name).read() == content
    assert (root / 'escape').is_symlink()
    assert outside.read_bytes() == b'not part of scratch'


def test_export_rejects_source_outside_executor_workspace(tmp_path):
    executor, root = local_exporter(tmp_path)
    with pytest.raises(AgenticVerifierInfrastructureError, match='escaped'):
        executor._export_regular_tree(str(tmp_path))


@pytest.mark.parametrize('prefix', ['../escape', '/absolute', 'safe/../../escape'])
def test_export_rejects_unsafe_archive_prefix(tmp_path, prefix):
    executor, root = local_exporter(tmp_path)
    with pytest.raises(AgenticVerifierInfrastructureError, match='unsafe'):
        executor._export_regular_tree(str(root), archive_prefix=prefix)


def test_export_does_not_follow_a_file_replaced_by_symlink(tmp_path, monkeypatch):
    executor, root = local_exporter(tmp_path)
    (root / 'swap').write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private unrelated content')
    original_open = os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == 'swap' and kwargs.get('dir_fd') is not None and not replaced:
            replaced = True
            (root / 'swap').unlink()
            (root / 'swap').symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', racing_open)
    with pytest.raises(AgenticVerifierInfrastructureError, match='archive'):
        executor._export_regular_tree(str(root))
    assert replaced
    assert outside.read_bytes() == b'private unrelated content'


def test_failed_export_is_removable_by_desktop_controller(tmp_path, monkeypatch):
    executor, root = local_exporter(tmp_path)
    owned = set()
    removed = []
    fchown = os.fchown
    cleanup = executor._vm.run_command

    def transfer_ownership(fd, uid, gid):
        owned.add(os.fstat(fd).st_ino)
        return fchown(fd, uid, gid)

    def failed_directory_read(fd):
        raise PermissionError('permission denied during scratch export')

    def desktop_cleanup(command, **kwargs):
        import shlex
        path = Path(shlex.split(command)[3])
        # Model the desktop controller: it cannot unlink another user's file
        # in the sticky /tmp or /dev/shm transport directory.
        if path.exists() and path.stat().st_ino not in owned:
            raise PermissionError('desktop cannot remove root-owned partial tar')
        removed.append(path)
        return cleanup(command, **kwargs)

    monkeypatch.setattr(os, 'fchown', transfer_ownership)
    monkeypatch.setattr(os, 'listdir', failed_directory_read)
    monkeypatch.setattr(executor._vm, 'run_command', desktop_cleanup)
    with pytest.raises(AgenticVerifierInfrastructureError, match='during scratch export'):
        executor._export_regular_tree(str(root))
    assert len(removed) == 2
    assert all(not path.exists() for path in removed)
