"""Frozen full-baseline configuration, sharding, and leakage-boundary checks."""
from dataclasses import fields
import hashlib
import json
from pathlib import Path

import yaml

from config.settings import Config, load
from tools import run_osworld_v2_baseline_shard as shard
from tools import summarize_osworld_v2_baseline as summarize


REPO = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO / "config/osworld_v2_glm53_k3_baseline.lock.json"
ACTOR_PATH = REPO / "config/osworld_v2_glm53_k3_agentic_baseline.yaml"
VERIFIER_PATH = REPO / "config/osworld_v2_k3_agentic_verifier.yaml"
ESCALATED_VERIFIER_PATH = (
    REPO / "config/osworld_v2_glm53_agentic_verifier.yaml")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_root(tmp_path: Path) -> Path:
    root = tmp_path / "OSWorld-V2/evaluation_examples/task_class"
    root.mkdir(parents=True)
    for task in shard.EXPECTED_TASKS:
        (root / f"{task}.py").touch()
    return root


def test_lock_binds_all_role_configs_and_memory_off():
    lock = json.loads(LOCK_PATH.read_text())
    assert lock["actor_agent"]["sha256"] == _sha256(ACTOR_PATH)
    assert lock["verifier_agent"]["sha256"] == _sha256(VERIFIER_PATH)
    assert lock["escalation"]["verifier_config_sha256"] == _sha256(
        ESCALATED_VERIFIER_PATH)
    assert lock["memory"] == {"enabled": False, "directory": ""}
    assert lock["tag"] == "glm53_k3_agentic_baseline_v3"
    assert lock["evaluator"]["model"] == "openai/gpt-5.4"

    known = {field.name for field in fields(Config)}
    for path in (ACTOR_PATH, VERIFIER_PATH, ESCALATED_VERIFIER_PATH):
        raw = yaml.safe_load(path.read_text())
        assert set(raw) <= known, f"unknown config keys: {set(raw) - known}"


def test_actor_and_verifier_roles_are_exactly_frozen():
    actor = load(str(ACTOR_PATH))
    verifier = load(str(VERIFIER_PATH))
    escalated_verifier = load(str(ESCALATED_VERIFIER_PATH))

    assert actor.model == "z-ai/glm-5.3"
    assert actor.provider_order == ["Z.AI"]
    assert actor.provider_require_parameters is True
    assert (actor.reasoning_effort, actor.max_tokens, actor.temperature,
            actor.primary_temperature, actor.top_p) \
        == ("max", 65536, 1.0, 1.0, 1.0)
    assert (actor.vision_model, actor.vision_rounds, actor.look_ensemble) \
        == ("moonshotai/kimi-k3", 1, 1)
    assert actor.escalation_model == "moonshotai/kimi-k3"
    assert actor.escalation_verifier_cross_model is True
    assert actor.escalation_primary_temperature == 1.0
    assert actor.escalation_provider_order == []
    assert actor.env_memory_dir == ""
    assert (REPO / actor.agentic_verifier_config).resolve() == VERIFIER_PATH
    assert (REPO / actor.escalation_agentic_verifier_config).resolve() \
        == ESCALATED_VERIFIER_PATH

    assert verifier.model == "moonshotai/kimi-k3"
    assert (verifier.reasoning_effort, verifier.max_tokens,
            verifier.primary_temperature, verifier.top_p) \
        == ("max", 65536, 1.0, 1.0)
    assert verifier.agent_decided_stop is True
    assert verifier.history_keep_pairs == 0
    assert verifier.independent_verify is False
    assert (verifier.vision_model, verifier.vision_rounds,
            verifier.look_ensemble) == ("", 1, 1)

    assert escalated_verifier.model == "z-ai/glm-5.3"
    assert escalated_verifier.provider_order == ["Z.AI"]
    assert (escalated_verifier.vision_model,
            escalated_verifier.vision_rounds,
            escalated_verifier.look_ensemble) == (
                "moonshotai/kimi-k3", 1, 1)
    assert escalated_verifier.history_keep_pairs == 0
    assert escalated_verifier.independent_verify is False


