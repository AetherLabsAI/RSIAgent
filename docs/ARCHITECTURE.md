# Architecture

RSIAgent separates task execution, independent verification, curriculum selection,
memory learning, and final benchmark measurement.

## Role responsibilities

| Role | Inputs and authority |
| --- | --- |
| Actor | Operates the guest, reads its durable memory, and distills its own verified experiences. |
| Verifier | Inspects candidate evidence with its own persistent context across revisions of one task/project; issues PASS, FAIL, or an unresolved outcome. |
| Curriculum | Reviews learning progress and selects the next practice or target attempt; does not grade candidates or author Actor memory. |
| Host harness | Enforces isolation, records artifacts, commits memory at valid boundaries, and invokes the sealed official evaluator. |

Verifier inspection uses a rollback-protected candidate with Actor-private
artifacts hidden. Its guest-local mutations are rolled back before Actor work
resumes. This does not roll back shared remote services; use independent service
accounts or serialize studies that share mutable external state.

## Phase 1

Curriculum authors a wave of practice projects. Each branch starts from the same
pre-wave memory snapshot, independently performs its work, and receives a grounded
Verifier verdict. All branches must finish valid verification before the same
Actors distill their experiences, serially in the authored order. The next wave
starts only after those memory commits are durable.

An incomplete or quarantined branch blocks the wave. Its unpublished memory is
not merged. Budget checks happen at complete-wave boundaries; Curriculum can
declare saturation earlier. Distribution-guided exploration and target-conditioned
prerequisite exploration are separate study designs.

## Phase 2

Phase 2 begins with an actual development-target attempt. Both PASS and FAIL can
ground same-Actor learning. Under the default `curriculum_review`, Curriculum
reviews the learned experience even after a target PASS. It can request more
practice or return readiness. If practice occurs, a fresh attempt on the unchanged
target is required before successful natural completion. Curriculum readiness is
not a task correctness verdict.

The alternative `verifier_pass` policy stops after a target PASS and its memory
commit. Neither policy turns an infrastructure error into a PASS or FAIL.
`STALLED` and budget exits must remain distinguishable from successful convergence.

Curriculum context persists within the target lineage. Its default direct memory
view is read-only. The `none` comparison setting removes that direct view while
retaining normal trajectory and verifier feedback. Actor memory access remains
enabled. The public protocol orchestrator currently uses the default view; the
explicit comparison flag is available on `run_self_evolving.py`.

## Phase 3

A fresh Actor receives a frozen memory snapshot. The Actor/Verifier lifecycle
finishes before the official evaluator runs. There is no Curriculum and no memory
writeback. Evaluator output is a host artifact and cannot enter a subsequent
learning phase within that protocol run.

Frozen configuration hashes, task-release identities, memory hashes, and terminal
statuses record what was executed. An infrastructure failure is unscored; it is
not an official zero. An optional, explicitly identified evaluator correction
must be reported separately from unmodified official evaluation.

## Source map

| Path | Responsibility |
| --- | --- |
| `run_recursive_improvement.py` | Protocol validation and three-stage orchestration |
| `run_phase1_exploration.py`, `explore/phase1_wave.py` | Phase 1 exploration and wave barrier |
| `run_self_evolving.py`, `core/self_evolving_loop.py` | Phase 2 target/practice lifecycle |
| `run_task.py` | Frozen-memory task execution and sealed evaluation |
| `core/`, `llm/`, `env/` | Agent runtime, model transport, and guest interface |
| `explore/` | Curriculum, memory, provisioning, and recovery validation |
| `config/` | Role profiles, runtime paths, and benchmark locks |
| `tools/` | Preparation, audit, and batch utilities |

Some internal modules retain historical `e6`/`e7`/`e15` names because the current
runtime reuses their implementations. Start new studies through the documented
protocol runner rather than those legacy entrypoints.
