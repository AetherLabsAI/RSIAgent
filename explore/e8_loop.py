"""E8 drill-era episode loop (PREREG E8 v1.2, EXP-2026-005).

Per night: provision -> card intake (parse v2 -> v11b hard-reject -> fence ->
budget range -> static token screen) -> memory in -> DRY-RUN GATE on the
prepared boot (double-run, GUARD slot, mtime manifest; mutation => the boot
is polluted => immediate drill-void, recorded mechanism deviation vs the
prereg's literal clone) -> actor at the card's BUDGET (harness-enforced
max_iters + the one fixed disclosure line) -> harness grade (v11c) ->
verifier -> memory restore -> scan v1.2 -> OBSERVE ledger (F15: nothing
pruned, ever) -> commit -> notebook loop (F16: push prior notes, pull,
fence-audit, archive) -> curriculum authors the next card (re-author path
fed verbatim rejections, F6).

No free-practice nights exist in E8: unfixable cards => drill-void (no
actor run). Self-contained; does NOT import run_explore.
"""
import copy
import json
import os
import time

from core.loop import run_attempt
from core.trace import ArtifactSink
from explore import commit as mem
from explore import reward
from explore.charter import (E8_DISCLOSURE, e6_actor_charter,
                             e6_verifier_charter, e8_curriculum_charter)
from explore.e6_loop import (_guest_cat, _has_terminal_run_script_trailer,
                             _provision_shotcut,
                             _strip_run_script_trailer, _transcript_slice,
                             graded_block, listing_text)
from explore.gate import (dry_run_verdict, find_guard, static_token_screen,
                          strip_guard)
from tools.exam_fence import audit_text

from config.runtime_paths import resolve_forge_root

FORGE = str(resolve_forge_root())
CORPUS = os.path.join(FORGE, "results", "explore", "corpus_shingles.json")
BUDGET_MIN, BUDGET_MAX = 10, 60
GATE_STAMP = "/tmp/.gate_stamp"


# ------------------------------------------------------------- card intake --
def validate_card(text: str) -> dict:
    """Host-side checks that need no VM: parse (e8 grammar), v11b hard-reject,
    fence, budget range, static token screen, single-GUARD rule.
    Returns {ok, card, reasons}."""
    reasons = []
    if not text.strip():
        return {"ok": False, "card": None, "reasons": ["empty card"]}
    card = reward.parse_instance(text, e8=True)
    if _has_terminal_run_script_trailer(text):
        reasons.append("transport: terminal [exit N] trailer")
    if not card["ok"]:
        reasons.append("parse: missing/invalid CLASS, MODE, TARGET, REP, "
                       "BUDGET, RATIONALE, prose, or SUCCESS criteria")
    if "~/.memory" in text or "/home/user/.memory" in text:
        reasons.append("v11b: card references ~/.memory (hard reject in E8)")
    hits = audit_text(text, mode="practice")
    if hits:
        reasons.append(f"fence: {json.dumps(hits)[:400]}")
    b = card.get("budget", 0) if card else 0
    if card["ok"] and not (BUDGET_MIN <= b <= BUDGET_MAX):
        reasons.append(f"budget: {b} outside [{BUDGET_MIN},{BUDGET_MAX}] "
                       "(reject, never clamp — F17)")
    if card["ok"]:
        viol = static_token_screen(card["criteria"])
        if viol:
            reasons.append(f"token-screen: {viol}")
        try:
            find_guard(card["criteria"])
        except ValueError as e:
            reasons.append(f"guard: {e}")
    return {"ok": not reasons, "card": card, "reasons": reasons}


