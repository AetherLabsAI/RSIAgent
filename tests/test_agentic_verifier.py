#!/usr/bin/env python3
"""Opt-in effect-isolated Agent verifier transport and continuity tests."""
import json
from types import SimpleNamespace

import pytest

from config.settings import Config
from core import loop as L
from core import verifier as V
from core.trace import ArtifactSink
from core.verifier_runtime import AgenticVerifierNoProgressError
from explore.charter import (
    agent_harness_verifier_charter,
    agent_harness_verifier_orientation_charter,
)
from llm.client import durable_user_message


def test_report_parser_is_transport_only_and_unambiguous():
    report = """# Investigation
```text
VERDICT: FAIL
```
> VERDICT: FAIL

Evidence was gathered independently.
VERDICT: PASS
"""
    assert V._parse_agentic_verifier_report(report) == "pass"
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: PASS\n") == "pass"
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: FAIL\n") is None
    assert V._parse_agentic_verifier_report("no token") is None


@pytest.mark.parametrize("mode", ["binary", "evidence", "route"])
def test_recurrent_replay_rehydrates_authority_until_an_action_anchors_it(
        monkeypatch, tmp_path, mode):
    """The first real action can follow a dry reply to the task opening."""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    task = "Preserve unrelated appointments. Move only the named review to Tuesday."
    context = "Candidate: /candidate. Frozen original: /original."
    calls = []
    action = '{"program":{"lang":"bash","code":"cat /candidate"}}'
    dry = "I will inspect the candidate."
    observation = "Exact independent probe result: original value is unchanged."

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append((prompt, list(history)))
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": dry},
                {"role": "user", "content": "Emit one real action now."},
                {"role": "assistant", "content": action},
                {"role": "user", "content": observation},
                {"role": "assistant", "content": dry},
            ]
        elif len(calls) == 2:
            # This repeated dry response activates semantic replay, which removes
            # the first user/dry-assistant pair containing the authority envelope.
            history += [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": dry}]
        else:
            assert task in prompt
            assert context in prompt
            assert "AUTHORITATIVE TASK — verbatim" in prompt
            assert observation in prompt
            assert [m["content"] for m in history
                    if m["role"] == "assistant"] == [action]
            assert kwargs["continue_context"] is True
            if len(calls) == 3:
                # Another dry segment must not mark the restored opening as
                # anchored until a real action includes it in the checkpoint.
                history += [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": dry}]
            else:
                report = ("Independent findings.\nROUTE: HANDOFF\n"
                          if mode == "route" else
                          "Independent findings.\nVERDICT: PASS\n")
                kwargs["program_executor"]("verifier-report", report)
                assert kwargs["terminal_handoff_ready"]()
                history += [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content":
                     '{"program":{"lang":"verifier-report","code":"publish"}}'},
                ]
                sink.save_transcript("system", history)
                return SimpleNamespace(status="done"), history
        sink.save_transcript("system", history)
        return SimpleNamespace(status="stalled"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    control = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=mode == "route",
        verifier_unverified_evidence=mode == "evidence")
    verdict, _ = V.verify_agentic(
        task, _VM(), control, context=context,
        sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)
    assert verdict == "pass" and len(calls) == 4
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_001" /
                           "recovery.json").read_text())
    assert recovery["semantic_replay_activated"] is True


def test_unverified_verdict_is_configured_transport_not_legacy_alias():
    report = "# Evidence gap\nVERDICT: UNVERIFIED\n"
    assert V._parse_agentic_verifier_report(report) is None
    assert V._parse_agentic_verifier_report(
        report, allow_unverified=True) == "unverified"
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: UNVERIFIED\n",
        allow_unverified=True) is None


def test_unified_report_parser_accepts_idempotent_duplicates_not_conflicts():
    assert V._parse_agentic_verifier_report(
        "# Evidence\nROUTE: HANDOFF\n", allow_evolve=True) == "pass"
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\n", allow_evolve=True) == "wrong"
    assert V._parse_agentic_verifier_report(
        "ROUTE: EVOLVE\n", allow_evolve=True) == "evolve"
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\nEvidence.\nROUTE: REVISE\n",
        allow_evolve=True) == "wrong"
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\nROUTE: EVOLVE\n", allow_evolve=True) is None
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\n", allow_evolve=True) is None


