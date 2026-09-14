"""Preserve the practice Actor/Verifier identities across an evidence request."""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import time
import tarfile
from pathlib import Path

from explore import target_learning as V
from explore.practice_loop import _manifest, _atomic_json, _safe_memory_name
from explore.phase2_recovery import read, require, sha


def paired(messages):
    return bool(messages) and len(messages) % 2 == 0 and all(
        m.get('role') == ('user' if i % 2 == 0 else 'assistant')
        for i, m in enumerate(messages))


def actor_leads(history, project, cfg):
    """Transport only the Actor's final reproducible Done checks, never its history."""
    from core.actor import Done, parse_turn
    from core.checks import validate
    from core.loop import _actor_evidence_leads
    turn = parse_turn(history[-1].get('content', '')) if history else None
    if not isinstance(turn, Done):
        return ''
    accepted, _, _ = validate(turn.checks, cfg.max_checks, instruction=project,
                             witness_gates=getattr(cfg, 'done_witness_gates', False))
    return _actor_evidence_leads(accepted, [])


def run_practice_with_evidence(*, hooks, vm, lineage, episode_dir, project_index,
                              project, fixture_dir, target, actor_cfg, verifier_cfg,
                              memory_dir, emit, agentic_verifier_cfg,
                              target_visible_inputs=None, resume_state=None):
    if resume_state is not None and resume_state.get('resume_kind') == 'verified_memory':
        from explore.practice_memory_recovery import restore_learning_candidate
        return restore_learning_candidate(
            hooks=hooks, vm=vm, episode_dir=episode_dir, fixture_dir=fixture_dir,
            memory_dir=memory_dir, target=target, emit=emit, saved=resume_state)
    from config.settings import load
    from core.trace import ArtifactSink
    from core.verifier import (VerifierSession, _parse_agentic_verifier_report,
                               unverified_message, verify_agentic)

    require(agentic_verifier_cfg is not None and
            agentic_verifier_cfg.agentic_verifier_config, 'missing Agentic Verifier config')
    before_memory = V._read_memory_tree(str(memory_dir))
    audit_target = V._target_audit_surface(target, target_visible_inputs)
    saved = resume_state or {}
    history = list(saved.get('actor_history', []))
    session = VerifierSession(saved.get('verifier_history', []))
    session.inspections = session.candidate_generation = int(saved.get('inspections', 0))
    findings = saved.get('findings', '')
    candidate = Path(saved['candidate_dir']) if saved else None
    publication = int(saved.get('publication', 0))
    actor_used = int(saved.get('prior_actor_iters', 0))
    actor_wall = float(saved.get('prior_actor_wall_secs', 0))
    verifier_used = int(saved.get('prior_verifier_iters', 0))
    verifier_wall = float(saved.get('prior_verifier_wall_secs', 0))
    verifier_role = load(agentic_verifier_cfg.agentic_verifier_config)
    verifier_wall_cap = min(verifier_role.wall_clock_secs,
                            agentic_verifier_cfg.wall_clock_secs)
    private_paths = tuple(dict.fromkeys(
        tuple(getattr(agentic_verifier_cfg, 'verifier_private_paths', ()) or ())
        + (V.ACTOR_HANDOFF, V.ACTOR_EXECUTION_EVIDENCE)))
    control = dataclasses.replace(
        agentic_verifier_cfg, verifier_evolve_route=False,
        verifier_local_verdict_only=False, verifier_hide_actor_memory=True,
        verifier_stage_lifecycle=False, verifier_persist_scratch=False,
        verifier_private_paths=private_paths)
    context = (f'The candidate project is {V.PROJECT_ROOT}. The frozen original '
               f'input fixture is {V.PRACTICE_FIXTURE_EVIDENCE}; use it only as '
               'pre-candidate evidence. Actor-private memory, handoff prose, and '
               'execution logs are hidden by the harness.')
    if saved:
        context += (' This inspection continues the same Verifier conversation in '
                    'a fresh verification VM. Previous private scratch files are absent; '
                    're-establish probes against the current candidate and original fixtures.')
    try:
        while True:
            require(actor_used < actor_cfg.max_iters and actor_wall < actor_cfg.wall_clock_secs,
                    'practice Actor remaining budget exhausted')
            started = time.monotonic()
            V._fresh_vm(hooks, vm, target)
            V._replay(hooks, vm, str(fixture_dir))
            if candidate is not None:
                V._replay(hooks, vm, str(candidate))
            V._push_canonical_memory(hooks, vm, str(memory_dir))
            prompt = V.self_evolving_actor_charter(
                project, V._listing(before_memory), V.ACTOR_HANDOFF) + V._actor_runtime_contract()
            evidence_requested = bool(findings)
            if evidence_requested:
                prompt += '\n\n' + unverified_message(findings, evidence_request=True)
            emit('ACTOR_EVIDENCE_REQUESTED' if evidence_requested else 'ACTOR_PHASE_STARTED',
                 status='in_progress', project_open=True, memory_phase_open=False,
                 payload={'project_index': project_index, 'actor_attempt': 1,
                          'verification_cycle': session.inspections,
                          'verifier_report_sha256': hashlib.sha256(findings.encode()).hexdigest()})
            actor_root = episode_dir / 'actor/attempt_001'
            before_turns = len(list(actor_root.glob('segment_*/iter_*/turn.txt')))
            remaining_wall = actor_cfg.wall_clock_secs - actor_wall - (time.monotonic() - started)
            require(remaining_wall > 0, 'Actor setup exhausted remaining budget')
            cfg = dataclasses.replace(actor_cfg, max_iters=actor_cfg.max_iters - actor_used,
                                      wall_clock_secs=remaining_wall)
            decision, history, result = V._run_handoff_phase(
                hooks=hooks, vm=vm, cfg=cfg, prompt=prompt, role='ACTOR',
                handoff_path=V.ACTOR_HANDOFF, token_pattern=V._ACTOR_TOKEN,
                sink_root=str(actor_root), target=audit_target,
                history=history, continuation=bool(history), bounded_transport=True)
            actor_used += max(1, len(list(actor_root.glob('segment_*/iter_*/turn.txt'))) - before_turns,
                              int(getattr(result, 'turns', 0) or getattr(result, 'iters', 0)))
            actor_wall += time.monotonic() - started
            publication += 1
            candidate = episode_dir / 'candidate' / f'publication_{publication:03d}'
            require(not candidate.exists(), 'candidate publication already exists')
            V._require_owned_project_shape(vm)
            V._capture_owned_tree(hooks, vm, f'ep{project_index:03d}-candidate-{publication:03d}',
                                  str(candidate), audit_target)
            handoff_path = episode_dir / ('actor_handoff.md' if publication == 1 else
                                         f'actor_handoff_{publication:03d}.md')
            require(not handoff_path.exists(), 'Actor handoff already exists')
            V._atomic_text(handoff_path, decision.text)
            evidence = V._build_actor_execution_evidence(
                episode_dir / 'actor', 1,
                episode_dir / 'actor_execution_evidence' / f'publication_{publication:03d}')
            require(verifier_used < verifier_role.max_iters and
                    verifier_wall < verifier_wall_cap,
                    'practice Verifier remaining budget exhausted')
            started = time.monotonic()
            V._fresh_vm(hooks, vm, target)
            V._replay(hooks, vm, str(fixture_dir))
            V._stage_original_practice_fixtures(vm)
            V._replay(hooks, vm, str(candidate))
            emit('VERIFIER_PHASE_STARTED', status='in_progress', project_open=True,
                 memory_phase_open=False, payload={'project_index': project_index,
                 'verification_cycle': session.inspections + 1,
                 'actor_execution_manifest_sha256': evidence['manifest_sha256']})
            verifier_root = episode_dir / 'verifier'
            before_turns = len(list(verifier_root.glob('verifier_agent/inspection_*/segment_*/iter_*/turn.txt')))
            remaining_wall = verifier_wall_cap - verifier_wall - (time.monotonic() - started)
            require(remaining_wall > 0, 'Verifier setup exhausted remaining budget')
            verdict, report = verify_agentic(
                project, vm, control, sink=ArtifactSink(str(verifier_root)),
                turn_no=project_index, context=context, session=session,
                wall_budget=remaining_wall, iters_budget=verifier_role.max_iters-verifier_used,
                actor_evidence_leads=actor_leads(history, project, actor_cfg) if evidence_requested else '')
            verifier_wall += time.monotonic() - started
            verifier_used += max(1, len(list(verifier_root.glob(
                'verifier_agent/inspection_*/segment_*/iter_*/turn.txt'))) - before_turns)
            V._verify_candidate(hooks, vm, str(candidate))
            V._verify_original_practice_fixtures(hooks, vm, fixture_dir)
            require(_manifest(before_memory) == _manifest(
                V._read_memory_tree(str(memory_dir))), 'memory changed during unverified practice')
            _atomic_json(episode_dir / f'evidence_cycle_{publication:03d}.json', {
                'verdict': verdict, 'actor_iters_charged': actor_used,
                'actor_wall_secs_charged': actor_wall, 'verifier_iters_charged': verifier_used,
                'verifier_wall_secs_charged': verifier_wall,
                'actor_memory_unchanged': _manifest(before_memory) ==
                    _manifest(V._read_memory_tree(str(memory_dir)))})
            if verdict == 'unverified':
                require(_parse_agentic_verifier_report(str(report), allow_unverified=True) == 'unverified',
                        'Verifier infrastructure absence is not an evidence request')
                findings = str(report)
                V._atomic_text(episode_dir / f'unverified_report_{publication:03d}.md', findings)
                continue
            require(verdict in {'pass', 'wrong'}, 'Verifier did not publish a binary task verdict')
            V._push_actor_execution_evidence(hooks, vm, evidence)
            V._verify_actor_execution_evidence(vm, evidence)
            V._atomic_text(episode_dir / 'verifier_report.md', str(report))
            V._fresh_vm(hooks, vm, target)
            V._replay(hooks, vm, str(candidate))
            V._push_canonical_memory(hooks, vm, str(memory_dir))
            return {'terminal_outcome': 'PASS' if verdict == 'pass' else 'FAIL',
                    'verifier_report': str(report), 'actor_handoff': decision.text,
                    'actor_history': history, 'before_memory': before_memory}
    finally:
        session.close_executor()


