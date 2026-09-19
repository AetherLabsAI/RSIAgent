#!/usr/bin/env python3
"""Analyze RSI run result directories and produce structured diagnostics.

Scans a run output tree for ``result.json`` files and reports:
- overall task statistics (count, mean score, pass rate)
- iteration efficiency (mean attempts, mean wall time)
- status distribution (PASS / FAIL / STALLED / error)
- per-task detail sorted by score

Usage:
    python tools/analyze_run.py /path/to/run/output
    python tools/analyze_run.py /path/to/run/output --format json
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


def load_result(path: Path) -> dict[str, Any]:
    """Load a single result.json, returning an empty dict on parse error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def classify_status(result: dict[str, Any]) -> str:
    """Classify a result into a coarse status bucket."""
    status = str(result.get("status", "unknown")).lower()
    score = result.get("score")
    if score is not None:
        try:
            if float(score) >= 1.0:
                return "pass"
            if float(score) > 0:
                return "partial"
        except (TypeError, ValueError):
            pass
    if "fail" in status or "error" in status:
        return "fail"
    if "stall" in status:
        return "stalled"
    if "complete" in status or "done" in status or "success" in status:
        return "pass"
    return status or "unknown"


def analyze(root: Path) -> dict[str, Any]:
    """Analyze all result.json files under root and return a report dict."""
    files = find_result_files(root)
    if not files:
        return {"error": f"No result.json files found under {root}", "root": str(root)}

    results = []
    for f in files:
        data = load_result(f)
        if not data:
            continue
        task_name = f.parent.name
        rel = f.relative_to(root)
        results.append({
            "task": task_name,
            "path": str(rel),
            "score": data.get("score"),
            "iters": data.get("iters"),
            "status": data.get("status", "unknown"),
            "duration_sec": data.get("duration_sec") or data.get("elapsed"),
            "classified": classify_status(data),
        })

    scores = [r["score"] for r in results if isinstance(r["score"], (int, float))]
    iters = [r["iters"] for r in results if isinstance(r["iters"], int) and r["iters"] > 0]
    durations = [r["duration_sec"] for r in results
                 if isinstance(r["duration_sec"], (int, float)) and r["duration_sec"] > 0]

    status_counts: dict[str, int] = {}
    for r in results:
        s = r["classified"]
        status_counts[s] = status_counts.get(s, 0) + 1

    passed = status_counts.get("pass", 0)
    total = len(results)

    return {
        "root": str(root),
        "total_attempts": total,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "mean_score": round(statistics.mean(scores), 4) if scores else None,
        "median_score": round(statistics.median(scores), 4) if scores else None,
        "min_score": round(min(scores), 4) if scores else None,
        "max_score": round(max(scores), 4) if scores else None,
        "mean_iters": round(statistics.mean(iters), 1) if iters else None,
        "mean_duration_sec": round(statistics.mean(durations), 1) if durations else None,
        "status_distribution": status_counts,
        "tasks": sorted(results, key=lambda r: (-(r["score"] or 0), r["task"])),
    }


def format_text(report: dict[str, Any]) -> str:
    """Format the report as human-readable text."""
    if "error" in report:
        return f"Error: {report['error']}"

    lines = [
        f"RSI Run Analysis: {report['root']}",
        "=" * 60,
        f"  Total attempts:    {report['total_attempts']}",
        f"  Pass rate:        {report['pass_rate']*100:.1f}% ({report['status_distribution'].get('pass', 0)} passed)",
    ]
    if report["mean_score"] is not None:
        lines.append(f"  Mean score:       {report['mean_score']} (median: {report['median_score']})")
        lines.append(f"  Score range:      {report['min_score']} — {report['max_score']}")
    if report["mean_iters"] is not None:
        lines.append(f"  Mean iterations: {report['mean_iters']}")
    if report["mean_duration_sec"] is not None:
        mins = report["mean_duration_sec"] / 60
        lines.append(f"  Mean duration:    {report['mean_duration_sec']:.0f}s ({mins:.1f} min)")
    lines.append("")
    lines.append("Status distribution:")
    for s, c in sorted(report["status_distribution"].items(), key=lambda x: -x[1]):
        lines.append(f"  {s:12s} {c}")
    lines.append("")
    lines.append("Tasks (sorted by score):")
    for t in report["tasks"][:20]:
        score = t["score"] if t["score"] is not None else "N/A"
        iters = t["iters"] if t["iters"] is not None else "?"
        lines.append(f"  {str(score):>6}  {t['classified']:10s}  {iters} iters  {t['task']}")
    if len(report["tasks"]) > 20:
        lines.append(f"  ... and {len(report['tasks']) - 20} more")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path, help="Run output directory to analyze")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="Output format (default: text)")
    args = p.parse_args(argv)

    if not args.root.exists():
        print(f"Error: directory not found: {args.root}", file=sys.stderr)
        return 1

    report = analyze(args.root)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
