"""Fail-closed host provenance for the E15 self-evolution lifecycle.

This module deliberately has no Agent or evaluator entry point.  It records the
immutable conditions under which an E15 lineage starts, provides a hash-chained
append-only lifecycle ledger, and emits the one marker an external evaluator may
consume after Curriculum convergence.

The hashes here are provenance, not grades.  In particular, neither the run
manifest nor the readiness record contains an OSWorld score or invokes an
official evaluator.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


RUN_MANIFEST_FILENAME = "E15_RUN_MANIFEST.json"
EVENT_LEDGER_FILENAME = "E15_EVENTS.jsonl"
READINESS_FILENAME = "E15_LINEAGE_READY.json"

RUN_MANIFEST_SCHEMA = 1
EVENT_SCHEMA = 1
READINESS_SCHEMA = 1

EVENT_STATUSES = frozenset({"in_progress", "completed", "failed", "yielded"})
EVENT_STATE_KEYS = frozenset(
    {"project_open", "memory_phase_open", "recovery_open"})

DEFAULT_EMERGENCY_POLICY: dict[str, bool] = {
    "agent_limits_are_infrastructure_watchdogs": True,
    "normal_completion_is_agent_decided": True,
    "watchdog_exit_is_convergence": False,
    "watchdog_exit_promotes_memory": False,
    # The current paid runner deliberately refuses an existing manifest because
    # model-context recovery is not implemented. Do not claim resumability.
    "watchdog_exit_is_resumable": False,
}

DEFAULT_RUNTIME_FILES = (
    "run_e15.py",
    "explore/e15_loop.py",
    "explore/e15_state.py",
    "explore/charter.py",
    "explore/commit.py",
    "explore/initialization.py",
    "explore/provision7.py",
    "explore/targeting.py",
    "core/actor.py",
    "core/checks.py",
    "core/eyes.py",
    "core/imagery.py",
    "core/loop.py",
    "core/trace.py",
    "core/verifier.py",
    "config/settings.py",
    "tools/exam_fence.py",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_EVENT_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MANIFEST_KEYS = frozenset({
    "schema_version", "kind", "created_at_utc", "lineage_root",
    "targeting", "initialization", "fence", "instruction_corpus",
    "configs", "systems", "runtime", "git", "invocation",
    "emergency_policy",
})
_EVENT_KEYS = frozenset({
    "schema_version", "sequence", "timestamp_utc", "event_type", "status",
    "state", "payload", "previous_event_sha256", "event_sha256",
})


class E15StateError(RuntimeError):
    """An E15 state/provenance invariant did not hold."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def _require_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise E15StateError("timestamp must be an ISO-8601 UTC string")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise E15StateError("timestamp must be an ISO-8601 UTC string") from exc
    return value


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        rendered = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise E15StateError(f"state is not canonical JSON data: {exc}") from exc
    return (rendered + ("\n" if newline else "")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 of canonical UTF-8 JSON (sorted keys, no insignificant space)."""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _check_real_directory(path: Path) -> Path:
    """Resolve a directory only after rejecting symlinks in every component."""

    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise E15StateError(f"directory is unavailable: {absolute}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise E15StateError(
                f"directory path must contain only real directories: {absolute}")
    return absolute


def _check_real_file(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    _check_real_directory(absolute.parent)
    try:
        mode = absolute.lstat().st_mode
    except OSError as exc:
        raise E15StateError(f"file is unavailable: {absolute}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise E15StateError(f"path must be a real regular file: {absolute}")
    return absolute


def _safe_file_bytes(path: os.PathLike[str] | str) -> bytes:
    checked = _check_real_file(Path(path))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(checked, flags)
    except OSError as exc:
        raise E15StateError(f"cannot open provenance file: {checked}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise E15StateError(f"path is not a regular file: {checked}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise E15StateError(f"file changed while hashing: {checked}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def file_sha256(path: os.PathLike[str] | str) -> str:
    return hashlib.sha256(_safe_file_bytes(path)).hexdigest()


def tree_hashes(root: os.PathLike[str] | str) -> dict[str, str]:
    """Hash every regular file below ``root`` and reject all symlinks.

    Special files are rejected too: a provenance tree must be a stable set of
    byte strings, not a device, socket, FIFO, or host-dependent link target.
    Empty directories do not contribute entries; callers that require a truly
    empty tree must additionally require that the root has no directory entries.
    """

    base = _check_real_directory(Path(root))
    hashes: dict[str, str] = {}

    def walk(directory: Path, prefix: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise E15StateError(f"cannot scan provenance tree: {directory}") from exc
        for entry in entries:
            if "/" in entry.name or entry.name in {".", ".."}:
                raise E15StateError("unsafe entry in provenance tree")
            path = directory / entry.name
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise E15StateError(f"cannot inspect provenance entry: {path}") from exc
            if stat.S_ISLNK(mode):
                raise E15StateError(f"symlink rejected in provenance tree: {path}")
            rel_parts = prefix + (entry.name,)
            if stat.S_ISDIR(mode):
                walk(path, rel_parts)
            elif stat.S_ISREG(mode):
                rel = "/".join(rel_parts)
                hashes[rel] = file_sha256(path)
            else:
                raise E15StateError(f"special file rejected in provenance tree: {path}")

    walk(base, ())
    return dict(sorted(hashes.items()))


def tree_sha256(hashes: Mapping[str, str]) -> str:
    """Canonical root digest for a ``tree_hashes`` mapping."""

    normalized: dict[str, str] = {}
    for rel, digest in hashes.items():
        if (not isinstance(rel, str) or not rel or rel.startswith("/")
                or ".." in Path(rel).parts or not _SHA256_RE.fullmatch(str(digest))):
            raise E15StateError("invalid tree-hash mapping")
        normalized[rel] = str(digest)
    return canonical_sha256(dict(sorted(normalized.items())))


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise E15StateError("short state-file write")
        offset += written


def write_json_exclusive(path: os.PathLike[str] | str, payload: Any) -> str:
    """Create one immutable-by-contract JSON file; never replace a prior file."""

    destination = Path(path).expanduser().absolute()
    _check_real_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise E15StateError(f"exclusive state file already exists: {destination}")
    data = _canonical_bytes(payload, newline=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(destination, flags, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise E15StateError(
                f"exclusive state file appeared concurrently: {destination}") from exc
        raise E15StateError(f"cannot create state file: {destination}") from exc
    return hashlib.sha256(data).hexdigest()


def write_json_atomic(path: os.PathLike[str] | str, payload: Any) -> str:
    """Atomically install JSON, rejecting symlink targets and parents."""

    destination = Path(path).expanduser().absolute()
    parent = _check_real_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        _check_real_file(destination)
    data = _canonical_bytes(payload, newline=True)
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if destination.is_symlink():
            raise E15StateError(f"atomic target became a symlink: {destination}")
        os.replace(temporary, destination)
        temporary = ""
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise E15StateError(f"cannot atomically write state file: {destination}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return hashlib.sha256(data).hexdigest()


def _json_object(path: os.PathLike[str] | str) -> tuple[dict[str, Any], bytes]:
    raw = _safe_file_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E15StateError(f"invalid JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise E15StateError(f"JSON state must be an object: {path}")
    return value, raw


def _normal_json(value: Any) -> Any:
    """Round-trip through canonical JSON and detach mutable caller objects."""

    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _metadata(value: Any) -> dict[str, Any]:
    candidate = value if isinstance(value, Mapping) else getattr(value, "metadata", None)
    if not isinstance(candidate, Mapping):
        raise E15StateError("target/initialization context lacks metadata")
    normalized = _normal_json(dict(candidate))
    if not isinstance(normalized, dict):
        raise E15StateError("context metadata must be an object")
    return normalized


def _config_semantics(cfg: Any) -> dict[str, Any]:
    if is_dataclass(cfg):
        value = asdict(cfg)
    elif hasattr(cfg, "__dict__"):
        value = dict(vars(cfg))
    else:
        raise E15StateError("resolved Config must be a dataclass-like object")
    normalized = _normal_json(value)
    if not isinstance(normalized, dict):
        raise E15StateError("resolved Config semantics must be an object")
    return normalized


def _repo_file(repo: Path, value: os.PathLike[str] | str) -> tuple[Path, str]:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(repo).as_posix()
    except ValueError as exc:
        raise E15StateError(f"runtime path escapes repository: {candidate}") from exc
    if not relative or any(ord(ch) < 32 for ch in relative):
        raise E15StateError("runtime path is unsafe")
    _check_real_file(candidate)
    return candidate, relative


def _run_git(repo: Path, args: Sequence[str]) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise E15StateError("git is unavailable for run provenance") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise E15StateError(f"git provenance command failed: {detail}")
    return proc.stdout


def _git_state(repo: Path, runtime_paths: Sequence[str]) -> dict[str, Any]:
    branch = _run_git(repo, ["branch", "--show-current"]).decode(
        "utf-8", errors="strict").strip()
    commit = _run_git(repo, ["rev-parse", "HEAD"]).decode(
        "ascii", errors="strict").strip()
    if not branch or not _GIT_COMMIT_RE.fullmatch(commit):
        raise E15StateError("E15 launch requires a named git branch and commit")
    dirty: list[dict[str, str]] = []
    for relative in sorted(runtime_paths):
        raw = _run_git(
            repo, ["status", "--porcelain=v1", "--untracked-files=all", "--", relative])
        text = raw.decode("utf-8", errors="strict").rstrip("\n")
        if text:
            dirty.append({"path": relative, "porcelain": text})
    return {"branch": branch, "commit": commit, "dirty_runtime_files": dirty}


def _relative_or_fail(path: Path, root: Path, *, label: str) -> str:
    try:
        rel = path.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise E15StateError(f"{label} must be beneath its provenance root") from exc
    if not rel:
        raise E15StateError(f"{label} path is invalid")
    return rel


def _model_projection(semantics: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "model", "vision_model", "vision_model_2", "verifier_model",
        "verifier_vision_model", "escalation_model", "look_merge_model",
    )
    return {key: semantics[key] for key in keys if key in semantics}


def build_run_manifest(
    lineage_root: os.PathLike[str] | str,
    *,
    targeting: Any,
    initialization: Any,
    config_paths: Mapping[str, os.PathLike[str] | str],
    configs: Mapping[str, Any],
    argv: Sequence[str],
    repo_root: os.PathLike[str] | str,
    runtime_files: Sequence[os.PathLike[str] | str] | None = None,
    fence_path: os.PathLike[str] | str | None = None,
    corpus_path: os.PathLike[str] | str | None = None,
    emergency_policy: Mapping[str, Any] = DEFAULT_EMERGENCY_POLICY,
    expected_instruction_count: int = 108,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build (but do not write) the immutable pre-Agent E15 manifest.

    All checks run before the returned object exists.  The caller should use
    :func:`write_json_exclusive` (or :func:`create_run_manifest`) so a restart
    cannot silently relabel an already-started lineage.
    """

    lineage = _check_real_directory(Path(lineage_root))
    repo = _check_real_directory(Path(repo_root))
    target_metadata = _metadata(targeting)
    init_metadata = _metadata(initialization)

    # Target registration and quarantined instruction bytes are bound without
    # copying target plaintext into the ordinary lineage manifest.
    target_meta_path = lineage / "_target_input" / "metadata.json"
    actual_target_metadata, target_meta_raw = _json_object(target_meta_path)
    if actual_target_metadata != target_metadata:
        raise E15StateError("targeting context drifted from its registry")
    instruction_digest = target_metadata.get("instruction_sha256", "")
    if target_metadata.get("mode") in {
            "instruction_only_targeted", "task_visible_targeted"}:
        instruction_path = lineage / "_target_input" / "instruction.txt"
        actual_instruction_digest = file_sha256(instruction_path)
        if (not _SHA256_RE.fullmatch(str(instruction_digest))
                or actual_instruction_digest != instruction_digest):
            raise E15StateError("target instruction hash does not match registration")
        context_digest = getattr(targeting, "instruction_sha256", instruction_digest)
        if context_digest != instruction_digest:
            raise E15StateError("in-memory targeting context has a different target hash")
    elif instruction_digest:
        raise E15StateError("blind target metadata must not carry an instruction hash")

    # E15 is a scratch experiment.  Registering an empty start is insufficient
    # if bytes or even empty subdirectories appeared before the manifest.
    if init_metadata.get("memory_mode") != "empty":
        raise E15StateError("E15 run manifest requires empty initialization")
    init_path = lineage / "_lineage_init" / "memory.json"
    actual_init_metadata, init_raw = _json_object(init_path)
    if actual_init_metadata != init_metadata:
        raise E15StateError("initialization context drifted from its registry")
    memory = _check_real_directory(lineage / "memory")
    try:
        if next(memory.iterdir(), None) is not None:
            raise E15StateError("pre-Agent E15 memory must be genuinely empty")
    except OSError as exc:
        raise E15StateError("cannot inspect pre-Agent E15 memory") from exc
    empty_hashes = tree_hashes(memory)

    fence = Path(fence_path) if fence_path is not None else lineage / "fence_probe.json"
    if not fence.is_absolute():
        fence = lineage / fence
    fence = _check_real_file(fence)
    fence_rel = _relative_or_fail(fence, lineage, label="fence")
    fence_record, fence_raw = _json_object(fence)
    if fence_record.get("failures") or fence_record.get("targeting") != target_metadata:
        raise E15StateError("fence probe failed or is bound to different target bytes")

    if corpus_path is None:
        corpus = repo / "results" / "explore" / "corpus_shingles.json"
    else:
        corpus = Path(corpus_path)
        if not corpus.is_absolute():
            corpus = repo / corpus
    corpus = _check_real_file(corpus)
    corpus_manifest = _check_real_file(Path(str(corpus) + ".MANIFEST.json"))
    corpus_rel = _relative_or_fail(corpus, repo, label="instruction corpus")
    corpus_manifest_rel = _relative_or_fail(
        corpus_manifest, repo, label="instruction corpus manifest")
    from explore.commit import validate_instruction_corpus
    try:
        corpus_record = validate_instruction_corpus(
            str(corpus), expected_instruction_count=expected_instruction_count)
    except RuntimeError as exc:
        raise E15StateError(f"instruction corpus provenance is invalid: {exc}") from exc

    if not config_paths or set(config_paths) != set(configs):
        raise E15StateError("config paths and resolved Config roles must match exactly")
    from config.settings import load as load_config
    from core.actor import build_system
    config_records: dict[str, dict[str, Any]] = {}
    systems: dict[str, dict[str, str]] = {}
    models: dict[str, dict[str, Any]] = {}
    config_runtime_paths: list[str] = []
    for role in sorted(config_paths):
        if not isinstance(role, str) or not role or any(ord(ch) < 32 for ch in role):
            raise E15StateError("config role names must be nonempty safe strings")
        config_file, relative = _repo_file(repo, config_paths[role])
        supplied_semantics = _config_semantics(configs[role])
        loaded = load_config(str(config_file))
        loaded_semantics = _config_semantics(loaded)
        if supplied_semantics != loaded_semantics:
            raise E15StateError(
                f"resolved Config for {role!r} differs from its YAML")
        config_records[role] = {
            "path": relative,
            "raw_yaml_sha256": file_sha256(config_file),
            "resolved_semantics": supplied_semantics,
            "resolved_semantics_sha256": canonical_sha256(supplied_semantics),
        }
        systems[role] = {
            "build_system_sha256": hashlib.sha256(
                build_system(configs[role]).encode("utf-8")).hexdigest(),
        }
        models[role] = _model_projection(supplied_semantics)
        config_runtime_paths.append(relative)

    selected_runtime = runtime_files if runtime_files is not None else DEFAULT_RUNTIME_FILES
    if not selected_runtime:
        raise E15StateError("E15 runtime file set must not be empty")
    runtime_hashes: dict[str, str] = {}
    for value in selected_runtime:
        runtime_file, relative = _repo_file(repo, value)
        if relative in runtime_hashes:
            raise E15StateError(f"duplicate E15 runtime file: {relative}")
        runtime_hashes[relative] = file_sha256(runtime_file)
    # Raw YAMLs are part of runtime dirtiness even though their hashes have a
    # dedicated schema section.
    git_paths = sorted(set(runtime_hashes) | set(config_runtime_paths))
    git_state = _git_state(repo, git_paths)

    if (not isinstance(argv, Sequence) or isinstance(argv, (str, bytes))
            or any(not isinstance(value, str) for value in argv)):
        raise E15StateError("argv must be a sequence of strings")
    policy = _normal_json(dict(emergency_policy))
    if not isinstance(policy, dict) or not policy:
        raise E15StateError("emergency policy must be a nonempty JSON object")

    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "kind": "e15_run_manifest",
        "created_at_utc": _require_timestamp(created_at_utc or _utc_now()),
        "lineage_root": str(lineage),
        "targeting": {
            "metadata": target_metadata,
            "instruction_sha256": str(instruction_digest),
            "metadata_file_sha256": hashlib.sha256(target_meta_raw).hexdigest(),
        },
        "initialization": {
            "metadata": init_metadata,
            "metadata_file_sha256": hashlib.sha256(init_raw).hexdigest(),
            "memory_tree_sha256": tree_sha256(empty_hashes),
            "memory_files": empty_hashes,
        },
        "fence": {
            "path": fence_rel,
            "sha256": hashlib.sha256(fence_raw).hexdigest(),
            "record": fence_record,
        },
        "instruction_corpus": {
            "corpus_path": corpus_rel,
            "corpus_sha256": file_sha256(corpus),
            "manifest_path": corpus_manifest_rel,
            "manifest_sha256": file_sha256(corpus_manifest),
            "manifest": _normal_json(corpus_record),
        },
        "configs": config_records,
        "systems": systems,
        "runtime": {"files": dict(sorted(runtime_hashes.items()))},
        "git": git_state,
        "invocation": {"argv": list(argv), "models": models},
        "emergency_policy": policy,
    }


