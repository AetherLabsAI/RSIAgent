from dataclasses import dataclass, asdict
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import time

import pytest

from explore import e15_v12_loop as V
from explore import practice_memory_recovery as R
from explore.e15_loop import E15InfrastructureError
from explore.phase2_recovery import Phase2CurriculumRecovery


@dataclass
class Config:
    max_iters: int = 2000
    wall_clock_secs: float = 86400


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def boundary(tmp_path):
    root = tmp_path / 'attempt'
    cycle = root / 'evolution_cycles/cycle_001'
    ep = cycle / 'episodes/ep001'
    target_root = root / 'target_cycles/cycle_001'
    memory = {'lesson.md': b'committed target memory'}
    for folder in [root / 'active_memory', cycle / 'memory']:
        folder.mkdir(parents=True)
        (folder / 'lesson.md').write_bytes(memory['lesson.md'])
    cfg = Config()
    config_path = tmp_path / 'config.json'
    save(config_path, asdict(cfg))
    report = 'Independent target inspection.\nVERDICT: PASS'
    diagnosis = 'Original Actor diagnosis.'
    learning = dict(target_cycle=1, target_verifier_verdict='PASS', memory_before={},
                    memory_after=V._manifest(memory), verifier_report=report,
                    actor_learning_diagnosis=diagnosis)
    save(target_root / 'terminal_learning/outcome.json', learning)
    save(cycle / 'trigger/outcome.json', dict(target='target', trigger_authority='phase2_outcome_protocol',
         verifier_outcome='PASS', verifier_report=report, actor_learning_diagnosis=diagnosis,
         memory_before=V._manifest(memory)))
    (cycle / 'trigger/outcome.md').write_text('original trigger')
    save(cycle / 'state.json', dict(status='infra', projects=1, learning_experiences=1,
         reason='ACTOR phase ended at emergency/infrastructure status infra',
         memory_manifest=V._manifest(memory), memory_tree_sha256=V._memory_tree_sha256(memory)))
    save(root / 'manifest.json', dict(target_direction_sha256=hashlib.sha256(b'target').hexdigest(),
         initial_memory={'manifest': {}}, protocol=dict(curriculum_memory_access='none',
         stop_policy='curriculum_review', official_evaluator_calls=0,
         official_evaluator_feedback_enters_learning=False),
         configs={role: dict(path=str(config_path), sha256=R.sha(config_path), effective=asdict(cfg))
                  for role in ['memory_actor', 'curriculum']}))
    verdict = 'Independent practice inspection.\nVERDICT: FAIL'
    save(ep / 'verifier/iter_01/verify2.json', dict(verdict='wrong', findings=verdict))
    (ep / 'verifier_report.md').write_text(verdict)
    save(root / 'events.jsonl', dict(event='EVOLUTION_MEMORY_PHASE_STARTED', payload=dict(
         payload=dict(experience_index=1, terminal_outcome='FAIL',
                      verifier_report_sha256=hashlib.sha256(verdict.encode()).hexdigest()),
         state=dict(memory_phase_open=True))))
    pairs = [dict(role='user', content='observation'), dict(role='assistant', content='response')]
    actor = ep / 'actor/attempt_001/segment_000'
    source = ep / 'memory_distillation/segment_000'
    save(actor / 'transcript.json', dict(system='original actor system', messages=pairs))
    save(actor / 'result.json', dict(status='done'))
    save(source / 'transcript.json', dict(system='original actor system', messages=pairs * 4))
    for idx in range(1, 4):
        p = source / f'iter_{idx:02d}'
        save(p / 'trace_meta.json', dict(infra_fail=True, exit_code=None))
        (p / 'trace.txt').write_text('mktemp: Read-only file system')
        (p / 'turn.txt').write_text('failed staging')
    program = cycle / 'curriculum/turn_001/segment_000/iter_01/program.py'
    save(program.parent / 'trace_meta.json', dict(exit_code=0))
    project = 'DECISION: PROJECT\nInspect /home/user/evolution_project.'
    program.write_text(f'handoff={project!r}\nnotes="private notes"\n'
                       "with open('/home/user/curriculum_notes.md','w') as f:\n    f.write(notes)\n")
    save(program.parent.parent / 'transcript.json', dict(system='curriculum', messages=pairs))
    (ep / 'project.md').write_text(project)
    (ep / 'actor_handoff.md').write_text('STATUS: SUBMIT')
    for folder in [ep / 'candidate/publication_001', ep / 'fixtures']:
        folder.mkdir(parents=True)
        (folder / 'materials.tgz').write_bytes(b'fixture archive')
    log = tmp_path / 'failed.log'
    log.write_text('3 consecutive run-wrapper failures')
    plan = dict(recovery_kind='verified_practice_memory_fs', status='admitted_for_isolated_recovery',
                attempt_root=str(root), completed_projects=0, completed_curriculum_turns=1,
                input_sha256={}, curriculum_memory_access='none', failed_runtime_log=str(log),
                prior_memory_budget=dict(iterations=3, wall_secs_conservative=30),
                partial_guest_memory_written=False, curriculum_publication_program=str(program))
    plan_path = tmp_path / 'recovery_plan.json'
    save(plan_path, plan)
    return SimpleNamespace(root=root, cycle=cycle, ep=ep, source=source, memory=memory,
                           cfg=cfg, plan=plan, plan_path=plan_path, program=program)


