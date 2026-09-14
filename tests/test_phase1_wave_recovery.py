"""Recovery must preserve grades and stop before memory on incomplete waves."""
import json
import sys
import types
from types import SimpleNamespace

import pytest

from tools import recover_phase1_wave as recovery


@pytest.mark.parametrize('verdict', ['PASS', 'FAIL'])
def test_both_terminal_verdicts_reuse_the_saved_candidate(tmp_path, verdict):
    candidate = tmp_path / 'candidates/cycle_001/materials.tgz'
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b'archive validated separately')
    handoffs = tmp_path / 'handoffs'
    handoffs.mkdir()
    (handoffs / 'verifier_001.md').write_text(f'VERDICT: {verdict}\nComplete evidence report.')
    assert recovery.classify_branch(tmp_path) == 'reuse_verdict'


def test_capture_retry_is_rejected_after_any_verifier_evidence(tmp_path):
    (tmp_path / 'verifier').mkdir()
    with pytest.raises(RuntimeError, match='Missing candidate has Verifier evidence'):
        recovery.classify_branch(tmp_path)


def test_resume_requires_verifier_failure_before_machine_execution(tmp_path):
    candidate = tmp_path / 'candidates/cycle_001/materials.tgz'
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b'archive validated separately')
    trace = tmp_path / 'verifier/iter_01/trace_meta.json'
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps({'infra_fail': True, 'secs': 0}))
    assert recovery.classify_branch(tmp_path) == 'resume_verifier'
    trace.write_text(json.dumps({'infra_fail': False, 'secs': 0.1}))
    with pytest.raises(RuntimeError, match='machine effects are unarchived'):
        recovery.classify_branch(tmp_path)


def test_partially_learned_branch_cannot_be_replayed(tmp_path):
    (tmp_path / 'memory_distillation').mkdir()
    with pytest.raises(RuntimeError, match='Memory phase already began'):
        recovery.classify_branch(tmp_path)


def test_a_second_recovery_preserves_the_first_failure_archive(tmp_path):
    first = tmp_path / 'infrastructure_recovery_001'
    first.mkdir()
    with pytest.raises(RuntimeError, match='still open'):
        recovery.next_recovery_dir(tmp_path)
    failure = '{"status": "infra", "reason": "candidate replay failed"}\n'
    (first / 'failure.json').write_text(failure)
    assert recovery.next_recovery_dir(tmp_path) == tmp_path / 'infrastructure_recovery_002'
    assert (first / 'failure.json').read_text() == failure
    (first / 'completion.json').write_text('{"status": "complete"}')
    with pytest.raises(RuntimeError, match='already completed'):
        recovery.next_recovery_dir(tmp_path)


def test_wave_failure_keeps_original_result_and_closes_restored_vms(tmp_path, monkeypatch):
    from explore import phase1_wave as wave
    original = json.dumps({'status': 'infra', 'projects': 6})
    for name in ('state.json', 'result.json', 'manifest.json'):
        (tmp_path / name).write_text(original)
    module = types.ModuleType('desktop_env.desktop_env')
    module.DesktopEnv = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    closed, committed = [], []
    def restore(context, item, factory, hooks):
        if item[0] == 8:
            raise RuntimeError('verification is incomplete')
        return SimpleNamespace(project_index=7, terminal_outcome='PASS',
                               desktop=SimpleNamespace(close=lambda: closed.append(7)))
    monkeypatch.setattr(recovery, 'restore_branch', restore)
    monkeypatch.setattr(wave, '_commit_branch_memory', lambda **kwargs: committed.append(True))
    context = SimpleNamespace(root=tmp_path, runner=SimpleNamespace(_append_event=lambda *args, **kwargs: None),
                              configs={'verifier_control': SimpleNamespace(verifier_execution_mode='effect_isolated')},
                              plan={'wave': 2, 'branches': []}, assignments=[(7,), (8,)],
                              protocol={'phase1': {'parallelism': 2}})
    with pytest.raises(RuntimeError, match='verification is incomplete'):
        recovery.execute(context)
    assert committed == []
    assert closed == [7]
    assert (tmp_path / 'result.json').read_text() == original
    assert (tmp_path / 'infrastructure_recovery_001/before_result.json').read_text() == original
    assert (tmp_path / 'infrastructure_recovery_001/failure.json').is_file()


