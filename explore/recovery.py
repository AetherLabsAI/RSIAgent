"""Fail-closed continuation of an interrupted complete-feedback episode.

The normal episode path deliberately refuses to promote memory when either the
Verifier Agent has not produced a complete report or the continuing Actor Agent
has not declared its memory ready.  Those are valid *phase* failures, not a
reason to replay already-graded Actor work.  This module resumes only that
unfinished suffix from host artifacts written by :func:`e7_episode`:

* the byte-verified, frozen graded project trees;
* the Actor's provisional memory and exact conversation;
* the exact unfinished Verifier or memory-research conversation; and
* the mechanical grade already recorded in the provisional verdict.

It never synthesizes feedback and never re-runs the Actor's task.  Any ambiguous
or already-advanced lineage state is rejected before a model call or durable
promotion.
"""
from __future__ import annotations

import copy
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
from pathlib import Path

from core.actor import (Done, build_system, continuation_message,
                        opening_message, parse_turn)
from core.loop import run_attempt
from core.trace import ArtifactSink
from explore import commit as mem
from explore import reward
from explore.charter import agentic_verifier_charter, post_verdict_memory_msg
from explore import e7_loop


_INCOMPLETE_REVIEW = (
    "\nreviewer: (complete report unavailable; provisional memory was not "
    "promoted)"
)
_ACTOR_INSTANCE_PREFIX = "\nTonight's task:\n---\n"
_ACTOR_INSTANCE_SUFFIX = "\n---\n\nWork as always — one action per turn"
_SAFE_STATUSES = {
    "feedback_incomplete", "feedback_recovered", "memory_not_ready",
    "memory_recovered",
}
_TXN_VERSION = 1
_TXN_STATES = {
    "COMMIT_PREPARED", "RULING_RUNNING", "RULING_READY",
    "RULING_APPLYING", "RULING_APPLIED", "DONE",
}
_LOOP_STATUSES = {
    "done", "budget", "safety_ceiling", "stalled",
    "stalled_quiescent", "infra",
}
_MISSING = "missing"


class RecoveryError(RuntimeError):
    """The persisted episode is not an unambiguous recoverable checkpoint."""


def _failure_point(_name: str) -> None:
    """Test-only crash hook; production intentionally does nothing."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, candidate = tempfile.mkstemp(prefix=f".{path.name}.",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.lexists(candidate):
            os.unlink(candidate)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_write(path, (json.dumps(payload, sort_keys=True, indent=1)
                         + "\n").encode("utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(text: str) -> str:
    return _sha256(text.encode("utf-8"))


def _tree_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(4, "big"))
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise RecoveryError(
                f"transaction target contains a symlink: {candidate}")
        rel = str(candidate.relative_to(path)).encode("utf-8")
        kind = b"D" if candidate.is_dir() else b"F"
        digest.update(kind)
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(stat.S_IMODE(candidate.stat().st_mode).to_bytes(4, "big"))
        if candidate.is_file():
            data = candidate.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        elif not candidate.is_dir():
            raise RecoveryError(
                f"transaction target has unsupported entry: {candidate}")
    return digest.hexdigest()


def _path_sha256(path: Path) -> str:
    if not os.path.lexists(path):
        return _MISSING
    if path.is_symlink():
        raise RecoveryError(f"transaction target is a symlink: {path}")
    if path.is_file():
        digest = hashlib.sha256()
        digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(4, "big"))
        digest.update(path.read_bytes())
        return "file:" + digest.hexdigest()
    if not path.is_dir():
        raise RecoveryError(f"transaction target has unsupported type: {path}")
    return "dir:" + _directory_sha256(path)


def _validate_or_set(meta: dict, key: str, value) -> None:
    if key in meta and meta[key] != value:
        raise RecoveryError(f"persisted recovery binding drifted: {key}")
    meta[key] = value


def _read_jsonl(path: Path, *, allow_torn_tail: bool = False) \
        -> tuple[list[dict], bytes]:
    if path.is_symlink():
        raise RecoveryError(f"recovery ledger is a symlink: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"cannot read recovery ledger {path}: {exc}") \
            from exc
    rows = []
    valid_end = 0
    for line in raw.splitlines(keepends=True):
        complete = line.endswith((b"\n", b"\r"))
        is_last = valid_end + len(line) == len(raw)
        if allow_torn_tail and is_last and not complete:
            # A write can be killed after the JSON bytes but before its row
            # delimiter.  The lack of a newline is still an uncommitted tail;
            # never concatenate a later row directly onto it.
            return rows, raw[:valid_end]
        try:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("row is not an object")
                rows.append(value)
            valid_end += len(line)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            if allow_torn_tail and is_last and not complete:
                return rows, raw[:valid_end]
            raise RecoveryError(
                f"cannot read recovery ledger {path}: {exc}") from exc
    return rows, raw[:valid_end]


def _record_episode(root: Path, meta: dict, *, allow_torn_tail: bool = False) \
        -> None:
    path = root / "episodes.jsonl"
    if os.path.lexists(path):
        rows, prefix = _read_jsonl(path, allow_torn_tail=allow_torn_tail)
    else:
        rows, prefix = [], b""
    encoded = (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8")
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\n"
    if rows and rows[-1] == meta:
        if prefix != path.read_bytes():
            _atomic_write(path, prefix)
        return
    _atomic_write(path, prefix + encoded)


@contextlib.contextmanager
def lineage_operation_lock(root: Path, episode: int):
    """Exclude every mutating episode operation for one lineage.

    Recovery transactions replace lineage-wide ledgers and directories, so an
    episode-specific lock is not sufficient: episode N recovery must also
    exclude episode N+1 and the ordinary episode path through its terminal
    ``episodes.jsonl`` record.
    """
    recovery_dir = root / ".recovery"
    if os.path.lexists(recovery_dir) and (
            recovery_dir.is_symlink() or not recovery_dir.is_dir()):
        raise RecoveryError("lineage recovery state is not a real directory")
    recovery_dir.mkdir(parents=True, exist_ok=True)
    lock_path = recovery_dir / "lineage.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise RecoveryError(f"cannot open recovery lock: {exc}") from exc
    try:
        lock_stat = os.fstat(descriptor)
        if (not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_nlink != 1):
            raise RecoveryError("lineage recovery lock file is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RecoveryError(
                    f"another episode operation is already active for this "
                    f"lineage (requested episode {episode})") from exc
            raise
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


# Private compatibility names retained for focused tests and the pushed
# recovery entry point.  Production callers should use the public wrappers.
_recovery_lock = lineage_operation_lock


def record_episode(root: str | Path, meta: dict, *,
                   allow_torn_tail: bool = False) -> None:
    """Atomically append one lifecycle row while the caller holds the lock."""
    _record_episode(Path(root), meta, allow_torn_tail=allow_torn_tail)


def record_preflight(root: str | Path, meta: dict) -> None:
    """Atomically append a non-graded Curriculum preflight ledger row.

    The caller must hold ``lineage_operation_lock``.  Keeping this separate
    from ``episodes.jsonl`` prevents a pre-Night-1 ruling from masquerading as
    a graded or recoverable episode lifecycle.
    """
    if meta.get("record_type") != "curriculum_preflight" or \
            meta.get("graded") is not False:
        raise RecoveryError("refusing invalid Curriculum preflight record")
    path = Path(root) / "preflights.jsonl"
    if os.path.lexists(path):
        rows, prefix = _read_jsonl(path, allow_torn_tail=True)
    else:
        rows, prefix = [], b""
    encoded = (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8")
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\n"
    if rows and rows[-1] == meta:
        if prefix != path.read_bytes():
            _atomic_write(path, prefix)
        return
    _atomic_write(path, prefix + encoded)


def assert_lineage_recovery_clear(root: str | Path) -> None:
    """Refuse an ordinary episode while a recovery checkpoint is live."""
    lineage = Path(root)
    recovery_dir = lineage / ".recovery"
    if os.path.lexists(recovery_dir):
        if recovery_dir.is_symlink() or not recovery_dir.is_dir():
            raise RecoveryError("lineage recovery state is unsafe")
        for candidate in sorted(recovery_dir.iterdir()):
            match = re.fullmatch(r"ep(\d+)", candidate.name)
            if not match:
                continue
            txn = _load_transaction(lineage, int(match.group(1)))
            if txn is not None and txn["state"] != "DONE":
                raise RecoveryError(
                    "unfinished recovery transaction must be resumed before "
                    "starting an ordinary episode")
    ledger = lineage / "episodes.jsonl"
    if not os.path.lexists(ledger):
        return
    rows, _ = _read_jsonl(ledger, allow_torn_tail=True)
    if not rows:
        return
    latest = rows[-1]
    if (latest.get("status") in _SAFE_STATUSES
            or latest.get("status") == "recovery_promoting"):
        raise RecoveryError(
            "unfinished episode recovery must be resumed before starting "
            "an ordinary episode")


def _assert_no_other_recovery_transaction(root: Path, episode: int) -> None:
    recovery_dir = root / ".recovery"
    if not recovery_dir.is_dir() or recovery_dir.is_symlink():
        return
    for candidate in sorted(recovery_dir.iterdir()):
        match = re.fullmatch(r"ep(\d+)", candidate.name)
        if not match:
            continue
        other_episode = int(match.group(1))
        txn = _load_transaction(root, other_episode)
        if (txn is not None and txn["state"] != "DONE"
                and other_episode != episode):
            raise RecoveryError(
                "another episode has an unfinished lineage transaction")


def _json_lines(path: Path) -> list[dict]:
    return _read_jsonl(path)[0]


def _read_transcript(path: Path) -> tuple[str, list[dict]]:
    if path.is_symlink():
        raise RecoveryError(f"transcript path is a symlink: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError(f"cannot read transcript {path}: {exc}") from exc
    system = payload.get("system")
    messages = payload.get("messages")
    if not isinstance(system, str) or not isinstance(messages, list) \
            or not messages or len(messages) % 2:
        raise RecoveryError(f"transcript is incomplete: {path}")
    for i, message in enumerate(messages):
        expected = "user" if i % 2 == 0 else "assistant"
        if (not isinstance(message, dict)
                or message.get("role") != expected
                or not isinstance(message.get("content"), str)):
            raise RecoveryError(
                f"transcript role/content sequence is invalid: {path}")
    return system, [dict(message) for message in messages]


def _actor_instance(actor_history: list[dict]) -> str:
    opening = actor_history[0]["content"]
    start = opening.find(_ACTOR_INSTANCE_PREFIX)
    if start < 0:
        raise RecoveryError("Actor transcript lacks the exact task boundary")
    start += len(_ACTOR_INSTANCE_PREFIX)
    end = opening.find(_ACTOR_INSTANCE_SUFFIX, start)
    if end < 0 or opening.find(_ACTOR_INSTANCE_PREFIX, start) >= 0:
        raise RecoveryError("Actor transcript has an ambiguous task boundary")
    instance = opening[start:end].strip()
    if not instance:
        raise RecoveryError("Actor transcript contains an empty task")
    return instance


def _verifier_recovery_notice() -> str:
    report_guest = "/home/user/verifier_report.md"
    return f"""The harness interrupted this review before a complete
