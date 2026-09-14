# Architecture

RSIAgent separates task execution, independent verification, curriculum selection,
memory learning, and final benchmark measurement.

## Role responsibilities

| Role | Inputs and authority |
| --- | --- |
| Actor Agent | Operates the guest, reads its durable memory, and distills its own verified experiences. |
| Verifier Agent | Inspects candidate evidence with its own persistent context across revisions of one task/project; issues PASS, FAIL, or an unresolved outcome. |
| Curriculum Agent | Reviews learning progress and selects the next practice or target attempt; does not grade candidates or author the Actor Agent's memory. |
| Host harness | Enforces isolation, records artifacts, commits memory at valid boundaries, and invokes the sealed official evaluator. |

The Verifier Agent inspects a rollback-protected candidate with the Actor Agent's
private artifacts hidden. Its guest-local mutations are rolled back before the
Actor Agent's work resumes. This does not roll back shared remote services; use independent service
accounts or serialize studies that share mutable external state.

## Phase 1

The Curriculum Agent authors a wave of practice projects. Each branch starts from
the same pre-wave memory snapshot, independently performs its work, and receives a grounded
Verifier Agent verdict. All branches must finish valid verification before the same
Actor Agents distill their experiences, serially in the authored order. The next wave
starts only after those memory commits are durable.

An incomplete or quarantined branch blocks the wave. Its unpublished memory is
not merged. Budget checks happen at complete-wave boundaries; the Curriculum Agent can
declare saturation earlier. Distribution-guided exploration and target-conditioned
prerequisite exploration are separate study designs.

## Phase 2

Phase 2 begins with an actual development-target attempt. Both PASS and FAIL can
ground learning by the Actor Agent that performed the attempt. Under the default
`curriculum_review`, the Curriculum Agent reviews the learned experience even
after a target PASS. It can request more
practice or return readiness. If practice occurs, a fresh attempt on the unchanged
target is required before successful natural completion. Readiness from the
Curriculum Agent is not a task correctness verdict.

The alternative `verifier_pass` policy stops after a target PASS and its memory
commit. Neither policy turns an infrastructure error into a PASS or FAIL.
`STALLED` and budget exits must remain distinguishable from successful convergence.

The Curriculum Agent's context persists within the target lineage. Its default
direct memory view is read-only. The `none` comparison setting removes that direct
view while retaining normal trajectory and Verifier Agent feedback. The Actor
Agent's memory access remains enabled. The public protocol orchestrator currently
uses the default view; the
explicit comparison flag is available through `python -m benchmarks.osworld.phase2`.

## Phase 3

The Actor Agent uses a frozen memory snapshot within the same agent framework
used during RSI. Each evaluation starts with a reset interaction history and task
environment. The interaction between the Actor Agent and Verifier Agent finishes
before the official evaluator runs. The Curriculum Agent and memory updates are
disabled. Evaluator output is a host artifact and cannot enter a subsequent
learning phase within that protocol run.

Frozen configuration hashes, task-release identities, memory hashes, and terminal
statuses record what was executed. An infrastructure failure is unscored; it is
not an official zero. An optional, explicitly identified evaluator correction
must be reported separately from unmodified official evaluation.

## Source map

| Path | Responsibility |
| --- | --- |
| `run_osworld.py`, `run_ale.py` | Public benchmark entrypoints |
| `benchmarks/osworld/pipeline.py` | Protocol validation and three-stage orchestration |
| `benchmarks/osworld/phase1.py`, `explore/phase1_wave.py` | Phase 1 exploration and wave barrier |
| `benchmarks/osworld/phase2.py`, `core/self_evolving_loop.py` | Phase 2 target/practice lifecycle |
| `benchmarks/osworld/task.py` | Frozen-memory task execution and sealed evaluation |
| `benchmarks/osworld/evaluator_corrections.py` | Explicit, source-bound evaluator corrections |
| `core/`, `llm/`, `env/` | Agent runtime, model transport, and guest interface |
| `explore/` | Curriculum Agent orchestration, memory, provisioning, and recovery validation |
| `config/` | Role profiles, runtime paths, and benchmark locks |
| `scripts/` | Environment setup and individual-study helpers |
| `tools/` | Preparation, smoke checks, and recovery utilities |

Internal OSWorld stages run as modules from the checkout, for example
`python -m benchmarks.osworld.pipeline --help`. Both the batch entrypoint and
the individual-study helper launch them this way, keeping imports independent
of the module's directory.

The OSWorld adapter lives under `benchmarks/osworld/`; ALE lives under
`benchmarks/ale/`. ALE's outer process owns provisioning and grading, while a
separate worker process runs the shared learning runtime. Verified Phase 1 ALE
branches can release their VM before ordered memory consolidation; their Actor
contexts and captured candidates remain intact and are replayed when committing.
