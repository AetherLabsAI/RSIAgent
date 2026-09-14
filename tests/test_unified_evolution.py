"""Contracts for the Curriculum-only READY_FOR_RETRY adapter."""

import json
from pathlib import Path
from types import SimpleNamespace

from config.settings import load
from explore import unified_evolution as U
from explore.charter import (
    self_evolving_curriculum_charter,
    unified_curriculum_pass_charter,
    unified_curriculum_route_charter,
)
from explore.practice_loop import CURRICULUM_HANDOFF, PracticeHooks


class _VM:
    def __init__(self):
        self.files = {}
        self.memory = {}

    def run_command(self, command, timeout=30, cap=4000):
        if command.startswith("rm -f -- "):
            path = command.split(";", 1)[0][len("rm -f -- "):].strip("'\"")
            self.files.pop(path, None)
            return "RSI_REMOVE_RC=0"
        if "RSI_PROJECT_SHAPE_RC" in command:
            return "RSI_PROJECT_SHAPE_RC=0"
        return ""


def _hooks(vm, decisions, seen_prompts, handoff_path=CURRICULUM_HANDOFF):
    pending = iter(decisions)

    def run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        assert passed_vm is vm
        seen_prompts.append(prompt)
        passed_vm.files[handoff_path] = next(pending)
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": '{"done": null}'},
        ]
        return SimpleNamespace(status="done"), history

    def push_memory(passed_vm, memory_dir):
        passed_vm.memory = U._read_memory_tree(memory_dir)
        return True

    return PracticeHooks(
        run_attempt=run_attempt,
        sink_factory=lambda _path: object(),
        reset_vm=lambda _vm, _target: {"ok": True},
        read_guest_text=lambda passed_vm, path: passed_vm.files.get(path, ""),
        capture_project=lambda *args, **kwargs: {"ok": True},
        audit_captured=lambda *args, **kwargs: [],
        audit_memory=lambda files, *args, **kwargs: dict(files),
        validate_corpus=lambda *args, **kwargs: {},
        audit_text=lambda *args, **kwargs: [],
        audit_transcripts=lambda *args, **kwargs: [],
        push_memory=push_memory,
    )


def test_unified_curriculum_contract_returns_only_ready_for_retry():
    prompt = self_evolving_curriculum_charter(
        "target query", latest_outcome="target verifier report",
        unified_retry=True)

    assert "DECISION: READY_FOR_RETRY" in prompt
    assert "DECISION: READY_FOR_TARGET_TEST" not in prompt
    assert "DECISION: CONVERGED" not in prompt
    assert "persistent target Verifier Agent" in prompt
    assert "official evaluator" in prompt


def test_target_failure_route_contract_separates_local_and_high_level_authority():
    prompt = unified_curriculum_route_charter(
        "target query", "evidence\nVERDICT: FAIL\n")

    assert "ROUTE: REVISE" in prompt
    assert "ROUTE: EVOLVE" in prompt
    assert "cannot change it to PASS" in prompt
    assert "official evaluator" in prompt
    assert "author a practice" in prompt and "project" in prompt


def test_target_pass_contract_separates_correctness_from_terminal_authority():
    prompt = unified_curriculum_pass_charter(
        "target query", "orientation evidence",
        "candidate evidence\nVERDICT: PASS\n")

    assert "ROUTE: HANDOFF" in prompt
    assert "ROUTE: VERIFY_MORE" in prompt
    assert "cannot inspect the candidate" in prompt
    assert "cannot directly" in prompt and "REVISE or EVOLVE" in prompt
    assert "official evaluator" in prompt
    assert "no checklist" in prompt.lower()


def test_same_curriculum_context_can_challenge_pass_then_authorize_handoff(
        tmp_path):
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        [
            "One requirement lacks support.\nROUTE: VERIFY_MORE\n",
            "The new report resolves it.\nROUTE: HANDOFF\n",
        ],
        prompts,
        handoff_path=U.CURRICULUM_ROUTE_HANDOFF,
    )
    session = U.UnifiedCurriculumSession()

    first = U.route_target_pass(
        vm, str(tmp_path / "routes"), "target query", "orientation evidence",
        "first support\nVERDICT: PASS\n", load(None),
        hooks=hooks, session=session)
    first_history = list(session.history)
    second = U.route_target_pass(
        vm, str(tmp_path / "routes"), "target query", "orientation evidence",
        "stronger support\nVERDICT: PASS\n", load(None),
        hooks=hooks, session=session)

    assert first.route == "VERIFY_MORE"
    assert second.route == "HANDOFF"
    assert session.history[:len(first_history)] == first_history
    assert session.target_passes == 2
    assert session.route_summaries == [
        "target PASS review 1: VERIFY_MORE",
        "target PASS review 2: HANDOFF",
    ]
    assert "first support" in session.history[0]["content"]
    assert "stronger support" in prompts[1]


def test_same_curriculum_context_routes_revise_then_evolve(tmp_path):
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        [
            "Local repair has highest value.\nROUTE: REVISE\n",
            "Practice search now has highest value.\nROUTE: EVOLVE\n",
        ],
        prompts,
        handoff_path=U.CURRICULUM_ROUTE_HANDOFF,
    )
    session = U.UnifiedCurriculumSession()

    first = U.route_target_failure(
        vm, str(tmp_path / "routes"), "target query",
        "first concrete failure\nVERDICT: FAIL\n", load(None),
        hooks=hooks, session=session)
    first_history = list(session.history)
    second = U.route_target_failure(
        vm, str(tmp_path / "routes"), "target query",
        "second concrete failure\nVERDICT: FAIL\n", load(None),
        hooks=hooks, session=session)

    assert first.route == "REVISE"
    assert second.route == "EVOLVE"
    assert session.history[:len(first_history)] == first_history
    assert session.target_failures == 2
    assert session.route_summaries == [
        "target failure 1: REVISE", "target failure 2: EVOLVE"]
    assert "first concrete failure" in session.history[0]["content"]
    assert "second concrete failure" in prompts[1]


