#!/usr/bin/env python3
"""Recover a final, uncommitted parallel practice wave from durable artifacts.

This intentionally supports only a stopped wave that will reach the existing
project ceiling when closed. It never chooses projects or retries from grades.
Completed reports and Actor conversations are reused. Missing candidate capture
may be retried from the same fixture only before any verification; an interrupted
Verifier may continue only if all its attempted programs failed before execution.
The previous infrastructure result remains archived, and memory is written only
by the normal continuing Actor distillation/reconciliation phases.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def load(path):
    return json.loads(Path(path).read_text())


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def actor_history(episode):
    paths = sorted((episode / 'actor').glob('attempt_*/segment_*/transcript.json'))
    require(bool(paths), f'Missing Actor transcript: {episode}')
    history = load(paths[-1])['messages']
    require(bool(history) and len(history) % 2 == 0,
            f'Incomplete Actor conversation: {episode}')
    for index, message in enumerate(history):
        require(message.get('role') == ('user' if index % 2 == 0 else 'assistant'),
                f'Invalid Actor conversation roles: {episode}')
    return history


def classify_branch(episode):
    """Recovery selection depends on durable boundaries, never verdict quality."""
    from core.verifier import _parse_agentic_verifier_report
    candidate = episode / 'candidates/cycle_001/materials.tgz'
    report = episode / 'handoffs/verifier_001.md'
    require(not (episode / 'outcome.json').exists(), 'Wave already partly committed')
    require(not (episode / 'memory_distillation').exists(), 'Memory phase already began')
    require(not (episode / 'memory_reconciliation').exists(), 'Memory phase already began')
    if report.exists():
        require(candidate.is_file(), 'Graded candidate is missing')
        verdict = _parse_agentic_verifier_report(report.read_text())
        require(verdict in {'pass', 'wrong'}, 'Saved Verifier report is not terminal')
        return 'reuse_verdict'
    if candidate.exists():
        traces = list((episode / 'verifier').rglob('trace_meta.json'))
        for trace in traces:
            meta = load(trace)
            require(meta.get('infra_fail') is True and meta.get('secs') == 0,
                    'Cannot restore a Verifier whose machine effects are unarchived')
        require(not list((episode / 'verifier').rglob('look.json')),
                'Verifier Look state requires a separate recovery audit')
        require(not list((episode / 'verifier').rglob('verifier_report.md')),
                'Do not replace an already published report')
        return 'resume_verifier'
    require(not (episode / 'verifier').exists(), 'Missing candidate has Verifier evidence')
    require(not (episode / 'handoffs').exists(), 'Missing candidate has committed handoffs')
    require(not any(p.is_file() for p in (episode / 'candidates').rglob('*')),
            'Ambiguous partial candidate capture')
    return 'retry_capture_lost_actor'


def validate_archive(directory):
    from explore.provisioning import _inventory_from_tgz, read_manifest, read_symlinks
    manifest, links = _inventory_from_tgz((directory / 'materials.tgz').read_bytes())
    require(bool(manifest) and manifest == read_manifest(str(directory)),
            f'Archive manifest mismatch: {directory}')
    require(links == read_symlinks(str(directory)), f'Archive symlink mismatch: {directory}')


def next_recovery_dir(root):
    """Preserve failed recovery attempts; never replay an open or completed one."""
    index = 1
    while True:
        path = root / f'infrastructure_recovery_{index:03d}'
        if not path.exists() and not path.is_symlink():
            return path
        require(path.is_dir() and not path.is_symlink(), 'Unsafe recovery archive')
        require(not (path / 'completion.json').exists(), 'A recovery already completed')
        require((path / 'failure.json').is_file(), 'Previous recovery is still open')
        require(load(path / 'failure.json').get('status') == 'infra',
                'Previous recovery did not stop at an infrastructure boundary')
        index += 1


def build_plan(protocol_path):
    import benchmarks.osworld.phase1 as runner
    runner._install_paths()
    from explore import phase1_wave as wave
    from explore.practice_loop import _manifest, _read_memory_tree

    protocol = load(protocol_path)
    phase = protocol['phase1']
    root = runner._safe_phase_root(protocol['run_name'])
    require((root.parent / 'protocol.json').read_bytes() == protocol_path.read_bytes(),
            'Protocol changed after launch')
    require(not any((root.parent / name).exists() for name in ('phase2', 'phase3')),
            'Later phase artifacts already exist')
    manifest, state, result = (load(root / name) for name in
                               ('manifest.json', 'state.json', 'result.json'))
    require(state['status'] == result['status'] == 'infra', 'Lineage is not infrastructure-stopped')
    require(phase['parallel_waves'] is True and manifest['parallel_waves'] is True,
            'Not a parallel wave lineage')
    configs, paths = runner._load_configs(SimpleNamespace(**{
        name: phase[name] for name in ('actor_config', 'verifier_config',
        'verifier_control_config', 'curriculum_config', 'memory_config')}))
    for role, config in configs.items():
        old = manifest['configs'][role]
        require(digest(paths[role]) == old['sha256'], f'Changed {role} config bytes')
        require(json.loads(json.dumps(dataclasses.asdict(config))) == old['effective'],
                f'Changed {role} effective config')
    distribution_path = runner._resolve_input_path(phase['distribution_file'])
    target = distribution_path.read_text().strip()
    require(digest(distribution_path) == manifest['distribution_sha256'], 'Distribution drift')
    require(hashlib.sha256(target.encode()).hexdigest() == state['target_sha256'], 'Target drift')
    require(digest(runner.DEFAULT_CORPUS) == manifest['corpus_sha256'], 'Corpus drift')
    require(result['official_evaluator_calls'] == 0, 'Unexpected evaluator call')
    count = state['projects']
    require(count == result['projects'] and count > 0, 'Inconsistent project boundary')
    require(phase['project_budget'] == state['project_budget'] == result['project_budget'], 'Budget drift')
    require(phase['parallelism'] == state['max_parallel'] == manifest['max_parallel'], 'Parallelism drift')
    require(phase['checkpoints'] == state['checkpoint_projects'] == result['checkpoints'], 'Checkpoint drift')
    wave_number = state['waves']
    wave_dir = root / 'waves' / f'wave_{wave_number:03d}'
    require(not (wave_dir / 'outcomes.md').exists(), 'Stopped wave already closed')
    decision = wave.parse_wave_handoff((wave_dir / 'curriculum_decision.json').read_text())
    require(decision is not None and decision.decision == 'WAVE', 'Missing committed wave decision')
    require(count < phase['project_budget'] <= count + len(decision.projects),
            'Recovery supports only the final budget-crossing wave')
    memory = _manifest(_read_memory_tree(str(root / 'memory')))
    require(memory == state['memory_manifest'] == result['memory_manifest'], 'Canonical memory drift')
    require(memory == _manifest(_read_memory_tree(str(root / 'memory_frozen'))), 'Frozen memory drift')
    require(memory == _manifest(_read_memory_tree(str(wave_dir / 'memory_snapshot'))), 'Wave snapshot drift')
    completed = sorted((root / 'episodes').glob('ep*/outcome.json'))
    require([p.parent.name for p in completed] == [f'ep{i:03d}' for i in range(1, count + 1)],
            'Committed project ledger is not contiguous')
    require(load(completed[-1])['memory_after'] == memory, 'Last committed memory differs')
    assignments, rows, inputs = [], [], {}
    for offset, project in enumerate(decision.projects, 1):
        index = count + offset
        episode = root / 'episodes' / f'ep{index:03d}'
        require((episode / 'project.md').read_text() == project.instruction, 'Project instruction drift')
        validate_archive(episode / 'fixtures')
        mode = classify_branch(episode)
        actor_history(episode)
        if mode != 'retry_capture_lost_actor':
            validate_archive(episode / 'candidates/cycle_001')
            require((episode / 'handoffs/actor_001.md').is_file(), 'Missing Actor handoff')
        else:
            require(state['reason'].startswith('AbsoluteLinkError:'),
                    'Uncaptured Actor recovery requires the diagnosed archive fault')
        assignments.append((index, project, episode, mode))
        rows.append({'project': index, 'project_id': project.project_id, 'recovery': mode})
        for path in episode.rglob('*'):
            if path.is_file() and (path.name in {'transcript.json', 'project.md'}
                                  or 'fixtures' in path.parts or 'candidates' in path.parts
                                  or 'handoffs' in path.parts):
                inputs[str(path)] = digest(path)
    plan = {'time': datetime.now(timezone.utc).isoformat(), 'root': str(root),
            'wave': wave_number, 'committed_projects': count, 'branches': rows,
            'budget': phase['project_budget'], 'complete_wave_total': count + len(rows),
            'memory_before': memory, 'input_sha256': inputs,
            'script_sha256': digest(__file__), 'official_evaluator_calls': 0}
    return SimpleNamespace(root=root, protocol=protocol, configs=configs, state=state,
                           result=result, target=target, decision=decision, wave_dir=wave_dir,
                           assignments=assignments, plan=plan, runner=runner)


def restore_branch(context, assignment, factory, hooks):
    from explore import phase1_wave as wave
    from explore.practice_loop import _read_memory_tree, _replay, _push_canonical_memory, _verify_candidate
    from core.verifier import VerifierSession, verify_agentic, _agentic_substantive_checkpoint, _parse_agentic_verifier_report
    from core.trace import ArtifactSink
    index, project, episode, mode = assignment
    snapshot = context.wave_dir / 'memory_snapshot'
    if mode == 'retry_capture_lost_actor':
        archive = episode / 'infrastructure_attempts/capture_failed_001'
        archive.mkdir(parents=True, exist_ok=False)
        (episode / 'actor').rename(archive / 'actor')
        return wave._execute_branch(
            vm_factory=factory, hooks=hooks, project_index=index, project=project,
            episode_dir=episode, wave_memory_dir=snapshot,
            actor_cfg=context.configs['actor'], verifier_control_cfg=context.configs['verifier_control'],
            target_direction=context.target)
    desktop, vm = factory()
    try:
        candidate = episode / 'candidates/cycle_001'
        _replay(hooks, vm, str(candidate))
        report_path = episode / 'handoffs/verifier_001.md'
        if mode == 'resume_verifier':
            session = VerifierSession()
            transcripts = sorted((episode / 'verifier').rglob('transcript.json'))
            if transcripts:
                transcript = transcripts[-1]
                history, observation, images, turn = _agentic_substantive_checkpoint(
                    [], load(transcript)['messages'])
                session[:] = history
                trace_paths = sorted(transcript.parent.glob('iter_*/trace.txt'))
                session.pending_observation = observation or (trace_paths[-1].read_text() if trace_paths else '')
                session.pending_images, session.pending_turn = images, turn
            control = wave._branch_control_config(context.configs['verifier_control'])
            try:
                verdict, report = verify_agentic(
                    project.instruction, vm, control,
                    sink=ArtifactSink(str(getattr(context, 'audit_dir', episode)
                                          / f'verifier_resume_ep{index:03d}')), turn_no=index,
                    context=(f'The candidate project is {wave.PROJECT_ROOT}. Actor-private '
                             'memory, handoff prose, reasoning, and execution logs are '
                             'hidden by the harness. Investigate the actual candidate independently.'),
                    session=session, wall_budget=control.wall_clock_secs)
            finally:
                session.close_executor()
            require(verdict in {'pass', 'wrong'}, f'Project {index} Verifier incomplete: {report}')
            wave._atomic_text(report_path, str(report))
            _verify_candidate(hooks, vm, str(candidate))
            wave._fresh_vm(hooks, vm, context.target)
            _replay(hooks, vm, str(candidate))
        report = report_path.read_text()
        verdict = _parse_agentic_verifier_report(report)
        _push_canonical_memory(hooks, vm, str(snapshot))
        return wave._BranchRuntime(
            project_index=index, project=project, episode_dir=episode,
            desktop=desktop, vm=vm, actor_history=actor_history(episode),
            actor_handoff=(episode / 'handoffs/actor_001.md').read_text(),
            verifier_report=report, terminal_outcome='PASS' if verdict == 'pass' else 'FAIL',
            wave_memory=_read_memory_tree(str(snapshot)))
    except BaseException:
        desktop.close()
        raise


def execute(context):
    from explore import phase1_wave as wave
    from explore.practice_loop import PracticeHooks, _manifest, _read_memory_tree
    from explore.target_learning import _memory_tree_sha256
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    from desktop_env.desktop_env import DesktopEnv
    from env.vm import VM
    root, runner = context.root, context.runner
    prepare_checkpointable_docker_provider(context.configs['verifier_control'].verifier_execution_mode)
    audit = next_recovery_dir(root)
    audit.mkdir(exist_ok=False)
    context.audit_dir = audit
    for name in ('state.json', 'result.json', 'manifest.json'):
        shutil.copy2(root / name, audit / ('before_' + name))
    wave._atomic_json(audit / 'plan.json', context.plan)
    hooks = PracticeHooks()

    def event(name, **payload):
        runner._append_event(root / 'events.jsonl', name, status='in_progress',
                             state={'project_open': False, 'memory_phase_open': False,
                                    'recovery_open': True}, payload=payload)
        print(json.dumps({'event': name, **payload}), flush=True)

    def factory():
        desktop = DesktopEnv(provider_name='docker', action_space='pyautogui', os_type='Ubuntu',
                             screen_size=(1920, 1080), headless=True, require_a11y_tree=False, volume_size=60,
                             cache_dir=os.environ.get('RSIAGENT_OSWORLD_CACHE_DIR', 'cache'))
        try:
            desktop.reset(task_config=None)
            return desktop, VM(desktop)
        except BaseException:
            desktop.close()
            raise

    active, completed, errors = [], {}, []
    event('PHASE1_WAVE_RECOVERY_STARTED', wave=context.plan['wave'], branches=context.plan['branches'])
    try:
        with ThreadPoolExecutor(max_workers=context.protocol['phase1']['parallelism']) as pool:
            futures = {pool.submit(restore_branch, context, item, factory, hooks): item[0]
                       for item in context.assignments}
            for future in as_completed(futures):
                try:
                    branch = future.result()
                    active.append(branch)
                    completed[branch.project_index] = branch
                    event('PHASE1_RECOVERY_BRANCH_READY', project=branch.project_index,
                          terminal_outcome=branch.terminal_outcome)
                except Exception as exc:
                    errors.append(f'project {futures[future]}: {type(exc).__name__}: {exc}')
        require(not errors, '\n'.join(errors))
        rendered = []
        for index, project, episode, mode in context.assignments:
            branch = completed[index]
            memory = wave._commit_branch_memory(
                branch=branch, hooks=hooks, memory_cfg=context.configs['memory_actor'],
                canonical_memory_dir=root / 'memory', journal_dir=root / 'memory_journal',
                corpus_path=str(runner.DEFAULT_CORPUS), target_direction=context.target)
            record = {'schema_version': 2, 'wave_index': context.plan['wave'],
                      'project_index': index, 'project_id': project.project_id,
                      'project': project.instruction, 'terminal_outcome': branch.terminal_outcome,
                      'verification_cycles': 1, 'actor_handoffs': [branch.actor_handoff],
                      'verifier_reports': [branch.verifier_report],
                      'wave_memory_snapshot': _manifest(branch.wave_memory),
                      'memory_before': memory['before'], 'memory_after': memory['after'],
                      'memory_changes': memory['changes'],
                      'infrastructure_recovery': {'plan': str(audit / 'plan.json'), 'mode': mode}}
            wave._atomic_json(episode / 'outcome.json', record)
            text = wave._outcome_text(record) + '\n\nMEMORY CHANGES:\n' + json.dumps(memory['changes'], indent=2)
            wave._atomic_text(episode / 'outcome.md', text)
            rendered.append(text)
            if index in context.protocol['phase1']['checkpoints']:
                hooks.install_memory(str(root / 'checkpoints' / f'project_{index:03d}' / 'memory'),
                                     _read_memory_tree(str(root / 'memory')))
            branch.desktop.close()
            active.remove(branch)
            event('PHASE1_BRANCH_MEMORY_COMMITTED', wave=context.plan['wave'], project=index,
                  project_id=project.project_id, terminal_outcome=branch.terminal_outcome)
        wave._atomic_text(context.wave_dir / 'outcomes.md',
                          f"WAVE {context.plan['wave']} CURRICULUM RATIONALE:\n{context.decision.rationale}\n\n"
                          + '\n\n---\n\n'.join(rendered))
        memory = _read_memory_tree(str(root / 'memory'))
        hooks.install_memory(str(root / 'memory_frozen'), memory)
        total = context.plan['complete_wave_total']
        require(len(list((root / 'episodes').glob('ep*/outcome.json'))) == total,
                'Incomplete final project ledger')
        budget = context.plan.get('budget', total)
        status = 'budget_exhausted' if total >= budget else 'infra'
        reason = ('external complete-wave compute boundary reached' if status == 'budget_exhausted'
                  else 'recovered complete wave; persistent Curriculum continuation required')
        state = {**context.state, 'status': status, 'projects': total,
                 'last_project': total, 'reason': reason, 'memory_manifest': _manifest(memory),
                 'infrastructure_recovery': str(audit / 'plan.json')}
        result = {**context.result, 'status': status, 'projects': total,
                  'stop_reason': reason, 'memory_manifest': _manifest(memory),
                  'memory_files': len(memory), 'memory_bytes': sum(map(len, memory.values())),
                  'memory_tree_sha256': _memory_tree_sha256(memory),
                  'infrastructure_recovery': str(audit / 'plan.json')}
        wave._atomic_json(root / 'state.json', state)
        wave._atomic_json(root / 'result.json', result)
        event('PHASE1_WAVE_COMPLETED', wave=context.plan['wave'], total_projects=total,
              recovered=True)
        wave._atomic_json(audit / 'completion.json', {'status': 'complete', 'result_sha256': digest(root / 'result.json')})
    except BaseException as exc:
        wave._atomic_json(audit / 'failure.json', {'status': 'infra', 'reason': f'{type(exc).__name__}: {exc}'})
        raise
    finally:
        for branch in active:
            try:
                branch.desktop.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    protocol_path = args.protocol.resolve(strict=True)
    context = build_plan(protocol_path)
    print(json.dumps({k: v for k, v in context.plan.items() if k not in {'input_sha256', 'memory_before'}}, indent=2), flush=True)
    if args.execute:
        guard = (context.root / '.parallel_recovery.lock').open('a')
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        execute(context)
        # Let the ordinary protocol wrapper validate the now-complete Phase 1
        # and record its completion event; it reuses the result without work.
        subprocess.run([
            sys.executable, '-m', 'benchmarks.osworld.pipeline',
            '--protocol', str(protocol_path), '--phase', 'phase1',
            '--execute', 'RUN-RECURSIVE-IMPROVEMENT-PHASE1'], cwd=REPO, check=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    main()
