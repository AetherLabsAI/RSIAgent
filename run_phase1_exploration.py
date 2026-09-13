#!/usr/bin/env python3
"""Run grader-free distribution-guided capability exploration.

This is Phase 1 of the recursive self-improvement protocol. It boots only
null-task OSWorld desktops, gives a persistent Curriculum Agent an explicitly
supplied task-distribution description, and runs fresh Actor Agent -> Verifier
Agent projects. In parallel-wave mode the Curriculum Agent authors each wave,
all branches read one pre-wave memory snapshot, and Actor-owned memory writes are
serialized afterward. The official benchmark task loader and evaluator are
never imported here.

The external project budget counts complete project/memory lifecycles.  It does
not constrain an Agent turn, prescribe a curriculum, or stand for convergence.
The Curriculum Agent may independently declare SATURATED earlier.  Exact memory
checkpoints are saved without a byte or file-count ceiling.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_forge_path,
    resolve_forge_root,
    resolve_osworld_root,
)


FORGE_ROOT = resolve_forge_root()
OSWORLD_ROOT = resolve_osworld_root(forge_root=FORGE_ROOT)
RESULTS_ROOT = FORGE_ROOT / "results" / "recursive_improvement"
DEFAULT_ACTOR_CONFIG = FORGE_ROOT / "config/glm53_practice_actor.yaml"
DEFAULT_VERIFIER_CONFIG = FORGE_ROOT / "config/e15_verify.yaml"
DEFAULT_VERIFIER_CONTROL_CONFIG = (
    FORGE_ROOT / "config/osworld_v2_glm53_k3_recursive_practice.yaml")
DEFAULT_CURRICULUM_CONFIG = FORGE_ROOT / "config/k3_curriculum.yaml"
DEFAULT_MEMORY_CONFIG = FORGE_ROOT / "config/glm53_practice_actor.yaml"
DEFAULT_CORPUS = FORGE_ROOT / "results/explore/corpus_shingles.json"
EXECUTE_ACK = "RUN-PHASE1-DISTRIBUTION-EXPLORATION"
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


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


def _append_event(path: Path, event_type: str, *, status: str,
                  state: dict[str, Any], payload: dict[str, Any]) -> None:
    record = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": event_type,
        "status": status,
        "state": state,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _parse_checkpoints(value: str, budget: int) -> tuple[int, ...]:
    try:
        checkpoints = tuple(sorted(set(
            int(item.strip()) for item in value.split(",") if item.strip())))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "checkpoints must be comma-separated integers") from exc
    if any(index < 0 or index > budget for index in checkpoints):
        raise argparse.ArgumentTypeError(
            "checkpoints must lie between zero and the project budget")
    return checkpoints


def _safe_phase_root(name: str) -> Path:
    if not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("--run-name must be one safe basename")
    root = (RESULTS_ROOT / name / "phase1").resolve()
    if root.parent.parent != RESULTS_ROOT.resolve():
        raise ValueError("Phase-1 result root escapes results/recursive_improvement")
    return root


def _resolve_input_path(value: str, *, directory: bool = False) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else FORGE_ROOT / raw
    if candidate.is_symlink():
        raise RuntimeError(f"input path may not be a symlink: {candidate}")
    path = candidate.resolve(strict=True)
    expected = path.is_dir() if directory else path.is_file()
    if not expected:
        kind = "directory" if directory else "file"
        raise RuntimeError(f"input path is not one {kind}: {path}")
    return path


def _install_paths() -> None:
    if not OSWORLD_ROOT.is_dir():
        raise RuntimeError(f"OSWorld-V2 checkout is missing: {OSWORLD_ROOT}")
    try:
        import dotenv
        dotenv.load_dotenv(OSWORLD_ROOT / ".env", override=False)
    except ImportError:
        pass
    for path in (str(OSWORLD_ROOT), str(FORGE_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(OSWORLD_ROOT)


def _load_configs(args) -> tuple[dict[str, Any], dict[str, Path]]:
    from config.settings import load

    paths = {
        "actor": resolve_forge_path(args.actor_config, forge_root=FORGE_ROOT),
        "verifier": resolve_forge_path(
            args.verifier_config, forge_root=FORGE_ROOT),
        "verifier_control": resolve_forge_path(
            args.verifier_control_config, forge_root=FORGE_ROOT),
        "curriculum": resolve_forge_path(
            args.curriculum_config, forge_root=FORGE_ROOT),
        "memory_actor": resolve_forge_path(
            args.memory_config, forge_root=FORGE_ROOT),
    }
    configs = {role: load(str(path)) for role, path in paths.items()}
    for cfg in configs.values():
        normalize_verifier_config_paths(cfg, FORGE_ROOT)
    for role in ("actor", "verifier", "curriculum", "memory_actor"):
        cfg = configs[role]
        if (not cfg.agent_decided_stop or not cfg.practice_mode
                or cfg.independent_verify or cfg.max_resumes != 0):
            raise RuntimeError(
                f"Phase-1 {role} must use Agent-owned practice mode")
    if configs["actor"].model != "z-ai/glm-5.3":
        raise RuntimeError("Phase-1 Actor Agent must be GLM-5.3")
    if configs["memory_actor"].model != configs["actor"].model:
        raise RuntimeError(
            "memory writing must continue with the same Actor Agent model")
    if configs["verifier"].model != "moonshotai/kimi-k3":
        raise RuntimeError("Phase-1 Verifier Agent must be Kimi K3")
    if configs["curriculum"].model != "moonshotai/kimi-k3":
        raise RuntimeError("Phase-1 Curriculum Agent must be Kimi K3")
    control = configs["verifier_control"]
    if (control.verifier_model != "moonshotai/kimi-k3"
            or not control.independent_verify
            or control.verifier_stage_lifecycle
            or control.verifier_execution_mode != "rollback_mirror"
            or not control.verifier_hide_actor_memory):
        raise RuntimeError(
            "Phase-1 candidate-only Agentic Verifier control profile drifted")
    return configs, paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", required=True)
    parser.add_argument(
        "--distribution-file", required=True,
        help=("UTF-8 distribution description, or exact query when "
              "--target-query-conditioned is set"))
    parser.add_argument(
        "--target-query-conditioned", action="store_true",
        help=("derive diverse Phase-1 exploration from the supplied exact query; "
              "the unchanged target itself remains reserved for Phase 2"))
    parser.add_argument("--project-budget", type=int, default=8)
    parser.add_argument("--checkpoints", default="0,2,4,8")
    parser.add_argument(
        "--parallel-waves", action="store_true",
        help=("let the persistent Curriculum Agent author adaptive project "
              "waves instead of one project per outer turn"))
    parser.add_argument(
        "--max-parallel", type=int, default=4,
        help=("host scheduling capacity for a Curriculum-authored wave; this "
              "does not prescribe or truncate the wave width"))
    parser.add_argument("--actor-config", default=str(DEFAULT_ACTOR_CONFIG))
    parser.add_argument("--verifier-config", default=str(DEFAULT_VERIFIER_CONFIG))
    parser.add_argument(
        "--verifier-control-config",
        default=str(DEFAULT_VERIFIER_CONTROL_CONFIG))
    parser.add_argument(
        "--curriculum-config", default=str(DEFAULT_CURRICULUM_CONFIG))
    parser.add_argument("--memory-config", default=str(DEFAULT_MEMORY_CONFIG))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument(
        "--resume-completed-boundary", action="store_true",
        help=("resume an infrastructure-stopped lineage only when every "
              "accepted project is durably closed and the persistent "
              "Curriculum transcript is intact"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute", default="", help=EXECUTE_ACK)
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_paths()
    args = _parser().parse_args(argv)
    if args.project_budget < 0:
        raise RuntimeError("--project-budget must be nonnegative")
    if args.max_parallel < 1:
        raise RuntimeError("--max-parallel must be positive")
    checkpoints = _parse_checkpoints(args.checkpoints, args.project_budget)
    distribution_path = _resolve_input_path(args.distribution_file)
    distribution = distribution_path.read_text(encoding="utf-8").strip()
    if not distribution:
        raise RuntimeError("distribution description is empty")
    corpus = _resolve_input_path(args.corpus)
    root = _safe_phase_root(args.run_name)
    configs, config_paths = _load_configs(args)
    from env.qemu_rollback import normalize_verifier_execution_mode
    from qemu_provider import prepare_checkpointable_docker_provider
    verifier_execution_mode = normalize_verifier_execution_mode(
        configs["verifier_control"].verifier_execution_mode)
    prepare_checkpointable_docker_provider(verifier_execution_mode)

    preflight = {
        "status": "ready",
        "phase": "distribution_guided_capability_exploration",
        "study_design": (
            "target_conditioned_adaptation" if args.target_query_conditioned
            else "held_out_generalization"),
        "result_root": str(root),
        "project_budget": args.project_budget,
        "memory_checkpoints": list(checkpoints),
        "parallel_waves": args.parallel_waves,
        "max_parallel": args.max_parallel if args.parallel_waves else 1,
        "wave_width_agent_owned": args.parallel_waves,
        "branch_memory_snapshot_isolated": args.parallel_waves,
        "memory_writes_serialized": args.parallel_waves,
        "curriculum_may_stop_early": True,
        "curriculum_terminal_token": "SATURATED",
        "actor_agent": configs["actor"].model,
        "verifier_agent": configs["verifier"].model,
        "verifier_execution_mode": verifier_execution_mode,
        "verifier_candidate_surface":
            "actual_candidate_with_actor_private_artifacts_hidden",
        "verifier_pre_actor_orientation": False,
        "curriculum_agent": configs["curriculum"].model,
        "official_evaluator_imported": False,
        "official_evaluator_calls": 0,
        "distribution_sha256": _sha256(distribution_path),
    }
    if args.preflight:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    if args.execute != EXECUTE_ACK:
        raise RuntimeError(f"--execute must equal {EXECUTE_ACK}")
    if (root.exists() and any(root.iterdir())
            and not args.resume_completed_boundary):
        raise RuntimeError("Phase-1 root is nonempty; preserve it and use a new name")
    if args.resume_completed_boundary and not root.is_dir():
        raise RuntimeError("Phase-1 resume root does not exist")
    root.mkdir(parents=True, exist_ok=True)
    events_path = root / "events.jsonl"
    manifest = {
        "schema_version": 1,
        "kind": "recursive_self_improvement_phase1",
        **preflight,
        "distribution_path": str(distribution_path),
        "corpus_sha256": _sha256(corpus),
        "configs": {
            role: {
                "path": str(config_paths[role]),
                "sha256": _sha256(config_paths[role]),
                "effective": dataclasses.asdict(configs[role]),
            }
            for role in sorted(configs)
        },
    }
    # Persistence turns dataclass tuples into JSON arrays.  Compare the same
    # canonical JSON value on resume so tuple/list representation is not
    # mistaken for protocol drift, while every actual field and hash remains
    # locked.
    manifest = json.loads(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True))
    manifest_path = root / "manifest.json"
    if args.resume_completed_boundary:
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeError("Phase-1 resume manifest is unreadable") from exc
        if existing_manifest != manifest:
            raise RuntimeError(
                "Phase-1 resume protocol/config manifest drifted")
    else:
        _write_json_atomic(manifest_path, manifest)

    from desktop_env.desktop_env import DesktopEnv
    from env.vm import VM
    from explore.e15_loop import (
        E15Hooks,
        _atomic_install_memory,
        _manifest,
        _read_memory_tree,
        e15_evolve,
    )
    from explore.e15_v12_loop import _memory_tree_sha256

    def vm_factory():
        desktop = DesktopEnv(
            provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
            screen_size=(1920, 1080), headless=True, require_a11y_tree=False,
            volume_size=60)
        try:
            desktop.reset(task_config=None)
            return desktop, VM(desktop)
        except Exception:
            desktop.close()
            raise

    if args.parallel_waves:
        from explore.phase1_wave import evolve_parallel_phase1

        result = evolve_parallel_phase1(
            root=str(root), target_direction=distribution,
            actor_cfg=configs["actor"],
            curriculum_cfg=configs["curriculum"],
            memory_cfg=configs["memory_actor"],
            verifier_control_cfg=configs["verifier_control"],
            vm_factory=vm_factory, corpus_path=str(corpus), hooks=E15Hooks(),
            event_sink=lambda event_type, **kwargs: _append_event(
                events_path, event_type, **kwargs),
            project_budget=args.project_budget,
            checkpoint_projects=checkpoints,
            max_parallel=args.max_parallel,
            target_query_conditioned=args.target_query_conditioned,
            resume_completed_boundary=args.resume_completed_boundary)
    else:
        desktop, vm = vm_factory()
        try:
            result = e15_evolve(
                vm, str(root), distribution,
                configs["actor"], configs["verifier"], configs["curriculum"],
                configs["memory_actor"], corpus_path=str(corpus), hooks=E15Hooks(),
                event_sink=lambda event_type, **kwargs: _append_event(
                    events_path, event_type, **kwargs),
                project_budget=args.project_budget,
                checkpoint_projects=checkpoints,
                phase1_exploration=True,
                phase1_target_conditioned=args.target_query_conditioned,
                agentic_verifier_cfg=configs["verifier_control"],
                resume_completed_boundary=args.resume_completed_boundary)
        finally:
            desktop.close()

    memory = _read_memory_tree(result.memory_dir)
    frozen = root / "memory_frozen"
    _atomic_install_memory(str(frozen), memory)
    payload = {
        "schema_version": 1,
        "phase": "distribution_guided_capability_exploration",
        "study_design": (
            "target_conditioned_adaptation" if args.target_query_conditioned
            else "held_out_generalization"),
        "status": result.status,
        "projects": result.projects,
        "stop_reason": result.reason,
        "project_budget": args.project_budget,
        "checkpoints": list(checkpoints),
        "parallel_waves": args.parallel_waves,
        "max_parallel": args.max_parallel if args.parallel_waves else 1,
        "memory_files": len(memory),
        "memory_bytes": sum(len(data) for data in memory.values()),
        "memory_manifest": _manifest(memory),
        "memory_tree_sha256": _memory_tree_sha256(memory),
        "memory_frozen_path": str(frozen),
        "official_evaluator_calls": 0,
    }
    _write_json_atomic(root / "result.json", payload)
    print("\n=== PHASE 1 RESULT " + json.dumps(payload, sort_keys=True))
    return 0 if result.status in {"saturated", "budget_exhausted"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
