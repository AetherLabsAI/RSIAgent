"""State-machine contracts for the full task-local self-evolving benchmark."""

from dataclasses import dataclass, replace

import pytest

from core.self_evolving_loop import (
    ActorLearning,
    EvolutionResult,
    EvolutionStatus,
    SelfEvolvingLoopHooks,
    SelfEvolvingLoopError,
    TargetVerdict,
    TargetVerification,
    run_self_evolving_loop,
)


def test_pass_review_requires_actor_learning_before_any_environment():
    hooks, events, released, _ = _hooks([])
    with pytest.raises(SelfEvolvingLoopError, match="same-Actor PASS learning"):
        run_self_evolving_loop("target", {}, replace(hooks, curriculum_after_pass=True))
    assert events == released == []


def test_pass_before_new_practice_cannot_certify_a_later_failed_target():
    hooks, events, released, learned = _hooks(
        [TargetVerdict.PASS, TargetVerdict.FAIL, TargetVerdict.FAIL],
        [(EvolutionStatus.READY_FOR_RETRY, 1), (EvolutionStatus.STALLED, 1)])
    result = run_self_evolving_loop("target", {}, replace(
        hooks, learn_on_pass=True, curriculum_after_pass=True))
    assert result.termination == "curriculum_stalled_final_target_test"
    assert result.verifier_verdict is TargetVerdict.FAIL
    assert result.target_cycles == 3
    assert released == [1, 2]
    assert [verdict for _, verdict in learned] == [
        TargetVerdict.PASS, TargetVerdict.FAIL, TargetVerdict.FAIL]
    assert sum(name == "PASS_NEXT_EXPERIENCE_REVIEW_STARTED" for name, _ in events) == 1


def test_unverified_never_enters_pass_review_or_memory_learning():
    hooks, events, released, learned = _hooks([TargetVerdict.UNVERIFIED])
    result = run_self_evolving_loop("target", {}, replace(
        hooks, learn_on_pass=True, curriculum_after_pass=True))
    assert result.termination == "target_harness_unverified"
    assert result.memory == {}
    assert learned == released == []
    assert all("LEARNING" not in name and "REVIEW" not in name for name, _ in events)


@dataclass
class _Environment:
    cycle: int


@dataclass
class _Actor:
    cycle: int
    memory: dict


@dataclass
class _Verifier:
    cycle: int
    oriented: bool = False


def _hooks(verdicts, evolutions=()):
    verdicts = iter(verdicts)
    evolutions = iter(evolutions)
    events = []
    released = []
    learned_with = []

    def environment(_direction, cycle):
        return _Environment(cycle)

    def verifier(active_environment, _direction, cycle):
        assert active_environment.cycle == cycle
        return _Verifier(cycle)

    def orient(active_verifier, active_environment, _direction, cycle):
        assert active_verifier.cycle == active_environment.cycle == cycle
        assert not active_verifier.oriented
        active_verifier.oriented = True

    def actor(memory, cycle):
        return _Actor(cycle, dict(memory))

    def work(active_actor, active_verifier, active_environment, direction):
        assert active_actor.cycle == active_environment.cycle
        assert active_verifier.cycle == active_actor.cycle
        assert active_verifier.oriented
        return {"cycle": active_actor.cycle, "direction": direction}

    def verify(active_verifier, environment, _direction, output):
        assert active_verifier.oriented
        assert environment.cycle == output["cycle"]
        verdict = next(verdicts)
        return TargetVerification(
            verdict, f"cycle {environment.cycle}\nVERDICT: {verdict.value}")

    def learn(
            active_actor, environment, _direction, output, verdict,
            report, memory):
        assert active_actor.cycle == environment.cycle == output["cycle"]
        assert active_actor.memory == memory
        assert f"VERDICT: {verdict.value}" in report
        learned_with.append((active_actor, verdict))
        updated = dict(memory)
        updated[f"target_{environment.cycle}.md"] = b"target lesson"
        return ActorLearning(updated, "A grounded transferable diagnosis")

    def evolve(_direction, verdict, report, diagnosis, memory, index):
        assert f"VERDICT: {verdict.value}" in report
        assert diagnosis == "A grounded transferable diagnosis"
        expected = next(evolutions)
        if isinstance(expected, tuple):
            expected, projects = expected
        else:
            projects = index
        updated = dict(memory)
        if projects:
            updated[f"practice_{index}.md"] = b"practice lesson"
        return EvolutionResult(expected, updated, projects=projects)

    return SelfEvolvingLoopHooks(
        fresh_target_environment=environment,
        fresh_target_verifier=verifier,
        verifier_orient=orient,
        fresh_target_actor=actor,
        actor_work=work,
        verifier_verify=verify,
        actor_learn=learn,
        evolve=evolve,
        release_target_environment=lambda env: released.append(env.cycle),
        on_event=lambda event, payload: events.append((event, payload)),
    ), events, released, learned_with


def test_pass_transfers_live_state_without_evolution_or_early_release():
    hooks, events, released, learned_with = _hooks([TargetVerdict.PASS])

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass"
    assert result.verifier_verdict is TargetVerdict.PASS
    assert result.target_cycles == 1
    assert result.evolutions == result.practice_projects == 0
    assert result.memory == {}
    assert result.environment.cycle == 1
    assert released == []
    assert learned_with == []
    assert [event for event, _ in events] == [
        "TARGET_ORIENTATION_STARTED", "TARGET_ORIENTATION_COMPLETED",
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LOOP_TERMINATED"]


def test_fail_learns_then_practices_then_retries_with_fresh_actor_and_environment():
    hooks, events, released, learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.PASS],
        [EvolutionStatus.READY_FOR_RETRY])

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass"
    assert result.target_cycles == 2
    assert result.evolutions == 1
    assert result.practice_projects == 1
    assert set(result.memory) == {"target_1.md", "practice_1.md"}
    assert result.environment.cycle == 2
    assert released == [1]
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.FAIL)]
    assert "EVOLUTION_STARTED" in [event for event, _ in events]
    assert "READY_FOR_RETRY" in [event for event, _ in events]


