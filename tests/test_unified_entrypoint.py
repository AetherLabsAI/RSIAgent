"""Static boundary checks for the paid unified OSWorld entrypoint."""

import hashlib
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import run_unified as unified_runner
from config.settings import Config, load
from explore.unified_evolution import evolve_until_ready

ROOT = Path(__file__).resolve().parents[1]


def test_unified_mode_is_opt_in_and_cannot_change_frozen_benchmark_runner():
    assert Config().verifier_evolve_route is False
    assert Config().verifier_hide_actor_memory is False
    ordinary = (ROOT / "run_task.py").read_text(encoding="utf-8")
    assert "verifier_evolve_route belongs to the unified-loop entrypoint" in ordinary


def test_external_evaluator_is_strictly_after_unified_handoff():
    source = (ROOT / "run_unified.py").read_text(encoding="utf-8")

    loop_call = source.index("unified = run_unified_loop(")
    evaluator_attach = source.index(
        "final_environment.desktop._set_task_info(evaluator_task)")
    grade_call = source.index("final_environment.desktop.evaluate")
    assert loop_call < evaluator_attach < grade_call
    assert source.count(".evaluate") == 1
    hook_block = source[
        source.index("UnifiedLoopHooks(", loop_call):
        source.index("# SEALED EXTERNAL MEASUREMENT", loop_call)]
    assert "evaluate" not in hook_block
    assert "official_evaluator_feedback_entered_loop\": False" in source
    assert "desktop.reset(task_config=setup_only_target)" in source
    assert "desktop.reset(task_config=task_config)" not in source
    assert "sealed_target_tasks[id(desktop)] = task_config" in source


def test_sealed_evaluator_uses_frozen_lock_and_fails_closed_before_run(
        tmp_path, monkeypatch):
    target = tmp_path / "actor.yaml"
    target.write_text("model: z-ai/glm-5.3\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-secret\n", encoding="utf-8")
    lock = tmp_path / "benchmark.lock.json"
    lock.write_text(json.dumps({
        "actor_agent": {
            "model": "z-ai/glm-5.3",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        },
        "evaluator": {
            "provider": "openai_compatible",
            "model": "openai/gpt-5.4",
            "base_url": "https://openrouter.ai/api/v1",
            "website_host_suffix": "web.hku.icu",
            "retry_attempts": 5,
            "retry_delay_seconds": 15,
            "visibility": "sealed_after_actor_verifier_loop",
        },
    }), encoding="utf-8")
    for name in (
            "OPENROUTER_API_KEY", "WEBSITE_HOST_SUFFIX",
            "OSWORLD_EVAL_MODEL_PROVIDER", "OSWORLD_EVAL_MODEL_NAME",
            "OSWORLD_EVAL_MODEL_BASE_URL", "OSWORLD_EVAL_MODEL_API_KEY_ENV",
            "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS",
            "OSWORLD_EVAL_MODEL_RETRY_DELAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(unified_runner, "resolve_env_file",
                        lambda **_kwargs: env_file)

    record = unified_runner._configure_sealed_evaluator(
        lock, SimpleNamespace(model="z-ai/glm-5.3"), target)

    assert record["model"] == "openai/gpt-5.4"
    assert record["credential"] == "configured"
    assert "test-secret" not in json.dumps(record)
    assert unified_runner.os.environ["OSWORLD_EVAL_MODEL_PROVIDER"] == \
        "openai_compatible"
    assert unified_runner.os.environ["OSWORLD_EVAL_MODEL_API_KEY_ENV"] == \
        "OPENROUTER_API_KEY"


def test_sealed_evaluator_transport_probe_contains_no_task_data(monkeypatch):
    captured = {}

    def fake_generate(prompt, **options):
        captured.update(prompt=prompt, options=options)
        return "READY"

    import sys
    from types import ModuleType
    # The probe contract can be checked without installing the benchmark client.
    client = ModuleType("desktop_env.evaluators.model_client")
    client.generate_text = fake_generate
    monkeypatch.setitem(sys.modules, client.__name__, client)
    result = unified_runner._probe_sealed_evaluator_transport()

    assert result["status"] == "ready"
    assert captured["prompt"] == \
        "Evaluator transport preflight only. Reply exactly READY."
    assert "task" not in captured["prompt"].lower()


