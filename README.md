# RSIAgent

**Autonomous Exploration for Recursive Self-improvement in New Environments**

[Paper (Overleaf)](https://www.overleaf.com/project/6a9a6f621edd6601b808861f) · [Method](#method) · [Results](#results) · [Installation](#installation) · [Citation](#citation)

RSIAgent is a **training-free framework for recursive self-improvement** in new
digital environments. It coordinates Curriculum, Actor, and Verifier agents to
discover how an environment works, check what they learn against actual execution,
and retain reusable knowledge in persistent memory. Model parameters stay fixed
throughout exploration and downstream task execution.

The paper's central strategy is **broad-then-deep exploration**: first acquire
diverse experience, then investigate hard cases, hidden constraints, and boundary
conditions. The resulting memory contains procedures, scripts, and failure lessons
that a fresh Actor can reuse at test time.

[![RSIAgent framework: parallel Broad Recursive Self-exploration, sequential Deep Recursive Self-exploration, and test-time reuse of frozen memory, illustrated with FreeCAD.](docs/assets/framework.png)](docs/assets/framework.png)

*The paper's original framework figure, illustrated with a FreeCAD task. Broad
experience is progressively refined into targeted memory, then frozen for reuse.
Click the figure for full resolution. [Figure provenance](docs/PAPER.md#figure-provenance).*

## Method

Three agents carry out the recursive learning loop:

- **Curriculum** chooses informative exploration tasks using prior outcomes and
  accumulated knowledge, then decides whether further practice is useful.
- **Actor** interacts with software through executable Python or Bash programs
  and visual observations. After verification, the same Actor distills its
  experience and reconciles it with existing memory.
- **Verifier** independently inspects task requirements and the resulting
  environment. Its feedback grounds learning; it cannot read the Actor's private
  reasoning or memory.

The paper has **two exploration stages followed by test-time memory reuse**.
The implementation exposes these as three runtime phases:

| Runtime phase | Paper stage | Learning and execution |
| --- | --- | --- |
| **Phase 1** | **Broad Recursive Self-exploration (BRS)** | Curriculum proposes diverse projects. Actors execute and Verifiers check them in parallel from a shared starting memory. After the complete wave, Actors consolidate their experiences in order. |
| **Phase 2** | **Deep Recursive Self-exploration (DRS)** | Target attempts reveal gaps and fragile successes. Curriculum selects focused practice; each verified experience updates memory before subsequent practice or a fresh target attempt. |
| **Phase 3** | **Test-time memory reuse** | A fresh Actor uses the frozen memory in a reset environment. The Actor–Verifier loop runs without Curriculum or learning, followed by sealed official evaluation. |

Memory is the persistent learning state. Later Actors inherit its files, while
their interaction histories and environments start fresh. Both grounded successes
and failures can teach useful lessons. Official benchmark scores are kept outside
the learning loop. See [Architecture](docs/ARCHITECTURE.md) for the role interfaces,
wave memory barrier, and stopping rules.

## Results

The manuscript reports these **mean partial-credit scores (%)** for the shared
Actor–Verifier harness with and without RSI:

| Benchmark and reporting coverage | RSIAgent w/o RSI | RSIAgent |
| --- | ---: | ---: |
| OSWorld 2.0 · 0808 offline · 82 tasks | 71.97 | **78.98** |
| Agents' Last Exam · Near-term · 64 of 67 tasks | 84.40 | **85.52** |

These are the manuscript's reported aggregates. The RSI column uses 41 recorded
RSI entries for OSWorld and 19 for ALE, retaining baseline scores for the other
tasks. It includes selected retries and checkpoints with differing budgets; it
is not an average over matched repeated runs. ALE also includes qualified local
regrades and protocol variants. See the [paper and reporting notes](docs/PAPER.md)
for the full scope, full-credit metrics, and aggregation details.

The paper also examines stage ablations, memory growth, and game development.
Its failure analysis identifies three limits to improvement: practice can miss
the relevant weakness, verification can accept incomplete work, and memory can
preserve an incorrect rule. The quality of exploration, verification, and memory
consolidation therefore matters alongside the amount of practice.

**This source release provides the OSWorld integration**, pinned to OSWorld-V2's
August 8, 2026 release with Docker/QEMU guests. ALE and game-development runners
are not included in this package.

## Installation

Use Python 3.12 and a Linux host with Docker and accessible `/dev/kvm` for desktop
runs. Model API access, the OSWorld guest image, and access to its gated task and
asset datasets are required. This repository contains source, configuration, and
tests; it does not contain credentials, benchmark assets, VM images, or research
trajectories. Install and run it from a source checkout.

```bash
git clone https://github.com/AetherLabsAI/RSIAgent.git
git clone --branch v2026.08.08 https://github.com/xlang-ai/OSWorld-V2.git
```

Follow the pinned OSWorld [installation instructions](https://github.com/xlang-ai/OSWorld-V2/tree/v2026.08.08)
and [Docker provider setup](https://github.com/xlang-ai/OSWorld-V2/blob/v2026.08.08/docs/PROVIDER_SETUP.md#docker).
With `uv` installed, create its environment and add RSIAgent's dependencies:

```bash
cd OSWorld-V2
uv sync --frozen
uv pip install --python .venv/bin/python -r ../RSIAgent/requirements.txt
source .venv/bin/activate
cd ../RSIAgent

export FORGE_ROOT="$PWD"
export OSWORLD_ROOT="$(dirname "$FORGE_ROOT")/OSWorld-V2"
cp .env.example .env
```

Fill in your own `OPENROUTER_API_KEY` in `.env`. `FORGE_*` names are retained for
compatibility with existing configurations. Hugging Face access and any website
credentials are configured separately using OSWorld's instructions.

Prepare the pinned task release and the host-only rejection checks:

```bash
python tools/prepare_osworld_v2_release.py --osworld-root "$OSWORLD_ROOT"
python tools/build_p2_corpus.py
python tools/exam_fence.py build
```

The two audit tools create reject-only data under the ignored `results/`
directory. These files must stay on the host and must never enter an Agent prompt
or learning memory.

## Run a study

Copy the supplied target-conditioned example and give the study a unique name:

```bash
mkdir -p results/protocols
cp config/recursive_self_improvement_0808.example.json results/protocols/my_study.json
```

Edit `run_name`, the public task/query file, and both task lists before launching.
The example uses T080, an eight-project Phase 1 boundary, up to four concurrent
practice branches, and `curriculum_review` in Phase 2. In a target-conditioned
study, development and evaluation name the same task, using fresh environments.
For unseen-task research, use `held_out_generalization` and disjoint task sets.

Inspect the configuration, then run all three stages in sequence:

```bash
python run_recursive_improvement.py \
  --protocol results/protocols/my_study.json --phase phase1 --preflight

bash scripts/run_rsi.sh results/protocols/my_study.json
```

The wrapper uses a separate cache for each stage, streams output, and stops on a
failed command. It does not restart failed attempts. To invoke a stage directly:

```bash
python run_recursive_improvement.py \
  --protocol results/protocols/my_study.json --phase phase1 \
  --execute RUN-RECURSIVE-IMPROVEMENT-PHASE1
```

Use `phase2`/`PHASE2` and `phase3`/`PHASE3` for later stages. Results, protocol
locks, and memory snapshots are saved under
`results/recursive_improvement/<run_name>/`; detailed task runs have their own
paths referenced by the phase results. See [operation and recovery](docs/OPERATIONS.md).

## Defaults and boundaries

- The Actor owns memory updates after valid Verifier PASS and FAIL outcomes.
  An infrastructure failure or `UNVERIFIED` outcome is not a learning verdict.
- A Phase 1 wave is a memory barrier: sibling Actors see the same frozen memory,
  and all branch verdicts precede serial memory commits. The eight-project budget
  is checked after complete waves and can be exceeded by the final wave.
- Phase 2 defaults to `curriculum_review`: a target PASS is learned, then
  Curriculum reviews whether to continue. `READY_FOR_TARGET` after practice
  requests another target attempt. It does not bypass target verification.
  `verifier_pass` is an explicit alternative stopping policy.
- Curriculum's direct Phase 2 memory access defaults to `read_only`. The direct
  Phase 2 runner also exposes `--phase2-curriculum-memory-access none` for separate
  comparison lineages. This disables Curriculum's direct access, not the Actor's.
- The default DRS lifecycle has no two-project cap. The paper's deep-only
  ablations used at most two Curriculum practice projects through separate
  experiment controls, which are not a public runner API. Target attempts and
  their memory updates do not count as practice projects.
- Official evaluation is unavailable during learning. Phase 3 uses frozen memory
  with no host writeback and no evaluation feedback into learning.

The supplied role profiles use GLM-5.3 for the Actor and Kimi K3 for Curriculum and
verification. The target Actor watchdog is ten hours per Actor run; this is not a
ten-hour limit on the whole study. Configuration hashes and evaluator release
bindings are checked before evaluation. Change profiles in a new, explicitly
recorded protocol rather than editing a running study's locked configuration.

## Tests

The portable suite runs without model credentials, Docker, or a benchmark checkout:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python tools/check_rsi_release.py
```

Tests cover role boundaries, memory commits, stopping policies, recovery admission,
transport handling, configuration locks, and sealed evaluation. They do not replace
a live VM smoke test on a newly provisioned machine.

Two optional tests exercise the T102 correction against the frozen external
evaluator. In a fully prepared OSWorld Python environment, run them with
`RSI_TEST_OSWORLD_INTEGRATION=1 python -m pytest -q tests/test_evaluator_corrections.py`.

## Documentation

- [Paper, figure provenance, and reporting scope](docs/PAPER.md)
- [Architecture and learning protocol](docs/ARCHITECTURE.md)
- [Operation, configuration, and recovery](docs/OPERATIONS.md)
- [Release provenance](docs/RELEASE.md)
- [Contributing](CONTRIBUTING.md)
- [Dependency and benchmark attribution](THIRD_PARTY.md)

A target-conditioned result does not establish generalization to an unseen task.
Record the study design, exploration budget, all evaluation draws, and aggregation
rule when reporting new experiments.

## Citation

If you use RSIAgent, please cite the manuscript:

```bibtex
@unpublished{zhu2026rsiagent,
  title = {RSIAgent: Autonomous Exploration for Recursive
           Self-improvement in New Environments},
  author = {Zhu, Sibo and Fan, Shicheng and Wang, Xinyue
            and Wu, Wenyi and Zhou, Kun and Huang, Biwei},
  year = {2026},
  note = {Technical report},
  url = {https://www.overleaf.com/project/6a9a6f621edd6601b808861f}
}
```

## License status

A distribution license has not yet been selected for this initial company-review
package. Third-party dependency licenses are described in [THIRD_PARTY.md](THIRD_PARTY.md).
