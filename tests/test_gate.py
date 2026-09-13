"""E8 dry-run gate (explore/gate.py) — PREREG E8 §2 (F5) + F9 anti-forgery.

Every reject path, the GUARD happy path, the determinism screen, the static
token screen (catches AND legitimate conditional forms), and the run_gate
harness (double-run, stamp/manifest mechanics, mutation reject, fail-closed
manifest). No VM, no API — vm_exec is a scripted callable throughout.
"""
import sys

import pytest

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from explore import gate as G                         # noqa: E402


def _r(n, run1, run2=None):
    return {"n": n, "run1": run1, "run2": run1 if run2 is None else run2}


# ------------------------------------------------------------ token rule --
def test_token_judged_on_final_nonempty_line():
    # mid-stream PASS earns nothing; trailing blank lines are skipped
    v = G.dry_run_verdict([_r(1, "PASS\nsome epilogue chatter")])
    assert v["results"][0]["verdict"] == "reject-crash"
    v = G.dry_run_verdict([_r(1, "chatter\nFAIL: virgin\n\n")])
    assert v == {"verdict": "ACCEPT",
                 "results": [{"n": 1, "verdict": "ok",
                              "run1": "FAIL", "run2": "FAIL"}],
                 "reasons": []}
    # both tokens on the final line = FAIL (grade_instance rule), not crash
    v = G.dry_run_verdict([_r(1, "PASS and also FAIL")])
    assert v["results"][0]["run1"] == "FAIL"
    assert v["verdict"] == "ACCEPT"


# ---------------------------------------------------------- reject paths --
def test_reject_crash_either_run():
    for r in (_r(1, "", "FAIL"), _r(1, "FAIL", "no token here"),
              _r(1, "[gate error] boom", "[gate error] boom")):
        v = G.dry_run_verdict([r])
        assert v["verdict"] == "REJECT"
        assert v["results"][0]["verdict"] == "reject-crash"
        assert v["reasons"] == ["criterion 1: reject-crash"]


def test_reject_nondeterministic():
    v = G.dry_run_verdict([_r(1, "PASS", "FAIL")])
    assert v["verdict"] == "REJECT"
    assert v["results"][0]["verdict"] == "reject-nondeterministic"
    # a flapping GUARD is nondeterministic too (checked before guard rule)
    v = G.dry_run_verdict([_r(1, "FAIL", "PASS")], guard_index=0)
    assert v["results"][0]["verdict"] == "reject-nondeterministic"


def test_reject_vacuous_non_guard_passes_virgin():
    v = G.dry_run_verdict([_r(1, "FAIL"), _r(2, "PASS")])
    assert v["verdict"] == "REJECT"
    assert [r["verdict"] for r in v["results"]] == ["ok", "reject-vacuous"]
    assert v["reasons"] == ["criterion 2: reject-vacuous"]


def test_reject_guard_fails_virgin():
    v = G.dry_run_verdict([_r(1, "FAIL"), _r(2, "FAIL")], guard_index=1)
    assert v["verdict"] == "REJECT"
    assert v["results"][1]["verdict"] == "reject-guard-fails-virgin"


def test_zero_criteria_never_accept():
    v = G.dry_run_verdict([])
    assert v["verdict"] == "REJECT" and v["reasons"] == ["reject-no-criteria"]


# ------------------------------------------------------- GUARD happy path --
def test_guard_happy_path():
    v = G.dry_run_verdict(
        [_r(1, "FAIL: no export yet"),
         _r(2, "sha ok\nPASS"),                       # the hash pin
         _r(3, "FAIL")], guard_index=1)
    assert v["verdict"] == "ACCEPT" and v["reasons"] == []
    assert [r["verdict"] for r in v["results"]] == ["ok", "ok", "ok"]


# ------------------------------------------------------------ GUARD parse --
def test_find_guard_and_strip():
    crit = ["1. test -f out.mp4 && echo PASS || echo FAIL",
            "2. sha256sum -c /tmp/pin && echo PASS || echo FAIL  # GUARD"]
    assert G.find_guard(crit) == 1
    assert G.find_guard(crit[:1]) is None
    assert G.strip_guard(crit[1]).endswith("echo FAIL")
    assert "GUARD" not in G.strip_guard(crit[1])
    assert G.strip_guard(crit[0]) == crit[0]          # no marker = untouched
    with pytest.raises(ValueError):
        G.find_guard(["a  # GUARD", "b # GUARD"])


