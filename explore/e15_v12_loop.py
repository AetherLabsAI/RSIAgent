"""Actor-first, target-gated self-evolution lifecycle for E15 V12.

The immutable target is used only as a normal solver-visible environment.  This
module has no evaluator entry point and consumes no score.  A persistent
Curriculum Agent may author null-task practice projects or request a fresh target
test, but only a fresh Verifier Agent PASS on that target test can converge.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Callable

from explore.charter import (
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
    self_evolving_curriculum_charter,
    self_evolving_target_actor_charter,
    self_evolving_target_verifier_charter,
    self_evolving_verifier_charter,
)
from explore.e15_loop import (
    ACTOR_EXECUTION_EVIDENCE,
    ACTOR_HANDOFF,
    CURRICULUM_HANDOFF,
    CURRICULUM_NOTES,
    DEFAULT_CORPUS,
    LEARNING_DIAGNOSIS,
    PROJECT_ROOT,
    PROJECT_ROOT_NAME,
    VERIFIER_REPORT,
    E15BoundaryError,
    E15Hooks,
    E15InfrastructureError,
    E15Result,
    _ACTOR_TOKEN,
    _V12_CURRICULUM_TOKEN,
    _VERIFIER_TOKEN,
    _actor_runtime_contract,
    _atomic_json,
    _atomic_text,
    _build_actor_execution_evidence,
    _capture_owned_tree,
    _continuation_after_transport,
    _fresh_vm,
    _listing,
    _manifest,
    _parse_unique_handoff,
    _pull_terminal_memory,
    _push_actor_execution_evidence,
    _push_canonical_memory,
    _read_memory_tree,
    _replay,
    _run_free_phase,
    _run_handoff_phase,
    _run_learning_diagnosis,
    _safe_memory_name,
    _verify_actor_execution_evidence,
    _verify_candidate,
    _curriculum_runtime_contract,
)
from explore.e15_state import (
    EVENT_LEDGER_FILENAME,
    E15StateError,
    read_event_ledger,
    tree_sha256,
)
from explore.provision7 import MANIFEST, MATERIALS, ROOTS


TARGET_CANDIDATE_ROOT_NAME = "e15_target_candidate"
TARGET_CANDIDATE_ROOT = f"/home/user/{TARGET_CANDIDATE_ROOT_NAME}"
PRACTICE_FIXTURE_EVIDENCE = "/home/user/.e15_original_project"
_TARGET_SURFACES: dict[str, dict[str, Any]] = {
    "task_003": {
        "inputs": (
            "/home/user/Desktop/city.zip",
            "/home/user/Desktop/filter.zip",
            "/home/user/Desktop/weather_of_hongkong.pptx",
        ),
        # The presentation is a supplied editable document and the final saved
        # output. Composite filenames depend on the Actor-selected source numbers;
        # the unified runner evaluates the live target VM and does not use this
        # legacy single-file capture surface for those dynamic PNGs.
        "output_file": "/home/user/Desktop/weather_of_hongkong.pptx",
        "output_dir": None,
    },
    "task_019": {
        "inputs": (
            "/home/user/Desktop/Chiikawa_episode1.mp4",
            "/home/user/Desktop/Chiikawa_episode2.mp4",
            "/home/user/Desktop/Chiikawa_episode3.mp4",
            "/home/user/Desktop/caption_requirement.pdf",
        ),
        "output_file": "/home/user/Desktop/Chiikawa.mp4",
        "output_dir": None,
        "candidate_outputs": (
            "/home/user/Desktop/Chiikawa_episode1_watermarked.mp4",
            "/home/user/Desktop/Chiikawa_episode2_watermarked.mp4",
            "/home/user/Desktop/Chiikawa_episode3_watermarked.mp4",
            "/home/user/Desktop/Chiikawa.mp4",
            "/home/user/Desktop/cream_soup_w_scripts_EN.mp4",
            "/home/user/Desktop/cream_soup_w_scripts_EN.srt",
        ),
    },
    "task_042": {
        "inputs": (
            "/home/user/Desktop/raw_materials/A_roll.mp4",
            "/home/user/Desktop/raw_materials/BGM.mp3",
            "/home/user/Desktop/raw_materials/Logo.png",
        ),
        "output_file": None,
        "output_dir": "/home/user/Desktop/HoK_Montage",
        "candidate_outputs": ("/home/user/Desktop/HoK_Montage/",),
    },
    "task_044": {
        "inputs": ("/home/user/Desktop/promo_video.mp4",),
        "output_file": "/home/user/Desktop/promo_video_v1.mp4",
        "output_dir": None,
        "candidate_outputs": (
            "/home/user/Desktop/promo_video_v1.mp4",
            "/home/user/Desktop/promo_video.mlt",
        ),
    },
    "task_056": {
        "inputs": (
            "/home/user/Desktop/raw_materials/2325093-hd.mp4",
            "/home/user/Desktop/raw_materials/4370831-hd.mp4",
            "/home/user/Desktop/raw_materials/1284284-hd.mp4",
            "/home/user/Desktop/raw_materials/BGM.mp3",
            "/home/user/Desktop/groundtruth_video.mp4",
        ),
        "output_file": "/home/user/Desktop/OSWorld.mp4",
        "output_dir": "/home/user/Desktop/OSWorld",
    },
    "task_063": {
        "inputs": ("/home/user/Desktop/SiriDemo.pptx",),
        "output_file": "/home/user/Desktop/SiriDemo.pptx",
        "output_dir": None,
    },
    "task_080": {
        "inputs": (
            "/home/user/Desktop/FY26_GTM_Planning_Model_Broken.xlsx",
            "/home/user/Desktop/GTM_Operating_Model_Change_Memo.pdf",
        ),
        "output_file": (
            "/home/user/Desktop/FY26_GTM_Planning_Model_Broken.xlsx"),
        "output_dir": None,
    },
    "task_084": {
        "inputs": (
            "/home/user/Desktop/dry_voice.wav",
            "/home/user/Desktop/music_bed.wav",
            "/home/user/Desktop/processing_spec.txt",
            "/home/user/Desktop/regions.txt",
            "/home/user/Desktop/telephone_autopan.jsfx",
        ),
        "output_file": "/home/user/Desktop/processed_teaser.flac",
        "output_dir": None,
        "candidate_outputs": (
            "/home/user/Desktop/telephone_autopan.jsfx",
            "/home/user/Desktop/processed_teaser.flac",
            "/home/user/Desktop/processed_teaser.render_stats.html",
        ),
    },
    "task_089": {
        "inputs": (
            "/home/user/Desktop/broken_presentation/index.html",
            "/home/user/Desktop/broken_presentation/styles.css",
            "/home/user/Desktop/broken_presentation/slides.json",
            "/home/user/Desktop/broken_presentation/app.js",
            "/home/user/Desktop/broken_presentation/references/reference_slide1.png",
            "/home/user/Desktop/broken_presentation/references/reference_slide2.png",
            "/home/user/Desktop/broken_presentation/assets/fig1.png",
            "/home/user/Desktop/broken_presentation/assets/fig2.png",
            "/home/user/Desktop/broken_presentation/assets/fig3.png",
            "/home/user/Desktop/presentation_requirements.txt",
        ),
        "output_file": None,
        "output_dir": "/home/user/Desktop/broken_presentation",
        "candidate_outputs": (
            "/home/user/Desktop/broken_presentation/",
        ),
    },
    "task_094": {
        "inputs": ("/home/user/Videos/task094_ref.mp4",),
        "output_file": "/home/user/Documents/SolveSpace/part.slvs",
        "output_dir": None,
    },
}

# Public defaults preserve the reviewed t056 API and its existing tests. A
# launcher selects another registered surface once, before any VM or Agent is
# created; one evolution process owns exactly one target.
TARGET_TASK_ID = "task_056"
TARGET_INPUTS = _TARGET_SURFACES[TARGET_TASK_ID]["inputs"]
TARGET_OUTPUT_FILE = _TARGET_SURFACES[TARGET_TASK_ID]["output_file"]
TARGET_OUTPUT_DIR = _TARGET_SURFACES[TARGET_TASK_ID]["output_dir"]


def configure_target_surface(task_id: str) -> None:
    """Select one reviewed task-visible input/output boundary for this process."""

    try:
        surface = _TARGET_SURFACES[task_id]
    except KeyError as exc:
        raise ValueError(f"unsupported V12 target surface: {task_id}") from exc
    global TARGET_TASK_ID, TARGET_INPUTS, TARGET_OUTPUT_FILE, TARGET_OUTPUT_DIR
    TARGET_TASK_ID = task_id
    TARGET_INPUTS = tuple(surface["inputs"])
    TARGET_OUTPUT_FILE = surface["output_file"]
    TARGET_OUTPUT_DIR = surface["output_dir"]


def target_candidate_outputs() -> tuple[str, ...]:
    surface = _TARGET_SURFACES[TARGET_TASK_ID]
    explicit = surface.get("candidate_outputs")
    if explicit is not None:
        return tuple(explicit)
    outputs = []
    if TARGET_OUTPUT_FILE:
        outputs.append(TARGET_OUTPUT_FILE)
    if TARGET_OUTPUT_DIR:
        outputs.append(TARGET_OUTPUT_DIR.rstrip("/") + "/")
    return tuple(outputs)


class E15PublicationError(E15InfrastructureError):
    """Agent-correctable owned-output shape defect, not transport failure."""


def _target_audit_surface(
        target: str, target_visible_inputs: tuple[str, ...] | None = None) -> str:
    """Authorize only the ordinary task-visible input names for V12.

    The legacy exam fence predates task-visible evolution and deliberately
    treats several Task056 asset basenames as suspicious grader constants.
    In V12 those paths are part of the normal solver-visible desktop, so they
    must be bound into the operator-authorized audit surface.  Structural
    benchmark paths, repositories, task ids, evaluator values, and scores
    remain forbidden by the unchanged fence rules.
    """

    visible_inputs = (TARGET_INPUTS if target_visible_inputs is None
                      else tuple(target_visible_inputs))
    if not visible_inputs:
        return target
    return target + "\n\nNORMAL TASK-VISIBLE INPUT PATHS:\n" + \
        "\n".join(visible_inputs)


def _memory_tree_sha256(files: dict[str, bytes]) -> str:
    hashes = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files.items())
    }
    return tree_sha256(hashes)


def _target_input_fingerprint(vm) -> str:
    """Return a canonical solver-visible input fingerprint or fail closed."""

    quoted = " ".join(shlex.quote(path) for path in TARGET_INPUTS)
    out = vm.run_command(
        "set -eu; for p in " + quoted + "; do "
        "test -f \"$p\" && test ! -L \"$p\" || exit 41; "
        "digest=$(sha256sum \"$p\"); digest=${digest%% *}; "
        "printf '%s  %s\\n' \"$p\" \"$digest\"; "
        "done; echo E15_TARGET_INPUTS_RC=$?",
        timeout=180, cap=0) or ""
    if "E15_TARGET_INPUTS_RC=0" not in out:
        raise E15InfrastructureError(
            "task-visible target inputs are missing or non-regular")
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        stripped = line.strip()
        matches = [
            path for path in TARGET_INPUTS
            if stripped.startswith(path + "  ")]
        if len(matches) != 1:
            continue
        path = matches[0]
        digest = stripped[len(path) + 2:]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if path in parsed:
            raise E15InfrastructureError(
                "task-visible target input fingerprint contains duplicates")
        parsed[path] = digest
    if set(parsed) != set(TARGET_INPUTS):
        raise E15InfrastructureError(
            "task-visible target input fingerprint is malformed or incomplete")
    return "\n".join(f"{path}  {parsed[path]}" for path in TARGET_INPUTS) + "\n"


def _stage_original_practice_fixtures(vm) -> None:
    """Publish a read-only comparison view of pre-Actor practice fixtures."""

    command = f"""