feedback file was available. It has now restored the same frozen graded project
bytes on a fresh null-task machine. Your prior conversation is preserved. Continue
your independent investigation as deeply as you judge useful. When you judge your
feedback ready, write its complete contents to {report_guest} and declare done; its
content and organization remain entirely yours."""


def _grade_outcome(block: str, what: str) -> str:
    results = re.findall(r"^\[\d+\]\s+(PASS|FAIL)\b", block, re.M)
    if not results:
        raise RecoveryError(f"{what} mechanical grade has no result rows")
    return "fail" if "FAIL" in results else "pass"


def _validate_grade_binding(gblock: str, meta: dict) -> None:
    delimiter = "\n--- PROJECT FINAL ---\n"
    if meta.get("kind") == "final":
        parts = gblock.split(delimiter)
        if len(parts) != 2:
            raise RecoveryError("final mechanical grade boundary is invalid")
        milestone, final = parts
        if (_grade_outcome(milestone, "milestone") != meta.get("outcome")
                or _grade_outcome(final, "final") !=
                meta.get("final_outcome")):
            raise RecoveryError(
                "mechanical grade outcomes differ from episode metadata")
    else:
        if delimiter in gblock or _grade_outcome(gblock, "milestone") != \
                meta.get("outcome"):
            raise RecoveryError(
                "mechanical grade outcome differs from episode metadata")


def _tree(path: Path) -> dict[str, bytes]:
    if not path.is_dir():
        raise RecoveryError(f"required memory directory is missing: {path}")
    files: dict[str, bytes] = {}
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise RecoveryError(f"memory tree contains a symlink: {candidate}")
        if candidate.is_file():
            files[str(candidate.relative_to(path))] = candidate.read_bytes()
    return files


def _atomic_memory_tree(path: Path, files: dict[str, bytes]) -> None:
    """Install a captured memory tree as one fsync'd directory rename."""
    if os.path.lexists(path):
        raise RecoveryError(f"declared memory output already exists: {path}")
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{path.name}.stage-", dir=str(path.parent)))
    try:
        mem.write_memory(str(temporary), files)
        _fsync_tree(temporary)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _tar_tree(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is not None:
                    files[member.name.lstrip("./")] = source.read()
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError(f"cannot read prior memory journal {path}: {exc}") \
            from exc
    return files


def _latest_prior_memory(root: Path, episode: int) -> dict[str, bytes]:
    journal_path = root / "journal" / "journal.jsonl"
    if not journal_path.exists():
        return {}
    rows = [row for row in _json_lines(journal_path)
            if isinstance(row.get("episode"), int)
            and row["episode"] < episode]
    if not rows:
        return {}
    prior = max(rows, key=lambda row: row["episode"])["episode"]
    return _tar_tree(root / "journal" / f"ep{prior:03d}.tgz")


def _next_sink(eproot: Path, stem: str) -> Path:
    existing = []
    pattern = re.compile(re.escape(stem) + r"_(\d+)$")
    for candidate in eproot.iterdir():
        match = pattern.fullmatch(candidate.name)
        if match:
            existing.append(int(match.group(1)))
    return eproot / f"{stem}_{max(existing, default=0) + 1:02d}"


def _latest_transcript(eproot: Path, original: str, recovery_stem: str) \
        -> Path:
    """Return the original only; recovery sinks require ledger provenance.

    Kept as a compatibility helper for callers/tests.  Directory discovery is
    deliberately not recovery authority: an interrupted or operator-created
    sink is unrecorded and therefore ignored.
    """
    return eproot / original / "transcript.json"


def _recorded_sink(root: Path, eproot: Path, meta: dict, field: str,
                   stem: str) -> Path | None:
    raw = meta.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise RecoveryError(f"recorded recovery sink is invalid: {field}")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise RecoveryError(f"recorded recovery sink escapes lineage: {field}")
    candidate = root / rel
    expected_parent = eproot.resolve()
    if candidate.parent.resolve() != expected_parent:
        raise RecoveryError(f"recorded recovery sink has wrong episode: {field}")
    match = re.fullmatch(re.escape(stem) + r"_(\d+)", candidate.name)
    if not match or int(match.group(1)) < 1:
        raise RecoveryError(f"recorded recovery sink has invalid name: {field}")
    if candidate.is_symlink() or not candidate.is_dir():
        raise RecoveryError(f"recorded recovery sink is unavailable: {field}")
    return candidate


def _recorded_episode_file(root: Path, eproot: Path, meta: dict,
                           field: str) -> Path:
    raw = meta.get(field)
    if not isinstance(raw, str) or not raw:
        raise RecoveryError(f"recorded recovery path is invalid: {field}")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise RecoveryError(f"recorded recovery path escapes lineage: {field}")
    candidate = root / rel
    try:
        candidate.resolve().relative_to(eproot.resolve())
    except ValueError as exc:
        raise RecoveryError(
            f"recorded recovery path has wrong episode: {field}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise RecoveryError(f"recorded recovery file is unavailable: {field}")
    return candidate


def _recorded_episode_dir(root: Path, eproot: Path, meta: dict,
                          field: str) -> Path:
    raw = meta.get(field)
    if not isinstance(raw, str) or not raw:
        raise RecoveryError(f"recorded recovery path is invalid: {field}")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise RecoveryError(f"recorded recovery path escapes lineage: {field}")
    candidate = root / rel
    try:
        candidate.resolve().relative_to(eproot.resolve())
    except ValueError as exc:
        raise RecoveryError(
            f"recorded recovery path has wrong episode: {field}") from exc
    if candidate.is_symlink() or not candidate.is_dir():
        raise RecoveryError(
            f"recorded recovery directory is unavailable: {field}")
    return candidate


def _phase_result_payload(result) -> dict:
    status = getattr(result, "status", None)
    if status not in _LOOP_STATUSES:
        raise RecoveryError(f"unexpected recovery Agent status: {status!r}")
    return {
        "status": status,
        "iters": int(getattr(result, "iters", 0)),
        "turns": int(getattr(result, "turns", 0)),
        "wall_secs": round(float(getattr(result, "wall_secs", 0))),
    }


def _read_phase_result(sink: Path, transcript: Path) -> dict:
    path = sink / "recovery_result.json"
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(
            "declared recovery Agent transcript has no durable result; "
            "refusing an ambiguous replay")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError(f"invalid recovery Agent result: {exc}") from exc
    expected = {"status", "iters", "turns", "wall_secs",
                "transcript_sha256", "output_kind", "output_sha256"}
    if (not isinstance(result, dict) or set(result) != expected
            or result.get("status") not in _LOOP_STATUSES
            or not all(isinstance(result.get(key), int)
                       for key in ("iters", "turns", "wall_secs"))
            or result.get("output_kind") not in {"report", "memory"}
            or not isinstance(result.get("output_sha256"), str)
            or result.get("transcript_sha256") !=
            _sha256(transcript.read_bytes())):
        raise RecoveryError("recovery Agent result binding is invalid")
    return result


def _persist_phase_result(sink: Path, result, transcript: Path, *,
                          output_kind: str, output_sha256: str) -> dict:
    if output_kind not in {"report", "memory"}:
        raise RecoveryError("invalid recovery Agent output receipt")
    payload = _phase_result_payload(result)
    payload["transcript_sha256"] = _sha256(transcript.read_bytes())
    payload["output_kind"] = output_kind
    payload["output_sha256"] = output_sha256
    _atomic_json(sink / "recovery_result.json", payload)
    return payload


def _declare_sink(root: Path, eproot: Path, meta: dict, *, stem: str,
                  sink_field: str, state_field: str,
                  input_bindings: dict, parent_transcript: Path,
                  input_memory_dir: Path | None = None,
                  clear_fields: tuple[str, ...] = ()) -> Path:
    sink = _next_sink(eproot, stem)
    sink.mkdir(parents=False, exist_ok=False)
    _fsync_dir(eproot)
    declaration = {
        "version": 1,
        "sink": str(sink.relative_to(root)),
        "parent_transcript": str(parent_transcript.relative_to(root)),
        "parent_transcript_sha256": _sha256(parent_transcript.read_bytes()),
        "bindings": input_bindings,
    }
    if input_memory_dir is not None:
        declaration["input_memory_dir"] = str(
            input_memory_dir.relative_to(root))
        declaration["input_memory_sha256"] = _tree_sha256(
            _tree(input_memory_dir))
    _atomic_json(sink / "recovery_input.json", declaration)
    for field in clear_fields:
        meta.pop(field, None)
    meta[sink_field] = declaration["sink"]
    meta[state_field] = "RUNNING"
    prefix = sink_field.removesuffix("_sink")
    meta[prefix + "_input_manifest_sha256"] = _sha256(
        (sink / "recovery_input.json").read_bytes())
    meta[prefix + "_parent_transcript"] = declaration[
        "parent_transcript"]
    meta[prefix + "_parent_transcript_sha256"] = declaration[
        "parent_transcript_sha256"]
    if input_memory_dir is not None:
        meta[prefix + "_input_memory_dir"] = declaration[
            "input_memory_dir"]
        meta[prefix + "_input_memory_sha256"] = declaration[
            "input_memory_sha256"]
    for key, value in input_bindings.items():
        meta[key] = value
    _record_episode(root, meta)
    return sink


def _sink_has_agent_evidence(sink: Path) -> bool:
    return any(candidate.name != "recovery_input.json"
               for candidate in sink.iterdir())


def _validate_declared_sink(root: Path, eproot: Path, meta: dict,
                            sink: Path, prefix: str, role: str) -> None:
    manifest = sink / "recovery_input.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise RecoveryError("declared recovery sink lacks its input manifest")
    _validate_or_set(
        meta, prefix + "_input_manifest_sha256",
        _sha256(manifest.read_bytes()))
    parent = _recorded_episode_file(
        root, eproot, meta, prefix + "_parent_transcript")
    _validate_or_set(
        meta, prefix + "_parent_transcript_sha256",
        _sha256(parent.read_bytes()))
    rel = parent.relative_to(eproot)
    allowed = {"verify"} if role == "verifier" else {
        "session", "memory_reflection"}
    if len(rel.parts) != 2 or rel.name != "transcript.json":
        raise RecoveryError("declared recovery parent transcript is invalid")
    parent_name = rel.parts[0]
    if role == "verifier":
        valid = (parent_name in allowed or bool(re.fullmatch(
            r"verify_recovery_\d+", parent_name)))
    else:
        valid = (parent_name in allowed or bool(re.fullmatch(
            r"memory_reflection_recovery_\d+", parent_name)))
    if not valid:
        raise RecoveryError("declared recovery parent transcript is invalid")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError(f"invalid recovery input manifest: {exc}") from exc
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or payload.get("sink") != str(sink.relative_to(root))
            or payload.get("parent_transcript") !=
            str(parent.relative_to(root))
            or payload.get("parent_transcript_sha256") !=
            _sha256(parent.read_bytes())
            or not isinstance(payload.get("bindings"), dict)):
        raise RecoveryError("recovery input manifest binding is invalid")
    for key, value in payload["bindings"].items():
        if meta.get(key) != value:
            raise RecoveryError("recovery input manifest binding drifted")
    if role == "reflection":
        input_dir = _recorded_episode_dir(
            root, eproot, meta, prefix + "_input_memory_dir")
        input_rel = input_dir.relative_to(eproot)
        if parent_name == "session":
            expected_input = Path("_mem_mid")
        elif parent_name == "memory_reflection":
            expected_input = Path("_mem_reflection_incomplete")
        else:
            expected_input = Path(parent_name) / "partial_memory"
        if input_rel != expected_input:
            raise RecoveryError(
                "recovery parent transcript and partial memory disagree")
        input_hash = _tree_sha256(_tree(input_dir))
        if (payload.get("input_memory_dir") !=
                str(input_dir.relative_to(root))
                or payload.get("input_memory_sha256") != input_hash
                or meta.get(prefix + "_input_memory_sha256") != input_hash):
            raise RecoveryError("recovery input memory binding drifted")
    elif "input_memory_dir" in payload or "input_memory_sha256" in payload:
        raise RecoveryError("Verifier recovery input manifest is invalid")


def _bound_transcript(root: Path, eproot: Path, meta: dict, *,
                      field: str, stem: str, original: str,
                      hash_field: str) -> tuple[Path, bool]:
    sink = _recorded_sink(root, eproot, meta, field, stem)
    if sink is None:
        return eproot / original / "transcript.json", False
    transcript = sink / "transcript.json"
    if transcript.is_symlink() or not transcript.is_file():
        raise RecoveryError(f"recorded recovery transcript is missing: {field}")
    actual = _sha256(transcript.read_bytes())
    expected = meta.get(hash_field)
    if expected is None:
        # Migration for the first recovery implementation: the exact sink was
        # recorded, but hashes were not. Semantic checks at the caller bind the
        # history before this computed hash is durably added to the next row.
        meta[hash_field] = actual
    elif not isinstance(expected, str) or expected != actual:
        raise RecoveryError(
            f"recorded recovery transcript hash drifted: {field}")
    return transcript, True


def _read_checkpoint(root: str, episode: int, cfg, vcfg,
                     reflection_cfg) -> dict:
    lineage = Path(root)
    eproot = lineage / "episodes" / f"ep{episode:03d}"
    if not eproot.is_dir():
        raise RecoveryError(f"episode directory is missing: {eproot}")

    episodes = _json_lines(lineage / "episodes.jsonl")
    if not episodes or episodes[-1].get("episode") != episode:
        raise RecoveryError(
            "only the lineage's latest recorded episode can be recovered")
    meta = copy.deepcopy(episodes[-1])
    if meta.get("status") not in _SAFE_STATUSES:
        raise RecoveryError(
            f"episode status is not recoverable: {meta.get('status')!r}")
    if any(row.get("episode") == episode
           for row in (_json_lines(lineage / "curve.jsonl")
                       if (lineage / "curve.jsonl").exists() else [])):
        raise RecoveryError("episode already has a promoted curve row")
    if (lineage / "journal" / f"ep{episode:03d}.tgz").exists():
        raise RecoveryError("episode already has a promoted memory journal")

    pj, ppath = e7_loop.live_project(root)
    if pj is None or ppath is None:
        raise RecoveryError("recovery currently requires one live project")
    if (meta.get("project") != pj.get("name")
            or meta.get("night") != pj.get("next_night")
            or meta.get("n_nights") != pj.get("n_nights")):
        raise RecoveryError("live project pointer drifted after the episode")
    # Early Curriculum-preflight pilots recorded their ruling in this legacy
    # list before preflight provenance moved exclusively to preflights.jsonl.
    # Such a pre-Night-1 audit is not the post-Actor ruling whose presence
    # makes suffix recovery ambiguous.
    if any(row.get("ep") == episode and row.get("preflight") is not True
           for row in pj.get("decisions", [])):
        raise RecoveryError("Curriculum already ruled on this episode")

    actor_path = eproot / "session" / "transcript.json"
    actor_system, actor_history = _read_transcript(actor_path)
    if actor_system != build_system(cfg):
        raise RecoveryError("Actor config/system drifted since the checkpoint")
    if not isinstance(parse_turn(actor_history[-1]["content"]), Done):
        raise RecoveryError(
            "Actor transcript does not end in its accepted done declaration")
    instance = _actor_instance(actor_history)
    expected_goal = f"PROJECT GOAL:\n{pj['target_prose']}"
    expected_night = f"TONIGHT (night {meta['night']} of {meta['n_nights']}"
    if expected_goal not in instance or expected_night not in instance:
        raise RecoveryError("Actor task does not match the recorded project night")

    durable = _tree(lineage / "memory")
    if durable != _latest_prior_memory(lineage, episode):
        raise RecoveryError(
            "durable memory drifted from the latest promoted journal")
    provisional_dir = eproot / "_mem_mid"
    provisional = _tree(provisional_dir)
    if not provisional and durable:
        raise RecoveryError("provisional Actor memory is unexpectedly empty")

    artifact = eproot / "graded_artifact"
    for name in ("snapshot.json", "materials.tgz",
                 "materials.MANIFEST.sha256", "materials.ROOTS.json"):
        if not (artifact / name).is_file():
            raise RecoveryError(f"frozen graded artifact is incomplete: {name}")

    verifier_sink = _recorded_sink(
        lineage, eproot, meta, "verifier_recovery_sink", "verify_recovery")
    verifier_state = meta.get("verifier_recovery_state")
    if verifier_state is not None and verifier_state not in {
            "RUNNING", "FINISHED"}:
        raise RecoveryError("recorded Verifier recovery state is invalid")
    if verifier_sink is not None and verifier_state == "RUNNING":
        _validate_declared_sink(
            lineage, eproot, meta, verifier_sink,
            "verifier_recovery", "verifier")
    verdict_path = (verifier_sink / "verdict.txt" if verifier_sink is not None
                    and meta["status"] != "feedback_incomplete" else
                    lineage / "verdicts" / f"ep{episode:03d}.txt")
    if verdict_path.is_symlink():
        raise RecoveryError("recorded verdict path is a symlink")
    try:
        stored_verdict = verdict_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryError(f"cannot read provisional verdict: {exc}") from exc
    report_path = eproot / "verifier_report.md"
    report = ""
    if meta["status"] == "feedback_incomplete":
        if not stored_verdict.endswith(_INCOMPLETE_REVIEW):
            raise RecoveryError("provisional verdict marker is missing or changed")
        gblock = stored_verdict[:-len(_INCOMPLETE_REVIEW)]
    else:
        report_path = (verifier_sink / "verifier_report.md"
                       if verifier_sink is not None else report_path)
        if report_path.is_symlink():
            raise RecoveryError("recorded Verifier report path is a symlink")
        try:
            report = report_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RecoveryError(
                f"completed Verifier report is unavailable: {exc}") from exc
        suffix = "\nreviewer: " + report
        if not report.strip() or not stored_verdict.endswith(suffix):
            raise RecoveryError(
                "stored verdict does not contain the completed report verbatim")
        gblock = stored_verdict[:-len(suffix)]
    if not gblock.strip():
        raise RecoveryError("mechanical grade block is empty")
    _validate_grade_binding(gblock, meta)

    bindings = {
        "recovery_actor_transcript_sha256": _sha256(actor_path.read_bytes()),
        "recovery_task_sha256": _text_sha256(instance),
        "recovery_grade_sha256": _text_sha256(gblock),
        "recovery_durable_memory_sha256": _tree_sha256(durable),
        "recovery_provisional_memory_sha256": _tree_sha256(provisional),
        "recovery_graded_artifact_sha256": _path_sha256(artifact),
    }
    for key, value in bindings.items():
        _validate_or_set(meta, key, value)
    if meta["status"] != "feedback_incomplete":
        _validate_or_set(
            meta, "verifier_report_sha256", _text_sha256(report))
        _validate_or_set(
            meta, "recovery_verdict_sha256", _text_sha256(stored_verdict))

    review_cfg = copy.deepcopy(vcfg)
    review_cfg.practice_mode = True
    review_cfg.independent_verify = False
    review_cfg.practice_done_requires = "/home/user/verifier_report.md"
    review_cfg.max_resumes = 0

    verifier_history = None
    verifier_protocol_transition = "none"
    original_path = eproot / "verify" / "transcript.json"
    original_system, original_history = _read_transcript(original_path)
    _validate_or_set(
        meta, "recovery_original_verifier_transcript_sha256",
        _sha256(original_path.read_bytes()))
    expected_verifier_task = agentic_verifier_charter(
        instance, gblock, "/home/user/verifier_report.md")
    if original_history[0]["content"] != opening_message(
            expected_verifier_task):
        raise RecoveryError(
            "Verifier transcript is not bound to the task and grade")
    if original_system != build_system(review_cfg):
        # The ep005 failure exposed one protocol bug: K3 narrated that it
        # would write the report, which the strict JSON parser treated as
        # three dry turns.  The repaired profile adds turn discipline only.
        # Permit precisely that one system transition; every other model,
        # sampling, context, or prompt drift remains fail-closed.
        previous_review_cfg = copy.deepcopy(review_cfg)
        previous_review_cfg.action_discipline = False
        if (not getattr(review_cfg, "action_discipline", False)
                or original_system != build_system(previous_review_cfg)):
            raise RecoveryError(
                "Verifier config/system drifted since the checkpoint")
        verifier_protocol_transition = "action_discipline_false_to_true"

    selected_verifier_path = original_path
    verifier_transcript_exists = False
    if verifier_sink is not None:
        current_transcript = verifier_sink / "transcript.json"
        if current_transcript.is_file() and not current_transcript.is_symlink():
            selected_path, recovered = _bound_transcript(
                lineage, eproot, meta, field="verifier_recovery_sink",
                stem="verify_recovery", original="verify",
                hash_field="verifier_recovery_transcript_sha256")
            assert recovered
            verifier_transcript_exists = True
        elif verifier_state == "RUNNING" and not \
                _sink_has_agent_evidence(verifier_sink):
            selected_path = _recorded_episode_file(
                lineage, eproot, meta,
                "verifier_recovery_parent_transcript")
            _validate_or_set(
                meta, "verifier_recovery_parent_transcript_sha256",
                _sha256(selected_path.read_bytes()))
        elif verifier_state == "RUNNING":
            raise RecoveryError(
                "declared Verifier recovery has partial artifacts but no "
                "complete transcript; refusing an ambiguous replay")
        else:
            raise RecoveryError(
                "recorded Verifier recovery transcript is missing")
        selected_verifier_path = selected_path
        if selected_path == original_path:
            selected_system, selected_history = original_system, original_history
        else:
            selected_system, selected_history = _read_transcript(selected_path)
            if selected_system != build_system(review_cfg):
                raise RecoveryError("Verifier recovery config/system drifted")
            if selected_history[:len(original_history)] != original_history:
                raise RecoveryError(
                    "Verifier recovery transcript changed its original history")
            boundary = len(original_history)
            if (len(selected_history) <= boundary
                    or selected_history[boundary]["content"] !=
                    continuation_message(_verifier_recovery_notice())):
                raise RecoveryError(
                    "Verifier recovery transcript is not bound to its recovery "
                    "phase")
        for field, expected in (
                ("verifier_recovery_task_sha256",
                 bindings["recovery_task_sha256"]),
                ("verifier_recovery_grade_sha256",
                 bindings["recovery_grade_sha256"]),
                ("verifier_recovery_memory_sha256",
                 bindings["recovery_provisional_memory_sha256"])):
            _validate_or_set(meta, field, expected)
    else:
        selected_history = original_history
    if meta["status"] == "feedback_incomplete":
        verifier_history = selected_history

    reflection_history = actor_history
    reflection_memory_dir = provisional_dir
    bound_transcript_dirs = [actor_path.parent]
    if selected_verifier_path.parent not in bound_transcript_dirs:
        bound_transcript_dirs.append(selected_verifier_path.parent)
    reflection_sink = _recorded_sink(
        lineage, eproot, meta, "reflection_recovery_sink",
        "memory_reflection_recovery")
    reflection_state = meta.get("reflection_recovery_state")
    if reflection_state is not None and reflection_state not in {
            "RUNNING", "FINISHED"}:
        raise RecoveryError("recorded memory-recovery state is invalid")
    if reflection_sink is not None and reflection_state == "RUNNING":
        _validate_declared_sink(
            lineage, eproot, meta, reflection_sink,
            "reflection_recovery", "reflection")
    reflection_transcript_path = actor_path
    reflection_transcript_exists = False
    if (meta["status"] in {"memory_not_ready", "memory_recovered"}
            or reflection_sink is not None):
        if reflection_sink is not None:
            current_transcript = reflection_sink / "transcript.json"
            if current_transcript.is_file() and not \
                    current_transcript.is_symlink():
                selected_path, recovered = _bound_transcript(
                    lineage, eproot, meta, field="reflection_recovery_sink",
                    stem="memory_reflection_recovery",
                    original="memory_reflection",
                    hash_field="reflection_recovery_transcript_sha256")
                reflection_transcript_exists = True
            elif reflection_state == "RUNNING" and not \
                    _sink_has_agent_evidence(reflection_sink):
                selected_path = _recorded_episode_file(
                    lineage, eproot, meta,
                    "reflection_recovery_parent_transcript")
                _validate_or_set(
                    meta, "reflection_recovery_parent_transcript_sha256",
                    _sha256(selected_path.read_bytes()))
                recovered = selected_path.parent != actor_path.parent
            elif reflection_state == "RUNNING":
                raise RecoveryError(
                    "declared memory recovery has partial artifacts but no "
                    "complete transcript; refusing an ambiguous replay")
            else:
                raise RecoveryError(
                    "recorded memory-recovery transcript is missing")
        else:
            selected_path = eproot / "memory_reflection" / "transcript.json"
            recovered = False
        reflection_transcript_path = selected_path
        reflection_system, reflection_history = _read_transcript(selected_path)
        declared_actor_parent = (
            reflection_state == "RUNNING"
            and selected_path == actor_path
            and not reflection_transcript_exists)
        if (not declared_actor_parent
                and reflection_system != build_system(reflection_cfg)):
            raise RecoveryError(
                "memory-research config/system drifted since the checkpoint")
        if reflection_history[:len(actor_history)] != actor_history:
            raise RecoveryError(
                "memory-research transcript changed the Actor history")
        snapshot = json.loads(
            (artifact / "snapshot.json").read_text(encoding="utf-8"))
        expected_reflection = post_verdict_memory_msg(
            stored_verdict, open_memory_research=True,
            disposable_roots=tuple(
                f"/home/user/{name}" for name in snapshot.get("roots", [])))
        boundary = len(actor_history)
        if (not declared_actor_parent
                and (len(reflection_history) <= boundary
                     or reflection_history[boundary]["content"] !=
                     continuation_message(expected_reflection))):
            raise RecoveryError(
                "memory-research transcript is not bound to the verdict")
        if meta["status"] == "memory_recovered" and not recovered:
            raise RecoveryError("ready recovery memory lacks a recorded sink")
        if reflection_state == "RUNNING":
            reflection_memory_dir = _recorded_episode_dir(
                lineage, eproot, meta,
                "reflection_recovery_input_memory_dir")
            _validate_or_set(
                meta, "reflection_recovery_input_memory_sha256",
                _tree_sha256(_tree(reflection_memory_dir)))
        else:
            reflection_memory_dir = (
                selected_path.parent /
                ("ready_memory" if meta["status"] == "memory_recovered"
                 else "partial_memory")
                if recovered else eproot / "_mem_reflection_incomplete")
        partial = _tree(reflection_memory_dir)
        if not partial and provisional:
            raise RecoveryError("incomplete memory-research state is empty")
        if reflection_sink is not None:
            for field, expected in (
                    ("reflection_recovery_task_sha256",
                     bindings["recovery_task_sha256"]),
                    ("reflection_recovery_grade_sha256",
                     bindings["recovery_grade_sha256"]),
                    ("reflection_recovery_verdict_sha256",
                     _text_sha256(stored_verdict))):
                _validate_or_set(meta, field, expected)
        if recovered and reflection_state != "RUNNING":
            _validate_or_set(
                meta, "reflection_recovery_memory_sha256",
                _tree_sha256(partial))
        elif reflection_sink is None:
            _validate_or_set(
                meta, "recovery_original_reflection_transcript_sha256",
                _sha256(selected_path.read_bytes()))
            _validate_or_set(
                meta, "recovery_original_partial_memory_sha256",
                _tree_sha256(partial))
        bound_transcript_dirs.append(selected_path.parent)

    return {
        "root": lineage, "eproot": eproot, "meta": meta,
        "project": pj, "project_path": ppath, "instance": instance,
        "gblock": gblock, "report": report, "artifact": artifact,
        "durable": durable, "provisional": provisional,
        "provisional_dir": provisional_dir, "review_cfg": review_cfg,
        "verifier_history": verifier_history,
        "verifier_transcript_path": selected_verifier_path,
        "verifier_recovery_sink": verifier_sink,
        "verifier_recovery_state": verifier_state,
        "verifier_transcript_exists": verifier_transcript_exists,
        "verifier_protocol_transition": verifier_protocol_transition,
        "actor_history": actor_history,
        "reflection_history": reflection_history,
        "reflection_transcript_path": reflection_transcript_path,
        "reflection_recovery_sink": reflection_sink,
        "reflection_recovery_state": reflection_state,
        "reflection_transcript_exists": reflection_transcript_exists,
        "reflection_memory_dir": reflection_memory_dir,
        "bound_transcript_dirs": bound_transcript_dirs,
        "bindings": bindings, "stored_verdict": stored_verdict,
    }


def _target_preflight(root: str, target_task: str, meta: dict) -> None:
    from explore.targeting import (TARGET_AWARE_MODE, TargetingError,
                                   read_lineage_metadata,
                                   validate_lineage_instruction)
    try:
        targeting = read_lineage_metadata(root)
    except TargetingError as exc:
        raise RecoveryError(f"invalid target registry: {exc}") from exc
    target_aware = bool(
        targeting and targeting.get("mode") == TARGET_AWARE_MODE)
    if target_aware != bool(target_task.strip()):
        raise RecoveryError("recovery target mode does not match the lineage")
    if not target_aware:
        return
    try:
        validate_lineage_instruction(root, target_task)
        mem.validate_instruction_corpus(e7_loop.CORPUS)
    except (TargetingError, RuntimeError) as exc:
        raise RecoveryError(f"target recovery preflight failed: {exc}") from exc
    encoded = target_task.encode("utf-8")
    if (meta.get("target_instruction_sha256")
            != hashlib.sha256(encoded).hexdigest()
            or meta.get("target_instruction_bytes") != len(encoded)):
        raise RecoveryError("target instruction differs from the episode record")


def _audit_target_text(text: str, target_task: str, what: str) -> None:
    if not target_task.strip():
        return
    from tools.exam_fence import audit_text
    hits = audit_text(
        text, mode="practice", authorized_instruction=target_task)
    if hits:
        kinds = sorted({hit.get("kind", "unknown") for hit in hits})
        raise RecoveryError(f"{what} failed the target boundary: {kinds}")


def _reset(vm, artifact: Path, target_task: str) -> dict:
    result = e7_loop._reset_to_canonical_artifact(
        vm, str(artifact), authorized_instruction=target_task)
    if not result.get("ok"):
        raise RecoveryError(f"fresh canonical reset failed: {result}")
    return result


def _txn_dir(root: Path, episode: int) -> Path:
    return root / ".recovery" / f"ep{episode:03d}"


def _txn_state_path(root: Path, episode: int) -> Path:
    return _txn_dir(root, episode) / "state.json"


def _load_transaction(root: Path, episode: int) -> dict | None:
    directory = _txn_dir(root, episode)
    state_path = directory / "state.json"
    if not os.path.lexists(directory):
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise RecoveryError("recovery transaction path is unsafe")
    if state_path.is_symlink() or not state_path.is_file():
        raise RecoveryError("recovery transaction directory lacks state.json")
    try:
        txn = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError(f"cannot read recovery transaction: {exc}") from exc
    if (not isinstance(txn, dict) or txn.get("version") != _TXN_VERSION
            or txn.get("episode") != episode
            or txn.get("state") not in _TXN_STATES):
        raise RecoveryError("recovery transaction header is invalid")
    expected_targets = _transaction_targets(episode)
    targets = txn.get("targets")
    if targets != expected_targets:
        raise RecoveryError("recovery transaction target schema is invalid")
    for rel in targets:
        path = Path(rel)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise RecoveryError("recovery transaction target escapes lineage")
    for field in ("preimages", "commit_postimages"):
        values = txn.get(field)
        if not isinstance(values, dict) or set(values) != set(targets) \
                or not all(isinstance(values[rel], str) for rel in targets):
            raise RecoveryError(
                f"recovery transaction {field} schema is invalid")
    postimages = txn.get("postimages")
    if txn["state"] not in {"COMMIT_PREPARED", "RULING_RUNNING"}:
        if (not isinstance(postimages, dict)
                or set(postimages) != set(targets)
                or not all(isinstance(postimages[rel], str)
                           for rel in targets)):
            raise RecoveryError(
                "recovery transaction postimage schema is invalid")
    applied = txn.get("applied")
    if (not isinstance(applied, list) or len(applied) != len(set(applied))
            or not set(applied).issubset(targets)):
        raise RecoveryError("recovery transaction applied set is invalid")
    if not isinstance(txn.get("meta"), dict):
        raise RecoveryError("recovery transaction metadata is invalid")
    project = txn["meta"].get("project")
    expected_project_rel = f"curriculum/project_{project}.json"
    project_rel = txn.get("project_rel")
    if (not isinstance(project, str)
            or not reward.valid_project_name(project)
            or project_rel != expected_project_rel):
        raise RecoveryError("recovery transaction project path is invalid")
    return txn


def _write_transaction(root: Path, episode: int, txn: dict) -> None:
    if txn.get("episode") != episode or txn.get("state") not in _TXN_STATES:
        raise RecoveryError("refusing to persist invalid recovery transaction")
    _atomic_json(_txn_state_path(root, episode), txn)


def _parameter_bindings(ccfg, research_direction: str,
                        target_task: str) -> dict:
    return {
        "curriculum_system_sha256": _text_sha256(build_system(ccfg)),
        "research_direction_sha256": _text_sha256(research_direction),
        "target_task_sha256": _text_sha256(target_task),
        "agent_decided_stop": True,
    }


def _transaction_targets(episode: int) -> list[str]:
    ep = f"episodes/ep{episode:03d}"
    return [
        "memory", "journal", "curriculum", "projects", "verdicts",
        "curve.jsonl", "observe_ledger.json", "audit_rejects.jsonl",
        f"{ep}/curriculum", f"{ep}/curriculum_retry",
    ]


def _fsync_tree(path: Path) -> None:
    if not path.exists():
        return
    for candidate in sorted(path.rglob("*"), reverse=True):
        if candidate.is_symlink():
            raise RecoveryError(f"transaction staging contains symlink: {candidate}")
        if candidate.is_file():
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
        elif candidate.is_dir():
            _fsync_dir(candidate)
    _fsync_dir(path)


def _clone_lineage(root: Path, destination: Path) -> None:
    shutil.copytree(
        root, destination,
        ignore=shutil.ignore_patterns(
            ".recovery", "_archive_stage", "_target_input"))


def _prepare_transaction(checkpoint: dict, after: dict[str, bytes],
                         verdict: str, ccfg,
                         curriculum_research_direction: str,
                         target_task: str,
                         agent_decided_stop: bool) -> dict:
    root: Path = checkpoint["root"]
    eproot: Path = checkpoint["eproot"]
    meta = copy.deepcopy(checkpoint["meta"])
    episode = int(meta["episode"])
    final_dir = _txn_dir(root, episode)
    if final_dir.exists():
        txn = _load_transaction(root, episode)
        assert txn is not None
        return txn

    recovery_dir = root / ".recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".ep{episode:03d}-prepare-", dir=str(recovery_dir)))
    try:
        targets = _transaction_targets(episode)
        preimages = {rel: _path_sha256(root / rel) for rel in targets}
        commit_root = temporary / "commit_lineage"
        _clone_lineage(root, commit_root)

        inv = reward.scan_invocations(str(eproot), known_files=set(after))
        meta.update(invoked_n=inv["distinct"],
                    exec_n=len(inv.get("exec_counts", {})),
                    read_n=len(inv.get("read_counts", {})))
        ledger_path = commit_root / "observe_ledger.json"
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) \
                if ledger_path.exists() else {}
        except (OSError, ValueError, TypeError) as exc:
            raise RecoveryError(f"cannot read observation ledger: {exc}") \
                from exc
        kept, _pruned, ledger2 = reward.observe_ledger(
            after, set(inv["counts"]), ledger, episode - 1)
        _atomic_json(ledger_path, ledger2)
        accepted = mem.silent_audit(
            kept, e7_loop.CORPUS,
            str(commit_root / "audit_rejects.jsonl"),
            authorized_instruction=target_task,
            require_corpus=bool(target_task.strip()))
        mem.journal(str(commit_root / "journal"), episode, accepted,
                    {"kind": meta["kind"],
                     "project": meta.get("project")})
        mem.write_memory(str(commit_root / "memory"), accepted)

        previous_bytes = sum(
            len(data) for data in checkpoint["durable"].values())
        current_bytes = sum(len(data) for data in accepted.values())
        meta["memory_promoted"] = True
        meta["recovery_transaction"] = f"ep{episode:03d}"
        meta["artifact"] = {
            "bytes": current_bytes, "files": len(accepted),
            "delta": current_bytes - previous_bytes,
        }

        project_rel = str(Path(checkpoint["project_path"]).relative_to(root))
        staged_project_path = commit_root / project_rel
        staged_project = json.loads(
            staged_project_path.read_text(encoding="utf-8"))
        if meta["kind"] == "final" and meta.get("final_outcome") == "pass":
            staged_project["status"] = "complete"
            e7_loop.save_project(str(staged_project_path), staged_project)

        _atomic_write(
            commit_root / "verdicts" / f"ep{episode:03d}.txt",
            verdict.encode("utf-8"))
        curve_row = {
            "episode": episode, "project": meta.get("project") or "-",
            "night": meta["night"], "n_nights": meta["n_nights"],
            "kind": meta["kind"],
            "outcome": (meta.get("final_outcome")
                        if meta["kind"] == "final" else meta.get("outcome")),
            "milestone_outcome": meta.get("outcome"),
            "iters": meta["iters"],
            "budget": ("agent-decided" if agent_decided_stop
                       else "recovered"),
            "invoked_n": inv["distinct"], "exec_n": meta["exec_n"],
            "mem_delta": meta["artifact"]["delta"], "stalled": False,
        }
        reward.append_curve(str(commit_root), curve_row)

        txn = {
            "version": _TXN_VERSION,
            "episode": episode,
            "state": "COMMIT_PREPARED",
            "phase_history": [
                "GRADED", "FEEDBACK_READY", "MEMORY_READY",
                "COMMIT_PREPARED",
            ],
            "created": time.strftime("%F %T"),
            "params": _parameter_bindings(
                ccfg, curriculum_research_direction, target_task),
            "bindings": {
                **checkpoint["bindings"],
                "verdict_sha256": _text_sha256(verdict),
                "ready_memory_sha256": _tree_sha256(after),
                "accepted_memory_sha256": _tree_sha256(accepted),
            },
            "meta": meta,
            "project_rel": project_rel,
            "targets": targets,
            "preimages": preimages,
            "commit_postimages": {
                rel: _path_sha256(commit_root / rel) for rel in targets},
            "commit_lineage_sha256": _path_sha256(commit_root),
            "applied": [],
        }
        _atomic_json(temporary / "state.json", txn)
        _fsync_tree(commit_root)
        _fsync_dir(temporary)
        os.replace(temporary, final_dir)
        _fsync_dir(recovery_dir)
        return txn
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _validate_transaction_params(txn: dict, ccfg, research_direction: str,
                                 target_task: str) -> None:
    if txn.get("params") != _parameter_bindings(
            ccfg, research_direction, target_task):
        raise RecoveryError("recovery transaction parameters drifted")


