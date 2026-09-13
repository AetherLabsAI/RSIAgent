"""Executable contracts for the on-demand self-evolving state machine."""

import dataclasses
import tempfile

import pytest

from config.settings import load
from core import loop as actor_loop
from core.trace import ArtifactSink
from core.unified_loop import (
    ActorLearning,
    UnifiedLoopError,
    UnifiedLoopHooks,
    Verification,
    VerifierRoute,
    run_unified_loop,
)
from env.vm import Trace


class _Actor:
    def __init__(self, memory):
        self.memory = memory
        self.reports = []
        self.works = 0


def _harness(routes):
    state = {
        "actors": [],
        "environments": [],
        "verifiers": [],
        "released": [],
        "actor_learning_inputs": [],
        "evolve_inputs": [],
        "events": [],
        "order": [],
    }
    pending = iter(routes)

    def new_verifier(query):
        verifier = {"query": query, "inspections": 0}
        state["verifiers"].append(verifier)
        return verifier

    def fresh_environment(query):
        environment = {"query": query, "cycle": len(state["environments"]) + 1}
        state["environments"].append(environment)
        state["order"].append(("environment", environment["cycle"]))
        return environment

    def orient(verifier, environment, query, target_cycle):
        assert verifier is state["verifiers"][0]
        assert query == "original query"
        assert environment["cycle"] == target_cycle
        state["order"].append(("orientation", target_cycle))

    def fresh_actor(memory):
        actor = _Actor(memory)
        state["actors"].append(actor)
        state["order"].append(("actor", len(state["actors"])))
        return actor

    def work(actor, environment, query):
        actor.works += 1
        return {
            "actor": actor,
            "environment": environment,
            "query": query,
            "attempt": actor.works,
        }

    def verify(verifier, environment, query, output):
        verifier["inspections"] += 1
        route = next(pending)
        return Verification(
            route, f"complete report for {route}",
            (f"complete Curriculum report for {route}"
             if route in {VerifierRoute.HANDOFF, VerifierRoute.EVOLVE}
             else ""))

    def receive(actor, report):
        actor.reports.append(report)

    def learn(actor, environment, query, output, report, memory):
        state["actor_learning_inputs"].append(
            (actor, environment, query, output, report, memory,
             environment in state["released"]))
        return ActorLearning(memory, f"diagnosis-{len(state['actor_learning_inputs'])}")

    def evolve(query, report, diagnosis, memory, curriculum_report):
        state["evolve_inputs"].append(
            (query, report, diagnosis, memory, curriculum_report))
        return tuple(memory) + (f"memory-v{len(state['evolve_inputs'])}",)

    hooks = UnifiedLoopHooks(
        new_target_verifier=new_verifier,
        fresh_target_environment=fresh_environment,
        verifier_orient=orient,
        fresh_target_actor=fresh_actor,
        actor_work=work,
        verifier_verify=verify,
        actor_receive=receive,
        actor_learn=learn,
        evolve=evolve,
        release_target_environment=state["released"].append,
        on_event=lambda event, payload: state["events"].append((event, payload)),
    )
    return hooks, state


def test_revise_preserves_actor_environment_and_verifier():
    hooks, state = _harness((VerifierRoute.REVISE, VerifierRoute.HANDOFF))

    result = run_unified_loop("original query", (), hooks)

    assert len(state["actors"]) == 1
    assert len(state["environments"]) == 1
    assert len(state["verifiers"]) == 1
    assert state["actors"][0].works == 2
    assert len(state["actors"][0].reports) == 1
    assert state["verifiers"][0]["inspections"] == 2
    assert state["released"] == []
    assert result.environment is state["environments"][0]
    assert result.revisions == 1 and result.evolutions == 0
    assert state["order"][:3] == [
        ("environment", 1), ("orientation", 1), ("actor", 1)]


def test_evolve_refreshes_actor_and_environment_but_preserves_verifier():
    hooks, state = _harness((VerifierRoute.EVOLVE, VerifierRoute.HANDOFF))

    result = run_unified_loop("original query", (), hooks)

    assert len(state["actors"]) == 2
    assert len(state["environments"]) == 2
    assert len(state["verifiers"]) == 1
    assert state["verifiers"][0]["inspections"] == 2
    assert state["actors"][0] is not state["actors"][1]
    assert state["environments"][0] is not state["environments"][1]
    assert state["actors"][1].memory == ("memory-v1",)
    assert state["released"] == [state["environments"][0]]
    assert state["actor_learning_inputs"][0][-1] is False
    assert state["evolve_inputs"][0][2] == "diagnosis-1"
    assert "Curriculum report" in state["evolve_inputs"][0][4]
    assert result.environment is state["environments"][1]
    assert result.memory == ("memory-v1",)
    assert result.target_cycles == 2 and result.evolutions == 1
    assert state["order"] == [
        ("environment", 1), ("orientation", 1), ("actor", 1),
        ("environment", 2), ("orientation", 2), ("actor", 2),
    ]