set -eu
rm -rf {PRACTICE_FIXTURE_EVIDENCE}
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT}
cp -a {PROJECT_ROOT} {PRACTICE_FIXTURE_EVIDENCE}
chmod -R a-w {PRACTICE_FIXTURE_EVIDENCE}
echo E15_ORIGINAL_FIXTURE_STAGE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "E15_ORIGINAL_FIXTURE_STAGE_RC=0" not in out:
        raise E15InfrastructureError(
            "original practice fixtures could not be staged for verification")


def _verify_original_practice_fixtures(
        hooks: E15Hooks, vm, fixture_dir: Path) -> None:
    """Fail closed if the Verifier mutated its original-fixture evidence."""

    command = f"""
set -eu
test -d {PRACTICE_FIXTURE_EVIDENCE} && \
  test ! -L {PRACTICE_FIXTURE_EVIDENCE}
rm -rf {PROJECT_ROOT}
mv {PRACTICE_FIXTURE_EVIDENCE} {PROJECT_ROOT}
echo E15_ORIGINAL_FIXTURE_RESTORE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "E15_ORIGINAL_FIXTURE_RESTORE_RC=0" not in out:
        raise E15InfrastructureError(
            "original practice fixture evidence could not be restored")
    _verify_candidate(hooks, vm, str(fixture_dir))


def _stage_target_candidate(vm) -> None:
    """Copy only the registered target outputs into a replayable root."""

    file_stage = "file_state=absent"
    if TARGET_OUTPUT_FILE:
        output_file = shlex.quote(TARGET_OUTPUT_FILE)
        file_stage = f"""
file_state=absent
if test -e {output_file} || test -L {output_file}; then
  test -f {output_file} && test ! -L {output_file}
  cp --preserve=mode,timestamps {output_file} \\
    {TARGET_CANDIDATE_ROOT}/payload/output_file
  file_state=file
fi
"""
    dir_stage = "dir_state=absent"
    if TARGET_OUTPUT_DIR:
        output_dir = shlex.quote(TARGET_OUTPUT_DIR)
        dir_stage = f"""
dir_state=absent
if test -e {output_dir} || test -L {output_dir}; then
  test -d {output_dir} && test ! -L {output_dir}
  test -z "$(find {output_dir} -type l -print -quit)"
  test -z "$(find {output_dir} ! -type f ! -type d -print -quit)"
  cp -a {output_dir} {TARGET_CANDIDATE_ROOT}/payload/output_directory
  dir_state=directory
fi
"""
    command = f"""
set -eu
rm -rf {TARGET_CANDIDATE_ROOT}
mkdir -p {TARGET_CANDIDATE_ROOT}/payload
{file_stage}
{dir_stage}
printf '{{"file":"%s","directory":"%s"}}\n' \\
  "$file_state" "$dir_state" > {TARGET_CANDIDATE_ROOT}/CANDIDATE_STATE.json
echo E15_TARGET_STAGE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "E15_TARGET_STAGE_RC=0" not in out:
        raise E15InfrastructureError(
            "target candidate outputs could not be staged safely")


def _require_target_output_shapes(vm) -> None:
    """Separate unsafe Actor-owned outputs from a failed VM command channel."""

    file_check = ""
    if TARGET_OUTPUT_FILE:
        output_file = shlex.quote(TARGET_OUTPUT_FILE)
        file_check = f"""
if test -e {output_file} || test -L {output_file}; then
  test -f {output_file} && test ! -L {output_file} || bad=1
fi
"""
    dir_check = ""
    if TARGET_OUTPUT_DIR:
        output_dir = shlex.quote(TARGET_OUTPUT_DIR)
        dir_check = f"""
if test -e {output_dir} || test -L {output_dir}; then
  test -d {output_dir} && test ! -L {output_dir} || bad=1
  if test "$bad" -eq 0; then
    test -z "$(find {output_dir} -type l -print -quit)" || bad=1
    test -z "$(find {output_dir} ! -type f ! -type d -print -quit)" \\
      || bad=1
  fi
fi
"""
    command = f"""
bad=0
{file_check}
{dir_check}
echo E15_TARGET_SHAPE_RC=$bad
"""
    out = vm.run_command(command, timeout=120, cap=1024) or ""
    if "E15_TARGET_SHAPE_RC=1" in out:
        raise E15PublicationError(
            "target outputs contain a symlink or non-regular entry")
    if "E15_TARGET_SHAPE_RC=0" not in out:
        raise E15InfrastructureError(
            "could not inspect target output shape")


def _require_owned_project_shape(vm) -> None:
    """Require a nonempty ordinary project tree before host transport."""

    command = f"""
bad=0
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT} || bad=1
if test "$bad" -eq 0; then
  test -n "$(find {PROJECT_ROOT} -type f -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} -type l -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} ! -type f ! -type d -print -quit)" || bad=1
fi
echo E15_PROJECT_SHAPE_RC=$bad
"""
    out = vm.run_command(command, timeout=120, cap=1024) or ""
    if "E15_PROJECT_SHAPE_RC=1" in out:
        raise E15PublicationError(
            "owned project root is absent, empty, or unsafe")
    if "E15_PROJECT_SHAPE_RC=0" not in out:
        raise E15InfrastructureError(
            "could not inspect owned project shape")


def _capture_target_candidate(
        hooks: E15Hooks, vm, key: str, host_dir: str,
        audit_target: str) -> None:
    _require_target_output_shapes(vm)
    _stage_target_candidate(vm)
    result = hooks.capture_project(
        vm, key, host_dir, guest_dirs=[TARGET_CANDIDATE_ROOT_NAME])
    if not result.get("ok"):
        raise E15InfrastructureError(
            "target candidate snapshot could not be captured")
    if hooks.audit_captured(
            host_dir, authorized_instruction=audit_target):
        raise E15BoundaryError(
            "target candidate snapshot failed the target boundary")


