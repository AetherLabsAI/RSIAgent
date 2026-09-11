#!/usr/bin/env python3
"""Orchestrate the three leakage-separated recursive-improvement phases.

Each invocation executes exactly one phase. This is intentional: official scores
created in Phase 3 cannot flow into a later learning phase in the same protocol
run. Agent stopping remains Agent-owned; the Phase-1 project budget is an external
budget over complete project/memory lifecycles only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from config.runtime_paths import resolve_forge_root, resolve_osworld_root
from core.self_evolving_loop import DEFAULT_PHASE2_STOP_POLICY, Phase2StopPolicy
from explore.e15_loop import _atomic_install_memory, _manifest, _read_memory_tree
from explore.e15_v12_loop import _memory_tree_sha256


FORGE_ROOT = resolve_forge_root()
OSWORLD_ROOT = resolve_osworld_root(forge_root=FORGE_ROOT)
RESULTS_ROOT = FORGE_ROOT / "results" / "recursive_improvement"
TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))
TASK_RELEASES = {
    "osworld-v2-2026.06.24": frozenset({"task_048", "task_082"}),
    "osworld-v2-2026.08.08": frozenset(),
}
BENCHMARK_PROVENANCE_PROFILES = {
    "osworld-v2-2026.06.24": (
        FORGE_ROOT / "config/osworld_v2_0624_runtime_0808_compat.lock.json"),
    "osworld-v2-2026.08.08": (
        FORGE_ROOT / "config/osworld_v2_0808_glm53_k3_agentic_baseline.lock.json"),
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_ACK = {
    "phase1": "RUN-RECURSIVE-IMPROVEMENT-PHASE1",
    "phase2": "RUN-RECURSIVE-IMPROVEMENT-PHASE2",
    "phase3": "RUN-RECURSIVE-IMPROVEMENT-PHASE3",
}


class ProtocolError(RuntimeError):
    """The declared study violates a protocol or artifact boundary."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"expected a JSON object: {path}")
    return value


