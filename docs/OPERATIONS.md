# Operation and recovery

## Environment

Run from the checkout with its dependencies and the pinned OSWorld environment
installed. Export `FORGE_ROOT` to this checkout and `OSWORLD_ROOT` to the benchmark
checkout. `FORGE_ENV_FILE` optionally names a private credentials file. These path
variables are read before `.env` loading, so export them in the shell.

Each machine needs Docker/KVM access, the official guest image, gated task/assets
access, and the appropriate website/service setup. Assets and setup sources stay
in OSWorld; RSIAgent does not redistribute them. Keep model and benchmark access
credentials in your own environment. The configuration preflight checks the
declared study; it does not prove that every VM, API, or website will be reachable.

`prepare_osworld_v2_release.py --verify-only` verifies an already prepared release.
It still checks the remote dataset revision. `--refresh-assets` replaces the
dedicated asset snapshot and should only be used deliberately on an idle setup.

## Configuration

Copy `config/recursive_self_improvement_0808.example.json` and choose a unique
`run_name`. Change task IDs and the matching public Phase 1 query before starting.
Use a new protocol lineage for a different policy, model, memory seed, or budget.

`curriculum_review` is the Phase 2 default. `verifier_pass` is the explicit
alternative. Curriculum's direct-memory comparison setting is exposed by the
direct runner, not by the public JSON orchestrator. Do not add an unsupported JSON
field and assume it took effect.

The example has an eight-project Phase 1 boundary and four maximum concurrent
branches. Its target Actor watchdog is 36,000 seconds per invocation; practice
Actor and Curriculum profiles allow 86,400 seconds. A study can run much longer
than any one invocation. There is no default two-practice Phase 2 limit.

## Running and observing

`bash scripts/run_rsi.sh <protocol.json>` invokes the three phases sequentially,
uses a separate cache per phase, and records streamed logs. To inspect a finished
phase, start with `results/recursive_improvement/<run_name>/phaseN/result.json`.
Follow its recorded paths to the detailed target/project artifacts.

Phase 1 wave authoring records are under `phase1/curriculum/wave_NNN/`. Check
published projects, completed verdicts, and committed memory, not just a live PID.
Phase 2 records distinguish target attempts, practice projects, Curriculum
decisions, and learning commits. Phase 3 records official scores only after sealed
evaluation succeeds. Preserve all draws and distinguish verifier verdicts from
official scores.

The wrapper performs no unattended recovery. It exits on failure. No monitoring
daemon or scheduler is installed by this release.

## Concurrency

Begin with one study on a newly provisioned machine. Independent studies need
unique run names and caches, enough memory/disk/KVM capacity, and independent
external application state. Increasing branch parallelism increases the number
of guest VMs. Two runs for the same task can interfere through a shared website
account even when their local VMs are separate.

## Recovery boundaries

Inspect the failed attempt's `recovery.json`, authoring state, and the preserved
runtime before starting a replacement. Keep the failed attempt and its memory
snapshot. Check for a live recovery first. Never reuse a quarantined lineage,
turn an infrastructure error into a task verdict, or overwrite a sealed score.

Phase 1 offers `--resume-completed-boundary` and `tools/recover_phase1_wave.py`
for validated boundaries. Phase 2 recovery modules under `explore/` validate
specific preserved checkpoints, including completed learning and guest-filesystem
failures. They are strict internal APIs, not a universal resume command. Consult
the matching module and tests before an isolated continuation. An absent candidate
or missing preserved context cannot be reconstructed by relabeling a result.

If an evaluator defect is found, preserve the original result and identify the
corrected evaluator revision in a separate measurement. Do not supply diagnostic
grading details to a learning agent. The included T102 correction is explicitly
opt-in, source-hash-bound, and disabled by the standard example.
