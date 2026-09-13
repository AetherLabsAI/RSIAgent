"""E6 episode loop (PREREG E6 v2.1) — the two nested loops, wired:

    outer loop:   curriculum agent  <->  ( actor agent <-> verifier agent )
    inner loop:                          actor agent <-> verifier agent

Per episode: provision -> card (fence-gated) -> actor agent works from memory
-> HARNESS grades the end state (v11c: before the verifier agent mutates the
machine) -> verifier agent audits (spurious-pass hunt + what-broke) -> memory
restored (never-edits property) -> invocation scan -> invocation-aged
consolidation -> commit -> curve row -> curriculum agent authors the next
card (fence-gated before persist). The era exam is NOT here — it is the
measurement wrapper (run_task.py lane).

Self-contained on purpose: does NOT import run_explore (its import chdir()s).
"""
import dataclasses
import json
import os
import re
import time

from core.loop import run_attempt
from core.trace import ArtifactSink
from explore import commit as mem
from explore import reward
from explore.charter import (E6_SEEDS, e6_actor_charter,
                             e6_curriculum_charter, e6_verifier_charter)

from config.runtime_paths import resolve_forge_root

FORGE = str(resolve_forge_root())
CORPUS = os.path.join(FORGE, "results", "explore", "corpus_shingles.json")

# ------------------------------------------------------------ provisioning --
# Version parity with the eval env (E0-A law): both apps are pre-installed
# snaps in the golden image (t042: "pre-installed... /snap/bin/shotcut";
# zotero tasks read snap/zotero-snap/common/Zotero/zotero.sqlite). Practice
# provisioning verifies presence and creates GENERIC raw material only —
# never task assets (Constraint #0).


def _provision_shotcut(vm, ep_no: int) -> bool:
    out = vm.run_command(
        "which shotcut melt || ls /snap/bin/shotcut 2>/dev/null; "
        "which ffmpeg", timeout=60)
    ok = ("shotcut" in (out or "")) and ("ffmpeg" in (out or ""))
    # throwaway clips, varied per episode (colors/durations keyed to ep_no) —
    # generated locally with ffmpeg testsrc: generic by construction.
    c = ["red", "blue", "green", "yellow", "purple", "cyan"]
    for i in range(4):
        col = c[(ep_no + i) % len(c)]
        dur = 4 + ((ep_no + i) % 5)
        vm.run_command(
            f"mkdir -p /home/user/raw && test -f /home/user/raw/clip{i}.mp4 "
            f"|| ffmpeg -loglevel error -f lavfi -i "
            f"testsrc=duration={dur}:size=640x360:rate=30,"
            f"format=yuv420p -vf drawbox=color={col}@0.5:t=fill "
            f"/home/user/raw/clip{i}.mp4", timeout=120)
    return ok


def _provision_zotero(vm, ep_no: int) -> bool:
    out = vm.run_command(
        "snap list 2>/dev/null | grep -i zotero; which zotero zotero-bin; "
        "ls /snap/bin/ 2>/dev/null | grep -i zotero", timeout=60)
    return "zotero" in (out or "").lower()


PROVISION = {"shotcut": _provision_shotcut, "zotero": _provision_zotero}


# ------------------------------------------------------------- card intake --
def accept_card(text: str) -> dict:
    """Fence-gated card intake (pure). Returns {status, card, hits}:
      * 'fence-reject'  — exam-content hit (L2): the card is NEVER shown to
        the actor agent; the night falls back to seed-only free practice.
        (Prevention at the gate; the era-level disposition still applies.)
      * 'memory-ref'    — v11b violation: runs AS-AUTHORED, logged (prereg).
      * 'malformed'     — parse failed: runs as ungraded free practice.
      * 'ok'
    """
    from tools.exam_fence import audit_text
    if not text.strip():
        return {"status": "malformed", "card": None, "hits": []}
    if _has_terminal_run_script_trailer(text):
        return {"status": "malformed", "card": None,
                "hits": ["terminal run_script transport trailer"]}
    hits = audit_text(text, mode="practice")
    if hits:
        return {"status": "fence-reject", "card": None, "hits": hits}
    card = reward.parse_instance(text)
    if not card["ok"]:
        return {"status": "malformed", "card": card, "hits": []}
    v11b = "~/.memory" in text or "/home/user/.memory" in text
    return {"status": "memory-ref" if v11b else "ok", "card": card,
            "hits": (["memory-ref"] if v11b else [])}


def graded_block(grade: dict) -> str:
    if grade["outcome"] == "ungraded":
        return "(ungraded night — no criteria ran)"
    return "\n".join(f"[{r['n']}] {r['verdict']}  {r['evidence'][:160]}"
                     for r in grade["results"])


