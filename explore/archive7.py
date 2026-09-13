"""E7 curriculum archive — allowlist-BUILT (PREREG E7 v2.2 §4 N3/M7).

Only clean artifacts enter: past notebooks as persisted, accepted cards,
verdicts, the live curriculum notebook. Quarantine residue (*.REJECTED,
*.VOID, audit_rejects.jsonl) and episode transcripts never match the
allowlist BY CONSTRUCTION — and are belt-and-braces filtered anyway. Every
allowlisted file then re-passes the fence (v1.3, practice mode) as the
one-time inclusion gate at era launch (N3); a hit excludes the file and is
recorded in out_dir/ARCHIVE_EXCLUSIONS.log with hit KINDS only — never the
matched text, because out_dir is exactly what gets pushed to a guest
(commit.push_dir, curriculum sessions ONLY) and a log quoting the leak
would BE the leak. Teardown before any actor turn = commit.rm_guest_dir +
tools.e6_audit.assert_no_archive on its output (M7).
"""
import glob
import json
import os
import shutil
import time

GUEST_ARCHIVE = "~/curriculum_archive"     # push/teardown target (M7)

ALLOWLIST = (                              # relative to the era root (N3)
    "curriculum/journal_ep*_notes.md",     # past notebooks as persisted
    "curriculum/journal_ep*_instance.md",  # accepted cards
    "curriculum/curriculum_notes.md",      # the live notebook
    "verdicts/*.txt",                      # verifier verdicts
)
_NEVER = (".REJECTED", ".VOID")            # quarantine suffixes (defensive —
                                           # the allowlist already misses them)


def build_curriculum_archive(root: str, out_dir: str,
                             authorized_instruction: str = "") -> dict:
    """Stage the allowlisted, fence-clean files of an era root into out_dir
    (relative paths preserved), ready for commit.push_dir. Returns
    {files: [relpaths included], bytes: total, excluded: [relpaths that were
    allowlisted but fence-hit]} — exclusions also land, sanitized, in
    out_dir/ARCHIVE_EXCLUSIONS.log."""
    from tools.exam_fence import audit_text
    # ``out_dir`` is disposable staging, never durable lineage state. Rebuild
    # it from zero so a file included on an earlier pass cannot survive after
    # a later fence rejection.
    if os.path.lexists(out_dir):
        if os.path.islink(out_dir) or not os.path.isdir(out_dir):
            raise RuntimeError("curriculum archive stage must be a real directory")
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    log_p = os.path.join(out_dir, "ARCHIVE_EXCLUSIONS.log")
    included, excluded = [], []
    total = 0
    for pat in ALLOWLIST:
        for p in sorted(glob.glob(os.path.join(root, pat))):
            if not os.path.isfile(p):
                continue
            rel = os.path.relpath(p, root)
            base = os.path.basename(p)
            if (base.endswith(_NEVER) or base == "audit_rejects.jsonl"
                    or "episodes" in rel.split(os.sep)):
                continue
            text = open(p, encoding="utf-8", errors="ignore").read()
            hits = audit_text(
                text, mode="practice",
                authorized_instruction=authorized_instruction)
            if hits:
                excluded.append(rel)
                # Do not turn a hidden-seat/constant fence into Curriculum
                # feedback. The matched text, kind, seat, and tuple all stay
                # outside the guest-visible archive.
                if not authorized_instruction:
                    with open(log_p, "a") as f:
                        f.write(json.dumps({"file": rel,
                                            "reason": "content-boundary",
                                            "ts": time.strftime("%F %T")}) + "\n")
                continue
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            included.append(rel)
            total += os.path.getsize(p)
    return {"files": included, "bytes": total, "excluded": excluded}