def _run_staged_curriculum(vm, root: Path, txn: dict, ccfg,
                           curriculum_research_direction: str,
                           target_task: str) -> dict:
    episode = int(txn["episode"])
    directory = _txn_dir(root, episode)
    commit_root = directory / "commit_lineage"
    ruling_root = directory / "ruling_lineage"
    if not commit_root.is_dir():
        raise RecoveryError("recovery commit staging is missing")
    if _path_sha256(commit_root) != txn.get("commit_lineage_sha256"):
        raise RecoveryError("recovery commit staging drifted")
    if txn["state"] == "COMMIT_PREPARED":
        if os.path.lexists(ruling_root):
            if ruling_root.is_symlink() or not ruling_root.is_dir():
                raise RecoveryError(
                    "undeclared staged Curriculum path is unsafe")
            # COMMIT_PREPARED durably proves no Agent call was declared.  A
            # leftover clone can therefore only be an interrupted staging
            # copy and is safe to rebuild under the lineage lock.
            shutil.rmtree(ruling_root)
        _failure_point("before_ruling_copy")
        shutil.copytree(commit_root, ruling_root)
        _failure_point("after_ruling_copy")
        _fsync_tree(ruling_root)
        _failure_point("after_ruling_copy_fsync")
        txn["state"] = "RULING_RUNNING"
        if not txn["phase_history"] or txn["phase_history"][-1] != \
                "RULING_RUNNING":
            txn["phase_history"].append("RULING_RUNNING")
        _write_transaction(root, episode, txn)
        _failure_point("after_ruling_declared")
    elif txn["state"] == "RULING_RUNNING":
        if not ruling_root.is_dir():
            raise RecoveryError("declared staged Curriculum root is missing")
        # Archive construction and an empty ArtifactSink happen before the
        # first model response.  They are mechanical pre-Agent staging and can
        # be discarded after a crash.  Any nonempty sink or other postimage is
        # evidence of a possibly completed Agent ruling and must be preserved.
        archive_stage = ruling_root / "_archive_stage"
        if os.path.lexists(archive_stage):
            if archive_stage.is_symlink() or not archive_stage.is_dir():
                raise RecoveryError(
                    "staged Curriculum archive path is unsafe")
            shutil.rmtree(archive_stage)
        staged_eproot = ruling_root / "episodes" / f"ep{episode:03d}"
        commit_eproot = commit_root / "episodes" / f"ep{episode:03d}"
        for name in ("curriculum", "curriculum_retry"):
            sink = staged_eproot / name
            original_sink = commit_eproot / name
            if not os.path.lexists(sink):
                continue
            if sink.is_symlink() or not sink.is_dir():
                raise RecoveryError("staged Curriculum sink is unsafe")
            if any(sink.iterdir()):
                raise RecoveryError(
                    "staged Curriculum stopped in an ambiguous RUNNING "
                    "state; refusing to rerun or replace its Agent ruling")
            if not os.path.lexists(original_sink):
                sink.rmdir()
        if _path_sha256(ruling_root) != _path_sha256(commit_root):
            raise RecoveryError(
                "staged Curriculum stopped in an ambiguous RUNNING state; "
                "refusing to rerun or replace its Agent ruling")
    else:
        raise RecoveryError(
            f"cannot run Curriculum from transaction state {txn['state']}")

    artifact = root / "episodes" / f"ep{episode:03d}" / "graded_artifact"
    reset = _reset(vm, artifact, target_task)
    staged_meta = copy.deepcopy(txn["meta"])
    staged_meta["curriculum_vm_reset"] = reset["ok"]
    staged_project_path = ruling_root / txn["project_rel"]
    staged_project = json.loads(
        staged_project_path.read_text(encoding="utf-8"))
    staged_eproot = ruling_root / "episodes" / f"ep{episode:03d}"
    staged_verdict = (
        ruling_root / "verdicts" / f"ep{episode:03d}.txt"
    ).read_bytes().decode("utf-8")

    _failure_point("before_curriculum")
    e7_loop._curriculum_stage(
        vm, episode, str(ruling_root), str(ruling_root / "curriculum"),
        str(ruling_root / "journal"), str(staged_eproot),
        staged_verdict,
        staged_meta, ccfg, staged_project, str(staged_project_path),
        agent_decided_stop=True,
        research_direction=curriculum_research_direction,
        target_task=target_task)
    _failure_point("after_curriculum_return")
    staged_meta["status"] = "done"
    staged_meta["t_end"] = time.strftime("%F %T")
    _fsync_tree(ruling_root)

    txn["meta"] = staged_meta
    txn["postimages"] = {
        rel: _path_sha256(ruling_root / rel) for rel in txn["targets"]}
    txn["state"] = "RULING_READY"
    txn["phase_history"].append("RULING_READY")
    _write_transaction(root, episode, txn)
    _failure_point("after_ruling_ready")
    return txn