def create_run_manifest(
    lineage_root: os.PathLike[str] | str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build, exclusively create, and immediately revalidate the run manifest."""

    lineage = _check_real_directory(Path(lineage_root))
    ledger = lineage / EVENT_LEDGER_FILENAME
    ready = lineage / READINESS_FILENAME
    if ledger.exists() or ledger.is_symlink() or ready.exists() or ready.is_symlink():
        raise E15StateError("pre-Agent manifest cannot be created after lifecycle state")
    manifest = build_run_manifest(lineage, **kwargs)
    path = lineage / RUN_MANIFEST_FILENAME
    write_json_exclusive(path, manifest)
    validate_run_manifest(
        path,
        repo_root=kwargs.get("repo_root"),
        loaded_configs=kwargs.get("configs"),
        expected_argv=kwargs.get("argv"),
        expected_emergency_policy=kwargs.get(
            "emergency_policy", DEFAULT_EMERGENCY_POLICY),
    )
    return manifest


def _assert_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise E15StateError(f"{label} schema keys are invalid")


def validate_run_manifest(
    manifest_path: os.PathLike[str] | str,
    *,
    repo_root: os.PathLike[str] | str | None = None,
    loaded_configs: Mapping[str, Any] | None = None,
    expected_argv: Sequence[str] | None = None,
    expected_emergency_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate immutable bytes against current target/config/system/runtime state.

    The pre-Agent empty-memory fact is historical and is therefore validated as
    a canonical empty-tree binding, not compared with the current (evolved)
    durable memory.  The immutable initialization registry is still rechecked.
    """

    path = _check_real_file(Path(manifest_path))
    manifest, _raw = _json_object(path)
    _assert_exact_keys(manifest, _MANIFEST_KEYS, "run manifest")
    if (manifest.get("schema_version") != RUN_MANIFEST_SCHEMA
            or manifest.get("kind") != "e15_run_manifest"):
        raise E15StateError("run manifest schema/version is invalid")
    _require_timestamp(manifest.get("created_at_utc"))
    lineage = _check_real_directory(Path(manifest.get("lineage_root", "")))
    if path != lineage / RUN_MANIFEST_FILENAME:
        raise E15StateError("run manifest is not at the registered lineage path")

    targeting = manifest.get("targeting")
    if not isinstance(targeting, dict) or set(targeting) != {
            "metadata", "instruction_sha256", "metadata_file_sha256"}:
        raise E15StateError("run manifest targeting schema is invalid")
    target_meta, target_meta_raw = _json_object(
        lineage / "_target_input" / "metadata.json")
    if (targeting["metadata"] != target_meta
            or targeting["metadata_file_sha256"]
            != hashlib.sha256(target_meta_raw).hexdigest()):
        raise E15StateError("target registration drifted after manifest creation")
    target_digest = targeting["instruction_sha256"]
    if target_meta.get("mode") in {
            "instruction_only_targeted", "task_visible_targeted"}:
        if (target_digest != target_meta.get("instruction_sha256")
                or file_sha256(lineage / "_target_input" / "instruction.txt")
                != target_digest):
            raise E15StateError("target instruction bytes drifted")
    elif target_digest:
        raise E15StateError("blind target binding carries an instruction hash")

    initialization = manifest.get("initialization")
    if not isinstance(initialization, dict) or set(initialization) != {
            "metadata", "metadata_file_sha256", "memory_tree_sha256", "memory_files"}:
        raise E15StateError("run manifest initialization schema is invalid")
    init_meta, init_raw = _json_object(lineage / "_lineage_init" / "memory.json")
    if (init_meta.get("memory_mode") != "empty"
            or initialization["metadata"] != init_meta
            or initialization["metadata_file_sha256"]
            != hashlib.sha256(init_raw).hexdigest()
            or initialization["memory_files"] != {}
            or initialization["memory_tree_sha256"] != tree_sha256({})):
        raise E15StateError("empty initialization provenance drifted")

    fence = manifest.get("fence")
    if not isinstance(fence, dict) or set(fence) != {"path", "sha256", "record"}:
        raise E15StateError("run manifest fence schema is invalid")
    fence_path = lineage / str(fence["path"])
    if _relative_or_fail(fence_path, lineage, label="fence") != fence["path"]:
        raise E15StateError("fence path is not canonical")
    fence_record, fence_raw = _json_object(fence_path)
    if (fence_record != fence["record"]
            or hashlib.sha256(fence_raw).hexdigest() != fence["sha256"]
            or fence_record.get("failures")
            or fence_record.get("targeting") != target_meta):
        raise E15StateError("fence provenance drifted or is not passing")

    repo = _check_real_directory(
        Path(repo_root) if repo_root is not None
        else Path(__file__).resolve().parents[1])
    # Runtime paths are deliberately repo-relative.  A copied manifest cannot
    # silently validate against another checkout.
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {"files"} \
            or not isinstance(runtime.get("files"), dict) or not runtime["files"]:
        raise E15StateError("run manifest runtime schema is invalid")
    runtime_files: dict[str, str] = runtime["files"]
    for relative, expected in runtime_files.items():
        runtime_file, canonical = _repo_file(repo, relative)
        if canonical != relative or file_sha256(runtime_file) != expected:
            raise E15StateError(f"E15 runtime drift detected: {relative}")

    corpus = manifest.get("instruction_corpus")
    if not isinstance(corpus, dict) or set(corpus) != {
            "corpus_path", "corpus_sha256", "manifest_path", "manifest_sha256",
            "manifest"}:
        raise E15StateError("instruction-corpus manifest schema is invalid")
    corpus_file, corpus_rel = _repo_file(repo, corpus["corpus_path"])
    corpus_manifest, corpus_manifest_rel = _repo_file(repo, corpus["manifest_path"])
    if (corpus_rel != corpus["corpus_path"]
            or corpus_manifest_rel != corpus["manifest_path"]
            or file_sha256(corpus_file) != corpus["corpus_sha256"]
            or file_sha256(corpus_manifest) != corpus["manifest_sha256"]):
        raise E15StateError("instruction corpus bytes drifted")
    expected_count = corpus["manifest"].get("instruction_count")
    from explore.commit import validate_instruction_corpus
    try:
        current_corpus_record = validate_instruction_corpus(
            str(corpus_file), expected_instruction_count=expected_count)
    except RuntimeError as exc:
        raise E15StateError("instruction corpus no longer validates") from exc
    if current_corpus_record != corpus["manifest"]:
        raise E15StateError("instruction corpus semantic manifest drifted")

    config_records = manifest.get("configs")
    systems = manifest.get("systems")
    if (not isinstance(config_records, dict) or not config_records
            or not isinstance(systems, dict) or set(systems) != set(config_records)):
        raise E15StateError("Config/system role bindings are invalid")
    if loaded_configs is not None and set(loaded_configs) != set(config_records):
        raise E15StateError("active Config roles differ from manifest")
    from config.settings import load as load_config
    from core.actor import build_system
    models: dict[str, dict[str, Any]] = {}
    config_paths: list[str] = []
    for role, record in config_records.items():
        if not isinstance(record, dict) or set(record) != {
                "path", "raw_yaml_sha256", "resolved_semantics",
                "resolved_semantics_sha256"}:
            raise E15StateError(f"Config record schema is invalid for {role!r}")
        config_file, relative = _repo_file(repo, record["path"])
        if relative != record["path"] or file_sha256(config_file) != record["raw_yaml_sha256"]:
            raise E15StateError(f"raw YAML drift detected for {role!r}")
        current_cfg = load_config(str(config_file))
        current_semantics = _config_semantics(current_cfg)
        if (current_semantics != record["resolved_semantics"]
                or canonical_sha256(current_semantics)
                != record["resolved_semantics_sha256"]):
            raise E15StateError(f"resolved Config drift detected for {role!r}")
        if (loaded_configs is not None
                and _config_semantics(loaded_configs[role]) != current_semantics):
            raise E15StateError(f"active Config drift detected for {role!r}")
        system = systems[role]
        if not isinstance(system, dict) or set(system) != {"build_system_sha256"}:
            raise E15StateError(f"system binding schema is invalid for {role!r}")
        current_system_hash = hashlib.sha256(
            build_system(current_cfg).encode("utf-8")).hexdigest()
        if current_system_hash != system["build_system_sha256"]:
            raise E15StateError(f"build_system drift detected for {role!r}")
        models[role] = _model_projection(current_semantics)
        config_paths.append(relative)

    invocation = manifest.get("invocation")
    if not isinstance(invocation, dict) or set(invocation) != {"argv", "models"}:
        raise E15StateError("invocation schema is invalid")
    if invocation["models"] != models:
        raise E15StateError("explicit model binding differs from resolved Configs")
    if expected_argv is not None and list(expected_argv) != invocation["argv"]:
        raise E15StateError("active argv differs from pre-Agent manifest")

    policy = manifest.get("emergency_policy")
    if not isinstance(policy, dict) or not policy:
        raise E15StateError("emergency policy binding is invalid")
    if (expected_emergency_policy is not None
            and _normal_json(dict(expected_emergency_policy)) != policy):
        raise E15StateError("active emergency policy differs from manifest")

    git = manifest.get("git")
    if not isinstance(git, dict) or set(git) != {
            "branch", "commit", "dirty_runtime_files"}:
        raise E15StateError("git binding schema is invalid")
    current_git = _git_state(repo, sorted(set(runtime_files) | set(config_paths)))
    if current_git != git:
        raise E15StateError("git branch/commit/runtime dirtiness drift detected")
    return manifest


def _event_hash(record_without_hash: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(record_without_hash))


def _validate_event_record(
    record: Any,
    *,
    sequence: int,
    previous: str | None,
    require_target_gated: bool = False,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise E15StateError("event ledger row must be a JSON object")
    _assert_exact_keys(record, _EVENT_KEYS, "event")
    if (record.get("schema_version") != EVENT_SCHEMA
            or record.get("sequence") != sequence):
        raise E15StateError("event ledger sequence/schema is invalid")
    _require_timestamp(record.get("timestamp_utc"))
    if not _EVENT_TYPE_RE.fullmatch(str(record.get("event_type", ""))):
        raise E15StateError("event_type must be uppercase snake-case")
    if record.get("status") not in EVENT_STATUSES:
        raise E15StateError("event status is invalid")
    state = record.get("state")
    if (not isinstance(state, dict) or set(state) != EVENT_STATE_KEYS
            or any(type(state[key]) is not bool for key in EVENT_STATE_KEYS)):
        raise E15StateError("event open-state flags are invalid")
    if not isinstance(record.get("payload"), dict):
        raise E15StateError("event payload must be an object")
    if record.get("previous_event_sha256") != previous:
        raise E15StateError("event hash chain is broken")
    digest = record.get("event_sha256")
    unsigned = {key: value for key, value in record.items() if key != "event_sha256"}
    if not _SHA256_RE.fullmatch(str(digest)) or _event_hash(unsigned) != digest:
        raise E15StateError("event self-hash is invalid")
    if record["event_type"] == "CONVERGED":
        _validate_converged_event(
            record, require_target_gated=require_target_gated)
    return record


def _parse_ledger_bytes(
        raw: bytes, *, require_target_gated: bool = False) \
        -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise E15StateError("event ledger has a partial final row")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise E15StateError("event ledger is not valid UTF-8") from exc
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, line in enumerate(lines, 1):
        if not line:
            raise E15StateError("event ledger contains a blank row")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise E15StateError("event ledger contains invalid JSON") from exc
        row = _validate_event_record(
            value, sequence=sequence, previous=previous,
            require_target_gated=require_target_gated)
        if rows and rows[-1]["event_type"] == "CONVERGED":
            raise E15StateError("event exists after terminal CONVERGED")
        rows.append(row)
        previous = row["event_sha256"]
    if rows and rows[-1]["event_type"] == "CONVERGED" \
            and "target_pass_event_sha256" in rows[-1]["payload"]:
        converged = rows[-1]
        payload = converged["payload"]
        pass_rows = [row for row in rows[:-1]
                     if row["event_type"] == "TARGET_TEST_PASSED"
                     and row["event_sha256"] ==
                     payload["target_pass_event_sha256"]]
        if len(pass_rows) != 1 or pass_rows[0]["status"] != "completed":
            raise E15StateError(
                "CONVERGED does not reference one completed target-test PASS")
        target_pass = pass_rows[0]
        pass_payload = target_pass["payload"]
        target_test_index = pass_payload.get("target_test_index")
        if type(target_test_index) is not int or target_test_index < 0:
            raise E15StateError("target-test PASS index is invalid")
        frozen = payload["frozen_memory_tree_sha256"]
        if target_pass["payload"].get(
                "frozen_memory_tree_sha256") != frozen:
            raise E15StateError(
                "CONVERGED memory differs from target-test memory")
        starts = [row for row in rows[:target_pass["sequence"] - 1]
                  if row["event_type"] == "TARGET_TEST_STARTED"
                  and row["payload"].get("target_test_index") ==
                  target_test_index]
        if len(starts) != 1 or starts[0]["payload"].get(
                "frozen_memory_tree_sha256") != frozen:
            raise E15StateError(
                "target-test PASS lacks one matching frozen-memory start")
        target_start = starts[0]
        if type(target_start["payload"].get("target_test_index")) is not int:
            raise E15StateError("target-test start index is invalid")
        if target_test_index == 0:
            bootstrap_rows = [
                row for row in rows[:target_start["sequence"] - 1]
                if row["event_type"] == "TARGET_BOOTSTRAP_STARTED"]
            continual_rows = [
                row for row in rows[:target_start["sequence"] - 1]
                if row["event_type"] == "CONTINUAL_MEMORY_IMPORTED"]
            bootstrap_memory = tree_sha256({})
            continual_valid = not continual_rows
            if len(continual_rows) == 1:
                continual = continual_rows[0]
                continual_payload = continual["payload"]
                bootstrap_memory = continual_payload.get(
                    "memory_tree_sha256", "")
                continual_valid = (
                    continual["status"] == "completed"
                    and not any(continual["state"].values())
                    and _SHA256_RE.fullmatch(str(bootstrap_memory)) is not None
                    and _SHA256_RE.fullmatch(str(continual_payload.get(
                        "source_convergence_event_sha256", ""))) is not None
                    and _SHA256_RE.fullmatch(str(continual_payload.get(
                        "source_target_pass_event_sha256", ""))) is not None
                    and _SHA256_RE.fullmatch(str(continual_payload.get(
                        "source_closure_sha256", ""))) is not None
                    and isinstance(continual_payload.get("source_lineage"), str)
                    and bool(continual_payload.get("source_lineage"))
                    and isinstance(continual_payload.get("source_task_id"), str)
                    and bool(continual_payload.get("source_task_id")))
            elif len(continual_rows) > 1:
                continual_valid = False
            if (not continual_valid
                    or frozen != bootstrap_memory
                    or target_start["payload"].get("bootstrap") is not True
                    or pass_payload.get("bootstrap") is not True
                    or len(bootstrap_rows) != 1
                    or bootstrap_rows[0]["status"] != "in_progress"
                    or bootstrap_rows[0]["payload"].get(
                        "frozen_memory_tree_sha256") != bootstrap_memory
                    or not _SHA256_RE.fullmatch(str(
                        bootstrap_rows[0]["payload"].get(
                            "target_sha256", "")))
                    or not _SHA256_RE.fullmatch(str(
                        bootstrap_rows[0]["payload"].get(
                            "input_manifest_sha256", "")))):
                raise E15StateError(
                    "bootstrap target PASS is not bound to its registered-memory "
                    "Q0 (empty-memory Q0 without continual import)")
        elif (target_start["payload"].get("bootstrap") is True
              or pass_payload.get("bootstrap") is True):
            raise E15StateError(
                "non-bootstrap target PASS is mislabeled as bootstrap")
        between = rows[starts[0]["sequence"]:target_pass["sequence"] - 1]
        actors = [
            row for row in between
            if row["event_type"] == "TARGET_TEST_ACTOR_STARTED"
            and row["payload"].get("attempt_index") == target_test_index]
        verifiers = [
            row for row in between
            if row["event_type"] == "TARGET_TEST_VERIFIER_STARTED"
            and row["payload"].get("attempt_index") == target_test_index]
        if (len(actors) != 1 or len(verifiers) != 1
                or type(actors[0]["payload"].get("attempt_index")) is not int
                or type(verifiers[0]["payload"].get("attempt_index")) is not int
                or actors[0]["sequence"] >= verifiers[0]["sequence"]
                or actors[0]["payload"].get(
                    "frozen_memory_tree_sha256") != frozen
                or verifiers[0]["payload"].get(
                    "frozen_memory_tree_sha256") != frozen):
            raise E15StateError(
                "target-test PASS lacks one ordered Actor/Verifier chain")
        evidence_keys = {
            "actor_handoff_sha256", "candidate_manifest_sha256",
            "candidate_archive_sha256", "candidate_roots_sha256",
            "actor_execution_manifest_sha256",
        }
        if (any(not _SHA256_RE.fullmatch(str(pass_payload.get(key, "")))
                for key in evidence_keys)
                or any(verifiers[0]["payload"].get(key)
                       != pass_payload.get(key) for key in evidence_keys)
                or verifiers[0]["payload"].get("candidate_snapshot_path")
                != pass_payload.get("candidate_snapshot_path")):
            raise E15StateError(
                "target-test PASS is not bound to Verifier candidate evidence")
        expected_prefix = (
            "bootstrap/q000/candidate/" if target_test_index == 0 else
            f"target_tests/test{target_test_index:03d}/candidate/")
        candidate_path = pass_payload.get("candidate_snapshot_path")
        if (not isinstance(candidate_path, str)
                or not candidate_path.startswith(expected_prefix)
                or re.fullmatch(
                    re.escape(expected_prefix) + r"publication_[0-9]{3}",
                    candidate_path) is None
                or not _SHA256_RE.fullmatch(str(
                    pass_payload.get("verifier_report_sha256", "")))):
            raise E15StateError(
                "target-test PASS candidate/report binding is malformed")
        if any(row["event_type"] in {
                "MEMORY_PHASE_STARTED", "MEMORY_PROMOTED"} for row in between):
            raise E15StateError(
                "memory changed while the passing target test was open")
    return rows


def read_event_ledger(
        path: os.PathLike[str] | str, *, require_target_gated: bool = False) \
        -> list[dict[str, Any]]:
    """Read and fully verify an existing E15 event ledger."""

    return _parse_ledger_bytes(
        _safe_file_bytes(path), require_target_gated=require_target_gated)


def _validate_converged_event(
        record: Mapping[str, Any], *, require_target_gated: bool = False) \
        -> None:
    if record.get("status") != "completed" or any(record["state"].values()):
        raise E15StateError(
            "CONVERGED requires completed status and no open project/memory/recovery")
    payload = record["payload"]
    required = {
        "memory_promoted", "latest_memory_promotion_sha256",
        "convergence_rationale_sha256",
    }
    if not required <= set(payload) or payload.get("memory_promoted") is not True:
        raise E15StateError("CONVERGED lacks completed memory-promotion evidence")
    for key in ("latest_memory_promotion_sha256", "convergence_rationale_sha256"):
        if not _SHA256_RE.fullmatch(str(payload.get(key, ""))):
            raise E15StateError(f"CONVERGED {key} is not a SHA-256 digest")
    target_keys = {
        "target_pass_event_sha256", "frozen_memory_tree_sha256"}
    if require_target_gated and not target_keys <= set(payload):
        raise E15StateError(
            "task-visible CONVERGED requires target-test PASS linkage")
    if target_keys & set(payload):
        if not target_keys <= set(payload):
            raise E15StateError(
                "target-gated CONVERGED linkage is incomplete")
        for key in target_keys:
            if not _SHA256_RE.fullmatch(str(payload.get(key, ""))):
                raise E15StateError(
                    f"CONVERGED {key} is not a SHA-256 digest")


class EventLedger:
    """Locked, hash-chained JSONL append API for one lineage."""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path).expanduser().absolute()

    def append(
        self,
        event_type: str,
        *,
        status: str,
        state: Mapping[str, bool],
        payload: Mapping[str, Any] | None = None,
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        parent = _check_real_directory(self.path.parent)
        if self.path.is_symlink():
            raise E15StateError("event ledger must not be a symlink")
        flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise E15StateError("cannot open event ledger") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise E15StateError("event ledger is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            rows = _parse_ledger_bytes(b"".join(chunks))
            if rows and rows[-1]["event_type"] == "CONVERGED":
                raise E15StateError("cannot append after terminal CONVERGED")
            previous = rows[-1]["event_sha256"] if rows else None
            unsigned = {
                "schema_version": EVENT_SCHEMA,
                "sequence": len(rows) + 1,
                "timestamp_utc": _require_timestamp(timestamp_utc or _utc_now()),
                "event_type": event_type,
                "status": status,
                "state": _normal_json(dict(state)),
                "payload": _normal_json(dict(payload or {})),
                "previous_event_sha256": previous,
            }
            record = dict(unsigned, event_sha256=_event_hash(unsigned))
            _validate_event_record(record, sequence=len(rows) + 1, previous=previous)
            os.lseek(fd, 0, os.SEEK_END)
            _write_all(fd, _canonical_bytes(record, newline=True))
            os.fsync(fd)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return record
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def read(self) -> list[dict[str, Any]]:
        return read_event_ledger(self.path)


def verify_target_pass_archives(
        lineage: Path, target_pass: Mapping[str, Any]) -> None:
    """Verify the exact archived handoff, candidate, execution, and report."""

    payload = target_pass["payload"]
    index = payload["target_test_index"]
    test_root = _check_real_directory(
        lineage / "bootstrap/q000" if index == 0 else
        lineage / "target_tests" / f"test{index:03d}")
    candidate_relative = str(payload["candidate_snapshot_path"])
    candidate = _check_real_directory(lineage / candidate_relative)
    if _relative_or_fail(
            candidate, lineage, label="target candidate") != candidate_relative:
        raise E15StateError("target candidate archive path is not canonical")

    bindings = {
        test_root / "actor_handoff.md": payload["actor_handoff_sha256"],
        test_root / "verifier_report.md": payload["verifier_report_sha256"],
        candidate / "materials.MANIFEST.sha256":
            payload["candidate_manifest_sha256"],
        candidate / "materials.tgz": payload["candidate_archive_sha256"],
        candidate / "materials.ROOTS.json": payload["candidate_roots_sha256"],
        test_root / "actor_execution_evidence" / "SHA256SUMS":
            payload["actor_execution_manifest_sha256"],
    }
    for path, expected in bindings.items():
        if file_sha256(path) != expected:
            raise E15StateError(
                "target-test archived evidence drifted after PASS")

    evidence_root = _check_real_directory(
        test_root / "actor_execution_evidence")
    manifest_raw = _safe_file_bytes(evidence_root / "SHA256SUMS")
    try:
        lines = manifest_raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise E15StateError(
            "Actor execution evidence manifest is not UTF-8") from exc
    expected_files: dict[str, str] = {}
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise E15StateError(
                "Actor execution evidence manifest is malformed")
        digest, name = line[:64], line[66:]
        relative = Path(name)
        if (not _SHA256_RE.fullmatch(digest) or not name
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or name == "SHA256SUMS" or name in expected_files):
            raise E15StateError(
                "Actor execution evidence manifest contains an unsafe entry")
        expected_files[name] = digest
    actual_files = tree_hashes(evidence_root)
    actual_files.pop("SHA256SUMS", None)
    if actual_files != expected_files:
        raise E15StateError(
            "Actor execution evidence files differ from their manifest")


def write_readiness_record(
    lineage_root: os.PathLike[str] | str,
    *,
    manifest_path: os.PathLike[str] | str | None = None,
    ledger_path: os.PathLike[str] | str | None = None,
    memory_path: os.PathLike[str] | str | None = None,
    loaded_configs: Mapping[str, Any] | None = None,
    repo_root: os.PathLike[str] | str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Exclusively record readiness after a fully closed CONVERGED event.

    This function never starts an evaluator.  The explicit false flag is a
    hand-off fact: the official evaluator is allowed only *after* this record is
    durably written.
    """

    lineage = _check_real_directory(Path(lineage_root))
    manifest_file = _check_real_file(
        Path(manifest_path) if manifest_path is not None
        else lineage / RUN_MANIFEST_FILENAME)
    ledger_file = _check_real_file(
        Path(ledger_path) if ledger_path is not None
        else lineage / EVENT_LEDGER_FILENAME)
    memory = _check_real_directory(
        Path(memory_path) if memory_path is not None else lineage / "memory")
    if manifest_file != lineage / RUN_MANIFEST_FILENAME \
            or ledger_file != lineage / EVENT_LEDGER_FILENAME \
            or memory != lineage / "memory":
        raise E15StateError("readiness inputs must be the registered lineage files")
    manifest = validate_run_manifest(
        manifest_file, loaded_configs=loaded_configs, repo_root=repo_root)
    require_target_gated = (
        manifest.get("targeting", {}).get("metadata", {}).get("mode")
        == "task_visible_targeted")
    rows = read_event_ledger(
        ledger_file, require_target_gated=require_target_gated)
    if not rows or rows[-1]["event_type"] != "CONVERGED":
        raise E15StateError("readiness requires terminal CONVERGED")
    converged = rows[-1]
    _validate_converged_event(
        converged, require_target_gated=require_target_gated)

    promotions = [row for row in rows
                  if row["event_type"] == "MEMORY_PROMOTED"
                  and row["status"] == "completed"]
    if not promotions:
        raise E15StateError("CONVERGED does not follow a completed MEMORY_PROMOTED event")
    latest_promotion = promotions[-1]
    if (converged["payload"]["latest_memory_promotion_sha256"]
            != latest_promotion["event_sha256"]):
        raise E15StateError("CONVERGED points to the wrong memory-promotion event")

    memory_hashes = tree_hashes(memory)
    memory_digest = tree_sha256(memory_hashes)
    if latest_promotion["payload"].get("memory_tree_sha256") != memory_digest:
        raise E15StateError("durable memory drifted after its latest promotion")
    if "target_pass_event_sha256" in converged["payload"]:
        target_pass_sha = converged["payload"]["target_pass_event_sha256"]
        target_passes = [
            row for row in rows
            if row["event_type"] == "TARGET_TEST_PASSED"
            and row["event_sha256"] == target_pass_sha]
        if len(target_passes) != 1:
            raise E15StateError(
                "readiness lacks the referenced target-test PASS")
        frozen = converged["payload"]["frozen_memory_tree_sha256"]
        if (target_passes[0]["payload"].get(
                "frozen_memory_tree_sha256") != frozen
                or latest_promotion["payload"].get(
                    "target_pass_event_sha256") != target_pass_sha
                or latest_promotion["payload"].get(
                    "memory_tree_sha256") != frozen
                or memory_digest != frozen):
            raise E15StateError(
                "readiness memory is not the memory that passed target test")
        if require_target_gated:
            verify_target_pass_archives(lineage, target_passes[0])

    manifest_raw = _safe_file_bytes(manifest_file)
    ledger_raw = _safe_file_bytes(ledger_file)
    record = {
        "schema_version": READINESS_SCHEMA,
        "kind": "e15_lineage_ready",
        "status": "ready_for_official_evaluation",
        "official_evaluator_ran": False,
        "created_at_utc": _require_timestamp(created_at_utc or _utc_now()),
        "run_manifest": {
            "path": RUN_MANIFEST_FILENAME,
            "sha256": hashlib.sha256(manifest_raw).hexdigest(),
        },
        "event_ledger": {
            "path": EVENT_LEDGER_FILENAME,
            "sha256": hashlib.sha256(ledger_raw).hexdigest(),
            "records": len(rows),
            "last_sequence": converged["sequence"],
            "last_event_sha256": converged["event_sha256"],
        },
        "memory": {
            "path": "memory",
            "tree_sha256": memory_digest,
            "files": memory_hashes,
        },
        "convergence": {
            "event_sequence": converged["sequence"],
            "event_sha256": converged["event_sha256"],
            **({
                "target_pass_event_sha256":
                    converged["payload"]["target_pass_event_sha256"],
                "frozen_memory_tree_sha256":
                    converged["payload"]["frozen_memory_tree_sha256"],
            } if "target_pass_event_sha256" in converged["payload"] else {}),
        },
    }
    write_json_exclusive(lineage / READINESS_FILENAME, record)
    return record
