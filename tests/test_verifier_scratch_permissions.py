"""Lossless trusted scratch export, including transport ownership and failures."""
import io
import os
from pathlib import Path
import shlex
import tarfile

import pytest

from core.verifier_runtime import (
    AgenticVerifierExecutor, AgenticVerifierInfrastructureError, _trace,
)


class TransferVM:
    env = None

    def __init__(self):
        self.fetched = []

    def fetch_file(self, path, max_bytes=None):
        assert max_bytes is None
        source = Path(path)
        assert source.stat().st_uid == os.getuid()
        assert source.stat().st_mode & 0o777 == 0o600
        self.fetched.append(path)
        return source.read_bytes(), ""

    def run_command(self, command, **kwargs):
        tokens = shlex.split(command)
        assert tokens[:3] == ["rm", "-f", "--"]
        Path(tokens[3]).unlink(missing_ok=True)


def exporter(tmp_path):
    vm = TransferVM()
    executor = AgenticVerifierExecutor(vm)
    executor._desktop_uid = os.getuid()
    executor._desktop_gid = os.getgid()
    executor._workspace = str(tmp_path / "scratch")
    calls = []

    def trusted_helper(lang, code, *, timeout, as_desktop=True):
        # The real namespace helper performs privilege selection. Execute the
        # fixed Python transport locally to exercise actual tar bytes and cleanup.
        assert as_desktop is False
        calls.append(as_desktop)
        try:
            exec(compile(code, "<trusted export>", "exec"), {})
            return _trace("exported")
        except Exception as exc:
            return _trace(type(exc).__name__ + ": " + str(exc), 1)

    executor._run_in_private_mount_namespace = trusted_helper
    return executor, vm, calls


def test_export_preserves_regular_bytes_metadata_and_omits_links(tmp_path):
    source = tmp_path / "scratch"
    source.mkdir()
    (source / "nested").mkdir()
    payload = b"\x00\xffevidence\n" * 200_000
    f = source / "nested" / "[Content_Types].xml"
    f.write_bytes(payload)
    f.chmod(0o640)
    before = f.stat()
    (source / "outside-link").symlink_to("/etc/passwd")
    (source / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(source / "pipe")
    executor, vm, calls = exporter(tmp_path)
    archive = executor._export_regular_tree(str(source))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        assert set(tf.getnames()) == {"nested", "nested/[Content_Types].xml"}
        assert tf.extractfile("nested/[Content_Types].xml").read() == payload
        assert tf.getmember("nested/[Content_Types].xml").mode == 0o640
    after = f.stat()
    assert (after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns) == (
        before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns)
    assert calls == [False]
    assert len(vm.fetched) == 1 and not Path(vm.fetched[0]).exists()


def test_directory_walk_failure_cannot_publish_partial_archive(tmp_path, monkeypatch):
    source = tmp_path / "scratch"
    source.mkdir()
    executor, vm, calls = exporter(tmp_path)

    def failed_walk(root):
        raise PermissionError("permission denied reading directory")

    monkeypatch.setattr(os, "listdir", failed_walk)
    with pytest.raises(AgenticVerifierInfrastructureError, match="permission denied"):
        executor._export_regular_tree(str(source))
    assert vm.fetched == []
    assert calls == [False, False]


def test_missing_source_fails_without_fetch_or_fallback(tmp_path):
    executor, vm, calls = exporter(tmp_path)
    executor._workspace = str(tmp_path / "missing")
    with pytest.raises(AgenticVerifierInfrastructureError, match="No such file"):
        executor._export_regular_tree(str(tmp_path / "missing"))
    assert vm.fetched == [] and calls == [False]
