"""Frozen 106-task self-evolving benchmark configuration and boundaries."""

from dataclasses import fields
import hashlib
import json
from pathlib import Path
import tempfile

import yaml

import run_self_evolving as runner
from config.settings import Config, load
from core import loop as actor_loop
from core.trace import ArtifactSink
from env.vm import Trace
from tools import run_osworld_v2_self_evolving_shard as shard
from tools import summarize_osworld_v2_self_evolving as summarize


REPO = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO / "config/osworld_v2_glm53_k3_self_evolving.lock.json"
TARGET_PATH = REPO / "config/osworld_v2_glm53_k3_self_evolving.yaml"
PRACTICE_ACTOR_PATH = REPO / "config/glm53_practice_actor.yaml"
VERIFIER_PATH = REPO / "config/osworld_v2_k3_agentic_verifier.yaml"
CURRICULUM_PATH = REPO / "config/k3_curriculum.yaml"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_root(tmp_path: Path) -> Path:
    root = tmp_path / "OSWorld-V2/evaluation_examples/task_class"
    root.mkdir(parents=True)
    for task in shard.EXPECTED_SOURCE_TASKS:
        (root / f"{task}.py").touch()
    return root


def test_lock_binds_roles_protocol_profile_and_exact_valid_set():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["source_task_count"] == 108
    assert lock["valid_task_count"] == len(lock["valid_task_ids"]) == 106
    assert set(lock["excluded_tasks"]) == {"task_048", "task_082"}
    assert "task_048" not in lock["valid_task_ids"]
    assert "task_082" not in lock["valid_task_ids"]
    assert lock["actor_agent"]["sha256"] == _sha256(TARGET_PATH)
    assert lock["verifier_agent"]["sha256"] == _sha256(VERIFIER_PATH)
    assert lock["curriculum_agent"]["sha256"] == _sha256(CURRICULUM_PATH)
    assert lock["practice"]["actor_config_sha256"] == _sha256(
        PRACTICE_ACTOR_PATH)
    assert lock["protocol"] == {
        "initial_memory": "blank_per_task",
        "pass_transition": "sealed_evaluation",
        "fail_transition": "same_actor_learning_then_curriculum_practice",
        "curriculum_ready_transition": "fresh_target_actor_and_environment",
        "curriculum_stalled_transition":
            "one_final_fresh_target_test_then_sealed_evaluation",
        "official_evaluator_calls": 1,
        "official_evaluator_feedback_enters_loop": False,
        "cross_task_memory": False,
        "target_verifier_orientation": "candidate_blind_pre_actor",
        "orientation_context": "same_context_as_candidate_verification",
        "escalation_verifier_orientation":
            "proactive_on_same_task_start_state",
        "practice_verifier_orientation": False,
    }
    assert lock["evaluator"]["visibility"] == \
        "sealed_after_actor_verifier_loop"


def test_role_configs_are_known_and_keep_verifier_authority_local():
    known = {field.name for field in fields(Config)}
    for path in (
            TARGET_PATH, PRACTICE_ACTOR_PATH, VERIFIER_PATH, CURRICULUM_PATH):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw) <= known, f"unknown config keys: {set(raw) - known}"

    target = load(str(TARGET_PATH))
    practice_actor = load(str(PRACTICE_ACTOR_PATH))
    verifier = load(str(VERIFIER_PATH))
    curriculum = load(str(CURRICULUM_PATH))
    assert Config().verifier_failure_starts_evolution is False
    assert target.verifier_failure_starts_evolution is True
    assert target.verifier_local_verdict_only is True
    assert target.verifier_evolve_route is True
    assert target.verifier_hide_actor_memory is True
    assert target.verifier_stage_lifecycle is True
    assert target.verifier_persist_scratch is True
    assert (target.model, practice_actor.model) == (
        "z-ai/glm-5.3", "z-ai/glm-5.3")
    assert (verifier.model, curriculum.model) == (
        "moonshotai/kimi-k3", "moonshotai/kimi-k3")
    assert (target.vision_model, target.vision_rounds, target.look_ensemble) == (
        "moonshotai/kimi-k3", 1, 1)
    assert target.env_memory_dir == ""


