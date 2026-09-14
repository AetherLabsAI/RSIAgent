"""Read-only render helpers for the R2a appearance-gate — PURE INFRASTRUCTURE.

CONTRACT: this module must contain NO model-facing prompt literals — every string
here is either a shell command template or a filename pattern. It is therefore not
an audited prompt surface (tests/test_no_leakage.py SURFACES); anything that goes
into model context (questions, evidence framing) lives in core/verifier.py and IS
audited. Keep it that way: if you add a prompt string here, move it to verifier.py.

Path discovery is provenance-only: candidates come from the run's file-change
ledger, the task instruction, and the inspector's own probe output — never from
names known to the harness.
"""
import os
import re

_PATH_RE = re.compile(r"(/home/user/[^\s'\"`|;)\]}>]+|~/[^\s'\"`|;)\]}>]+)")

# filetype -> renderer family (general file-format knowledge, not task knowledge)
_RENDERABLE = {".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
               ".webm": "video",
               ".pptx": "office", ".docx": "office", ".odp": "office",
               ".odt": "office", ".xlsx": "office",
               ".pdf": "pdfdoc",
               ".png": "image", ".jpg": "image", ".jpeg": "image",
               ".bmp": "image", ".gif": "image"}


def harvest_paths(instruction: str, context: str, history) -> list:
    """Candidate deliverable paths, ranked: instruction-mentioned first, then
    provenance/probe order. Renderable types only; deduped."""
    blobs = [context or ""] + [str(m.get("content", "")) for m in history
                               if isinstance(m, dict) and m.get("role") == "user"]
    seen, cands = set(), []
    for blob in blobs:
        for p in _PATH_RE.findall(blob):
            p = p.rstrip(".,:")
            if os.path.splitext(p)[1].lower() in _RENDERABLE and p not in seen:
                seen.add(p)
                cands.append(p)
    return sorted(cands, key=lambda p: (0 if os.path.basename(p) in instruction
                                        else 1, cands.index(p)))


# P1 destination-binding: filename-looking tokens in TASK TEXT (pure lexical — no
# comprehension, no task-specific names). Extension must be alphabetic 2-5 chars
# (rejects version numbers like "4.6"); web TLDs rejected so domain names in
# instructions don't masquerade as files.
_FILENAME_RE = re.compile(r"[\w./\\-]{1,80}\.([A-Za-z]{2,5})\b")
_NONFILE_EXT = {"com", "org", "net", "edu", "gov", "io", "ai", "icu", "www",
                "http", "https", "html", "htm", "php", "aspx"}


def named_files(text: str, cap: int = 8) -> list:
    """Basenames of file-looking tokens mentioned in ``text``, deduped, capped."""
    seen, out = set(), []
    for m in _FILENAME_RE.finditer(text or ""):
        ext = m.group(1).lower()
        if ext in _NONFILE_EXT:
            continue
        base = os.path.basename(m.group(0).rstrip(".,:"))
        if base and base not in seen:
            seen.add(base)
            out.append(base)
        if len(out) >= cap:
            break
    return out


def render_cmd(path: str, out_png: str) -> str:
    """Shell command rendering ``path`` to ``out_png`` inside the guest.
    Read-only w.r.t. the source; writes only under /tmp."""
    kind = _RENDERABLE.get(os.path.splitext(path)[1].lower())
    if kind == "image":
        return f"cp {path!r} {out_png!r}"
    if kind == "video":       # middle frame (fallback 1s if duration unreadable)
        return ("D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "
                f"{path!r} 2>/dev/null | cut -d. -f1); "
                f"ffmpeg -v error -y -ss ${{D:-2}}s -i {path!r} -frames:v 1 "
                f"{out_png!r} 2>/dev/null || ffmpeg -v error -y -ss 1 -i {path!r} "
                f"-frames:v 1 {out_png!r}")
    if kind == "pdfdoc":
        return (f"pdftoppm -f 1 -l 1 -png -singlefile {path!r} "
                f"{out_png[:-4]!r} 2>/dev/null")
    if kind == "office":      # page 1 via headless LO, move to target name
        return (f"soffice --headless --convert-to png --outdir /tmp/rsiagent_insp "
                f"{path!r} >/dev/null 2>&1; mv -f /tmp/rsiagent_insp/*.png {out_png!r} "
                "2>/dev/null")
    return ""
