"""Regression checks for the infrastructure faults that stopped RSI training."""
import hashlib
import io
import sys
import tarfile
import types

import pytest

from explore.provisioning import _inventory_from_tgz, _symlink_digest
from benchmarks.osworld.provider import prepare_checkpointable_docker_provider


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
            else:
                member.linkname = content
                stream.addfile(member)
    return buffer.getvalue()


def test_guest_absolute_symlink_is_hashed_without_host_extraction(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("archive inventory must never extract on the host")
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    manifest, links = _inventory_from_tgz(archive([
        ("evolution_project/venv/bin/python3", tarfile.SYMTYPE, "/usr/bin/python3"),
        ("evolution_project/broken", tarfile.SYMTYPE, "../missing"),
        ("evolution_project/data", tarfile.REGTYPE, b"candidate bytes"),
        ("evolution_project/copy", tarfile.LNKTYPE, "evolution_project/data"),
    ]))
    assert manifest["evolution_project/venv/bin/python3"] == _symlink_digest("/usr/bin/python3")
    assert links["evolution_project/broken"] == "../missing"
    assert manifest["evolution_project/data"] == hashlib.sha256(b"candidate bytes").hexdigest()
    assert manifest["evolution_project/copy"] == manifest["evolution_project/data"]


@pytest.mark.parametrize("entries", [
    [("../escape", tarfile.REGTYPE, b"x")],
    [("/absolute", tarfile.REGTYPE, b"x")],
    [("project/link", tarfile.SYMTYPE, "/tmp"), ("project/link/file", tarfile.REGTYPE, b"x")],
    [("project/file", tarfile.REGTYPE, b"x"), ("project/file", tarfile.REGTYPE, b"y")],
    [("project/link", tarfile.LNKTYPE, "/etc/passwd")],
    [("project/link", tarfile.LNKTYPE, "project/link")],
    [("project/fifo", tarfile.FIFOTYPE, "")],
])
def test_archive_rejects_unsafe_or_ambiguous_member_layout(entries):
    with pytest.raises(ValueError):
        _inventory_from_tgz(archive(entries))


def test_launch_adapter_keeps_release_provider_ports_local(monkeypatch):
    calls = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
        list=lambda: ["existing guest"])
    class Provider:
        def __init__(self, region):
            self.client = types.SimpleNamespace(containers=collection)
            self.environment = {"RAM_SIZE": "4G"}
    module = types.ModuleType("desktop_env.providers.docker.provider")
    module.DockerProvider = Provider
    package = types.ModuleType("desktop_env.providers.docker")
    package.provider = module
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    prepare_checkpointable_docker_provider("rollback_mirror")
    prepare_checkpointable_docker_provider("rollback_mirror")
    provider = Provider("local")
    original = {5000: 5001, 8006: 8007, 9222: 9223, 8080: 8081}
    provider.client.containers.run("unchanged-image", ports=original, detach=True)
    assert calls == [(("unchanged-image",), {
        "ports": {port: ("127.0.0.1", value) for port, value in original.items()},
        "detach": True})]
    assert original[5000] == 5001
    assert provider.client.containers.list() == ["existing guest"]
    assert provider.environment["RAM_SIZE"] == "4G"
    assert provider.environment["CPU_FLAGS"].count("migratable=yes") == 1
