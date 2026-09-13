"""A late Curriculum recovery must preserve every preceding transaction."""
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import load
from explore import e15_loop as E, unified_evolution as U
from explore.e15_v12_loop import _memory_tree_sha256
from explore.phase2_recovery import Phase2CurriculumRecovery, sha
from test_phase2_recovery import fixture, save, admit
from test_unified_evolution import _VM, _hooks


def ten_practices(tmp_path):
    f = fixture(tmp_path)
    plan = json.loads(f.plan.read_text())
    old_turn = f.cycle / 'curriculum/turn_002'
    turn = f.cycle / 'curriculum/turn_011'
    old_turn.rename(turn)
    for index in range(2, 11):
        (f.cycle / f'curriculum/turn_{index:03d}').mkdir()
    before = f.memory
    for index in range(2, 11):
        after = {'lesson.md': f'practice {index}'.encode()}
        episode = f.cycle / f'episodes/ep{index:03d}'
        save(episode / 'outcome.json', {
            'project_index': index, 'terminal_outcome': 'PASS',
            'memory_before': E._manifest(before), 'memory_after': E._manifest(after),
        })
        (episode / 'outcome.md').write_text(f'Completed practice {index}.')
        before = after
    f.memory = before
    E._atomic_install_memory(str(f.cycle / 'memory'), before)
    save(f.cycle / 'state.json', {
        'status': 'running', 'projects': 10, 'learning_experiences': 10,
        'memory_manifest': E._manifest(before), 'memory_tree_sha256': _memory_tree_sha256(before),
    })
    event = {'event': 'EVOLUTION_PROJECT_CLOSED', 'payload': {
        'state': {'project_open': False, 'memory_phase_open': False},
        'payload': {'project_index': 10, 'memory_tree_sha256': _memory_tree_sha256(before)},
    }}
    (f.root / 'events.jsonl').write_text(json.dumps(event) + '\n')
    summary = ['Phase-2 target outcome PASS: the same Actor Agent committed grounded memory '
               'before Curriculum selected the next experience']
    summary.extend(f'practice project {i}: PASS' for i in range(1, 11))
    prompt = U.self_evolving_curriculum_charter(
        f.target, project_history='\n'.join(summary), latest_outcome='Completed practice 10.',
        curriculum_notes='all ten private notes', handoff_path=E.CURRICULUM_HANDOFF,
        notes_path=E.CURRICULUM_NOTES, phase2_outcome=True) + E._curriculum_runtime_contract()
    history = [{'role': 'user', 'content': 'NEXT PHASE:\n' + prompt},
               {'role': 'assistant', 'content': 'actionless narration'}]
    for source in sorted(turn.glob('segment_*/transcript.json')):
        save(source, {'messages': history})
    Path(plan['raw_checkpoint']).write_bytes(source.read_bytes())
    plan.update(completed_projects=10, completed_curriculum_turns=10,
                source_transcript=str(source), source_transcript_sha256=sha(source))
    save(f.plan, plan)
    return f


def test_admit_ten_transactions_and_remaining_budget(tmp_path):
    f = ten_practices(tmp_path)
    recovery = admit(f)
    assert recovery.projects == recovery.completed_curriculum_turns == 10
    assert len(recovery.project_records) == 10
    assert recovery.notes == 'all ten private notes'
    assert recovery.remaining_iters == f.configs['curriculum'].max_iters - 3
    assert recovery.project['memory_after'] == E._manifest(f.memory)


@pytest.mark.parametrize('fault', ['gap', 'extra_open', 'middle_chain', 'wrong_last_event', 'wrong_count'])
def test_reject_corrupt_or_open_multi_practice_checkpoint_without_writes(tmp_path, fault):
    f = ten_practices(tmp_path)
    if fault == 'gap':
        (f.cycle / 'episodes/ep005').rename(f.cycle / 'episodes/ep015')
    elif fault == 'extra_open':
        (f.cycle / 'episodes/ep011').mkdir()
    elif fault == 'middle_chain':
        path = f.cycle / 'episodes/ep005/outcome.json'
        data = json.loads(path.read_text()); data['memory_before'] = {}
        save(path, data)
    elif fault == 'wrong_last_event':
        path = f.root / 'events.jsonl'
        data = json.loads(path.read_text()); data['payload']['payload']['project_index'] = 1
        path.write_text(json.dumps(data) + '\n')
    else:
        plan = json.loads(f.plan.read_text()); plan['completed_projects'] = 9; save(f.plan, plan)
    before = {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(E.E15InfrastructureError):
        admit(f)
    assert before == {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}


def test_continue_turn11_without_replaying_ten_actors_or_learning(tmp_path, monkeypatch):
    f = ten_practices(tmp_path)
    boundary = admit(f)
    vm, prompts = _VM(), []
    hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn control.'], prompts)
    session = U.UnifiedCurriculumSession(history=boundary.history, notes=boundary.notes, turns=10)
    monkeypatch.setattr(U, '_run_practice_attempt', lambda **kw: pytest.fail('Actor replayed'))
    monkeypatch.setattr(U, '_promote_learning', lambda **kw: pytest.fail('Learning replayed'))
    hashes = {str(p): sha(p) for p in (f.cycle / 'episodes').rglob('*') if p.is_file()}
    result = U.evolve_until_ready(
        vm, str(f.cycle), f.target, boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'], f.active,
        load(None), load(None), load(None), load(None), hooks=hooks, session=session,
        trigger_authority='phase2_outcome_protocol', triggering_outcome='PASS', resume_boundary=boundary)
    assert result.status == 'ready_for_retry' and result.projects == 10 and session.turns == 11
    assert E._read_memory_tree(result.memory_dir) == f.memory
    assert all(sha(p) == h for p, h in hashes.items())
    assert 'practice project 10: PASS' in prompts[0]


def test_context_amendment_applies_only_to_interrupted_turn(tmp_path):
    boundary = admit(ten_practices(tmp_path))
    cfg = dataclasses.replace(load(None), practice_done_requires=E.CURRICULUM_HANDOFF)
    called = []
    wrapped = boundary.wrap_attempt(lambda instruction, vm, config, sink, **kw: called.append(config))
    for turn in (11, 12):
        wrapped('continue', None, cfg, SimpleNamespace(root=boundary.cycle / f'curriculum/turn_{turn:03d}/segment_003'))
    assert called[0].history_keep_pairs == 20
    assert called[1] is cfg