def run_dry_gate(vm, criteria: list) -> dict:
    """The F5 gate on the prepared boot (post-provision, post-memory —
    grade-time environment). Double-run each criterion, mtime manifest.
    Returns {accept, reasons, mutated, per_criterion}."""
    import re as _re
    guard = find_guard(criteria)
    cmds = [_re.sub(r"^\s*\d+[.)]\s*", "", strip_guard(c)).strip()
            for c in criteria]
    vm.run_command(f"touch {GATE_STAMP}", timeout=30)

    results = []
    for i, cmd in enumerate(cmds):
        runs = []
        for _ in range(2):
            try:
                tr = vm.run_script("bash", cmd, timeout=180)
                runs.append(_strip_run_script_trailer(tr.stdout))
            except Exception as e:                          # noqa: BLE001
                runs.append(f"[gate error] {e}")
        results.append({"n": i + 1, "run1": runs[0], "run2": runs[1]})
    verdict = dry_run_verdict(results, guard_index=guard)
    # mutation manifest: /home/user NON-HIDDEN files only — the live desktop
    # writes dotfiles (.cache/.config) continuously, and criteria may
    # legitimately scratch in /tmp (E6-style pixel checks); task-visible
    # state the actor could be pre-fed lives in non-hidden /home/user paths
    created = vm.run_command(
        f"find /home/user -newer {GATE_STAMP} -type f "
        "! -path '*/.*' 2>/dev/null | head -20", timeout=60) or ""
    created = [l for l in created.splitlines()
               if l.strip() and "[exit" not in l]
    mutated = bool(created)
    reasons = list(verdict["reasons"])
    if mutated:
        reasons.append(f"mutation: gate created/modified {created[:5]}")
    return {"accept": verdict["verdict"] == "ACCEPT" and not mutated,
            "reasons": reasons, "mutated": mutated,
            "per_criterion": verdict["results"]}


def curve_tail_text(root: str, k: int = 14) -> str:
    rows = reward.read_curve(root)[-k:]
    return "\n".join(
        f"{r.get('mode','?')}  {r.get('target','?')}  rep{r.get('rep',0)}"
        f"  budget {r.get('budget',0)}  {r.get('outcome','?')}"
        f"  {r.get('iters','?')} iters"
        for r in rows)


def _padding_tell(eproot: str, iters: int) -> float:
    """F11: fraction of turns AFTER the last program-bearing turn (approx of
    'turns after last state-changing command'). 0.0 = worked to the end."""
    sess = os.path.join(eproot, "session")
    try:
        its = sorted(os.listdir(sess))
    except FileNotFoundError:
        return 0.0
    last_prog = 0
    for idx, it in enumerate(its, 1):
        d = os.path.join(sess, it)
        if any(f.startswith("program") for f in os.listdir(d)):
            last_prog = idx
    return round(1.0 - last_prog / max(1, len(its)), 3)


def _notebook_push(vm, notes: str) -> None:
    import base64
    b64 = base64.b64encode(notes.encode()).decode()
    vm.run_command(
        f"echo {b64} | base64 -d > /home/user/curriculum_notes.md",
        timeout=30)


def _artifact_telemetry(memory_dir: str) -> dict:
    n, b = 0, 0
    for r, _, fns in os.walk(memory_dir):
        for fn in fns:
            n += 1
            b += os.path.getsize(os.path.join(r, fn))
    return {"files": n, "bytes": b}


