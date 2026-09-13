"""Agent-owned self-evolving lifecycle (E15, opt-in).

This module is intentionally separate from the registered E7/E10 experiment
loops.  It provides only orchestration and containment:

    persistent Curriculum
      -> one fresh Actor submission
      -> one fresh Verifier verdict (PASS or FAIL)
      -> same Actor drafts and reconciles terminal experience into durable memory
      -> persistent Curriculum searches again

There is no host grade, project-specific preflight, reward, benchmark evaluator,
or host-authored success criterion here. Each handoff carries a small lexical
transport token; every semantic decision belongs to an Agent.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import time
from typing import Any, Callable

from core.actor import PLAIN_JSON_TRANSPORT_NOTE
from core.loop import run_attempt
from core.trace import ArtifactSink
from llm.client import DURABLE_IMAGES_FIELD
from explore import commit as memory_transport
from explore.charter import (
    self_evolving_actor_learning_diagnosis_msg,
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
    self_evolving_curriculum_charter,
    self_evolving_target_actor_charter,
    self_evolving_target_verifier_charter,
    self_evolving_verifier_charter,
)
from explore.provision7 import (
    audit_captured_materials,
    capture_project_materials,
    replay_project_materials,
    verify_project_materials,
)
from tools.exam_fence import audit_text, audit_transcripts


PROJECT_ROOT_NAME = "evolution_project"
PROJECT_ROOT = f"/home/user/{PROJECT_ROOT_NAME}"
CURRICULUM_HANDOFF = "/home/user/project_next.md"
CURRICULUM_NOTES = "/home/user/curriculum_notes.md"
ACTOR_HANDOFF = "/home/user/actor_handoff.md"
VERIFIER_REPORT = "/home/user/verifier_report.md"
LEARNING_DIAGNOSIS = "/home/user/learning_diagnosis.md"
ACTOR_EXECUTION_EVIDENCE = "/home/user/.e15_actor_execution"
DEFAULT_CORPUS = str(
    Path(__file__).resolve().parents[1] / "results/explore/corpus_shingles.json")

_ACTOR_EXECUTION_FILENAMES = frozenset({
    "program.py", "program.sh", "trace.txt", "trace_meta.json", "look.json",
})
_ACTOR_EXECUTION_MANIFEST = "SHA256SUMS"

_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|CONVERGED|STALLED)$")
_PHASE1_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|SATURATED|STALLED)$")
_V12_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_TARGET_TEST|STALLED)$")
_UNIFIED_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_RETRY|STALLED)$")
_ACTOR_TOKEN = re.compile(r"^STATUS: (SUBMIT)$")
_VERIFIER_TOKEN = re.compile(r"^VERDICT: (PASS|FAIL)$")


class E15InfrastructureError(RuntimeError):
    """A transport, VM boundary, or emergency watchdog prevented a ruling."""


class E15BoundaryError(RuntimeError):
    """A target-leakage containment boundary rejected an Agent surface."""


def _scope_prior_curriculum_visuals(
        history: list[dict[str, Any]]) \
        -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """End pixel replay at a project boundary without compacting Agent context.

    Native Look pixels are needed throughout the Curriculum Agent's active project
    decision, but replaying every historical image into every later project eventually
    violates provider image-payload limits. The complete images remain losslessly in
    the raw transcript that first recorded them. This live-context projection removes
    only the private wire attachments after the project boundary; it retains every
    message and every original character, then appends a truthful retrieval note so the
    Agent can freely reissue a Look when old visual evidence is relevant again.
    """

    scoped = []
    archived = []
    for index, original in enumerate(list(history or [])):
        message = dict(original)
        attachments = message.pop(DURABLE_IMAGES_FIELD, [])
        if attachments:
            if (not isinstance(attachments, list)
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get("data"), str)
                           or not isinstance(item.get("sha256"), str)
                           for item in attachments)):
                raise E15InfrastructureError(
                    "persistent Curriculum visual attachment is malformed")
            valid = attachments
            content = message.get("content", "")
            if not isinstance(content, str):
                raise E15InfrastructureError(
                    "persistent Curriculum visual observation is not text")
            message["content"] = (
                content
                + "\n\n[HARNESS PROJECT-BOUNDARY SENSORY ARCHIVE: "
                  f"{len(valid)} exact Look image(s) from the prior project remain "
                  "losslessly stored in that turn's raw transcript but are not "
                  "resent as pixels in this new project context. Reissue a Look "
                  "whenever you judge those pixels relevant.]"
            )
            archived.append({
                "message_index": index,
                "images": len(valid),
                "sha256": [str(item.get("sha256", "")) for item in valid],
            })
        scoped.append(message)
    return scoped, archived


@dataclass(frozen=True)
class ParsedHandoff:
    token: str
    body: str
    text: str


@dataclass
class E15Result:
    status: str
    projects: int
    root: str
    memory_dir: str
    reason: str = ""
    terminal_text: str = ""
    last_project: int = 0


def _reset_null_vm(vm, target_direction: str) -> dict[str, Any]:
    """Cross a fresh null-task boundary and remove only E15-owned surfaces."""

    env = getattr(vm, "env", None)
    reset = getattr(env, "reset", None)
    if not callable(reset):
        return {"ok": False, "error": "VM environment has no reset boundary"}
    try:
        # Forge mutates through its guest controller, outside DesktopEnv.step().
        # Force DesktopEnv's reset optimization onto the actual snapshot path.
        env.is_environment_used = True
        reset(task_config=None)
    except Exception as exc:  # noqa: BLE001 - converted to infrastructure state
        return {"ok": False, "error": f"fresh VM reset failed: {exc}"}
    if hasattr(vm, "_conda"):
        vm._conda = ""
    # DesktopEnv.reset(None) historically leaves the prior task/evaluator
    # fields untouched. V12 alternates task-visible and null-task phases, so
    # scrub those host-only callbacks explicitly at every null boundary.
    for name, value in {
            "task_config": None, "instruction": None, "config": [],
            "user_simulator": None, "evaluator": None, "metric": None,
            "metric_conj": "and", "result_getter": None,
            "expected_getter": None, "metric_options": {}}.items():
        try:
            setattr(env, name, value)
        except Exception:  # noqa: BLE001 - reset boundary fails below if dirty
            return {"ok": False,
                    "error": f"could not scrub stale task field {name}"}
    owned = " ".join((PROJECT_ROOT, CURRICULUM_HANDOFF, CURRICULUM_NOTES,
                      ACTOR_HANDOFF, VERIFIER_REPORT,
                      LEARNING_DIAGNOSIS, ACTOR_EXECUTION_EVIDENCE,
                      "/home/user/.e15_original_project",
                      "/home/user/e15_target_candidate",
                      "/home/user/.memory"))
    out = vm.run_command(
        f"rm -rf {owned}; echo E15_CLEAN_RC=$?", timeout=120) or ""
    if "E15_CLEAN_RC=0" not in out:
        return {"ok": False, "error": "could not clean E15-owned guest state"}
    return {"ok": True}


def _read_guest_text(vm, path: str, max_bytes: int | None = None) -> str:
    """Fetch exact UTF-8 bytes; no implicit size ceiling or truncation."""

    # DesktopEnv's file downloader retries a missing file three times with a
    # five-second pause.  Atomic terminal-publication probes run after every
    # completed Agent program, so probing a report that has not been published
    # yet otherwise adds ~15 seconds to every investigative action.  Skip the
    # downloader only when the live guest positively confirms that no nonempty
    # regular file exists.  An indeterminate command channel still falls back
    # to the existing retrying fetch: transport uncertainty must never create a
    # false "not published" result followed by another stochastic model turn.
    quoted = shlex.quote(path)
    state = vm.run_command(
        "if test -f " + quoted + " && test -s " + quoted + "; then "
        "printf 'E15_FILE_STATE=PRESENT'; else "
        "printf 'E15_FILE_STATE=ABSENT'; fi",
        timeout=10, cap=128) or ""
    if state.strip() == "E15_FILE_STATE=ABSENT":
        return ""

    fetch = getattr(vm, "fetch_file", None)
    if not callable(fetch):
        raise E15InfrastructureError("VM transport cannot fetch handoff files")
    try:
        value = fetch(path, max_bytes=max_bytes)
    except TypeError:
        value = fetch(path)
    data = value[0] if isinstance(value, tuple) else value
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return bytes(data).decode("utf-8", errors="strict")
    except (TypeError, UnicodeDecodeError):
        return ""


def _remove_guest_file(vm, path: str) -> None:
    quoted = shlex.quote(path)
    out = vm.run_command(
        f"rm -f -- {quoted}; test ! -e {quoted}; echo E15_REMOVE_RC=$?",
        timeout=30) or ""
    if "E15_REMOVE_RC=0" not in out:
        raise E15InfrastructureError(f"could not clear stale handoff {path}")


def _remove_incomplete_curriculum_project(vm) -> None:
    """Discard fixtures authored by a Curriculum segment that did not commit."""

    # This is intentionally not a generic recursive-delete helper. Curriculum
    # owns exactly this one bounded guest root, and no unresolved path or glob
    # may widen the deletion target.
    quoted = shlex.quote(PROJECT_ROOT)
    out = vm.run_command(
        f"rm -rf -- {quoted}; test ! -e {quoted}; "
        "echo E15_REMOVE_TREE_RC=$?",
        timeout=120) or ""
    if "E15_REMOVE_TREE_RC=0" not in out:
        raise E15InfrastructureError(
            f"could not discard incomplete Curriculum project {PROJECT_ROOT}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_memory_name(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts)


def _read_memory_tree(path: str) -> dict[str, bytes]:
    root = Path(path)
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise E15InfrastructureError("durable memory root is not a real directory")
    files: dict[str, bytes] = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise E15InfrastructureError(
                f"durable memory contains a symlink: {candidate}")
        if candidate.is_file():
            files[str(candidate.relative_to(root))] = candidate.read_bytes()
    return files


def _fsync_tree(root: Path) -> None:
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file():
            with candidate.open("rb") as handle:
                os.fsync(handle.fileno())
    for candidate in sorted(
            (p for p in root.rglob("*") if p.is_dir()), reverse=True):
        fd = os.open(candidate, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_install_memory(path: str, files: dict[str, bytes]) -> None:
    """Install a complete memory tree with same-filesystem rename recovery."""

    if any(not _safe_memory_name(name) for name in files):
        raise E15InfrastructureError("candidate memory contains an unsafe path")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (destination.exists()
                                    and not destination.is_dir()):
        raise E15InfrastructureError("durable memory root is not replaceable")
    stage = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.stage-", dir=str(destination.parent)))
    backup = destination.parent / (
        f".{destination.name}.backup-{os.getpid()}-{time.time_ns()}")
    moved_old = False
    try:
        for name, data in sorted(files.items()):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _fsync_tree(stage)
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        try:
            os.replace(stage, destination)
        except Exception:
            if moved_old and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        parent_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        # A backup remaining beside an installed destination is only cleanup
        # residue.  If destination is absent it is recovery state and retained.
        if backup.exists() and destination.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _manifest(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    }


def _listing(files: dict[str, bytes]) -> str:
    return "\n".join(f"{name}  {len(data)}B"
                     for name, data in sorted(files.items()))


def _parse_handoff(text: str, token_pattern: re.Pattern[str]) \
        -> ParsedHandoff | None:
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines)
                  if line.strip()), None)
    if first is None:
        return None
    token_line = lines[first].strip()
    match = token_pattern.fullmatch(token_line)
    if not match:
        return None
    body = "\n".join(lines[first + 1:]).strip()
    return ParsedHandoff(match.group(1), body, text)


def _parse_unique_handoff(text: str, token_pattern: re.Pattern[str]) \
        -> ParsedHandoff | None:
    """Parse one unambiguous standalone token outside Markdown code fences.

    Verifier reports are intentionally free-form. Requiring their Agent-owned
    verdict to be moved above a natural title caused a correct terminal report
    to be resampled and silently reversed. This parser recognizes the existing
    lexical token without rewriting a byte or judging the report's semantics.
    """

    lines = text.splitlines()
    matches: list[tuple[int, re.Match[str]]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or line.startswith(("\t", "    ")):
            continue
        match = token_pattern.fullmatch(stripped)
        if match:
            matches.append((index, match))
    if len(matches) != 1:
        return None
    index, match = matches[0]
    body = "\n".join(lines[:index] + lines[index + 1:]).strip()
    return ParsedHandoff(match.group(1), body, text)


def _phase_cfg(base_cfg, required_path: str = ""):
    cfg = copy.deepcopy(base_cfg)
    # These switches define the generic Agent-owned transport. Model identity,
    # sampling, tool access, and emergency ceilings remain exactly configured.
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.agent_decided_stop = True
    cfg.practice_done_requires = required_path
    cfg.history_keep_pairs = 0
    cfg.max_resumes = 0
    return cfg


def _curriculum_runtime_contract() -> str:
    return f"""

