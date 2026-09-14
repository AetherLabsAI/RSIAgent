"""Run Phase 2 target-conditioned learning without official evaluation."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from benchmarks.osworld.runtime import (
    DEFAULT_BENCHMARK_PROFILE,
    OSWORLD_ROOT,
    RSIAGENT_ROOT,
    _append_event,
    _benchmark_provenance,
    _install_paths,
)
from config.runtime_paths import normalize_verifier_config_paths, resolve_path
from core.self_evolving_loop import DEFAULT_PHASE2_STOP_POLICY

RECURSIVE_RESULTS_ROOT = RSIAGENT_ROOT / "results" / "recursive_improvement"

PHASE2_EXECUTE_ACK = "RUN-PHASE2-FAILURE-CONDITIONED-PRACTICE"

ALL_TASKS = tuple((f"task_{index:03d}" for index in range(1, 109)))

DEFAULT_PHASE2_TARGET_CONFIG = RSIAGENT_ROOT / "config/roles/target.yaml"

DEFAULT_PRACTICE_ACTOR_CONFIG = RSIAGENT_ROOT / "config/roles/actor.yaml"

DEFAULT_PRACTICE_VERIFIER_CONFIG = RSIAGENT_ROOT / "config/roles/verifier.yaml"

DEFAULT_CURRICULUM_CONFIG = RSIAGENT_ROOT / "config/roles/curriculum.yaml"

DEFAULT_MEMORY_CONFIG = DEFAULT_PRACTICE_ACTOR_CONFIG

DEFAULT_CORPUS = RSIAGENT_ROOT / "results/explore/corpus_shingles.json"

_SAFE_NAME = re.compile("[A-Za-z0-9_.-]+\\Z")

log = logging.getLogger("rsiagent.self_evolving")


def _release_target_vm(environment, emit):
    """Keep a blocked verification candidate available for explicit recovery."""
    from core.verifier_runtime import AgenticVerifierNoProgressError

    if isinstance(sys.exc_info()[1], AgenticVerifierNoProgressError):
        emit(
            "TARGET_VM_PRESERVED_FOR_INFRASTRUCTURE_REVIEW",
            {
                "reason": "Verifier actionless guard; no semantic verdict",
                "automatic_retry": False,
            },
        )
        return
    environment.desktop.close()


@dataclasses.dataclass
class _TargetEnvironment:
    desktop: Any
    vm: Any
    instruction: str
    boot_secs: float
    surface_baseline: dict[str, str]
    verifier_private_paths: tuple[str, ...]


@dataclasses.dataclass
class _TargetVerifier:
    cycle: int
    sessions: dict[tuple[str, str], Any] = dataclasses.field(default_factory=dict)
    configs: dict[tuple[str, str], Any] = dataclasses.field(default_factory=dict)
    orientation_report_sha256s: dict[tuple[str, str], str] = dataclasses.field(
        default_factory=dict
    )
    task_start_identity_sha256: str = ""


@dataclasses.dataclass
class _TargetActor:
    memory: dict[str, bytes]
    cycle: int


@dataclasses.dataclass
class _TargetOutput:
    loop_result: Any
    history: list[dict[str, Any]]
    active_history: list[dict[str, Any]]
    active_cfg: Any
    sink_root: str
    active_verifier_identity: tuple[str, str]
    orientation_report_sha256: str


def _sha256_file(path: Path) -> str:
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
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _safe_phase2_result_root(protocol_run: str, task_id: str, seed: int) -> Path:
    if not _SAFE_NAME.fullmatch(protocol_run) or protocol_run in {".", ".."}:
        raise ValueError("--protocol-run must be one safe basename")
    if seed < 0:
        raise ValueError("--seed must be nonnegative")
    root = (
        RECURSIVE_RESULTS_ROOT
        / protocol_run
        / "phase2"
        / task_id
        / f"attempt_{seed:04d}"
    ).resolve()
    expected = (RECURSIVE_RESULTS_ROOT / protocol_run / "phase2" / task_id).resolve()
    if root.parent != expected:
        raise ValueError("Phase-2 result root escapes recursive_improvement")
    return root


def _task_instruction(task: Any) -> str:
    """Read instance data before class attributes (dynamic credentials depend on it)."""
    if hasattr(task, "get") and callable(task.get):
        value = task.get("instruction")
        if isinstance(value, str) and value.strip():
            return value
    value = getattr(task, "instruction", "")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("loaded task has no nonempty instruction")
    return value


def _load_task(task_id: str) -> tuple[Any, str]:
    from task_loader import load_task_config, resolve_task_json_path

    task_path = resolve_task_json_path(
        task_id=task_id, base_dir="evaluation_examples", eval_version="v2"
    )
    task = load_task_config(
        task_path, task_id=task_id, base_dir="evaluation_examples", eval_version="v2"
    )
    return (task, _task_instruction(task))


def _stable_target_direction(task: Any, instruction: str) -> str:
    """Remove only per-run credentials while retaining task semantics verbatim."""
    from explore.commit import normalize_instruction_for_corpus

    base = getattr(task, "base_instruction", "")
    semantic = base if isinstance(base, str) and base.strip() else instruction
    direction = normalize_instruction_for_corpus(semantic).strip()
    if not direction:
        raise RuntimeError("stable target direction is empty")
    return direction


def _load_configs(args):
    from config.settings import load

    paths = {
        "target_actor": resolve_path(args.target_config, repo_root=RSIAGENT_ROOT),
        "practice_actor": resolve_path(
            args.practice_actor_config, repo_root=RSIAGENT_ROOT
        ),
        "practice_verifier": resolve_path(
            args.practice_verifier_config, repo_root=RSIAGENT_ROOT
        ),
        "curriculum": resolve_path(args.curriculum_config, repo_root=RSIAGENT_ROOT),
        "memory_actor": resolve_path(args.memory_config, repo_root=RSIAGENT_ROOT),
    }
    configs = {role: load(str(path)) for (role, path) in paths.items()}
    for cfg in configs.values():
        normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
    target = configs["target_actor"]
    required_target = {
        "model": "z-ai/glm-5.3",
        "vision_model": "moonshotai/kimi-k3",
        "look_ensemble": 1,
        "independent_verify": True,
        "verifier_model": "moonshotai/kimi-k3",
        "verifier_continuity": True,
        "verifier_evolve_route": True,
        "verifier_local_verdict_only": True,
        "verifier_failure_starts_evolution": True,
        "verifier_hide_actor_memory": True,
        "verifier_stage_lifecycle": False,
        "verifier_persist_scratch": True,
        "agent_decided_stop": True,
        "env_memory_dir": "",
    }
    mismatches = {
        key: {"expected": expected, "observed": getattr(target, key)}
        for (key, expected) in required_target.items()
        if getattr(target, key) != expected
    }
    if mismatches:
        raise RuntimeError(f"target self-evolving config mismatch: {mismatches}")
    if not target.agentic_verifier_config:
        raise RuntimeError("target config lacks the full Agentic Verifier")
    phase2_required = {
        "verifier_unverified_evidence": True,
        "verifier_execution_mode": "rollback_mirror",
        "actor_evaluator_isolation": True,
    }
    phase2_mismatches = {
        key: {"expected": expected, "observed": getattr(target, key)}
        for (key, expected) in phase2_required.items()
        if getattr(target, key) != expected
    }
    if phase2_mismatches:
        raise RuntimeError(f"Phase-2 target config mismatch: {phase2_mismatches}")
    for role in ("practice_actor", "practice_verifier", "curriculum", "memory_actor"):
        cfg = configs[role]
        if (
            not cfg.agent_decided_stop
            or not cfg.practice_mode
            or cfg.independent_verify
            or (cfg.max_resumes != 0)
        ):
            raise RuntimeError(f"{role} config is not Agent-owned practice mode")
    if configs["practice_actor"].model != target.model:
        raise RuntimeError("target and practice Actor Agent identities differ")
    if configs["memory_actor"].model != configs["practice_actor"].model:
        raise RuntimeError("practice and memory Actor Agent identities differ")
    if configs["practice_verifier"].model != "moonshotai/kimi-k3":
        raise RuntimeError("practice Verifier Agent must be Kimi K3")
    if configs["curriculum"].model != "moonshotai/kimi-k3":
        raise RuntimeError("Curriculum Agent must be Kimi K3")
    return (configs, paths)


def _practice_agentic_control_config(
    target_cfg: Any, practice_verifier_path: Path
) -> Any:
    """Bind the full practice Verifier to the explicitly selected config.

    ``evolve_until_ready`` needs the target control configuration for rollback
    isolation and other harness policy, while ``verify_agentic`` loads the
    Verifier Agent itself from ``agentic_verifier_config``. Keeping the target
    value here would silently ignore ``--practice-verifier-config``.
    """
    return dataclasses.replace(
        target_cfg, agentic_verifier_config=str(practice_verifier_path.resolve())
    )


def _write_phase2_manifest(
    root: Path,
    *,
    task_id: str,
    protocol_run: str,
    instruction: str,
    target_direction: str,
    initial_memory_path: Path,
    initial_memory: dict[str, bytes],
    configs: dict[str, Any],
    paths: dict[str, Path],
    benchmark_provenance: dict[str, Any],
    stop_policy: str = DEFAULT_PHASE2_STOP_POLICY,
    curriculum_memory_access: str = "read_only",
) -> None:
    from explore.practice_loop import _manifest
    from explore.target_learning import _memory_tree_sha256

    record = {
        "schema_version": 1,
        "kind": "recursive_self_improvement_phase2_targeted_practice",
        "task_id": task_id,
        "protocol_run": protocol_run,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "target_direction_sha256": hashlib.sha256(
            target_direction.encode("utf-8")
        ).hexdigest(),
        "initial_memory": {
            "path": str(initial_memory_path),
            "files": len(initial_memory),
            "bytes": sum((len(data) for data in initial_memory.values())),
            "tree_sha256": _memory_tree_sha256(initial_memory),
            "manifest": _manifest(initial_memory),
            "writeback": False,
        },
        "protocol": phase2_protocol_metadata(stop_policy, curriculum_memory_access),
        "benchmark_provenance_host_only": benchmark_provenance,
        "configs": {
            role: {
                "path": str(paths[role]),
                "sha256": _sha256_file(paths[role]),
                "effective": dataclasses.asdict(configs[role]),
            }
            for role in sorted(configs)
        },
    }
    _write_json_atomic(root / "manifest.json", record)


def phase2_protocol_metadata(
    stop_policy: str, curriculum_memory_access: str = "read_only"
) -> dict[str, Any]:
    """One description shared by preflight and the persisted run manifest."""
    from core.self_evolving_loop import Phase2StopPolicy

    policy = Phase2StopPolicy(stop_policy)
    if curriculum_memory_access not in {"read_only", "none"}:
        raise ValueError("unknown Curriculum memory access mode")
    review = policy is Phase2StopPolicy.CURRICULUM_REVIEW
    return {
        "stop_policy": policy.value,
        "curriculum_memory_access": curriculum_memory_access,
        "curriculum_actor_learning_diagnosis": "available",
        "first_action": "fresh_actor_attempts_development_task",
        "pass_transition": "same_actor_learning_then_curriculum_selects_next_experience"
        if review
        else "same_actor_learning_then_phase2_task_complete",
        "fail_transition": "same_actor_learning_then_curriculum_selects_next_experience",
        "verified_experience_transition": "same_actor_distillation_and_reconciliation_after_pass_or_fail",
        "curriculum_authority": "select_experience_only_never_edit_or_approve_memory",
        "curriculum_after_pass": review,
        "pass_without_high_value_project": "ready_for_target_and_phase2_task_complete"
        if review
        else "phase2_task_complete_without_curriculum_review",
        "project_after_any_outcome": "fresh_target_retry_required_after_practice",
        "unverified_transition": "same_actor_evidence_then_same_persistent_verifier",
        "unresolved_unverified_blocks_phase_transition": True,
        "retry_transition": "fresh_actor_with_updated_memory",
        "target_verifier_orientation": False,
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_enters_learning": False,
    }


def _parser() -> argparse.ArgumentParser:
    from core.self_evolving_loop import Phase2StopPolicy

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", choices=ALL_TASKS)
    parser.add_argument("--seed", type=int, default=5301)
    parser.add_argument(
        "--target-config",
        default=None,
        help="target Actor/Verifier control config; defaults to config/roles/target.yaml",
    )
    parser.add_argument(
        "--practice-actor-config", default=str(DEFAULT_PRACTICE_ACTOR_CONFIG)
    )
    parser.add_argument(
        "--practice-verifier-config", default=str(DEFAULT_PRACTICE_VERIFIER_CONFIG)
    )
    parser.add_argument("--curriculum-config", default=str(DEFAULT_CURRICULUM_CONFIG))
    parser.add_argument("--memory-config", default=str(DEFAULT_MEMORY_CONFIG))
    parser.add_argument("--benchmark-profile", default=str(DEFAULT_BENCHMARK_PROFILE))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))

    parser.add_argument(
        "--initial-memory",
        default="",
        help="required immutable input memory directory for Phase 2",
    )
    parser.add_argument(
        "--phase2-stop-policy",
        choices=[p.value for p in Phase2StopPolicy],
        default=None,
        help="Phase-2 training only: curriculum_review (default) reviews PASS learning for further useful practice and requires a fresh target test after practice; verifier_pass stops after PASS learning",
    )
    parser.add_argument(
        "--phase2-curriculum-memory-access",
        choices=["read_only", "none"],
        default=None,
        help="Phase-2 training only: expose a disposable Actor memory copy to Curriculum (read_only, default), or attach an empty directory for a fresh comparison (none); Actor memory and diagnosis remain",
    )
    parser.add_argument(
        "--protocol-run", default="", help="safe experiment lineage name for Phase 2"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument(
        "--execute",
        default="",
        help=f"paid launch acknowledgement: {PHASE2_EXECUTE_ACK}",
    )
    return parser


def main(argv: list[str] | None = None, *, recovery_plan=None) -> int:
    args = _parser().parse_args(argv)
    _install_paths()
    args.phase2_stop_policy = args.phase2_stop_policy or DEFAULT_PHASE2_STOP_POLICY
    args.phase2_curriculum_memory_access = (
        args.phase2_curriculum_memory_access or "read_only"
    )
    if recovery_plan is not None:
        try:
            declared_access = json.loads(Path(recovery_plan).read_text()).get(
                "curriculum_memory_access", "read_only"
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "memory-access ablations require a fresh Phase-2 lineage or a valid condition-preserving recovery plan"
            ) from exc
        if args.phase2_curriculum_memory_access != declared_access:
            raise RuntimeError("recovery cannot change Curriculum memory access")
    if args.target_config is None:
        args.target_config = str(DEFAULT_PHASE2_TARGET_CONFIG)
    if not args.protocol_run:
        raise RuntimeError("Phase 2 requires --protocol-run")
    if not args.initial_memory:
        raise RuntimeError("Phase 2 requires --initial-memory")
    root = _safe_phase2_result_root(args.protocol_run, args.task_id, args.seed)
    (configs, config_paths) = _load_configs(args)
    corpus = Path(args.corpus).expanduser().resolve(strict=True)
    from explore.commit import validate_instruction_corpus

    validate_instruction_corpus(str(corpus))
    (initial_task, initial_instruction) = _load_task(args.task_id)
    target_direction = _stable_target_direction(initial_task, initial_instruction)
    task_class_path = (
        OSWORLD_ROOT / "evaluation_examples/task_class" / f"{args.task_id}.py"
    ).resolve(strict=True)
    profile_path = resolve_path(args.benchmark_profile, repo_root=RSIAGENT_ROOT)
    benchmark_provenance = _benchmark_provenance(
        args.task_id, task_class_path, profile_path
    )
    target_cfg = configs["target_actor"]
    practice_agentic_control_cfg = _practice_agentic_control_config(
        target_cfg, config_paths["practice_verifier"]
    )
    initial_memory_path = None
    raw_memory_path = Path(args.initial_memory).expanduser()
    initial_memory_path = (
        raw_memory_path
        if raw_memory_path.is_absolute()
        else RSIAGENT_ROOT / raw_memory_path
    )
    initial_memory: dict[str, bytes] = {}
    if initial_memory_path is not None:
        if initial_memory_path.is_symlink():
            raise RuntimeError("--initial-memory may not be a symlink")
        initial_memory_path = initial_memory_path.resolve(strict=True)
        if not initial_memory_path.is_dir():
            raise RuntimeError("--initial-memory must be one directory")
        from explore.practice_loop import _read_memory_tree
        from explore.target_learning import _memory_tree_sha256

        initial_memory = _read_memory_tree(str(initial_memory_path))
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    from env.qemu_rollback import normalize_verifier_execution_mode

    verifier_execution_mode = normalize_verifier_execution_mode(
        target_cfg.verifier_execution_mode
    )
    prepare_checkpointable_docker_provider(verifier_execution_mode)
    preflight = {
        "status": "ready",
        "task": args.task_id,
        "result_root": str(root),
        "mode": "outcome_grounded_targeted_practice",
        "phase2_protocol": phase2_protocol_metadata(
            args.phase2_stop_policy, args.phase2_curriculum_memory_access
        ),
        "seed_label": args.seed,
        "target_actor_agent": target_cfg.model,
        "target_verifier_agent": target_cfg.verifier_model,
        "practice_actor_agent": configs["practice_actor"].model,
        "practice_verifier_agent": configs["practice_verifier"].model,
        "practice_verifier_config": str(config_paths["practice_verifier"].resolve()),
        "curriculum_agent": configs["curriculum"].model,
        "actor_eye": target_cfg.vision_model,
        "actor_eye_readers_per_look": target_cfg.look_ensemble,
        "memory": "inherited, mutable only within Phase 2",
        "initial_memory_files": len(initial_memory),
        "initial_memory_bytes": sum((len(data) for data in initial_memory.values())),
        "initial_memory_tree_sha256": _memory_tree_sha256(initial_memory),
        "verifier_execution_mode": verifier_execution_mode,
        "verifier_candidate_protection": "qemu_checkpoint_restore"
        if verifier_execution_mode == "rollback_mirror"
        else "read_only_pid_network_ipc_namespaces",
        "verifier_context": "persistent within one target attempt/resume",
        "target_attempt_verifier_context": "fresh after each evolution wave",
        "target_verifier_stages": ["candidate_verification"],
        "target_verifier_orientation_context": "disabled_clean_protocol",
        "escalation_verifier_orientation": "disabled_clean_protocol",
        "practice_verifier_orientation": False,
        "official_evaluator_inside_loop": False,
        "official_evaluator_calls_after_loop": 0,
        "benchmark_profile": benchmark_provenance["profile"],
        "task_release": benchmark_provenance["task_release"],
    }
    if args.preflight:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    expected_ack = PHASE2_EXECUTE_ACK
    if args.execute != expected_ack:
        raise RuntimeError(f"--execute must equal {expected_ack}")
    recovery = None
    if recovery_plan is not None:
        from explore.phase2_recovery import Phase2CurriculumRecovery

        recovery = Phase2CurriculumRecovery.admit(
            recovery_plan,
            root,
            configs,
            initial_memory,
            target_direction,
            args.phase2_stop_policy,
        )
    if recovery is None and root.exists() and any(root.iterdir()):
        raise RuntimeError(
            "self-evolving result root is nonempty; preserve it and use a new locked retry tag"
        )
    root.mkdir(parents=True, exist_ok=True)
    if recovery is None:
        _write_phase2_manifest(
            root,
            task_id=args.task_id,
            protocol_run=args.protocol_run,
            instruction=initial_instruction,
            target_direction=target_direction,
            initial_memory_path=initial_memory_path,
            initial_memory=initial_memory,
            configs=configs,
            paths=config_paths,
            benchmark_provenance=benchmark_provenance,
            stop_policy=args.phase2_stop_policy,
            curriculum_memory_access=args.phase2_curriculum_memory_access,
        )
    events_path = root / "events.jsonl"
    from desktop_env.desktop_env import DesktopEnv

    from benchmarks.osworld.actor_boundary import quarantine_actor_visible_evaluators
    from core.actor import build_system
    from core.loop import (
        _snapshot,
        _verifier_session_identity,
        escalation_cfg,
        run_with_resume,
    )
    from core.self_evolving_loop import (
        ActorLearning,
        EvolutionResult,
        EvolutionStatus,
        SelfEvolvingLoopHooks,
        SelfEvolvingStart,
        TargetVerdict,
        TargetVerification,
        run_self_evolving_loop,
    )
    from core.task_baseline import capture_task_start_baseline
    from core.trace import ArtifactSink
    from core.verifier import VerifierSession, verify_agentic
    from env.vm import VM
    from explore.charter import memory_preamble
    from explore.commit import push_memory
    from explore.practice_loop import (
        PracticeHooks,
        _atomic_install_memory,
        _manifest,
        _read_memory_tree,
    )
    from explore.target_learning import _memory_tree_sha256, _promote_learning
    from explore.unified_evolution import UnifiedCurriculumSession, evolve_until_ready
    from llm.client import provider_counts, reset_provider_counts

    active_memory_dir = root / "active_memory"
    memory_journal_dir = root / "memory_journal"
    if recovery is None:
        _atomic_install_memory(str(active_memory_dir), initial_memory)
    curriculum_session = UnifiedCurriculumSession()
    counters = {"target_learning": 0, "target_orientations": 0}
    if recovery is not None:
        curriculum_session.history = recovery.history
        curriculum_session.notes = recovery.notes
        curriculum_session.turns = recovery.completed_curriculum_turns
        counters["target_learning"] = 1
        if recovery.ready_target_cycles:
            curriculum_session.waves = 1
            curriculum_session.project_summaries = [
                f"Phase-2 target outcome {recovery.trigger['verifier_outcome']}: the same Actor Agent committed grounded memory before Curriculum selected the next experience",
                *[
                    f"practice project {i}: " + record["terminal_outcome"]
                    for (i, record) in enumerate(recovery.project_records, 1)
                ],
            ]

    def emit(event: str, payload: dict[str, Any]) -> None:
        _append_event(events_path, event, payload)

    def fresh_target_environment(
        direction: str, target_cycle: int
    ) -> _TargetEnvironment:
        (task, instruction) = _load_task(args.task_id)
        observed_direction = _stable_target_direction(task, instruction)
        if direction != target_direction or observed_direction != target_direction:
            raise RuntimeError("stable target direction drifted across target resets")
        desktop = DesktopEnv(
            provider_name="docker",
            action_space="pyautogui",
            os_type="Ubuntu",
            screen_size=(1920, 1080),
            headless=True,
            cache_dir=os.environ.get("RSIAGENT_OSWORLD_CACHE_DIR", "cache"),
            require_a11y_tree=False,
            volume_size=60,
        )
        started = time.time()
        try:
            desktop.reset(task_config=task)
            actual_instruction = str(desktop.instruction or instruction)
            if actual_instruction != instruction:
                raise RuntimeError("DesktopEnv target instruction drifted")
            vm = VM(desktop)
            quarantined_evaluators = []
            if target_cfg.actor_evaluator_isolation:
                quarantined_evaluators = quarantine_actor_visible_evaluators(vm)
                emit(
                    "ACTOR_VISIBLE_EVALUATORS_QUARANTINED",
                    {
                        "target_cycle": target_cycle,
                        "files": len(quarantined_evaluators),
                        "bytes": sum(
                            (record["bytes"] for record in quarantined_evaluators)
                        ),
                        "content_sha256s": [
                            record["sha256"] for record in quarantined_evaluators
                        ],
                        "before_task_start_baseline": True,
                        "before_verifier_orientation": bool(
                            target_cfg.verifier_stage_lifecycle
                        ),
                        "paths_disclosed_to_agents": False,
                    },
                )
            private_paths = tuple(
                (
                    path
                    for path in target_cfg.verifier_private_paths
                    if path not in actual_instruction
                )
            )
            baseline = _snapshot(vm, excluded_paths=private_paths)
            emit(
                "TARGET_ENVIRONMENT_READY",
                {
                    "target_cycle": target_cycle,
                    "instruction_sha256": hashlib.sha256(
                        actual_instruction.encode("utf-8")
                    ).hexdigest(),
                    "target_direction_sha256": hashlib.sha256(
                        direction.encode("utf-8")
                    ).hexdigest(),
                    "evaluator_object_visibility": "host_only",
                    "guest_evaluator_source_visibility": "quarantined_pre_s0"
                    if target_cfg.actor_evaluator_isolation
                    else "historical_unmodified",
                    "guest_evaluator_files_quarantined": len(quarantined_evaluators),
                    "evaluator_called": False,
                },
            )
            return _TargetEnvironment(
                desktop=desktop,
                vm=vm,
                instruction=actual_instruction,
                boot_secs=time.time() - started,
                surface_baseline=baseline,
                verifier_private_paths=private_paths,
            )
        except Exception:
            desktop.close()
            raise

    def fresh_target_verifier(
        environment: _TargetEnvironment, direction: str, target_cycle: int
    ) -> _TargetVerifier:
        if direction != target_direction:
            raise RuntimeError("target direction drifted before Verifier creation")
        return _TargetVerifier(cycle=target_cycle)

    def _verifier_identity_token(identity: tuple[str, str]) -> str:
        encoded = json.dumps(
            list(identity), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def _orientation_configs(environment: _TargetEnvironment) -> tuple[Any, ...]:
        primary = dataclasses.replace(
            target_cfg, verifier_private_paths=environment.verifier_private_paths
        )
        configs_to_orient = [primary]
        if (
            primary.max_resumes > 0
            and primary.escalation_model
            and (primary.escalation_model != primary.model)
        ):
            alternate = escalation_cfg(primary)
            if _verifier_session_identity(alternate) != _verifier_session_identity(
                primary
            ):
                configs_to_orient.append(alternate)
        return tuple(configs_to_orient)

    def _orientation_event_callback(target_cycle: int, identity: tuple[str, str]):
        def callback(event: str, payload: dict[str, Any]) -> None:
            emit(
                "TARGET_VERIFIER_" + event,
                {
                    "target_cycle": target_cycle,
                    "verifier_identity": list(identity),
                    **payload,
                },
            )

        return callback

    def verifier_orient(
        verifier: _TargetVerifier,
        environment: _TargetEnvironment,
        direction: str,
        target_cycle: int,
    ) -> None:
        if verifier.cycle != target_cycle or direction != target_direction:
            raise RuntimeError("target Verifier orientation inputs drifted")
        baseline = capture_task_start_baseline(environment.vm)
        identity_record = {
            "schema_version": 1,
            "task": args.task_id,
            "target_cycle": target_cycle,
            "instruction_sha256": hashlib.sha256(
                environment.instruction.encode("utf-8")
            ).hexdigest(),
            "target_direction_sha256": hashlib.sha256(
                direction.encode("utf-8")
            ).hexdigest(),
            "task_class_sha256": benchmark_provenance["task_class_sha256"],
            "runtime_checkout": benchmark_provenance["runtime_checkout_describe"],
            "persistent_content_sha256": baseline["content_sha256"],
        }
        encoded_identity = json.dumps(
            identity_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        verifier.task_start_identity_sha256 = hashlib.sha256(
            encoded_identity
        ).hexdigest()
        baseline_dir = root / "task_start_baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = baseline_dir / f"cycle_{target_cycle:03d}.json"
        _write_json_atomic(
            baseline_path,
            {
                **baseline,
                "task_start_identity": {
                    **identity_record,
                    "sha256": verifier.task_start_identity_sha256,
                },
            },
        )
        emit(
            "TRUSTED_TASK_START_BASELINE_CAPTURED",
            {
                "target_cycle": target_cycle,
                "manifest": str(baseline_path),
                "entry_count": baseline["entry_count"],
                "content_sha256": baseline["content_sha256"],
                "exact_sha256": baseline["exact_sha256"],
                "task_start_identity_sha256": verifier.task_start_identity_sha256,
                "external_service_state": baseline["external_service_state"],
                "actor_has_acted": False,
                "actor_memory_attached": False,
            },
        )
        try:
            for position, active_cfg in enumerate(
                _orientation_configs(environment), start=1
            ):
                identity = _verifier_session_identity(active_cfg)
                if identity in verifier.sessions:
                    raise RuntimeError(
                        "duplicate target Verifier identity during orientation"
                    )
                session = VerifierSession(
                    on_event=_orientation_event_callback(target_cycle, identity)
                )
                verifier.sessions[identity] = session
                verifier.configs[identity] = active_cfg
                role = "primary" if position == 1 else "potential_escalation"
                context = (
                    "The host archived a complete trusted task-start manifest for audit. It is not mounted into your environment: inspect the exact live, pre-Actor S0 available to you.\nTask-start identity digest: "
                    + verifier.task_start_identity_sha256
                    + "\nPersistent-file entries: "
                    + str(baseline["entry_count"])
                    + "\nPersistent-content digest: "
                    + baseline["content_sha256"]
                    + "\nExternal-service snapshot: "
                    + baseline["external_service_state"]
                    + "\nNo Actor has acted and no Actor memory is attached."
                    + (
                        "\nThis context is being prepared prospectively for the fully asymmetric verifier role that is activated only if the primary Actor later stalls."
                        if role == "potential_escalation"
                        else ""
                    )
                )
                counters["target_orientations"] += 1
                (verdict, findings) = verify_agentic(
                    environment.instruction,
                    environment.vm,
                    active_cfg,
                    sink=ArtifactSink(
                        str(
                            root
                            / "target_verifier_lifecycle"
                            / f"cycle_{target_cycle:03d}"
                            / _verifier_identity_token(identity)
                        )
                    ),
                    turn_no=-counters["target_orientations"],
                    context=context,
                    session=session,
                    wall_budget=active_cfg.wall_clock_secs,
                    stage="orientation",
                )
                if verdict != "orientation_ready":
                    raise RuntimeError(
                        "target Verifier Agent did not complete orientation: "
                        + str(findings)
                    )
                report = str(findings)
                report_sha256 = hashlib.sha256(
                    report.encode("utf-8", "replace")
                ).hexdigest()
                session.detach_executor(environment.vm, preserve=True)
                verifier.orientation_report_sha256s[identity] = report_sha256
                emit(
                    "VERIFIER_ORIENTATION_COMPLETED",
                    {
                        "target_cycle": target_cycle,
                        "role": role,
                        "verifier_identity": list(identity),
                        "verifier_model": active_cfg.verifier_model,
                        "task_start_identity_sha256": verifier.task_start_identity_sha256,
                        "orientation_report_sha256": report_sha256,
                        "same_context_reserved_for_candidate": True,
                    },
                )
        except Exception:
            for session in verifier.sessions.values():
                session.close_executor()
            raise

    def fresh_target_actor(memory: dict[str, bytes], target_cycle: int) -> _TargetActor:
        return _TargetActor(memory=dict(memory), cycle=target_cycle)

    def actor_work(
        actor: _TargetActor,
        verifier: _TargetVerifier,
        environment: _TargetEnvironment,
        direction: str,
    ) -> _TargetOutput:
        if direction != target_direction:
            raise RuntimeError("target direction drifted before Actor work")
        if verifier.cycle != actor.cycle:
            raise RuntimeError("target Actor/Verifier cycle identity drifted")
        _atomic_install_memory(str(active_memory_dir), actor.memory)
        if not push_memory(environment.vm, str(active_memory_dir)):
            raise RuntimeError("could not attach task-local memory to target Actor")
        listing = "\n".join(
            (
                f"  {len(data):>7}  {name}"
                for (name, data) in sorted(actor.memory.items())
            )
        )
        cycle_cfg = dataclasses.replace(
            target_cfg,
            env_memory_dir=str(active_memory_dir) if actor.memory else "",
            verifier_private_paths=tuple(environment.verifier_private_paths),
        )
        emit(
            "TARGET_MEMORY_ATTACHED",
            {
                "target_cycle": actor.cycle,
                "state": "populated" if actor.memory else "blank",
                "files": len(actor.memory),
                "bytes": sum((len(data) for data in actor.memory.values())),
                "memory_tree_sha256": _memory_tree_sha256(actor.memory),
                "retrieval": "Actor Agent decides from complete filename/size inventory",
            },
        )
        sink_root = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
        sink = ArtifactSink(str(sink_root))
        runtime_state: dict[str, Any] = {}

        def ensure_verifier_orientation(active_cfg, session: VerifierSession) -> None:
            identity = _verifier_session_identity(active_cfg)
            if (
                verifier.sessions.get(identity) is not session
                or identity not in verifier.orientation_report_sha256s
            ):
                raise RuntimeError(
                    "candidate verification activated a Verifier identity that did not orient on this attempt's pre-Actor task state"
                )
            session.on_event = _orientation_event_callback(actor.cycle, identity)

        def attach_verifier_event_stream(active_cfg, session: VerifierSession) -> None:
            identity = _verifier_session_identity(active_cfg)
            if verifier.sessions.get(identity) is not session:
                raise RuntimeError(
                    "candidate verification Verifier session registry drifted"
                )
            session.on_event = _orientation_event_callback(actor.cycle, identity)

        try:
            (result, history) = run_with_resume(
                environment.instruction,
                environment.vm,
                cycle_cfg,
                sink,
                opening_extra=memory_preamble(listing),
                verifier_sessions=verifier.sessions,
                runtime_state=runtime_state,
                surface_baseline=environment.surface_baseline,
                verifier_session_prepare=ensure_verifier_orientation
                if target_cfg.verifier_stage_lifecycle
                else attach_verifier_event_stream,
                ask_user=environment.desktop.user_simulator.respond
                if environment.desktop.user_simulator is not None
                else None,
            )
        finally:
            for session in verifier.sessions.values():
                session.close_executor()
        active_identity = runtime_state.get("active_verifier_identity")
        if (
            target_cfg.verifier_stage_lifecycle
            and active_identity not in verifier.orientation_report_sha256s
        ):
            raise RuntimeError(
                "target result has no candidate-blind orientation provenance"
            )
        if active_identity not in verifier.sessions:
            raise RuntimeError("target result has no persistent Verifier session")
        sink.save_transcript(
            build_system(
                cycle_cfg, ask_enabled=environment.desktop.user_simulator is not None
            ),
            history,
        )
        return _TargetOutput(
            loop_result=result,
            history=history,
            active_history=list(runtime_state.get("active_history") or history),
            active_cfg=runtime_state.get("active_cfg") or cycle_cfg,
            sink_root=str(sink_root),
            active_verifier_identity=active_identity,
            orientation_report_sha256=verifier.orientation_report_sha256s[
                active_identity
            ]
            if target_cfg.verifier_stage_lifecycle
            else "",
        )

    def verifier_verify(
        verifier: _TargetVerifier,
        _environment: _TargetEnvironment,
        _direction: str,
        output: _TargetOutput,
    ) -> TargetVerification:
        if (
            target_cfg.verifier_stage_lifecycle
            and output.active_verifier_identity
            not in verifier.orientation_report_sha256s
        ):
            raise RuntimeError(
                "candidate verdict is detached from its Verifier orientation"
            )
        result = output.loop_result
        route = str(getattr(result, "verifier_route", "") or "")
        report = str(getattr(result, "verifier_report", "") or "")
        if result.status == "done" and route == "HANDOFF" and report.strip():
            return TargetVerification(TargetVerdict.PASS, report)
        if result.status == "evolve" and route == "EVOLVE" and report.strip():
            return TargetVerification(TargetVerdict.FAIL, report)
        reason = f"Target harness ended without a committed Verifier PASS/FAIL; status={result.status!r}, verifier_transport={route!r}. This is not converted into correctness or an evolution trigger."
        return TargetVerification(TargetVerdict.UNVERIFIED, report or reason)

    def actor_learn(
        actor: _TargetActor,
        environment: _TargetEnvironment,
        direction: str,
        output: _TargetOutput,
        verdict: TargetVerdict,
        report: str,
        memory: dict[str, bytes],
    ) -> ActorLearning:
        if direction != target_direction or actor.memory != memory:
            raise RuntimeError("target Actor learning inputs drifted")
        if verdict not in {TargetVerdict.PASS, TargetVerdict.FAIL}:
            raise RuntimeError("only a grounded PASS/FAIL may enter durable memory")
        if not output.active_history:
            raise RuntimeError("target Actor has no active context to learn from")
        _atomic_install_memory(str(active_memory_dir), memory)
        if not push_memory(environment.vm, str(active_memory_dir)):
            raise RuntimeError("could not restore canonical memory before learning")
        counters["target_learning"] += 1
        episode_dir = Path(output.sink_root) / "terminal_learning"
        episode_dir.mkdir(parents=True, exist_ok=True)

        def learning_emit(
            event_type: str,
            *,
            status: str,
            project_open: bool,
            memory_phase_open: bool,
            payload: dict[str, Any],
        ):
            emit(
                "TARGET_" + event_type,
                {
                    "status": status,
                    "project_open": project_open,
                    "memory_phase_open": memory_phase_open,
                    **payload,
                },
            )

        (learned, diagnosis, learned_history) = _promote_learning(
            hooks=PracticeHooks(),
            vm=environment.vm,
            cfg=output.active_cfg,
            lineage=root,
            episode_dir=episode_dir,
            experience_index=counters["target_learning"],
            before_memory=memory,
            actor_history=output.active_history,
            terminal_outcome=verdict.value,
            verifier_report=report,
            target=environment.instruction,
            audit_mode="exam",
            emit=learning_emit,
            project_open=False,
            corpus_path=str(corpus),
            memory_dir=active_memory_dir,
            journal_dir=memory_journal_dir,
            experience_kind="phase2-exact-target-" + verdict.value.lower(),
            target_visible_inputs=(),
        )
        _write_json_atomic(
            episode_dir / "outcome.json",
            {
                "schema_version": 1,
                "kind": "full_benchmark_target_learning",
                "target_cycle": actor.cycle,
                "target_verifier_verdict": verdict.value,
                "verifier_report": report,
                "actor_learning_diagnosis": diagnosis,
                "memory_before": _manifest(memory),
                "memory_after": _manifest(learned),
                "active_actor_model": output.active_cfg.model,
                "active_history_messages_before": len(output.active_history),
                "active_history_messages_after": len(learned_history),
            },
        )
        return ActorLearning(memory=learned, diagnosis=diagnosis)

    def evolve(
        direction: str,
        verdict: TargetVerdict,
        report: str,
        diagnosis: str,
        memory: dict[str, bytes],
        evolution_index: int,
    ) -> EvolutionResult:
        from benchmarks.osworld.setup import _boot_vm, _close, _reset_null_vm_sealed
        from explore.target_isolation import TargetIsolationSeal

        cycle_root = root / "evolution_cycles" / f"cycle_{evolution_index:03d}"
        (practice_environment, practice_vm) = _boot_vm()
        egress_seal = TargetIsolationSeal()
        try:
            practice_hooks = PracticeHooks(
                reset_vm=lambda active_vm, target: _reset_null_vm_sealed(
                    active_vm, target, egress_seal
                )
            )
            current_recovery = (
                recovery
                if evolution_index == 1
                and (not getattr(recovery, "post_target_learning", False))
                else None
            )
            if current_recovery is not None:
                practice_hooks = dataclasses.replace(
                    practice_hooks,
                    run_attempt=current_recovery.wrap_attempt(
                        practice_hooks.run_attempt
                    ),
                )
            result = evolve_until_ready(
                practice_vm,
                str(cycle_root),
                direction,
                report,
                diagnosis,
                memory,
                configs["practice_actor"],
                configs["practice_verifier"],
                configs["curriculum"],
                configs["memory_actor"],
                corpus_path=str(corpus),
                trigger_authority="phase2_outcome_protocol",
                triggering_outcome=verdict.value,
                curriculum_memory_access=args.phase2_curriculum_memory_access,
                agentic_verifier_cfg=practice_agentic_control_cfg,
                target_visible_inputs=(),
                hooks=practice_hooks,
                session=curriculum_session,
                event_sink=lambda event_type, **kwargs: emit(
                    "EVOLUTION_" + event_type, kwargs
                ),
                **{"resume_boundary": current_recovery}
                if current_recovery is not None
                else {},
            )
        finally:
            _close(practice_environment)
            egress_seal.close()
        learned = _read_memory_tree(result.memory_dir)
        _atomic_install_memory(str(active_memory_dir), learned)
        if result.status == "ready_for_retry":
            status = EvolutionStatus.READY_FOR_RETRY
        elif result.status == "stalled":
            status = EvolutionStatus.STALLED
        else:
            raise RuntimeError(
                f"self-evolution ended at non-semantic infrastructure/boundary status {result.status!r}: {result.reason}"
            )
        return EvolutionResult(
            status=status,
            memory=learned,
            projects=result.projects,
            reason=result.reason,
        )

    def release_target_environment(environment: _TargetEnvironment) -> None:
        _release_target_vm(environment, emit)

    loop_hooks = SelfEvolvingLoopHooks(
        fresh_target_environment=fresh_target_environment,
        fresh_target_verifier=fresh_target_verifier,
        fresh_target_actor=fresh_target_actor,
        actor_work=actor_work,
        verifier_verify=verifier_verify,
        actor_learn=actor_learn,
        evolve=evolve,
        verifier_orient=verifier_orient
        if target_cfg.verifier_stage_lifecycle
        else None,
        learn_on_pass=True,
        curriculum_after_pass=args.phase2_stop_policy == "curriculum_review",
        release_target_environment=release_target_environment,
        on_event=emit,
    )
    reset_provider_counts()
    if recovery is not None and getattr(recovery, "post_target_learning", False):
        from explore.post_target_learning_recovery import continue_after_target_learning

        emit(
            "COMMITTED_TARGET_LEARNING_RECOVERY_STARTED",
            {
                "target_cycle": 1,
                "recovery_plan": str(recovery.plan_path),
                "completed_target_learning_replayed": False,
                "curriculum_calls_before_recovery": 0,
            },
        )
        loop_result = continue_after_target_learning(
            recovery, target_direction, loop_hooks
        )
    elif recovery is not None and recovery.ready_target_cycles:
        learned = _read_memory_tree(str(active_memory_dir))
        start = SelfEvolvingStart(
            target_cycles=recovery.ready_target_cycles,
            evolutions=1,
            practice_projects=recovery.projects,
            final_after_stall=False,
        )
        emit(
            "READY_BOUNDARY_TARGET_REPLACEMENT",
            {
                "recovery_plan": str(recovery.plan_path),
                "completed_practices": recovery.projects,
                "previous_target_cycle": recovery.ready_target_cycles,
                "fresh_actor": True,
                "completed_work_replayed": False,
            },
        )
        loop_result = run_self_evolving_loop(
            target_direction, learned, loop_hooks, start=start
        )
    elif recovery is not None:
        learned = (
            recovery.initial_evolution_memory
            if recovery.initial_evolution_memory is not None
            else _read_memory_tree(str(active_memory_dir))
        )
        trigger = recovery.trigger
        evolved = evolve(
            target_direction,
            TargetVerdict(trigger["verifier_outcome"]),
            trigger["verifier_report"],
            trigger["actor_learning_diagnosis"],
            learned,
            1,
        )
        if evolved.projects < 1:
            raise RuntimeError("Recovered completed practice count was lost")
        start = SelfEvolvingStart(
            target_cycles=1,
            evolutions=1,
            practice_projects=evolved.projects,
            final_after_stall=evolved.status is EvolutionStatus.STALLED,
        )
        emit(
            "RECOVERED_EVOLUTION_COMPLETED",
            {
                "target_cycle": 1,
                "evolution": 1,
                "projects": evolved.projects,
                "status": evolved.status.value,
                "recovery_plan": str(recovery.plan_path),
            },
        )
        loop_result = run_self_evolving_loop(
            target_direction, evolved.memory, loop_hooks, start=start
        )
    else:
        loop_result = run_self_evolving_loop(
            target_direction, initial_memory, loop_hooks
        )
    final_environment = loop_result.environment
    try:
        final_memory = dict(loop_result.memory)
        frozen_memory_dir = root / "memory_frozen"
        _atomic_install_memory(str(frozen_memory_dir), final_memory)
        payload = {
            "schema_version": 1,
            "phase": "outcome_grounded_targeted_practice",
            "phase2_stop_policy": args.phase2_stop_policy,
            "phase2_curriculum_memory_access": args.phase2_curriculum_memory_access,
            "task": args.task_id,
            "seed": args.seed,
            "protocol_run": args.protocol_run,
            "model": target_cfg.model,
            "status": loop_result.termination,
            "target_verifier_verdict": loop_result.verifier_verdict.value,
            "target_cycles": loop_result.target_cycles,
            "evolutions": loop_result.evolutions,
            "practice_projects": loop_result.practice_projects,
            "target_learning_updates": counters["target_learning"],
            "initial_memory_tree_sha256": _memory_tree_sha256(initial_memory),
            "final_memory_files": len(final_memory),
            "final_memory_bytes": sum((len(data) for data in final_memory.values())),
            "final_memory_tree_sha256": _memory_tree_sha256(final_memory),
            "final_memory_manifest": _manifest(final_memory),
            "final_memory_path": str(frozen_memory_dir),
            "input_memory_writeback": False,
            "target_verifier_orientation": "disabled_clean_protocol",
            "official_evaluator_exposed_to_agents": False,
            "official_evaluator_calls": 0,
            "official_evaluator_feedback_entered_learning": False,
            "llm_providers": provider_counts(),
        }
        if recovery is not None:
            payload["infrastructure_recovery"] = str(recovery.plan_path)
            payload["llm_provider_counts_scope"] = "recovery_process_only"
        _write_json_atomic(root / "result.json", payload)
        print("\n=== PHASE 2 RESULT " + json.dumps(payload, sort_keys=True))
        return 2 if loop_result.termination == "target_harness_unverified" else 0
    finally:
        if final_environment is not None:
            release_target_environment(final_environment)
        elif not (
            recovery is not None
            and getattr(recovery, "post_target_learning", False)
            and (loop_result.practice_projects == 0)
        ):
            raise RuntimeError("Phase2 terminal target environment is missing")


if __name__ == "__main__":
    raise SystemExit(main())
