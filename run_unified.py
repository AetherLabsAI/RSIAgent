#!/usr/bin/env python3
"""Run the on-demand unified Actor–Verifier and self-evolving loop.

This entrypoint supports only explicitly reviewed, release-bound target-visible
evolution surfaces. The official evaluator is called
exactly once, only after the unified loop returns HANDOFF, and its output never
feeds an Agent or a later evolution cycle.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_env_file,
    resolve_forge_path,
    resolve_forge_root,
    resolve_osworld_root,
)

FORGE_ROOT = resolve_forge_root()
OSWORLD_ROOT = resolve_osworld_root(forge_root=FORGE_ROOT)
RESULTS_ROOT = FORGE_ROOT / "results" / "unified"
EXECUTE_ACK = "RUN-ON-DEMAND-SELF-EVOLVING-LOOP"
SUPPORTED_TASKS = (
    "task_003", "task_019", "task_042", "task_044", "task_056",
    "task_063", "task_080", "task_084", "task_089", "task_094",
)
BENCHMARK_TASK_RELEASE = "osworld-v2-2026.06.24"

DEFAULT_TARGET_CONFIG = (
    FORGE_ROOT / "config/osworld_v2_glm53_k3_agentic_baseline.yaml")
DEFAULT_BENCHMARK_LOCK = (
    FORGE_ROOT / "config/osworld_v2_glm53_k3_baseline.lock.json")
DEFAULT_BENCHMARK_PROFILE = (
    FORGE_ROOT /
    "config/osworld_v2_0624_runtime_0808_compat.lock.json")
DEFAULT_EVOLUTION_CONFIGS = {
    "actor": FORGE_ROOT / "config/glm53_practice_actor.yaml",
    "verifier": FORGE_ROOT / "config/e15_verify.yaml",
    "curriculum": FORGE_ROOT / "config/k3_curriculum.yaml",
    # Memory distillation/reconciliation continues the same Actor context.  Using
    # the same config prevents a silent GLM-5.3 -> legacy GLM-5.2 identity swap.
    "memory": FORGE_ROOT / "config/glm53_practice_actor.yaml",
}
DEFAULT_CORPUS = FORGE_ROOT / "results/explore/corpus_shingles.json"

signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
logging.basicConfig(
    level=logging.INFO, format="[%(levelname)s %(name)s] %(message)s")
log = logging.getLogger("forge.unified")


@dataclasses.dataclass
class _TargetEnvironment:
    desktop: Any
    vm: Any
    boot_secs: float
    baseline: dict[str, Any] | None = None
    surface_baseline: dict[str, str] | None = None


@dataclasses.dataclass
class _TargetVerifier:
    sessions: dict
    baseline_content_sha256: str = ""
    s0_identity_sha256: str = ""
    orientation_report_sha256: str = ""
    orientation_report: str = ""
    active_identity: tuple[str, str] | None = None
    orientation_sequence: int = 0
    orientation_reports: dict[tuple[str, str], str] = dataclasses.field(
        default_factory=dict)
    orientation_report_sha256s: dict[tuple[str, str], str] = \
        dataclasses.field(default_factory=dict)
    orientation_baselines: dict[tuple[str, str], str] = dataclasses.field(
        default_factory=dict)


@dataclasses.dataclass
class _TargetActor:
    memory: dict[str, bytes]
    cycle: int
    worked: bool = False


@dataclasses.dataclass
class _TargetOutput:
    loop_result: Any
    history: list[dict[str, Any]]
    active_history: list[dict[str, Any]]
    active_cfg: Any
    sink_root: str


def _install_paths() -> None:
    if not OSWORLD_ROOT.is_dir():
        raise RuntimeError(
            f"OSWorld-V2 checkout does not exist: {OSWORLD_ROOT}")
    try:
        import dotenv
        dotenv.load_dotenv(OSWORLD_ROOT / ".env", override=False)
    except ImportError:
        pass
    for path in (str(OSWORLD_ROOT), str(FORGE_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(OSWORLD_ROOT)


def _forge_openrouter_key() -> str:
    """Resolve the shared transport credential without exposing it in artifacts."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env_path = resolve_env_file(forge_root=FORGE_ROOT)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "OPENROUTER_API_KEY":
            return value.strip().strip("'\"")
    return ""


def _configure_sealed_evaluator(
        lock_path: Path, target_cfg, target_path: Path) -> dict[str, Any]:
    """Bind the post-HANDOFF evaluator to the same frozen benchmark config.

    OSWorld task evaluators may catch model-client exceptions and translate them
    into zero-valued checks.  Missing evaluator configuration must therefore fail
    before expensive Actor work rather than silently understate the final score.
    The returned record is secret-free and is written to the experiment manifest.
    """
    lock_path = lock_path.expanduser().resolve(strict=True)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    actor = lock.get("actor_agent") or {}
    evaluator = lock.get("evaluator") or {}
    target_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
    if actor.get("model") != target_cfg.model:
        raise RuntimeError("benchmark lock Actor model does not match target config")
    if actor.get("sha256") != target_sha256:
        raise RuntimeError("benchmark lock Actor config hash does not match target config")

    required = (
        "provider", "model", "base_url", "website_host_suffix",
        "retry_attempts", "retry_delay_seconds", "visibility")
    missing = [name for name in required if evaluator.get(name) in (None, "")]
    if missing:
        raise RuntimeError(
            f"benchmark lock evaluator config is incomplete: {missing}")
    if evaluator["visibility"] != "sealed_after_actor_verifier_loop":
        raise RuntimeError("benchmark lock evaluator is not sealed after the loop")
    key = _forge_openrouter_key()
    if not key:
        raise RuntimeError(
            "sealed evaluator credential is absent: configure OPENROUTER_API_KEY")

    # Assign, rather than setdefault, so a stale interactive shell cannot silently
    # drift a release-bound experiment away from its reviewed evaluator config.
    os.environ["OPENROUTER_API_KEY"] = key
    os.environ["WEBSITE_HOST_SUFFIX"] = str(evaluator["website_host_suffix"])
    os.environ["OSWORLD_EVAL_MODEL_PROVIDER"] = str(evaluator["provider"])
    os.environ["OSWORLD_EVAL_MODEL_NAME"] = str(evaluator["model"])
    os.environ["OSWORLD_EVAL_MODEL_BASE_URL"] = str(evaluator["base_url"])
    os.environ["OSWORLD_EVAL_MODEL_API_KEY_ENV"] = "OPENROUTER_API_KEY"
    os.environ["OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS"] = str(
        evaluator["retry_attempts"])
    os.environ["OSWORLD_EVAL_MODEL_RETRY_DELAY"] = str(
        evaluator["retry_delay_seconds"])
    return {
        "lock_path": str(lock_path),
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "provider": str(evaluator["provider"]),
        "model": str(evaluator["model"]),
        "base_url": str(evaluator["base_url"]),
        "retry_attempts": int(evaluator["retry_attempts"]),
        "retry_delay_seconds": float(evaluator["retry_delay_seconds"]),
        "visibility": str(evaluator["visibility"]),
        "credential": "configured",
    }


def _probe_sealed_evaluator_transport() -> dict[str, str]:
    """Make one task-independent authentication probe before Actor work."""
    from desktop_env.evaluators.model_client import generate_text

    response = str(generate_text(
        "Evaluator transport preflight only. Reply exactly READY.",
        options={"max_tokens": 8, "temperature": 0.0})).strip()
    if not response:
        raise RuntimeError("sealed evaluator transport preflight returned empty output")
    return {
        "status": "ready",
        "response_sha256": hashlib.sha256(
            response.encode("utf-8", "replace")).hexdigest(),
    }