def listing_text(memory_dir: str) -> str:
    rows = []
    for root, _, fns in os.walk(memory_dir):
        for fn in sorted(fns):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, memory_dir)
            rows.append(f"{rel}  {os.path.getsize(p)}B")
    return "\n".join(sorted(rows))


def curve_tail_text(root: str, k: int = 12) -> str:
    rows = reward.read_curve(root)[-k:]
    return "\n".join(
        f"{r.get('cls','?')}  ep{r.get('episode',0):03d}  rung {r.get('rung',0)}"
        f"  {r.get('outcome','?')}  {r.get('iters','?')} iters"
        for r in rows)


def _transcript_slice(eproot: str, cap: int = 25000) -> str:
    sess = os.path.join(eproot, "session")
    parts = []
    try:
        for it in sorted(os.listdir(sess)):
            tp = os.path.join(sess, it, "turn.txt")
            if os.path.isfile(tp):
                parts.append(f"[{it}] " + open(
                    tp, encoding="utf-8", errors="ignore").read(400).strip())
    except FileNotFoundError:
        pass
    return "\n".join(parts)[:cap]


_RUN_SCRIPT_TRAILER = re.compile(r"\[exit -?\d+\]\Z")


def _has_terminal_run_script_trailer(text: str) -> bool:
    return bool(_RUN_SCRIPT_TRAILER.search((text or "").strip()))


def _strip_run_script_trailer(output: str) -> str:
    """Remove exactly one terminal runner trailer from command stdout.

    ``run_script`` appends ``[exit N]`` directly after the command's bytes,
    so a guest file without a trailing newline glues the trailer to its final
    line.  Only the final transport suffix is removed; identical text inside
    the payload is preserved.
    """
    text = (output or "").strip()
    return _RUN_SCRIPT_TRAILER.sub("", text, count=1).strip()


def _guest_cat(vm, path: str) -> str:
    tr = vm.run_script("bash", f"cat {path} 2>/dev/null")
    return _strip_run_script_trailer(tr.stdout)