def admit(b):
    return Phase2CurriculumRecovery.admit(b.plan_path, b.root,
        {'memory_actor': b.cfg, 'curriculum': b.cfg}, {}, 'target', 'curriculum_review')


def test_admits_saved_fail_and_complete_failed_learning_history(boundary):
    b = boundary
    result = admit(b)
    assert result.projects == 0 and result.completed_curriculum_turns == 1
    assert result.pending_practice['terminal_outcome'] == 'FAIL'
    assert len(result.pending_practice['actor_history']) == 8
    assert result.notes == 'private notes' and result.initial_evolution_memory == b.memory
    assert not (b.ep / 'outcome.json').exists()


@pytest.mark.parametrize('fault', ['unverified', 'memory', 'history', 'system', 'config',
    'advanced', 'quarantine', 'grading', 'readonly', 'budget', 'iterations', 'notes',
    'candidate', 'partial', 'hash', 'event'])
def test_refuses_unsafe_recovery(boundary, fault):
    b = boundary
    if fault == 'unverified':
        save(b.ep / 'verifier/iter_01/verify2.json', dict(verdict='unverified', findings='VERDICT: UNVERIFIED'))
    elif fault == 'memory':
        (b.cycle / 'memory/lesson.md').write_text('uncommitted edit')
    elif fault in ['history', 'system']:
        p = b.source / 'transcript.json';d = R.read(p)
        if fault == 'history':d['messages'][0]['content'] = 'replaced context'
        else:d['system'] = 'replacement system'
        save(p, d)
    elif fault == 'config':b.cfg.max_iters += 1
    elif fault == 'advanced':save(b.ep / 'outcome.json', {})
    elif fault == 'quarantine':save(b.ep / 'boundary_audit.json', dict(status='quarantined'))
    elif fault == 'grading':save(b.root / 'grading.json', {})
    elif fault == 'readonly':(b.source / 'iter_03/trace.txt').write_text('ordinary error')
    elif fault == 'budget':b.plan['prior_memory_budget']['wall_secs_conservative'] = b.cfg.wall_clock_secs
    elif fault == 'iterations':b.plan['prior_memory_budget']['iterations'] = 2
    elif fault == 'notes':b.program.write_text(b.program.read_text().replace('f.write(notes)', 'pass'))
    elif fault == 'candidate':(b.ep / 'candidate/publication_001/materials.tgz').unlink()
    elif fault == 'partial':b.plan['partial_guest_memory_written'] = True
    elif fault == 'hash':b.plan['input_sha256'] = {str(b.program): 'changed'}
    elif fault == 'event':save(b.root / 'events.jsonl', dict(event='EVOLUTION_PROJECT_CLOSED'))
    save(b.plan_path, b.plan)
    with pytest.raises((E15InfrastructureError, KeyError)):
        admit(b)


def test_restore_replays_only_saved_candidate_and_committed_memory(boundary, monkeypatch):
    b = boundary;recovery = admit(b);calls = [];events = []
    monkeypatch.setattr(V, '_fresh_vm', lambda *a: calls.append('fresh'))
    monkeypatch.setattr(V, '_replay', lambda h, vm, path: calls.append(path))
    monkeypatch.setattr(V, '_push_canonical_memory', lambda h, vm, path: calls.append(path))
    value = R.restore_learning_candidate(hooks=object(), vm=object(), episode_dir=b.ep,
        fixture_dir=b.ep / 'fixtures', memory_dir=b.cycle / 'memory', target='target',
        emit=lambda event, **kw: events.append((event, kw)), saved=recovery.pending_practice)
    assert calls == ['fresh', str(b.ep / 'fixtures'), str(b.ep / 'candidate/publication_001'), str(b.cycle / 'memory')]
    assert value['actor_history'] == R.read(b.source / 'transcript.json')['messages']
    assert value['terminal_outcome'] == 'FAIL' and value['before_memory'] == b.memory
    assert events[0][1]['payload']['actor_replayed'] is False
    assert not (b.ep / 'outcome.json').exists()


