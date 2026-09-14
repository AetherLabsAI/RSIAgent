# Contributing

Open an issue describing the problem or send a pull request with a focused change.
Include the concrete behavior before and after the change and relevant validation.

From the repository root, use Python 3.12, install
[requirements-dev.txt](../requirements-dev.txt), and run
`python tools/check_rsi_release.py`. Tests use fake guests and model responses;
routine validation should not launch paid model calls or real benchmark tasks.

Preserve the separation between Actor, Verifier, Curriculum, memory commits, and
sealed evaluation. Add a meaningful regression test for changes to those
boundaries or recovery admission. Changes to a role profile or benchmark lock
require explicit provenance and a new experimental lineage.

Never commit credentials, private trajectories, memory snapshots, VM images,
benchmark assets, or generated result directories. Describe infrastructure
failures separately from task failures and keep all planned evaluation outcomes.