def test_owner_ranges_are_disjoint_and_cover_the_106_valid_tasks(tmp_path):
    lock, _paths = shard.validate_lock(REPO, LOCK_PATH, _task_root(tmp_path))
    assignments = [shard.assignment_for(
        lock, owner=None, shard_index=index) for index in range(3)]
    groups = [set(shard.assignment_tasks(lock, assignment))
              for assignment in assignments]
    assert [assignment["owner"] for assignment in assignments] == [
        "xinyue", "shicheng", "sibo"]
    assert [len(group) for group in groups] == [36, 36, 34]
    assert not (groups[0] & groups[1] or groups[0] & groups[2]
                or groups[1] & groups[2])
    assert set.union(*groups) == set(lock["valid_task_ids"])
    assert "task_048" not in groups[1]
    assert "task_082" not in groups[2]
    assert shard.assignment_for(
        lock, owner="xinyu", shard_index=None) == assignments[0]


def test_scheduler_never_loads_tasks_or_calls_the_evaluator():
    source = (
        REPO / "tools/run_osworld_v2_self_evolving_shard.py"
    ).read_text(encoding="utf-8")
    assert "from task_loader" not in source
    assert "desktop_env" not in source
    assert ".evaluate(" not in source
    assert "run_self_evolving.py" in source


def test_curriculum_direction_excludes_run_specific_credentials():
    class _Task:
        base_instruction = "stable user request"

    assert runner._stable_target_direction(
        _Task(), "stable user request password is dynamic") == \
        "stable user request"

    class _OverleafTask:
        pass

    instruction = (
        "Compile the supplied report.\n\n"
        "Overleaf login credentials - Email: generated@example.com   "
        "Password: generated-secret\n\nLeave it open.")
    direction = runner._stable_target_direction(_OverleafTask(), instruction)
    assert direction == "Compile the supplied report.\n\nLeave it open."
    assert "generated" not in direction


def test_external_evaluator_call_is_strictly_after_agent_state_machine():
    runner = (REPO / "run_self_evolving.py").read_text(encoding="utf-8")
    state_machine_call = runner.index("loop_result = run_self_evolving_loop(")
    sealed_boundary = runner.index("# SEALED EXTERNAL MEASUREMENT", state_machine_call)
    evaluator_call = runner.index("final_environment.desktop.evaluate", sealed_boundary)
    assert state_machine_call < sealed_boundary < evaluator_call
    assert runner.count(".evaluate") == 1
    hooks_start = runner.index("loop_hooks = SelfEvolvingLoopHooks(")
    assert hooks_start < state_machine_call
    hooks = runner[hooks_start:sealed_boundary]
    assert "evaluate" not in hooks
    assert "target_direction, {},\n            loop_hooks" in runner
    assert '"phase2_outcome_protocol" if args.phase2_training' in runner
    assert 'else "verifier_fail_protocol"' in runner
    assert "triggering_outcome=verdict.value" in runner
    assert "session=curriculum_session" in runner
    assert 'stage="orientation"' in runner
    assert "ensure_verifier_orientation" in runner
    assert "if target_cfg.verifier_stage_lifecycle" in runner
    assert runner.index("def verifier_orient(") < runner.index("def actor_work(")
    assert "if args.phase2_training:" in hooks
    assert "target_direction, initial_memory, loop_hooks" in hooks
    assert 'args.phase2_stop_policy == "curriculum_review"' in hooks
    assert "verifier_orient if target_cfg.verifier_stage_lifecycle else None" \
        in hooks


