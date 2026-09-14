from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.self_evolving_loop import EvolutionResult, EvolutionStatus, TargetVerdict
from explore.practice_loop import PracticeInfrastructureError, _manifest
from explore.phase2_recovery import Phase2CurriculumRecovery
from explore.post_target_learning_recovery import continue_after_target_learning


@dataclass
class Config:
    max_iters: int = 500
    wall_clock_secs: float = 36000


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def boundary(tmp_path):
    root = tmp_path / 'attempt'
    cycle = root / 'target_cycles/cycle_001'
    learning = cycle / 'terminal_learning'
    memory = {'lesson.md': b'grounded actor memory'}
    (root / 'active_memory').mkdir(parents=True)
    (root / 'active_memory/lesson.md').write_bytes(memory['lesson.md'])
    cfg = Config()
    config_file = tmp_path / 'config.json'
    save(config_file, asdict(cfg))
    report = 'VERDICT: PASS\nObserved target evidence.'
    diagnosis = 'Actor-generated diagnosis.'
    pairs = [{'role': 'user', 'content': 'original observation'},
             {'role': 'assistant', 'content': 'original response'}]
    transcripts = [cycle / 'transcript.json',
                   learning / 'memory_distillation/segment_000/transcript.json',
                   learning / 'memory_reconciliation/segment_000/transcript.json',
                   learning / 'learning_diagnosis/attempt_000/segment_000/transcript.json']
    for i, path in enumerate(transcripts, 1):
        save(path, {'system': 'original system', 'messages': pairs * i})
    save(learning / 'outcome.json', dict(target_cycle=1, target_verifier_verdict='PASS',
        memory_before={}, memory_after=_manifest(memory), verifier_report=report,
        actor_learning_diagnosis=diagnosis, active_history_messages_before=2,
        active_history_messages_after=8))
    (learning / 'learning_diagnosis.md').write_text(diagnosis)
    verdict = cycle / 'iter_001/verify2.json'
    save(verdict, dict(verdict='pass', findings=report))
    save(root / 'manifest.json', dict(target_direction_sha256=hashlib.sha256(b'target').hexdigest(),
        initial_memory={'manifest': {}},
        protocol=dict(curriculum_memory_access='none', stop_policy='curriculum_review',
                      official_evaluator_calls=0, official_evaluator_feedback_enters_learning=False),
        configs={'curriculum': dict(path=str(config_file), sha256=hashlib.sha256(config_file.read_bytes()).hexdigest(),
                                    effective=asdict(cfg))}))
    events = [dict(event='TARGET_LEARNING_COMPLETED', payload=dict(target_cycle=1, verdict='PASS')),
              dict(event='PASS_NEXT_EXPERIENCE_REVIEW_STARTED', payload=dict(target_cycle=1, prospective_evolution=1))]
    (root / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    log = tmp_path / 'failed.log'
    log.write_text('TimeoutError: VM failed to become ready within timeout period')
    plan = dict(recovery_kind='post_target_learning_boot_failure', status='admitted_for_isolated_recovery',
        attempt_root=str(root), completed_projects=0, completed_curriculum_turns=0,
        prior_curriculum_iters=0, prior_curriculum_wall_secs=0, input_sha256={},
        curriculum_memory_access='none', target_verification=str(verdict), failed_runtime_log=str(log))
    path = tmp_path / 'recovery.json'
    save(path, plan)
    return SimpleNamespace(root=root, cycle=cycle, learning=learning, memory=memory,
                           plan=plan, path=path, cfg=cfg, transcripts=transcripts)


def admit(b):
    return Phase2CurriculumRecovery.admit(b.path, b.root, {'curriculum': b.cfg}, {}, 'target', 'curriculum_review')


def test_admits_without_replaying_target_or_curriculum(boundary):
    result = admit(boundary)
    assert result.post_target_learning
    assert result.initial_evolution_memory == boundary.memory
    assert result.history == [] and result.notes == ''
    assert result.projects == result.completed_curriculum_turns == 0
    assert result.remaining_iters == 500 and result.remaining_wall == 36000
    assert not (boundary.root / 'evolution_cycles').exists()


@pytest.mark.parametrize('marker', ['result.json', 'memory_frozen', 'evolution_cycles', 'audit_rejects.jsonl'])
def test_rejects_advanced_or_rejected_boundary(boundary, marker):
    (boundary.root / marker).touch()
    with pytest.raises(PracticeInfrastructureError):
        admit(boundary)


@pytest.mark.parametrize('change', ['memory', 'verdict', 'diagnosis', 'context', 'config',
                                    'event', 'curriculum', 'feedback', 'boot_fault', 'input_hash'])
def test_rejects_invalid_saved_boundary(boundary, change):
    b = boundary
    if change == 'memory':
        (b.root / 'active_memory/lesson.md').write_text('changed')
    elif change == 'verdict':
        save(Path(b.plan['target_verification']), dict(verdict='wrong', findings='FAIL'))
    elif change == 'diagnosis':
        (b.learning / 'learning_diagnosis.md').write_text('host advice')
    elif change == 'context':
        save(b.transcripts[-1], dict(system='changed', messages=[]))
    elif change == 'config':
        b.cfg.max_iters += 1
    elif change == 'event':
        (b.root / 'events.jsonl').write_text('{}\n{}')
    elif change == 'curriculum':
        b.plan['completed_curriculum_turns'] = 1; save(b.path, b.plan)
    elif change == 'feedback':
        save(b.root / 'grading.json', dict(score=1))
    elif change == 'boot_fault':
        Path(b.plan['failed_runtime_log']).write_text('ordinary task failure')
    elif change == 'input_hash':
        b.plan['input_sha256'] = {str(b.root / 'events.jsonl'): 'bad'}; save(b.path, b.plan)
    with pytest.raises((PracticeInfrastructureError, KeyError)):
        admit(b)


@pytest.mark.parametrize('status,suffix', [(EvolutionStatus.READY_FOR_RETRY, 'ready'),
                                         (EvolutionStatus.STALLED, 'stalled')])
def test_zero_practice_keeps_existing_pass_without_new_target(boundary, status, suffix):
    recovery = admit(boundary)
    calls = []
    def evolve(*args):
        calls.append(args)
        return EvolutionResult(status, boundary.memory, 0, 'original Curriculum decision')
    hooks = SimpleNamespace(evolve=evolve, on_event=lambda *args: None)
    result = continue_after_target_learning(recovery, 'target', hooks)
    assert len(calls) == 1 and calls[0][1] is TargetVerdict.PASS
    assert result.termination == 'verifier_pass_curriculum_' + suffix
    assert result.environment is None and result.output is None
    assert result.target_cycles == 1 and result.practice_projects == result.evolutions == 0
    assert result.memory == boundary.memory


def test_zero_practice_cannot_rewrite_actor_memory(boundary):
    hooks = SimpleNamespace(evolve=lambda *args: EvolutionResult(EvolutionStatus.READY_FOR_RETRY, {}, 0))
    with pytest.raises(PracticeInfrastructureError, match='changed Actor memory'):
        continue_after_target_learning(admit(boundary), 'target', hooks)


@pytest.mark.parametrize('status', [EvolutionStatus.READY_FOR_RETRY, EvolutionStatus.STALLED])
def test_completed_practice_requires_fresh_target(boundary, monkeypatch, status):
    import core.self_evolving_loop as loop
    calls = []
    sentinel = object()
    def next_target(direction, memory, hooks, *, start):
        calls.append(start)
        assert memory == {'new.md': b'practice memory'}
        return sentinel
    monkeypatch.setattr(loop, 'run_self_evolving_loop', next_target)
    hooks = SimpleNamespace(evolve=lambda *args: EvolutionResult(status, {'new.md': b'practice memory'}, 1),
                            on_event=lambda *args: None)
    assert continue_after_target_learning(admit(boundary), 'target', hooks) is sentinel
    assert calls[0].target_cycles == calls[0].evolutions == calls[0].practice_projects == 1
    assert calls[0].final_after_stall == (status is EvolutionStatus.STALLED)
