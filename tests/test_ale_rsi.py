"""ALE phase transitions, memory boundaries, and bounded wave VM ownership."""
import asyncio
import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

import pytest

from benchmarks.ale.protocol import protocol, memory_record, verify_memory


def test_public_batch_covers_each_pinned_task_once():
    from benchmarks.ale.protocol import select_tasks
    lock = protocol()
    assert len(lock['tasks']) == len(set(lock['tasks'])) == 67
    assert select_tasks(lock) == lock['tasks']
    assert not {'assignments', 'existing_baseline_tasks'} & lock.keys()


def test_frozen_memory_detects_changes_and_symlinks(tmp_path):
    bank = tmp_path / 'memory'; bank.mkdir()
    (bank / 'lesson.md').write_bytes(b'original')
    record = memory_record(bank)
    verify_memory(record)
    (bank / 'lesson.md').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='Frozen memory changed'):
        verify_memory(record)
    (bank / 'lesson.md').unlink()
    (bank / 'lesson.md').symlink_to(tmp_path / 'outside')
    with pytest.raises(RuntimeError, match='symlink'):
        memory_record(bank)


def test_windows_host_paths_leave_native_task_paths_intact():
    from benchmarks.ale.windows import WindowsVM
    vm = object.__new__(WindowsVM); vm.home = 'C:/Users/User'
    assert vm.host_path('/home/user/.memory/ü.md') == 'C:/Users/User/.memory/ü.md'
    assert vm.host_path('/tmp/proj.tgz') == 'C:/Users/User/.ale/tmp/proj.tgz'
    assert vm.host_path(vm.host_path('/tmp/proj.tgz')) == 'C:/Users/User/.ale/tmp/proj.tgz'
    assert vm.host_path('E:/agenthle/task/input.csv') == 'E:/agenthle/task/input.csv'


def test_audit_authorizes_target_but_rejects_other_task_and_private_source(tmp_path):
    from explore.commit import _shingles
    from benchmarks.ale.practice import Audit
    target = 'Create one spreadsheet that sums all rows from the supplied financial table.'
    other = 'Segment every individual cell in the provided microscope image series.'
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps(sorted(_shingles(target) | _shingles(other))))
    audit = Audit(corpus)
    assert audit.text(target, authorized_instruction=target) == []
    assert audit.text(other, authorized_instruction=target)
    assert audit.text('https://github.com/rdi-berkeley/agents-last-exam/tree/main/tasks')


def test_parked_wave_can_exceed_vm_capacity_without_losing_actor_context(tmp_path, monkeypatch):
    from explore import phase1_wave as wave
    from explore.practice_loop import PracticeHooks, _atomic_install_memory, _manifest, _read_memory_tree
    slots = queue.Queue(); slots.put(0); slots.put(1)
    live = set(); guard = threading.Lock(); peak = 0
    histories = {}; committed = []; replayed = []
    def factory():
        nonlocal peak
        slot = slots.get(timeout=2)
        with guard:
            assert slot not in live
            live.add(slot); peak = max(peak, len(live))
        def close():
            with guard:
                live.remove(slot)
            slots.put(slot)
        return SimpleNamespace(close=close), SimpleNamespace(slot=slot)
    decision = wave.WaveDecision('WAVE', 'independent practice', tuple(
        wave.WaveProject(f'p{i}', '/home/user/evolution_project') for i in range(5)), '{}')
    monkeypatch.setattr(wave, '_author_wave', lambda **kw: (decision, []))
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *args: None)
    monkeypatch.setattr(wave, '_capture_wave_fixtures', lambda **kw: None)
    monkeypatch.setattr(wave, '_replay', lambda hooks, vm, path: replayed.append(path))
    def execute(**kw):
        desktop, vm = kw['vm_factory']()
        i = kw['project_index']
        history = [{'role': 'assistant', 'content': f'branch-{i}-private-context'}]
        histories[i] = history
        return wave._BranchRuntime(i, kw['project'], kw['episode_dir'], desktop, vm,
            history, 'STATUS: SUBMIT', 'VERDICT: PASS', 'PASS', {})
    monkeypatch.setattr(wave, '_execute_branch', execute)
    def commit(**kw):
        branch = kw['branch']
        assert branch.actor_history is histories[branch.project_index]
        assert branch.vm is not None and branch.desktop is not None
        before = _read_memory_tree(str(kw['canonical_memory_dir']))
        after = {**before, f'{branch.project_index}.md': b'Actor-owned lesson'}
        _atomic_install_memory(str(kw['canonical_memory_dir']), after)
        committed.append(branch.project_index)
        return {'before': _manifest(before), 'after': _manifest(after), 'changes': {}}
    monkeypatch.setattr(wave, '_commit_branch_memory', commit)
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction='public target',
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=factory, corpus_path='unused', project_budget=1, max_parallel=2,
        hooks=PracticeHooks(validate_corpus=lambda p: {}, audit_text=lambda *a, **k: []),
        park_verified_branches=True)
    assert result.status == 'budget_exhausted' and result.projects == 5
    assert committed == [1, 2, 3, 4, 5]
    assert len(replayed) == 5 and peak <= 2 and not live
    assert len(_read_memory_tree(result.memory_dir)) == 5


