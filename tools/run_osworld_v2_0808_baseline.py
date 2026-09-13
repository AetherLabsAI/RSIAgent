#!/usr/bin/env python3
"""Run one owner shard of the frozen OSWorld 2.0 0808 baseline."""
from __future__ import annotations

from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import run_osworld_v2_baseline_shard as baseline  # noqa: E402


baseline.DEFAULT_LOCK = (
    REPO / "config/osworld_v2_0808_glm53_k3_agentic_baseline.lock.json")


if __name__ == "__main__":
    try:
        raise SystemExit(baseline.main())
    except baseline.PreflightError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
