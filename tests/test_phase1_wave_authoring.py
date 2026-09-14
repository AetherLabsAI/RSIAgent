"""A Curriculum retry must preserve work and cannot renew an empty loop forever."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from config.settings import Config
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
from core.loop import LoopResult
from explore import phase1_wave as wave
from explore.practice_loop import PracticeBoundaryError, PracticeHooks, PracticeInfrastructureError


class LocalWaveVM:
    """Execute only the harness's fixture probes against pytest-owned paths."""

    def run_command(self, command, timeout=30):
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            timeout=timeout, check=True)
        return result.stdout


@pytest.fixture
def surface(tmp_path, monkeypatch):
    root = tmp_path / "guest/evolution_wave"
    handoff = tmp_path / "guest/phase1_wave.json"
    root.mkdir(parents=True)
    monkeypatch.setattr(wave, "WAVE_ROOT", str(root))
    monkeypatch.setattr(wave, "WAVE_HANDOFF", str(handoff))
    return root, handoff, tmp_path / "curriculum/wave_002"


def _publish(surface, decision="WAVE"):
    root, handoff, _ = surface
    record = {
        "decision": decision, "rationale": "the Curriculum's own evidence",
        "projects": ([{"id": "p", "instruction":
                       "Keep all work in /home/user/evolution_project."}]
                     if decision == "WAVE" else []),
    }
    handoff.write_text(json.dumps(record))
    return record


def _draft(surface, text="original partial input"):
    path = surface[0] / "p/evolution_project/input.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _hooks(surface, attempt):
    def capture(_vm, _key, destination, *, guest_dirs, attempts):
        assert guest_dirs == [surface[0].name] and attempts == 1
        if not surface[0].exists():
            return {"ok": False, "files": 0}
        shutil.copytree(surface[0], Path(destination) / "evolution_wave")
        return {"ok": True}

    return PracticeHooks(
        run_attempt=attempt,
        read_guest_text=lambda _vm, path: (
            Path(path).read_text() if Path(path).is_file() else ""),
        audit_text=lambda *_args, **_kwargs: [],
        audit_transcripts=lambda *_args, **_kwargs: [],
        capture_project=capture)


def _author(surface, attempt, *, cfg=None, history=None):
    return wave._author_wave(
        hooks=_hooks(surface, attempt), vm=LocalWaveVM(),
        cfg=cfg or Config(max_iters=20, wall_clock_secs=60),
        target_direction="public task instruction", previous_wave_outcomes="",
        history=history or [], sink_root=surface[2],
        target_query_conditioned=True)


def test_retry_preserves_partial_files_and_complete_context(surface):
    stale = surface[0] / "old-wave.txt"
    stale.write_text("previous wave must not survive the fresh-wave boundary")
    prior = [{"role": "user", "content": "prior project outcome"},
             {"role": "assistant", "content": "prior lesson", "reasoning": "kept"}]
    original = copy.deepcopy(prior)
    cfg = Config(max_iters=20, wall_clock_secs=60, system_extra="existing contract")
    histories = []

    def attempt(prompt, _vm, phase_cfg, sink, **kwargs):
        histories.append(copy.deepcopy(kwargs["initial_history"]))
        assert PLAIN_JSON_TRANSPORT_NOTE in phase_cfg.system_extra
        assert phase_cfg.system_extra.startswith("existing contract")
        assert not stale.exists()
        if len(histories) == 1:
            _draft(surface)
            surface[1].write_text('{"decision": "WAVE",')
            assert not kwargs["terminal_handoff_ready"]()
            history = prior + [{"role": "user", "content": prompt},
                               {"role": "assistant", "content": "drafted input"}]
            sink.save_transcript("system", history)
            return LoopResult(status="stalled", iters=1, programs_run=1,
                              wall_secs=2), history
        assert not surface[1].exists(), "a retry must invalidate the stale handoff"
        assert (surface[0] / "p/evolution_project/input.txt").read_text() == \
            "original partial input"
        assert "draft fixtures" in prompt and "remain on the same machine" in prompt
        assert kwargs["continue_context"] is True
        _publish(surface)
        assert kwargs["terminal_handoff_ready"]()
        return LoopResult(status="done", iters=1, programs_run=1), histories[-1]

    decision, returned = _author(surface, attempt, cfg=cfg, history=prior)
    assert decision.decision == "WAVE"
    assert returned == histories[-1]
    assert histories[1][:2] == original and len(histories[1]) == 4
    assert prior == original and cfg.system_extra == "existing contract"
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    assert state["status"] == "published" and state["segments"] == 2


def test_terminal_probe_waits_for_the_declared_fixture_tree(surface):
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        _publish(surface)
        assert not kwargs["terminal_handoff_ready"]()
        (surface[0] / "p/evolution_project").mkdir(parents=True)
        assert not kwargs["terminal_handoff_ready"](), "empty fixtures are incomplete"
        _draft(surface)
        assert kwargs["terminal_handoff_ready"]()
        return LoopResult(status="done", iters=1, programs_run=1), []

    assert _author(surface, attempt)[0].decision == "WAVE"


