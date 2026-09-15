"""User-channel routing reaches phase children without configuring the grader."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import benchmarks.osworld.pipeline as pipeline
from config.benchmark_runtime import (
    BenchmarkRuntimeError,
    configure_user_simulator,
)
from config.settings import load
from core.actor import build_system


REPO = Path(__file__).resolve().parents[1]
ROUTE = {
    "provider": "openai_compatible",
    "model": "openai/test-user",
    "base_url": "https://user.invalid/v1",
    "api_key_env": "TEST_USER_KEY",
    "max_tokens": 256,
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(pipeline, "RSIAGENT_ROOT", tmp_path)


def make_study(tmp_path, monkeypatch, *, name="first", route=ROUTE):
    root = tmp_path / name
    memory = root / "phase1/memory_frozen"
    memory.mkdir(parents=True)
    (memory / "lesson.md").write_text("A practice lesson")
    pipeline._write_json_atomic(root / "phase1/result.json", {
        "status": "budget_exhausted", "memory_frozen_path": str(memory)})
    lock = root / "evaluation.lock.json"
    # Deliberately no evaluator model/credentials: learning needs only task setup.
    lock.write_text(json.dumps({
        "task_release": "osworld-v2-2026.08.08",
        "evaluator": {"website_host_suffix": "site.invalid"},
        "user_simulator": route,
    }))
    monkeypatch.setattr(pipeline, "BENCHMARK_PROVENANCE_PROFILES", {
        "osworld-v2-2026.08.08": lock})
    return {
        "run_name": name,
        "task_release": "osworld-v2-2026.08.08",
        "phase2": {"seed": 1},
        "_resolved": {
            "run_root": root,
            "development_tasks": ("task_024",),
            "phase2_stop_policy": "verifier_pass",
            "configs": {
                "phase2": {field: REPO / "config/roles/target.yaml" for field in (
                    "target_config", "practice_actor_config",
                    "practice_verifier_config", "curriculum_config", "memory_config")},
                "phase3": {"benchmark_lock": lock},
            },
        },
    }


def complete_child(spec):
    root = spec["_resolved"]["run_root"]
    memory = root / "phase1/memory_frozen"
    memory_hash = pipeline._memory_record(memory)["tree_sha256"]
    pipeline._write_json_atomic(root / "phase2/task_024/attempt_0001/result.json", {
        "status": "verifier_pass", "target_verifier_verdict": "PASS",
        "phase2_stop_policy": "verifier_pass",
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
        "initial_memory_tree_sha256": memory_hash,
        "final_memory_path": str(memory), "final_memory_tree_sha256": memory_hash,
    })


def test_phase2_launch_passes_user_route_and_preserves_actor_input(
        tmp_path, monkeypatch):
    spec = make_study(tmp_path, monkeypatch)
    os.environ["TEST_USER_KEY"] = "synthetic-key"
    cfg = load(str(REPO / "config/roles/target.yaml"))
    prompts = {enabled: build_system(cfg, ask_enabled=enabled)
               for enabled in (False, True)}
    before = dict(os.environ)
    calls = []

    def launch(argv, **kwargs):
        child = kwargs.get("env") or dict(os.environ)
        calls.append(child)
        assert child["OSWORLD_USER_SIM_MODEL"] == ROUTE["model"]
        assert child["OSWORLD_USER_SIM_PROVIDER"] == ROUTE["provider"]
        assert child["OSWORLD_USER_SIM_BASE_URL"] == ROUTE["base_url"]
        assert child["OSWORLD_USER_SIM_API_KEY_ENV"] == "TEST_USER_KEY"
        assert child["OSWORLD_USER_SIM_MAX_TOKENS"] == "256"
        assert child["TEST_USER_KEY"] == "synthetic-key"
        assert child["WEBSITE_HOST_SUFFIX"] == "site.invalid"
        assert not any(key.startswith("OSWORLD_EVAL_") for key in child)
        assert argv[argv.index("--target-config") + 1] == str(
            REPO / "config/roles/target.yaml")
        assert not any("synthetic-key" in argument for argument in argv)
        complete_child(spec)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pipeline.subprocess, "run", launch)
    assert pipeline.execute_phase2(spec)["status"] == "complete"
    assert len(calls) == 1
    assert dict(os.environ) == before
    assert {enabled: build_system(cfg, ask_enabled=enabled)
            for enabled in (False, True)} == prompts


def test_successive_studies_do_not_inherit_previous_user_route(tmp_path, monkeypatch):
    before = dict(os.environ)
    seen = []
    for name, route in (("first", ROUTE), ("second", None)):
        spec = make_study(tmp_path, monkeypatch, name=name, route=route)

        def launch(_argv, **kwargs):
            seen.append(kwargs.get("env") or dict(os.environ))
            complete_child(spec)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(pipeline.subprocess, "run", launch)
        pipeline.execute_phase2(spec)
    assert seen[0]["OSWORLD_USER_SIM_MODEL"] == ROUTE["model"]
    assert not any(key.startswith("OSWORLD_USER_SIM_") for key in seen[1])
    assert dict(os.environ) == before


def test_user_route_is_optional_and_missing_credential_does_not_block_learning(
        tmp_path, monkeypatch):
    for name, route in (("absent", None), ("declared", ROUTE)):
        spec = make_study(tmp_path, monkeypatch, name=name, route=route)
        environment = {"UNRELATED": "unchanged"}
        receipt = pipeline._configure_release_environment(
            spec, environment=environment)
        assert receipt["user_simulator"] == route
        assert environment["UNRELATED"] == "unchanged"
        assert "TEST_USER_KEY" not in environment
        assert not any(key.startswith("OSWORLD_EVAL_") for key in environment)


def test_release_user_route_matches_baseline():
    baseline = json.loads((REPO / "config/osworld/baseline.lock.json").read_text())
    evaluation = json.loads((REPO / "config/osworld/evaluation.lock.json").read_text())
    assert evaluation["user_simulator"] == baseline["user_simulator"]


def test_matching_user_configuration_is_preserved(tmp_path):
    environment = {
        **{f"OSWORLD_USER_SIM_{key.upper()}": str(value)
           for key, value in ROUTE.items()},
        "TEST_USER_KEY": "synthetic-key",
        "OSWORLD_USER_SIM_API_KEY": "explicit-direct-key",
        "OSWORLD_USER_SIM_TEMPERATURE": "0.3",
    }
    before = dict(environment)
    configure_user_simulator(
        ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
        environment=environment)
    assert environment == before


def test_conflicting_user_configuration_is_not_silently_replaced(tmp_path):
    environment = {"TEST_USER_KEY": "synthetic-key",
                   "OSWORLD_USER_SIM_MODEL": "other/model"}
    before = dict(environment)
    with pytest.raises(BenchmarkRuntimeError) as caught:
        configure_user_simulator(
            ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment)
    assert environment == before
    assert "synthetic-key" not in str(caught.value)


def test_baseline_credential_loads_from_configured_env_file(tmp_path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text("TEST_USER_KEY=synthetic-key\n")
    environment = {"RSIAGENT_ENV_FILE": str(env_file)}
    receipt = configure_user_simulator(
        ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
        environment=environment)
    assert environment["TEST_USER_KEY"] == "synthetic-key"
    assert "synthetic-key" not in json.dumps(receipt)


@pytest.mark.parametrize("route", [[], {}, {**ROUTE, "model": ""}])
def test_incomplete_declared_user_route_remains_an_error(tmp_path, route):
    environment = {}
    with pytest.raises(BenchmarkRuntimeError):
        configure_user_simulator(
            route, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment, load_credential=False)
    assert environment == {}


def initialized_child(tmp_path, monkeypatch, *, module, route=ROUTE,
                      shell=None, repo_env="", osworld_env=""):
    """Exercise real child initialization after the pipeline constructs its env."""
    spec = make_study(tmp_path, monkeypatch, route=route)
    osworld = tmp_path / "OSWorld-V2"
    osworld.mkdir()
    (osworld / ".env").write_text(osworld_env)
    (tmp_path / ".env").write_text(repo_env)
    environment = {
        "PYTHONPATH": str(REPO),
        "RSIAGENT_ROOT": str(tmp_path),
        "OSWORLD_ROOT": str(osworld),
        **(shell or {}),
    }
    pipeline._configure_release_environment(spec, environment=environment)
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib, json, os, sys
from llm.client import _api_key
importlib.import_module(sys.argv[1])._install_paths()
name = os.environ.get("OSWORLD_USER_SIM_API_KEY_ENV", "")
print(json.dumps({
    "actor_key": _api_key(),
    "shared_key": os.environ.get("OPENROUTER_API_KEY"),
    "named_key": os.environ.get(name),
    "user_key": os.environ.get("OSWORLD_USER_SIM_API_KEY"),
}))
""", module],
        cwd=REPO, env=environment, text=True, capture_output=True, check=True)
    return environment, json.loads(result.stdout)