@pytest.mark.parametrize('budget, expected_status', [(8, 'budget_exhausted'), (10, 'infra')])
def test_completion_waits_for_whole_wave_then_freezes_actor_written_memory(
        tmp_path, monkeypatch, budget, expected_status):
    from explore import phase1_wave as wave
    from explore.e15_loop import _manifest, _read_memory_tree
    original = {'status': 'infra', 'projects': 6, 'official_evaluator_calls': 0}
    for name in ('state.json', 'result.json', 'manifest.json'):
        (tmp_path / name).write_text(json.dumps(original))
    memory = tmp_path / 'memory'
    memory.mkdir()
    (memory / 'prior.md').write_text('six completed projects')
    for index in range(1, 7):
        ep = tmp_path / 'episodes' / f'ep{index:03d}'
        ep.mkdir(parents=True)
        (ep / 'outcome.json').write_text('{}')
    module = types.ModuleType('desktop_env.desktop_env')
    module.DesktopEnv = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ready, commits = [], []
    assignments = []
    for index, verdict in ((7, 'PASS'), (8, 'FAIL')):
        project = SimpleNamespace(project_id=f'p{index}', instruction='original project')
        episode = tmp_path / 'episodes' / f'ep{index:03d}'
        assignments.append((index, project, episode, 'reuse_verdict'))
    def restore(context, item, factory, hooks):
        index, project, episode, _ = item
        ready.append(index)
        return SimpleNamespace(project_index=index, project=project, episode_dir=episode,
                               terminal_outcome='PASS' if index == 7 else 'FAIL',
                               actor_handoff='SUBMIT', verifier_report='saved report',
                               wave_memory={'prior.md': b'six completed projects'},
                               desktop=SimpleNamespace(close=lambda: None))
    def actor_commit(**kwargs):
        assert sorted(ready) == [7, 8]
        branch = kwargs['branch']
        commits.append((branch.project_index, branch.terminal_outcome))
        before = _manifest(_read_memory_tree(str(memory)))
        # This is the stubbed continuing Actor's output, including FAIL learning.
        (memory / f'actor_{branch.project_index}.md').write_text(branch.terminal_outcome)
        after = _manifest(_read_memory_tree(str(memory)))
        return {'before': before, 'after': after, 'changes': {}}
    monkeypatch.setattr(recovery, 'restore_branch', restore)
    monkeypatch.setattr(wave, '_commit_branch_memory', actor_commit)
    monkeypatch.setattr(wave, '_outcome_text', lambda record: record['terminal_outcome'])
    context = SimpleNamespace(
        root=tmp_path, runner=SimpleNamespace(_append_event=lambda *args, **kwargs: None,
                                            DEFAULT_CORPUS=tmp_path / 'corpus'),
        configs={'verifier_control': SimpleNamespace(verifier_execution_mode='effect_isolated'),
                 'memory_actor': None},
        state=original, result=original, target='original direction',
        decision=SimpleNamespace(rationale='original rationale'), wave_dir=tmp_path / 'wave',
        plan={'wave': 2, 'branches': [], 'complete_wave_total': 8, 'budget': budget}, assignments=assignments,
        protocol={'phase1': {'parallelism': 2, 'checkpoints': [8]}})
    recovery.execute(context)
    assert commits == [(7, 'PASS'), (8, 'FAIL')]
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['status'] == expected_status and result['projects'] == 8
    assert result['official_evaluator_calls'] == 0
    assert _read_memory_tree(str(tmp_path / 'memory_frozen')) == _read_memory_tree(str(memory))
    assert (tmp_path / 'memory_frozen/actor_8.md').read_text() == 'FAIL'


def completed_wave(tmp_path):
    import hashlib
    from explore.e15_loop import _manifest
    memory = tmp_path / 'memory'
    memory.mkdir()
    (memory / 'actor.md').write_text('Actor learned after a valid FAIL')
    manifest = _manifest({'actor.md': b'Actor learned after a valid FAIL'})
    frozen = tmp_path / 'memory_frozen'
    frozen.mkdir()
    (frozen / 'actor.md').write_bytes((memory / 'actor.md').read_bytes())
    episode = tmp_path / 'episodes/ep001'
    episode.mkdir(parents=True)
    (episode / 'outcome.json').write_text(json.dumps({
        'project_index': 1, 'wave_index': 1, 'project_id': 'one',
        'terminal_outcome': 'FAIL', 'memory_before': {}, 'memory_after': manifest}))
    (episode / 'outcome.md').write_text('Original FAIL evidence')
    (tmp_path / 'events.jsonl').write_text(json.dumps({
        'event_type': 'PHASE1_WAVE_COMPLETED', 'payload': {'wave': 1, 'total_projects': 1}}) + '\n')
    wave_dir = tmp_path / 'waves/wave_001'
    wave_dir.mkdir(parents=True)
    (wave_dir / 'curriculum_decision.json').write_text(json.dumps({
        'decision': 'WAVE', 'rationale': 'practice needed',
        'projects': [{'id': 'one', 'instruction': 'Create /home/user/evolution_project/output'}]}))
    (wave_dir / 'outcomes.md').write_text('Original FAIL and Actor memory changes')
    history = [{'role': 'user', 'content': 'Original direction'},
               {'role': 'assistant', 'content': 'Original completed wave handoff'}]
    transcript = tmp_path / 'curriculum/wave_001/segment_009/transcript.json'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'messages': history}))
    state = {'status': 'infra', 'projects': 1, 'last_project': 1, 'waves': 2,
             'phase1_parallel_waves': True, 'project_budget': 8,
             'max_parallel': 4, 'checkpoint_projects': [0, 4, 8],
             'target_query_conditioned': True, 'memory_manifest': manifest,
             'target_sha256': hashlib.sha256(b'original target').hexdigest()}
    (tmp_path / 'state.json').write_text(json.dumps(state))
    return history


