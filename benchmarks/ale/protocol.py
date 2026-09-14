"""Pinned cohort, role settings, and immutable phase boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

RSIAGENT = Path(__file__).resolve().parents[2]
LOCK_PATH = RSIAGENT / "config/ale/protocol.lock.json"
ROLES = {
    "baseline": "osworld/baseline.yaml",
    "target": "roles/target.yaml",
    "actor": "roles/actor.yaml",
    "verifier": "roles/practice_verifier.yaml",
    "curriculum": "roles/curriculum.yaml",
    "memory": "roles/actor.yaml",
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as f:
        json.dump(value, f, indent=2, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def protocol(ale_root=None):
    lock = json.loads(LOCK_PATH.read_text())
    for relative, expected in lock["config_sha256"].items():
        if sha(RSIAGENT / relative) != expected:
            raise RuntimeError("Role configuration drift: " + relative)
    tasks = lock["tasks"]
    if len(tasks) != 67 or len(set(tasks)) != 67:
        raise RuntimeError("Pinned task coverage/uniqueness changed")
    if set(tasks) != set(lock["task_cards"]):
        raise RuntimeError("Pinned task cards differ from the cohort")
    if ale_root is not None:
        ale = Path(ale_root)
        head = subprocess.check_output(
            ["git", "-C", str(ale), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != lock["ale_commit"]:
            raise RuntimeError("ALE revision differs from the pinned release")
        if sha(ale / "selected_tasks/full/near-term.txt") != lock["cohort_sha256"]:
            raise RuntimeError("Canonical Near-Term cohort changed")
        for task, expected in lock["task_cards"].items():
            if sha(ale / "tasks" / task / "task_card.json") != expected:
                raise RuntimeError("Task metadata drift: " + task)
    return lock


def role(name, memory_dir=""):
    from config.runtime_paths import normalize_verifier_config_paths
    from config.settings import load

    cfg = load(str(RSIAGENT / "config" / ROLES[name]))
    normalize_verifier_config_paths(cfg, RSIAGENT)
    if memory_dir:
        cfg.env_memory_dir = str(memory_dir)
    return cfg


def memory_record(path):
    # Match RSIAgent's canonical memory digest without importing its core package
    # into the ALE host process (CUA also has a top-level package named core).
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Frozen memory must be a real directory without a symlink")
    files = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise RuntimeError("Frozen memory contains a symlink: " + str(candidate))
        if candidate.is_file():
            content = candidate.read_bytes()
            files[candidate.relative_to(root).as_posix()] = {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        elif not candidate.is_dir():
            raise RuntimeError(
                "Frozen memory contains a special file: " + str(candidate)
            )
    hashes = {name: record["sha256"] for name, record in files.items()}
    digest = hashlib.sha256(
        json.dumps(
            hashes,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return {"path": str(root.resolve()), "sha256": digest, "files": files}


def instruction_shingles(text):
    """Same eight-word normalization as RSIAgent's audit, with no agent imports."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i : i + 8]) for i in range(len(words) - 7)}


def verify_memory(record):
    actual = memory_record(record["path"])
    if actual != record:
        raise RuntimeError("Frozen memory changed after its phase boundary")
    return actual


def source_fingerprint(root=None):
    root = Path(root) if root is not None else RSIAGENT
    files = {}
    for directory in ("benchmarks", "config", "core", "env", "explore", "llm", "tools"):
        for p in sorted((root / directory).rglob("*")):
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith(".pyc")
            ):
                files[p.relative_to(root).as_posix()] = sha(p)
    for p in sorted(root.glob("*.py")):
        files[p.name] = sha(p)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def select_tasks(lock, task_file=None):
    """Select a reviewed cohort subset without operator identities."""
    tasks = lock["tasks"]
    if task_file is not None:
        selected = [
            line.strip()
            for line in Path(task_file).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if (
            not selected
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(tasks)
        ):
            raise ValueError(
                "Task list must be a nonempty unique subset of the pinned cohort"
            )
        return selected
    return list(tasks)


def event(root, kind, payload):
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": kind, **payload}
    with (Path(root) / "events.jsonl").open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
        f.flush()