def admit_practice_recovery(cls, plan_path, root, configs, initial_memory, target, stop_policy):
    """Admit a preserved first-cycle evidence request after zero or one practices."""
    from core.verifier import _parse_agentic_verifier_report
    from explore.practice_loop import _parse_handoff, _UNIFIED_CURRICULUM_TOKEN
    plan = read(plan_path)
    completed = plan.get('completed_projects', 1)
    require(type(completed) is int and completed in {0, 1}, 'unsupported practice boundary')
    pending_index = completed + 1
    require(plan['status'] == 'admitted_for_isolated_recovery' and stop_policy == 'curriculum_review',
            'recovery authorization/policy changed')
    require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
    for path, expected in plan['input_sha256'].items():
        require(sha(path) == expected, 'archived practice input changed: ' + path)
    require(not (root / 'result.json').exists(), 'attempt already completed')
    require(sorted(p.name for p in (root/'target_cycles').iterdir()) == ['cycle_001'] and
            sorted(p.name for p in (root/'evolution_cycles').iterdir()) == ['cycle_001'], 'cycle count changed')
    cycle = root/'evolution_cycles/cycle_001'
    require(sorted(p.name for p in (cycle/'episodes').iterdir()) ==
            [f'ep{i:03d}' for i in range(1, pending_index + 1)] and
            sorted(p.name for p in (cycle/'curriculum').iterdir()) ==
            [f'turn_{i:03d}' for i in range(1, pending_index + 1)],
            'practice/Curriculum boundary changed')
    episode = cycle/f'episodes/ep{pending_index:03d}'
    require(not any((episode/name).exists() for name in
                    ('outcome.json','verifier_report.md','memory_distillation','memory_reconciliation','learning_diagnosis')),
            'open practice already reached learning or a terminal verdict')
    for p in root.rglob('boundary_audit.json'):
        r = read(p)
        require(r.get('status') != 'quarantined' and not r.get('hits'), 'quarantined lineage')
    require(not list(root.rglob('audit_rejects.jsonl')), 'rejected lineage needs separate review')
    manifest = read(root/'manifest.json')
    require(manifest.get('protocol', {}).get('curriculum_memory_access', 'read_only') ==
            plan.get('curriculum_memory_access', 'read_only'), 'Curriculum memory access changed')
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest() and
            manifest['initial_memory']['manifest'] == _manifest(initial_memory), 'target/initial memory changed')
    for role,cfg in configs.items():
        entry = manifest['configs'][role]
        require(sha(entry['path']) == entry['sha256'] and entry['effective'] ==
                json.loads(json.dumps(dataclasses.asdict(cfg))), 'role configuration changed')
    learning = read(root/'target_cycles/cycle_001/terminal_learning/outcome.json')
    records = [read(cycle/f'episodes/ep{i:03d}/outcome.json') for i in range(1, completed + 1)]
    project = records[-1] if records else learning
    trigger = read(cycle/'trigger/outcome.json')
    state = read(cycle/'state.json')
    require(state['status'] == 'infra' and state['projects'] == pending_index
            and state['learning_experiences'] == completed
            and state['reason'].startswith('practice Agentic Verifier ended without PASS/FAIL:'),
            'not the recorded UNVERIFIED contract failure')
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] in ('PASS','FAIL')
            and all(p['project_index'] == i and p['terminal_outcome'] in ('PASS','FAIL')
                    for i, p in enumerate(records, 1)),
            'completed learning has no grounded verdict')
    require(learning['memory_before'] == _manifest(initial_memory) and
            learning['memory_after'] == trigger['memory_before'] and
            (not records or trigger['memory_before'] == records[0]['memory_before']),
            'target/practice memory chain changed')
    # The failed evolve wrapper installed the last committed practice memory
    # into active_memory before raising. Recover its original trigger input from
    # the immutable target-learning journal, without changing either live tree.
    require(project['memory_after'] == _manifest(V._read_memory_tree(str(root/'active_memory'))),
            'failed evolution did not preserve the last complete practice memory')
    with tarfile.open(root/'memory_journal/ep001.tgz') as archive:
        members = archive.getmembers()
        require(all(m.isfile() and _safe_memory_name(m.name) for m in members) and
                len({m.name for m in members}) == len(members), 'unsafe target-learning journal')
        trigger_memory = {m.name: archive.extractfile(m).read() for m in members}
    require(_manifest(trigger_memory) == trigger['memory_before'], 'target-learning journal changed')
    require(project['memory_after'] == state['memory_manifest'] ==
            _manifest(V._read_memory_tree(str(cycle/'memory'))) and state['memory_tree_sha256'] ==
            V._memory_tree_sha256(V._read_memory_tree(str(cycle/'memory'))), 'committed memory changed')
    require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol' and
            trigger['verifier_report'] == learning['verifier_report'] and
            trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'], 'trigger changed')
    events=[json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
    require(events[-1]['event'] == 'EVOLUTION_VERIFIER_PHASE_STARTED' and
            events[-1]['payload']['payload']['project_index'] == pending_index and
            events[-1]['payload']['state']['memory_phase_open'] is False, 'a later transition exists')
    verification=read(episode/f'verifier/iter_{pending_index:02d}/verify2.json')
    require(verification['verdict'] == 'unverified' and _parse_agentic_verifier_report(
        verification['findings'],allow_unverified=True) == 'unverified', 'no valid evidence request')
    source=Path(plan['source_transcript'])
    require(source == sorted((cycle/f'curriculum/turn_{pending_index:03d}').glob('segment_*/transcript.json'))[-1],
            'not latest Curriculum checkpoint')
    history=read(source)['messages']
    actor_source=sorted((episode/'actor/attempt_001').glob('segment_*/transcript.json'))[-1]
    actor_history=read(actor_source)['messages']
    require(paired(history) and paired(actor_history) and paired(verification['transcript']), 'incomplete role history')
    # Parse only literal constants from the last successful Curriculum publication;
    # never execute the saved Agent Program or infer its private notes from prose.
    program=Path(plan['curriculum_publication_program'])
    nodes=ast.parse(program.read_text()).body
    literals={n.targets[0].id:ast.literal_eval(n.value) for n in nodes
              if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)
              and n.targets[0].id in ('notes','handoff') and isinstance(n.value,ast.Constant)}
    require(isinstance(literals.get('notes'),str) and literals.get('handoff') ==
            (episode/'project.md').read_text(), 'exact published Curriculum notes/handoff unavailable')
    trace = (program.parent/'trace.txt').read_text()
    # Accept the first-practice publication's exact UTF-8 byte-count witness
    # only alongside a direct top-level write of the literal private notes.
    notes_write = ast.dump(ast.parse(
        "open('/home/user/curriculum_notes.md', 'w').write(notes)").body[0])
    direct_write = any(ast.dump(n) == notes_write for n in nodes)
    byte_witness = f"curriculum_notes.md: {len(literals['notes'].encode('utf-8'))} bytes"
    require(read(program.parent/'trace_meta.json')['exit_code'] == 0 and
            ('notes exists: True' in trace or (direct_write and byte_witness in trace)),
            'notes were not published')
    saved_project = _parse_handoff(literals['handoff'], _UNIFIED_CURRICULUM_TOKEN)
    require(saved_project is not None and saved_project.token == 'PROJECT', 'invalid saved project')
    candidate=episode/'candidate/publication_001'
    require(sorted(p.name for p in (episode/'candidate').iterdir()) == ['publication_001'],
            'candidate generation changed')
    budget=plan['prior_role_budget']
    require(budget['prior_actor_iters'] == len(list((episode/'actor').rglob('turn.txt'))) and
            budget['prior_verifier_iters'] == len(list((episode/'verifier').rglob('turn.txt'))),
            'saved role iteration count changed')
    for role,kind in [('practice_actor','actor'),('practice_verifier','verifier')]:
        cap = configs[role].wall_clock_secs
        if kind == 'verifier':
            cap = min(cap, configs['target_actor'].wall_clock_secs)
        require(0 < budget[f'prior_{kind}_wall_secs'] < cap and
                0 < budget[f'prior_{kind}_iters'] < configs[role].max_iters, 'remaining role budget exhausted')
    pending=dict(budget,actor_history=actor_history,verifier_history=verification['transcript'],
                 findings=verification['findings'],candidate_dir=str(candidate),publication=1,inspections=1,
                 project=saved_project.body)
    latest = cycle/f'episodes/ep{completed:03d}/outcome.md' if completed else cycle/'trigger/outcome.md'
    return cls(Path(plan_path),root,cycle,target,learning,trigger,project,history,literals['notes'],
               latest.read_text(),configs['curriculum'].max_iters,
               configs['curriculum'].wall_clock_secs,{},'',projects=completed,project_records=records,
               completed_curriculum_turns=pending_index,pending_practice=pending,
               initial_evolution_memory=trigger_memory,
               curriculum_memory_access=plan.get('curriculum_memory_access', 'read_only'))
