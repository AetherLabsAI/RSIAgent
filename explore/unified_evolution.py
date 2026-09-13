"""Curriculum routing and practice adapter for the on-demand unified loop.

Unlike :func:`explore.e15_v12_loop.e15_v12_evolve`, this adapter never runs the
north-star target and cannot declare it converged. The same persistent Curriculum
Agent first routes a target Verifier Agent's FAIL to REVISE or EVOLVE. After EVOLVE,
it runs only null-task practice projects and returns when it declares
``READY_FOR_RETRY``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from explore.charter import (
    self_evolving_curriculum_charter,
    unified_curriculum_pass_charter,
    unified_curriculum_route_charter,
)
from explore.e15_loop import (
    _UNIFIED_CURRICULUM_TOKEN,
    CURRICULUM_HANDOFF,
    CURRICULUM_NOTES,
    DEFAULT_CORPUS,
    PROJECT_ROOT,
    E15BoundaryError,
    E15Hooks,
    E15InfrastructureError,
    E15Result,
    _atomic_json,
    _atomic_text,
    _capture_owned_tree,
    _continuation_after_transport,
    _curriculum_runtime_contract,
    _fresh_vm,
    _manifest,
    _parse_unique_handoff,
    _push_canonical_memory,
    _read_memory_tree,
    _run_handoff_phase,
)
from explore.e15_v12_loop import (
    E15PublicationError,
    _memory_tree_sha256,
    _outcome_text,
    _promote_learning,
    _require_owned_project_shape,
    _run_practice_attempt,
    _target_audit_surface,
)


CURRICULUM_ROUTE_HANDOFF = "/home/user/curriculum_route.md"
_CURRICULUM_ROUTE_TOKEN = re.compile(r"^ROUTE: (REVISE|EVOLVE)$")
_CURRICULUM_PASS_TOKEN = re.compile(r"^ROUTE: (HANDOFF|VERIFY_MORE)$")
_PHASE2_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_TARGET|STALLED)$")


@dataclass
class UnifiedCurriculumSession:
    """One target-scoped Curriculum Agent paused across target retries."""

    history: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    project_summaries: list[str] = field(default_factory=list)
    route_summaries: list[str] = field(default_factory=list)
    turns: int = 0
    waves: int = 0
    target_failures: int = 0
    target_passes: int = 0


@dataclass(frozen=True)
class CurriculumRoutingDecision:
    """One high-level decision made after a target Verifier Agent FAIL."""

    route: str
    report: str

    def __post_init__(self) -> None:
        normalized = str(self.route).strip().upper()
        if normalized not in {"REVISE", "EVOLVE"}:
            raise E15InfrastructureError(
                f"unknown Curriculum Agent target route: {self.route!r}")
        if not isinstance(self.report, str) or not self.report.strip():
            raise E15InfrastructureError(
                "Curriculum Agent routing report is empty")
        object.__setattr__(self, "route", normalized)


@dataclass(frozen=True)
class CurriculumPassDecision:
    """One lifecycle decision made after a target Verifier Agent PASS."""

    route: str
    report: str

    def __post_init__(self) -> None:
        normalized = str(self.route).strip().upper()
        if normalized not in {"HANDOFF", "VERIFY_MORE"}:
            raise E15InfrastructureError(
                f"unknown Curriculum Agent PASS route: {self.route!r}")
        if not isinstance(self.report, str) or not self.report.strip():
            raise E15InfrastructureError(
                "Curriculum Agent PASS routing report is empty")
        object.__setattr__(self, "route", normalized)


def route_target_pass(
        vm, root: str, target: str, verifier_orientation: str,
        verifier_report: str, curriculum_cfg, *, hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        ) -> CurriculumPassDecision:
    """Let one persistent Curriculum Agent authorize HANDOFF or VERIFY_MORE.

    The candidate and target environment remain inaccessible.  This audits only
    whether the Verifier's own complete report is sufficient and coherent enough to
    end the outer search; it cannot replace or override the local PASS/FAIL role.
    """

    hooks = hooks or E15Hooks()
    session = session or UnifiedCurriculumSession()
    if not target.strip():
        raise E15InfrastructureError("immutable target query is empty")
    if not verifier_report.strip():
        raise E15InfrastructureError("target Verifier Agent PASS report is empty")
    if not any(
            line.strip().upper() == "VERDICT: PASS"
            for line in verifier_report.splitlines()):
        raise E15InfrastructureError(
            "Curriculum PASS routing requires a committed Verifier Agent PASS")

    audit_target = _target_audit_surface(target)
    for label, text in (
            ("orientation", verifier_orientation),
            ("PASS report", verifier_report)):
        if text and hooks.audit_text(
                text, mode="exam", authorized_instruction=audit_target):
            raise E15BoundaryError(
                f"target Verifier Agent {label} failed target boundary")

    route_root = Path(root)
    route_root.mkdir(parents=True, exist_ok=True)
    _fresh_vm(hooks, vm, target)
    session.target_passes += 1
    session.turns += 1
    prompt = unified_curriculum_pass_charter(
        target, verifier_orientation, verifier_report,
        route_history="\n".join(session.route_summaries),
        curriculum_notes=session.notes,
        handoff_path=CURRICULUM_ROUTE_HANDOFF,
        notes_path=CURRICULUM_NOTES)
    decision, history, _ = _run_handoff_phase(
        hooks=hooks, vm=vm, cfg=curriculum_cfg, prompt=prompt,
        role="CURRICULUM", handoff_path=CURRICULUM_ROUTE_HANDOFF,
        token_pattern=_CURRICULUM_PASS_TOKEN,
        sink_root=str(route_root / f"turn_{session.turns:03d}"),
        target=audit_target, history=session.history,
        continuation=bool(session.history),
        handoff_parser=_parse_unique_handoff,
        require_completed_publication=True, audit_mode="exam")
    session.history = history
    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
    if notes:
        if hooks.audit_text(
                notes, mode="exam", authorized_instruction=audit_target):
            raise E15BoundaryError("Curriculum notes failed target boundary")
        session.notes = notes
    session.route_summaries.append(
        f"target PASS review {session.target_passes}: {decision.token}")
    record = {
        "schema_version": 1,
        "kind": "curriculum_target_pass_route",
        "target_pass_review": session.target_passes,
        "route": decision.token,
        "verifier_orientation_sha256": hashlib.sha256(
            verifier_orientation.encode("utf-8")).hexdigest(),
        "verifier_report_sha256": hashlib.sha256(
            verifier_report.encode("utf-8")).hexdigest(),
        "curriculum_report": decision.text,
    }
    _atomic_json(
        route_root / f"pass_review_{session.target_passes:03d}.json", record)
    if event_sink is not None:
        event_sink(
            "TARGET_PASS_ROUTED", status="completed",
            state={"route": decision.token},
            payload={
                "target_pass_review": session.target_passes,
                "route": decision.token,
                "verifier_report_sha256": record["verifier_report_sha256"],
                "curriculum_report_sha256": hashlib.sha256(
                    decision.text.encode("utf-8")).hexdigest(),
            })
    return CurriculumPassDecision(decision.token, decision.text)


def route_target_failure(
        vm, root: str, target: str, verifier_report: str, curriculum_cfg,
        *, hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        ) -> CurriculumRoutingDecision:
    """Let one persistent Curriculum Agent route a concrete target FAIL.

    This phase has no target environment, candidate, Actor-private artifacts, or
    evaluator. It owns only the high-level REVISE/EVOLVE choice and preserves its
    semantic context for later failures and practice search.
    """

    hooks = hooks or E15Hooks()
    session = session or UnifiedCurriculumSession()
    if not target.strip():
        raise E15InfrastructureError("immutable target query is empty")
    if not verifier_report.strip():
        raise E15InfrastructureError("target Verifier Agent FAIL report is empty")
    if not any(
            line.strip().upper() == "VERDICT: FAIL"
            for line in verifier_report.splitlines()):
        raise E15InfrastructureError(
            "Curriculum routing requires a committed Verifier Agent FAIL")

    audit_target = _target_audit_surface(target)
    if hooks.audit_text(
            verifier_report, mode="exam",
            authorized_instruction=audit_target):
        raise E15BoundaryError(
            "target Verifier Agent FAIL report failed target boundary")

    route_root = Path(root)
    route_root.mkdir(parents=True, exist_ok=True)
    _fresh_vm(hooks, vm, target)
    session.target_failures += 1
    session.turns += 1
    prompt = unified_curriculum_route_charter(
        target, verifier_report,
        route_history="\n".join(session.route_summaries),
        curriculum_notes=session.notes,
        handoff_path=CURRICULUM_ROUTE_HANDOFF,
        notes_path=CURRICULUM_NOTES)
    decision, history, _ = _run_handoff_phase(
        hooks=hooks, vm=vm, cfg=curriculum_cfg, prompt=prompt,
        role="CURRICULUM", handoff_path=CURRICULUM_ROUTE_HANDOFF,
        token_pattern=_CURRICULUM_ROUTE_TOKEN,
        sink_root=str(route_root / f"turn_{session.turns:03d}"),
        target=audit_target, history=session.history,
        continuation=bool(session.history),
        handoff_parser=_parse_unique_handoff,
        require_completed_publication=True, audit_mode="exam")
    session.history = history
    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
    if notes:
        if hooks.audit_text(
                notes, mode="exam", authorized_instruction=audit_target):
            raise E15BoundaryError("Curriculum notes failed target boundary")
        session.notes = notes
    session.route_summaries.append(
        f"target failure {session.target_failures}: {decision.token}")
    record = {
        "schema_version": 1,
        "kind": "curriculum_target_failure_route",
        "target_failure": session.target_failures,
        "route": decision.token,
        "verifier_report_sha256": hashlib.sha256(
            verifier_report.encode("utf-8")).hexdigest(),
        "curriculum_report": decision.text,
    }
    _atomic_json(route_root / f"route_{session.target_failures:03d}.json", record)
    if event_sink is not None:
        event_sink(
            "TARGET_FAILURE_ROUTED", status="completed",
            state={"route": decision.token},
            payload={
                "target_failure": session.target_failures,
                "route": decision.token,
                "verifier_report_sha256": record["verifier_report_sha256"],
                "curriculum_report_sha256": hashlib.sha256(
                    decision.text.encode("utf-8")).hexdigest(),
            })
    return CurriculumRoutingDecision(decision.token, decision.text)


def evolve_until_ready(
        vm, root: str, target: str, triggering_report: str,
        triggering_diagnosis: str,
        initial_memory: dict[str, bytes], actor_cfg, verifier_cfg,
        curriculum_cfg, memory_cfg, *, corpus_path: str = DEFAULT_CORPUS,
        routing_report: str = "",
        trigger_authority: str = "curriculum_route",
        triggering_outcome: str = "FAIL",
        curriculum_memory_access: str = "read_only",
        agentic_verifier_cfg=None,
        target_visible_inputs: tuple[str, ...] | None = None,
        hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        resume_boundary=None) -> E15Result:
    """Let Curriculum choose experiences until it returns target control.

    ``phase2_outcome_protocol`` accepts either a grounded PASS or FAIL after the
    same Actor has updated memory.  The Curriculum Agent may inspect that memory
    and choose projects, but cannot edit or approve it. Other authorities retain
    the historical FAIL-only contract. A stalled, quarantined, or infrastructure
    result is not readiness and must not be converted into a target retry.
    """

    if curriculum_memory_access not in {"read_only", "none"}:
        raise ValueError("unknown Curriculum memory access mode")
    if (curriculum_memory_access == "none"
            and trigger_authority != "phase2_outcome_protocol"):
        raise ValueError("Curriculum memory ablation requires Phase 2")
    if (resume_boundary is not None and curriculum_memory_access !=
            getattr(resume_boundary, 'curriculum_memory_access', 'read_only')):
        raise ValueError("recovery cannot change Curriculum memory access")
    hooks = hooks or E15Hooks()
    session = session or UnifiedCurriculumSession()
    if resume_boundary is not None:
        resume_boundary.validate_evolution(
            root, target, initial_memory, triggering_report,
            triggering_diagnosis, trigger_authority, triggering_outcome)
    session.waves += 1
    lineage = Path(root)
    memory_dir = lineage / "memory"
    journal_dir = lineage / "memory_journal"
    episodes_dir = lineage / "episodes"
    curriculum_dir = lineage / "curriculum"
    trigger_dir = lineage / "trigger"
    state_path = lineage / "state.json"
    for path in (lineage, memory_dir, journal_dir, episodes_dir,
                 curriculum_dir, trigger_dir):
        path.mkdir(parents=True, exist_ok=True)

    project_index = resume_boundary.projects if resume_boundary else 0
    experience_index = project_index
    curriculum_history = session.history
    project_summaries = session.project_summaries
    normalized_outcome = str(triggering_outcome).strip().upper()
    if normalized_outcome not in {"PASS", "FAIL"}:
        raise E15InfrastructureError(
            f"unknown grounded target outcome: {triggering_outcome!r}")
    if trigger_authority == "curriculum_route":
        project_summaries.append(
            f"target retry wave {session.waves}: Curriculum Agent routed EVOLVE "
            "after persistent target Verifier Agent FAIL")
    elif trigger_authority == "verifier_fail_protocol":
        project_summaries.append(
            f"target retry wave {session.waves}: the self-evolving benchmark "
            "protocol entered learning after a target Verifier Agent FAIL")
    elif trigger_authority == "phase2_outcome_protocol":
        project_summaries.append(
            f"Phase-2 target outcome {normalized_outcome}: the same Actor Agent "
            "committed grounded memory before Curriculum selected the next "
            "experience")
    else:
        raise E15InfrastructureError(
            f"unknown evolution trigger authority: {trigger_authority!r}")
    if resume_boundary is not None:
        records = (resume_boundary.project_records or [resume_boundary.project]
                   if resume_boundary.projects else [])
        project_summaries.extend(
            f'practice project {i}: ' + record['terminal_outcome']
            for i, record in enumerate(records, 1))
    curriculum_notes = session.notes
    audit_target = _target_audit_surface(target, target_visible_inputs)

    def emit(event_type: str, *, status: str, project_open: bool,
             memory_phase_open: bool, payload: dict[str, Any]) -> Any:
        if event_sink is None:
            return None
        return event_sink(
            event_type, status=status,
            state={
                "project_open": project_open,
                "memory_phase_open": memory_phase_open,
                "recovery_open": False,
            },
            payload=payload)

    def finish(status: str, reason: str = "", terminal_text: str = "") \
            -> E15Result:
        memory = _read_memory_tree(str(memory_dir))
        _atomic_json(state_path, {
            "schema_version": 1,
            "kind": "unified_evolution_cycle",
            "status": status,
            "projects": project_index,
            "learning_experiences": experience_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "target_sha256": hashlib.sha256(
                target.encode("utf-8")).hexdigest(),
            "trigger_report_sha256": hashlib.sha256(
                triggering_report.encode("utf-8")).hexdigest(),
            "trigger_authority": trigger_authority,
            "triggering_outcome": normalized_outcome,
            "curriculum_route_sha256": hashlib.sha256(
                routing_report.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(memory),
            "memory_tree_sha256": _memory_tree_sha256(memory),
        })
        return E15Result(
            status=status,
            projects=project_index,
            root=str(lineage),
            memory_dir=str(memory_dir),
            reason=reason,
            terminal_text=terminal_text,
            last_project=project_index,
        )

    try:
        if not target.strip():
            raise E15InfrastructureError("immutable target query is empty")
        if not triggering_report.strip():
            raise E15InfrastructureError("EVOLVE trigger report is empty")
        if not triggering_diagnosis.strip():
            raise E15InfrastructureError(
                "target Actor learning diagnosis is empty")
        if not any(
                line.strip().upper() == f"VERDICT: {normalized_outcome}"
                for line in triggering_report.splitlines()):
            raise E15InfrastructureError(
                "learning trigger lacks the committed Verifier Agent "
                f"{normalized_outcome}")
        if (trigger_authority != "phase2_outcome_protocol"
                and normalized_outcome != "FAIL"):
            raise E15InfrastructureError(
                "historical evolution authorities require a concrete FAIL")
        if trigger_authority == "curriculum_route":
            if not any(
                    line.strip().upper() == "ROUTE: EVOLVE"
                    for line in routing_report.splitlines()):
                raise E15InfrastructureError(
                    "EVOLVE trigger lacks the Curriculum Agent route")
        elif routing_report.strip():
            raise E15InfrastructureError(
                "outcome protocol trigger must not manufacture a "
                "Curriculum Agent routing report")
        if _read_memory_tree(str(memory_dir)) and resume_boundary is None:
            raise E15InfrastructureError(
                "unified evolution cycle requires a new empty lineage root")

        hooks.validate_corpus(corpus_path)
        if hooks.audit_text(
                triggering_report, mode="exam",
                authorized_instruction=audit_target):
            raise E15BoundaryError("EVOLVE trigger report failed target boundary")
        if hooks.audit_text(
                triggering_diagnosis, mode="exam",
                authorized_instruction=audit_target):
            raise E15BoundaryError(
                "target Actor learning diagnosis failed target boundary")
        if routing_report and hooks.audit_text(
                routing_report, mode="exam",
                authorized_instruction=audit_target):
            raise E15BoundaryError(
                "Curriculum Agent routing report failed target boundary")
        accepted_memory = hooks.audit_memory(
            initial_memory, corpus_path,
            str(lineage / "audit_rejects.jsonl"),
            authorized_instruction=audit_target, require_corpus=True)
        if accepted_memory != initial_memory:
            raise E15BoundaryError("input memory failed target boundary")
        if resume_boundary is None:
            hooks.install_memory(str(memory_dir), accepted_memory)

        trigger_record = {
            "schema_version": 1,
            "kind": "target_verifier_learning_outcome",
            "target": target,
            "verifier_report": triggering_report,
            "verifier_outcome": normalized_outcome,
            "trigger_authority": trigger_authority,
            "curriculum_routing_report": routing_report or None,
            "actor_learning_diagnosis": triggering_diagnosis,
            "memory_before": _manifest(accepted_memory),
        }
        if resume_boundary is None:
            _atomic_json(trigger_dir / "outcome.json", trigger_record)
        route_context = (
            "\n\nCURRICULUM AGENT EVOLVE ROUTING HANDOFF (verbatim):\n"
            + routing_report
            if trigger_authority == "curriculum_route" else
            "\n\nLIFECYCLE TRANSITION:\nThe declared self-evolving benchmark "
            f"protocol entered learning after the target Verifier Agent "
            f"{normalized_outcome}. No Curriculum Agent routing decision was "
            "requested or fabricated.")
        latest_outcome = (
            "ORIGINAL NORTH-STAR TARGET (verbatim):\n" + target
            + "\n\nPERSISTENT TARGET VERIFIER AGENT REPORT (verbatim):\n"
            + triggering_report
            + route_context
            + "\n\nSAME TARGET ACTOR AGENT LEARNING DIAGNOSIS (verbatim):\n"
            + triggering_diagnosis)
        if resume_boundary is None:
            _atomic_text(trigger_dir / "outcome.md", latest_outcome)
        emit(
            ("EVOLVE_TRIGGER_IMPORTED" if resume_boundary is None
             else ("PRACTICE_MEMORY_BOUNDARY_RESUMED"
                   if getattr(resume_boundary, "pending_memory_learning", None) is not None
                   else "UNVERIFIED_PRACTICE_BOUNDARY_RESUMED"
                   if getattr(resume_boundary, "pending_practice", None) is not None
                   else "COMPLETED_PROJECT_BOUNDARY_RESUMED")), status="completed",
            project_open=getattr(resume_boundary, "pending_practice", None) is not None,
            memory_phase_open=getattr(resume_boundary, "pending_memory_learning", None) is not None,
            payload={
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
                "trigger_report_sha256": hashlib.sha256(
                    triggering_report.encode("utf-8")).hexdigest(),
                "trigger_diagnosis_sha256": hashlib.sha256(
                    triggering_diagnosis.encode("utf-8")).hexdigest(),
                "trigger_authority": trigger_authority,
                "triggering_outcome": normalized_outcome,
                "curriculum_route_sha256": hashlib.sha256(
                    routing_report.encode("utf-8")).hexdigest(),
                "memory_tree_sha256": _memory_tree_sha256(
                    _read_memory_tree(str(memory_dir))
                    if getattr(resume_boundary, "pending_practice", None) is not None
                    else accepted_memory),
            })

        pending_practice = getattr(resume_boundary, "pending_practice", None)
        recovering_turn = resume_boundary is not None and pending_practice is None
        if recovering_turn:
            latest_outcome = resume_boundary.latest_outcome
        while True:
            practice_resume = pending_practice
            if practice_resume is not None:
                pending_practice = None
                project_index += 1
                episode_dir = episodes_dir / f"ep{project_index:03d}"
                fixture_dir = episode_dir / "fixtures"
                project = practice_resume["project"]
                emit("PRACTICE_MEMORY_RESUMED" if practice_resume.get("resume_kind") == "verified_memory"
                     else "UNVERIFIED_PRACTICE_RESUMED", status="in_progress",
                     project_open=True, memory_phase_open=practice_resume.get("resume_kind") == "verified_memory",
                     payload={"project_index": project_index,
                              "recovery_plan": str(resume_boundary.plan_path)})
            else:
                # The target environment was already discarded by the unified state
                # machine. Curriculum and practice run only on a fresh null-task VM.
                _fresh_vm(hooks, vm, target)
                curriculum_memory_dir = memory_dir
                if curriculum_memory_access == "none":
                    # Use the ordinary full-replacement transport with an empty
                    # source, including after a practice Actor used this VM.
                    # Canonical memory and all Actor learning paths are unchanged.
                    curriculum_memory_dir = lineage / "curriculum_empty_memory"
                    curriculum_memory_dir.mkdir(exist_ok=True)
                    if any(curriculum_memory_dir.iterdir()):
                        raise E15InfrastructureError(
                            "Curriculum ablation attachment must remain empty")
                _push_canonical_memory(hooks, vm, str(curriculum_memory_dir))
                session.turns += 1
                curriculum_sink = curriculum_dir / f"turn_{session.turns:03d}"
                attached_memory = _read_memory_tree(str(curriculum_memory_dir))
                _atomic_json(curriculum_sink / "memory_access.json", {
                    "schema_version": 1,
                    "access": curriculum_memory_access,
                    "attached_files": len(attached_memory),
                    "attached_bytes": sum(len(v) for v in attached_memory.values()),
                    "attached_tree_sha256": _memory_tree_sha256(attached_memory),
                    "actor_learning_diagnosis_available": True,
                })
                curriculum_prompt = self_evolving_curriculum_charter(
                    target,
                    project_history="\n".join(project_summaries),
                    latest_outcome=latest_outcome,
                    curriculum_notes=curriculum_notes,
                    handoff_path=CURRICULUM_HANDOFF,
                    notes_path=CURRICULUM_NOTES,
                    unified_retry=(trigger_authority !=
                                   "phase2_outcome_protocol"),
                    phase2_outcome=(trigger_authority ==
                                    "phase2_outcome_protocol"),
                    curriculum_memory_access=curriculum_memory_access,
                ) + _curriculum_runtime_contract()
                turn_cfg = curriculum_cfg
                if recovering_turn:
                    from dataclasses import replace
                    turn_cfg = replace(
                        curriculum_cfg, max_iters=resume_boundary.remaining_iters,
                        wall_clock_secs=resume_boundary.remaining_wall)
                    curriculum_prompt += resume_boundary.recovery_observation

                while True:
                    decision, curriculum_history, _ = _run_handoff_phase(
                        hooks=hooks, vm=vm, cfg=turn_cfg,
                        prompt=curriculum_prompt, role="CURRICULUM",
                        handoff_path=CURRICULUM_HANDOFF,
                        token_pattern=(
                            _PHASE2_CURRICULUM_TOKEN
                            if trigger_authority == "phase2_outcome_protocol"
                            else _UNIFIED_CURRICULUM_TOKEN),
                        sink_root=str(curriculum_sink), target=audit_target,
                        history=curriculum_history,
                        continuation=bool(curriculum_history),
                        require_completed_publication=True)
                    session.history = curriculum_history
                    recovering_turn = False
                    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
                    if notes:
                        if hooks.audit_text(
                                notes, mode="practice",
                                authorized_instruction=audit_target):
                            raise E15BoundaryError(
                                "Curriculum notes failed target boundary")
                        curriculum_notes = notes
                        session.notes = notes

                    if decision.token == "STALLED":
                        _atomic_text(
                            lineage / "curriculum_terminal.md", decision.text)
                        return finish(
                            "stalled",
                            "Curriculum Agent reported no productive new hypothesis",
                            decision.text)

                    if decision.token in {"READY_FOR_RETRY", "READY_FOR_TARGET"}:
                        _atomic_text(
                            lineage / "curriculum_ready_for_retry.md", decision.text)
                        emit(
                            decision.token, status="completed",
                            project_open=False, memory_phase_open=False,
                            payload={
                                "projects": project_index,
                                "memory_tree_sha256": _memory_tree_sha256(
                                    _read_memory_tree(str(memory_dir))),
                                "curriculum_decision_sha256": hashlib.sha256(
                                    decision.text.encode("utf-8")).hexdigest(),
                            })
                        return finish(
                            "ready_for_retry",
                            "Curriculum Agent returned control to the target loop",
                            decision.text)

                    if (not decision.body or PROJECT_ROOT not in decision.body):
                        curriculum_prompt = _continuation_after_transport(
                            "CURRICULUM", CURRICULUM_HANDOFF,
                            f"a PROJECT must be nonempty and name {PROJECT_ROOT}")
                        continue

                    candidate_index = project_index + 1
                    episode_dir = episodes_dir / f"ep{candidate_index:03d}"
                    fixture_dir = episode_dir / "fixtures"
                    try:
                        _require_owned_project_shape(vm)
                        _capture_owned_tree(
                            hooks, vm, f"ep{candidate_index:03d}-fixtures",
                            str(fixture_dir), audit_target)
                    except E15PublicationError:
                        curriculum_prompt = _continuation_after_transport(
                            "CURRICULUM", CURRICULUM_HANDOFF,
                            f"{PROJECT_ROOT} was absent, empty, or not replayable")
                        continue

                    project_index = candidate_index
                    project = decision.body
                    _atomic_text(episode_dir / "project.md", decision.text)
                    emit(
                        "PROJECT_OPENED", status="in_progress",
                        project_open=True, memory_phase_open=False,
                        payload={
                            "project_index": project_index,
                            "project_sha256": hashlib.sha256(
                                project.encode("utf-8")).hexdigest(),
                        })
                    break

            practice = _run_practice_attempt(
                hooks=hooks, vm=vm, lineage=lineage,
                episode_dir=episode_dir, project_index=project_index,
                project=project, fixture_dir=fixture_dir, target=target,
                actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
                memory_dir=memory_dir, emit=emit,
                agentic_verifier_cfg=agentic_verifier_cfg,
                target_visible_inputs=target_visible_inputs,
                **({"resume_state": practice_resume} if practice_resume is not None else {}))
            experience_index += 1
            learned, diagnosis, _ = _promote_learning(
                hooks=hooks, vm=vm, cfg=memory_cfg, lineage=lineage,
                episode_dir=episode_dir, experience_index=experience_index,
                before_memory=practice["before_memory"],
                actor_history=practice["actor_history"],
                terminal_outcome=practice["terminal_outcome"],
                verifier_report=practice["verifier_report"], target=target,
                audit_mode="practice", emit=emit, project_open=True,
                corpus_path=corpus_path, memory_dir=memory_dir,
                journal_dir=journal_dir,
                experience_kind="unified-practice-project",
                target_visible_inputs=target_visible_inputs)
            record = {
                "schema_version": 1,
                "kind": "unified_practice_project",
                "project_index": project_index,
                "request": project,
                "terminal_outcome": practice["terminal_outcome"],
                "actor_handoff": practice["actor_handoff"],
                "verifier_report": practice["verifier_report"],
                "learning_diagnosis": diagnosis,
                "memory_before": _manifest(practice["before_memory"]),
                "memory_after": _manifest(learned),
            }
            _atomic_json(episode_dir / "outcome.json", record)
            latest_outcome = _outcome_text({
                "label": f"PRACTICE PROJECT {project_index}",
                **record,
            })
            _atomic_text(episode_dir / "outcome.md", latest_outcome)
            project_summaries.append(
                f"practice project {project_index}: "
                + practice["terminal_outcome"])
            _atomic_json(state_path, {
                "schema_version": 1,
                "kind": "unified_evolution_cycle",
                "status": "running",
                "projects": project_index,
                "learning_experiences": experience_index,
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(learned),
                "memory_tree_sha256": _memory_tree_sha256(learned),
            })
            emit(
                "PROJECT_CLOSED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": practice["terminal_outcome"],
                    "memory_tree_sha256": _memory_tree_sha256(learned),
                    "learning_diagnosis_sha256": hashlib.sha256(
                        diagnosis.encode("utf-8")).hexdigest(),
                })

    except E15BoundaryError as exc:
        return finish("quarantined", str(exc))
    except E15InfrastructureError as exc:
        return finish("infra", str(exc))
    except Exception as exc:  # noqa: BLE001 - durable inspectable incident
        return finish("infra", f"{type(exc).__name__}: {exc}")


__all__ = [
    "CurriculumRoutingDecision",
    "UnifiedCurriculumSession",
    "evolve_until_ready",
    "route_target_failure",
]
