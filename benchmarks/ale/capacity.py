"""Cooperative host-wide reservations, including VMs from the earlier baseline."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import time
import uuid
from pathlib import Path


class Capacity:
    def __init__(self, root, disk, *, limit=16):
        self.root, self.disk, self.limit = Path(root), Path(disk), limit
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = uuid.uuid4().hex

    async def acquire(self, count, reserve_gib, stop, *, vcpus=0, memory_gb=0):
        if count > self.limit:
            raise RuntimeError("One phase needs more VM slots than the host limit")
        if vcpus > (os.cpu_count() or 1):
            raise RuntimeError(
                "This phase requests more vCPUs than this host has logical CPUs"
            )
        import docker

        client = docker.from_env()
        try:
            while not stop.is_set():
                with (self.root / "lock").open("a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    leases = []
                    for path in self.root.glob("*.json"):
                        data = json.loads(path.read_text())
                        try:
                            os.kill(data["pid"], 0)
                        except ProcessLookupError:
                            path.unlink()
                            continue
                        except PermissionError:
                            pass  # Another process still owns this live process.
                        leases.append(data)
                    if any(
                        lease.get("vm_limit", self.limit) != self.limit
                        for lease in leases
                    ):
                        raise RuntimeError(
                            "All sessions sharing the host pool must use the same VM limit"
                        )
                    containers = client.containers.list()
                    # ALE QEMU guests expose their monitor in this container.
                    live = {
                        c.id
                        for c in containers
                        if c.name.startswith("ale-qemu-")
                        or any(
                            str(p).startswith("7100/")
                            for p in (
                                c.attrs.get("Config", {}).get("ExposedPorts") or {}
                            )
                        )
                        or "ale" in c.name
                        and "qemu"
                        in str(c.attrs.get("Config", {}).get("Image", "")).lower()
                    }
                    managed = {
                        cid for lease in leases for cid in lease.get("containers", [])
                    }
                    unmanaged = len(live - managed)
                    used = unmanaged + sum(lease["count"] for lease in leases)
                    reserved = sum(lease["reserve_gib"] for lease in leases)
                    free_gib = shutil.disk_usage(self.disk).free / 1024**3
                    available_ram = next(
                        int(line.split()[1]) / 1024**2
                        for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:")
                    )
                    cpu_reserved = sum(lease.get("vcpus", 0) for lease in leases)
                    ram_reserved = sum(lease.get("memory_gb", 0) for lease in leases)
                    if (
                        used + count <= self.limit
                        and free_gib >= 128 + reserved + reserve_gib
                        and cpu_reserved + vcpus <= (os.cpu_count() or 1)
                        and ram_reserved + memory_gb + 8 <= available_ram
                    ):
                        data = {
                            "pid": os.getpid(),
                            "count": count,
                            "reserve_gib": reserve_gib,
                            "containers": [],
                            "created": time.time(),
                            "vcpus": vcpus,
                            "memory_gb": memory_gb,
                            "vm_limit": self.limit,
                        }
                        (self.root / (self.token + ".json")).write_text(
                            json.dumps(data)
                        )
                        return
                await asyncio.sleep(5)
        finally:
            client.close()
        raise InterruptedError("Stopped while waiting for host VM capacity")

    def attach(self, container_id):
        import docker

        client = docker.from_env()
        try:
            container_id = client.containers.get(container_id).id
        finally:
            client.close()
        with (self.root / "lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            path = self.root / (self.token + ".json")
            data = json.loads(path.read_text())
            data["containers"].append(container_id)
            path.write_text(json.dumps(data))

    def release(self):
        with (self.root / "lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            (self.root / (self.token + ".json")).unlink(missing_ok=True)
