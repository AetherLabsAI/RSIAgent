# Source and validation

This revision consolidates the current OSWorld runtime and the ALE Near-term
integration into one public source tree. Runtime modules are organized by
responsibility; benchmark adapters live under `benchmarks/osworld/` and
`benchmarks/ale/`. Role profiles live under `config/roles/`, with one current
configuration set per benchmark.

## Provenance

- Public repository base: `e7f713028a484ac10747bb68fb6c0bf61502a7cb`.
- Initial runtime export: `8a142a40955b9aa7a4f3b249d988990cb9619633`.
- Passive browser readiness fix: `04421e5e89154b733292620e148b351f9f52330c`.
- Infrastructure and recovery integration: source `e790a0142ad7749ef423ec45d70ac7d8bffe5cde`,
  with quarantine and partial-export fixes already included in the public base.
- ALE integration source: `a92f5bed64c98ed690a984b2c6d628e0a1271649`.
- ALE benchmark: `d10fb61a14f9719774c3520c5763068b28ef5546`.

The previous export's file hashes remain in `source-manifest.json` as historical
provenance. That manifest describes the original export, including names which
no longer exist; it is not the current runtime's file inventory.

## Maintained behavior

The current model profiles, reasoning settings, Verifier isolation, memory
commit protocol, Phase 1 wave ordering, and Phase 2 `curriculum_review` policy are
preserved. The merged infrastructure fixes remain in the shared runtime.

ALE adds a longer VM checkpoint timeout and permits verified Phase 1 branches
to release idle guests before ordered memory commits. It retains their Actor
contexts and replays exact captured candidates when a guest is reacquired.
OSWorld keeps its existing default checkpoint timeout and wave behavior.

The public interface has no personnel assignments or shard launchers. Historical
pilot runners, their exclusive state machines and prompts, earlier OSWorld
profiles, and unapplied companion patch files have been removed. New runs use
`RSIAGENT_*` environment variables. Old experiment directories and renamed
internal APIs are not a compatibility target.

The OSWorld release remains August 8, 2026. Its officially pinned VM artifact
still carries a June release tag; that is an upstream artifact identity, not a
second supported runtime.

## Validation

The September 14, 2026 release checks passed: 717 portable tests, plus the two
optional evaluator tests in the pinned OSWorld environment. The full OSWorld
batch dry run covers 108 tasks (432 commands for baseline and RSI together).
All seven current model profiles retain their previous effective settings.

Real VM smoke passed over both OSWorld HTTP and virtio transports (10 checks
each), and on ALE Linux (16 checks, including a fresh guest reset). ALE Windows
and GPU execution were not tested on real guests in this release.

Run `python tools/check_rsi_release.py` for the portable suite. It covers phase
boundaries, failure recovery, memory integrity, VM ownership, role configuration,
batch execution, and ALE adapters. Two evaluator tests require the separately
installed OSWorld evaluator and are opt-in with `RSI_TEST_OSWORLD_INTEGRATION=1`.

Real guest smoke commands are documented in the README. They check transport,
project material replay, private memory isolation, checkpoint rollback, and
persistent Verifier scratch without model or official grading calls. Smoke
success establishes runtime mechanics, not a newly measured benchmark score.

Historical paper aggregates retain their original reporting scope in
[PAPER.md](PAPER.md). New reports keep all task outcomes and distinguish an
infrastructure failure from an official score.
