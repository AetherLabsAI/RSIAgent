"""Evaluator-free target-test/self-evolution lifecycle used by the full benchmark.

This protocol intentionally does not contain the unified loop's REVISE/EVOLVE
router.  A fresh Actor Agent attempts the target and one persistent Verifier
Agent context investigates every candidate revision in that attempt. A caller
may opt into a candidate-blind pre-Actor orientation experiment, but orientation
is not part of the clean default lifecycle. A caller may also opt into learning
from a verified PASS and asking the persistent Curriculum Agent whether one
contrastive experience has higher value than immediately returning target
control. FAIL always lets that same Actor Agent learn, then invokes the persistent
Curriculum Agent and its practice Actor Agent -> Verifier Agent inner loop before
a fresh target attempt.

The caller may attach a sealed external evaluator only after this state machine
returns.  No score or evaluator output is accepted by any hook.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SelfEvolvingLoopError(RuntimeError):
    """The role lifecycle produced an invalid transport-level state."""


class Phase2StopPolicy(str, Enum):
    """Precommitted training policy; neither option changes who judges a task."""

    VERIFIER_PASS = "verifier_pass"
    CURRICULUM_REVIEW = "curriculum_review"


DEFAULT_PHASE2_STOP_POLICY = Phase2StopPolicy.CURRICULUM_REVIEW.value


class TargetVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class EvolutionStatus(str, Enum):
    READY_FOR_RETRY = "READY_FOR_RETRY"
    STALLED = "STALLED"


@dataclass(frozen=True)
class TargetVerification:
    verdict: TargetVerdict
    report: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, TargetVerdict):
            try:
                object.__setattr__(
                    self, "verdict",
                    TargetVerdict(str(self.verdict).strip().upper()))
            except ValueError as exc:
                raise SelfEvolvingLoopError(
                    f"unknown target verdict: {self.verdict!r}") from exc
        if not isinstance(self.report, str) or not self.report.strip():
            raise SelfEvolvingLoopError(
                "target Verifier Agent report/reason must be nonempty")


@dataclass(frozen=True)
class ActorLearning:
    memory: Any
    diagnosis: str

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis, str) or not self.diagnosis.strip():
            raise SelfEvolvingLoopError(
                "Actor Agent learning diagnosis must be nonempty")


@dataclass(frozen=True)
class EvolutionResult:
    status: EvolutionStatus
    memory: Any
    projects: int
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvolutionStatus):
            try:
                object.__setattr__(
                    self, "status",
                    EvolutionStatus(str(self.status).strip().upper()))
            except ValueError as exc:
                raise SelfEvolvingLoopError(
                    f"unknown evolution status: {self.status!r}") from exc
        if type(self.projects) is not int or self.projects < 0:
            raise SelfEvolvingLoopError(
                "evolution project count must be a nonnegative integer")


@dataclass(frozen=True)
class SelfEvolvingStart:
    """Counters at an admitted boundary after completed practice, before a target.

    The caller must validate durable artifacts and resume any interrupted
    Curriculum turn before constructing this boundary. This never accepts an
    infrastructure stop as a semantic Curriculum decision.
    """

    target_cycles: int
    evolutions: int
    practice_projects: int
    final_after_stall: bool = False

    def __post_init__(self) -> None:
        if (any(type(v) is not int or v < 1 for v in (
                self.target_cycles, self.evolutions, self.practice_projects))
                or self.evolutions > self.target_cycles
                or type(self.final_after_stall) is not bool):
            raise SelfEvolvingLoopError("invalid completed-practice boundary")


@dataclass(frozen=True)
class SelfEvolvingLoopResult:
    """Terminal live target state; ownership transfers to the caller."""

    output: Any
    environment: Any
    verifier_verdict: TargetVerdict
    verifier_report: str
    memory: Any
    target_cycles: int
    evolutions: int
    practice_projects: int
    termination: str


def _noop_event(_event: str, _payload: dict[str, Any]) -> None:
    pass


def _noop_release(_environment: Any) -> None:
    pass


@dataclass(frozen=True)
class SelfEvolvingLoopHooks:
    fresh_target_environment: Callable[[str, int], Any]
    fresh_target_verifier: Callable[[Any, str, int], Any]
    fresh_target_actor: Callable[[Any, int], Any]
    actor_work: Callable[[Any, Any, Any, str], Any]
    verifier_verify: Callable[[Any, Any, str, Any], TargetVerification]
    actor_learn: Callable[
        [Any, Any, str, Any, TargetVerdict, str, Any], ActorLearning]
    evolve: Callable[
        [str, TargetVerdict, str, str, Any, int], EvolutionResult]
    verifier_orient: Callable[[Any, Any, str, int], None] | None = None
    learn_on_pass: bool = False
    curriculum_after_pass: bool = False
    release_target_environment: Callable[[Any], None] = _noop_release
    on_event: Callable[[str, dict[str, Any]], None] = _noop_event


def run_self_evolving_loop(
        target_direction: str, current_memory: Any,
        hooks: SelfEvolvingLoopHooks, *,
        start: SelfEvolvingStart | None = None) -> SelfEvolvingLoopResult:
    """Run target tests and practice without consulting an external evaluator.

    Curriculum ``STALLED`` is a semantic stop condition, not target correctness.
    The latest memory therefore receives one final fresh target test and that live
    state is returned for sealed measurement regardless of its local verdict.
    Infrastructure failures must raise from their hook and are never mapped here.
    """

    if not isinstance(target_direction, str) or not target_direction.strip():
        raise ValueError("target_direction must be nonempty")
    if hooks.curriculum_after_pass and not hooks.learn_on_pass:
        raise SelfEvolvingLoopError(
            "Curriculum review after PASS requires same-Actor PASS learning")

    memory = current_memory
    target_cycles = start.target_cycles if start else 0
    evolutions = start.evolutions if start else 0
    practice_projects = start.practice_projects if start else 0
    final_after_stall = start.final_after_stall if start else False

    while True:
        target_cycles += 1
        environment = hooks.fresh_target_environment(
            target_direction, target_cycles)
        if environment is None:
            raise SelfEvolvingLoopError(
                "fresh target environment was not created")
        transferred = False
        released = False
        try:
            verifier = hooks.fresh_target_verifier(
                environment, target_direction, target_cycles)
            if verifier is None:
                raise SelfEvolvingLoopError(
                    "fresh target Verifier Agent was not created")
            if hooks.verifier_orient is not None:
                hooks.on_event("TARGET_ORIENTATION_STARTED", {
                    "target_cycle": target_cycles,
                    "evolutions": evolutions,
                })
                # Optional experimental stage. No Actor context or action exists
                # at this boundary, and the same Verifier context must continue
                # into candidate verification.
                hooks.verifier_orient(
                    verifier, environment, target_direction, target_cycles)
                hooks.on_event("TARGET_ORIENTATION_COMPLETED", {
                    "target_cycle": target_cycles,
                    "evolutions": evolutions,
                })

            actor = hooks.fresh_target_actor(memory, target_cycles)
            if actor is None:
                raise SelfEvolvingLoopError(
                    "fresh target Actor Agent was not created")
            hooks.on_event("TARGET_STARTED", {
                "target_cycle": target_cycles,
                "evolutions": evolutions,
                "final_after_curriculum_stall": final_after_stall,
            })
            output = hooks.actor_work(
                actor, verifier, environment, target_direction)
            verification = hooks.verifier_verify(
                verifier, environment, target_direction, output)
            if not isinstance(verification, TargetVerification):
                raise SelfEvolvingLoopError(
                    "verifier_verify must return TargetVerification")
            hooks.on_event("TARGET_VERIFIED", {
                "target_cycle": target_cycles,
                "verdict": verification.verdict.value,
            })

            learning = None
            should_learn = (
                verification.verdict is TargetVerdict.FAIL
                or (verification.verdict is TargetVerdict.PASS
                    and hooks.learn_on_pass))
            if should_learn:
                hooks.on_event("TARGET_LEARNING_STARTED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "evolutions": evolutions,
                })
                learning = hooks.actor_learn(
                    actor, environment, target_direction, output,
                    verification.verdict, verification.report, memory)
                if not isinstance(learning, ActorLearning):
                    raise SelfEvolvingLoopError(
                        "actor_learn must return ActorLearning")
                memory = learning.memory
                hooks.on_event("TARGET_LEARNING_COMPLETED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "evolutions": evolutions,
                })

            review_pass = (
                verification.verdict is TargetVerdict.PASS
                and hooks.curriculum_after_pass
                and not final_after_stall)
            if (verification.verdict is TargetVerdict.PASS
                    and not review_pass):
                termination = "verifier_pass"
            elif final_after_stall:
                termination = "curriculum_stalled_final_target_test"
            elif verification.verdict is TargetVerdict.UNVERIFIED:
                termination = "target_harness_unverified"
            else:
                termination = ""

            if termination:
                hooks.on_event("TARGET_LOOP_TERMINATED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "termination": termination,
                    "evolutions": evolutions,
                })
                transferred = True
                return SelfEvolvingLoopResult(
                    output=output,
                    environment=environment,
                    verifier_verdict=verification.verdict,
                    verifier_report=verification.report,
                    memory=memory,
                    target_cycles=target_cycles,
                    evolutions=evolutions,
                    practice_projects=practice_projects,
                    termination=termination,
                )

            # Every grounded outcome has already been learned by the same Actor.
            # FAIL requires targeted practice. An opt-in PASS review lets the
            # Curriculum Agent choose whether a genuinely informative contrast
            # is worth running; it cannot edit or approve memory or correctness.
            if learning is None:
                raise SelfEvolvingLoopError(
                    "grounded outcome reached Curriculum without Actor learning")

            next_evolution = evolutions + 1
            if verification.verdict is TargetVerdict.FAIL:
                hooks.on_event("EVOLUTION_STARTED", {
                    "target_cycle": target_cycles,
                    "evolution": next_evolution,
                })
                # The failed live state is no longer needed. A PASS state remains
                # live while Curriculum decides, so zero-project READY can return
                # without silently changing the generic handoff contract.
                released = True
                hooks.release_target_environment(environment)
            else:
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_STARTED", {
                    "target_cycle": target_cycles,
                    "prospective_evolution": next_evolution,
                })

            evolved = hooks.evolve(
                target_direction, verification.verdict, verification.report,
                learning.diagnosis, memory, next_evolution)
            if not isinstance(evolved, EvolutionResult):
                raise SelfEvolvingLoopError(
                    "evolve must return EvolutionResult")
            memory = evolved.memory
            practice_projects += evolved.projects

            if (verification.verdict is TargetVerdict.PASS
                    and evolved.projects == 0):
                termination = (
                    "verifier_pass_curriculum_ready"
                    if evolved.status is EvolutionStatus.READY_FOR_RETRY
                    else "verifier_pass_curriculum_stalled")
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED", {
                    "target_cycle": target_cycles,
                    "projects": 0,
                    "reason": evolved.reason,
                    "termination": termination,
                })
                hooks.on_event("TARGET_LOOP_TERMINATED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "termination": termination,
                    "evolutions": evolutions,
                })
                transferred = True
                return SelfEvolvingLoopResult(
                    output=output,
                    environment=environment,
                    verifier_verdict=verification.verdict,
                    verifier_report=verification.report,
                    memory=memory,
                    target_cycles=target_cycles,
                    evolutions=evolutions,
                    practice_projects=practice_projects,
                    termination=termination,
                )

            if verification.verdict is TargetVerdict.PASS:
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED", {
                    "target_cycle": target_cycles,
                    "projects": evolved.projects,
                    "reason": evolved.reason,
                    "fresh_target_required": True,
                })
                released = True
                hooks.release_target_environment(environment)
            evolutions = next_evolution
            final_after_stall = evolved.status is EvolutionStatus.STALLED
            hooks.on_event(
                "READY_FOR_RETRY" if not final_after_stall
                else "CURRICULUM_STALLED_FINAL_TEST_REQUIRED",
                {
                    "target_cycle": target_cycles,
                    "evolution": evolutions,
                    "projects": evolved.projects,
                    "reason": evolved.reason,
                })
        finally:
            if not transferred and not released:
                primary_failure = sys.exc_info()[1]
                try:
                    hooks.release_target_environment(environment)
                except Exception as release_error:
                    if primary_failure is None:
                        raise
                    hooks.on_event("TARGET_RELEASE_FAILED", {
                        "target_cycle": target_cycles,
                        "error_type": type(release_error).__name__,
                        "error": str(release_error),
                        "primary_error_type": type(primary_failure).__name__,
                    })


__all__ = [
    "ActorLearning",
    "EvolutionResult",
    "EvolutionStatus",
    "Phase2StopPolicy",
    "SelfEvolvingLoopError",
    "SelfEvolvingLoopHooks",
    "SelfEvolvingLoopResult",
    "TargetVerdict",
    "TargetVerification",
    "run_self_evolving_loop",
]