# ---------------------------------------------------------------- episode --
def e8_episode(vm, ep_no: int, cfg, vcfg, ccfg, root: str) -> dict:
    MEMORY = os.path.join(root, "memory")
    JOURNAL = os.path.join(root, "journal")
    CURR = os.path.join(root, "curriculum")
    VERD = os.path.join(root, "verdicts")
    eproot = os.path.join(root, "episodes", f"ep{ep_no:03d}")
    for d in (MEMORY, JOURNAL, CURR, VERD):
        os.makedirs(d, exist_ok=True)
    meta = {"episode": ep_no, "cls": "shotcut",
            "t_start": time.strftime("%F %T")}

    # 1 provision (presence + generic clips; never task assets)
    meta["provisioned"] = _provision_shotcut(vm, ep_no)
    if not meta["provisioned"]:
        meta["status"] = "provision_failed"
        return meta

    # 2 card intake — host-side validation, one re-author allowed (F6)
    card_path = os.path.join(CURR, "instance_cur_shotcut.md")
    card_text = open(card_path, encoding="utf-8").read() \
        if os.path.exists(card_path) else ""
    v = validate_card(card_text)
    meta["card_status"] = "ok" if v["ok"] else "; ".join(v["reasons"])[:300]
    rejection_history = []

    # 3 memory in (grade-time environment for the gate)
    before = {}
    for r_, _, fns in os.walk(MEMORY):
        for fn in fns:
            p = os.path.join(r_, fn)
            before[os.path.relpath(p, MEMORY)] = open(p, "rb").read()
    if not mem.push_memory(vm, MEMORY):
        meta["status"] = "sync_failed"
        return meta

    # 4 dry-run gate (+ the single re-author pass on any non-mutation reject)
    def gated(vv):
        if not vv["ok"]:
            return {"accept": False, "reasons": vv["reasons"],
                    "mutated": False}
        return run_dry_gate(vm, vv["card"]["criteria"])

    g = gated(v)
    meta["gate1"] = {"accept": g["accept"], "reasons": g["reasons"][:4]}
    if not g["accept"] and not g["mutated"]:
        rejection_history.append("; ".join(g["reasons"])[:600])
        # ONE curriculum re-author pass, fed the verbatim reasons
        rres, _ = run_attempt(
            e8_curriculum_charter(curve_tail_text(root), "", "",
                                  _load_notes(CURR),
                                  rejection=f"CARD:\n{card_text[:1500]}\n"
                                            f"REJECTED: {rejection_history[-1]}"),
            vm, ccfg, ArtifactSink(os.path.join(eproot, "reauthor")))
        meta["reauthor_status"] = rres.status
        card_text = _guest_cat(vm, "~/instance_next.md")
        v = validate_card(card_text)
        g = gated(v)
        meta["gate2"] = {"accept": g["accept"], "reasons": g["reasons"][:4]}
    if not g["accept"]:
        # drill-void: no actor run (mutation => polluted boot, also void)
        meta["status"] = "drill_void"
        meta["void_reason"] = ("mutation" if g["mutated"]
                               else "; ".join(g["reasons"])[:300])
        open(card_path + ".VOID", "w").write(card_text)
        if os.path.exists(card_path):
            os.remove(card_path)
        reward.append_curve(root, {
            "episode": ep_no, "mode": "void", "target": "-", "rep": 0,
            "budget": 0, "outcome": "drill-void", "iters": 0})
        # still author the next night's card (fresh boot next night)
        _author_next(vm, ep_no, root, CURR, JOURNAL, eproot,
                     "(drill-void night — the actor did not run; author a "
                     "valid card)", meta, ccfg)
        meta["t_end"] = time.strftime("%F %T")
        return meta

    card = v["card"]
    meta.update(mode=card["mode"], target=card["target"], rep=card["rep"],
                budget=card["budget"])

    # 5 actor agent at the card's budget (F17: cfg copy, never clamp)
    fb_path = os.path.join(VERD, f"ep{ep_no - 1:03d}.txt")
    feedback = open(fb_path, encoding="utf-8").read() \
        if os.path.exists(fb_path) else ""
    night_cfg = copy.deepcopy(cfg)
    night_cfg.max_iters = card["budget"]
    instance = card_text + E8_DISCLOSURE.format(n=card["budget"])
    res, _ = run_attempt(
        e6_actor_charter(instance, feedback, listing_text(MEMORY)),
        vm, night_cfg, ArtifactSink(os.path.join(eproot, "session")))
    meta.update(status=res.status, iters=res.iters,
                session_secs=round(res.wall_secs))
    if res.status == "infra":
        return meta

    # 6 harness grade (v11c: before the verifier mutates)
    criteria = [strip_guard(c) for c in card["criteria"]]
    grade = reward.grade_instance(vm, criteria)
    meta["outcome"] = grade["outcome"]

    # 7 memory end-state
    after = mem.pull_memory(vm)
    if not after and before:
        meta["status"] = "pull_failed"
        return meta

    # 8 verifier agent (never-edits via restore)
    rres, rhist = run_attempt(
        e6_verifier_charter(card_text, graded_block(grade)),
        vm, vcfg, ArtifactSink(os.path.join(eproot, "verify")))
    review = reward.last_review(rhist, cap=4000)
    meta["verify_status"] = rres.status
    tmp = os.path.join(eproot, "_mem_mid")
    mem.write_memory(tmp, after)
    mem.push_memory(vm, tmp)

    # 9 scan v1.2 + OBSERVE ledger (F15: nothing pruned)
    inv = reward.scan_invocations(eproot, known_files=set(after))
    meta.update(invoked_n=inv["distinct"],
                exec_n=len(inv.get("exec_counts", {})),
                read_n=len(inv.get("read_counts", {})))
    lpath = os.path.join(root, "observe_ledger.json")
    ledger = json.load(open(lpath)) if os.path.exists(lpath) else {}
    kept, pruned, ledger2 = reward.observe_ledger(
        after, set(inv["counts"]), ledger, ep_no - 1)
    json.dump(ledger2, open(lpath, "w"))
    assert not pruned, "observe_ledger must never prune (F15)"

    # 10 commit + telemetry
    accepted = mem.silent_audit(kept, CORPUS,
                                os.path.join(root, "audit_rejects.jsonl"))
    mem.journal(JOURNAL, ep_no, accepted, {"mode": card["mode"],
                                           "target": card["target"]})
    mem.write_memory(MEMORY, accepted)
    meta["artifact"] = _artifact_telemetry(MEMORY)

    # 11 verdict + curve row (with pressure ratio + padding tell)
    verdict = graded_block(grade) + "\nreviewer: " + review
    open(os.path.join(VERD, f"ep{ep_no:03d}.txt"), "w").write(verdict)
    reward.append_curve(root, {
        "episode": ep_no, "mode": card["mode"], "target": card["target"],
        "rep": card["rep"], "budget": card["budget"],
        "outcome": grade["outcome"], "iters": res.iters,
        "pressure": round(card["budget"] / 55.0, 2),
        "padding": _padding_tell(eproot, res.iters),
        "invoked_n": inv["distinct"], "exec_n": meta["exec_n"]})

    # 12 notebook loop + next card (F16 + F6)
    _author_next(vm, ep_no, root, CURR, JOURNAL, eproot, verdict, meta, ccfg)
    meta["t_end"] = time.strftime("%F %T")
    return meta


