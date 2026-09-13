"""Recovery must not replay completed work or turn transport failure into success."""
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import load
from core.self_evolving_loop import (
    SelfEvolvingStart, TargetVerdict, run_self_evolving_loop,
)
from explore import e15_loop as E, unified_evolution as U
from explore.phase2_recovery import Phase2CurriculumRecovery, sha
from explore.e15_v12_loop import _memory_tree_sha256
from test_self_evolving_loop import _hooks as target_hooks
from test_unified_evolution import _VM, _hooks


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def fixture(tmp_path):
    root = tmp_path / 'attempt'
    cycle = root / 'evolution_cycles/cycle_001'
    target = 'Update the supplied document.'
    initial, active, memory = ({'lesson.md': value} for value in (b'initial', b'target', b'practice'))
    for path, tree in ((root / 'active_memory', active), (cycle / 'memory', memory)):
        E._atomic_install_memory(str(path), tree)
    cfg = load(None)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('model: original-model\n')
    manifest = {
        'target_direction_sha256': hashlib.sha256(target.encode()).hexdigest(),
        'initial_memory': {'manifest': E._manifest(initial)},
        'configs': {'curriculum': {'path': str(config_path), 'sha256': sha(config_path),
                                  'effective': dataclasses.asdict(cfg)}},
    }
    save(root / 'manifest.json', manifest)
    learning = {'target_cycle': 1, 'target_verifier_verdict': 'PASS',
                'verifier_report': 'VERDICT: PASS\nrole evidence',
                'actor_learning_diagnosis': 'same Actor diagnosis',
                'memory_before': E._manifest(initial), 'memory_after': E._manifest(active)}
    save(root / 'target_cycles/cycle_001/terminal_learning/outcome.json', learning)
    trigger = {'target': target, 'trigger_authority': 'phase2_outcome_protocol',
               'verifier_report': learning['verifier_report'], 'verifier_outcome': 'PASS',
               'actor_learning_diagnosis': learning['actor_learning_diagnosis'],
               'memory_before': E._manifest(active)}
    save(cycle / 'trigger/outcome.json', trigger)
    project = {'project_index': 1, 'terminal_outcome': 'PASS',
               'memory_before': E._manifest(active), 'memory_after': E._manifest(memory)}
    save(cycle / 'episodes/ep001/outcome.json', project)
    latest = 'The completed practice outcome, verbatim.'
    (cycle / 'episodes/ep001/outcome.md').write_text(latest)
    save(cycle / 'state.json', {'status': 'running', 'projects': 1, 'learning_experiences': 1,
                               'memory_manifest': E._manifest(memory),
                               'memory_tree_sha256': _memory_tree_sha256(memory)})
    event = {'event': 'EVOLUTION_PROJECT_CLOSED', 'payload': {
        'state': {'project_open': False, 'memory_phase_open': False},
        'payload': {'project_index': 1, 'memory_tree_sha256': _memory_tree_sha256(memory)}}}
    (root / 'events.jsonl').write_text(json.dumps(event) + '\n')
    summary = ('Phase-2 target outcome PASS: the same Actor Agent committed grounded memory '
               'before Curriculum selected the next experience\npractice project 1: PASS')
    prompt = U.self_evolving_curriculum_charter(
        target, project_history=summary, latest_outcome=latest, curriculum_notes='private notes',
        handoff_path=E.CURRICULUM_HANDOFF, notes_path=E.CURRICULUM_NOTES,
        phase2_outcome=True) + E._curriculum_runtime_contract()
    history = [{'role': 'user', 'content': 'NEXT PHASE:\n' + prompt},
               {'role': 'assistant', 'content': 'actionless narration'}]
    (cycle / 'curriculum/turn_001').mkdir(parents=True)
    for n in range(3):
        source = cycle / f'curriculum/turn_002/segment_{n:03d}/transcript.json'
        save(source, {'messages': history})
        (source.parent / 'iter_01').mkdir()
        (source.parent / 'iter_01/turn.txt').write_text('actionless narration')
    raw = tmp_path / 'raw.json'
    raw.write_bytes(source.read_bytes())
    plan = {'attempt_root': str(root), 'status': 'admitted_for_isolated_recovery',
            'input_sha256': {str(root / 'manifest.json'): sha(root / 'manifest.json')},
            'source_transcript': str(source), 'source_transcript_sha256': sha(source),
            'raw_checkpoint': str(raw), 'prior_iters': 3, 'prior_wall_secs_conservative': 12,
            'context_fields': {'history_keep_pairs': 20},
            'recovery_observation': '\nFresh authoring VM; prior private files are absent.'}
    plan_path = tmp_path / 'plan.json'
    save(plan_path, plan)
    return SimpleNamespace(root=root, cycle=cycle, initial=initial, active=active, memory=memory,
                           target=target, configs={'curriculum': cfg}, plan=plan_path)


