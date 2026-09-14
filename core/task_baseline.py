"""Trusted post-setup/pre-Actor baseline capture for unified target runs.

This module records mechanical persistent state only.  It contains no task-specific
checks, evaluator, golden answer, semantic classifier, or stopping policy.  The
Verifier Agent independently inspects the same live S0 and decides what matters.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


_BEGIN = "__RSIAGENT_TASK_BASELINE_BEGIN__"
_END = "__RSIAGENT_TASK_BASELINE_END__"


def _capture_program() -> str:
    # json.dumps transports arbitrary filenames without line-oriented ambiguity.
    # ~/.memory is a harness-attached Actor policy surface, not task-start state.
    return r'''import hashlib
import json
import os
import pathlib
import stat

roots = [pathlib.Path("/home/user")]
excluded = {pathlib.Path("/home/user/.memory")}
entries = []

def excluded_path(path):
    return any(path == root or root in path.parents for root in excluded)

def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

for root in roots:
    if not root.exists():
        continue
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = pathlib.Path(current)
        visible_dirs = sorted(
            name for name in dirs
            if not excluded_path(current_path / name))
        symlink_dirs = [
            name for name in visible_dirs
            if (current_path / name).is_symlink()]
        dirs[:] = [
            name for name in visible_dirs
            if not (current_path / name).is_symlink()]
        # Real directories are recorded as `current_path` when walked; directory
        # symlinks are not followed and therefore must be recorded here.
        names = ["."] + symlink_dirs + sorted(files)
        for name in names:
            path = current_path if name == "." else current_path / name
            if excluded_path(path):
                continue
            try:
                info = path.lstat()
                record = {
                    "path": str(path),
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
                if stat.S_ISREG(info.st_mode):
                    record.update(type="file", sha256=digest_file(path))
                elif stat.S_ISDIR(info.st_mode):
                    record.update(type="directory")
                elif stat.S_ISLNK(info.st_mode):
                    record.update(type="symlink", target=os.readlink(path))
                else:
                    record.update(type="special")
            except Exception as exc:
                record = {
                    "path": str(path),
                    "type": "unreadable",
                    "error": type(exc).__name__,
                }
            entries.append(record)

entries.sort(key=lambda item: (item["path"], item["type"]))
print("__RSIAGENT_TASK_BASELINE_BEGIN__")
print(json.dumps({"entries": entries}, ensure_ascii=False,
                 sort_keys=True, separators=(",", ":")))
print("__RSIAGENT_TASK_BASELINE_END__")
'''


def _digest(entries: list[dict[str, Any]], *, exact: bool) -> str:
    projected = []
    for entry in entries:
        item = dict(entry)
        if not exact:
            item.pop("mtime_ns", None)
            item.pop("uid", None)
            item.pop("gid", None)
        projected.append(item)
    encoded = json.dumps(
        projected, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_task_start_baseline(vm) -> dict[str, Any]:
    """Capture all persistent state below the task user's home without a cap.

    The live Verifier can also inspect non-file application state during its
    orientation.  External-service state cannot be snapshotted universally by this
    local harness and is explicitly marked unavailable rather than guessed.
    """
    trace = vm.run_script("python", _capture_program(), timeout=3600, cap=0)
    if trace.infra_fail or trace.exit_code != 0:
        raise RuntimeError(
            "trusted task-start baseline capture failed: " + trace.stdout)
    output = trace.stdout or ""
    start = output.find(_BEGIN + "\n")
    end = output.find("\n" + _END, start + len(_BEGIN) + 1)
    if start < 0 or end < 0:
        raise RuntimeError(
            "trusted task-start baseline capture returned no complete manifest")
    raw = output[start + len(_BEGIN) + 1:end]
    parsed = json.loads(raw)
    entries = parsed.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("trusted task-start baseline manifest is malformed")
    return {
        "schema_version": 1,
        "scope": ["/home/user"],
        "excluded_actor_private": ["/home/user/.memory"],
        "external_service_state": "unavailable_to_local_baseline_capture",
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": _digest(entries, exact=False),
        "exact_sha256": _digest(entries, exact=True),
    }


def stable_task_start_identity(
        *, task_id: str, instruction: str, setup_projection: str,
        setup_manifest: dict[str, Any],
        target_input_fingerprint: str) -> dict[str, Any]:
    """Bind the reproducible, task-visible identity of one clean S0.

    The exhaustive home manifest intentionally contains volatile application and
    cache state, so it is an audit artifact rather than a clone-equivalence key.
    This digest instead binds the immutable task, reviewed setup implementation,
    and exact bytes of every registered input. It contains no evaluator state.
    """
    for label, value in (
            ("task_id", task_id),
            ("instruction", instruction),
            ("setup_projection", setup_projection),
            ("target_input_fingerprint", target_input_fingerprint)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"stable S0 {label} must be nonempty text")
    if not isinstance(setup_manifest, dict) or not setup_manifest:
        raise ValueError("stable S0 setup_manifest must be a nonempty object")
    record = {
        "schema_version": 1,
        "task_id": task_id,
        "instruction_sha256": hashlib.sha256(
            instruction.encode("utf-8")).hexdigest(),
        "setup_projection": setup_projection,
        "setup_manifest": setup_manifest,
        "target_input_fingerprint": target_input_fingerprint,
    }
    encoded = json.dumps(
        record, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return {**record, "sha256": hashlib.sha256(encoded).hexdigest()}


__all__ = ["capture_task_start_baseline", "stable_task_start_identity"]