def _copy_candidate(source: Path, candidate: Path) -> None:
    if candidate.exists():
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
    if source.is_dir():
        shutil.copytree(source, candidate)
        _fsync_tree(candidate)
    else:
        shutil.copy2(source, candidate)
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())


def _ensure_durable_directory(path: Path) -> None:
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise RecoveryError(f"transaction directory is unsafe: {path}")
        return
    path.mkdir(parents=False)
    _fsync_dir(path)
    _fsync_dir(path.parent)


def _install_postimage(root: Path, txn_dir: Path, rel: str,
                       preimage: str, postimage: str) -> None:
    target = root / rel
    source = txn_dir / "ruling_lineage" / rel
    current = _path_sha256(target)
    backup = txn_dir / "apply_backups" / _sha256(rel.encode("utf-8"))

    if current == postimage:
        return
    if current == _MISSING and preimage != _MISSING and backup.exists():
        if _path_sha256(backup) != preimage:
            raise RecoveryError(f"transaction backup drifted: {rel}")
    elif current != preimage:
        raise RecoveryError(f"transaction target drifted outside pre/post: {rel}")

    if postimage == _MISSING:
        if current != _MISSING:
            _ensure_durable_directory(backup.parent)
            os.replace(target, backup)
            _fsync_dir(target.parent)
            _fsync_dir(backup.parent)
            _failure_point(f"inside_install_backup:{rel}")
        return
    if _path_sha256(source) != postimage:
        raise RecoveryError(f"staged transaction postimage drifted: {rel}")

    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = target.parent / (
        f".{target.name}.recovery-{txn_dir.name}-"
        f"{_sha256(rel.encode('utf-8'))[:12]}")
    if os.path.lexists(candidate):
        if candidate.is_symlink() or _path_sha256(candidate) != postimage:
            raise RecoveryError(
                f"transaction install candidate drifted: {rel}")
    else:
        _copy_candidate(source, candidate)
    if current != _MISSING and target.is_dir():
        _ensure_durable_directory(backup.parent)
        if backup.exists():
            if _path_sha256(backup) != preimage:
                raise RecoveryError(f"transaction backup collision: {rel}")
            shutil.rmtree(target)
        else:
            os.replace(target, backup)
        _fsync_dir(target.parent)
        _fsync_dir(backup.parent)
        _failure_point(f"inside_install_backup:{rel}")
    os.replace(candidate, target)
    _fsync_dir(target.parent)
    _failure_point(f"inside_install_post:{rel}")
    if _path_sha256(target) != postimage:
        raise RecoveryError(f"transaction postimage install failed: {rel}")


