"""Recorded ALE public-text collisions, exercised through bound learning hooks."""

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.ale.practice import Audit
from explore.commit import _shingles


TASK = "health_medicine/example_analysis"
ROOT = "/media/user/data/agenthle/" + TASK + "/base"
FOREIGN = "Segment every individual cell in the provided microscope image series."


def make_audit(tmp_path, prompt, instruction, *, collision="", roots=None, task=TASK):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(sorted(
        _shingles(prompt) | _shingles(instruction) | _shingles(collision) | _shingles(FOREIGN)
    )))
    public_task = {
        "task_id": task, "task_prompt": prompt, "instruction": instruction,
        "visible_roots": roots if roots is not None else [ROOT + "/input", ROOT + "/output"],
    }
    return Audit(corpus, public_task=public_task), public_task


def test_own_public_task_card_is_authorized_only_for_its_bound_instruction(tmp_path):
    # Sibo TCGA: staged public wording was absent from TaskLoader's shorter text.
    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    instruction = "Analyze the supplied survival cohort."
    audit, _ = make_audit(tmp_path, prompt, instruction)
    assert Audit(audit.path).text(prompt, authorized_instruction=instruction)
    assert audit.text(prompt, authorized_instruction=instruction) == []
    assert audit.text(prompt) == [{"kind": "other-task-instruction"}]
    with pytest.raises(ValueError, match="bound instruction"):
        audit.text(prompt, authorized_instruction="A different task.")
    assert audit.text(FOREIGN, authorized_instruction=instruction)


@pytest.mark.parametrize("windows", [False, True])
def test_declared_relative_and_localized_output_paths_are_equivalent(tmp_path, windows):
    task = "psychology_neuro/example_coding"
    root = ("E:/agenthle/" if windows else "/media/user/data/agenthle/") + task + "/base"
    prompt = "Save the completed workbook exactly to base/output/annotations.xlsx."
    instruction = prompt.replace("base/", root + "/")
    if windows:
        prompt, instruction = prompt.replace("/", "\\"), instruction.replace("/", "\\")
    alias = "Save the completed workbook exactly to output/annotations.xlsx."
    audit, _ = make_audit(tmp_path, prompt, instruction, task=task, collision=alias,
                         roots=[root + "/input", root + "/output"])
    assert Audit(audit.path).text(alias, authorized_instruction=instruction)
    assert audit.text(prompt, authorized_instruction=instruction) == []
    assert audit.text(instruction, authorized_instruction=instruction) == []
    assert audit.text(alias, authorized_instruction=instruction) == []


def test_humanoid_relative_output_phrase_matches_its_public_path(tmp_path):
    prompt = "Provide a visible motion demo artifact per case under /output/visual_demos/."
    instruction = prompt.replace("/output/", ROOT + "/output/")
    alias = "Provide a visible motion demo artifact per case under `output/visual_demos/`."
    audit, _ = make_audit(tmp_path, prompt, instruction, collision=alias)
    assert Audit(audit.path).text(alias, authorized_instruction=instruction)
    assert audit.text(alias, authorized_instruction=instruction) == []


def test_own_dependency_filename_pair_is_occurrence_scoped(tmp_path):
    prompt = "Use base/input/runtime_env/pyproject.toml. Install from base/input/runtime_env/uv.lock."
    instruction = prompt.replace("base/", ROOT + "/")
    pair = "runtime_env/pyproject.toml, runtime_env/uv.lock"
    bare = "runtime env pyproject toml runtime env uv lock"
    audit, _ = make_audit(tmp_path, prompt, instruction, collision=pair)
    assert Audit(audit.path).text(pair, authorized_instruction=instruction)
    assert audit.text(pair, authorized_instruction=instruction) == []
    assert audit.text(pair + "\n" + bare, authorized_instruction=instruction)
    assert audit.text("foreign/" + pair, authorized_instruction=instruction)
    assert audit.text(pair + ".backup", authorized_instruction=instruction)


def test_adjacent_own_file_listing_does_not_authorize_other_task_paths(tmp_path):
    instruction = "Analyze the files supplied under " + ROOT + "/input."
    listing = ROOT + "/input/runtime_env/a.py\n" + ROOT + "/input/runtime_env/b.py"
    foreign = listing.replace(TASK, "life_sciences/foreign_analysis")
    audit, _ = make_audit(tmp_path, instruction, instruction, collision=listing + "\n" + foreign)
    assert Audit(audit.path).text(listing, authorized_instruction=instruction)
    assert audit.text(listing, authorized_instruction=instruction) == []
    assert audit.text(foreign, authorized_instruction=instruction)
    # CP searched outside its task; a broad visible ancestor must not hide that.
    wide, _ = make_audit(tmp_path, instruction, instruction,
                         collision=foreign, roots=["/home/user", "/media/user/data"])
    assert wide.text(foreign, authorized_instruction=instruction)


