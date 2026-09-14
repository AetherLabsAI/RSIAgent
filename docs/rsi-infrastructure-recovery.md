# RSI runtime and recovery fixes

This patch addresses transport failures, VM lifecycle leaks, and incomplete
practice handoffs observed during parallel RSI execution.

- Each thread retains its own model reasoning; concurrent calls cannot attach
  another branch's reasoning to the next request.
- HTTP 200 responses containing provider errors follow the embedded status
  code's retry policy. Retrying an empty response preserves request options.
- Simulated tool-call envelopes cannot authorize a trailing `done` action.
  `FORGE_JSON_ACTION_RETRY=1` optionally requests JSON object output after an
  unparseable reply; the existing parser still decides whether an action exists.
- Guest signal termination and staging failures remain transport evidence,
  including when wrapper diagnostics arrive after the output envelope.
- Verifier replay preserves the inspection authority. Private scratch export
  handles unreadable files through a confined reader while model programs retain
  the desktop UID, dropped capabilities, and isolated scratch namespace.
- Phase 1 records branch failures immediately and drains sibling futures before
  cleanup. One unscored physical failure may restart from immutable fixtures;
  terminal verdicts, learning commits, and boundary violations are not retried.
- Explicit `--resume-completed-boundary` recovery validates completed waves,
  memory manifests, protocol fields, and the absence of later phases before
  archiving incomplete work. It preserves the existing project budget.
  Quarantined lineages are always rejected: saved transcripts may omit the
  rejected guest artifact, so a clean transcript audit cannot clear quarantine.
- Release-specific task validation happens after loading the benchmark profile, so a legacy
  exclusion does not reject a task enabled in the 0808 release.

## Docker provider behavior

The rollback-mirror adapter permits two concurrent cold boots per host UID and
caches port discovery only for one allocation. Docker and allocation-lock
operations have a 300-second timeout. Ready guests are not limited to two.

Set `FORGE_VM_OWNER` to an experiment identifier to label launched containers.
A unique launch token limits cleanup after a partially failed Docker launch to
that launch. A readiness timeout can retry once, before task setup or agent
execution, after ownership validation and archival of boot diagnostics.
`FORGE_BOOT_DIAGNOSTICS_DIR` overrides the default `results/boot_failures`.
Normal teardown removes the owned disposable container and anonymous volume.

`FORGE_DNSMASQ_START_RETRY=1` opts into a wrapper that retries only the transient
inotify startup error; other dnsmasq failures propagate unchanged. It does not
change unrelated container environment entries or mounts.

Before task setup, Ubuntu guests receive a runtime-only `OOMPolicy=continue`
service drop-in so an OOM-killed child command does not stop the controller.
The controller PID and active state are checked; RAM limits and OOM scores are
not changed, and the service is not restarted.

`FORGE_TRUSTED_VERIFIER_TRANSPORT=virtio` optionally provisions a host-only
Verifier setup channel before task setup disables desktop sudo. Its socket is
container-loopback-only and unpublished; the guest device is root-only. It
runs trusted isolation wrappers, which still drop privileges before agent code.

## Validation

Run `python tools/check_rsi_release.py` in the existing OSWorld Python environment.
The portable suite uses synthetic fixtures and mocked provider interfaces; it
does not call model APIs or start VMs. A live compatibility smoke test remains
appropriate when deploying against a different Docker/OSWorld image version.

## Companion patches

Additional infra fixes are supplied as small unified diffs so this handoff
contains only the changed lines from files that also hold legacy task fixtures:

- `patches/rsi-practice-verifier-contract.patch`: disable target-only UNVERIFIED
  and evolution routes in the practice adapter, whose caller consumes PASS/FAIL.
- `patches/rsi-phase-budget.patch`: retain cumulative work/action ceilings across
  same-context segments and preserve incomplete Curriculum fixtures. Includes
  regression tests and updates the legacy fake-call signatures.

These companion patches are **not applied** to the source in this PR. Apply separately
with `git apply --check <patch>` followed by `git apply <patch>`, then run the
portable suite and `pytest -q tests/test_phase_emergency_budget.py`. The legacy
`tests/test_e15_loop.py` suite also depends on historical task fixtures.
