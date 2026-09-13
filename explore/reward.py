"""E6 CAPABILITY-GRADED REWARD (PREREG E6 v2.1 §5-contract, §6) — pure,
unit-testable functions (the v11c lesson: inline VM-bound logic is where bugs
hide for months).

The nightly reward is THE WORK, NEVER THE MEMORY: harness-executed pass/fail +
iterations. Hygiene is a tax; answers die to instance variation; only banked
SKILL pays across nights. The mechanisms live here:

  grade_instance()      the ground truth — ONE-COMMAND self-grading criteria
                        (authored by the curriculum agent) executed by the
                        HARNESS on the actor agent's end state, BEFORE the
                        verifier agent touches the machine (it mutates state).
  scan_invocations()    which banked artifacts the actor agent actually
                        EXECUTED this episode (host-side transcript scan; the
                        harness-surfaced entry point never counts).
  consolidate()         invocation-aged survival: memory invoked during a
                        PASSING episode earns its place; never-invoked memory
                        ages out after GRACE_ROTATIONS class-rotations.
                        ⚠️ NEVER DISCLOSED TO THE ACTOR AGENT (prereg §6) —
                        no charter may describe this rule; it exists only in
                        this file and its tests.
  observe_ledger()      E8 drill-era stand-in for consolidate() (PREREG E8
                        F15): born/last recorded, NOTHING pruned.

Plus the curve ledger (append_curve / read_curve) — the in-era capability
signal the curriculum agent schedules from.
"""
import json
import os
import re
import time

GRACE_ROTATIONS = 3          # age-out horizon (prereg §6; wenyi may override)
BREADTH_FLAG_N = 8           # >N distinct tools in one episode -> hand-audit


# ---------------------------------------------------------------- grading --
def grade_instance(vm, criteria: list) -> dict:
    """Execute the instance's SUCCESS criteria VERBATIM on the actor agent's
    end state — the authoritative nightly grade (E5 v11c contract, ported).

    An outcome grade must never depend on an agent's attention or output
    format: the criteria are executable BY CONTRACT (one self-grading command
    each), so the harness runs them itself. PASS iff output contains PASS and
    not FAIL — the same rule checks.py uses for exam probes."""
    out = []
    for i, line in enumerate(criteria, 1):
        cmd = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        try:
            tr = vm.run_script("bash", cmd, timeout=180)
            txt = (tr.stdout or "").strip()
        except Exception as e:                             # noqa: BLE001
            txt = f"[grader error] {e}"
        up = txt.upper()
        ok = ("PASS" in up) and ("FAIL" not in up)
        out.append({"n": i, "verdict": "PASS" if ok else "FAIL",
                    "evidence": txt[-300:]})
    # v1.1 (EXP-2026-002 deviation #5, ep015): a criterion that RUNS and
    # prints FAIL is evidence; a criterion that CRASHES (no PASS/FAIL in its
    # output — e.g. shell quoting errors) is a broken instrument. If NO
    # criterion produced a verdict token, the night is ungraded, not failed.
    # Partial crashes still grade (crashed ones count FAIL, as before).
    ran_any = any(("PASS" in r["evidence"].upper()) or
                  ("FAIL" in r["evidence"].upper()) for r in out)
    if out and not ran_any:
        return {"outcome": "ungraded", "results": out}
    return {"outcome": "pass" if out and all(r["verdict"] == "PASS"
                                            for r in out) else "fail",
            "results": out}


