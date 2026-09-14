"""Resume only a verified, fully committed parallel-wave prefix.

Uncommitted work is archived, never promoted. The last complete Curriculum
conversation, outcomes, memory and original total budget are preserved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from explore.e15_loop import (
    E15InfrastructureError, _atomic_json, _manifest, _read_memory_tree,
    _scope_prior_curriculum_visuals,
)


def _require(condition, detail):
    if not condition:
        raise E15InfrastructureError("parallel-wave resume: " + detail)


def validate_boundary(lineage: Path, target: str, *, project_budget,
                      checkpoint_projects, max_parallel,
                      target_query_conditioned):
    state = json.loads((lineage / "state.json").read_text())
    count = state.get("projects")
    # A rejected guest handoff may never have reached the saved transcripts.
    # Re-auditing those transcripts cannot establish that quarantine was false.
    _require(state.get("status") != "quarantined",
             "quarantined lineages cannot resume from a completed boundary")
    _require(state.get("status") == "infra",
             "lineage is not stopped at an infrastructure boundary")
    _require(type(count) is int and count >= 0
             and state.get("last_project") == count, "invalid project counter")
    expected = {
        "phase1_parallel_waves": True, "project_budget": project_budget,
        "checkpoint_projects": list(checkpoint_projects),
        "max_parallel": max_parallel,
        "target_query_conditioned": target_query_conditioned,
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
    }
    _require(all(state.get(k) == v for k, v in expected.items()),
             "protocol fields drifted")
    _require(not any((lineage.parent / p).exists() for p in ("phase2", "phase3")),
             "a later phase already exists")
    memory = _manifest(_read_memory_tree(str(lineage / "memory")))
    _require(memory == state.get("memory_manifest"), "canonical memory drifted")
    _require(memory == _manifest(_read_memory_tree(str(lineage / "memory_frozen"))),
             "frozen memory differs from stopped canonical memory")

    records = []
    before = {}
    for index in range(1, count + 1):
        episode = lineage / "episodes" / f"ep{index:03d}"
        record = json.loads((episode / "outcome.json").read_text())
        _require(record.get("project_index") == index
                 and record.get("terminal_outcome") in {"PASS", "FAIL"},
                 f"episode {index} is not closed")
        _require(record.get("memory_before") == before,
                 f"episode {index} memory chain drifted")
        _require((episode / "outcome.md").is_file(), "missing rendered outcome")
        before = record["memory_after"]
        records.append(record)
    _require(before == memory, "memory includes a partial/uncommitted project")

    last_wave = records[-1]["wave_index"] if records else 0
    event_path = lineage / "events.jsonl"
    _require(not count or event_path.is_file(), "completed-wave event log is missing")
    completed = [json.loads(line) for line in (event_path.read_text() if event_path.is_file() else "").splitlines()
                 if line.strip()]
    completed = [e for e in completed if e.get("event_type") == "PHASE1_WAVE_COMPLETED"]
    _require((not count and not completed) or (bool(completed)
             and completed[-1]["payload"]["total_projects"] == count
             and completed[-1]["payload"]["wave"] == last_wave),
             "counter is not the end of a completed wave")
    for wave in range(1, last_wave + 1):
        directory = lineage / "waves" / f"wave_{wave:03d}"
        decision = json.loads((directory / "curriculum_decision.json").read_text())
        committed = [r for r in records if r["wave_index"] == wave]
        _require(decision.get("decision") == "WAVE"
                 and [r["project_id"] for r in committed]
                 == [p["id"] for p in decision["projects"]],
                 "completed wave differs from its authored assignment")
        _require((directory / "outcomes.md").is_file(), "wave feedback is missing")

    history, outcomes = [], ""
    if count:
        transcripts = sorted((lineage / "curriculum" / f"wave_{last_wave:03d}").glob(
            "segment_*/transcript.json"))
        _require(bool(transcripts), "completed Curriculum context is missing")
        messages = json.loads(transcripts[-1].read_text()).get("messages")
        _require(isinstance(messages, list) and bool(messages), "invalid Curriculum context")
        history, _ = _scope_prior_curriculum_visuals(messages)
        outcomes = (lineage / "waves" / f"wave_{last_wave:03d}" / "outcomes.md").read_text()

    pending = []
    for directory, prefix, ceiling in (("episodes", "ep", count),
                                       ("waves", "wave_", last_wave),
                                       ("curriculum", "wave_", last_wave)):
        for path in sorted((lineage / directory).glob(prefix + "*")):
            suffix = path.name[len(prefix):]
            _require(suffix.isdigit() and not path.is_symlink(), "unexpected artifact path")
            if int(suffix) <= ceiling:
                continue
            if directory == "episodes":
                _require(not (path / "outcome.json").exists()
                         and not (path / "memory_distillation").exists()
                         and not (path / "memory_reconciliation").exists(),
                         "the pending wave has already started learning")
            pending.append(path)
    return {"projects": count, "wave": last_wave, "history": history,
            "outcomes": outcomes, "pending": pending, "state": state}


def archive_pending(lineage: Path, plan):
    archive = lineage / "boundary_recovery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive.mkdir(parents=True)
    for name in ("state.json", "result.json", "manifest.json", "events.jsonl"):
        if (lineage / name).is_file():
            shutil.copy2(lineage / name, archive / name)
    receipt = {"status": "prepared", "resumed_projects": plan["projects"],
               "last_complete_wave": plan["wave"], "total_budget_unchanged": True,
               "archived_paths": [str(p.relative_to(lineage)) for p in plan["pending"]],
               "memory_manifest": plan["state"]["memory_manifest"]}
    _atomic_json(archive / "receipt.json", receipt)
    for path in plan["pending"]:
        destination = archive / path.relative_to(lineage)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.rename(destination)
    receipt["status"] = "archived_pending_wave"
    _atomic_json(archive / "receipt.json", receipt)
    return archive