def test_orientation_parser_accepts_idempotent_duplicates_not_conflicts():
    assert V._parse_agentic_orientation_report(
        "STAGE: ORIENTATION_READY\nSTAGE: ORIENTATION_READY\n") == \
        "orientation_ready"
    assert V._parse_agentic_orientation_report(
        r"Orientation complete.\n\nSTAGE: ORIENTATION_READY") == \
        "orientation_ready"
    assert V._parse_agentic_orientation_report("no stage") is None


def test_report_parser_treats_literal_escaped_newlines_as_transport_lines():
    assert V._parse_agentic_verifier_report(
        r"Evidence complete.\nVERDICT: PASS") == "pass"
    assert V._parse_agentic_verifier_report(
        r"Evidence complete.\r\nROUTE: REVISE", allow_evolve=True) == "wrong"


class _VM:
    def __init__(self):
        self.files = {}
        self.removals = []

    def run_command(self, command, timeout=30, cap=4000):
        if command.startswith("rm -f -- "):
            path = command[len("rm -f -- "):]
            self.removals.append(path)
            self.files.pop(path, None)
            return ""
        if command.startswith("test -s "):
            path = command.split(" && echo ", 1)[0][len("test -s "):]
            return ("__FORGE_REPORT_READY__\n"
                    if self.files.get(path) else "")
        return ""

    def fetch_file(self, path, max_bytes=None):
        data = self.files.get(path)
        return ((data, "") if data else (None, "missing"))


def _verifier_cfg():
    cfg = Config()
    cfg.model = "verifier-model"
    cfg.vision_model = ""
    cfg.agent_decided_stop = True
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 100
    return cfg


def test_full_agent_report_is_lossless_and_session_persists(
        monkeypatch, tmp_path):
    long_fail = "# Report\nVERDICT: FAIL\n" + ("detailed evidence\n" * 400)
    long_pass = "# Reinspection\n" + ("new evidence\n" * 400) + "VERDICT: PASS\n"
    reports = iter((long_fail, long_pass))
    calls = []

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        assert kwargs["instruction_is_complete_opening"] is True
        report = next(reports)
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        history = list(kwargs.get("initial_history") or [])
        history += [{"role": "user", "content": prompt},
                    {"role": "assistant", "content":
                     '{"program":{"lang":"verifier-report","code":"publish report"}}'}]
        calls.append({"prompt": prompt,
                      "continue": kwargs.get("continue_context"),
                      "history_before": len(kwargs.get("initial_history") or [])})
        return SimpleNamespace(
            status="done", infra_pause_secs=4, infra_pauses=1), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    outer_cfg = SimpleNamespace(agentic_verifier_config="verifier.yaml")
    vm = _VM()
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict1, findings1 = V.verify_agentic(
        "task", vm, outer_cfg, sink=sink, turn_no=5, session=session,
        context="mechanical candidate revision one", wall_budget=50)
    verdict2, findings2 = V.verify_agentic(
        "task", vm, outer_cfg, sink=sink, turn_no=6, session=session,
        context="mechanical candidate revision two", wall_budget=50)

    assert verdict1 == "wrong" and str(findings1) == long_fail
    assert verdict2 == "pass" and str(findings2) == long_pass
    assert len(str(findings1)) > 2000 and len(str(findings2)) > 2000
    assert session.inspections == 2 and len(session) == 4
    assert session.infra_pause_secs == 8 and session.infra_pauses == 2
    assert calls[0]["continue"] is False
    assert calls[1]["continue"] is True and calls[1]["history_before"] == 2
    assert "mechanical candidate revision two" in calls[1]["prompt"]

    saved = json.loads(
        (tmp_path / "run" / "iter_06" / "verify2.json").read_text())
    assert saved["findings"] == long_pass