# ------------------------------------------------------------ invocations --
# v1.1 (EXP-2026-002 deviation #3, ep006 Kill-A hand-audit): the original
# pattern required the path IMMEDIATELY after the command word, so flag-args
# ("head -60 ~/.memory/x") escaped; and the import idiom the actor's own
# quick-refs recommend (sys.path.insert + "from zotero_manager import ...")
# was invisible entirely — real use earned no consolidation credit.
# v1.2 (PREREG E8 §2 F4): the read→run flip needs EXEC (interpreter/shell
# launches + the armed import) told apart from READ (cat/head/tail/less).
# The command alternation is split into the two classes; the merged pattern
# is composed from them and scanned ONCE, so `counts` stays byte-identical
# to the v1.1 ledger. A match is classed by the command on the path's OWN
# line: v1.1's \s+ crosses newlines, so a markdown fence tag
# ("```bash\ncat ~/.memory/x") matches as "bash" — crediting a LAUNCH from
# a fence tag would fake the flip (EXP-2026-004 replay found exactly this).
# Fence-credited lines led by unlisted readers (nl, base64) stay in merged
# counts only, classed as neither.
_EXEC_CMDS = r"python3?|bash|sh|source|\.\s"
_READ_CMDS = r"cat|head|tail|less"
_PATH_TAIL = (r"\s+(?:[-\w=+.%:]+\s+)*"
              r"(?:[\"']?)(?:~|/home/user)/\.memory/([\w./-]+)")
_INVOKE_PAT = re.compile(
    r"(" + _EXEC_CMDS + r"|" + _READ_CMDS + r")" + _PATH_TAIL)
_EXEC_LINE_PAT = re.compile(r"(?:" + _EXEC_CMDS + r")" + _PATH_TAIL)
_READ_LINE_PAT = re.compile(r"(?:" + _READ_CMDS + r")" + _PATH_TAIL)
_ANY_MEM_PAT = re.compile(r"(?:~|/home/user)/\.memory/([\w./-]+)")
_SYSPATH_MEM_PAT = re.compile(r"sys\.path[^\n]{0,120}\.memory")
_IMPORT_PAT = re.compile(
    r"(?:^|[\n;])\s*(?:from\s+([A-Za-z_]\w*)\s+import|"
    r"import\s+([A-Za-z_]\w*))")


def scan_invocations(eproot: str, entry_point: str = "",
                     known_files=None) -> dict:
    """Which banked artifacts the actor agent executed/read this episode.

    Scans session transcripts (session/iter_*/turn.txt) for memory-path
    executions: interpreter/shell launches count for any file (flag-args
    allowed); bare reads (cat/head/...) count too — for notes, a read IS the
    use (prereg §6). Python imports of a banked module count IFF the session
    armed sys.path with ~/.memory AND known_files (the pulled memory tree)
    contains that module — no false credit for stdlib imports. The
    harness-surfaced entry point NEVER counts (it wasn't the actor agent's
    choice). Returns {rel_path: count} + distinct count + breadth flag,
    plus (v1.2) exec_counts / read_counts — the merged counts split by
    command class (armed imports are EXEC: the module RUNS on import)."""
    counts, exec_counts, read_counts = {}, {}, {}
    sess = os.path.join(eproot, "session")
    if not os.path.isdir(sess):
        sess = eproot          # v1.2 autodetect: exam dirs keep iter_NN/
    try:                       # at TOP level (no session/ subdir)
        iters = sorted(os.listdir(sess))
    except FileNotFoundError:
        iters = []
    texts = []
    for it in iters:
        # v1.3 (EXP-2026-007 bug #12): turn.txt stores program code as ONE
        # JSON-escaped line (literal \n), so the line-anchored EXEC/import
        # patterns never fire — ep009's armed import of shotcut_project.py
        # (the era's first read->run flip) scanned as exec_n=0. The
        # materialized program.* artifacts carry real newlines; scan those,
        # with turn.txt as fallback for iters that emitted no program.
        idir = os.path.join(sess, it)
        try:
            srcs = [os.path.join(idir, f) for f in sorted(os.listdir(idir))
                    if f.startswith("program.")]
        except (FileNotFoundError, NotADirectoryError):
            srcs = []
        if not srcs:
            tp = os.path.join(idir, "turn.txt")
            srcs = [tp] if os.path.isfile(tp) else []
        for sp in srcs:
            texts.append(open(sp, encoding="utf-8", errors="ignore").read())
    for txt in texts:
        for m in _INVOKE_PAT.finditer(txt):
            rel = m.group(2).strip("\"'")
            counts[rel] = counts.get(rel, 0) + 1
            line = m.group(0).rsplit("\n", 1)[-1].lstrip()
            if _READ_LINE_PAT.match(line):
                read_counts[rel] = read_counts.get(rel, 0) + 1
            elif _EXEC_LINE_PAT.match(line):
                exec_counts[rel] = exec_counts.get(rel, 0) + 1
    if known_files and any(_SYSPATH_MEM_PAT.search(t) for t in texts):
        mods = {}
        for rel in known_files:
            base = os.path.basename(rel)
            if base.endswith(".py"):
                mods[base[:-3]] = rel
        for txt in texts:
            for m in _IMPORT_PAT.finditer(txt):
                mod = m.group(1) or m.group(2)
                if mod in mods:
                    rel = mods[mod]
                    counts[rel] = counts.get(rel, 0) + 1
                    exec_counts[rel] = exec_counts.get(rel, 0) + 1
    if entry_point:
        for d in (counts, exec_counts, read_counts):
            d.pop(entry_point.strip("/"), None)
    distinct = len(counts)
    return {"counts": counts, "distinct": distinct,
            "breadth_flag": distinct > BREADTH_FLAG_N,
            "exec_counts": exec_counts, "read_counts": read_counts}