def test_repeated_evolution_is_iterative_recursive_improvement():
    hooks, state = _harness((
        VerifierRoute.EVOLVE,
        VerifierRoute.EVOLVE,
        VerifierRoute.HANDOFF,
    ))

    result = run_unified_loop("original query", (), hooks)

    assert len(state["verifiers"]) == 1
    assert len(state["actors"]) == len(state["environments"]) == 3
    assert state["released"] == state["environments"][:2]
    assert result.environment is state["environments"][2]
    assert [entry[3] for entry in state["evolve_inputs"]] == [
        (), ("memory-v1",),
    ]
    assert result.memory == ("memory-v1", "memory-v2")
    assert result.target_cycles == 3 and result.evolutions == 2
    assert [event for event, _ in state["events"]].count(
        "READY_FOR_RETRY") == 2


def test_release_failure_never_masks_the_primary_target_failure():
    hooks, state = _harness((VerifierRoute.HANDOFF,))

    def fail_work(_actor, _environment, _query):
        raise ValueError("causal target failure")

    def fail_release(_environment):
        raise RuntimeError("secondary scratch preservation failure")

    hooks = dataclasses.replace(
        hooks, actor_work=fail_work,
        release_target_environment=fail_release)

    try:
        run_unified_loop("original query", (), hooks)
    except ValueError as exc:
        assert str(exc) == "causal target failure"
    else:
        raise AssertionError("primary target failure was swallowed")

    release_events = [
        payload for event, payload in state["events"]
        if event == "TARGET_RELEASE_FAILED"]
    assert len(release_events) == 1
    assert release_events[0]["primary_error_type"] == "ValueError"
    assert release_events[0]["error_type"] == "RuntimeError"


def test_external_grader_is_not_a_unified_loop_hook():
    assert "grader" not in UnifiedLoopHooks.__dataclass_fields__
    assert "evaluate" not in UnifiedLoopHooks.__dataclass_fields__


def test_verifier_pass_alone_cannot_construct_terminal_handoff():
    with pytest.raises(
            UnifiedLoopError,
            match="HANDOFF requires the complete Curriculum Agent"):
        Verification(
            VerifierRoute.HANDOFF,
            "local evidence\nVERDICT: PASS\n")


def test_actor_harness_surfaces_evolve_without_accepting_candidate(monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        (
            '{"done":{"checks":[{"desc":"candidate exists",'
            '"probe":"test -e /tmp/candidate && echo PASS"}]}}'
        ),
    ))

    monkeypatch.setattr(
        actor_loop, "chat", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(
        actor_loop, "verify_independent",
        lambda *args, **kwargs: (
            "evolve", "complete unmet-requirement evidence\nROUTE: EVOLVE\n"))

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="unified-route-")))

    assert result.status == "evolve"
    assert result.verifier_route == "EVOLVE"
    assert result.verifier_report.endswith("ROUTE: EVOLVE\n")


def test_actor_harness_surfaces_explicit_handoff_report(monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        (
            '{"done":{"checks":[{"desc":"candidate exists",'
            '"probe":"test -e /tmp/candidate && echo PASS"}]}}'
        ),
    ))
    monkeypatch.setattr(
        actor_loop, "chat", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(
        actor_loop, "verify_independent",
        lambda *args, **kwargs: (
            "pass", "complete supporting evidence\nROUTE: HANDOFF\n"))

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="unified-handoff-")))

    assert result.status == "done"
    assert result.verifier_route == "HANDOFF"
    assert result.verifier_report.endswith("ROUTE: HANDOFF\n")


def test_separated_curriculum_routes_local_fail_without_changing_verifier_contract(
        monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        (
            '{"done":{"checks":[{"desc":"candidate exists",'
            '"probe":"test -e /tmp/candidate && echo PASS"}]}}'
        ),
    ))
    monkeypatch.setattr(
        actor_loop, "chat", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(
        actor_loop, "verify_independent",
        lambda *args, **kwargs: (
            "wrong", "concrete unmet requirement\nVERDICT: FAIL\n"))

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    seen = []

    def route(report):
        seen.append(report)
        return "EVOLVE", "high-level search warranted\nROUTE: EVOLVE\n"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="separated-route-")),
        verifier_failure_router=route)

    assert seen == ["concrete unmet requirement\nVERDICT: FAIL\n"]
    assert result.status == "evolve"
    assert result.verifier_route == "EVOLVE"
    assert result.verifier_report.endswith("VERDICT: FAIL\n")
    assert result.curriculum_route == "EVOLVE"
    assert result.curriculum_report.endswith("ROUTE: EVOLVE\n")