def test_agentic_verifier_honors_loaded_context_compaction(monkeypatch, tmp_path):
    loaded = _verifier_cfg()
    loaded.history_keep_pairs = 20
    loaded.ctx_high_water = 500000
    observed = {}
    monkeypatch.setattr("config.settings.load", lambda _path: loaded)

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        observed["history_keep_pairs"] = cfg.history_keep_pairs
        observed["ctx_high_water"] = cfg.ctx_high_water
        report = "Evidence complete.\nVERDICT: PASS\n"
        kwargs["program_executor"]("verifier-report", report)
        return SimpleNamespace(status="done"), [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "published"},
        ]

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    verdict, _ = V.verify_agentic(
        "task", _VM(), SimpleNamespace(agentic_verifier_config="fixed.yaml"),
        sink=ArtifactSink(str(tmp_path / "run")), wall_budget=50)

    assert verdict == "pass"
    assert observed == {"history_keep_pairs": 20, "ctx_high_water": 500000}


@pytest.mark.parametrize("status", ["stalled", "stalled_quiescent", "budget", "done"])
@pytest.mark.parametrize("repeated_reply", [True, False])
def test_actionless_verifier_stops_without_a_candidate_verdict(
        monkeypatch, tmp_path, status, repeated_reply):
    loaded = _verifier_cfg()
    monkeypatch.setattr("config.settings.load", lambda _path: loaded)
    original = [
        {"role": "user", "content": "original inspection task"},
        {"role": "assistant", "content":
         '{"program":{"lang":"bash","code":"echo evidence"}}'},
    ]
    session = V.VerifierSession(original)
    session.pending_observation = "complete prior evidence"
    session.pending_turn = V.parse_turn(original[-1]["content"])
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        calls.append(prompt)
        assert len(calls) <= 3, "an empty checkpoint must not retry indefinitely"
        assert cfg.history_keep_pairs == 0, "the guard must respect context policy"
        assert "complete prior evidence" in prompt
        history = list(kwargs["initial_history"]) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": (
                "(empty reply)" if repeated_reply else
                f"Different actionless narrative {len(calls)}"),
             "reasoning": "complete raw reasoning"},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status=status), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    root = tmp_path / "run"
    with pytest.raises(AgenticVerifierNoProgressError,
                       match="3 consecutive segments"):
        V.verify_agentic(
            "original inspection task", _VM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=ArtifactSink(str(root)), session=session, wall_budget=50)

    assert len(calls) == 3
    assert list(session) == original
    assert session.pending_observation == "complete prior evidence"
    assert not list(root.rglob("verify2.json")), "infrastructure cannot grade the task"
    segments = sorted((root / "verifier_agent/inspection_001").glob("segment_*"))
    for index, segment in enumerate(segments, 1):
        raw = json.loads((segment / "transcript.json").read_text())
        assert raw["messages"][-1]["reasoning"] == "complete raw reasoning"
        recovery = json.loads((segment / "recovery.json").read_text())
        assert recovery["actionless_segments"] == index
        assert recovery["checkpoint_advanced"] is False
    assert recovery["stop_reason"] == "no_substantive_progress"
    assert recovery["status"] == "infra"
    assert recovery["segment_status"] == status


