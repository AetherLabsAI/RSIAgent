# Dependencies and benchmark attribution

RSIAgent is licensed under the [Apache License 2.0](../LICENSE). The third-party
components and assets described below retain their respective licenses and terms.

RSIAgent integrates with [OSWorld-V2](https://github.com/xlang-ai/OSWorld-V2),
maintained by the OSWorld contributors. Its source is separately licensed under
[Apache-2.0](https://github.com/xlang-ai/OSWorld-V2/blob/main/LICENSE).
Benchmark tasks, gated assets, guest images, and hosted websites are
obtained separately and remain subject to their respective terms and access
requirements. They are not bundled in this source release.

The `config/target_queries/` files contain public task instructions used to define
target-conditioned studies. Configuration locks record benchmark revision and
content identities. Host-only rejection tests include small task-derived
denylist fixtures; they are never Agent prompt material.

Python dependencies are installed separately from `requirements.txt`; each
retains its own license. Model names identify configurable third-party services.
Users provide their own service accounts and credentials.

The [ALE evaluation framework](https://github.com/rdi-berkeley/agents-last-exam)
is installed separately at the revision in `config/ale/protocol.lock.json`.
Its source and task data have separate upstream license files (`LICENSE` and
`LICENSE-DATA`). The QEMU runner derives from the pinned
`agentslastexam/ale-qemu` image. Guest operating systems, licensed applications,
images, and task datasets are not redistributed by RSIAgent; use the upstream
installation and access instructions.