def test_concrete_local_fail_enters_protocol_without_a_curriculum_route(monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.verifier_failure_starts_evolution = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        ('{"done":{"checks":[{"desc":"candidate exists",'
         '"probe":"test -e /tmp/candidate && echo PASS"}]}}'),
    ))
    monkeypatch.setattr(actor_loop, "chat", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(
        actor_loop, "verify_independent",
        lambda *args, **kwargs: (
            "wrong", "concrete local evidence\nVERDICT: FAIL\n"))

    class _VM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="self-evolving-fail-")))

    assert result.status == "evolve"
    assert result.verifier_route == "EVOLVE"
    assert result.verifier_report.endswith("VERDICT: FAIL\n")
    assert result.curriculum_route == ""
    assert result.curriculum_report == ""


def test_hidden_memory_is_absent_from_verifier_file_change_metadata(monkeypatch):
    cfg = load(None)
    cfg.max_iters = 6
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.verifier_hide_actor_memory = True
    cfg.strict_one_action = False
    replies = iter((
        '{"program":{"lang":"bash","code":"echo work"}}',
        ('{"done":{"checks":[{"desc":"candidate exists",'
         '"probe":"test -e /home/user/Desktop/candidate && echo PASS"}]}}'),
    ))
    monkeypatch.setattr(actor_loop, "chat", lambda *args, **kwargs: next(replies))
    seen = {}

    def verify(*args, **kwargs):
        seen["context"] = kwargs["context"]
        return "pass", "complete evidence\nVERDICT: PASS\n"

    monkeypatch.setattr(actor_loop, "verify_independent", verify)

    class _VM:
        snapshots = 0

        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            if command == actor_loop._FIND:
                self.snapshots += 1
                if self.snapshots == 1:
                    return "1 /home/user/Desktop/original"
                return (
                    "1 /home/user/Desktop/original\n"
                    "2 /home/user/Desktop/candidate\n"
                    "2 /home/user/.memory/secret_skill.md")
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    result, _ = actor_loop.run_attempt(
        "authoritative task", _VM(), cfg,
        ArtifactSink(tempfile.mkdtemp(prefix="self-evolving-private-memory-")))

    assert result.status == "done"
    assert "/home/user/Desktop/candidate" in seen["context"]
    assert ".memory" not in seen["context"]


def test_result_identity_and_aggregator_cover_exactly_106(tmp_path):
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    seed, tag = lock["seed_label"], lock["tag"]
    for index, task in enumerate(lock["valid_task_ids"]):
        path = (
            tmp_path / "self_evolving" / task / f"seed{seed}_{tag}"
            / "result.json")
        path.parent.mkdir(parents=True)
        evolved = int(index % 2 == 0)
        result = {
            "task": task,
            "seed": seed,
            "tag": tag,
            "model": "z-ai/glm-5.3",
            "score": 0.75,
            "status": "verifier_pass",
            "target_verifier_verdict": "PASS",
            "target_cycles": 1 + evolved,
            "evolutions": evolved,
            "practice_projects": 2 * evolved,
            "target_verifier_orientations": 2 * (1 + evolved),
            "target_verifier_orientation":
                "candidate_blind_pre_actor_same_context",
            "target_verifier_orientation_report_sha256": "a" * 64,
            "final_memory_files": evolved,
            "final_memory_bytes": 100 * evolved,
            "official_evaluator_feedback_entered_loop": False,
            "sealed_evaluator": {"model": "openai/gpt-5.4"},
            "llm_providers": {
                "z-ai/glm-5.3": {"Z.AI": 2},
                "moonshotai/kimi-k3": {"Moonshot AI": 1},
            },
        }
        path.write_text(json.dumps(result), encoding="utf-8")
        shard.validate_result_identity(result, task, lock)

    summary = summarize.collect([tmp_path], lock)
    assert summary["completed_tasks"] == summary["valid_scored_tasks"] == 106
    assert summary["missing_tasks"] == summary["invalid_results"] == []
    assert summary["mean_score"] == summary["median_score"] == 0.75
    assert summary["evolved_task_count"] == 53
    assert summary["first_pass_task_count"] == 53
    assert summary["totals"]["evolutions"] == 53
    assert summary["totals"]["practice_projects"] == 106
    assert summary["totals"]["target_verifier_orientations"] == 318
    assert summary["provider_calls"]["z-ai/glm-5.3"]["Z.AI"] == 212
