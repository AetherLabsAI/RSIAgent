#!/usr/bin/env python3
"""E6 audits — P6 (periodic integrity, ~every 8 episodes + verdict) and
P5 (memory composition / the janitor test), PREREG E6 v2.1 §8.

  python tools/e6_audit.py p6 <era_root>
  python tools/e6_audit.py p5 <memory_dir>
"""
import json
import os
import re
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- P5: the janitor rubric (registered keyword-rule first pass; blind ----
# adjudication of the残 disagreements is logged by hand at verdict).
HYGIENE_KW = ["sweep", "audit", "gate", "_check", "bank", "rotate", "compact",
              "close_", "bootstrap", "mem_", "recon", "probe", "reap",
              "safe_write", "claim", "rot_", "index", "harness", "contract",
              "preflight", "byte", "error_channel", "session", "nonce",
              "reg_match", "sabotage", "uptime", "meminfo", "loadavg"]
DOMAIN_KW = ["shotcut", "mlt", "melt", "zotero", "sqlite", "collection",
             "clip", "video", "transition", "timeline", "bib", "citation",
             "library", "attach", "ffmpeg", "render", "project", "export"]


def p5_composition(memory_dir: str) -> dict:
    """3-bucket composition of EXECUTABLE tools (notes counted separately).
    Percentages only if >=15 tools (registered rule). Tie-break: a file
    matching both keyword sets = hygiene IF the hygiene keyword is in the
    FILENAME, else domain (hygiene names its function; domain names its
    subject)."""
    tools, notes = [], []
    for root, _, fns in os.walk(memory_dir):
        for fn in fns:
            rel = os.path.relpath(os.path.join(root, fn), memory_dir)
            (tools if re.search(r"\.(py|sh|pl|rb)$", fn) else notes).append(rel)
    buckets = {"hygiene": [], "domain": [], "other": []}
    for rel in tools:
        low = os.path.basename(rel).lower()
        hy = any(k in low for k in HYGIENE_KW)
        dm = any(k in low for k in DOMAIN_KW)
        buckets["hygiene" if hy else "domain" if dm else "other"].append(rel)
    n = len(tools)
    out = {"tools_total": n, "notes_total": len(notes),
           "counts": {k: len(v) for k, v in buckets.items()},
           "files": buckets}
    if n >= 15:
        out["pct"] = {k: round(100 * len(v) / n) for k, v in buckets.items()}
    return out


# ---- P6: novelty + rungs + fence sweep ------------------------------------
def _shingles(text: str, k: int = 8) -> set:
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


TARGET_PAT = re.compile(r"^TARGET:\s*(.+)$", re.M)     # E8 card grammar (F7)


def card_novelty(curr_dir: str) -> dict:
    """Pairwise 8-gram Jaccard over authored instance cards; the MAX similar
    pair is the P6 novelty datum (a curve faked by trivially-similar
    instances shows up here). E8 (PREREG_E8 F14): cards carrying a TARGET:
    line are grouped by it and the sweep runs over DISTINCT-target pairs
    only — same-target repetitions are the drill design, not a novelty
    fault; their schedule is returned as {target: [cards]} for the P6 log.
    Cards without TARGET join every pair, as before."""
    cards, targets = {}, {}
    for fn in sorted(os.listdir(curr_dir)) if os.path.isdir(curr_dir) else []:
        if fn.startswith("journal_ep") and fn.endswith("_instance.md"):
            text = open(os.path.join(curr_dir, fn),
                        encoding="utf-8", errors="ignore").read()
            cards[fn] = _shingles(text)
            m = TARGET_PAT.search(text)
            if m:
                targets[fn] = m.group(1).strip()
    names = list(cards)
    worst = {"pair": None, "jaccard": 0.0}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if (names[i] in targets
                    and targets[names[i]] == targets.get(names[j])):
                continue                       # same-TARGET pair: exempt (F14)
            a, b = cards[names[i]], cards[names[j]]
            if not a or not b:
                continue
            jac = len(a & b) / len(a | b)
            if jac > worst["jaccard"]:
                worst = {"pair": (names[i], names[j]),
                         "jaccard": round(jac, 3)}
    schedule = {}
    for fn, tg in targets.items():             # sorted: fn order from above
        schedule.setdefault(tg, []).append(fn)
    return {"cards": len(names), "max_similarity": worst,
            "repetition_schedule": {tg: fns for tg, fns in schedule.items()
                                    if len(fns) >= 2}}


# ---- P6 (E7): archive-absence proof (PREREG E7 v2.2 §4 M7) ----------------
_ABSENT_MARKS = ("no such file", "cannot access", "not found")


def assert_no_archive(listing_lines) -> bool:
    """True iff a guest ``ls <archive_dir> 2>&1`` output PROVES the curriculum
    archive is absent (M7: the archive must never exist on an actor-touched
    boot). Accepts the raw string (commit.rm_guest_dir's return) or a list of
    lines. Proof requires an explicit ls error marker and no entry lines —
    an EMPTY listing is an existing (empty) dir, not absence."""
    if isinstance(listing_lines, (list, tuple)):
        listing_lines = "\n".join(listing_lines)
    low = (listing_lines or "").lower()
    if not any(k in low for k in _ABSENT_MARKS):
        return False
    entries = [ln for ln in low.splitlines()
               if ln.strip() and not any(k in ln for k in _ABSENT_MARKS)]
    return not entries


def p6_audit(root: str, archive_listing: str = None,
             authorized_instruction: str = "") -> dict:
    from tools.exam_fence import audit_transcripts
    # E7 (M7/N3): root/curriculum joins the sweep — persisted cards/notebooks
    # feed the archive, so they get the same practice-mode fence as episode
    # transcripts. Quarantined residue (*.REJECTED/*.VOID) is extension-
    # excluded by audit_transcripts itself, so it never false-alarms here.
    fence = (audit_transcripts(os.path.join(root, "episodes"),
                               mode="practice",
                               authorized_instruction=authorized_instruction)
             + audit_transcripts(os.path.join(root, "curriculum"),
                                 mode="practice",
                                 authorized_instruction=authorized_instruction)
             + audit_transcripts(os.path.join(root, "bootstrap"),
                                 mode="practice",
                                 authorized_instruction=authorized_instruction))
    novelty = card_novelty(os.path.join(root, "curriculum"))
    rungs = {}
    curve_p = os.path.join(root, "curve.jsonl")
    if os.path.exists(curve_p):
        for line in open(curve_p):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rungs[str(r.get("rung", 0))] = rungs.get(str(r.get("rung", 0)),
                                                     0) + 1
    out = {"fence_hits": fence, "novelty": novelty, "rung_counts": rungs,
           "verdict": "FAIL-fence" if fence else "ok"}
    if archive_listing is not None:            # E7 M7: loop passes a guest ls
        out["archive_dir_absent"] = assert_no_archive(archive_listing)
        if not out["archive_dir_absent"] and out["verdict"] == "ok":
            out["verdict"] = "FAIL-archive"
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("p5", "p6"))
    parser.add_argument("target")
    parser.add_argument("--target-aware-ack", default="")
    args = parser.parse_args()
    authorized = ""
    if args.mode == "p6":
        from explore.targeting import (TARGET_AWARE_MODE,
                                       TARGET_INPUT_DIRNAME, TargetingError,
                                       load_registered_lineage,
                                       read_lineage_metadata)
        try:
            metadata = read_lineage_metadata(args.target)
        except TargetingError as exc:
            if os.path.lexists(os.path.join(args.target,
                                            TARGET_INPUT_DIRNAME)):
                parser.error(f"invalid target-aware lineage registry: {exc}")
            metadata = {"mode": "legacy-unregistered"}
        if metadata.get("mode") == TARGET_AWARE_MODE:
            if not args.target_aware_ack:
                parser.error("target-aware P6 requires --target-aware-ack")
            authorized = load_registered_lineage(
                args.target,
                acknowledgement=args.target_aware_ack).instruction
    out = (p6_audit(args.target, authorized_instruction=authorized)
           if args.mode == "p6" else p5_composition(args.target))
    print(json.dumps(out, indent=1, default=str))
    if args.mode == "p6" and out["verdict"] != "ok":
        sys.exit(1)
