"""Contracts for the three-phase recursive self-improvement framework."""

from dataclasses import fields
import json
from pathlib import Path

import pytest
import yaml

from config.settings import Config, load
from core.self_evolving_loop import SelfEvolvingLoopHooks
from explore.charter import phase1_wave_curriculum_charter
from explore.phase1_wave import parse_wave_handoff
import run_recursive_improvement as protocol
import run_self_evolving as phase2_runner


REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config/recursive_self_improvement.example.json"
PHASE2_CONFIG = REPO / "config/osworld_v2_glm53_k3_recursive_practice.yaml"
PHASE3_CONFIG = REPO / "config/osworld_v2_glm53_k3_recursive_eval.yaml"
PHASE3_LOCK = REPO / "config/osworld_v2_glm53_k3_recursive_eval.lock.json"


def _spec(tmp_path: Path, *, development=None, held_out=None,
          distribution=None) -> Path:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run_name"] = "unit_protocol"
    value["phase2"]["development_tasks"] = development or []
    value["phase3"]["held_out_tasks"] = held_out or []
    if distribution is not None:
        value["phase1"]["distribution_file"] = str(distribution)
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_example_declares_three_disjoint_modes_and_budget_checkpoints():
    spec = protocol.load_protocol(EXAMPLE)

    assert spec["phase1"]["project_budget"] == 8
    assert spec["phase1"]["checkpoints"] == [0, 2, 4, 8]
    assert spec["_resolved"]["development_tasks"] == ()
    assert spec["_resolved"]["held_out_tasks"] == ()
    assert protocol.preflight(spec, "phase1")[
        "official_grader_available_in_phase"] is False
    assert protocol.preflight(spec, "phase2")[
        "official_grader_available_in_phase"] is False
    assert protocol.preflight(spec, "phase3")[
        "official_grader_available_in_phase"] is True
    assert protocol.preflight(spec, "phase3")[
        "memory_mutable_in_phase"] is False
    assert protocol.preflight(spec, "phase3")[
        "curriculum_enabled_in_phase"] is False
    assert protocol.preflight(spec, "phase2")["status"] == \
        "configuration_required"
    assert "phase2.development_tasks is empty" in \
        protocol.preflight(spec, "phase2")["blockers"]
    lock = protocol._protocol_lock(spec, EXAMPLE.read_bytes())
    assert lock["boundaries"] == {
        "official_evaluator_calls_phase1": 0,
        "official_evaluator_calls_phase2": 0,
        "official_evaluator_phase3_only_after_agent_handoff": True,
        "phase3_memory_writeback": False,
        "learning_after_phase3_begins": False,
    }
    assert "verifier_control_config" in lock["configs"]["phase1"]


def test_parallel_phase1_is_agent_authored_not_a_static_search_menu(tmp_path):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run_name"] = "parallel_wave_unit"
    value["phase1"]["parallel_waves"] = True
    value["phase1"]["parallelism"] = 4
    path = tmp_path / "parallel.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    loaded = protocol.load_protocol(path)
    report = protocol.preflight(loaded, "phase1")
    lock = protocol._protocol_lock(loaded, path.read_bytes())

    assert report["phase1_parallel_waves"] is True
    assert report["phase1_parallelism"] == 4
    assert lock["phase1_parallel_waves"] is True
    assert lock["phase1_parallelism"] == 4

    prompt = phase1_wave_curriculum_charter(
        "a target direction", previous_wave_outcomes="",
        handoff_path="/home/user/phase1_wave.json",
        wave_root="/home/user/evolution_wave",
        target_query_conditioned=True)
    assert "any positive number" in prompt
    assert "same pre-wave memory snapshot" in prompt
    assert "No fixed project count is a semantic convergence rule" in prompt
    assert "DEEPEN" not in prompt
    assert "BROADEN" not in prompt
    assert "MIXED" not in prompt


def test_parallel_wave_transport_accepts_free_topology_and_rejects_dependencies():
    raw = json.dumps({
        "decision": "WAVE",
        "rationale": "test two independent hypotheses",
        "projects": [
            {"id": "writer-format", "instruction":
             "Work in /home/user/evolution_project and save the deliverable there."},
            {"id": "mail-attach", "instruction":
             "Work in /home/user/evolution_project and keep all outputs there."},
        ],
    })
    decision = parse_wave_handoff(raw)
    assert decision is not None
    assert [project.project_id for project in decision.projects] == [
        "writer-format", "mail-attach"]
    assert parse_wave_handoff(json.dumps({
        "decision": "WAVE", "rationale": "invalid",
        "projects": [{"id": "bad", "instruction": "use sibling output"}],
    })) is None
    saturated = parse_wave_handoff(json.dumps({
        "decision": "SATURATED", "rationale": "marginal value is low",
        "projects": [],
    }))
    assert saturated is not None and saturated.projects == ()


def test_development_and_held_out_tasks_must_be_disjoint(tmp_path):
    path = _spec(
        tmp_path, development=["task_003"], held_out=["task_003"])
    with pytest.raises(protocol.ProtocolError, match="overlap"):
        protocol.load_protocol(path)