def _assert_transaction_episode(root: Path, txn: dict) -> None:
    rows, _ = _read_jsonl(
        root / "episodes.jsonl", allow_torn_tail=True)
    if not rows or rows[-1].get("episode") != txn["episode"]:
        raise RecoveryError(
            "lineage episode authority advanced during recovery")
    latest = rows[-1]
    if latest == txn.get("meta"):
        return
    if latest.get("status") == "recovery_promoting" and \
            latest.get("recovery_transaction") == \
            f"ep{int(txn['episode']):03d}":
        return
    if latest.get("status") in _SAFE_STATUSES:
        return
    raise RecoveryError("lineage episode authority drifted during recovery")


def _validate_transaction_images(root: Path, txn: dict) -> None:
    directory = _txn_dir(root, int(txn["episode"]))
    for rel in txn["targets"]:
        preimage = txn["preimages"][rel]
        postimage = txn["postimages"][rel]
        source = directory / "ruling_lineage" / rel
        if _path_sha256(source) != postimage:
            raise RecoveryError(f"staged transaction postimage drifted: {rel}")
        current = _path_sha256(root / rel)
        if current in {preimage, postimage}:
            continue
        backup = directory / "apply_backups" / _sha256(rel.encode("utf-8"))
        if (current == _MISSING and preimage != _MISSING
                and backup.exists() and _path_sha256(backup) == preimage):
            continue
        raise RecoveryError(f"transaction target drifted outside pre/post: {rel}")


