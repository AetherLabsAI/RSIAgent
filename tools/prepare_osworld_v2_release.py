#!/usr/bin/env python3
"""Prepare and verify the benchmark inputs pinned by a Forge baseline lock."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_osworld_v2_baseline_shard import (  # noqa: E402
    PreflightError,
    asset_content_identity,
    read_json,
    task_content_identity,
)


DEFAULT_LOCK = (
    REPO / "config/osworld_v2_0808_glm53_k3_agentic_baseline.lock.json")
DEFAULT_OSWORLD_ROOT = REPO.parent / "OSWorld-V2"


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True,
            stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(f"cannot inspect Git checkout: {repo}") from exc


def _run(command: list[str], cwd: Path) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_remote_revision(metadata: dict) -> None:
    from huggingface_hub import HfApi

    observed = HfApi().repo_info(
        repo_id=str(metadata["repository"]),
        repo_type=str(metadata.get("repo_type", "dataset")),
        revision=str(metadata["tag"]),
    ).sha
    if observed != metadata["commit"]:
        raise PreflightError(
            f"{metadata['repository']}@{metadata['tag']} resolved to {observed}, "
            f"not locked commit {metadata['commit']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--osworld-root", type=Path, default=DEFAULT_OSWORLD_ROOT)
    parser.add_argument(
        "--verify-only", action="store_true",
        help="verify an already-prepared release without downloading anything")
    parser.add_argument(
        "--refresh-assets", action="store_true",
        help="replace the dedicated release asset directory before downloading")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    lock_path = args.lock.resolve()
    osworld_root = args.osworld_root.resolve()
    lock = read_json(lock_path)
    release = str(lock.get("benchmark_release", ""))
    if not release:
        raise PreflightError("the selected lock has no benchmark_release")

    expected_osworld = str((lock.get("osworld_code") or {}).get("commit", ""))
    observed_osworld = _git(osworld_root, "rev-parse", "HEAD")
    if observed_osworld != expected_osworld:
        raise PreflightError(
            "switch OSWorld-V2 to the locked release before preparation: "
            f"expected {expected_osworld}, observed {observed_osworld}")

    task_meta = lock.get("task_files") or {}
    asset_meta = lock.get("task_assets") or {}
    _require_remote_revision(task_meta)
    _require_remote_revision(asset_meta)

    release_manifest = (
        osworld_root / "benchmark_releases" / f"{release}.json")
    if not release_manifest.is_file():
        raise PreflightError(
            f"OSWorld release manifest is missing: {release_manifest}")
    release_meta = lock.get("release_manifest") or {}
    expected_manifest_hash = str(release_meta.get("sha256", ""))
    observed_manifest_hash = hashlib.sha256(
        release_manifest.read_bytes()).hexdigest()
    if observed_manifest_hash != expected_manifest_hash:
        raise PreflightError(
            "OSWorld release manifest does not match the lock: "
            f"expected {expected_manifest_hash}, observed {observed_manifest_hash}")

    task_root = osworld_root / "evaluation_examples/task_class"
    asset_root = osworld_root / str(asset_meta["local_dir"])
    marker_name = str(asset_meta.get(
        "release_marker", ".forge_osworld_release.json"))
    marker_path = asset_root / marker_name

    if not args.verify_only:
        if asset_root.exists() and any(asset_root.iterdir()) and not args.refresh_assets:
            if not marker_path.is_file():
                raise PreflightError(
                    "the dedicated asset directory is non-empty but has no valid "
                    "release marker; inspect it or pass --refresh-assets explicitly: "
                    + str(asset_root))
            prior_marker = read_json(marker_path)
            if (prior_marker.get("benchmark_release") != release
                    or prior_marker.get("commit") != asset_meta.get("commit")):
                raise PreflightError(
                    "the asset directory belongs to another release; pass "
                    "--refresh-assets explicitly: " + str(asset_root))
        task_command = [
            str(Path(sys.executable).resolve()),
            str(osworld_root / "scripts/tools/download_osworld_v2_tasks.py"),
            "--benchmark-release", str(release_manifest),
            "--target-dir", str(task_root),
        ]
        asset_command = [
            str(Path(sys.executable).resolve()),
            str(osworld_root / "scripts/tools/download_osworld_v2_assets.py"),
            "--benchmark-release", str(release_manifest),
            "--target-dir", str(asset_root),
        ]
        if args.refresh_assets:
            asset_command.append("--clean")
        if args.dry_run:
            task_command.append("--dry-run")
            asset_command.append("--dry-run")
        _run(task_command, osworld_root)
        _run(asset_command, osworld_root)
        if args.dry_run:
            return 0

    task_identity = task_content_identity(task_root)
    expected_task_identity = {
        "content_tree_sha256": task_meta.get("content_tree_sha256"),
        "total_bytes": task_meta.get("total_bytes"),
        "task_count": int(lock["task_count"]),
    }
    if task_identity != expected_task_identity:
        raise PreflightError(
            "downloaded task sources do not match the lock: "
            f"expected={expected_task_identity}, observed={task_identity}")

    if args.verify_only:
        if not marker_path.is_file():
            raise PreflightError(f"asset release marker is missing: {marker_path}")
        marker = read_json(marker_path)
        expected_marker = {
            "benchmark_release": release,
            "repository": asset_meta.get("repository"),
            "repo_type": asset_meta.get("repo_type"),
            "tag": asset_meta.get("tag"),
            "commit": asset_meta.get("commit"),
        }
        mismatch = {
            key: {"expected": value, "observed": marker.get(key)}
            for key, value in expected_marker.items()
            if marker.get(key) != value}
        if mismatch:
            raise PreflightError(f"asset release marker mismatch: {mismatch}")
        observed_asset_identity = asset_content_identity(asset_root, marker_name)
        if marker.get("content_identity") != observed_asset_identity:
            raise PreflightError(
                "asset snapshot changed after preparation: "
                f"expected={marker.get('content_identity')}, "
                f"observed={observed_asset_identity}")
    else:
        content_identity = asset_content_identity(asset_root, marker_name)
        if content_identity["file_count"] == 0:
            raise PreflightError("asset download produced an empty snapshot")
        marker = {
            "schema_version": 1,
            "created_at": _utc_now(),
            "benchmark_release": release,
            "repository": asset_meta["repository"],
            "repo_type": asset_meta.get("repo_type", "dataset"),
            "tag": asset_meta["tag"],
            "commit": asset_meta["commit"],
            "content_identity": content_identity,
        }
        _write_json_atomic(marker_path, marker)

    print(f"benchmark_release={release}")
    print(f"osworld_commit={observed_osworld}")
    print(f"task_content_tree_sha256={task_identity['content_tree_sha256']}")
    print(f"asset_root={asset_root}")
    print(f"asset_content_tree_sha256={marker['content_identity']['content_tree_sha256']}")
    print("release_preparation=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PreflightError, subprocess.CalledProcessError) as exc:
        print(f"PREPARATION ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