def test_verifier_progress_resets_empty_streak_and_publication_wins(
        monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    steps = iter(["empty", "empty", "program", "empty", "empty", "publish"])
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        step = next(steps)
        calls.append(step)
        content = "(empty reply)"
        if step == "program":
            content = '{"program":{"lang":"bash","code":"echo new evidence"}}'
        if step == "publish":
            kwargs["program_executor"]("verifier-report", "Evidence.\nVERDICT: PASS\n")
            # A real publication has authority even at a coincident dry boundary.
            assert kwargs["terminal_handoff_ready"]()
        history = list(kwargs["initial_history"]) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="stalled"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    root = tmp_path / "run"
    verdict, _ = V.verify_agentic(
        "task", _VM(), SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=ArtifactSink(str(root)), session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass" and len(calls) == 6
    records = [json.loads(p.read_text()) for p in sorted(root.rglob("recovery.json"))]
    assert [record["actionless_segments"] for record in records] == [1, 2, 0, 1, 2, 3]
    assert all("stop_reason" not in record for record in records)


def test_full_agent_can_route_to_evolution_without_changing_runtime_authority(
        monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    captured = {}

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        report = "# Evidence\nunmet target requirement\nROUTE: EVOLVE\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        captured.update(
            prompt=prompt,
            system=kwargs["system_prompt"],
            reminder=kwargs["user_message_transform"]("observation"),
        )
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml", verifier_evolve_route=True,
        verifier_hide_actor_memory=True)

    verdict, findings = V.verify_agentic(
        "task", _VM(), cfg, sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)

    assert verdict == "evolve"
    assert str(findings).endswith("ROUTE: EVOLVE\n")
    assert "ROUTE: EVOLVE" in captured["prompt"]
    assert "ROUTE: EVOLVE" in captured["system"]
    assert "mechanically hidden" in captured["system"]
    assert "HANDOFF, REVISE, or EVOLVE" in captured["reminder"]


def test_full_agent_can_request_evidence_without_losing_its_session(
        monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    captured = {}

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        report = "# Unsettled material claim\nVERDICT: UNVERIFIED\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        captured.update(prompt=prompt, system=kwargs["system_prompt"])
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_unverified_evidence=True)
    session = V.VerifierSession()

    verdict, findings = V.verify_agentic(
        "task", _VM(), cfg, sink=ArtifactSink(str(tmp_path / "run")),
        session=session, wall_budget=50,
        actor_evidence_leads="Exact probe: curl localhost")

    assert verdict == "unverified"
    assert str(findings).endswith("VERDICT: UNVERIFIED\n")
    assert session.stage == "EVIDENCE_REQUESTED"
    assert "VERDICT: UNVERIFIED" in captured["system"]
    assert "ACTOR-SUPPLIED EVIDENCE LEADS" in captured["prompt"]


def test_verify_independent_dispatches_only_when_opted_in(monkeypatch):
    sentinel = ("wrong", V.Findings("full report"))
    monkeypatch.setattr(V, "verify_agentic", lambda *a, **kw: sentinel)
    cfg = SimpleNamespace(agentic_verifier_config="verifier.yaml")
    assert V.verify_independent("task", object(), cfg) is sentinel


def test_orientation_preserves_context_without_creating_an_evaluator_lifecycle(
        monkeypatch):
    from core import loop as actor_loop
    from core.verifier_runtime import AgenticVerifierExecutor

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    prompts = []

    def fake_attempt(prompt, vm, cfg, sink, **kwargs):
        prompts.append(prompt)
        kwargs["program_executor"](
            "verifier-report",
            "Private S0 observations only.\nSTAGE: ORIENTATION_READY\n")
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"ready"}}'},
        ]
        return SimpleNamespace(
            status="done", infra_pause_secs=0, infra_pauses=0), history

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    events = []
    session = V.VerifierSession(
        on_event=lambda event, payload: events.append((event, payload)))
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
        verifier_stage_lifecycle=True,
        verifier_persist_scratch=True,
    )

    verdict, findings = V.verify_agentic(
        "task", object(), cfg, session=session,
        stage="orientation", wall_budget=50)

    assert verdict == "orientation_ready"
    assert "STAGE: ORIENTATION_READY" in str(findings)
    assert "STAGE: ORIENTATION" in prompts[0]
    assert "No Actor Agent for this target cycle has been created" in prompts[0]
    assert "private_evaluator" not in prompts[0]
    assert "future candidate is correct" in prompts[0]
    assert session.stage == "ORIENTATION_READY"
    assert session.orientation_runs == 1
    assert session.candidate_generation == 0
    assert not hasattr(session, "current_evaluator")
    assert not hasattr(AgenticVerifierExecutor, "run_frozen_private_evaluator")
    assert all(not event.startswith("VERIFIER_EVALUATOR_")
               for event, _payload in events)