def _resolve_file(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{label} must name one repository file")
    raw = Path(value).expanduser()
    path = raw if raw.is_absolute() else FORGE_ROOT / raw
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ProtocolError(f"{label} does not exist: {path}") from exc
    if not path.is_file():
        raise ProtocolError(f"{label} is not a file: {path}")
    return path


def _safe_run_root(run_name: str) -> Path:
    if (not isinstance(run_name, str) or not _SAFE_NAME.fullmatch(run_name)
            or run_name in {".", ".."}):
        raise ProtocolError("run_name must be one safe basename")
    root = (RESULTS_ROOT / run_name).resolve()
    if root.parent != RESULTS_ROOT.resolve():
        raise ProtocolError("run_name escapes results/recursive_improvement")
    return root


def _task_list(value: Any, *, label: str,
               valid_tasks: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ProtocolError(f"{label} must be a JSON list of task IDs")
    tasks = tuple(value)
    if len(set(tasks)) != len(tasks):
        raise ProtocolError(f"{label} contains duplicates")
    invalid = sorted(set(tasks) - valid_tasks)
    if invalid:
        raise ProtocolError(
            f"{label} contains unknown or excluded tasks: {invalid}")
    return tasks


def load_protocol(path: Path) -> dict[str, Any]:
    spec = _read_object(path)
    if spec.get("schema_version") != 1:
        raise ProtocolError("protocol schema_version must be 1")
    task_release = spec.get("task_release")
    if task_release not in TASK_RELEASES:
        raise ProtocolError(
            "task_release must be one reviewed release: "
            + ", ".join(sorted(TASK_RELEASES)))
    valid_tasks = frozenset(TASKS) - TASK_RELEASES[task_release]
    study_design = spec.get("study_design", "held_out_generalization")
    if study_design not in {
            "held_out_generalization", "target_conditioned_adaptation"}:
        raise ProtocolError(
            "study_design must be held_out_generalization or "
            "target_conditioned_adaptation")
    run_root = _safe_run_root(spec.get("run_name"))
    for phase in ("phase1", "phase2", "phase3"):
        if not isinstance(spec.get(phase), dict):
            raise ProtocolError(f"protocol lacks object {phase}")

    phase1 = spec["phase1"]
    budget = phase1.get("project_budget")
    checkpoints = phase1.get("checkpoints")
    if type(budget) is not int or budget < 0:
        raise ProtocolError("phase1.project_budget must be nonnegative")
    if (not isinstance(checkpoints, list)
            or any(type(item) is not int for item in checkpoints)
            or checkpoints != sorted(set(checkpoints))
            or any(item < 0 or item > budget for item in checkpoints)):
        raise ProtocolError(
            "phase1.checkpoints must be sorted unique integers within the budget")
    parallel_waves = phase1.get("parallel_waves", False)
    parallelism = phase1.get("parallelism", 1)
    if type(parallel_waves) is not bool:
        raise ProtocolError("phase1.parallel_waves must be a boolean")
    if type(parallelism) is not int or parallelism < 1:
        raise ProtocolError("phase1.parallelism must be a positive integer")
    if not parallel_waves and parallelism != 1:
        raise ProtocolError(
            "phase1.parallelism must be 1 when parallel_waves is false")
    distribution = _resolve_file(
        phase1.get("distribution_file"), label="phase1.distribution_file")
    distribution_text = distribution.read_text(encoding="utf-8")
    if not distribution_text.strip():
        raise ProtocolError("Phase-1 distribution description is empty")

    development = _task_list(
        spec["phase2"].get("development_tasks"),
        label="phase2.development_tasks", valid_tasks=valid_tasks)
    held_out = _task_list(
        spec["phase3"].get("held_out_tasks"),
        label="phase3.held_out_tasks", valid_tasks=valid_tasks)
    overlap = sorted(set(development) & set(held_out))
    if overlap and study_design == "held_out_generalization":
        raise ProtocolError(
            "development and held-out task sets overlap: " + ", ".join(overlap))
    if (study_design == "target_conditioned_adaptation"
            and set(development) != set(held_out)):
        raise ProtocolError(
            "target-conditioned development_tasks and held_out_tasks must name "
            "the same target set")
    named_held_out = sorted(
        task for task in held_out if task.lower() in distribution_text.lower())
    if named_held_out and study_design == "held_out_generalization":
        raise ProtocolError(
            "Phase-1 distribution text names held-out instances: "
            + ", ".join(named_held_out))

    config_fields = {
        "phase1": ("actor_config", "verifier_config",
                   "verifier_control_config", "curriculum_config",
                   "memory_config"),
        "phase2": ("target_config", "practice_actor_config",
                   "practice_verifier_config", "curriculum_config",
                   "memory_config"),
        "phase3": ("config", "benchmark_lock"),
    }
    resolved_configs: dict[str, dict[str, Path]] = {}
    for phase, fields in config_fields.items():
        resolved_configs[phase] = {
            field: _resolve_file(
                spec[phase].get(field), label=f"{phase}.{field}")
            for field in fields
        }
    for phase in ("phase2", "phase3"):
        seed = spec[phase].get("seed")
        if type(seed) is not int or seed < 0:
            raise ProtocolError(f"{phase}.seed must be nonnegative")
    try:
        stop_policy = Phase2StopPolicy(
            spec["phase2"].get("stop_policy", DEFAULT_PHASE2_STOP_POLICY))
    except (ValueError, TypeError) as exc:
        raise ProtocolError(
            "phase2.stop_policy must be verifier_pass or curriculum_review") from exc

    spec["_resolved"] = {
        "protocol_path": path,
        "run_root": run_root,
        "distribution": distribution,
        "development_tasks": development,
        "held_out_tasks": held_out,
        "study_design": study_design,
        "phase2_stop_policy": stop_policy.value,
        "configs": resolved_configs,
    }
    return spec


def _memory_record(path: Path) -> dict[str, Any]:
    memory = _read_memory_tree(str(path))
    manifest = _manifest(memory)
    return {
        "path": str(path.resolve()),
        "files": len(memory),
        "bytes": sum(len(data) for data in memory.values()),
        "tree_sha256": _memory_tree_sha256(memory),
        "manifest_sha256": hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }


def _configure_release_environment(spec: dict[str, Any]) -> dict[str, Any]:
    """Bind task setup to the protocol's benchmark release before any phase."""

    release = spec["task_release"]
    lock_path = spec["_resolved"]["configs"]["phase3"]["benchmark_lock"]
    lock = _read_object(lock_path)
    if lock.get("task_release") != release:
        raise ProtocolError(
            "Phase-3 lock task release conflicts with the protocol")
    evaluator = lock.get("evaluator") or {}
    website = evaluator.get("website_host_suffix")
    if not isinstance(website, str) or not website:
        raise ProtocolError("Phase-3 lock lacks a website release binding")
    os.environ["WEBSITE_HOST_SUFFIX"] = website

    assets = lock.get("task_assets")
    asset_record: dict[str, Any] = {"mode": "inherited"}
    if assets is not None:
        if not isinstance(assets, dict):
            raise ProtocolError("Phase-3 lock task_assets must be an object")
        local_dir = assets.get("local_dir")
        marker_name = assets.get("release_marker")
        if (not isinstance(local_dir, str) or not local_dir
                or not isinstance(marker_name, str) or not marker_name):
            raise ProtocolError("Phase-3 lock task_assets is incomplete")
        root = (OSWORLD_ROOT / local_dir).resolve(strict=True)
        marker_path = (root / marker_name).resolve(strict=True)
        marker = _read_object(marker_path)
        expected = {
            "benchmark_release": release,
            "repository": assets.get("repository"),
            "repo_type": assets.get("repo_type"),
            "tag": assets.get("tag"),
            "commit": assets.get("commit"),
        }
        mismatch = {
            key: {"expected": value, "observed": marker.get(key)}
            for key, value in expected.items() if marker.get(key) != value}
        if mismatch:
            raise ProtocolError(
                f"prepared task assets conflict with the protocol: {mismatch}")
        os.environ["OSWORLD_FILE_BASE_URL"] = str(root)
        asset_record = {
            "mode": "release_bound_local_snapshot",
            "path": str(root),
            "marker": str(marker_path),
            "marker_sha256": _sha256(marker_path),
        }
    return {
        "task_release": release,
        "website_host_suffix": website,
        "task_assets": asset_record,
    }


def _append_event(run_root: Path, event: str, **payload: Any) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "payload": payload,
    }
    with (run_root / "protocol_events.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _run(command: list[str], *, run_root: Path, phase: str,
         task: str = "") -> None:
    _append_event(run_root, "SUBPROCESS_STARTED", phase=phase, task=task,
                  argv=command)
    completed = subprocess.run(command, cwd=FORGE_ROOT, check=False)
    _append_event(
        run_root, "SUBPROCESS_COMPLETED", phase=phase, task=task,
        returncode=completed.returncode)
    if completed.returncode != 0:
        raise ProtocolError(
            f"{phase} subprocess failed for {task or 'lineage'} "
            f"with exit code {completed.returncode}")


def _ensure_no_later_phase(run_root: Path, phase: str) -> None:
    later = {
        "phase1": ("phase2", "phase3"),
        "phase2": ("phase3",),
        "phase3": (),
    }[phase]
    present = [name for name in later if (run_root / name).exists()]
    if present:
        raise ProtocolError(
            f"cannot execute {phase} after later phase artifacts exist: {present}")


def _configure_phase3_evaluator(
        lock_path: Path, config_path: Path,
        task_release: str) -> dict[str, Any]:
    """Bind secret-free evaluator identity before any held-out Actor starts."""
    lock = _read_object(lock_path)
    excluded = TASK_RELEASES[task_release]
    required = {
        "schema_version": 1,
        "benchmark": "OSWorld-v2",
        "task_release": task_release,
        "source_task_count": 108,
        "valid_task_count": 108 - len(excluded),
    }
    drift = {
        key: {"expected": expected, "observed": lock.get(key)}
        for key, expected in required.items() if lock.get(key) != expected}
    if drift:
        raise ProtocolError(f"Phase-3 benchmark lock drifted: {drift}")
    if set(lock.get("excluded_tasks") or ()) != excluded:
        raise ProtocolError("Phase-3 excluded task set drifted")
    actor = lock.get("actor_agent") or {}
    if (actor.get("config") != str(config_path.relative_to(FORGE_ROOT))
            or actor.get("sha256") != _sha256(config_path)
            or actor.get("model") != "z-ai/glm-5.3"):
        raise ProtocolError("Phase-3 Actor Agent config is not lock-bound")
    for role in ("verifier_agent", "escalated_verifier_agent"):
        record = lock.get(role) or {}
        path = _resolve_file(record.get("config"), label=f"lock.{role}.config")
        if record.get("sha256") != _sha256(path):
            raise ProtocolError(f"Phase-3 {role} config drifted")

    from config.settings import load
    from config.runtime_paths import normalize_verifier_config_paths
    from run_unified import (
        _configure_sealed_evaluator,
        _probe_sealed_evaluator_transport,
    )

    cfg = load(str(config_path))
    normalize_verifier_config_paths(cfg, FORGE_ROOT)
    sealed = _configure_sealed_evaluator(lock_path, cfg, config_path)
    sealed["transport_preflight"] = _probe_sealed_evaluator_transport()
    return sealed


def execute_phase1(spec: dict[str, Any], *, resume_completed_boundary=False) -> dict[str, Any]:
    resolved = spec["_resolved"]
    run_root: Path = resolved["run_root"]
    _ensure_no_later_phase(run_root, "phase1")
    phase1_result = run_root / "phase1/result.json"
    if phase1_result.is_file() and not resume_completed_boundary:
        result = _read_object(phase1_result)
        if result.get("status") not in {"saturated", "budget_exhausted"}:
            raise ProtocolError("existing Phase-1 result is not transition-ready")
        return result
    cfg = resolved["configs"]["phase1"]
    command = [
        sys.executable, str(FORGE_ROOT / "run_phase1_exploration.py"),
        "--run-name", spec["run_name"],
        "--distribution-file", str(resolved["distribution"]),
        "--project-budget", str(spec["phase1"]["project_budget"]),
        "--checkpoints", ",".join(map(str, spec["phase1"]["checkpoints"])),
        "--actor-config", str(cfg["actor_config"]),
        "--verifier-config", str(cfg["verifier_config"]),
        "--verifier-control-config", str(cfg["verifier_control_config"]),
        "--curriculum-config", str(cfg["curriculum_config"]),
        "--memory-config", str(cfg["memory_config"]),
        "--execute", "RUN-PHASE1-DISTRIBUTION-EXPLORATION",
    ]
    if spec["phase1"].get("parallel_waves", False):
        command[-2:-2] = [
            "--parallel-waves", "--max-parallel",
            str(spec["phase1"]["parallelism"]),
        ]
    if resolved["study_design"] == "target_conditioned_adaptation":
        command.insert(-2, "--target-query-conditioned")
    if resume_completed_boundary:
        command.insert(-2, "--resume-completed-boundary")
    _run(command, run_root=run_root, phase="phase1")
    result = _read_object(phase1_result)
    if result.get("official_evaluator_calls") != 0:
        raise ProtocolError("Phase 1 recorded an evaluator call")
    return result


def execute_phase2(spec: dict[str, Any]) -> dict[str, Any]:
    resolved = spec["_resolved"]
    run_root: Path = resolved["run_root"]
    _ensure_no_later_phase(run_root, "phase2")
    phase1_result = _read_object(run_root / "phase1/result.json")
    if phase1_result.get("status") not in {"saturated", "budget_exhausted"}:
        raise ProtocolError("Phase 1 is not transition-ready")
    tasks = resolved["development_tasks"]
    if not tasks:
        raise ProtocolError(
            "Phase 2 needs a precommitted nonempty development task set")
    memory_path = Path(phase1_result["memory_frozen_path"]).resolve(strict=True)
    initial_record = _memory_record(memory_path)
    cfg = resolved["configs"]["phase2"]
    stop_policy = resolved["phase2_stop_policy"]
    benchmark_profile = BENCHMARK_PROVENANCE_PROFILES[
        spec["task_release"]].resolve(strict=True)
    task_results = []
    for position, task in enumerate(tasks):
        seed = int(spec["phase2"]["seed"]) + position
        result_path = (run_root / "phase2" / task
                       / f"attempt_{seed:04d}" / "result.json")
        if not result_path.is_file():
            command = [
                sys.executable, str(FORGE_ROOT / "run_self_evolving.py"), task,
                "--phase2-training", "--protocol-run", spec["run_name"],
                "--seed", str(seed), "--initial-memory", str(memory_path),
                "--phase2-stop-policy", stop_policy,
                "--target-config", str(cfg["target_config"]),
                "--practice-actor-config", str(cfg["practice_actor_config"]),
                "--practice-verifier-config", str(cfg["practice_verifier_config"]),
                "--curriculum-config", str(cfg["curriculum_config"]),
                "--memory-config", str(cfg["memory_config"]),
                "--benchmark-profile", str(benchmark_profile),
                "--execute", "RUN-PHASE2-FAILURE-CONDITIONED-PRACTICE",
            ]
            _run(command, run_root=run_root, phase="phase2", task=task)
        result = _read_object(result_path)
        _validate_phase2_terminal(result, stop_policy=stop_policy, task=task)
        if (result.get("official_evaluator_calls") != 0
                or result.get("official_evaluator_feedback_entered_learning")
                is not False):
            raise ProtocolError(f"Phase-2 grader boundary failed for {task}")
        if result.get("initial_memory_tree_sha256") != \
                _memory_record(memory_path)["tree_sha256"]:
            raise ProtocolError(f"Phase-2 memory lineage mismatch for {task}")
        memory_path = Path(result["final_memory_path"]).resolve(strict=True)
        if _memory_record(memory_path)["tree_sha256"] != \
                result.get("final_memory_tree_sha256"):
            raise ProtocolError(f"Phase-2 final memory mismatch for {task}")
        task_results.append({
            "task": task,
            "seed": seed,
            "status": result.get("status"),
            "stop_policy": result["phase2_stop_policy"],
            "verdict": result.get("target_verifier_verdict"),
            "evolutions": result.get("evolutions"),
            "practice_projects": result.get("practice_projects"),
            "memory_tree_sha256": result.get("final_memory_tree_sha256"),
        })

    frozen = run_root / "phase2/memory_frozen"
    if frozen.exists():
        existing = _memory_record(frozen)
        source = _memory_record(memory_path)
        if existing["tree_sha256"] != source["tree_sha256"]:
            raise ProtocolError("existing Phase-2 frozen memory conflicts")
    else:
        _atomic_install_memory(
            str(frozen), _read_memory_tree(str(memory_path)))
    payload = {
        "schema_version": 1,
        "phase": "outcome_grounded_targeted_practice",
        "phase2_stop_policy": stop_policy,
        "status": "complete",
        "development_tasks": list(tasks),
        "task_results": task_results,
        "memory_before": initial_record,
        "memory_frozen": _memory_record(frozen),
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
    }
    _write_json_atomic(run_root / "phase2/result.json", payload)
    return payload


def _validate_phase2_terminal(
        result: dict[str, Any], *, stop_policy: str, task: str) -> None:
    """Validate new and cached results before advancing to another task/phase."""
    if result.get("phase2_stop_policy") != stop_policy:
        raise ProtocolError(
            f"Phase-2 stopping policy is absent or differs for {task}; "
            "use a new lineage rather than reuse an unlabelled/different policy")
    status = result.get("status")
    verdict = result.get("target_verifier_verdict")
    allowed = {"verifier_pass", "curriculum_stalled_final_target_test"}
    if stop_policy == Phase2StopPolicy.CURRICULUM_REVIEW.value:
        allowed |= {"verifier_pass_curriculum_ready", "verifier_pass_curriculum_stalled"}
    if status not in allowed or verdict not in {"PASS", "FAIL"}:
        raise ProtocolError(
            f"Phase-2 target {task} is not transition-ready: {status!r}, {verdict!r}")
    if status.startswith("verifier_pass") and verdict != "PASS":
        raise ProtocolError(f"Phase-2 PASS termination lacks a Verifier PASS for {task}")


def execute_phase3(spec: dict[str, Any]) -> dict[str, Any]:
    resolved = spec["_resolved"]
    target_conditioned = (
        resolved["study_design"] == "target_conditioned_adaptation")
    run_root: Path = resolved["run_root"]
    phase2 = _read_object(run_root / "phase2/result.json")
    if phase2.get("status") != "complete":
        raise ProtocolError("Phase 2 is not complete")
    tasks = resolved["held_out_tasks"]
    if not tasks:
        raise ProtocolError("Phase 3 needs a precommitted held-out task set")
    source = Path(phase2["memory_frozen"]["path"]).resolve(strict=True)
    phase3_root = run_root / "phase3"
    frozen = phase3_root / "memory_frozen"
    if not frozen.exists():
        _atomic_install_memory(str(frozen), _read_memory_tree(str(source)))
    frozen_record = _memory_record(frozen)
    if frozen_record["tree_sha256"] != phase2["memory_frozen"]["tree_sha256"]:
        raise ProtocolError("Phase-3 memory does not match the Phase-2 freeze")
    _write_json_atomic(phase3_root / "memory_lock.json", {
        "schema_version": 1,
        **frozen_record,
        "writeback": False,
        "curriculum_enabled": False,
    })

    tag = f"{spec['run_name']}_phase3"
    cfg = resolved["configs"]["phase3"]["config"]
    evaluator_lock = resolved["configs"]["phase3"]["benchmark_lock"]
    sealed_evaluator = _configure_phase3_evaluator(
        evaluator_lock, cfg, spec["task_release"])
    task_results = []
    for position, task in enumerate(tasks):
        seed = int(spec["phase3"]["seed"]) + position
        result_path = (FORGE_ROOT / "results" / task
                       / f"seed{seed}_{tag}" / "result.json")
        if not result_path.is_file():
            command = [
                sys.executable, str(FORGE_ROOT / "run_task.py"), task,
                "--seed", str(seed), "--tag", tag,
                "--config", str(cfg), "--memory-dir", str(frozen),
            ]
            _run(command, run_root=run_root, phase="phase3", task=task)
        result = _read_object(result_path)
        after = _memory_record(frozen)
        if after["tree_sha256"] != frozen_record["tree_sha256"]:
            raise ProtocolError("Phase-3 evaluation mutated frozen host memory")
        memory = result.get("memory") or {}
        if (memory.get("tree_sha256") != frozen_record["tree_sha256"]
                or memory.get("host_writeback") is not False
                or result.get("official_evaluator_feedback_entered_agent")
                is not False):
            raise ProtocolError(f"Phase-3 boundary record failed for {task}")
        task_results.append({
            "task": task,
            "seed": seed,
            "score": float(result["score"]),
            "status": result.get("status"),
            "official_evaluator_attempts":
                int(result.get("official_evaluator_attempts", 1)),
            "source_result": str(result_path),
        })
    scores = [row["score"] for row in task_results]
    payload = {
        "schema_version": 1,
        "phase": ("frozen_memory_target_conditioned_evaluation"
                  if target_conditioned
                  else "frozen_memory_held_out_evaluation"),
        "study_design": resolved["study_design"],
        "status": "complete",
        "held_out_tasks": list(tasks),
        "memory_frozen": frozen_record,
        "curriculum_enabled": False,
        "memory_updates": 0,
        "official_evaluator_calls": len(tasks),
        "official_evaluator_successful_calls": len(tasks),
        "official_evaluator_attempts": sum(
            row["official_evaluator_attempts"] for row in task_results),
        "official_evaluator_feedback_entered_learning": False,
        "sealed_evaluator": sealed_evaluator,
        "mean_score": sum(scores) / len(scores),
        "task_results": task_results,
    }
    _write_json_atomic(phase3_root / "result.json", payload)
    return payload


def preflight(spec: dict[str, Any], phase: str) -> dict[str, Any]:
    resolved = spec["_resolved"]
    configs = {
        name: {
            field: {"path": str(path), "sha256": _sha256(path)}
            for field, path in values.items()
        }
        for name, values in resolved["configs"].items()
    }
    blockers = []
    if phase == "phase2":
        if not resolved["development_tasks"]:
            blockers.append("phase2.development_tasks is empty")
        if not (resolved["run_root"] / "phase1/result.json").is_file():
            blockers.append("Phase 1 result is not present")
    elif phase == "phase3":
        if not resolved["held_out_tasks"]:
            blockers.append("phase3.held_out_tasks is empty")
        if not (resolved["run_root"] / "phase2/result.json").is_file():
            blockers.append("Phase 2 result is not present")
    release_environment = _configure_release_environment(spec)
    return {
        "status": "ready" if not blockers else "configuration_required",
        "blockers": blockers,
        "phase": phase,
        "required_execute_ack": _ACK[phase],
        "run_name": spec["run_name"],
        "run_root": str(resolved["run_root"]),
        "task_release": spec["task_release"],
        "release_environment": release_environment,
        "phase1_project_budget": spec["phase1"]["project_budget"],
        "phase1_checkpoints": spec["phase1"]["checkpoints"],
        "phase1_parallel_waves": spec["phase1"].get(
            "parallel_waves", False),
        "phase1_parallelism": spec["phase1"].get("parallelism", 1),
        "phase2_stop_policy": resolved["phase2_stop_policy"],
        "development_tasks": list(resolved["development_tasks"]),
        "held_out_tasks": list(resolved["held_out_tasks"]),
        "study_design": resolved["study_design"],
        "task_split_disjoint": not bool(
            set(resolved["development_tasks"])
            & set(resolved["held_out_tasks"])),
        "distribution_file": str(resolved["distribution"]),
        "distribution_sha256": _sha256(resolved["distribution"]),
        "official_grader_available_in_phase": phase == "phase3",
        "memory_mutable_in_phase": phase in {"phase1", "phase2"},
        "curriculum_enabled_in_phase": phase in {"phase1", "phase2"},
        "configs": configs[phase],
    }


def _protocol_lock(spec: dict[str, Any], source_bytes: bytes) -> dict[str, Any]:
    resolved = spec["_resolved"]
    return {
        "schema_version": 1,
        "kind": "recursive_self_improvement_protocol_lock",
        "protocol_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "task_release": spec["task_release"],
        "run_name": spec["run_name"],
        "study_design": resolved["study_design"],
        "distribution_sha256": _sha256(resolved["distribution"]),
        "phase1_project_budget": spec["phase1"]["project_budget"],
        "phase1_checkpoints": spec["phase1"]["checkpoints"],
        "phase1_parallel_waves": spec["phase1"].get(
            "parallel_waves", False),
        "phase1_parallelism": spec["phase1"].get("parallelism", 1),
        "phase2_stop_policy": resolved["phase2_stop_policy"],
        "development_tasks": list(resolved["development_tasks"]),
        "held_out_tasks": list(resolved["held_out_tasks"]),
        "held_out_environment_families": list(
            spec["phase3"].get("held_out_environment_families") or []),
        "configs": {
            phase: {
                field: {
                    "path": str(path),
                    "sha256": _sha256(path),
                }
                for field, path in values.items()
            }
            for phase, values in resolved["configs"].items()
        },
        "boundaries": {
            "official_evaluator_calls_phase1": 0,
            "official_evaluator_calls_phase2": 0,
            "official_evaluator_phase3_only_after_agent_handoff": True,
            "phase3_memory_writeback": False,
            "learning_after_phase3_begins": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--phase", choices=("phase1", "phase2", "phase3"),
                        required=True)
    parser.add_argument("--resume-completed-boundary", action="store_true")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.resume_completed_boundary and args.phase != "phase1":
        raise ProtocolError("--resume-completed-boundary requires phase1")
    protocol_path = Path(args.protocol).expanduser().resolve(strict=True)
    spec = load_protocol(protocol_path)
    report = preflight(spec, args.phase)
    if args.preflight:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.execute != _ACK[args.phase]:
        raise ProtocolError(
            f"--execute must equal {_ACK[args.phase]} for {args.phase}")
    run_root = spec["_resolved"]["run_root"]
    run_root.mkdir(parents=True, exist_ok=True)
    protocol_copy = run_root / "protocol.json"
    source_bytes = protocol_path.read_bytes()
    if protocol_copy.exists() and protocol_copy.read_bytes() != source_bytes:
        raise ProtocolError("protocol changed after this run began")
    if not protocol_copy.exists():
        shutil.copyfile(protocol_path, protocol_copy)
    lock_path = run_root / "protocol_lock.json"
    expected_lock = _protocol_lock(spec, source_bytes)
    if lock_path.exists():
        if _read_object(lock_path) != expected_lock:
            raise ProtocolError(
                "protocol inputs or role configs drifted after this run began")
    else:
        _write_json_atomic(lock_path, expected_lock)
    _append_event(run_root, "PHASE_STARTED", phase=args.phase,
                  protocol_sha256=hashlib.sha256(source_bytes).hexdigest())
    runner = {
        "phase1": execute_phase1,
        "phase2": execute_phase2,
        "phase3": execute_phase3,
    }[args.phase]
    result = (runner(spec, resume_completed_boundary=True)
              if args.resume_completed_boundary else runner(spec))
    _append_event(run_root, "PHASE_COMPLETED", phase=args.phase,
                  status=result.get("status"))
    print("\n=== RECURSIVE IMPROVEMENT " + args.phase.upper() + " "
          + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
