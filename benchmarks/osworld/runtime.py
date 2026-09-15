"""OSWorld release validation and host-only evaluator configuration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from config.benchmark_runtime import load_user_simulator_credential
from config.runtime_paths import (
    resolve_env_file,
    resolve_osworld_root,
    resolve_root,
)

RSIAGENT_ROOT = resolve_root()

OSWORLD_ROOT = resolve_osworld_root(repo_root=RSIAGENT_ROOT)

BENCHMARK_TASK_RELEASE = "osworld-v2-2026.08.08"

DEFAULT_BENCHMARK_PROFILE = RSIAGENT_ROOT / "config/osworld/baseline.lock.json"


def _install_paths() -> None:
    if not OSWORLD_ROOT.is_dir():
        raise RuntimeError(f"OSWorld-V2 checkout does not exist: {OSWORLD_ROOT}")
    try:
        import dotenv

        dotenv.load_dotenv(OSWORLD_ROOT / ".env", override=False)
    except ImportError:
        pass
    load_user_simulator_credential(repo_root=RSIAGENT_ROOT)
    for path in (str(OSWORLD_ROOT), str(RSIAGENT_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(OSWORLD_ROOT)


def _rsiagent_openrouter_key() -> str:
    """Resolve the shared transport credential without exposing it in artifacts."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env_path = resolve_env_file(repo_root=RSIAGENT_ROOT)
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
    lock_path: Path, target_cfg, target_path: Path
) -> dict[str, Any]:
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
        raise RuntimeError(
            "benchmark lock Actor config hash does not match target config"
        )

    required = (
        "provider",
        "model",
        "base_url",
        "website_host_suffix",
        "retry_attempts",
        "retry_delay_seconds",
        "visibility",
    )
    missing = [name for name in required if evaluator.get(name) in (None, "")]
    if missing:
        raise RuntimeError(f"benchmark lock evaluator config is incomplete: {missing}")
    if evaluator["visibility"] != "sealed_after_actor_verifier_loop":
        raise RuntimeError("benchmark lock evaluator is not sealed after the loop")
    key = _rsiagent_openrouter_key()
    if not key:
        raise RuntimeError(
            "sealed evaluator credential is absent: configure OPENROUTER_API_KEY"
        )

    # Assign, rather than setdefault, so a stale interactive shell cannot silently
    # drift a release-bound experiment away from its reviewed evaluator config.
    os.environ["OPENROUTER_API_KEY"] = key
    os.environ["WEBSITE_HOST_SUFFIX"] = str(evaluator["website_host_suffix"])
    os.environ["OSWORLD_EVAL_MODEL_PROVIDER"] = str(evaluator["provider"])
    os.environ["OSWORLD_EVAL_MODEL_NAME"] = str(evaluator["model"])
    os.environ["OSWORLD_EVAL_MODEL_BASE_URL"] = str(evaluator["base_url"])
    os.environ["OSWORLD_EVAL_MODEL_API_KEY_ENV"] = "OPENROUTER_API_KEY"
    os.environ["OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS"] = str(evaluator["retry_attempts"])
    os.environ["OSWORLD_EVAL_MODEL_RETRY_DELAY"] = str(evaluator["retry_delay_seconds"])
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

    response = str(
        generate_text(
            "Evaluator transport preflight only. Reply exactly READY.",
            options={"max_tokens": 8, "temperature": 0.0},
        )
    ).strip()
    if not response:
        raise RuntimeError("sealed evaluator transport preflight returned empty output")
    return {
        "status": "ready",
        "response_sha256": hashlib.sha256(
            response.encode("utf-8", "replace")
        ).hexdigest(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
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
            "\n".join(rows).encode("utf-8")
        ).hexdigest(),
        "total_bytes": total_bytes,
        "task_count": len(paths),
        "task_ids_sha256": hashlib.sha256(
            "\n".join(path.stem for path in paths).encode("utf-8")
        ).hexdigest(),
    }