def test_ale_phase2_preserves_actor_learning_and_fresh_verifier_sessions(tmp_path, monkeypatch):
    from benchmarks.ale import learning
    from core import loop
    from explore import target_learning, unified_evolution
    from explore.practice_loop import PracticeHooks, _atomic_install_memory, _read_memory_tree
    initial = tmp_path / 'initial'
    _atomic_install_memory(str(initial), {'seed.md': b'phase1'})
    work = tmp_path / 'phase2'; work.mkdir()
    pools = []; session_ids = []; history_ids = []; learned_ids = []; reviews = []
    class Pool:
        def __init__(self, sandboxes, practice, reset_endpoint):
            self.practice = practice; self.acquisitions = 0; self.closed = False
            pools.append(self)
        def acquire(self):
            self.acquisitions += 1
            return SimpleNamespace(close=lambda: None), SimpleNamespace(visible_roots=())
        def close(self): self.closed = True
    monkeypatch.setattr(learning, 'CleanPool', Pool)
    monkeypatch.setattr(learning, 'hooks_for', lambda *a: PracticeHooks(push_memory=lambda *a: True))
    def actor(direction, vm, cfg, sink, **kw):
        session_ids.append(kw['verifier_sessions'])
        history = [{'role': 'user', 'content': 'task'}, {'role': 'assistant', 'content': 'candidate'}]
        history_ids.append(history)
        kw['runtime_state']['active_history'] = history
        if len(history_ids) == 1:
            return loop.LoopResult(status='evolve', verifier_route='EVOLVE', verifier_report='VERDICT: FAIL\nMissing rows'), history
        return loop.LoopResult(status='done', verifier_route='HANDOFF', verifier_report='VERDICT: PASS\nAll rows checked'), history
    monkeypatch.setattr(loop, 'run_with_resume', actor)
    def learn(**kw):
        learned_ids.append(kw['actor_history'])
        memory = {**kw['before_memory'], f'learn{len(learned_ids)}.md': kw['terminal_outcome'].encode()}
        _atomic_install_memory(str(kw['memory_dir']), memory)
        return memory, 'Improve row coverage', kw['actor_history']
    monkeypatch.setattr(target_learning, '_promote_learning', learn)
    def evolve(vm, root, direction, report, diagnosis, memory, *configs, **kw):
        reviews.append((kw['triggering_outcome'], kw['session']))
        destination = Path(root) / 'memory'
        _atomic_install_memory(str(destination), memory)
        return SimpleNamespace(status='ready_for_retry', memory_dir=str(destination),
            projects=1 if len(reviews) == 1 else 0, reason='Ready')
    monkeypatch.setattr(unified_evolution, 'evolve_until_ready', evolve)
    result = learning.phase2({'work_dir': str(work), 'input_memory': memory_record(initial),
        'sandbox': {}, 'practice_sandboxes': [{}], 'corpus': 'unused', 'instruction': 'public task', 'reset_endpoint': 'fake'})
    assert result['transition_ready'] and result['official_evaluator_calls'] == 0
    assert result['target_cycles'] == 2 and result['learning_updates'] == 2
    assert session_ids[0] is not session_ids[1]
    assert learned_ids[0] is history_ids[0] and learned_ids[1] is history_ids[1]
    assert reviews[0][0] == 'FAIL' and reviews[1][0] == 'PASS'
    assert reviews[0][1] is reviews[1][1]
    assert _read_memory_tree(str(initial)) == {'seed.md': b'phase1'}
    assert len(_read_memory_tree(result['memory']['path'])) == 3
    assert all(p.closed for p in pools)


