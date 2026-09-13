#!/usr/bin/env python3
"""Aggregate sealed result.json files for the frozen OSWorld v2 baseline."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "config/osworld_v2_glm53_k3_baseline.lock.json"
TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def collect(results_roots: list[Path], lock: dict) -> dict:
    seed = int(lock["seed_label"])
    tag = str(lock["tag"])
    found = {}
    sources = {}
    for root in results_roots:
        for task in TASKS:
            path = root / task / f"seed{seed}_{tag}" / "result.json"
            if not path.is_file():
                continue
            result = _read_object(path)
            if task in found and result != found[task]:
                raise RuntimeError(
                    f"conflicting duplicate result for {task}: {sources[task]} vs {path}")
            found[task] = result
            sources[task] = str(path)

    scores = []
    statuses = Counter()
    provider_calls = defaultdict(Counter)
    actor_fallback_tasks = []
    invalid = []
    for task, result in sorted(found.items()):
        expected = {
            "task": task,
            "seed": seed,
            "tag": tag,
            "model": lock["actor_agent"]["model"],
        }
        mismatch = {key: {"expected": value, "observed": result.get(key)}
                    for key, value in expected.items() if result.get(key) != value}
        try:
            score = float(result["score"])
        except (KeyError, TypeError, ValueError):
            mismatch["score"] = {"expected": "finite numeric", "observed": result.get("score")}
            score = None
        if mismatch:
            invalid.append({"task": task, "path": sources[task], "mismatch": mismatch})
            continue
        scores.append(score)
        statuses[str(result.get("status", "<missing>"))] += 1
        providers = result.get("llm_providers") or {}
        if not isinstance(providers, dict):
            invalid.append({"task": task, "path": sources[task],
                            "mismatch": {"llm_providers": "not an object"}})
            continue
        for model, counts in providers.items():
            if isinstance(counts, dict):
                for provider, count in counts.items():
                    provider_calls[str(model)][str(provider)] += int(count)
        actor_providers = set((providers.get(lock["actor_agent"]["model"]) or {}).keys())
        if actor_providers - set(lock["actor_agent"]["provider_preference"]):
            actor_fallback_tasks.append(task)

    missing = sorted(set(TASKS) - set(found))
    summary = {
        "schema_version": 1,
        "tag": tag,
        "seed_label": seed,
        "expected_tasks": len(TASKS),
        "completed_tasks": len(found),
        "valid_scored_tasks": len(scores),
        "missing_tasks": missing,
        "invalid_results": invalid,
        "mean_score": statistics.fmean(scores) if scores else None,
        "median_score": statistics.median(scores) if scores else None,
        "min_score": min(scores) if scores else None,
        "max_score": max(scores) if scores else None,
        "statuses": dict(sorted(statuses.items())),
        "provider_calls": {
            model: dict(sorted(counts.items()))
            for model, counts in sorted(provider_calls.items())},
        "actor_provider_fallback_tasks": actor_fallback_tasks,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--results-root", type=Path, action="append",
                        help="repeat for results synced from multiple teammates; "
                             "default: forge/results")
    parser.add_argument("--output", type=Path,
                        help="also write the summary JSON to this path")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    lock = _read_object(args.lock)
    roots = [path.resolve() for path in (args.results_root or [REPO / "results"])]
    summary = collect(roots, lock)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    complete = (not summary["missing_tasks"] and not summary["invalid_results"]
                and summary["valid_scored_tasks"] == summary["expected_tasks"])
    return 0 if complete or args.allow_incomplete else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"AGGREGATION ERROR: {exc}")
        raise SystemExit(2)