def _apply_transaction(root: Path, txn: dict) -> dict:
    episode = int(txn["episode"])
    directory = _txn_dir(root, episode)
    _assert_transaction_episode(root, txn)
    _validate_transaction_images(root, txn)
    if txn["state"] == "RULING_READY":
        txn["state"] = "RULING_APPLYING"
        txn["phase_history"].append("RULING_APPLYING")
        _write_transaction(root, episode, txn)

    for rel in txn["targets"]:
        _failure_point(f"before_apply:{rel}")
        _install_postimage(
            root, directory, rel, txn["preimages"][rel],
            txn["postimages"][rel])
        if rel not in txn["applied"]:
            txn["applied"].append(rel)
            _write_transaction(root, episode, txn)
        _failure_point(f"after_apply:{rel}")

    for rel in txn["targets"]:
        if _path_sha256(root / rel) != txn["postimages"][rel]:
            raise RecoveryError(f"applied recovery state drifted: {rel}")
    _assert_transaction_episode(root, txn)
    txn["state"] = "RULING_APPLIED"
    txn["phase_history"].append("RULING_APPLIED")
    _write_transaction(root, episode, txn)
    _failure_point("after_ruling_applied")
    return txn


def _complete_transaction(root: Path, txn: dict) -> dict:
    episode = int(txn["episode"])
    _assert_transaction_episode(root, txn)
    for rel in txn["targets"]:
        if _path_sha256(root / rel) != txn["postimages"][rel]:
            raise RecoveryError(f"completed recovery state drifted: {rel}")
    meta = copy.deepcopy(txn["meta"])
    _record_episode(root, meta, allow_torn_tail=True)
    _failure_point("after_terminal_episode_record")
    txn["state"] = "DONE"
    txn["phase_history"].append("DONE")
    _write_transaction(root, episode, txn)
    for name in ("commit_lineage", "ruling_lineage", "apply_backups"):
        payload = _txn_dir(root, episode) / name
        if payload.is_dir() and not payload.is_symlink():
            shutil.rmtree(payload)
    return meta