def test_evolution_can_return_memory_for_retry_without_running_target(tmp_path):
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        ["DECISION: READY_FOR_RETRY\nCurrent memory is ready to falsify."],
        prompts,
    )
    initial = {"skill.md": b"existing memory"}

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        initial, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)

    assert result.status == "ready_for_retry"
    assert result.projects == 0
    assert U._read_memory_tree(result.memory_dir) == initial
    assert "READY_FOR_RETRY" in prompts[0]
    assert "READY_FOR_TARGET_TEST" not in prompts[0]
    state = json.loads(
        (Path(result.root) / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "ready_for_retry"


def test_phase2_pass_curriculum_reads_memory_and_can_skip_extra_practice(
        tmp_path):
    vm = _VM()
    prompts = []
    initial = {"skills/target.md": b"same Actor grounded PASS lesson"}
    hooks = _hooks(
        vm,
        ["DECISION: READY_FOR_TARGET\nNo contrast now has higher value."],
        prompts,
    )

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "independent candidate evidence\nVERDICT: PASS\n",
        "same Actor retained a scoped successful procedure",
        initial, load(None), load(None), load(None), load(None),
        trigger_authority="phase2_outcome_protocol",
        triggering_outcome="PASS",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)

    assert result.status == "ready_for_retry"
    assert result.projects == 0
    assert vm.memory == initial
    assert "CURRENT ACTOR-OWNED DURABLE MEMORY" in prompts[0]
    assert "DECISION: REWRITE" not in prompts[0]
    state = json.loads(
        (Path(result.root) / "state.json").read_text(encoding="utf-8"))
    assert state["triggering_outcome"] == "PASS"


def test_project_outcome_updates_memory_before_ready_for_retry(
        monkeypatch, tmp_path):
    vm = _VM()
    prompts = []
    hooks = _hooks(vm, [
        (
            "DECISION: PROJECT\nCreate a robust artifact entirely beneath "
            "/home/user/evolution_project."
        ),
        "DECISION: READY_FOR_RETRY\nThe practice evidence now justifies retry.",
    ], prompts)
    initial = {"skill.md": b"v0"}

    monkeypatch.setattr(U, "_run_practice_attempt", lambda **kwargs: {
        "terminal_outcome": "FAIL",
        "verifier_report": "VERDICT: FAIL\nmissing property",
        "actor_handoff": "STATUS: SUBMIT\npartial artifact",
        "actor_history": [{"role": "user", "content": "project"},
                          {"role": "assistant", "content": "attempt"}],
        "before_memory": initial,
    })

    def promote(**kwargs):
        learned = {"skill.md": b"v1 grounded by failure"}
        kwargs["hooks"].install_memory(str(kwargs["memory_dir"]), learned)
        return learned, "causal diagnosis", kwargs["actor_history"]

    monkeypatch.setattr(U, "_promote_learning", promote)

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        initial, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)

    assert result.status == "ready_for_retry"
    assert result.projects == 1
    assert U._read_memory_tree(result.memory_dir) == {
        "skill.md": b"v1 grounded by failure"}
    assert "missing property" in prompts[1]
    assert "causal diagnosis" in prompts[1]


def test_curriculum_stall_is_not_reinterpreted_as_readiness(tmp_path):
    vm = _VM()
    hooks = _hooks(
        vm, ["DECISION: STALLED\nNo productive hypothesis remains."], [])

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        {}, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)

    assert result.status == "stalled"
    assert result.status != "ready_for_retry"


def test_curriculum_context_is_paused_and_resumed_across_evolve_waves(tmp_path):
    vm = _VM()
    prompts = []
    received_histories = []
    decisions = iter((
        "DECISION: READY_FOR_RETRY\nFirst wave is ready.",
        "DECISION: READY_FOR_RETRY\nSecond wave is ready.",
    ))

    def run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        del cfg, sink
        assert passed_vm is vm
        prompts.append(prompt)
        received_histories.append(list(kwargs.get("initial_history") or []))
        passed_vm.files[CURRICULUM_HANDOFF] = next(decisions)
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": '{"done": null}'},
        ]
        return SimpleNamespace(status="done"), history

    hooks = _hooks(vm, [], [])
    hooks.run_attempt = run_attempt
    session = U.UnifiedCurriculumSession()
    cfg = load(None)

    first = U.evolve_until_ready(
        vm, str(tmp_path / "cycle1"), "target query",
        "first target evidence\nVERDICT: FAIL\n", "first diagnosis",
        {}, cfg, cfg, cfg, cfg,
        routing_report="first Curriculum route\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks,
        session=session)
    second = U.evolve_until_ready(
        vm, str(tmp_path / "cycle2"), "target query",
        "second target evidence\nVERDICT: FAIL\n", "second diagnosis",
        {}, cfg, cfg, cfg, cfg,
        routing_report="second Curriculum route\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks,
        session=session)

    assert first.status == second.status == "ready_for_retry"
    assert received_histories[0] == []
    assert received_histories[1] == session.history[:2]
    assert "first target evidence" in received_histories[1][0]["content"]
    assert "second target evidence" in prompts[1]
    assert session.waves == 2