def admit(f):
    return Phase2CurriculumRecovery.admit(
        f.plan, f.root, f.configs, f.initial, f.target, 'curriculum_review')


def test_admission_restores_context_notes_and_consumed_budget(tmp_path):
    f = fixture(tmp_path)
    b = admit(f)
    assert b.notes == 'private notes'
    assert b.history[-1]['content'] == 'actionless narration'
    assert b.remaining_iters == f.configs['curriculum'].max_iters - 3
    assert b.remaining_wall == f.configs['curriculum'].wall_clock_secs - 12
    assert E._read_memory_tree(str(f.cycle / 'memory')) == f.memory


@pytest.mark.parametrize('fault', ['memory', 'open_project', 'quarantine', 'terminal',
                                  'changed_config', 'late_context', 'event_open', 'budget'])
def test_admission_refuses_unsafe_boundaries_without_writes(tmp_path, fault):
    f = fixture(tmp_path)
    if fault == 'memory':
        (f.cycle / 'memory/lesson.md').write_bytes(b'partial')
    elif fault == 'open_project':
        (f.cycle / 'episodes/ep002').mkdir()
    elif fault == 'quarantine':
        save(f.cycle / 'curriculum/turn_002/boundary_audit.json', {'status': 'quarantined'})
    elif fault == 'terminal':
        save(f.root / 'result.json', {'status': 'complete'})
    elif fault == 'changed_config':
        f.configs['curriculum'] = dataclasses.replace(f.configs['curriculum'], temperature=0.7)
    elif fault == 'late_context':
        save(f.cycle / 'curriculum/turn_002/segment_003/transcript.json', {'messages': []})
    elif fault == 'event_open':
        (f.root / 'events.jsonl').write_text(json.dumps({'event': 'EVOLUTION_PROJECT_OPENED'}) + '\n')
    else:
        plan = json.loads(f.plan.read_text())
        plan['prior_wall_secs_conservative'] = f.configs['curriculum'].wall_clock_secs + 1
        save(f.plan, plan)
    before = {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(E.E15InfrastructureError):
        admit(f)
    assert before == {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}


def test_resume_continues_only_curriculum_and_keeps_completed_practice(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    boundary = admit(f)
    vm, prompts = _VM(), []
    hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn to the target.'], prompts)
    session = U.UnifiedCurriculumSession(history=boundary.history, notes=boundary.notes, turns=1)
    monkeypatch.setattr(U, '_run_practice_attempt', lambda **kw: pytest.fail('completed Actor replayed'))
    monkeypatch.setattr(U, '_promote_learning', lambda **kw: pytest.fail('memory replayed'))
    before = sha(f.cycle / 'episodes/ep001/outcome.json')
    result = U.evolve_until_ready(
        vm, str(f.cycle), f.target, boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'], f.active,
        load(None), load(None), load(None), load(None),
        hooks=hooks, session=session, trigger_authority='phase2_outcome_protocol',
        triggering_outcome='PASS', resume_boundary=boundary)
    assert result.status == 'ready_for_retry'
    assert result.projects == 1 and session.turns == 2 and session.waves == 1
    assert session.history[:len(boundary.history)] == boundary.history
    assert result.memory_dir == str(f.cycle / 'memory')
    assert E._read_memory_tree(result.memory_dir) == f.memory
    assert sha(f.cycle / 'episodes/ep001/outcome.json') == before
    assert 'private notes' in prompts[0]


def test_fresh_target_resumes_at_cycle_two_and_stall_gets_one_final_test():
    hooks, events, released, learned = target_hooks([TargetVerdict.FAIL])
    result = run_self_evolving_loop(
        'target', {'practice_1.md': b'committed'}, hooks,
        start=SelfEvolvingStart(1, 1, 1, final_after_stall=True))
    assert result.termination == 'curriculum_stalled_final_target_test'
    assert result.target_cycles == 2 and result.evolutions == result.practice_projects == 1
    assert result.output['cycle'] == 2
    assert [actor.cycle for actor, verdict in learned] == [2]
    assert released == []


@pytest.mark.parametrize('actions,terminal,expected', [
    ([0, 0, 0], False, 'infra'),
    ([0, 0, 0], True, 'done'),
    ([0, 0, 1, 0, 0, 0], True, 'done'),
])
def test_actionless_guard_preserves_terminal_authority_and_productive_resets(
        tmp_path, actions, terminal, expected):
    class TransportVM(_VM):
        def run_command(self, command, **kwargs):
            if 'E15_REMOVE_TREE_RC' in command:
                return 'E15_REMOVE_TREE_RC=0'
            return super().run_command(command, **kwargs)
    vm, calls = TransportVM(), []
    def run(prompt, vm, cfg, sink, **kwargs):
        n = len(calls)
        calls.append(kwargs)
        done = terminal and n == len(actions) - 1
        if done:
            vm.files[E.CURRICULUM_HANDOFF] = 'DECISION: PROJECT\nOwned project.'
        return SimpleNamespace(status='done' if done else 'stalled', programs_run=actions[n],
                               looks=0, iters=1, wall_secs=2), []
    hooks = dataclasses.replace(_hooks(vm, [], []), run_attempt=run)
    def invoke():
        return E._run_handoff_phase(
            hooks=hooks, vm=vm, cfg=load(None), prompt='search', role='CURRICULUM',
            handoff_path=E.CURRICULUM_HANDOFF, token_pattern=E._CURRICULUM_TOKEN,
            sink_root=str(tmp_path), target='target', require_completed_publication=True)
    if expected == 'infra':
        with pytest.raises(E.E15InfrastructureError, match='no executable progress'):
            invoke()
        assert json.loads((tmp_path / 'transport_stop.json').read_text())['semantic_decision'] is None
    else:
        assert invoke()[0].token == 'PROJECT'
    assert len(calls) == len(actions)
    assert calls[-1]['iters_budget'] == load(None).max_iters - len(calls) + 1
    assert calls[-1]['wall_budget'] == load(None).wall_clock_secs - 2 * (len(calls) - 1)


def test_compaction_amendment_only_applies_to_interrupted_curriculum_turn(tmp_path):
    boundary = admit(fixture(tmp_path))
    observed = []
    def original(instruction, vm, cfg, sink, **kwargs):
        observed.append(cfg)
        return None
    wrapped = boundary.wrap_attempt(original)
    cfg = dataclasses.replace(load(None), practice_done_requires=E.CURRICULUM_HANDOFF)
    for role_path in ('curriculum/turn_002/segment_003', 'curriculum/turn_003/segment_000', 'episodes/ep002/actor'):
        wrapped('instruction', None, cfg, SimpleNamespace(root=str(boundary.cycle / role_path)))
    assert observed[0].history_keep_pairs == 20
    assert observed[1] is cfg and observed[2] is cfg
    assert dataclasses.replace(observed[0], history_keep_pairs=cfg.history_keep_pairs) == cfg


def test_recovery_cannot_change_model_via_context_amendment(tmp_path):
    f = fixture(tmp_path)
    plan = json.loads(f.plan.read_text())
    plan['context_fields']['model'] = 'another-model'
    save(f.plan, plan)
    with pytest.raises(E.E15InfrastructureError, match='non-context'):
        admit(f)


def test_productive_segments_still_share_one_role_budget(tmp_path, monkeypatch):
    vm, calls = _VM(), []
    monkeypatch.setattr(E, '_remove_incomplete_curriculum_project', lambda vm: None)
    def run(*args, **kw):
        calls.append(kw)
        return SimpleNamespace(status='budget', programs_run=1, looks=0, iters=1, wall_secs=4), []
    hooks = dataclasses.replace(_hooks(vm, [], []), run_attempt=run)
    cfg = dataclasses.replace(load(None), max_iters=2, wall_clock_secs=100)
    with pytest.raises(E.E15InfrastructureError, match='cumulative transport budget'):
        E._run_handoff_phase(
            hooks=hooks, vm=vm, cfg=cfg, prompt='search', role='CURRICULUM',
            handoff_path=E.CURRICULUM_HANDOFF, token_pattern=E._CURRICULUM_TOKEN,
            sink_root=str(tmp_path), target='target', require_completed_publication=True)
    assert len(calls) == 2
    assert calls[1]['iters_budget'] == 1 and calls[1]['wall_budget'] == 96
