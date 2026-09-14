"""Unsupported function envelopes must not launder a later done declaration."""
import json

import pytest

from core.actor import Done, Program, parse_turn
import core.loop as loop
from core.trace import ArtifactSink
from config.settings import load
from env.vm import Trace


def mixed_reply(name="python", string_arguments=False):
    args = {"code": "print('this has not executed')"}
    envelope = {"name": name, "arguments": json.dumps(args) if string_arguments else args}
    return ("<tool_call>\n" + json.dumps(envelope) + "\n</tool_call>\n"
            "<tool_result>\nI claim the output file already exists.\n</tool_result>\n"
            '{"done":{"checks":[]}}')


@pytest.mark.parametrize("name", ["python", "python3", "bash", "program", "look", "verifier-report", "ask", "ask_user",
                                 "terminal", "functions.shell", "create_project", "custom_tool"])
def test_unsupported_native_envelope_cannot_be_silently_dropped(name):
    assert parse_turn(mixed_reply(name)) is None


def test_serialized_function_arguments_are_still_unhandled_transport():
    assert parse_turn(mixed_reply(string_arguments=True)) is None


def test_valid_program_with_envelope_as_literal_data_is_unchanged():
    code = "print(" + repr(mixed_reply()) + ")"
    parsed = parse_turn(json.dumps({"program": {"lang": "python", "code": code}}))
    assert isinstance(parsed, Program) and parsed.code == code


def test_plain_done_stays_a_valid_declaration():
    assert isinstance(parse_turn('{"done":null}'), Done)


def test_unrelated_metadata_with_non_string_name_does_not_crash():
    text = '{"name":[],"arguments":{}}\n{"program":{"code":"print(1)"}}'
    assert isinstance(parse_turn(text), Program)


@pytest.mark.parametrize("metadata", [{"name": "output", "size": 10},
                                     {"name": "output", "arguments": 3}])
def test_metadata_without_function_arguments_is_not_a_tool_envelope(metadata):
    text = json.dumps(metadata) + '\n{"program":{"code":"print(1)"}}'
    assert isinstance(parse_turn(text), Program)


@pytest.mark.parametrize("tool_name", ["python", "terminal", "custom_tool"])
def test_existing_json_retry_recovers_same_context_without_executing_fake_calls(monkeypatch, tmp_path, tool_name):
    monkeypatch.setenv("RSIAGENT_JSON_ACTION_RETRY", "1")
    cfg = load(None)
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.primary_temperature = 1.0
    cfg.max_iters = 10
    cfg.wall_clock_secs = 3600
    cfg.practice_done_requires = ""
    malformed = mixed_reply(tool_name)
    replies = iter([malformed, '{"program":{"lang":"bash","code":"echo actual work"}}', '{"done":null}'])
    requests, executed = [], []

    def chat(model, system, user, **kwargs):
        requests.append({"model": model, "system": system, "user": user,
                         **kwargs, "history": list(kwargs.get("history", []))})
        return next(replies)

    class VM:
        def run_command(self, *_args, **_kwargs):
            return ""

        def run_script(self, lang, code, **_kwargs):
            executed.append((lang, code))
            return Trace(stdout="actual work\n[exit 0]", exit_code=0, secs=0.1)

    monkeypatch.setattr(loop, "chat", chat)
    result, history = loop.run_attempt("practice", VM(), cfg, ArtifactSink(str(tmp_path)))
    assert result.status == "done" and result.programs_run == 1
    assert executed == [("bash", "echo actual work")]
    assert "json_object" not in requests[0]
    assert requests[1].get("json_object") is True
    assert "json_object" not in requests[2]
    assert requests[1]["history"][-1]["content"] == malformed
    for key in ("model", "system", "max_tokens", "temperature", "top_p", "reasoning_effort"):
        assert requests[0][key] == requests[1][key]
    assert any(m.get("content") == malformed for m in history)
