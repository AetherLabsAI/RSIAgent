"""On-demand self-evolving orchestration for the unified agent harness.

The loop deliberately contains no benchmark evaluator and no numeric convergence
policy. A persistent target Verifier Agent owns local correctness (PASS / FAIL),
while a persistent Curriculum Agent owns every high-level transition:

``HANDOFF``
    A Verifier PASS is necessary but does not terminate alone. The Curriculum Agent
    audits the report's sufficiency and either authorizes HANDOFF or asks that same
    Verifier Agent to investigate further. A sealed evaluator, when one exists, is a
    caller concern and cannot feed this loop.
``REVISE``
    Return the complete report to the same Actor Agent in the same environment.
``EVOLVE``
    End target work, let that same Actor Agent learn from the complete Verifier
    report, discard the target Actor/environment, invoke the existing self-evolving
    pipeline, and retry the original query with its returned memory. The target
    Verifier Agent and Curriculum Agent are retained.

Concrete runtimes are dependency-injected through :class:`UnifiedLoopHooks`.  This
keeps the state machine reusable while letting OSWorld preserve its own reset,
memory-audit, provenance, and leakage boundaries.

Before the first Actor action, the primary target Verifier freely studies the trusted
task-start state and preserves any evidence it judges useful. A different Verifier
model activated by Actor escalation must independently orient on a matched fresh S0
clone before seeing the live candidate; model-private histories are never copied. A
Verifier may freely use code to understand S0, but the harness does not designate,
freeze, or automatically run any private artifact as an evaluator. Orientation is
separate from candidate evaluation: there is no candidate and it cannot emit PASS /
FAIL or any lifecycle route.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class UnifiedLoopError(RuntimeError):
    """The Agent lifecycle produced an invalid transport-level state."""


class VerifierRoute(str, Enum):
    """Mechanical orchestration route after local verification and routing."""

    HANDOFF = "HANDOFF"
    REVISE = "REVISE"
    EVOLVE = "EVOLVE"


@dataclass(frozen=True)
class Verification:
    """One complete target Verifier report and optional Curriculum route report."""

    route: VerifierRoute
    report: str
    curriculum_report: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.route, VerifierRoute):
            try:
                object.__setattr__(
                    self, "route", VerifierRoute(str(self.route).strip().upper()))
            except ValueError as exc:
                raise UnifiedLoopError(
                    f"unknown Verifier Agent route: {self.route!r}") from exc
        if not isinstance(self.report, str) or not self.report.strip():
            raise UnifiedLoopError("Verifier Agent report must be complete and nonempty")
        if (self.route in {VerifierRoute.HANDOFF, VerifierRoute.EVOLVE}
                and (not isinstance(self.curriculum_report, str)
                     or not self.curriculum_report.strip())):
            raise UnifiedLoopError(
                f"{self.route.value} requires the complete Curriculum Agent "
                "routing report")


@dataclass(frozen=True)
class ActorLearning:
    """The same target Actor Agent's terminal learning product after EVOLVE."""

    memory: Any
    diagnosis: str

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis, str) or not self.diagnosis.strip():
            raise UnifiedLoopError(
                "target Actor Agent learning diagnosis must be complete and nonempty")


@dataclass(frozen=True)
class UnifiedLoopResult:
    """A live candidate authorized for handoff and the memory that produced it.

    The caller owns ``environment`` after return.  This lets a sealed external
    evaluator inspect the exact accepted state before the caller releases it.
    """

    output: Any
    environment: Any
    verifier_report: str
    curriculum_report: str
    memory: Any
    target_cycles: int
    revisions: int
    evolutions: int


def _noop_event(_event: str, _payload: dict[str, Any]) -> None:
    pass


def _noop_release(_environment: Any) -> None:
    pass


@dataclass(frozen=True)
class UnifiedLoopHooks:
    """Runtime operations needed by :func:`run_unified_loop`.

    ``evolve`` is the existing Curriculum Agent -> (Actor Agent -> Verifier
    Agent) pipeline.  It must return only after the Curriculum Agent declares the
    memory ready for a fresh retry; abnormal infrastructure termination should
    raise instead of manufacturing readiness.
    """

    new_target_verifier: Callable[[str], Any]
    fresh_target_environment: Callable[[str], Any]
    verifier_orient: Callable[[Any, Any, str, int], Any]
    fresh_target_actor: Callable[[Any], Any]
    actor_work: Callable[[Any, Any, str], Any]
    verifier_verify: Callable[[Any, Any, str, Any], Verification]
    actor_receive: Callable[[Any, str], None]
    actor_learn: Callable[
        [Any, Any, str, Any, str, Any], ActorLearning]
    evolve: Callable[[str, str, str, Any, str], Any]
    release_target_environment: Callable[[Any], None] = _noop_release
    on_event: Callable[[str, dict[str, Any]], None] = _noop_event


