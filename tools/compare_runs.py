#!/usr/bin/env python3
"""Compare two RSI run directories (e.g., baseline vs RSI arm).

Analyzes and compares two run output directories to quantify the improvement
from RSI. This is the core evaluation tool for RSI research: it answers
"how much better is RSI than baseline?"

Usage:
    python tools/compare_runs.py /path/to/baseline/ /path/to/rsi/
    python tools/compare_runs.py /path/to/baseline/ /path/to/rsi/ --format json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def find_result_files(root: Path) -> list[Path]:
    """Recursively find all result.json files under root."""
    return sorted(root.rglob("result.json"))


def load_runs(root: Path) -> list[dict[str, Any]]:
    """Load all result.json files from a run directory."""
    runs = []
    for f in find_result_files(root):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            score = data.get("score")
            if isinstance(score, (int, float)):
                runs.append({
                    "path": str(f.relative_to(root)),
                    "score": float(score),
                    "iters": data.get("iters"),
                    "status": data.get("status", "unknown"),
                })
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def compare_runs(baseline_runs: list[dict[str, Any]], rsi_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two sets of run results."""
    baseline_scores = [r["score"] for r in baseline_runs]
    rsi_scores = [r["score"] for r in rsi_runs]

    baseline_iters = [r["iters"] for r in baseline_runs if isinstance(r["iters"], int)]
    rsi_iters = [r["iters"] for r in rsi_runs if isinstance(r["iters"], int)]

    # Per-task comparison (match by path basename)
    baseline_by_task = {Path(r["path"]).parent.name: r for r in baseline_runs}
    rsi_by_task = {Path(r["path"]).parent.name: r for r in rsi_runs}
    common_tasks = set(baseline_by_task.keys()) & set(rsi_by_task.keys())

    task_comparisons = []
    improved = 0
    degraded = 0
    same = 0
    for task in sorted(common_tasks):
        b_score = baseline_by_task[task]["score"]
        r_score = rsi_by_task[task]["score"]
        delta = r_score - b_score
        if delta > 0.01:
            improved += 1
        elif delta < -0.01:
            degraded += 1
        else:
            same += 1
        task_comparisons.append({
            "task": task,
            "baseline_score": b_score,
            "rsi_score": r_score,
            "delta": round(delta, 4),
        })

    return {
        "baseline": {
            "runs": len(baseline_runs),
            "mean_score": round(statistics.mean(baseline_scores), 4) if baseline_scores else None,
            "median_score": round(statistics.median(baseline_scores), 4) if baseline_scores else None,
            "mean_iters": round(statistics.mean(baseline_iters), 1) if baseline_iters else None,
        },
        "rsi": {
            "runs": len(rsi_runs),
            "mean_score": round(statistics.mean(rsi_scores), 4) if rsi_scores else None,
            "median_score": round(statistics.median(rsi_scores), 4) if rsi_scores else None,
            "mean_iters": round(statistics.mean(rsi_iters), 1) if rsi_iters else None,
        },
        "improvement": {
            "mean_delta": round(statistics.mean(rsi_scores) - statistics.mean(baseline_scores), 4) if baseline_scores and rsi_scores else None,
            "median_delta": round(statistics.median(rsi_scores) - statistics.median(baseline_scores), 4) if baseline_scores and rsi_scores else None,
            "improved_tasks": improved,
            "degraded_tasks": degraded,
            "same_tasks": same,
            "total_common_tasks": len(common_tasks),
        },
        "task_comparisons": task_comparisons,
    }


def format_text(report: dict[str, Any]) -> str:
    """Format comparison as human-readable text."""
    b = report["baseline"]
    r = report["rsi"]
    imp = report["improvement"]

    lines = [
        "RSI vs Baseline Comparison",
        "=" * 60,
        "",
        f"  {'Metric':<25} {'Baseline':>12} {'RSI':>12} {'Delta':>12}",
        f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}",
    ]

    if b["mean_score"] is not None and r["mean_score"] is not None:
        delta = imp["mean_delta"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"  {'Mean Score':<25} {b['mean_score']:>12.4f} {r['mean_score']:>12.4f} {sign}{delta:>11.4f}")

    if b["median_score"] is not None and r["median_score"] is not None:
        delta = imp["median_delta"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"  {'Median Score':<25} {b['median_score']:>12.4f} {r['median_score']:>12.4f} {sign}{delta:>11.4f}")

    if b["mean_iters"] is not None and r["mean_iters"] is not None:
        delta = r["mean_iters"] - b["mean_iters"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"  {'Mean Iterations':<25} {b['mean_iters']:>12.1f} {r['mean_iters']:>12.1f} {sign}{delta:>11.1f}")

    lines.append("")
    lines.append(f"  Total common tasks: {imp['total_common_tasks']}")
    lines.append(f"    Improved:  {imp['improved_tasks']}")
    lines.append(f"    Degraded:  {imp['degraded_tasks']}")
    lines.append(f"    Same:      {imp['same_tasks']}")

    if report["task_comparisons"]:
        lines.append("")
        lines.append("  Task details (top 10 by improvement):")
        sorted_tasks = sorted(report["task_comparisons"], key=lambda t: -t["delta"])
        for t in sorted_tasks[:10]:
            sign = "+" if t["delta"] >= 0 else ""
            lines.append(f"    {t['task']:<20} {t['baseline_score']:.2f} → {t['rsi_score']:.2f}  ({sign}{t['delta']:.2f})")

    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("baseline", type=Path, help="Baseline run output directory")
    p.add_argument("rsi", type=Path, help="RSI run output directory")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="Output format (default: text)")
    args = p.parse_args(argv)

    if not args.baseline.exists():
        print(f"Error: baseline directory not found: {args.baseline}", file=sys.stderr)
        return 1
    if not args.rsi.exists():
        print(f"Error: RSI directory not found: {args.rsi}", file=sys.stderr)
        return 1

    baseline_runs = load_runs(args.baseline)
    rsi_runs = load_runs(args.rsi)

    if not baseline_runs:
        print(f"Error: no valid result.json files found in {args.baseline}", file=sys.stderr)
        return 1
    if not rsi_runs:
        print(f"Error: no valid result.json files found in {args.rsi}", file=sys.stderr)
        return 1

    report = compare_runs(baseline_runs, rsi_runs)

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