GENERIC RUNTIME OWNERSHIP BOUNDARY:
For DECISION: PROJECT, create all non-solution inputs, raw materials, references,
and blank workspace for the proposed experience inside {PROJECT_ROOT}. The owned
root must exist and contain at least one file when you declare done, and the
natural-language project handoff must name {PROJECT_ROOT}. The root must not
contain your own completed implementation of the Actor Agent's requested
deliverable. Treat this root as the exact solver-visible publication set, not as
construction scratch space. Every persistent or final Actor Agent deliverable
required by the natural-language project, including any additional saved or
exported copy, must have its destination inside {PROJECT_ROOT}; never require a
persistent or final deliverable at any path outside that root. You remain free to
choose every filename and layout within the root. The harness snapshots and
replays only this root. Create disposable fixture-building intermediates outside
it (for example under /tmp). Before declaring PROJECT, recursively inspect the
root and use your judgment to remove or relocate accidental recipes, partial
assemblies, caches, probes, and other construction debris that the natural-language
project does not intentionally supply to the Actor Agent. This is a
transport/isolation boundary, not a success criterion.
"""


def _actor_runtime_contract() -> str:
    return f"""

GENERIC RUNTIME OWNERSHIP BOUNDARY:
The complete replayable project tree is {PROJECT_ROOT}. Keep supplied project
inputs, editable project state, required project-local dependencies, and final
deliverables inside that root and leave it nonempty. Put disposable application
extractions, caches, reference-frame dumps, probes, and parameter-sweep artifacts
outside it (for example under /tmp), and remove any such debugging debris you
accidentally created inside the root before your terminal handoff. Do not remove
supplied inputs or anything required to replay the submitted project. The harness
snapshots only this root; it does not judge the contents. During project work,
~/.memory is canonical read-only experience: any work-phase changes to it are
discarded. Only the later terminal memory-distillation phase can promote memory.
"""


def _continuation_after_transport(role: str, path: str, detail: str) -> str:
    return f"""{role} AGENT — SAME-CONTEXT TRANSPORT CONTINUATION.

The prior turn did not leave a usable terminal handoff at {path}: {detail}.
This is not a correctness grade and does not change your role or decision. Continue
in this same context, correct the transport/workspace issue, and declare done only
after the complete current handoff is ready. No fixed retry count applies."""


def _continue_after_stall(role: str, path: str = "") -> str:
    suffix = (f" and leave the complete terminal handoff at {path}"
              if path else "")
    return f"""{role} AGENT — SAME-CONTEXT CONTINUATION.

