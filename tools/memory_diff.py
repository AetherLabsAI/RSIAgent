#!/usr/bin/env python3
"""Diff two RSI memory snapshots and report structural changes.

This is a diagnostic tool for understanding what the Actor Agent learned
across evolution rounds. It compares two memory directories and reports:

- File-level changes (added / removed / modified)
- Size delta (total bytes before/after)
- Tree hash comparison (proves structural difference)
- Top changed files by size delta

Usage:
    python tools/memory_diff.py /path/to/memory_before /path/to/memory_after
    python tools/memory_diff.py /path/to/memory_before /path/to/memory_after --format json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def read_memory_tree(root: Path) -> dict[str, bytes]:
    """Read all files under a memory directory into a {relpath: content} dict."""
    tree: dict[str, bytes] = {}
    if not root.exists():
        return tree
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = str(p.relative_to(root))
            try:
                tree[rel] = p.read_bytes()
            except OSError:
                continue
    return tree


def tree_sha256(tree: dict[str, bytes]) -> str:
    """Compute canonical SHA-256 of a memory tree (consistent with memory_record)."""
    hashes = {rel: hashlib.sha256(content).hexdigest() for rel, content in tree.items()}
    normalized = dict(sorted(hashes.items()))
    rendered = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def diff_trees(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, Any]:
    """Compare two memory trees and return a structured diff."""
    before_keys = set(before.keys())
    after_keys = set(after.keys())

    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)

    modified = []
    for k in sorted(before_keys & after_keys):
        if before[k] != after[k]:
            modified.append({
                "path": k,
                "before_bytes": len(before[k]),
                "after_bytes": len(after[k]),
                "delta_bytes": len(after[k]) - len(before[k]),
            })

    return {
        "added_files": added,
        "removed_files": removed,
        "modified_files": modified,
        "added_count": len(added),
        "removed_count": len(removed),
        "modified_count": len(modified),
    }


def analyze(before_dir: Path, after_dir: Path) -> dict[str, Any]:
    """Analyze the diff between two memory directories."""
    before = read_memory_tree(before_dir)
    after = read_memory_tree(after_dir)

    before_bytes = sum(len(c) for c in before.values())
    after_bytes = sum(len(c) for c in after.values())

    diff = diff_trees(before, after)

    # Top modified files by absolute size delta
    top_modified = sorted(
        diff["modified_files"],
        key=lambda m: abs(m["delta_bytes"]),
        reverse=True,
    )[:10]

    return {
        "before": {
            "path": str(before_dir),
            "files": len(before),
            "bytes": before_bytes,
            "tree_sha256": tree_sha256(before),
        },
        "after": {
            "path": str(after_dir),
            "files": len(after),
            "bytes": after_bytes,
            "tree_sha256": tree_sha256(after),
        },
        "size_delta_bytes": after_bytes - before_bytes,
        "tree_changed": tree_sha256(before) != tree_sha256(after),
        "added_count": diff["added_count"],
        "removed_count": diff["removed_count"],
        "modified_count": diff["modified_count"],
        "added_files": diff["added_files"],
        "removed_files": diff["removed_files"],
        "top_modified": top_modified,
    }


def format_text(report: dict[str, Any]) -> str:
    """Format the diff report as human-readable text."""
    lines = [
        "Memory Diff Report",
        "=" * 60,
        f"  Before:  {report['before']['path']}",
        f"           {report['before']['files']} files, {report['before']['bytes']} bytes",
        f"           tree: {report['before']['tree_sha256'][:16]}...",
        f"  After:   {report['after']['path']}",
        f"           {report['after']['files']} files, {report['after']['bytes']} bytes",
        f"           tree: {report['after']['tree_sha256'][:16]}...",
        "",
        f"  Tree changed:    {'YES' if report['tree_changed'] else 'NO'}",
        f"  Size delta:      {report['size_delta_bytes']:+d} bytes",
        f"  Files added:     {report['added_count']}",
        f"  Files removed:   {report['removed_count']}",
        f"  Files modified:  {report['modified_count']}",
    ]

    if report["added_files"]:
        lines.append("")
        lines.append("Added files:")
        for f in report["added_files"][:15]:
            lines.append(f"  + {f}")
        if len(report["added_files"]) > 15:
            lines.append(f"  ... and {len(report['added_files']) - 15} more")

    if report["removed_files"]:
        lines.append("")
        lines.append("Removed files:")
        for f in report["removed_files"][:15]:
            lines.append(f"  - {f}")
        if len(report["removed_files"]) > 15:
            lines.append(f"  ... and {len(report['removed_files']) - 15} more")

    if report["top_modified"]:
        lines.append("")
        lines.append("Top modified files (by size delta):")
        for m in report["top_modified"]:
            lines.append(
                f"  ~ {m['path']}  "
                f"{m['before_bytes']} -> {m['after_bytes']} bytes "
                f"({m['delta_bytes']:+d})"
            )

    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("before", type=Path, help="Before memory directory")
    p.add_argument("after", type=Path, help="After memory directory")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="Output format (default: text)")
    args = p.parse_args(argv)

    if not args.before.exists():
        print(f"Error: before directory not found: {args.before}", file=sys.stderr)
        return 1
    if not args.after.exists():
        print(f"Error: after directory not found: {args.after}", file=sys.stderr)
        return 1

    report = analyze(args.before, args.after)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
