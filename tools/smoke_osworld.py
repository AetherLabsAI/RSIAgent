#!/usr/bin/env python3
"""Check a disposable OSWorld VM without model calls, tasks, or official grading."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for smoke evidence"
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    from benchmarks.osworld.runtime import _install_paths

    _install_paths()
    import hashlib

    from desktop_env.desktop_env import DesktopEnv

    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    from core.verifier_runtime import AgenticVerifierExecutor
    from env.vm import VM
    from explore.commit import push_memory
    from explore.memory_hash import tree_sha256
    from explore.practice_loop import _read_memory_tree

    os.environ["RSIAGENT_VM_OWNER"] = "public_smoke_" + uuid.uuid4().hex
    os.environ["RSIAGENT_BOOT_DIAGNOSTICS_DIR"] = str(output / "boot")
    prepare_checkpointable_docker_provider("rollback_mirror")
    result = {
        "status": "running",
        "checks": [],
        "model_calls": 0,
        "official_evaluator_calls": 0,
    }
    environment = executor = None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        result["checks"].append(name)
        print("PASS", name, flush=True)

    try:
        environment = DesktopEnv(
            provider_name="docker",
            action_space="pyautogui",
            os_type="Ubuntu",
            headless=True,
            require_a11y_tree=False,
            cache_dir=str(output / "cache"),
            volume_size=60,
        )
        vm = VM(environment)
        check("command roundtrip", "SMOKE_OK" in vm.run_command("printf SMOKE_OK"))
        trace = vm.run_script("python", "print('PROGRAM_OK')")
        check(
            "program roundtrip", trace.exit_code == 0 and "PROGRAM_OK" in trace.stdout
        )
        trace = vm.run_script("bash", "echo PARTIAL; sleep 99", timeout=45)
        result["timeout_trace"] = {
            "exit_code": trace.exit_code,
            "timed_out": trace.timed_out,
            "output": trace.stdout,
        }
        check("timeout preserves output", trace.timed_out and "PARTIAL" in trace.stdout)
        check("controller survives timeout", "READY" in vm.run_command("printf READY"))
        memory = output / "memory"
        memory.mkdir()
        (memory / "lesson.txt").write_text("PRIVATE_ACTOR_MEMORY")
        check("memory upload", push_memory(vm, str(memory)))
        candidate = "/home/user/rsiagent_smoke_candidate.txt"
        vm.run_command(f"printf ORIGINAL > {candidate}")
        executor = AgenticVerifierExecutor(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory",),
        )
        trace = executor(
            "python",
            f"""import pathlib, os
p = pathlib.Path({candidate!r})
assert p.read_text() == 'ORIGINAL'
p.write_text('VERIFIER_EFFECT')
try:
    pathlib.Path('/home/user/.memory/lesson.txt').read_text()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise AssertionError('private memory leaked')
s = pathlib.Path(os.environ['VERIFIER_SCRATCH']) / 'evidence.txt'
s.write_text('SCRATCH_PERSISTS')
s.chmod(0)
print('ISOLATION_OK')
""",
            timeout=90,
        )
        check(
            "verifier isolation",
            trace.exit_code == 0 and "ISOLATION_OK" in trace.stdout,
        )
        scratch = executor.export_scratch()
        check("unreadable scratch exported", bool(scratch))
        executor.close()
        executor = None
        check(
            "candidate rollback",
            vm.run_command(f"cat {candidate}").strip() == "ORIGINAL",
        )
        check(
            "actor memory restored",
            vm.run_command("cat /home/user/.memory/lesson.txt").strip()
            == "PRIVATE_ACTOR_MEMORY",
        )
        check(
            "host memory immutable",
            (memory / "lesson.txt").read_text() == "PRIVATE_ACTOR_MEMORY",
        )
        result["memory_sha256"] = tree_sha256(
            {
                k: hashlib.sha256(v).hexdigest()
                for k, v in _read_memory_tree(str(memory)).items()
            }
        )
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        raise
    finally:
        try:
            if executor is not None:
                executor.close()
        finally:
            if environment is not None:
                environment.close()
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
