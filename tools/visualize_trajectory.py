#!/usr/bin/env python3
"""Visualize RSI run trajectory as a formatted timeline.

Reads a run directory and renders the iteration-by-iteration
progression as a human-readable timeline, showing what happened
at each step: model output, program submitted, execution result,
verifier feedback, and final score.

Usage:
    python tools/visualize_trajectory.py /path/to/run/
    python tools/visualize_trajectory.py /path/to/run/ --max-turns 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def find_iter_dirs(root: Path) -> list[Path]:
    """Find all iter_XX directories, sorted numerically."""
    iters = []
    for p in root.iterdir():
        if p.is_dir() and re.match(r"iter_\d+", p.name):
            iters.append(p)
    return sorted(iters, key=lambda p: int(p.name.split("_")[1]))


def read_text(path: Path) -> str:
    """Read a text file, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning empty dict on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def truncate(text: str, max_len: int = 200) -> str:
    """Truncate text to max_len characters."""
    text = text.strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def extract_action(program_path: Path) -> str:
    """Extract a short description of what the program does."""
    if not program_path.exists():
        return "(no program)"
    text = read_text(program_path)
    # Get first meaningful line
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return "(empty program)"
    return truncate(lines[0], 80)


def extract_turn_summary(turn_path: Path) -> str:
    """Extract a summary of the model's turn."""
    text = read_text(turn_path)
    if not text:
        return "(empty turn)"
    return truncate(text, 120)


def format_timeline(run_dir: Path, max_turns: int | None = None) -> str:
    """Format the run trajectory as a timeline."""
    iter_dirs = find_iter_dirs(run_dir)
    if max_turns:
        iter_dirs = iter_dirs[:max_turns]

    if not iter_dirs:
        return f"No iter_XX directories found in {run_dir}"

    # Read final result
    result = read_json(run_dir / "result.json")

    lines = [
        f"RSI Run Trajectory: {run_dir.name}",
        "=" * 70,
        f"  Total iterations: {len(iter_dirs)}",
        f"  Final status:     {result.get('status', 'unknown')}",
        f"  Final score:      {result.get('score', 'N/A')}",
        "",
    ]

    for i, iter_dir in enumerate(iter_dirs, start=1):
        # Read metadata
        meta = read_json(iter_dir / "trace_meta.json")
        secs = meta.get("secs", "?")
        exit_code = meta.get("exit_code", "?")
        timed_out = meta.get("timed_out", False)

        # Read turn
        turn_summary = extract_turn_summary(iter_dir / "turn.txt")

        # Find program file
        program_files = list(iter_dir.glob("program.*"))
        program_desc = extract_action(program_files[0]) if program_files else "(no program)"

        # Check for checks/verifier
        checks = read_json(iter_dir / "checks.json")
        has_checks = bool(checks)
        accepted = len(checks.get("accepted", [])) if has_checks else 0
        rejections = len(checks.get("rejections", [])) if has_checks else 0

        # Check for review (verifier feedback)
        review = read_json(iter_dir / "review.json")
        verdict = review.get("verdict", "") if review else ""

        # Format the timeline entry
        status_icon = "✓" if exit_code == 0 else "✗"
        if timed_out:
            status_icon = "⏱"

        lines.append(f"├─ Iter {i:02d}  [{status_icon}]  {secs}s  exit={exit_code}")
        lines.append(f"│  Turn: {turn_summary}")
        lines.append(f"│  Action: {program_desc}")

        if has_checks:
            lines.append(f"│  Checks: {accepted} accepted, {rejections} rejected")
        if verdict:
            lines.append(f"│  Verifier: {verdict}")

        lines.append("│")

    # Final summary
    lines.append("└─ Final result:")
    lines.append(f"   Status: {result.get('status', 'unknown')}")
    lines.append(f"   Score:  {result.get('score', 'N/A')}")
    if result.get("iters"):
        lines.append(f"   Iters:  {result.get('iters')}")

    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path, help="Run output directory")
    p.add_argument("--max-turns", type=int, default=None,
                   help="Max number of iterations to show (default: all)")
    args = p.parse_args(argv)

    if not args.run_dir.exists():
        print(f"Error: directory not found: {args.run_dir}", file=sys.stderr)
        return 1

    print(format_timeline(args.run_dir, args.max_turns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
