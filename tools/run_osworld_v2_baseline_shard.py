#!/usr/bin/env python3
"""Run one frozen, disjoint shard of the 108-task OSWorld v2 baseline.

This is scheduler/provenance code only. It enumerates task module filenames and
invokes the sealed ``run_task.py`` boundary; it never imports task definitions,
setup data, graders, or evaluator outputs into an Agent context.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml


REPO = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO / "config/osworld_v2_glm53_k3_baseline.lock.json"
DEFAULT_TASK_ROOT = REPO.parent / "OSWorld-V2/evaluation_examples/task_class"
DEFAULT_PYTHON = Path(sys.executable)
EXPECTED_TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))
TASK_RE = re.compile(r"task_(\d{3})\.py\Z")
GITLAB_TASKS = frozenset({"task_026", "task_041"})
TASK_RUNTIME_MODULES = {
    "task_055": ("imageio_ffmpeg",),
    "task_056": ("imageio_ffmpeg",),
}


class PreflightError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def absolute_executable_path(path: Path) -> Path:
    """Make an executable path absolute without dereferencing venv symlinks.

    Resolving ``.venv/bin/python`` can select the base interpreter and silently
    discard the virtual environment's site-packages for launched task processes.
    """
    return Path(os.path.abspath(os.fspath(path.expanduser())))


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


def _deep_merge(base: dict, overrides: dict) -> dict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def read_lock(path: Path) -> dict:
    """Read a full lock or a small content-bound extension of another lock."""
    raw = read_json(path)
    inherited = raw.get("extends")
    if inherited is None:
        return raw
    if not isinstance(inherited, str) or not inherited.strip():
        raise PreflightError("lock 'extends' must name a base lock")
    base_path = (path.parent / inherited).resolve()
    if base_path.parent != path.parent.resolve():
        raise PreflightError("extended lock must remain in the same config directory")
    expected_hash = raw.get("base_sha256")
    if not isinstance(expected_hash, str) or sha256_file(base_path) != expected_hash:
        raise PreflightError("extended lock base_sha256 does not match its base lock")
    overrides = raw.get("overrides")
    if not isinstance(overrides, dict):
        raise PreflightError("extended lock requires an object-valued 'overrides'")
    return _deep_merge(read_json(base_path), overrides)


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def discover_tasks(task_root: Path) -> tuple[str, ...]:
    if not task_root.is_dir():
        raise PreflightError(f"task module directory does not exist: {task_root}")
    observed = tuple(sorted(
        match.group(0)[:-3]
        for path in task_root.iterdir()
        if path.is_file() and (match := TASK_RE.fullmatch(path.name))))
    if observed != EXPECTED_TASKS:
        missing = sorted(set(EXPECTED_TASKS) - set(observed))
        extra = sorted(set(observed) - set(EXPECTED_TASKS))
        raise PreflightError(
            f"expected exactly task_001..task_108; missing={missing}, extra={extra}")
    return observed


def shard_assignment(lock: dict, shard_index: int) -> dict:
    assignments = lock.get("assignments")
    if not isinstance(assignments, list):
        raise PreflightError("lock assignments must be a list")
    matches = [item for item in assignments
               if isinstance(item, dict)
               and item.get("shard_index") == shard_index]
    if len(matches) != 1:
        raise PreflightError(
            f"expected exactly one assignment for shard {shard_index}")
    return matches[0]


def owner_assignment(lock: dict, owner: str) -> dict:
    assignments = lock.get("assignments")
    if not isinstance(assignments, list):
        raise PreflightError("lock assignments must be a list")
    normalized = owner.strip().lower()
    matches = [item for item in assignments
               if isinstance(item, dict)
               and str(item.get("owner", "")).lower() == normalized]
    if len(matches) != 1:
        raise PreflightError(
            f"expected exactly one assignment for owner {owner!r}")
    return matches[0]


def shard_tasks(tasks: tuple[str, ...], assignment: dict) -> tuple[str, ...]:
    try:
        first = int(assignment["first_task"])
        last = int(assignment["last_task"])
        expected_count = int(assignment["task_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError(f"invalid shard assignment: {assignment!r}") from exc
    if not 1 <= first <= last <= len(tasks):
        raise PreflightError(f"invalid task range in assignment: {assignment!r}")
    selected = tasks[first - 1:last]
    if len(selected) != expected_count:
        raise PreflightError(
            f"assignment expected {expected_count} tasks, got {len(selected)}")
    return selected


def requested_tasks(assigned: tuple[str, ...], value: str | None) -> tuple[str, ...]:
    """Restrict a launch to an explicit, ordered subset of its frozen owner shard."""
    if value is None:
        return assigned
    selected = tuple(part.strip() for part in value.split(",") if part.strip())
    if not selected:
        raise PreflightError("--tasks must name at least one task")
    if len(selected) != len(set(selected)):
        raise PreflightError("--tasks contains duplicate task ids")
    invalid = [task for task in selected if task not in assigned]
    if invalid:
        raise PreflightError(
            f"requested tasks are outside the selected owner's frozen shard: {invalid}")
    return selected


def manifest_task_scope(lock: dict, selected: tuple[str, ...]) -> tuple[str, ...]:
    """Return the stable multi-wave task scope recorded by a repair manifest."""
    repairs = lock.get("runtime_repairs") or {}
    declared = repairs.get("source_failed_tasks")
    if declared is None:
        return selected
    if (not isinstance(declared, list) or not declared
            or any(not isinstance(task, str) for task in declared)
            or len(declared) != len(set(declared))):
        raise PreflightError("runtime repair lock has invalid source_failed_tasks")
    outside = [task for task in selected if task not in declared]
    if outside:
        raise PreflightError(
            f"repair launch selected tasks outside its declared scope: {outside}")
    return tuple(declared)


def _yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PreflightError(f"cannot read YAML: {path}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"expected a YAML mapping: {path}")
    return value


def validate_lock(repo: Path, lock_path: Path,
                  task_root: Path) -> tuple[dict, Path, Path, Path]:
    lock = read_lock(lock_path)
    if lock.get("schema_version") not in {1, 2}:
        raise PreflightError(
            "lock field 'schema_version' must be 1 or 2")
    required = {
        "benchmark": "OSWorld-v2",
        "task_count": 108,
        "num_shards": 3,
    }
    for key, expected in required.items():
        if lock.get(key) != expected:
            raise PreflightError(
                f"lock field {key!r}: expected {expected!r}, got {lock.get(key)!r}")

    if lock.get("assignment_policy") == "equal_36":
        expected_assignments = [
            {"shard_index": 0, "owner": "xinyue", "first_task": 1,
             "last_task": 36, "task_count": 36},
            {"shard_index": 1, "owner": "shicheng", "first_task": 37,
             "last_task": 72, "task_count": 36},
            {"shard_index": 2, "owner": "sibo", "first_task": 73,
             "last_task": 108, "task_count": 36},
        ]
    else:
        # Schema-v1 baseline locks preserve the original historical split.
        expected_assignments = [
            {"shard_index": 0, "owner": "xinyue", "first_task": 1,
             "last_task": 36, "task_count": 36},
            {"shard_index": 1, "owner": "shicheng", "first_task": 37,
             "last_task": 73, "task_count": 37},
            {"shard_index": 2, "owner": "sibo", "first_task": 74,
             "last_task": 108, "task_count": 35},
        ]
    if lock.get("assignments") != expected_assignments:
        raise PreflightError(
            "lock assignments must be the frozen xinyue/shicheng/sibo ranges")

    tasks = discover_tasks(task_root)
    task_digest = sha256_bytes("\n".join(tasks).encode("utf-8"))
    if task_digest != lock.get("task_ids_sha256"):
        raise PreflightError("task filename set does not match the frozen lock")
    task_files = lock.get("task_files")
    if task_files is not None:
        if not isinstance(task_files, dict):
            raise PreflightError("lock task_files must be an object")
        observed = task_content_identity(task_root)
        for key in ("content_tree_sha256", "total_bytes"):
            if observed[key] != task_files.get(key):
                raise PreflightError(
                    f"task sources do not match the frozen benchmark release: "
                    f"{key} expected={task_files.get(key)!r} "
                    f"observed={observed[key]!r}")

    actor_meta = lock.get("actor_agent") or {}
    verifier_meta = lock.get("verifier_agent") or {}
    escalation_meta = lock.get("escalation") or {}
    actor_path = (repo / str(actor_meta.get("config", ""))).resolve()
    verifier_path = (repo / str(verifier_meta.get("config", ""))).resolve()
    escalated_verifier_path = (
        repo / str(escalation_meta.get("verifier_config", ""))).resolve()
    for label, path, expected_hash in (
            ("Actor Agent", actor_path, actor_meta.get("sha256")),
            ("main Verifier Agent", verifier_path, verifier_meta.get("sha256")),
            ("escalated Verifier Agent", escalated_verifier_path,
             escalation_meta.get("verifier_config_sha256"))):
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise PreflightError(f"{label} config does not match the frozen lock: {path}")

    actor = _yaml(actor_path)
    verifier = _yaml(verifier_path)
    escalated_verifier = _yaml(escalated_verifier_path)
    # This standalone repair lock preserves the original eight-hour cohort and
    # its task-user transport. The current 0808 baseline remains ten hours with
    # the explicitly locked user simulator; importing historical repairs must
    # not silently upgrade their protocol or relax the current baseline.
    historical_0808_repair = (
        lock.get("benchmark_release") == "osworld-v2-2026.08.08"
        and lock.get("tag") == "osworld_v2_0808_glm53_k3_agentic_repair_v2"
        and (lock.get("runtime_repairs") or {}).get("profile") == "infra_context_v2")
    critical_actor = {
        "model": "z-ai/glm-5.3",
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "temperature": 1.0,
        "primary_temperature": 1.0,
        "top_p": 1.0,
        "vision_model": "moonshotai/kimi-k3",
        "vision_rounds": 1,
        "look_ensemble": 1,
        "agent_decided_stop": True,
        "max_iters": 500,
        "wall_clock_secs": (
            36000 if lock.get("benchmark_release") == "osworld-v2-2026.08.08"
            and not historical_0808_repair else 28800),
        "max_resumes": 1,
        "escalation_model": "moonshotai/kimi-k3",
        "escalation_primary_temperature": 1.0,
        "escalation_provider_order": [],
        "escalation_verifier_cross_model": True,
        "resume_synthesize_worklog": True,
        "env_memory_dir": "",
    }
    critical_verifier = {
        "model": "moonshotai/kimi-k3",
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "primary_temperature": 1.0,
        "top_p": 1.0,
        "agent_decided_stop": True,
        "max_iters": 2000,
        "wall_clock_secs": 86400,
        "history_keep_pairs": 0,
        "vision_model": "",
        "vision_rounds": 1,
        "look_ensemble": 1,
        "independent_verify": False,
        "escalation_model": "",
    }
    critical_escalated_verifier = {
        "model": "z-ai/glm-5.3",
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "primary_temperature": 1.0,
        "top_p": 1.0,
        "agent_decided_stop": True,
        "max_iters": 2000,
        "wall_clock_secs": 86400,
        "history_keep_pairs": 0,
        "vision_model": "moonshotai/kimi-k3",
        "vision_rounds": 1,
        "look_ensemble": 1,
        "independent_verify": False,
        "escalation_model": "",
    }
    repairs = lock.get("runtime_repairs") or {}
    if repairs:
        if repairs.get("profile") != "infra_context_v2":
            raise PreflightError("unknown runtime repair profile in lock")
        critical_actor["verifier_infra_retries"] = 2
        for expected in (critical_verifier, critical_escalated_verifier):
            expected.update({
                "history_keep_pairs": 20,
                "keep_chars": 200000,
                "ctx_high_water": 500000,
                "ctx_low_water": 250000,
                "fold_batch": 2,
                "worklog_max_chars": 200000,
                "trace_context_max_chars": 250000,
            })
    for label, config, expected in (
            ("Actor Agent", actor, critical_actor),
            ("main Verifier Agent", verifier, critical_verifier),
            ("escalated Verifier Agent", escalated_verifier,
             critical_escalated_verifier)):
        mismatches = {
            key: {"expected": value, "observed": config.get(key)}
            for key, value in expected.items() if config.get(key) != value}
        if mismatches:
            raise PreflightError(f"{label} critical config mismatch: {mismatches}")
    if actor.get("provider_order") != ["Z.AI"]:
        raise PreflightError("Actor Agent must prefer the official Z.AI provider")
    if escalated_verifier.get("provider_order") != ["Z.AI"]:
        raise PreflightError(
            "escalated GLM-5.3 Verifier must prefer the official Z.AI provider")
    expected_escalation = {
        "actor_model": "moonshotai/kimi-k3",
        "verifier_model": "z-ai/glm-5.3",
        "actor_vision": "native_single_read",
        "verifier_vision": "one_delegated_kimi_k3_read",
        "fully_asymmetric": True,
        "fresh_verifier_context_on_model_switch": True,
    }
    escalation_mismatch = {
        key: {"expected": value, "observed": escalation_meta.get(key)}
        for key, value in expected_escalation.items()
        if escalation_meta.get(key) != value}
    if escalation_mismatch:
        raise PreflightError(
            f"escalation role/eye topology mismatch: {escalation_mismatch}")
    expected_user_simulator = {
        "provider": "openai_compatible",
        "model": "openai/gpt-4o",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "max_tokens": 256,
        "failure_policy": "abort_infra_invalid_before_official_evaluation",
    }
    requires_user_simulator = (
        (lock.get("benchmark_release") == "osworld-v2-2026.08.08"
         and not historical_0808_repair)
        or "user_simulator" in lock)
    if requires_user_simulator and lock.get("user_simulator") != expected_user_simulator:
        raise PreflightError(
            "task user-simulator transport must match the frozen OpenRouter route")
    configured_verifier = Path(str(actor.get("agentic_verifier_config", "")))
    if not configured_verifier.is_absolute():
        configured_verifier = repo / configured_verifier
    if configured_verifier.resolve() != verifier_path:
        raise PreflightError("Actor config does not point to the locked Verifier config")
    configured_escalated_verifier = Path(str(
        actor.get("escalation_agentic_verifier_config", "")))
    if not configured_escalated_verifier.is_absolute():
        configured_escalated_verifier = repo / configured_escalated_verifier
    if configured_escalated_verifier.resolve() != escalated_verifier_path:
        raise PreflightError(
            "Actor config does not point to the locked escalated Verifier config")
    return lock, actor_path, verifier_path, escalated_verifier_path


def git_identity(repo: Path) -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(repo), *args], text=True,
                stderr=subprocess.STDOUT).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PreflightError(f"cannot inspect git repository: {repo}") from exc

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit,
        "tracked_worktree_clean": not bool(status),
        "tracked_status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def validate_release_assets(
        osworld_root: Path, lock: dict,
        override: Path | None = None) -> tuple[Path | None, dict | None]:
    """Validate the release marker written by the matching preparation tool."""
    metadata = lock.get("task_assets")
    if metadata is None:
        return None, None
    if not isinstance(metadata, dict):
        raise PreflightError("lock task_assets must be an object")
    asset_root = (override.resolve() if override is not None
                  else (osworld_root / str(metadata.get("local_dir", ""))).resolve())
    marker_name = str(metadata.get(
        "release_marker", ".forge_osworld_release.json"))
    marker_path = asset_root / marker_name
    if not asset_root.is_dir() or not marker_path.is_file():
        raise PreflightError(
            "matching benchmark assets are not prepared; run "
            "tools/prepare_osworld_v2_release.py first: " + str(asset_root))
    marker = read_json(marker_path)
    expected = {
        "benchmark_release": lock.get("benchmark_release"),
        "repository": metadata.get("repository"),
        "repo_type": metadata.get("repo_type"),
        "tag": metadata.get("tag"),
        "commit": metadata.get("commit"),
    }
    mismatch = {
        key: {"expected": value, "observed": marker.get(key)}
        for key, value in expected.items() if marker.get(key) != value}
    if mismatch:
        raise PreflightError(
            f"asset release marker conflicts with the frozen lock: {mismatch}")
    expected_identity = marker.get("content_identity")
    if not isinstance(expected_identity, dict):
        raise PreflightError("asset release marker has no content_identity")
    observed_identity = asset_content_identity(asset_root, marker_name)
    if observed_identity != expected_identity:
        raise PreflightError(
            "prepared asset snapshot changed after release preparation: "
            f"expected={expected_identity}, observed={observed_identity}")
    return asset_root, marker


def result_dir(repo: Path, task: str, seed: int, tag: str) -> Path:
    return repo / "results" / task / f"seed{seed}_{tag}"


def validate_result_identity(result: dict, task: str, lock: dict) -> None:
    expected = {
        "task": task,
        "seed": int(lock["seed_label"]),
        "tag": str(lock["tag"]),
        "model": str(lock["actor_agent"]["model"]),
    }
    mismatch = {key: {"expected": value, "observed": result.get(key)}
                for key, value in expected.items() if result.get(key) != value}
    if not isinstance(result.get("llm_providers"), dict):
        mismatch["llm_providers"] = {
            "expected": "provider provenance object",
            "observed": type(result.get("llm_providers")).__name__,
        }
    if mismatch:
        raise PreflightError(f"result identity/provenance mismatch for {task}: {mismatch}")


def _load_key(environment: dict[str, str], repo: Path) -> None:
    if environment.get("OPENROUTER_API_KEY"):
        return
    env_path = repo / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if line.startswith("OPENROUTER_API_KEY="):
            environment["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip()
            break
    if not environment.get("OPENROUTER_API_KEY"):
        raise PreflightError(
            "OPENROUTER_API_KEY is absent from the environment and forge/.env")


def _load_environment_keys(
        environment: dict[str, str], path: Path, keys: tuple[str, ...]) -> None:
    """Load selected dotenv values without overriding the caller's environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    wanted = set(keys)
    for line in lines:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, value = candidate.split("=", 1)
        key = key.strip()
        if key not in wanted or environment.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        environment[key] = value


