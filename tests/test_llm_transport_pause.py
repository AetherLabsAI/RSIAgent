"""Provider failures pause the harness; they are never synthetic Agent turns."""
import json
from types import SimpleNamespace

import pytest

from config.settings import load
from core import loop as L
from core.trace import ArtifactSink
from env.vm import Trace
from llm import client as C
from llm.client import LLMTransportError


class _StatusError(Exception):
    def __init__(self, status_code, body):
        super().__init__(body)
        self.status_code = status_code
        self.body = body


class _FailingCompletions:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.error


def test_402_raises_recoverable_transport_instead_of_empty_turn(monkeypatch):
    completions = _FailingCompletions(
        _StatusError(402, {"message": "Insufficient credits"}))
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")

    assert raised.value.recoverable is True
    assert raised.value.status_code == 402
    assert completions.calls == 4


def test_bad_request_is_explicit_fatal_transport(monkeypatch):
    completions = _FailingCompletions(
        _StatusError(400, {"message": "unsupported parameter"}))
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")

    assert raised.value.recoverable is False
    assert raised.value.status_code == 400


def test_local_sdk_programming_error_does_not_pause_forever(monkeypatch):
    completions = _FailingCompletions(TypeError("unexpected keyword"))
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")

    assert raised.value.recoverable is False
    assert raised.value.status_code is None


class _VM:
    def __init__(self):
        self.codes = []

    def run_command(self, _command, **_kwargs):
        return ""

    def run_script(self, _lang, code, **_kwargs):
        self.codes.append(code)
        return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)

    def fetch_file(self, _path, max_bytes=0):
        return None, "missing"


def _practice_cfg():
    cfg = load(None)
    cfg.model = "actor"
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.agent_decided_stop = True
    cfg.history_keep_pairs = 0
    cfg.max_iters = 20
    cfg.wall_clock_secs = 100
    cfg.max_consec_dry = 3
    cfg.max_consec_degen = 3
    cfg.llm_infra_retry_secs = 7
    return cfg


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_recoverable_transport_pauses_same_context_and_work_clock(
        monkeypatch, tmp_path):
    cfg = _practice_cfg()
    clock = _Clock()
    replies = iter((
        LLMTransportError("actor", _StatusError(402, "credits"),
                          recoverable=True, status_code=402, detail="credits"),
        '{"program":{"lang":"bash","code":"echo work"}}',
        '{"done":null}',
    ))

    def fake_chat(*_args, **_kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(L, "chat", fake_chat)
    monkeypatch.setattr(L.time, "time", clock.time)
    monkeypatch.setattr(L.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(L.time, "sleep", clock.sleep)

    result, history = L.run_attempt(
        "practice", _VM(), cfg, ArtifactSink(str(tmp_path / "pause")))

    assert result.status == "done"
    assert result.turns == 2 and result.programs_run == 1
    assert result.infra_pauses == 1 and result.infra_pause_secs == 7
    assert result.wall_secs == 0
    assert all("empty reply" not in str(message["content"]) for message in history)


def test_cumulative_nudges_never_terminate_productive_agent(monkeypatch, tmp_path):
    cfg = _practice_cfg()
    cfg.max_nudges = 1  # legacy value must be inert
    replies = iter((
        "I will act next.",
        '{"program":{"lang":"bash","code":"echo first"}}',
        "I will take another action next.",
        '{"program":{"lang":"bash","code":"echo second"}}',
        '{"done":null}',
    ))
    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))

    vm = _VM()
    result, history = L.run_attempt(
        "practice", vm, cfg, ArtifactSink(str(tmp_path / "nudges")))

    assert result.status == "done"
    assert result.turns == 5 and result.programs_run == 2
    assert vm.codes == ["echo first", "echo second"]
    assert sum(m["role"] == "assistant" and "I will" in str(m["content"])
               for m in history) == 2


def test_json_format_retry_keeps_context_and_only_executes_parsed_action(monkeypatch, tmp_path):
    monkeypatch.setenv("RSIAGENT_JSON_ACTION_RETRY", "1")
    requests = []
    replies = iter(("Tool choice is none, so I cannot act.",
                    '{"program":{"lang":"bash","code":"echo actual work"}}',
                    '{"done":null}'))
    def fake_chat(*args, **kwargs):
        requests.append({**kwargs, "history": list(kwargs.get("history", []))})
        return next(replies)
    monkeypatch.setattr(L, "chat", fake_chat)
    vm = _VM()
    result, history = L.run_attempt("practice", vm, _practice_cfg(), ArtifactSink(str(tmp_path)))
    assert result.status == "done" and vm.codes == ["echo actual work"]
    assert "json_object" not in requests[0]
    assert requests[1]["json_object"] is True
    assert requests[1]["history"][-1]["content"] == "Tool choice is none, so I cannot act."
    assert "json_object" not in requests[2]


def test_configured_user_channel_failure_aborts_infra_invalid(monkeypatch, tmp_path):
    cfg = _practice_cfg()
    monkeypatch.setattr(
        L, "chat", lambda *_args, **_kwargs:
        '{"ask":{"question":"Please provide the missing evidence."}}')
    sink = ArtifactSink(str(tmp_path / "user-channel-failure"))

    def unavailable(_question):
        raise RuntimeError("provider unavailable")

    with pytest.raises(L.UserChannelInfrastructureError, match="infra-invalid"):
        L.run_attempt("task", _VM(), cfg, sink, ask_user=unavailable)

    record = json.loads(
        (tmp_path / "user-channel-failure" / "iter_01" / "ask.json")
        .read_text(encoding="utf-8"))
    assert record["ok"] is False
    assert record["question"] == "Please provide the missing evidence."
    assert "USER CHANNEL ERROR" in record["answer"]


def test_nested_verifier_pause_also_pauses_outer_actor_clock(
        monkeypatch, tmp_path):
    cfg = _practice_cfg()
    cfg.practice_mode = False
    cfg.independent_verify = True
    cfg.agentic_verifier_config = "verifier.yaml"
    cfg.done_witness_gates = False
    cfg.split_done_gate = False
    clock = _Clock()
    replies = iter((
        '{"program":{"lang":"bash","code":"echo build"}}',
        ('{"done":{"checks":[{"desc":"ACTOR_PRIVATE_SELF_CHECK_SENTINEL",'
         '"probe":"test -e /tmp && echo PASS || echo FAIL"}]}}'),
    ))

    class PassVM(_VM):
        def run_command(self, _command, **_kwargs):
            return "PASS"

    def fake_verify(_instruction, _vm, _cfg, sink=None, turn_no=0,
                    context="", session=None, wall_budget=None):
        assert isinstance(session, L.VerifierSession)
        assert wall_budget == 100
        assert "ACTOR_PRIVATE_SELF_CHECK_SENTINEL" not in context
        # Model a provider outage handled inside the nested Verifier Agent runtime.
        clock.sleep(9)
        session.infra_pause_secs += 9
        session.infra_pauses += 2
        return "pass", "independently confirmed"

    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr(L, "verify_independent", fake_verify)
    monkeypatch.setattr(L.time, "time", clock.time)
    monkeypatch.setattr(L.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(L.time, "sleep", clock.sleep)

    result, _ = L.run_attempt(
        "task", PassVM(), cfg, ArtifactSink(str(tmp_path / "nested-pause")))

    assert result.status == "done"
    assert result.infra_pause_secs == 9
    assert result.infra_pauses == 2
    assert result.wall_secs == 0