def test_resume_deducts_prior_budget_and_preserves_full_context(boundary):
    b = boundary;recovery = admit(b);calls = []
    sink = SimpleNamespace(root=str(b.ep / 'memory_distillation/segment_001'))
    def original(instruction, vm, cfg, sink, **kwargs):
        calls.append((instruction, cfg, kwargs))
        return SimpleNamespace(status='done', turns=2), kwargs['initial_history']
    result, history = recovery.wrap_attempt(original)('original learning prompt', None, b.cfg, sink,
        initial_history=recovery.pending_practice['actor_history'], continue_context=True)
    assert result.status == 'done' and len(history) == 8
    assert calls[0][1].max_iters == 1997 and 0 < calls[0][1].wall_clock_secs <= 86370
    assert calls[0][2]['continue_context'] is True
    receipt = R.read(b.ep / 'memory_recovery_receipt.json')
    assert receipt['total_memory_iters'] == 5 and receipt['actor_work_replayed'] is False


@pytest.mark.parametrize('fault', ['exhausted', 'dropped_context'])
def test_resume_refuses_exhausted_budget_or_lost_history(boundary, fault):
    b = boundary;recovery = admit(b)
    if fault == 'exhausted':recovery.pending_memory_learning['start_monotonic'] = time.monotonic() - 90000
    history = [] if fault == 'dropped_context' else recovery.pending_practice['actor_history']
    with pytest.raises(E15InfrastructureError):
        recovery.wrap_attempt(lambda *a, **kw: pytest.fail('must not call Actor'))(
            'prompt', None, b.cfg, SimpleNamespace(root=str(b.ep / 'memory_distillation/segment_001')),
            initial_history=history)


def test_commit_once_before_next_curriculum_without_actor_or_verifier_replay(boundary, monkeypatch):
    from config.settings import load
    from explore import unified_evolution as U
    from explore.practice_evidence_recovery import run_practice_with_evidence
    from test_unified_evolution import _VM, _hooks
    b = boundary;recovery = admit(b);calls = [];prompts = [];events = []
    vm = _VM();hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn control.'], prompts)
    session = U.UnifiedCurriculumSession(history=recovery.history, notes=recovery.notes, turns=1)
    monkeypatch.setattr(V, '_fresh_vm', lambda *a: calls.append('restore'))
    monkeypatch.setattr(V, '_replay', lambda *a: None)
    monkeypatch.setattr(V, '_push_canonical_memory', lambda *a: None)
    monkeypatch.setattr(U, '_run_practice_attempt', run_practice_with_evidence)
    def learning(**kw):
        assert calls == ['restore'] and not prompts
        assert kw['actor_history'] == recovery.pending_practice['actor_history']
        assert kw['terminal_outcome'] == 'FAIL' and kw['experience_index'] == 1
        calls.append('learn')
        return b.memory, 'same Actor diagnosis', kw['actor_history']
    monkeypatch.setattr(U, '_promote_learning', learning)
    result = U.evolve_until_ready(vm, str(b.cycle), 'target', recovery.trigger['verifier_report'],
        recovery.trigger['actor_learning_diagnosis'], b.memory, load(None), load(None), load(None), load(None),
        hooks=hooks, session=session, trigger_authority='phase2_outcome_protocol', triggering_outcome='PASS',
        resume_boundary=recovery, curriculum_memory_access='none',
        event_sink=lambda event, **kw: events.append((event, kw)))
    assert result.status == 'ready_for_retry', result.reason
    assert result.projects == 1 and calls == ['restore', 'learn'] and session.turns == 2
    assert [event for event, fields in events].count('PROJECT_CLOSED') == 1
    assert 'practice project 1: FAIL' in prompts[0]
    assert R.read(b.ep / 'outcome.json')['terminal_outcome'] == 'FAIL'
    access = R.read(b.cycle / 'curriculum/turn_002/memory_access.json')
    assert access['access'] == 'none' and access['attached_files'] == access['attached_bytes'] == 0
