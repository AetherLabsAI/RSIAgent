"""Candidate evidence and Actor-owned learning shared by target and practice runs."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any, Callable

from explore.charter import (
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
    self_evolving_verifier_charter,
)
from explore.memory_hash import tree_sha256
from explore.practice_loop import (
    _ACTOR_TOKEN,
    _VERIFIER_TOKEN,
    ACTOR_EXECUTION_EVIDENCE,
    ACTOR_HANDOFF,
    PROJECT_ROOT,
    VERIFIER_REPORT,
    PracticeBoundaryError,
    PracticeHooks,
    PracticeInfrastructureError,
    _actor_runtime_contract,
    _atomic_text,
    _build_actor_execution_evidence,
    _capture_owned_tree,
    _continuation_after_transport,
    _fresh_vm,
    _listing,
    _parse_unique_handoff,
    _pull_terminal_memory,
    _push_actor_execution_evidence,
    _push_canonical_memory,
    _read_memory_tree,
    _replay,
    _run_free_phase,
    _run_handoff_phase,
    _run_learning_diagnosis,
    _verify_actor_execution_evidence,
    _verify_candidate,
)

PRACTICE_FIXTURE_EVIDENCE = "/home/user/.practice_original_project"

class PracticePublicationError(PracticeInfrastructureError):
    """Agent-correctable owned-output shape defect, not transport failure."""


def _target_audit_surface(
    target: str, target_visible_inputs: tuple[str, ...] | None = None
) -> str:
    """Authorize only the ordinary task-visible input names for TARGET.

    The exam fence deliberately
    treats several Task056 asset basenames as suspicious grader constants.
    In TARGET those paths are part of the normal solver-visible desktop, so they
    must be bound into the operator-authorized audit surface.  Structural
    benchmark paths, repositories, task ids, evaluator values, and scores
    remain forbidden by the unchanged fence rules.
    """

    visible_inputs = (
        () if target_visible_inputs is None else tuple(target_visible_inputs)
    )
    if not visible_inputs:
        return target
    return target + "\n\nNORMAL TASK-VISIBLE INPUT PATHS:\n" + "\n".join(visible_inputs)


def _memory_tree_sha256(files: dict[str, bytes]) -> str:
    hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())
    }
    return tree_sha256(hashes)




def _stage_original_practice_fixtures(vm) -> None:
    """Publish a read-only comparison view of pre-Actor practice fixtures."""

    command = f"""
set -eu
rm -rf {PRACTICE_FIXTURE_EVIDENCE}
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT}
cp -a {PROJECT_ROOT} {PRACTICE_FIXTURE_EVIDENCE}
chmod -R a-w {PRACTICE_FIXTURE_EVIDENCE}
echo RSI_ORIGINAL_FIXTURE_STAGE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "RSI_ORIGINAL_FIXTURE_STAGE_RC=0" not in out:
        raise PracticeInfrastructureError(
            "original practice fixtures could not be staged for verification"
        )


def _verify_original_practice_fixtures(
    hooks: PracticeHooks, vm, fixture_dir: Path
) -> None:
    """Fail closed if the Verifier mutated its original-fixture evidence."""

    command = f"""
set -eu
test -d {PRACTICE_FIXTURE_EVIDENCE} && \
  test ! -L {PRACTICE_FIXTURE_EVIDENCE}
rm -rf {PROJECT_ROOT}
mv {PRACTICE_FIXTURE_EVIDENCE} {PROJECT_ROOT}
echo RSI_ORIGINAL_FIXTURE_RESTORE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "RSI_ORIGINAL_FIXTURE_RESTORE_RC=0" not in out:
        raise PracticeInfrastructureError(
            "original practice fixture evidence could not be restored"
        )
    _verify_candidate(hooks, vm, str(fixture_dir))






def _require_owned_project_shape(vm) -> None:
    """Require a nonempty ordinary project tree before host transport."""

    command = f"""
bad=0
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT} || bad=1
if test "$bad" -eq 0; then
  test -n "$(find {PROJECT_ROOT} -type f -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} -type l -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} ! -type f ! -type d -print -quit)" || bad=1
