#!/usr/bin/env python3
"""Run the full pinned OSWorld cohort, with independent outputs for every task."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld.task_surface import load_public_task_surface
from config.runtime_paths import resolve_osworld_root
from explore.commit import normalize_instruction_for_corpus

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "config/osworld/baseline.lock.json"
TEMPLATE = ROOT / "config/osworld/rsi.example.json"


def build_plan(name, arm, *, tasks=None, osworld_root=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError(
            "Batch name must contain 1–64 letters, numbers, underscores or hyphens"
        )
    lock = json.loads(LOCK.read_text())
    tasks = (
        tasks
        if tasks is not None
        else [f"task_{i:03d}" for i in range(1, lock["task_count"] + 1)]
    )
    osworld_root = osworld_root or resolve_osworld_root()
    folder = ROOT / "results/batches" / name
    jobs = []
    for task in tasks:
        job = {"task": task, "commands": [], "result_paths": []}
        if arm in ("baseline", "both"):
            result = ROOT / "results" / task / f"seed{lock['seed_label']}_{name}"
            job["result_paths"].append(str(result))
            job["commands"].append(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "benchmarks.osworld.task",
                    task,
                    "--config",
                    str(ROOT / "config/osworld/baseline.yaml"),
                    "--seed",
                    str(lock["seed_label"]),
                    "--tag",
                    name,
                ]
            )
        if arm in ("rsi", "both"):
            spec = json.loads(TEMPLATE.read_text())
            spec["run_name"] = f"{name}_{task}"
            query = ROOT / "config/target_queries" / f"{task}.md"
            instruction = (
                query.read_text()
                if query.is_file()
                else normalize_instruction_for_corpus(
                    load_public_task_surface(
                        osworld_root,
                        task,
                        website_host_suffix=lock["website"]["host_suffix"],
                    ).instruction
                )
            )
            spec["phase1"]["distribution_file"] = str(folder / task / "direction.md")
            spec["phase2"]["development_tasks"] = [task]
            spec["phase3"]["held_out_tasks"] = [task]
            job.update(protocol=spec, direction=instruction)
            job["result_paths"].append(
                str(ROOT / "results/recursive_improvement" / spec["run_name"])
            )
            for phase in ("phase1", "phase2", "phase3"):
                job["commands"].append(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "benchmarks.osworld.pipeline",
                        "--protocol",
                        str(folder / task / "protocol.json"),
                        "--phase",
                        phase,
                        "--execute",
                        "RUN-RECURSIVE-IMPROVEMENT-" + phase.upper(),
                    ]
                )
        jobs.append(job)
    return {
        "name": name,
        "arm": arm,
        "task_release": lock["benchmark_release"],
        "output": str(folder),
        "jobs": jobs,
    }


def execute(plan, concurrency, osworld_root):
    folder = Path(plan["output"])
    for path in [
        folder,
        *(Path(p) for job in plan["jobs"] for p in job["result_paths"]),
    ]:
        if path.exists():
            raise FileExistsError(
                f"Use a new batch name; output already exists: {path}"
            )
    # Validate inputs before any model or VM is launched.
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/prepare_osworld_v2_release.py"),
            "--osworld-root",
            str(osworld_root),
            "--verify-only",
        ],
        cwd=ROOT,
        check=True,
    )
    lock = json.loads(LOCK.read_text())
    environment = dict(
        os.environ,
        RSIAGENT_ROOT=str(ROOT),
        OSWORLD_ROOT=str(osworld_root),
        OSWORLD_FILE_BASE_URL=str(osworld_root / lock["task_assets"]["local_dir"]),
        WEBSITE_HOST_SUFFIX=lock["website"]["host_suffix"],
    )
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    def run_job(job):
        work = folder / job["task"]
        work.mkdir()
        if "protocol" in job:
            (work / "protocol.json").write_text(
                json.dumps(job["protocol"], indent=2) + "\n"
            )
            (work / "direction.md").write_text(job["direction"])
        completed = []
        for index, command in enumerate(job["commands"]):
            env = dict(
                environment, RSIAGENT_OSWORLD_CACHE_DIR=str(work / "cache" / str(index))
            )
            log = work / f"{index + 1}.log"
            try:
                with log.open("w") as stream:
                    code = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        check=False,
                    ).returncode
            except OSError as exc:
                log.write_text(str(exc) + "\n")
                code = 1
            completed.append({"command": command, "returncode": code, "log": str(log)})
            if code:
                break
        result = {
            "task": job["task"],
            "status": "failed" if completed[-1]["returncode"] else "completed",
            "steps": completed,
            "result_paths": job["result_paths"],
        }
        (work / "status.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"{job['task']}: {result['status']}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(run_job, plan["jobs"]))
    (folder / "status.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(result["status"] != "completed" for result in results))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm", choices=("baseline", "rsi", "both"), default="baseline"
    )
    parser.add_argument(
        "--name", default=datetime.now(timezone.utc).strftime("batch_%Y%m%d_%H%M%S")
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent task lineages; each RSI lineage may own multiple VMs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan without launching or writing outputs",
    )
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    osworld_root = resolve_osworld_root()
    plan = build_plan(args.name, args.arm, osworld_root=osworld_root)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    return execute(plan, args.concurrency, osworld_root)


if __name__ == "__main__":
    raise SystemExit(main())
