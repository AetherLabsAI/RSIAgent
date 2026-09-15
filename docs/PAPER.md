# Paper and README sources

The README describes **RSIAgent: Autonomous Exploration for Recursive
Self-improvement in New Environments**, by Sibo Zhu, Shicheng Fan, Xinyue Wang,
Wenyi Wu, Kun Zhou, and Biwei Huang.

Read the [public paper PDF](RSIAgent.pdf).

The PDF was exported on September 15, 2026, from manuscript revision
`0234fa06a107b0384b5f480615f233faee22ae37`, verified against the remote Overleaf
head on that date. It is a locally compiled, 49-page technical-report snapshot.
The export used Tectonic with a build-only package-option adjustment for `xcolor`;
the manuscript text and Overleaf source were not changed. No arXiv identifier or
conference acceptance is implied.

The method text and framework figure were read from a clean local checkout at
revision `6e36cb432ddda99f8cc60a8af74d72b1a9b693d5` on September 13, 2026.
The main results and reporting scope were updated from local manuscript revision
`8c8d6006e6da2b0670bcd8f45d6428d047102afc` on September 14, 2026.

## Method sources

The title, author list, and overview follow `main.tex`, especially the abstract,
Introduction, Multi-Agent Harness Framework, and Multi-stage Autonomous
Exploration for RSI sections. The runtime mapping follows the Implementation and
Agent Interfaces appendix:

| Paper terminology | Runtime |
| --- | --- |
| Broad Recursive Self-exploration (BRS) | Phase 1: parallel experience acquisition, followed by ordered memory consolidation |
| Deep Recursive Self-exploration (DRS) | Phase 2: sequential target attempts and practice selected by the Curriculum Agent |
| Test-time memory reuse | Phase 3: the Actor Agent and Verifier Agent use the shared framework with frozen memory and sealed evaluation |

The failure-analysis summary follows the manuscript's Insufficiently Targeted
Exploration, Incomplete Verification, and Unreliable Memory Consolidation sections.

## Figure provenance

[The README framework image](assets/framework.png) is a faithful PNG rendering
of `assets/ExRSI_framework.pdf` at the original method revision above. This is the
figure labeled `fig:rsi-framework` in `main.tex`, which illustrates the method with a
FreeCAD task. Its original labels, artwork, and layout are preserved.

Source PDF SHA-256:

```text
eb33267920acabbf1706247b4f91e501d3847ef4dae28ff954cc3ea9fce7f452
```

The single PDF page was rendered with PyMuPDF 1.28.2, a scale factor of
`2400 / page.rect.width`, and `alpha=False`. The PDF is not bundled; the README
links to the full-resolution PNG stored in this repository.

## Reported results and scope

The README's partial-credit values are transcribed from `tab:main-results` in
`main.tex`. They are percentages, as are the corresponding full-credit rates:

| Benchmark | Partial w/o RSI | Partial with RSI | Full credit w/o RSI | Full credit with RSI |
| --- | ---: | ---: | ---: | ---: |
| OSWorld 2.0, 0808 offline | 71.97 | 78.98 | 37.80 | 42.68 |
| Agents' Last Exam, Near-term | 83.75 | 84.82 | 49.25 | 50.75 |

The RSI Task Selection and Reporting and Main-Table Reporting Details appendices
define how these figures were aggregated:

- **OSWorld:** 82 tasks, including a zero for T082's setup failure. The RSI row
  uses 41 reported non-diagnostic RSI scores, including regressions and recorded
  retries. The remaining 41 tasks retain their baseline scores.
- **ALE:** all 67 Near-term tasks. The RSI row uses 19 reported RSI-column
  entries and retains 48 baseline scores. The three GPU baseline results are
  included in both aggregates under the baseline-retention rule. Full credit is
  achieved on 33/67 tasks without RSI and 34/67 tasks in the RSI aggregate.
  Entries include local corrected grades, ECG results qualified by public-label
  transfer, and a no-BRS Tax Form variant.
- **Comparison scope:** retained baselines are not new RSI evaluations.
  Selected runs, checkpoints, budgets, and evaluation scopes are not fully
  matched. These tables are manuscript-reported results, not a new result-file
  audit or a matched-protocol leaderboard submission by this release.

## Stage ablations and the public runner

The four-task stage comparison uses an exploratory cohort selected for recorded
improvements: T080, T085, T089, and T106. `appendix/ablation_protocol.tex` describes
the historical baselines, single-stage scores, and two historical evaluation
draws averaged for each full-RSI result.

The broad-only condition evaluates preserved BRS memory directly. The deep-only
condition starts with empty memory and allows **at most two practice projects
selected by the Curriculum Agent**; target attempts and memory updates are additional.
The Actor Agent retains memory access, while the Curriculum Agent does not
directly read its memory in those comparisons.

Those ablations used separate experiment controls. The released default instead
uses `curriculum_review` and a read-only memory view for the Curriculum Agent, with no
two-project cap. See [Architecture](ARCHITECTURE.md) and [Operations](OPERATIONS.md)
for the supported runner interfaces.
