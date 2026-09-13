"""Harness-only launch adapter for checkpointable OSWorld Docker/QEMU guests.

This module deliberately lives beside the benchmark entrypoints rather than in
the generic Agent packages.  It changes only the launch profile of subsequently
created Docker environments; no benchmark task, grader, or task-derived data is
read here.
"""
from __future__ import annotations

from env.qemu_rollback import (
    ROLLBACK_MIRROR,
    QemuRollbackError,
    normalize_verifier_execution_mode,
)


_PATCH_MARKER = "_forge_checkpointable_provider_v1"
_READ_ONLY_OVMF = (
    "-drive if=pflash,format=raw,unit=0,readonly=on,"
    "file=/usr/share/OVMF/edk2-x86_64-code.fd"
)


class _LoopbackContainers:
    """Keep the provider's host ports local without editing the release tree."""

    def __init__(self, collection):
        self._collection = collection

    def __getattr__(self, name):
        return getattr(self._collection, name)

    def run(self, *args, **kwargs):
        ports = kwargs.get("ports")
        if ports:
            kwargs["ports"] = {
                port: ("127.0.0.1", binding) if isinstance(binding, int)
                else binding for port, binding in ports.items()
            }
        return self._collection.run(*args, **kwargs)


class _LoopbackClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    @property
    def containers(self):
        return _LoopbackContainers(self._client.containers)


def prepare_checkpointable_docker_provider(mode: str | None) -> None:
    """Make future Docker guests checkpointable when mirror mode is selected."""
    if normalize_verifier_execution_mode(mode) != ROLLBACK_MIRROR:
        return

    from desktop_env.providers.docker.provider import DockerProvider

    current = DockerProvider.__init__
    if getattr(current, _PATCH_MARKER, False):
        return

    def checkpointable_init(self, region):
        current(self, region)
        self.client = _LoopbackClient(self.client)
        existing_flags = str(self.environment.get("CPU_FLAGS", "") or "")
        flags = [part for part in existing_flags.split(",") if part]
        flags.extend(["-invtsc", "migratable=yes"])
        existing_arguments = str(
            self.environment.get("ARGUMENTS", "") or "").strip()
        if "if=pflash" in existing_arguments:
            raise QemuRollbackError(
                "custom Docker QEMU arguments already define a pflash device")
        arguments = " ".join(
            part for part in (existing_arguments, _READ_ONLY_OVMF) if part)
        self.environment.update({
            "CPU_MODEL": "host",
            "CPU_FLAGS": ",".join(flags),
            # Suppress qemu-docker's writable -pflash attachment. The same OVMF
            # code image is reattached read-only through ARGUMENTS above.
            "BOOT_MODE": "legacy",
            "ARGUMENTS": arguments,
        })

    setattr(checkpointable_init, _PATCH_MARKER, True)
    setattr(checkpointable_init, "_forge_original", current)
    DockerProvider.__init__ = checkpointable_init


__all__ = ["prepare_checkpointable_docker_provider"]
