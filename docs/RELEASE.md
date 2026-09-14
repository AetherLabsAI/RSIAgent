# Initial source release

This release exports the reusable RSI runtime from source commit
`8a142a40955b9aa7a4f3b249d988990cb9619633`, including the merged runtime recovery
work and the `curriculum_review` default. It also includes the passive browser
readiness fix from `04421e5e89154b733292620e148b351f9f52330c`.

The source export has a fresh Git history. Credentials, results, trajectories,
preserved experiment archives, task assets, VM images, and machine-specific
experiment launchers are excluded. Historical pilot tests requiring private
artifacts or excluded pilot entrypoints are excluded; shared runtime and protocol
tests are included.

Release-specific changes provide checkout-relative paths, installation and
operation documentation, a sequential three-phase wrapper, and portable test
automation. Internal Forge names remain for configuration compatibility.

`source-manifest.json` records hashes of the exported upstream files before release
edits and the separately applied runtime fix. These are provenance records, not a
claim that every release file is byte-identical to its upstream source.

The release does not publish a benchmark leaderboard. Historical ablations have
different budgets and repeated-draw counts and cannot be reduced to a universal
claim that full RSI always wins. Report task selection, configuration, evaluator
revision, all outcomes, and aggregation rules when publishing measurements.

## Validation

The initial package was validated on Python 3.12 in a fresh virtual environment:

- 697 portable tests passed with `OSWORLD_ROOT` pointing to an absent checkout;
  two opt-in evaluator integration tests were skipped in that run.
- Both evaluator integration tests passed separately in the prepared OSWorld
  environment against the frozen task source.
- The August 8 example's Phase 1 configuration preflight passed against the
  prepared asset release. This did not launch an Actor or a VM.
- Dependency consistency, shell syntax, documentation links, and staged-file
  credential/artifact checks passed.

A new live end-to-end experiment was not launched as part of packaging.

## Distribution status

This initial company-review package has no selected distribution license yet.
Public visibility and licensing remain separate release decisions. Third-party
dependencies retain their own licenses.

## Runtime integration — September 14, 2026

Integrated [Shicheng's PR #5](https://github.com/NickSiboZhu/forge/pull/5),
source commit `e790a0142ad7749ef423ec45d70ac7d8bffe5cde`, with Sibo's follow-up
fixes `de16c4c` (reject quarantined recovery) and
`b09e3a84d5b20538ead876408faa3ea439324db7` (clean up partial scratch exports).
Shicheng's original commit attribution is retained in the company history.

The company integration keeps full wave-decision parsing, ordered project
ledgers, wave counters, persistent Curriculum conversation validation, and the
complete portable test runner. Interrupted, uncommitted work can be archived
only after the completed boundary is validated. The direct `filelock` dependency
is explicitly pinned.

Validation on Python 3.12:

- 794 portable tests passed; the two optional evaluator integration tests passed
  separately against the frozen OSWorld checkout.
- The corrected source PR passed its 464-test suite.
- A disposable Docker/QEMU VM passed both the default HTTP and optional virtio
  Verifier transports: desktop identity, dropped capabilities, hidden Actor
  memory, lossless mode-000 scratch export, symlink exclusion, and rollback of
  the candidate and Actor memory were checked with synthetic files.
- VM ownership labels, loopback port bindings, signal status, and the controller
  OOM policy were checked. Teardown removed the smoke VM and its anonymous
  volume; every preexisting container retained its ID and status.
- Dependency consistency and shell syntax checks passed. The smoke test used
  no model API calls and made no official evaluator calls.

The two historical companion diffs remain unapplied. Their overlap with current
code and the remaining behavior changes are documented in
[runtime recovery notes](rsi-infrastructure-recovery.md#companion-patches).
