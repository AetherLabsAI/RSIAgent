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
