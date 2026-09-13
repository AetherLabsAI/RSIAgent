#!/usr/bin/env python3
"""Run one frozen owner shard of the 106-task self-evolving benchmark.

This scheduler sees task identifiers and sealed result records only.  Each child
process owns one task-local memory lineage and calls the official evaluator only
after its Actor/Verifier/Curriculum lifecycle has terminated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_osworld_v2_baseline_shard import (  # noqa: E402
    PreflightError,
    benchmark_environment,
    discover_tasks,
    git_identity,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json_atomic,
)


DEFAULT_LOCK = REPO / "config/osworld_v2_glm53_k3_self_evolving.lock.json"
DEFAULT_TASK_ROOT = REPO.parent / "OSWorld-V2/evaluation_examples/task_class"
DEFAULT_PYTHON = Path(sys.executable)
EXECUTE_ACK = "RUN-OSWORLD-V2-SELF-EVOLVING-V1"
EXPECTED_SOURCE_TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))
EXPECTED_EXCLUSIONS = frozenset(("task_048", "task_082"))
OWNER_ALIASES = {"xinyu": "xinyue"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PreflightError(f"cannot read YAML: {path}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"expected a YAML mapping: {path}")
    return value


def _require_fields(label: str, observed: dict, expected: dict) -> None:
    mismatches = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in expected.items() if observed.get(key) != value
    }
    if mismatches:
        raise PreflightError(f"{label} critical config mismatch: {mismatches}")


def _locked_path(repo: Path, record: dict, key: str = "path") -> Path:
    path = (repo / str(record.get(key, ""))).resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise PreflightError(f"locked path escapes Forge: {path}") from exc
    return path


def _validate_config_record(
        repo: Path, label: str, record: dict, expected_model: str) -> Path:
    path = _locked_path(repo, record)
    if not path.is_file():
        raise PreflightError(f"{label} config is missing: {path}")
    if record.get("model") != expected_model:
        raise PreflightError(f"{label} model is not {expected_model}")
    if sha256_file(path) != record.get("sha256"):
        raise PreflightError(f"{label} config hash drifted: {path}")
    return path


def validate_lock(repo: Path, lock_path: Path, task_root: Path) -> tuple[dict, dict]:
    lock = read_json(lock_path)
    _require_fields("benchmark lock", lock, {
        "schema_version": 1,
        "benchmark": "OSWorld-v2",
        "task_release": "osworld-v2-2026.06.24",
        "source_task_count": 108,
        "valid_task_count": 106,
        "num_shards": 3,
        "tag": "glm53_k3_self_evolving_v1",
        "seed_label": 5301,
    })
    expected_assignments = [
        {"shard_index": 0, "owner": "xinyue", "first_task": 1,
         "last_task": 36, "range_task_count": 36, "valid_task_count": 36},
        {"shard_index": 1, "owner": "shicheng", "first_task": 37,
         "last_task": 73, "range_task_count": 37, "valid_task_count": 36},
        {"shard_index": 2, "owner": "sibo", "first_task": 74,
         "last_task": 108, "range_task_count": 35, "valid_task_count": 34},
    ]
    if lock.get("assignments") != expected_assignments:
        raise PreflightError("owner assignments differ from the frozen 36/36/34 split")

    source_tasks = discover_tasks(task_root)
    if source_tasks != EXPECTED_SOURCE_TASKS:
        raise PreflightError("OSWorld source task set is not task_001..task_108")
    if set(lock.get("excluded_tasks") or {}) != EXPECTED_EXCLUSIONS:
        raise PreflightError("frozen environment-invalid exclusion set drifted")
    valid_tasks = tuple(
        task for task in source_tasks if task not in EXPECTED_EXCLUSIONS)
    if tuple(lock.get("valid_task_ids") or ()) != valid_tasks:
        raise PreflightError("frozen valid-task sequence drifted")
    valid_digest = sha256_bytes("\n".join(valid_tasks).encode("utf-8"))
    if lock.get("valid_task_ids_sha256") != valid_digest:
        raise PreflightError("frozen valid-task digest drifted")

    config_records = lock.get("configs") or {}
    role_specs = {
        "target_actor": "z-ai/glm-5.3",
        "practice_actor": "z-ai/glm-5.3",
        "practice_verifier": "moonshotai/kimi-k3",
        "curriculum": "moonshotai/kimi-k3",
        "memory_actor": "z-ai/glm-5.3",
    }
    config_paths = {
        role: _validate_config_record(
            repo, role.replace("_", " ").title(),
            config_records.get(role) or {}, model)
        for role, model in role_specs.items()
    }
    target = _yaml(config_paths["target_actor"])
    practice_actor = _yaml(config_paths["practice_actor"])
    practice_verifier = _yaml(config_paths["practice_verifier"])
    curriculum = _yaml(config_paths["curriculum"])
    memory_actor = _yaml(config_paths["memory_actor"])

    _require_fields("Target Actor Agent", target, {
        "model": "z-ai/glm-5.3",
        "provider_order": ["Z.AI"],
        "provider_allow_fallbacks": True,
        "provider_require_parameters": True,
        "max_tokens": 65536,
        "reasoning_effort": "max",
        "primary_temperature": 1.0,
        "top_p": 1.0,
        "agent_decided_stop": True,
        "max_iters": 500,
        "wall_clock_secs": 28800,
        "max_resumes": 1,
        "resume_synthesize_worklog": True,
        "vision_model": "moonshotai/kimi-k3",
        "vision_rounds": 1,
        "look_ensemble": 1,
        "independent_verify": True,
        "verifier_model": "moonshotai/kimi-k3",
        "verifier_continuity": True,
        "agentic_verifier_config": "config/osworld_v2_k3_agentic_verifier.yaml",
        "verifier_evolve_route": True,
        "verifier_local_verdict_only": True,
        "verifier_failure_starts_evolution": True,
        "verifier_hide_actor_memory": True,
        "verifier_stage_lifecycle": True,
        "verifier_persist_scratch": True,
        "env_memory_dir": "",
        "env_memory_orient": False,
        "env_memory_brief": False,
    })
    generic_practice = {
        "agent_decided_stop": True,
        "practice_mode": True,
        "independent_verify": False,
        "max_resumes": 0,
        "max_iters": 2000,
        "wall_clock_secs": 86400,
        "history_keep_pairs": 0,
    }
    _require_fields("Practice Actor Agent", practice_actor, {
        **generic_practice,
        "model": "z-ai/glm-5.3",
        "provider_order": ["Z.AI"],
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "vision_model": "moonshotai/kimi-k3",
        "look_ensemble": 1,
    })
    _require_fields("Memory Actor Agent", memory_actor, {
        **generic_practice,
        "model": "z-ai/glm-5.3",
        "provider_order": ["Z.AI"],
        "reasoning_effort": "max",
        "max_tokens": 65536,
    })
    _require_fields("Practice Verifier Agent", practice_verifier, {
        **generic_practice,
        "model": "moonshotai/kimi-k3",
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "vision_model": "",
        "look_ensemble": 1,
    })
    _require_fields("Curriculum Agent", curriculum, {
        **generic_practice,
        "model": "moonshotai/kimi-k3",
        "reasoning_effort": "max",
        "max_tokens": 65536,
        "vision_model": "",
    })

    actor_meta = lock.get("actor_agent") or {}
    verifier_meta = lock.get("verifier_agent") or {}
    curriculum_meta = lock.get("curriculum_agent") or {}
    if (actor_meta.get("config") != config_records["target_actor"].get("path")
            or actor_meta.get("sha256") != config_records["target_actor"].get("sha256")
            or actor_meta.get("model") != "z-ai/glm-5.3"):
        raise PreflightError("Target Actor Agent lock records disagree")
    if (verifier_meta.get("config") != config_records["practice_verifier"].get("path")
            or verifier_meta.get("sha256") != config_records["practice_verifier"].get("sha256")
            or verifier_meta.get("model") != "moonshotai/kimi-k3"):
        raise PreflightError("Verifier Agent lock records disagree")
    _require_fields("Target Verifier lifecycle", verifier_meta, {
        "candidate_blind_orientation": True,
        "orientation_scope": "each_fresh_target_attempt",
        "orientation_context_continues_into_candidate_verification": True,
        "persistent_within_target_attempt_and_resume": True,
        "fresh_after_each_evolution_wave": True,
        "effect_isolated": True,
    })
    if (curriculum_meta.get("config") != config_records["curriculum"].get("path")
            or curriculum_meta.get("sha256") != config_records["curriculum"].get("sha256")
            or curriculum_meta.get("model") != "moonshotai/kimi-k3"):
        raise PreflightError("Curriculum Agent lock records disagree")

    escalation = lock.get("escalation") or {}
    escalation_path = (repo / str(escalation.get("verifier_config", ""))).resolve()
    if (not escalation_path.is_file()
            or sha256_file(escalation_path)
            != escalation.get("verifier_config_sha256")):
        raise PreflightError("escalated GLM-5.3 Verifier config drifted")
    _require_fields("Escalation topology", escalation, {
        "max_switches_per_target_attempt": 1,
        "actor_model": "moonshotai/kimi-k3",
        "actor_vision": "native_single_read",
        "verifier_model": "z-ai/glm-5.3",
        "verifier_vision": "one_delegated_kimi_k3_read",
        "fresh_verifier_context_on_model_switch": True,
        "candidate_blind_orientation":
            "proactive_on_same_pre_actor_task_start_state",
        "fully_asymmetric": True,
    })

    protocol = lock.get("protocol") or {}
    _require_fields("Self-evolving protocol", protocol, {
        "initial_memory": "blank_per_task",
        "pass_transition": "sealed_evaluation",
        "fail_transition": "same_actor_learning_then_curriculum_practice",
        "curriculum_ready_transition": "fresh_target_actor_and_environment",
        "curriculum_stalled_transition":
            "one_final_fresh_target_test_then_sealed_evaluation",
        "official_evaluator_calls": 1,
        "official_evaluator_feedback_enters_loop": False,
        "cross_task_memory": False,
        "target_verifier_orientation": "candidate_blind_pre_actor",
        "orientation_context": "same_context_as_candidate_verification",
        "escalation_verifier_orientation": "proactive_on_same_task_start_state",
        "practice_verifier_orientation": False,
    })
    profile = lock.get("benchmark_profile") or {}
    profile_path = _locked_path(repo, profile)
    if not profile_path.is_file() or sha256_file(profile_path) != profile.get("sha256"):
        raise PreflightError("0624/0808 compatibility profile drifted")
    if profile.get("classification") != \
            "controlled_compatibility_profile_not_strict_official_release":
        raise PreflightError("compatibility profile classification drifted")

    return lock, {
        **config_paths,
        "escalated_verifier": escalation_path,
        "benchmark_profile": profile_path,
    }


def assignment_for(lock: dict, *, owner: str | None, shard_index: int | None) -> dict:
    assignments = lock["assignments"]
    if owner is not None:
        normalized = OWNER_ALIASES.get(owner.lower(), owner.lower())
        matches = [item for item in assignments if item["owner"] == normalized]
    else:
        matches = [item for item in assignments
                   if item["shard_index"] == shard_index]
    if len(matches) != 1:
        raise PreflightError("owner/shard selector has no unique frozen assignment")
    return matches[0]


def assignment_tasks(lock: dict, assignment: dict) -> tuple[str, ...]:
    selected = tuple(
        task for task in lock["valid_task_ids"]
        if assignment["first_task"] <= int(task[-3:]) <= assignment["last_task"])
    if len(selected) != assignment["valid_task_count"]:
        raise PreflightError("assignment valid-task count drifted")
    return selected


def result_dir(repo: Path, task: str, seed: int, tag: str) -> Path:
    return repo / "results/self_evolving" / task / f"seed{seed}_{tag}"


def validate_result_identity(result: dict, task: str, lock: dict) -> None:
    expected = {
        "task": task,
        "seed": int(lock["seed_label"]),
        "tag": str(lock["tag"]),
        "model": str(lock["actor_agent"]["model"]),
        "official_evaluator_feedback_entered_loop": False,
        "target_verifier_orientation":
            "candidate_blind_pre_actor_same_context",
    }
    mismatch = {
        key: {"expected": value, "observed": result.get(key)}
        for key, value in expected.items() if result.get(key) != value
    }
    try:
        score = float(result["score"])
        if not math.isfinite(score):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        mismatch["score"] = {
            "expected": "finite numeric", "observed": result.get("score")}
    for name in (
            "target_cycles", "evolutions", "practice_projects",
            "target_verifier_orientations"):
        if type(result.get(name)) is not int or result[name] < 0:
            mismatch[name] = {
                "expected": "nonnegative integer", "observed": result.get(name)}
    if (type(result.get("target_cycles")) is int
            and type(result.get("target_verifier_orientations")) is int
            and result["target_verifier_orientations"]
            != 2 * result["target_cycles"]):
        mismatch["target_verifier_orientations"] = {
            "expected": 2 * result["target_cycles"],
            "observed": result["target_verifier_orientations"],
        }
    digest = result.get("target_verifier_orientation_report_sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)):
        mismatch["target_verifier_orientation_report_sha256"] = {
            "expected": "lowercase SHA-256", "observed": digest}
    if not isinstance(result.get("llm_providers"), dict):
        mismatch["llm_providers"] = {
            "expected": "provider provenance object",
            "observed": type(result.get("llm_providers")).__name__,
        }
    if mismatch:
        raise PreflightError(
            f"result identity/provenance mismatch for {task}: {mismatch}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--owner", choices=("xinyue", "xinyu", "shicheng", "sibo"),
        help="select the frozen task range by teammate name")
    selector.add_argument(
        "--shard-index", type=int,
        help="compatibility selector: 0=xinyue, 1=shicheng, 2=sibo")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--boot-stagger-secs", type=float, default=20.0)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="development only; official jobs require clean tracked trees")
    args = parser.parse_args()
    if args.concurrency < 1 or args.boot_stagger_secs < 0:
        raise PreflightError("concurrency must be positive and stagger nonnegative")

    repo = args.repo.resolve()
    task_root = args.task_root.resolve()
    lock_path = args.lock.resolve()
    lock, paths = validate_lock(repo, lock_path, task_root)
    assignment = assignment_for(
        lock, owner=args.owner, shard_index=args.shard_index)
    tasks = assignment_tasks(lock, assignment)

    forge_git = git_identity(repo)
    osworld_root = task_root.parents[1]
    osworld_git = git_identity(osworld_root)
    if not args.allow_dirty and (
            not forge_git["tracked_worktree_clean"]
            or not osworld_git["tracked_worktree_clean"]):
        raise PreflightError(
            "official jobs require clean tracked Forge and OSWorld-v2 worktrees")

    seed = int(lock["seed_label"])
    tag = str(lock["tag"])
    print(f"freeze={lock_path}")
    print(f"forge_commit={forge_git['commit']}")
    print(f"osworld_commit={osworld_git['commit']}")
    print(f"lock_sha256={sha256_file(lock_path)}")
    print(f"Target Actor Agent={lock['actor_agent']['model']} ({paths['target_actor']})")
    print(f"Target Verifier Agent={lock['verifier_agent']['model']} "
          f"({paths['practice_verifier']})")
    print(f"Practice Actor Agent={lock['practice']['actor_model']} "
          f"({paths['practice_actor']})")
    print(f"Practice Verifier Agent={lock['practice']['verifier_model']} "
          f"({paths['practice_verifier']})")
    print(f"Curriculum Agent={lock['curriculum_agent']['model']} "
          f"({paths['curriculum']})")
    print("Target escalation="
          f"{lock['escalation']['actor_model']} Actor Agent <-> "
          f"{lock['escalation']['verifier_model']} Verifier Agent")
    print(f"memory={lock['memory']['initial_state']}; "
          f"scope={lock['memory']['scope']}; "
          f"cross_task={lock['memory']['cross_task_transfer']}")
    print(f"shard={assignment['shard_index']}/{lock['num_shards']} "
          f"owner={assignment['owner']} "
          f"range={assignment['first_task']:03d}-{assignment['last_task']:03d} "
          f"valid_tasks={len(tasks)}")
    print(" ".join(tasks))
    if args.dry_run:
        return 0

    python = args.python.resolve()
    if not python.is_file():
        raise PreflightError(f"Python interpreter does not exist: {python}")
    environment = benchmark_environment(repo, osworld_root, lock)

    completed = []
    partial = []
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
            "partial result directories would be overwritten; preserve them and "
            f"use a separately locked retry tag: {partial}")

    shard_index = int(assignment["shard_index"])
    job_root = repo / "results/self_evolving_jobs" / tag / f"shard_{shard_index}"
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
        "task_ids": list(tasks),
        "config_paths": {
            role: {"path": str(path), "sha256": sha256_file(path)}
            for role, path in sorted(paths.items())},
        "forge_git": forge_git,
        "osworld_git": osworld_git,
        "scheduler": {
            "concurrency": args.concurrency,
            "boot_stagger_seconds": args.boot_stagger_secs,
            "python": str(python),
        },
    }
    if manifest_path.exists():
        prior = read_json(manifest_path)
        immutable = (
            "lock_sha256", "tag", "seed_label", "shard_index", "num_shards",
            "owner", "assignment", "task_ids", "config_paths", "forge_git",
            "osworld_git")
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
    active: dict[subprocess.Popen, tuple[str, object, Path]] = {}
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
                    str(python), str(repo / "run_self_evolving.py"), task,
                    "--seed", str(seed), "--tag", tag,
                    "--target-config", str(paths["target_actor"]),
                    "--practice-actor-config", str(paths["practice_actor"]),
                    "--practice-verifier-config", str(paths["practice_verifier"]),
                    "--curriculum-config", str(paths["curriculum"]),
                    "--memory-config", str(paths["memory_actor"]),
                    "--benchmark-lock", str(lock_path),
                    "--benchmark-profile", str(paths["benchmark_profile"]),
                    "--execute", EXECUTE_ACK,
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
                if pending and args.boot_stagger_secs:
                    time.sleep(args.boot_stagger_secs)

            finished = [process for process in active if process.poll() is not None]
            for process in finished:
                task, handle, log_path = active.pop(process)
                handle.close()
                result_path = result_dir(repo, task, seed, tag) / "result.json"
                record = state["tasks"][task]
                record.update({"finished_at": utc_now(), "exit_code": process.returncode})
                if process.returncode == 0 and result_path.is_file():
                    result = read_json(result_path)
                    validate_result_identity(result, task, lock)
                    record.update({
                        "state": "complete", "status": result.get("status"),
                        "score": result.get("score"),
                        "target_cycles": result.get("target_cycles"),
                        "evolutions": result.get("evolutions"),
                        "practice_projects": result.get("practice_projects"),
                        "result": str(result_path),
                    })
                    print(
                        f"finished {task} score={result.get('score')} "
                        f"cycles={result.get('target_cycles')} "
                        f"evolutions={result.get('evolutions')} "
                        f"practice={result.get('practice_projects')}", flush=True)
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
