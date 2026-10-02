#!/usr/bin/env python3
"""Detect RSI saturation from historical run results.

Analyzes a sequence of RSI run outputs to determine if the system
has saturated (stopped improving). This answers the core RSI question:
"when should we stop training?"

Saturation criteria:
1. **No improvement**: N consecutive runs with no score improvement > threshold
2. **Diminishing returns**: Average improvement per run is decreasing
3. **Score plateau**: Score variance is below threshold for M runs

Usage:
    python tools/detect_saturation.py /path/to/runs/
    python tools/detect_saturation.py /path/to/runs/ --min-improvement 0.05 --stall-rounds 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def find_result_files(root: Path) -> list[Path]:
    """Find all result.json files, sorted by path (assumes path encodes order)."""
    return sorted(root.rglob("result.json"))


def load_scores(files: list[Path], root: Path) -> list[dict[str, Any]]:
    """Load scores from result files, skipping invalid ones."""
    runs = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            score = data.get("score")
            if isinstance(score, (int, float)):
                try:
                    rel_path = str(f.relative_to(root))
                except ValueError:
                    rel_path = str(f)
                runs.append({
                    "path": rel_path,
                    "score": float(score),
                    "iters": data.get("iters"),
                    "status": data.get("status", "unknown"),
                })
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def detect_saturation(
    runs: list[dict[str, Any]],
    min_improvement: float = 0.05,
    stall_rounds: int = 3,
) -> dict[str, Any]:
    """Detect if RSI has saturated from a sequence of run results.

    Args:
        runs: List of run dicts with 'score' key, in chronological order.
        min_improvement: Minimum score improvement to count as progress (0-1).
        stall_rounds: Number of consecutive no-improvement rounds to declare stall.

    Returns:
        Dict with saturation analysis.
    """
    if len(runs) < 2:
        return {
            "saturated": None,
            "reason": "Not enough runs to determine saturation (need >= 2)",
            "runs_analyzed": len(runs),
        }

    scores = [r["score"] for r in runs]

    # 1. Check for stall: consecutive rounds with no improvement > threshold
    best_score = scores[0]
    stall_count = 0
    stall_start = None

    for i, score in enumerate(scores[1:], start=1):
        improvement = score - best_score
        if improvement >= min_improvement:
            best_score = score
            stall_count = 0
            stall_start = None
        else:
            stall_count += 1
            if stall_start is None:
                stall_start = i

    # 2. Diminishing returns: average improvement in first half vs second half
    mid = len(scores) // 2
    first_half_improvement = scores[mid] - scores[0] if mid > 0 else 0
    second_half_improvement = scores[-1] - scores[mid] if mid > 0 else 0
    diminishing_returns = second_half_improvement < first_half_improvement * 0.3 if mid > 0 else False

    # 3. Score plateau: variance in last M runs is very low
    last_m = scores[-stall_rounds:] if len(scores) >= stall_rounds else scores
    score_variance = statistics.pvariance(last_m) if len(last_m) >= 2 else 0
    is_plateau = score_variance < 0.001  # very low variance

    # Determine saturation
    saturated = stall_count >= stall_rounds
    near_saturated = stall_count >= stall_rounds - 1 and stall_count < stall_rounds

    reasons = []
    if stall_count >= stall_rounds:
        reasons.append(f"{stall_count} consecutive rounds with < {min_improvement:.0%} improvement")
    if diminishing_returns:
        reasons.append("diminishing returns: second-half improvement < 30% of first-half")
    if is_plateau:
        reasons.append(f"score plateau: variance in last {len(last_m)} runs < 0.001")

    return {
        "saturated": saturated,
        "near_saturated": near_saturated and not saturated,
        "reason": "; ".join(reasons) if reasons else "still improving",
        "runs_analyzed": len(runs),
        "initial_score": scores[0],
        "final_score": scores[-1],
        "best_score": max(scores),
        "total_improvement": scores[-1] - scores[0],
        "stall_rounds_counted": stall_count,
        "stall_threshold": stall_rounds,
        "diminishing_returns": diminishing_returns,
        "score_plateau": is_plateau,
        "score_variance_last_rounds": round(score_variance, 6),
    }


def format_text(report: dict[str, Any]) -> str:
    """Format saturation report as human-readable text."""
    if report.get("saturated") is None:
        return f"⚠️  {report['reason']}"

    status = "🔴 SATURATED" if report["saturated"] else (
        "🟡 NEAR SATURATION" if report["near_saturated"] else "🟢 STILL IMPROVING"
    )

    lines = [
        "RSI Saturation Detection Report",
        "=" * 60,
        f"  Status:           {status}",
        f"  Reason:           {report['reason']}",
        "",
        f"  Runs analyzed:    {report['runs_analyzed']}",
        f"  Initial score:    {report['initial_score']:.4f}",
        f"  Final score:      {report['final_score']:.4f}",
        f"  Best score:       {report['best_score']:.4f}",
        f"  Total improvement: {report['total_improvement']:+.4f}",
        "",
        f"  Stall rounds:     {report['stall_rounds_counted']} / {report['stall_threshold']}",
        f"  Diminishing returns: {report['diminishing_returns']}",
        f"  Score plateau:    {report['score_plateau']} (variance: {report['score_variance_last_rounds']})",
    ]

    if report["saturated"]:
        lines.append("")
        lines.append("Recommendation: Consider stopping RSI training.")
        lines.append("The system has plateaued and further rounds are unlikely to yield significant gains.")

    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path, help="Directory containing run result files")
    p.add_argument("--min-improvement", type=float, default=0.05,
                   help="Minimum improvement to count as progress (default: 0.05 = 5%%)")
    p.add_argument("--stall-rounds", type=int, default=3,
                   help="Consecutive no-improvement rounds to declare saturation (default: 3)")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="Output format (default: text)")
    args = p.parse_args(argv)

    if not args.root.exists():
        print(f"Error: directory not found: {args.root}", file=sys.stderr)
        return 1

    files = find_result_files(args.root)
    runs = load_scores(files, args.root)

    if not runs:
        print(f"Error: no valid result.json files found under {args.root}", file=sys.stderr)
        return 1

    report = detect_saturation(runs, args.min_improvement, args.stall_rounds)

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