def _overlay_target_candidate(hooks: E15Hooks, vm, snapshot_dir: str) -> None:
    _replay(hooks, vm, snapshot_dir)
    outputs = [path for path in (TARGET_OUTPUT_FILE, TARGET_OUTPUT_DIR) if path]
    remove_outputs = " ".join(shlex.quote(path) for path in outputs)
    file_overlay = ""
    if TARGET_OUTPUT_FILE:
        output_file = shlex.quote(TARGET_OUTPUT_FILE)
        file_parent = shlex.quote(str(Path(TARGET_OUTPUT_FILE).parent))
        file_overlay = f"""
if grep -q '"file":"file"' "$state"; then
  test -f {TARGET_CANDIDATE_ROOT}/payload/output_file
  mkdir -p {file_parent}
  cp --preserve=mode,timestamps \\
    {TARGET_CANDIDATE_ROOT}/payload/output_file {output_file}
fi
"""
    dir_overlay = ""
    if TARGET_OUTPUT_DIR:
        output_dir = shlex.quote(TARGET_OUTPUT_DIR)
        dir_parent = shlex.quote(str(Path(TARGET_OUTPUT_DIR).parent))
        dir_overlay = f"""
if grep -q '"directory":"directory"' "$state"; then
  test -d {TARGET_CANDIDATE_ROOT}/payload/output_directory
  mkdir -p {dir_parent}
  cp -a {TARGET_CANDIDATE_ROOT}/payload/output_directory {output_dir}
fi
"""
    command = f"""
set -eu
state={TARGET_CANDIDATE_ROOT}/CANDIDATE_STATE.json
test -f "$state" && test ! -L "$state"
rm -rf {remove_outputs}
{file_overlay}
{dir_overlay}
rm -rf {TARGET_CANDIDATE_ROOT}
echo E15_TARGET_OVERLAY_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "E15_TARGET_OVERLAY_RC=0" not in out:
        raise E15InfrastructureError(
            "target candidate snapshot could not be overlaid")


def _verify_live_target_candidate(
        hooks: E15Hooks, vm, snapshot_dir: str) -> None:
    _stage_target_candidate(vm)
    _verify_candidate(hooks, vm, snapshot_dir)
    vm.run_command(f"rm -rf {TARGET_CANDIDATE_ROOT}", timeout=60)


def _fresh_target(
        reset_target_vm: Callable[..., dict[str, Any]], vm, target: str,
        expected_inputs: str | None = None) -> str:
    result = reset_target_vm(vm, target)
    if not result.get("ok"):
        raise E15InfrastructureError(
            "fresh target boundary failed: "
            + str(result.get("error", "unknown")))
    actual = _target_input_fingerprint(vm)
    if expected_inputs is not None and actual != expected_inputs:
        raise E15InfrastructureError(
            "task-visible target inputs drifted across clean resets")
    return actual


def _outcome_text(record: dict[str, Any]) -> str:
    pieces = [
        record["label"],
        "ORIGINAL REQUEST (verbatim):\n" + record["request"],
        "TERMINAL OUTCOME: " + record["terminal_outcome"],
        "VERIFIER REPORT (verbatim):\n" + record["verifier_report"],
        "ACTOR LEARNING DIAGNOSIS (verbatim):\n"
        + record["learning_diagnosis"],
    ]
    return "\n\n".join(pieces)


def _promote_learning(
        *, hooks: E15Hooks, vm, cfg, lineage: Path, episode_dir: Path,
        experience_index: int, before_memory: dict[str, bytes],
        actor_history: list[dict[str, Any]], terminal_outcome: str,
        verifier_report: str, target: str, audit_mode: str,
        emit: Callable[..., None], project_open: bool, corpus_path: str,
        memory_dir: Path, journal_dir: Path, experience_kind: str,
        target_visible_inputs: tuple[str, ...] | None = None) \
        -> tuple[dict[str, bytes], str, list[dict[str, Any]]]:
    audit_target = _target_audit_surface(target, target_visible_inputs)
    emit(
        "MEMORY_PHASE_STARTED", status="in_progress",
        project_open=project_open, memory_phase_open=True,
        payload={
            "experience_index": experience_index,
            "terminal_outcome": terminal_outcome,
            "verifier_report_sha256": hashlib.sha256(
                verifier_report.encode("utf-8")).hexdigest(),
        })
    memory_prompt = self_evolving_actor_memory_distillation_msg(
        terminal_outcome, verifier_report)
    actor_history, _ = _run_free_phase(
        hooks=hooks, vm=vm, cfg=cfg, prompt=memory_prompt, role="ACTOR",
        sink_root=str(episode_dir / "memory_distillation"),
        target=audit_target,
        history=actor_history, audit_mode=audit_mode)
    actor_history, _ = _run_free_phase(
        hooks=hooks, vm=vm, cfg=cfg,
        prompt=self_evolving_actor_memory_reconciliation_msg(),
        role="ACTOR", sink_root=str(episode_dir / "memory_reconciliation"),
        target=audit_target, history=actor_history, audit_mode=audit_mode)
    diagnosis, actor_history = _run_learning_diagnosis(
        hooks=hooks, vm=vm, cfg=cfg, terminal_outcome=terminal_outcome,
        verifier_report=verifier_report,
        sink_root=str(episode_dir / "learning_diagnosis"),
        target=audit_target,
        history=actor_history, audit_mode=audit_mode)
    _atomic_text(episode_dir / "learning_diagnosis.md", diagnosis)

    candidate_memory = _pull_terminal_memory(hooks, vm)
    accepted_memory = hooks.audit_memory(
        candidate_memory, corpus_path,
        str(lineage / "audit_rejects.jsonl"),
        authorized_instruction=audit_target, require_corpus=True)
    if accepted_memory != candidate_memory:
        raise E15BoundaryError("terminal memory failed the target boundary")
    hooks.journal_memory(
        str(journal_dir), experience_index, accepted_memory,
        {"kind": experience_kind, "terminal_outcome": terminal_outcome})
    hooks.install_memory(str(memory_dir), accepted_memory)
    return accepted_memory, diagnosis, actor_history


def _run_target_attempt(
        *, hooks: E15Hooks, reset_target_vm: Callable[..., dict[str, Any]],
        vm, lineage: Path, episode_dir: Path, target: str, actor_cfg,
        verifier_cfg, memory_dir: Path, expected_inputs: str,
        emit: Callable[..., None], event_prefix: str,
        attempt_index: int) -> dict[str, Any]:
    """Run one fresh target Actor and one fresh Verifier on a frozen candidate."""

    before_memory = _read_memory_tree(str(memory_dir))
    audit_target = _target_audit_surface(target)
    frozen_memory_sha256 = _memory_tree_sha256(before_memory)
    _fresh_target(reset_target_vm, vm, target, expected_inputs)
    _push_canonical_memory(hooks, vm, str(memory_dir))
    actor_history: list[dict[str, Any]] = []
    actor_prompt = self_evolving_target_actor_charter(
        target, _listing(before_memory), ACTOR_HANDOFF,
        candidate_outputs=target_candidate_outputs())
    actor_sink = episode_dir / "actor" / "attempt_001"
    emit(
        f"{event_prefix}_ACTOR_STARTED", status="in_progress",
        project_open=True, memory_phase_open=False,
        payload={
            "attempt_index": attempt_index,
            "frozen_memory_tree_sha256": frozen_memory_sha256,
        })

    publication = 0
    actor_continuation = False
    while True:
        actor_decision, actor_history, _ = _run_handoff_phase(
            hooks=hooks, vm=vm, cfg=actor_cfg, prompt=actor_prompt,
            role="ACTOR", handoff_path=ACTOR_HANDOFF,
            token_pattern=_ACTOR_TOKEN, sink_root=str(actor_sink),
            target=audit_target, history=actor_history,
            continuation=actor_continuation, audit_mode="exam")
        actor_continuation = True
        publication += 1
        snapshot_dir = episode_dir / "candidate" / f"publication_{publication:03d}"
        try:
            _capture_target_candidate(
                hooks, vm, f"{event_prefix.lower()}-{publication:03d}",
                str(snapshot_dir), audit_target)
        except E15PublicationError:
            # A missing/unsafe output tree is a publication defect, not a
            # correctness verdict. Keep the exact Actor model context and live
            # workspace so it can repair its own submission transport.
            actor_prompt = _continuation_after_transport(
                "ACTOR", ACTOR_HANDOFF,
                "the target outputs were missing, unsafe, or could not be "
                "snapshotted as ordinary files and directories")
            continue
        if _target_input_fingerprint(vm) == expected_inputs:
            break
        # Changing supplied inputs is a replayability/ownership defect, not a
        # correctness grade. Restore the same submitted outputs over canonical
        # inputs and let the same Actor context republish.
        _fresh_target(reset_target_vm, vm, target, expected_inputs)
        _overlay_target_candidate(hooks, vm, str(snapshot_dir))
        _push_canonical_memory(hooks, vm, str(memory_dir))
        actor_prompt = _continuation_after_transport(
            "ACTOR", ACTOR_HANDOFF,
            "a supplied target input changed; supplied inputs are immutable, "
            "and generated dependencies must be self-contained beneath the "
            "target's output directory")

    _atomic_text(episode_dir / "actor_handoff.md", actor_decision.text)
    execution_evidence = _build_actor_execution_evidence(
        episode_dir / "actor", 1, episode_dir / "actor_execution_evidence")
    actor_handoff_sha256 = hashlib.sha256(
        actor_decision.text.encode("utf-8")).hexdigest()
    candidate_manifest_sha256 = hashlib.sha256(
        (snapshot_dir / MANIFEST).read_bytes()).hexdigest()
    candidate_archive_sha256 = hashlib.sha256(
        (snapshot_dir / MATERIALS).read_bytes()).hexdigest()
    candidate_roots_sha256 = hashlib.sha256(
        (snapshot_dir / ROOTS).read_bytes()).hexdigest()
    candidate_snapshot_path = snapshot_dir.relative_to(lineage).as_posix()

    # The Verifier receives a fresh model context and a fresh canonical target
    # setup with only the frozen Actor outputs overlaid. No Actor memory enters.
    _fresh_target(reset_target_vm, vm, target, expected_inputs)
    _overlay_target_candidate(hooks, vm, str(snapshot_dir))
    _push_actor_execution_evidence(hooks, vm, execution_evidence)
    emit(
        f"{event_prefix}_VERIFIER_STARTED", status="in_progress",
        project_open=True, memory_phase_open=False,
        payload={
            "attempt_index": attempt_index,
            "actor_handoff_sha256": actor_handoff_sha256,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "candidate_archive_sha256": candidate_archive_sha256,
            "candidate_roots_sha256": candidate_roots_sha256,
            "candidate_snapshot_path": candidate_snapshot_path,
            "actor_execution_manifest_sha256":
                execution_evidence["manifest_sha256"],
            "frozen_memory_tree_sha256": frozen_memory_sha256,
        })
    verifier_prompt = self_evolving_target_verifier_charter(
        target, report_path=VERIFIER_REPORT,
        actor_execution_path=ACTOR_EXECUTION_EVIDENCE)
    verifier_decision, _, _ = _run_handoff_phase(
        hooks=hooks, vm=vm, cfg=verifier_cfg, prompt=verifier_prompt,
        role="VERIFIER", handoff_path=VERIFIER_REPORT,
        token_pattern=_VERIFIER_TOKEN,
        sink_root=str(episode_dir / "verifier"), target=audit_target,
        history=[], continuation=False,
        handoff_parser=_parse_unique_handoff, commit_on_publish=True,
        audit_mode="exam")
    _verify_live_target_candidate(hooks, vm, str(snapshot_dir))
    if _target_input_fingerprint(vm) != expected_inputs:
        raise E15InfrastructureError(
            "Verifier phase changed a supplied target input")
    _verify_actor_execution_evidence(vm, execution_evidence)
    _atomic_text(episode_dir / "verifier_report.md", verifier_decision.text)

    # Restore the exact candidate and canonical pre-attempt memory for the
    # same-Actor learning context. This removes Verifier scratch/mutations.
    _fresh_target(reset_target_vm, vm, target, expected_inputs)
    _overlay_target_candidate(hooks, vm, str(snapshot_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    return {
        "terminal_outcome": verifier_decision.token,
        "verifier_report": verifier_decision.text,
        "actor_handoff": actor_decision.text,
        "actor_history": actor_history,
        "before_memory": before_memory,
        "frozen_memory_tree_sha256": frozen_memory_sha256,
        "candidate_dir": str(snapshot_dir),
        "actor_handoff_sha256": actor_handoff_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "candidate_archive_sha256": candidate_archive_sha256,
        "candidate_roots_sha256": candidate_roots_sha256,
        "candidate_snapshot_path": candidate_snapshot_path,
        "actor_execution_manifest_sha256":
            execution_evidence["manifest_sha256"],
    }


def _run_practice_attempt(
        *, hooks: E15Hooks, vm, lineage: Path, episode_dir: Path,
        project_index: int, project: str, fixture_dir: Path, target: str,
        actor_cfg, verifier_cfg, memory_dir: Path,
        emit: Callable[..., None], agentic_verifier_cfg=None,
        target_visible_inputs: tuple[str, ...] | None = None,
        resume_state=None) -> dict[str, Any]:
    """Run the existing one-submission/one-verdict null-task inner loop."""

    if resume_state is not None or (agentic_verifier_cfg is not None and
            getattr(agentic_verifier_cfg, "verifier_unverified_evidence", False)):
        from explore.practice_evidence_recovery import run_practice_with_evidence
        return run_practice_with_evidence(
            hooks=hooks, vm=vm, lineage=lineage, episode_dir=episode_dir,
            project_index=project_index, project=project, fixture_dir=fixture_dir,
            target=target, actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
            memory_dir=memory_dir, emit=emit,
            agentic_verifier_cfg=agentic_verifier_cfg,
            target_visible_inputs=target_visible_inputs, resume_state=resume_state)

    before_memory = _read_memory_tree(str(memory_dir))
    audit_target = _target_audit_surface(target, target_visible_inputs)
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    actor_history: list[dict[str, Any]] = []
    actor_prompt = self_evolving_actor_charter(
        project, _listing(before_memory), ACTOR_HANDOFF) \
        + _actor_runtime_contract()
    emit(
        "ACTOR_PHASE_STARTED", status="in_progress", project_open=True,
        memory_phase_open=False,
        payload={"project_index": project_index, "actor_attempt": 1,
                 "verification_cycle": 0})
    actor_continuation = False
    publication = 0
    while True:
        actor_decision, actor_history, _ = _run_handoff_phase(
            hooks=hooks, vm=vm, cfg=actor_cfg, prompt=actor_prompt,
            role="ACTOR", handoff_path=ACTOR_HANDOFF,
            token_pattern=_ACTOR_TOKEN,
            sink_root=str(episode_dir / "actor" / "attempt_001"),
            target=audit_target, history=actor_history,
            continuation=actor_continuation)
        actor_continuation = True
        publication += 1
        candidate_dir = episode_dir / "candidate" / \
            f"publication_{publication:03d}"
        try:
            _require_owned_project_shape(vm)
            _capture_owned_tree(
                hooks, vm,
                f"ep{project_index:03d}-candidate-{publication:03d}",
                str(candidate_dir), audit_target)
        except E15PublicationError:
            actor_prompt = _continuation_after_transport(
                "ACTOR", ACTOR_HANDOFF,
                f"{PROJECT_ROOT} was absent, empty, unsafe, or not "
                "replayable")
            continue
        break
    _atomic_text(episode_dir / "actor_handoff.md", actor_decision.text)
    execution_evidence = _build_actor_execution_evidence(
        episode_dir / "actor", 1, episode_dir / "actor_execution_evidence")
    emit(
        "VERIFIER_PHASE_STARTED", status="in_progress", project_open=True,
        memory_phase_open=False,
        payload={
            "project_index": project_index,
            "verification_cycle": 1,
            "actor_handoff_sha256": hashlib.sha256(
                actor_decision.text.encode("utf-8")).hexdigest(),
            "actor_execution_manifest_sha256":
                execution_evidence["manifest_sha256"],
        })
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _stage_original_practice_fixtures(vm)
    _replay(hooks, vm, str(candidate_dir))
    if agentic_verifier_cfg is None:
        # Historical E15 path. It remains byte-for-byte available to registered
        # experiments, while the full benchmark opts into the mechanically
        # effect-isolated Agentic Verifier below.
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
        verifier_prompt = self_evolving_verifier_charter(
            project, report_path=VERIFIER_REPORT,
            actor_execution_path=ACTOR_EXECUTION_EVIDENCE,
            original_fixtures_path=PRACTICE_FIXTURE_EVIDENCE)
        verifier_decision, _, _ = _run_handoff_phase(
            hooks=hooks, vm=vm, cfg=verifier_cfg, prompt=verifier_prompt,
            role="VERIFIER", handoff_path=VERIFIER_REPORT,
            token_pattern=_VERIFIER_TOKEN,
            sink_root=str(episode_dir / "verifier"), target=audit_target,
            history=[], continuation=False,
            handoff_parser=_parse_unique_handoff, commit_on_publish=True)
        terminal_outcome = verifier_decision.token
        verifier_report = verifier_decision.text
    else:
        # The Verifier remains a full code-as-policy Agent, but its programs run
        # in the read-only effect-isolated namespace used by the target harness.
        # Actor memory, handoff prose, and execution logs are mechanically hidden;
        # only the authoritative project, candidate, and original fixtures are
        # correctness evidence.
        from core.trace import ArtifactSink
        from core.verifier import VerifierSession, verify_agentic

        if not getattr(agentic_verifier_cfg, "agentic_verifier_config", ""):
            raise E15InfrastructureError(
                "practice Agentic Verifier control config is missing")
        private_paths = tuple(dict.fromkeys(
            tuple(getattr(
                agentic_verifier_cfg, "verifier_private_paths", ()) or ())
            + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)))
        control_cfg = dataclasses.replace(
            agentic_verifier_cfg,
            verifier_evolve_route=False,
            verifier_local_verdict_only=False,
            verifier_hide_actor_memory=True,
            verifier_stage_lifecycle=False,
            verifier_persist_scratch=False,
            verifier_private_paths=private_paths)
        verifier_session = VerifierSession()
        context = (
            f"The candidate project is {PROJECT_ROOT}. The frozen original "
            f"input fixture is {PRACTICE_FIXTURE_EVIDENCE}; use it only as "
            "pre-candidate evidence. Actor-private memory, handoff prose, and "
            "execution logs are hidden by the harness.")
        try:
            verdict, findings = verify_agentic(
                project, vm, control_cfg,
                sink=ArtifactSink(str(episode_dir / "verifier")),
                turn_no=project_index, context=context,
                session=verifier_session,
                wall_budget=control_cfg.wall_clock_secs)
        finally:
            verifier_session.close_executor()
        if verdict not in {"pass", "wrong"}:
            raise E15InfrastructureError(
                "practice Agentic Verifier ended without PASS/FAIL: "
                + str(findings))
        terminal_outcome = "PASS" if verdict == "pass" else "FAIL"
        verifier_report = str(findings)
        # Archive integrity remains auditable, but the evidence is mounted only
        # after the independent Verifier has terminated so it cannot inherit the
        # Actor's narrative or self-checks.
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
    _verify_candidate(hooks, vm, str(candidate_dir))
    _verify_actor_execution_evidence(vm, execution_evidence)
    _verify_original_practice_fixtures(hooks, vm, fixture_dir)
    _atomic_text(episode_dir / "verifier_report.md", verifier_report)
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(candidate_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    return {
        "terminal_outcome": terminal_outcome,
        "verifier_report": verifier_report,
        "actor_handoff": actor_decision.text,
        "actor_history": actor_history,
        "before_memory": before_memory,
    }


def _read_bootstrap_seed_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise E15InfrastructureError(
            f"bootstrap seed artifact is unavailable: {path}")
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise E15InfrastructureError(
            f"bootstrap seed artifact is unreadable: {path}") from exc


def _bootstrap_manifest_tree_sha256(value: Any, *, label: str) -> str:
    """Validate an archived memory manifest and return its canonical tree hash."""

    if not isinstance(value, dict):
        raise E15InfrastructureError(
            f"bootstrap seed {label} is not a memory manifest")
    hashes: dict[str, str] = {}
    for name, metadata in value.items():
        if not isinstance(name, str) or not _safe_memory_name(name):
            raise E15InfrastructureError(
                f"bootstrap seed {label} contains an unsafe memory path")
        if (not isinstance(metadata, dict)
                or set(metadata) != {"bytes", "sha256"}
                or type(metadata.get("bytes")) is not int
                or metadata["bytes"] < 0
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(metadata.get("sha256", "")))
                is None):
            raise E15InfrastructureError(
                f"bootstrap seed {label} contains invalid memory metadata")
        hashes[name] = metadata["sha256"]
    try:
        return tree_sha256(hashes)
    except E15StateError as exc:
        raise E15InfrastructureError(
            f"bootstrap seed {label} has an invalid tree digest") from exc


def _import_completed_bootstrap(
        *, hooks: E15Hooks, source_root: Path, lineage: Path,
        bootstrap_dir: Path, memory_dir: Path, journal_dir: Path,
        target: str, corpus_path: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Import exactly one completed Q0 learning state into a clean lineage."""

    if (source_root.is_symlink() or not source_root.is_dir()
            or source_root.resolve() == lineage.resolve()):
        raise E15InfrastructureError(
            "bootstrap seed must be a different real lineage directory")
    if _read_memory_tree(str(memory_dir)) or any(bootstrap_dir.iterdir()):
        raise E15InfrastructureError(
            "bootstrap seed destination is not a clean empty lineage")
    source_target = _read_bootstrap_seed_text(
        source_root / "_target_input" / "instruction.txt")
    if source_target.encode("utf-8") != target.encode("utf-8"):
        raise E15InfrastructureError(
            "bootstrap seed target differs from the immutable target")

    outcome_path = source_root / "bootstrap" / "q000" / "outcome.json"
    try:
        outcome = json.loads(_read_bootstrap_seed_text(outcome_path))
    except json.JSONDecodeError as exc:
        raise E15InfrastructureError(
            "bootstrap seed outcome is not valid JSON") from exc
    if not isinstance(outcome, dict):
        raise E15InfrastructureError("bootstrap seed outcome is not an object")
    required = {
        "schema_version": 2,
        "kind": "target_bootstrap",
        "label": "IMMUTABLE TARGET BOOTSTRAP Q0",
        "request": target,
        "terminal_outcome": "FAIL",
    }
    if any(outcome.get(key) != value for key, value in required.items()):
        raise E15InfrastructureError(
            "bootstrap seed is not one completed failed Q0")
    before_memory_sha = _bootstrap_manifest_tree_sha256(
        outcome.get("memory_before"), label="memory_before")
    after_memory_sha = _bootstrap_manifest_tree_sha256(
        outcome.get("memory_after"), label="memory_after")
    for key in ("actor_handoff", "verifier_report", "learning_diagnosis"):
        if not isinstance(outcome.get(key), str) or not outcome[key].strip():
            raise E15InfrastructureError(
                f"bootstrap seed lacks nonempty {key}")

    source_memory = _read_memory_tree(str(source_root / "memory"))
    if not source_memory or outcome.get("memory_after") != _manifest(
            source_memory):
        raise E15InfrastructureError(
            "bootstrap seed memory does not match its Q0 outcome")
    artifact_names = {
        "actor_handoff": "actor_handoff.md",
        "verifier_report": "verifier_report.md",
        "learning_diagnosis": "learning_diagnosis.md",
    }
    for key, name in artifact_names.items():
        archived = _read_bootstrap_seed_text(
            source_root / "bootstrap" / "q000" / name)
        if archived != outcome[key]:
            raise E15InfrastructureError(
                f"bootstrap seed {name} differs from its Q0 outcome")

    try:
        rows = read_event_ledger(source_root / EVENT_LEDGER_FILENAME)
    except (E15StateError, OSError) as exc:
        raise E15InfrastructureError(
            "bootstrap seed event ledger is invalid") from exc
    completions = [
        row for row in rows
        if row["event_type"] == "TARGET_BOOTSTRAP_COMPLETED"
        and row["status"] == "completed"
    ]
    memory_sha = _memory_tree_sha256(source_memory)
    diagnosis_sha = hashlib.sha256(
        outcome["learning_diagnosis"].encode("utf-8")).hexdigest()
    if (len(completions) != 1
            or completions[0]["payload"].get("terminal_outcome") != "FAIL"
            or completions[0]["payload"].get(
                "memory_tree_sha256") != memory_sha
            or completions[0]["payload"].get(
                "learning_diagnosis_sha256") != diagnosis_sha):
        raise E15InfrastructureError(
            "bootstrap seed lacks one matching completed Q0 event")
    if after_memory_sha != memory_sha:
        raise E15InfrastructureError(
            "bootstrap seed memory_after tree digest is inconsistent")

    # A nonempty Q0 may only come from one explicit, hash-bound continual
    # import registered before the Q0 began.  The exact pre-Q0 manifest, its
    # tree digest, the source closure linkage, and the event order must all
    # agree.  This permits recovery of a costly completed cross-domain Q0
    # without weakening the legacy genuinely-empty Q0 path.
    continual_rows = [
        row for row in rows
        if row["event_type"] == "CONTINUAL_MEMORY_IMPORTED"]
    source_continual_event_sha = ""
    source_continual_seed_sha = ""
    if outcome["memory_before"]:
        bootstrap_starts = [
            row for row in rows
            if row["event_type"] == "TARGET_BOOTSTRAP_STARTED"]
        if len(continual_rows) != 1 or len(bootstrap_starts) != 1:
            raise E15InfrastructureError(
                "continual bootstrap seed lacks one import and one Q0 start")
        continual = continual_rows[0]
        bootstrap_start = bootstrap_starts[0]
        if (continual["status"] != "completed"
                or any(continual["state"].values())
                or continual["sequence"] >= bootstrap_start["sequence"]
                or bootstrap_start["sequence"] >= completions[0]["sequence"]
                or bootstrap_start["status"] != "in_progress"
                or bootstrap_start["payload"].get(
                    "frozen_memory_tree_sha256") != before_memory_sha):
            raise E15InfrastructureError(
                "continual bootstrap seed has invalid Q0 event ordering")

        continual_seed_text = _read_bootstrap_seed_text(
            source_root / "continual_seed.json")
        try:
            continual_seed = json.loads(continual_seed_text)
        except json.JSONDecodeError as exc:
            raise E15InfrastructureError(
                "bootstrap continual provenance is not valid JSON") from exc
        if not isinstance(continual_seed, dict):
            raise E15InfrastructureError(
                "bootstrap continual provenance is not an object")
        linked_fields = (
            "source_lineage", "source_task_id",
            "source_convergence_event_sha256",
            "source_target_pass_event_sha256", "source_closure_sha256",
            "memory_tree_sha256",
        )
        payload = continual["payload"]
        if (continual_seed.get("schema_version") != 1
                or continual_seed.get("memory_manifest")
                != outcome["memory_before"]
                or continual_seed.get("memory_tree_sha256")
                != before_memory_sha
                or any(payload.get(field) != continual_seed.get(field)
                       for field in linked_fields)
                or not continual_seed.get("source_lineage")
                or not continual_seed.get("source_task_id")):
            raise E15InfrastructureError(
                "bootstrap continual provenance does not bind memory_before")
        hash_fields = (
            "source_target_instruction_sha256",
            "source_convergence_event_sha256",
            "source_target_pass_event_sha256", "source_closure_sha256",
            "source_memory_manifest_sha256", "memory_tree_sha256",
        )
        if any(re.fullmatch(
                r"[0-9a-f]{64}", str(continual_seed.get(field, ""))) is None
               for field in hash_fields):
            raise E15InfrastructureError(
                "bootstrap continual provenance contains an invalid digest")
        source_continual_event_sha = continual["event_sha256"]
        source_continual_seed_sha = hashlib.sha256(
            continual_seed_text.encode("utf-8")).hexdigest()
    elif continual_rows:
        raise E15InfrastructureError(
            "empty-memory bootstrap seed unexpectedly imports continual memory")

    audit_target = _target_audit_surface(target)
    for key in ("actor_handoff", "verifier_report", "learning_diagnosis"):
        if hooks.audit_text(
                outcome[key], mode="exam",
                authorized_instruction=audit_target):
            raise E15BoundaryError(
                f"bootstrap seed {key} failed the target boundary")
    accepted_memory = hooks.audit_memory(
        source_memory, corpus_path, str(lineage / "audit_rejects.jsonl"),
        authorized_instruction=audit_target, require_corpus=True)
    if accepted_memory != source_memory:
        raise E15BoundaryError(
            "bootstrap seed memory failed the target boundary")

    for key, name in artifact_names.items():
        _atomic_text(bootstrap_dir / name, outcome[key])
    _atomic_json(bootstrap_dir / "outcome.json", outcome)
    _atomic_text(bootstrap_dir / "outcome.md", _outcome_text(outcome))
    hooks.journal_memory(
        str(journal_dir), 0, accepted_memory,
        {"kind": "v12-target-bootstrap-import", "terminal_outcome": "FAIL"})
    hooks.install_memory(str(memory_dir), accepted_memory)
    seed_record = {
        "schema_version": 1,
        "source_lineage": source_root.name,
        "source_bootstrap_event_sha256": completions[0]["event_sha256"],
        "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "source_memory_before_tree_sha256": before_memory_sha,
        "memory_tree_sha256": memory_sha,
        "learning_diagnosis_sha256": diagnosis_sha,
    }
    if source_continual_event_sha:
        seed_record.update({
            "source_continual_import_event_sha256":
                source_continual_event_sha,
            "source_continual_seed_sha256": source_continual_seed_sha,
        })
    _atomic_json(lineage / "bootstrap_seed.json", seed_record)
    return outcome, accepted_memory


