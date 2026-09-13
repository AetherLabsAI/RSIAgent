"""E8 DRY-RUN GATE (PREREG E8 §2 "The dry-run gate (F5)", F9 anti-forgery) —
pure, unit-testable functions, same law as reward.py (the v11c lesson:
inline VM-bound logic is where bugs hide for months).

Every accepted card's SUCCESS criteria are rehearsed on a VIRGIN clone of
the exact grade-time environment BEFORE the actor agent ever sees the card.
Four channels are closed here:

  dry_run_verdict()     the PURE verdict — per-criterion token check on TWO
                        executions (determinism screen), virgin-FAIL demand
                        for every non-GUARD criterion, virgin-PASS demand
                        for every GUARD criterion (drills: at most one;
                        E7 MILESTONE/FINAL sections: any number — N4).
  static_token_screen() F9 anti-forgery — best-effort regex screen for
                        UNCONDITIONALLY-reachable PASS emissions
                        ("echo PASS" with no guard; "print('PASS'" outside
                        a conditional). GUARD lines are NOT exempt: a hash
                        pin must still emit its token conditionally.
  run_gate()            the thin harness — stamps mtime, runs each
                        criterion twice through a caller-supplied
                        vm_exec(cmd)->str, diffs the mtime manifest; ANY
                        created/modified file => reject-mutation
                        (read-only enforcement, fail-closed).
  find_guard()          '# GUARD' marker parse — index of the single GUARD
                        criterion, or None; >1 GUARD raises ValueError (the
                        caller maps that to card rejection, F6 path).
  find_guards()         PREREG E7 §3 N4 — indices of ALL GUARD criteria,
                        never raises; the milestone grammar allows several,
                        drills keep find_guard's single-GUARD law.

⚠️ The CALLER is responsible for pointing vm_exec at a DISCARDABLE clone of
the grade-time environment (docker commit + throwaway container) — never
the actor agent's live boot. gate.py stays transport-agnostic and
pure-testable: it knows nothing about docker, ssh, or the VM object.

Token rule (F9): identical to grade_instance — PASS iff "PASS" in upper and
"FAIL" not in upper — but judged on the FINAL NON-EMPTY LINE of the output
only (a criterion that chats about PASSing mid-stream earns nothing). A run
whose final non-empty line carries neither token is a crash: a broken
instrument, never a virgin-FAIL.
"""
import re

STAMP = "/tmp/.gate_stamp"           # mtime anchor; excluded from manifest
_SCRATCH_PREFIX = "/tmp/forge_"      # harness scratch; excluded from manifest
_NUM_PAT = re.compile(r"^\s*\d+[.)]\s*")          # same strip as grade_instance
_GUARD_PAT = re.compile(r"#\s*GUARD\s*$")


# ------------------------------------------------------------ GUARD parse --
def strip_guard(line: str) -> str:
    """Remove a trailing '# GUARD' marker (it is metadata, never shell)."""
    return _GUARD_PAT.sub("", line).rstrip()


def find_guards(criteria: list) -> list:
    """0-based indices of ALL '# GUARD'-marked criteria, never raises.

    PREREG E7 §3 N4: MILESTONE/FINAL sections may carry multiple GUARD
    criteria (each demanded virgin-PASS by dry_run_verdict); drills keep
    find_guard's single-GUARD law."""
    return [i for i, ln in enumerate(criteria) if _GUARD_PAT.search(ln)]


def find_guard(criteria: list) -> "int | None":
    """0-based index of the single '# GUARD'-marked criterion, or None.

    PREREG §2: **up to one** criterion may be GUARD. >1 raises ValueError —
    the caller (e8 loop) maps that to card rejection (F6 re-author path);
    it is a card-grammar violation, not a gate verdict."""
    hits = find_guards(criteria)
    if len(hits) > 1:
        raise ValueError(f"multiple GUARD criteria (lines {hits})")
    return hits[0] if hits else None