def test_target_conditioned_mode_requires_and_allows_same_target_set(tmp_path):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run_name"] = "target_conditioned_unit"
    value["study_design"] = "target_conditioned_adaptation"
    value["phase2"]["development_tasks"] = ["task_095"]
    value["phase3"]["held_out_tasks"] = ["task_095"]
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    loaded = protocol.load_protocol(path)

    assert loaded["_resolved"]["study_design"] == \
        "target_conditioned_adaptation"
    assert protocol.preflight(loaded, "phase1")["task_split_disjoint"] is False

    value["phase3"]["held_out_tasks"] = ["task_099"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(protocol.ProtocolError, match="same target set"):
        protocol.load_protocol(path)


def test_phase1_distribution_may_not_name_a_held_out_instance(tmp_path):
    distribution = tmp_path / "distribution.md"
    distribution.write_text(
        "Explore office workflows, especially task_019.", encoding="utf-8")
    path = _spec(
        tmp_path, development=["task_003"], held_out=["task_019"],
        distribution=distribution)
    with pytest.raises(protocol.ProtocolError, match="names held-out"):
        protocol.load_protocol(path)


def test_phase_profiles_keep_roles_agentic_and_phase_boundaries_distinct():
    known = {field.name for field in fields(Config)}
    for path in (PHASE2_CONFIG, PHASE3_CONFIG):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw) <= known

    practice = load(str(PHASE2_CONFIG))
    evaluation = load(str(PHASE3_CONFIG))
    for cfg in (practice, evaluation):
        assert cfg.model == "z-ai/glm-5.3"
        assert cfg.verifier_model == "moonshotai/kimi-k3"
        assert cfg.look_ensemble == 1
        assert cfg.verifier_continuity is True
        assert cfg.verifier_stage_lifecycle is False
        assert cfg.verifier_unverified_evidence is True
        assert cfg.verifier_execution_mode == "rollback_mirror"
        assert cfg.actor_evaluator_isolation is True
        assert cfg.env_memory_dir == ""
    assert practice.verifier_failure_starts_evolution is True
    assert practice.verifier_evolve_route is True
    assert evaluation.verifier_failure_starts_evolution is False
    assert evaluation.verifier_evolve_route is False

    lock = json.loads(PHASE3_LOCK.read_text(encoding="utf-8"))
    assert lock["actor_agent"]["sha256"] == protocol._sha256(PHASE3_CONFIG)
    assert lock["evaluator"]["visibility"] == \
        "sealed_after_actor_verifier_loop"
    assert lock["memory"]["host_writeback"] is False


def test_phase2_runner_accepts_clean_profile_and_has_no_evaluator_hook():
    class Args:
        target_config = str(PHASE2_CONFIG)
        practice_actor_config = str(
            phase2_runner.DEFAULT_PRACTICE_ACTOR_CONFIG)
        practice_verifier_config = str(
            phase2_runner.DEFAULT_PRACTICE_VERIFIER_CONFIG)
        curriculum_config = str(phase2_runner.DEFAULT_CURRICULUM_CONFIG)
        memory_config = str(phase2_runner.DEFAULT_MEMORY_CONFIG)

    configs, _paths = phase2_runner._load_configs(
        Args(), phase2_training=True)
    assert configs["target_actor"].verifier_stage_lifecycle is False
    assert "evaluator" not in SelfEvolvingLoopHooks.__dataclass_fields__
    assert "score" not in SelfEvolvingLoopHooks.__dataclass_fields__


def test_phase2_selects_release_specific_benchmark_provenance():
    profile = protocol.BENCHMARK_PROVENANCE_PROFILES[
        "osworld-v2-2026.08.08"]
    assert profile.name == \
        "osworld_v2_0808_glm53_k3_agentic_baseline.lock.json"
    source = (REPO / "run_recursive_improvement.py").read_text(
        encoding="utf-8")
    assert '"--benchmark-profile", str(benchmark_profile)' in source


def test_explicit_practice_verifier_controls_full_agentic_verifier(tmp_path):
    selected = tmp_path / "selected_verifier.yaml"
    selected.write_text("model: moonshotai/kimi-k3\n", encoding="utf-8")
    target = Config(
        agentic_verifier_config="config/should_not_survive.yaml")

    effective = phase2_runner._practice_agentic_control_config(
        target, selected)

    assert effective.agentic_verifier_config == str(selected.resolve())
    assert target.agentic_verifier_config == \
        "config/should_not_survive.yaml"


def test_phase2_learns_from_both_outcomes_with_explicit_pass_policy():
    source = (REPO / "run_self_evolving.py").read_text(encoding="utf-8")
    assert "learn_on_pass=args.phase2_training" in source
    assert 'args.phase2_stop_policy == "curriculum_review"' in source
    assert '"phase2_outcome_protocol" if args.phase2_training' in source
    assert "terminal_outcome=verdict.value" in source
    assert 'only a grounded PASS/FAIL may enter durable memory' in source


def test_phase3_runner_has_explicit_copy_in_only_memory_transport():
    source = (REPO / "run_task.py").read_text(encoding="utf-8")
    assert '"--memory-dir"' in source
    assert '"host_writeback": False' in source
    assert "pull_memory" not in source
    assert source.count(".evaluate(") == 1


def test_learning_entrypoints_do_not_call_official_evaluator():
    phase1 = (REPO / "run_phase1_exploration.py").read_text(encoding="utf-8")
    orchestrator = (
        REPO / "run_recursive_improvement.py").read_text(encoding="utf-8")
    assert ".evaluate(" not in phase1
    assert "task_loader" not in phase1
    assert '"official_evaluator_calls": 0' in phase1
    # The orchestrator delegates the sole scored phase to run_task; it never owns
    # or serializes an evaluator object itself.
    assert ".evaluate(" not in orchestrator
    assert "task_loader" not in orchestrator