def test_generic_environment_setup_requires_real_local_shell_syntax(tmp_path):
    # Sibo MERFISH: ordinary setup collided with a different public task.
    setup = "python3 -m venv .venv && uv pip install --python .venv/bin/python starfish cellpose"
    gram = "venv uv pip install python venv bin python"
    instruction = "Create a synthetic microscopy exercise."
    audit, _ = make_audit(tmp_path, instruction, instruction, collision=gram)
    assert Audit(audit.path).text(setup, authorized_instruction=instruction)
    assert audit.text(setup, authorized_instruction=instruction) == []
    assert audit.text(gram, authorized_instruction=instruction)
    assert audit.text(setup + "\n" + gram, authorized_instruction=instruction)
    assert audit.text(setup + "\n" + FOREIGN, authorized_instruction=instruction)
    assert audit.text("İ\n" + setup, authorized_instruction=instruction) == []
    assert audit.text(setup.replace("venv .venv", "venv other/.venv"),
                      authorized_instruction=instruction)
    # Only this complete setup grammar qualifies, not a command suffix quoted
    # without creating the same environment first.
    assert audit.text(".venv && uv pip install --python .venv/bin/python",
                      authorized_instruction=instruction)
    assert audit.text(setup)  # No bound task authorization in blind exploration.


@pytest.mark.parametrize("text", [
    "https://github.com/rdi-berkeley/agents-last-exam/tree/main/tasks",
    "secret/eval_time", "ALE_REFERENCE_ARCHIVE_PASSWORD",
])
def test_private_sources_remain_rejected_even_if_a_public_card_mentions_them(tmp_path, text):
    audit, _ = make_audit(tmp_path, text, "Do the task.")
    assert {hit["kind"] for hit in audit.text(text, authorized_instruction="Do the task.")} == {
        "benchmark-private-source"
    }


def test_all_audit_entrypoints_share_the_public_binding(tmp_path):
    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    instruction = "Analyze the cohort."
    audit, _ = make_audit(tmp_path, prompt, instruction)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "transcript.json").write_text(json.dumps({
        "messages": [{"role": "user", "content": prompt}]
    }))
    with tarfile.open(artifacts / "materials.tgz", "w:gz") as archive:
        data = prompt.encode()
        member = tarfile.TarInfo("notes.md")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    assert audit.transcripts(artifacts, authorized_instruction=instruction) == []
    assert audit.captured(artifacts, authorized_instruction=instruction) == []
    assert audit.memory({"notes.md": prompt.encode()}, audit.path, "unused",
                        authorized_instruction=instruction) == {"notes.md": prompt.encode()}


def test_host_binding_reaches_worker_phase1_hooks_without_entering_any_model_prompt(tmp_path, monkeypatch):
    from benchmarks.ale import host, learning, worker
    from explore import phase1_wave

    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    instruction = "Analyze the cohort."
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(sorted(_shingles(prompt) | _shingles(FOREIGN))))
    public_task = host.public_task_audit(
        TASK, {"taskId": TASK, "taskPrompt": prompt}, instruction, [ROOT + "/input"]
    )
    class Pool:
        vms = [SimpleNamespace(is_windows=False, home="/home/user")]
        def __init__(self, *args, **kwargs): pass
        def acquire(self): raise AssertionError("no VM work in wiring test")
        def close(self): pass

    monkeypatch.setattr(learning, "CleanPool", Pool)
    def evolve(**kwargs):
        assert kwargs["target_direction"] == instruction
        for name in ("actor_cfg", "curriculum_cfg", "memory_cfg", "verifier_control_cfg"):
            assert prompt not in kwargs[name].system_extra
        hooks = kwargs["hooks"]
        assert hooks.audit_text(prompt, authorized_instruction=instruction) == []
        assert hooks.audit_text(prompt)  # Caller has not authorized target content.
        assert hooks.audit_text(FOREIGN, authorized_instruction=instruction)
        memory = tmp_path / "result-memory"
        memory.mkdir()
        return SimpleNamespace(memory_dir=str(memory), status="saturated", projects=0, reason="done")
    monkeypatch.setattr(phase1_wave, "evolve_parallel_phase1", evolve)
    work = tmp_path / "phase1"
    work.mkdir()
    spec = tmp_path / "worker-spec.json"
    spec.write_text(json.dumps({
        "phase": "phase1",
        "work_dir": str(work), "practice_sandboxes": [{}], "reset_endpoint": "unused",
        "corpus": str(corpus), "instruction": instruction, "audit_public_task": public_task,
    }))
    monkeypatch.setattr(worker.sys, "argv", ["worker", str(spec)])
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    assert worker.main() == 0
    result = json.loads((work / "worker-result.json").read_text())
    assert result["transition_ready"]
    assert result["official_evaluator_calls"] == 0


