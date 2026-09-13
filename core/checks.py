"""Verification checks — model-authored, machine-executed, instruction-derived.

The model declares done with a list of {desc, probe} checks; the harness's only job is
(a) a falsifiability gate that rejects lazy or mutating probes, and (b) running the
accepted probes read-only in the VM. PASS/FAIL semantics are conservative: a probe
passes iff its output contains PASS and no FAIL.
"""
import re
from dataclasses import dataclass


@dataclass
class Check:
    desc: str
    probe: str


@dataclass
class CheckResult:
    desc: str
    probe: str
    passed: bool
    output: str


# Persistent-state mutations that are destructive/installing regardless of target.
# v34: matched as WHOLE COMMAND WORDS (start-of-string or after a shell separator),
# not substrings — "confirm file" used to trip 'rm ', "grep -w add" tripped 'dd '.
_HARD_MUTATING_RE = re.compile(
    r"(?:^|[;&|`$(\s])"
    r"(rm|mv|shred|dd|chmod|chown|wget|rmdir|unlink)\s"
    r"|sed\s+-i|pip3?\s+install|apt(?:-get)?\s|curl\s+(?:-\S*\s+)*-[oO]\b")
# File-CREATING commands whose target is checked: writing under /tmp scratch is
# read-only w.r.t. graded state (deliverables never live in /tmp), so a check may
# stage a render/copy there and inspect it (v16: enables render-and-compare checks).
_TARGETED_MUTATING = ("cp", "tee", "mkdir", "touch", "truncate")
_SCRATCH = "/tmp/"
# redirections that are harmless in a read-only probe. v34: added the plain
# stdout silencers — `cmd >/dev/null && echo PASS` is the standard read-only idiom
# and was the single largest false-positive class in the gate's history (174 of 337
# archived rejections; agents were being taught to write weaker checks).
_HARMLESS_REDIR = ("2>&1", "2>/dev/null", "&>/dev/null", "1>&2", "2> /dev/null",
                   ">/dev/null", "> /dev/null", "1>/dev/null", "1> /dev/null",
                   ">> /dev/null", ">>/dev/null")

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_REDIR = re.compile(r">>?\s*([^\s|;&()]+)")
_MKFILE = re.compile(r"\b(cp|tee|mkdir|touch|truncate)\b\s+((?:-\S+\s+)*)([^\s|;&()]+)")

# Interpreter one-liner bodies — python -c "...", perl -e '...', etc. The v16 quote-
# masking deliberately hides these from the shell-token scan (so a python '>' inside a
# string is not misread as a redirect), but that same masking blinds the gate to real
# mutations INSIDE the body: a check `python3 -c "shutil.rmtree('X'); print('PASS')"`
# would pass the read-only gate and destroy graded state. These run against the live
# scored machine, so the body is scanned separately for destructive calls / non-scratch
# writes (a hard prerequisite once the inspector authors verification programs).
_INTERP_BODY = re.compile(
    r"(?:python3?|perl|ruby|node|php|bash|sh)\s+-[ce]\s+(['\"])(.*?)\1", re.DOTALL)
_BODY_DELETE = re.compile(                          # deletes/moves/renames: never
    r"shutil\.(?:rmtree|move)\b"                    # read-only, flag regardless of path
    r"|os\.(?:remove|unlink|rmdir|removedirs|rename|renames|replace|truncate)\b"
    r"|\.unlink\s*\(|\brmtree\s*\(|\bunlink\b"
    r"|\.write_(?:text|bytes)\s*\(")
_BODY_WRITE_OPEN = re.compile(                       # open(path, 'w'|'a'|'x'|...)
    r"open\s*\(\s*[^,)]+,\s*(['\"])[^'\"]*[wax][^'\"]*\1")


def _nonscratch_mutations(probe: str):
    """Shell-level mutations of NON-scratch (non-/tmp) state in ``probe``. Empty list
    means the probe is read-only (or writes only to /tmp scratch).

    v16 fix: quoted spans are MASKED before scanning, so interpreter-body operators —
    a python `print(a > b)` comparison, a substring like 'cp' inside a string — are no
    longer misread as shell mutations. That false positive rejected 7 of 8 legitimate
    zipfile.read() checks on one run; the real read-only guarantee (no unquoted shell
    redirect / destructive command touching graded state) is preserved exactly."""
    sk = probe
    for h in _HARMLESS_REDIR:
        sk = sk.replace(h, " ")
    sk = _QUOTED.sub("  ", sk)                       # mask quoted interpreter bodies
    bad = []
    for m in _HARD_MUTATING_RE.finditer(sk):
        bad.append((m.group(1) or m.group(0)).strip())
    for m in _REDIR.finditer(sk):                   # shell output redirection
        if not m.group(1).startswith(_SCRATCH):
            bad.append("redirect to " + m.group(1)[:40])
    for m in _MKFILE.finditer(sk):                  # file-creating commands
        # v34: validate the WRITE TARGET. For copy-class commands that is the LAST
        # path argument (cp SRC DST) — the old code checked the first argument (the
        # SOURCE), which rejected the documented stage-to-/tmp pattern
        # (`cp ~/x.pdf /tmp/c.pdf`) while PASSING a genuine overwrite of graded
        # state (`cp /tmp/planted ~/deliverable`). Inverted protection.
        cmd = m.group(1)
        if cmd == "cp":
            tail = sk[m.start():].split("|")[0].split(";")[0].split("&&")[0]
            args = [a for a in tail.split()[1:] if not a.startswith("-")]
            target = args[-1] if len(args) >= 2 else m.group(3)
        else:
            target = m.group(3)
        if not target.startswith(_SCRATCH):
            bad.append(cmd + " " + target[:30])
    for _q, body in _INTERP_BODY.findall(probe):    # scan UNMASKED interpreter bodies
        if _BODY_DELETE.search(body):
            bad.append("interpreter-body delete/move/rename")
        elif _BODY_WRITE_OPEN.search(body) and _SCRATCH not in body:
            bad.append("interpreter-body write to non-scratch")
    return bad