def _resume_transaction(vm, root: Path, episode: int, ccfg,
                        curriculum_research_direction: str,
                        target_task: str) -> dict | None:
    txn = _load_transaction(root, episode)
    if txn is None:
        return None
    _validate_transaction_params(
        txn, ccfg, curriculum_research_direction, target_task)
    _target_preflight(str(root), target_task, txn["meta"])
    e7_loop._assert_target_null_task(vm, target_task)
    if txn["state"] == "DONE":
        for rel in txn["targets"]:
            if _path_sha256(root / rel) != txn["postimages"][rel]:
                raise RecoveryError(f"completed recovery state drifted: {rel}")
        rows, _ = _read_jsonl(root / "episodes.jsonl")
        if not rows or rows[-1] != txn["meta"]:
            raise RecoveryError("terminal recovery episode record drifted")
        for name in ("commit_lineage", "ruling_lineage", "apply_backups"):
            payload = _txn_dir(root, episode) / name
            if payload.is_dir() and not payload.is_symlink():
                shutil.rmtree(payload)
        return copy.deepcopy(txn["meta"])
    if txn["state"] in {"COMMIT_PREPARED", "RULING_RUNNING"}:
        txn = _run_staged_curriculum(
            vm, root, txn, ccfg, curriculum_research_direction, target_task)
    if txn["state"] in {"RULING_READY", "RULING_APPLYING"}:
        txn = _apply_transaction(root, txn)
    if txn["state"] == "RULING_APPLIED":
        return _complete_transaction(root, txn)
    raise RecoveryError(f"unhandled recovery transaction state: {txn['state']}")


def _finish_episode(vm, checkpoint: dict, after: dict[str, bytes], verdict: str,
                    ccfg, curriculum_research_direction: str,
                    target_task: str, agent_decided_stop: bool) -> dict:
    root: Path = checkpoint["root"]
    eproot: Path = checkpoint["eproot"]
    meta = checkpoint["meta"]
    episode = int(meta["episode"])
    if target_task:
        from tools.exam_fence import audit_transcripts
        transcript_hits = []
        for directory in checkpoint["bound_transcript_dirs"]:
            transcript_hits.extend(audit_transcripts(
                str(directory), mode="practice",
                authorized_instruction=target_task))
        if transcript_hits:
            meta["status"] = "target_transcript_quarantined"
            meta["memory_promoted"] = False
            meta["target_transcript_fence_hits"] = [
                {"file": os.path.relpath(hit["file"], eproot),
                 "kinds": sorted({item.get("kind", "unknown")
                                  for item in hit.get("hits", [])})}
                for hit in transcript_hits[:20]]
            return meta

    _failure_point("before_commit_prepare")
    txn = _prepare_transaction(
        checkpoint, after, verdict, ccfg, curriculum_research_direction,
        target_task, agent_decided_stop)
    promoting = copy.deepcopy(txn["meta"])
    promoting["status"] = "recovery_promoting"
    promoting["memory_promoted"] = False
    promoting["recovery_transaction"] = f"ep{episode:03d}"
    promoting["recovery_transaction_state"] = txn["state"]
    _record_episode(root, promoting)
    _failure_point("after_commit_prepared")
    result = _resume_transaction(
        vm, root, episode, ccfg, curriculum_research_direction, target_task)
    if result is None:
        raise RecoveryError("prepared recovery transaction disappeared")
    return result


def _finish_declared_verifier(checkpoint: dict, meta: dict) -> dict | None:
    """Promote a declared Verifier call only from fully durable host output."""
    if checkpoint["verifier_recovery_state"] != "RUNNING" or not \
            checkpoint["verifier_transcript_exists"]:
        return None
    sink: Path = checkpoint["verifier_recovery_sink"]
    transcript = sink / "transcript.json"
    result = _read_phase_result(sink, transcript)
    report_path = sink / "verifier_report.md"
    if report_path.is_symlink():
        raise RecoveryError("declared Verifier report is a symlink")
    try:
        report_bytes = report_path.read_bytes() if report_path.is_file() else b""
        report = report_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryError(f"cannot read declared Verifier report: {exc}") \
            from exc
    actual_output = _sha256(report_bytes) if report_path.is_file() else _MISSING
    if (result["output_kind"] != "report"
            or result["output_sha256"] != actual_output):
        raise RecoveryError("declared Verifier report receipt drifted")
    errors = [] if report.strip() else ["report missing or empty"]
    if result["status"] == "done" and errors:
        raise RecoveryError(
            "Verifier completed but its declared report was not durably "
            "captured; refusing an ambiguous replay")
    complete = result["status"] == "done" and not errors
    meta.update(
        verifier_report_status=result["status"],
        verifier_report_complete=complete,
        verifier_report_errors=errors,
        verifier_report_iters=result["iters"],
        verifier_report_turns=result["turns"],
        verify_status=result["status"],
        verifier_recovery_state="FINISHED",
        verifier_recovery_transcript_sha256=result["transcript_sha256"])
    if complete:
        verdict = checkpoint["gblock"] + "\nreviewer: " + report
        existing = sink / "verdict.txt"
        if existing.exists() and existing.read_bytes() != verdict.encode("utf-8"):
            raise RecoveryError("declared recovery verdict drifted")
        _atomic_write(existing, verdict.encode("utf-8"))
        meta["status"] = "feedback_recovered"
        meta["verifier_report_sha256"] = _text_sha256(report)
        meta["recovery_verdict_sha256"] = _text_sha256(verdict)
    else:
        verdict = ""
        meta["status"] = "feedback_incomplete"
        meta["t_end"] = time.strftime("%F %T")
    _record_episode(checkpoint["root"], meta)
    return {"complete": complete, "report": report, "verdict": verdict}


def _finish_declared_reflection(checkpoint: dict, meta: dict) \
        -> dict | None:
    """Promote a declared memory call only with its exact persisted tree."""
    if checkpoint["reflection_recovery_state"] != "RUNNING" or not \
            checkpoint["reflection_transcript_exists"]:
        return None
    sink: Path = checkpoint["reflection_recovery_sink"]
    transcript = sink / "transcript.json"
    result = _read_phase_result(sink, transcript)
    memory_name = ("ready_memory" if result["status"] == "done"
                   else "partial_memory")
    memory_dir = sink / memory_name
    if not memory_dir.is_dir() or memory_dir.is_symlink():
        raise RecoveryError(
            "memory Agent completed but its declared memory tree was not "
            "durably captured; refusing an ambiguous replay")
    after = _tree(memory_dir)
    if not after and checkpoint["provisional"]:
        raise RecoveryError("declared recovery memory is unexpectedly empty")
    if (result["output_kind"] != "memory"
            or result["output_sha256"] != _tree_sha256(after)):
        raise RecoveryError("declared recovery memory receipt drifted")
    meta.update(
        reflection_status=result["status"],
        reflection_iters=result["iters"],
        reflection_turns=result["turns"],
        reflection_secs=result["wall_secs"],
        reflection_recovery_state="FINISHED",
        reflection_recovery_transcript_sha256=result["transcript_sha256"],
        reflection_recovery_memory_sha256=_tree_sha256(after))
    if result["status"] == "done":
        meta["status"] = "memory_recovered"
        meta["recovery_memory_ready"] = time.strftime("%F %T")
    else:
        meta["status"] = "memory_not_ready"
        meta["t_end"] = time.strftime("%F %T")
    _record_episode(checkpoint["root"], meta)
    return {"complete": result["status"] == "done", "memory": after,
            "memory_dir": memory_dir}