def test_evolution_adapter_has_no_target_or_evaluator_callback():
    parameters = inspect.signature(evolve_until_ready).parameters
    assert "evaluator" not in parameters
    assert "reset_target_vm" not in parameters
    source = inspect.getsource(evolve_until_ready)
    assert ".evaluate(" not in source
    assert "READY_FOR_TARGET_TEST" not in source
    assert "READY_FOR_RETRY" in source


def test_unified_entrypoint_does_not_nest_old_target_gated_runner():
    source = (ROOT / "run_unified.py").read_text(encoding="utf-8")
    assert "e15_v12_evolve(" not in source
    assert "evolve_until_ready(" in source


def test_unified_target_learning_precedes_curriculum_and_teardown():
    state_machine = (ROOT / "core/unified_loop.py").read_text(encoding="utf-8")
    learning = state_machine.index("learning = hooks.actor_learn(")
    release = state_machine.index(
        "hooks.release_target_environment(environment)", learning)
    evolution = state_machine.index("memory = hooks.evolve(", release)
    assert learning < release < evolution

    runner = (ROOT / "run_unified.py").read_text(encoding="utf-8")
    assert "_promote_learning(" in runner
    assert "session=curriculum_session" in runner
    assert "verifier_hide_actor_memory=True" in runner


def test_unified_defaults_use_role_separated_k3_curriculum_and_two_key_handoff():
    actor = load(str(unified_runner.DEFAULT_EVOLUTION_CONFIGS["actor"]))
    verifier = load(str(unified_runner.DEFAULT_EVOLUTION_CONFIGS["verifier"]))
    curriculum = load(str(
        unified_runner.DEFAULT_EVOLUTION_CONFIGS["curriculum"]))
    memory_actor = load(str(
        unified_runner.DEFAULT_EVOLUTION_CONFIGS["memory"]))

    assert actor.model == "z-ai/glm-5.3"
    assert verifier.model == "moonshotai/kimi-k3"
    assert curriculum.model == "moonshotai/kimi-k3"
    assert len({actor.model, verifier.model, curriculum.model}) == 2
    assert curriculum.reasoning_effort == "max"
    assert (unified_runner.DEFAULT_EVOLUTION_CONFIGS["curriculum"]
            != unified_runner.DEFAULT_EVOLUTION_CONFIGS["verifier"])
    assert memory_actor.model == actor.model
    assert (unified_runner.DEFAULT_EVOLUTION_CONFIGS["memory"]
            == unified_runner.DEFAULT_EVOLUTION_CONFIGS["actor"])

    runner = (ROOT / "run_unified.py").read_text(encoding="utf-8")
    assert "route_target_pass(" in runner
    assert "verifier_pass_router=curriculum_review_pass" in runner
    assert 'curriculum_route == "HANDOFF"' in runner
    assert "session.detach_executor(environment.vm, preserve=True)" in runner
    assert "verifier_session_prepare=ensure_verifier_orientation" in runner
    assert "LAZY_ESCALATION_CLONE" in runner
    assert "clone_s0[\"sha256\"] != expected_s0" in runner


def test_unified_memory_is_adaptive_not_mislabeled_off():
    source = (ROOT / "run_unified.py").read_text(encoding="utf-8")
    assert '"memory_mode": "adaptive"' in source
    assert "opening_extra = memory_preamble(listing)" in source
    assert "env_memory_dir=(str(active_memory_dir) if actor.memory else \"\")" \
        in source


def test_t003_is_a_reviewed_unified_target_surface():
    runner = (ROOT / "run_unified.py").read_text(encoding="utf-8")
    surfaces = (ROOT / "explore/e15_v12_loop.py").read_text(encoding="utf-8")
    setup = (ROOT / "run_e15.py").read_text(encoding="utf-8")
    assert "task_003" in unified_runner.SUPPORTED_TASKS
    assert '"task_003": {' in surfaces
    assert "class _Task003SetupOnly" in setup
    assert "CORRECT_CITY_NUM" not in setup
    assert "CORRECT_RAIN_FILTER" not in setup
    assert "CORRECT_SNOW_FILTER" not in setup


