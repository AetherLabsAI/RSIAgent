# ALE operations

The integration uses the canonical 67-task Near-term cohort at upstream commit
`d10fb61a14f9719774c3520c5763068b28ef5546`. It supports the 64 CPU tasks through
Linux and Windows QEMU guests. All three GPU-task baselines were completed using
separately provisioned runners; Chroma Key used the official ALE Windows image
on a Google Cloud (GCP) VM with an NVIDIA L4 vWS GPU. These completed results are
included in the reported 67-task aggregate. The bundled QEMU batch launcher still
requires a separate GPU runner for those tasks; missing results are never scored
as zero.

## Install and prepare

Follow the upstream [QEMU/KVM guide](https://github.com/rdi-berkeley/agents-last-exam/blob/d10fb61a14f9719774c3520c5763068b28ef5546/docs/local-qemu.md).
Docker must be able to pass `/dev/kvm` into its containers. Install both isolated
Python environments with:

```bash
python3 scripts/setup_ale.py
```

The default upstream checkout is `../agents-last-exam`; `--ale-root` selects
another location. The grader uses its own `.venv`, while RSIAgent uses
`.venv-ale-worker`. The setup script refuses a mismatched upstream revision or
lock and does not rewrite an existing development environment.

Prepare one OS at a time:

```bash
../agents-last-exam/.venv/bin/python run_ale.py prepare --os linux
../agents-last-exam/.venv/bin/python run_ale.py prepare --os windows
```

Use `--cache` to select storage for the pinned images and writable VM overlays.
The base disks require approximately 167 GiB for Linux and 157 GiB for Windows.
Allow extra space for download parts, overlays, and checkpoints. Image downloads
resume through the upstream provider and verify the pinned checksums.

## Validate and run

Run the synthetic guest smoke for each OS you intend to evaluate:

```bash
../agents-last-exam/.venv/bin/python run_ale.py smoke --os linux
../agents-last-exam/.venv/bin/python run_ale.py smoke --os windows
```

The smoke checks lossless nested memory transfer, project capture and replay,
Verifier isolation, persistent scratch, candidate rollback, and fresh guest
reset. It makes no model or grader calls. A smoke receipt is bound to the current
source and runner image; code or image changes require a new receipt. Use a new
`--output` directory when repeating a smoke. Failed guests are preserved for
inspection; only owned guests should be cleaned up.

```bash
../agents-last-exam/.venv/bin/python run_ale.py plan --arm both
../agents-last-exam/.venv/bin/python run_ale.py run \
  --arm both --output results/ale/run_01 --concurrency 2 --vm-limit 16
```

No assignments or shards are needed. The default is the entire cohort.
`--tasks file.txt` optionally limits a debugging run to explicit task IDs, one
per line. Unknown IDs, duplicates, and empty selections are rejected.
`--concurrency` limits task lineages; `--vm-limit` limits their shared VM pool.
A Phase 1 wave may own several guests. Begin with one lineage when checking a
new machine.

The official evaluator is available only to the outer ALE host. The learning
worker receives public instructions and owned sandbox handles, with no grader
objects. Phase 3 consumes immutable learned memory. Phase 1 branches keep their
Actor contexts after releasing idle guests and replay saved candidates for
ordered memory consolidation.

## Reports

```bash
../agents-last-exam/.venv/bin/python run_ale.py report \
  --runs results/ale/run_01 --output results/ale/report_01
```

`tasks.csv` records each task's baseline and RSI score and source result.
`summary.json` reports observed coverage and missing tasks. A full-cohort mean is
available only when all 67 official scores exist. Duplicate scored attempts
require explicit resolution; the reporter never selects the best score or
substitutes a historical baseline for an unrun RSI task.
