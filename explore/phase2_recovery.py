"""Strict admission for a stopped Phase-2 Curriculum search.

This deliberately supports only a completed first target-learning transaction
and consecutive completed practice transactions, followed by an unpublished
Curriculum turn. All role evidence comes from saved artifacts, never host prose.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from explore.charter import self_evolving_curriculum_charter
from explore.e15_loop import (
    CURRICULUM_HANDOFF, CURRICULUM_NOTES, E15InfrastructureError,
    _curriculum_runtime_contract, _manifest, _read_memory_tree,
)
from explore.e15_v12_loop import _memory_tree_sha256


def require(ok, message):
    if not ok:
        raise E15InfrastructureError("Phase-2 resume refused: " + message)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclasses.dataclass
class Phase2CurriculumRecovery:
    plan_path: Path
    root: Path
    cycle: Path
    target: str
    target_learning: dict
    trigger: dict
    project: dict
    history: list
    notes: str
    latest_outcome: str
    remaining_iters: int
    remaining_wall: float
    context_fields: dict
    recovery_observation: str
    projects: int = 1
    project_records: list = dataclasses.field(default_factory=list)
    completed_curriculum_turns: int = 1
    pending_practice: object = None
    initial_evolution_memory: object = None
    curriculum_memory_access: str = 'read_only'
    ready_target_cycles: int = 0
    post_target_learning: bool = False
    pending_memory_learning: object = None

    @classmethod
    def admit(cls, plan_path, root, configs, initial_memory, target, stop_policy):
        plan_path, root = Path(plan_path), Path(root)
        plan = read(plan_path)
        if plan.get('recovery_kind') == 'verified_practice_memory_fs':
            from explore.practice_memory_recovery import admit_memory_recovery
            return admit_memory_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
        if plan.get('recovery_kind') == 'post_target_learning_boot_failure':
            from explore.post_target_learning_recovery import admit_post_target_learning
            return admit_post_target_learning(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
        if plan.get('recovery_kind') == 'ready_target_replacement':
            from explore.ready_target_recovery import admit_ready_target_recovery
            return admit_ready_target_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
        if plan.get('recovery_kind') == 'unverified_practice':
            from explore.practice_evidence_recovery import admit_practice_recovery
            return admit_practice_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
        count = plan.get('completed_projects', 1)
        require(type(count) is int and count >= 1, 'invalid completed project count')
        completed_turns = plan.get('completed_curriculum_turns', count)
        require(type(completed_turns) is int and completed_turns == count,
                'unsupported Curriculum/project boundary')
        require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
        require(stop_policy == 'curriculum_review', 'stop policy changed')
        require(plan['status'] == 'admitted_for_isolated_recovery', 'plan is not admitted')
        for path, expected in plan['input_sha256'].items():
            require(sha(path) == expected, 'an archived input changed: ' + path)
        require(not (root / 'result.json').exists(), 'attempt already completed')
        require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'],
                'target cycle count changed')
        require(sorted(p.name for p in (root / 'evolution_cycles').iterdir()) == ['cycle_001'],
                'evolution cycle count changed')
        cycle = root / 'evolution_cycles/cycle_001'
        require(sorted(p.name for p in (cycle / 'episodes').iterdir()) ==
                [f'ep{i:03d}' for i in range(1, count + 1)],
                'a project is open or a completed project is missing')
        require(sorted(p.name for p in (cycle / 'curriculum').iterdir()) ==
                [f'turn_{i:03d}' for i in range(1, completed_turns + 2)],
                'Curriculum turn count changed')
        for parent in (root, cycle):
            require(not any((parent / name).exists() for name in (
                'result.json', 'curriculum_terminal.md', 'curriculum_ready_for_retry.md',
                'audit_rejects.jsonl')), 'terminal or rejected lineage')
        for path in root.rglob('boundary_audit.json'):
            record = read(path)
            require(record.get('status') != 'quarantined' and not record.get('hits'),
                    'quarantined transcript')
        manifest = read(root / 'manifest.json')
        require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest(),
                'target direction changed')
        require(manifest['initial_memory']['manifest'] == _manifest(initial_memory),
                'initial frozen memory changed')
        for role, cfg in configs.items():
            saved = manifest['configs'][role]
            require(saved['sha256'] == sha(saved['path']), 'role config bytes changed')
            effective = json.loads(json.dumps(dataclasses.asdict(cfg)))
            require(saved['effective'] == effective, 'effective role config changed')
        learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
        projects = [read(cycle / f'episodes/ep{i:03d}/outcome.json')
                    for i in range(1, count + 1)]
        project = projects[-1]
        trigger = read(cycle / 'trigger/outcome.json')
        state = read(cycle / 'state.json')
        require(state['status'] == 'running' and state['projects'] == state['learning_experiences'] == count,
                'not a complete-project boundary')
        require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] in ('PASS', 'FAIL'),
                'no grounded target learning')
        require(all(p['project_index'] == i and p['terminal_outcome'] in ('PASS', 'FAIL')
                    for i, p in enumerate(projects, 1)),
                'no grounded practice learning')
        require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol',
                'trigger identity changed')
        require(trigger['verifier_report'] == learning['verifier_report']
                and trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis']
                and trigger['verifier_outcome'] == learning['target_verifier_verdict'],
                'trigger and target learning disagree')
        require(learning['memory_before'] == _manifest(initial_memory), 'initial memory chain mismatch')
        active = _read_memory_tree(str(root / 'active_memory'))
        memory = _read_memory_tree(str(cycle / 'memory'))
        require(learning['memory_after'] == trigger['memory_before'] == projects[0]['memory_before'] == _manifest(active),
                'target-to-practice memory chain mismatch')
        require(all(projects[i]['memory_before'] == projects[i-1]['memory_after']
                    for i in range(1, count)), 'practice memory chain mismatch')
        require(project['memory_after'] == state['memory_manifest'] == _manifest(memory)
                and state['memory_tree_sha256'] == _memory_tree_sha256(memory),
                'practice memory transaction is incomplete')
        events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
        require(events and events[-1]['event'] == 'EVOLUTION_PROJECT_CLOSED',
                'a project or learning phase is still open')
        closed = events[-1]['payload']
        require(closed['state']['project_open'] is False
                and closed['state']['memory_phase_open'] is False
                and closed['payload']['project_index'] == count
                and closed['payload']['memory_tree_sha256'] == _memory_tree_sha256(memory),
                'last memory-close event disagrees with the committed tree')
        source = Path(plan['source_transcript'])
        turn = cycle / f'curriculum/turn_{completed_turns + 1:03d}'
        transcripts = sorted(turn.glob('segment_*/transcript.json'))
        require(transcripts and source == transcripts[-1], 'checkpoint is not the last completed segment')
        require(sha(source) == plan['source_transcript_sha256'], 'checkpoint changed')
        require(sha(plan['raw_checkpoint']) == sha(source), 'raw archive differs')
        history = read(source)['messages']
        require(history and len(history) % 2 == 0 and all(
            m.get('role') == ('user' if i % 2 == 0 else 'assistant')
            for i, m in enumerate(history)), 'incomplete context pairs')
        require(len(transcripts) >= 3 and all(not list(p.parent.glob('iter_*/trace.txt'))
                and not list(p.parent.glob('iter_*/look.json')) for p in transcripts[-3:]),
                'no confirmed actionless transport stop')
        # Recover the exact private notes from the last opening charter. Compare
        # the complete regenerated charter, so delimiter text in role evidence
        # cannot substitute for a missing or different checkpoint.
        latest = (cycle / f'episodes/ep{count:03d}/outcome.md').read_text()
        summary = [f"Phase-2 target outcome {trigger['verifier_outcome']}: the same Actor Agent "
                   "committed grounded memory before Curriculum selected the next experience",
                   *[f"practice project {i}: " + p['terminal_outcome']
                     for i, p in enumerate(projects, 1)]]
        notes = None
        for message in reversed(history):
            content = message.get('content', '')
            if message['role'] != 'user' or not isinstance(content, str):
                continue
            marker = '\nYOUR PRIVATE SEARCH NOTES:\n---\n'
            if marker not in content:
                continue
            candidate = content.rsplit(marker, 1)[1].split('\n---\n\nCURRENT ACTOR-OWNED DURABLE MEMORY', 1)[0]
            prompt = self_evolving_curriculum_charter(
                target, project_history='\n'.join(summary), latest_outcome=latest,
                curriculum_notes=candidate, handoff_path=CURRICULUM_HANDOFF,
                notes_path=CURRICULUM_NOTES, phase2_outcome=True) + _curriculum_runtime_contract()
            if prompt in content:
                notes = candidate
                break
        require(notes is not None, 'could not reproduce the exact in-progress Curriculum charter')
        curriculum = configs['curriculum']
        used_iters = len(list(turn.glob('segment_*/iter_*/turn.txt')))
        require(used_iters == plan['prior_iters'], 'prior iteration count changed')
        wall = float(plan['prior_wall_secs_conservative'])
        require(wall > 0, 'prior work not charged')
        remaining_iters = curriculum.max_iters - used_iters
        remaining_wall = curriculum.wall_clock_secs - wall
        require(remaining_iters > 0 and remaining_wall > 0, 'original role budget exhausted')
        require(set(plan['context_fields']) <= {
            'history_keep_pairs', 'keep_chars', 'ctx_high_water', 'ctx_low_water',
            'fold_batch', 'worklog_max_chars'}, 'amendment changes non-context role settings')
        return cls(plan_path, root, cycle, target, learning, trigger, project,
                   history, notes, latest, remaining_iters, remaining_wall,
                   plan['context_fields'], plan['recovery_observation'],
                   projects=count, project_records=projects,
                   completed_curriculum_turns=completed_turns)

    def validate_evolution(self, root, target, initial_memory, report, diagnosis, authority, outcome):
        require(Path(root) == self.cycle and target == self.target, 'evolution identity changed')
        require(authority == 'phase2_outcome_protocol' and outcome == self.trigger['verifier_outcome'],
                'evolution authority changed')
        require(report == self.trigger['verifier_report'] and diagnosis == self.trigger['actor_learning_diagnosis'],
                'evolution evidence changed')
        require(_manifest(initial_memory) == self.trigger['memory_before'], 'evolution input memory changed')
        require(_manifest(_read_memory_tree(str(self.cycle / 'memory'))) == self.project['memory_after'],
                'committed practice memory changed')

    def wrap_attempt(self, original):
        if self.pending_memory_learning is not None:
            from explore.practice_memory_recovery import wrap_memory_attempt
            return wrap_memory_attempt(self, original)
        def invoke(instruction, vm, cfg, sink, **kwargs):
            if (Path(sink.root).is_relative_to(
                    self.cycle / f'curriculum/turn_{self.completed_curriculum_turns + 1:03d}')
                    and cfg.practice_done_requires == CURRICULUM_HANDOFF):
                cfg = dataclasses.replace(cfg, **self.context_fields)
            return original(instruction, vm, cfg, sink, **kwargs)
        return invoke
