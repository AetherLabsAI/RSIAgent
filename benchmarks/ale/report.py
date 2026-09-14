"""Merge all attempts into a complete-cohort ledger without best-of selection."""

import csv
import json
import math
from pathlib import Path

from .protocol import protocol, write_json


def valid_score(value):
    if isinstance(value, bool):
        raise ValueError("ALE score must be a finite number between zero and one")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("ALE score must be a finite number between zero and one")
    return value


def collect(roots):
    lock = protocol()
    tasks = lock["tasks"]
    rows = {
        task: {
            "task": task,
            "baseline_score": None,
            "rsi_score": None,
            "baseline_source": "",
            "rsi_source": "",
        }
        for task in tasks
    }

    def ingest(task, arm, path):
        path = path.resolve()
        data = json.loads(path.read_text())
        score = data.get("score")
        if score is None:
            return
        source = rows[task][arm + "_source"]
        if source and source != str(path):
            raise RuntimeError(
                f"Duplicate {arm} evaluations for {task}; explicitly resolve attempts before merging"
            )
        rows[task][arm + "_source"] = str(path)
        rows[task][arm + "_score"] = valid_score(score)

    for root in roots:
        root = Path(root)
        for task in tasks:
            for arm, phase in [("baseline", "baseline"), ("rsi", "phase3")]:
                path = root / task / phase / "result.json"
                if path.is_file():
                    ingest(task, arm, path)
    return rows


def report(args):
    if not args.runs:
        raise RuntimeError(
            "report requires --runs with one or more archived run directories"
        )
    rows = collect(args.runs)
    summary = {"cohort_count": 67, "arms": {}}
    for arm in ("baseline", "rsi"):
        scores = [
            r[arm + "_score"] for r in rows.values() if r[arm + "_score"] is not None
        ]
        passes = sum(s == 1.0 for s in scores)
        summary["arms"][arm] = {
            "evaluated": len(scores),
            "missing": 67 - len(scores),
            "full_credit": passes,
            "observed_full_credit_over_67": passes / 67,
            "mean_available_score": sum(scores) / len(scores) if scores else None,
            "complete_cohort_mean_score": sum(scores) / 67
            if len(scores) == 67
            else None,
        }
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        write_json(args.output / "summary.json", summary)
        with (args.output / "tasks.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(next(iter(rows.values()))))
            writer.writeheader()
            writer.writerows(rows.values())
    print(json.dumps(summary, indent=2))
