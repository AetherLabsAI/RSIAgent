"""Connect ALE environments to the existing evaluator-free RSIAgent RSI loops."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .practice import CleanPool, hooks_for
from .protocol import event, memory_record, role, verify_memory, write_json


def phase1(spec):
    from explore.phase1_wave import evolve_parallel_phase1
    from explore.practice_loop import _atomic_install_memory, _read_memory_tree

    root = Path(spec["work_dir"])
    pool = CleanPool(
        spec["practice_sandboxes"], practice=True, reset_endpoint=spec["reset_endpoint"]
    )
    try:
        hooks = hooks_for(pool, spec["corpus"], spec.get("audit_public_task"))
        result = evolve_parallel_phase1(
            root=str(root),
            target_direction=spec["instruction"],
            actor_cfg=role("actor"),
            curriculum_cfg=role("curriculum"),
            memory_cfg=role("memory"),
            verifier_control_cfg=role("target"),
            vm_factory=pool.acquire,
            corpus_path=spec["corpus"],
            hooks=hooks,
            event_sink=lambda kind, **payload: event(root, kind, payload),
            project_budget=8,
            checkpoint_projects=(0, 4, 8),
            max_parallel=4,
            target_query_conditioned=True,
            park_verified_branches=True,
        )
        _atomic_install_memory(
            str(root / "memory_frozen"), _read_memory_tree(result.memory_dir)
        )
        record = {
            "phase": "phase1",
            "status": result.status,
            "projects": result.projects,
            "reason": result.reason,
            "memory": memory_record(root / "memory_frozen"),
            "official_evaluator_calls": 0,
            "transition_ready": result.status in ("saturated", "budget_exhausted"),
        }
        write_json(root / "phase-result.json", record)
        return record
    finally:
        pool.close()


def phase2(spec):
    from core import actor as actor_module
    from core.loop import run_with_resume
    from core.self_evolving_loop import (
        ActorLearning,
        EvolutionResult,
        EvolutionStatus,
        SelfEvolvingLoopHooks,
        TargetVerdict,
        TargetVerification,
        run_self_evolving_loop,
    )
    from core.trace import ArtifactSink
    from explore.charter import memory_preamble
    from explore.practice_loop import (
        _atomic_install_memory,
        _listing,
        _read_memory_tree,
    )
    from explore.target_learning import _promote_learning
    from explore.unified_evolution import UnifiedCurriculumSession, evolve_until_ready

    root = Path(spec["work_dir"])
    verify_memory(spec["input_memory"])
    initial = _read_memory_tree(spec["input_memory"]["path"])
    target_pool = CleanPool(
        [spec["sandbox"]], practice=False, reset_endpoint=spec["reset_endpoint"]
    )
    practice_pool = None
    try:
        practice_pool = CleanPool(
            spec["practice_sandboxes"],
            practice=True,
            reset_endpoint=spec["reset_endpoint"],
        )
        hooks = hooks_for(practice_pool, spec["corpus"], spec.get("audit_public_task"))
        curriculum = UnifiedCurriculumSession()
        active_memory = root / "memory"
        learning_updates = 0

        def fresh_environment(direction, cycle):
            lease, vm = target_pool.acquire()
            vm.visible_roots = tuple(spec.get("visible_roots", ()))
            return SimpleNamespace(lease=lease, vm=vm, cycle=cycle)

        def fresh_verifier(environment, direction, cycle):
            return {}

        def fresh_actor(memory, cycle):
            return SimpleNamespace(memory=dict(memory), cycle=cycle)

        def work(actor, sessions, environment, direction):
            _atomic_install_memory(str(active_memory), actor.memory)
            if not hooks.push_memory(environment.vm, str(active_memory)):
                raise RuntimeError("Target memory upload failed")
            cfg = role("target", str(active_memory) if actor.memory else "")
            sink_root = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
            sink = ArtifactSink(str(sink_root))
            runtime = {}
            try:
                result, history = run_with_resume(
                    direction,
                    environment.vm,
                    cfg,
                    sink,
                    opening_extra=memory_preamble(_listing(actor.memory)),
                    verifier_sessions=sessions,
                    runtime_state=runtime,
                )
            finally:
                for session in sessions.values():
                    session.close_executor()
            sink.save_transcript(actor_module.build_system(cfg), history)
            return SimpleNamespace(
                result=result,
                cfg=runtime.get("active_cfg") or cfg,
                history=runtime.get("active_history") or history,
                root=sink_root,
            )

        def verify(sessions, environment, direction, output):
            result = output.result
            report = str(getattr(result, "verifier_report", "") or "")
            route = str(getattr(result, "verifier_route", "") or "")
            if result.status == "done" and route == "HANDOFF" and report.strip():
                return TargetVerification(TargetVerdict.PASS, report)
            if result.status == "evolve" and route == "EVOLVE" and report.strip():
                return TargetVerification(TargetVerdict.FAIL, report)
            return TargetVerification(
                TargetVerdict.UNVERIFIED,
                report
                or f"No committed verdict: status={result.status}, route={route}",
            )

        def learn(actor, environment, direction, output, verdict, report, memory):
            nonlocal learning_updates
            _atomic_install_memory(str(active_memory), memory)
            if not hooks.push_memory(environment.vm, str(active_memory)):
                raise RuntimeError("Canonical memory restore failed")
            learning_updates += 1
            episode = output.root / "terminal_learning"
            episode.mkdir(parents=True, exist_ok=True)
            learned, diagnosis, history = _promote_learning(
                hooks=hooks,
                vm=environment.vm,
                cfg=output.cfg,
                lineage=root,
                episode_dir=episode,
                experience_index=learning_updates,
                before_memory=memory,
                actor_history=output.history,
                terminal_outcome=verdict.value,
                verifier_report=report,
                target=direction,
                audit_mode="exam",
                emit=lambda kind, **payload: event(root, kind, payload),
                project_open=False,
                corpus_path=spec["corpus"],
                memory_dir=active_memory,
                journal_dir=root / "memory_journal",
                experience_kind="ale-phase2-target-" + verdict.value.lower(),
                target_visible_inputs=(),
            )
            write_json(
                episode / "outcome.json",
                {
                    "verdict": verdict.value,
                    "diagnosis": diagnosis,
                    "memory": memory_record(active_memory),
                    "actor_model": output.cfg.model,
                    "same_actor_context": True,
                },
            )
            return ActorLearning(learned, diagnosis)

        def evolve(direction, verdict, report, diagnosis, memory, index):
            lease, vm = practice_pool.acquire()
            try:
                result = evolve_until_ready(
                    vm,
                    str(root / "evolution_cycles" / f"cycle_{index:03d}"),
                    direction,
                    report,
                    diagnosis,
                    memory,
                    role("actor"),
                    role("verifier"),
                    role("curriculum"),
                    role("memory"),
                    corpus_path=spec["corpus"],
                    trigger_authority="phase2_outcome_protocol",
                    triggering_outcome=verdict.value,
                    agentic_verifier_cfg=role("target"),
                    target_visible_inputs=(),
                    hooks=hooks,
                    session=curriculum,
                    event_sink=lambda kind, **payload: event(root, kind, payload),
                )
            finally:
                lease.close()
            if result.status not in ("ready_for_retry", "stalled"):
                raise RuntimeError(
                    "Practice stopped without semantic completion: "
                    + result.status
                    + ": "
                    + result.reason
                )
            return EvolutionResult(
                EvolutionStatus.READY_FOR_RETRY
                if result.status == "ready_for_retry"
                else EvolutionStatus.STALLED,
                _read_memory_tree(result.memory_dir),
                result.projects,
                result.reason,
            )

        loop_hooks = SelfEvolvingLoopHooks(
            fresh_target_environment=fresh_environment,
            fresh_target_verifier=fresh_verifier,
            fresh_target_actor=fresh_actor,
            actor_work=work,
            verifier_verify=verify,
            actor_learn=learn,
            evolve=evolve,
            learn_on_pass=True,
            curriculum_after_pass=True,
            release_target_environment=lambda environment: environment.lease.close(),
            on_event=lambda kind, payload: event(root, kind, payload),
        )
        result = run_self_evolving_loop(spec["instruction"], initial, loop_hooks)
        result.environment.lease.close()
        _atomic_install_memory(str(root / "memory_frozen"), result.memory)
        verify_memory(spec["input_memory"])
        record = {
            "phase": "phase2",
            "status": result.termination,
            "verdict": result.verifier_verdict.value,
            "target_cycles": result.target_cycles,
            "practice_projects": result.practice_projects,
            "learning_updates": learning_updates,
            "memory": memory_record(root / "memory_frozen"),
            "official_evaluator_calls": 0,
            "transition_ready": result.verifier_verdict != TargetVerdict.UNVERIFIED,
        }
        write_json(root / "phase-result.json", record)
        return record
    finally:
        if practice_pool is not None:
            practice_pool.close()
        target_pool.close()
