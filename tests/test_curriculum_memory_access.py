"""Isolate Curriculum file visibility while preserving Actor learning."""
import json
from pathlib import Path

import pytest

from config.settings import Config, load
from explore import unified_evolution as U
from explore.charter import self_evolving_curriculum_charter
import benchmarks.osworld.phase2 as runner
from test_unified_evolution import _VM, _hooks


@pytest.mark.parametrize("access", ["read_only", "none"])
@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_visibility_changes_only_curriculum_across_practice_learning(
        tmp_path, monkeypatch, access, verdict):
    vm = _VM()
    vm.memory = {"stale.md": b"must not survive an empty attachment"}
    initial = {"skill.md": b"original Phase1 knowledge"}
    learned = {"skill.md": b"original knowledge plus practice lesson"}
    prompts = []
    hooks = _hooks(vm, [
        "DECISION: PROJECT\nBuild an artifact in /home/user/evolution_project.",
        "DECISION: READY_FOR_TARGET\nReturn to the unchanged target.",
    ], prompts)
    observations = []
    original_attempt = hooks.run_attempt

    def curriculum_attempt(*args, **kwargs):
        observations.append(dict(vm.memory))
        return original_attempt(*args, **kwargs)

    hooks.run_attempt = curriculum_attempt

    def practice(**kwargs):
        # The same original memory remains the source for the fresh Actor.
        assert U._read_memory_tree(str(kwargs["memory_dir"])) == initial
        hooks.push_memory(vm, str(kwargs["memory_dir"]))
        assert vm.memory == initial
        return {
            "terminal_outcome": verdict,
            "verifier_report": f"independent practice evidence\nVERDICT: {verdict}",
            "actor_handoff": "STATUS: SUBMIT\nCandidate complete.",
            "actor_history": [{"role": "user", "content": "project"},
                              {"role": "assistant", "content": "candidate"}],
            "before_memory": dict(initial),
        }

    def promote(**kwargs):
        assert kwargs["terminal_outcome"] == verdict
        assert kwargs["before_memory"] == initial
        hooks.install_memory(str(kwargs["memory_dir"]), learned)
        return dict(learned), "Actor retained a practice lesson", kwargs["actor_history"]

    monkeypatch.setattr(U, "_run_practice_attempt", practice)
    monkeypatch.setattr(U, "_promote_learning", promote)
    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "unchanged target",
        f"independent target evidence\nVERDICT: {verdict}",
        "Actor retained a target lesson", initial,
        load(None), load(None), load(None), load(None),
        trigger_authority="phase2_outcome_protocol", triggering_outcome=verdict,
        curriculum_memory_access=access,
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)

    assert result.status == "ready_for_retry" and result.projects == 1
    assert observations == ([initial, learned] if access == "read_only" else [{}, {}])
    assert U._read_memory_tree(result.memory_dir) == learned
    assert initial == {"skill.md": b"original Phase1 knowledge"}
    assert "Actor retained a target lesson" in prompts[0]
    assert "independent practice evidence" in prompts[1]
    assert "Actor retained a practice lesson" in prompts[1]
    for prompt in prompts:
        assert "After PASS, do not create practice by default" in prompt
        assert "DECISION: READY_FOR_TARGET" in prompt
        if access == "none":
            assert "Your ~/.memory directory is empty" in prompt
            assert "disposable evidence copy" not in prompt
    records = sorted((Path(result.root) / "curriculum").glob("turn_*/memory_access.json"))
    assert len(records) == 2
    for path in records:
        record = json.loads(path.read_text())
        assert record["access"] == access
        assert record["attached_files"] == (1 if access == "read_only" else 0)
        assert record["actor_learning_diagnosis_available"] is True


@pytest.mark.parametrize("access", ["read_only", "none"])
def test_cli_scope_and_manifest(tmp_path, monkeypatch, access):
    monkeypatch.setattr(runner, "_install_paths", lambda: None)
    with pytest.raises(RuntimeError, match="requires --protocol-run"):
        runner.main(["task_098", "--preflight",
                     "--phase2-curriculum-memory-access", access])
    config = tmp_path / "actor.yaml"
    config.write_text("model: test\n")
    root = tmp_path / "attempt"
    runner._write_phase2_manifest(
        root, task_id="task_098", protocol_run="memory_comparison",
        instruction="target", target_direction="target",
        initial_memory_path=tmp_path, initial_memory={"lesson.md": b"retained"},
        configs={"target_actor": Config()}, paths={"target_actor": config},
        benchmark_provenance={}, stop_policy="curriculum_review",
        curriculum_memory_access=access)
    record = json.loads((root / "manifest.json").read_text())
    assert record["initial_memory"]["files"] == 1
    assert record["initial_memory"]["writeback"] is False
    assert record["protocol"]["curriculum_memory_access"] == access
    assert record["protocol"]["curriculum_after_pass"] is True
    assert record["protocol"]["official_evaluator_calls"] == 0


def test_ablation_cannot_change_an_existing_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_install_paths", lambda: None)
    with pytest.raises(RuntimeError, match="fresh Phase-2 lineage"):
        runner.main([
            "task_098",  "--protocol-run", "comparison",
            "--initial-memory", str(tmp_path), "--preflight",
            "--phase2-curriculum-memory-access", "none"], recovery_plan=object())


@pytest.mark.parametrize("mode", ["bad", "", None])
def test_unknown_access_rejected(mode):
    with pytest.raises(ValueError, match="unknown Curriculum memory access"):
        runner.phase2_protocol_metadata("curriculum_review", mode)


def test_default_visibility_and_other_charters():
    assert runner.phase2_protocol_metadata("curriculum_review")["curriculum_memory_access"] == "read_only"
    for kwargs in [{}, {"phase1_exploration": True}, {"phase2_outcome": True}]:
        assert self_evolving_curriculum_charter("target", **kwargs) == self_evolving_curriculum_charter(
            "target", curriculum_memory_access="read_only", **kwargs)
    with pytest.raises(ValueError, match="requires Phase 2"):
        self_evolving_curriculum_charter("target", phase1_exploration=True,
                                        curriculum_memory_access="none")