def test_current_batch_is_release_bound_and_has_reviewed_setup_surfaces():
    surfaces = (ROOT / "explore/e15_v12_loop.py").read_text(encoding="utf-8")
    setup = (ROOT / "run_e15.py").read_text(encoding="utf-8")
    manifest = json.loads((
        ROOT / "config/osworld_v2_0624_task_hashes.json"
    ).read_text(encoding="utf-8"))
    for task_id in unified_runner.SUPPORTED_TASKS:
        assert task_id in unified_runner.SUPPORTED_TASKS
        assert f'"{task_id}": {{' in surfaces
        assert task_id in setup
        assert f"{task_id}.py" in manifest["files"]
    assert manifest["task_count"] == len(manifest["files"]) == 108
    assert unified_runner.BENCHMARK_TASK_RELEASE == "osworld-v2-2026.06.24"
    assert "class _ReviewedDesktopSetupOnly" in setup


def test_compatibility_profile_fails_closed_across_release_boundary(
        tmp_path, monkeypatch):
    osworld = tmp_path / "OSWorld-V2"
    task_dir = osworld / "evaluation_examples" / "task_class"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task_003.py"
    task_path.write_text("class Task003:\n    instruction = 'test'\n",
                         encoding="utf-8")
    release_dir = osworld / "benchmark_releases"
    release_dir.mkdir()
    (release_dir / "osworld-v2-2026.06.24.json").write_text(
        json.dumps({"release": "osworld-v2-2026.06.24"}),
        encoding="utf-8")
    image = osworld / "docker_vm_data" / "image.qcow2"
    image.parent.mkdir()
    image.write_bytes(b"frozen image")

    subprocess.run(["git", "init", "-q", str(osworld)], check=True)
    subprocess.run(["git", "-C", str(osworld), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(osworld), "config", "user.name",
                    "Test"], check=True)
    subprocess.run(["git", "-C", str(osworld), "add", "."], check=True)
    subprocess.run(["git", "-C", str(osworld), "commit", "-qm", "fixture"],
                   check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(osworld), "rev-parse", "HEAD"], text=True).strip()

    task_manifest = tmp_path / "tasks.json"
    task_manifest.write_text(json.dumps({
        "task_count": 1,
        "files": {
            "task_003.py": {
                "size": task_path.stat().st_size,
                "sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            }
        },
    }), encoding="utf-8")
    assets = tmp_path / "assets"
    metadata = assets / ".cache/huggingface/download/file.bin.metadata"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("asset-commit\netag\n0\n", encoding="utf-8")
    (assets / "file.bin").write_bytes(b"asset")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "schema_version": 1,
        "profile": "osworld-v2-0624-runtime-0808-compat",
        "classification": "controlled-test",
        "benchmark_semantics": {
            "release": "osworld-v2-2026.06.24",
            "website_host_suffix": "web.hku.icu",
            "tasks": {
                "tag": "v2026.06.24",
                "hash_manifest": str(task_manifest),
                "hash_manifest_sha256": hashlib.sha256(
                    task_manifest.read_bytes()).hexdigest(),
                "task_count": 1,
            },
            "assets": {
                "tag": "v2026.06.24",
                "minimum_release_files": 1,
                "compatible_snapshot_commits": ["asset-commit"],
            },
        },
        "runtime": {"tag": "test-runtime", "commit": commit},
        "provider": {
            "name": "docker",
            "local_raw_image": {
                "path": "docker_vm_data/image.qcow2",
                "size": image.stat().st_size,
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(unified_runner, "OSWORLD_ROOT", osworld)
    monkeypatch.setenv("WEBSITE_HOST_SUFFIX", "web.hku.icu")
    monkeypatch.setenv("OSWORLD_FILE_BASE_URL", str(assets))

    record = unified_runner._benchmark_provenance(
        "task_003", task_path, profile)

    assert record["profile"] == "osworld-v2-0624-runtime-0808-compat"
    assert record["task_count_verified"] == 1
    assert record["runtime_checkout_commit"] == commit
    assert record["asset_metadata_commits"] == ["asset-commit"]
    assert record["vm_image_sha256"] == hashlib.sha256(
        image.read_bytes()).hexdigest()


def test_strict_0808_release_lock_verifies_tasks_runtime_website_and_assets(
        tmp_path, monkeypatch):
    osworld = tmp_path / "OSWorld-V2"
    task_dir = osworld / "evaluation_examples" / "task_class"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task_102.py"
    task_path.write_text(
        "class Task102:\n    instruction = 'test'\n", encoding="utf-8")

    release_name = "osworld-v2-2026.08.08"
    release_path = osworld / "benchmark_releases" / f"{release_name}.json"
    release_path.parent.mkdir()
    release_path.write_text(
        json.dumps({"release": release_name}), encoding="utf-8")

    assets = osworld / "cache" / "assets-0808"
    assets.mkdir(parents=True)
    (assets / "fixture.bin").write_bytes(b"release asset")
    marker_name = ".forge_osworld_release.json"
    marker = {
        "benchmark_release": release_name,
        "repository": "example/assets",
        "repo_type": "dataset",
        "tag": "v2026.08.08",
        "commit": "asset-0808",
        "content_identity": unified_runner._asset_content_identity(
            assets, marker_name),
    }
    (assets / marker_name).write_text(json.dumps(marker), encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(osworld)], check=True)
    subprocess.run(["git", "-C", str(osworld), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(osworld), "config", "user.name",
                    "Test"], check=True)
    subprocess.run(["git", "-C", str(osworld), "add", "."], check=True)
    subprocess.run(["git", "-C", str(osworld), "commit", "-qm", "fixture"],
                   check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(osworld), "rev-parse", "HEAD"], text=True).strip()

    monkeypatch.setattr(unified_runner, "OSWORLD_ROOT", osworld)
    identity = unified_runner._task_content_identity(task_dir)
    profile = tmp_path / "0808.lock.json"
    profile.write_text(json.dumps({
        "schema_version": 2,
        "benchmark": "OSWorld-v2",
        "benchmark_release": release_name,
        "release_manifest": {
            "path": f"benchmark_releases/{release_name}.json",
            "sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
        },
        "task_count": 1,
        "task_ids_sha256": identity["task_ids_sha256"],
        "task_files": {
            "tag": "v2026.08.08",
            "manifest_path": "manifests/task_hashes.json",
            "manifest_sha256": "manifest-0808",
            "content_tree_sha256": identity["content_tree_sha256"],
            "total_bytes": identity["total_bytes"],
        },
        "osworld_code": {"tag": "v2026.08.08", "commit": commit},
        "website": {"host_suffix": "site.hku.icu"},
        "task_assets": {
            "repository": "example/assets",
            "repo_type": "dataset",
            "tag": "v2026.08.08",
            "commit": "asset-0808",
            "local_dir": "cache/assets-0808",
            "release_marker": marker_name,
        },
        "provider_image": {"artifact_tag": "v2026.06.24"},
    }), encoding="utf-8")
    monkeypatch.setenv("WEBSITE_HOST_SUFFIX", "site.hku.icu")
    monkeypatch.setenv("OSWORLD_FILE_BASE_URL", str(assets))

    record = unified_runner._benchmark_provenance(
        "task_102", task_path, profile)

    assert record["profile"] == f"{release_name}-strict-release"
    assert record["classification"] == "strict_official_release"
    assert record["task_release"] == release_name
    assert record["task_count_verified"] == 1
    assert record["task_content_tree_sha256"] == \
        identity["content_tree_sha256"]
    assert record["runtime_checkout_commit"] == commit
    assert record["asset_content_tree_sha256"] == \
        marker["content_identity"]["content_tree_sha256"]


def test_public_instruction_extraction_never_executes_evaluator_module(
        tmp_path, monkeypatch):
    task_dir = tmp_path / "evaluation_examples" / "task_class"
    task_dir.mkdir(parents=True)
    (task_dir / "task_003.py").write_text(
        'PREFIX = "public"\n'
        'class Task003:\n'
        '    instruction = f"{PREFIX} instruction"\n'
        'raise RuntimeError("evaluator module executed")\n',
        encoding="utf-8")
    monkeypatch.setattr(unified_runner, "OSWORLD_ROOT", tmp_path)

    assert unified_runner._load_public_instruction("task_003") == \
        "public instruction"
