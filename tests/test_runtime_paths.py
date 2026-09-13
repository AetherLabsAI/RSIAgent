"""Machine-portable runtime path resolution for the OSWorld baseline."""
import hashlib
import json
from pathlib import Path

import pytest

from config.benchmark_runtime import (
    BenchmarkRuntimeError,
    configure_associated_benchmark_lock,
)

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_env_file,
    resolve_forge_path,
    resolve_forge_root,
    resolve_osworld_root,
)
from config.settings import load


REPO = Path(__file__).resolve().parents[1]


def test_runtime_paths_default_to_this_checkout_and_its_sibling():
    forge_root = resolve_forge_root({})

    assert forge_root == REPO
    assert resolve_osworld_root({}, forge_root) == REPO.parent / "OSWorld-V2"
    assert resolve_env_file({}, forge_root) == REPO / ".env"


def test_runtime_paths_accept_explicit_machine_layout(tmp_path):
    environment = {
        "FORGE_ROOT": str(tmp_path / "forge-checkout"),
        "OSWORLD_ROOT": str(tmp_path / "benchmarks" / "OSWorld-V2"),
        "FORGE_ENV_FILE": str(tmp_path / "secrets" / "forge.env"),
    }

    forge_root = resolve_forge_root(environment)

    assert forge_root == (tmp_path / "forge-checkout").resolve()
    assert resolve_osworld_root(environment, forge_root) == (
        tmp_path / "benchmarks" / "OSWorld-V2").resolve()
    assert resolve_env_file(environment, forge_root) == (
        tmp_path / "secrets" / "forge.env").resolve()


def test_forge_owned_paths_ignore_osworld_working_directory(
        monkeypatch, tmp_path):
    fake_osworld = tmp_path / "OSWorld-V2"
    fake_osworld.mkdir()
    monkeypatch.chdir(fake_osworld)

    actor_path = resolve_forge_path(
        "config/osworld_v2_glm53_k3_agentic_baseline.yaml",
        forge_root=REPO)
    actor = normalize_verifier_config_paths(load(str(actor_path)), REPO)

    assert actor_path == (
        REPO / "config/osworld_v2_glm53_k3_agentic_baseline.yaml")
    assert Path(actor.agentic_verifier_config) == (
        REPO / "config/osworld_v2_k3_agentic_verifier.yaml")
    assert Path(actor.escalation_agentic_verifier_config) == (
        REPO / "config/osworld_v2_glm53_agentic_verifier.yaml")
    assert load(actor.agentic_verifier_config).model == "moonshotai/kimi-k3"
    assert load(actor.escalation_agentic_verifier_config).model == "z-ai/glm-5.3"


def test_official_runtime_has_no_person_specific_absolute_path():
    for relative_path in (
            "run_task.py",
            "llm/client.py",
            "tools/run_osworld_v2_baseline_shard.py",
            "config/osworld_v2_glm53_k3_agentic_baseline.yaml"):
        source = (REPO / relative_path).read_text(encoding="utf-8")
        assert "/home/sibo" not in source
        assert "/home/admin" not in source


def test_direct_run_inherits_evaluator_and_user_channel_from_sibling_lock(
        tmp_path):
    forge = tmp_path / "forge"
    config_dir = forge / "config"
    config_dir.mkdir(parents=True)
    actor = config_dir / "experiment.yaml"
    actor.write_text("model: example/actor\n", encoding="utf-8")
    lock = {
        "benchmark_release": "test-release",
        "actor_agent": {
            "config": "config/experiment.yaml",
            "sha256": hashlib.sha256(actor.read_bytes()).hexdigest(),
        },
        "evaluator": {
            "provider": "openai_compatible",
            "model": "openai/test-evaluator",
            "base_url": "https://router.invalid/v1",
            "api_key_env": "TEST_ROUTER_KEY",
            "retry_attempts": 5,
            "retry_delay_seconds": 15,
            "website_host_suffix": "site.invalid",
        },
        "user_simulator": {
            "provider": "openai_compatible",
            "model": "openai/test-user",
            "base_url": "https://router.invalid/v1",
            "api_key_env": "TEST_ROUTER_KEY",
            "max_tokens": 256,
        },
    }
    lock_path = actor.with_suffix(".lock.json")
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    environment = {"TEST_ROUTER_KEY": "secret"}

    receipt = configure_associated_benchmark_lock(
        actor, forge_root=forge, environment=environment)

    assert receipt["benchmark_release"] == "test-release"
    assert environment["OSWORLD_EVAL_MODEL_NAME"] == \
        "openai/test-evaluator"
    assert environment["OSWORLD_EVAL_MODEL_API_KEY_ENV"] == "TEST_ROUTER_KEY"
    assert environment["OSWORLD_USER_SIM_MODEL"] == "openai/test-user"
    assert environment["WEBSITE_HOST_SUFFIX"] == "site.invalid"
    assert "secret" not in json.dumps(receipt)


def test_direct_run_rejects_config_drift_before_actor_starts(tmp_path):
    forge = tmp_path / "forge"
    config_dir = forge / "config"
    config_dir.mkdir(parents=True)
    actor = config_dir / "experiment.yaml"
    actor.write_text("model: changed/actor\n", encoding="utf-8")
    lock = {
        "actor_agent": {
            "config": "config/experiment.yaml",
            "sha256": "0" * 64,
        },
        "evaluator": {},
    }
    actor.with_suffix(".lock.json").write_text(
        json.dumps(lock), encoding="utf-8")

    with pytest.raises(BenchmarkRuntimeError, match="does not match"):
        configure_associated_benchmark_lock(
            actor, forge_root=forge, environment={"TEST_ROUTER_KEY": "secret"})
