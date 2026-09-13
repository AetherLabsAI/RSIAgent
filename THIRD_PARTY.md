# Dependencies and benchmark attribution

RSIAgent integrates with [OSWorld-V2](https://github.com/xlang-ai/OSWorld-V2),
maintained by the OSWorld contributors. Its source is separately licensed under
Apache-2.0. Benchmark tasks, gated assets, guest images, and hosted websites are
obtained separately and remain subject to their respective terms and access
requirements. They are not bundled in this source release.

The `config/target_queries/` files contain public task instructions used to define
target-conditioned studies. Configuration locks record benchmark revision and
content identities. Host-only rejection tests include small task-derived
denylist fixtures; they are never Agent prompt material.

Python dependencies are installed separately from `requirements.txt`; each
retains its own license. Model names identify configurable third-party services.
Users provide their own service accounts and credentials.