The previous segment ended without your terminal decision. This was not treated
as a submission, verdict, convergence, or learning failure. Continue your current
phase in the same context{suffix}; declare done when you judge the phase complete.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _continue_after_incomplete_curriculum(path: str) -> str:
    return f"""CURRICULUM AGENT — SAME-CONTEXT CONTINUATION.

The previous segment stalled or exhausted its transport budget before you
declared the phase complete. It was not accepted as a Curriculum decision.
Any draft handoff at {path} and incomplete fixture tree at {PROJECT_ROOT} were
discarded. Continue your investigation in the same context. Recreate the
fixture tree and publish the terminal handoff only when you judge the complete
project decision ready, then declare done.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _audit_prompt(hooks: "E15Hooks", text: str, target: str,
                  mode: str = "practice") -> None:
    hits = hooks.audit_text(
        text, mode=mode, authorized_instruction=target)
    if hits:
        raise E15BoundaryError("an Agent prompt failed the target boundary")


def _audit_agent_artifacts(hooks: "E15Hooks", sink_dir: str,
                           target: str, mode: str = "practice") -> None:
    hits = hooks.audit_transcripts(
        sink_dir, mode=mode, authorized_instruction=target)
    if hits:
        # These exact detector records stay beside the host transcript. Never
        # include matched content in a role prompt, memory, or exception message.
        report_path = Path(sink_dir) / "boundary_audit.json"
        reason = "an Agent transcript failed the target boundary"
        try:
            _atomic_json(report_path, {
                "schema_version": 1, "status": "quarantined",
                "surface": "agent_transcripts", "mode": mode, "hits": hits,
            })
        except OSError as exc:
            # Failure to persist optional diagnostics cannot turn a detected
            # boundary violation into a retryable infrastructure outcome.
            raise E15BoundaryError(
                f"{reason}; host audit report could not be written "
                f"({type(exc).__name__})") from exc
        raise E15BoundaryError(f"{reason}; host audit report: {report_path}")


def _run_handoff_phase(
        *, hooks: "E15Hooks", vm, cfg, prompt: str, role: str,
        handoff_path: str, token_pattern: re.Pattern[str], sink_root: str,
        target: str, history: list[dict[str, Any]] | None = None,
        continuation: bool = False,
        handoff_parser: Callable[[str, re.Pattern[str]],
                                 ParsedHandoff | None] = _parse_handoff,
        commit_on_publish: bool = False,
        require_completed_publication: bool = False,
        bounded_transport: bool = False,
        audit_mode: str = "practice") \
        -> tuple[ParsedHandoff, list[dict[str, Any]], Any]:
    """Run until the Agent itself leaves one valid transport token."""

    current_history = list(history or [])
    current_prompt = prompt
    continuing = continuation or bool(current_history)
    segment = 0
    actionless_segments = 0
    used_iters = 0
    used_wall = 0.0
    while True:
        _remove_guest_file(vm, handoff_path)
        _audit_prompt(hooks, current_prompt, target, mode=audit_mode)
        sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        while os.path.exists(sink_dir):
            segment += 1
            sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        phase_cfg = _phase_cfg(cfg, handoff_path)
        budgets = {}
        if require_completed_publication or bounded_transport:
            remaining_iters = phase_cfg.max_iters - used_iters
            remaining_wall = phase_cfg.wall_clock_secs - used_wall
            if remaining_iters <= 0 or remaining_wall <= 0:
                raise E15InfrastructureError(
                    f"{role} handoff exhausted its cumulative transport budget")
            budgets = dict(iters_budget=remaining_iters,
                           wall_budget=remaining_wall)
        terminal_probe = None
        if commit_on_publish:
            terminal_probe = lambda: handoff_parser(
                hooks.read_guest_text(vm, handoff_path), token_pattern
            ) is not None
        result, current_history = hooks.run_attempt(
            current_prompt, vm, phase_cfg, hooks.sink_factory(sink_dir),
            initial_history=current_history,
            continue_context=continuing,
            terminal_handoff_ready=terminal_probe, **budgets)
        if require_completed_publication or bounded_transport:
            used_iters += int(getattr(result, "iters", 0))
            used_wall += float(getattr(result, "wall_secs", 0.0))
            actionless_segments = (
                0 if (getattr(result, "programs_run", 0)
                      or getattr(result, "looks", 0))
                else actionless_segments + 1)
            if dataclasses.is_dataclass(result):
                _atomic_json(Path(sink_dir) / "result.json",
                             dataclasses.asdict(result))
        _audit_agent_artifacts(
            hooks, sink_dir, target, mode=audit_mode)
        status = str(getattr(result, "status", "infra"))
        # Read a published handoff before interpreting the loop status. A valid
        # canonical publication is the Agent's terminal action and survives a
        # coincident stall/budget boundary; no extra model sample may rewrite it.
        text = hooks.read_guest_text(vm, handoff_path)
        if text:
            hits = hooks.audit_text(
                text, mode=audit_mode, authorized_instruction=target)
            if hits:
                raise E15BoundaryError(
                    f"{role} handoff failed the target boundary")
        parsed = handoff_parser(text, token_pattern)
        if (parsed is not None
                and (not require_completed_publication or status == "done")):
            return parsed, current_history, result
        if (require_completed_publication or bounded_transport) and actionless_segments >= 3:
            reason = (f"{role} handoff made no executable progress in "
                      f"{actionless_segments} consecutive segments; "
                      "runtime recovery is required")
            _atomic_json(Path(sink_root) / "transport_stop.json", {
                "status": "infra", "reason": reason,
                "last_segment": Path(sink_dir).name,
                "actionless_segments": actionless_segments,
                "iters": used_iters, "wall_secs": used_wall,
                "semantic_decision": None,
            })
            raise E15InfrastructureError(reason)
        if (require_completed_publication
                and status in {"done", "stalled", "stalled_quiescent",
                               "budget"}):
            # Curriculum may draft its handoff before the fixture is actually
            # ready. Unlike an atomically published Verifier verdict, that
            # draft cannot commit a lifecycle transition unless the Agent also
            # completed its segment. Retry in the same model context from a
            # clean publication workspace.
            _remove_guest_file(vm, handoff_path)
            _remove_incomplete_curriculum_project(vm)
        if status == "done":
            current_prompt = _continuation_after_transport(
                role, handoff_path,
                "it was missing, non-UTF-8, or did not contain an allowed "
                "unambiguous transport token")
        elif status in {"stalled", "stalled_quiescent", "budget"}:
            current_prompt = (
                _continue_after_incomplete_curriculum(handoff_path)
                if require_completed_publication
                else _continue_after_stall(role, handoff_path))
        else:
            raise E15InfrastructureError(
                f"{role} phase ended at emergency/infrastructure status {status}")
        continuing = True
        segment += 1


def _run_free_phase(
        *, hooks: "E15Hooks", vm, cfg, prompt: str, role: str,
        sink_root: str, target: str, history: list[dict[str, Any]],
        required_path: str = "", audit_mode: str = "practice") \
        -> tuple[list[dict[str, Any]], Any]:
    """Run an uncapped same-context phase whose only terminal is Agent done."""

    current_history = list(history)
    current_prompt = prompt
    segment = 0
    while True:
        _audit_prompt(hooks, current_prompt, target, mode=audit_mode)
        sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        while os.path.exists(sink_dir):
            segment += 1
            sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        result, current_history = hooks.run_attempt(
            current_prompt, vm, _phase_cfg(cfg, required_path),
            hooks.sink_factory(sink_dir), initial_history=current_history,
            continue_context=True, allow_noop_done=True)
        _audit_agent_artifacts(
            hooks, sink_dir, target, mode=audit_mode)
        status = str(getattr(result, "status", "infra"))
        if status == "done":
            return current_history, result
        if status in {"stalled", "stalled_quiescent", "budget"}:
            current_prompt = _continue_after_stall(role)
            segment += 1
            continue
        raise E15InfrastructureError(
            f"{role} phase ended at emergency/infrastructure status {status}")


def _run_learning_diagnosis(
        *, hooks: "E15Hooks", vm, cfg, terminal_outcome: str,
        verifier_report: str, sink_root: str, target: str,
        history: list[dict[str, Any]], audit_mode: str = "practice") \
        -> tuple[str, list[dict[str, Any]]]:
    """Publish the Actor's free-form causal handoff without imposing a schema."""

    current_history = list(history)
    prompt = self_evolving_actor_learning_diagnosis_msg(
        terminal_outcome, verifier_report, LEARNING_DIAGNOSIS)
    segment = 0
    while True:
        _remove_guest_file(vm, LEARNING_DIAGNOSIS)
        current_history, _ = _run_free_phase(
            hooks=hooks, vm=vm, cfg=cfg, prompt=prompt, role="ACTOR",
            sink_root=os.path.join(sink_root, f"attempt_{segment:03d}"),
            target=target, history=current_history,
            required_path=LEARNING_DIAGNOSIS, audit_mode=audit_mode)
        diagnosis = hooks.read_guest_text(vm, LEARNING_DIAGNOSIS)
        if diagnosis.strip():
            if hooks.audit_text(
                    diagnosis, mode=audit_mode,
                    authorized_instruction=target):
                raise E15BoundaryError(
                    "Actor learning diagnosis failed the target boundary")
            return diagnosis, current_history
        prompt = _continuation_after_transport(
            "ACTOR", LEARNING_DIAGNOSIS,
            "the free-form diagnosis was missing, empty, or non-UTF-8")
        segment += 1


