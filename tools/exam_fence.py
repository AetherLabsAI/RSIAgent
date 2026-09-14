#!/usr/bin/env python3
"""Host-only rejection checks for benchmark leakage.

Guest isolation, injected-text checks, transcript audits, and distinctive grader
constant tuples keep grading material outside practice. Practice mode rejects
task identifiers and instructions; exam mode permits the authorized instruction
while continuing to reject grader and repository signals.
"""
import json
import os
import re
import sys
import shlex

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.runtime_paths import resolve_root, resolve_osworld_root

V2_DIR = str(resolve_osworld_root() / "evaluation_examples/task_class")
FENCE_DIR = str(resolve_root() / "results/audit/fence")
EXAM_SEATS = ["042", "044", "056", "011", "036", "062",           # benchmark-derived fixtures
              "001", "005", "006", "085"]                          # guards

# --- signal patterns -------------------------------------------------------
PATH_PAT = re.compile(r"OSWorld-V2|evaluation_examples|task_class", re.I)
REPO_PAT = re.compile(r"github\.com/+[^ '\"]*OSWorld|xlang-ai", re.I)
TASKID_PAT = re.compile(r"\btask[_ ]?0*(%s)\b" % "|".join(EXAM_SEATS), re.I)

_COMMON = {"0", "1", "2", "3", "10", "100", "1.0", "0.0", "0.5", "1080",
           "1920", "60", "30", "24", "50", "0.1", "0.25", "0.75"}

# v1.2 (2026-08-15, EXP-2026-002 deviation #4): APP-VOCABULARY ALLOWLIST.
# A grader that parses an app's files necessarily quotes the app's format
# vocabulary — and every legitimate practice session writes that same
# vocabulary, because it is what the app's files ARE. Such tokens can never
# identify a seat. Frozen from the eps1-8 intersection sweep, run while the
# regime was PROVEN clean (L1 probe passed, all cards gated) — the only
# non-circular corpus. Reviewed per-token; every entry is app-schema/tool
# vocabulary, none is task-author content:
#   MLT/Shotcut XML schema + services: frame_rate_num/den, lift_gamma_gain,
#     warp_speed;  Zotero API/DB fields + item types: entry_count, item_ids,
#     item_type, parent_id, journal article, Collection ;  shell idiom /
#     generic tool names: sleep 2, list_directory;  bare decimals already
#     colliding in clean practice: 1.35, 1034, 3.00, 7.00.
_APP_VOCAB = {"frame_rate_num", "frame_rate_den", "lift_gamma_gain",
              "warp_speed", "entry_count", "item_ids", "item_type",
              "parent_id", "journal article", "Collection ", "sleep 2",
              "list_directory", "1.35", "1034", "3.00", "7.00",
              # MLT filter/property schema uses the same vocabulary
              # as lift_gamma_gain/warp_speed above; ordinary editing
              # writes these in any legitimate fade/color/mask/speed work.
              # Task-author content (deliverable filenames, instruction
              # fragments) deliberately NOT allowlisted.
              "fade_out", "gain_r", "gain_g", "gain_b", "mask_start",
              "warp_resource", "0.96"}
# Clock fragments ("02.000" from HH:MM:SS.mmm) are format artifacts, never
# distinctive — excluded as a CLASS, not just the two observed instances.
_CLOCK_FRAG = re.compile(r"^\d{2}\.\d{3}$")

# Numeric harness-contract lines ("BUDGET: 40",
# "REP: 2") are bare integers by construction and can collide with a seat's
# numeric constants; such lines are stripped from the copy of the text used
# for constant-tuple counting ONLY — the path/URL/task-id/8-gram layers
# always audit the full text.
_BUDGET_LINE = re.compile(r"^(?:BUDGET|REP):\s*\d+\s*$", re.M)
_NUMERIC_LITERAL = re.compile(r"\d+(?:\.\d+)?\Z")


def _contains_exact_literal(text: str, literal: str) -> bool:
    """Return whether ``literal`` occurs as a complete token in ``text``.

    The denylist contains both numeric values and identifier-like strings.
    Raw substring matching makes an SVG coordinate such as ``131.45508``
    look like the constant ``1.45``, and makes ``tile_size_bytes_minus1``
    look like ``size_bytes``.  Keep the literal match case-sensitive, as it
    has always been, but require token boundaries at word-like edges.  A dot
    is additionally part of a numeric token so a decimal cannot match inside
    a longer decimal or version number.
    """
    if not literal or literal not in text:
        return False
    numeric = bool(_NUMERIC_LITERAL.fullmatch(literal))
    boundary = r"[\w.]" if numeric else r"\w"
    left = rf"(?<!{boundary})" if re.match(r"\w", literal[0]) else ""
    right = rf"(?!{boundary})" if re.match(r"\w", literal[-1]) else ""
    return re.search(left + re.escape(literal) + right, text) is not None