def resume_kwargs(root):
    return dict(root=str(root), target_direction='original target', actor_cfg=None,
                curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
                corpus_path='unused', project_budget=8, checkpoint_projects=(0, 4, 8),
                max_parallel=4, target_query_conditioned=True, resume_completed_boundary=True)


def test_resume_keeps_memory_curriculum_and_next_wave_number(tmp_path, monkeypatch):
    from explore import phase1_wave as wave
    from explore.e15_loop import E15Hooks
    history = completed_wave(tmp_path)
    checkpoint = tmp_path / 'checkpoints/project_000/memory/initial'
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('Preserve original checkpoint zero')
    calls, events, closed = [], [], []
    def author(**kwargs):
        calls.append(kwargs)
        return wave.parse_wave_handoff(json.dumps({
            'decision': 'SATURATED', 'rationale': 'agent assessment', 'projects': []})), history
    monkeypatch.setattr(wave, '_author_wave', author)
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *args: None)
    result = wave.evolve_parallel_phase1(
        **resume_kwargs(tmp_path), hooks=E15Hooks(validate_corpus=lambda _: None,
                                               audit_text=lambda *args, **kwargs: []),
        vm_factory=lambda: (SimpleNamespace(close=lambda: closed.append(True)), object()),
        event_sink=lambda name, **kw: events.append(name))
    assert result.status == 'saturated' and result.projects == 1
    assert calls[0]['history'] == history
    assert calls[0]['previous_wave_outcomes'] == 'Original FAIL and Actor memory changes'
    assert calls[0]['sink_root'].name == 'wave_002'
    assert checkpoint.read_text() == 'Preserve original checkpoint zero'
    assert (tmp_path / 'memory/actor.md').read_text() == 'Actor learned after a valid FAIL'
    assert 'PHASE1_WAVE_BOUNDARY_RESUMED' in events and closed == [True]


@pytest.mark.parametrize('damage', [
    'memory', 'partial_learning', 'curriculum_symlink', 'quarantine', 'budget', 'ledger',
    'wave_counter', 'incomplete_history', 'history_roles', 'invalid_assignment'])
def test_invalid_wave_resume_never_overwrites_preserved_state(tmp_path, damage):
    from explore import phase1_wave as wave
    from explore.e15_loop import E15InfrastructureError
    completed_wave(tmp_path)
    if damage == 'memory':
        (tmp_path / 'memory/actor.md').write_text('drift')
    elif damage == 'partial_learning':
        (tmp_path / 'episodes/ep002/memory_distillation').mkdir(parents=True)
    elif damage == 'curriculum_symlink':
        (tmp_path / 'curriculum/wave_002').symlink_to(tmp_path, target_is_directory=True)
    elif damage in {'quarantine', 'budget', 'wave_counter'}:
        state = json.loads((tmp_path / 'state.json').read_text())
        key, value = {'quarantine': ('status', 'quarantined'),
                      'budget': ('project_budget', 9), 'wave_counter': ('waves', 9)}[damage]
        state[key] = value
        (tmp_path / 'state.json').write_text(json.dumps(state))
    elif damage in {'incomplete_history', 'history_roles'}:
        path = tmp_path / 'curriculum/wave_001/segment_009/transcript.json'
        transcript = json.loads(path.read_text())
        if damage == 'incomplete_history':
            transcript['messages'].pop()
        else:
            transcript['messages'][0]['role'] = 'assistant'
        path.write_text(json.dumps(transcript))
    elif damage == 'invalid_assignment':
        path = tmp_path / 'waves/wave_001/curriculum_decision.json'
        decision = json.loads(path.read_text())
        del decision['projects'][0]['instruction']
        path.write_text(json.dumps(decision))
    else:
        path = tmp_path / 'episodes/ep001/outcome.json'
        record = json.loads(path.read_text())
        record['project_id'] = 'different'
        path.write_text(json.dumps(record))
    original = (tmp_path / 'state.json').read_bytes()
    with pytest.raises(E15InfrastructureError):
        wave.evolve_parallel_phase1(**resume_kwargs(tmp_path),
                                   vm_factory=lambda: pytest.fail('Must not boot VM'))
    assert (tmp_path / 'state.json').read_bytes() == original