@pytest.mark.parametrize("module", [
    "benchmarks.osworld.phase1", "benchmarks.osworld.runtime"])
@pytest.mark.parametrize("shell_key", [None, "shell-key"])
def test_real_child_keeps_original_actor_credential_precedence(
        tmp_path, monkeypatch, module, shell_key):
    _, child = initialized_child(
        tmp_path, monkeypatch, module=module,
        route={**ROUTE, "api_key_env": "OPENROUTER_API_KEY"},
        shell={"OPENROUTER_API_KEY": shell_key} if shell_key else {},
        repo_env="OPENROUTER_API_KEY=repo-stale-key\n",
        osworld_env="OPENROUTER_API_KEY=osworld-working-key\n")
    expected = shell_key or "osworld-working-key"
    assert child == {
        "actor_key": expected, "shared_key": expected,
        "named_key": expected, "user_key": None}


@pytest.mark.parametrize("key_env", ["OPENROUTER_API_KEY", "CUSTOM_USER_KEY"])
def test_real_runtime_loads_repo_fallback_only_into_user_channel(
        tmp_path, monkeypatch, key_env):
    repo_env = "OPENROUTER_API_KEY=repo-actor-key\n"
    expected = "repo-actor-key"
    if key_env != "OPENROUTER_API_KEY":
        repo_env += "CUSTOM_USER_KEY=repo-user-key\n"
        expected = "repo-user-key"
    environment, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        route={**ROUTE, "api_key_env": key_env}, repo_env=repo_env)
    assert key_env not in environment
    assert "OSWORLD_USER_SIM_API_KEY" not in environment
    assert child == {
        "actor_key": "repo-actor-key", "shared_key": None,
        "named_key": None, "user_key": expected}


