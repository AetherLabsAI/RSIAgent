"""Phase-2 policy is declared before execution and enforced on cached results."""
import json
from pathlib import Path

import pytest

from config.settings import Config
import benchmarks.osworld.pipeline as protocol
import benchmarks.osworld.phase2 as runner


REPO = Path(__file__).resolve().parents[1]


def make_spec(tmp_path, monkeypatch, policy="verifier_pass"):
    monkeypatch.setattr(protocol, "RESULTS_ROOT", tmp_path / "results")
    value = json.loads((REPO / "config/osworld/rsi.example.json").read_text())
    value["run_name"] = "policy_test"
    value["study_design"] = "held_out_generalization"
    value["phase1"]["distribution_file"] = "config/osworld_v2_training_distribution.md"
    value["phase2"].update(development_tasks=["task_095"], stop_policy=policy)
    value["phase3"]["held_out_tasks"] = ["task_099"]
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(value))
    return path


def learning_files(spec):
    root = spec["_resolved"]["run_root"]
    memory = root / "phase1/memory_frozen"
    memory.mkdir(parents=True)
    (memory / "lesson.md").write_text("A practice lesson")
    protocol._write_json_atomic(root / "phase1/result.json", {
        "status": "budget_exhausted", "memory_frozen_path": str(memory)})
    seed = spec["phase2"]["seed"]
    attempt = root / "phase2/task_095" / f"attempt_{seed:04d}"
    frozen = attempt / "memory_frozen"
    frozen.mkdir(parents=True)
    (frozen / "lesson.md").write_text("A practice lesson and target experience")
    result = {
        "phase2_stop_policy": spec["_resolved"]["phase2_stop_policy"],
        "status": "verifier_pass",
        "target_verifier_verdict": "PASS",
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
        "initial_memory_tree_sha256": protocol._memory_record(memory)["tree_sha256"],
        "final_memory_path": str(frozen),
        "final_memory_tree_sha256": protocol._memory_record(frozen)["tree_sha256"],
    }
    return attempt / "result.json", result


@pytest.mark.parametrize("declared_policy,policy", [
    (None, "curriculum_review"),
    ("verifier_pass", "verifier_pass"),
    ("curriculum_review", "curriculum_review"),
])
def test_policy_reaches_child_result_and_protocol_lock(
        tmp_path, monkeypatch, declared_policy, policy):
    path = make_spec(tmp_path, monkeypatch, declared_policy)
    if declared_policy is None:
        value = json.loads(path.read_text())
        del value["phase2"]["stop_policy"]
        path.write_text(json.dumps(value))
    spec = protocol.load_protocol(path)
    result_path, result = learning_files(spec)
    result["status"] = ("verifier_pass_curriculum_ready" if policy == "curriculum_review"
                        else "verifier_pass")
    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        assert argv[argv.index("--phase2-stop-policy") + 1] == policy
        assert kwargs["phase"] == "phase2"
        protocol._write_json_atomic(result_path, result)

    monkeypatch.setattr(protocol, "_run", launch)
    completed = protocol.execute_phase2(spec)
    assert completed["phase2_stop_policy"] == policy
    assert completed["task_results"][0]["stop_policy"] == policy
    assert completed["task_results"][0]["verdict"] == "PASS"
    assert completed["memory_frozen"]["tree_sha256"] == result["final_memory_tree_sha256"]
    assert protocol._protocol_lock(spec, path.read_bytes())["phase2_stop_policy"] == policy
    # A restart may consume only a result with the same declared stopping policy.
    assert protocol.execute_phase2(spec) == completed
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", [
    {"phase2_stop_policy": "curriculum_review"},
    {"phase2_stop_policy": None},
    {"status": "target_harness_unverified", "target_verifier_verdict": "UNVERIFIED"},
    {"status": "verifier_pass", "target_verifier_verdict": "FAIL"},
    {"status": "verifier_pass_curriculum_ready"},
])
def test_cached_results_cannot_bypass_policy_or_completion_checks(tmp_path, monkeypatch, mutation):
    spec = protocol.load_protocol(make_spec(tmp_path, monkeypatch))
    result_path, result = learning_files(spec)
    result.update(mutation)
    protocol._write_json_atomic(result_path, result)
    monkeypatch.setattr(protocol, "_run", lambda *_a, **_kw: pytest.fail("must not launch"))
    with pytest.raises(protocol.ProtocolError):
        protocol.execute_phase2(spec)
    assert not (spec["_resolved"]["run_root"] / "phase2/result.json").exists()