def _fresh_vm(hooks: "E15Hooks", vm, target: str) -> None:
    result = hooks.reset_vm(vm, target)
    if not result.get("ok"):
        raise E15InfrastructureError(
            "fresh VM boundary failed: " + str(result.get("error", "unknown")))


def _replay(hooks: "E15Hooks", vm, snapshot_dir: str) -> None:
    result = hooks.replay_project(vm, snapshot_dir)
    if not result.get("ok"):
        raise E15InfrastructureError(
            "project snapshot replay failed: "
            + str(result.get("error") or result.get("mismatches") or "unknown"))


def _verify_candidate(hooks: "E15Hooks", vm, snapshot_dir: str) -> None:
    """Fail closed if live candidate files differ from the Actor snapshot."""
    result = hooks.verify_project(vm, snapshot_dir)
    if not result.get("ok"):
        raise E15InfrastructureError(
            "candidate snapshot integrity verification failed: "
            + str(result.get("error") or result.get("mismatches") or
                  "unknown"))


def _push_canonical_memory(hooks: "E15Hooks", vm,
                           memory_dir: str) -> None:
    if not hooks.push_memory(vm, memory_dir):
        raise E15InfrastructureError("canonical memory push failed")


def _hashed_regular_tree(root: Path) -> dict[str, str]:
    """Describe an exact regular-file/directory tree without following links."""

    if root.is_symlink() or not root.is_dir():
        raise E15InfrastructureError("evidence view is not a real directory")
    files: dict[str, str] = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise E15InfrastructureError("evidence view contains a symlink")
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_file():
            files[f"file:{relative}"] = hashlib.sha256(
                candidate.read_bytes()).hexdigest()
        elif candidate.is_dir():
            files[f"dir:{relative}"] = ""
        else:
            raise E15InfrastructureError(
                "evidence view contains a non-regular entry")
    return files


