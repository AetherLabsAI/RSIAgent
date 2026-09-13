"""v35.1 history hygiene: an EMPTY assistant reply must never enter a chat history
verbatim. Moonshot's API rejects any request whose history contains an empty
assistant message ("must not be empty", 400) — one empty k3 reply poisoned every
subsequent call in the segment (k3-native smoke, 2026-07-16: dry -> 400 storm ->
stall -> resume -> repeat). Providers that tolerate empties (GLM/m3) hid this.

Structural test in the repo's source-scan style (cf. test_no_leakage): every
assistant-append site in core/ must carry the `or "(empty reply)"` fallback.
"""
import re
import sys


def test_no_bare_assistant_append():
    bad = []
    for p in ("core/loop.py", "core/verifier.py", "core/eyes.py"):
        src = open(p).read()
        for m in re.finditer(r'"role":\s*"assistant",\s*"content":\s*([^}]+)}', src):
            expr = m.group(1).strip()
            if "or" not in expr:                    # any bare `out` (no fallback) regresses
                line = src[:m.start()].count("\n") + 1
                bad.append(f"{p}:{line}: {expr}")
    assert not bad, f"bare assistant append(s) — empty replies would poison history: {bad}"


def test_fallback_is_nonempty_string():
    # the fallback itself must be a non-empty literal (an `or ""` would be a no-op)
    for p in ("core/loop.py", "core/verifier.py"):
        src = open(p).read()
        for m in re.finditer(r'"content":\s*out\s+or\s+"([^"]*)"', src):
            assert m.group(1).strip(), f"{p}: empty fallback literal"


if __name__ == "__main__":
    fails = 0
    for name in ("test_no_bare_assistant_append", "test_fallback_is_nonempty_string"):
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception as e:                              # noqa: BLE001
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("ALL OK" if not fails else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)