def _recover_incomplete_locked(
        vm, episode: int, cfg, vcfg, ccfg, root: str, *, reflection_cfg,
        require_complete_feedback: bool = True,
        curriculum_research_direction: str = "",
        curriculum_target_task: str = "",
        open_memory_research: bool = True) -> dict:
    """Resume an incomplete Verifier/memory suffix without replaying Actor work.

    Normal exits remain Agent-owned.  Hidden watchdog exits remain incomplete
    checkpoints, so this function can be invoked again on the resulting ledger.
    """
    configs = (cfg, vcfg, ccfg, reflection_cfg)
    if (not require_complete_feedback or not open_memory_research
            or reflection_cfg is None
            or not all(getattr(item, "agent_decided_stop", False)
                       for item in configs)):
        raise RecoveryError(
            "recovery requires complete feedback, open research, and "
            "Agent-decided stopping for every role")

    checkpoint = _read_checkpoint(root, episode, cfg, vcfg, reflection_cfg)
    meta = checkpoint["meta"]
    source_status = meta["status"]
    meta["recovery_started"] = time.strftime("%F %T")
    meta["recovery_from_status"] = source_status
    meta["verifier_protocol_transition"] = checkpoint[
        "verifier_protocol_transition"]
    meta["memory_promoted"] = False
    _target_preflight(root, curriculum_target_task, meta)
    _audit_target_text(
        checkpoint["gblock"], curriculum_target_task, "mechanical grade")
    e7_loop._assert_target_null_task(vm, curriculum_target_task)
    _record_episode(checkpoint["root"], meta)

    artifact: Path = checkpoint["artifact"]
    if source_status != "memory_recovered":
        initial_restore = e7_loop._restore_graded_artifact(
            vm, str(artifact), authorized_instruction=curriculum_target_task)
        if not initial_restore.get("ok"):
            raise RecoveryError(
                f"cannot replay the frozen graded artifact: {initial_restore}")
        meta["recovery_initial_restore"] = True

    report = checkpoint["report"]
    if source_status == "feedback_incomplete":
        durable_attempt = _finish_declared_verifier(checkpoint, meta)
        if durable_attempt is not None:
            if not durable_attempt["complete"]:
                return meta
            report = durable_attempt["report"]
            verdict = durable_attempt["verdict"]
        else:
            if not mem.push_memory(vm, str(checkpoint["provisional_dir"])):
                raise RecoveryError("cannot restore provisional Actor memory")
            report_guest = "/home/user/verifier_report.md"
            vm.run_command(f"rm -f {report_guest}", timeout=30)
            if checkpoint["verifier_recovery_state"] == "RUNNING":
                sink_path = checkpoint["verifier_recovery_sink"]
            else:
                sink_path = _declare_sink(
                    checkpoint["root"], checkpoint["eproot"], meta,
                    stem="verify_recovery",
                    sink_field="verifier_recovery_sink",
                    state_field="verifier_recovery_state",
                    input_bindings={
                        "verifier_recovery_task_sha256": checkpoint[
                            "bindings"]["recovery_task_sha256"],
                        "verifier_recovery_grade_sha256": checkpoint[
                            "bindings"]["recovery_grade_sha256"],
                        "verifier_recovery_memory_sha256": checkpoint[
                            "bindings"]["recovery_provisional_memory_sha256"],
                    },
                    parent_transcript=checkpoint[
                        "verifier_transcript_path"],
                    clear_fields=(
                        "verifier_recovery_transcript_sha256",
                        "verifier_report_status",
                        "verifier_report_complete",
                        "verifier_report_errors",
                        "verifier_report_iters",
                        "verifier_report_turns",
                        "verify_status",
                    ))
                checkpoint["verifier_recovery_state"] = "RUNNING"
                checkpoint["verifier_recovery_sink"] = sink_path
            _failure_point("after_verifier_sink_declared")
            verifier_notice = _verifier_recovery_notice()
            rres, rhist = run_attempt(
                verifier_notice, vm, checkpoint["review_cfg"],
                ArtifactSink(str(sink_path)),
                initial_history=checkpoint["verifier_history"],
                continue_context=True, allow_noop_done=True)
            boundary = len(checkpoint["verifier_history"])
            if (rhist[:boundary] != checkpoint["verifier_history"]
                    or len(rhist) <= boundary
                    or rhist[boundary]["content"] !=
                    continuation_message(verifier_notice)):
                raise RecoveryError(
                    "Verifier recovery transcript is not a continuation of "
                    "the recorded history")
            transcript = sink_path / "transcript.json"
            _atomic_json(transcript, {
                "system": build_system(checkpoint["review_cfg"]),
                "messages": rhist,
            })
            _failure_point("after_verifier_transcript")
            report_stage = sink_path / ".report_collect"
            report_stage.mkdir(exist_ok=False)
            collected = e7_loop._collect_verifier_report(
                vm, report_guest, rres, str(report_stage))
            report = collected["report"]
            if report:
                _atomic_write(
                    sink_path / "verifier_report.md",
                    report.encode("utf-8"))
            shutil.rmtree(report_stage)
            result_payload = _persist_phase_result(
                sink_path, rres, transcript, output_kind="report",
                output_sha256=(
                    _text_sha256(report) if report else _MISSING))
            _failure_point("after_verifier_report")
            meta.update(
                verifier_report_status=collected["status"],
                verifier_report_complete=collected["ok"],
                verifier_report_errors=collected["errors"],
                verifier_report_iters=collected["iters"],
                verifier_report_turns=collected["turns"],
                verify_status=rres.status,
                verifier_recovery_state="FINISHED",
                verifier_recovery_transcript_sha256=result_payload[
                    "transcript_sha256"])
            checkpoint["bound_transcript_dirs"].append(sink_path)
            reset = _reset(vm, artifact, curriculum_target_task)
            meta["research_vm_reset"] = reset["ok"]
            meta["graded_restore_after_verifier"] = reset["ok"]
            if not collected["ok"]:
                meta["status"] = "feedback_incomplete"
                meta["t_end"] = time.strftime("%F %T")
                _record_episode(checkpoint["root"], meta)
                return meta
            verdict = checkpoint["gblock"] + "\nreviewer: " + report
            _atomic_write(
                sink_path / "verdict.txt", verdict.encode("utf-8"))
            meta["status"] = "feedback_recovered"
            meta["verifier_report_sha256"] = _text_sha256(report)
            meta["recovery_verdict_sha256"] = _text_sha256(verdict)
            _record_episode(checkpoint["root"], meta)
        checkpoint["meta"] = meta
        checkpoint["report"] = report
        checkpoint["stored_verdict"] = verdict

    if report:
        _audit_target_text(report, curriculum_target_task,
                           "Verifier feedback")
    verdict = checkpoint["gblock"] + "\nreviewer: " + report
    if source_status == "memory_recovered":
        after = _tree(checkpoint["reflection_memory_dir"])
        result = _finish_episode(
            vm, checkpoint, after, verdict, ccfg,
            curriculum_research_direction, curriculum_target_task,
            agent_decided_stop=True)
        if result.get("status") != "done":
            _record_episode(checkpoint["root"], result)
        return result

    memory_source: Path = checkpoint["reflection_memory_dir"]
    durable_memory_attempt = _finish_declared_reflection(checkpoint, meta)
    if durable_memory_attempt is not None:
        if not durable_memory_attempt["complete"]:
            return meta
        after = durable_memory_attempt["memory"]
        checkpoint["meta"] = meta
        checkpoint["reflection_memory_dir"] = durable_memory_attempt[
            "memory_dir"]
        result = _finish_episode(
            vm, checkpoint, after, verdict, ccfg,
            curriculum_research_direction, curriculum_target_task,
            agent_decided_stop=True)
        if result.get("status") != "done":
            _record_episode(checkpoint["root"], result)
        return result

    if checkpoint["reflection_recovery_state"] == "RUNNING":
        reflection_sink = checkpoint["reflection_recovery_sink"]
    else:
        reflection_sink = _declare_sink(
            checkpoint["root"], checkpoint["eproot"], meta,
            stem="memory_reflection_recovery",
            sink_field="reflection_recovery_sink",
            state_field="reflection_recovery_state",
            input_bindings={
                "reflection_recovery_task_sha256": checkpoint["bindings"][
                    "recovery_task_sha256"],
                "reflection_recovery_grade_sha256": checkpoint["bindings"][
                    "recovery_grade_sha256"],
                "reflection_recovery_verdict_sha256": _text_sha256(verdict),
            },
            parent_transcript=checkpoint["reflection_transcript_path"],
            input_memory_dir=memory_source,
            clear_fields=(
                "reflection_recovery_transcript_sha256",
                "reflection_recovery_memory_sha256",
                "reflection_status", "reflection_iters",
                "reflection_turns", "reflection_secs",
                "recovery_memory_ready",
            ))
        checkpoint["reflection_recovery_state"] = "RUNNING"
        checkpoint["reflection_recovery_sink"] = reflection_sink
    _failure_point("after_reflection_sink_declared")
    if not mem.push_memory(vm, str(memory_source)):
        raise RecoveryError("cannot restore unfinished Actor memory state")
    snapshot = json.loads(
        (artifact / "snapshot.json").read_text(encoding="utf-8"))
    reflection_instruction = post_verdict_memory_msg(
        verdict, open_memory_research=True,
        disposable_roots=tuple(
            f"/home/user/{name}" for name in snapshot.get("roots", [])))
    mres, memory_history = run_attempt(
        reflection_instruction, vm, reflection_cfg,
        ArtifactSink(str(reflection_sink)),
        initial_history=checkpoint["reflection_history"],
        continue_context=True, allow_noop_done=True)
    boundary = len(checkpoint["reflection_history"])
    if (memory_history[:boundary] != checkpoint["reflection_history"]
            or len(memory_history) <= boundary
            or memory_history[boundary]["content"] !=
            continuation_message(reflection_instruction)):
        raise RecoveryError(
            "memory-research recovery transcript is not a continuation of "
            "the recorded history")
    transcript = reflection_sink / "transcript.json"
    _atomic_json(transcript, {
        "system": build_system(reflection_cfg),
        "messages": memory_history,
    })
    _failure_point("after_reflection_transcript")
    checkpoint["bound_transcript_dirs"].append(reflection_sink)

    restored = e7_loop._restore_graded_artifact(
        vm, str(artifact), authorized_instruction=curriculum_target_task)
    meta["graded_restore_after_memory"] = restored.get("ok", False)
    if not restored.get("ok"):
        raise RecoveryError(
            f"cannot restore graded artifact after memory research: {restored}")
    after = mem.pull_memory(vm)
    if not after and checkpoint["provisional"]:
        raise RecoveryError("cannot pull Actor memory after research")
    memory_name = "ready_memory" if mres.status == "done" else \
        "partial_memory"
    memory_output = reflection_sink / memory_name
    _atomic_memory_tree(memory_output, after)
    result_payload = _persist_phase_result(
        reflection_sink, mres, transcript, output_kind="memory",
        output_sha256=_tree_sha256(after))
    _failure_point("after_reflection_memory")
    meta.update(
        reflection_status=mres.status, reflection_iters=mres.iters,
        reflection_turns=mres.turns,
        reflection_secs=round(mres.wall_secs),
        reflection_recovery_state="FINISHED",
        reflection_recovery_transcript_sha256=result_payload[
            "transcript_sha256"])
    if mres.status != "done":
        meta["reflection_recovery_memory_sha256"] = _tree_sha256(after)
        meta["status"] = "memory_not_ready"
        meta["t_end"] = time.strftime("%F %T")
        _record_episode(checkpoint["root"], meta)
        return meta

    meta["reflection_recovery_memory_sha256"] = _tree_sha256(after)
    meta["status"] = "memory_recovered"
    meta["recovery_memory_ready"] = time.strftime("%F %T")
    _record_episode(checkpoint["root"], meta)
    checkpoint["meta"] = meta
    checkpoint["reflection_memory_dir"] = reflection_sink / "ready_memory"

    result = _finish_episode(
        vm, checkpoint, after, verdict, ccfg,
        curriculum_research_direction, curriculum_target_task,
        agent_decided_stop=True)
    if result.get("status") != "done":
        _record_episode(checkpoint["root"], result)
    return result


def recover_incomplete_episode(
        vm, episode: int, cfg, vcfg, ccfg, root: str, *, reflection_cfg,
        require_complete_feedback: bool = True,
        curriculum_research_direction: str = "",
        curriculum_target_task: str = "",
        open_memory_research: bool = True) -> dict:
    """Lock, resume, and durably record one incomplete episode suffix."""
    configs = (cfg, vcfg, ccfg, reflection_cfg)
    if (not require_complete_feedback or not open_memory_research
            or reflection_cfg is None
            or not all(getattr(item, "agent_decided_stop", False)
                       for item in configs)):
        raise RecoveryError(
            "recovery requires complete feedback, open research, and "
            "Agent-decided stopping for every role")
    lineage = Path(root)
    with _recovery_lock(lineage, episode):
        _assert_no_other_recovery_transaction(lineage, episode)
        resumed = _resume_transaction(
            vm, lineage, episode, ccfg, curriculum_research_direction,
            curriculum_target_task)
        if resumed is not None:
            return resumed
        return _recover_incomplete_locked(
            vm, episode, cfg, vcfg, ccfg, root,
            reflection_cfg=reflection_cfg,
            require_complete_feedback=require_complete_feedback,
            curriculum_research_direction=curriculum_research_direction,
            curriculum_target_task=curriculum_target_task,
            open_memory_research=open_memory_research)
