"""Durable, fail-closed memory initialization for an evolution lineage.

Initialization is provenance, not an instruction to restore memory.  Once a
lineage is registered as starting empty, later nonempty memory is expected: it
is the product of evolution.  What must never happen is relabeling that lineage
as seeded, or copying a seed over any existing memory.

The registration lives outside ``memory/`` so Actor memory transport cannot
modify it.  Existing pre-registration lineages remain readable by callers that
do not opt into this API; a lineage that *does* have a registration is always
validated fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Mapping


INITIALIZATION_DIRNAME = "_lineage_init"
INITIALIZATION_FILENAME = "memory.json"
INITIALIZATION_SCHEMA = 1

EMPTY_MEMORY_MODE = "empty"
FROZEN_E8_MEMORY_MODE = "frozen_e8"
FROZEN_E8_SOURCE = "results/explore/e8/memory_frozen_e8"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREINITIALIZATION_ENTRIES = {
    "_target_input",
    "fence_probe.json",
    "memory",
}


class InitializationError(RuntimeError):
    """A requested memory initialization is unsafe or conflicts with history."""


@dataclass(frozen=True)
class InitializationContext:
    mode: str
    source_manifest_sha256: str = ""

    @property
    def metadata(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": INITIALIZATION_SCHEMA,
            "memory_mode": self.mode,
        }
        if self.mode == FROZEN_E8_MEMORY_MODE:
            value.update(
                source=FROZEN_E8_SOURCE,
                source_manifest_sha256=self.source_manifest_sha256,
            )
        return value


def _paths(root: os.PathLike[str] | str) -> tuple[Path, Path, Path, Path]:
    root_path = Path(root).expanduser()
    if root_path.is_symlink():
        raise InitializationError("lineage root must not be a symlink")
    root_path = root_path.resolve()
    return (
        root_path,
        root_path / "memory",
        root_path / INITIALIZATION_DIRNAME,
        root_path / INITIALIZATION_DIRNAME / INITIALIZATION_FILENAME,
    )


def _context_from_metadata(data: Mapping[str, Any]) -> InitializationContext:
    mode = data.get("memory_mode")
    expected = {"schema_version", "memory_mode"}
    if mode == FROZEN_E8_MEMORY_MODE:
        expected.update({"source", "source_manifest_sha256"})
    if (data.get("schema_version") != INITIALIZATION_SCHEMA
            or mode not in {EMPTY_MEMORY_MODE, FROZEN_E8_MEMORY_MODE}
            or set(data) != expected):
        raise InitializationError(
            "lineage memory initialization metadata violates its schema")
    digest = data.get("source_manifest_sha256", "")
    if mode == FROZEN_E8_MEMORY_MODE and (
            data.get("source") != FROZEN_E8_SOURCE
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)):
        raise InitializationError(
            "lineage seeded-memory provenance is malformed")
    return InitializationContext(str(mode), str(digest))


def read_memory_initialization(
    root: os.PathLike[str] | str,
) -> InitializationContext | None:
    """Read and validate a durable registration, or return ``None``.

    A partially present registration is corruption, not an unregistered
    lineage.  The memory directory is also checked structurally, but its
    contents are deliberately unconstrained after initialization.
    """

    _root, memory, init_dir, metadata_path = _paths(root)
    if not init_dir.exists() and not init_dir.is_symlink():
        return None
    if init_dir.is_symlink() or not init_dir.is_dir():
        raise InitializationError(
            "lineage memory initialization registry must be a real directory")
    try:
        entries = list(init_dir.iterdir())
    except OSError as exc:
        raise InitializationError(
            "cannot inspect lineage memory initialization registry") from exc
    if ([entry.name for entry in entries] != [INITIALIZATION_FILENAME]
            or metadata_path.is_symlink() or not metadata_path.is_file()):
        raise InitializationError(
            "lineage memory initialization registry is incomplete or contains "
            "unexpected files")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitializationError(
            "lineage memory initialization metadata is unreadable") from exc
    if not isinstance(data, dict):
        raise InitializationError(
            "lineage memory initialization metadata must be a JSON object")
    context = _context_from_metadata(data)
    if memory.is_symlink() or not memory.is_dir():
        raise InitializationError(
            "registered lineage memory must be a real directory")
    return context


def _require_preinitialization_root(root: Path, memory: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise InitializationError(
            "memory initialization requires an existing real lineage directory")
    try:
        entries = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise InitializationError("cannot inspect lineage root") from exc
    unexpected = sorted(entries - _PREINITIALIZATION_ENTRIES)
    if unexpected:
        raise InitializationError(
            "memory initialization requires a new lineage with no prior "
            f"artifacts; found {unexpected!r}")
    if memory.exists() or memory.is_symlink():
        if memory.is_symlink() or not memory.is_dir():
            raise InitializationError(
                "lineage memory must be an absent or empty real directory")
        try:
            has_memory = next(memory.iterdir(), None) is not None
        except OSError as exc:
            raise InitializationError("cannot inspect lineage memory") from exc
        if has_memory:
            raise InitializationError(
                "unregistered lineage memory is nonempty; refusing to infer or "
                "overwrite its provenance")


def _write_registration(root: Path, context: InitializationContext) -> None:
    destination = root / INITIALIZATION_DIRNAME
    if destination.exists() or destination.is_symlink():
        raise InitializationError(
            "lineage memory initialization appeared concurrently")
    try:
        staging = Path(tempfile.mkdtemp(prefix="._lineage_init.", dir=root))
    except OSError as exc:
        raise InitializationError(
            f"cannot stage lineage memory initialization: {exc}") from exc
    try:
        os.chmod(staging, 0o700)
        metadata = staging / INITIALIZATION_FILENAME
        metadata.write_text(
            json.dumps(context.metadata, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(metadata, 0o600)
        os.rename(staging, destination)
    except OSError as exc:
        raise InitializationError(
            f"cannot register lineage memory initialization: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def initialize_empty_memory(
    root: os.PathLike[str] | str,
) -> InitializationContext:
    """Register a genuinely empty start without deleting or replacing anything."""

    root_path, memory, _init_dir, _metadata = _paths(root)
    existing = read_memory_initialization(root_path)
    if existing is not None:
        if existing.mode != EMPTY_MEMORY_MODE:
            raise InitializationError(
                "memory initialization drift refused: lineage is already "
                f"registered as {existing.mode!r}, not {EMPTY_MEMORY_MODE!r}")
        return existing

    _require_preinitialization_root(root_path, memory)
    if not memory.exists():
        try:
            memory.mkdir()
        except OSError as exc:
            raise InitializationError(
                f"cannot create empty lineage memory: {exc}") from exc
    context = InitializationContext(EMPTY_MEMORY_MODE)
    _write_registration(root_path, context)
    return read_memory_initialization(root_path) or context


def _manifest_entries(
    source: Path,
    manifest: Path,
) -> tuple[dict[str, str], str]:
    if (source.is_symlink() or not source.is_dir()
            or manifest.is_symlink() or not manifest.is_file()):
        raise InitializationError("seed source or manifest is unavailable")
    try:
        raw_manifest = manifest.read_bytes()
        lines = raw_manifest.decode("utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InitializationError("seed manifest is unreadable") from exc
    expected: dict[str, str] = {}
    for line in lines:
        try:
            digest, raw_name = line.split(None, 1)
        except ValueError as exc:
            raise InitializationError("seed manifest is malformed") from exc
        name = raw_name.strip()
        if name.startswith("./"):
            name = name[2:]
        posix = PurePosixPath(name)
        if (not name or posix.is_absolute() or ".." in posix.parts
                or str(posix) != name or not _SHA256.fullmatch(digest)
                or name in expected):
            raise InitializationError("seed manifest contains an unsafe entry")
        expected[name] = digest
    if not expected:
        raise InitializationError("seed manifest is empty")

    actual: set[str] = set()
    for base, dirs, files in os.walk(source, followlinks=False):
        for dirname in dirs:
            path = Path(base) / dirname
            if path.is_symlink():
                raise InitializationError("seed source contains a symlink")
        for filename in files:
            path = Path(base) / filename
            if path.is_symlink() or not path.is_file():
                raise InitializationError("seed source contains a non-regular file")
            actual.add(path.relative_to(source).as_posix())
    if actual != set(expected):
        raise InitializationError(
            "seed source file set does not match its manifest")
    for name, digest in expected.items():
        try:
            got = hashlib.sha256((source / name).read_bytes()).hexdigest()
        except OSError as exc:
            raise InitializationError("cannot read seed source") from exc
        if got != digest:
            raise InitializationError(
                f"seed manifest mismatch on {name!r}")
    return expected, hashlib.sha256(raw_manifest).hexdigest()


def _tree_matches(memory: Path, expected: Mapping[str, str]) -> bool:
    actual: set[str] = set()
    for base, dirs, files in os.walk(memory, followlinks=False):
        for dirname in dirs:
            if (Path(base) / dirname).is_symlink():
                return False
        for filename in files:
            path = Path(base) / filename
            if path.is_symlink() or not path.is_file():
                return False
            actual.add(path.relative_to(memory).as_posix())
    if actual != set(expected):
        return False
    try:
        return all(
            hashlib.sha256((memory / name).read_bytes()).hexdigest() == digest
            for name, digest in expected.items()
        )
    except OSError:
        return False


def initialize_seeded_memory(
    root: os.PathLike[str] | str,
    source: os.PathLike[str] | str,
    manifest: os.PathLike[str] | str,
) -> InitializationContext:
    """Initialize from the frozen E8 seed without overwriting existing bytes."""

    root_path, memory, _init_dir, _metadata = _paths(root)
    expected, manifest_digest = _manifest_entries(Path(source), Path(manifest))
    requested = InitializationContext(
        FROZEN_E8_MEMORY_MODE, manifest_digest)
    existing = read_memory_initialization(root_path)
    if existing is not None:
        if existing != requested:
            raise InitializationError(
                "memory initialization drift refused: lineage registration "
                "does not match the requested frozen seed")
        return existing

    _require_preinitialization_root(root_path, memory)
    if memory.exists():
        # A prior interrupted initialization may have completed the exact copy
        # before writing provenance. Adopt only those exact bytes; infer nothing
        # from a different or partial tree.
        if any(memory.iterdir()):
            if not _tree_matches(memory, expected):
                raise InitializationError(
                    "unregistered nonempty memory does not exactly match the "
                    "frozen seed; refusing to overwrite it")
        else:
            for name in sorted(expected):
                src = Path(source) / name
                dst = memory / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with src.open("rb") as source_file, dst.open("xb") as out:
                        shutil.copyfileobj(source_file, out)
                    shutil.copymode(src, dst)
                except OSError as exc:
                    raise InitializationError(
                        f"cannot copy frozen seed without overwrite: {exc}") from exc
    else:
        staging = Path(tempfile.mkdtemp(prefix="._memory_seed.", dir=root_path))
        try:
            shutil.rmtree(staging)
            shutil.copytree(source, staging, symlinks=False)
            if not _tree_matches(staging, expected):
                raise InitializationError("staged frozen seed failed verification")
            os.rename(staging, memory)
        except OSError as exc:
            raise InitializationError(
                f"cannot install frozen seed without overwrite: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    if not _tree_matches(memory, expected):
        raise InitializationError("installed frozen seed failed verification")
    _write_registration(root_path, requested)
    return read_memory_initialization(root_path) or requested
