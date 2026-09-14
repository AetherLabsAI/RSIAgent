"""Public batch execution preserves phase order and isolates task failures."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_osworld_batch as batch
from scripts import setup_ale
from benchmarks.ale.protocol import protocol, select_tasks


def test_baseline_plan_covers_full_release_without_creating_outputs():
    plan = batch.build_plan("portable_smoke", "baseline")
    assert len(plan["jobs"]) == 108
    assert len({job["task"] for job in plan["jobs"]}) == 108
    assert all(len(job["commands"]) == 1 for job in plan["jobs"])
    assert all("--seed" in job["commands"][0] for job in plan["jobs"])


def test_rsi_plan_preserves_latest_protocol_and_public_direction():
    plan = batch.build_plan("portable_rsi", "rsi", tasks=["task_080"])
    job = plan["jobs"][0]
    reference = json.loads(batch.TEMPLATE.read_text())
    assert (
        job["protocol"]["phase1"]["project_budget"]
        == reference["phase1"]["project_budget"]
    )
    assert job["protocol"]["phase2"] == reference["phase2"]
    assert (
        job["direction"]
        == (batch.ROOT / "config/target_queries/task_080.md").read_text()
    )
    assert [cmd[cmd.index("--phase") + 1] for cmd in job["commands"]] == [
        "phase1",
        "phase2",
        "phase3",
    ]


def test_batch_stops_failed_lineage_and_continues_other_tasks(tmp_path, monkeypatch):
    jobs = [
        dict(task=name, commands=[[name, str(i)] for i in range(3)], result_paths=[])
        for name in ("fails", "passes")
    ]
    calls = []

    def child(argv, **kwargs):
        if "--verify-only" in argv:
            return SimpleNamespace(returncode=0)
        calls.append(argv)
        kwargs["stdout"].write("scripted child\n")
        return SimpleNamespace(returncode=int(argv == ["fails", "1"]))

    monkeypatch.setattr(batch.subprocess, "run", child)
    plan = dict(output=str(tmp_path / "batch"), jobs=jobs)
    assert batch.execute(plan, 1, tmp_path / "OSWorld-V2") == 1
    assert calls == [
        ["fails", "0"],
        ["fails", "1"],
        ["passes", "0"],
        ["passes", "1"],
        ["passes", "2"],
    ]
    status = json.loads((tmp_path / "batch/status.json").read_text())
    assert [s["status"] for s in status] == ["failed", "completed"]
    with pytest.raises(FileExistsError):
        batch.execute(plan, 1, tmp_path / "OSWorld-V2")


def test_ale_batch_selection_rejects_unknown_and_duplicate_tasks(tmp_path):
    lock = protocol()
    path = tmp_path / "tasks.txt"
    for text in ("unknown/task\n", "\n", lock["tasks"][0] + "\n" + lock["tasks"][0]):
        path.write_text(text)
        with pytest.raises((ValueError, RuntimeError)):
            select_tasks(lock, path)
    path.write_text("# subset\n" + lock["tasks"][0] + "\n")
    assert select_tasks(lock, path) == [lock["tasks"][0]]


def test_setup_refuses_shared_worker_and_grader_environment(tmp_path):
    ale = tmp_path / "ale"
    with pytest.raises(ValueError, match="dedicated worker"):
        setup_ale.install(tmp_path, ale, ale / ".venv", "uv")


def test_setup_uses_separate_environments_and_pinned_dependencies(
    tmp_path, monkeypatch
):
    ale = tmp_path / "ale"
    ale.mkdir()
    (ale / "uv.lock").write_bytes(
        (batch.ROOT / "benchmarks/ale/ale.uv.lock").read_bytes()
    )
    commands = []
    monkeypatch.setattr(
        setup_ale.subprocess, "check_output", lambda *a, **kw: protocol()["ale_commit"]
    )
    monkeypatch.setattr(
        setup_ale.subprocess, "run", lambda argv, **kw: commands.append(argv)
    )
    worker = tmp_path / "worker"
    setup_ale.install(batch.ROOT, ale, worker, "uv")
    assert [
        "uv",
        "sync",
        "--frozen",
        "--project",
        str(ale),
        "--python",
        "3.12",
    ] in commands
    installs = [c for c in commands if c[:3] == ["uv", "pip", "install"]]
    assert len(installs) == 3
    assert installs[-1][4] == str(worker / "bin/python")
    assert installs[-1][-1] == str(batch.ROOT / "requirements.txt")
    assert all(str(batch.ROOT / ".venv/bin/python") not in c for c in commands)


def test_public_instructions_resolve_class_strings_without_executing_task_code(
    tmp_path,
):
    from benchmarks.osworld.task_surface import load_public_task_surface

    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    (
        folder / "task_001.py"
    ).write_text("""raise AssertionError("must never import this task")
class Task001:
    base_instruction = "  Make a table.  "
    instruction = base_instruction.strip()
""")
    assert load_public_task_surface(tmp_path, "task_001").instruction == "Make a table."


def test_public_direction_omits_generated_credentials_and_uses_pinned_website(tmp_path):
    from benchmarks.osworld.task_surface import load_public_task_surface
    from explore.commit import normalize_instruction_for_corpus

    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    (
        folder / "task_001.py"
    ).write_text("""URL = f"{build_website_url('editor')}/project"
class Task001:
    instruction = f"Edit {URL}.\\n\\nOverleaf login credentials - Email: {_OVERLEAF_USER_EMAIL}   Password: {_OVERLEAF_USER_PASSWORD}\\n\\nSave the result."
""")
    text = load_public_task_surface(
        tmp_path, "task_001", website_host_suffix="site.example"
    ).instruction
    direction = normalize_instruction_for_corpus(text)
    assert direction == "Edit https://editor.site.example/project.\n\nSave the result."
    assert "<run-generated>" not in direction


def test_public_instruction_rejects_arbitrary_calls(tmp_path):
    from benchmarks.osworld.task_surface import load_public_task_surface

    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    (folder / "task_001.py").write_text(
        'class Task001:\n    instruction = open("private.txt").read()\n'
    )
    with pytest.raises(RuntimeError, match="static string expression"):
        load_public_task_surface(tmp_path, "task_001")


def test_readiness_rejects_missing_windows_image_before_any_launch(tmp_path):
    from benchmarks.ale.host import IMAGE_REVISION
    from benchmarks.ale.prepare import validate_readiness

    worker = tmp_path / "python"
    worker.touch()
    disk = tmp_path / "linux.qcow2"
    disk.touch()
    ready = tmp_path / "rsiagent-readiness"
    ready.mkdir()
    (ready / "images.json").write_text(
        json.dumps({"revision": IMAGE_REVISION, "disks": {"linux": str(disk)}})
    )
    args = SimpleNamespace(worker_python=worker, cache=tmp_path)
    manifest = {
        "tasks": [
            {"os": "linux", "status": "ready"},
            {"os": "windows", "status": "ready"},
        ]
    }
    with pytest.raises(RuntimeError, match="image readiness"):
        validate_readiness(args, manifest)


def test_ale_cli_dispatches_to_real_smoke_entrypoint(monkeypatch, tmp_path):
    import run_ale
    from benchmarks.ale import smoke

    seen = []

    async def provision(args):
        seen.append((args.os, args.output))

    monkeypatch.setattr(smoke, "provision_smoke", provision)
    run_ale.main(["smoke", "--os", "linux", "--output", str(tmp_path)])
    assert seen == [("linux", tmp_path)]