def extract_constants(seat: str) -> list:
    """Distinctive literals from one grader: floats with >=2 decimals, ints
    >=4 digits, and quoted strings 6..60 chars that look like content (not
    code). Host-side only — the output file must NEVER be pushed to a guest
    or referenced in any prompt."""
    src = open(os.path.join(V2_DIR, f"task_{seat}.py"),
               encoding="utf-8", errors="ignore").read()
    out = set()
    for m in re.findall(r"(?<![\w.])(\d+\.\d{2,}|\d{4,})(?![\w.])", src):
        if m not in _COMMON:
            out.add(m)
    for m in re.findall(r"['\"]([A-Za-z][A-Za-z0-9 _\-\.]{5,60})['\"]", src):
        low = m.lower()
        if (" " in m or "_" in m) and not any(
                k in low for k in ("http", "self.", "utf", "ignore", "task_",
                                   ".py", "def ", "return", "error")):
            out.add(m)
    return sorted(out)


def build_denylist(path: str = "") -> dict:
    """Write the per-seat constant lists (L4). Idempotent; run at build.

    v1.1 (2026-08-14, EXP-2026-002 recorded deviation): drop benchmark-common
    constants — document frequency measured across ALL task graders in
    V2_DIR, not just our 10 seats: a token appearing in >=3 different tasks'
    graders ('codec_type', 'mkdir -p ', '1337') is tool vocabulary, not a
    seat identifier; ep002's legitimate card was false-positived on exactly
    such tokens."""
    path = path or os.path.join(FENCE_DIR, "grader_constants.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = {s: extract_constants(s) for s in EXAM_SEATS}
    df = {}
    for fn in os.listdir(V2_DIR):
        m = re.match(r"task_(\d+)\.py$", fn)
        if not m:
            continue
        for v in set(extract_constants(m.group(1))):
            df[v] = df.get(v, 0) + 1
    data = {s: [v for v in vals if df.get(v, 0) < 3
                and v not in _APP_VOCAB and not _CLOCK_FRAG.match(v)]
            for s, vals in raw.items()}
    json.dump(data, open(path, "w"), indent=1)
    return data


def _load_constants() -> dict:
    p = os.path.join(FENCE_DIR, "grader_constants.json")
    if os.path.exists(p):
        return json.load(open(p))
    return build_denylist()


def instruction_text(seat: str) -> str:
    """The task's instruction string (handles triple- and single-quoted)."""
    src = open(os.path.join(V2_DIR, f"task_{seat}.py"),
               encoding="utf-8", errors="ignore").read()
    m = re.search(r'instruction\s*=\s*f?(?:"""(.+?)"""|"(.+?)")', src, re.S)
    return (m.group(1) or m.group(2)) if m else ""


def _instruction_8grams() -> set:
    """8-gram shingles of the exam-seat instructions (practice-mode L2/L3)."""
    grams = set()
    for s in EXAM_SEATS[:6]:
        words = re.findall(r"[a-z0-9']+", instruction_text(s).lower())
        for i in range(len(words) - 7):
            grams.add(" ".join(words[i:i + 8]))
    return grams


_GRAMS_CACHE = None


def _authorized_near_shingle(gram: str, authorized_words: list[str]) -> bool:
    """Accept a target shingle after at most four in-order word omissions.

    Agent-authored text naturally drops courtesy words and determiners from the
    operator-authorized instruction, including a repeated role noun phrase.
    That is still the authorized input, not a grader leak. Keep the exception
    deliberately narrow: all eight words must occur in order inside a
    twelve-word window of the exact authorized instruction. Reordering,
    substitution, and wider paraphrase remain fenced.
    """
    wanted = gram.split()
    if len(wanted) != 8:
        return False
    max_span = len(wanted) + 4
    for start, word in enumerate(authorized_words):
        if word != wanted[0]:
            continue
        end = min(len(authorized_words), start + max_span)
        cursor = start + 1
        for expected in wanted[1:]:
            while cursor < end and authorized_words[cursor] != expected:
                cursor += 1
            if cursor >= end:
                break
            cursor += 1
        else:
            return True
    return False


def audit_text(text: str, mode: str = "practice",
               authorized_instruction: str = "") -> list:
    """Return violation records for one text (empty means clean).

    ``authorized_instruction`` is the narrow target-aware exception: shingles
    and literal constants already present in that operator-authorized natural-
    language instruction are training input, not leakage.  Structural signals
    (benchmark paths/repositories/task ids) and every grader-only value remain
    forbidden.  The empty default preserves the blind fence exactly.
    """
    global _GRAMS_CACHE
    hits = []
    if PATH_PAT.search(text):
        hits.append({"kind": "exam-path", "match": PATH_PAT.search(text).group(0)})
    if REPO_PAT.search(text):
        hits.append({"kind": "repo-url", "match": REPO_PAT.search(text).group(0)})
    if mode == "practice":
        m = TASKID_PAT.search(text)
        if m:
            hits.append({"kind": "task-id", "match": m.group(0)})
        if _GRAMS_CACHE is None:
            _GRAMS_CACHE = _instruction_8grams()
        allowed_grams = set()
        awords = []
        if authorized_instruction:
            awords = re.findall(
                r"[a-z0-9']+", authorized_instruction.lower())
            allowed_grams = {" ".join(awords[i:i + 8])
                             for i in range(len(awords) - 7)}
        words = re.findall(r"[a-z0-9']+", text.lower())
        for i in range(len(words) - 7):
            g = " ".join(words[i:i + 8])
            if (g in _GRAMS_CACHE and g not in allowed_grams
                    and not _authorized_near_shingle(g, awords)):
                hits.append({"kind": "instruction-8gram", "match": g})
                break
    consts = _load_constants()
    tuple_text = _BUDGET_LINE.sub("", text)    # constant-tuple copy only
    for seat, vals in consts.items():
        found = [v for v in vals if _contains_exact_literal(tuple_text, v)]
        if authorized_instruction:
            found = [v for v in found if not _contains_exact_literal(
                authorized_instruction, v)]
        strings = [v for v in found
                   if len(v) >= 6 and re.search(r"[A-Za-z]", v)]
        nums = [v for v in found if v not in strings]
        # tuple rule v1.1 (red-team #7 + ep002 false positive): a pair of
        # generic decimals ("0.20"+"0.35") is not a leak. A hit now needs a
        # letter-bearing distinctive constant (>=2 matches total) or >=3
        # co-occurring numeric constants of one seat.
        if (strings and len(found) >= 2) or len(nums) >= 3:
            hits.append({"kind": "constant-tuple", "seat": seat,
                         "match": (strings + nums)[:4]})
    return hits


def audit_transcripts(root: str, mode: str = "practice",
                      authorized_instruction: str = "") -> list:
    """Walk transcripts under root; return per-file violations (L3)."""
    out = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith((".txt", ".json", ".jsonl", ".md")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            hits = audit_text(
                txt, mode=mode,
                authorized_instruction=authorized_instruction)
            if hits:
                out.append({"file": p, "hits": hits})
    return out


GUEST_PROBE_CMDS = [
    # L1 live verification, run INSIDE the guest via vm.run_command at P0.
    # Every probe must come back DENIED/absent or the fence FAILS.
    ("host-fs", "ls " + shlex.quote(str(resolve_osworld_root())) + " 2>&1 | head -2"),
    ("host-fs-2", "cat " + shlex.quote(str(Path(FENCE_DIR) / "grader_constants.json"))
                  + " 2>&1 | head -1"),
    ("qcow-mount", "ls /System.qcow2 2>&1 | head -1"),
    ("host-http", "for p in 8000 8080 5003 3000 80; do "
                  "curl -sm2 http://10.0.2.2:$p/OSWorld-V2/ -o /dev/null "
                  "-w \"$p:%{http_code} \"; done; echo"),
]


def judge_guest_probe(results: dict) -> list:
    """results = {name: output}. Return failures (empty = L1 PASS)."""
    bad = []
    for name, out in results.items():
        low = (out or "").lower()
        if name.startswith("host-fs") and not any(
                k in low for k in ("no such file", "not found", "denied",
                                   "cannot access")):
            bad.append({"probe": name, "out": out[:120]})
        if name == "qcow-mount" and "no such file" not in low:
            bad.append({"probe": name, "out": out[:120]})
        if name == "host-http" and re.search(r":(200|301|302)", out or ""):
            bad.append({"probe": name, "out": out[:120]})
    return bad


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        d = build_denylist()
        print(f"constants: {sum(len(v) for v in d.values())} across "
              f"{len(d)} seats -> {FENCE_DIR}/grader_constants.json")
    elif len(sys.argv) > 2 and sys.argv[1] == "audit":
        mode = sys.argv[3] if len(sys.argv) > 3 else "practice"
        v = audit_transcripts(sys.argv[2], mode=mode)
        print(json.dumps(v, indent=1) if v else "CLEAN")
        sys.exit(1 if v else 0)
    else:
        print(__doc__)
