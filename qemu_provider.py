"""Harness-only launch adapter for checkpointable OSWorld Docker/QEMU guests.

This module deliberately lives beside the benchmark entrypoints rather than in
the generic Agent packages.  It changes only the launch profile of subsequently
created Docker environments; no benchmark task, grader, or task-derived data is
read here.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from filelock import FileLock, Timeout

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


@contextmanager
def _boot_slot():
    """Bound simultaneous cold boots across processes, not running guests."""
    directory = Path(f"/tmp/forge-vm-boots-{os.getuid()}")
    directory.mkdir(mode=0o700, exist_ok=True)
    deadline = time.monotonic() + 1800
    slots = [FileLock(str(directory / f"slot-{i}.lock")) for i in range(2)]
    while True:
        for lock in slots:
            try:
                lock.acquire(timeout=0)
            except Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for a shared VM boot slot")
        time.sleep(0.5)


def _prepare_boot_lifecycle(module):
    provider = module.DockerProvider
    start = getattr(provider, "start_emulator", None)
    if start is None or getattr(start, _PATCH_MARKER, False):
        return
    module.LOCK_TIMEOUT = 300
    wait_ready = provider._wait_for_vm_ready
    used_ports = provider._get_used_ports

    def cached_ports(self):
        if getattr(self, "_forge_allocating", False):
            if self._forge_ports is None:
                self._forge_ports = used_ports(self)
            return self._forge_ports
        return used_ports(self)

    def ready(self, timeout=600):
        try:
            return wait_ready(self, timeout=timeout)
        except TimeoutError as exc:
            owner = os.environ.get("FORGE_VM_OWNER", "")
            if (str(exc) == "VM failed to become ready within timeout period"
                    and owner):
                # Upstream removes this guest before start_emulator propagates
                # the error. Capture cold-boot evidence first, then remove its
                # anonymous volume too. No task setup or Agent ran in this VM.
                try:
                    _archive_failed_boot(self, owner, timeout)
                    self._forge_retryable_boot_timeout = True
                except Exception as diagnostic_error:
                    import logging
                    logging.getLogger(__name__).warning(
                        "cold-boot evidence/cleanup failed: %s", diagnostic_error)
            raise

    def start_guarded(self, *args, **kwargs):
        for attempt in range(2):
            with _boot_slot():
                self._forge_allocating = True
                self._forge_ports = None
                self._forge_retryable_boot_timeout = False
                try:
                    return start(self, *args, **kwargs)
                except BaseException as exc:
                    # Force-remove only this provider's own failed guest.
                    container = self.container
                    if container is not None:
                        try:
                            container.remove(force=True, v=True)
                        except Exception as cleanup_error:
                            import logging
                            logging.getLogger(__name__).warning(
                                "failed-boot cleanup: %s", cleanup_error)
                        self.container = None
                    if not (attempt == 0 and isinstance(exc, TimeoutError)
                            and self._forge_retryable_boot_timeout):
                        raise
                finally:
                    self._forge_allocating = False
                    self._forge_ports = None
            # Retry the pristine provider launch, never Actor/Verifier actions
            # or task setup. Release/reacquire the shared two-boot admission.

    setattr(start_guarded, _PATCH_MARKER, True)
    provider.start_emulator = start_guarded
    provider._wait_for_vm_ready = ready
    provider._get_used_ports = cached_ports
    finalize = getattr(provider, "finalize_volume", None)
    if finalize is not None:
        def finalize_with_controller_oom_policy(
                self, path_to_vm, volume_size, os_type, controller,
                setup_controller, client_password):
            result = finalize(self, path_to_vm, volume_size, os_type, controller,
                              setup_controller, client_password)
            if os_type == "Ubuntu":
                # This runs after pristine guest readiness, before task setup
                # can restrict desktop sudo and before any Agent/QEMU snapshot.
                from types import SimpleNamespace
                from env.controller_oom import ensure_controller_oom_policy
                desktop = SimpleNamespace(provider=self, controller=controller,
                                          client_password=client_password)
                self._forge_controller_oom_policy = ensure_controller_oom_policy(desktop)
            return result
        provider.finalize_volume = finalize_with_controller_oom_policy
    stop = getattr(provider, "stop_emulator", None)
    if stop is not None:
        def stop_guarded(self, *args, **kwargs):
            owned = self.container
            if owned is None:
                return
            try:
                owned.stop(timeout=10)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "graceful guest stop failed; forcing owned cleanup: %s", exc)
            # Retain the handle if removal fails so a later cleanup can retry.
            # Disposable guests own their anonymous /storage volumes.
            owned.remove(force=True, v=True)
            self.container = None
            self.server_port = self.vnc_port = None
            self.chromium_port = self.vlc_port = None
        provider.stop_emulator = stop_guarded


def _archive_failed_boot(provider, owner, timeout):
    """Host-only evidence and positive ownership before a bounded cold retry."""
    container = provider.container
    container.reload()
    labels = container.labels
    if (labels.get("forge.owner") != owner
            or labels.get("forge.host_pid") != str(os.getpid())
            or not labels.get("forge.launch_token")):
        raise QemuRollbackError("failed boot ownership mismatch")
    root = Path(os.environ.get("FORGE_BOOT_DIAGNOSTICS_DIR",
                               str(Path(__file__).resolve().parent / "results/boot_failures")))
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                      + "_" + labels["forge.launch_token"])
    receipt.mkdir(mode=0o700)
    log = container.logs(stdout=True, stderr=True)
    (receipt / "container.log").write_bytes(log)
    details = {"owner": owner, "host_pid": os.getpid(),
               "container_id": container.id, "labels": labels,
               "timeout_seconds": timeout,
               "state": container.attrs.get("State", {}),
               "ports": container.attrs.get("NetworkSettings", {}).get("Ports"),
               "log_sha256": hashlib.sha256(log).hexdigest(),
               "stage": "provider_boot_before_task_setup_or_agent",
               "removed_with_anonymous_volume": False}
    path = receipt / "receipt.json"
    path.write_text(json.dumps(details, indent=2) + "\n")
    container.remove(force=True, v=True)
    provider.container = None
    provider.server_port = provider.vnc_port = None
    provider.chromium_port = provider.vlc_port = None
    details["removed_with_anonymous_volume"] = True
    path.write_text(json.dumps(details, indent=2) + "\n")


class _LoopbackContainers:
    """Keep the provider's host ports local without editing the release tree."""

    def __init__(self, collection):
        self._collection = collection

    def __getattr__(self, name):
        return getattr(self._collection, name)

    def run(self, *args, **kwargs):
        owner = os.environ.get("FORGE_VM_OWNER")
        if os.environ.get("FORGE_DNSMASQ_START_RETRY") == "1":
            # A transient host inotify quota failure makes qemu-docker silently
            # fall back to networking without host port forwarding. Retry only
            # that dnsmasq startup error inside the new container.
            wrapper = Path(__file__).resolve().parent / "tools/dnsmasq_start_retry.sh"
            environment = dict(kwargs.get("environment") or {})
            if environment.get("DNSMASQ", "/usr/sbin/dnsmasq") != "/usr/sbin/dnsmasq":
                raise QemuRollbackError("unexpected custom dnsmasq launcher")
            kwargs["environment"] = {**environment, "DNSMASQ": "/run/forge-dnsmasq-start"}
            kwargs["volumes"] = {**(kwargs.get("volumes") or {}), str(wrapper): {
                "bind": "/run/forge-dnsmasq-start", "mode": "ro"}}
        launch_token = uuid.uuid4().hex if owner else None
        if owner:
            kwargs["labels"] = {**(kwargs.get("labels") or {}),
                                "forge.owner": owner,
                                "forge.host_pid": str(os.getpid()),
                                "forge.launch_token": launch_token}
        ports = kwargs.get("ports")
        if ports:
            kwargs["ports"] = {
                port: ("127.0.0.1", binding) if isinstance(binding, int)
                else binding for port, binding in ports.items()
            }
        try:
            return self._collection.run(*args, **kwargs)
        except Exception:
            # Docker can create a container and then time out starting it before
            # returning its handle. The unique token identifies only this launch.
            if launch_token:
                try:
                    for container in self._collection.list(all=True, filters={
                            "label": f"forge.launch_token={launch_token}"}):
                        container.remove(force=True, v=True)
                except Exception as cleanup_error:
                    import logging
                    logging.getLogger(__name__).warning(
                        "incomplete container launch cleanup: %s", cleanup_error)
            raise


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

    from desktop_env.providers.docker import provider as provider_module
    DockerProvider = provider_module.DockerProvider

    current = DockerProvider.__init__
    if getattr(current, _PATCH_MARKER, False):
        return

    def checkpointable_init(self, region):
        current(self, region)
        if hasattr(self.client, "api"):
            self.client.api.timeout = 300
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
        if os.environ.get("FORGE_TRUSTED_VERIFIER_TRANSPORT") == "virtio":
            from env.verifier_control import QEMU_ARGUMENTS
            arguments += " " + QEMU_ARGUMENTS
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
    _prepare_boot_lifecycle(provider_module)
    if os.environ.get("FORGE_TRUSTED_VERIFIER_TRANSPORT") == "virtio":
        from desktop_env.desktop_env import DesktopEnv
        from env.verifier_control import provision_verifier_control
        original_setup = DesktopEnv._setup_task

        def setup_with_verifier_control(self, task_config, use_proxy):
            # reset() can replace the VM. Provision each fresh container before
            # benchmark setup locks down the desktop user's privileges.
            container_id = self.provider.container.id
            if getattr(self, "_forge_control_container_id", None) != container_id:
                provision_verifier_control(self)
                self._forge_control_container_id = container_id
            result = original_setup(self, task_config, use_proxy)
            return result

        DesktopEnv._setup_task = setup_with_verifier_control


__all__ = ["prepare_checkpointable_docker_provider"]