def test_three_owned_ranges_are_disjoint_complete_and_stable(tmp_path):
    tasks = shard.discover_tasks(_task_root(tmp_path))
    lock = json.loads(LOCK_PATH.read_text())
    assignments = [shard.shard_assignment(lock, index) for index in range(3)]
    arms = [set(shard.shard_tasks(tasks, assignment))
            for assignment in assignments]
    assert [assignment["owner"] for assignment in assignments] == [
        "xinyue", "shicheng", "sibo"]
    assert [len(arm) for arm in arms] == [36, 37, 35]
    assert not (arms[0] & arms[1] or arms[0] & arms[2] or arms[1] & arms[2])
    assert set.union(*arms) == set(tasks) == set(shard.EXPECTED_TASKS)
    assert tuple(shard.shard_tasks(tasks, assignments[0])[:2]) == (
        "task_001", "task_002")
    assert tuple(shard.shard_tasks(tasks, assignments[1])[:2]) == (
        "task_037", "task_038")
    assert tuple(shard.shard_tasks(tasks, assignments[2])[-2:]) == (
        "task_107", "task_108")

    assert shard.owner_assignment(lock, "XINYUE") == assignments[0]
    assert shard.owner_assignment(lock, "shicheng") == assignments[1]
    assert shard.owner_assignment(lock, "sibo") == assignments[2]


def test_launcher_validates_frozen_inputs_without_loading_task_modules(tmp_path):
    lock, actor, verifier, escalated_verifier = shard.validate_lock(
        REPO, LOCK_PATH, _task_root(tmp_path))
    assert lock["task_count"] == 108
    assert actor == ACTOR_PATH and verifier == VERIFIER_PATH
    assert escalated_verifier == ESCALATED_VERIFIER_PATH

    source = (REPO / "tools/run_osworld_v2_baseline_shard.py").read_text()
    assert "from task_loader" not in source
    assert "desktop_env" not in source
    assert ".evaluate(" not in source


def test_runner_persists_provider_provenance_in_result():
    source = (REPO / "run_task.py").read_text()
    assert '"llm_providers": provider_counts()' in source

    lock = json.loads(LOCK_PATH.read_text())
    good = {
        "task": "task_001", "seed": lock["seed_label"], "tag": lock["tag"],
        "model": "z-ai/glm-5.3", "llm_providers": {"z-ai/glm-5.3": {"Z.AI": 1}},
    }
    shard.validate_result_identity(good, "task_001", lock)


def test_launcher_propagates_actual_runtime_roots(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("FORGE_ROOT", "/stale/forge")
    monkeypatch.setenv("OSWORLD_ROOT", "/stale/osworld")
    monkeypatch.setenv("FORGE_ENV_FILE", "/stale/forge.env")
    lock = json.loads(LOCK_PATH.read_text())
    osworld_root = REPO.parent / "OSWorld-V2"

    environment = shard.benchmark_environment(REPO, osworld_root, lock)

    assert environment["FORGE_ROOT"] == str(REPO)
    assert environment["OSWORLD_ROOT"] == str(osworld_root)
    assert environment["FORGE_ENV_FILE"] == str(REPO / ".env")
    assert environment["OSWORLD_EVAL_MODEL_NAME"] == "openai/gpt-5.4"


def test_aggregator_requires_and_summarizes_the_exact_108_results(tmp_path):
    lock = json.loads(LOCK_PATH.read_text())
    seed, tag = lock["seed_label"], lock["tag"]
    for task in summarize.TASKS:
        path = tmp_path / task / f"seed{seed}_{tag}" / "result.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "task": task, "seed": seed, "tag": tag,
            "model": "z-ai/glm-5.3", "score": 0.5, "status": "done",
            "llm_providers": {
                "z-ai/glm-5.3": {"Z.AI": 2},
                "moonshotai/kimi-k3": {"Moonshot AI": 1},
            },
        }))

    summary = summarize.collect([tmp_path], lock)

    assert summary["completed_tasks"] == summary["valid_scored_tasks"] == 108
    assert summary["mean_score"] == summary["median_score"] == 0.5
    assert summary["missing_tasks"] == summary["invalid_results"] == []
    assert summary["actor_provider_fallback_tasks"] == []
    assert summary["provider_calls"]["z-ai/glm-5.3"]["Z.AI"] == 216
