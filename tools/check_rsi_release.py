#!/usr/bin/env python3
"""Run the complete portable suite from this source checkout."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    environment = dict(os.environ)
    environment["FORGE_ROOT"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=REPO, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