# ---------------------------------------------------------------- episode --
def e6_episode(vm, ep_no: int, cls: str, rotation: int,
               cfg, vcfg, ccfg, root: str) -> dict:
    """One night. Driver owns class scheduling + the rotation counter."""
    MEMORY = os.path.join(root, "memory")
    JOURNAL = os.path.join(root, "journal")
    CURR = os.path.join(root, "curriculum")
    VERD = os.path.join(root, "verdicts")
    eproot = os.path.join(root, "episodes", f"ep{ep_no:03d}")
    for d in (MEMORY, JOURNAL, CURR, VERD):
        os.makedirs(d, exist_ok=True)
    meta = {"episode": ep_no, "cls": cls, "rotation": rotation,
            "t_start": time.strftime("%F %T")}
    seed = E6_SEEDS[cls]

    # 1 provision (presence + generic material; never task assets)
    meta["provisioned"] = PROVISION[cls](vm, ep_no)
    if not meta["provisioned"]:
        meta["status"] = "provision_failed"
        return meta

    # 2 card intake (fence-gated; PER-CLASS thread — a card waits for its own
    # class's next night, so pair-block boundaries never cross-feed. Found
    # live at ep002->ep003: the shotcut card would have served a zotero night)
    card_path = os.path.join(CURR, f"instance_cur_{cls}.md")
    legacy = os.path.join(CURR, "instance_cur.md")
    if os.path.exists(legacy):                 # one-time pre-fix migration
        lc = reward.parse_instance(open(legacy, encoding="utf-8").read())
        tgt = lc["cls"] if lc["cls"] in E6_SEEDS else cls
        os.replace(legacy, os.path.join(CURR, f"instance_cur_{tgt}.md"))
    card_text = open(card_path, encoding="utf-8").read() \
        if os.path.exists(card_path) else ""
    intake = accept_card(card_text)
    meta["card_status"] = intake["status"]
    if intake["status"] in ("fence-reject", "malformed"):
        instance, criteria = seed, []          # seed-only free practice
        if intake["status"] == "fence-reject":
            meta["fence_hits"] = intake["hits"]
    else:
        instance, criteria = card_text, intake["card"]["criteria"]
        meta.update(rung=intake["card"]["rung"],
                    v11b_logged=(intake["status"] == "memory-ref"))

    # 3 memory in
    before = {}
    if os.path.isdir(MEMORY):
        for r_, _, fns in os.walk(MEMORY):
            for fn in fns:
                p = os.path.join(r_, fn)
                before[os.path.relpath(p, MEMORY)] = open(p, "rb").read()
    if not mem.push_memory(vm, MEMORY):
        meta["status"] = "sync_failed"
        return meta

    # 4 actor agent (elastic: cfg carries the UNADVERTISED backstop)
    fb_path = os.path.join(VERD, f"ep{ep_no - 1:03d}.txt")
    feedback = open(fb_path, encoding="utf-8").read() \
        if os.path.exists(fb_path) else ""
    res, _ = run_attempt(
        e6_actor_charter(instance, feedback, listing_text(MEMORY)),
        vm, cfg, ArtifactSink(os.path.join(eproot, "session")))
    meta.update(status=res.status, iters=res.iters,
                session_secs=round(res.wall_secs))
    if res.status == "infra":
        return meta

    # 5 HARNESS GRADE — v11c: before the verifier agent mutates anything
    grade = reward.grade_instance(vm, criteria) if criteria else \
        {"outcome": "ungraded", "results": []}
    meta["outcome"] = grade["outcome"]

    # 6 memory state (actor agent's end state)
    after = mem.pull_memory(vm)
    if not after and before:                   # E5 law: never a wipe
        meta["status"] = "pull_failed"
        return meta

    # 7 verifier agent (never-edits enforced by restore below)
    rres, rhist = run_attempt(
        e6_verifier_charter(instance, graded_block(grade)),
        vm, vcfg, ArtifactSink(os.path.join(eproot, "verify")))
    review = reward.last_review(rhist, cap=4000)
    meta["verify_status"] = rres.status
    tmp = os.path.join(eproot, "_mem_mid")
    mem.write_memory(tmp, after)
    mem.push_memory(vm, tmp)                   # restore: verifier writes die

    # 8 invocation scan + consolidation (rule NEVER disclosed)
    inv = reward.scan_invocations(eproot, known_files=set(after))
    meta.update(invoked_n=inv["distinct"], breadth_flag=inv["breadth_flag"])
    lpath = os.path.join(root, "consolidation_ledger.json")
    ledger = json.load(open(lpath)) if os.path.exists(lpath) else {}
    kept, pruned, ledger2 = reward.consolidate(
        after, set(inv["counts"]), ledger, rotation,
        grade["outcome"] == "pass")
    json.dump(ledger2, open(lpath, "w"))
    meta["pruned"] = pruned

    # 9 commit (8-gram audit unchanged from E5)
    accepted = mem.silent_audit(kept, CORPUS,
                                os.path.join(root, "audit_rejects.jsonl"))
    mem.journal(JOURNAL, ep_no, accepted, {"cls": cls})
    mem.write_memory(MEMORY, accepted)
    meta.update(files_final=len(accepted),
                memory_bytes=sum(len(d) for d in accepted.values()))

    # 10 verdict for tomorrow + the curve row
    verdict = graded_block(grade) + "\nreviewer: " + review
    open(os.path.join(VERD, f"ep{ep_no:03d}.txt"), "w").write(verdict)
    reward.append_curve(root, {
        "episode": ep_no, "cls": cls, "rung": meta.get("rung", 0),
        "outcome": grade["outcome"], "iters": res.iters,
        "invoked_n": inv["distinct"], "breadth_flag": inv["breadth_flag"]})

    # 11 curriculum agent authors the next card (fence-gated before persist)
    cres, _ = run_attempt(
        e6_curriculum_charter(seed, curve_tail_text(root), verdict,
                              _transcript_slice(eproot)),
        vm, ccfg, ArtifactSink(os.path.join(eproot, "curriculum")))
    meta["curriculum_status"] = cres.status
    nxt = _guest_cat(vm, "~/instance_next.md")
    notes = _guest_cat(vm, "~/curriculum_notes.md")
    if notes:
        open(os.path.join(CURR, "curriculum_notes.md"), "w").write(notes)
    nintake = accept_card(nxt)
    meta["next_card_status"] = nintake["status"]
    ncls = (nintake["card"] or {}).get("cls", "")
    npath = os.path.join(
        CURR, f"instance_cur_{ncls if ncls in E6_SEEDS else cls}.md")
    if nintake["status"] == "fence-reject":
        open(npath + ".REJECTED", "w").write(nxt)
        if os.path.exists(npath):
            os.remove(npath)                   # that class's night: seed-only
    elif nxt.strip():
        open(npath, "w").write(nxt)
        open(os.path.join(CURR, f"journal_ep{ep_no:03d}_instance.md"),
             "w").write(nxt)

    meta["t_end"] = time.strftime("%F %T")
    return meta
