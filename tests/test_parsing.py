"""Offline tests for turn parsing — M3-style output shapes (prose preambles, markdown
fences, bare objects, draft-then-correct) must parse without a live model."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.checks import validate                      # noqa: E402
from core.actor import (Ask, Done, Look, Program, build_system,  # noqa: E402
                         extract_plan, parse_turn, trace_message)


def test_program_with_prose():
    t = parse_turn('I will scan the disk first.\n'
                   '{"program": {"lang": "bash", "code": "ls -la ~"}}')
    assert isinstance(t, Program) and t.lang == "bash" and t.code == "ls -la ~"


def test_provider_native_text_tool_call_is_not_a_dry_turn():
    raw = ('I will inspect now.<|open|>tools<|sep|>'
           '<|open|>call tool="bash" index="1"<|sep|>'
           '<|open|>argument key="code" type="string"<|sep|>'
           'printf "evidence\\n"\nls /home/user/Desktop'
           '<|close|>argument<|sep|><|close|>call<|sep|><|close|>tools')

    t = parse_turn(raw)

    assert isinstance(t, Program)
    assert t.lang == "bash"
    assert t.code == 'printf "evidence\\n"\nls /home/user/Desktop'


def test_provider_native_look_call_is_not_a_dry_turn():
    raw = ('<|open|>call tool="look" index="1"<|sep|>'
           '<|open|>argument key="path" type="string"<|sep|>'
           '/tmp/evidence.png<|close|>argument<|sep|>'
           '<|open|>argument key="question" type="string"<|sep|>'
           'Read the exact label.<|close|>argument<|sep|>'
           '<|close|>call')

    t = parse_turn(raw)

    assert isinstance(t, Look)
    assert t.path == "/tmp/evidence.png"
    assert t.question == "Read the exact label."


def test_markdown_fenced():
    t = parse_turn('```json\n{"program": {"lang": "python", "code": "print(1)"}}\n```')
    assert isinstance(t, Program) and t.code == "print(1)"


def test_bare_program_object():
    t = parse_turn('{"lang": "python", "code": "print(2)"}')
    assert isinstance(t, Program) and t.code == "print(2)"


def test_draft_then_correct_takes_last():
    t = parse_turn('{"program": {"lang": "bash", "code": "old"}}\n'
                   'Wait, better:\n{"program": {"lang": "bash", "code": "new"}}')
    assert isinstance(t, Program) and t.code == "new"


def test_done_with_checks():
    t = parse_turn('{"done": {"checks": [{"desc": "d", "probe": "ls / && echo PASS"}]}}')
    assert isinstance(t, Done) and len(t.checks) == 1


def test_bare_checks_object():
    t = parse_turn('{"checks": [{"desc": "d", "probe": "ls ~/x && echo PASS"}]}')
    assert isinstance(t, Done) and len(t.checks) == 1


def test_program_wins_over_done():
    t = parse_turn('{"program": {"lang": "bash", "code": "x"}, "done": {"checks": []}}')
    assert isinstance(t, Program)


def test_program_then_hedged_done_two_objects():
    # seed2 killer: a good program followed by a hedging done object in the SAME turn
    t = parse_turn('{"program": {"lang": "bash", "code": "ls -la ~"}}\n'
                   '{"done": {"checks": []}}')
    assert isinstance(t, Program) and t.code == "ls -la ~"


def test_program_then_done_null():
    # seed3 killer: trailing {"done": null}
    t = parse_turn('{"program": {"lang": "bash", "code": "ls"}}\n{"done": null}')
    assert isinstance(t, Program) and t.code == "ls"


def test_done_null_alone_is_explicit_done():
    t = parse_turn('{"done": null}')
    assert isinstance(t, Done) and t.checks == []


def test_dry_turns():
    assert parse_turn("") is None
    assert parse_turn("thinking out loud, no action") is None
    assert parse_turn('{"note": "irrelevant object"}') is None
    assert parse_turn('{"program": {"lang": "python", "code": ""}}') is None


def test_multiline_code_survives():
    t = parse_turn('{"program": {"lang": "python", '
                   '"code": "import os\\nfor f in os.listdir(\'.\'):\\n    print(f)"}}')
    assert isinstance(t, Program) and "\n" in t.code and "listdir" in t.code


def test_nested_look_tolerance():
    # seed12 killer: M3 wrapped a look inside a program object
    t = parse_turn('{"program": {"look": {"path": "/tmp/grid.png", '
                   '"question": "compare styles"}}}')
    assert isinstance(t, Look) and t.path == "/tmp/grid.png"


def test_look_parsing():
    t = parse_turn('{"look": {"path": "/tmp/x/img.png", "question": "what date?"}}')
    assert isinstance(t, Look) and t.path == "/tmp/x/img.png" and "date" in t.question
    t = parse_turn('{"look": {"path": ""}}')               # empty path -> not a look
    assert t is None
    # precedence: program > look > done
    t = parse_turn('{"look": {"path": "/a.png"}}\n'
                   '{"program": {"lang": "bash", "code": "ls"}}')
    assert isinstance(t, Program)
    t = parse_turn('{"look": {"path": "/a.png"}}\n{"done": {"checks": []}}')
    assert isinstance(t, Look)


def test_ask_parsing_and_precedence():
    t = parse_turn('{"ask": {"question": "Could you provide the missing file?"}}')
    assert isinstance(t, Ask)
    assert t.question == "Could you provide the missing file?"
    t = parse_turn('{"ask": {"question": "Need input"}}\n'
                   '{"done": {"checks": []}}')
    assert isinstance(t, Ask)
    t = parse_turn('{"program": {"lang": "bash", "code": "ls"}}\n'
                   '{"ask": {"question": "Need input"}}')
    assert isinstance(t, Program)


def test_ask_is_advertised_only_when_channel_is_attached():
    assert '"ask"' not in build_system()
    assert '"ask"' in build_system(ask_enabled=True)


def test_extract_plan():
    assert extract_plan("PLAN: recon 4, extract 8, decide 2, place 3, verify 3\n"
                        '{"program": {"lang": "bash", "code": "ls"}}'
                        ).startswith("recon 4")
    two = extract_plan("PLAN: old plan\nsome prose\nPLAN: revised plan\n{}")
    assert two == "revised plan"                      # latest revision wins
    assert extract_plan("no plan line here") == ""
    assert extract_plan("") == ""
    assert len(extract_plan("PLAN: " + "x" * 999)) == 300   # bounded


def test_trace_message_carries_plan_and_commit():
    class _T:                                          # minimal Trace stand-in
        stdout, exit_code, secs, timed_out = "out", 0, 1.0, False
    m = trace_message(_T(), 12, plan="recon then place", commit=False)
    assert "YOUR PLAN: recon then place" in m and "12 action turn(s) remain." in m
    assert "COMMIT PHASE" not in m
    m2 = trace_message(_T(), 3, plan="p", commit=True)
    assert "COMMIT PHASE" in m2


def test_trace_message_is_lossless_by_default():
    marker = "MEMORY-MIDDLE-MUST-SURVIVE"
    payload = "a" * 12000 + marker + "z" * 12000

    class _T:
        stdout, exit_code, secs, timed_out = payload, 0, 1.0, False

    rendered = trace_message(_T())
    assert payload in rendered
    assert marker in rendered
    assert "chars omitted" not in rendered


def test_check_gate():
    accepted, rejections, dropped = validate([
        {"desc": "real", "probe": "ls ~/target && echo PASS || echo FAIL"},   # ok
        {"desc": "constant", "probe": "echo PASS"},                            # rejected
        {"desc": "mutates", "probe": "rm -rf /home/x && echo PASS"},           # rejected
        {"desc": "redir-home", "probe": "ls / > ~/out && echo PASS"},          # rejected (non-scratch)
        {"desc": "no env", "probe": "true"},                                   # rejected
        "not-a-dict",                                                          # rejected
    ])
    assert len(accepted) == 1 and accepted[0].desc == "real", [c.desc for c in accepted]
    assert len(rejections) == 5 and not dropped
    a2, r2, d2 = validate([{"desc": "harmless redir",
                            "probe": "cat ~/f 2>/dev/null | head -1 && echo PASS || echo FAIL"}])
    assert len(a2) == 1 and not r2 and not d2, f"harmless 2>/dev/null wrongly rejected: {r2}"
    a3, r3, d3 = validate([])
    assert not a3 and r3, "empty checks must be rejected with guidance"


def test_v16_quoted_comparison_not_mutating():
    # THE v16 fix: a python '>' comparison inside quotes must NOT read as a shell
    # redirect (this false positive rejected 7 of 8 legitimate read probes on one run)
    a, r, d = validate([{"desc": "zipread",
        "probe": "python3 -c \"import zipfile; z=zipfile.ZipFile('/home/u/f.pptx'); "
                 "print('PASS' if len(z.read('ppt/slides/slide1.xml')) > 100 else 'FAIL')\""}])
    assert len(a) == 1 and not r, f"quoted '>' wrongly rejected: {r}"
    # and a genuine unquoted shell redirect to a non-scratch path is still caught
    a2, r2, _ = validate([{"desc": "real redir", "probe": "cat ~/f > ~/out && echo PASS"}])
    assert not a2 and r2


def test_v16_scratch_write_allowed():
    # a check may render to /tmp scratch and inspect it (render-and-compare)
    a, r, d = validate([{"desc": "render+compare",
        "probe": "solvespace-cli --render /tmp/ck.png ~/part.slvs && "
                 "python3 -c \"print('PASS')\" && test -f /tmp/ck.png && echo PASS"}])
    assert len(a) == 1 and not r, f"/tmp scratch write wrongly rejected: {r}"


def test_v16_eviction_keeps_content_over_proxies():
    # THE t056 bug: content checks must survive when structural proxies crowd capacity
    checks = ([{"desc": f"exist{i}", "probe": f"test -f ~/f{i} && echo PASS || echo FAIL"}
               for i in range(14)]
              + [{"desc": "content-psnr",
                  "probe": "python3 ~/psnr.py /tmp/out.mp4 && echo PASS || echo FAIL"},
                 {"desc": "content-grep", "probe": "grep -q OSWorld ~/out.txt && echo PASS"}])
    accepted, rejections, dropped = validate(checks, max_checks=12)
    assert len(accepted) == 12 and not rejections
    kept = {c.desc for c in accepted}
    assert "content-psnr" in kept and "content-grep" in kept, f"content evicted! {kept}"
    assert sum(1 for c in accepted if c.desc.startswith("exist")) == 10


def test_capacity_overflow_drops_but_does_not_reject():
    # seed9 bug: overflow beyond max_checks must not veto a done declaration
    many = [{"desc": f"c{i}", "probe": f"ls ~/f{i} && echo PASS || echo FAIL"}
            for i in range(15)]
    accepted, rejections, dropped = validate(many, max_checks=12)
    assert len(accepted) == 12
    assert not rejections, "capacity overflow must not be a soundness rejection"
    assert len(dropped) == 3


def test_look_in_program_envelope_with_lang():
    # t004 s21/s22 killer: look fields inside a program envelope, lang "look"
    t = parse_turn('{"program": {"lang": "look", "path": "/tmp/w/after-19.png", '
                   '"question": "what font?"}}')
    assert isinstance(t, Look) and t.path == "/tmp/w/after-19.png"


def test_bare_look_fields():
    t = parse_turn('{"path": "/tmp/w/s.png", "question": "layout?"}')
    assert isinstance(t, Look) and t.path == "/tmp/w/s.png"


def test_program_envelope_with_code_still_program():
    # a real program that HAPPENS to mention a path key must stay a program
    t = parse_turn('{"program": {"lang": "python", "code": "print(1)", '
                   '"path": "/tmp/x.png"}}')
    assert isinstance(t, Program) and t.code == "print(1)"



def test_size_triggered_fold_v92():
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.history_keep_pairs = 2
    cfg.fold_batch = 2
    cfg.ctx_high_water = 5000
    cfg.ctx_low_water = 3000
    old_chat = L.chat
    L.chat = lambda *a, **k: "WORK LOG: folded"
    try:
        h = []
        for i in range(10):                      # 10 pairs, ~800 chars each
            h += [{"role": "user", "content": "u" + "x" * 400},
                  {"role": "assistant", "content": "a" + "y" * 400}]
        # BELOW high water: no fold, even with 2 unfolded pairs beyond the window
        # (count mode would have folded here — that is the v9.2 difference)
        s1, c1 = L._fold_summary(h[:8], 0, "", cfg, None, 1)
        assert c1 == 0 and s1 == "", (c1, s1)
        # ABOVE high water (~8k chars): fold oldest pairs down under low water
        s2, c2 = L._fold_summary(h, 0, "", cfg, None, 2)
        assert c2 > 0
        size = len(s2) + sum(len(m["content"]) for m in h[c2 * 2:])
        assert size <= cfg.ctx_low_water, size
        # the keep window is never folded
        assert c2 <= len(h) // 2 - cfg.history_keep_pairs
        # legacy count trigger still works when high water is 0
        cfg.ctx_high_water = 0
        s3, c3 = L._fold_summary(h[:8], 0, "", cfg, None, 3)
        assert c3 == 2 and s3 == "WORK LOG: folded", (c3, s3)
    finally:
        L.chat = old_chat



def test_multi_program_extras_counted():
    # s15 killer: 7 program objects per reply, only the last runs — count the discards
    t = parse_turn('{"program": {"lang": "bash", "code": "a"}} '
                   '{"program": {"lang": "bash", "code": "b"}} '
                   '{"program": {"lang": "bash", "code": "c"}}')
    assert isinstance(t, Program) and t.code == "c" and t.extras == 2
    t2 = parse_turn('{"program": {"lang": "bash", "code": "only"}}')
    assert t2.extras == 0


def test_fold_floor_guard_v11():
    # seed13 thrash: keep-window alone above high water must NOT fold every turn
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.history_keep_pairs = 4
    cfg.fold_batch = 3
    cfg.ctx_high_water = 1000     # keep-window (4 pairs x ~800) alone exceeds this
    cfg.ctx_low_water = 500
    old_chat = L.chat
    L.chat = lambda *a, **k: "LOG"
    try:
        h = []
        for i in range(6):
            h += [{"role": "user", "content": "u" + "x" * 400},
                  {"role": "assistant", "content": "a" + "y" * 400}]
        # 6 pairs, 2 foldable (< fold_batch=3): guard refuses the 1-2-pair thrash fold
        s1, c1 = L._fold_summary(h, 0, "", cfg, None, 1)
        assert c1 == 0, c1
        # 7 pairs -> 3 foldable = exactly one batch: folds once, then stops at guard
        h += [{"role": "user", "content": "u" + "x" * 400},
              {"role": "assistant", "content": "a" + "y" * 400}]
        s2, c2 = L._fold_summary(h, 0, "", cfg, None, 2)
        assert c2 == 3, c2
    finally:
        L.chat = old_chat



def test_look_paths_and_region_v13():
    t = parse_turn('{"look": {"paths": ["/a.png", "/b.png", "/c.png", '
                   '"/d.png", "/e.png"], '
                   '"region": [10, 20, 300, 400], "question": "compare views"}}')
    assert isinstance(t, Look) and t.paths == [
        "/a.png", "/b.png", "/c.png", "/d.png", "/e.png"]
    assert t.region == [10, 20, 300, 400]
    t2 = parse_turn('{"look": {"path": "/one.png", "question": "q"}}')
    assert t2.paths == ["/one.png"] and t2.region is None and t2.path == "/one.png"


def test_look_normalizes_screen_prefix_accidentally_joined_to_absolute_path():
    turn = parse_turn(
        '{"look": {"path": "screen: /mnt/verifier/reference.png", '
        '"question": "inspect reference"}}')

    assert isinstance(turn, Look)
    assert turn.paths == ["/mnt/verifier/reference.png"]

    live_screen = parse_turn(
        '{"look": {"path": "screen:1.0", "question": "inspect desktop"}}')
    assert live_screen.paths == ["screen:1.0"]


def test_imagery_tiling_v13():
    import io
    try:
        from PIL import Image
    except ImportError:
        return                       # graceful-degrade env: helper passes through
    from core.imagery import prepare_look_images
    big = Image.new("RGB", (3000, 2200), (200, 200, 200))
    buf = io.BytesIO(); big.save(buf, format="PNG")
    atts, note = prepare_look_images([buf.getvalue()])
    assert len(atts) == 5, len(atts)             # overview + 4 tiles
    assert "native-resolution" in note and "top-left" in note
    small = Image.new("RGB", (600, 400), (10, 10, 10))
    buf2 = io.BytesIO(); small.save(buf2, format="JPEG")
    atts2, note2 = prepare_look_images([buf2.getvalue()])
    assert len(atts2) == 1 and "full view" in note2
    atts3, note3 = prepare_look_images([buf.getvalue()], region=[100, 100, 900, 700])
    assert len(atts3) == 1 and "requested region" in note3
    # Multi-image comparisons preserve every requested item exactly once. Large-image
    # tiling must not consume an attachment budget and silently drop later paths.
    atts4, note4 = prepare_look_images([buf.getvalue()] * 8)
    assert len(atts4) == 8, len(atts4)
    assert "image 8: full view" in note4, note4



def test_char_budget_keep_window_v14():
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.compaction = "summary"; cfg.history_keep_pairs = 30
    cfg.fold_batch = 2; cfg.ctx_high_water = 100_000; cfg.ctx_low_water = 62_000
    cfg.keep_chars = 40_000
    old = L.chat; L.chat = lambda *a, **k: "LOG"
    try:
        fat = []
        for i in range(20):                     # 20 pairs x ~10k chars = 200k total
            fat += [{"role": "user", "content": "u" + "x" * 5000},
                    {"role": "assistant", "content": "a" + "y" * 5000}]
        # effective keep = 40k budget / 10k per pair = 4 -> floored to 6
        assert L._effective_keep(fat, cfg) == 6
        s1, c1 = L._fold_summary(fat, 0, "", cfg, None, 1)
        size = len(s1) + sum(len(m["content"]) for m in fat[c1 * 2:])
        assert size <= cfg.ctx_low_water, size   # reachable now (was not
        assert c1 == 14, c1                      # under pair-window: 30>20 kept all)
        # thin traces: full 30-pair window behavior unchanged
        thin = []
        for i in range(40):
            thin += [{"role": "user", "content": "u" + "x" * 300},
                     {"role": "assistant", "content": "a" + "y" * 300}]
        assert L._effective_keep(thin, cfg) == 30
        # legacy: keep_chars=0 -> fixed pairs
        cfg.keep_chars = 0
        assert L._effective_keep(fat, cfg) == 30
    finally:
        L.chat = old



def test_large_program_trace_is_archived_losslessly_but_bounded_in_live_context():
    from env.vm import Trace

    raw = "HEAD" + "x" * 1_200_000 + "TAIL"
    trace = Trace(stdout=raw, exit_code=0)
    message = trace_message(trace, context_max_chars=500_000)

    assert trace.stdout == raw
    assert len(message) < 510_000
    assert "HEAD" in message and "TAIL" in message
    assert "chars omitted; full output archived" in message


def test_unchanged_baseline_keeps_complete_live_program_output():
    from pathlib import Path
    from config.settings import load
    from env.vm import Trace

    repo = Path(__file__).resolve().parents[1]
    cfg = load(str(repo / "config/osworld_v2_0808_glm53_k3_agentic_baseline.yaml"))
    raw = "H" * 300_000 + "MIDDLE_EVIDENCE" + "T" * 300_000
    message = trace_message(
        Trace(stdout=raw, exit_code=0), head=cfg.trace_head, tail=cfg.trace_tail,
        context_max_chars=cfg.trace_context_max_chars)

    assert raw in message
    assert "chars omitted" not in message


def test_v17_infra_detection():
    from env.vm import Trace
    ro = ("mktemp: failed to create file via template '/tmp/forge_XXXXXX.py': "
          "Read-only file system")
    assert "Read-only file system" in ro and "mktemp" in ro
    t = Trace(stdout="hello\n[exit 0]", exit_code=0)
    assert not t.infra_fail


def test_actor_loop_normalizes_unclassified_run_wrapper_failure():
    import core.loop as L
    from env.vm import Trace

    wrapper_failure = Trace(
        stdout=(
            "[exit 2]\n[stderr] mktemp: failed to create file via template "
            "‘/tmp/forge_XXXXXX.sh’: Read-only file system\n"
            "/bin/sh: cannot create /tmp/forge_run_abc123.log: "
            "Read-only file system"),
        exit_code=None,
        infra_fail=False,
    )
    assert L._normalize_program_transport_failure(wrapper_failure) is True
    assert wrapper_failure.infra_fail is True

    genuine_program_output = Trace(
        stdout=(
            "mktemp: failed to create file via template "
            "‘/tmp/forge_XXXXXX.sh’: Read-only file system\n[exit 0]"),
        exit_code=0,
        infra_fail=False,
    )
    assert L._normalize_program_transport_failure(genuine_program_output) is False
    assert genuine_program_output.infra_fail is False


@pytest.mark.parametrize("no_progress", [False, True])
@pytest.mark.parametrize("flush_failure", [False, True])
def test_verifier_infra_retry_survives_dead_scratch_and_rolls_back_before_retry(
        monkeypatch, tmp_path, no_progress, flush_failure):
    import json
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from core.verifier_runtime import (
        AgenticVerifierInfrastructureError, AgenticVerifierNoProgressError,
    )
    from env.vm import Trace

    cfg = load(None)
    cfg.model = "actor"
    cfg.max_iters = 3
    cfg.wall_clock_secs = 60
    cfg.agent_decided_stop = True
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.agentic_verifier_config = "verifier.yaml"
    cfg.verifier_continuity = True
    cfg.verifier_persist_scratch = True
    cfg.verifier_infra_retries = 1
    replies = iter((
        '{"program":{"lang":"bash","code":"printf work"}}',
        '{"done":{"checks":[{"desc":"artifact exists",'
        '"probe":"test -f /tmp/artifact && echo PASS"}]}}',
    ))

    class VM:
        def run_script(self, *_args, **_kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0)

        def run_command(self, command, **_kwargs):
            return "PASS" if "echo PASS" in command else ""

    class Executor:
        def __init__(self, vm):
            self.vm = vm
            self.closed = False

        def export_scratch(self):
            raise AgenticVerifierInfrastructureError(
                "controller unavailable while exporting scratch")

        def close(self):
            self.closed = True

    vm = VM()
    failed_executor = Executor(vm)
    verify_calls = []

    def fake_verify(*_args, **kwargs):
        verify_calls.append(kwargs)
        if len(verify_calls) == 1:
            kwargs["session"]._program_executor = failed_executor
            if no_progress:
                raise AgenticVerifierNoProgressError("no substantive progress")
            raise AgenticVerifierInfrastructureError("trusted mirror unavailable")
        return "pass", "candidate verified after rebuilt mirror"

    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr(L, "verify_independent", fake_verify)
    if flush_failure and no_progress:
        def fail_flush(*_args):
            raise OSError('diagnostic disk full')
        monkeypatch.setattr(ArtifactSink, 'save_transcript', fail_flush)
    if no_progress:
        with pytest.raises(AgenticVerifierNoProgressError):
            L.run_attempt("make artifact", vm, cfg, ArtifactSink(str(tmp_path / "run")))
        assert len(verify_calls) == 1
        assert failed_executor.closed is False, "preserve the boundary for recovery"
        if not flush_failure:
            transcript = json.loads((tmp_path / 'run/transcript.json').read_text())
            assert transcript['system']
            assert any('artifact exists' in str(m) for m in transcript['messages'])
            assert any('work' in str(m) for m in transcript['messages'])
            assert not (tmp_path / 'run/result.json').exists()
        return
    result, _ = L.run_attempt(
        "make artifact", vm, cfg, ArtifactSink(str(tmp_path / "run")))

    assert result.status == "done"
    assert len(verify_calls) == 2
    assert verify_calls[1]["continue_candidate"] is True
    assert failed_executor.closed is True
    assert result.infra_pauses == 1


def test_channel_restart_recovers_same_actor_without_replaying_action(tmp_path):
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.max_iters = 3
    cfg.wall_clock_secs = 60
    cfg.history_keep_pairs = 0
    cfg.independent_verify = False
    cfg.practice_mode = True
    replies = [
        '{"program":{"lang":"bash","code":"python heavy.py"}}',
        '{"program":{"lang":"bash","code":"echo resumed"}}',
        '{"done":null}',
    ]
    actor_users = []

    def fake_chat(_model, _system, user, **_kwargs):
        actor_users.append(user)
        return replies.pop(0)

    class VM:
        def __init__(self):
            self.codes = []
            self.recoveries = 0

        def run_command(self, _cmd, **_kwargs):
            return ""

        def run_script(self, _lang, code, **_kwargs):
            self.codes.append(code)
            if len(self.codes) == 1:
                return Trace(
                    stdout="[channel error: ConnectionError — controller restarting]",
                    secs=2.0, infra_fail=True)
            return Trace(stdout="resumed\n[exit 0]", exit_code=0, secs=0.1)

        def wait_for_controller(self, **_kwargs):
            self.recoveries += 1
            return True, "controller recovered; 2 consecutive probes passed"

    vm = VM()
    old_chat = L.chat
    L.chat = fake_chat
    try:
        result, _history = L.run_attempt(
            "practice", vm, cfg, ArtifactSink(str(tmp_path / "recovery")))
    finally:
        L.chat = old_chat

    assert result.status == "done"
    assert vm.recoveries == 1
    assert vm.codes == ["python heavy.py", "echo resumed"]
    assert "NOT replayed" in actor_users[1]
    assert "controller recovered" in actor_users[1]


def test_v17_surface_delta():
    import core.loop as L
    class FakeVM:
        def __init__(self, seq): self.seq = seq; self.i = 0
        def run_command(self, *a, **k):
            out = self.seq[self.i]; self.i = min(self.i + 1, len(self.seq) - 1); return out
    base = "1000.0 /home/user/Desktop/form.pdf\n1000.0 /home/user/Desktop/ref.jpg"
    after = ("1000.0 /home/user/Desktop/form.pdf\n1000.0 /home/user/Desktop/ref.jpg\n"
             "2000.0 /home/user/Desktop/form-filled.pdf")
    vm = FakeVM([base, after])
    baseline = L._snapshot(vm)
    assert "/home/user/Desktop/form.pdf" in baseline
    delta = L._surface_delta(vm, baseline)
    assert "CREATED this run" in delta and "form-filled.pdf" in delta
    vm2 = FakeVM([base, base])
    assert L._surface_delta(vm2, L._snapshot(vm2)) == ""


def test_unified_surface_delta_hides_actor_private_path_names():
    import core.loop as L

    class FakeVM:
        def run_command(self, *args, **kwargs):
            assert kwargs["cap"] == 0
            return (
                "2000.0 /home/user/work/private_strategy.md\n"
                "2000.0 /home/user/Desktop/candidate.xlsx")

    delta = L._surface_delta(
        FakeVM(), {"/home/user/Desktop/input.xlsx": "1000.0"},
        excluded_paths=("/home/user/work",))

    assert "candidate.xlsx" in delta
    assert "private_strategy" not in delta


def _v18_harness(find_seq, pivot_enabled=True):
    """Shared rig for the v18 restart tests: an actor that repeats one program until
    the repeat-cap stalls each attempt, a verifier that never rescues, and a VM whose
    find output follows ``find_seq`` (baseline, stall-exit, resume-baseline, ...)."""
    import tempfile

    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.strategy_pivot = pivot_enabled
    cfg.max_iters = 40
    cfg.max_consec_repeat = 3
    cfg.independent_verify = True
    cfg.max_resumes = 1
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0

    class VM:
        def __init__(self): self.i = 0
        def run_command(self, cmd, timeout=30, **kw):
            if cmd.startswith("find /home/user"):
                out = find_seq[min(self.i, len(find_seq) - 1)]
                self.i += 1
                return out
            return "PASS"
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, path, max_bytes=0):
            return None, "n"

    calls = {"review": 0}
    PROG = '{"program": {"lang": "bash", "code": "echo same"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if "post-mortem" in system:
            calls["review"] += 1
            return "review-text-XYZ: tried A via B; stuck at C; try D instead"
        return PROG
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        return "wrong", "not there"

    old_chat, old_verify = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        sink = ArtifactSink(tempfile.mkdtemp(prefix="v18_"))
        res, hist = L.run_with_resume("t", VM(), cfg, sink)
    finally:
        L.chat, L.verify_independent = old_chat, old_verify
    return res, hist, calls


def test_v18_pivot_on_zero_delta():
    base = "1000.0 /home/user/Desktop/a.txt"
    res, hist, calls = _v18_harness([base, base, base, base])   # nothing ever changes
    assert res.resumes == 1
    tags = [v for _, v in res.inspections]
    assert "pivot1" in tags and "resume1" not in tags, tags
    assert calls["review"] == 1                       # exactly one post-mortem call
    openings = [m["content"] for m in hist if m["role"] == "user"
                and "MATERIALLY DIFFERENT" in str(m["content"])]
    assert openings and "review-text-XYZ" in openings[0]


def test_v18_ab_toggle_off_forces_continuation():
    base = "1000.0 /home/user/Desktop/a.txt"
    res, hist, calls = _v18_harness([base, base, base, base], pivot_enabled=False)
    assert res.resumes == 1
    tags = [v for _, v in res.inspections]
    assert "resume1" in tags and "pivot1" not in tags, tags   # control arm = pure v15
    assert calls["review"] == 0


def test_v18_continuation_when_work_banked():
    base = "1000.0 /home/user/Desktop/a.txt"
    grown = base + "\n2000.0 /home/user/Desktop/out.csv"
    res, hist, calls = _v18_harness([base, grown, grown, grown])  # attempt banked a file
    assert res.resumes == 1
    tags = [v for _, v in res.inspections]
    assert "resume1" in tags and "pivot1" not in tags, tags
    assert calls["review"] == 0                       # no post-mortem on banked work
    assert any(m["role"] == "user" and "RETAINS" in str(m["content"]) for m in hist)


def test_phase0_interpreter_body_mutation_caught():
    # Phase-0 safety fix: a check that deletes/overwrites the deliverable INSIDE a
    # python/perl body must be rejected — it runs against the live scored machine.
    from core.checks import _nonscratch_mutations as M
    assert M("python3 -c \"import shutil; shutil.rmtree('/home/user/Documents'); print('PASS')\"")
    assert M("python3 -c \"import os; os.remove('/home/user/Desktop/out.pdf'); print('PASS')\"")
    assert M("python3 -c \"open('/home/user/f','w').write('x'); print('PASS')\"")
    assert M("perl -e \"unlink '/home/u/x'; print 'PASS'\"")
    # legit content READS inside a body stay allowed (no false positive)
    assert not M("python3 -c \"import zipfile; z=zipfile.ZipFile('/home/u/f.pptx'); "
                 "print('PASS' if len(z.read('ppt/slides/slide1.xml')) > 100 else 'FAIL')\"")
    assert not M("python3 -c \"import openpyxl; wb=openpyxl.load_workbook('/home/u/x.xlsx'); "
                 "print('PASS' if wb.active['A1'].value else 'FAIL')\"")
    # a write to /tmp scratch inside a body is allowed (render-and-compare, v16)
    assert not M("python3 -c \"open('/tmp/ck.png','wb').write(b'x'); print('PASS')\"")


def test_phase0_verifier_requires_content_probe():
    # Phase-0: a verifier "pass" must rest on a probe that READ deliverable content;
    # existence/metadata alone (ls/test) downgrades to unverified (the docstring's
    # long-promised guard, now actually enforced).
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.verifier_probes = 4

    class VM:
        def run_command(self, cmd, timeout=30, **k):
            if "grep" in cmd or "python" in cmd:
                return "PASS cell A1=42"                  # content probe: informative
            if cmd.startswith("ls") or cmd.startswith("test"):
                return "form.xlsx\nref.pdf"               # metadata: informative, not content
            return ""

    def run(seq):
        box = list(seq)
        def fake(model, system, user, **k):
            return box.pop(0) if box else '{"verdict":"unverified","findings":"x"}'
        old = V.chat
        V.chat = fake
        try:
            return V.verify_independent("t", VM(), cfg)[0]
        finally:
            V.chat = old

    # existence-only pass -> downgraded
    assert run(['{"probes": ["ls /home/user/Desktop"]}',
                '{"verdict":"pass","findings":"file exists"}']) == "unverified"
    # content-backed pass -> survives
    assert run(['{"probes": ["grep -q 42 /home/user/Desktop/form.xlsx && echo PASS"]}',
                '{"verdict":"pass","findings":"content correct"}']) == "pass"


def test_v21_dup_counting():
    # A: parse_turn reports same-kind duplication so the loop can reject decoder-
    # degeneration turns. Two programs -> dup 2 (the disaster: last-wins runs a
    # ritual/duplicate object); benign cross-kind hedge -> dup 1.
    from core.actor import Done as _D
    t = parse_turn('{"program":{"lang":"bash","code":"real work"}}\n'
                   '{"program":{"lang":"bash","code":"echo done"}}')     # 2 programs
    assert isinstance(t, Program) and t.dup == 2, t
    t = parse_turn('{"program":{"lang":"bash","code":"x"}}\n{"done": null}')  # prog+done
    assert isinstance(t, Program) and t.dup == 1, t                      # precedence, benign
    t = parse_turn('{"look":{"path":"/a.png","question":"q"}}\n'
                   '{"look":{"path":"/a.png","question":"q"}}\n'
                   '{"look":{"path":"/a.png","question":"q"}}')          # 3 looks
    assert isinstance(t, Look) and t.dup == 3, t
    t = parse_turn('{"program":{"lang":"bash","code":"ls"}}')            # single clean
    assert isinstance(t, Program) and t.dup == 1, t


def _v21_drive(strict, replies, max_iters=8):
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = max_iters
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = strict
    cfg.max_consec_degen = 3
    cfg.wall_clock_secs = 3600
    ran = {"n": 0}
    box = list(replies)
    def fake_chat(*a, **k):
        return box.pop(0) if box else replies[-1]
    class VM:
        def run_script(self, lang, code, timeout=600):
            ran["n"] += 1
            return Trace(stdout="hi\n[exit 0]", exit_code=0, secs=0.1)
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    old = L.chat
    L.chat = fake_chat
    try:
        res, hist = L.run_attempt("t", VM(), cfg,
                                  ArtifactSink(tempfile.mkdtemp(prefix="v21_")))
    finally:
        L.chat = old
    return res, hist, ran


def test_complete_role_opening_bypasses_actor_plan_wrapper():
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.max_iters = 2
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    opening = "VERIFIER AGENT — complete role-owned opening"
    seen = {}

    def fake_chat(_model, _system, user, **_kwargs):
        seen["user"] = user
        return '{"program":{"lang":"bash","code":"echo evidence"}}'

    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="evidence\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **_kwargs):
            return ""

    old = L.chat
    L.chat = fake_chat
    try:
        result, history = L.run_attempt(
            opening, VM(), cfg,
            ArtifactSink(tempfile.mkdtemp(prefix="role_opening_")),
            instruction_is_complete_opening=True,
            terminal_handoff_ready=lambda: True)
    finally:
        L.chat = old

    assert result.status == "done"
    assert seen["user"] == opening
    assert history[0]["content"] == opening
    assert "PLAN:" not in seen["user"]


def test_v21_degenerate_turn_runs_nothing_then_pivots():
    DUP = ('{"program":{"lang":"bash","code":"real"}}'
           '{"program":{"lang":"bash","code":"echo done"}}')   # 2 programs = degenerate
    res, hist, ran = _v21_drive(True, [DUP])
    assert ran["n"] == 0, ran                                  # NOTHING executed
    assert res.status == "stalled", res.status                 # escalated after 3
    assert any("MULTIPLE action objects" in str(m["content"])
               for m in hist if m["role"] == "user")           # re-ask was sent


def test_v21_control_runs_last_program():
    # strict off = pre-v21 behavior: a multi-program reply still runs (the last one)
    DUP = ('{"program":{"lang":"bash","code":"real"}}'
           '{"program":{"lang":"bash","code":"echo done"}}')
    res, hist, ran = _v21_drive(False, [DUP], max_iters=3)
    assert ran["n"] >= 1, ran                                  # old behavior executed


def test_v21_clean_single_action_unaffected():
    # a normal single-program run is untouched by strict mode
    res, hist, ran = _v21_drive(True,
        ['{"program":{"lang":"bash","code":"ls"}}',
         '{"done": {"checks": [{"desc":"d","probe":"ls / && echo PASS"}]}}'])
    assert ran["n"] >= 1, ran                                  # the clean program ran


_B_PROG = '{"program":{"lang":"bash","code":"echo work"}}'
_B_DONE = '{"done":{"checks":[{"desc":"d","probe":"ls / && echo PASS"}]}}'


def _b_drive(budget_cond, replies, verdicts, max_iters=12, commit_frac=0.75):
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = max_iters
    cfg.independent_verify = True
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False          # isolate B from A
    cfg.budget_conditional_accept = budget_cond
    cfg.commit_frac = commit_frac
    cfg.unverified_accept = 2
    cfg.max_wrong_before_pivot = 3
    cfg.wall_clock_secs = 3600
    rbox, vbox = list(replies), list(verdicts)
    def fake_chat(*a, **k):
        return rbox.pop(0) if rbox else replies[-1]
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        return (vbox.pop(0) if vbox else verdicts[-1]), "finding text"
    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        res, _ = L.run_attempt("t", VM(), cfg,
                               ArtifactSink(tempfile.mkdtemp(prefix="b_")))
    finally:
        L.chat, L.verify_independent = oc, ov
    return res


def test_v21_b_unverified_does_not_accept_before_commit():
    # B: early unverified (budget left) must NOT auto-accept — it bounces; done-spam
    # then stalls (-> fresh-context pivot upstream), never a soft-accept.
    res = _b_drive(True, [_B_PROG] + [_B_DONE] * 6, ["unverified"] * 6, commit_frac=0.9)
    assert res.status == "stalled", res.status          # refused to soft-accept early
    assert not any(v == "unverified" and False for _, v in res.inspections)  # sanity


def test_v21_b_unverified_accepts_in_commit_phase():
    # B: once in the commit phase (budget mostly spent) and no wrongs preceded, the
    # 2x-unverified accept is allowed again.
    res = _b_drive(True, [_B_PROG, _B_PROG, _B_DONE, _B_DONE], ["unverified", "unverified"],
                   max_iters=10, commit_frac=0.2)   # commit at iter>=2
    assert res.status == "done", res.status


def test_v21_b_repeated_wrong_keeps_same_actor_until_budget():
    # A Verifier WRONG is semantic feedback. Even after three findings, it must not
    # discard the Actor conversation or trigger a fresh-model pivot.
    res = _b_drive(True, [_B_PROG, _B_DONE, _B_PROG, _B_DONE, _B_PROG, _B_DONE],
                   ["wrong", "wrong", "wrong"])
    assert res.status == "budget", res.status
    assert sum(1 for _, v in res.inspections if v == "wrong") >= 3


def test_v21_b_control_accepts_early_2x_unverified():
    # control (flag off) = pre-v21: the 2nd unverified accepts regardless of budget.
    res = _b_drive(False, [_B_PROG, _B_DONE, _B_DONE], ["unverified", "unverified"],
                   commit_frac=0.9)
    assert res.status == "done", res.status


def test_v21_ctx_cap_worklog_line_boundary():
    from core.loop import _cap_worklog, _fold_input
    assert _cap_worklog("short\nlog", 100) == "short\nlog"          # under cap unchanged
    text = "\n".join(f"line{i} value={i}" for i in range(400))
    out = _cap_worklog(text, 500)
    assert len(out) < len(text) and "truncated at the char cap" in out
    kept = out.split("\n[... WORK LOG")[0]
    assert all(l.startswith("line") for l in kept.splitlines() if l)  # no mid-line chop
    # _fold_input keeps head AND tail (errors land at the tail)
    s = "HEAD_START" + "x" * 9000 + "TRACEBACK_TAIL"
    fi = _fold_input(s, 1500, 2500)
    assert fi.startswith("HEAD_START") and fi.endswith("TRACEBACK_TAIL") and len(fi) < len(s)


def test_v21_ctx_task_pinned_after_fold():
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = 12
    cfg.independent_verify = False
    cfg.strict_one_action = False
    cfg.history_keep_pairs = 30
    cfg.keep_chars = 200          # tiny verbatim window
    cfg.ctx_high_water = 300      # tiny -> fold early
    cfg.ctx_low_water = 150
    cfg.fold_batch = 2            # fold in small chunks so a fold happens fast
    cfg.wall_clock_secs = 3600
    TASK = "UNIQUE_TASK_MARKER_XYZ do the specific thing"
    seen = {"pinned": False}
    PROG = '{"program":{"lang":"bash","code":"echo a longish output line to grow context here"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if system == L.SUMMARIZER_SYSTEM:
            return "WORK LOG\n- CURRENT STATE: working"          # produce a summary
        if history and any("TASK (verbatim, unchanged" in str(m.get("content", ""))
                           and "UNIQUE_TASK_MARKER_XYZ" in str(m.get("content", ""))
                           for m in history):
            seen["pinned"] = True
        return PROG
    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="x" * 400 + "\n[exit 0]", exit_code=0, secs=0.1)
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc = L.chat
    L.chat = fake_chat
    try:
        L.run_attempt(TASK, VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="ctx_")))
    finally:
        L.chat = oc
    assert seen["pinned"], "task instruction not pinned into the compacted context"


def test_v21_escalation_model_on_stuck_restart():
    # model-escalation: a stuck run's fresh pivot/resume attempt runs on the DIFFERENT
    # model; the first (primary) attempt runs on M3. M3 stays primary.
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/m3"
    cfg.escalation_model = "escalation/qwen"
    cfg.max_iters = 40
    cfg.max_consec_repeat = 3        # identical program+output -> stall fast
    cfg.independent_verify = True
    cfg.max_resumes = 1
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False
    cfg.wall_clock_secs = 3600
    models_seen = []
    verify_pairs = []                                # (actor_model, resolved_verifier_model)
    PROG = '{"program":{"lang":"bash","code":"echo same"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        models_seen.append(model)
        return PROG                                  # identical forever -> repeat stall
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        verify_pairs.append((cfg.model, cfg.verifier_model or cfg.model))  # what verifier.py resolves
        return "wrong", "not there"                  # never rescue -> stays stalled
    class VM:
        def run_command(self, cmd, timeout=30, **kw):
            return "" if cmd.startswith("find /home/user") else "PASS"  # empty -> pivot
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        res, _ = L.run_with_resume("t", VM(), cfg,
                                   ArtifactSink(tempfile.mkdtemp(prefix="esc_")))
    finally:
        L.chat, L.verify_independent = oc, ov
    assert "primary/m3" in models_seen, "primary attempt didn't run on M3"
    assert "escalation/qwen" in models_seen, "stuck restart didn't escalate to the model"
    assert any(":esc" in str(v) for _, v in res.inspections), res.inspections
    # cross-model verification: on the escalated attempt the ACTOR is the escalation
    # model but the VERIFIER is pinned to the original model (not self-verify).
    esc_verify = [v for a, v in verify_pairs if a == "escalation/qwen"]
    assert esc_verify, "escalated attempt never reached the verifier"
    assert all(v == "primary/m3" for v in esc_verify), \
        f"escalated attempt self-verified instead of cross-checking: {verify_pairs}"
    # sanity: the primary (non-escalated) attempt still verifies on its own model
    assert ("primary/m3", "primary/m3") in verify_pairs, verify_pairs


def test_v21_no_escalation_when_unset():
    # default (escalation_model="") -> the restart stays on the primary model
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None); cfg.model = "primary/m3"; cfg.escalation_model = ""
    cfg.max_iters = 40; cfg.max_consec_repeat = 3; cfg.independent_verify = True
    cfg.max_resumes = 1; cfg.history_keep_pairs = 0; cfg.strict_one_action = False
    cfg.wall_clock_secs = 3600
    seen = set()
    def fc(model, system, user, **k): seen.add(model); return '{"program":{"lang":"bash","code":"echo same"}}'
    def fv(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None): return "wrong", "x"
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "" if cmd.startswith("find /home/user") else "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fc, fv
    try:
        res, _ = L.run_with_resume("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="noesc_")))
    finally:
        L.chat, L.verify_independent = oc, ov
    assert seen == {"primary/m3"}, seen                  # only the primary model used
    assert not any(":esc" in str(v) for _, v in res.inspections)


def _tiny_png():
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (8, 8), (10, 20, 30)).save(b, format="PNG")
    return b.getvalue()


def test_v22_vision_tool_look_delegates_to_vlm():
    # vision_model set: a look routes image+question to the VLM; the primary stays BLIND
    # (image=None) and gets the VLM's TEXT answer in its next message.
    import tempfile
    import core.loop as L
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/glm"; cfg.vision_model = "eyes/vlm"
    cfg.max_iters = 6; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    png = _tiny_png()
    calls = []
    script = ['{"look": {"path": "/home/user/vase.png", "question": "how many flowers?"}}',
              '{"done": null}']
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        kind = "VISION" if system == VISION_SYSTEM else "PRIMARY"
        calls.append({"model": model, "kind": kind, "img": image is not None, "user": user})
        if kind == "VISION":
            return "FAKE_VISION_ANSWER: I count 3 flowers. ALSO VISIBLE: a blue rim."
        return script.pop(0) if script else '{"done": null}'
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return png, None
    import core.eyes as E                       # v30: the eyes call happens inside core.eyes
    oc, oe = L.chat, E.chat; L.chat = E.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="v22_")))
    finally:
        L.chat, E.chat = oc, oe
    vis = [c for c in calls if c["kind"] == "VISION"]
    assert vis and vis[0]["model"] == "eyes/vlm" and vis[0]["img"], "look didn't reach the VLM with an image"
    # the primary call AFTER the look must be BLIND and carry the VLM's text answer
    post = next(c for c in calls if c["kind"] == "PRIMARY" and "FAKE_VISION_ANSWER" in c["user"])
    assert not post["img"], "primary was handed pixels — should stay blind"
    assert "VISION TOOL REPORT" in post["user"]
    assert not any(c["kind"] == "PRIMARY" and c["img"] for c in calls), "no primary call may carry an image"
    print("v22 vision-tool look: PASS (VLM sees, primary blind, text delivered)")


def test_v22_multi_path_look_preserves_every_requested_image():
    """No parser, fetch-loop, or imagery-stage ceiling may drop comparison paths."""
    import json
    import tempfile
    import core.loop as L
    import core.eyes as E
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.model = "primary/glm"; cfg.vision_model = "eyes/vlm"; cfg.look_ensemble = 1
    cfg.max_iters = 3; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    paths = [f"/home/user/frame_{i}.png" for i in range(8)]
    png = _tiny_png(); fetched = []; vision_counts = []
    script = [json.dumps({"look": {"paths": paths, "question": "compare all"}}),
              '{"done": null}']

    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            vision_counts.append(len(image) if isinstance(image, list) else 1)
            return "all eight images compared"
        return script.pop(0) if script else '{"done": null}'

    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, path, max_bytes=None):
            fetched.append(path)
            return png, ""

    old_loop_chat, old_eyes_chat = L.chat, E.chat
    L.chat = E.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg,
                      ArtifactSink(tempfile.mkdtemp(prefix="v22_multi_")))
    finally:
        L.chat, E.chat = old_loop_chat, old_eyes_chat

    assert fetched == paths, (fetched, paths)
    assert vision_counts == [len(paths)], vision_counts


def test_v22_no_vision_model_legacy_attach():
    # vision_model unset: legacy path — the image is attached to the PRIMARY itself.
    import tempfile
    import core.loop as L
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/m3"; cfg.vision_model = ""       # <- legacy
    cfg.max_iters = 6; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    png = _tiny_png(); calls = []
    script = ['{"look": {"path": "/home/user/x.png", "question": "what is shown?"}}', '{"done": null}']
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        calls.append({"kind": "VISION" if system == VISION_SYSTEM else "PRIMARY", "img": image is not None})
        return script.pop(0) if (system != VISION_SYSTEM and script) else '{"done": null}'
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return png, None
    oc = L.chat; L.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="v22b_")))
    finally:
        L.chat = oc
    assert not any(c["kind"] == "VISION" for c in calls), "no VLM should be called when vision_model unset"
    assert any(c["kind"] == "PRIMARY" and c["img"] for c in calls), "legacy: image must attach to the primary"
    print("v22 legacy attach: PASS (no VLM, image on primary)")


def test_v22_1_vision_agent_multiround():
    # vision_rounds>1: the eyes become an AGENT — inspect (grid/crop) over rounds, then
    # commit to a confident answer; the formatted answer (text+confidence+context) returns.
    import core.loop as L
    from core.actor import VISION_AGENT_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = 5
    png = _tiny_png(); calls = []
    script = ['{"inspect": {"op": "grid", "rows": 3, "cols": 1, "note": "split vase"}}',
              '{"inspect": {"op": "crop", "region": [0,0,4,4], "note": "zoom upper"}}',
              '{"answer": {"text": "27 flowers", "confidence": "high", "also_visible": "blue rim"}}']
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        calls.append({"model": model, "agent_sys": system == VISION_AGENT_SYSTEM, "img": image is not None})
        return script.pop(0) if script else '{"answer": {"text": "x", "confidence": "low"}}'
    oc = L.chat; L.chat = fake_chat
    try:
        ans = L._vision_agent("eyes/vlm", "count the flowers", [png], cfg)
    finally:
        L.chat = oc
    assert len(calls) >= 3, calls                              # multiple inspect rounds
    assert all(c["model"] == "eyes/vlm" and c["agent_sys"] and c["img"] for c in calls)
    assert "27 flowers" in ans and "confidence: high" in ans and "ALSO VISIBLE" in ans, ans
    print("v22.1 vision agent: PASS (multi-round inspect->answer, formatted)")


def test_v22_1_final_demand_forces_answer():
    # if the agent never volunteers an answer, the final-demand call extracts one.
    import core.loop as L
    from config.settings import load
    cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = 3
    png = _tiny_png(); n = {"i": 0}
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        n["i"] += 1
        if "budget exhausted" in user:                         # final demand
            return '{"answer": {"text": "final count 12", "confidence": "low"}}'
        return '{"inspect": {"op": "full"}}'                   # inspect forever otherwise
    oc = L.chat; L.chat = fake_chat
    try:
        ans = L._vision_agent("eyes/vlm", "count", [png], cfg)
    finally:
        L.chat = oc
    assert "final count 12" in ans, ans
    assert n["i"] == cfg.vision_rounds + 1, n                  # 3 rounds + 1 final demand
    print("v22.1 final-demand: PASS")


def test_v22_1_dispatch_rounds_gate():
    # _vlm_look routes to the AGENT when vision_rounds>1; rounds<=1 goes through the
    # v30 look-ensemble (which at look_ensemble=1 IS the single-shot path — its eyes
    # chat call happens inside core.eyes, so patch there).
    import core.eyes as E
    import core.loop as L
    from core.actor import VISION_AGENT_SYSTEM
    from config.settings import load
    png = _tiny_png()
    for rounds, want_agent in [(1, False), (4, True)]:
        cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = rounds
        seen = {"agent": False, "ensemble": False}
        def fake_agent_chat(model, system, user, max_tokens=0, temperature=0.0,
                            reasoning_effort="", history=None, image=None, **_kw):
            seen["agent"] = system == VISION_AGENT_SYSTEM
            return '{"answer": {"text": "ok", "confidence": "high"}}'
        def fake_eyes_chat(model, system, user, max_tokens=0, temperature=0.0,
                           reasoning_effort="", history=None, image=None, **_kw):
            seen["ensemble"] = True
            return "ok"
        oc, oe = L.chat, E.chat; L.chat = fake_agent_chat; E.chat = fake_eyes_chat
        try:
            L._vlm_look("eyes/vlm", "q", [png], None, cfg)
        finally:
            L.chat, E.chat = oc, oe
        assert seen["agent"] == want_agent, (rounds, seen)
        assert seen["ensemble"] == (not want_agent), (rounds, seen)
    print("v22.1 dispatch gate: PASS (rounds>1 -> agent, <=1 -> ensemble single-shot)")


def test_v22_1_grid_cells():
    # grid_cells splits into cells and returns encoded images.
    from core.imagery import grid_cells
    png = _tiny_png()
    allc, note = grid_cells([png], 2, 2)
    assert len(allc) == 4 and all(isinstance(b, (bytes, bytearray)) for b in allc), note
    one, note2 = grid_cells([png], 3, 1, cell=[1, 0])
    assert len(one) == 1, note2
    print("v22.1 grid_cells: PASS")


def test_v24_vision_verifier():
    # the independent inspector issues a look-probe -> eyes see the deliverable -> it can
    # conclude WRONG on a visual requirement a text probe cannot check (the t063 fix).
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 4
    png = _tiny_png(); eyes = {"n": 0}
    insp = ['{"look": {"path": "/x/render.png", "question": "do the tails point at the mouth?"}}',
            '{"verdict": "wrong", "findings": "the tails point away from the mouth per the visual read"}']
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:                        # the EYES
            eyes["n"] += 1
            assert image is not None, "vision probe must send an image"
            return "The speech-bubble tails point AWAY from the mouth."
        return insp.pop(0) if insp else '{"verdict": "unverified", "findings": "x"}'  # inspector
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "PASS"
        def fetch_file(self, path, max_bytes=4_000_000): return png, None
    import core.eyes as E                       # v30: the probe's eyes call lives in core.eyes
    oc, oe = V.chat, E.chat; V.chat = E.chat = fc
    try:
        verdict, findings = V.verify_independent("point the tails at the mouth", VM(), cfg)
    finally:
        V.chat, E.chat = oc, oe
    assert eyes["n"] == 1, "the eyes were not called for the look-probe"
    assert verdict == "wrong", verdict
    assert "away" in findings.lower(), findings
    print("v24 vision-verifier: PASS (inspector looked, eyes saw mismatch, verdict=wrong)")


def test_v24_vision_verify_off():
    # vision_verify=False -> a look-only reply is ignored (nudged); eyes never called.
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = False
    cfg.verifier_probes = 2
    seq = ['{"look": {"path": "/x.png", "question": "?"}}',
           '{"verdict": "unverified", "findings": "could not confirm"}']
    eyes = {"n": 0}
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            eyes["n"] += 1; return "x"
        return seq.pop(0) if seq else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "PASS"
        def fetch_file(self, path, max_bytes=4_000_000): return _tiny_png(), None
    import core.eyes as E                       # v30: probe eyes live in core.eyes
    oc, oe = V.chat, E.chat; V.chat = E.chat = fc
    try:
        V.verify_independent("t", VM(), cfg)
    finally:
        V.chat, E.chat = oc, oe
    assert eyes["n"] == 0, "eyes must NOT be called when vision_verify=False"
    print("v24 vision-verify OFF: PASS (look-probe ignored, no eyes call)")


def test_v27_stall_steer():
    # v27: the eyes-reminder fires on a STALL (probes read no content for 2+ rounds), NOT
    # on the instruction wording. The instruction here has NO visual words; the probes
    # return nothing -> the steer must be injected (surfacing the eyes when text fails).
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 6
    seen = []
    replies = ['{"probes": ["unzip -o /x/f.pptx"]}',            # round 1: empty -> no content
               '{"probes": ["unzip -o /x/f.pptx -d /tmp/y"]}',  # round 2: empty -> STALL
               '{"verdict": "unverified", "findings": "x"}']
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000):
            if "__forge_canary__" in cmd:          # v32.2: channel alive, output real
                return "__forge_canary__"
            return ""                              # never any content
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("finish the task on the file", VM(), cfg)   # NO visual words
    finally:
        V.chat = oc
    j = "\n".join(seen).lower()
    assert "[inspector note]" in j and "eyes" in j, "v27 stall-steer not injected"
    print("v27 stall-steer: PASS (fires on stall, no instruction classification)")


def test_v27_no_overfire():
    # v27 IS the regression fix: an instruction that USES visual words (align/center) but
    # whose probes SUCCEED (read content across 2+ rounds) must NOT trigger the steer.
    # Proves the steer no longer classifies the instruction — v25's _VISUAL_REQ_RE
    # over-fired on 23/69 tasks on words like these.
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 4
    seen = []; eyes = {"n": 0}
    replies = ['{"probes": ["cat /x/report.csv"]}',            # content-read (round 1)
               '{"probes": ["grep total /x/report.csv"]}',     # content-read (round 2)
               '{"verdict": "pass", "findings": "aligned columns and centered title verified"}']
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            eyes["n"] += 1; return "x"
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "col1,col2\n1,2\n3,4"
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("align the columns and center the title", VM(), cfg)  # visual WORDS
    finally:
        V.chat = oc
    assert "[inspector note]" not in "\n".join(seen).lower(), "v27 over-fired on visual words"
    assert eyes["n"] == 0, "eyes called on a text task that read content fine"
    print("v27 no-overfire: PASS (visual words + successful probes -> no steer)")


def test_v25_no_steer_when_blind():
    # vision off -> no eyes -> the steer must NOT fire (nothing to steer toward).
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = ""; cfg.vision_verify = True
    cfg.verifier_probes = 4
    seen = []
    replies = ['{"probes": ["ls /home/user"]}',
               '{"verdict": "unverified", "findings": "x"}']
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "out"
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("make the tail point to his mouth", VM(), cfg)
    finally:
        V.chat = oc
    assert "[inspector note]" not in "\n".join(seen).lower(), "steer fired with no eyes"
    print("v25 no-steer-when-blind: PASS (no vision_model -> no steer)")


def test_v26_autorender_image_passthrough():
    # an image path is returned as-is — NO render command issued.
    from core.imagery import fetch_look_image
    calls = {"run": 0}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            calls["run"] += 1; return ""
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"PNGDATA", ""
    d, e = fetch_look_image(VM(), "/x/shot.PNG")          # case-insensitive
    assert d == b"PNGDATA" and e == "", (d, e)
    assert calls["run"] == 0, "an image path must not trigger a render"
    print("v26 autorender image-passthrough: PASS")


def test_v26_autorender_pptx():
    # a document -> harness renders via soffice, fetches the produced PNG.
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/forge_render/SiriDemo.png\n"
        def fetch_file(self, path, max_bytes=4_000_000):
            seen["fetched"] = path; return b"RENDERED", ""
    d, e = fetch_look_image(VM(), "/home/user/Desktop/SiriDemo.pptx")
    assert d == b"RENDERED" and e == "", (d, e)
    assert "soffice" in seen["cmd"] and "convert-to png" in seen["cmd"], seen["cmd"]
    assert seen["fetched"] == "/tmp/forge_render/SiriDemo.png", seen
    print("v26 autorender document(pptx): PASS")


def test_v26_autorender_pdf():
    # a PDF -> pdftoppm branch.
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/forge_render/page-1.png"
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"PDFPAGE", ""
    d, e = fetch_look_image(VM(), "/docs/report.pdf")
    assert d == b"PDFPAGE" and e == "", (d, e)
    assert "pdftoppm" in seen["cmd"], seen["cmd"]
    print("v26 autorender document(pdf): PASS")


def test_v26_autorender_failure():
    # render produced nothing (soffice absent/failed) -> (None, err), gracefully.
    from core.imagery import fetch_look_image
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            return ""                                     # no PNG path echoed
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"x", ""
    d, e = fetch_look_image(VM(), "/x/thing.pptx")
    assert d is None and "render" in e.lower(), (d, e)
    print("v26 autorender render-failure: PASS")


def test_v29_screen_capture():
    # v29: fetch_look_image("screen:") captures the LIVE screen via a known-good command
    # and returns the PNG bytes — the session-verify fix (t021 drove the web apps, scored
    # 0 because the verifier could only probe the filesystem).
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/forge_render/screen.png"
        def fetch_file(self, path, max_bytes=4_000_000):
            seen["fetched"] = path; return b"SCREENPNG", ""
    d, e = fetch_look_image(VM(), "screen:")
    assert d == b"SCREENPNG" and e == "", (d, e)
    assert "DISPLAY=:0" in seen["cmd"] and "screen.png" in seen["cmd"], seen["cmd"]
    assert seen["fetched"] == "/tmp/forge_render/screen.png", seen
    # explicit display
    d2, _ = fetch_look_image(VM(), "screen:1")
    assert "DISPLAY=:1" in seen["cmd"], seen["cmd"]
    print("v29 screen-capture: PASS (screen: -> live capture -> eyes)")


def test_v29_screen_capture_fail():
    # no display / no tool -> graceful (None, err), never a crash
    from core.imagery import fetch_look_image
    class VM:
        def run_command(self, command, timeout=30, cap=4000): return ""   # nothing produced
        def fetch_file(self, path, max_bytes=4_000_000): return b"x", ""
    d, e = fetch_look_image(VM(), "screen:")
    assert d is None and "screen" in e.lower(), (d, e)
    print("v29 screen-capture fail: PASS (no display -> graceful error)")


def test_v29_screen_capture_rejects_display_shell_injection():
    from core.imagery import fetch_look_image
    class VM:
        def __init__(self): self.commands = []
        def run_command(self, command, timeout=30, cap=4000):
            self.commands.append(command); return "/tmp/forge_render/screen.png"
        def fetch_file(self, path, max_bytes=4_000_000): return b"x", ""
    vm = VM()
    data, error = fetch_look_image(vm, "screen:0; touch /tmp/verifier-wrote")
    assert data is None and "display" in error.lower(), (data, error)
    assert vm.commands == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_parsing: OK")
