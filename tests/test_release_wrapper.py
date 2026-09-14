"""Exercise stage ordering and failure propagation without any Agent or VM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


STUB = '''import argparse, json, os
from pathlib import Path

def load_protocol(path):
    spec = json.loads(path.read_text())
    if spec["run_name"] != "wrapper_test":
        raise ValueError("invalid run name")
    return spec

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--phase")
    parser.add_argument("--execute")
    args = parser.parse_args()
    spec = load_protocol(args.protocol)
    assert args.execute == "RUN-RECURSIVE-IMPROVEMENT-" + args.phase.upper()
    record = {"phase": args.phase, "cache": os.environ["RSIAGENT_OSWORLD_CACHE_DIR"],
              "root": os.environ["RSIAGENT_ROOT"]}
    with Path("calls.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\\n")
    print(args.phase + " output", flush=True)
    if spec.get("fail_at") == args.phase:
        raise SystemExit(75)
'''


@pytest.mark.parametrize("fail_at,expected,exit_code", [
    (None, ["phase1", "phase2", "phase3"], 0),
    ("phase2", ["phase1", "phase2"], 75),
])
def test_wrapper_preserves_order_caches_logs_and_failures(tmp_path, fail_at, expected, exit_code):
    root = tmp_path / "checkout with spaces"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/run_rsi.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/run_rsi.sh", script)
    package = root / "benchmarks/osworld"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "pipeline.py").write_text(STUB)
    protocol = tmp_path / "study with spaces.json"
    protocol.write_text(json.dumps({"run_name": "wrapper_test", "fail_at": fail_at}))
    process = subprocess.run(
        ["bash", str(script), str(protocol)], cwd=tmp_path,
        env={**os.environ, "RSI_PYTHON": sys.executable},
        text=True, capture_output=True, timeout=30)
    assert process.returncode == exit_code, process.stderr
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert [item["phase"] for item in calls] == expected
    for item in calls:
        assert item["root"] == str(root)
        assert item["cache"] == str(root / "results/task_cache/wrapper_test" / item["phase"])
        assert item["phase"] + " output" in process.stdout
        log = root / "results/batch_logs/wrapper_test" / (item["phase"] + ".log")
        assert item["phase"] + " output" in log.read_text()
    if fail_at:
        assert not (root / "results/batch_logs/wrapper_test/phase3.log").exists()
