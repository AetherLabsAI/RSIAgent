"""Prepare pinned local images and check host-specific transport readiness."""

import json
import os
import subprocess
from pathlib import Path

from .host import IMAGE_REVISION, install_ale_path, provider_config
from .protocol import RSIAGENT, protocol, source_fingerprint, write_json


async def prepare(args):
    install_ale_path(args.ale_root)
    protocol(args.ale_root)
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError(
            "Read/write access to /dev/kvm is required; grant this user the kvm group and start a new login"
        )
    import docker

    client = docker.from_env()
    client.ping()
    client.close()
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            args.runner_image,
            str(RSIAGENT / "benchmarks/ale/checkpoint_runner"),
        ],
        check=True,
    )
    from ale_run.environments.providers.qemu import QemuProvider

    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    snapshot = provider.config.snapshots[
        "cpu-free-ubuntu" if args.os == "linux" else "cpu-free"
    ]
    disk = await provider._resolve_disk(snapshot)
    ready = Path(args.cache) / "rsiagent-readiness"
    ready.mkdir(parents=True, exist_ok=True)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
        text=True,
    ).strip()
    record_path = ready / "images.json"
    disks = {}
    if record_path.is_file():
        previous = json.loads(record_path.read_text())
        if (
            previous.get("revision") == IMAGE_REVISION
            and previous.get("runner_image") == image_id
        ):
            disks.update(previous["disks"])
    disks[args.os] = str(disk)
    write_json(
        ready / "images.json",
        {
            "revision": IMAGE_REVISION,
            "runner_image": image_id,
            "disks": disks,
            "verified_by": "pinned upstream QEMU provider checksum validation",
        },
    )
    print(
        json.dumps(
            {"status": "images_ready", "record": str(ready / "images.json")}, indent=2
        )
    )


def validate_readiness(args, manifest):
    if not Path(args.worker_python).is_file():
        raise RuntimeError(
            "--worker-python does not point to the prepared RSIAgent environment"
        )
    root = Path(args.cache) / "rsiagent-readiness"
    images = json.loads((root / "images.json").read_text())
    platforms = {t["os"] for t in manifest["tasks"] if t["status"] == "ready"}
    if images.get("revision") != IMAGE_REVISION or any(
        platform not in images["disks"] or not Path(images["disks"][platform]).is_file()
        for platform in platforms
    ):
        raise RuntimeError("Pinned image readiness record is invalid")
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
        text=True,
    ).strip()
    if image_id != images["runner_image"]:
        raise RuntimeError("Checkpoint runner image changed since preparation")
    fingerprint = source_fingerprint()
    for os_type in platforms:
        result = json.loads((root / f"smoke-{os_type}.json").read_text())
        if (
            result.get("status") != "passed"
            or result.get("source_sha256") != fingerprint
            or result.get("runner_image") != image_id
        ):
            raise RuntimeError(
                f"{os_type} smoke must pass on this exact release and runner image"
            )
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("/dev/kvm access was lost")
