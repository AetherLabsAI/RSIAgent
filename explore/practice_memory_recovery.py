"""Resume first-practice learning after a disposable guest became read-only.

The saved terminal verdict and complete Actor history are mandatory. No Actor
work or Verifier inspection is repeated, and only normal learning may commit.
"""
from pathlib import Path
import ast
import dataclasses
import hashlib
import json
import time

from explore import e15_v12_loop as V
from explore.phase2_recovery import read, require, sha
from explore.practice_evidence_recovery import paired


def _published_notes(program, project_text):
    """Read literal private notes; never execute a saved Agent program."""
    tree = ast.parse(program.read_text())
    literals = {n.targets[0].id: n.value.value for n in tree.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    require(literals.get('handoff') == project_text and
            isinstance(literals.get('notes'), str), 'published notes are unavailable')
    expected = ast.dump(ast.parse(
        "with open('/home/user/curriculum_notes.md','w') as f:\n"
        "    f.write(notes)").body[0])
    require(any(ast.dump(n) == expected for n in tree.body) and
            read(program.parent / 'trace_meta.json')['exit_code'] == 0,
            'private notes were not successfully published')
    return literals['notes']


def admit_memory_recovery(cls, plan_path, root, configs, initial_memory, target, stop_policy):
    from core.verifier import _parse_agentic_verifier_report
    from explore.e15_loop import _parse_handoff, _UNIFIED_CURRICULUM_TOKEN

    plan_path, root = Path(plan_path), Path(root)
    plan = read(plan_path)
    require(plan['status'] == 'admitted_for_isolated_recovery' and
            plan['recovery_kind'] == 'verified_practice_memory_fs', 'wrong recovery plan')
    require(root.resolve() == Path(plan['attempt_root']).resolve() and
            stop_policy == 'curriculum_review', 'attempt or stop policy changed')
    require(plan['completed_projects'] == 0 and plan['completed_curriculum_turns'] == 1,
            'only first-practice uncommitted learning is supported')
    for path, digest in plan['input_sha256'].items():
        require(sha(path) == digest, 'archived input changed: ' + path)
    require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'] and
            sorted(p.name for p in (root / 'evolution_cycles').iterdir()) == ['cycle_001'],
            'target or evolution cycle changed')
    cycle = root / 'evolution_cycles/cycle_001'
    episode = cycle / 'episodes/ep001'
    require(sorted(p.name for p in (cycle / 'episodes').iterdir()) == ['ep001'] and
            sorted(p.name for p in (cycle / 'curriculum').iterdir()) == ['turn_001'],
            'a later practice or Curriculum turn exists')
    for parent, names in [(root, ('result.json', 'memory_frozen')),
                          (cycle, ('result.json', 'curriculum_terminal.md', 'curriculum_ready_for_retry.md')),
                          (episode, ('outcome.json', 'memory_reconciliation', 'learning_diagnosis'))]:
        require(not any((parent / name).exists() for name in names), 'learning boundary advanced')
    require(not list(root.rglob('grading.json')) and not list(root.rglob('audit_rejects.jsonl')),
            'graded or rejected lineage cannot resume')
    for path in root.rglob('boundary_audit.json'):
        audit = read(path)
        require(audit.get('status') != 'quarantined' and not audit.get('hits'), 'quarantined lineage')
    manifest = read(root / 'manifest.json')
    protocol = manifest['protocol']
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest() and
            manifest['initial_memory']['manifest'] == V._manifest(initial_memory), 'target/input changed')
    require(protocol['curriculum_memory_access'] == plan['curriculum_memory_access'] == 'none' and
            protocol['stop_policy'] == stop_policy and protocol['official_evaluator_calls'] == 0 and
            not protocol['official_evaluator_feedback_enters_learning'], 'protocol changed')
    for role, cfg in configs.items():
        saved = manifest['configs'][role]
        require(saved['sha256'] == sha(saved['path']) and
                saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))), 'role config changed')
    learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
    trigger = read(cycle / 'trigger/outcome.json')
    state = read(cycle / 'state.json')
    memory = V._read_memory_tree(str(cycle / 'memory'))
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] == 'PASS' and
            learning['memory_before'] == V._manifest(initial_memory), 'target learning changed')
    require(learning['memory_after'] == trigger['memory_before'] == state['memory_manifest'] ==
            V._manifest(memory) == V._manifest(V._read_memory_tree(str(root / 'active_memory'))),
            'last committed memory changed')
    require(state['status'] == 'infra' and state['projects'] == state['learning_experiences'] == 1 and
            state['reason'] == 'ACTOR phase ended at emergency/infrastructure status infra' and
            state['memory_tree_sha256'] == V._memory_tree_sha256(memory), 'not the failed memory boundary')
    require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol' and
            trigger['verifier_outcome'] == learning['target_verifier_verdict'] and
            trigger['verifier_report'] == learning['verifier_report'] and
            trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'], 'trigger changed')
    verdict = read(episode / 'verifier/iter_01/verify2.json')
    require(verdict['verdict'] in ('pass', 'wrong') and
            _parse_agentic_verifier_report(verdict['findings'], allow_unverified=True) == verdict['verdict'] and
            (episode / 'verifier_report.md').read_text() == verdict['findings'], 'no grounded terminal verdict')
    terminal = 'PASS' if verdict['verdict'] == 'pass' else 'FAIL'
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    require(events[-1]['event'] == 'EVOLUTION_MEMORY_PHASE_STARTED' and
            events[-1]['payload']['payload']['experience_index'] == 1 and
            events[-1]['payload']['payload']['terminal_outcome'] == terminal and
            events[-1]['payload']['payload']['verifier_report_sha256'] ==
            hashlib.sha256(verdict['findings'].encode()).hexdigest() and
            events[-1]['payload']['state']['memory_phase_open'] is True, 'memory start event changed')
    actor_source = episode / 'actor/attempt_001/segment_000/transcript.json'
    memory_source = episode / 'memory_distillation/segment_000/transcript.json'
    actor, failed = read(actor_source), read(memory_source)
    require(read(actor_source.parent / 'result.json')['status'] == 'done' and
            paired(actor['messages']) and paired(failed['messages']) and
            actor['system'] == failed['system'] and
            failed['messages'][:len(actor['messages'])] == actor['messages'], 'same-Actor history lost')
    require(sorted(p.name for p in memory_source.parent.parent.iterdir()) == ['segment_000'],
            'memory recovery already attempted')
    traces = sorted(memory_source.parent.glob('iter_*/trace_meta.json'))
    require(len(traces) >= 3 and all(read(p)['infra_fail'] and
            'Read-only file system' in p.with_name('trace.txt').read_text()
            for p in traces[-3:]), 'read-only staging failure not established')
    require('3 consecutive run-wrapper failures' in Path(plan['failed_runtime_log']).read_text(),
            'missing failed memory execution guard')
    budget = plan['prior_memory_budget']
    used_iters = len(list(memory_source.parent.glob('iter_*/turn.txt')))
    require(used_iters == budget['iterations'] and used_iters > 0 and
            0 < budget['wall_secs_conservative'] < configs['memory_actor'].wall_clock_secs and
            used_iters < configs['memory_actor'].max_iters, 'remaining memory budget invalid')
    require(plan['partial_guest_memory_written'] is False, 'partial guest memory needs its own recovery')
    curriculum_source = cycle / 'curriculum/turn_001/segment_000/transcript.json'
    history = read(curriculum_source)['messages']
    require(paired(history), 'incomplete Curriculum history')
    project_text = (episode / 'project.md').read_text()
    notes = _published_notes(Path(plan['curriculum_publication_program']), project_text)
    project = _parse_handoff(project_text, _UNIFIED_CURRICULUM_TOKEN)
    require(project is not None and project.token == 'PROJECT', 'invalid saved practice')
    candidate = episode / 'candidate/publication_001'
    require(sorted(p.name for p in candidate.parent.iterdir()) == ['publication_001'] and
            all((folder / 'materials.tgz').is_file() for folder in [candidate, episode / 'fixtures']),
            'candidate or fixture snapshot is missing')
    pending = dict(resume_kind='verified_memory', project=project.body,
                   actor_history=failed['messages'], before_memory=memory,
                   terminal_outcome=terminal, verifier_report=verdict['findings'],
                   actor_handoff=(episode / 'actor_handoff.md').read_text(), candidate_dir=str(candidate))
    metadata = dict(episode=str(episode), prior_iters=used_iters,
                    prior_wall_secs=budget['wall_secs_conservative'],
                    source_transcript=str(memory_source), source_sha256=sha(memory_source),
                    source_history=failed['messages'], start_monotonic=time.monotonic())
    return cls(plan_path, root, cycle, target, learning, trigger, learning,
               history, notes, (cycle / 'trigger/outcome.md').read_text(),
               configs['curriculum'].max_iters, configs['curriculum'].wall_clock_secs, {}, '',
               projects=0, project_records=[], completed_curriculum_turns=1,
               pending_practice=pending, initial_evolution_memory=memory,
               curriculum_memory_access='none', pending_memory_learning=metadata)


