"""Pinned OSWorld task and asset content identities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

EXPECTED_TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))


class PreflightError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def task_content_identity(task_root: Path) -> dict:
    """Return a release-sensitive identity for the complete task source set."""
    rows = []
    total_bytes = 0
    for task in EXPECTED_TASKS:
        path = task_root / f"{task}.py"
        if not path.is_file():
            raise PreflightError(f"task source is missing: {path}")
        size = path.stat().st_size
        total_bytes += size
        rows.append(f"{path.name}\t{size}\t{sha256_file(path)}")
    return {
        "content_tree_sha256": sha256_bytes("\n".join(rows).encode("utf-8")),
        "total_bytes": total_bytes,
        "task_count": len(rows),
    }


def asset_content_identity(asset_root: Path, marker_name: str) -> dict:
    """Hash the prepared asset snapshot while excluding downloader metadata."""
    rows = []
    total_bytes = 0
    for path in sorted(asset_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(asset_root)
        if relative.parts and relative.parts[0] == ".cache":
            continue
        if relative.as_posix() == marker_name:
            continue
        size = path.stat().st_size
        total_bytes += size
        rows.append(f"{relative.as_posix()}\t{size}\t{sha256_file(path)}")
    return {
        "content_tree_sha256": sha256_bytes("\n".join(rows).encode("utf-8")),
        "total_bytes": total_bytes,
        "file_count": len(rows),
    }


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"expected a JSON object: {path}")
    return value
