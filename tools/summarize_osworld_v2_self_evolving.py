#!/usr/bin/env python3
"""Aggregate the frozen 106-task task-local self-evolving benchmark."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics


REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "config/osworld_v2_glm53_k3_self_evolving.lock.json"


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _owner(task: str, lock: dict) -> str:
    number = int(task[-3:])
    for assignment in lock["assignments"]:
        if assignment["first_task"] <= number <= assignment["last_task"]:
            return str(assignment["owner"])
    raise RuntimeError(f"task falls outside every assignment: {task}")


def collect(results_roots: list[Path], lock: dict) -> dict:
    tasks = tuple(lock["valid_task_ids"])
    seed = int(lock["seed_label"])
    tag = str(lock["tag"])
    model = str(lock["actor_agent"]["model"])
    found: dict[str, dict] = {}
    sources: dict[str, str] = {}
    for root in results_roots:
        for task in tasks:
            path = root / "self_evolving" / task / f"seed{seed}_{tag}" / "result.json"
            if not path.is_file():
                continue
            result = _read_object(path)
            if task in found and result != found[task]:
                raise RuntimeError(
                    f"conflicting duplicate result for {task}: "
                    f"{sources[task]} vs {path}")
            found[task] = result
            sources[task] = str(path)

    scores: list[float] = []
    score_by_owner: dict[str, list[float]] = defaultdict(list)
    statuses = Counter()
    verifier_verdicts = Counter()
    provider_calls: dict[str, Counter] = defaultdict(Counter)
    evolved_tasks = []
    first_pass_tasks = []
    memory_nonempty_tasks = []
    invalid = []
    totals = Counter()
    for task, result in sorted(found.items()):
        expected = {
            "task": task,
            "seed": seed,
            "tag": tag,
            "model": model,
            "official_evaluator_feedback_entered_loop": False,
            "target_verifier_orientation":
                "candidate_blind_pre_actor_same_context",
        }
        mismatch = {
            key: {"expected": value, "observed": result.get(key)}
            for key, value in expected.items() if result.get(key) != value
        }
        try:
            score = float(result["score"])
            if not math.isfinite(score):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            mismatch["score"] = {
                "expected": "finite numeric", "observed": result.get("score")}
            score = None
        integers = {}
        for name in (
                "target_cycles", "evolutions", "practice_projects",
                "final_memory_files", "final_memory_bytes",
                "target_verifier_orientations"):
            value = result.get(name)
            if type(value) is not int or value < 0:
                mismatch[name] = {
                    "expected": "nonnegative integer", "observed": value}
            else:
                integers[name] = value
        if ({"target_cycles", "target_verifier_orientations"}
                <= integers.keys()
                and integers["target_verifier_orientations"]
                != 2 * integers["target_cycles"]):
            mismatch["target_verifier_orientations"] = {
                "expected": 2 * integers["target_cycles"],
                "observed": integers["target_verifier_orientations"],
            }
        orientation_digest = result.get(
            "target_verifier_orientation_report_sha256")
        if (not isinstance(orientation_digest, str)
                or len(orientation_digest) != 64
                or any(character not in "0123456789abcdef"
                       for character in orientation_digest)):
            mismatch["target_verifier_orientation_report_sha256"] = {
                "expected": "lowercase SHA-256",
                "observed": orientation_digest,
            }
        evaluator = result.get("sealed_evaluator") or {}
        if evaluator.get("model") != lock["evaluator"]["model"]:
            mismatch["sealed_evaluator.model"] = {
                "expected": lock["evaluator"]["model"],
                "observed": evaluator.get("model"),
            }
        providers = result.get("llm_providers")
        if not isinstance(providers, dict):
            mismatch["llm_providers"] = {
                "expected": "provider provenance object",
                "observed": type(providers).__name__,
            }
        if mismatch:
            invalid.append({
                "task": task, "path": sources[task], "mismatch": mismatch})
            continue

        scores.append(score)
        score_by_owner[_owner(task, lock)].append(score)
        statuses[str(result.get("status", "<missing>"))] += 1
        verifier_verdicts[str(
            result.get("target_verifier_verdict", "<missing>"))] += 1
        for name, value in integers.items():
            totals[name] += value
        if integers["evolutions"]:
            evolved_tasks.append(task)
        if integers["target_cycles"] == 1 and integers["evolutions"] == 0:
            first_pass_tasks.append(task)
        if integers["final_memory_files"]:
            memory_nonempty_tasks.append(task)
        for provider_model, counts in providers.items():
            if isinstance(counts, dict):
                for provider, count in counts.items():
                    provider_calls[str(provider_model)][str(provider)] += int(count)

    missing = sorted(set(tasks) - set(found))
    owner_summary = {
        owner: {
            "valid_scored_tasks": len(values),
            "mean_score": statistics.fmean(values) if values else None,
        }
        for owner, values in sorted(score_by_owner.items())
    }
    summary = {
        "schema_version": 1,
        "benchmark": lock["benchmark"],
        "task_release": lock["task_release"],
        "tag": tag,
        "seed_label": seed,
        "expected_valid_tasks": len(tasks),
        "excluded_tasks": lock["excluded_tasks"],
        "completed_tasks": len(found),
        "valid_scored_tasks": len(scores),
        "missing_tasks": missing,
        "invalid_results": invalid,
        "mean_score": statistics.fmean(scores) if scores else None,
        "median_score": statistics.median(scores) if scores else None,
        "min_score": min(scores) if scores else None,
        "max_score": max(scores) if scores else None,
        "owners": owner_summary,
        "terminations": dict(sorted(statuses.items())),
        "final_verifier_verdicts": dict(sorted(verifier_verdicts.items())),
        "first_pass_task_count": len(first_pass_tasks),
        "first_pass_tasks": first_pass_tasks,
        "evolved_task_count": len(evolved_tasks),
        "evolved_tasks": evolved_tasks,
        "memory_nonempty_task_count": len(memory_nonempty_tasks),
        "memory_nonempty_tasks": memory_nonempty_tasks,
        "totals": dict(sorted(totals.items())),
        "provider_calls": {
            provider_model: dict(sorted(counts.items()))
            for provider_model, counts in sorted(provider_calls.items())},
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument(
        "--results-root", type=Path, action="append",
        help="repeat for each teammate's results directory; default: forge/results")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    lock = _read_object(args.lock.resolve())
    roots = [path.resolve() for path in (
        args.results_root or [REPO / "results"])]
    summary = collect(roots, lock)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    complete = (
        not summary["missing_tasks"]
        and not summary["invalid_results"]
        and summary["valid_scored_tasks"] == summary["expected_valid_tasks"])
    return 0 if complete or args.allow_incomplete else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"AGGREGATION ERROR: {exc}")
        raise SystemExit(2)