def test_wave_transcript_quarantine_updates_authoring_state(surface):
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        _draft(surface)
        _publish(surface)
        return LoopResult(status="done", iters=1, programs_run=1), []

    hooks = _hooks(surface, attempt)
    hooks.audit_transcripts = lambda *_args, **_kwargs: [
        {"file": "trace.txt", "hits": [{"kind": "repo-url", "match": "blocked"}]}]
    with pytest.raises(PracticeBoundaryError):
        wave._author_wave(
            hooks=hooks, vm=LocalWaveVM(), cfg=Config(),
            target_direction="public task instruction", previous_wave_outcomes="",
            history=[], sink_root=surface[2], target_query_conditioned=True)

    state = json.loads((surface[2] / "authoring_state.json").read_text())
    assert state["status"] == "quarantined"
    assert "boundary_audit.json" in state["reason"]
    assert "decision" not in state
    assert (surface[2] / "segment_000/boundary_audit.json").is_file()


def test_repeated_narration_stops_as_infrastructure_and_archives_drafts(surface):
    calls = []

    def attempt(prompt, _vm, _cfg, sink, **kwargs):
        calls.append(sink.root)
        # An earlier productive segment must not be erased by later dry retries.
        if len(calls) == 1:
            _draft(surface)
            return LoopResult(status="stalled", programs_run=1, iters=1), []
        history = list(kwargs["initial_history"])
        for index in range(1, 4):
            reply = "Tool channel explicitly closed this turn (tool_choice=none)."
            sink.save_turn(index, reply)
            history += [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": reply}]
        sink.save_transcript("system", history)
        return LoopResult(status="stalled", turns=3, wall_secs=1), history

    with pytest.raises(PracticeInfrastructureError, match="no Program or Look actions"):
        _author(surface, attempt)
    assert len(calls) == 4
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    assert state["status"] == "infra" and state["actionless_segments"] == 3
    assert "decision" not in state
    archived = surface[2] / "blocked_draft/evolution_wave/p/evolution_project/input.txt"
    assert archived.read_text() == "original partial input"
    transcripts = sorted(surface[2].glob("segment_*/transcript.json"))
    assert len(transcripts) == 3
    assert len(json.loads(transcripts[-1].read_text())["messages"]) == 18
    assert len(list(surface[2].glob("segment_*/result.json"))) == 4


def test_executed_work_resets_the_actionless_retry_streak(surface):
    remaining = iter([0, 0, 1, 0, 0, 1])
    calls = []

    def attempt(_prompt, _vm, _cfg, _sink, **_kwargs):
        work = next(remaining)
        calls.append(work)
        if work:
            _draft(surface)
        if len(calls) == 6:
            _publish(surface)
            return LoopResult(status="done", programs_run=work, iters=work), []
        return LoopResult(status="stalled", programs_run=work, iters=work), []

    assert _author(surface, attempt)[0].decision == "WAVE"
    assert len(calls) == 6


@pytest.mark.parametrize("ceiling", ["wall", "iterations"])
def test_segments_share_one_configured_emergency_ceiling(surface, ceiling):
    cfg = Config(max_iters=3 if ceiling == "iterations" else 20,
                 wall_clock_secs=5 if ceiling == "wall" else 60)
    calls = []

    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        calls.append((kwargs["iters_budget"], kwargs["wall_budget"]))
        _draft(surface)
        return LoopResult(status="stalled", programs_run=1,
                          iters=2 if len(calls) == 1 else 1,
                          wall_secs=3 if len(calls) == 1 else 2), []

    with pytest.raises(PracticeInfrastructureError, match="cumulative transport ceiling"):
        _author(surface, attempt, cfg=cfg)
    assert len(calls) == 2
    assert calls[1] == (cfg.max_iters - 2, cfg.wall_clock_secs - 3)
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    assert state["status"] == "infra" and state["segments"] == 2


@pytest.mark.parametrize("decision", ["SATURATED", "STALLED"])
def test_only_the_agent_can_publish_a_semantic_terminal_decision(surface, decision):
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        _publish(surface, decision)
        assert kwargs["terminal_handoff_ready"]()
        return LoopResult(status="done", iters=1, programs_run=1), []

    assert _author(surface, attempt)[0].decision == decision
    assert json.loads((surface[2] / "authoring_state.json").read_text())["decision"] == decision


def test_capture_failure_does_not_hide_the_original_infrastructure_failure(surface):
    def attempt(*_args, **_kwargs):
        return LoopResult(status="infra"), []

    hooks = _hooks(surface, attempt)
    def broken_capture(*_args, **_kwargs):
        raise OSError("archive transport unavailable")
    hooks.capture_project = broken_capture
    with pytest.raises(PracticeInfrastructureError, match="infrastructure status infra"):
        wave._author_wave(
            hooks=hooks, vm=LocalWaveVM(), cfg=Config(),
            target_direction="public task instruction", previous_wave_outcomes="",
            history=[], sink_root=surface[2], target_query_conditioned=True)
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    assert state["draft_capture"] == {
        "ok": False, "error": "archive transport unavailable"}