# ---------------------------------------------------------- consolidation --
def consolidate(after: dict, invoked: set, ledger: dict, rotation: int,
                passed: bool, entry_point: str = "") -> tuple:
    """Invocation-aged survival. PURE — returns (kept, pruned, new_ledger);
    never mutates inputs. The journal keeps full history by design, so a
    pruned artifact is recoverable by us, just absent from the live memory.

    Rules (prereg §6):
      * a file invoked during a PASSING episode refreshes (last=rotation);
        invocation during a FAILING episode does NOT refresh — only success
        earns keep.
      * a new file enters with born=rotation (grace starts).
      * age-out: (rotation - last_or_born) >= GRACE_ROTATIONS -> pruned.
      * the agent-named entry point is EXEMPT (load-bearing read handle).
    """
    new_ledger = {k: dict(v) for k, v in ledger.items()}
    kept, pruned = {}, []
    for rel, data in after.items():
        rec = new_ledger.get(rel)
        if rec is None:
            rec = {"born": rotation, "last": None}
            new_ledger[rel] = rec
        if passed and rel in invoked:
            rec["last"] = rotation
        anchor = rec["last"] if rec["last"] is not None else rec["born"]
        if rel != entry_point.strip("/") and \
                rotation - anchor >= GRACE_ROTATIONS:
            pruned.append(rel)
            new_ledger.pop(rel, None)
        else:
            kept[rel] = data
    # ledger entries for files no longer present die with them
    for rel in list(new_ledger):
        if rel not in after:
            new_ledger.pop(rel)
    return kept, pruned, new_ledger


def observe_ledger(after: dict, invoked: set, ledger: dict,
                   rotation: int) -> tuple:
    """E8 observational ledger (PREREG E8 §2 F15): consolidation is OFF for
    the drill era, but born/last stay RECORDED — same bookkeeping as
    consolidate(), zero pruning. PURE — returns (kept, pruned, new_ledger);
    kept == after ALWAYS, pruned == [] ALWAYS. No passed gate: this is
    observation, so any invocation refreshes last."""
    new_ledger = {k: dict(v) for k, v in ledger.items()}
    for rel in after:
        rec = new_ledger.get(rel)
        if rec is None:
            rec = {"born": rotation, "last": None}
            new_ledger[rel] = rec
        if rel in invoked:
            rec["last"] = rotation
    # ledger entries for files no longer present die with them
    for rel in list(new_ledger):
        if rel not in after:
            new_ledger.pop(rel)
    return dict(after), [], new_ledger