def test_curriculum_verify_more_keeps_candidate_and_same_verifier_then_handoffs(
        monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        (
            '{"done":{"checks":[{"desc":"candidate exists",'
            '"probe":"test -e /tmp/candidate && echo PASS"}]}}'
        ),
    ))
    monkeypatch.setattr(
        actor_loop, "chat", lambda *args, **kwargs: next(replies))
    verifier_calls = []

    def verify(*args, **kwargs):
        verifier_calls.append(kwargs)
        number = len(verifier_calls)
        return "pass", f"local evidence report {number}\nVERDICT: PASS\n"

    monkeypatch.setattr(actor_loop, "verify_independent", verify)

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    seen = []
    routes = iter((
        ("VERIFY_MORE", "Resolve omitted evidence.\nROUTE: VERIFY_MORE\n"),
        ("HANDOFF", "The report is now sufficient.\nROUTE: HANDOFF\n"),
    ))

    def review(report):
        seen.append(report)
        return next(routes)

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="pass-review-")),
        verifier_pass_router=review)

    assert len(verifier_calls) == 2
    # The opt-in continuation surface is absent on the incumbent first call, so
    # ordinary/custom Verifiers remain compatible.
    assert "continue_candidate" not in verifier_calls[0]
    assert verifier_calls[1]["continue_candidate"] is True
    assert "VERIFY_MORE" in verifier_calls[1]["curriculum_review"]
    assert seen == [
        "local evidence report 1\nVERDICT: PASS\n",
        "local evidence report 2\nVERDICT: PASS\n",
    ]
    assert result.status == "done"
    assert result.verifier_route == "HANDOFF"
    assert result.curriculum_route == "HANDOFF"
    assert result.curriculum_report.endswith("ROUTE: HANDOFF\n")


def test_verify_more_can_reveal_fail_then_curriculum_routes_evolve(monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        (
            '{"done":{"checks":[{"desc":"candidate exists",'
            '"probe":"test -e /tmp/candidate && echo PASS"}]}}'
        ),
    ))
    monkeypatch.setattr(
        actor_loop, "chat", lambda *args, **kwargs: next(replies))
    verdicts = iter((
        ("pass", "initial support\nVERDICT: PASS\n"),
        ("wrong", "new concrete violation\nVERDICT: FAIL\n"),
    ))
    verifier_calls = []

    def verify(*args, **kwargs):
        verifier_calls.append(kwargs)
        return next(verdicts)

    monkeypatch.setattr(actor_loop, "verify_independent", verify)

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    pass_reports = []
    failure_reports = []

    def review_pass(report):
        pass_reports.append(report)
        return "VERIFY_MORE", "Check the unsupported claim.\nROUTE: VERIFY_MORE\n"

    def route_failure(report):
        failure_reports.append(report)
        return "EVOLVE", "Capability search is warranted.\nROUTE: EVOLVE\n"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="pass-to-evolve-")),
        verifier_failure_router=route_failure,
        verifier_pass_router=review_pass)

    assert len(verifier_calls) == 2
    assert verifier_calls[1]["continue_candidate"] is True
    assert pass_reports == ["initial support\nVERDICT: PASS\n"]
    assert failure_reports == ["new concrete violation\nVERDICT: FAIL\n"]
    assert result.status == "evolve"
    assert result.verifier_route == "EVOLVE"
    assert result.verifier_report.endswith("VERDICT: FAIL\n")
    assert result.curriculum_route == "EVOLVE"
    assert result.curriculum_report.endswith("ROUTE: EVOLVE\n")


def test_target_verifier_session_registry_survives_fresh_target_cycles(
        monkeypatch, tmp_path):
    cfg = load(None)
    registry = {}
    seen = []
    runtime = {}

    def fake_attempt(instruction, vm, passed_cfg, sink, **kwargs):
        seen.append(kwargs["verifier_session"])
        return actor_loop.LoopResult(status="done"), []

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    sink = ArtifactSink(str(tmp_path / "run"))

    actor_loop.run_with_resume(
        "query", object(), cfg, sink, verifier_sessions=registry,
        runtime_state=runtime)
    actor_loop.run_with_resume(
        "query", object(), cfg, sink, verifier_sessions=registry)

    assert len(registry) == 1
    assert seen[0] is seen[1] is next(iter(registry.values()))
    assert runtime["active_cfg"] is cfg
    assert runtime["active_history"] == []


def test_resume_runtime_identifies_the_active_escalated_actor_context(
        monkeypatch, tmp_path):
    cfg = load(None)
    cfg.max_iters = 100
    cfg.wall_clock_secs = 3600
    cfg.max_resumes = 1
    cfg.escalation_model = "escalated-actor"
    cfg.resume_synthesize_worklog = False
    cfg.strategy_pivot = False
    calls = []

    def fake_attempt(instruction, vm, passed_cfg, sink, **kwargs):
        del instruction, vm, sink, kwargs
        calls.append(passed_cfg)
        if len(calls) == 1:
            return actor_loop.LoopResult(
                status="stalled", iters=5, turns=2, programs_run=1,
                looks=0, wall_secs=5, inspections=[], worklog="banked",
                surface_delta="changed"), [
                    {"role": "user", "content": "primary"},
                    {"role": "assistant", "content": "work"},
                ]
        return actor_loop.LoopResult(
            status="done", iters=2, turns=1, programs_run=1,
            looks=0, wall_secs=2, inspections=[]), [
                {"role": "user", "content": "escalated"},
                {"role": "assistant", "content": "finish"},
            ]

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    runtime = {}
    actor_loop.run_with_resume(
        "query", object(), cfg,
        ArtifactSink(str(tmp_path / "run")), runtime_state=runtime)

    assert len(calls) == 2
    assert runtime["active_cfg"].model == "escalated-actor"
    assert runtime["active_history"][0]["content"] == "escalated"