_EXIST = re.compile(r"\btest\s+-[efdrsL]|\[\s*-[efdrsL]\b|\bls\b")
_META = re.compile(r"\bffprobe\b|\bstat\b|\bfile\b|\bwc\b|\bdu\b|identify\b|"
                   r"zipinfo|unzip\s+-l|pdfinfo|\bhead\b|\btail\b")
_CONTENT = re.compile(r"diff|cmp|md5|sha[0-9]|psnr|ssim|compare|==|!=|<=|>=|"
                      r"grep|python|awk|jq|pdftotext|json")


def _category(probe: str) -> str:
    """Value tier of a check, for capacity eviction (v16). content > metadata >
    existence. Conservative: anything not clearly existence/metadata is content, so a
    borderline check is KEPT under capacity pressure, never evicted for a proxy."""
    p = probe.lower()
    if _CONTENT.search(p):
        return "content"
    if _META.search(p):
        return "metadata"
    if _EXIST.search(p):
        return "existence"
    return "content"


_PRIORITY = {"content": 0, "metadata": 1, "existence": 2}


def validate(raw_checks, max_checks: int = 16, instruction: str = "",
             witness_gates: bool = True):
    """Split model-proposed checks into (accepted, rejections, dropped).

    ``rejections`` are SOUNDNESS failures (malformed / mutating / vacuous) — they veto
    a done declaration. ``dropped`` is capacity overflow beyond ``max_checks`` — noted,
    but NOT a veto (a real seed earned done with 8/8 passing and was refused because
    two overflow drops were counted as rejections; that was a harness bug). v16: when
    over capacity, evict the LEAST valuable checks first (existence/metadata before
    content), so a run's content-vs-reference checks survive a crowd of structural
    proxies (one run had every content check evicted while 12 proxies passed at 0)."""
    import re as _re
    _instr_l = (instruction or "").lower()
    ok, rejections, dropped = [], [], []
    for c in (raw_checks or []):
        if not isinstance(c, dict) or not str(c.get("probe", "")).strip():
            rejections.append(f"malformed check (need desc + probe): {str(c)[:120]}")
            continue
        desc = str(c.get("desc", "")).strip() or "(no desc)"
        probe = str(c["probe"]).strip()
        muts = _nonscratch_mutations(probe)
        if muts:
            rejections.append(f"probe must be READ-ONLY (mutates: {'; '.join(muts)[:90]}): {probe[:100]}")
            continue
        if not any(ch in probe for ch in "/~$|`"):
            rejections.append(f"probe does not inspect the machine (no path/var/pipe): {probe[:120]}")
            continue
        if probe.startswith("echo") and not any(ch in probe for ch in "|;&$`"):
            rejections.append(f"probe is a constant echo — not falsifiable: {probe[:120]}")
            continue
        # Fix D-1 (E2 autopsy): a probe that reads the agent's own this-run
        # self-report file certifies the reporter, not the work. Structural
        # name-pattern rule (content-blind); a file the TASK itself names is
        # exempt (pass instruction via validate(..., instruction=...)).
        _sr = None if not witness_gates else _re.search(r"[\w~/.-]*(?:evidence|verification|_proof|self_?report)[\w.-]*",
                         probe, _re.IGNORECASE)
        if _sr and _sr.group(0).split("/")[-1].lower() not in _instr_l:
            rejections.append(
                "probe reads a self-authored report file — verify the WORK "
                f"through an independent channel instead: {probe[:100]}")
            continue
        ok.append(Check(desc=desc, probe=probe))
    # capacity eviction by value tier (stable within a tier)
    if len(ok) > max_checks:
        order = sorted(range(len(ok)),
                       key=lambda i: (_PRIORITY[_category(ok[i].probe)], i))
        keep = set(order[:max_checks])
        accepted = [ok[i] for i in range(len(ok)) if i in keep]
        dropped = [f"over capacity (max {max_checks}, kept higher-value checks) — "
                   f"not run: {ok[i].desc[:70]}" for i in range(len(ok)) if i not in keep]
    else:
        accepted = ok
    if not accepted and not rejections:
        rejections.append("no checks provided — derive at least one falsifiable check "
                          "from the task's own requirements")
    return accepted, rejections, dropped


def run_checks(accepted, vm, timeout: int = 30):
    """Run accepted probes in the VM; conservative PASS semantics."""
    results = []
    for c in accepted:
        out = vm.run_command(c.probe, timeout=timeout) or ""
        passed = bool(re.search(r"\bPASS\b", out)) and not re.search(r"\bFAIL\b", out)
        results.append(CheckResult(desc=c.desc, probe=c.probe, passed=passed,
                                   output=out[-400:]))
    return results