def e15_v12_evolve(
        vm, root: str, target: str, actor_cfg, verifier_cfg, curriculum_cfg,
        memory_cfg, *, reset_target_vm: Callable[..., dict[str, Any]],
        corpus_path: str = DEFAULT_CORPUS, hooks: E15Hooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        expected_input_manifest_sha256: str = "",
        bootstrap_seed_root: str = "",
        continual_seed_memory: dict[str, bytes] | None = None,
        continual_seed_record: dict[str, Any] | None = None) -> E15Result:
    """Run Q0 bootstrap, adaptive practice, and immutable target-gated stop."""

    hooks = hooks or E15Hooks()
    audit_target = _target_audit_surface(target)
    lineage = Path(root)
    memory_dir = lineage / "memory"
    journal_dir = lineage / "memory_journal"
    episodes_dir = lineage / "episodes"
    target_tests_dir = lineage / "target_tests"
    bootstrap_dir = lineage / "bootstrap" / "q000"
    state_path = lineage / "state.json"
    for path in (lineage, memory_dir, journal_dir, episodes_dir,
                 target_tests_dir, bootstrap_dir):
        path.mkdir(parents=True, exist_ok=True)

    project_index = 0
    experience_index = 0
    learning_experiences = 0
    target_test_index = 0
    curriculum_turn = 0
    curriculum_history: list[dict[str, Any]] = []
    project_summaries: list[str] = []
    latest_outcome = ""
    curriculum_notes = ""

    def emit(event_type: str, *, status: str, project_open: bool,
             memory_phase_open: bool, payload: dict[str, Any]) -> Any:
        if event_sink is None:
            return None
        return event_sink(
            event_type, status=status,
            state={"project_open": project_open,
                   "memory_phase_open": memory_phase_open,
                   "recovery_open": False},
            payload=payload)

    def finish(status: str, reason: str = "", terminal_text: str = "") \
            -> E15Result:
        memory = _read_memory_tree(str(memory_dir))
        _atomic_json(state_path, {
            "schema_version": 2,
            "status": status,
            "projects": project_index,
            "learning_experiences": learning_experiences,
            "target_tests": target_test_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "target_sha256": hashlib.sha256(
                target.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(memory),
            "memory_tree_sha256": _memory_tree_sha256(memory),
        })
        return E15Result(
            status=status, projects=project_index, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            terminal_text=terminal_text, last_project=project_index)

    try:
        if not target.strip():
            raise E15InfrastructureError("immutable target query is empty")
        hooks.validate_corpus(corpus_path)

        expected_inputs = _fresh_target(reset_target_vm, vm, target)
        actual_input_manifest_sha256 = hashlib.sha256(
            expected_inputs.encode("utf-8")).hexdigest()
        if (expected_input_manifest_sha256
                and actual_input_manifest_sha256
                != expected_input_manifest_sha256):
            raise E15InfrastructureError(
                "task-visible inputs differ from the fenced setup")
        if bootstrap_seed_root and continual_seed_memory is not None:
            raise E15InfrastructureError(
                "failed-Q0 recovery and continual memory are mutually exclusive")
        if (continual_seed_memory is None) != (continual_seed_record is None):
            raise E15InfrastructureError(
                "continual memory and its provenance record must travel together")
        if continual_seed_memory is not None:
            # The lineage itself is still registered from an empty Q0. Import
            # cross-domain experience only after EVOLUTION_STARTED, as one
            # explicit hash-bound transition before any Agent is called.
            if _read_memory_tree(str(memory_dir)):
                raise E15InfrastructureError(
                    "continual import requires an empty registered memory tree")
            seed_sha = _memory_tree_sha256(continual_seed_memory)
            if (not continual_seed_memory
                    or continual_seed_record.get("memory_tree_sha256")
                    != seed_sha):
                raise E15InfrastructureError(
                    "continual memory does not match its provenance record")
            accepted_seed = hooks.audit_memory(
                continual_seed_memory, corpus_path,
                str(lineage / "audit_rejects.jsonl"),
                authorized_instruction=audit_target, require_corpus=True)
            if accepted_seed != continual_seed_memory:
                raise E15BoundaryError(
                    "continual memory failed the new target boundary")
            hooks.install_memory(str(memory_dir), accepted_seed)
            persisted_seed = dict(continual_seed_record)
            persisted_seed["schema_version"] = 1
            persisted_seed["memory_manifest"] = _manifest(accepted_seed)
            _atomic_json(lineage / "continual_seed.json", persisted_seed)
            emit(
                "CONTINUAL_MEMORY_IMPORTED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "source_lineage": persisted_seed["source_lineage"],
                    "source_task_id": persisted_seed["source_task_id"],
                    "source_convergence_event_sha256": persisted_seed[
                        "source_convergence_event_sha256"],
                    "source_target_pass_event_sha256": persisted_seed[
                        "source_target_pass_event_sha256"],
                    "source_closure_sha256": persisted_seed[
                        "source_closure_sha256"],
                    "memory_tree_sha256": seed_sha,
                })
        if bootstrap_seed_root:
            bootstrap_record, bootstrap_memory = _import_completed_bootstrap(
                hooks=hooks, source_root=Path(bootstrap_seed_root),
                lineage=lineage, bootstrap_dir=bootstrap_dir,
                memory_dir=memory_dir, journal_dir=journal_dir,
                target=target, corpus_path=corpus_path)
            diagnosis = bootstrap_record["learning_diagnosis"]
            learning_experiences = 1
            latest_outcome = _outcome_text(bootstrap_record)
            project_summaries.append("Q0 target bootstrap: FAIL")
            seed_record = json.loads(
                (lineage / "bootstrap_seed.json").read_text(
                    encoding="utf-8"))
            import_payload = {
                "source_lineage": seed_record["source_lineage"],
                "source_bootstrap_event_sha256":
                    seed_record["source_bootstrap_event_sha256"],
                "terminal_outcome": "FAIL",
                "source_memory_before_tree_sha256":
                    seed_record["source_memory_before_tree_sha256"],
                "memory_tree_sha256": _memory_tree_sha256(
                    bootstrap_memory),
                "learning_diagnosis_sha256": hashlib.sha256(
                    diagnosis.encode("utf-8")).hexdigest(),
            }
            if "source_continual_import_event_sha256" in seed_record:
                import_payload.update({
                    "source_continual_import_event_sha256": seed_record[
                        "source_continual_import_event_sha256"],
                    "source_continual_seed_sha256": seed_record[
                        "source_continual_seed_sha256"],
                })
            emit(
                "TARGET_BOOTSTRAP_IMPORTED", status="completed",
                project_open=False, memory_phase_open=False,
                payload=import_payload)
        else:
            # Q0: the Curriculum does not exist yet. The Actor starts either
            # from genuinely empty memory (scratch Arm A) or from one explicitly
            # registered continual snapshot (Arm B). Its real target outcome
            # then initializes the Curriculum through role-owned evidence.
            q0_memory = _read_memory_tree(str(memory_dir))
            if continual_seed_memory is None and q0_memory:
                raise E15InfrastructureError(
                    "V12 bootstrap requires genuinely empty durable memory")
            q0_memory_sha256 = _memory_tree_sha256(q0_memory)
            emit(
                "TARGET_BOOTSTRAP_STARTED", status="in_progress",
                project_open=True, memory_phase_open=False,
                payload={
                    "target_sha256": hashlib.sha256(
                        target.encode("utf-8")).hexdigest(),
                    "input_manifest_sha256": actual_input_manifest_sha256,
                    "frozen_memory_tree_sha256": q0_memory_sha256,
                })
            emit(
                "TARGET_TEST_STARTED", status="in_progress",
                project_open=True, memory_phase_open=False,
                payload={
                    "target_test_index": 0,
                    "bootstrap": True,
                    "frozen_memory_tree_sha256": q0_memory_sha256,
                })
            bootstrap = _run_target_attempt(
                hooks=hooks, reset_target_vm=reset_target_vm, vm=vm,
                lineage=lineage, episode_dir=bootstrap_dir, target=target,
                actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
                memory_dir=memory_dir, expected_inputs=expected_inputs,
                emit=emit, event_prefix="TARGET_TEST", attempt_index=0)
            if bootstrap["terminal_outcome"] == "PASS":
                if _read_memory_tree(str(memory_dir)) != q0_memory:
                    raise E15InfrastructureError(
                        "registered Q0 memory changed during passing target test")
                pass_event = emit(
                    "TARGET_TEST_PASSED", status="completed",
                    project_open=False, memory_phase_open=False,
                    payload={
                        "target_test_index": 0,
                        "bootstrap": True,
                        "frozen_memory_tree_sha256": q0_memory_sha256,
                        "verifier_report_sha256": hashlib.sha256(
                            bootstrap["verifier_report"].encode(
                                "utf-8")).hexdigest(),
                        "actor_handoff_sha256":
                            bootstrap["actor_handoff_sha256"],
                        "candidate_manifest_sha256":
                            bootstrap["candidate_manifest_sha256"],
                        "candidate_archive_sha256":
                            bootstrap["candidate_archive_sha256"],
                        "candidate_roots_sha256":
                            bootstrap["candidate_roots_sha256"],
                        "candidate_snapshot_path":
                            bootstrap["candidate_snapshot_path"],
                        "actor_execution_manifest_sha256":
                            bootstrap["actor_execution_manifest_sha256"],
                    })
                _atomic_json(bootstrap_dir / "outcome.json", {
                    "schema_version": 2,
                    "kind": "target_bootstrap",
                    "terminal_outcome": "PASS",
                    "frozen_memory_tree_sha256": q0_memory_sha256,
                    "verifier_report": bootstrap["verifier_report"],
                    "target_pass_event_sha256":
                        (pass_event or {}).get("event_sha256", ""),
                })
                return finish(
                    "target_converged",
                    "fresh Verifier PASS on registered-memory immutable Q0",
                    bootstrap["verifier_report"])

            emit(
                "TARGET_TEST_FAILED", status="completed",
                project_open=True, memory_phase_open=False,
                payload={
                    "target_test_index": 0,
                    "bootstrap": True,
                    "frozen_memory_tree_sha256": q0_memory_sha256,
                    "verifier_report_sha256": hashlib.sha256(
                        bootstrap["verifier_report"].encode(
                            "utf-8")).hexdigest(),
                })
            bootstrap_memory, diagnosis, _ = _promote_learning(
                hooks=hooks, vm=vm, cfg=memory_cfg, lineage=lineage,
                episode_dir=bootstrap_dir, experience_index=experience_index,
                before_memory=bootstrap["before_memory"],
                actor_history=bootstrap["actor_history"],
                terminal_outcome=bootstrap["terminal_outcome"],
                verifier_report=bootstrap["verifier_report"], target=target,
                audit_mode="exam", emit=emit, project_open=True,
                corpus_path=corpus_path, memory_dir=memory_dir,
                journal_dir=journal_dir,
                experience_kind="v12-target-bootstrap")
            learning_experiences += 1
            bootstrap_record = {
                "schema_version": 2,
                "kind": "target_bootstrap",
                "label": "IMMUTABLE TARGET BOOTSTRAP Q0",
                "request": target,
                "terminal_outcome": bootstrap["terminal_outcome"],
                "actor_handoff": bootstrap["actor_handoff"],
                "verifier_report": bootstrap["verifier_report"],
                "learning_diagnosis": diagnosis,
                "memory_before": _manifest(bootstrap["before_memory"]),
                "memory_after": _manifest(bootstrap_memory),
            }
            _atomic_json(bootstrap_dir / "outcome.json", bootstrap_record)
            latest_outcome = _outcome_text(bootstrap_record)
            _atomic_text(bootstrap_dir / "outcome.md", latest_outcome)
            project_summaries.append(
                "Q0 target bootstrap: " + bootstrap["terminal_outcome"])
            emit(
                "TARGET_BOOTSTRAP_COMPLETED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "terminal_outcome": bootstrap["terminal_outcome"],
                    "memory_tree_sha256": _memory_tree_sha256(
                        bootstrap_memory),
                    "learning_diagnosis_sha256": hashlib.sha256(
                        diagnosis.encode("utf-8")).hexdigest(),
                })

        while True:
            # Curriculum is persistent but always authors on a fresh null task.
            _fresh_vm(hooks, vm, target)
            curriculum_turn += 1
            curriculum_sink = lineage / "curriculum" / \
                f"turn_{curriculum_turn:03d}"
            curriculum_prompt = self_evolving_curriculum_charter(
                target, project_history="\n".join(project_summaries),
                latest_outcome=latest_outcome,
                curriculum_notes=curriculum_notes,
                handoff_path=CURRICULUM_HANDOFF,
                notes_path=CURRICULUM_NOTES,
                target_gated=True) + _curriculum_runtime_contract()

            while True:
                decision, curriculum_history, _ = _run_handoff_phase(
                    hooks=hooks, vm=vm, cfg=curriculum_cfg,
                    prompt=curriculum_prompt, role="CURRICULUM",
                    handoff_path=CURRICULUM_HANDOFF,
                    token_pattern=_V12_CURRICULUM_TOKEN,
                    sink_root=str(curriculum_sink), target=audit_target,
                    history=curriculum_history,
                    continuation=bool(curriculum_history),
                    require_completed_publication=True)
                notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
                if notes:
                    if hooks.audit_text(
                            notes, mode="practice",
                            authorized_instruction=audit_target):
                        raise E15BoundaryError(
                            "Curriculum notes failed the target boundary")
                    curriculum_notes = notes

                if decision.token == "STALLED":
                    _atomic_text(
                        lineage / "curriculum_terminal.md", decision.text)
                    return finish(
                        "stalled", "Curriculum Agent reported no productive "
                        "new hypothesis", decision.text)

                if decision.token == "READY_FOR_TARGET_TEST":
                    target_test_index += 1
                    test_dir = target_tests_dir / \
                        f"test{target_test_index:03d}"
                    preprobe_memory = _read_memory_tree(str(memory_dir))
                    preprobe_sha = _memory_tree_sha256(preprobe_memory)
                    emit(
                        "TARGET_TEST_STARTED", status="in_progress",
                        project_open=True, memory_phase_open=False,
                        payload={
                            "target_test_index": target_test_index,
                            "frozen_memory_tree_sha256": preprobe_sha,
                            "curriculum_request_sha256": hashlib.sha256(
                                decision.text.encode("utf-8")).hexdigest(),
                        })
                    probe = _run_target_attempt(
                        hooks=hooks, reset_target_vm=reset_target_vm, vm=vm,
                        lineage=lineage, episode_dir=test_dir, target=target,
                        actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
                        memory_dir=memory_dir, expected_inputs=expected_inputs,
                        emit=emit, event_prefix="TARGET_TEST",
                        attempt_index=target_test_index)
                    if probe["frozen_memory_tree_sha256"] != preprobe_sha:
                        raise E15InfrastructureError(
                            "target test did not bind the requested memory")
                    if probe["terminal_outcome"] == "PASS":
                        # Freeze exactly the bytes that the fresh target Actor
                        # read. No post-PASS distillation can change the tested
                        # policy before external evaluation.
                        after = _read_memory_tree(str(memory_dir))
                        if after != preprobe_memory:
                            raise E15InfrastructureError(
                                "host memory changed during passing target test")
                        pass_event = emit(
                            "TARGET_TEST_PASSED", status="completed",
                            project_open=False, memory_phase_open=False,
                            payload={
                                "target_test_index": target_test_index,
                                "frozen_memory_tree_sha256": preprobe_sha,
                                "verifier_report_sha256": hashlib.sha256(
                                    probe["verifier_report"].encode(
                                        "utf-8")).hexdigest(),
                                "actor_handoff_sha256":
                                    probe["actor_handoff_sha256"],
                                "candidate_manifest_sha256":
                                    probe["candidate_manifest_sha256"],
                                "candidate_archive_sha256":
                                    probe["candidate_archive_sha256"],
                                "candidate_roots_sha256":
                                    probe["candidate_roots_sha256"],
                                "candidate_snapshot_path":
                                    probe["candidate_snapshot_path"],
                                "actor_execution_manifest_sha256":
                                    probe[
                                        "actor_execution_manifest_sha256"],
                            })
                        _atomic_json(test_dir / "outcome.json", {
                            "schema_version": 2,
                            "kind": "target_test",
                            "target_test_index": target_test_index,
                            "terminal_outcome": "PASS",
                            "frozen_memory_tree_sha256": preprobe_sha,
                            "verifier_report": probe["verifier_report"],
                            "target_pass_event_sha256": (
                                pass_event or {}).get("event_sha256", ""),
                        })
                        return finish(
                            "target_converged",
                            "fresh Verifier PASS on immutable target",
                            probe["verifier_report"])

                    emit(
                        "TARGET_TEST_FAILED", status="completed",
                        project_open=True, memory_phase_open=False,
                        payload={
                            "target_test_index": target_test_index,
                            "frozen_memory_tree_sha256": preprobe_sha,
                            "verifier_report_sha256": hashlib.sha256(
                                probe["verifier_report"].encode(
                                    "utf-8")).hexdigest(),
                        })
                    experience_index += 1
                    learned, diagnosis, _ = _promote_learning(
                        hooks=hooks, vm=vm, cfg=memory_cfg, lineage=lineage,
                        episode_dir=test_dir,
                        experience_index=experience_index,
                        before_memory=probe["before_memory"],
                        actor_history=probe["actor_history"],
                        terminal_outcome="FAIL",
                        verifier_report=probe["verifier_report"],
                        target=target, audit_mode="exam", emit=emit,
                        project_open=True, corpus_path=corpus_path,
                        memory_dir=memory_dir, journal_dir=journal_dir,
                        experience_kind="v12-target-test-fail")
                    learning_experiences += 1
                    record = {
                        "schema_version": 2,
                        "kind": "target_test",
                        "label": f"IMMUTABLE TARGET TEST {target_test_index}",
                        "request": target,
                        "terminal_outcome": "FAIL",
                        "actor_handoff": probe["actor_handoff"],
                        "verifier_report": probe["verifier_report"],
                        "learning_diagnosis": diagnosis,
                        "memory_before": _manifest(probe["before_memory"]),
                        "memory_after": _manifest(learned),
                    }
                    _atomic_json(test_dir / "outcome.json", record)
                    latest_outcome = _outcome_text(record)
                    _atomic_text(test_dir / "outcome.md", latest_outcome)
                    project_summaries.append(
                        f"immutable target test {target_test_index}: FAIL")
                    emit(
                        "TARGET_TEST_LEARNING_COMPLETED", status="completed",
                        project_open=False, memory_phase_open=False,
                        payload={
                            "target_test_index": target_test_index,
                            "memory_tree_sha256": _memory_tree_sha256(learned),
                            "learning_diagnosis_sha256": hashlib.sha256(
                                diagnosis.encode("utf-8")).hexdigest(),
                        })
                    break

                # PROJECT transport and fixture capture. The Curriculum owns
                # content; the harness owns only replayability and containment.
                if (not decision.body or PROJECT_ROOT not in decision.body):
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"a PROJECT must be nonempty and name {PROJECT_ROOT}")
                    continue
                candidate_index = project_index + 1
                episode_dir = episodes_dir / f"ep{candidate_index:03d}"
                fixture_dir = episode_dir / "fixtures"
                try:
                    _require_owned_project_shape(vm)
                    _capture_owned_tree(
                        hooks, vm, f"ep{candidate_index:03d}-fixtures",
                        str(fixture_dir), audit_target)
                except E15PublicationError:
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"{PROJECT_ROOT} was absent, empty, or not replayable")
                    continue
                project_index = candidate_index
                project = decision.body
                _atomic_text(episode_dir / "project.md", decision.text)
                emit(
                    "PROJECT_OPENED", status="in_progress",
                    project_open=True, memory_phase_open=False,
                    payload={
                        "project_index": project_index,
                        "project_sha256": hashlib.sha256(
                            project.encode("utf-8")).hexdigest(),
                    })
                break

            if decision.token == "READY_FOR_TARGET_TEST":
                # A failed target test completed above and returns directly to
                # the same persistent Curriculum conversation.
                continue

            practice = _run_practice_attempt(
                hooks=hooks, vm=vm, lineage=lineage,
                episode_dir=episode_dir, project_index=project_index,
                project=project, fixture_dir=fixture_dir, target=target,
                actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
                memory_dir=memory_dir, emit=emit)
            experience_index += 1
            learned, diagnosis, _ = _promote_learning(
                hooks=hooks, vm=vm, cfg=memory_cfg, lineage=lineage,
                episode_dir=episode_dir, experience_index=experience_index,
                before_memory=practice["before_memory"],
                actor_history=practice["actor_history"],
                terminal_outcome=practice["terminal_outcome"],
                verifier_report=practice["verifier_report"], target=target,
                audit_mode="practice", emit=emit, project_open=True,
                corpus_path=corpus_path, memory_dir=memory_dir,
                journal_dir=journal_dir,
                experience_kind="v12-practice-project")
            learning_experiences += 1
            record = {
                "schema_version": 2,
                "kind": "practice_project",
                "label": f"PRACTICE PROJECT {project_index}",
                "request": project,
                "terminal_outcome": practice["terminal_outcome"],
                "actor_handoff": practice["actor_handoff"],
                "verifier_report": practice["verifier_report"],
                "learning_diagnosis": diagnosis,
                "memory_before": _manifest(practice["before_memory"]),
                "memory_after": _manifest(learned),
            }
            _atomic_json(episode_dir / "outcome.json", record)
            latest_outcome = _outcome_text(record)
            _atomic_text(episode_dir / "outcome.md", latest_outcome)
            project_summaries.append(
                f"practice project {project_index}: "
                + practice["terminal_outcome"])
            _atomic_json(state_path, {
                "schema_version": 2,
                "status": "running",
                "projects": project_index,
                "learning_experiences": learning_experiences,
                "target_tests": target_test_index,
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(learned),
                "memory_tree_sha256": _memory_tree_sha256(learned),
            })
            emit(
                "PROJECT_CLOSED", status="completed", project_open=False,
                memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": practice["terminal_outcome"],
                    "memory_tree_sha256": _memory_tree_sha256(learned),
                    "learning_diagnosis_sha256": hashlib.sha256(
                        diagnosis.encode("utf-8")).hexdigest(),
                })

    except E15BoundaryError as exc:
        return finish("quarantined", str(exc))
    except E15InfrastructureError as exc:
        return finish("infra", str(exc))
    except Exception as exc:  # noqa: BLE001 - durable inspectable incident
        return finish("infra", f"{type(exc).__name__}: {exc}")


__all__ = [
    "PRACTICE_FIXTURE_EVIDENCE", "TARGET_CANDIDATE_ROOT",
    "TARGET_CANDIDATE_ROOT_NAME", "TARGET_INPUTS", "TARGET_OUTPUT_DIR",
    "TARGET_OUTPUT_FILE", "configure_target_surface", "e15_v12_evolve",
    "target_candidate_outputs",
]
