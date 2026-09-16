<p align="center">
  <img src="docs/assets/rsiagent-icon.svg" alt="RSIAgent icon" width="104" height="104">
</p>

<h1 align="center">
  <img src="docs/assets/rsiagent-wordmark.svg" alt="RSIAgent" width="480">
</h1>

<h2 align="center">
  <picture>
    <source media="(max-width: 600px)" srcset="docs/assets/rsiagent-subtitle-mobile.svg">
    <img src="docs/assets/rsiagent-subtitle.svg" alt="Autonomous Exploration for Recursive Self-improvement in New Environments" width="100%">
  </picture>
</h2>

<p align="center">
  <a href="https://arxiv.org/pdf/2609.15364"><img src="https://img.shields.io/badge/Paper-arXiv-b31b1b?style=flat" alt="Paper: arXiv"></a>
  <a href="https://huggingface.co/papers/2609.15364" title="No. 6 on the September 15, 2026 Daily Papers list; checked September 16, 2026 (UTC)"><img src="https://img.shields.io/badge/HF_Daily_Papers-%236_%C2%B7_2026--09--15-FFD21E?style=flat&amp;logo=huggingface&amp;logoColor=FFD21E" alt="Hugging Face Daily Papers: #6 on the September 15, 2026 list"></a>
  <a href="https://aetherlabsai.github.io/RSIAgent/"><img src="https://img.shields.io/badge/Website-RSIAgent-6554c0?style=flat" alt="Website: RSIAgent"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat" alt="License: Apache 2.0"></a>
</p>

<!-- Rank snapshot verified September 16, 2026 (UTC): https://huggingface.co/papers/date/2026-09-15 -->

<p align="center">
  <a href="#news">News</a> · <a href="#demos">Demos</a> · <a href="#method">Method</a> · <a href="#results">Results</a> · <a href="#installation">Quickstart</a> · <a href="#documentation">Documentation</a> · <a href="#citation">Citation</a>
</p>

https://github.com/user-attachments/assets/11cb3919-1073-4ba3-8bcd-37a15ec53c33

<a id="news"></a>

## 📰 News & Highlights