def test_report_rejects_two_rsi_scores_for_the_same_task(tmp_path):
    from benchmarks.ale.report import collect
    task = protocol()['tasks'][0]
    for run, score in [('one', 0.25), ('two', 0.75)]:
        path = tmp_path / run / task / 'phase3/result.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'score': score}))
    with pytest.raises(RuntimeError, match='Duplicate rsi'):
        collect([tmp_path / 'one', tmp_path / 'two'])


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -0.1, 1.1, True])
def test_report_rejects_invalid_official_scores(score):
    from benchmarks.ale.report import valid_score
    with pytest.raises(ValueError, match='finite number'):
        valid_score(score)


def test_frozen_source_is_complete_and_omits_credentials(tmp_path):
    from benchmarks.ale.host import freeze_source
    from benchmarks.ale.protocol import source_fingerprint
    release = tmp_path / 'release'
    freeze_source(release)
    assert not (release / '.env').exists()
    assert source_fingerprint(release) == source_fingerprint()
    (release / 'run_ale.py').write_text('changed launcher')
    assert source_fingerprint(release) != source_fingerprint()


def test_reset_service_handles_independent_guests_and_rejects_foreign_ids():
    from benchmarks.ale.lifecycle import ResetService, request_reset
    async def exercise():
        owned = {'first', 'second'}
        async def reset(sandbox_id):
            if sandbox_id not in owned:
                raise RuntimeError('Not an owned guest')
            owned.remove(sandbox_id)
            await asyncio.sleep(0.01)
            return {'id': sandbox_id + '-fresh'}
        service = ResetService(reset)
        try:
            replies = await asyncio.gather(*(asyncio.to_thread(request_reset, service.path, name)
                                              for name in ('first', 'second')))
            assert replies == [{'id': 'first-fresh'}, {'id': 'second-fresh'}]
            with pytest.raises(RuntimeError, match='Not an owned guest'):
                await asyncio.to_thread(request_reset, service.path, 'foreign')
        finally:
            await service.close()
        assert not Path(service.path).exists()
    asyncio.run(exercise())


def test_capacity_counts_attached_container_names_once(tmp_path, monkeypatch):
    import docker
    import shutil
    from benchmarks.ale.capacity import Capacity
    guest = SimpleNamespace(id='full-container-id', name='ale-qemu-example', attrs={})
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda: [guest], get=lambda name: guest),
                             close=lambda: None)
    monkeypatch.setattr(docker, 'from_env', lambda: client)
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024**4))
    read_text = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        'MemAvailable: 104857600 kB\n' if str(path) == '/proc/meminfo'
        else read_text(path, *a, **kw))
    async def exercise():
        first = Capacity(tmp_path / 'pool', tmp_path, limit=2)
        second = Capacity(tmp_path / 'pool', tmp_path, limit=2)
        stop = asyncio.Event()
        try:
            await asyncio.wait_for(first.acquire(1, 0, stop), timeout=1)
            first.attach(guest.name)
            await asyncio.wait_for(second.acquire(1, 0, stop), timeout=1)
            leases = [json.loads(p.read_text()) for p in (tmp_path / 'pool').glob('*.json')]
            assert len(leases) == 2
            assert any(row['containers'] == [guest.id] for row in leases)
        finally:
            first.release(); second.release()
    asyncio.run(exercise())


def test_audit_decodes_multiline_transcript_strings(tmp_path):
    from benchmarks.ale.practice import Audit
    from explore.commit import _shingles
    other = 'Segment every individual cell in the provided microscope image series.'
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps(sorted(_shingles(other))))
    (tmp_path / 'transcript.json').write_text(json.dumps([
        {'role': 'assistant', 'content': other.replace(' ', '\n')}]))
    assert Audit(corpus).transcripts(tmp_path)


def test_host_memory_and_instruction_protocol_match_rsiagent(tmp_path):
    from benchmarks.ale.protocol import instruction_shingles
    from explore.practice_loop import _manifest
    from explore.target_learning import _memory_tree_sha256
    from explore.commit import _shingles
    memory = {'ü.md': b'hello', 'binary.bin': bytes(range(256))}
    for name, content in memory.items():
        (tmp_path / name).write_bytes(content)
    record = memory_record(tmp_path)
    assert record['sha256'] == _memory_tree_sha256(memory)
    assert record['files'] == _manifest(memory)
    instruction = 'Create a table with at least ten rows, then save the UTF-8 output.'
    assert instruction_shingles(instruction) == _shingles(instruction)