# ------------------------------------------------------------- token rule --
def _token(text: str) -> "str | None":
    """PASS / FAIL / None(crash) from the FINAL non-empty line (F9)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    up = lines[-1].upper()
    if "PASS" not in up and "FAIL" not in up:
        return None
    return "PASS" if ("PASS" in up and "FAIL" not in up) else "FAIL"


# ------------------------------------------------------------ the verdict --
def dry_run_verdict(results: list, guard_index: "int | None" = None,
                    guard_indices: "list | None" = None) -> dict:
    """PURE card verdict from two virgin executions per criterion.

    results: [{n, run1: str, run2: str}, ...] in card order — run1/run2 are
    the raw outputs of the same criterion executed twice on the virgin
    clone. guard_index = 0-based POSITION of the GUARD criterion (as
    returned by find_guard), or None. guard_indices (PREREG E7 §3 N4) =
    the POSITIONS of ALL GUARD criteria (as returned by find_guards) for
    multi-GUARD MILESTONE/FINAL sections; guard_index keeps working
    unchanged (equivalent to a one-element list). Passing BOTH non-None is
    a caller bug and raises ValueError.

    Per criterion, first failure wins (checked in this order):
      reject-crash              no token in either run (broken instrument)
      reject-nondeterministic   run1 verdict != run2 verdict
      reject-vacuous            non-GUARD criterion PASSes the virgin clone
                                (it cannot distinguish work from no-work)
      reject-guard-fails-virgin GUARD criterion must PASS the virgin clone
                                (a hash pin that fails pins nothing)
      ok                        otherwise

    Card verdict: ACCEPT iff every criterion is 'ok'; else REJECT with a
    reasons list. Zero criteria => REJECT (a malformed card is not a free
    win — same stance as grade_instance)."""
    if guard_index is not None and guard_indices is not None:
        raise ValueError("pass guard_index OR guard_indices, never both")
    if guard_indices is not None:
        guards = set(guard_indices)
    else:
        guards = set() if guard_index is None else {guard_index}
    out, reasons = [], []
    for i, r in enumerate(results):
        t1, t2 = _token(r.get("run1", "")), _token(r.get("run2", ""))
        if t1 is None or t2 is None:
            v = "reject-crash"
        elif t1 != t2:
            v = "reject-nondeterministic"
        elif i in guards:
            v = "ok" if t1 == "PASS" else "reject-guard-fails-virgin"
        else:
            v = "reject-vacuous" if t1 == "PASS" else "ok"
        out.append({"n": r.get("n", i + 1), "verdict": v,
                    "run1": t1, "run2": t2})
        if v != "ok":
            reasons.append(f"criterion {out[-1]['n']}: {v}")
    if not results:
        reasons.append("reject-no-criteria")
    verdict = "ACCEPT" if results and not reasons else "REJECT"
    return {"verdict": verdict, "results": out, "reasons": reasons}


# ------------------------------------------- F9 static anti-forgery screen --
# Best-effort regex heuristics, documented + pinned by tests. They flag the
# two forgery shapes E6 transcripts actually produced; a determined
# adversarial card can evade regexes — the dry run + double-run + verifier
# spot-check are the backstops, this screen is the cheap first fence.
_ECHO_PASS = re.compile(r"\becho\b(?:\s+-[A-Za-z]+)*\s+[\"']?[^|&;\n]*PASS")
_PRINT_PASS = re.compile(r"\bprint\s*\(\s*f?[\"']PASS")
# conditional-expression tail: print("PASS" if ...)  /  print("PASS") if ...
_COND_TAIL = re.compile(r"^[^\"']*[\"']\s*\)?\s*if\s")
# statement-level guard earlier in the same segment: `if cond: print(...`
_IF_GUARD = re.compile(r"\bif\b[^;]*:")


def static_token_screen(criteria: list) -> list:
    """Violations: criteria containing an UNCONDITIONALLY-reachable PASS
    emission. Returns [{n, kind, snippet}, ...] (empty = clean).

    Heuristics (documented best-effort, F9):
      * shell: 'echo ...PASS' is legal only as the right arm of '&&'/'||'
        (optionally behind '{'/'(') or inside an 'if ... then ... fi' body;
        anything else — start of line, after ';' or '|' — is a violation.
      * python: 'print("PASS' is legal only in a conditional expression
        ('PASS' if cond else 'FAIL' / print(..) if cond else ..) or behind
        a statement-level 'if cond:' in the same ';'-segment."""
    hits = []
    for i, raw in enumerate(criteria, 1):
        line = strip_guard(_NUM_PAT.sub("", raw).strip())
        for m in _ECHO_PASS.finditer(line):
            pre = re.sub(r"[\s{(]+$", "", line[:m.start()])
            if pre.endswith("&&") or pre.endswith("||"):
                continue
            if (re.search(r"\bif\b", pre) and re.search(r"\bthen\b", pre)
                    and re.search(r"\bfi\b", line[m.end():])):
                continue
            hits.append({"n": i, "kind": "echo-pass", "snippet": m.group(0)})
        for m in _PRINT_PASS.finditer(line):
            if _COND_TAIL.match(line[m.end():]):
                continue
            seg = re.split(r"[;\n]", line[:m.start()])[-1]
            if _IF_GUARD.search(seg):
                continue
            hits.append({"n": i, "kind": "print-pass", "snippet": m.group(0)})
    return hits


# ------------------------------------------------------------ the harness --
def run_gate(vm_exec, criteria: list, guard_index: "int | None" = None,
             guard_indices: "list | None" = None) -> tuple:
    """Run the full gate against a DISCARDABLE clone (see module docstring).
    guard_index / guard_indices pass straight through to dry_run_verdict
    (N4 multi-GUARD; same never-both rule).

    vm_exec(cmd: str) -> str runs one shell command on the clone and returns
    its output. Sequence: touch the mtime stamp -> run each criterion TWICE
    (numbering + '# GUARD' marker stripped, exactly what grade-time will
    execute) -> pull the mtime manifest. A raised vm_exec is recorded as a
    crashed run (=> reject-crash), same shape as grade_instance's
    [grader error]. Any created/modified file under /home/user or /tmp
    (minus the stamp itself and /tmp/forge_* scratch) => REJECT
    'reject-mutation'; an unreadable manifest fails CLOSED (read-only
    unverifiable = reject). Returns (verdict_dict, created_files)."""
    vm_exec(f"touch {STAMP}")
    results = []
    for i, raw in enumerate(criteria, 1):
        cmd = strip_guard(_NUM_PAT.sub("", raw).strip())
        runs = []
        for _ in range(2):
            try:
                runs.append(vm_exec(cmd) or "")
            except Exception as e:                     # noqa: BLE001
                runs.append(f"[gate error] {e}")
        results.append({"n": i, "run1": runs[0], "run2": runs[1]})
    verdict = dry_run_verdict(results, guard_index=guard_index,
                              guard_indices=guard_indices)
    try:
        manifest = vm_exec(
            f"find /home/user /tmp -newer {STAMP} -type f") or ""
    except Exception as e:                             # noqa: BLE001
        manifest = f"[manifest error] {e}"
        verdict["verdict"] = "REJECT"
        verdict["reasons"].append(f"reject-mutation: {manifest}")
        return verdict, []
    created = [p.strip() for p in manifest.splitlines()
               if p.strip().startswith("/")            # drop find warnings
               and p.strip() != STAMP
               and not p.strip().startswith(_SCRATCH_PREFIX)]
    if created:
        verdict["verdict"] = "REJECT"
        verdict["reasons"].append(
            "reject-mutation: " + ", ".join(created[:8]))
    return verdict, created