def run_unified_loop(
        query: str, current_memory: Any, hooks: UnifiedLoopHooks,
        ) -> UnifiedLoopResult:
    """Run until Verifier PASS and Curriculum HANDOFF authorize termination.

    This is intentionally an iterative state machine, even though improved memory
    recursively becomes the capability input to another attempt.  It therefore has
    no call-stack recursion and no harness-authored iteration/convergence cap.
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be nonempty")

    memory = current_memory
    verifier = hooks.new_target_verifier(query)
    if verifier is None:
        raise UnifiedLoopError("target Verifier Agent was not created")

    target_cycles = 0
    revisions = 0
    evolutions = 0

    while True:
        target_cycles += 1
        environment = hooks.fresh_target_environment(query)
        if environment is None:
            raise UnifiedLoopError("fresh target environment was not created")
        hooks.on_event("TARGET_ORIENTATION_STARTED", {
            "target_cycle": target_cycles,
            "evolutions": evolutions,
        })
        handed_off = False
        environment_released = False
        try:
            # The task setup is complete, but no Actor context or action exists yet.
            # This ordering is the trusted task-start barrier: the Verifier may
            # inspect authoritative inputs without seeing an Actor-shaped candidate.
            hooks.verifier_orient(
                verifier, environment, query, target_cycles)
            hooks.on_event("TARGET_ORIENTATION_COMPLETED", {
                "target_cycle": target_cycles,
                "evolutions": evolutions,
            })

            actor = hooks.fresh_target_actor(memory)
            if actor is None:
                raise UnifiedLoopError(
                    "fresh target Actor Agent was not created")
            hooks.on_event("TARGET_STARTED", {
                "target_cycle": target_cycles,
                "evolutions": evolutions,
            })
            while True:
                output = hooks.actor_work(actor, environment, query)
                decision = hooks.verifier_verify(
                    verifier, environment, query, output)
                if not isinstance(decision, Verification):
                    raise UnifiedLoopError(
                        "verifier_verify must return Verification")

                hooks.on_event("TARGET_VERIFIED", {
                    "target_cycle": target_cycles,
                    "route": decision.route.value,
                })

                if decision.route is VerifierRoute.HANDOFF:
                    hooks.on_event("HANDOFF", {
                        "target_cycle": target_cycles,
                        "revisions": revisions,
                        "evolutions": evolutions,
                    })
                    handed_off = True
                    return UnifiedLoopResult(
                        output=output,
                        environment=environment,
                        verifier_report=decision.report,
                        curriculum_report=decision.curriculum_report,
                        memory=memory,
                        target_cycles=target_cycles,
                        revisions=revisions,
                        evolutions=evolutions,
                    )

                if decision.route is VerifierRoute.REVISE:
                    revisions += 1
                    hooks.actor_receive(actor, decision.report)
                    hooks.on_event("TARGET_REVISION_REQUESTED", {
                        "target_cycle": target_cycles,
                        "revision": revisions,
                    })
                    continue

                # EVOLVE ends target work but not this Actor Agent's ownership of
                # its experience. It first receives the complete Verifier report
                # in a terminal learning-only phase. Only then are ``actor`` and
                # ``environment`` discarded. ``verifier`` remains the persistent
                # target Verifier Agent created above.
                evolutions += 1
                hooks.on_event("EVOLUTION_STARTED", {
                    "target_cycle": target_cycles,
                    "evolution": evolutions,
                })
                learning = hooks.actor_learn(
                    actor, environment, query, output, decision.report, memory)
                if not isinstance(learning, ActorLearning):
                    raise UnifiedLoopError(
                        "actor_learn must return ActorLearning")
                memory = learning.memory
                hooks.on_event("TARGET_LEARNING_COMPLETED", {
                    "target_cycle": target_cycles,
                    "evolution": evolutions,
                })
                environment_released = True
                hooks.release_target_environment(environment)
                memory = hooks.evolve(
                    query, decision.report, learning.diagnosis, memory,
                    decision.curriculum_report)
                hooks.on_event("READY_FOR_RETRY", {
                    "target_cycle": target_cycles,
                    "evolution": evolutions,
                })
                break
        finally:
            # The sealed evaluator, if any, needs the exact accepted live state.
            # Ownership transfers to the caller on HANDOFF; every abandoned target
            # cycle is released here.
            if not handed_off and not environment_released:
                primary_failure = sys.exc_info()[1]
                try:
                    hooks.release_target_environment(environment)
                except Exception as release_error:
                    if primary_failure is None:
                        raise
                    # Once another failure has already made the live environment
                    # unusable, preservation is best-effort. Never replace the
                    # causal exception with a cleanup exception; retain the latter
                    # as an explicit infrastructure event for diagnosis.
                    hooks.on_event("TARGET_RELEASE_FAILED", {
                        "target_cycle": target_cycles,
                        "error_type": type(release_error).__name__,
                        "error": str(release_error),
                        "primary_error_type": type(primary_failure).__name__,
                    })


__all__ = [
    "ActorLearning",
    "UnifiedLoopError",
    "UnifiedLoopHooks",
    "UnifiedLoopResult",
    "Verification",
    "VerifierRoute",
    "run_unified_loop",
]
