"""Host-only registration for target-aware curriculum evolution.

This module deliberately does not know how to build an agent prompt.  It owns
the narrower boundary between an operator-provided task statement and an
evolution lineage:

* blind mode is the default;
* target-aware mode requires an exact, conspicuous acknowledgement;
* the first registered mode and target hash are immutable for that root;
* the registry's source copy lives only in
  ``<root>/_target_input/instruction.txt``;
* metadata contains a hash, never the statement or its source pathname.

Callers must keep ``_target_input`` out of every guest/archive transport.  The
directory name is intentionally outside the E7 curriculum archive allowlist,
but that existing structural exclusion is not replaced by this module.

The registry proves byte identity and lineage separation; it cannot infer that
arbitrary prose is truly an instruction rather than an answer or evaluator.
The exact acknowledgement is therefore an operator attestation, not a semantic
content classifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping


TARGET_AWARE_ACK = (
    "I-ATTEST-TARGET-IS-INSTRUCTION-ONLY-NO-GRADER-SCORE-OR-ANSWER")
TASK_VISIBLE_TARGET_ACK = (
    "I-ATTEST-TARGET-USES-NORMAL-TASK-VISIBLE-SETUP-NO-EVALUATOR-SCORE-"
    "OR-HIDDEN-GRADING")
LEGACY_BLIND_ACK = (
    "I-ATTEST-THIS-EXISTING-LINEAGE-NEVER-RECEIVED-A-TARGET-INSTRUCTION")
TARGET_INPUT_DIRNAME = "_target_input"
TARGET_INPUT_FILENAME = "instruction.txt"
TARGET_METADATA_FILENAME = "metadata.json"
TARGET_METADATA_SCHEMA = 1
LEGACY_BLIND_ORIGIN = "operator_attested_legacy_blind"

BLIND_MODE = "blind"
TARGET_AWARE_MODE = "instruction_only_targeted"
TASK_VISIBLE_TARGET_MODE = "task_visible_targeted"
_LINEAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TargetingError(RuntimeError):
    """The requested lineage mode violates the target-isolation contract."""


def resolve_lineage_root(
    base: os.PathLike[str] | str,
    name: str,
) -> str:
    """Resolve one safe lineage basename directly beneath ``base``."""

    if not isinstance(name, str) or not _LINEAGE_NAME.fullmatch(name):
        raise TargetingError(
            "lineage root must be one safe 1-128 character basename")
    base_path = Path(base).expanduser().resolve()
    unresolved = base_path / name
    if unresolved.is_symlink() or (
            unresolved.exists() and not unresolved.is_dir()):
        raise TargetingError(
            "lineage root must be a real directory directly under the "
            "results directory")
    candidate = unresolved.resolve()
    if candidate.parent != base_path:
        # Defense in depth against path resolution changing between checks.
        raise TargetingError(
            "lineage root must resolve directly under the results directory")
    return str(candidate)


@dataclass(frozen=True)
class TargetingContext:
    """Validated targeting state returned to the host-side runner.

    ``instruction`` is intentionally available only in this in-memory object;
    :attr:`metadata` is the safe object to serialize into ordinary lineage
    results.
    """

    mode: str
    instruction: str
    instruction_sha256: str
    quarantine_dir: str
    registration_origin: str = ""
    task_id: str = ""

    @property
    def target_aware(self) -> bool:
        return self.mode in {TARGET_AWARE_MODE, TASK_VISIBLE_TARGET_MODE}

    @property
    def task_visible(self) -> bool:
        return self.mode == TASK_VISIBLE_TARGET_MODE

    @property
    def metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": TARGET_METADATA_SCHEMA,
            "mode": self.mode,
        }
        if self.target_aware:
            data["instruction_sha256"] = self.instruction_sha256
        if self.task_visible:
            data["task_id"] = self.task_id
        if self.registration_origin:
            data["registration_origin"] = self.registration_origin
        return data


def _paths(root: os.PathLike[str] | str) -> tuple[Path, Path, Path, Path]:
    root_path = Path(root).expanduser().resolve()
    quarantine = root_path / TARGET_INPUT_DIRNAME
    statement = quarantine / TARGET_INPUT_FILENAME
    metadata = quarantine / TARGET_METADATA_FILENAME
    return root_path, quarantine, statement, metadata


def _require_ack(acknowledgement: str | None) -> None:
    supplied = acknowledgement or ""
    if not hmac.compare_digest(supplied, TARGET_AWARE_ACK):
        raise TargetingError(
            "target-aware mode requires the exact acknowledgement "
            f"{TARGET_AWARE_ACK!r}"
        )


def _require_task_visible_ack(acknowledgement: str | None) -> None:
    supplied = acknowledgement or ""
    if not hmac.compare_digest(supplied, TASK_VISIBLE_TARGET_ACK):
        raise TargetingError(
            "task-visible target mode requires the exact acknowledgement "
            f"{TASK_VISIBLE_TARGET_ACK!r}"
        )


def _require_legacy_blind_ack(acknowledgement: str | None) -> None:
    supplied = acknowledgement or ""
    if not hmac.compare_digest(supplied, LEGACY_BLIND_ACK):
        raise TargetingError(
            "legacy blind migration requires the exact acknowledgement "
            f"{LEGACY_BLIND_ACK!r}"
        )


def _read_target_file(path: os.PathLike[str] | str) -> tuple[bytes, str, str]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise TargetingError("target input must be an existing regular file")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise TargetingError(f"cannot read target input: {exc}") from exc
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TargetingError("target input must be valid UTF-8") from exc
    if not text.strip():
        raise TargetingError("target input must not be empty")
    if "\x00" in text:
        raise TargetingError("target input must not contain NUL bytes")
    return raw, text, hashlib.sha256(raw).hexdigest()


def _read_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TargetingError("lineage target registration is incomplete") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetingError("lineage target metadata is unreadable") from exc
    if not isinstance(data, dict):
        raise TargetingError("lineage target metadata must be a JSON object")
    mode = data.get("mode")
    expected_keys = {"schema_version", "mode"}
    if mode in {TARGET_AWARE_MODE, TASK_VISIBLE_TARGET_MODE}:
        expected_keys.add("instruction_sha256")
        if mode == TASK_VISIBLE_TARGET_MODE:
            expected_keys.add("task_id")
    elif data.get("registration_origin") == LEGACY_BLIND_ORIGIN:
        expected_keys.add("registration_origin")
    if (data.get("schema_version") != TARGET_METADATA_SCHEMA
            or mode not in {
                BLIND_MODE, TARGET_AWARE_MODE, TASK_VISIBLE_TARGET_MODE}
            or set(data) != expected_keys):
        raise TargetingError("lineage target metadata violates its schema")
    digest = data.get("instruction_sha256", "")
    if mode == TARGET_AWARE_MODE and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)):
        raise TargetingError("lineage target hash is malformed")
    if mode == TASK_VISIBLE_TARGET_MODE:
        task_id = data.get("task_id")
        if (not isinstance(task_id, str)
                or not re.fullmatch(r"task_[0-9]{3}", task_id)):
            raise TargetingError("task-visible target id is malformed")
        if (not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)):
            raise TargetingError("lineage target hash is malformed")
    return data


def _validate_quarantine(quarantine: Path, metadata: Mapping[str, Any]) -> None:
    if quarantine.is_symlink() or not quarantine.is_dir():
        raise TargetingError("target quarantine must be a real directory")
    expected = {TARGET_METADATA_FILENAME}
    if metadata["mode"] in {TARGET_AWARE_MODE, TASK_VISIBLE_TARGET_MODE}:
        expected.add(TARGET_INPUT_FILENAME)
    try:
        actual = {entry.name for entry in quarantine.iterdir()}
    except OSError as exc:
        raise TargetingError("cannot inspect target quarantine") from exc
    if actual != expected:
        raise TargetingError("target quarantine contains unexpected or missing files")


def _existing_registration(root: Path, quarantine: Path,
                           metadata_path: Path) -> dict[str, Any] | None:
    if not quarantine.exists() and not quarantine.is_symlink():
        return None
    metadata = _read_metadata(metadata_path)
    _validate_quarantine(quarantine, metadata)
    return metadata


def _write_registration(root: Path, metadata: Mapping[str, Any],
                        raw_instruction: bytes | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    quarantine = root / TARGET_INPUT_DIRNAME
    if quarantine.exists() or quarantine.is_symlink():
        raise TargetingError("lineage target registration appeared concurrently")

    staging = Path(tempfile.mkdtemp(prefix="._target_input.", dir=root))
    try:
        os.chmod(staging, 0o700)
        if raw_instruction is not None:
            statement_path = staging / TARGET_INPUT_FILENAME
            statement_path.write_bytes(raw_instruction)
            os.chmod(statement_path, 0o600)
        metadata_path = staging / TARGET_METADATA_FILENAME
        metadata_path.write_text(
            json.dumps(dict(metadata), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(metadata_path, 0o600)
        os.rename(staging, quarantine)
    except OSError as exc:
        raise TargetingError(f"cannot register lineage targeting mode: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def register_blind_lineage(root: os.PathLike[str] | str) -> TargetingContext:
    """Register or validate a blind lineage.

    A target-aware root can never be reopened through this API as blind.  A
    previously registered blind root is idempotent.
    """

    root_path, quarantine, statement, metadata_path = _paths(root)
    existing = _existing_registration(root_path, quarantine, metadata_path)
    if existing is not None:
        if existing["mode"] != BLIND_MODE:
            raise TargetingError("target-aware lineage cannot be used in blind mode")
        if statement.exists():
            raise TargetingError("blind lineage contains quarantined target plaintext")
    else:
        # The registry is the durable provenance boundary.  Silently creating
        # one inside an already-populated root would let deletion of a prior
        # target-aware registry relabel its descendants as blind.  Legacy
        # migration therefore needs a separate, explicit procedure; the
        # ordinary runner only registers new empty roots.
        if root_path.exists() and any(root_path.iterdir()):
            raise TargetingError(
                "blind mode requires a new empty or already registered "
                "lineage root")
        metadata = {
            "schema_version": TARGET_METADATA_SCHEMA,
            "mode": BLIND_MODE,
        }
        _write_registration(root_path, metadata, None)
    return TargetingContext(
        BLIND_MODE, "", "", str(quarantine),
        registration_origin=(existing or {}).get("registration_origin", ""))


def register_legacy_blind_lineage(
    root: os.PathLike[str] | str,
    *,
    acknowledgement: str | None,
) -> TargetingContext:
    """Explicitly attest and register a nonempty, pre-registry blind root.

    This is intentionally separate from ordinary blind registration. It is an
    operator-owned migration assertion, not an inference from descendant files.
    """

    _require_legacy_blind_ack(acknowledgement)
    root_path, quarantine, statement, metadata_path = _paths(root)
    existing = _existing_registration(root_path, quarantine, metadata_path)
    if existing is not None:
        if existing["mode"] != BLIND_MODE:
            raise TargetingError(
                "target-aware lineage cannot be migrated to blind mode")
        if statement.exists():
            raise TargetingError(
                "blind lineage contains quarantined target plaintext")
        return TargetingContext(
            BLIND_MODE, "", "", str(quarantine),
            registration_origin=existing.get("registration_origin", ""))
    if not root_path.is_dir() or not any(root_path.iterdir()):
        raise TargetingError(
            "legacy blind migration requires an existing nonempty root; "
            "use ordinary blind registration for a new root")
    metadata = {
        "schema_version": TARGET_METADATA_SCHEMA,
        "mode": BLIND_MODE,
        "registration_origin": LEGACY_BLIND_ORIGIN,
    }
    _write_registration(root_path, metadata, None)
    return TargetingContext(
        BLIND_MODE, "", "", str(quarantine),
        registration_origin=LEGACY_BLIND_ORIGIN)


def register_target_aware_lineage(
    root: os.PathLike[str] | str,
    target_input_file: os.PathLike[str] | str,
    *,
    acknowledgement: str | None,
) -> TargetingContext:
    """Register an immutable target statement for one lineage root."""

    _require_ack(acknowledgement)
    raw, _source_text, source_digest = _read_target_file(target_input_file)
    root_path, quarantine, statement, metadata_path = _paths(root)
    existing = _existing_registration(root_path, quarantine, metadata_path)
    if existing is not None:
        if existing["mode"] != TARGET_AWARE_MODE:
            raise TargetingError("blind lineage cannot be changed to target-aware")
        if existing["instruction_sha256"] != source_digest:
            raise TargetingError("target drift refused for existing lineage")
    else:
        # A target is training data.  Refuse to relabel a pre-existing,
        # unregistered evolution history after seeing it; targeted and blind
        # runs must begin in distinct lineage roots.
        if root_path.exists() and any(root_path.iterdir()):
            raise TargetingError(
                "target-aware mode requires a new empty lineage root")
        metadata = {
            "schema_version": TARGET_METADATA_SCHEMA,
            "mode": TARGET_AWARE_MODE,
            "instruction_sha256": source_digest,
        }
        _write_registration(root_path, metadata, raw)

    try:
        stored_raw = statement.read_bytes()
        stored_text = stored_raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetingError("quarantined target instruction is unreadable") from exc
    stored_digest = hashlib.sha256(stored_raw).hexdigest()
    if stored_digest != source_digest:
        raise TargetingError("quarantined target instruction failed its hash check")
    return TargetingContext(
        TARGET_AWARE_MODE,
        stored_text,
        stored_digest,
        str(quarantine),
    )


def register_task_visible_lineage(
    root: os.PathLike[str] | str,
    target_input_file: os.PathLike[str] | str,
    *,
    task_id: str,
    acknowledgement: str | None,
) -> TargetingContext:
    """Register an immutable query plus its normal solver-visible task setup."""

    _require_task_visible_ack(acknowledgement)
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[0-9]{3}", task_id):
        raise TargetingError("task-visible mode requires an id like task_056")
    raw, _source_text, source_digest = _read_target_file(target_input_file)
    root_path, quarantine, statement, metadata_path = _paths(root)
    existing = _existing_registration(root_path, quarantine, metadata_path)
    if existing is not None:
        if existing["mode"] != TASK_VISIBLE_TARGET_MODE:
            raise TargetingError(
                "lineage cannot be changed to task-visible target mode")
        if (existing["instruction_sha256"] != source_digest
                or existing["task_id"] != task_id):
            raise TargetingError("task-visible target drift refused")
    else:
        if root_path.exists() and any(root_path.iterdir()):
            raise TargetingError(
                "task-visible target mode requires a new empty lineage root")
        metadata = {
            "schema_version": TARGET_METADATA_SCHEMA,
            "mode": TASK_VISIBLE_TARGET_MODE,
            "instruction_sha256": source_digest,
            "task_id": task_id,
        }
        _write_registration(root_path, metadata, raw)

    try:
        stored_raw = statement.read_bytes()
        stored_text = stored_raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetingError("quarantined target instruction is unreadable") from exc
    stored_digest = hashlib.sha256(stored_raw).hexdigest()
    if stored_digest != source_digest:
        raise TargetingError("quarantined target instruction failed its hash check")
    return TargetingContext(
        TASK_VISIBLE_TARGET_MODE, stored_text, stored_digest,
        str(quarantine), task_id=task_id)


def register_lineage(
    root: os.PathLike[str] | str,
    *,
    target_input_file: os.PathLike[str] | str | None = None,
    acknowledgement: str | None = None,
    legacy_blind_acknowledgement: str | None = None,
    task_visible_id: str | None = None,
) -> TargetingContext:
    """Register blind-by-default or explicitly target-aware lineage state."""

    if target_input_file is None:
        if task_visible_id:
            raise TargetingError(
                "task-visible id supplied without target input")
        if acknowledgement:
            raise TargetingError("target acknowledgement supplied without target input")
        if legacy_blind_acknowledgement:
            return register_legacy_blind_lineage(
                root, acknowledgement=legacy_blind_acknowledgement)
        return register_blind_lineage(root)
    if legacy_blind_acknowledgement:
        raise TargetingError(
            "legacy blind acknowledgement cannot be combined with target input")
    if task_visible_id:
        return register_task_visible_lineage(
            root, target_input_file, task_id=task_visible_id,
            acknowledgement=acknowledgement)
    return register_target_aware_lineage(
        root, target_input_file, acknowledgement=acknowledgement)


def read_lineage_metadata(root: os.PathLike[str] | str) -> dict[str, Any]:
    """Return safe, plaintext-free targeting metadata for reports/results."""

    root_path, quarantine, _statement, metadata_path = _paths(root)
    existing = _existing_registration(root_path, quarantine, metadata_path)
    if existing is None:
        raise TargetingError("lineage targeting mode has not been registered")
    return dict(existing)


def load_registered_lineage(
    root: os.PathLike[str] | str,
    *,
    acknowledgement: str | None = None,
) -> TargetingContext:
    """Load a registered lineage, requiring acknowledgement for plaintext."""

    metadata = read_lineage_metadata(root)
    if metadata["mode"] == BLIND_MODE:
        return register_blind_lineage(root)
    if metadata["mode"] == TASK_VISIBLE_TARGET_MODE:
        _require_task_visible_ack(acknowledgement)
    else:
        _require_ack(acknowledgement)
    root_path, quarantine, statement, _metadata_path = _paths(root)
    try:
        raw = statement.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetingError("quarantined target instruction is unreadable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != metadata["instruction_sha256"]:
        raise TargetingError("quarantined target instruction failed its hash check")
    return TargetingContext(
        metadata["mode"], text, digest, str(quarantine),
        task_id=str(metadata.get("task_id", "")))


def validate_lineage_instruction(
    root: os.PathLike[str] | str,
    instruction: str,
) -> dict[str, Any]:
    """Bind an in-memory target to the immutable registration for ``root``.

    This is the non-disclosing check used by lower-level episode callers.  It
    does not load target plaintext for a caller that lacks it; it only proves
    that the bytes already supplied by that caller equal the quarantined bytes.
    """
    metadata = read_lineage_metadata(root)
    if metadata["mode"] not in {TARGET_AWARE_MODE, TASK_VISIBLE_TARGET_MODE}:
        raise TargetingError(
            "a target task requires a registered targeted lineage")
    _root_path, _quarantine, statement, _metadata_path = _paths(root)
    try:
        stored = statement.read_bytes()
        supplied = instruction.encode("utf-8")
    except (OSError, UnicodeEncodeError) as exc:
        raise TargetingError("cannot validate registered target bytes") from exc
    digest = hashlib.sha256(stored).hexdigest()
    if (digest != metadata["instruction_sha256"]
            or not hmac.compare_digest(stored, supplied)):
        raise TargetingError(
            "episode target does not match the registered lineage instruction")
    return dict(metadata)
