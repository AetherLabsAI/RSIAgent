"""Agent-authored parallel waves for Phase-1 environment exploration.

The Curriculum Agent chooses each wave's width and projects.  Every branch reads
the same immutable pre-wave memory snapshot and runs in an isolated fresh VM.
Only after every branch has a terminal Verifier verdict are the *same* branch
Actor contexts resumed, one at a time, to reconcile their experience into the
single flat canonical memory bank.  The serialized commit boundary prevents
sibling observation and lost-update races without imposing a memory schema.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any, Callable

from core.actor import PLAIN_JSON_TRANSPORT_NOTE
from core.trace import ArtifactSink
from explore import commit as memory_transport
from explore.charter import (
    phase1_wave_curriculum_charter,
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
)
from explore.e15_loop import (
    ACTOR_EXECUTION_EVIDENCE,
    ACTOR_HANDOFF,
    PROJECT_ROOT,
    VERIFIER_REPORT,
    E15BoundaryError,
    E15Hooks,
    E15InfrastructureError,
    E15Result,
    ParsedHandoff,
    _ACTOR_TOKEN,
    _actor_runtime_contract,
    _atomic_install_memory,
    _atomic_json,
    _atomic_text,
    _audit_agent_artifacts,
    _audit_prompt,
    _build_actor_execution_evidence,
    _capture_owned_tree,
    _continuation_after_transport,
    _fresh_vm,
    _listing,
    _manifest,
    _outcome_text,
    _phase_cfg,
    _pull_terminal_memory,
    _push_actor_execution_evidence,
    _push_canonical_memory,
    _read_memory_tree,
    _remove_guest_file,
    _replay,
    _run_free_phase,
    _run_handoff_phase,
    _scope_prior_curriculum_visuals,
    _verify_actor_execution_evidence,
    _verify_candidate,
)


WAVE_ROOT = "/home/user/evolution_wave"
WAVE_HANDOFF = "/home/user/phase1_wave.json"
CURRICULUM_EVIDENCE = "/home/user/.phase1_wave_outcomes"
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
# Mechanical liveness only: an exhausted retry allowance is infrastructure,
# never Curriculum saturation or a project verdict. Productive segments reset it.
_MAX_ACTIONLESS_WAVE_SEGMENTS = 3


@dataclass(frozen=True)
class WaveProject:
    project_id: str
    instruction: str


@dataclass(frozen=True)
class WaveDecision:
    decision: str
    rationale: str
    projects: tuple[WaveProject, ...]
    raw: str


@dataclass
class _BranchRuntime:
    project_index: int
    project: WaveProject
    episode_dir: Path
    desktop: Any
    vm: Any
    actor_history: list[dict[str, Any]]
    actor_handoff: str
    verifier_report: str
    terminal_outcome: str
    wave_memory: dict[str, bytes]


def parse_wave_handoff(text: str) -> WaveDecision | None:
    """Parse the Curriculum's lossless transport envelope, not its strategy."""

    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    decision = value.get("decision")
    rationale = value.get("rationale")
    projects = value.get("projects")
    if (decision not in {"WAVE", "SATURATED", "STALLED"}
            or not isinstance(rationale, str) or not rationale.strip()
            or not isinstance(projects, list)):
        return None
    if decision != "WAVE":
        if projects:
            return None
        return WaveDecision(decision, rationale.strip(), (), text)
    if not projects:
        return None
    parsed: list[WaveProject] = []
    seen: set[str] = set()
    for record in projects:
        if not isinstance(record, dict):
            return None
        project_id = record.get("id")
        instruction = record.get("instruction")
        if (not isinstance(project_id, str)
                or not _PROJECT_ID.fullmatch(project_id)
                or project_id in seen
                or not isinstance(instruction, str)
                or not instruction.strip()
                or PROJECT_ROOT not in instruction):
            return None
        seen.add(project_id)
        parsed.append(WaveProject(project_id, instruction.strip()))
    return WaveDecision(decision, rationale.strip(), tuple(parsed), text)


def _remove_wave_publication(vm) -> None:
    quoted_root = shlex.quote(WAVE_ROOT)
    quoted_handoff = shlex.quote(WAVE_HANDOFF)
    out = vm.run_command(
        f"rm -rf -- {quoted_root}; rm -f -- {quoted_handoff}; "
        f"test ! -e {quoted_root} && test ! -e {quoted_handoff}; "
        "echo PHASE1_WAVE_CLEAR_RC=$?", timeout=120) or ""
    if "PHASE1_WAVE_CLEAR_RC=0" not in out:
        raise E15InfrastructureError("could not clear the Curriculum wave surface")