def test_candidate_verification_uses_only_explicit_agent_actions(monkeypatch):
    from core import loop as actor_loop

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    prompts = []

    def fake_attempt(prompt, vm, cfg, sink, **kwargs):
        prompts.append(prompt)
        kwargs["program_executor"](
            "verifier-report",
            "Direct inspection affirmatively supports the task.\nROUTE: HANDOFF\n")
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"published"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    events = []
    session = V.VerifierSession(
        on_event=lambda event, payload: events.append((event, payload)))
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
        verifier_stage_lifecycle=True,
        verifier_persist_scratch=True,
    )

    verdict, findings = V.verify_agentic(
        "task", object(), cfg, session=session, wall_budget=50)

    assert verdict == "pass"
    assert "affirmatively supports" in str(findings)
    assert "private_evaluator" not in prompts[0]
    assert "freely create, adapt, or discard whatever probes" in prompts[0]
    assert all(not event.startswith("VERIFIER_EVALUATOR_")
               for event, _payload in events)


def test_agentic_charter_leaves_investigation_and_report_shape_to_agent():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "/tmp/report.md", "file changed")
    assert "full investigation Agent" in prompt
    assert "no prescribed checklist, report schema, probe count" in prompt
    assert "code-as-policy freedom" in prompt
    assert "mechanically\neffect-isolated action surface" in prompt
    assert "Freely author Python or Bash" in prompt
    assert "VERIFIER_SCRATCH" in prompt
    assert "output truncation" in prompt
    assert "verifier-read" not in prompt
    assert "verifier-report" in prompt
    assert "VERDICT: PASS" in prompt and "VERDICT: FAIL" in prompt


def test_unverified_charter_requests_untrusted_reproducible_leads():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel",
        allow_unverified=True,
        actor_evidence_leads=(
            "Actor-stated claim: service is live\n"
            "Exact reproducible probe proposed by Actor: curl localhost"))

    assert "VERDICT: UNVERIFIED" in prompt
    assert "same persistent Verifier Agent" in prompt
    assert "credible instruments or observation channels disagree" in prompt
    assert "ACTOR-SUPPLIED EVIDENCE LEADS" in prompt
    assert "untrusted leads" in prompt
    assert "curl localhost" in prompt


def test_rollback_charter_truthfully_exposes_live_namespaces_then_restore():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel",
        execution_mode="rollback_mirror")

    assert "transactional\nmirror of the exact live Actor machine" in prompt
    assert "processes, localhost services, network, GUI, and IPC" in prompt
    assert "restores that checkpoint before grading" in prompt
    assert "`$HOME` remains `/home/user`" in prompt
    assert "without access to the Actor's processes" not in prompt


def test_agentic_charter_restores_universal_falsification_principles():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)
    flat = " ".join(prompt.split())

    assert "Verification is method-neutral unless the authoritative task makes a " \
        "method, application, native editable state, behavior, workflow, or " \
        "provenance material" in flat
    assert "compatible file, backend render, or visually similar export is not by " \
        "itself proof" in flat
    assert "evidence that distinguishes the required result from a nearby plausible " \
        "but incorrect substitute" in flat
    assert "keep investigating through a distinguishing channel" in flat
    assert "no prescribed checklist, report schema, probe count" in flat


def test_orientation_is_free_and_creates_no_automatic_instrument():
    prompt = agent_harness_verifier_orientation_charter(
        "authoritative task", "host channel", "trusted baseline")
    flat = " ".join(prompt.split())

    assert "Freely get familiar with this environment" in prompt
    assert "task-start observations and hypotheses" in flat
    assert "Orientation produces only your private report" in flat
    assert "Only actions you explicitly choose to run are executed" in flat
    assert "does not discover, freeze, rerun, score, or privilege files" in flat
    assert "private_evaluator" not in prompt
    assert "run.sh" not in prompt
    assert "STAGE: ORIENTATION_READY" in prompt
    assert "HANDOFF, REVISE, or EVOLVE" in prompt


