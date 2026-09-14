"""Resume a committed PASS after the first practice VM failed to boot.

No Curriculum context or practice exists at this boundary. The saved target
verdict, same-Actor learning, and original role settings are mandatory inputs.
The target candidate is not restored or regraded; Phase2 exports only memory.
"""
from pathlib import Path
import dataclasses
import hashlib
import json

from explore.practice_loop import _manifest, _read_memory_tree
from explore.phase2_recovery import read, require, sha


def admit_post_target_learning(cls, plan_path, root, configs, initial_memory,
                              target, stop_policy):
    plan_path, root = Path(plan_path), Path(root)
    plan = read(plan_path)
    require(plan['status'] == 'admitted_for_isolated_recovery', 'plan is not admitted')
    require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
    require(stop_policy == 'curriculum_review', 'stop policy changed')
    require(plan['completed_projects'] == plan['completed_curriculum_turns'] == 0,
            'Curriculum or practice work already exists')
    require(plan['prior_curriculum_iters'] == plan['prior_curriculum_wall_secs'] == 0,
            'unexpected prior Curriculum budget')
    for path, digest in plan['input_sha256'].items():
        require(sha(path) == digest, 'archived input changed: ' + path)
    for marker in ('result.json', 'memory_frozen', 'evolution_cycles',
                   'curriculum_terminal.md', 'audit_rejects.jsonl'):
        require(not (root / marker).exists(), 'boundary already advanced: ' + marker)
    require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'],
            'target cycle count changed')
    for path in root.rglob('boundary_audit.json'):
        audit = read(path)
        require(audit.get('status') != 'quarantined' and not audit.get('hits'),
                'quarantined transcript')
    manifest = read(root / 'manifest.json')
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest(),
            'target direction changed')
    require(manifest['initial_memory']['manifest'] == _manifest(initial_memory),
            'initial memory changed')
    protocol = manifest['protocol']
    access = plan['curriculum_memory_access']
    require(access in ('none', 'read_only') and protocol['curriculum_memory_access'] == access
            and protocol['stop_policy'] == stop_policy
            and protocol['official_evaluator_calls'] == 0
            and not protocol['official_evaluator_feedback_enters_learning'],
            'protocol boundary changed')
    require(not list(root.rglob('grading.json')), 'official grading entered Phase2')
    for role, cfg in configs.items():
        saved = manifest['configs'][role]
        require(saved['sha256'] == sha(saved['path']), 'role config bytes changed')
        require(saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))),
                'effective role config changed')
    cycle = root / 'target_cycles/cycle_001'
    learning_root = cycle / 'terminal_learning'
    learning = read(learning_root / 'outcome.json')
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] == 'PASS',
            'completed target PASS learning required')
    require(learning['memory_before'] == _manifest(initial_memory), 'memory chain changed')
    memory = _read_memory_tree(str(root / 'active_memory'))
    require(learning['memory_after'] == _manifest(memory), 'committed memory changed')
    verifier_path = Path(plan['target_verification'])
    require(verifier_path.is_relative_to(cycle), 'verdict from another target')
    verifier = read(verifier_path)
    require(verifier['verdict'] == 'pass' and verifier['findings'] == learning['verifier_report'],
            'target verdict and learning disagree')
    paths = [cycle / 'transcript.json',
             learning_root / 'memory_distillation/segment_000/transcript.json',
             learning_root / 'memory_reconciliation/segment_000/transcript.json',
             learning_root / 'learning_diagnosis/attempt_000/segment_000/transcript.json']
    histories = [read(path) for path in paths]
    require(all(a['system'] == b['system']
                and b['messages'][:len(a['messages'])] == a['messages']
                for a, b in zip(histories, histories[1:])), 'same-Actor history prefix lost')
    require(len(histories[0]['messages']) == learning['active_history_messages_before']
            and len(histories[-1]['messages']) == learning['active_history_messages_after'],
            'learning history counters changed')
    require(all(history['messages'] and len(history['messages']) % 2 == 0
                and all(message.get('role') == ('user' if i % 2 == 0 else 'assistant')
                        for i, message in enumerate(history['messages']))
                for history in histories), 'incomplete Actor context')
    require((learning_root / 'learning_diagnosis.md').read_text() ==
            learning['actor_learning_diagnosis'], 'diagnosis changed')
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    require(len(events) >= 2 and events[-2]['event'] == 'TARGET_LEARNING_COMPLETED'
            and events[-2]['payload']['target_cycle'] == 1
            and events[-2]['payload']['verdict'] == 'PASS'
            and events[-1]['event'] == 'PASS_NEXT_EXPERIENCE_REVIEW_STARTED'
            and events[-1]['payload']['target_cycle'] == 1
            and events[-1]['payload']['prospective_evolution'] == 1,
            'not the pre-Curriculum boundary')
    require('TimeoutError: VM failed to become ready within timeout period' in
            Path(plan['failed_runtime_log']).read_text(), 'VM boot fault is not recorded')
    trigger = dict(target=target, trigger_authority='phase2_outcome_protocol',
                   verifier_outcome='PASS', verifier_report=learning['verifier_report'],
                   actor_learning_diagnosis=learning['actor_learning_diagnosis'],
                   memory_before=learning['memory_after'])
    curriculum = configs['curriculum']
    return cls(plan_path, root, root / 'evolution_cycles/cycle_001', target,
               learning, trigger, {}, [], '', '', curriculum.max_iters,
               curriculum.wall_clock_secs, {}, '', projects=0, project_records=[],
               completed_curriculum_turns=0, initial_evolution_memory=memory,
               curriculum_memory_access=access, post_target_learning=True)