def _build_actor_execution_evidence(
        actor_root: str | Path, through_attempt: int,
        output_dir: str | Path) -> dict[str, Any]:
    """Copy the exact available Actor action record into a durable host view.

    The view deliberately excludes model turns, transcripts, worklogs, and
    harness judgements. It contains only exact submitted programs, returned
    traces and their transport metadata, plus look requests. Preserving the
    original attempt/segment/iteration paths lets a Verifier reason about
    chronology without a host-authored semantic summary.
    """

    if through_attempt < 1:
        raise E15InfrastructureError(
            "Actor execution evidence requires at least one attempt")
    source_root = Path(actor_root)
    destination = Path(output_dir)
    if source_root.is_symlink() or not source_root.is_dir():
        raise E15InfrastructureError(
            "Actor artifact root is not a real directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)))
    entries: list[tuple[str, str, int]] = []
    try:
        for attempt_index in range(1, through_attempt + 1):
            attempt = source_root / f"attempt_{attempt_index:03d}"
            if attempt.is_symlink() or not attempt.is_dir():
                raise E15InfrastructureError(
                    f"Actor artifact attempt is missing: {attempt.name}")
            for walk_root, dirnames, filenames in os.walk(
                    attempt, followlinks=False):
                walk_path = Path(walk_root)
                for dirname in dirnames:
                    if (walk_path / dirname).is_symlink():
                        raise E15InfrastructureError(
                            "Actor artifacts contain a directory symlink")
                for filename in sorted(filenames):
                    source = walk_path / filename
                    if source.is_symlink():
                        raise E15InfrastructureError(
                            "Actor artifacts contain a file symlink")
                    if filename not in _ACTOR_EXECUTION_FILENAMES:
                        continue
                    if not source.is_file():
                        raise E15InfrastructureError(
                            "Actor execution artifact is not a regular file")
                    relative = source.relative_to(source_root)
                    target = temporary / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    data = target.read_bytes()
                    entries.append((
                        relative.as_posix(), hashlib.sha256(data).hexdigest(),
                        len(data)))

        manifest = "".join(
            f"{digest}  {name}\n"
            for name, digest, _size in sorted(entries))
        _atomic_text(temporary / _ACTOR_EXECUTION_MANIFEST, manifest)
        _fsync_tree(temporary)
        if os.path.lexists(destination):
            if _hashed_regular_tree(destination) != \
                    _hashed_regular_tree(temporary):
                raise E15InfrastructureError(
                    "existing Actor execution evidence does not match "
                    "the current attempt chronology")
        else:
            os.replace(temporary, destination)
            parent_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    manifest_bytes = (destination / _ACTOR_EXECUTION_MANIFEST).read_bytes()
    return {
        "path": str(destination),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "files": len(entries),
        "bytes": sum(size for _name, _digest, size in entries),
    }


def _verify_actor_execution_evidence(
        vm, evidence: dict[str, Any],
        guest_path: str = ACTOR_EXECUTION_EVIDENCE) -> None:
    """Fail closed unless the guest view still matches the host manifest."""

    expected = str(evidence["manifest_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise E15InfrastructureError(
            "Actor execution evidence manifest hash is malformed")
    verifier = """import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected_manifest_sha = sys.argv[2]
manifest = root / "SHA256SUMS"
if root.is_symlink() or not root.is_dir():
    raise SystemExit(2)
if manifest.is_symlink() or not manifest.is_file():
    raise SystemExit(3)
raw = manifest.read_bytes()
if hashlib.sha256(raw).hexdigest() != expected_manifest_sha:
    raise SystemExit(4)

expected = {}
for line in raw.decode("utf-8", errors="strict").splitlines():
    if len(line) < 67 or line[64:66] != "  ":
        raise SystemExit(5)
    digest, name = line[:64], line[66:]
    relative = pathlib.PurePosixPath(name)
    if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or not name or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or name == "SHA256SUMS" or name in expected):
        raise SystemExit(6)
    expected[name] = digest

actual_files = {}
actual_dirs = set()
for item in root.rglob("*"):
    if item.is_symlink():
        raise SystemExit(7)
    name = item.relative_to(root).as_posix()
    if item.is_file():
        if name != "SHA256SUMS":
            actual_files[name] = hashlib.sha256(item.read_bytes()).hexdigest()
    elif item.is_dir():
        actual_dirs.add(name)
    else:
        raise SystemExit(8)

expected_dirs = set()
for name in expected:
    parent = pathlib.PurePosixPath(name).parent
    while str(parent) != ".":
        expected_dirs.add(parent.as_posix())
        parent = parent.parent
if actual_files != expected or actual_dirs != expected_dirs:
    raise SystemExit(9)
"""
    root = shlex.quote(guest_path)
    out = vm.run_command(
        f"python3 -c {shlex.quote(verifier)} {root} "
        f"{shlex.quote(expected)}; "
        "echo E15_ACTOR_EVIDENCE_RC=$?",
        timeout=120) or ""
    if "E15_ACTOR_EVIDENCE_RC=0" not in out:
        raise E15InfrastructureError(
            "Actor execution evidence failed guest hash verification")


def _push_actor_execution_evidence(
        hooks: "E15Hooks", vm, evidence: dict[str, Any]) -> None:
    """Transport and byte-verify a harness-owned execution-evidence view."""

    if not hooks.push_dir(
            vm, str(evidence["path"]), ACTOR_EXECUTION_EVIDENCE):
        raise E15InfrastructureError("Actor execution evidence push failed")
    _verify_actor_execution_evidence(vm, evidence)


def _capture_owned_tree(hooks: "E15Hooks", vm, key: str,
                        host_dir: str, target: str) -> None:
    result = hooks.capture_project(
        vm, key, host_dir, guest_dirs=[PROJECT_ROOT_NAME])
    if not result.get("ok"):
        raise E15InfrastructureError(
            "owned project root was absent, empty, or could not be captured")
    findings = hooks.audit_captured(
        host_dir, authorized_instruction=target)
    if findings:
        raise E15BoundaryError("captured project tree failed the target boundary")


def _pull_terminal_memory(hooks: "E15Hooks", vm) -> dict[str, bytes]:
    files = hooks.pull_memory(vm)
    if not isinstance(files, dict) or any(
            not isinstance(name, str) or not isinstance(data, bytes)
            for name, data in files.items()):
        raise E15InfrastructureError("terminal memory pull was malformed")
    if any(not _safe_memory_name(name) for name in files):
        raise E15InfrastructureError("terminal memory contained an unsafe path")
    # Distinguish an intentional empty bank from the transport's historical
    # empty-dict-on-failure sentinel. Bytecode is excluded just as pull_memory
    # excludes it.
    out = vm.run_command(
        "n=$(find /home/user/.memory -type f "
        "! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '*.pyo' "
        "2>/dev/null | wc -l); echo E15_MEMORY_FILES=$n; "
        "test -d /home/user/.memory; echo E15_MEMORY_DIR_RC=$?",
        timeout=60) or ""
    match = re.search(r"E15_MEMORY_FILES=(\d+)", out)
    if ("E15_MEMORY_DIR_RC=0" not in out or match is None
            or int(match.group(1)) != len(files)):
        raise E15InfrastructureError("terminal memory pull was incomplete")
    return files


def _outcome_text(record: dict[str, Any]) -> str:
    """Build the black-box outcome exposed to the Curriculum Agent.

    The lossless host archive retains Actor handoffs and memory manifests for
    research/recovery, but those internal surfaces do not enter curriculum
    context. Curriculum observes only its project, the Verifier's terminal
    signal, and the Verifier's own evidence.
    """
    pieces = [
        f"PROJECT {record['project_index']}",
        "ORIGINAL NATURAL-LANGUAGE PROJECT (verbatim):\n" + record["project"],
        f"TERMINAL OUTCOME: {record['terminal_outcome']}",
    ]
    for index, report in enumerate(record["verifier_reports"], 1):
        pieces.append(f"VERIFIER REPORT {index} (verbatim):\n{report}")
    return "\n\n".join(pieces)


@dataclass
class E15Hooks:
    """Dependency seams for deterministic lifecycle tests and recovery tools."""

    run_attempt: Callable[..., Any] = run_attempt
    sink_factory: Callable[[str], Any] = ArtifactSink
    reset_vm: Callable[..., dict[str, Any]] = _reset_null_vm
    read_guest_text: Callable[..., str] = _read_guest_text
    capture_project: Callable[..., dict[str, Any]] = capture_project_materials
    replay_project: Callable[..., dict[str, Any]] = replay_project_materials
    verify_project: Callable[..., dict[str, Any]] = verify_project_materials
    audit_captured: Callable[..., list[Any]] = audit_captured_materials
    push_memory: Callable[..., bool] = memory_transport.push_memory
    pull_memory: Callable[..., dict[str, bytes]] = memory_transport.pull_memory
    audit_memory: Callable[..., dict[str, bytes]] = memory_transport.silent_audit
    journal_memory: Callable[..., None] = memory_transport.journal
    install_memory: Callable[..., None] = _atomic_install_memory
    validate_corpus: Callable[..., dict[str, Any]] = \
        memory_transport.validate_instruction_corpus
    audit_text: Callable[..., list[Any]] = audit_text
    audit_transcripts: Callable[..., list[Any]] = audit_transcripts
    push_dir: Callable[..., bool] = memory_transport.push_dir


def _resume_completed_phase1_boundary(
        lineage: Path, target_direction: str, *,
        project_budget: int | None, checkpoint_projects: tuple[int, ...],
        phase1_exploration: bool,
        phase1_target_conditioned: bool) -> dict[str, Any]:
    """Recover only a fully closed Phase-1 project boundary.

    Agent work is never reconstructed or regraded here.  The durable memory,
    completed outcome records, and the last lossless Curriculum transcript are
    treated as the recovery witnesses.  Any open/partial next episode fails
    closed instead of being guessed through.
    """

    state_path = lineage / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise E15InfrastructureError(
            "Phase-1 resume requires a real infrastructure state file")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise E15InfrastructureError(
            "Phase-1 resume state is unreadable") from exc
    project_index = state.get("projects")
    if (state.get("status") != "infra"
            or type(project_index) is not int or project_index < 0
            or state.get("last_project") != project_index
            or state.get("phase1_exploration") is not phase1_exploration
            or state.get("phase1_target_conditioned")
            is not phase1_target_conditioned
            or state.get("project_budget") != project_budget
            or state.get("checkpoint_projects")
            != list(checkpoint_projects)
            or state.get("target_sha256") != hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest()):
        raise E15InfrastructureError(
            "Phase-1 resume state does not match the requested protocol")

    memory = _read_memory_tree(str(lineage / "memory"))
    if state.get("memory_manifest") != _manifest(memory):
        raise E15InfrastructureError(
            "Phase-1 resume memory drifted from the infrastructure state")

    episodes = lineage / "episodes"
    project_summaries: list[str] = []
    latest_outcome = ""
    for index in range(1, project_index + 1):
        episode = episodes / f"ep{index:03d}"
        outcome_path = episode / "outcome.json"
        rendered_path = episode / "outcome.md"
        if (episode.is_symlink() or outcome_path.is_symlink()
                or rendered_path.is_symlink()
                or not outcome_path.is_file() or not rendered_path.is_file()):
            raise E15InfrastructureError(
                f"Phase-1 resume is missing closed episode {index}")
        try:
            record = json.loads(outcome_path.read_text(encoding="utf-8"))
            rendered = rendered_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise E15InfrastructureError(
                f"Phase-1 resume episode {index} is unreadable") from exc
        if (record.get("project_index") != index
                or record.get("terminal_outcome") not in {"PASS", "FAIL"}
                or rendered != _outcome_text(record)):
            raise E15InfrastructureError(
                f"Phase-1 resume episode {index} is not a closed outcome")
        project_summaries.append(
            f"project {index}: {record['terminal_outcome']}; "
            "one Verifier investigation")
        latest_outcome = rendered

    partial = episodes / f"ep{project_index + 1:03d}"
    if partial.exists() or partial.is_symlink():
        try:
            has_entries = partial.is_symlink() or any(partial.iterdir())
        except OSError as exc:
            raise E15InfrastructureError(
                "Phase-1 resume cannot inspect the next episode boundary") from exc
        if has_entries:
            raise E15InfrastructureError(
                "Phase-1 resume refuses a partially opened next episode")

    curriculum_root = lineage / "curriculum"
    turns: list[tuple[int, Path]] = []
    if curriculum_root.is_dir() and not curriculum_root.is_symlink():
        for candidate in curriculum_root.iterdir():
            match = re.fullmatch(r"turn_(\d{3})", candidate.name)
            if match and candidate.is_dir() and not candidate.is_symlink():
                turns.append((int(match.group(1)), candidate))
    curriculum_turn = max((number for number, _ in turns), default=0)
    transcripts: list[Path] = []
    for _number, turn in turns:
        transcripts.extend(
            path for path in turn.glob("segment_*/transcript.json")
            if path.is_file() and not path.is_symlink())
    curriculum_history: list[dict[str, Any]] = []
    scoped_visuals: list[dict[str, Any]] = []
    if transcripts:
        transcript = max(transcripts, key=lambda path: path.stat().st_mtime_ns)
        try:
            stored = json.loads(transcript.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise E15InfrastructureError(
                "Phase-1 Curriculum transcript is unreadable") from exc
        messages = stored.get("messages") if isinstance(stored, dict) else None
        if (not isinstance(messages, list)
                or any(not isinstance(item, dict) for item in messages)):
            raise E15InfrastructureError(
                "Phase-1 Curriculum transcript is malformed")
        curriculum_history, scoped_visuals = \
            _scope_prior_curriculum_visuals(messages)
    elif curriculum_turn:
        raise E15InfrastructureError(
            "Phase-1 resume lost the persistent Curriculum context")

    return {
        "project_index": project_index,
        "curriculum_turn": curriculum_turn,
        "curriculum_history": curriculum_history,
        "project_summaries": project_summaries,
        "latest_outcome": latest_outcome,
        "scoped_visuals": scoped_visuals,
    }


def e15_evolve(
        vm, root: str, target_direction: str, actor_cfg, verifier_cfg,
        curriculum_cfg, memory_cfg, *, corpus_path: str = DEFAULT_CORPUS,
        hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        project_budget: int | None = None,
        checkpoint_projects: tuple[int, ...] = (),
        phase1_exploration: bool = False,
        phase1_target_conditioned: bool = False,
        agentic_verifier_cfg=None,
        resume_completed_boundary: bool = False) -> E15Result:
    """Run the agent-owned outer search until Curriculum CONVERGED/STALLED.

    ``target_direction`` is exposed only to the Curriculum prompt. The official
    evaluator is intentionally not an argument and no benchmark score is read or
    produced. Configured iteration/wall ceilings remain emergency infrastructure
    watchdogs; they are never interpreted as convergence or a project outcome.

    ``phase1_exploration`` selects the distribution-guided Phase-1 vocabulary:
    Curriculum may declare ``SATURATED`` rather than claiming task convergence.
    ``phase1_target_conditioned`` keeps that exploration lifecycle but gives the
    Curriculum an exact query from which to derive diverse non-target projects.
    ``project_budget`` is an orchestration budget over complete projects, never an
    Agent turn limit or a semantic success condition. Memory checkpoints are exact,
    uncapped byte trees captured only after committed memory phases. All arguments
    default to the historical E15 behavior.
    """

    hooks = hooks or E15Hooks()
    lineage = Path(root)
    memory_dir = lineage / "memory"
    journal_dir = lineage / "memory_journal"
    episodes_dir = lineage / "episodes"
    checkpoints_dir = lineage / "checkpoints"
    state_path = lineage / "state.json"
    lineage.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    journal_dir.mkdir(parents=True, exist_ok=True)
    episodes_dir.mkdir(parents=True, exist_ok=True)

    if (project_budget is not None
            and (type(project_budget) is not int or project_budget < 0)):
        raise ValueError("project_budget must be a nonnegative integer or None")
    if phase1_target_conditioned and not phase1_exploration:
        raise ValueError(
            "phase1_target_conditioned requires phase1_exploration")
    if any(type(index) is not int or index < 0 for index in checkpoint_projects):
        raise ValueError("checkpoint_projects must contain nonnegative integers")
    checkpoint_projects = tuple(sorted(set(checkpoint_projects)))
    if (project_budget is not None
            and any(index > project_budget for index in checkpoint_projects)):
        raise ValueError("a checkpoint cannot exceed project_budget")

    project_index = 0
    curriculum_turn = 0
    curriculum_history: list[dict[str, Any]] = []
    project_summaries: list[str] = []
    latest_outcome = ""
    curriculum_notes = ""
    resume_notice = ""
    # A resume attempt is not allowed to rewrite the durable state it is still
    # validating.  This matters when the validation itself fails: calling
    # ``finish`` with the not-yet-recovered zero-valued counters would erase a
    # valid completed-project boundary.
    resume_admitted = not resume_completed_boundary

    def checkpoint(project: int) -> None:
        if project not in checkpoint_projects:
            return
        memory = _read_memory_tree(str(memory_dir))
        destination = checkpoints_dir / f"project_{project:03d}"
        if destination.exists() or destination.is_symlink():
            raise E15InfrastructureError(
                f"Phase-1 checkpoint already exists: {destination}")
        destination.mkdir(parents=True)
        hooks.install_memory(str(destination / "memory"), memory)
        manifest = _manifest(memory)
        _atomic_json(destination / "manifest.json", {
            "schema_version": 1,
            "kind": "phase1_memory_checkpoint",
            "project": project,
            "memory_manifest": manifest,
            "memory_manifest_sha256": hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        })
        emit(
            "MEMORY_CHECKPOINT_COMMITTED", status="completed",
            project_open=False, memory_phase_open=False,
            payload={
                "project": project,
                "files": len(memory),
                "bytes": sum(len(data) for data in memory.values()),
                "memory_manifest_sha256": hashlib.sha256(
                    json.dumps(
                        manifest, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
            },
        )

    def emit(event_type: str, *, status: str,
             project_open: bool, memory_phase_open: bool,
             payload: dict[str, Any]) -> None:
        """Persist a conservative phase boundary when a ledger is attached.

        The callback is deliberately transport-only: event names identify the
        active Agent seat while the three generic flags describe recovery
        obligations.  Agent content is represented only by hashes/tokens.
        """
        if event_sink is None:
            return
        event_sink(
            event_type, status=status,
            state={
                "project_open": project_open,
                "memory_phase_open": memory_phase_open,
                "recovery_open": False,
            },
            payload=payload,
        )

    def finish(status: str, reason: str = "", terminal_text: str = "") \
            -> E15Result:
        payload = {
            "schema_version": 1,
            "status": status,
            "projects": project_index,
            "last_project": project_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "phase1_exploration": phase1_exploration,
            "phase1_target_conditioned": phase1_target_conditioned,
            "project_budget": project_budget,
            "checkpoint_projects": list(checkpoint_projects),
            "target_sha256": hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(_read_memory_tree(str(memory_dir))),
        }
        _atomic_json(state_path, payload)
        return E15Result(
            status=status, projects=project_index, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            terminal_text=terminal_text, last_project=project_index)

    def preserve_unadmitted_resume(reason: str) -> E15Result:
        """Report a failed resume without mutating its untrusted checkpoint."""

        persisted_projects = 0
        persisted_last_project = 0
        try:
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            if type(persisted.get("projects")) is int:
                persisted_projects = persisted["projects"]
            if type(persisted.get("last_project")) is int:
                persisted_last_project = persisted["last_project"]
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
        return E15Result(
            status="infra", projects=persisted_projects, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            last_project=persisted_last_project)

    try:
        if not target_direction.strip():
            raise E15InfrastructureError("target direction is empty")
        hooks.validate_corpus(corpus_path)
        _audit_prompt(hooks, target_direction, target_direction)

        if resume_completed_boundary:
            if not phase1_exploration:
                raise E15InfrastructureError(
                    "completed-boundary resume is only defined for Phase 1")
            recovered = _resume_completed_phase1_boundary(
                lineage, target_direction,
                project_budget=project_budget,
                checkpoint_projects=checkpoint_projects,
                phase1_exploration=phase1_exploration,
                phase1_target_conditioned=phase1_target_conditioned)
            resume_admitted = True
            project_index = recovered["project_index"]
            curriculum_turn = recovered["curriculum_turn"]
            curriculum_history = recovered["curriculum_history"]
            project_summaries = recovered["project_summaries"]
            latest_outcome = recovered["latest_outcome"]
            resume_notice = f"""

HARNESS RECOVERY NOTICE:
Infrastructure or transport interrupted the next project after project
{project_index} had already closed. The partial project produced no admitted
correctness outcome and changed no durable memory; its transient environment was
discarded. Continue in your same persistent context: recreate that proposed
experiment or choose another one using your own judgment. This notice is transport
state, not a grade or convergence signal.
"""
            emit(
                "PHASE_RESUMED", status="in_progress",
                project_open=False, memory_phase_open=False,
                payload={
                    "after_project": project_index,
                    "recovered_curriculum_turn": curriculum_turn,
                    "archived_visual_messages": len(
                        recovered.get("scoped_visuals", [])),
                    "archived_visual_images": sum(
                        item["images"]
                        for item in recovered.get("scoped_visuals", [])),
                },
            )
        else:
            checkpoint(0)
        if project_budget is not None and project_index >= project_budget:
            return finish(
                "budget_exhausted",
                "Phase-1 exploration project budget exhausted")

        while True:
            # Curriculum is one persistent conversation but receives a fresh,
            # target-null machine at every outer decision. It may author the next
            # non-solution fixtures on that live VM; capture happens before reset.
            # The canonical memory is mounted only as a disposable evidence copy:
            # Curriculum may inspect it to choose an experience, but only the
            # post-outcome Actor memory phase can change the host-owned corpus.
            _fresh_vm(hooks, vm, target_direction)
            _push_canonical_memory(hooks, vm, str(memory_dir))
            curriculum_turn += 1
            curriculum_sink = lineage / "curriculum" / \
                f"turn_{curriculum_turn:03d}"
            curriculum_prompt = self_evolving_curriculum_charter(
                target_direction,
                project_history="\n".join(project_summaries),
                latest_outcome=latest_outcome,
                curriculum_notes=curriculum_notes,
                handoff_path=CURRICULUM_HANDOFF,
                notes_path=CURRICULUM_NOTES,
                phase1_exploration=phase1_exploration,
                phase1_target_conditioned=phase1_target_conditioned,
            ) + _curriculum_runtime_contract() + resume_notice
            resume_notice = ""

            while True:
                decision, curriculum_history, _ = _run_handoff_phase(
                    hooks=hooks, vm=vm, cfg=curriculum_cfg,
                    prompt=curriculum_prompt, role="CURRICULUM",
                    handoff_path=CURRICULUM_HANDOFF,
                    token_pattern=(
                        _PHASE1_CURRICULUM_TOKEN
                        if phase1_exploration else _CURRICULUM_TOKEN),
                    sink_root=str(curriculum_sink), target=target_direction,
                    history=curriculum_history,
                    continuation=bool(curriculum_history),
                    require_completed_publication=True)
                notes_candidate = hooks.read_guest_text(vm, CURRICULUM_NOTES)
                if notes_candidate:
                    if hooks.audit_text(
                            notes_candidate, mode="practice",
                            authorized_instruction=target_direction):
                        raise E15BoundaryError(
                            "Curriculum notes failed the target boundary")
                    curriculum_notes = notes_candidate

                if decision.token in {"CONVERGED", "SATURATED", "STALLED"}:
                    _atomic_text(lineage / "curriculum_terminal.md",
                                 decision.text)
                    return finish(
                        decision.token.lower(),
                        "Curriculum Agent terminal decision", decision.text)

                if (not decision.body
                        or PROJECT_ROOT not in decision.body):
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"a PROJECT must be nonempty and name {PROJECT_ROOT}")
                    continue

                candidate_index = project_index + 1
                episode_dir = episodes_dir / f"ep{candidate_index:03d}"
                fixture_dir = episode_dir / "fixtures"
                try:
                    _capture_owned_tree(
                        hooks, vm, f"ep{candidate_index:03d}-fixtures",
                        str(fixture_dir), target_direction)
                except E15InfrastructureError:
                    # A missing/empty owned root is a correctable generic
                    # transport issue, not a project grade or convergence signal.
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"{PROJECT_ROOT} was absent, empty, or not replayable")
                    continue
                project_index = candidate_index
                project = decision.body
                # Open conservatively before writing any further project state:
                # a kill after this record must remain visibly recoverable.
                emit(
                    "PROJECT_OPENED", status="in_progress",
                    project_open=True, memory_phase_open=False,
                    payload={
                        "project_index": project_index,
                        "project_sha256": hashlib.sha256(
                            project.encode("utf-8")).hexdigest(),
                    },
                )
                _atomic_text(episode_dir / "project.md", decision.text)
                break

            before_memory = _read_memory_tree(str(memory_dir))
            _fresh_vm(hooks, vm, target_direction)
            _replay(hooks, vm, str(fixture_dir))
            _push_canonical_memory(hooks, vm, str(memory_dir))

            actor_history: list[dict[str, Any]] = []
            actor_handoffs: list[str] = []
            verifier_reports: list[str] = []
            actor_prompt = self_evolving_actor_charter(
                project, _listing(before_memory), ACTOR_HANDOFF) \
                + _actor_runtime_contract()
            actor_continuation = False
            verification_cycle = 0
            # Correctness has exactly one Actor attempt. The surrounding loop
            # exists only to correct a malformed transport/workspace publication
            # in the same context before that one submission is accepted.
            actor_attempt = 1
            terminal_outcome = ""

            emit(
                "ACTOR_PHASE_STARTED", status="in_progress",
                project_open=True, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "actor_attempt": actor_attempt,
                    "verification_cycle": verification_cycle,
                },
            )
            while True:
                actor_decision, actor_history, _ = _run_handoff_phase(
                    hooks=hooks, vm=vm, cfg=actor_cfg,
                    prompt=actor_prompt, role="ACTOR",
                    handoff_path=ACTOR_HANDOFF,
                    token_pattern=_ACTOR_TOKEN,
                    sink_root=str(episode_dir / "actor" /
                                  f"attempt_{actor_attempt:03d}"),
                    target=target_direction, history=actor_history,
                    continuation=actor_continuation)
                actor_continuation = True
                actor_handoffs.append(actor_decision.text)

                candidate_number = verification_cycle + 1
                candidate_dir = episode_dir / "candidates" / \
                    f"cycle_{candidate_number:03d}"
                try:
                    _capture_owned_tree(
                        hooks, vm,
                        f"ep{project_index:03d}-candidate-"
                        f"{candidate_number:03d}",
                        str(candidate_dir), target_direction)
                except E15InfrastructureError:
                    actor_prompt = _continuation_after_transport(
                        "ACTOR", ACTOR_HANDOFF,
                        f"{PROJECT_ROOT} was absent, empty, or not replayable")
                    actor_handoffs.pop()
                    continue
                verification_cycle = candidate_number
                _atomic_text(
                    episode_dir / "handoffs" /
                    f"actor_{verification_cycle:03d}.md",
                    actor_decision.text)

                execution_evidence = _build_actor_execution_evidence(
                    episode_dir / "actor", actor_attempt,
                    episode_dir / "actor_execution_evidence" /
                    f"cycle_{verification_cycle:03d}")
                candidate_manifest = \
                    candidate_dir / "materials.MANIFEST.sha256"
                if candidate_manifest.is_symlink() or \
                        not candidate_manifest.is_file():
                    raise E15InfrastructureError(
                        "candidate snapshot manifest is missing")
                candidate_manifest_sha256 = hashlib.sha256(
                    candidate_manifest.read_bytes()).hexdigest()

                # Fresh machine for Verifier, exact Actor snapshot, and no Actor
                # memory. The separate harness-owned execution view exposes
                # causal actions without Actor reasoning or a host grade.
                # Verifier mutations and the view are thrown away at reset.
                emit(
                    "VERIFIER_PHASE_STARTED", status="in_progress",
                    project_open=True, memory_phase_open=False,
                    payload={
                        "project_index": project_index,
                        "verification_cycle": verification_cycle,
                        "actor_status": actor_decision.token,
                        "actor_handoff_sha256": hashlib.sha256(
                            actor_decision.text.encode("utf-8")).hexdigest(),
                        "actor_execution_manifest_sha256":
                            execution_evidence["manifest_sha256"],
                        "actor_execution_files": execution_evidence["files"],
                        "actor_execution_bytes": execution_evidence["bytes"],
                        "candidate_manifest_sha256":
                            candidate_manifest_sha256,
                    },
                )
                _fresh_vm(hooks, vm, target_direction)
                _replay(hooks, vm, str(candidate_dir))
                if agentic_verifier_cfg is None:
                    # Historical E15 transport remains reproducible. Phase 1 of
                    # the recursive-improvement protocol opts into the clean
                    # candidate-only Agentic Verifier branch below.
                    _push_actor_execution_evidence(
                        hooks, vm, execution_evidence)
                    verifier_prompt = self_evolving_verifier_charter(
                        project, report_path=VERIFIER_REPORT,
                        actor_execution_path=ACTOR_EXECUTION_EVIDENCE)
                    verifier_decision, _, _ = \
                        _run_handoff_phase(
                            hooks=hooks, vm=vm, cfg=verifier_cfg,
                            prompt=verifier_prompt, role="VERIFIER",
                            handoff_path=VERIFIER_REPORT,
                            token_pattern=_VERIFIER_TOKEN,
                            sink_root=str(episode_dir / "verifier" /
                                          f"cycle_{verification_cycle:03d}"),
                            target=target_direction, history=[],
                            continuation=False,
                            handoff_parser=_parse_unique_handoff,
                            commit_on_publish=True)
                else:
                    from core.verifier import VerifierSession, verify_agentic

                    if not getattr(
                            agentic_verifier_cfg,
                            "agentic_verifier_config", ""):
                        raise E15InfrastructureError(
                            "Phase-1 Agentic Verifier control config is missing")
                    private_paths = tuple(dict.fromkeys(
                        tuple(getattr(
                            agentic_verifier_cfg,
                            "verifier_private_paths", ()) or ())
                        + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)))
                    control_cfg = dataclasses.replace(
                        agentic_verifier_cfg,
                        verifier_evolve_route=False,
                        verifier_local_verdict_only=False,
                        verifier_unverified_evidence=False,
                        verifier_failure_starts_evolution=False,
                        verifier_hide_actor_memory=True,
                        verifier_stage_lifecycle=False,
                        verifier_persist_scratch=False,
                        verifier_private_paths=private_paths)
                    verifier_session = VerifierSession()
                    try:
                        verdict, findings = verify_agentic(
                            project, vm, control_cfg,
                            sink=ArtifactSink(str(
                                episode_dir / "verifier" /
                                f"cycle_{verification_cycle:03d}")),
                            turn_no=project_index,
                            context=(
                                f"The candidate project is {PROJECT_ROOT}. "
                                "Actor-private memory, handoff prose, reasoning, "
                                "and execution logs are hidden by the harness. "
                                "Investigate the actual candidate independently."),
                            session=verifier_session,
                            wall_budget=control_cfg.wall_clock_secs)
                    finally:
                        verifier_session.close_executor()
                    if verdict not in {"pass", "wrong"}:
                        raise E15InfrastructureError(
                            "Phase-1 Verifier Agent ended without PASS/FAIL: "
                            + str(findings))
                    token = "PASS" if verdict == "pass" else "FAIL"
                    report = str(findings)
                    verifier_decision = ParsedHandoff(
                        token=token, body=report, text=report)
                    # Preserve the exact execution archive for host audit only
                    # after the independent Verifier Agent has terminated.
                    _push_actor_execution_evidence(
                        hooks, vm, execution_evidence)
                _verify_candidate(hooks, vm, str(candidate_dir))
                _verify_actor_execution_evidence(vm, execution_evidence)

                verifier_reports.append(verifier_decision.text)
                _atomic_text(
                    episode_dir / "handoffs" /
                    f"verifier_{verification_cycle:03d}.md",
                    verifier_decision.text)

                # Restore the Actor's exact pre-verification project and the
                # canonical pre-project memory. This discards both Verifier
                # mutations and any prohibited work-phase memory changes.
                _fresh_vm(hooks, vm, target_direction)
                _replay(hooks, vm, str(candidate_dir))
                _push_canonical_memory(hooks, vm, str(memory_dir))

                # The sole Verifier verdict is the exact terminal outcome for
                # either PASS or FAIL. No repair or confirmation follows it.
                terminal_outcome = verifier_decision.token
                break

            # Only now may memory change. This uses the same Actor context and
            # the exact terminal project state plus final Verifier evidence.
            emit(
                "MEMORY_PHASE_STARTED", status="in_progress",
                project_open=True, memory_phase_open=True,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": terminal_outcome,
                    "verifier_report_sha256": hashlib.sha256(
                        verifier_reports[-1].encode("utf-8")).hexdigest(),
                },
            )
            memory_prompt = self_evolving_actor_memory_distillation_msg(
                terminal_outcome, verifier_reports[-1])
            actor_history, _ = _run_free_phase(
                hooks=hooks, vm=vm, cfg=memory_cfg, prompt=memory_prompt,
                role="ACTOR", sink_root=str(episode_dir / "memory_distillation"),
                target=target_direction, history=actor_history)
            # The first update remains an unpromoted draft in the same live VM.
            # Give the same Actor, with its full history and unchanged memory
            # configuration, one dedicated opportunity to reconcile the whole
            # corpus before the sole pull/audit/journal/install boundary.
            reconciliation_prompt = \
                self_evolving_actor_memory_reconciliation_msg()
            actor_history, _ = _run_free_phase(
                hooks=hooks, vm=vm, cfg=memory_cfg,
                prompt=reconciliation_prompt, role="ACTOR",
                sink_root=str(episode_dir / "memory_reconciliation"),
                target=target_direction, history=actor_history)
            candidate_memory = _pull_terminal_memory(hooks, vm)
            accepted_memory = hooks.audit_memory(
                candidate_memory, corpus_path,
                str(lineage / "audit_rejects.jsonl"),
                authorized_instruction=target_direction, require_corpus=True)
            # A silent audit rejection quarantines the entire candidate. It does
            # not partially rewrite the Actor's meaning and never feeds the leak
            # signal back to an Agent.
            if accepted_memory != candidate_memory:
                raise E15BoundaryError(
                    "terminal memory failed the target boundary")

            hooks.journal_memory(
                str(journal_dir), project_index, accepted_memory,
                {"kind": "e15-terminal-memory",
                 "terminal_outcome": terminal_outcome})
            hooks.install_memory(str(memory_dir), accepted_memory)

            checkpoint(project_index)

            record = {
                "schema_version": 1,
                "project_index": project_index,
                "project": project,
                "terminal_outcome": terminal_outcome,
                "verification_cycles": verification_cycle,
                "actor_handoffs": actor_handoffs,
                "verifier_reports": verifier_reports,
                "memory_before": _manifest(before_memory),
                "memory_after": _manifest(accepted_memory),
            }
            _atomic_json(episode_dir / "outcome.json", record)
            latest_outcome = _outcome_text(record)
            _atomic_text(episode_dir / "outcome.md", latest_outcome)
            project_summaries.append(
                f"project {project_index}: {terminal_outcome}; "
                "one Verifier investigation")
            _atomic_json(state_path, {
                "schema_version": 1,
                "status": "running",
                "projects": project_index,
                "last_project": project_index,
                "target_sha256": hashlib.sha256(
                    target_direction.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(accepted_memory),
            })
            emit(
                "PROJECT_CLOSED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": terminal_outcome,
                    "memory_manifest_sha256": hashlib.sha256(
                        json.dumps(
                            _manifest(accepted_memory), sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                },
            )
            # A closed project's complete Curriculum transcript is already durable.
            # Keep every textual turn and the Agent's reasoning live, while ending
            # automatic pixel replay before the next independent project decision.
            curriculum_history, scoped_visuals = \
                _scope_prior_curriculum_visuals(curriculum_history)
            if scoped_visuals:
                emit(
                    "CURRICULUM_SENSORY_CONTEXT_SCOPED",
                    status="completed", project_open=False,
                    memory_phase_open=False,
                    payload={
                        "after_project": project_index,
                        "archived_visual_messages": len(scoped_visuals),
                        "archived_visual_images": sum(
                            item["images"] for item in scoped_visuals),
                    },
                )
            if (project_budget is not None
                    and project_index >= project_budget):
                return finish(
                    "budget_exhausted",
                    "Phase-1 exploration project budget exhausted")

    except KeyboardInterrupt:
        # External operational stops are infrastructure events.  Persist the
        # last known boundary so a completed-boundary continuation is possible,
        # while still returning control promptly to the caller.
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(
                "KeyboardInterrupt during resume validation")
        return finish("infra", "KeyboardInterrupt: external interruption")
    except E15BoundaryError as exc:
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(str(exc))
        return finish("quarantined", str(exc))
    except E15InfrastructureError as exc:
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(str(exc))
        return finish("infra", str(exc))
    except Exception as exc:  # noqa: BLE001 - persist inspectable infra outcome
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(
                f"{type(exc).__name__}: {exc}")
        return finish("infra", f"{type(exc).__name__}: {exc}")


__all__ = [
    "ACTOR_HANDOFF",
    "ACTOR_EXECUTION_EVIDENCE",
    "CURRICULUM_HANDOFF",
    "CURRICULUM_NOTES",
    "DEFAULT_CORPUS",
    "E15Hooks",
    "E15Result",
    "PROJECT_ROOT",
    "PROJECT_ROOT_NAME",
    "VERIFIER_REPORT",
    "e15_evolve",
]