def _load_notes(CURR: str) -> str:
    p = os.path.join(CURR, "curriculum_notes.md")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _author_next(vm, ep_no, root, CURR, JOURNAL, eproot, verdict, meta, ccfg):
    notes_prior = _load_notes(CURR)
    _notebook_push(vm, notes_prior)
    cres, _ = run_attempt(
        e8_curriculum_charter(curve_tail_text(root), verdict,
                              _transcript_slice(eproot), notes_prior),
        vm, ccfg, ArtifactSink(os.path.join(eproot, "curriculum")))
    meta["curriculum_status"] = cres.status
    nxt = _guest_cat(vm, "~/instance_next.md")
    notes = _guest_cat(vm, "~/curriculum_notes.md")
    if notes:
        hits = audit_text(notes, mode="practice")
        if hits:
            open(os.path.join(CURR, "curriculum_notes.md.REJECTED"),
                 "w").write(notes)
            meta["notebook"] = "fence-quarantined"
        else:
            open(os.path.join(CURR, "curriculum_notes.md"), "w").write(
                notes[:8000])
            open(os.path.join(JOURNAL, f"journal_ep{ep_no:03d}_notes.md"),
                 "w").write(notes[:8000])
            meta["notebook"] = "ok"
    card_path = os.path.join(CURR, "instance_cur_shotcut.md")
    nv = validate_card(nxt)
    meta["next_card_status"] = "ok" if nv["ok"] else \
        "; ".join(nv["reasons"])[:200]
    if nxt.strip():
        # persist even imperfect cards — next night's intake re-validates and
        # owns the re-author pass on its own boot (F6)
        open(card_path, "w").write(nxt)
        open(os.path.join(CURR, f"journal_ep{ep_no:03d}_instance.md"),
             "w").write(nxt)