> **🔥 Headline feature in 机器之心 · September 2026**
>
> **Open-source models surpass GPT-6 Astra on challenging computer-use benchmarks.**
> Discover how RSIAgent turns autonomous exploration into reusable experience—without updating model weights.
>
> [![WeChat article reads: 40K+](https://img.shields.io/badge/WeChat_reads-40K%2B-2E7D32?style=flat-square&logo=wechat&logoColor=white)](https://mp.weixin.qq.com/s/bg0tkvHsKTZ88uBNKUdVTQ)
>
> <sub>Read-count snapshot: September 16, 2026.</sub>

<a id="overview"></a>

## 🧭 Overview

RSIAgent is a **training-free framework for recursive self-improvement** in new
digital environments. It coordinates the Curriculum Agent, Actor Agent, and
Verifier Agent to discover how an environment works, check what they learn against actual execution,
and retain reusable knowledge in persistent memory. Model parameters stay fixed
throughout exploration and downstream task execution.

The paper's central strategy is **broad-then-deep exploration**: first acquire
diverse experience, then investigate hard cases, hidden constraints, and boundary
conditions. The resulting memory contains procedures, scripts, and failure lessons
that the Actor Agent reuses for downstream task execution.

<a id="demos"></a>

## 🎬 Demos

https://github.com/user-attachments/assets/b939a9c2-4acb-4c30-b72e-18bab646529c

<a id="framework"></a>

## 🧩 Framework

[![RSIAgent framework: parallel Broad Recursive Self-exploration, sequential Deep Recursive Self-exploration, and test-time reuse of frozen memory, illustrated with FreeCAD.](docs/assets/framework.png)](docs/assets/framework.png)

*The paper's original framework figure, illustrated with a FreeCAD task. Broad
experience is progressively refined into targeted memory, then frozen for reuse.
Click the figure for full resolution. [Figure provenance](docs/PAPER.md#figure-provenance).*

<a id="method"></a>

## 🔄 Method

Three agents carry out the recursive learning loop:

- **Curriculum Agent** chooses informative exploration tasks using prior outcomes and
  accumulated knowledge, then decides whether further practice is useful.
- **Actor Agent** interacts with software through executable Python or Bash programs
  and visual observations. After verification, the same Actor Agent distills its
  experience and reconciles it with existing memory.
- **Verifier Agent** independently inspects task requirements and the resulting
  environment. Its feedback grounds learning; it cannot read the Actor Agent's
  private reasoning or memory.

The paper has **two exploration stages followed by test-time memory reuse**.
**RSI and test-time execution use the same agent framework**, with fixed model
parameters throughout. At test time, memory is frozen, and the Curriculum Agent
and memory updates are disabled. The implementation exposes these as three
runtime phases:

| Runtime phase | Paper stage | Learning and execution |
| --- | --- | --- |
| **Phase 1** | **Broad Recursive Self-exploration (BRS)** | The Curriculum Agent proposes diverse projects. Actor Agents execute and Verifier Agents check them in parallel from a shared starting memory. After the complete wave, Actor Agents consolidate their experiences in order. |
| **Phase 2** | **Deep Recursive Self-exploration (DRS)** | Target attempts reveal gaps and fragile successes. The Curriculum Agent selects focused practice; each verified experience updates memory before subsequent practice or another target attempt. |
| **Phase 3** | **Test-time memory reuse** | The Actor Agent uses frozen memory to guide task execution, interacting with the Verifier Agent through the same action–verification loop used during RSI. Sealed official evaluation follows task execution and verification. |

Memory is the persistent learning state across tasks. Interaction histories and
task environments are reset between independent attempts; the Agent framework
remains unchanged. Both grounded successes and failures can teach useful lessons.
Official benchmark scores are kept outside the learning loop. See
[Architecture](docs/ARCHITECTURE.md) for the role interfaces, wave memory barrier,
and stopping rules.

<a id="results"></a>

## 📊 Results

The manuscript reports these **mean partial-credit scores (%)** for the shared
harness coordinating the Actor Agent and Verifier Agent, with and without RSI:

| Benchmark and reporting coverage | RSIAgent w/o RSI | RSIAgent |
| --- | ---: | ---: |
| OSWorld 2.0 · 0808 offline · 82 tasks | 71.97 | **78.98** |
| Agents' Last Exam · Near-term · 67 tasks | 83.75 | **84.82** |

These are the manuscript's reported aggregates. The RSI column uses 41 recorded
RSI entries for OSWorld and 19 for ALE, retaining baseline scores for the other
41 OSWorld tasks and 48 ALE tasks. ALE includes all 67 Near-term tasks, including
the three GPU baseline results. The RSI column includes selected retries and
checkpoints with differing budgets; it is not an average over matched repeated
runs. ALE also includes qualified local
regrades and protocol variants. See the [paper and reporting notes](docs/PAPER.md)
for the full scope, full-credit metrics, and aggregation details.

The paper also examines stage ablations, memory growth, and game development.
Its failure analysis identifies three limits to improvement: practice can miss
the relevant weakness, verification can accept incomplete work, and memory can
preserve an incorrect rule. The quality of exploration, verification, and memory
consolidation therefore matters alongside the amount of practice.

<a id="benchmarks"></a>

## 🧪 Benchmarks

| Integration | Pinned release | Public batch |
| --- | --- | --- |
| OSWorld-V2 | August 8, 2026 | 108 tasks, Docker/QEMU |
| Agents' Last Exam (ALE) | `d10fb61a14f9719774c3520c5763068b28ef5546` | 67 Near-term tasks; 64 CPU tasks and 3 completed GPU-task baselines |

All three GPU-task baselines are complete. Chroma Key ran on a Google Cloud
(GCP) VM using the official ALE Windows image and an NVIDIA L4 vWS GPU.

Only the current runtime is included. Both integrations use the same Actor,
Verifier, Curriculum, and memory protocol. Benchmark setup and grading remain
outside the learning process.

<a id="repository-layout"></a>

## 🗂️ Repository layout

```text
run_osworld.py        OSWorld batch entrypoint
run_ale.py            ALE preparation, execution, and reporting
benchmarks/
  osworld/           OSWorld stages, VM adapter, and evaluation
  ale/               ALE host, worker, and VM adapters
core/                Shared Actor and Verifier runtime
explore/             Curriculum, learning, memory, and recovery
env/                 Shared guest transport and isolation
llm/                 Model clients
config/              Role profiles and benchmark configurations
scripts/             Setup and individual-study helpers
tools/               Preparation, smoke checks, and recovery utilities
tests/               Regression tests
docs/                Guides, architecture, and attribution
```

The two root entrypoints are the starting point for benchmark runs. Internal
OSWorld stages are Python modules under `benchmarks/osworld/`; see the
[source map](docs/ARCHITECTURE.md#source-map) for their responsibilities.

<a id="installation"></a>

## ⚙️ Installation

Use Python 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and a Linux host with Docker and access to `/dev/kvm`. Start with the repository
and your own model API credential:

```bash
git clone https://github.com/AetherLabsAI/RSIAgent.git
cd RSIAgent
cp .env.example .env
```

Fill in `OPENROUTER_API_KEY` in `.env`. Paths default to this checkout and sibling
benchmark directories. Export `RSIAGENT_ROOT`, `OSWORLD_ROOT`, or
`RSIAGENT_ENV_FILE` only when using a different layout.

<a id="orcarouter"></a>

### 🐋 OrcaRouter

[OrcaRouter](https://www.orcarouter.ai) is an OpenAI-compatible gateway that can
serve as the model provider instead of OpenRouter. It is selected explicitly, so
an unset value leaves every existing run on the endpoint it already used:

```bash
# .env
RSIAGENT_LLM_PROVIDER=orcarouter
```

Inference and model discovery go to `https://api.orcarouter.ai/v1`; authentication
goes to `https://www.orcarouter.ai`. There are two ways to supply a credential,
and both end in an ordinary `sk-orca-…` API key that belongs to your own account —
billed to it, listed in its console, revocable by you at any time. No client
secret is involved.

**1. Paste an existing key** (`https://www.orcarouter.ai/console/authorized-apps`):

```bash
# .env
ORCA_API_KEY=sk-orca-…
```

**2. Authorize in a browser** (OAuth 2.0 + PKCE, `S256`) — the issued key is
stored in the same `.env`, and reused on later runs:

```bash
python -m llm.connect login              # open the consent screen, paste the code
python -m llm.connect login --loopback   # local desktop: redirects back to 127.0.0.1
python -m llm.connect status             # show the stored credential
python -m llm.connect logout             # remove it
```

The default is the out-of-band code flow because RSIAgent usually runs over SSH,
in a container, or on a box whose address differs per deployment — there is no
callback address to pre-register. `--loopback` is for a local desktop session.

A PKCE-issued key is durable but it is **not** a refresh token: there is no
refresh grant, OrcaRouter caps issuance at 10 keys per user per 24 hours, and the
key is reused until you revoke it. A `401` from the relay marks exactly the
credential generation that made the request as needing reauthentication and tells
you to sign in again — it is never retried in a loop, and no replacement key is
minted silently.

**Choosing a model.** The selectable models are read live from
`GET https://api.orcarouter.ai/v1/models` with your own key, so the list is what
your workspace can actually call:

```bash
python -m llm.models list                                   # text chat / agent loop
python -m llm.models list --capability multimodal --modality image
python -m llm.models check deepseek/deepseek-v4-pro         # validate a configured slug
```

Each entry point gets its own capability filter. Text selectors require a declared
text route (`openai` / `anthropic` / `gemini` / `openai-response`) and exclude
dedicated image, video, embedding, and rerank models. The image-understanding path
(native Look, `core/eyes.py`) additionally requires the model to declare an
`image` input modality; a model that declares nothing is left out rather than
guessed at, so `deepseek/deepseek-v4-flash` (declared text-only) and
`orcarouter/auto` (declares no modality) are not offered for images while
`deepseek/deepseek-v4.1-flash` is. If discovery fails, the command says so and
falls back to a small verified catalog rather than accepting a model name typed
from memory.

Self-hosted deployments can set one shared `ORCA_BASE_URL`, or separate
`ORCA_AUTH_BASE_URL` / `ORCA_API_BASE_URL` overrides; explicit values win. Remote
origins must be `https` — plain `http` is accepted only for loopback development.

The Actor's model is the `model` field of its role config (`config/roles/*.yaml`,
`config/osworld/*.yaml`), so point it at one of the listed IDs — the vendor
namespace is part of the slug and must be kept exactly as listed:

```yaml
model: deepseek/deepseek-v4-pro
```

Note that an OrcaRouter run is a new provider for the same frozen experiment
lineage: record the endpoint and model alongside your results as you would for
any other route.

Follow the setup for the benchmark you want to run. All commands below start
from the `RSIAgent` directory unless a `cd` is shown. VM images and benchmark
assets are downloaded separately.

<a id="osworld"></a>

### 🖥️ OSWorld

Install the pinned [OSWorld release](https://github.com/xlang-ai/OSWorld-V2/tree/v2026.08.08)
and follow its [Docker setup](https://github.com/xlang-ai/OSWorld-V2/blob/v2026.08.08/docs/PROVIDER_SETUP.md#docker).
From the RSIAgent checkout:

```bash
git clone --branch v2026.08.08 https://github.com/xlang-ai/OSWorld-V2.git ../OSWorld-V2
cd ../OSWorld-V2
uv sync --frozen
uv pip install --python .venv/bin/python -r ../RSIAgent/requirements.txt
source .venv/bin/activate
cd ../RSIAgent
```

Prepare the benchmark assets and audit files, then check the VM:

```bash
python tools/prepare_osworld_v2_release.py
python tools/build_p2_corpus.py
python tools/exam_fence.py build
python tools/smoke_osworld.py --output results/smoke/osworld
```

Keep this environment active for the OSWorld batch commands below. In a new
shell, activate it with `source ../OSWorld-V2/.venv/bin/activate`.
The audit files under `results/` stay on the host and never enter Agent prompts
or memory.

<a id="ale"></a>

### 🎓 ALE

The setup script clones the pinned upstream source and installs separate grader
and worker environments. This separation prevents the two projects' Python
packages from shadowing each other.

```bash
python3 scripts/setup_ale.py
../agents-last-exam/.venv/bin/python run_ale.py prepare --os linux
../agents-last-exam/.venv/bin/python run_ale.py prepare --os windows
```

Each preparation downloads only the selected OS image. Linux requires about
167 GiB and Windows about 157 GiB, plus download and VM working space. Use
`--cache /path/with/space` consistently for preparation, smoke tests, and runs.

Check both guest types before running the full CPU cohort:

```bash
../agents-last-exam/.venv/bin/python run_ale.py smoke \
  --os linux --output results/smoke/ale_linux
../agents-last-exam/.venv/bin/python run_ale.py smoke \
  --os windows --output results/smoke/ale_windows
```

ALE requires a successful smoke for each requested OS on the current source and
runner image. Use a new `--output` directory when repeating a smoke. See
[ALE operations](docs/ALE.md) for storage options and the upstream guide.

<a id="run-batches"></a>

## 🚀 Run batches

Choose `--arm baseline` for task execution without RSI, `--arm rsi` for learning
followed by frozen-memory evaluation, or `--arm both` to run both.

<a id="osworld-1"></a>

### 🖥️ OSWorld

Inspect the full 108-task plan, then run it:

```bash
python run_osworld.py --arm both --name osworld_run_01 --dry-run
python run_osworld.py --arm both --name osworld_run_01 --concurrency 1
```

`--dry-run` prints the plan without starting VMs, making model calls, or writing
outputs. Logs and task status are under `results/batches/<name>/`. A task failure
stops its remaining phases; other tasks continue. Choose a new `--name` for each
batch because existing outputs are never overwritten.

<a id="ale-1"></a>

### 🎓 ALE

Inspect the cohort, run the supported tasks, and generate a report:

```bash
../agents-last-exam/.venv/bin/python run_ale.py plan --arm both
../agents-last-exam/.venv/bin/python run_ale.py run \
  --arm both --output results/ale/run_01 --concurrency 1
../agents-last-exam/.venv/bin/python run_ale.py report \
  --runs results/ale/run_01 --output results/ale/report_01
```

Use a new `--output` directory for each run. The report contains `tasks.csv` and
`summary.json`, keeps missing results explicit, and rejects
duplicate scored attempts.

Both entrypoints run batches directly. Begin with `--concurrency 1`; raise it
when the host has capacity for additional independent task lineages. For custom
OSWorld protocols and recovery, see [operations](docs/OPERATIONS.md).

<a id="validation"></a>

## ✅ Validation

Portable checks run without credentials, Docker, or benchmark installations:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python tools/check_rsi_release.py
```

The VM smoke commands in the installation steps check transport, immutable
memory, candidate replay, Verifier isolation, and checkpoint rollback using
synthetic files. They make no model or official grader calls. Smoke success
validates runtime mechanics; reproducing benchmark scores requires complete
experiments with the pinned configuration.

<a id="documentation"></a>

## 📚 Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [OSWorld operations and recovery](docs/OPERATIONS.md)
- [ALE setup, batches, and reports](docs/ALE.md)
- [Release provenance and validation](docs/RELEASE.md)
- [Paper and reporting scope](docs/PAPER.md)
- [Contributing](docs/CONTRIBUTING.md) · [Third-party attribution](docs/THIRD_PARTY.md)

<a id="citation"></a>

## 📝 Citation

If you use RSIAgent, please cite the [arXiv preprint](https://arxiv.org/abs/2609.15364):

```bibtex
@misc{zhu2026rsiagentautonomousexplorationrecursive,
      title={RSIAgent: Autonomous Exploration for Recursive Self-improvement in New Environments},
      author={Sibo Zhu and Shicheng Fan and Xinyue Wang and Wenyi Wu and Kun Zhou and Biwei Huang},
      year={2026},
      eprint={2609.15364},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2609.15364},
}
```

<a id="license"></a>

## ⚖️ License

RSIAgent is licensed under the [Apache License 2.0](LICENSE). Third-party dependencies and benchmark assets retain their respective licenses and terms; see [third-party attribution](docs/THIRD_PARTY.md).