def validate_runtime_requirements(
        python: Path, tasks: tuple[str, ...], environment: dict[str, str]) -> None:
    """Fail before VM launch when selected tasks lack host-side dependencies."""
    modules = sorted({
        module
        for task in tasks
        for module in TASK_RUNTIME_MODULES.get(task, ())
    })
    if modules:
        probe = (
            "import importlib.util,sys; "
            "missing=[n for n in sys.argv[1:] if importlib.util.find_spec(n) is None]; "
            "print(','.join(missing)); raise SystemExit(bool(missing))")
        checked = subprocess.run(
            [str(python), "-c", probe, *modules], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, timeout=30, check=False)
        if checked.returncode != 0:
            missing = checked.stdout.strip() or ", ".join(modules)
            raise PreflightError(
                "selected tasks are missing locked Python dependencies: " + missing
                + "; synchronize the OSWorld environment with `uv sync --frozen`")

    if not (set(tasks) & GITLAB_TASKS):
        return
    gitlab_url = environment.get("GITLAB_URL", "").strip().rstrip("/")
    token = environment.get("GITLAB_PRIVATE_TOKEN", "").strip()
    if not gitlab_url or not token:
        raise PreflightError(
            "selected GitLab tasks require GITLAB_URL and GITLAB_PRIVATE_TOKEN "
            "in the process environment or OSWorld-V2/.env")
    if not re.fullmatch(r"https?://[^/]+", gitlab_url):
        raise PreflightError("GITLAB_URL must be an http(s) origin without a path")
    api_request = urllib_request.Request(
        gitlab_url + "/api/v4/version",
        headers={"PRIVATE-TOKEN": token, "User-Agent": "forge-preflight"})
    try:
        with urllib_request.urlopen(api_request, timeout=20) as response:
            if response.status != 200:
                raise PreflightError(
                    f"GitLab API preflight returned HTTP {response.status}")
    except PreflightError:
        raise
    except (OSError, urllib_error.URLError, urllib_error.HTTPError) as exc:
        status = getattr(exc, "code", None)
        detail = f"HTTP {status}" if status is not None else type(exc).__name__
        raise PreflightError(
            "GitLab API preflight failed without exposing the token: " + detail
        ) from exc


