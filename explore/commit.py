"""Phase-2 memory transport + commit gates (host side).

The agent's MEMORY is a directory it fully owns in the guest (~/.memory). This
module only MOVES it (sync in/out), ARCHIVES it (journal), and legality-scans
it (silent audit) — it never authors content (v2.0 control split).
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time

log = logging.getLogger("forge.explore.commit")

GUEST_MEM = "~/.memory"
_CHUNK = 3000          # base64 chars per run_command append (arg-length safe)


# ---------------------------------------------------------------- transport --
def push_memory(vm, memory_dir: str) -> bool:
    """memory (host dir) -> guest ~/.memory. Full replace; guest dir recreated."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if os.path.isdir(memory_dir):
            for name in sorted(os.listdir(memory_dir)):
                tf.add(os.path.join(memory_dir, name), arcname=name)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    vm.run_command(f"rm -rf {GUEST_MEM} /tmp/mem_in.b64 && mkdir -p {GUEST_MEM}")
    for i in range(0, len(b64), _CHUNK):
        chunk = b64[i:i + _CHUNK]
        vm.run_command(f"printf %s '{chunk}' >> /tmp/mem_in.b64", timeout=30)
    out = vm.run_command(
        f"base64 -d /tmp/mem_in.b64 > /tmp/mem_in.tgz 2>/tmp/mem_err && "
        f"tar xzf /tmp/mem_in.tgz -C {GUEST_MEM} 2>>/tmp/mem_err; "
        f"echo SYNC_RC=$?; ls {GUEST_MEM} | head -5")
    return "SYNC_RC=0" in out