fi
echo RSI_PROJECT_SHAPE_RC=$bad
"""
    out = vm.run_command(command, timeout=120, cap=1024) or ""
    if "RSI_PROJECT_SHAPE_RC=1" in out:
        raise PracticePublicationError("owned project root is absent, empty, or unsafe")
    if "RSI_PROJECT_SHAPE_RC=0" not in out:
        raise PracticeInfrastructureError("could not inspect owned project shape")








def _outcome_text(record: dict[str, Any]) -> str:
    pieces = [
        record["label"],
        "ORIGINAL REQUEST (verbatim):\n" + record["request"],
        "TERMINAL OUTCOME: " + record["terminal_outcome"],
        "VERIFIER REPORT (verbatim):\n" + record["verifier_report"],
        "ACTOR LEARNING DIAGNOSIS (verbatim):\n" + record["learning_diagnosis"],
    ]
    return "\n\n".join(pieces)


def _promote_learning(
    *,
    hooks: PracticeHooks,
    vm,
    cfg,
    lineage: Path,
    episode_dir: Path,
    experience_index: int,
    before_memory: dict[str, bytes],
    actor_history: list[dict[str, Any]],
    terminal_outcome: str,
    verifier_report: str,
    target: str,
    audit_mode: str,
    emit: Callable[..., None],
    project_open: bool,
    corpus_path: str,
    memory_dir: Path,
    journal_dir: Path,
    experience_kind: str,
    target_visible_inputs: tuple[str, ...] | None = None,
) -> tuple[dict[str, bytes], str, list[dict[str, Any]]]:
    audit_target = _target_audit_surface(target, target_visible_inputs)
    emit(
        "MEMORY_PHASE_STARTED",
        status="in_progress",
        project_open=project_open,
        memory_phase_open=True,
        payload={
            "experience_index": experience_index,
            "terminal_outcome": terminal_outcome,
            "verifier_report_sha256": hashlib.sha256(
                verifier_report.encode("utf-8")
            ).hexdigest(),
        },
    )
    memory_prompt = self_evolving_actor_memory_distillation_msg(
        terminal_outcome, verifier_report
    )
    actor_history, _ = _run_free_phase(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        prompt=memory_prompt,
        role="ACTOR",
        sink_root=str(episode_dir / "memory_distillation"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    actor_history, _ = _run_free_phase(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        prompt=self_evolving_actor_memory_reconciliation_msg(),
        role="ACTOR",
        sink_root=str(episode_dir / "memory_reconciliation"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    diagnosis, actor_history = _run_learning_diagnosis(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        terminal_outcome=terminal_outcome,
        verifier_report=verifier_report,
        sink_root=str(episode_dir / "learning_diagnosis"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    _atomic_text(episode_dir / "learning_diagnosis.md", diagnosis)

    candidate_memory = _pull_terminal_memory(hooks, vm)
    accepted_memory = hooks.audit_memory(
        candidate_memory,
        corpus_path,
        str(lineage / "audit_rejects.jsonl"),
        authorized_instruction=audit_target,
        require_corpus=True,
    )
    if accepted_memory != candidate_memory:
        raise PracticeBoundaryError("terminal memory failed the target boundary")
    hooks.journal_memory(
        str(journal_dir),
        experience_index,
        accepted_memory,
        {"kind": experience_kind, "terminal_outcome": terminal_outcome},
    )
    hooks.install_memory(str(memory_dir), accepted_memory)
    return accepted_memory, diagnosis, actor_history


def _run_practice_attempt(
    *,
    hooks: PracticeHooks,
    vm,
    lineage: Path,
    episode_dir: Path,
    project_index: int,
    project: str,
    fixture_dir: Path,
    target: str,
    actor_cfg,
    verifier_cfg,
    memory_dir: Path,
    emit: Callable[..., None],
    agentic_verifier_cfg=None,
    target_visible_inputs: tuple[str, ...] | None = None,
    resume_state=None,
) -> dict[str, Any]:
    """Run the existing one-submission/one-verdict null-task inner loop."""

    if resume_state is not None or (
        agentic_verifier_cfg is not None
        and getattr(agentic_verifier_cfg, "verifier_unverified_evidence", False)
    ):
        from explore.practice_evidence_recovery import run_practice_with_evidence

        return run_practice_with_evidence(
            hooks=hooks,
            vm=vm,
            lineage=lineage,
            episode_dir=episode_dir,
            project_index=project_index,
            project=project,
            fixture_dir=fixture_dir,
            target=target,
            actor_cfg=actor_cfg,
            verifier_cfg=verifier_cfg,
            memory_dir=memory_dir,
            emit=emit,
            agentic_verifier_cfg=agentic_verifier_cfg,
            target_visible_inputs=target_visible_inputs,
            resume_state=resume_state,
        )

    before_memory = _read_memory_tree(str(memory_dir))
    audit_target = _target_audit_surface(target, target_visible_inputs)
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    actor_history: list[dict[str, Any]] = []
    actor_prompt = (
        self_evolving_actor_charter(project, _listing(before_memory), ACTOR_HANDOFF)
        + _actor_runtime_contract()
    )
    emit(
        "ACTOR_PHASE_STARTED",
        status="in_progress",
        project_open=True,
        memory_phase_open=False,
        payload={
            "project_index": project_index,
            "actor_attempt": 1,
            "verification_cycle": 0,
        },
    )
    actor_continuation = False
    publication = 0
    while True:
        actor_decision, actor_history, _ = _run_handoff_phase(
            hooks=hooks,
            vm=vm,
            cfg=actor_cfg,
            prompt=actor_prompt,
            role="ACTOR",
            handoff_path=ACTOR_HANDOFF,
            token_pattern=_ACTOR_TOKEN,
            sink_root=str(episode_dir / "actor" / "attempt_001"),
            target=audit_target,
            history=actor_history,
            continuation=actor_continuation,
        )
        actor_continuation = True
        publication += 1
        candidate_dir = episode_dir / "candidate" / f"publication_{publication:03d}"
        try:
            _require_owned_project_shape(vm)
            _capture_owned_tree(
                hooks,
                vm,
                f"ep{project_index:03d}-candidate-{publication:03d}",
                str(candidate_dir),
                audit_target,
            )
        except PracticePublicationError:
            actor_prompt = _continuation_after_transport(
                "ACTOR",
                ACTOR_HANDOFF,
                f"{PROJECT_ROOT} was absent, empty, unsafe, or not replayable",
            )
            continue
        break
    _atomic_text(episode_dir / "actor_handoff.md", actor_decision.text)
    execution_evidence = _build_actor_execution_evidence(
        episode_dir / "actor", 1, episode_dir / "actor_execution_evidence"
    )
    emit(
        "VERIFIER_PHASE_STARTED",
        status="in_progress",
        project_open=True,
        memory_phase_open=False,
        payload={
            "project_index": project_index,
            "verification_cycle": 1,
            "actor_handoff_sha256": hashlib.sha256(
                actor_decision.text.encode("utf-8")
            ).hexdigest(),
            "actor_execution_manifest_sha256": execution_evidence["manifest_sha256"],
        },
    )
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _stage_original_practice_fixtures(vm)
    _replay(hooks, vm, str(candidate_dir))
    if agentic_verifier_cfg is None:
        # Historical RSI path. It remains byte-for-byte available to registered
        # experiments, while the full benchmark opts into the mechanically
        # effect-isolated Agentic Verifier below.
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
        verifier_prompt = self_evolving_verifier_charter(
            project,
            report_path=VERIFIER_REPORT,
            actor_execution_path=ACTOR_EXECUTION_EVIDENCE,
            original_fixtures_path=PRACTICE_FIXTURE_EVIDENCE,
        )
        verifier_decision, _, _ = _run_handoff_phase(
            hooks=hooks,
            vm=vm,
            cfg=verifier_cfg,
            prompt=verifier_prompt,
            role="VERIFIER",
            handoff_path=VERIFIER_REPORT,
            token_pattern=_VERIFIER_TOKEN,
            sink_root=str(episode_dir / "verifier"),
            target=audit_target,
            history=[],
            continuation=False,
            handoff_parser=_parse_unique_handoff,
            commit_on_publish=True,
        )
        terminal_outcome = verifier_decision.token
        verifier_report = verifier_decision.text
    else:
        # The Verifier remains a full code-as-policy Agent, but its programs run
        # in the read-only effect-isolated namespace used by the target harness.
        # Actor memory, handoff prose, and execution logs are mechanically hidden;
        # only the authoritative project, candidate, and original fixtures are
        # correctness evidence.
        from core.trace import ArtifactSink
        from core.verifier import VerifierSession, verify_agentic

        if not getattr(agentic_verifier_cfg, "agentic_verifier_config", ""):
            raise PracticeInfrastructureError(
                "practice Agentic Verifier control config is missing"
            )
        private_paths = tuple(
            dict.fromkeys(
                tuple(getattr(agentic_verifier_cfg, "verifier_private_paths", ()) or ())
                + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)
            )
        )
        control_cfg = dataclasses.replace(
            agentic_verifier_cfg,
            verifier_evolve_route=False,
            verifier_local_verdict_only=False,
            verifier_hide_actor_memory=True,
            verifier_stage_lifecycle=False,
            verifier_persist_scratch=False,
            verifier_private_paths=private_paths,
        )
        verifier_session = VerifierSession()
        context = (
            f"The candidate project is {PROJECT_ROOT}. The frozen original "
            f"input fixture is {PRACTICE_FIXTURE_EVIDENCE}; use it only as "
            "pre-candidate evidence. Actor-private memory, handoff prose, and "
            "execution logs are hidden by the harness."
        )
        try:
            verdict, findings = verify_agentic(
                project,
                vm,
                control_cfg,
                sink=ArtifactSink(str(episode_dir / "verifier")),
                turn_no=project_index,
                context=context,
                session=verifier_session,
                wall_budget=control_cfg.wall_clock_secs,
            )
        finally:
            verifier_session.close_executor()
        if verdict not in {"pass", "wrong"}:
            raise PracticeInfrastructureError(
                "practice Agentic Verifier ended without PASS/FAIL: " + str(findings)
            )
        terminal_outcome = "PASS" if verdict == "pass" else "FAIL"
        verifier_report = str(findings)
        # Archive integrity remains auditable, but the evidence is mounted only
        # after the independent Verifier has terminated so it cannot inherit the
        # Actor's narrative or self-checks.
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
    _verify_candidate(hooks, vm, str(candidate_dir))
    _verify_actor_execution_evidence(vm, execution_evidence)
    _verify_original_practice_fixtures(hooks, vm, fixture_dir)
    _atomic_text(episode_dir / "verifier_report.md", verifier_report)
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(candidate_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    return {
        "terminal_outcome": terminal_outcome,
        "verifier_report": verifier_report,
        "actor_handoff": actor_decision.text,
        "actor_history": actor_history,
        "before_memory": before_memory,
    }