def test_rollback_orientation_describes_its_actual_home_and_transaction():
    prompt = agent_harness_verifier_orientation_charter(
        "authoritative task", "host channel", "trusted baseline",
        execution_mode="rollback_mirror")
    flat = " ".join(prompt.split())

    assert "transactional mirror of the exact live S0 machine" in flat
    assert "restores it when orientation ends" in flat
    assert "`$HOME` remains `/home/user`" in flat
    assert "effect-isolated and read-only outside" not in flat


def test_candidate_verification_uses_fresh_agent_chosen_probes_without_checklist():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)

    assert "S0 observations, old hypotheses" in prompt
    assert "freely create, adapt, or discard whatever probes" in prompt
    assert "private_evaluator" not in prompt
    assert "automatically" not in prompt
    assert "no prescribed checklist, report schema, probe count" in prompt
    assert "HANDOFF_FALSIFICATION" not in prompt


def test_curriculum_verify_more_preserves_verifier_authority_on_same_candidate():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", candidate_generation=4,
        curriculum_review=(
            "The report omitted evidence for one requirement.\n"
            "ROUTE: VERIFY_MORE\n"))

    assert "This is candidate generation 4" in prompt
    assert "candidate has not changed" in prompt
    assert "cannot see the candidate" in prompt
    assert "has not overruled your local correctness judgment" in prompt
    assert "publish PASS again" in prompt and "or FAIL" in prompt


def test_unified_charter_adds_agent_owned_evolve_route_without_teaching():
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)

    assert "ROUTE: HANDOFF" in prompt
    assert "ROUTE: REVISE" in prompt
    assert "ROUTE: EVOLVE" in prompt
    assert "EVOLVE is a routing judgment" in prompt
    assert "not a request to teach" in prompt
    assert "no prescribed checklist, report schema, probe count" in prompt
    assert "VERDICT: PASS" not in prompt and "VERDICT: FAIL" not in prompt


def test_substantive_checkpoint_drops_dry_tail_but_returns_observation():
    prior = [{"role": "user", "content": "opening"},
             {"role": "assistant", "content":
              '{"program":{"lang":"bash","code":"echo first"}}'}]
    raw = prior + [
        {"role": "user", "content": "first trace"},
        {"role": "assistant", "content":
         '{"look":{"path":"/tmp/a.png","question":"inspect"}}'},
        {"role": "user", "content": "the look result, losslessly"},
        {"role": "assistant", "content": "Now publishing the report:"},
        {"role": "user", "content": "strict format nudge"},
        {"role": "assistant", "content": "Now publishing the report:"},
    ]

    checkpoint, observation, images, turn = \
        V._agentic_substantive_checkpoint(prior, raw)

    assert len(checkpoint) == 4
    assert checkpoint[-1]["content"].startswith('{"look"')
    assert observation == "the look result, losslessly"
    assert images == []
    assert isinstance(turn, V.Look)
    assert all("Now publishing" not in m["content"] for m in checkpoint)


def test_semantic_replay_keeps_every_action_and_result_without_dry_turns():
    raw = [
        {"role": "user", "content": "full opening"},
        {"role": "assistant", "content":
         'analysis then {"program":{"lang":"bash","code":"probe"}}'},
        {"role": "user", "content": "complete probe trace"},
        {"role": "assistant", "content": "I will look now"},
        {"role": "user", "content": "strict nudge"},
        {"role": "assistant", "content":
         '{"look":{"path":"/tmp/a.png","question":"inspect"}}'},
        {"role": "user", "content": "complete look observation"},
        {"role": "assistant", "content": "I will publish now"},
    ]

    replay, observation, images, turn = V._agentic_semantic_replay(raw)

    assert len(replay) == 4
    assert replay[0]["content"] == "full opening"
    assert "complete probe trace" in replay[2]["content"]
    assert "strict nudge" in replay[2]["content"]
    assert [type(V.parse_turn(replay[i]["content"])).__name__
            for i in (1, 3)] == ["Program", "Look"]
    assert all("I will" not in message["content"] for message in replay)
    assert observation == "complete look observation"
    assert images == []
    assert isinstance(turn, V.Look)