def continue_after_target_learning(recovery, direction, hooks):
    """Call the original Curriculum once, then honor its normal stop semantics."""
    from core.self_evolving_loop import (
        EvolutionResult, EvolutionStatus, SelfEvolvingLoopResult,
        SelfEvolvingStart, TargetVerdict, run_self_evolving_loop,
    )
    require(recovery.post_target_learning and recovery.projects == 0
            and recovery.trigger['verifier_outcome'] == 'PASS', 'wrong recovery boundary')
    trigger = recovery.trigger
    evolved = hooks.evolve(direction, TargetVerdict.PASS, trigger['verifier_report'],
                           trigger['actor_learning_diagnosis'],
                           recovery.initial_evolution_memory, 1)
    require(isinstance(evolved, EvolutionResult), 'invalid evolution result')
    if evolved.projects == 0:
        termination = ('verifier_pass_curriculum_ready'
                       if evolved.status is EvolutionStatus.READY_FOR_RETRY
                       else 'verifier_pass_curriculum_stalled')
        # No completed practice means the memory must still be the committed
        # target learning. No synthetic candidate or extra target test is needed.
        require(evolved.memory == recovery.initial_evolution_memory,
                'zero-practice Curriculum changed Actor memory')
        hooks.on_event('PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED', dict(
            target_cycle=1, projects=0, reason=evolved.reason, termination=termination))
        hooks.on_event('TARGET_LOOP_TERMINATED', dict(
            target_cycle=1, verdict='PASS', termination=termination, evolutions=0,
            recovered_after_target_learning=True))
        return SelfEvolvingLoopResult(None, None, TargetVerdict.PASS,
                                      trigger['verifier_report'], evolved.memory,
                                      1, 0, 0, termination)
    start = SelfEvolvingStart(target_cycles=1, evolutions=1,
                             practice_projects=evolved.projects,
                             final_after_stall=evolved.status is EvolutionStatus.STALLED)
    hooks.on_event('RECOVERED_EVOLUTION_COMPLETED', dict(
        target_cycle=1, evolution=1, projects=evolved.projects,
        status=evolved.status.value, recovery_plan=str(recovery.plan_path)))
    return run_self_evolving_loop(direction, evolved.memory, hooks, start=start)