def test_real_runtime_preserves_custom_osworld_user_credential(
        tmp_path, monkeypatch):
    _, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        repo_env="TEST_USER_KEY=repo-stale-key\n",
        osworld_env="TEST_USER_KEY=osworld-user-key\nOPENROUTER_API_KEY=actor-key\n")
    assert child == {
        "actor_key": "actor-key", "shared_key": "actor-key",
        "named_key": "osworld-user-key", "user_key": None}


def test_real_runtime_keeps_explicit_user_override_without_loading_other_keys(
        tmp_path, monkeypatch):
    environment, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        shell={"OSWORLD_USER_SIM_API_KEY": "direct-user-key"},
        repo_env="TEST_USER_KEY=repo-stale-key\nOPENROUTER_API_KEY=actor-key\n")
    assert "TEST_USER_KEY" not in environment
    assert child == {
        "actor_key": "actor-key", "shared_key": None,
        "named_key": None, "user_key": "direct-user-key"}


@pytest.mark.parametrize("route", [None, ROUTE])
def test_real_runtime_does_not_require_a_user_credential(
        tmp_path, monkeypatch, route):
    _, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime", route=route)
    assert child == {
        "actor_key": "", "shared_key": None,
        "named_key": None, "user_key": None}


def test_baseline_user_binding_still_requires_the_declared_credential(tmp_path):
    environment = {"OSWORLD_USER_SIM_API_KEY": "direct-user-key"}
    with pytest.raises(BenchmarkRuntimeError, match="TEST_USER_KEY"):
        configure_user_simulator(
            ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment)