def _asset_content_identity(asset_root: Path, marker_name: str) -> dict[str, Any]:
    """Verify the prepared release snapshot, excluding downloader metadata."""

    rows = []
    total_bytes = 0
    for path in sorted(asset_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(asset_root)
        if (
            relative.parts and relative.parts[0] == ".cache"
        ) or relative.as_posix() == marker_name:
            continue
        size = path.stat().st_size
        total_bytes += size
        rows.append(f"{relative.as_posix()}\t{size}\t{_sha256_file(path)}")
    return {
        "content_tree_sha256": hashlib.sha256(
            "\n".join(rows).encode("utf-8")
        ).hexdigest(),
        "total_bytes": total_bytes,
        "file_count": len(rows),
    }


def _release_lock_provenance(
    task_id: str, task_class_path: Path, profile_path: Path, profile: dict[str, Any]
) -> dict[str, Any]:
    """Fail closed on a strict release lock such as the 2026-08-08 lock."""

    if profile.get("schema_version") != 2 or profile.get("benchmark") != "OSWorld-v2":
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
            f"expected={expected_count}, observed={len(observed_names)}"
        )
    identity = _task_content_identity(task_dir)
    task_files = profile.get("task_files") or {}
    for key in ("content_tree_sha256", "total_bytes"):
        if identity[key] != task_files.get(key):
            raise RuntimeError(
                "OSWorld task sources differ from the strict release lock: "
                f"{key} expected={task_files.get(key)!r}, "
                f"observed={identity[key]!r}"
            )
    if identity["task_ids_sha256"] != profile.get("task_ids_sha256"):
        raise RuntimeError("OSWorld task filename set differs from the release lock")

    selected_name = f"{task_id}.py"
    expected_path = (task_dir / selected_name).resolve(strict=True)
    if task_class_path.resolve(strict=True) != expected_path:
        raise RuntimeError("selected task class path escaped the frozen task tree")
    selected_sha256 = _sha256_file(expected_path)

    release_meta = profile.get("release_manifest") or {}
    release_path = (OSWORLD_ROOT / str(release_meta.get("path", ""))).resolve(
        strict=True
    )
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
            f"expected={runtime.get('commit')}, observed={runtime_commit}"
        )
    if _git_value(OSWORLD_ROOT, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("OSWorld runtime has tracked worktree changes")

    website = profile.get("website") or {}
    expected_website = website.get("host_suffix")
    if os.environ.get("WEBSITE_HOST_SUFFIX") != expected_website:
        raise RuntimeError("WEBSITE_HOST_SUFFIX does not match the strict release lock")

    assets = profile.get("task_assets") or {}
    asset_value = os.environ.get("OSWORLD_FILE_BASE_URL", "").strip()
    if not asset_value or "://" in asset_value:
        raise RuntimeError("strict release lock requires a local OSWORLD_FILE_BASE_URL")
    asset_root = Path(asset_value).expanduser().resolve(strict=True)
    expected_asset_root = (OSWORLD_ROOT / str(assets.get("local_dir", ""))).resolve(
        strict=True
    )
    if asset_root != expected_asset_root:
        raise RuntimeError(
            "OSWORLD_FILE_BASE_URL does not name the locked asset snapshot"
        )
    marker_name = str(assets.get("release_marker", ".rsiagent_osworld_release.json"))
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
        for key, value in expected_marker.items()
        if marker.get(key) != value
    }
    if mismatch:
        raise RuntimeError(
            f"prepared asset marker differs from the release lock: {mismatch}"
        )
    marker_identity = marker.get("content_identity")
    if not isinstance(marker_identity, dict):
        raise RuntimeError("prepared asset marker lacks content_identity")
    observed_asset_identity = _asset_content_identity(asset_root, marker_name)
    if observed_asset_identity != marker_identity:
        raise RuntimeError("prepared asset snapshot changed after release preparation")

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
            OSWORLD_ROOT, "describe", "--tags", "--always", "--dirty"
        ),
        "website_host_suffix": expected_website,
        "asset_root": str(asset_root),
        "asset_tag": assets.get("tag"),
        "asset_metadata_commits": [assets.get("commit")],
        "asset_metadata_files": observed_asset_identity["file_count"],
        "asset_content_tree_sha256": observed_asset_identity["content_tree_sha256"],
        "provider": "docker",
        "provider_image": profile.get("provider_image"),
    }


def _benchmark_provenance(
    task_id: str, task_class_path: Path, profile_path: Path = DEFAULT_BENCHMARK_PROFILE
) -> dict[str, Any]:
    """Validate the pinned OSWorld release before executing a target."""

    profile_path = profile_path.expanduser().resolve(strict=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    return _release_lock_provenance(task_id, task_class_path, profile_path, profile)


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
