"""Release and harness contracts for the OSWorld 2.0 0808 baseline."""
from dataclasses import fields
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from config.settings import Config, load
from tools import run_osworld_v2_baseline_shard as shard


REPO = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO / "config/osworld_v2_0808_glm53_k3_agentic_baseline.lock.json"
ACTOR_PATH = REPO / "config/osworld_v2_0808_glm53_k3_agentic_baseline.yaml"
VERIFIER_PATH = REPO / "config/osworld_v2_k3_agentic_verifier.yaml"
ESCALATED_VERIFIER_PATH = REPO / "config/osworld_v2_glm53_agentic_verifier.yaml"
REPAIR_LOCK_PATH = (
    REPO / "config/osworld_v2_0808_glm53_k3_agentic_repair_v2.lock.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_launcher_keeps_virtualenv_python_symlink(tmp_path):
    base = tmp_path / "base-python"
    base.write_text("base", encoding="utf-8")
    venv_python = tmp_path / "venv-python"
    venv_python.symlink_to(base)

    selected = shard.absolute_executable_path(venv_python)

    assert selected == venv_python.absolute()
    assert selected != venv_python.resolve()


def test_0808_lock_pins_every_release_component_and_role_config():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["benchmark_release"] == "osworld-v2-2026.08.08"
    assert lock["experiment"] == "full_0808_current_agent_harness_baseline_v2_10h"
    assert lock["tag"] == "osworld_v2_0808_glm53_k3_agentic_baseline_v2"
    assert lock["seed_label"] == 5400
    assert lock["assignment_policy"] == "equal_36"
    assert lock["assignments"] == [
        {"shard_index": 0, "owner": "xinyue", "first_task": 1,
         "last_task": 36, "task_count": 36},
        {"shard_index": 1, "owner": "shicheng", "first_task": 37,
         "last_task": 72, "task_count": 36},
        {"shard_index": 2, "owner": "sibo", "first_task": 73,
         "last_task": 108, "task_count": 36},
    ]
    assert lock["osworld_code"] == {
        "repository": "xlang-ai/OSWorld-V2",
        "tag": "v2026.08.08",
        "commit": "d578d2d4e0dc82b43e270fdaa7fa89d9708cd154",
    }
    assert lock["task_files"]["commit"] == \
        "3736efa55d9d5dc78f57e873ef78886663e41200"
    assert lock["task_files"]["manifest_sha256"] == \
        "42f8f6f8939b8712997d5891456a575f8a2a5f53465e9e3e6747af5d6efd0915"
    assert lock["task_assets"]["commit"] == \
        "acad110ef3136405f95434b54862bf9066176c2a"
    assert lock["website"]["host_suffix"] == "site.hku.icu"
    assert lock["evaluator"]["website_host_suffix"] == "site.hku.icu"
    assert lock["evaluator"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert lock["user_simulator"] == {
        "provider": "openai_compatible",
        "model": "openai/gpt-4o",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "max_tokens": 256,
        "failure_policy": "abort_infra_invalid_before_official_evaluation",
    }
    assert lock["actor_agent"]["sha256"] == _sha256(ACTOR_PATH)
    assert lock["verifier_agent"]["sha256"] == _sha256(VERIFIER_PATH)
    assert lock["escalation"]["verifier_config_sha256"] == \
        _sha256(ESCALATED_VERIFIER_PATH)


def test_0808_baseline_is_pure_current_harness_with_memory_off():
    known = {field.name for field in fields(Config)}
    raw = yaml.safe_load(ACTOR_PATH.read_text(encoding="utf-8"))
    assert set(raw) <= known
    cfg = load(str(ACTOR_PATH))

    assert cfg.model == "z-ai/glm-5.3"
    assert cfg.provider_order == ["Z.AI"]
    assert (cfg.vision_model, cfg.vision_rounds, cfg.look_ensemble) == (
        "moonshotai/kimi-k3", 1, 1)
    assert cfg.agentic_verifier_config == \
        "config/osworld_v2_k3_agentic_verifier.yaml"
    assert cfg.wall_clock_secs == 36000
    assert cfg.verifier_continuity is True
    assert cfg.verifier_execution_mode == "rollback_mirror"
    assert cfg.verifier_unverified_evidence is True
    assert cfg.verifier_stage_lifecycle is False
    assert cfg.verifier_evolve_route is False
    assert cfg.actor_evaluator_isolation is True
    assert cfg.env_memory_dir == ""


def test_task_content_identity_detects_any_task_source_change(tmp_path):
    task_root = tmp_path / "task_class"
    task_root.mkdir()
    for index, task in enumerate(shard.EXPECTED_TASKS):
        (task_root / f"{task}.py").write_text(
            f"# release source {index}\n", encoding="utf-8")

    first = shard.task_content_identity(task_root)
    (task_root / "task_080.py").write_text("# changed\n", encoding="utf-8")
    second = shard.task_content_identity(task_root)

    assert first["task_count"] == second["task_count"] == 108
    assert first["content_tree_sha256"] != second["content_tree_sha256"]


def test_asset_marker_binds_release_and_detects_post_prepare_change(tmp_path):
    osworld_root = tmp_path / "OSWorld-V2"
    asset_root = osworld_root / "cache/osworld_v2_assets_0808"
    (asset_root / "task_001").mkdir(parents=True)
    asset = asset_root / "task_001/input.bin"
    asset.write_bytes(b"official-input")
    marker_name = ".forge_osworld_release.json"
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    identity = shard.asset_content_identity(asset_root, marker_name)
    marker = {
        "benchmark_release": lock["benchmark_release"],
        "repository": lock["task_assets"]["repository"],
        "repo_type": lock["task_assets"]["repo_type"],
        "tag": lock["task_assets"]["tag"],
        "commit": lock["task_assets"]["commit"],
        "content_identity": identity,
    }
    (asset_root / marker_name).write_text(json.dumps(marker), encoding="utf-8")

    observed_root, observed_marker = shard.validate_release_assets(
        osworld_root, lock)
    assert observed_root == asset_root.resolve()
    assert observed_marker == marker

    asset.write_bytes(b"tampered-input")
    try:
        shard.validate_release_assets(osworld_root, lock)
    except shard.PreflightError as exc:
        assert "changed after release preparation" in str(exc)
    else:
        raise AssertionError("modified assets passed release validation")


def test_release_environment_uses_pinned_asset_root(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    asset_root = tmp_path / "assets"
    environment = shard.benchmark_environment(
        REPO, tmp_path / "OSWorld-V2", lock, assets_root=asset_root)

    assert environment["OSWORLD_FILE_BASE_URL"] == str(asset_root)
    assert environment["WEBSITE_HOST_SUFFIX"] == "site.hku.icu"
    assert environment["OSWORLD_EVAL_MODEL_NAME"] == "openai/gpt-5.4"
    assert environment["OSWORLD_USER_SIM_PROVIDER"] == "openai_compatible"
    assert environment["OSWORLD_USER_SIM_MODEL"] == "openai/gpt-4o"
    assert environment["OSWORLD_USER_SIM_BASE_URL"] == \
        "https://openrouter.ai/api/v1"
    assert environment["OSWORLD_USER_SIM_API_KEY_ENV"] == "OPENROUTER_API_KEY"
    assert environment["OSWORLD_USER_SIM_MAX_TOKENS"] == "256"


def test_repair_lock_preserves_release_but_binds_fixed_runtime_configs():
    lock, actor, verifier, escalated = shard.validate_lock(
        REPO, REPAIR_LOCK_PATH,
        REPO.parent / "OSWorld-V2/evaluation_examples/task_class")

    assert lock["benchmark_release"] == "osworld-v2-2026.08.08"
    assert lock["osworld_code"]["commit"] == \
        "d578d2d4e0dc82b43e270fdaa7fa89d9708cd154"
    assert lock["runtime_repairs"]["profile"] == "infra_context_v2"
    assert actor.name.endswith("repair_v2.yaml")
    assert verifier.name.endswith("repair_v2.yaml")
    assert escalated.name.endswith("repair_v2.yaml")
    assert load(str(actor)).wall_clock_secs == 28800
    assert load(str(actor)).trace_context_max_chars == 500000
    assert load(str(verifier)).trace_context_max_chars == 250000
    assert load(str(escalated)).trace_context_max_chars == 250000


def test_current_0808_still_requires_its_locked_user_simulator(tmp_path):
    lock = json.loads(LOCK_PATH.read_text())
    del lock["user_simulator"]
    path = tmp_path / "missing-user-simulator.lock.json"
    path.write_text(json.dumps(lock))

    with pytest.raises(shard.PreflightError, match="user-simulator transport"):
        shard.validate_lock(
            REPO, path, REPO.parent / "OSWorld-V2/evaluation_examples/task_class")


def test_historical_repair_lock_is_independent_of_mutable_baseline(tmp_path):
    lock_copy = tmp_path / REPAIR_LOCK_PATH.name
    lock_copy.write_bytes(REPAIR_LOCK_PATH.read_bytes())
    (tmp_path / LOCK_PATH.name).write_text('{"tag": "a-new-baseline"}')

    lock = shard.read_lock(lock_copy)

    assert lock == shard.read_lock(REPAIR_LOCK_PATH)
    assert lock["tag"] == "osworld_v2_0808_glm53_k3_agentic_repair_v2"
    assert lock["seed_label"] == 5300


def test_targeted_retry_selection_cannot_escape_owner_shard():
    assigned = tuple(f"task_{index:03d}" for index in range(37, 73))
    assert shard.requested_tasks(
        assigned, "task_041,task_042,task_047,task_055,task_056") == (
            "task_041", "task_042", "task_047", "task_055", "task_056")
    try:
        shard.requested_tasks(assigned, "task_036")
    except shard.PreflightError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("targeted retry escaped its frozen owner shard")


def test_repair_manifest_scope_is_stable_across_dependency_waves():
    lock = shard.read_lock(REPAIR_LOCK_PATH)
    first_wave = ("task_042", "task_047", "task_055", "task_056")
    second_wave = ("task_041",)

    assert shard.manifest_task_scope(lock, first_wave) == \
        shard.manifest_task_scope(lock, second_wave) == (
            "task_041", "task_042", "task_047", "task_055", "task_056")