def test_curriculum_stalled_requests_exactly_one_final_fresh_target_test():
    hooks, _events, released, learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.FAIL],
        [EvolutionStatus.STALLED])

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "curriculum_stalled_final_target_test"
    assert result.verifier_verdict is TargetVerdict.FAIL
    assert result.target_cycles == 2
    assert result.evolutions == 1
    assert result.practice_projects == 1
    assert set(result.memory) == {
        "target_1.md", "practice_1.md", "target_2.md"}
    assert result.environment.cycle == 2
    assert released == [1]
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.FAIL), (2, TargetVerdict.FAIL)]


def test_opt_in_pass_learns_before_transferring_live_state():
    hooks, events, released, learned_with = _hooks([TargetVerdict.PASS])
    hooks = SelfEvolvingLoopHooks(
        **{**hooks.__dict__, "learn_on_pass": True})

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass"
    assert result.memory == {"target_1.md": b"target lesson"}
    assert result.environment.cycle == 1
    assert released == []
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS)]
    assert [event for event, _ in events] == [
        "TARGET_ORIENTATION_STARTED", "TARGET_ORIENTATION_COMPLETED",
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LEARNING_STARTED",
        "TARGET_LEARNING_COMPLETED", "TARGET_LOOP_TERMINATED"]


def test_phase2_pass_learning_then_curriculum_can_return_without_practice():
    hooks, events, released, learned_with = _hooks(
        [TargetVerdict.PASS],
        [(EvolutionStatus.READY_FOR_RETRY, 0)])
    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "learn_on_pass": True,
            "curriculum_after_pass": True,
        })

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass_curriculum_ready"
    assert result.target_cycles == 1
    assert result.evolutions == result.practice_projects == 0
    assert result.memory == {"target_1.md": b"target lesson"}
    assert released == []
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS)]
    names = [event for event, _ in events]
    assert "PASS_NEXT_EXPERIENCE_REVIEW_STARTED" in names
    assert "PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED" in names


def test_phase2_pass_can_select_contrast_then_requires_fresh_target():
    hooks, _events, released, learned_with = _hooks(
        [TargetVerdict.PASS, TargetVerdict.PASS],
        [
            (EvolutionStatus.READY_FOR_RETRY, 1),
            (EvolutionStatus.READY_FOR_RETRY, 0),
        ])
    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "learn_on_pass": True,
            "curriculum_after_pass": True,
        })

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass_curriculum_ready"
    assert result.target_cycles == 2
    assert result.evolutions == result.practice_projects == 1
    assert set(result.memory) == {
        "target_1.md", "practice_1.md", "target_2.md"}
    assert released == [1]
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS), (2, TargetVerdict.PASS)]


def test_unverified_is_not_mislabeled_as_pass_or_evolution_evidence():
    hooks, _events, released, learned_with = _hooks([TargetVerdict.UNVERIFIED])

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "target_harness_unverified"
    assert result.verifier_verdict is TargetVerdict.UNVERIFIED
    assert result.evolutions == 0
    assert released == []
    assert learned_with == []


def test_infrastructure_exception_propagates_and_releases_target_environment():
    hooks, _events, released, _learned_with = _hooks([TargetVerdict.PASS])

    def broken_work(*_args):
        raise ConnectionError("transport outage")

    hooks = SelfEvolvingLoopHooks(
        **{**hooks.__dict__, "actor_work": broken_work})
    with pytest.raises(ConnectionError, match="transport outage"):
        run_self_evolving_loop("target", {}, hooks)
    assert released == [1]


def test_orientation_precedes_actor_and_is_required_each_fresh_target_cycle():
    hooks, events, _released, _learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.PASS],
        [EvolutionStatus.READY_FOR_RETRY])

    run_self_evolving_loop("target", {}, hooks)

    names = [event for event, _ in events]
    assert names.count("TARGET_ORIENTATION_STARTED") == 2
    assert names.count("TARGET_ORIENTATION_COMPLETED") == 2
    first_orientation = names.index("TARGET_ORIENTATION_COMPLETED")
    first_actor = names.index("TARGET_STARTED")
    assert first_orientation < first_actor


def test_clean_protocol_does_not_require_pre_actor_orientation():
    hooks, events, released, _learned_with = _hooks([TargetVerdict.PASS])

    def work_without_orientation(
            active_actor, active_verifier, active_environment, direction):
        assert active_actor.cycle == active_environment.cycle
        assert active_verifier.cycle == active_actor.cycle
        assert not active_verifier.oriented
        return {"cycle": active_actor.cycle, "direction": direction}

    def verify_without_orientation(
            active_verifier, environment, _direction, output):
        assert not active_verifier.oriented
        assert environment.cycle == output["cycle"]
        return TargetVerification(
            TargetVerdict.PASS, "independent evidence\nVERDICT: PASS")

    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "verifier_orient": None,
            "actor_work": work_without_orientation,
            "verifier_verify": verify_without_orientation,
        })
    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "verifier_pass"
    assert released == []
    assert [event for event, _ in events] == [
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LOOP_TERMINATED"]


def test_protocol_contains_no_external_evaluator_hook():
    assert "evaluator" not in SelfEvolvingLoopHooks.__dataclass_fields__
    assert "score" not in SelfEvolvingLoopHooks.__dataclass_fields__