def push_dir(vm, host_dir: str, guest_path: str) -> bool:
    """host dir -> arbitrary guest path. Full replace; guest dir recreated.
    Generic sibling of push_memory (same chunked-b64 tar pipe), added for the
    E7 curriculum-archive transport (PREREG E7 v2.2 §4 M7) but destination-
    agnostic on purpose. Uses its own /tmp staging names so an interleaved
    push_memory can never collide."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if os.path.isdir(host_dir):
            for name in sorted(os.listdir(host_dir)):
                tf.add(os.path.join(host_dir, name), arcname=name)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    vm.run_command(f"rm -rf {guest_path} /tmp/dir_in.b64 && "
                   f"mkdir -p {guest_path}")
    for i in range(0, len(b64), _CHUNK):
        chunk = b64[i:i + _CHUNK]
        vm.run_command(f"printf %s '{chunk}' >> /tmp/dir_in.b64", timeout=30)
    out = vm.run_command(
        f"base64 -d /tmp/dir_in.b64 > /tmp/dir_in.tgz 2>/tmp/dir_err && "
        f"tar xzf /tmp/dir_in.tgz -C {guest_path} 2>>/tmp/dir_err; "
        f"echo SYNC_RC=$?; ls {guest_path} | head -5")
    return "SYNC_RC=0" in out


def rm_guest_dir(vm, guest_path: str) -> str:
    """rm -rf a guest dir and return the verification ls output — the ABSENCE
    PROOF string (M7: the archive is torn down with logged proof before any
    actor turn). The loop logs this string verbatim; tools.e6_audit.
    assert_no_archive() judges it mechanically."""
    return vm.run_command(
        f"rm -rf {guest_path}; ls {guest_path} 2>&1 | head -3")


def pull_memory(vm, attempts: int = 3) -> dict:
    """guest ~/.memory -> {relpath: bytes}. Empty dict if no memory exists.
    RETRIED (e1-v2 ep008: one silent transient empty-fetch cost a 46-iter
    episode's memory delta — the wipe-guard saved the memory, but the work was
    lost; a flake must not cost an episode). Logs every failed attempt."""
    import logging as _logging
    import time as _time
    _log = _logging.getLogger("forge.memory")
    data = None
    for att in range(attempts):
        out = vm.run_command(
            f"cd ~ && tar czf /tmp/mem_out.tgz .memory 2>/dev/null; echo TAR_RC=$?")
        if "TAR_RC=0" not in out:
            _log.warning("pull attempt %d: guest tar failed (out=%r)",
                         att + 1, out[:120])
            _time.sleep(15)
            continue
        data = vm.fetch_file("/tmp/mem_out.tgz")
        if isinstance(data, tuple):        # fetch_file returns (bytes|None, note)
            data = data[0]
        if data:
            break
        _log.warning("pull attempt %d: fetch returned empty", att + 1)
        _time.sleep(15)
    if not data:
        _log.warning("pull FAILED after %d attempts", attempts)
        return {}
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = m.name
            if rel.startswith(".memory/"):
                rel = rel[len(".memory/"):]
            # v1.1 (EXP-2026-007 bug #12b): ep009's armed import deposited
            # __pycache__/*.pyc into ~/.memory — bytecode is a build
            # artifact, not experience; ingesting it polluted mem_delta
            # (+7213 = -571 true page delta + 7784B pyc). Filter it.
            if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            f = tf.extractfile(m)
            if f is not None:
                files[rel] = f.read()
    return files


def write_memory(memory_dir: str, files: dict) -> None:
    """Replace the host memory with ``files`` ({relpath: bytes})."""
    os.makedirs(memory_dir, exist_ok=True)
    for name in os.listdir(memory_dir):
        p = os.path.join(memory_dir, name)
        if os.path.isfile(p):
            os.remove(p)
        else:
            import shutil
            shutil.rmtree(p)
    for rel, data in files.items():
        p = os.path.join(memory_dir, rel)
        os.makedirs(os.path.dirname(p) or memory_dir, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


# ------------------------------------------------------------------ journal --
def journal(journal_dir: str, episode: int, files: dict, meta: dict) -> None:
    """Append-only per-episode snapshot (tgz) + one meta line. Invisible to the
    agent; pure rollback/audit insurance."""
    os.makedirs(journal_dir, exist_ok=True)
    p = os.path.join(journal_dir, f"ep{episode:03d}.tgz")
    with tarfile.open(p, "w:gz") as tf:
        for rel, data in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    with open(os.path.join(journal_dir, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"episode": episode, "files": len(files),
                            "bytes": sum(len(d) for d in files.values()),
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **meta}) + "\n")


# -------------------------------------------------------------- silent audit --
_WORD = re.compile(r"[a-z0-9]+")
_N = 8                                     # shingle length (words)
CORPUS_NORMALIZATION = "strip-dynamic-overleaf-credentials-v1"
_DYNAMIC_OVERLEAF_CREDENTIALS = re.compile(
    r"(?:\r?\n)*Overleaf login credentials\s*[-—]\s*"
    r"Email:[^\r\n]*?\s+Password:[^\r\n]*(?:\r?\n)*",
    re.IGNORECASE,
)


def _shingles(text: str) -> set:
    words = _WORD.findall(text.lower())
    return {" ".join(words[i:i + _N]) for i in range(len(words) - _N + 1)}


def normalize_instruction_for_corpus(instruction: str) -> str:
    """Remove run-generated Overleaf credentials from benchmark text.

    OSWorld-V2 tasks 057 and 072 interpolate fresh credentials when their task
    modules are imported.  Those secrets are not stable benchmark semantics;
    retaining them makes an otherwise identical host-only leakage corpus hash
    differently on every build.  Leave all instructions without that exact
    credential label byte-for-byte unchanged.
    """
    if not _DYNAMIC_OVERLEAF_CREDENTIALS.search(instruction):
        return instruction
    return _DYNAMIC_OVERLEAF_CREDENTIALS.sub("\n\n", instruction).rstrip()


def build_corpus(corpus_path: str, instructions: list) -> None:
    """Host-side ONLY (grader-diagnostic class): 8-gram shingles of all task
    instructions. Never enters any agent context."""
    sh = set()
    for ins in instructions:
        sh |= _shingles(ins)
    with open(corpus_path, "w") as f:
        json.dump(sorted(sh), f)


def validate_instruction_corpus(corpus_path: str,
                                expected_instruction_count: int = 108) -> dict:
    """Validate the host-only OSWorld-V2 instruction-corpus provenance."""
    manifest_path = corpus_path + ".MANIFEST.json"
    try:
        raw = open(corpus_path, "rb").read()
        corpus = json.loads(raw)
        record = json.load(open(manifest_path, encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "instruction corpus or its manifest is missing/unreadable") from exc
    required = {"schema_version", "benchmark", "evaluation_version",
                "normalization",
                "instruction_count", "absent_task_ids", "shingle_count",
                "corpus_sha256"}
    if (not isinstance(corpus, list) or not corpus
            or any(not isinstance(v, str) or not v for v in corpus)
            or corpus != sorted(set(corpus))
            or set(record) != required
            or record.get("schema_version") != 2
            or record.get("benchmark") != "OSWorld-V2"
            or record.get("evaluation_version") != "v2"
            or record.get("normalization") != CORPUS_NORMALIZATION
            or record.get("instruction_count") != expected_instruction_count
            or not isinstance(record.get("absent_task_ids"), list)
            or record.get("shingle_count") != len(corpus)
            or record.get("corpus_sha256") != hashlib.sha256(raw).hexdigest()):
        raise RuntimeError("instruction corpus provenance validation failed")
    return record


def silent_audit(files: dict, corpus_path: str, reject_log: str,
                 authorized_instruction: str = "",
                 require_corpus: bool = False) -> dict:
    """Return the accepted subset of ``files``. A file is rejected if it shares
    any 8-gram shingle with a task instruction (task-leakage tripwire — should
    ~never fire). Rejections are logged host-side ONLY: no agent feedback."""
    if not os.path.exists(corpus_path):
        if require_corpus:
            raise RuntimeError(
                "target-aware memory audit requires the instruction corpus")
        return files
    corpus = set(json.load(open(corpus_path)))
    if require_corpus and not corpus:
        raise RuntimeError(
            "target-aware memory audit instruction corpus is empty")
    authorized = _shingles(authorized_instruction) \
        if authorized_instruction else set()
    authorized_words = _WORD.findall(authorized_instruction.lower()) \
        if authorized_instruction else []
    # Keep transcript and durable-memory target exceptions identical. Import
    # locally so the generic memory transport remains usable without the E15
    # practice fence at module-import time.
    from tools.exam_fence import _authorized_near_shingle
    accepted = {}
    for rel, data in files.items():
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:                    # noqa: BLE001
            accepted[rel] = data
            continue
        hits = {
            gram for gram in (_shingles(text) & corpus) - authorized
            if not _authorized_near_shingle(gram, authorized_words)
        }
        if hits:
            with open(reject_log, "a") as f:
                f.write(json.dumps({"file": rel, "hits": sorted(hits)[:3],
                                    "ts": time.strftime("%H:%M:%S")}) + "\n")
            log.warning("silent audit rejected %s (%d shingle hits)", rel, len(hits))
            continue
        accepted[rel] = data
    return accepted
