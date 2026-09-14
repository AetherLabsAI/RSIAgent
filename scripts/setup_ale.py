#!/usr/bin/env python3
"""Install isolated ALE grader and RSIAgent worker environments."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def install(root, ale, worker_env, uv, *, images=False):
    root, ale, worker_env = (
        Path(p).expanduser().resolve() for p in (root, ale, worker_env)
    )
    if worker_env in {root / ".venv", ale / ".venv"}:
        raise ValueError(
            "Use a dedicated worker environment, separate from the development and grader environments"
        )
    lock = json.loads((root / "config/ale/protocol.lock.json").read_text())
    if not ale.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "https://github.com/rdi-berkeley/agents-last-exam.git",
                str(ale),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(ale), "checkout", "--detach", lock["ale_commit"]],
            check=True,
        )
    revision = subprocess.check_output(
        ["git", "-C", str(ale), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != lock["ale_commit"]:
        raise RuntimeError(
            "ALE checkout differs from the pinned revision; use a separate --ale-root"
        )
    dependency_lock = root / "benchmarks/ale/ale.uv.lock"
    installed_lock = ale / "uv.lock"
    if (
        installed_lock.exists()
        and installed_lock.read_bytes() != dependency_lock.read_bytes()
    ):
        raise RuntimeError("ALE dependency lock differs; use a separate --ale-root")
    if not installed_lock.exists():
        shutil.copy2(dependency_lock, installed_lock)
    subprocess.run(
        [uv, "sync", "--frozen", "--project", str(ale), "--python", "3.12"], check=True
    )
    grader_python = ale / ".venv/bin/python"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(grader_python),
            "-r",
            str(root / "benchmarks/ale/requirements-grader.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(grader_python),
            "torch==2.6.0",
            "--index-url",
            "https://download.pytorch.org/whl/cpu",
        ],
        check=True,
    )
    worker_python = worker_env / "bin/python"
    if not worker_python.exists():
        subprocess.run([uv, "venv", str(worker_env), "--python", "3.12"], check=True)
    # Use the same maintained dependency list as the public agent runtime.
    # Install only into the dedicated worker environment; never sync root/.venv.
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(worker_python),
            "-r",
            str(root / "requirements.txt"),
        ],
        check=True,
    )
    for python in (grader_python, worker_python):
        subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)
    if images:
        subprocess.run(
            [
                str(grader_python),
                str(root / "run_ale.py"),
                "prepare",
                "--ale-root",
                str(ale),
                "--worker-python",
                str(worker_python),
            ],
            check=True,
        )
    print("Grader Python:", grader_python)
    print("Worker Python:", worker_python)
    print("Configure model access using the repository .env.example.")


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ale-root", type=Path, default=root.parent / "agents-last-exam"
    )
    parser.add_argument("--worker-env", type=Path, default=root / ".venv-ale-worker")
    parser.add_argument("--uv", default=shutil.which("uv"))
    parser.add_argument(
        "--images",
        action="store_true",
        help="Build the runner and download the pinned Linux VM image",
    )
    args = parser.parse_args()
    if not args.uv:
        parser.error("Install uv and put it on PATH, or provide --uv")
    install(root, args.ale_root, args.worker_env, args.uv, images=args.images)


if __name__ == "__main__":
    main()
