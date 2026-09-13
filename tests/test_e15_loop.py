"""Focused state-machine tests for the opt-in E15 lifecycle."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from config.settings import load as load_config
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
from explore import e15_loop
from explore import e15_v12_loop
from explore.e15_state import EventLedger
from llm.client import DURABLE_IMAGES_FIELD, durable_user_message
from tools.exam_fence import audit_text


TARGET = "Reproduce the disclosed long-horizon editing task from the supplied inputs."
PROJECT_1 = (
    "Work only in /home/user/evolution_project and create the requested editable "
    "deliverable from the supplied raw input."
)
PROJECT_2 = (
    "Work only in /home/user/evolution_project and transfer the learned method "
    "to the supplied contrasting input."
)


def test_transcript_quarantine_persists_host_only_hit_details(tmp_path):
    sink_dir = tmp_path / "curriculum/segment_000"
    sink_dir.mkdir(parents=True)
    hits = [{"file": str(sink_dir / "iter_04/trace.txt"), "hits": [
        {"kind": "repo-url", "match": "blocked repository evidence"}]}]
    calls = []

    def audit(root, **kwargs):
        calls.append((root, kwargs))
        return hits

    hooks = SimpleNamespace(audit_transcripts=audit)
    with pytest.raises(e15_loop.E15BoundaryError) as exc:
        e15_loop._audit_agent_artifacts(hooks, str(sink_dir), TARGET)

    report_path = sink_dir / "boundary_audit.json"
    report = json.loads(report_path.read_text())
    assert report["hits"] == hits and report["mode"] == "practice"
    assert report["status"] == "quarantined"
    assert calls == [(str(sink_dir), {
        "mode": "practice", "authorized_instruction": TARGET})]
    assert str(report_path) in str(exc.value)
    assert "blocked repository evidence" not in str(exc.value)
    assert report_path.stat().st_mode & 0o777 == 0o600


def test_failed_diagnostic_write_does_not_downgrade_quarantine(monkeypatch, tmp_path):
    hooks = SimpleNamespace(audit_transcripts=lambda *_args, **_kwargs: [
        {"file": "trace.txt", "hits": [{"kind": "repo-url", "match": "blocked"}]}])

    def fail_write(*_args, **_kwargs):
        raise PermissionError("read-only diagnostic directory")

    monkeypatch.setattr(e15_loop, "_atomic_json", fail_write)
    with pytest.raises(e15_loop.E15BoundaryError,
                       match="audit report could not be written"):
        e15_loop._audit_agent_artifacts(hooks, str(tmp_path), TARGET)


def test_curriculum_visuals_scope_only_pixels_at_project_boundary():
    pixels = b"exact prior project pixels"
    history = [
        durable_user_message("the complete Look result", pixels),
        {"role": "assistant", "content": "my complete visual reasoning"},
        {"role": "user", "content": "the next observation"},
        {"role": "assistant", "content": "the next action"},
    ]

    scoped, archived = e15_loop._scope_prior_curriculum_visuals(history)

    assert len(scoped) == len(history)
    assert [message["role"] for message in scoped] == \
        [message["role"] for message in history]
    assert scoped[0]["content"].startswith("the complete Look result")
    assert "losslessly stored" in scoped[0]["content"]
    assert "Reissue a Look" in scoped[0]["content"]
    assert scoped[1:] == history[1:]
    assert all(DURABLE_IMAGES_FIELD not in message for message in scoped)
    assert DURABLE_IMAGES_FIELD in history[0]
    assert archived == [{
        "message_index": 0,
        "images": 1,
        "sha256": [history[0][DURABLE_IMAGES_FIELD][0]["sha256"]],
    }]
    scoped_again, archived_again = \
        e15_loop._scope_prior_curriculum_visuals(scoped)
    assert scoped_again == scoped
    assert archived_again == []


class FakeVM:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.tree = None
        self.memory: dict[str, bytes] = {}
        self.execution_evidence: dict[str, bytes] = {}
        self.execution_evidence_expected: dict[str, bytes] = {}
        self.evidence_verify_ok = True
        self.target_shape_safe = True
        self.original_fixture_tree = None

    def run_command(self, command, **_kwargs):
        if "E15_REMOVE_TREE_RC" in command:
            assert e15_loop.PROJECT_ROOT in command
            self.tree = None
            return "E15_REMOVE_TREE_RC=0"
        if "E15_REMOVE_RC" in command:
            for path in (e15_loop.CURRICULUM_HANDOFF,
                         e15_loop.ACTOR_HANDOFF,
                         e15_loop.VERIFIER_REPORT,
                         e15_loop.LEARNING_DIAGNOSIS):
                if path in command:
                    self.files.pop(path, None)
            return "E15_REMOVE_RC=0"
        if "E15_MEMORY_FILES" in command:
            return (f"E15_MEMORY_FILES={len(self.memory)}\n"
                    "E15_MEMORY_DIR_RC=0")
        if "E15_TARGET_INPUTS_RC" in command:
            return "\n".join(
                f"{path}  {'a' * 64}"
                for path in e15_v12_loop.TARGET_INPUTS
            ) + "\nE15_TARGET_INPUTS_RC=0"
        if "E15_ORIGINAL_FIXTURE_STAGE_RC" in command:
            self.original_fixture_tree = copy.deepcopy(self.tree)
            return "E15_ORIGINAL_FIXTURE_STAGE_RC=0"
        if "E15_ORIGINAL_FIXTURE_RESTORE_RC" in command:
            self.tree = copy.deepcopy(self.original_fixture_tree)
            return "E15_ORIGINAL_FIXTURE_RESTORE_RC=0"
        if "E15_TARGET_SHAPE_RC" in command:
            return ("E15_TARGET_SHAPE_RC=0" if self.target_shape_safe
                    else "E15_TARGET_SHAPE_RC=1")
        if "E15_PROJECT_SHAPE_RC" in command:
            return ("E15_PROJECT_SHAPE_RC=0" if self.tree is not None
                    else "E15_PROJECT_SHAPE_RC=1")
        if "E15_TARGET_STAGE_RC" in command:
            return "E15_TARGET_STAGE_RC=0"
        if "E15_TARGET_OVERLAY_RC" in command:
            return "E15_TARGET_OVERLAY_RC=0"
        if "E15_ACTOR_EVIDENCE_RC" in command:
            intact = self.execution_evidence == \
                self.execution_evidence_expected
            return ("E15_ACTOR_EVIDENCE_RC=0"
                    if self.evidence_verify_ok and intact
                    else "E15_ACTOR_EVIDENCE_RC=1")
        return ""


class GuestTextVM:
    def __init__(self, state: str, payload=None):
        self.state = state
        self.payload = payload
        self.commands = []
        self.fetches = []

    def run_command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return self.state

    def fetch_file(self, path, **kwargs):
        self.fetches.append((path, kwargs))
        return self.payload


class LocalCommandVM:
    def run_command(self, command, **kwargs):
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True,
            timeout=kwargs.get("timeout"))
        return result.stdout + result.stderr


def test_v12_target_input_fingerprint_requires_exact_paths_and_hex_digests():
    class FingerprintVM:
        def __init__(self, output):
            self.output = output

        def run_command(self, *_args, **_kwargs):
            return self.output

    good_lines = [
        f"{path}  {index:064x}"
        for index, path in enumerate(e15_v12_loop.TARGET_INPUTS, 1)
    ]
    expected = "\n".join(good_lines) + "\n"
    assert e15_v12_loop._target_input_fingerprint(
        FingerprintVM(
            "\n".join(reversed(good_lines))
            + "\nE15_TARGET_INPUTS_RC=0")) == expected

    malformed = list(good_lines)
    malformed[0] = f"{e15_v12_loop.TARGET_INPUTS[0]}  "
    with pytest.raises(e15_loop.E15InfrastructureError, match="malformed"):
        e15_v12_loop._target_input_fingerprint(FingerprintVM(
            "\n".join(malformed) + "\nE15_TARGET_INPUTS_RC=0"))

    duplicate = good_lines + [good_lines[0]]
    with pytest.raises(e15_loop.E15InfrastructureError, match="duplicates"):
        e15_v12_loop._target_input_fingerprint(FingerprintVM(
            "\n".join(duplicate) + "\nE15_TARGET_INPUTS_RC=0"))


def test_v12_t094_surface_fingerprints_video_and_carries_native_file():
    class FingerprintVM:
        @staticmethod
        def run_command(*_args, **_kwargs):
            return (
                "/home/user/Videos/task094_ref.mp4  " + "a" * 64
                + "\nE15_TARGET_INPUTS_RC=0")

    try:
        e15_v12_loop.configure_target_surface("task_094")
        assert e15_v12_loop._target_input_fingerprint(FingerprintVM()) == (
            "/home/user/Videos/task094_ref.mp4  " + "a" * 64 + "\n")
        assert e15_v12_loop.target_candidate_outputs() == (
            "/home/user/Documents/SolveSpace/part.slvs",)
    finally:
        e15_v12_loop.configure_target_surface("task_056")


def test_guest_text_confirmed_absent_skips_retrying_fetch():
    vm = GuestTextVM("E15_FILE_STATE=ABSENT", b"must not be fetched")

    assert e15_loop._read_guest_text(vm, "/home/user/verifier_report.md") == ""
    assert vm.fetches == []
    assert "test -f /home/user/verifier_report.md" in vm.commands[0][0]


def test_guest_text_present_fetches_long_report_losslessly():
    report = ("# free-form report\n" + "evidence\n" * 1000
              + "VERDICT: FAIL\n").encode()
    vm = GuestTextVM("E15_FILE_STATE=PRESENT", (report, ""))

    actual = e15_loop._read_guest_text(vm, "/home/user/verifier_report.md")

    assert actual.encode() == report
    assert vm.fetches == [(
        "/home/user/verifier_report.md", {"max_bytes": None})]


def test_guest_text_indeterminate_presence_falls_back_to_fetch():
    report = b"# report\nVERDICT: PASS\n"
    vm = GuestTextVM(
        "[channel error: Timeout - the machine did not answer]", report)

    assert e15_loop._read_guest_text(
        vm, "/home/user/verifier_report.md") == report.decode()
    assert len(vm.fetches) == 1


def test_same_context_stall_continuation_repairs_plain_json_transport_only():
    path = "/home/user/arbitrary_handoff.md"
    prompt = e15_loop._continue_after_stall("GENERIC", path)

    assert PLAIN_JSON_TRANSPORT_NOTE in prompt
    assert "SAME-CONTEXT CONTINUATION" in prompt
    assert path in prompt
    assert "not treated as a submission, verdict" in " ".join(prompt.split())
    assert "VERDICT:" not in prompt
    assert "STATUS:" not in prompt
    assert "DECISION:" not in prompt


class ScriptedAgents:
    def __init__(self, vm: FakeVM, steps):
        self.vm = vm
        self.steps = list(steps)
        self.calls = []

    def __call__(self, prompt, _vm, _cfg, sink, *, initial_history=None,
                 continue_context=False, allow_noop_done=False, **_kwargs):
        assert _vm is self.vm
        assert self.steps, f"unexpected Agent call: {prompt[:120]}"
        step = self.steps.pop(0)
        history_in = copy.deepcopy(initial_history or [])
        call = {
            "name": step["name"],
            "prompt": prompt,
            "history_in": history_in,
            "continue_context": continue_context,
            "allow_noop_done": allow_noop_done,
            "tree_before": copy.deepcopy(self.vm.tree),
            "memory_before": dict(self.vm.memory),
            "execution_evidence_before": dict(
                self.vm.execution_evidence),
            "sink": sink.root,
        }
        self.calls.append(call)
        if "check" in step:
            checked = step["check"](self.vm, call)
            assert checked is not False
        if "tree" in step:
            self.vm.tree = copy.deepcopy(step["tree"])
        if "target_shape_safe" in step:
            self.vm.target_shape_safe = bool(step["target_shape_safe"])
        if "memory" in step:
            self.vm.memory = dict(step["memory"])
        if step.get("path"):
            self.vm.files[step["path"]] = step["text"].encode()
        if "program" in step:
            sink.save_program(1, "bash", step["program"])
            sink.save_trace(1, SimpleNamespace(
                stdout=step.get("trace", ""), exit_code=0,
                secs=0.1, timed_out=False))
        terminal_probe = _kwargs.get("terminal_handoff_ready")
        call["terminal_handoff_ready"] = bool(
            terminal_probe and terminal_probe())
        history = history_in + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": step["name"]},
        ]
        call["history_out"] = copy.deepcopy(history)
        result = SimpleNamespace(
            status=step.get("status", "done"), iters=1, turns=1,
            wall_secs=1.0)
        return result, history


class FakeHarness:
    def __init__(self, vm: FakeVM, driver: ScriptedAgents):
        self.vm = vm
        self.driver = driver
        self.snapshots = {}
        self.journals = []
        self.pulls = []
        self.memory_audits = []
        self.installs = []
        self.audit_calls = []
        self.events = []
        self.evidence_pushes = []
        self.evidence_push_ok = True

    def reset(self, vm, target):
        assert vm is self.vm
        assert target == TARGET
        vm.files.clear()
        vm.tree = None
        vm.memory = {}
        vm.execution_evidence = {}
        vm.execution_evidence_expected = {}
        vm.target_shape_safe = True
        return {"ok": True}

    def read_text(self, vm, path):
        data = vm.files.get(path)
        return data.decode() if data is not None else ""

    def capture(self, vm, _key, host_dir, guest_dirs):
        assert guest_dirs in ([e15_loop.PROJECT_ROOT_NAME],
                              [e15_v12_loop.TARGET_CANDIDATE_ROOT_NAME])
        if vm.tree is None:
            return {"ok": False, "files": 0, "bytes": 0}
        Path(host_dir).mkdir(parents=True, exist_ok=True)
        (Path(host_dir) / e15_v12_loop.MANIFEST).write_text(
            "fake captured candidate manifest\n")
        (Path(host_dir) / e15_v12_loop.MATERIALS).write_bytes(
            b"fake captured candidate archive")
        (Path(host_dir) / e15_v12_loop.ROOTS).write_text(
            '{"roots":["fake"]}\n', encoding="utf-8")
        self.snapshots[host_dir] = copy.deepcopy(vm.tree)
        return {"ok": True, "files": 1, "bytes": 1}

    def replay(self, vm, host_dir):
        if host_dir not in self.snapshots:
            return {"ok": False, "error": "missing snapshot"}
        vm.tree = copy.deepcopy(self.snapshots[host_dir])
        return {"ok": True, "mismatches": []}

    def verify_project(self, vm, host_dir):
        if host_dir not in self.snapshots:
            return {"ok": False, "error": "missing snapshot"}
        if vm.tree != self.snapshots[host_dir]:
            return {"ok": False, "error": "candidate tree differs"}
        return {"ok": True, "mismatches": []}

    @staticmethod
    def _tree(path):
        root = Path(path)
        if not root.exists():
            return {}
        return {
            str(item.relative_to(root)): item.read_bytes()
            for item in root.rglob("*") if item.is_file()
        }

    def push_memory(self, vm, memory_dir):
        vm.memory = self._tree(memory_dir)
        return True

    def push_dir(self, vm, host_dir, guest_path):
        assert guest_path == e15_loop.ACTOR_EXECUTION_EVIDENCE
        if not self.evidence_push_ok:
            return False
        evidence = self._tree(host_dir)
        vm.execution_evidence = evidence
        vm.execution_evidence_expected = dict(evidence)
        self.evidence_pushes.append(dict(evidence))
        return True

    def pull_memory(self, vm):
        pulled = dict(vm.memory)
        self.pulls.append(pulled)
        return pulled

    def journal(self, journal_dir, episode, files, meta):
        self.journals.append({
            "dir": journal_dir, "episode": episode,
            "files": dict(files), "meta": dict(meta)})

    def install(self, memory_dir, files):
        installed = dict(files)
        self.installs.append(installed)
        e15_loop._atomic_install_memory(memory_dir, installed)

    def audit_text(self, text, mode, authorized_instruction):
        self.audit_calls.append(("text", text, mode, authorized_instruction))
        return []

    def audit_transcripts(self, root, mode, authorized_instruction):
        self.audit_calls.append(
            ("transcript", root, mode, authorized_instruction))
        return []

    def audit_captured(self, root, authorized_instruction):
        self.audit_calls.append(
            ("capture", root, "practice", authorized_instruction))
        return []

    def hooks(self, audit_memory=None):
        def tracked_memory_audit(files, *args, **kwargs):
            candidate = dict(files)
            self.memory_audits.append(candidate)
            if audit_memory is not None:
                return audit_memory(candidate, *args, **kwargs)
            return candidate

        return e15_loop.E15Hooks(
            run_attempt=self.driver,
            reset_vm=self.reset,
            read_guest_text=self.read_text,
            capture_project=self.capture,
            replay_project=self.replay,
            verify_project=self.verify_project,
            audit_captured=self.audit_captured,
            push_memory=self.push_memory,
            pull_memory=self.pull_memory,
            audit_memory=tracked_memory_audit,
            journal_memory=self.journal,
            install_memory=self.install,
            validate_corpus=lambda _path: {"ok": True},
            audit_text=self.audit_text,
            audit_transcripts=self.audit_transcripts,
            push_dir=self.push_dir,
        )

    def append_event(self, event_type, *, status, state, payload):
        self.events.append({
            "event_type": event_type,
            "status": status,
            "state": dict(state),
            "payload": dict(payload),
        })


def cfg():
    # The fake Agent ignores model settings. These fields pin the runtime's
    # generic config overlay without importing production model clients.
    return SimpleNamespace(
        practice_mode=False, independent_verify=True,
        agent_decided_stop=False, practice_done_requires="",
        history_keep_pairs=80, max_resumes=3,
        max_iters=2000, wall_clock_secs=86400)


def project_step(name, project, tree, status="done", check=None):
    step = {
        "name": name,
        "path": e15_loop.CURRICULUM_HANDOFF,
        "text": "DECISION: PROJECT\n" + project,
        "tree": tree,
        "status": status,
    }
    if check is not None:
        step["check"] = check
    return step


def actor_step(name, status, tree, memory=None, check=None):
    step = {
        "name": name,
        "path": e15_loop.ACTOR_HANDOFF,
        "text": f"STATUS: {status}\n{name} handoff evidence",
        "tree": tree,
        "program": f"echo {name}",
        "trace": f"completed {name}",
    }
    if memory is not None:
        step["memory"] = memory
    if check is not None:
        step["check"] = check
    return step


def verifier_step(name, verdict, check=None, tree=None):
    step = {
        "name": name,
        "path": e15_loop.VERIFIER_REPORT,
        "text": f"VERDICT: {verdict}\n{name} independently observed evidence",
    }
    if tree is not None:
        step["tree"] = tree
    if check is not None:
        step["check"] = check
    return step


def memory_step(name, files, check=None, status="done"):
    step = {"name": name, "memory": files, "status": status}
    if check is not None:
        step["check"] = check
    return step


def diagnosis_step(name, text, check=None):
    step = {
        "name": name,
        "path": e15_loop.LEARNING_DIAGNOSIS,
        "text": text,
    }
    if check is not None:
        step["check"] = check
    return step


def terminal_step(name="curriculum-converged", token="CONVERGED"):
    return {
        "name": name,
        "path": e15_loop.CURRICULUM_HANDOFF,
        "text": f"DECISION: {token}\nEvidence-based outer-loop decision.",
    }


def run(tmp_path, steps, audit_memory=None):
    vm = FakeVM()
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)
    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(audit_memory=audit_memory),
        event_sink=harness.append_event)
    assert not driver.steps
    return result, driver, harness


def run_v12(tmp_path, steps, audit_memory=None, bootstrap_seed_root="",
            continual_seed_memory=None, continual_seed_record=None):
    vm = FakeVM()
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)
    result = e15_v12_loop.e15_v12_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(),
        reset_target_vm=harness.reset,
        corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(audit_memory=audit_memory),
        event_sink=harness.append_event,
        bootstrap_seed_root=bootstrap_seed_root,
        continual_seed_memory=continual_seed_memory,
        continual_seed_record=continual_seed_record)
    assert not driver.steps
    return result, driver, harness


def test_v12_continual_memory_is_registered_before_first_target_actor(
        tmp_path):
    seed = {"prior_domain.md": b"verified transferable operating lesson"}
    seed_sha = e15_v12_loop._memory_tree_sha256(seed)
    record = {
        "schema_version": 1,
        "source_lineage": "completed_prior_domain",
        "source_task_id": "task_prior",
        "source_target_instruction_sha256": "1" * 64,
        "source_convergence_event_sha256": "2" * 64,
        "source_target_pass_event_sha256": "3" * 64,
        "source_closure_sha256": "4" * 64,
        "source_memory_snapshot": "memory_frozen_eval",
        "source_memory_manifest_sha256": "5" * 64,
        "memory_tree_sha256": seed_sha,
    }

    def sees_seed(vm, call):
        assert vm.memory == seed
        assert call["memory_before"] == seed
        assert "prior_domain.md" in call["prompt"]

    result, _driver, harness = run_v12(
        tmp_path, [
            actor_step("bootstrap-actor", "SUBMIT", {"target": "complete"},
                       check=sees_seed),
            verifier_step("bootstrap-verifier", "PASS"),
        ], continual_seed_memory=seed, continual_seed_record=record)

    assert result.status == "target_converged"
    assert [event["event_type"] for event in harness.events[:4]] == [
        "CONTINUAL_MEMORY_IMPORTED", "TARGET_BOOTSTRAP_STARTED",
        "TARGET_TEST_STARTED", "TARGET_TEST_ACTOR_STARTED",
    ]
    assert harness.events[0]["payload"]["memory_tree_sha256"] == seed_sha
    assert e15_loop._read_memory_tree(result.memory_dir) == seed
    persisted = json.loads(
        (tmp_path / "lineage/continual_seed.json").read_text())
    assert persisted["memory_tree_sha256"] == seed_sha
    assert persisted["memory_manifest"] == e15_loop._manifest(seed)


def test_v12_bootstrap_precedes_curriculum_and_delivers_actor_diagnosis(
        tmp_path):
    tail = "LOSSLESS-DIAGNOSIS-TAIL"
    diagnosis = "causal hypothesis\n" + ("detail " * 900) + tail
    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {
            "draft.md": b"unverified target limitation"}),
        memory_step("bootstrap-memory-reconciled", {
            "lesson.md": b"scoped target limitation"}),
        diagnosis_step("bootstrap-diagnosis", diagnosis),
        terminal_step("curriculum-stalled", "STALLED"),
    ]

    result, driver, harness = run_v12(tmp_path, steps)

    assert result.status == "stalled"
    assert [call["name"] for call in driver.calls] == [
        "bootstrap-actor", "bootstrap-verifier",
        "bootstrap-memory-draft", "bootstrap-memory-reconciled",
        "bootstrap-diagnosis", "curriculum-stalled",
    ]
    calls = {call["name"]: call for call in driver.calls}
    assert calls["bootstrap-actor"]["history_in"] == []
    assert calls["bootstrap-verifier"]["history_in"] == []
    assert calls["bootstrap-memory-draft"]["history_in"] == \
        calls["bootstrap-actor"]["history_out"]
    assert calls["bootstrap-diagnosis"]["continue_context"] is True
    assert "bootstrap-verifier independently observed evidence" in \
        calls["bootstrap-diagnosis"]["prompt"]
    curriculum_prompt = calls["curriculum-stalled"]["prompt"]
    assert "IMMUTABLE TARGET BOOTSTRAP Q0" in curriculum_prompt
    assert "VERDICT: FAIL" in curriculum_prompt
    assert tail in curriculum_prompt
    assert "bootstrap-actor handoff evidence" not in curriculum_prompt
    assert "scoped target limitation" not in curriculum_prompt
    assert harness.journals[0]["episode"] == 0
    assert [event["event_type"] for event in harness.events[:3]] == [
        "TARGET_BOOTSTRAP_STARTED", "TARGET_TEST_STARTED",
        "TARGET_TEST_ACTOR_STARTED",
    ]


def test_v12_stalled_curriculum_draft_cannot_open_project(tmp_path):
    def clean_retry(vm, call):
        assert vm.tree is None
        assert e15_loop.CURRICULUM_HANDOFF not in vm.files
        assert call["tree_before"] is None

    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {"draft.md": b"draft"}),
        memory_step("bootstrap-memory", {"lesson.md": b"lesson"}),
        diagnosis_step("bootstrap-diagnosis", "causal diagnosis"),
        project_step(
            "curriculum-stale-project", PROJECT_1,
            {"fixture": "unfinished"}, status="stalled"),
        project_step(
            "curriculum-completed-project", PROJECT_2,
            {"fixture": "complete"}, check=clean_retry),
        actor_step("practice-actor", "SUBMIT", {"project": "complete"}),
        verifier_step("practice-verifier", "PASS"),
        memory_step("practice-memory-draft", {"draft.md": b"draft"}),
        memory_step("practice-memory", {"lesson.md": b"revised"}),
        diagnosis_step("practice-diagnosis", "transferable diagnosis"),
        terminal_step("curriculum-stalled", "STALLED"),
    ]

    result, driver, harness = run_v12(tmp_path, steps)

    assert result.status == "stalled"
    calls = {call["name"]: call for call in driver.calls}
    stale = calls["curriculum-stale-project"]
    completed = calls["curriculum-completed-project"]
    assert completed["history_in"] == stale["history_out"]
    assert completed["continue_context"] is True
    assert "incomplete fixture tree" in completed["prompt"]
    opened = [event for event in harness.events
              if event["event_type"] == "PROJECT_OPENED"]
    assert len(opened) == 1
    project = (tmp_path / "lineage/episodes/ep001/project.md").read_text()
    assert PROJECT_2 in project
    assert PROJECT_1 not in project
    fixture_dir = str(tmp_path / "lineage/episodes/ep001/fixtures")
    assert harness.snapshots[fixture_dir] == {"fixture": "complete"}


def test_v12_imports_completed_bootstrap_without_rerunning_q0(tmp_path):
    source = tmp_path / "source"
    source_bootstrap = source / "bootstrap/q000"
    source_memory = source / "memory"
    source_target = source / "_target_input"
    for path in (source_bootstrap, source_memory, source_target):
        path.mkdir(parents=True)
    memory = {"lesson.md": b"scoped verified bootstrap lesson"}
    for name, data in memory.items():
        (source_memory / name).write_bytes(data)
    (source_target / "instruction.txt").write_text(TARGET)
    actor_handoff = "STATUS: SUBMIT\nbootstrap evidence"
    verifier_report = "VERDICT: FAIL\nindependent mismatch"
    diagnosis = "perspective mechanism remains unresolved"
    artifacts = {
        "actor_handoff.md": actor_handoff,
        "verifier_report.md": verifier_report,
        "learning_diagnosis.md": diagnosis,
    }
    for name, text in artifacts.items():
        (source_bootstrap / name).write_text(text)
    outcome = {
        "schema_version": 2,
        "kind": "target_bootstrap",
        "label": "IMMUTABLE TARGET BOOTSTRAP Q0",
        "request": TARGET,
        "terminal_outcome": "FAIL",
        "actor_handoff": actor_handoff,
        "verifier_report": verifier_report,
        "learning_diagnosis": diagnosis,
        "memory_before": {},
        "memory_after": e15_loop._manifest(memory),
    }
    (source_bootstrap / "outcome.json").write_text(json.dumps(outcome))
    completion = EventLedger(
        source / "E15_EVENTS.jsonl").append(
            "TARGET_BOOTSTRAP_COMPLETED", status="completed",
            state={"project_open": False, "memory_phase_open": False,
                   "recovery_open": False},
            payload={
                "terminal_outcome": "FAIL",
                "memory_tree_sha256":
                    e15_v12_loop._memory_tree_sha256(memory),
                "learning_diagnosis_sha256": hashlib.sha256(
                    diagnosis.encode()).hexdigest(),
            })

    result, driver, harness = run_v12(
        tmp_path, [terminal_step("curriculum-stalled", "STALLED")],
        bootstrap_seed_root=str(source))

    assert result.status == "stalled"
    assert [call["name"] for call in driver.calls] == ["curriculum-stalled"]
    prompt = driver.calls[0]["prompt"]
    assert verifier_report in prompt
    assert diagnosis in prompt
    assert actor_handoff not in prompt
    assert e15_loop._read_memory_tree(result.memory_dir) == memory
    assert harness.journals[0]["meta"]["kind"] == \
        "v12-target-bootstrap-import"
    imported = next(event for event in harness.events
                    if event["event_type"] == "TARGET_BOOTSTRAP_IMPORTED")
    assert imported["payload"]["source_bootstrap_event_sha256"] == \
        completion["event_sha256"]
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["learning_experiences"] == 1


def test_v12_imports_completed_continual_bootstrap_with_bound_provenance(
        tmp_path):
    source = tmp_path / "continual-source"
    source_bootstrap = source / "bootstrap/q000"
    source_memory = source / "memory"
    source_target = source / "_target_input"
    for path in (source_bootstrap, source_memory, source_target):
        path.mkdir(parents=True)
    before = {"prior.md": b"verified prior-domain memory"}
    after = {
        **before,
        "new-domain.md": b"diagnosed failed-Q0 learning",
    }
    for name, data in after.items():
        (source_memory / name).write_bytes(data)
    (source_target / "instruction.txt").write_text(TARGET)
    actor_handoff = "STATUS: SUBMIT\npartial continual bootstrap"
    verifier_report = "VERDICT: FAIL\nnew-domain mismatch"
    diagnosis = "new-domain mechanism remains unresolved"
    for name, text in {
        "actor_handoff.md": actor_handoff,
        "verifier_report.md": verifier_report,
        "learning_diagnosis.md": diagnosis,
    }.items():
        (source_bootstrap / name).write_text(text)
    outcome = {
        "schema_version": 2,
        "kind": "target_bootstrap",
        "label": "IMMUTABLE TARGET BOOTSTRAP Q0",
        "request": TARGET,
        "terminal_outcome": "FAIL",
        "actor_handoff": actor_handoff,
        "verifier_report": verifier_report,
        "learning_diagnosis": diagnosis,
        "memory_before": e15_loop._manifest(before),
        "memory_after": e15_loop._manifest(after),
    }
    (source_bootstrap / "outcome.json").write_text(json.dumps(outcome))
    before_sha = e15_v12_loop._memory_tree_sha256(before)
    after_sha = e15_v12_loop._memory_tree_sha256(after)
    continual_seed = {
        "schema_version": 1,
        "source_lineage": "completed-prior-domain",
        "source_task_id": "task_prior",
        "source_target_instruction_sha256": "1" * 64,
        "source_convergence_event_sha256": "2" * 64,
        "source_target_pass_event_sha256": "3" * 64,
        "source_closure_sha256": "4" * 64,
        "source_memory_snapshot": "memory_frozen_eval",
        "source_memory_manifest_sha256": "5" * 64,
        "memory_tree_sha256": before_sha,
        "memory_manifest": e15_loop._manifest(before),
    }
    (source / "continual_seed.json").write_text(json.dumps(continual_seed))
    ledger = EventLedger(source / "E15_EVENTS.jsonl")
    closed = {"project_open": False, "memory_phase_open": False,
              "recovery_open": False}
    opened = {**closed, "project_open": True}
    continual = ledger.append(
        "CONTINUAL_MEMORY_IMPORTED", status="completed", state=closed,
        payload={
            key: continual_seed[key] for key in (
                "source_lineage", "source_task_id",
                "source_convergence_event_sha256",
                "source_target_pass_event_sha256", "source_closure_sha256",
                "memory_tree_sha256")
        })
    ledger.append(
        "TARGET_BOOTSTRAP_STARTED", status="in_progress", state=opened,
        payload={
            "target_sha256": hashlib.sha256(TARGET.encode()).hexdigest(),
            "input_manifest_sha256": "6" * 64,
            "frozen_memory_tree_sha256": before_sha,
        })
    completion = ledger.append(
        "TARGET_BOOTSTRAP_COMPLETED", status="completed", state=closed,
        payload={
            "terminal_outcome": "FAIL",
            "memory_tree_sha256": after_sha,
            "learning_diagnosis_sha256": hashlib.sha256(
                diagnosis.encode()).hexdigest(),
        })

    result, driver, harness = run_v12(
        tmp_path, [terminal_step("curriculum-stalled", "STALLED")],
        bootstrap_seed_root=str(source))

    assert result.status == "stalled"
    assert [call["name"] for call in driver.calls] == ["curriculum-stalled"]
    assert e15_loop._read_memory_tree(result.memory_dir) == after
    imported = next(event for event in harness.events
                    if event["event_type"] == "TARGET_BOOTSTRAP_IMPORTED")
    assert imported["payload"]["source_bootstrap_event_sha256"] == \
        completion["event_sha256"]
    assert imported["payload"]["source_continual_import_event_sha256"] == \
        continual["event_sha256"]
    seed_record = json.loads(
        (tmp_path / "lineage/bootstrap_seed.json").read_text())
    assert seed_record["source_memory_before_tree_sha256"] == before_sha


def test_v12_rejects_continual_bootstrap_with_unbound_memory_before(tmp_path):
    source = tmp_path / "bad-continual-source"
    for path in (source / "bootstrap/q000", source / "memory",
                 source / "_target_input"):
        path.mkdir(parents=True)
    before = {"prior.md": b"prior memory"}
    after = {**before, "new.md": b"new memory"}
    for name, data in after.items():
        (source / "memory" / name).write_bytes(data)
    (source / "_target_input/instruction.txt").write_text(TARGET)
    actor_handoff = "STATUS: SUBMIT\npartial"
    verifier_report = "VERDICT: FAIL\nmismatch"
    diagnosis = "causal diagnosis"
    for name, text in {
        "actor_handoff.md": actor_handoff,
        "verifier_report.md": verifier_report,
        "learning_diagnosis.md": diagnosis,
    }.items():
        (source / "bootstrap/q000" / name).write_text(text)
    (source / "bootstrap/q000/outcome.json").write_text(json.dumps({
        "schema_version": 2,
        "kind": "target_bootstrap",
        "label": "IMMUTABLE TARGET BOOTSTRAP Q0",
        "request": TARGET,
        "terminal_outcome": "FAIL",
        "actor_handoff": actor_handoff,
        "verifier_report": verifier_report,
        "learning_diagnosis": diagnosis,
        "memory_before": e15_loop._manifest(before),
        "memory_after": e15_loop._manifest(after),
    }))
    before_sha = e15_v12_loop._memory_tree_sha256(before)
    closed = {"project_open": False, "memory_phase_open": False,
              "recovery_open": False}
    opened = {**closed, "project_open": True}
    ledger = EventLedger(source / "E15_EVENTS.jsonl")
    ledger.append(
        "CONTINUAL_MEMORY_IMPORTED", status="completed", state=closed,
        payload={
            "source_lineage": "prior", "source_task_id": "task_prior",
            "source_convergence_event_sha256": "2" * 64,
            "source_target_pass_event_sha256": "3" * 64,
            "source_closure_sha256": "4" * 64,
            "memory_tree_sha256": before_sha,
        })
    ledger.append(
        "TARGET_BOOTSTRAP_STARTED", status="in_progress", state=opened,
        payload={"frozen_memory_tree_sha256": before_sha})
    ledger.append(
        "TARGET_BOOTSTRAP_COMPLETED", status="completed", state=closed,
        payload={
            "terminal_outcome": "FAIL",
            "memory_tree_sha256":
                e15_v12_loop._memory_tree_sha256(after),
            "learning_diagnosis_sha256": hashlib.sha256(
                diagnosis.encode()).hexdigest(),
        })
    # This file claims a different pre-Q0 manifest than the immutable outcome.
    (source / "continual_seed.json").write_text(json.dumps({
        "schema_version": 1,
        "memory_manifest": e15_loop._manifest(
            {"other.md": b"different"}),
        "memory_tree_sha256": before_sha,
    }))

    result, _driver, _harness = run_v12(
        tmp_path, [], bootstrap_seed_root=str(source))

    assert result.status == "infra"
    assert "does not bind memory_before" in result.reason


def test_v12_empty_memory_q0_pass_is_immediate_target_convergence(tmp_path):
    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "complete"}),
        verifier_step("bootstrap-verifier", "PASS"),
    ]

    result, driver, harness = run_v12(tmp_path, steps)

    assert result.status == "target_converged"
    assert [call["name"] for call in driver.calls] == [
        "bootstrap-actor", "bootstrap-verifier"]
    assert harness.journals == []
    assert harness.installs == []
    pass_event = next(
        event for event in harness.events
        if event["event_type"] == "TARGET_TEST_PASSED")
    assert pass_event["payload"]["target_test_index"] == 0
    assert pass_event["payload"]["bootstrap"] is True
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["learning_experiences"] == 0


def test_v12_failed_target_test_returns_learning_and_curriculum_continues(
        tmp_path):
    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {"draft.md": b"draft"}),
        memory_step("bootstrap-memory", {"lesson.md": b"baseline lesson"}),
        diagnosis_step("bootstrap-diagnosis", "baseline causal hypothesis"),
        terminal_step("curriculum-ready", "READY_FOR_TARGET_TEST"),
        actor_step("target-test-actor", "SUBMIT", {"target": "still partial"}),
        verifier_step("target-test-verifier", "FAIL"),
        memory_step("target-fail-memory-draft", {
            "lesson.md": b"baseline lesson", "draft2.md": b"draft"}),
        memory_step("target-fail-memory", {
            "lesson.md": b"revised after direct failure"}),
        diagnosis_step(
            "target-fail-diagnosis", "direct retest exposed unresolved timing"),
        terminal_step("curriculum-after-fail", "STALLED"),
    ]

    result, driver, harness = run_v12(tmp_path, steps)

    assert result.status == "stalled"
    calls = {call["name"]: call for call in driver.calls}
    assert TARGET in calls["target-test-actor"]["prompt"]
    assert calls["target-test-actor"]["history_in"] == []
    assert calls["target-test-verifier"]["history_in"] == []
    assert len(calls["curriculum-after-fail"]["history_in"]) == 2
    assert "direct retest exposed unresolved timing" in \
        calls["curriculum-after-fail"]["prompt"]
    assert "target-test-verifier independently observed evidence" in \
        calls["curriculum-after-fail"]["prompt"]
    assert [journal["meta"]["kind"] for journal in harness.journals] == [
        "v12-target-bootstrap", "v12-target-test-fail"]
    assert any(event["event_type"] == "TARGET_TEST_FAILED"
               for event in harness.events)
    assert not any(event["event_type"] == "TARGET_TEST_PASSED"
                   for event in harness.events)


def test_v12_target_pass_freezes_exact_preprobe_memory(tmp_path):
    frozen = {"lesson.md": b"memory that is actually tested"}
    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {"draft.md": b"draft"}),
        memory_step("bootstrap-memory", frozen),
        diagnosis_step("bootstrap-diagnosis", "grounded initial diagnosis"),
        terminal_step("curriculum-ready", "READY_FOR_TARGET_TEST"),
        actor_step(
            "target-test-actor", "SUBMIT", {"target": "complete"},
            memory={"illegal.md": b"must never become tested memory"}),
        verifier_step("target-test-verifier", "PASS"),
    ]

    result, driver, harness = run_v12(tmp_path, steps)

    assert result.status == "target_converged"
    assert e15_loop._read_memory_tree(result.memory_dir) == frozen
    assert harness.pulls == [frozen]
    assert harness.installs == [frozen]
    assert len(harness.journals) == 1
    assert not any(call["name"].startswith("target-pass-memory")
                   for call in driver.calls)
    pass_events = [event for event in harness.events
                   if event["event_type"] == "TARGET_TEST_PASSED"]
    assert len(pass_events) == 1
    expected_sha = e15_v12_loop._memory_tree_sha256(frozen)
    assert pass_events[0]["payload"]["frozen_memory_tree_sha256"] == \
        expected_sha


def test_v12_role_models_restore_k3_curriculum_and_verifier():
    from run_e15 import _load_configs

    configs = _load_configs()

    assert configs["actor"].model == "z-ai/glm-5.2"
    assert configs["memory"].model == "z-ai/glm-5.2"
    assert configs["curriculum"].model == "moonshotai/kimi-k3"
    assert configs["verifier"].model == "moonshotai/kimi-k3"
    for role in ("curriculum", "verifier"):
        assert configs[role].look_ensemble == 1
        assert configs[role].vision_model == ""
        assert configs[role].vision_model_2 == ""


def test_v12_task_visible_asset_names_are_authorized_without_weakening_fence(monkeypatch):
    from tools import exam_fence
    # This unit test exercises authorization, not external denylist extraction.
    monkeypatch.setattr(exam_fence, "_load_constants", lambda: {
        "056": list(e15_v12_loop.TARGET_INPUTS),
    })
    visible_names = " ".join(e15_v12_loop.TARGET_INPUTS)

    assert audit_text(
        visible_names, mode="exam", authorized_instruction=TARGET)
    assert audit_text(
        visible_names, mode="exam",
        authorized_instruction=e15_v12_loop._target_audit_surface(TARGET)) == []
    assert audit_text(
        "/home/sibo/OSWorld-V2/evaluation_examples/task_class/task_056.py",
        mode="exam",
        authorized_instruction=e15_v12_loop._target_audit_surface(TARGET))


def test_v12_target_publication_defect_returns_to_same_actor_context(tmp_path):
    unsafe = actor_step(
        "bootstrap-bad-publication", "SUBMIT", {"unsafe": "symlink"})
    unsafe["target_shape_safe"] = False
    repaired = actor_step(
        "bootstrap-repaired-publication", "SUBMIT", {"target": "partial"})
    repaired["target_shape_safe"] = True
    steps = [
        unsafe,
        repaired,
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {"draft.md": b"draft"}),
        memory_step("bootstrap-memory", {"lesson.md": b"lesson"}),
        diagnosis_step("bootstrap-diagnosis", "causal diagnosis"),
        terminal_step("curriculum-stalled", "STALLED"),
    ]

    result, driver, _harness = run_v12(tmp_path, steps)

    assert result.status == "stalled"
    calls = {call["name"]: call for call in driver.calls}
    first = calls["bootstrap-bad-publication"]
    repaired = calls["bootstrap-repaired-publication"]
    assert repaired["history_in"] == first["history_out"]
    assert repaired["continue_context"] is True
    assert "not a correctness grade" in repaired["prompt"]
    assert "missing, unsafe" in repaired["prompt"]


def test_v12_practice_publication_defect_returns_to_same_actor_context(
        tmp_path):
    steps = [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
        verifier_step("bootstrap-verifier", "FAIL"),
        memory_step("bootstrap-memory-draft", {"draft.md": b"draft"}),
        memory_step("bootstrap-memory", {"lesson.md": b"lesson"}),
        diagnosis_step("bootstrap-diagnosis", "causal diagnosis"),
        project_step("curriculum-project", PROJECT_1, {"fixture": "input"}),
        actor_step("practice-bad-publication", "SUBMIT", None),
        actor_step("practice-repaired-publication", "SUBMIT", {
            "project": "complete"}),
        verifier_step("practice-verifier", "PASS"),
        memory_step("practice-memory-draft", {"draft.md": b"draft"}),
        memory_step("practice-memory", {"lesson.md": b"revised"}),
        diagnosis_step("practice-diagnosis", "transferable diagnosis"),
        terminal_step("curriculum-stalled", "STALLED"),
    ]

    result, driver, _harness = run_v12(tmp_path, steps)

    assert result.status == "stalled"
    calls = {call["name"]: call for call in driver.calls}
    first = calls["practice-bad-publication"]
    repaired = calls["practice-repaired-publication"]
    assert repaired["history_in"] == first["history_out"]
    assert repaired["continue_context"] is True
    assert "not a correctness grade" in repaired["prompt"]
    assert e15_loop.PROJECT_ROOT in repaired["prompt"]


def test_v12_refuses_target_inputs_that_differ_from_fence(tmp_path):
    vm = FakeVM()
    driver = ScriptedAgents(vm, [])
    harness = FakeHarness(vm, driver)

    result = e15_v12_loop.e15_v12_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), reset_target_vm=harness.reset,
        corpus_path=str(tmp_path / "corpus"), hooks=harness.hooks(),
        event_sink=harness.append_event,
        expected_input_manifest_sha256="0" * 64)

    assert result.status == "infra"
    assert "differ from the fenced setup" in result.reason
    assert driver.calls == []


def test_v12_real_capture_transport_failure_does_not_resample_actor(tmp_path):
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        actor_step("bootstrap-actor", "SUBMIT", {"target": "partial"}),
    ])
    harness = FakeHarness(vm, driver)
    hooks = harness.hooks()
    hooks.capture_project = lambda *_args, **_kwargs: {
        "ok": False, "error": "host transport failed"}

    result = e15_v12_loop.e15_v12_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), reset_target_vm=harness.reset,
        corpus_path=str(tmp_path / "corpus"), hooks=hooks,
        event_sink=harness.append_event)

    assert result.status == "infra"
    assert "could not be captured" in result.reason
    assert [call["name"] for call in driver.calls] == ["bootstrap-actor"]


def test_actor_execution_view_is_exact_cumulative_and_reasoning_free(
        tmp_path):
    actor = tmp_path / "actor"
    first = actor / "attempt_001/segment_000/iter_01"
    second = actor / "attempt_002/segment_001/iter_09"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    long_trace = b"x" * 20_001
    (first / "program.sh").write_bytes(b"echo first\n")
    (first / "trace.txt").write_bytes(long_trace)
    (first / "trace_meta.json").write_bytes(b'{"exit_code": 0}\n')
    (first / "look.json").write_bytes(b'{"path": "/tmp/a"}\n')
    (first / "turn.txt").write_bytes(b"private model turn")
    (actor / "attempt_001/segment_000/transcript.json").write_bytes(
        b"private transcript")
    (second / "program.py").write_bytes(b"print('second')\n")
    (second / "trace.txt").write_bytes(b"second trace")
    (second / "worklog.txt").write_bytes(b"private reasoning summary")
    (second / "checks.json").write_bytes(b"host judgement")

    view_one = tmp_path / "view-one"
    view_two = tmp_path / "view-two"
    record_one = e15_loop._build_actor_execution_evidence(
        actor, 2, view_one)
    record_two = e15_loop._build_actor_execution_evidence(
        actor, 2, view_two)
    record_reused = e15_loop._build_actor_execution_evidence(
        actor, 2, view_one)

    expected = {
        "attempt_001/segment_000/iter_01/look.json",
        "attempt_001/segment_000/iter_01/program.sh",
        "attempt_001/segment_000/iter_01/trace.txt",
        "attempt_001/segment_000/iter_01/trace_meta.json",
        "attempt_002/segment_001/iter_09/program.py",
        "attempt_002/segment_001/iter_09/trace.txt",
        "SHA256SUMS",
    }
    actual = {
        str(path.relative_to(view_one))
        for path in view_one.rglob("*") if path.is_file()
    }
    assert actual == expected
    assert (view_one /
            "attempt_001/segment_000/iter_01/trace.txt").read_bytes() == \
        long_trace
    assert not any(name.endswith((
        "turn.txt", "transcript.json", "worklog.txt", "checks.json"))
                   for name in actual)
    assert (view_one / "SHA256SUMS").read_bytes() == \
        (view_two / "SHA256SUMS").read_bytes()
    assert record_one["manifest_sha256"] == \
        record_two["manifest_sha256"]
    assert record_reused == record_one
    assert record_one["files"] == 6
    assert record_one["bytes"] == sum(
        (view_one / name).stat().st_size for name in expected
        if name != "SHA256SUMS")
    assert record_one["manifest_sha256"] == hashlib.sha256(
        (view_one / "SHA256SUMS").read_bytes()).hexdigest()

    (view_one / "unmanifested-empty-directory").mkdir()
    with pytest.raises(
            e15_loop.E15InfrastructureError, match="does not match"):
        e15_loop._build_actor_execution_evidence(actor, 2, view_one)

    bad_actor = tmp_path / "bad-actor"
    bad_iter = bad_actor / "attempt_001/segment_000/iter_01"
    bad_iter.mkdir(parents=True)
    (bad_iter / "program.sh").symlink_to(first / "program.sh")
    with pytest.raises(
            e15_loop.E15InfrastructureError, match="file symlink"):
        e15_loop._build_actor_execution_evidence(
            bad_actor, 1, tmp_path / "bad-view")


def test_guest_execution_evidence_verification_rejects_additive_tamper(
        tmp_path):
    actor = tmp_path / "actor"
    iteration = actor / "attempt_001/segment_000/iter_01"
    iteration.mkdir(parents=True)
    (iteration / "program.sh").write_bytes(b"echo exact\n")
    (iteration / "trace.txt").write_bytes(b"exact trace\n")
    view = tmp_path / "guest evidence with spaces"
    record = e15_loop._build_actor_execution_evidence(actor, 1, view)
    vm = LocalCommandVM()

    e15_loop._verify_actor_execution_evidence(
        vm, record, guest_path=str(view))
    (view / "unmanifested.txt").write_bytes(b"fabricated")
    with pytest.raises(
            e15_loop.E15InfrastructureError, match="hash verification"):
        e15_loop._verify_actor_execution_evidence(
            vm, record, guest_path=str(view))


def test_curriculum_contract_keeps_all_deliverables_in_replayed_root():
    prompt = " ".join(e15_loop._curriculum_runtime_contract().split())

    assert "Every persistent or final Actor Agent deliverable required by the " \
        "natural-language project" in prompt
    assert "including any additional saved or exported copy" in prompt
    assert "must have its destination inside /home/user/evolution_project" \
        in prompt
    assert "never require a persistent or final deliverable at any path " \
        "outside that root" in prompt
    assert "free to choose every filename and layout within the root" in prompt
    assert "harness snapshots and replays only this root" in prompt


def test_context_lifecycle_isolation_and_verifier_authority(tmp_path):
    def memory_one_sees_failed_submission(vm, _call):
        assert vm.tree == {"state": "candidate-one"}
        assert vm.memory == {}  # prohibited work-phase edit was discarded

    def reconciliation_one_sees_unpromoted_draft(vm, call):
        assert vm.tree == {"state": "candidate-one"}
        assert vm.memory == {"draft.md": b"first-pass draft"}
        assert not (tmp_path / "lineage/memory/draft.md").exists()
        assert call["history_in"][-1] == {
            "role": "assistant", "content": "memory-one-draft"}

    def actor_two_loads_promoted_memory(vm, _call):
        assert vm.tree == {"state": "fixtures-two"}
        assert vm.memory == {"lesson.md": b"grounded failure lesson"}

    steps = [
        project_step("curriculum-project-one", PROJECT_1,
                     {"state": "fixtures-one"}),
        actor_step("actor-one-submit", "SUBMIT", {"state": "candidate-one"},
                   memory={"illegal-work-edit.md": b"discard me"}),
        verifier_step("verifier-one-fail", "FAIL",
                      check=lambda vm, _c: vm.tree ==
                      {"state": "candidate-one"}),
        memory_step("memory-one-draft", {"draft.md": b"first-pass draft"},
                    check=memory_one_sees_failed_submission),
        memory_step("memory-one-reconciled",
                    {"lesson.md": b"grounded failure lesson"},
                    check=reconciliation_one_sees_unpromoted_draft),
        project_step("curriculum-project-two", PROJECT_2,
                     {"state": "fixtures-two"}),
        actor_step("actor-two-submit", "SUBMIT", {"state": "candidate-two"},
                   check=actor_two_loads_promoted_memory),
        verifier_step("verifier-two-pass", "PASS",
                      check=lambda vm, _c: vm.tree ==
                      {"state": "candidate-two"}),
        memory_step("memory-two-draft", {
            "lesson.md": b"grounded failure lesson",
            "draft-transfer.md": b"first-pass transfer claim",
        }),
        memory_step("memory-two-reconciled", {
            "lesson.md": b"grounded failure lesson",
            "transfer.md": b"verified transfer",
        }),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    assert result.projects == 2
    calls = {call["name"]: call for call in driver.calls}

    # Actor and Verifier contexts are fresh for every episode. Each episode has
    # exactly one submission and one authoritative verdict.
    assert calls["actor-one-submit"]["history_in"] == []
    assert calls["actor-two-submit"]["history_in"] == []
    assert calls["verifier-one-fail"]["history_in"] == []
    assert calls["verifier-two-pass"]["history_in"] == []
    assert all(not call["continue_context"] for call in (
        calls["actor-one-submit"], calls["actor-two-submit"],
        calls["verifier-one-fail"], calls["verifier-two-pass"]))

    # Only the Verifier receives the reasoning-free execution evidence view.
    evidence_one = calls["verifier-one-fail"]["execution_evidence_before"]
    evidence_two = calls["verifier-two-pass"]["execution_evidence_before"]
    assert "attempt_001/segment_000/iter_01/program.sh" in evidence_one
    assert b"actor-one-submit" in evidence_one[
        "attempt_001/segment_000/iter_01/program.sh"]
    assert b"actor-two-submit" in evidence_two[
        "attempt_001/segment_000/iter_01/program.sh"]
    assert b"actor-one-submit" not in b"".join(evidence_two.values())
    assert len(harness.evidence_pushes) == 2
    for name, call in calls.items():
        if name.startswith("verifier-"):
            assert e15_loop.ACTOR_EXECUTION_EVIDENCE in call["prompt"]
        else:
            assert e15_loop.ACTOR_EXECUTION_EVIDENCE not in call["prompt"]

    # Both memory phases retain the submitting Actor context and promote only
    # after whole-corpus reconciliation.
    assert calls["memory-one-draft"]["continue_context"] is True
    assert calls["memory-one-reconciled"]["continue_context"] is True
    assert calls["memory-one-reconciled"]["history_in"] == \
        calls["memory-one-draft"]["history_out"]
    assert calls["memory-one-draft"]["allow_noop_done"] is True
    assert calls["memory-one-reconciled"]["allow_noop_done"] is True

    # Curriculum alone persists across episodes and sees black-box Verifier
    # evidence, not the Actor handoff or private memory contents.
    assert calls["curriculum-project-one"]["history_in"] == []
    assert len(calls["curriculum-project-two"]["history_in"]) == 2
    assert len(calls["curriculum-converged"]["history_in"]) == 4
    second_prompt = calls["curriculum-project-two"]["prompt"]
    assert "TERMINAL OUTCOME: FAIL" in second_prompt
    assert "verifier-one-fail independently observed evidence" in second_prompt
    assert "actor-one-submit handoff evidence" not in second_prompt
    assert "grounded failure lesson" not in second_prompt

    assert [entry["meta"]["terminal_outcome"]
            for entry in harness.journals] == ["FAIL", "PASS"]
    assert harness.pulls == [
        {"lesson.md": b"grounded failure lesson"},
        {
            "lesson.md": b"grounded failure lesson",
            "transfer.md": b"verified transfer",
        },
    ]
    assert harness.memory_audits == harness.pulls
    assert harness.installs == harness.pulls

    assert [event["event_type"] for event in harness.events] == [
        "PROJECT_OPENED", "ACTOR_PHASE_STARTED", "VERIFIER_PHASE_STARTED",
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED",
        "PROJECT_OPENED", "ACTOR_PHASE_STARTED", "VERIFIER_PHASE_STARTED",
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED",
    ]
    assert not any(event["event_type"] ==
                   "VERIFIER_PASS_CONFIRMATION_STARTED"
                   for event in harness.events)
    verifier_events = [event for event in harness.events
                       if event["event_type"] == "VERIFIER_PHASE_STARTED"]
    assert len(verifier_events) == 2
    assert all(len(event["payload"]["actor_execution_manifest_sha256"]) == 64
               for event in verifier_events)
    assert harness.audit_calls
    assert all(call[-1] == TARGET for call in harness.audit_calls)


def test_fail_is_terminal_and_distils_as_unverified_failure(tmp_path):
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-submit", "SUBMIT", {"state": "partial"}),
        verifier_step("verifier-fail", "FAIL"),
        memory_step("memory-fail", {
            "unresolved.md": b"unverified limitation, not success"}),
        memory_step("memory-fail-reconciled", {
            "unresolved.md": b"unverified limitation, not success"}),
        terminal_step("curriculum-stalled", "STALLED"),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "stalled"
    assert [call["name"] for call in driver.calls].count(
        "verifier-fail") == 1
    assert harness.journals[0]["meta"]["terminal_outcome"] == "FAIL"
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["terminal_outcome"] == "FAIL"
    assert outcome["verifier_reports"][0].startswith("VERDICT: FAIL")


def test_pass_is_terminal_without_fresh_confirmation(tmp_path):
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-submit", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-pass", {"verified.md": b"verified experience"}),
        memory_step("memory-pass-reconciled",
                    {"verified.md": b"verified experience"}),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert [name for name in calls if name.startswith("verifier-")] == [
        "verifier-pass"]
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["terminal_outcome"] == "PASS"
    assert outcome["verification_cycles"] == 1
    assert len(outcome["actor_handoffs"]) == 1
    assert len(outcome["verifier_reports"]) == 1
    assert (tmp_path / "lineage/episodes/ep001/handoffs/"
            "verifier_001.md").read_text().startswith("VERDICT: PASS")
    assert harness.journals[0]["meta"]["terminal_outcome"] == "PASS"
    assert len(harness.evidence_pushes) == 1
    assert all(event["event_type"] != "VERIFIER_PASS_CONFIRMATION_STARTED"
               for event in harness.events)


@pytest.mark.parametrize("failure", ["push", "hash"])
def test_execution_evidence_transport_fails_closed_before_verifier(
        tmp_path, failure):
    vm = FakeVM()
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
    ]
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)
    if failure == "push":
        harness.evidence_push_ok = False
    else:
        vm.evidence_verify_ok = False

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "execution evidence" in result.reason
    assert not driver.steps
    assert [call["name"] for call in driver.calls] == [
        "curriculum-project", "actor-ready"]
    assert harness.pulls == []
    assert harness.memory_audits == []
    assert harness.journals == []
    assert harness.installs == []
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": False,
        "recovery_open": False,
    }
    assert all(event["event_type"] not in {
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED"}
               for event in harness.events)
    assert not (tmp_path / "lineage/episodes/ep001/handoffs/"
                "verifier_001.md").exists()


@pytest.mark.parametrize("tamper_kind", ["replace", "add"])
def test_execution_evidence_mutation_invalidates_published_verdict(
        tmp_path, tamper_kind):
    def tamper(vm, _call):
        path = "attempt_001/segment_000/iter_01/program.sh"
        assert path in vm.execution_evidence
        if tamper_kind == "replace":
            vm.execution_evidence[path] = b"fabricated replacement"
        else:
            vm.execution_evidence["unmanifested/claim.txt"] = \
                b"fabricated additive evidence"

    vm = FakeVM()
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass-after-tamper", "PASS", check=tamper),
    ]
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "hash verification" in result.reason
    assert not driver.steps
    assert harness.pulls == []
    assert harness.journals == []
    assert harness.installs == []
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"
    assert not (tmp_path / "lineage/episodes/ep001/handoffs/"
                "verifier_001.md").exists()


def test_candidate_mutation_by_sole_verifier_fails_closed(tmp_path):
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step(
            "verifier-mutated-candidate", "PASS",
            tree={"state": "verifier-mutation"}),
    ]
    vm = FakeVM()
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "candidate snapshot integrity verification failed" in result.reason
    assert not driver.steps
    assert harness.pulls == []
    assert harness.journals == []
    assert harness.installs == []
    handoffs = tmp_path / "lineage/episodes/ep001/handoffs"
    assert not (handoffs / "verifier_001.md").exists()
    assert not (handoffs / "verifier_pass_proposal_001.md").exists()
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"


def test_malformed_handoff_resumes_same_agent_context_without_round_cap(
        tmp_path):
    malformed = verifier_step("verifier-malformed", "FAIL")
    malformed["text"] = "I am not using the transport token yet."
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-yield", "SUBMIT", {"state": "partial"}),
        malformed,
        verifier_step("verifier-corrected", "FAIL"),
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step(),
    ]
    result, driver, _ = run(tmp_path, steps)
    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert len(calls["verifier-corrected"]["history_in"]) == 2
    assert calls["verifier-corrected"]["continue_context"] is True
    assert "not contain an allowed" in \
        calls["verifier-corrected"]["prompt"]


def test_actor_accepts_only_neutral_submit_and_repairs_transport_in_context(
        tmp_path):
    obsolete = actor_step(
        "actor-obsolete-ready", "SUBMIT", {"state": "candidate"})
    obsolete["text"] = "STATUS: READY\nobsolete success-claiming token"
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        obsolete,
        actor_step("actor-submit", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-fail", "FAIL"),
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert len(calls["actor-submit"]["history_in"]) == 2
    assert calls["actor-submit"]["continue_context"] is True
    assert "did not contain an allowed" in calls["actor-submit"]["prompt"]
    assert [event["event_type"] for event in harness.events].count(
        "ACTOR_PHASE_STARTED") == 1
    assert e15_loop._parse_handoff(
        "STATUS: SUBMIT\ncurrent state", e15_loop._ACTOR_TOKEN).token == \
        "SUBMIT"
    assert e15_loop._parse_handoff(
        "STATUS: READY\nold token", e15_loop._ACTOR_TOKEN) is None
    assert e15_loop._parse_handoff(
        "STATUS: YIELD\nold token", e15_loop._ACTOR_TOKEN) is None


def test_verifier_heading_then_unique_verdict_publishes_atomically(tmp_path):
    report = (
        "# Independent report\n\nVERDICT: FAIL\n\n"
        "The required application workflow is contradicted by the evidence."
    )
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-yield", "SUBMIT", {"state": "candidate"}),
        {
            "name": "verifier-published",
            "path": e15_loop.VERIFIER_REPORT,
            "text": report,
            # A valid publication wins even if a generic loop boundary is
            # reported at the same time; no second stochastic verdict is drawn.
            "status": "stalled",
        },
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step("curriculum-stalled", "STALLED"),
    ]
    result, driver, _ = run(tmp_path, steps)

    assert result.status == "stalled"
    call = next(c for c in driver.calls
                if c["name"] == "verifier-published")
    assert call["terminal_handoff_ready"] is True
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["verifier_reports"] == [report]
    assert outcome["terminal_outcome"] == "FAIL"


def test_verifier_report_token_must_be_unique_and_outside_code_fences():
    parse = e15_loop._parse_unique_handoff
    token = e15_loop._VERIFIER_TOKEN

    accepted = parse("# title\n\nVERDICT: FAIL\nEvidence", token)
    assert accepted is not None and accepted.token == "FAIL"
    assert accepted.text.startswith("# title")
    assert parse("VERDICT: FAIL\nVERDICT: FAIL", token) is None
    assert parse("VERDICT: FAIL\nVERDICT: PASS", token) is None
    assert parse("```text\nVERDICT: FAIL\n```", token) is None
    assert parse("    VERDICT: FAIL", token) is None


def test_memory_reconciliation_continues_uncapped_until_actor_done(tmp_path):
    draft = {"draft.md": b"first-pass draft"}
    final = {"lesson.md": b"reconciled durable lesson"}
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", draft),
        memory_step("reconciliation-incomplete", draft, status="budget"),
        memory_step("reconciliation-done", final),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    first = calls["reconciliation-incomplete"]
    second = calls["reconciliation-done"]
    assert first["history_in"] == calls["memory-draft"]["history_out"]
    assert second["history_in"] == first["history_out"]
    assert first["continue_context"] is True
    assert second["continue_context"] is True
    assert first["allow_noop_done"] is True
    assert second["allow_noop_done"] is True
    assert first["sink"].endswith("memory_reconciliation/segment_000")
    assert second["sink"].endswith("memory_reconciliation/segment_001")
    assert "without your terminal decision" in second["prompt"]
    assert harness.pulls == [final]
    assert harness.memory_audits == [final]
    assert harness.journals[0]["files"] == final
    assert harness.installs == [final]


def test_memory_boundary_rejects_whole_candidate_and_preserves_durable_bank(
        tmp_path):
    lineage_memory = tmp_path / "lineage/memory"
    lineage_memory.mkdir(parents=True)
    (lineage_memory / "old.md").write_bytes(b"known-good memory")
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"unpromoted draft"}),
        memory_step("memory-leak", {"leak.md": b"forbidden grader content"}),
    ]

    def reject(_files, *_args, **_kwargs):
        return {}

    result, _, harness = run(tmp_path, steps, audit_memory=reject)
    assert result.status == "quarantined"
    assert harness.journals == []
    assert (lineage_memory / "old.md").read_bytes() == b"known-good memory"
    assert not (lineage_memory / "leak.md").exists()
    assert harness.events[-1]["event_type"] == "MEMORY_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": True,
        "recovery_open": False,
    }
    assert all(event["event_type"] != "PROJECT_CLOSED"
               for event in harness.events)


def test_emergency_ceiling_is_infrastructure_not_learning_outcome(tmp_path):
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        {"name": "actor-emergency-ceiling", "status": "safety_ceiling"},
    ]
    result, _, harness = run(tmp_path, steps)
    assert result.status == "infra"
    assert "safety_ceiling" in result.reason
    assert harness.journals == []
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["status"] == "infra"
    assert "PASS" not in state["reason"]
    assert "CONVERGED" not in state["reason"]
    assert harness.events[-1]["event_type"] == "ACTOR_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": False,
        "recovery_open": False,
    }


def test_e15_has_no_host_grade_preflight_reward_or_evaluator_surface():
    source = inspect.getsource(e15_loop)
    assert "from explore import reward" not in source
    assert "grade_instance(" not in source
    assert "run_dry_gate(" not in source
    assert "preflight" not in source.lower().replace(
        "project-specific preflight", "")
    assert "OSWorldEvaluator" not in source
    assert "terminal_outcome = verifier_decision.token" in source
    assert "VERIFIER_PASS_CONFIRMATION_STARTED" not in source
    assert "self_evolving_actor_repair_msg" not in source
    assert "verifier_decision.body" not in source
    signature = inspect.signature(e15_loop.e15_evolve)
    assert "evaluator" not in signature.parameters
    assert "score" not in signature.parameters


def test_phase1_budget_counts_complete_projects_and_checkpoints_exact_memory(
        tmp_path):
    final_memory = {
        "office/layout.md": b"reusable verified layout procedure",
        "media/timing.md": b"frame-accurate timing lesson",
    }
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"uncommitted"}),
        memory_step("memory-reconciled", final_memory),
    ])
    harness = FakeHarness(vm, driver)

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=1, checkpoint_projects=(0, 1),
        phase1_exploration=True)

    assert not driver.steps
    assert result.status == "budget_exhausted"
    assert result.projects == 1
    assert e15_loop._read_memory_tree(result.memory_dir) == final_memory
    assert e15_loop._read_memory_tree(str(
        tmp_path / "lineage/checkpoints/project_000/memory")) == {}
    assert e15_loop._read_memory_tree(str(
        tmp_path / "lineage/checkpoints/project_001/memory")) == final_memory
    checkpoints = [
        event for event in harness.events
        if event["event_type"] == "MEMORY_CHECKPOINT_COMMITTED"]
    assert [event["payload"]["project"] for event in checkpoints] == [0, 1]
    assert all(event["state"] == {
        "project_open": False,
        "memory_phase_open": False,
        "recovery_open": False,
    } for event in checkpoints)
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["phase1_exploration"] is True
    assert state["project_budget"] == 1
    assert state["status"] == "budget_exhausted"


def test_phase1_curriculum_can_stop_early_with_saturated(tmp_path):
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        terminal_step("curriculum-saturated", "SATURATED"),
    ])
    harness = FakeHarness(vm, driver)

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=8, checkpoint_projects=(0, 2, 4, 8),
        phase1_exploration=True)

    assert not driver.steps
    assert result.status == "saturated"
    assert result.projects == 0
    assert "DECISION: SATURATED" in result.terminal_text
    assert "TARGET TASK DISTRIBUTION" in driver.calls[0]["prompt"]


def test_phase1_curriculum_inspects_actor_owned_memory_as_disposable_copy(
        tmp_path):
    learned = {"skills/grounded.md": b"Actor-owned grounded experience"}
    terminal = terminal_step("curriculum-saturated", "SATURATED")
    terminal["check"] = lambda vm, call: (
        vm.memory == learned
        and call["memory_before"] == learned
        and "CURRENT ACTOR-OWNED DURABLE MEMORY" in call["prompt"])
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"uncommitted"}),
        memory_step("memory-reconciled", learned),
        terminal,
    ])
    harness = FakeHarness(vm, driver)

    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=8, checkpoint_projects=(0, 2, 4, 8),
        phase1_exploration=True)

    assert not driver.steps
    assert result.status == "saturated"
    assert e15_loop._read_memory_tree(result.memory_dir) == learned


def test_phase1_resumes_only_from_closed_project_with_curriculum_context(
        tmp_path):
    lineage = tmp_path / "lineage"
    memory_one = {"lesson.md": b"verified generation one"}
    vm_one = FakeVM()
    driver_one = ScriptedAgents(vm_one, [
        project_step("curriculum-project-1", PROJECT_1,
                     {"state": "fixtures-1"}),
        actor_step("actor-1", "SUBMIT", {"state": "candidate-1"}),
        verifier_step("verifier-1", "PASS"),
        memory_step("memory-1-draft", {"draft.md": b"draft"}),
        memory_step("memory-1-reconciled", memory_one),
        project_step("curriculum-project-2-lost", PROJECT_2,
                     {"state": "fixtures-2"}),
    ])
    harness_one = FakeHarness(vm_one, driver_one)
    hooks_one = harness_one.hooks()
    ordinary_capture = hooks_one.capture_project
    capture_calls = 0

    def fail_third_capture(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 3:
            raise FileNotFoundError("simulated lossless capture defect")
        return ordinary_capture(*args, **kwargs)

    hooks_one.capture_project = fail_third_capture
    first = e15_loop.e15_evolve(
        vm_one, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=hooks_one, event_sink=harness_one.append_event,
        project_budget=2, checkpoint_projects=(0, 2),
        phase1_exploration=True)

    assert first.status == "infra" and first.projects == 1
    assert e15_loop._read_memory_tree(first.memory_dir) == memory_one
    recovered_history = [
        {"role": "user", "content": "prior persistent curriculum turn"},
        {"role": "assistant", "content": "prior uncaptured project decision"},
    ]
    transcript = lineage / "curriculum/turn_002/segment_000/transcript.json"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({
        "system": "test curriculum system", "messages": recovered_history,
    }), encoding="utf-8")

    memory_two = {"lesson.md": b"verified generations one and two"}
    vm_two = FakeVM()
    driver_two = ScriptedAgents(vm_two, [
        project_step("curriculum-project-2-rebuilt", PROJECT_2,
                     {"state": "fixtures-2-rebuilt"}),
        actor_step("actor-2", "SUBMIT", {"state": "candidate-2"}),
        verifier_step("verifier-2", "PASS"),
        memory_step("memory-2-draft", memory_one),
        memory_step("memory-2-reconciled", memory_two),
    ])
    harness_two = FakeHarness(vm_two, driver_two)
    resumed = e15_loop.e15_evolve(
        vm_two, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness_two.hooks(), event_sink=harness_two.append_event,
        project_budget=2, checkpoint_projects=(0, 2),
        phase1_exploration=True, resume_completed_boundary=True)

    assert resumed.status == "budget_exhausted"
    assert resumed.projects == 2
    assert e15_loop._read_memory_tree(resumed.memory_dir) == memory_two
    first_resumed_call = driver_two.calls[0]
    assert first_resumed_call["history_in"] == recovered_history
    assert first_resumed_call["continue_context"] is True
    assert "HARNESS RECOVERY NOTICE" in first_resumed_call["prompt"]
    assert "not a grade or convergence signal" in first_resumed_call["prompt"]
    assert (lineage / "episodes/ep001/outcome.json").is_file()
    assert (lineage / "episodes/ep002/outcome.json").is_file()
    assert [event["event_type"] for event in harness_two.events[:2]] == [
        "PHASE_RESUMED", "PROJECT_OPENED"]
    assert not any(
        event["event_type"] == "MEMORY_CHECKPOINT_COMMITTED"
        and event["payload"]["project"] == 0
        for event in harness_two.events)


def test_failed_phase1_resume_does_not_overwrite_existing_state(tmp_path):
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    prior_state = {
        "schema_version": 1,
        "status": "infra",
        "projects": 5,
        "last_project": 5,
        # Deliberately incomplete: validation must fail without rewriting it.
        "sentinel": "preserve-me",
    }
    state_path = lineage / "state.json"
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")

    vm = FakeVM()
    harness = FakeHarness(vm, ScriptedAgents(vm, []))
    result = e15_loop.e15_evolve(
        vm, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), project_budget=8,
        checkpoint_projects=(0, 2, 4, 8), phase1_exploration=True,
        resume_completed_boundary=True)

    assert result.status == "infra"
    assert result.projects == 5 and result.last_project == 5
    assert json.loads(state_path.read_text(encoding="utf-8")) == prior_state


def test_phase1_keyboard_interrupt_persists_infra_boundary(tmp_path):
    vm = FakeVM()
    harness = FakeHarness(vm, ScriptedAgents(vm, []))
    hooks = harness.hooks()

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    hooks.run_attempt = interrupt
    lineage = tmp_path / "lineage"
    result = e15_loop.e15_evolve(
        vm, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=hooks, project_budget=8,
        checkpoint_projects=(0, 2, 4, 8), phase1_exploration=True)

    assert result.status == "infra"
    assert result.projects == 0
    state = json.loads((lineage / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "infra"
    assert state["projects"] == 0
    assert state["reason"] == "KeyboardInterrupt: external interruption"


def test_phase1_agentic_verifier_sees_candidate_not_actor_private_evidence(
        tmp_path, monkeypatch):
    final_memory = {"lesson.md": b"grounded lesson"}
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        memory_step("memory-draft", {"draft.md": b"draft"}),
        memory_step("memory-reconciled", final_memory),
    ])
    harness = FakeHarness(vm, driver)
    observed = {}

    def verify_agentic(project, active_vm, control_cfg, **kwargs):
        observed["project"] = project
        observed["tree"] = copy.deepcopy(active_vm.tree)
        observed["memory"] = dict(active_vm.memory)
        observed["execution_evidence"] = dict(
            active_vm.execution_evidence)
        observed["context"] = kwargs["context"]
        assert control_cfg.verifier_stage_lifecycle is False
        assert control_cfg.verifier_hide_actor_memory is True
        return "pass", "independent candidate evidence\nVERDICT: PASS\n"

    monkeypatch.setattr("core.verifier.verify_agentic", verify_agentic)
    control = load_config(str(
        Path(__file__).resolve().parents[1]
        / "config/osworld_v2_glm53_k3_recursive_practice.yaml"))
    result = e15_loop.e15_evolve(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=1, checkpoint_projects=(1,),
        phase1_exploration=True, agentic_verifier_cfg=control)

    assert result.status == "budget_exhausted"
    assert observed["project"] == PROJECT_1
    assert observed["tree"] == {"state": "candidate"}
    assert observed["memory"] == {}
    assert observed["execution_evidence"] == {}
    assert "Actor-private memory, handoff prose, reasoning" in \
        observed["context"]
    assert len(harness.evidence_pushes) == 1