# ------------------------------------------------------------ curve ledger --
def append_curve(root: str, row: dict) -> None:
    row = dict(row, ts=time.strftime("%F %T"))
    with open(os.path.join(root, "curve.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")


def read_curve(root: str, cls: str = "") -> list:
    p = os.path.join(root, "curve.jsonl")
    if not os.path.exists(p):
        return []
    rows = [json.loads(x) for x in open(p) if x.strip()]
    return [r for r in rows if not cls or r.get("cls") == cls]


# ----------------------------------------------------------- instance card --
def _card_fields(text: str) -> tuple:
    """The shared field parse behind parse_instance / parse_night_section —
    returns (out, braw) with ok left False. braw = the raw BUDGET string
    (presence signal: budget==0 alone cannot tell 'BUDGET: 0' from absent)."""
    out = {"ok": False, "cls": "", "rung": 0, "rationale": "",
           "criteria": [], "prose": "",
           "mode": "", "target": "", "rep": 0, "budget": 0}
    m = re.search(r"^CLASS:\s*(.+)$", text, re.M)
    out["cls"] = m.group(1).strip().lower() if m else ""
    m = re.search(r"^RUNG:\s*([1-5])\b", text, re.M)
    out["rung"] = int(m.group(1)) if m else 0
    m = re.search(r"^RATIONALE:\s*(.+)$", text, re.M)
    out["rationale"] = m.group(1).strip() if m else ""
    m = re.search(r"^MODE:\s*(.+)$", text, re.M)
    out["mode"] = m.group(1).strip().upper() if m else ""
    if out["mode"] == "REPAIR":   # v1.3 (bug #19): natural dialect
        out["mode"] = "MILESTONE"  # for repair nights; budget window
        #                            and semantics are milestone-class
    m = re.search(r"^TARGET:\s*(.+)$", text, re.M)
    out["target"] = m.group(1).strip() if m else ""
    m = re.search(r"^REP:\s*(\d+)\b", text, re.M)
    out["rep"] = int(m.group(1)) if m else 0
    m = re.search(r"^BUDGET:\s*(.+)$", text, re.M)
    braw = m.group(1).strip() if m else ""
    if braw:
        try:
            out["budget"] = int(braw)
        except ValueError:
            out["budget"] = braw               # loop polices type/range (F10)
    sm = re.search(r"^SUCCESS:\s*$(.*)\Z", text, re.M | re.S)
    if sm:
        out["criteria"] = [ln.strip() for ln in sm.group(1).splitlines()
                           if re.match(r"^\s*\d+[.)]\s", ln)]
    pm = re.search(r"^RATIONALE:.*?$\n(.*?)^SUCCESS:", text, re.M | re.S)
    out["prose"] = pm.group(1).strip() if pm else ""
    return out, braw


def parse_instance(text: str, e8: bool = False) -> dict:
    """Mechanical parse of the curriculum agent's INSTANCE card (contract in
    e6_curriculum_charter). Malformed => ok:False; the episode still runs as
    ungraded exploration — a bad card never kills a night (E5 law).

    v2 (PREREG E8 §2 F7): e8=True switches to the drill-card grammar —
    MODE (DRILL|COMPOSE) + TARGET + REP (int>=1) + BUDGET required, RUNG
    optional. BUDGET is parsed, never policed (F10 rejection lives in the
    loop): a clean int parses to int; anything else is kept verbatim so the
    intake can reject with the exact reason. e8=False keeps E6 behavior.

    v3 (PREREG E7 §3 B6): MODE additionally admits MILESTONE (project
    nights); the field parse is shared with parse_night_section."""
    out, braw = _card_fields(text)
    if e8:
        out["ok"] = bool(out["cls"] and
                         out["mode"] in ("DRILL", "COMPOSE", "MILESTONE")
                         and out["target"] and out["rep"] >= 1 and braw
                         and out["rationale"] and out["criteria"]
                         and out["prose"])
    else:
        out["ok"] = bool(out["cls"] and out["rung"] and out["criteria"]
                         and out["prose"])
    return out


# ------------------------------------------------------------ project card --
# PREREG E7 v2.2 §3 — the PROJECT card grammar (B6) + intake budget windows
# (B4, reject-not-clamp; the loop imports these, never re-declares). Multi-
# GUARD (N4) lives in explore/gate.py. Token screen / fence / GUARD counting
# stay in the intake path (loop-owned); this section is pure grammar.
MILESTONE_BUDGET = (60, 120)      # B4: MILESTONE-night intake window
DRILL_BUDGET = (10, 60)           # B4: drill window, unchanged from E8

_PROJECT_PAT = re.compile(r"^PROJECT:\s*(.+?)\s*$", re.M)
_PROJECT_NAME_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_NIGHTS_PAT = re.compile(r"^NIGHTS:\s*(.+?)\s*$", re.M)
_NIGHT_PAT = re.compile(r"^NIGHT:\s*(\d+)\s*/\s*(\d+)\s*(-\s*R)?\s*$", re.M)
_FINAL_PAT = re.compile(r"^FINAL:[ \t]*(.*)$", re.M)


def split_project_card(text: str) -> dict:
    """Delimit a PROJECT card into its sections (PREREG E7 §3 B6) — PURE
    delimitation; semantic judgment (ranges, contiguity, section grammar)
    lives in validate_project. Delimiters: ^PROJECT:, ^NIGHTS:, ^NIGHT: k/N
    (repair form k/N-R accepted — M9), ^FINAL:.

    No PROJECT: line => {ok: False, reasons: ['not-a-project']} and the
    caller falls back to the unchanged drill path; any OTHER reason is a
    malformed PROJECT card (re-author path, never a drill).

    Returns {ok, name, n_nights, target_prose, nights, final_text, reasons};
    nights = [{k, n, repair, text}, ...] in document order (n = the header's
    /N total, kept for validate_project; repair = the k/N-R form, so the
    loop can exclude repair nights from P5's resumption series)."""
    text = text or ""
    out = {"ok": False, "name": "", "n_nights": 0, "target_prose": "",
           "nights": [], "final_text": "", "reasons": []}
    pm = _PROJECT_PAT.search(text)
    if not pm:
        out["reasons"] = ["not-a-project"]
        return out
    out["name"] = pm.group(1)
    nm = _NIGHTS_PAT.search(text)
    if not nm:
        out["reasons"].append("no-nights-line")
    else:
        try:
            out["n_nights"] = int(nm.group(1))
        except ValueError:
            out["reasons"].append(f"bad-nights-value: {nm.group(1)}")
    heads = list(_NIGHT_PAT.finditer(text))
    fm = _FINAL_PAT.search(text)
    if not heads:
        out["reasons"].append("no-night-sections")
    if not fm:
        out["reasons"].append("no-final")
    elif heads and fm.start() < heads[-1].start():
        out["reasons"].append("night-after-final")
    marks = [h.start() for h in heads] + \
        ([fm.start()] if fm else []) + [len(text)]
    prose_start = nm.end() if nm else pm.end()
    out["target_prose"] = text[prose_start:min(
        p for p in marks if p >= prose_start)].strip()
    for i, h in enumerate(heads):
        nexts = [x.start() for x in heads[i + 1:]] + [len(text)]
        if fm and fm.start() > h.end():
            nexts.append(fm.start())
        out["nights"].append({"k": int(h.group(1)), "n": int(h.group(2)),
                              "repair": bool(h.group(3)),
                              "text": text[h.end():min(nexts)].strip()})
    if fm:
        out["final_text"] = (fm.group(1) + text[fm.end():]).strip()
    out["ok"] = not out["reasons"]
    return out


def parse_night_section(text: str, agent_decided_stop: bool = False) -> dict:
    """One NIGHT/FINAL section through the instance grammar MINUS CLASS
    (class is project-level — PREREG E7 §3 B6). Same dict shape as
    parse_instance; ok = the e8 contract with cls waived and MILESTONE
    admitted (BUDGET still parsed, never policed — validate_project and
    the loop police range)."""
    out, braw = _card_fields(text)
    out["ok"] = bool(out["mode"] in ("DRILL", "COMPOSE", "MILESTONE")
                     and out["target"] and out["rep"] >= 1
                     and (braw or agent_decided_stop)
                     and out["rationale"] and out["criteria"]
                     and out["prose"])
    return out


def valid_project_name(name: str) -> bool:
    """Whether a Curriculum-authored project name is a safe state basename."""
    return bool(_PROJECT_NAME_PAT.fullmatch(str(name or "")))


def validate_project(card: dict, agent_decided_stop: bool = False) -> dict:
    """Semantic screen over a split_project_card() dict (PREREG E7 §3 B6):
    n_nights in 2..4, contiguous non-repair numbering 1..N (repair nights
    ride alongside their k — M9), header /N totals match NIGHTS, every
    section parses, per-MODE budget window (MILESTONE [60,120], drills
    [10,60] — B4, reject-not-clamp), FINAL nonempty with valid criteria.
    Returns {ok, reasons}."""
    if not card.get("ok"):
        return {"ok": False,
                "reasons": list(card.get("reasons")) or ["not-a-project"]}
    reasons = []
    if not valid_project_name(card.get("name", "")):
        reasons.append("project name must be a safe 1-64 character basename")
    n = card.get("n_nights", 0)
    if not 2 <= n <= 4:
        reasons.append(f"n_nights: {n} outside [2,4]")
    ks = [nt["k"] for nt in card["nights"] if not nt["repair"]]
    if ks != list(range(1, n + 1)):
        reasons.append(f"night numbering: {ks} != contiguous 1..{n}")
    for nt in card["nights"]:
        label = f"night {nt['k']}" + ("-R" if nt["repair"] else "")
        if nt["n"] != n:
            reasons.append(f"{label}: header total {nt['n']} != NIGHTS {n}")
        if nt["repair"] and not 1 <= nt["k"] <= n:
            reasons.append(f"{label}: repair k outside 1..{n}")
        sec = parse_night_section(nt["text"], agent_decided_stop)
        if not sec["ok"]:
            reasons.append(f"{label}: parse: missing/invalid MODE, TARGET, "
                           "REP, " + ("" if agent_decided_stop else "BUDGET, ") +
                           "RATIONALE, prose, or SUCCESS "
                           "criteria")
            continue
        if not agent_decided_stop:
            lo, hi = (MILESTONE_BUDGET if sec["mode"] == "MILESTONE"
                      else DRILL_BUDGET)
            b = sec["budget"]
            if not (isinstance(b, int) and lo <= b <= hi):
                reasons.append(f"{label}: budget {b} outside [{lo},{hi}] "
                               "(reject, never clamp — B4)")
    final = card.get("final_text", "").strip()
    if not final:
        reasons.append("final: empty")
    else:
        # the charter's FINAL section is bare numbered criteria (no SUCCESS:
        # header) — accept either form (grammar aligned with e7 charter)
        bare = [l for l in final.splitlines()
                if re.match(r"^\s*\d+[.)]\s", l)]
        if not bare and not parse_night_section(final)["criteria"]:
            reasons.append("final: no SUCCESS criteria")
    return {"ok": not reasons, "reasons": reasons}


# ------------------------------------------------------------- review pick --
def last_review(history: list, cap: int = 7000) -> str:
    """The reviewer's final substantive message (skipping the done JSON).
    One implementation for both loops (moved from run_explore for E6)."""
    picks = []
    for m in reversed(history or []):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        txt = str(m.get("content", "")).strip()
        if txt.startswith("{") and '"done"' in txt[:2000]:
            continue
        if len(txt) > 150:
            picks.append(txt)
        if len(picks) >= 2:
            break
    return "\n\n".join(reversed(picks))[:cap] or "(reviewer produced no report)"