def restore_learning_candidate(*, hooks, vm, episode_dir, fixture_dir, memory_dir,
                               target, emit, saved):
    """Restore archived inputs for learning; do not rerun Actor or Verifier work."""
    require(saved['resume_kind'] == 'verified_memory', 'wrong pending memory state')
    require(V._read_memory_tree(str(memory_dir)) == saved['before_memory'], 'canonical memory changed')
    V._fresh_vm(hooks, vm, target)
    V._replay(hooks, vm, str(fixture_dir))
    V._replay(hooks, vm, saved['candidate_dir'])
    V._push_canonical_memory(hooks, vm, str(memory_dir))
    emit('PRACTICE_MEMORY_CANDIDATE_RESTORED', status='completed', project_open=True,
         memory_phase_open=True, payload=dict(project_index=1, actor_replayed=False,
             verifier_replayed=False, memory_committed=False))
    return {key: saved[key] for key in ('terminal_outcome', 'verifier_report', 'actor_handoff',
                                      'actor_history', 'before_memory')}


def wrap_memory_attempt(recovery, original):
    """Continue full saved context and charge failed distillation work."""
    meta = recovery.pending_memory_learning
    used_iters = meta['prior_iters']
    first = True

    def invoke(instruction, vm, cfg, sink, **kwargs):
        nonlocal used_iters, first
        if not Path(sink.root).is_relative_to(Path(meta['episode']) / 'memory_distillation'):
            return original(instruction, vm, cfg, sink, **kwargs)
        if first:
            require(kwargs.get('initial_history') == meta['source_history'], 'recovery dropped Actor context')
            require(sha(meta['source_transcript']) == meta['source_sha256'], 'failed transcript changed')
            instruction += ('\n\nINFRASTRUCTURE CONTINUATION: The disposable learning VM became '
                            'read-only before memory distillation completed. This fresh VM has the '
                            'same saved candidate and prior committed memory. Your full work and '
                            'failed learning history are retained. Previous private scratch files '
                            'outside that candidate are absent. Continue this unfinished learning phase.\n')
            first = False
        remaining_wall = cfg.wall_clock_secs - meta['prior_wall_secs'] - (
            time.monotonic() - meta['start_monotonic'])
        require(remaining_wall > 0 and used_iters < cfg.max_iters, 'original memory budget exhausted')
        limited = dataclasses.replace(cfg, max_iters=cfg.max_iters - used_iters,
                                      wall_clock_secs=remaining_wall)
        result, history = original(instruction, vm, limited, sink, **kwargs)
        used_iters += max(len(list(Path(sink.root).glob('iter_*/turn.txt'))),
                          int(getattr(result, 'turns', 0) or getattr(result, 'iters', 0)))
        V._atomic_json(Path(meta['episode']) / 'memory_recovery_receipt.json', dict(
            recovery_plan=str(recovery.plan_path), original_history_messages=len(meta['source_history']),
            prior_memory_iters=meta['prior_iters'], total_memory_iters=used_iters,
            prior_wall_secs=meta['prior_wall_secs'], remaining_wall_at_call=remaining_wall,
            resumed_sink=str(sink.root), status=str(getattr(result, 'status', 'infra')),
            actor_work_replayed=False, verifier_replayed=False, official_evaluator_calls=0))
        return result, history
    return invoke