def _validate_wave_fixtures(vm, decision: WaveDecision) -> bool:
    for project in decision.projects:
        source = f"{WAVE_ROOT}/{project.project_id}/evolution_project"
        quoted = shlex.quote(source)
        out = vm.run_command(
            f"test -d {quoted} && "
            f"test -n \"$(find {quoted} -mindepth 1 -print -quit)\"; "
            "echo PHASE1_WAVE_FIXTURE_RC=$?", timeout=60) or ""
        if "PHASE1_WAVE_FIXTURE_RC=0" not in out:
            return False
    return True


def _curriculum_continuation(detail: str) -> str:
    return f"""CURRICULUM AGENT — SAME-CONTEXT WAVE CONTINUATION.

The prior segment did not commit a replayable wave: {detail}. This is a generic
transport/workspace observation, not a grade, search instruction, or convergence
signal. The incomplete handoff was cleared, but your draft fixtures at {WAVE_ROOT}
and other private working files remain on the same machine. Inspect and continue
that work in your same context. Publish a fresh complete handoff when ready; only
its declared projects will be replayed. You remain free to choose WAVE, SATURATED,
or STALLED based on your evidence.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _author_wave(
        *, hooks: E15Hooks, vm, cfg, target_direction: str,
        previous_wave_outcomes: str, history: list[dict[str, Any]],
        sink_root: Path, target_query_conditioned: bool) \
        -> tuple[WaveDecision, list[dict[str, Any]]]:
    """Keep draft work across retries without resetting transport ceilings."""

    current_history = list(history)
    prompt = phase1_wave_curriculum_charter(
        target_direction,
        previous_wave_outcomes=previous_wave_outcomes,
        handoff_path=WAVE_HANDOFF,
        wave_root=WAVE_ROOT,
        target_query_conditioned=target_query_conditioned)
    continuing = bool(current_history)
    segment = 0
    phase_cfg = _phase_cfg(cfg, WAVE_HANDOFF)
    # This role uses content JSON, including when a provider advertises no native
    # tools. Pin the distinction above the accumulated same-context narration.
    phase_cfg.system_extra += "\n\n" + PLAIN_JSON_TRANSPORT_NOTE
    used_iters = 0
    used_wall = 0.0
    actionless_segments = 0
    report: dict[str, Any] = {
        "schema_version": 1, "status": "authoring", "segments": 0,
        "iters": 0, "wall_secs": 0.0, "actionless_segments": 0,
        "limits": {"iters": phase_cfg.max_iters,
                   "wall_secs": phase_cfg.wall_clock_secs,
                   "actionless_segments": _MAX_ACTIONLESS_WAVE_SEGMENTS},
    }

    def blocked(reason: str) -> None:
        # Archive unpublished inputs for inspection only. These bytes are never
        # promoted to project fixtures or Actor memory by a failed authoring turn.
        report.update(status="infra", reason=reason)
        _atomic_json(sink_root / "authoring_state.json", report)
        try:
            draft = hooks.capture_project(
                vm, "unpublished-curriculum-wave",
                str(sink_root / "blocked_draft"),
                guest_dirs=[Path(WAVE_ROOT).name], attempts=1)
            report["draft_capture"] = draft
        except Exception as exc:  # preserve the primary failure if capture fails
            report["draft_capture"] = {"ok": False, "error": str(exc)}
        _atomic_json(sink_root / "authoring_state.json", report)
        raise E15InfrastructureError(reason)

    # A fresh wave starts empty. Retries within that wave clear only the stale
    # terminal envelope, so multi-segment construction does not erase itself.
    _remove_wave_publication(vm)
    while True:
        remaining_iters = phase_cfg.max_iters - used_iters
        remaining_wall = phase_cfg.wall_clock_secs - used_wall
        if remaining_iters <= 0 or remaining_wall <= 0:
            blocked("Curriculum wave exhausted its cumulative transport ceiling")
        _audit_prompt(hooks, prompt, target_direction)
        sink_dir = sink_root / f"segment_{segment:03d}"
        while sink_dir.exists():
            segment += 1
            sink_dir = sink_root / f"segment_{segment:03d}"

        def publication_ready() -> bool:
            decision = parse_wave_handoff(
                hooks.read_guest_text(vm, WAVE_HANDOFF))
            return decision is not None and (
                decision.decision != "WAVE"
                or _validate_wave_fixtures(vm, decision))

        sink = hooks.sink_factory(str(sink_dir))
        result, current_history = hooks.run_attempt(
            prompt, vm, phase_cfg, sink,
            iters_budget=remaining_iters, wall_budget=remaining_wall,
            initial_history=current_history, continue_context=continuing,
            terminal_handoff_ready=publication_ready)
        sink.save_result(dataclasses.asdict(result))
        used_iters += result.iters
        # LoopResult.wall_secs already excludes provider transport pauses.
        used_wall += result.wall_secs
        actionless_segments = (
            0 if result.programs_run or result.looks else actionless_segments + 1)
        report.update(
            segments=report["segments"] + 1, last_segment=sink_dir.name,
            last_segment_status=result.status, iters=used_iters,
            wall_secs=used_wall, actionless_segments=actionless_segments)
        _atomic_json(sink_root / "authoring_state.json", report)
        try:
            _audit_agent_artifacts(hooks, str(sink_dir), target_direction)
        except E15BoundaryError as exc:
            report.update(status="quarantined", reason=str(exc))
            try:
                _atomic_json(sink_root / "authoring_state.json", report)
            except OSError:
                pass  # The original quarantine remains authoritative.
            raise
        status = str(getattr(result, "status", "infra"))
        text = hooks.read_guest_text(vm, WAVE_HANDOFF)
        if text and hooks.audit_text(
                text, mode="practice",
                authorized_instruction=target_direction):
            raise E15BoundaryError(
                "Curriculum wave handoff failed the target boundary")
        decision = parse_wave_handoff(text)
        if decision is not None and status == "done":
            if decision.decision != "WAVE" or _validate_wave_fixtures(
                    vm, decision):
                report.update(status="published", decision=decision.decision)
                _atomic_json(sink_root / "authoring_state.json", report)
                return decision, current_history
            detail = "one or more declared fixture trees were absent or empty"
        elif decision is None:
            detail = "the JSON handoff was missing, malformed, or incomplete"
        else:
            detail = "the publication preceded the Agent's completed segment"

        if status not in {"done", "stalled", "stalled_quiescent", "budget"}:
            blocked(
                "Curriculum wave phase ended at infrastructure status " + status)
        if actionless_segments >= _MAX_ACTIONLESS_WAVE_SEGMENTS:
            blocked(
                "Curriculum wave produced no Program or Look actions in "
                f"{actionless_segments} consecutive segments; retrying requires "
                "runtime review, not a convergence decision")
        _remove_guest_file(vm, WAVE_HANDOFF)
        prompt = _curriculum_continuation(detail)
        continuing = True
        segment += 1


def _capture_wave_fixtures(
        *, hooks: E15Hooks, vm, decision: WaveDecision,
        assignments: tuple[tuple[int, WaveProject, Path], ...],
        target_direction: str) -> None:
    for project_index, project, episode_dir in assignments:
        source = f"{WAVE_ROOT}/{project.project_id}/evolution_project"
        command = (
            f"rm -rf -- {shlex.quote(PROJECT_ROOT)}; "
            f"cp -a -- {shlex.quote(source)} {shlex.quote(PROJECT_ROOT)}; "
            "echo PHASE1_WAVE_STAGE_RC=$?")
        out = vm.run_command(command, timeout=120) or ""
        if "PHASE1_WAVE_STAGE_RC=0" not in out:
            _atomic_text(episode_dir / "fixture_stage_failure.txt", out)
            # Host-owned copy is idempotent; no model action is replayed.
            out = vm.run_command(command, timeout=120) or ""
        if "PHASE1_WAVE_STAGE_RC=0" not in out:
            raise E15InfrastructureError(
                f"could not stage Curriculum fixture {project.project_id}: {out[-1000:]}")
        fixture_dir = episode_dir / "fixtures"
        _capture_owned_tree(
            hooks, vm, f"phase1-project-{project_index:03d}",
            str(fixture_dir), target_direction)
        _atomic_text(episode_dir / "project.md", project.instruction)


def _branch_control_config(base_cfg):
    private_paths = tuple(dict.fromkeys(
        tuple(getattr(base_cfg, "verifier_private_paths", ()) or ())
        + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)))
    return dataclasses.replace(
        base_cfg,
        verifier_evolve_route=False,
        verifier_local_verdict_only=False,
        verifier_unverified_evidence=False,
        verifier_failure_starts_evolution=False,
        verifier_hide_actor_memory=True,
        verifier_stage_lifecycle=False,
        verifier_persist_scratch=False,
        verifier_private_paths=private_paths)


def _execute_branch(
        *, vm_factory: Callable[[], tuple[Any, Any]], hooks: E15Hooks,
        project_index: int, project: WaveProject, episode_dir: Path,
        wave_memory_dir: Path, actor_cfg, verifier_control_cfg,
        target_direction: str) -> _BranchRuntime:
    """Run one isolated Actor -> Verifier branch, leaving its Actor resumable."""

    desktop = None
    try:
        desktop, vm = vm_factory()
        wave_memory = _read_memory_tree(str(wave_memory_dir))
        _replay(hooks, vm, str(episode_dir / "fixtures"))
        _push_canonical_memory(hooks, vm, str(wave_memory_dir))

        actor_history: list[dict[str, Any]] = []
        actor_prompt = self_evolving_actor_charter(
            project.instruction, _listing(wave_memory), ACTOR_HANDOFF) \
            + _actor_runtime_contract()
        actor_continuation = False
        while True:
            actor_decision, actor_history, _ = _run_handoff_phase(
                hooks=hooks, vm=vm, cfg=actor_cfg, prompt=actor_prompt,
                role="ACTOR", handoff_path=ACTOR_HANDOFF,
                token_pattern=_ACTOR_TOKEN,
                sink_root=str(episode_dir / "actor" / "attempt_001"),
                target=target_direction, history=actor_history,
                continuation=actor_continuation)
            actor_continuation = True
            candidate_dir = episode_dir / "candidates/cycle_001"
            try:
                _capture_owned_tree(
                    hooks, vm, f"phase1-candidate-{project_index:03d}",
                    str(candidate_dir), target_direction)
                break
            except E15InfrastructureError:
                actor_prompt = _continuation_after_transport(
                    "ACTOR", ACTOR_HANDOFF,
                    f"{PROJECT_ROOT} was absent, empty, or not replayable")

        _atomic_text(
            episode_dir / "handoffs/actor_001.md", actor_decision.text)
        execution_evidence = _build_actor_execution_evidence(
            episode_dir / "actor", 1,
            episode_dir / "actor_execution_evidence/cycle_001")

        _fresh_vm(hooks, vm, target_direction)
        _replay(hooks, vm, str(candidate_dir))
        from core.verifier import VerifierSession, verify_agentic

        control_cfg = _branch_control_config(verifier_control_cfg)
        session = VerifierSession()
        try:
            verdict, findings = verify_agentic(
                project.instruction, vm, control_cfg,
                sink=ArtifactSink(str(episode_dir / "verifier/cycle_001")),
                turn_no=project_index,
                context=(
                    f"The candidate project is {PROJECT_ROOT}. Actor-private "
                    "memory, handoff prose, reasoning, and execution logs are "
                    "hidden by the harness. Investigate the actual candidate "
                    "independently."),
                session=session, wall_budget=control_cfg.wall_clock_secs)
        finally:
            session.close_executor()
        if verdict not in {"pass", "wrong"}:
            raise E15InfrastructureError(
                "Phase-1 Verifier Agent ended without PASS/FAIL: "
                + str(findings))
        terminal_outcome = "PASS" if verdict == "pass" else "FAIL"
        verifier_report = str(findings)
        _atomic_text(
            episode_dir / "handoffs/verifier_001.md", verifier_report)

        _push_actor_execution_evidence(hooks, vm, execution_evidence)
        _verify_candidate(hooks, vm, str(candidate_dir))
        _verify_actor_execution_evidence(vm, execution_evidence)

        # Throw away Verifier effects and work-phase memory edits, then replay the
        # exact submitted candidate.  The parent later replaces this branch's
        # pre-wave memory with the current canonical bank at its serialized turn.
        _fresh_vm(hooks, vm, target_direction)
        _replay(hooks, vm, str(candidate_dir))
        _push_canonical_memory(hooks, vm, str(wave_memory_dir))
        return _BranchRuntime(
            project_index=project_index, project=project,
            episode_dir=episode_dir, desktop=desktop, vm=vm,
            actor_history=actor_history, actor_handoff=actor_decision.text,
            verifier_report=verifier_report,
            terminal_outcome=terminal_outcome, wave_memory=wave_memory)
    except Exception:
        if desktop is not None:
            desktop.close()
        raise


def _execute_branch_with_infra_retry(**kwargs) -> _BranchRuntime:
    """One bounded clean retry for broken machines before a terminal verdict.

    Candidate state cannot be faithfully resumed after a failed guest filesystem.
    Archive that unscored attempt and restart from the same immutable fixtures.
    Completed verdicts, memory work, and boundary violations are never retried.
    """
    episode = kwargs["episode_dir"]
    try:
        return _execute_branch(**kwargs)
    except E15BoundaryError:
        raise
    except Exception as exc:
        if (episode / "handoffs/verifier_001.md").exists():
            raise
        physical = isinstance(exc, (TimeoutError, ConnectionError)) or any(
            marker in str(exc) for marker in (
                "fresh VM boundary failed", "Read timed out", "port_allocation"))
        for trace in episode.glob("**/trace_meta.json"):
            if "infra_attempts" in trace.parts:
                continue
            metadata = json.loads(trace.read_text())
            output = trace.with_name("trace.txt")
            if metadata.get("infra_fail") and output.is_file():
                text = output.read_text(errors="replace").lower()
                physical |= any(marker in text for marker in (
                    "read-only file system", "channel error",
                    "rollback-mirror setup failed"))
        if not physical:
            raise
        archive = episode / "infra_attempts/attempt_001"
        archive.mkdir(parents=True, exist_ok=False)
        for path in list(episode.iterdir()):
            if path.name not in {"fixtures", "project.md", "infra_attempts"}:
                shutil.move(str(path), archive / path.name)
        _atomic_json(archive / "retry_receipt.json", {
            "reason": f"{type(exc).__name__}: {exc}",
            "mode": "restart_unscored_branch_from_immutable_fixtures",
            "prior_memory_promoted": False, "maximum_retries": 1})
    return _execute_branch(**kwargs)


def _memory_changes(before: dict[str, bytes], after: dict[str, bytes]) \
        -> dict[str, list[str]]:
    before_names = set(before)
    after_names = set(after)
    return {
        "created": sorted(after_names - before_names),
        "deleted": sorted(before_names - after_names),
        "modified": sorted(
            name for name in before_names & after_names
            if before[name] != after[name]),
        "unchanged": sorted(
            name for name in before_names & after_names
            if before[name] == after[name]),
    }


def _commit_branch_memory(
        *, branch: _BranchRuntime, hooks: E15Hooks, memory_cfg,
        canonical_memory_dir: Path, journal_dir: Path, corpus_path: str,
        target_direction: str) -> dict[str, Any]:
    """Resume the same branch Actor and serialize its update into one bank."""

    before = _read_memory_tree(str(canonical_memory_dir))
    hooks.install_memory(
        str(branch.episode_dir / "memory_before_commit"), before)
    _push_canonical_memory(hooks, branch.vm, str(canonical_memory_dir))
    history, _ = _run_free_phase(
        hooks=hooks, vm=branch.vm, cfg=memory_cfg,
        prompt=self_evolving_actor_memory_distillation_msg(
            branch.terminal_outcome, branch.verifier_report),
        role="ACTOR",
        sink_root=str(branch.episode_dir / "memory_distillation"),
        target=target_direction, history=branch.actor_history)
    history, _ = _run_free_phase(
        hooks=hooks, vm=branch.vm, cfg=memory_cfg,
        prompt=self_evolving_actor_memory_reconciliation_msg(),
        role="ACTOR",
        sink_root=str(branch.episode_dir / "memory_reconciliation"),
        target=target_direction, history=history)
    candidate = _pull_terminal_memory(hooks, branch.vm)
    accepted = hooks.audit_memory(
        candidate, corpus_path,
        str(canonical_memory_dir.parent / "audit_rejects.jsonl"),
        authorized_instruction=target_direction, require_corpus=True)
    if accepted != candidate:
        raise E15BoundaryError("terminal memory failed the target boundary")
    hooks.journal_memory(
        str(journal_dir), branch.project_index, accepted,
        {"kind": "phase1-parallel-terminal-memory",
         "terminal_outcome": branch.terminal_outcome})
    hooks.install_memory(str(canonical_memory_dir), accepted)
    hooks.install_memory(
        str(branch.episode_dir / "memory_after_commit"), accepted)
    return {
        "before": _manifest(before),
        "after": _manifest(accepted),
        "changes": _memory_changes(before, accepted),
    }


def _resume_completed_waves(
        lineage: Path, *, target_direction: str, project_budget: int | None,
        checkpoint_projects: tuple[int, ...], max_parallel: int,
        target_query_conditioned: bool):
    """Admit only an infrastructure stop at a fully committed wave boundary."""
    def require(condition, reason):
        if not condition:
            raise E15InfrastructureError("Phase-1 wave resume: " + reason)

    state = json.loads((lineage / "state.json").read_text())
    require(state.get("status") == "infra", "state is not infrastructure-stopped")
    require(state.get("target_sha256") == hashlib.sha256(
        target_direction.encode()).hexdigest(), "target changed")
    require(state.get("project_budget") == project_budget, "project budget changed")
    require(state.get("max_parallel") == max_parallel, "parallelism changed")
    require(state.get("checkpoint_projects") == list(checkpoint_projects), "checkpoints changed")
    require(state.get("target_query_conditioned") == target_query_conditioned,
            "conditioning mode changed")
    count = state.get("projects")
    require(type(count) is int and count > 0, "no committed projects to resume")
    episodes = sorted((lineage / "episodes").glob("ep*"))
    require([p.name for p in episodes] == [f"ep{i:03d}" for i in range(1, count + 1)],
            "uncommitted or missing project directories")
    outcomes = [json.loads((p / "outcome.json").read_text()) for p in episodes]
    previous_memory = {}
    for index, record in enumerate(outcomes, 1):
        require(record.get("project_index") == index, "noncontiguous project ledger")
        require(record.get("terminal_outcome") in {"PASS", "FAIL"}, "nonterminal project")
        require(record.get("memory_before") == previous_memory, "memory journal discontinuity")
        previous_memory = record["memory_after"]
    memory = _manifest(_read_memory_tree(str(lineage / "memory")))
    require(memory == previous_memory == state.get("memory_manifest"), "canonical memory drift")
    waves = sorted((lineage / "waves").glob("wave_*"))
    require(bool(waves), "no completed waves")
    require([p.name for p in waves] == [f"wave_{i:03d}" for i in range(1, len(waves) + 1)],
            "noncontiguous wave ledger")
    cursor = 0
    for wave_index, directory in enumerate(waves, 1):
        require((directory / "outcomes.md").is_file(), "uncommitted wave remains")
        decision = parse_wave_handoff((directory / "curriculum_decision.json").read_text())
        require(decision is not None and decision.decision == "WAVE", "invalid wave decision")
        for project in decision.projects:
            require(cursor < len(outcomes), "wave exceeds committed project ledger")
            record = outcomes[cursor]
            require(record.get("wave_index") == wave_index
                    and record.get("project_id") == project.project_id,
                    "wave/project ledger disagreement")
            cursor += 1
    require(cursor == count, "project ledger exceeds completed waves")
    require(state.get("waves") in {len(waves), len(waves) + 1}, "wave counter drift")
    curriculum = lineage / "curriculum"
    require(not any(p.name > waves[-1].name for p in curriculum.glob("wave_*")),
            "archive the failed unpublished Curriculum attempt before resuming")
    transcripts = list((curriculum / waves[-1].name).glob("segment_*/transcript.json"))
    require(bool(transcripts), "missing persistent Curriculum context")
    last = max(transcripts, key=lambda p: int(p.parent.name.split("_")[1]))
    history = json.loads(last.read_text())["messages"]
    require(bool(history) and len(history) % 2 == 0, "incomplete Curriculum context")
    require(all(m.get("role") == ("user" if i % 2 == 0 else "assistant")
                for i, m in enumerate(history)), "invalid Curriculum context roles")
    history, _ = _scope_prior_curriculum_visuals(history)
    return count, len(waves), history, (waves[-1] / "outcomes.md").read_text()


def evolve_parallel_phase1(
        *, root: str, target_direction: str, actor_cfg,
        curriculum_cfg, memory_cfg, verifier_control_cfg,
        vm_factory: Callable[[], tuple[Any, Any]], corpus_path: str,
        hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        project_budget: int | None = None,
        checkpoint_projects: tuple[int, ...] = (),
        max_parallel: int = 4,
        target_query_conditioned: bool = False,
        resume_completed_boundary: bool = False) -> E15Result:
    """Run persistent-Curriculum parallel waves until Agent saturation.

    ``project_budget`` is an undisclosed emergency boundary checked only between
    complete Agent-authored waves. It never truncates a wave or defines semantic
    convergence. ``max_parallel`` is host scheduling capacity, not wave width.
    """

    if project_budget is not None and (
            type(project_budget) is not int or project_budget < 0):
        raise ValueError("project_budget must be nonnegative or None")
    if type(max_parallel) is not int or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    checkpoint_projects = tuple(sorted(set(checkpoint_projects)))
    if any(type(value) is not int or value < 0
           for value in checkpoint_projects):
        raise ValueError("checkpoint_projects must be nonnegative integers")

    hooks = hooks or E15Hooks()
    lineage = Path(root)
    memory_dir = lineage / "memory"
    journal_dir = lineage / "memory_journal"
    episodes_dir = lineage / "episodes"
    waves_dir = lineage / "waves"
    checkpoints_dir = lineage / "checkpoints"
    state_path = lineage / "state.json"
    for path in (memory_dir, journal_dir, episodes_dir, waves_dir):
        path.mkdir(parents=True, exist_ok=True)

    project_index = 0
    wave_index = 0
    curriculum_history: list[dict[str, Any]] = []
    previous_wave_outcomes = ""
    if resume_completed_boundary:
        from explore.phase1_boundary_recovery import validate_boundary, archive_pending
        plan = validate_boundary(
            lineage, target_direction, project_budget=project_budget,
            checkpoint_projects=checkpoint_projects, max_parallel=max_parallel,
            target_query_conditioned=target_query_conditioned)
        archive_pending(lineage, plan)
        project_index, wave_index = plan["projects"], plan["wave"]
        curriculum_history = plan["history"]
        previous_wave_outcomes = plan["outcomes"]

    def emit(event_type: str, **payload: Any) -> None:
        if event_sink is not None:
            event_sink(
                event_type, status=payload.pop("status", "in_progress"),
                state={"project_open": False, "memory_phase_open": False,
                       "recovery_open": False}, payload=payload)

    def checkpoint(index: int) -> None:
        if index not in checkpoint_projects:
            return
        destination = checkpoints_dir / f"project_{index:03d}/memory"
        hooks.install_memory(
            str(destination), _read_memory_tree(str(memory_dir)))

    def finish(status: str, reason: str, terminal_text: str = "") -> E15Result:
        payload = {
            "schema_version": 2,
            "status": status,
            "projects": project_index,
            "waves": wave_index,
            "last_project": project_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "phase1_parallel_waves": True,
            "target_query_conditioned": target_query_conditioned,
            "project_budget": project_budget,
            "max_parallel": max_parallel,
            "checkpoint_projects": list(checkpoint_projects),
            "target_sha256": hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(
                _read_memory_tree(str(memory_dir))),
        }
        _atomic_json(state_path, payload)
        return E15Result(
            status=status, projects=project_index, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            terminal_text=terminal_text, last_project=project_index)

    active_branches: list[_BranchRuntime] = []
    try:
        if not target_direction.strip():
            raise E15InfrastructureError("target direction is empty")
        hooks.validate_corpus(corpus_path)
        _audit_prompt(hooks, target_direction, target_direction)
        if not resume_completed_boundary:
            checkpoint(0)
        else:
            emit("PHASE1_WAVE_BOUNDARY_RESUMED", wave=wave_index, projects=project_index)
        while True:
            if project_budget is not None and project_index >= project_budget:
                return finish(
                    "budget_exhausted",
                    "external complete-wave compute boundary reached")

            wave_index += 1
            wave_dir = waves_dir / f"wave_{wave_index:03d}"
            curriculum_sink = lineage / "curriculum" / \
                f"wave_{wave_index:03d}"
            curriculum_desktop, curriculum_vm = vm_factory()
            try:
                _push_canonical_memory(hooks, curriculum_vm, str(memory_dir))
                decision, curriculum_history = _author_wave(
                    hooks=hooks, vm=curriculum_vm, cfg=curriculum_cfg,
                    target_direction=target_direction,
                    previous_wave_outcomes=previous_wave_outcomes,
                    history=curriculum_history, sink_root=curriculum_sink,
                    target_query_conditioned=target_query_conditioned)
                _atomic_text(wave_dir / "curriculum_decision.json", decision.raw)
                if decision.decision in {"SATURATED", "STALLED"}:
                    _atomic_text(
                        lineage / "curriculum_terminal.json", decision.raw)
                    return finish(
                        decision.decision.lower(),
                        "Curriculum Agent terminal decision", decision.raw)

                wave_memory_dir = wave_dir / "memory_snapshot"
                wave_memory = _read_memory_tree(str(memory_dir))
                hooks.install_memory(str(wave_memory_dir), wave_memory)
                assignments = tuple(
                    (project_index + offset, project,
                     episodes_dir / f"ep{project_index + offset:03d}")
                    for offset, project in enumerate(decision.projects, 1))
                _capture_wave_fixtures(
                    hooks=hooks, vm=curriculum_vm, decision=decision,
                    assignments=assignments,
                    target_direction=target_direction)
            finally:
                curriculum_desktop.close()

            emit(
                "PHASE1_WAVE_STARTED", wave=wave_index,
                projects=len(assignments),
                memory_manifest=_manifest(wave_memory),
                max_parallel=max_parallel)
            completed: dict[int, _BranchRuntime] = {}
            branch_errors: list[Exception] = []
            with ThreadPoolExecutor(
                    max_workers=min(max_parallel, len(assignments)),
                    thread_name_prefix=f"phase1-wave-{wave_index}") as pool:
                futures = {
                    pool.submit(
                        _execute_branch_with_infra_retry, vm_factory=vm_factory, hooks=hooks,
                        project_index=index, project=project,
                        episode_dir=episode_dir,
                        wave_memory_dir=wave_memory_dir,
                        actor_cfg=copy.deepcopy(actor_cfg),
                        verifier_control_cfg=copy.deepcopy(
                            verifier_control_cfg),
                        target_direction=target_direction): index
                    for index, project, episode_dir in assignments
                }
                for future in as_completed(futures):
                    try:
                        branch = future.result()
                    except Exception as exc:
                        branch_errors.append(exc)
                        # Persist immediately: a productive sibling can take
                        # hours to finish after this future has already failed.
                        # This event does not commit memory or decide a verdict.
                        emit(
                            "PHASE1_BRANCH_EXCEPTION", wave=wave_index,
                            project=futures[future],
                            error_type=type(exc).__name__, error=str(exc),
                            classification=("boundary" if isinstance(
                                exc, E15BoundaryError) else "infra"))
                        continue
                    completed[branch.project_index] = branch
                    active_branches.append(branch)
                    emit(
                        "PHASE1_BRANCH_VERIFIED", wave=wave_index,
                        project=branch.project_index,
                        project_id=branch.project.project_id,
                        terminal_outcome=branch.terminal_outcome)

            # Drain every future so successful siblings retain an owned handle
            # for cleanup even when another branch fails. Never commit a partial
            # wave merely because the failing future happened to return first.
            if branch_errors:
                boundary = next((e for e in branch_errors
                                 if isinstance(e, E15BoundaryError)), None)
                raise boundary or branch_errors[0]

            rendered_outcomes: list[str] = []
            for index, _project, _episode_dir in assignments:
                branch = completed[index]
                memory_record = _commit_branch_memory(
                    branch=branch, hooks=hooks, memory_cfg=memory_cfg,
                    canonical_memory_dir=memory_dir,
                    journal_dir=journal_dir, corpus_path=corpus_path,
                    target_direction=target_direction)
                record = {
                    "schema_version": 2,
                    "wave_index": wave_index,
                    "project_index": index,
                    "project_id": branch.project.project_id,
                    "project": branch.project.instruction,
                    "terminal_outcome": branch.terminal_outcome,
                    "verification_cycles": 1,
                    "actor_handoffs": [branch.actor_handoff],
                    "verifier_reports": [branch.verifier_report],
                    "wave_memory_snapshot": _manifest(branch.wave_memory),
                    "memory_before": memory_record["before"],
                    "memory_after": memory_record["after"],
                    "memory_changes": memory_record["changes"],
                }
                _atomic_json(branch.episode_dir / "outcome.json", record)
                rendered = _outcome_text(record) + "\n\nMEMORY CHANGES:\n" \
                    + json.dumps(
                        memory_record["changes"], ensure_ascii=False,
                        indent=2, sort_keys=True)
                _atomic_text(branch.episode_dir / "outcome.md", rendered)
                rendered_outcomes.append(rendered)
                project_index = index
                checkpoint(project_index)
                branch.desktop.close()
                active_branches.remove(branch)
                emit(
                    "PHASE1_BRANCH_MEMORY_COMMITTED", status="completed",
                    wave=wave_index, project=project_index,
                    project_id=branch.project.project_id,
                    terminal_outcome=branch.terminal_outcome,
                    memory_changes=memory_record["changes"])

            previous_wave_outcomes = (
                f"WAVE {wave_index} CURRICULUM RATIONALE:\n"
                f"{decision.rationale}\n\n" + "\n\n---\n\n".join(
                    rendered_outcomes))
            _atomic_text(wave_dir / "outcomes.md", previous_wave_outcomes)
            curriculum_history, _ = _scope_prior_curriculum_visuals(
                curriculum_history)
            _atomic_json(state_path, {
                "schema_version": 2,
                "status": "running",
                "waves": wave_index,
                "projects": project_index,
                "last_project": project_index,
                "target_sha256": hashlib.sha256(
                    target_direction.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(
                    _read_memory_tree(str(memory_dir))),
            })
            emit(
                "PHASE1_WAVE_COMPLETED", status="completed",
                wave=wave_index, projects=len(assignments),
                total_projects=project_index)
    except KeyboardInterrupt:
        return finish("infra", "KeyboardInterrupt: external interruption")
    except E15BoundaryError as exc:
        return finish("quarantined", str(exc))
    except Exception as exc:  # noqa: BLE001 - persist inspectable infra state
        return finish("infra", f"{type(exc).__name__}: {exc}")
    finally:
        for branch in active_branches:
            try:
                branch.desktop.close()
            except Exception:
                pass


__all__ = [
    "WAVE_HANDOFF",
    "WAVE_ROOT",
    "WaveDecision",
    "WaveProject",
    "evolve_parallel_phase1",
    "parse_wave_handoff",
]