def benchmark_environment(
        repo: Path, osworld_root: Path, lock: dict,
        assets_root: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    _load_key(environment, repo)
    _load_environment_keys(
        environment, osworld_root / ".env",
        ("GITLAB_URL", "GITLAB_PRIVATE_TOKEN"))
    evaluator = lock["evaluator"]
    user_simulator = lock.get("user_simulator")
    environment.update({
        # Pass the launcher's resolved layout into every task process. This keeps
        # --repo/--task-root meaningful even when the two checkouts are not siblings.
        "FORGE_ROOT": str(repo),
        "OSWORLD_ROOT": str(osworld_root),
        "FORGE_ENV_FILE": str(repo / ".env"),
        "PYTHONUNBUFFERED": "1",
        "WEBSITE_HOST_SUFFIX": str(evaluator["website_host_suffix"]),
        "OSWORLD_EVAL_MODEL_PROVIDER": str(evaluator["provider"]),
        "OSWORLD_EVAL_MODEL_NAME": str(evaluator["model"]),
        "OSWORLD_EVAL_MODEL_BASE_URL": str(evaluator["base_url"]),
        "OSWORLD_EVAL_MODEL_API_KEY_ENV": str(evaluator.get(
            "api_key_env", "OPENROUTER_API_KEY")),
        "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS": str(evaluator["retry_attempts"]),
        "OSWORLD_EVAL_MODEL_RETRY_DELAY": str(evaluator["retry_delay_seconds"]),
    })
    if user_simulator is not None:
        # Older locks did not declare a separate task-user route. Preserve those
        # launches; the 0808 lock requires and separately pins this transport.
        environment.update({
            "OSWORLD_USER_SIM_PROVIDER": str(user_simulator["provider"]),
            "OSWORLD_USER_SIM_MODEL": str(user_simulator["model"]),
            "OSWORLD_USER_SIM_BASE_URL": str(user_simulator["base_url"]),
            "OSWORLD_USER_SIM_API_KEY_ENV": str(user_simulator["api_key_env"]),
            "OSWORLD_USER_SIM_MAX_TOKENS": str(user_simulator["max_tokens"]),
        })
    if assets_root is not None:
        environment["OSWORLD_FILE_BASE_URL"] = str(assets_root)
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--owner", choices=("xinyue", "shicheng", "sibo"),
        help="select the frozen task range by teammate name")
    selector.add_argument(
        "--shard-index", type=int,
        help="compatibility selector: 0=xinyue, 1=shicheng, or 2=sibo")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="simultaneous task VMs on this machine (default: 2)")
    parser.add_argument("--boot-stagger-secs", type=float, default=20.0)
    parser.add_argument(
        "--tasks",
        help="comma-separated subset within the selected owner's frozen shard")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    parser.add_argument(
        "--assets-root", type=Path,
        help="prepared release-matched asset snapshot; defaults from the lock")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--python", type=Path,
                        default=DEFAULT_PYTHON)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="development only; official jobs require clean tracked trees")
    args = parser.parse_args()
    repo = args.repo.resolve()
    task_root = args.task_root.resolve()
    lock_path = args.lock.resolve()
    if args.concurrency < 1:
        raise PreflightError("concurrency must be positive")

    lock, actor_config, verifier_config, escalated_verifier_config = validate_lock(
        repo, lock_path, task_root)
    all_tasks = discover_tasks(task_root)
    assignment = (owner_assignment(lock, args.owner) if args.owner
                  else shard_assignment(lock, args.shard_index))
    shard_index = int(assignment["shard_index"])
    tasks = requested_tasks(shard_tasks(all_tasks, assignment), args.tasks)
    manifest_tasks = manifest_task_scope(lock, tasks)

    forge_git = git_identity(repo)
    osworld_root = task_root.parents[1]
    osworld_git = git_identity(osworld_root)
    osworld_code = lock.get("osworld_code")
    if osworld_code is not None:
        if not isinstance(osworld_code, dict):
            raise PreflightError("lock osworld_code must be an object")
        expected_commit = str(osworld_code.get("commit", ""))
        if osworld_git["commit"] != expected_commit:
            raise PreflightError(
                "OSWorld checkout does not match the frozen release: "
                f"expected {expected_commit}, observed {osworld_git['commit']}")
    assets_root, asset_marker = validate_release_assets(
        osworld_root, lock,
        args.assets_root.resolve() if args.assets_root is not None else None)
    if not args.allow_dirty and (
            not forge_git["tracked_worktree_clean"]
            or not osworld_git["tracked_worktree_clean"]):
        raise PreflightError(
            "official jobs require clean tracked forge and OSWorld-v2 worktrees")

    seed = int(lock["seed_label"])
    tag = str(lock["tag"])
    print(f"freeze={lock_path}")
    print(f"forge_commit={forge_git['commit']}")
    print(f"osworld_commit={osworld_git['commit']}")
    print(f"lock_sha256={sha256_file(lock_path)}")
    print(f"Actor Agent={lock['actor_agent']['model']} ({actor_config})")
    print(f"actor_config_sha256={sha256_file(actor_config)}")
    print(f"Verifier Agent={lock['verifier_agent']['model']} ({verifier_config})")
    print(f"verifier_config_sha256={sha256_file(verifier_config)}")
    print("Escalation="
          f"{lock['escalation']['actor_model']} Actor <-> "
          f"{lock['escalation']['verifier_model']} Verifier "
          f"({escalated_verifier_config})")
    print("escalated_verifier_config_sha256="
          f"{sha256_file(escalated_verifier_config)}")
    print(f"shard={shard_index}/{lock['num_shards']} "
          f"owner={assignment['owner']} "
          f"range={assignment['first_task']:03d}-{assignment['last_task']:03d} "
          f"tasks={len(tasks)}")
    print(" ".join(tasks))
    if args.dry_run:
        return 0

    python = absolute_executable_path(args.python)
    if not python.is_file():
        raise PreflightError(f"Python interpreter does not exist: {python}")
    environment = benchmark_environment(
        repo, osworld_root, lock, assets_root=assets_root)
    validate_runtime_requirements(python, tasks, environment)

    partial = []
    completed = []
    for task in tasks:
        run_root = result_dir(repo, task, seed, tag)
        result_path = run_root / "result.json"
        if result_path.is_file():
            validate_result_identity(read_json(result_path), task, lock)
            completed.append(task)
        elif run_root.exists() and any(run_root.iterdir()):
            partial.append(str(run_root))
    if partial:
        raise PreflightError(
            "partial result directories would be overwritten; preserve them and use "
            f"a separately locked retry tag: {partial}")

    job_root = repo / "results/benchmark_jobs" / tag / f"shard_{shard_index}"
    log_root = job_root / "logs"
    manifest_path = job_root / "manifest.json"
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "lock": str(lock_path),
        "lock_sha256": sha256_file(lock_path),
        "tag": tag,
        "seed_label": seed,
        "shard_index": shard_index,
        "num_shards": lock["num_shards"],
        "owner": assignment["owner"],
        "assignment": assignment,
        # Repair runs may be launched in dependency-separated waves (for example,
        # ordinary desktop tasks first and GitLab tasks after their service is
        # ready). Every wave records the same complete declared repair scope.
        "task_ids": list(manifest_tasks),
        "actor_config": {"path": str(actor_config),
                         "sha256": sha256_file(actor_config)},
        "verifier_config": {"path": str(verifier_config),
                            "sha256": sha256_file(verifier_config)},
        "escalated_verifier_config": {
            "path": str(escalated_verifier_config),
            "sha256": sha256_file(escalated_verifier_config)},
        "forge_git": forge_git,
        "osworld_git": osworld_git,
        "benchmark_release": lock.get("benchmark_release"),
        "task_content_identity": task_content_identity(task_root),
        "asset_root": str(assets_root) if assets_root is not None else None,
        "asset_release_marker": asset_marker,
        "scheduler": {"concurrency": args.concurrency,
                      "boot_stagger_seconds": args.boot_stagger_secs,
                      "python": str(python)},
    }
    if manifest_path.exists():
        prior = read_json(manifest_path)
        immutable = (
            "lock_sha256", "tag", "seed_label", "shard_index", "num_shards",
            "owner", "assignment", "task_ids", "actor_config", "verifier_config",
            "escalated_verifier_config", "forge_git", "osworld_git",
            "benchmark_release", "task_content_identity", "asset_root",
            "asset_release_marker")
        conflicts = [key for key in immutable if prior.get(key) != manifest.get(key)]
        if conflicts:
            raise PreflightError(
                f"existing shard manifest conflicts on fields: {conflicts}")
        manifest = prior
    else:
        write_json_atomic(manifest_path, manifest)
    log_root.mkdir(parents=True, exist_ok=True)

    state_path = job_root / "state.json"
    state = read_json(state_path) if state_path.exists() else {
        "schema_version": 1, "tasks": {}}
    for task in completed:
        state["tasks"].setdefault(task, {"state": "complete_before_launch"})
    pending = [task for task in tasks if task not in completed]
    active = {}
    stop_requested = False

    def request_stop(signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        print(f"received signal {signum}; stopping active task processes cleanly",
              file=sys.stderr, flush=True)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    failures = []
    try:
        while pending or active:
            while pending and len(active) < args.concurrency and not stop_requested:
                task = pending.pop(0)
                log_path = log_root / f"{task}.log"
                handle = log_path.open("a", encoding="utf-8", buffering=1)
                handle.write(f"\n=== launch {utc_now()} ===\n")
                command = [
                    str(python), str(repo / "run_task.py"), task,
                    "--seed", str(seed), "--config", str(actor_config),
                    "--tag", tag,
                ]
                process = subprocess.Popen(
                    command, cwd=repo, env=environment, stdout=handle,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
                active[process] = (task, handle, log_path)
                state["tasks"][task] = {
                    "state": "running", "pid": process.pid,
                    "launched_at": utc_now(), "log": str(log_path)}
                write_json_atomic(state_path, state)
                print(f"launched {task} pid={process.pid} active={len(active)}",
                      flush=True)
                if pending and args.boot_stagger_secs > 0:
                    time.sleep(args.boot_stagger_secs)

            finished = [process for process in active if process.poll() is not None]
            for process in finished:
                task, handle, log_path = active.pop(process)
                handle.close()
                result_path = result_dir(repo, task, seed, tag) / "result.json"
                record = state["tasks"][task]
                record.update({"finished_at": utc_now(),
                               "exit_code": process.returncode})
                if process.returncode == 0 and result_path.is_file():
                    result = read_json(result_path)
                    validate_result_identity(result, task, lock)
                    record.update({"state": "complete", "status": result.get("status"),
                                   "score": result.get("score"),
                                   "result": str(result_path)})
                    print(f"finished {task} score={result.get('score')} "
                          f"status={result.get('status')}", flush=True)
                else:
                    record["state"] = "failed"
                    record["result"] = str(result_path) if result_path.exists() else None
                    failures.append(task)
                    print(f"FAILED {task} exit={process.returncode}; see {log_path}",
                          file=sys.stderr, flush=True)
                write_json_atomic(state_path, state)

            if stop_requested:
                break
            if active and not finished:
                time.sleep(10)
    finally:
        if active:
            for process in active:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.time() + 30
            for process, (task, handle, _log_path) in list(active.items()):
                try:
                    process.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                handle.close()
                state["tasks"][task].update({
                    "state": "interrupted", "finished_at": utc_now()})
            write_json_atomic(state_path, state)

    if stop_requested:
        return 130
    print(f"shard complete: {len(tasks) - len(failures)}/{len(tasks)} successful",
          flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