def _safe_root(name: str) -> Path:
    if (not name or name in {".", ".."} or "/" in name or "\\" in name
            or not all(ch.isalnum() or ch in "-_." for ch in name)):
        raise ValueError("--root must be one safe basename")
    root = (RESULTS_ROOT / name).resolve()
    if root.parent != RESULTS_ROOT.resolve():
        raise ValueError("--root escapes results/unified")
    return root


def _load_evaluator_task(task_id: str):
    from task_loader import load_task_config, resolve_task_json_path

    task_path = resolve_task_json_path(
        task_id=task_id, base_dir="evaluation_examples", eval_version="v2")
    task = load_task_config(
        task_path, task_id=task_id,
        base_dir="evaluation_examples", eval_version="v2")
    instruction = str(getattr(task, "instruction", None)
                      or task["instruction"])
    return task, instruction


def _load_public_instruction(task_id: str) -> str:
    """Extract only a task's public user instruction; never import its evaluator."""
    from osworld_task_surface import load_public_task_surface

    return load_public_task_surface(OSWORLD_ROOT, task_id).instruction


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args], check=True,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return completed.stdout.strip()


def _task_content_identity(task_dir: Path) -> dict[str, Any]:
    """Return the release-sensitive identity used by the 0808 baseline lock."""

    paths = sorted(task_dir.glob("task_*.py"))
    rows = []
    total_bytes = 0
    for path in paths:
        size = path.stat().st_size
        total_bytes += size
        rows.append(f"{path.name}\t{size}\t{_sha256_file(path)}")
    return {
        "content_tree_sha256": hashlib.sha256(
            "\n".join(rows).encode("utf-8")).hexdigest(),
        "total_bytes": total_bytes,
        "task_count": len(paths),
        "task_ids_sha256": hashlib.sha256(
            "\n".join(path.stem for path in paths).encode("utf-8")).hexdigest(),
    }


