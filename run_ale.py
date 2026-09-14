#!/usr/bin/env python3
"""Plan, prepare, validate, and run the pinned ALE Near-Term benchmark."""

import argparse
import asyncio
import json
import logging
from pathlib import Path


def parser():
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "run", "prepare", "smoke", "report"))
    p.add_argument("--arm", choices=("baseline", "rsi", "both"), default="baseline")
    p.add_argument("--ale-root", type=Path, default=root.parent / "agents-last-exam")
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache/rsiagent/ale")
    p.add_argument("--runner-image", default="rsiagent-ale-checkpoint:v1")
    p.add_argument(
        "--worker-python", type=Path, default=root / ".venv-ale-worker/bin/python"
    )
    p.add_argument("--env-file", type=Path, default=root / ".env")
    p.add_argument("--output", type=Path)
    p.add_argument(
        "--tasks", type=Path, help="Task IDs, one per line; defaults to the full cohort"
    )
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--vm-limit", type=int, default=16)
    p.add_argument(
        "--pool-root", type=Path, default=Path("/var/tmp/rsiagent-ale-capacity")
    )
    p.add_argument("--judge-base-url", default="https://openrouter.ai/api/v1")
    p.add_argument("--os", choices=("linux", "windows"), default="linux")
    p.add_argument("--runs", nargs="*", type=Path)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    if args.concurrency < 1 or not 1 <= args.vm_limit <= 16:
        p.error("Use positive concurrency and a host VM limit between 1 and 16")
    if args.action == "run" and args.output is None:
        p.error("run requires a new --output directory")
    if args.action == "prepare":
        from benchmarks.ale.prepare import prepare

        asyncio.run(prepare(args))
    elif args.action == "smoke":
        from benchmarks.ale.smoke import provision_smoke

        asyncio.run(provision_smoke(args))
    elif args.action == "report":
        from benchmarks.ale.report import report

        report(args)
    else:
        from benchmarks.ale.host import plan, run

        manifest = plan(args)
        if args.action == "plan":
            print(
                json.dumps(
                    {k: v for k, v in manifest.items() if k != "corpus"}, indent=2
                )
            )
        else:
            from benchmarks.ale.prepare import validate_readiness

            validate_readiness(args, manifest)
            asyncio.run(run(args, manifest))


if __name__ == "__main__":
    main()