def test_new_default_cannot_resume_a_run_locked_to_stop_on_pass(tmp_path, monkeypatch):
    path = make_spec(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    del value["phase2"]["stop_policy"]
    path.write_text(json.dumps(value))
    spec = protocol.load_protocol(path)
    root = spec["_resolved"]["run_root"]
    root.mkdir(parents=True)
    (root / "protocol.json").write_bytes(path.read_bytes())
    old_lock = protocol._protocol_lock(spec, path.read_bytes())
    old_lock["phase2_stop_policy"] = "verifier_pass"
    protocol._write_json_atomic(root / "protocol_lock.json", old_lock)
    monkeypatch.setattr(protocol, "preflight", lambda *_a: {})
    monkeypatch.setattr(protocol, "execute_phase2", lambda *_a: pytest.fail("must not launch"))

    with pytest.raises(protocol.ProtocolError, match="drifted"):
        protocol.main(["--protocol", str(path), "--phase", "phase2",
                       "--execute", "RUN-RECURSIVE-IMPROVEMENT-PHASE2"])
    assert json.loads((root / "protocol_lock.json").read_text()) == old_lock
    assert not (root / "phase2/result.json").exists()


@pytest.mark.parametrize("declared_policy,policy", [
    (None, "curriculum_review"),
    ("verifier_pass", "verifier_pass"),
    ("curriculum_review", "curriculum_review"),
])
def test_direct_runner_resolves_policy_before_loading_configs(
        tmp_path, monkeypatch, declared_policy, policy):
    class ConfigsReached(Exception):
        pass

    def load_configs(args):
        assert args.phase2_stop_policy == policy
        metadata = runner.phase2_protocol_metadata(args.phase2_stop_policy)
        assert metadata["curriculum_after_pass"] is (policy == "curriculum_review")
        raise ConfigsReached

    monkeypatch.setattr(runner, "_install_paths", lambda: None)
    monkeypatch.setattr(runner, "RECURSIVE_RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(runner, "_load_configs", load_configs)
    argv = ["task_080",  "--protocol-run", "policy_test",
            "--initial-memory", str(tmp_path), "--preflight"]
    if declared_policy is not None:
        argv.extend(["--phase2-stop-policy", declared_policy])
    with pytest.raises(ConfigsReached):
        runner.main(argv)




@pytest.mark.parametrize("invalid", ["converged", "", None, True, []])
def test_unknown_stop_policies_fail_before_launch(tmp_path, monkeypatch, invalid):
    path = make_spec(tmp_path, monkeypatch, invalid)
    with pytest.raises(protocol.ProtocolError, match="phase2.stop_policy"):
        protocol.load_protocol(path)


@pytest.mark.parametrize("policy,review", [
    (None, True), ("verifier_pass", False), ("curriculum_review", True),
])
def test_manifest_records_actual_pass_transition(tmp_path, policy, review):
    actor = tmp_path / "actor.yaml"
    actor.write_text("model: test-actor\n")
    root = tmp_path / "attempt"
    runner._write_phase2_manifest(
        root, task_id="task_080", protocol_run="test_policy",
        instruction="task instruction", target_direction="task direction",
        initial_memory_path=tmp_path, initial_memory={},
        configs={"target_actor": Config()}, paths={"target_actor": actor},
        benchmark_provenance={}, **({"stop_policy": policy} if policy is not None else {}))
    record = json.loads((root / "manifest.json").read_text())["protocol"]
    assert record["stop_policy"] == (policy or "curriculum_review")
    assert record["curriculum_after_pass"] is review
    assert ("curriculum" in record["pass_transition"]) is review
    assert record["official_evaluator_calls"] == 0


@pytest.mark.parametrize("example", [
    "osworld/rsi.example.json",
])
def test_public_examples_declare_curriculum_review(example):
    value = json.loads((REPO / "config" / example).read_text())
    assert value["phase2"]["stop_policy"] == "curriculum_review"


def test_curriculum_stall_is_recorded_as_a_failed_final_test_not_a_pass():
    result = {"phase2_stop_policy": "curriculum_review",
              "status": "curriculum_stalled_final_target_test",
              "target_verifier_verdict": "FAIL"}
    protocol._validate_phase2_terminal(result, stop_policy="curriculum_review", task="task_080")
    assert result["target_verifier_verdict"] == "FAIL"