# ------------------------------------------------- F9 static token screen --
def test_static_screen_catches_unconditional_emissions():
    hits = G.static_token_screen([
        "1. echo PASS",                                # naked forgery
        "2. grep -q x f; echo PASS",                   # ';' = unconditional
        '3. python3 -c "print(\'PASS\')"',             # naked python
        '4. python3 -c "import os; print(\'PASS\')"',  # segment, no guard
    ])
    assert [h["n"] for h in hits] == [1, 2, 3, 4]
    assert {h["kind"] for h in hits} == {"echo-pass", "print-pass"}


def test_static_screen_passes_legitimate_conditionals():
    assert G.static_token_screen([
        "1. test -f /home/user/out.mp4 && echo PASS || echo FAIL",
        "2. grep -q needle hay && { echo PASS; } || echo FAIL",
        "3. if grep -q needle hay; then echo PASS; else echo FAIL; fi",
        '4. python3 -c "import os; print(\'PASS\' if '
        "os.path.exists('/tmp/x') else 'FAIL')\"",
        '5. python3 -c "if open(\'/tmp/x\').read() == \'7\': '
        "print('PASS')\"",
        "6. sha256sum -c /tmp/pin && echo PASS || echo FAIL  # GUARD",
    ]) == []


def test_static_screen_guard_lines_not_exempt():
    # a GUARD may pass on virgin, but its token must still be CONDITIONAL
    hits = G.static_token_screen(["1. echo PASS  # GUARD"])
    assert len(hits) == 1 and hits[0]["kind"] == "echo-pass"


# -------------------------------------------------------- run_gate harness --
class _VMExec:
    """Scripted vm_exec: stamp/find handled specially, criteria answered
    from per-command queues (a 2-list yields run1 then run2)."""

    def __init__(self, script, manifest=""):
        self.script = {k: list(v) for k, v in script.items()}
        self.manifest, self.seen = manifest, []

    def __call__(self, cmd):
        self.seen.append(cmd)
        if cmd == f"touch {G.STAMP}":
            return ""
        if cmd.startswith("find "):
            return self.manifest
        outs = self.script[cmd]
        out = outs.pop(0) if len(outs) > 1 else outs[0]
        if isinstance(out, Exception):
            raise out
        return out


def test_run_gate_accept_and_mechanics():
    c1 = "test -f /home/user/out.mp4 && echo PASS || echo FAIL"
    c2 = "sha256sum -c /tmp/pin && echo PASS || echo FAIL"
    vm = _VMExec({c1: ["FAIL"], c2: ["ok\nPASS"]},
                 manifest=f"{G.STAMP}\n/tmp/forge_scratch/probe.txt\n")
    verdict, created = G.run_gate(
        vm, [f"1. {c1}", f"2. {c2}  # GUARD"], guard_index=1)
    assert verdict["verdict"] == "ACCEPT" and created == []
    # stamp first; each criterion runs TWICE, numbering + GUARD stripped
    assert vm.seen[0] == f"touch {G.STAMP}"
    assert vm.seen[1:5] == [c1, c1, c2, c2]
    assert vm.seen[5] == f"find /home/user /tmp -newer {G.STAMP} -type f"


def test_run_gate_reject_mutation():
    c = "test -f /home/user/x && echo PASS || echo FAIL"
    vm = _VMExec({c: ["FAIL"]},
                 manifest=(f"{G.STAMP}\n/home/user/.cache/junk\n"
                           "find: '/tmp/gone': Permission denied\n"))
    verdict, created = G.run_gate(vm, [f"1. {c}"])
    assert verdict["verdict"] == "REJECT"
    assert created == ["/home/user/.cache/junk"]      # stamp+warning dropped
    assert any(r.startswith("reject-mutation") for r in verdict["reasons"])


def test_run_gate_nondeterministic_and_crash():
    flappy = "date +%s | grep -q 0 && echo PASS || echo FAIL"
    vm = _VMExec({flappy: ["PASS", "FAIL"]})
    verdict, _ = G.run_gate(vm, [f"1. {flappy}"])
    assert verdict["results"][0]["verdict"] == "reject-nondeterministic"
    boom = "bash -c 'oops"
    vm = _VMExec({boom: [RuntimeError("ssh died")]})
    verdict, _ = G.run_gate(vm, [f"1. {boom}"])
    assert verdict["results"][0]["verdict"] == "reject-crash"


def test_run_gate_manifest_failure_fails_closed():
    class _VM(_VMExec):
        def __call__(self, cmd):
            if cmd.startswith("find "):
                raise RuntimeError("clone gone")
            return _VMExec.__call__(self, cmd)

    c = "test -f /home/user/x && echo PASS || echo FAIL"
    vm = _VM({c: ["FAIL"]})
    verdict, created = G.run_gate(vm, [f"1. {c}"])
    assert verdict["verdict"] == "REJECT" and created == []
    assert any("reject-mutation" in r for r in verdict["reasons"])