def _asset_content_identity(asset_root: Path, marker_name: str) -> dict[str, Any]:
    """Verify the prepared release snapshot, excluding downloader metadata."""

    rows = []
    total_bytes = 0
    for path in sorted(asset_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(asset_root)
        if ((relative.parts and relative.parts[0] == ".cache")
                or relative.as_posix() == marker_name):
            continue
        size = path.stat().st_size
        total_bytes += size
        rows.append(
            f"{relative.as_posix()}\t{size}\t{_sha256_file(path)}")
    return {
        "content_tree_sha256": hashlib.sha256(
            "\n".join(rows).encode("utf-8")).hexdigest(),
        "total_bytes": total_bytes,
        "file_count": len(rows),
    }


def _release_lock_provenance(
        task_id: str, task_class_path: Path, profile_path: Path,
        profile: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on a strict release lock such as the 2026-08-08 lock."""

    if profile.get("schema_version") != 2 \
            or profile.get("benchmark") != "OSWorld-v2":
        raise RuntimeError("unsupported OSWorld release-lock schema")
    release_name = profile.get("benchmark_release")
    if not isinstance(release_name, str) or not release_name:
        raise RuntimeError("OSWorld release lock lacks benchmark_release")

    task_dir = OSWORLD_ROOT / "evaluation_examples" / "task_class"
    observed_names = sorted(path.name for path in task_dir.glob("task_*.py"))
    expected_count = int(profile.get("task_count", 0))
    if len(observed_names) != expected_count:
        raise RuntimeError(
            "OSWorld task count differs from the strict release lock: "
            f"expected={expected_count}, observed={len(observed_names)}")
    identity = _task_content_identity(task_dir)
    task_files = profile.get("task_files") or {}
    for key in ("content_tree_sha256", "total_bytes"):
        if identity[key] != task_files.get(key):
            raise RuntimeError(
                "OSWorld task sources differ from the strict release lock: "
                f"{key} expected={task_files.get(key)!r}, "
                f"observed={identity[key]!r}")
    if identity["task_ids_sha256"] != profile.get("task_ids_sha256"):
        raise RuntimeError("OSWorld task filename set differs from the release lock")

    selected_name = f"{task_id}.py"
    expected_path = (task_dir / selected_name).resolve(strict=True)
    if task_class_path.resolve(strict=True) != expected_path:
        raise RuntimeError("selected task class path escaped the frozen task tree")
    selected_sha256 = _sha256_file(expected_path)

    release_meta = profile.get("release_manifest") or {}
    release_path = (
        OSWORLD_ROOT / str(release_meta.get("path", ""))).resolve(strict=True)
    if _sha256_file(release_path) != release_meta.get("sha256"):
        raise RuntimeError("OSWorld release manifest differs from the release lock")
    release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
    if release_manifest.get("release") != release_name:
        raise RuntimeError("OSWorld release manifest names another release")

    runtime = profile.get("osworld_code") or {}
    runtime_commit = _git_value(OSWORLD_ROOT, "rev-parse", "HEAD")
    if runtime_commit != runtime.get("commit"):
        raise RuntimeError(
            "OSWorld runtime differs from the strict release lock: "
            f"expected={runtime.get('commit')}, observed={runtime_commit}")
    if _git_value(OSWORLD_ROOT, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("OSWorld runtime has tracked worktree changes")

    website = profile.get("website") or {}
    expected_website = website.get("host_suffix")
    if os.environ.get("WEBSITE_HOST_SUFFIX") != expected_website:
        raise RuntimeError(
            "WEBSITE_HOST_SUFFIX does not match the strict release lock")

    assets = profile.get("task_assets") or {}
    asset_value = os.environ.get("OSWORLD_FILE_BASE_URL", "").strip()
    if not asset_value or "://" in asset_value:
        raise RuntimeError(
            "strict release lock requires a local OSWORLD_FILE_BASE_URL")
    asset_root = Path(asset_value).expanduser().resolve(strict=True)
    expected_asset_root = (
        OSWORLD_ROOT / str(assets.get("local_dir", ""))).resolve(strict=True)
    if asset_root != expected_asset_root:
        raise RuntimeError(
            "OSWORLD_FILE_BASE_URL does not name the locked asset snapshot")
    marker_name = str(
        assets.get("release_marker", ".forge_osworld_release.json"))
    marker_path = (asset_root / marker_name).resolve(strict=True)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected_marker = {
        "benchmark_release": release_name,
        "repository": assets.get("repository"),
        "repo_type": assets.get("repo_type"),
        "tag": assets.get("tag"),
        "commit": assets.get("commit"),
    }
    mismatch = {
        key: {"expected": value, "observed": marker.get(key)}
        for key, value in expected_marker.items() if marker.get(key) != value}
    if mismatch:
        raise RuntimeError(
            f"prepared asset marker differs from the release lock: {mismatch}")
    marker_identity = marker.get("content_identity")
    if not isinstance(marker_identity, dict):
        raise RuntimeError("prepared asset marker lacks content_identity")
    observed_asset_identity = _asset_content_identity(asset_root, marker_name)
    if observed_asset_identity != marker_identity:
        raise RuntimeError(
            "prepared asset snapshot changed after release preparation")

    task_manifest = task_files.get("manifest_path")
    return {
        "profile": f"{release_name}-strict-release",
        "classification": "strict_official_release",
        "profile_path": str(profile_path),
        "profile_sha256": _sha256_file(profile_path),
        "task_release": release_name,
        "task_dataset_tag": task_files.get("tag"),
        "task_manifest_path": task_manifest,
        "task_manifest_sha256": task_files.get("manifest_sha256"),
        "task_count_verified": identity["task_count"],
        "task_content_tree_sha256": identity["content_tree_sha256"],
        "task_class_path": str(task_class_path),
        "task_class_sha256": selected_sha256,
        "release_manifest_path": str(release_path),
        "release_manifest_sha256": _sha256_file(release_path),
        "runtime_tag": runtime.get("tag"),
        "runtime_checkout_commit": runtime_commit,
        "runtime_checkout_describe": _git_value(
            OSWORLD_ROOT, "describe", "--tags", "--always", "--dirty"),
        "website_host_suffix": expected_website,
        "asset_root": str(asset_root),
        "asset_tag": assets.get("tag"),
        "asset_metadata_commits": [assets.get("commit")],
        "asset_metadata_files": observed_asset_identity["file_count"],
        "asset_content_tree_sha256": observed_asset_identity[
            "content_tree_sha256"],
        "provider": "docker",
        "provider_image": profile.get("provider_image"),
    }


def _benchmark_provenance(
        task_id: str, task_class_path: Path,
        profile_path: Path = DEFAULT_BENCHMARK_PROFILE) -> dict[str, Any]:
    """Fail closed on either a strict release lock or reviewed compat profile."""

    profile_path = profile_path.expanduser().resolve(strict=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if profile.get("schema_version") == 2:
        return _release_lock_provenance(
            task_id, task_class_path, profile_path, profile)
    if profile.get("schema_version") != 1:
        raise RuntimeError("unsupported OSWorld compatibility-profile schema")
    if profile.get("profile") != "osworld-v2-0624-runtime-0808-compat":
        raise RuntimeError("unexpected OSWorld compatibility profile")
    semantics = profile.get("benchmark_semantics") or {}
    runtime = profile.get("runtime") or {}
    provider = profile.get("provider") or {}
    tasks = semantics.get("tasks") or {}
    assets = semantics.get("assets") or {}
    if semantics.get("release") != BENCHMARK_TASK_RELEASE:
        raise RuntimeError("compatibility profile task release drifted")

    task_manifest_path = resolve_forge_path(
        str(tasks.get("hash_manifest", "")), forge_root=FORGE_ROOT)
    task_manifest_path = task_manifest_path.resolve(strict=True)
    manifest_sha256 = _sha256_file(task_manifest_path)
    if manifest_sha256 != tasks.get("hash_manifest_sha256"):
        raise RuntimeError("0624 task hash manifest drifted")
    task_manifest = json.loads(task_manifest_path.read_text(encoding="utf-8"))
    task_files = task_manifest.get("files") or {}
    expected_task_count = int(tasks.get("task_count", 0))
    if (task_manifest.get("task_count") != expected_task_count
            or len(task_files) != expected_task_count):
        raise RuntimeError("0624 task hash manifest has the wrong task count")

    task_dir = OSWORLD_ROOT / "evaluation_examples" / "task_class"
    expected_names = set(task_files)
    observed_names = {path.name for path in task_dir.glob("task_*.py")}
    if observed_names != expected_names:
        raise RuntimeError(
            "OSWorld task directory does not exactly match the 0624 manifest: "
            f"missing={sorted(expected_names - observed_names)}, "
            f"extra={sorted(observed_names - expected_names)}")
    for name, expected_record in sorted(task_files.items()):
        path = task_dir / name
        expected_size = int(expected_record["size"])
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"0624 task size drifted: {name}")
        actual_hash = _sha256_file(path)
        if actual_hash != expected_record["sha256"]:
            raise RuntimeError(f"0624 task hash drifted: {name}")

    selected_name = f"{task_id}.py"
    expected_path = (task_dir / selected_name).resolve(strict=True)
    if task_class_path.resolve(strict=True) != expected_path:
        raise RuntimeError("selected task class path escaped the frozen task tree")
    actual = task_files[selected_name]["sha256"]
    release_path = OSWORLD_ROOT / "benchmark_releases" / \
        f"{BENCHMARK_TASK_RELEASE}.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("release") != BENCHMARK_TASK_RELEASE:
        raise RuntimeError("OSWorld benchmark release manifest drifted")

    runtime_commit = _git_value(OSWORLD_ROOT, "rev-parse", "HEAD")
    if runtime_commit != runtime.get("commit"):
        raise RuntimeError(
            "OSWorld runtime is not the frozen compatibility commit: "
            f"expected {runtime.get('commit')}, got {runtime_commit}")
    tracked_status = _git_value(
        OSWORLD_ROOT, "status", "--porcelain", "--untracked-files=no")
    if tracked_status:
        raise RuntimeError("OSWorld runtime has tracked worktree changes")

    expected_website = semantics.get("website_host_suffix")
    if os.environ.get("WEBSITE_HOST_SUFFIX") != expected_website:
        raise RuntimeError(
            "WEBSITE_HOST_SUFFIX does not match the frozen 0624 website")

    asset_value = os.environ.get("OSWORLD_FILE_BASE_URL", "").strip()
    if not asset_value or "://" in asset_value:
        raise RuntimeError(
            "compatibility profile requires a local OSWORLD_FILE_BASE_URL")
    asset_root = Path(asset_value).expanduser().resolve(strict=True)
    metadata_root = asset_root / ".cache" / "huggingface" / "download"
    metadata_paths = sorted(metadata_root.rglob("*.metadata"))
    minimum_assets = int(assets.get("minimum_release_files", 0))
    if len(metadata_paths) < minimum_assets:
        raise RuntimeError(
            "local 0624 asset mirror is incomplete: "
            f"metadata={len(metadata_paths)}, expected_at_least={minimum_assets}")
    metadata_commits = set()
    for metadata_path in metadata_paths:
        first_line = metadata_path.read_text(
            encoding="utf-8", errors="replace").splitlines()[:1]
        if not first_line:
            raise RuntimeError(f"empty Hugging Face metadata: {metadata_path}")
        metadata_commits.add(first_line[0].strip())
    allowed_asset_commits = set(assets.get("compatible_snapshot_commits") or ())
    if not metadata_commits or not metadata_commits <= allowed_asset_commits:
        raise RuntimeError(
            "asset mirror revision is outside the frozen compatibility profile: "
            f"observed={sorted(metadata_commits)}")

    image = provider.get("local_raw_image") or {}
    image_path = (OSWORLD_ROOT / str(image.get("path", ""))).resolve(strict=True)
    if image_path.stat().st_size != int(image.get("size", -1)):
        raise RuntimeError("local OSWorld VM image size drifted")
    image_sha256 = _sha256_file(image_path)
    if image_sha256 != image.get("sha256"):
        raise RuntimeError("local OSWorld VM image hash drifted")

    return {
        "profile": profile["profile"],
        "classification": profile["classification"],
        "profile_path": str(profile_path),
        "profile_sha256": _sha256_file(profile_path),
        "task_release": BENCHMARK_TASK_RELEASE,
        "task_dataset_tag": tasks["tag"],
        "task_manifest_path": str(task_manifest_path),
        "task_manifest_sha256": manifest_sha256,
        "task_count_verified": len(task_files),
        "task_class_path": str(task_class_path),
        "task_class_sha256": actual,
        "release_manifest_path": str(release_path),
        "release_manifest_sha256": _sha256_file(release_path),
        "runtime_tag": runtime["tag"],
        "runtime_checkout_commit": runtime_commit,
        "runtime_checkout_describe": _git_value(
            OSWORLD_ROOT, "describe", "--tags", "--always", "--dirty"),
        "website_host_suffix": expected_website,
        "asset_root": str(asset_root),
        "asset_tag": assets["tag"],
        "asset_metadata_commits": sorted(metadata_commits),
        "asset_metadata_files": len(metadata_paths),
        "provider": provider["name"],
        "vm_image_path": str(image_path),
        "vm_image_size": image_path.stat().st_size,
        "vm_image_sha256": image_sha256,
    }


def _read_initial_memory(path: str) -> dict[str, bytes]:
    from explore.e15_loop import _read_memory_tree

    if not path:
        return {}
    resolved = Path(path).expanduser().resolve(strict=True)
    return _read_memory_tree(str(resolved))


def _append_event(path: Path, event: str, payload: dict[str, Any]) -> None:
    record = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_configs(args):
    from config.settings import load

    target_path = resolve_forge_path(
        args.target_config, forge_root=FORGE_ROOT)
    target_cfg = load(str(target_path))
    normalize_verifier_config_paths(target_cfg, FORGE_ROOT)
    if not target_cfg.agentic_verifier_config:
        raise RuntimeError(
            "unified target config requires the full Agent Verifier")
    if not target_cfg.verifier_continuity:
        raise RuntimeError(
            "unified target config requires persistent Verifier context")
    if target_cfg.env_memory_dir:
        raise RuntimeError(
            "target config must not inject a second memory source")
    target_cfg = dataclasses.replace(
        target_cfg, verifier_evolve_route=True,
        verifier_local_verdict_only=True,
        verifier_hide_actor_memory=True,
        verifier_stage_lifecycle=True,
        verifier_persist_scratch=True,
        verifier_private_paths=("/home/user/work",),
        env_memory_dir="", env_memory_orient=False, env_memory_brief=False)

    evolution_paths = {
        "actor": resolve_forge_path(args.evolution_actor_config,
                                    forge_root=FORGE_ROOT),
        "verifier": resolve_forge_path(args.evolution_verifier_config,
                                       forge_root=FORGE_ROOT),
        "curriculum": resolve_forge_path(args.curriculum_config,
                                         forge_root=FORGE_ROOT),
        "memory": resolve_forge_path(args.memory_config,
                                     forge_root=FORGE_ROOT),
    }
    evolution = {role: load(str(path))
                 for role, path in evolution_paths.items()}
    for role, cfg in evolution.items():
        normalize_verifier_config_paths(cfg, FORGE_ROOT)
        if (not cfg.agent_decided_stop or not cfg.practice_mode
                or cfg.independent_verify or cfg.max_resumes != 0):
            raise RuntimeError(
                f"self-evolving {role} config is not Agent-owned practice mode")
    if evolution["actor"].model != target_cfg.model:
        raise RuntimeError(
            "target and practice Actor Agent configs must use the same model "
            "identity")
    if evolution["memory"].model != evolution["actor"].model:
        raise RuntimeError(
            "memory learning must continue with the same Actor Agent model "
            "identity")
    return target_cfg, evolution, Path(target_path), evolution_paths


def _write_manifest(root: Path, *, task_id: str, instruction: str,
                    target_cfg, target_path: Path, evolution,
                    evolution_paths: dict[str, Path], initial_memory,
                    benchmark_provenance: dict[str, Any],
                    sealed_evaluator: dict[str, Any]) -> None:
    from explore.e15_loop import _manifest
    from explore.e15_v12_loop import _memory_tree_sha256

    def file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    record = {
        "schema_version": 1,
        "kind": "on_demand_unified_loop",
        "task_id": task_id,
        "instruction_sha256": hashlib.sha256(
            instruction.encode("utf-8")).hexdigest(),
        "external_grader_inside_loop": False,
        "target_verifier_authority": ["PASS", "FAIL"],
        "curriculum_pass_authority": ["HANDOFF", "VERIFY_MORE"],
        "curriculum_failure_authority": ["REVISE", "EVOLVE"],
        "pass_transition": "CURRICULUM_REVIEW",
        "verifier_orientation_policy":
            "per_model_identity_lazy_hash_matched_s0",
        "memory_mode": "adaptive",
        "benchmark_provenance": benchmark_provenance,
        "sealed_evaluator": sealed_evaluator,
        "target_config": {
            "path": str(target_path),
            "sha256": file_hash(target_path),
            "effective": dataclasses.asdict(target_cfg),
        },
        "evolution_configs": {
            role: {
                "path": str(evolution_paths[role]),
                "sha256": file_hash(evolution_paths[role]),
                "effective": dataclasses.asdict(evolution[role]),
            }
            for role in sorted(evolution)
        },
        "initial_memory": _manifest(initial_memory),
        "initial_memory_tree_sha256": _memory_tree_sha256(initial_memory),
    }
    destination = root / "manifest.json"
    destination.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True, choices=SUPPORTED_TASKS)
    parser.add_argument("--root", required=True,
                        help="safe basename beneath results/unified")
    parser.add_argument("--initial-memory", default="",
                        help="optional durable-memory directory; empty starts blank")
    parser.add_argument("--target-config", default=str(DEFAULT_TARGET_CONFIG))
    parser.add_argument(
        "--benchmark-lock", default=str(DEFAULT_BENCHMARK_LOCK),
        help="frozen Actor/Verifier/evaluator benchmark configuration")
    parser.add_argument(
        "--benchmark-profile", default=str(DEFAULT_BENCHMARK_PROFILE),
        help="frozen OSWorld task/runtime/asset compatibility profile")
    parser.add_argument(
        "--evolution-actor-config",
        default=str(DEFAULT_EVOLUTION_CONFIGS["actor"]))
    parser.add_argument(
        "--evolution-verifier-config",
        default=str(DEFAULT_EVOLUTION_CONFIGS["verifier"]))
    parser.add_argument(
        "--curriculum-config",
        default=str(DEFAULT_EVOLUTION_CONFIGS["curriculum"]))
    parser.add_argument(
        "--memory-config", default=str(DEFAULT_EVOLUTION_CONFIGS["memory"]))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute", default="",
                        help=f"paid launch acknowledgement: {EXECUTE_ACK}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_paths()
    args = _parser().parse_args(argv)
    root = _safe_root(args.root)
    corpus = Path(args.corpus).expanduser().resolve(strict=True)
    target_cfg, evolution_cfgs, target_path, evolution_paths = \
        _load_configs(args)
    benchmark_lock_path = resolve_forge_path(
        args.benchmark_lock, forge_root=FORGE_ROOT)
    sealed_evaluator = _configure_sealed_evaluator(
        benchmark_lock_path, target_cfg, target_path)

    # Read only the public user instruction here. The evaluator-bearing task module
    # is not imported until a real target environment is booted, and that object is
    # never passed to the self-evolving adapter.
    instruction = _load_public_instruction(args.task_id)
    # /home/user/work is the harness convention for Actor-private scratch. If a
    # future authoritative task literally names that path, it becomes candidate
    # surface and must remain visible rather than being hidden by convention.
    if any(
            path in instruction
            for path in tuple(target_cfg.verifier_private_paths or ())):
        target_cfg = dataclasses.replace(
            target_cfg, verifier_private_paths=tuple(
                path for path in target_cfg.verifier_private_paths
                if path not in instruction))
    from run_e15 import _load_setup_only_target
    setup_only_target, task_class_path = _load_setup_only_target(
        args.task_id, instruction)
    benchmark_profile_path = resolve_forge_path(
        args.benchmark_profile, forge_root=FORGE_ROOT)
    benchmark_provenance = _benchmark_provenance(
        args.task_id, task_class_path, benchmark_profile_path)
    from explore.e15_v12_loop import configure_target_surface
    configure_target_surface(args.task_id)
    from explore.commit import silent_audit, validate_instruction_corpus
    from explore.e15_v12_loop import _target_audit_surface
    validate_instruction_corpus(str(corpus))
    initial_memory = _read_initial_memory(args.initial_memory)
    with tempfile.TemporaryDirectory(prefix="forge-unified-memory-audit-") as audit:
        accepted_initial_memory = silent_audit(
            initial_memory, str(corpus), str(Path(audit) / "rejects.jsonl"),
            authorized_instruction=_target_audit_surface(instruction),
            require_corpus=True)
    if accepted_initial_memory != initial_memory:
        raise RuntimeError("initial memory failed the target leakage boundary")

    preflight = {
        "status": "ready",
        "task_id": args.task_id,
        "root": str(root),
        "target_actor_model": target_cfg.model,
        "target_verifier_model": target_cfg.verifier_model,
        "target_verifier_persistent": target_cfg.verifier_continuity,
        "target_verifier_stages": [
            "ORIENTATION", "CANDIDATE_VERIFICATION"],
        "target_verifier_orientation_policy":
            "per_model_identity_lazy_hash_matched_s0",
        "target_verifier_private_scratch_persistent":
            target_cfg.verifier_persist_scratch,
        "target_verifier_actor_private_paths": list(
            target_cfg.verifier_private_paths),
        "target_verifier_verdicts": ["PASS", "FAIL"],
        "curriculum_pass_routes": ["HANDOFF", "VERIFY_MORE"],
        "curriculum_failure_routes": ["REVISE", "EVOLVE"],
        "pass_transition": "CURRICULUM_REVIEW",
        "target_memory_files": len(initial_memory),
        "memory_mode": "adaptive",
        "initial_memory_state": "seeded" if initial_memory else "blank",
        "self_evolving_actor_model": evolution_cfgs["actor"].model,
        "self_evolving_memory_actor_model": evolution_cfgs["memory"].model,
        "self_evolving_actor_identity_preserved": (
            evolution_cfgs["memory"].model == evolution_cfgs["actor"].model),
        "curriculum_model": evolution_cfgs["curriculum"].model,
        "practice_verifier_model": evolution_cfgs["verifier"].model,
        "official_evaluator_inside_loop": False,
        "sealed_evaluator_model": sealed_evaluator["model"],
        "sealed_evaluator_provider": sealed_evaluator["provider"],
        "sealed_evaluator_credential": sealed_evaluator["credential"],
        "benchmark_profile": benchmark_provenance["profile"],
        "benchmark_profile_classification":
            benchmark_provenance["classification"],
        "benchmark_task_release": benchmark_provenance["task_release"],
        "benchmark_tasks_verified":
            benchmark_provenance["task_count_verified"],
        "task_class_sha256": benchmark_provenance["task_class_sha256"],
        "runtime_checkout": benchmark_provenance["runtime_checkout_describe"],
    }
    if args.preflight:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    if args.execute != EXECUTE_ACK:
        raise RuntimeError(f"--execute must equal {EXECUTE_ACK}")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(
            "unified run root is nonempty; automatic recovery is not implemented")
    sealed_evaluator["transport_preflight"] = \
        _probe_sealed_evaluator_transport()
    root.mkdir(parents=True, exist_ok=True)
    _write_manifest(
        root, task_id=args.task_id, instruction=instruction,
        target_cfg=target_cfg, target_path=target_path,
        evolution=evolution_cfgs, evolution_paths=evolution_paths,
        initial_memory=initial_memory,
        benchmark_provenance=benchmark_provenance,
        sealed_evaluator=sealed_evaluator)
    events_path = root / "events.jsonl"

    from core.actor import build_system
    from core.grading import run_until_success
    from core.loop import (
        _snapshot,
        _verifier_session_identity,
        run_with_resume,
    )
    from core.task_baseline import (
        capture_task_start_baseline,
        stable_task_start_identity,
    )
    from core.trace import ArtifactSink
    from core.unified_loop import (
        ActorLearning,
        UnifiedLoopError,
        UnifiedLoopHooks,
        Verification,
        VerifierRoute,
        run_unified_loop,
    )
    from core.verifier import VerifierSession, verify_agentic
    from env.vm import VM
    from explore.charter import memory_preamble
    from explore.commit import push_memory
    from explore.e15_loop import (
        E15Hooks,
        _atomic_install_memory,
        _manifest,
        _read_memory_tree,
    )
    from explore.e15_v12_loop import (
        _memory_tree_sha256,
        _promote_learning,
        _target_input_fingerprint,
    )
    from explore.unified_evolution import (
        UnifiedCurriculumSession,
        evolve_until_ready,
        route_target_failure,
        route_target_pass,
    )
    from llm.client import provider_counts, reset_provider_counts

    counters = {
        "target": 0,
        "evolution": 0,
        "revisions": 0,
        "target_learning": 0,
        "curriculum_failure_routes": 0,
        "curriculum_pass_reviews": 0,
    }
    target_session_registry: dict = {}
    curriculum_session = UnifiedCurriculumSession()
    # The authoritative evaluator-bearing task object remains host-sealed and
    # is never placed in the unified-loop environment.  The reviewed setup-only
    # projection creates S0; the task object is attached only after terminal
    # HANDOFF, immediately before the one external measurement.
    sealed_target_tasks: dict[int, Any] = {}
    active_memory_dir = root / "active_memory"
    memory_journal_dir = root / "memory_journal"
    _atomic_install_memory(str(active_memory_dir), initial_memory)

    def target_verifier_event(event: str, payload: dict[str, Any]):
        _append_event(events_path, event, payload)

    target_session = VerifierSession(on_event=target_verifier_event)
    target_session_registry[_verifier_session_identity(target_cfg)] = \
        target_session
    target_verifier = _TargetVerifier(sessions=target_session_registry)

    def _boot_target_environment(query: str, *, retain_for_grading: bool):
        """Construct the public task state without exposing evaluator internals.

        The live candidate environment retains the already host-sealed evaluator
        object for post-HANDOFF measurement. A Verifier-orientation clone uses only
        the reviewed setup projection and never imports that object at all.
        """
        if query != instruction:
            raise UnifiedLoopError("immutable target query drifted")
        from desktop_env.desktop_env import DesktopEnv

        task_config = None
        if retain_for_grading:
            task_config, current_instruction = _load_evaluator_task(args.task_id)
            if current_instruction != instruction:
                raise UnifiedLoopError("fresh target instruction drifted")
        desktop = DesktopEnv(
            provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
            screen_size=(1920, 1080), headless=True,
            require_a11y_tree=False, volume_size=60)
        started = time.time()
        try:
            desktop.reset(task_config=setup_only_target)
            if retain_for_grading:
                sealed_target_tasks[id(desktop)] = task_config
            return _TargetEnvironment(
                desktop=desktop, vm=VM(desktop),
                boot_secs=time.time() - started)
        except Exception:
            desktop.close()
            raise

    def fresh_target_environment(query: str):
        return _boot_target_environment(query, retain_for_grading=True)

    def _verifier_identity_token(identity: tuple[str, str]) -> str:
        encoded = json.dumps(
            list(identity), ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def _orientation_sink(identity: tuple[str, str]):
        return ArtifactSink(str(
            root / "target_verifier_lifecycle"
            / _verifier_identity_token(identity)))

    def _stable_s0_identity(environment: _TargetEnvironment) -> dict[str, Any]:
        """Hash stable, task-visible S0 facts rather than volatile home caches."""
        return stable_task_start_identity(
            task_id=args.task_id, instruction=instruction,
            setup_projection=setup_only_target.setup_projection,
            setup_manifest=setup_only_target.setup_manifest,
            target_input_fingerprint=_target_input_fingerprint(environment.vm))

    def _archive_s0(
            baseline: dict[str, Any], s0_identity: dict[str, Any],
            filename: str) -> Path:
        baseline_dir = root / "task_start_baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        destination = baseline_dir / filename
        destination.write_text(
            json.dumps(
                {**baseline, "stable_s0_identity": s0_identity},
                ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return destination

    def _activate_orientation(
            identity: tuple[str, str], report: str) -> None:
        target_verifier.active_identity = identity
        target_verifier.orientation_report = report
        target_verifier.orientation_report_sha256 = \
            target_verifier.orientation_report_sha256s[identity]

    def _run_model_orientation(
            active_cfg, session: VerifierSession,
            environment: _TargetEnvironment, baseline: dict[str, Any],
            s0_identity: dict[str, Any], *, target_cycle: int,
            reason: str, changed: bool) -> None:
        """Orient one model-private Verifier session and checkpoint its scratch."""
        identity = _verifier_session_identity(active_cfg)
        session.on_event = target_verifier_event
        context = (
            "A complete trusted baseline manifest was archived by the host "
            "harness for audit. It is deliberately not mounted into the guest; "
            "the exact live S0 in front of you is your inspection surface."
            + "\nStable task-visible S0 identity digest: "
            + s0_identity["sha256"]
            + "\nComplete persistent-file entries: "
            + str(baseline["entry_count"])
            + "\nFull-home audit content digest: "
            + baseline["content_sha256"]
            + "\nExternal-service snapshot: "
            + baseline["external_service_state"]
            + ("\nThis Verifier identity's prior orientation described a "
               "different stable S0. Re-orient independently before candidate "
               "inspection." if changed else "")
            + ("\nThis is a freshly reconstructed, stable-hash-matched S0 clone "
               "created only because this Verifier model was activated after an "
               "Actor escalation. No Actor has acted in this clone."
               if reason == "LAZY_ESCALATION_CLONE" else ""))
        target_verifier.orientation_sequence += 1
        verdict, findings = verify_agentic(
            instruction, environment.vm, active_cfg,
            sink=_orientation_sink(identity),
            turn_no=-target_verifier.orientation_sequence,
            context=context, session=session,
            wall_budget=active_cfg.wall_clock_secs,
            stage="orientation")
        if verdict != "orientation_ready":
            raise UnifiedLoopError(
                "target Verifier Agent did not complete orientation: "
                + str(findings))
        report = str(findings)
        report_sha256 = hashlib.sha256(
            report.encode("utf-8", "replace")).hexdigest()
        # No mount-namespace keeper may remain idle while an Actor or Curriculum
        # Agent works. Every byte remains archived on the host without a cap.
        session.detach_executor(environment.vm, preserve=True)
        target_verifier.orientation_reports[identity] = report
        target_verifier.orientation_report_sha256s[identity] = report_sha256
        target_verifier.orientation_baselines[identity] = s0_identity["sha256"]
        _activate_orientation(identity, report)
        _append_event(events_path, "VERIFIER_ORIENTATION_COMPLETED", {
            "target_cycle": target_cycle,
            "reason": reason,
            "verifier_identity": list(identity),
            "verifier_model": active_cfg.verifier_model,
            "stable_s0_identity_sha256": s0_identity["sha256"],
            "orientation_report_sha256": report_sha256,
            "scratch_checkpointed": True,
        })

    def verifier_orient(
            verifier: _TargetVerifier, environment: _TargetEnvironment,
            query: str, target_cycle: int):
        if verifier is not target_verifier:
            raise UnifiedLoopError("target Verifier Agent identity drifted")
        if query != instruction:
            raise UnifiedLoopError(
                "immutable target query drifted before Verifier orientation")

        baseline = capture_task_start_baseline(environment.vm)
        s0_identity = _stable_s0_identity(environment)
        surface_baseline = _snapshot(
            environment.vm,
            excluded_paths=target_cfg.verifier_private_paths)
        environment.baseline = baseline
        environment.surface_baseline = surface_baseline
        baseline_path = _archive_s0(
            baseline, s0_identity, f"cycle_{target_cycle:03d}.json")
        previous = verifier.baseline_content_sha256
        previous_s0 = verifier.s0_identity_sha256
        content_changed = bool(
            previous and previous != baseline["content_sha256"])
        stable_changed = bool(
            previous_s0 and previous_s0 != s0_identity["sha256"])
        _append_event(events_path, "TRUSTED_TASK_START_BASELINE_CAPTURED", {
            "target_cycle": target_cycle,
            "manifest": str(baseline_path),
            "entry_count": baseline["entry_count"],
            "content_sha256": baseline["content_sha256"],
            "exact_sha256": baseline["exact_sha256"],
            "stable_s0_identity_sha256": s0_identity["sha256"],
            "previous_content_sha256": previous or None,
            "previous_stable_s0_identity_sha256": previous_s0 or None,
            "content_changed_from_prior_cycle": content_changed,
            "stable_s0_changed_from_prior_cycle": stable_changed,
            "external_service_state": baseline["external_service_state"],
        })

        identity = _verifier_session_identity(target_cfg)
        prior_identity_s0 = verifier.orientation_baselines.get(identity, "")
        needs_orientation = (
            identity not in verifier.orientation_reports
            or prior_identity_s0 != s0_identity["sha256"])
        if needs_orientation:
            _run_model_orientation(
                target_cfg, target_session, environment, baseline, s0_identity,
                target_cycle=target_cycle, reason="PRIMARY_TASK_START",
                changed=bool(prior_identity_s0))
        else:
            report = verifier.orientation_reports[identity]
            _activate_orientation(identity, report)
            _append_event(events_path, "VERIFIER_ORIENTATION_REUSED", {
                "target_cycle": target_cycle,
                "verifier_identity": list(identity),
                "stable_s0_identity_sha256": s0_identity["sha256"],
                "orientation_report_sha256":
                    verifier.orientation_report_sha256,
            })
        verifier.baseline_content_sha256 = baseline["content_sha256"]
        verifier.s0_identity_sha256 = s0_identity["sha256"]

    def ensure_verifier_orientation(active_cfg, session: VerifierSession):
        """Lazily orient each newly activated Verifier model on matched S0."""
        identity = _verifier_session_identity(active_cfg)
        expected_s0 = target_verifier.s0_identity_sha256
        if not expected_s0:
            raise UnifiedLoopError(
                "Verifier activation preceded trusted S0 capture")
        prior_s0 = target_verifier.orientation_baselines.get(identity, "")
        report = target_verifier.orientation_reports.get(identity, "")
        if report and prior_s0 == expected_s0:
            session.on_event = target_verifier_event
            _activate_orientation(identity, report)
            return

        clone = _boot_target_environment(
            instruction, retain_for_grading=False)
        completed = False
        try:
            baseline = capture_task_start_baseline(clone.vm)
            clone_s0 = _stable_s0_identity(clone)
            token = _verifier_identity_token(identity)
            baseline_path = _archive_s0(
                baseline, clone_s0,
                f"cycle_{counters['target']:03d}_activation_"
                f"{target_verifier.orientation_sequence + 1:03d}_{token}.json")
            _append_event(events_path, "VERIFIER_ORIENTATION_CLONE_CAPTURED", {
                "target_cycle": counters["target"],
                "manifest": str(baseline_path),
                "verifier_identity": list(identity),
                "stable_s0_identity_sha256": clone_s0["sha256"],
                "expected_stable_s0_identity_sha256": expected_s0,
                "full_home_content_sha256": baseline["content_sha256"],
                "evaluator_object_imported": False,
            })
            if clone_s0["sha256"] != expected_s0:
                raise UnifiedLoopError(
                    "fresh Verifier-orientation clone does not match the "
                    "authoritative task-visible S0")
            _run_model_orientation(
                active_cfg, session, clone, baseline, clone_s0,
                target_cycle=counters["target"],
                reason="LAZY_ESCALATION_CLONE",
                changed=bool(prior_s0))
            completed = True
        finally:
            try:
                # On an incomplete orientation, discard model-private scratch; it
                # cannot be treated as a valid cross-model orientation checkpoint.
                session.detach_executor(clone.vm, preserve=completed)
            finally:
                clone.desktop.close()

    def release_target_environment(
            environment: _TargetEnvironment, *, preserve_verifier=True):
        detach_errors = []
        try:
            for session in tuple(target_session_registry.values()):
                try:
                    session.detach_executor(
                        environment.vm, preserve=preserve_verifier)
                except Exception as exc:  # cleanup all identities before raising
                    detach_errors.append(exc)
        finally:
            try:
                sealed_target_tasks.pop(id(environment.desktop), None)
            finally:
                environment.desktop.close()
        if detach_errors:
            raise detach_errors[0]

    def fresh_target_actor(memory: dict[str, bytes]):
        counters["target"] += 1
        return _TargetActor(memory=dict(memory), cycle=counters["target"])

    def curriculum_route_failure(report: str):
        """Route one local FAIL without exposing the target environment."""
        counters["curriculum_failure_routes"] += 1
        route_root = root / "curriculum_routes"
        from explore.e15_egress import V12EgressSeal
        from run_e15 import _boot_vm, _close, _reset_null_vm_sealed

        curriculum_environment, curriculum_vm = _boot_vm()
        egress_seal = V12EgressSeal()
        try:
            prepare_null_tools = getattr(
                setup_only_target, "prepare_unsealed", None)
            curriculum_hooks = E15Hooks(
                reset_vm=lambda active_vm, direction:
                    _reset_null_vm_sealed(
                        active_vm, direction, egress_seal,
                        prepare_unsealed=prepare_null_tools))
            decision = route_target_failure(
                curriculum_vm, str(route_root), instruction, report,
                evolution_cfgs["curriculum"], hooks=curriculum_hooks,
                session=curriculum_session,
                event_sink=lambda event_type, **kwargs: _append_event(
                    events_path, "CURRICULUM_" + event_type, kwargs))
        finally:
            _close(curriculum_environment)
            egress_seal.close()
        return decision.route, decision.report

    def curriculum_review_pass(report: str):
        """Audit terminal evidence without exposing candidate or grader state."""
        if (target_verifier.active_identity is None
                or not target_verifier.orientation_report.strip()):
            raise UnifiedLoopError(
                "PASS review has no orientation for the active Verifier identity")
        counters["curriculum_pass_reviews"] += 1
        route_root = root / "curriculum_routes"
        from explore.e15_egress import V12EgressSeal
        from run_e15 import _boot_vm, _close, _reset_null_vm_sealed

        curriculum_environment, curriculum_vm = _boot_vm()
        egress_seal = V12EgressSeal()
        try:
            prepare_null_tools = getattr(
                setup_only_target, "prepare_unsealed", None)
            curriculum_hooks = E15Hooks(
                reset_vm=lambda active_vm, direction:
                    _reset_null_vm_sealed(
                        active_vm, direction, egress_seal,
                        prepare_unsealed=prepare_null_tools))
            decision = route_target_pass(
                curriculum_vm, str(route_root), instruction,
                target_verifier.orientation_report, report,
                evolution_cfgs["curriculum"], hooks=curriculum_hooks,
                session=curriculum_session,
                event_sink=lambda event_type, **kwargs: _append_event(
                    events_path, "CURRICULUM_" + event_type, kwargs))
        finally:
            _close(curriculum_environment)
            egress_seal.close()
        return decision.route, decision.report

    def actor_work(actor: _TargetActor, environment: _TargetEnvironment,
                   query: str):
        if actor.worked:
            raise UnifiedLoopError(
                "normal REVISE must remain inside the same Actor harness call")
        actor.worked = True
        _atomic_install_memory(str(active_memory_dir), actor.memory)
        if not push_memory(environment.vm, str(active_memory_dir)):
            raise UnifiedLoopError("could not attach durable memory to target Actor")
        listing = "\n".join(
            f"  {len(data):>7}  {name}"
            for name, data in sorted(actor.memory.items()))
        opening_extra = memory_preamble(listing)
        cycle_cfg = dataclasses.replace(
            target_cfg,
            env_memory_dir=(str(active_memory_dir) if actor.memory else ""),
        )
        _append_event(events_path, "TARGET_MEMORY_ATTACHED", {
            "target_cycle": actor.cycle,
            "mode": "adaptive",
            "state": "populated" if actor.memory else "blank",
            "files": len(actor.memory),
            "bytes": sum(len(data) for data in actor.memory.values()),
            "memory_tree_sha256": _memory_tree_sha256(actor.memory),
        })
        sink_root = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
        sink = ArtifactSink(str(sink_root))
        runtime_state: dict[str, Any] = {}
        result, history = run_with_resume(
            query, environment.vm, cycle_cfg, sink,
            opening_extra=opening_extra,
            verifier_sessions=target_session_registry,
            runtime_state=runtime_state,
            surface_baseline=environment.surface_baseline,
            verifier_failure_router=curriculum_route_failure,
            verifier_pass_router=curriculum_review_pass,
            verifier_session_prepare=ensure_verifier_orientation)
        counters["revisions"] += sum(
            1 for _turn, route in (result.inspections or [])
            if str(route).split(":")[-1] == "wrong")
        sink.save_transcript(build_system(cycle_cfg), history)
        return _TargetOutput(
            loop_result=result,
            history=history,
            active_history=list(runtime_state.get("active_history") or history),
            active_cfg=runtime_state.get("active_cfg") or cycle_cfg,
            sink_root=str(sink_root))

    def verifier_verify(_verifier, _environment, _query,
                        output: _TargetOutput):
        result = output.loop_result
        route = str(getattr(result, "verifier_route", "") or "")
        report = str(getattr(result, "verifier_report", "") or "")
        curriculum_route = str(
            getattr(result, "curriculum_route", "") or "")
        curriculum_report = str(
            getattr(result, "curriculum_report", "") or "")
        if (result.status == "done" and route == "HANDOFF"
                and curriculum_route == "HANDOFF" and report.strip()
                and curriculum_report.strip()):
            return Verification(
                VerifierRoute.HANDOFF, report, curriculum_report)
        if result.status == "evolve" and route == "EVOLVE" and report.strip():
            return Verification(
                VerifierRoute.EVOLVE, report, curriculum_report)
        raise UnifiedLoopError(
            "target harness ended without both terminal authority keys: "
            f"status={result.status!r}, verifier_route={route!r}, "
            f"curriculum_route={curriculum_route!r}")

    def actor_receive(_actor, _report):
        raise UnifiedLoopError(
            "REVISE is handled inside run_with_resume so Actor context is retained")

    def actor_learn(
            actor: _TargetActor, environment: _TargetEnvironment, query: str,
            output: _TargetOutput, report: str,
            memory: dict[str, bytes]) -> ActorLearning:
        if query != instruction:
            raise UnifiedLoopError("immutable target query drifted before learning")
        if actor.memory != memory:
            raise UnifiedLoopError("target Actor memory snapshot drifted")
        if not output.active_history:
            raise UnifiedLoopError("target Actor has no active context to learn from")

        # Work-phase writes to ~/.memory are never canonical. Restore the exact
        # input snapshot, then let this same Actor context own a terminal,
        # learning-only update from the complete Verifier Agent report.
        _atomic_install_memory(str(active_memory_dir), memory)
        if not push_memory(environment.vm, str(active_memory_dir)):
            raise UnifiedLoopError(
                "could not restore canonical memory for target learning")
        counters["target_learning"] += 1
        learning_dir = Path(output.sink_root) / "terminal_learning"
        learning_dir.mkdir(parents=True, exist_ok=True)

        def emit(event_type: str, *, status: str, project_open: bool,
                 memory_phase_open: bool, payload: dict[str, Any]):
            _append_event(events_path, "TARGET_" + event_type, {
                "status": status,
                "project_open": project_open,
                "memory_phase_open": memory_phase_open,
                **payload,
            })

        try:
            learned, diagnosis, learned_history = _promote_learning(
                hooks=E15Hooks(), vm=environment.vm, cfg=output.active_cfg,
                lineage=root, episode_dir=learning_dir,
                experience_index=counters["target_learning"],
                before_memory=memory,
                actor_history=output.active_history,
                terminal_outcome="FAIL", verifier_report=report,
                target=query, audit_mode="exam", emit=emit,
                project_open=False, corpus_path=str(corpus),
                memory_dir=active_memory_dir,
                journal_dir=memory_journal_dir,
                experience_kind="unified-target-evolve")
        except Exception as exc:
            raise UnifiedLoopError(
                "target Actor terminal learning failed: "
                f"{type(exc).__name__}: {exc}") from exc
        record = {
            "schema_version": 1,
            "kind": "unified_target_evolve_learning",
            "target_cycle": actor.cycle,
            "verifier_report": report,
            "actor_learning_diagnosis": diagnosis,
            "memory_before": _manifest(memory),
            "memory_after": _manifest(learned),
            "active_actor_model": output.active_cfg.model,
            "active_history_messages_before": len(output.active_history),
            "active_history_messages_after": len(learned_history),
        }
        (learning_dir / "outcome.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return ActorLearning(memory=learned, diagnosis=diagnosis)

    def evolve(
            query: str, report: str, diagnosis: str,
            memory: dict[str, bytes], routing_report: str):
        counters["evolution"] += 1
        cycle_root = root / "evolution_cycles" / \
            f"cycle_{counters['evolution']:03d}"
        from explore.e15_egress import V12EgressSeal
        from run_e15 import _boot_vm, _close, _reset_null_vm_sealed

        practice_environment, practice_vm = _boot_vm()
        egress_seal = V12EgressSeal()
        try:
            prepare_null_tools = getattr(
                setup_only_target, "prepare_unsealed", None)
            practice_hooks = E15Hooks(
                reset_vm=lambda active_vm, direction:
                    _reset_null_vm_sealed(
                        active_vm, direction, egress_seal,
                        prepare_unsealed=prepare_null_tools))
            result = evolve_until_ready(
                practice_vm, str(cycle_root), query, report, diagnosis, memory,
                evolution_cfgs["actor"], evolution_cfgs["verifier"],
                evolution_cfgs["curriculum"], evolution_cfgs["memory"],
                corpus_path=str(corpus), hooks=practice_hooks,
                routing_report=routing_report,
                session=curriculum_session,
                event_sink=lambda event_type, **kwargs: _append_event(
                    events_path, "EVOLUTION_" + event_type, kwargs))
        finally:
            _close(practice_environment)
            egress_seal.close()
        if result.status != "ready_for_retry":
            raise UnifiedLoopError(
                "self-evolving cycle did not authorize a target retry: "
                f"{result.status}: {result.reason}")
        learned = _read_memory_tree(result.memory_dir)
        _atomic_install_memory(str(active_memory_dir), learned)
        return learned

    reset_provider_counts()
    unified = run_unified_loop(
        instruction, initial_memory,
        UnifiedLoopHooks(
            new_target_verifier=lambda _query: target_verifier,
            fresh_target_environment=fresh_target_environment,
            verifier_orient=verifier_orient,
            fresh_target_actor=fresh_target_actor,
            actor_work=actor_work,
            verifier_verify=verifier_verify,
            actor_receive=actor_receive,
            actor_learn=actor_learn,
            evolve=evolve,
            release_target_environment=release_target_environment,
            on_event=lambda event, payload: _append_event(
                events_path, event, payload),
        ))

    # SEALED EXTERNAL MEASUREMENT: the loop is over. Nothing below can become an
    # Agent message, a Curriculum observation, or a future memory update.
    final_environment = unified.environment
    evaluator_task = sealed_target_tasks.get(id(final_environment.desktop))
    if evaluator_task is None:
        raise UnifiedLoopError(
            "sealed authoritative evaluator task is unavailable after HANDOFF")
    final_environment.desktop._set_task_info(evaluator_task)
    _append_event(events_path, "SEALED_EVALUATOR_ATTACHED_AFTER_HANDOFF", {
        "task": args.task_id,
        "target_cycles": unified.target_cycles,
        "agent_loop_complete": True,
    })
    grade_failures = 0

    def on_grade_error(attempt, error):
        nonlocal grade_failures
        grade_failures = attempt
        log.warning(
            "evaluate() attempt %d failed; HANDOFF VM preserved: %s",
            attempt, error)

    try:
        grade = run_until_success(
            final_environment.desktop.evaluate,
            on_error=on_grade_error,
            retry_delay=max(0.0, float(os.environ.get(
                "OSWORLD_EVAL_OUTER_RETRY_DELAY", "30"))))
        score = grade.get("score", grade) if isinstance(grade, dict) else grade
        payload = {
            "schema_version": 1,
            "task": args.task_id,
            "score": float(score),
            "target_cycles": unified.target_cycles,
            "revisions": counters["revisions"],
            "evolutions": unified.evolutions,
            "curriculum_failure_routes":
                counters["curriculum_failure_routes"],
            "curriculum_pass_reviews":
                counters["curriculum_pass_reviews"],
            "final_memory_tree_sha256": _memory_tree_sha256(unified.memory),
            "verifier_report_sha256": hashlib.sha256(
                unified.verifier_report.encode("utf-8")).hexdigest(),
            "curriculum_handoff_report_sha256": hashlib.sha256(
                unified.curriculum_report.encode("utf-8")).hexdigest(),
            "accepted_verifier_identity": list(
                target_verifier.active_identity or ()),
            "accepted_verifier_orientation_report_sha256":
                target_verifier.orientation_report_sha256,
            "grade_failed_attempts": grade_failures,
            "sealed_evaluator": sealed_evaluator,
            "llm_providers": provider_counts(),
            "official_evaluator_feedback_entered_loop": False,
        }
        (root / "final_result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print("\n=== UNIFIED RESULT " + json.dumps(payload, sort_keys=True))
        return 0
    finally:
        try:
            # HANDOFF is terminal: the sealed grade consumes the accepted live
            # desktop first, then both VM and private Verifier scratch are
            # discarded.  Re-archiving scratch here would be immediately thrown
            # away and can double a large evidence tree for no semantic benefit.
            release_target_environment(
                final_environment, preserve_verifier=False)
        finally:
            for session in tuple(target_session_registry.values()):
                session.close_executor()


if __name__ == "__main__":
    raise SystemExit(main())
