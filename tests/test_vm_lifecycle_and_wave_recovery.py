"""Stopped-batch regressions: no fabricated grades or lost committed memory."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from explore import phase1_boundary_recovery as recovery
from explore import phase1_wave as wave
from explore.e15_loop import E15BoundaryError, E15InfrastructureError, _manifest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def boundary(tmp_path, count=1):
    root = tmp_path / "run/phase1"
    memory = {"lesson.md": b"preserved learned lesson"} if count else {}
    manifest = _manifest(memory)
    for folder in ("memory", "memory_frozen"):
        (root / folder).mkdir(parents=True)
        for name, data in memory.items():
            (root / folder / name).write_bytes(data)
    state = {"status": "infra", "projects": count, "last_project": count, "waves": count + 1,
             "phase1_parallel_waves": True, "project_budget": 8,
             "checkpoint_projects": [0, 4, 8], "max_parallel": 4,
             "target_query_conditioned": True,
             "target_sha256": hashlib.sha256(b"public instruction").hexdigest(),
             "memory_manifest": manifest}
    for name in ("state.json", "result.json", "manifest.json"):
        write(root / name, state)
    if count:
        write(root / "episodes/ep001/outcome.json", {
            "project_index": 1, "project_id": "p", "terminal_outcome": "FAIL",
            "wave_index": 1, "memory_before": {}, "memory_after": manifest})
        (root / "episodes/ep001/outcome.md").write_text("unaltered FAIL evidence")
        write(root / "waves/wave_001/curriculum_decision.json", {
            "decision": "WAVE", "rationale": "practice needed",
            "projects": [{"id": "p", "instruction": "Create /home/user/evolution_project/output"}]})
        (root / "waves/wave_001/outcomes.md").write_text("complete wave feedback")
        write(root / "curriculum/wave_001/segment_000/transcript.json", {
            "messages": [{"role": "user", "content": "original context"},
                         {"role": "assistant", "content": "original action"}]})
        write(root / "events.jsonl", {"event_type": "PHASE1_WAVE_COMPLETED",
              "payload": {"wave": 1, "total_projects": 1}})
    write(root / "curriculum/wave_002/segment_000/transcript.json", {
        "messages": [{"role": "assistant", "content": "failed draft"}]})
    return root


def validate(root):
    return recovery.validate_boundary(
        root, "public instruction", project_budget=8, checkpoint_projects=[0, 4, 8],
        max_parallel=4, target_query_conditioned=True)


def test_resume_preserves_failed_and_successful_learning_without_resetting_budget(tmp_path):
    root = boundary(tmp_path)
    old_state = (root / "state.json").read_bytes()
    plan = validate(root)
    assert plan["projects"] == 1 and plan["wave"] == 1
    assert plan["history"][0]["content"] == "original context"
    archive = recovery.archive_pending(root, plan)
    assert (archive / "state.json").read_bytes() == old_state
    assert (archive / "curriculum/wave_002/segment_000/transcript.json").is_file()
    assert (root / "memory/lesson.md").read_bytes() == b"preserved learned lesson"
    assert json.loads((root / "episodes/ep001/outcome.json").read_text())["terminal_outcome"] == "FAIL"
    assert json.loads((archive / "receipt.json").read_text())["total_budget_unchanged"]


@pytest.mark.parametrize("fault", ["memory", "partial", "later_phase", "counter"])
def test_resume_rejects_memory_drift_partial_learning_and_later_phases(tmp_path, fault):
    root = boundary(tmp_path)
    if fault == "memory":
        (root / "memory/lesson.md").write_text("uncommitted alteration")
    elif fault == "partial":
        (root / "episodes/ep002/memory_distillation").mkdir(parents=True)
    elif fault == "later_phase":
        (root.parent / "phase2").mkdir()
    else:
        state = json.loads((root / "state.json").read_text())
        state["project_budget"] = 16
        write(root / "state.json", state)
    with pytest.raises(E15InfrastructureError):
        validate(root)
    assert not (root / "boundary_recovery").exists()


def test_zero_project_boot_failure_needs_no_nonexistent_wave_event(tmp_path):
    root = boundary(tmp_path, count=0)
    plan = validate(root)
    assert plan["projects"] == 0 and plan["history"] == []
    recovery.archive_pending(root, plan)


@pytest.mark.parametrize("count", [0, 1])
def test_quarantined_boundary_cannot_resume_after_clean_transcript_audits(
        tmp_path, monkeypatch, count):
    from config.settings import Config
    from explore.e15_loop import E15Hooks

    root = boundary(tmp_path, count=count)
    state = json.loads((root / "state.json").read_text())
    # A rejected guest handoff is audited before it is saved on the host.
    # A clean saved transcript cannot clear that original boundary failure.
    state.update(status="quarantined",
                 reason="Curriculum wave handoff failed the target boundary")
    write(root / "state.json", state)
    preserved = {p.relative_to(root): p.read_bytes()
                 for p in root.rglob("*") if p.is_file()}
    started = []
    hooks = E15Hooks(validate_corpus=lambda *a: None,
                     audit_text=lambda *a, **k: [],
                     audit_transcripts=lambda *a, **k: [])
    decision = wave.WaveDecision(
        "SATURATED", "mock next decision", (), '{"decision":"SATURATED"}')
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    monkeypatch.setattr(wave, "_author_wave", lambda **k: (decision, []))

    def vm_factory():
        started.append(True)
        return SimpleNamespace(close=lambda: None), object()

    with pytest.raises(E15InfrastructureError, match="quarantined"):
        wave.evolve_parallel_phase1(
            root=str(root), target_direction="public instruction",
            actor_cfg=Config(), curriculum_cfg=Config(), memory_cfg=Config(),
            verifier_control_cfg=Config(), corpus_path="unused", hooks=hooks,
            vm_factory=vm_factory, project_budget=8,
            checkpoint_projects=(0, 4, 8), max_parallel=4,
            target_query_conditioned=True, resume_completed_boundary=True)
    assert started == []
    assert not (root / "boundary_recovery").exists()
    assert {p.relative_to(root): p.read_bytes()
            for p in root.rglob("*") if p.is_file()} == preserved




def test_broken_guest_restarts_once_and_archives_unscored_attempt(tmp_path, monkeypatch):
    episode = tmp_path / "ep001"
    write(episode / "fixtures/manifest.json", {"immutable": True})
    calls = []
    expected = object()
    def branch(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            write(episode / "actor/iter_01/trace_meta.json", {"infra_fail": True})
            (episode / "actor/iter_01/trace.txt").write_text("mktemp: Read-only file system")
            raise E15InfrastructureError("ACTOR phase ended at infrastructure status infra")
        assert not (episode / "actor").exists()
        assert (episode / "fixtures/manifest.json").is_file()
        return expected
    monkeypatch.setattr(wave, "_execute_branch", branch)
    assert wave._execute_branch_with_infra_retry(episode_dir=episode) is expected
    assert len(calls) == 2
    receipt = json.loads((episode / "infra_attempts/attempt_001/retry_receipt.json").read_text())
    assert receipt["prior_memory_promoted"] is False


@pytest.mark.parametrize("committed", [False, True])
def test_boundary_and_committed_verdict_are_never_retried(tmp_path, monkeypatch, committed):
    episode = tmp_path / "ep001"
    if committed:
        path = episode / "handoffs/verifier_001.md"
        path.parent.mkdir(parents=True)
        path.write_text("VERDICT: FAIL")
    def branch(**kwargs):
        raise TimeoutError("physical fault") if committed else E15BoundaryError("boundary")
    monkeypatch.setattr(wave, "_execute_branch", branch)
    with pytest.raises((TimeoutError, E15BoundaryError)):
        wave._execute_branch_with_infra_retry(episode_dir=episode)
    assert not (episode / "infra_attempts").exists()




def test_parallel_failure_drains_and_closes_successful_sibling(tmp_path, monkeypatch):
    import time
    from explore.e15_loop import E15Hooks
    from config.settings import Config
    closed = []
    projects = tuple(wave.WaveProject(str(i), "public instruction") for i in (1, 2))
    decision = wave.WaveDecision("WAVE", "agent rationale", projects, "{}")
    monkeypatch.setattr(wave, "_author_wave", lambda **kw: (decision, []))
    monkeypatch.setattr(wave, "_capture_wave_fixtures", lambda **kw: None)
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    def branch(**kw):
        if kw["project_index"] == 1:
            raise E15InfrastructureError("failed sibling")
        time.sleep(0.02)
        return SimpleNamespace(project_index=2, project=projects[1], terminal_outcome="FAIL",
                               desktop=SimpleNamespace(close=lambda: closed.append(2)))
    monkeypatch.setattr(wave, "_execute_branch_with_infra_retry", branch)
    hooks = E15Hooks(validate_corpus=lambda *a: None, audit_text=lambda *a, **k: [])
    result = wave.evolve_parallel_phase1(
        root=str(tmp_path / "lineage"), target_direction="public instruction",
        actor_cfg=Config(), curriculum_cfg=Config(), memory_cfg=Config(),
        verifier_control_cfg=Config(), corpus_path="unused", hooks=hooks,
        vm_factory=lambda: (SimpleNamespace(close=lambda: closed.append("curriculum")), object()),
        project_budget=8, max_parallel=2)
    assert result.status == "infra" and result.projects == 0
    assert closed == ["curriculum", 2]




def test_boot_adapter_caches_only_one_allocation_and_extends_waits(monkeypatch):
    from contextlib import nullcontext
    import qemu_provider as adapter
    queried, waits = [], []
    class Provider:
        container = None
        def _get_used_ports(self):
            queried.append(True)
            return {5000}
        def _wait_for_vm_ready(self, timeout=300):
            waits.append(timeout)
        def start_emulator(self):
            for _ in range(4):
                assert self._get_used_ports() == {5000}
            self._wait_for_vm_ready()
    module = SimpleNamespace(DockerProvider=Provider, LOCK_TIMEOUT=10)
    monkeypatch.setattr(adapter, "_boot_slot", nullcontext)
    adapter._prepare_boot_lifecycle(module)
    adapter._prepare_boot_lifecycle(module)
    p = Provider()
    p.start_emulator()
    p.start_emulator()
    assert len(queried) == 2 and waits == [600, 600]
    assert module.LOCK_TIMEOUT == 300
    p._get_used_ports()
    assert len(queried) == 3


def test_guest_cleanup_removes_its_anonymous_volume_even_when_stop_times_out(monkeypatch):
    from contextlib import nullcontext
    import qemu_provider as adapter
    calls = []
    class Container:
        def stop(self, timeout):
            raise TimeoutError("daemon stop timed out")
        def remove(self, **kwargs):
            calls.append(kwargs)
    class Provider:
        container = Container()
        def start_emulator(self): pass
        def stop_emulator(self): pytest.fail("unpatched stop")
        def _wait_for_vm_ready(self, timeout=300): pass
        def _get_used_ports(self): return set()
    monkeypatch.setattr(adapter, "_boot_slot", nullcontext)
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    p = Provider()
    p.stop_emulator()
    assert calls == [{"force": True, "v": True}]
    assert p.container is None and p.server_port is None


@pytest.mark.parametrize("failures,owner,error,expected", [
    (1, "example-study/worker-a/phase1", TimeoutError, 2),
    (2, "example-study/worker-a/phase1", TimeoutError, 2),
    (1, "", TimeoutError, 1),
    (1, "example-study/worker-a/phase1", RuntimeError, 1),
])
def test_cold_readiness_retry_is_bounded_and_before_any_task_setup(
        tmp_path, monkeypatch, failures, owner, error, expected):
    from contextlib import contextmanager
    import os
    import qemu_provider as adapter
    monkeypatch.setenv("FORGE_VM_OWNER", owner)
    monkeypatch.setattr(adapter, "__file__", str(tmp_path / "qemu_provider.py"))
    calls, removed, slots = [], [], []
    @contextmanager
    def slot():
        slots.append("acquire")
        try: yield
        finally: slots.append("release")
    class Container:
        def __init__(self, number):
            self.id = str(number)
            self.labels = {"forge.owner": owner, "forge.host_pid": str(os.getpid()),
                           "forge.launch_token": str(number)}
            self.attrs = {"State": {"Status": "running"}}
        def reload(self): pass
        def logs(self, **kwargs): return b"cold boot diagnostics"
        def remove(self, **kwargs): removed.append((self.id, kwargs))
    class Provider:
        container = None
        def _get_used_ports(self): return set()
        def _wait_for_vm_ready(self, timeout=300):
            if len(calls) <= failures:
                raise error("VM failed to become ready within timeout period")
        def start_emulator(self):
            calls.append(True)
            self.container = Container(len(calls))
            self._wait_for_vm_ready()
            return "ready for setup"
    monkeypatch.setattr(adapter, "_boot_slot", slot)
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    provider = Provider()
    if failures == 1 and expected == 2:
        assert provider.start_emulator() == "ready for setup"
        assert provider.container.id == "2"
    else:
        with pytest.raises(error): provider.start_emulator()
    assert len(calls) == expected
    assert slots == ["acquire", "release"] * expected
    assert all(opts == {"force": True, "v": True} for _, opts in removed)
    receipts = list(tmp_path.rglob("receipt.json"))
    if not owner or error is RuntimeError:
        assert not receipts
    else:
        assert len(receipts) == min(failures, 2)
        for path in receipts:
            receipt = json.loads(path.read_text())
            assert receipt["removed_with_anonymous_volume"]
            assert path.with_name("container.log").read_bytes() == b"cold boot diagnostics"


def test_failed_boot_cannot_remove_a_foreign_owner(tmp_path, monkeypatch):
    import qemu_provider as adapter
    from env.qemu_rollback import QemuRollbackError
    class Container:
        labels = {"forge.owner": "unrelated"}
        def reload(self): pass
        def remove(self, **kwargs): pytest.fail("foreign VM removed")
    with pytest.raises(QemuRollbackError, match="ownership mismatch"):
        adapter._archive_failed_boot(SimpleNamespace(container=Container()),
            "example-study/worker-a/phase1", 600)


@pytest.mark.parametrize("failure,failures,expected_calls,expected_status", [
    ("failed to create inotify: Too many open files", 2, 3, 0),
    ("failed to create inotify: Too many open files", 40, 30, 3),
    ("bad command line options", 40, 1, 3),
])
def test_dnsmasq_retries_only_transient_inotify_start_failure(
        tmp_path, failure, failures, expected_calls, expected_status):
    import os
    import subprocess
    fake = tmp_path / "dnsmasq"
    fake.write_text('''#!/bin/bash
n=$(cat "$PROBE_COUNT" 2>/dev/null || echo 0)
n=$((n+1))
echo "$n" > "$PROBE_COUNT"
printf '%s\\n' "$@" > "$PROBE_ARGS"
if [ "$n" -le "$PROBE_FAILURES" ]; then
    echo "dnsmasq: $PROBE_FAILURE" >&2
    exit 3
fi
''')
    fake.chmod(0o755)
    source = Path(__file__).resolve().parents[1] / "tools/dnsmasq_start_retry.sh"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(source.read_text().replace('/usr/sbin/dnsmasq', str(fake))
                       .replace('sleep 2', 'sleep 0'))
    args = ['--dhcp-option=option:router,20.20.20.1', '--host-record=two words']
    result = subprocess.run(['bash', str(wrapper), *args], text=True, capture_output=True,
        env={**os.environ, 'PROBE_COUNT':str(tmp_path/'count'), 'PROBE_ARGS':str(tmp_path/'args'),
             'PROBE_FAILURE':failure, 'PROBE_FAILURES':str(failures)}, timeout=15)
    assert result.returncode == expected_status
    assert int((tmp_path/'count').read_text()) == expected_calls
    assert (tmp_path/'args').read_text().splitlines() == args
    assert failure in result.stderr  # Original errors remain visible.


def test_dnsmasq_start_retry_requires_explicit_opt_in(monkeypatch):
    from qemu_provider import _LoopbackContainers
    calls = []
    collection = SimpleNamespace(run=lambda *a, **kw: calls.append(kw))
    monkeypatch.delenv('FORGE_DNSMASQ_START_RETRY', raising=False)
    monkeypatch.setenv('FORGE_VM_OWNER', 'unrelated_experiment')
    _LoopbackContainers(collection).run('image', environment={'KEEP':'unchanged'}, volumes={})
    assert calls[-1]['environment'] == {'KEEP':'unchanged'} and calls[-1]['volumes'] == {}
    monkeypatch.setenv('FORGE_DNSMASQ_START_RETRY', '1')
    monkeypatch.setenv('FORGE_VM_OWNER', 'example-study/worker-b/phase1')
    _LoopbackContainers(collection).run('image', environment={'KEEP':'unchanged'},
        volumes={'system':{'bind':'/System.qcow2','mode':'ro'}})
    assert calls[-1]['environment'] == {'KEEP':'unchanged', 'DNSMASQ':'/run/forge-dnsmasq-start'}
    assert calls[-1]['volumes']['system'] == {'bind':'/System.qcow2','mode':'ro'}
    mounts = [v for v in calls[-1]['volumes'].values() if v['bind']=='/run/forge-dnsmasq-start']
    assert mounts == [{'bind':'/run/forge-dnsmasq-start','mode':'ro'}]


def test_failed_container_removal_retains_handle_for_cleanup_retry(monkeypatch):
    import qemu_provider as adapter
    class Container:
        def stop(self, timeout): pass
        def remove(self, **kwargs): raise ConnectionError("daemon unavailable")
    class Provider:
        container = Container()
        def start_emulator(self): pass
        def stop_emulator(self): pass
        def _wait_for_vm_ready(self, timeout=300): pass
        def _get_used_ports(self): return set()
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    provider = Provider()
    owned = provider.container
    with pytest.raises(ConnectionError):
        provider.stop_emulator()
    assert provider.container is owned