def test_agentic_recovery_preserves_report_and_rolls_back_actionless_tail(
        monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _VM()
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({"prompt": prompt, "history": list(history),
                      "continue": kwargs.get("continue_context")})
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content":
                 '{"program":{"lang":"bash","code":"echo evidence"}}'},
                {"role": "user", "content": "full program trace"},
                {"role": "assistant", "content": "Now writing the report:"},
                {"role": "user", "content": "strict nudge"},
                {"role": "assistant", "content": "Now writing the report:"},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history

        assert len(history) == 2
        assert "Now writing" not in str(history)
        assert "full program trace" in prompt
        report = "# Independent report\nVERDICT: FAIL\ncomplete evidence\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"write report"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=sink, turn_no=9, session=session, wall_budget=50)

    assert verdict == "wrong"
    assert "complete evidence" in str(findings)
    assert len(vm.removals) == 0
    assert len(calls) == 2 and calls[1]["continue"] is True
    assert all("Now writing" not in m["content"] for m in session)
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_advanced"] is True
    assert recovery["discarded_tail_pairs"] == 2
    assert recovery["pending_observation_chars"] == len("full program trace")


def test_agentic_recovery_reattaches_look_without_replaying_dry_reply(
        monkeypatch, tmp_path):
    """Regression for the live t099 Look -> actionless-promise attractor."""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    calls = []
    pixels = b"\x89PNG\r\n\x1a\nexact-verifier-observation"

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({
            "prompt": prompt,
            "history": list(history),
            "opening_image": kwargs.get("opening_image"),
        })
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content":
                 '{"look":{"path":"/tmp/candidate.png",'
                 '"question":"identify it"}}'},
                durable_user_message("complete visual result", pixels),
                {"role": "assistant", "content":
                 "I will now run the exact format check."},
                {"role": "user", "content": "emit one action now"},
                {"role": "assistant", "content": ""},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history

        assert len(history) == 2
        assert all("I will now" not in str(message.get("content", ""))
                   for message in history)
        assert calls[-1]["opening_image"] == [pixels]
        assert "complete visual result" in prompt
        assert "exact pixels" in prompt
        report = "# Independent report\nVERDICT: PASS\nvisual and format evidence\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict, findings = V.verify_agentic(
        "task", _VM(),
        SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=sink, turn_no=3, session=session, wall_budget=50)

    assert verdict == "pass"
    assert "visual and format evidence" in str(findings)
    assert len(calls) == 2
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_history_pairs"] == 1
    assert recovery["discarded_tail_pairs"] == 2
    assert recovery["pending_observation_images"] == 1


def test_dry_first_segment_rehydrates_full_inspection_envelope(
        monkeypatch, tmp_path):
    """A transport stall before the first action must not erase task authority."""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({
            "prompt": prompt,
            "history": list(history),
            "continue": kwargs.get("continue_context"),
        })
        if len(calls) == 1:
            # Three dry decoder turns reproduce the live t019 recovery boundary:
            # no Program/Look exists, so there is no substantive checkpoint yet.
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "I will investigate."},
                {"role": "user", "content": "emit one action"},
                {"role": "assistant", "content": "I will investigate."},
                {"role": "user", "content": "emit one action now"},
                {"role": "assistant", "content": ""},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history

        assert history == []
        assert "AUTHORITATIVE TASK — verbatim" in prompt
        assert "the complete authoritative task" in prompt
        assert "current Actor candidate handoff" not in prompt
        assert "self-checks" in prompt and "conclusions" in prompt
        assert "SAME-CONTEXT TRANSPORT RECOVERY" in prompt
        report = "# Independent evidence\nROUTE: HANDOFF\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
    )
    verdict, _findings = V.verify_agentic(
        "the complete authoritative task", _VM(), cfg,
        sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass"
    assert len(calls) == 2
    assert calls[0]["continue"] is False
    assert calls[1]["continue"] is True
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_history_pairs"] == 0
    assert recovery["checkpoint_advanced"] is False
