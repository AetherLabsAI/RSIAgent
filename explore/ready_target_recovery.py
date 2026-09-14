"""Admit an explicitly authorized fresh target after a saved READY boundary.

The previous target must have stopped without verdict or learning. This is a
new target execution with retained committed experience, never a fabricated
continuation of its lost Actor/Verifier conversation.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

from explore.practice_loop import _manifest, _read_memory_tree
from explore.target_learning import _memory_tree_sha256
from explore.phase2_recovery import read, require, sha


def recover_appended_notes(program_path, trace_path):
    """Read a literal from saved source; never execute agent-authored code."""
    tree = ast.parse(Path(program_path).read_text())
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and any(
                  isinstance(t, ast.Name) and t.id == 'notes_append'
                  for t in node.targets)]
    require(len(values) == 1 and isinstance(values[0], str),
            'ambiguous saved private notes')
    notes = values[0]
    trace = Path(trace_path).read_text()
    require(trace == 'first line: DECISION: READY_FOR_TARGET\n'
            'handoff bytes: 27\n'
            f'notes appended; size: {len(notes.encode())}\n[exit 0]',
            'saved successful write does not prove complete notes')
    return notes


def admit_ready_target_recovery(cls, plan_path, root, configs, initial_memory,
                               target, stop_policy):
    plan = read(plan_path)
    source = Path(plan['source_attempt_root'])
    count = plan['completed_projects']
    require(plan['status'] == 'admitted_for_isolated_recovery'
            and plan['fresh_target_authorized'] is True,
            'fresh target execution not authorized')
    require(type(count) is int and count >= 1, 'invalid completed projects')
    require(root.resolve() == Path(plan['attempt_root']).resolve()
            and root.resolve() != source.resolve(), 'replacement needs a new root')
    require(stop_policy == plan['phase2_stop_policy'] == 'curriculum_review',
            'stop policy changed')
    require(plan['curriculum_memory_access'] == 'none'
            and plan['phase3_auto_launch'] is False
            and plan['automatic_actor_retry'] is False,
            'comparison scope changed')
    for name, expected in plan['input_sha256'].items():
        require(sha(name) == expected, 'saved input changed: ' + name)
    for name, expected in plan['clone_sha256'].items():
        require(sha(root / name) == expected, 'replacement checkpoint changed: ' + name)
    require(plan['completed_curriculum_turns'] == count + 1,
            'missing READY Curriculum turn')
    for attempt in (source, root):
        require(not (attempt / 'result.json').exists(), 'attempt already completed')
        require(sorted(p.name for p in (attempt / 'target_cycles').iterdir())
                == ['cycle_001', 'cycle_002'], 'target execution count changed')
        require(sorted(p.name for p in (attempt / 'evolution_cycles').iterdir())
                == ['cycle_001'], 'evolution count changed')
        failed = attempt / 'target_cycles/cycle_002'
        require(not (failed / 'terminal_learning').exists()
                and not (failed / 'result.json').exists(),
                'previous target already has a terminal transaction')
        for audit in attempt.rglob('boundary_audit.json'):
            record = read(audit)
            require(record.get('status') != 'quarantined' and not record.get('hits'),
                    'quarantined attempt cannot be resumed')
        require(not list(attempt.rglob('audit_rejects.jsonl')),
                'rejected boundary requires separate review')
    manifest = read(root / 'manifest.json')
    original_manifest = read(source / 'manifest.json')
    require(manifest['protocol_run'] == plan['protocol_run']
            and original_manifest['protocol_run'] == plan['source_protocol_run'],
            'lineage identity changed')
    expected_manifest = dict(original_manifest, protocol_run=plan['protocol_run'])
    expected_manifest['infrastructure_replacement'] = {
        'source_attempt_root': str(source), 'source_manifest_sha256': sha(source / 'manifest.json'),
        'plan': str(plan_path), 'fresh_target_execution': True}
    require(manifest == expected_manifest, 'replacement manifest changed')
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest()
            and manifest['initial_memory']['manifest'] == _manifest(initial_memory),
            'original target or Phase1 memory changed')
    for role, cfg in configs.items():
        saved = manifest['configs'][role]
        require(saved['sha256'] == sha(saved['path'])
                and saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))),
                'effective role configuration changed: ' + role)
    cycle = root / 'evolution_cycles/cycle_001'
    require(sorted(p.name for p in (cycle / 'episodes').iterdir()) ==
            [f'ep{i:03d}' for i in range(1, count + 1)], 'an unfinished practice exists')
    require(sorted(p.name for p in (cycle / 'curriculum').iterdir()) ==
            [f'turn_{i:03d}' for i in range(1, count + 2)], 'Curriculum context count changed')
    state = read(cycle / 'state.json')
    decision = (cycle / 'curriculum_ready_for_retry.md').read_text()
    require(state['status'] == 'ready_for_retry'
            and state['projects'] == state['learning_experiences'] == count
            and state['terminal_text'] == decision == 'DECISION: READY_FOR_TARGET\n',
            'no published semantic READY boundary')
    learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
    trigger = read(cycle / 'trigger/outcome.json')
    records = [read(cycle / f'episodes/ep{i:03d}/outcome.json')
               for i in range(1, count + 1)]
    require(learning['target_verifier_verdict'] in ('PASS', 'FAIL')
            and learning['target_cycle'] == 1
            and learning['memory_before'] == _manifest(initial_memory),
            'first target learning is not grounded')
    require(trigger['target'] == target
            and trigger['trigger_authority'] == 'phase2_outcome_protocol'
            and trigger['verifier_outcome'] == learning['target_verifier_verdict']
            and trigger['verifier_report'] == learning['verifier_report']
            and trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'],
            'target trigger changed')
    before = learning['memory_after']
    require(before == trigger['memory_before'], 'target-to-practice chain broken')
    for i, record in enumerate(records, 1):
        require(record['project_index'] == i
                and record['terminal_outcome'] in ('PASS', 'FAIL')
                and record['memory_before'] == before,
                'practice transaction is not complete or serial')
        before = record['memory_after']
    memory = _read_memory_tree(str(cycle / 'memory'))
    active = _read_memory_tree(str(root / 'active_memory'))
    require(before == state['memory_manifest'] == _manifest(memory) == _manifest(active)
            and state['memory_tree_sha256'] == _memory_tree_sha256(memory)
            == plan['committed_memory_tree_sha256'], 'committed memory changed')
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    ready = [e['payload'] for e in events if e['event'] == 'EVOLUTION_READY_FOR_TARGET']
    require(len(ready) == 1 and ready[0]['payload']['projects'] == count
            and ready[0]['payload']['memory_tree_sha256'] == _memory_tree_sha256(memory)
            and ready[0]['payload']['curriculum_decision_sha256'] == hashlib.sha256(decision.encode()).hexdigest()
            and not any(ready[0]['state'].values()), 'READY publication barrier changed')
    require(not any(e['event'] in ('TARGET_VERIFIED', 'TARGET_LEARNED')
                    and e['payload'].get('target_cycle') == 2 for e in events),
            'failed target already produced terminal evidence')
    turn = cycle / f'curriculum/turn_{count + 1:03d}'
    require(sorted(p.name for p in turn.iterdir() if p.is_dir()) == ['segment_000'],
            'READY checkpoint segment changed')
    history = read(turn / 'segment_000/transcript.json')['messages']
    require(len(history) == plan['curriculum_messages'] and len(history) % 2 == 0
            and all(m.get('role') == ('user' if i % 2 == 0 else 'assistant')
                    for i, m in enumerate(history)), 'saved Curriculum pairs incomplete')
    notes = recover_appended_notes(turn / 'segment_000/iter_02/program.py',
                                  turn / 'segment_000/iter_02/trace.txt')
    require(hashlib.sha256(notes.encode()).hexdigest() == plan['curriculum_notes_sha256'],
            'saved private notes changed')
    return cls(Path(plan_path), root, cycle, target, learning, trigger, records[-1],
               history, notes, (cycle / f'episodes/ep{count:03d}/outcome.md').read_text(),
               configs['curriculum'].max_iters, configs['curriculum'].wall_clock_secs,
               {}, '', projects=count, project_records=records,
               completed_curriculum_turns=count + 1, curriculum_memory_access='none',
               ready_target_cycles=2)