@pytest.mark.parametrize("identity", ["taskId", "task_id"])
def test_host_accepts_both_existing_card_identity_fields(tmp_path, identity):
    from benchmarks.ale.host import public_task_audit

    assert public_task_audit(TASK, {identity: TASK, "taskPrompt": "Public task."},
                             "Issued task.", []) == {
        "task_id": TASK, "task_prompt": "Public task.", "instruction": "Issued task.",
        "visible_roots": [],
    }
    with pytest.raises(ValueError, match="selected ALE task"):
        public_task_audit(TASK, {"taskId": TASK, "task_id": "other/task",
                                "taskPrompt": "Public task."}, "Issued task.", [])


def test_phase2_target_learning_and_curriculum_receive_bound_audit(tmp_path, monkeypatch):
    from dataclasses import replace
    from benchmarks.ale import host, learning
    from benchmarks.ale.protocol import memory_record
    from core import loop
    from explore import target_learning, unified_evolution
    from explore.practice_loop import _atomic_install_memory

    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    instruction = "Analyze the cohort."
    audit, _ = make_audit(tmp_path, prompt, instruction)
    public_task = host.public_task_audit(
        TASK, {"task_id": TASK, "taskPrompt": prompt}, instruction, [ROOT + "/input"]
    )
    initial = tmp_path / "initial"
    _atomic_install_memory(str(initial), {"seed.md": b"initial lesson"})
    work = tmp_path / "phase2"
    work.mkdir()
    seen = []

    class Pool:
        vms = [SimpleNamespace(is_windows=False, home="/home/user")]
        def __init__(self, *args, **kwargs): pass
        def acquire(self):
            return SimpleNamespace(close=lambda: None), SimpleNamespace()
        def close(self): pass

    monkeypatch.setattr(learning, "CleanPool", Pool)
    real_hooks = learning.hooks_for
    def hooks(pool, corpus, binding):
        # Preserve the actual Audit/hooks construction; replace only VM upload.
        return replace(real_hooks(pool, corpus, binding), push_memory=lambda *a: True)
    monkeypatch.setattr(learning, "hooks_for", hooks)

    def actor(direction, vm, cfg, sink, **kwargs):
        assert direction == instruction and prompt not in cfg.system_extra
        history = [{"role": "assistant", "content": prompt}]
        return loop.LoopResult(status="done", verifier_route="HANDOFF",
                               verifier_report="VERDICT: PASS\nAll rows checked"), history
    monkeypatch.setattr(loop, "run_with_resume", actor)

    def learn(**kwargs):
        assert kwargs["target"] == instruction
        bound = kwargs["hooks"]
        assert bound.audit_transcripts(work, authorized_instruction=instruction) == []
        assert bound.audit_transcripts(work)
        assert bound.audit_text(FOREIGN, authorized_instruction=instruction)
        seen.append("target-learning")
        return kwargs["before_memory"], "Ready for review", kwargs["actor_history"]
    monkeypatch.setattr(target_learning, "_promote_learning", learn)

    def evolve(vm, root, direction, report, diagnosis, memory, *configs, **kwargs):
        assert direction == instruction and all(prompt not in cfg.system_extra for cfg in configs)
        assert kwargs["hooks"].audit_text(prompt, authorized_instruction=instruction) == []
        assert kwargs["hooks"].audit_text(prompt)
        seen.append("curriculum")
        destination = Path(root) / "memory"
        _atomic_install_memory(str(destination), memory)
        return SimpleNamespace(status="ready_for_retry", memory_dir=str(destination),
                               projects=0, reason="No further practice")
    monkeypatch.setattr(unified_evolution, "evolve_until_ready", evolve)
    result = learning.phase2({
        "work_dir": str(work), "input_memory": memory_record(initial), "sandbox": {},
        "practice_sandboxes": [{}], "reset_endpoint": "unused", "corpus": str(audit.path),
        "instruction": instruction, "audit_public_task": public_task,
    })
    assert seen == ["target-learning", "curriculum"]
    assert result["target_cycles"] == 1 and result["learning_updates"] == 1
    assert result["transition_ready"] and result["official_evaluator_calls"] == 0


def test_old_unbound_and_unaffected_text_keep_their_results(tmp_path):
    target = "Summarize this public data table and return a concise analysis."
    audit, _ = make_audit(tmp_path, target, target)
    legacy = Audit(audit.path)
    for text in (target, FOREIGN, "Ordinary Python output: 12 rows.", "secret/eval_time"):
        assert audit.text(text, authorized_instruction=target) == legacy.text(
            text, authorized_instruction=target
        )
