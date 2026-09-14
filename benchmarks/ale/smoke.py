"""Real guest tests of RSI transport boundaries using synthetic, ungraded data."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
from dataclasses import asdict
from pathlib import Path

from .protocol import (
    RSIAGENT,
    memory_record,
    source_fingerprint,
    verify_memory,
    write_json,
)


async def provision_smoke(args):
    from .capacity import Capacity
    from .host import freeze_source, install_ale_path, provider_config

    install_ale_path(args.ale_root)
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.providers.qemu import QemuProvider

    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    output = args.output or RSIAGENT / "results/ale/smoke" / args.os
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = source_fingerprint()
    freeze_source(output / "release")
    if (
        source_fingerprint() != fingerprint
        or source_fingerprint(output / "release") != fingerprint
    ):
        raise RuntimeError("Source changed while freezing the smoke")
    capacity = Capacity(args.pool_root, output, limit=args.vm_limit)
    await capacity.acquire(1, 80, asyncio.Event(), vcpus=4, memory_gb=16)
    sandbox = None
    service = None
    passed = False
    try:
        sandbox = await provider.acquire(
            SandboxSpec(
                snapshot="cpu-free" if args.os == "windows" else "cpu-free-ubuntu",
                os=args.os,
                vcpus=4,
                memory_gb=16,
                task_id="rsiagent-rsi-synthetic-smoke",
            )
        )
        capacity.attach(sandbox.id)
        spec = asdict(sandbox)
        spec.setdefault("metadata", {})["rsiagent_practice"] = True
        from .lifecycle import ResetService

        async def reset_sandbox(sandbox_id):
            nonlocal sandbox
            if sandbox_id != sandbox.id:
                raise RuntimeError("Smoke reset must name its owned synthetic guest")
            await provider.release(sandbox, mode="delete")
            sandbox = await provider.acquire(
                SandboxSpec(
                    snapshot="cpu-free" if args.os == "windows" else "cpu-free-ubuntu",
                    os=args.os,
                    vcpus=4,
                    memory_gb=16,
                    task_id="rsiagent-rsi-synthetic-smoke",
                )
            )
            capacity.attach(sandbox.id)
            result = asdict(sandbox)
            result.setdefault("metadata", {})["rsiagent_practice"] = True
            write_json(output / "current-sandbox.json", result)
            return result

        service = ResetService(reset_sandbox)
        spec["reset_endpoint"] = service.path
        write_json(output / "sandbox.json", spec)
        command = [
            str(args.worker_python),
            "-m",
            "benchmarks.ale.smoke",
            str(output / "sandbox.json"),
            str(output),
        ]
        with (output / "smoke.log").open("wb") as log:
            child = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(output / "release"),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            code = await child.wait()
        result = json.loads((output / "result.json").read_text())
        if code or result["status"] != "passed":
            raise RuntimeError(
                f"{args.os} RSI smoke failed; see {output / 'smoke.log'}"
            )
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
            text=True,
        ).strip()
        result.update(
            source_sha256=fingerprint,
            runner_image=image_id,
            scope="real VM transport/checkpoint/learning-memory mechanics; no model or grader calls",
        )
        write_json(
            Path(args.cache) / "rsiagent-readiness" / f"smoke-{args.os}.json", result
        )
        passed = True
        print(json.dumps(result, indent=2))
    finally:
        if service is not None:
            await service.close()
        if sandbox is not None and passed:
            await provider.release(sandbox, mode="delete")
        capacity.release()


def exercise(sandbox, root):
    from core.verifier_runtime import AgenticVerifierExecutor
    from explore.commit import _shingles
    from explore.practice_loop import _atomic_install_memory, _pull_terminal_memory
    from explore.provisioning import (
        capture_project_materials,
        replay_project_materials,
        verify_project_materials,
    )

    from .practice import CleanPool, hooks_for
    from .windows import WindowsVerifierExecutor

    result = {"status": "running", "checks": [], "os": sandbox["os"]}
    pool = executor = None
    root.mkdir(parents=True, exist_ok=True)

    def check(name, condition, detail=""):
        if not condition:
            raise AssertionError(name + ": " + str(detail))
        result["checks"].append(name)
        write_json(root / "result.json", result)
        print("PASS", name, flush=True)

    try:
        pool = CleanPool(
            [sandbox], practice=True, reset_endpoint=sandbox["reset_endpoint"]
        )
        lease, vm = pool.acquire()
        corpus = root / "corpus.json"
        public = "Create a synthetic CSV total from a small table of integer values."
        write_json(corpus, sorted(_shingles(public)))
        hooks = hooks_for(pool, corpus)
        bank = root / "memory"
        original = {
            "procedure.md": b"Check row count and independently recompute totals.\n",
            "nested/bytes.bin": bytes(range(256)) * 17,
        }
        _atomic_install_memory(str(bank), original)
        check("upload initial memory", hooks.push_memory(vm, str(bank)))
        check(
            "lossless nested memory roundtrip",
            _pull_terminal_memory(hooks, vm) == original,
        )
        path = vm.home + "/evolution_project"
        vm.trusted_python(
            f'import pathlib\np=pathlib.Path({path!r});p.mkdir()\n(p/"input.csv").write_text("value\\n3\\n7\\n",encoding="utf-8")\n(p/"nested").mkdir()\n(p/"nested"/"unicode ü.txt").write_text("αβγ",encoding="utf-8")\n'
        )
        captured = root / "candidate"
        check(
            "capture synthetic project",
            capture_project_materials(
                vm, "smoke", str(captured), guest_dirs=["evolution_project"]
            )["ok"],
        )
        vm.trusted_python(
            f'import pathlib\n(pathlib.Path({path!r})/"input.csv").write_text("changed")\n'
        )
        check(
            "detect candidate mutation",
            not verify_project_materials(vm, str(captured))["ok"],
        )
        check(
            "replay exact candidate", replay_project_materials(vm, str(captured))["ok"]
        )
        cls = WindowsVerifierExecutor if vm.is_windows else AgenticVerifierExecutor
        executor = cls(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory", "/home/user/work"),
        )
        trace = executor(
            "python",
            f'import pathlib\np=pathlib.Path({path!r})\nassert "3" in (p/"input.csv").read_text()\n(p/"input.csv").write_text("verifier effect")\n',
            timeout=120,
        )
        check(
            "verifier can inspect candidate",
            not trace.infra_fail and trace.exit_code == 0,
            trace.stdout,
        )
        trace = executor(
            "python",
            f'import pathlib\np=pathlib.Path({vm.home + "/.memory/procedure.md"!r})\ntry:\n p.read_bytes()\nexcept (FileNotFoundError,PermissionError):\n print("PRIVATE_HIDDEN")\nelse:\n raise AssertionError("memory leaked")\n',
            timeout=120,
        )
        check(
            "actor memory hidden from verifier",
            trace.exit_code == 0 and "PRIVATE_HIDDEN" in trace.stdout,
        )
        trace = executor(
            "python",
            'import os,pathlib\n(pathlib.Path(os.environ["VERIFIER_SCRATCH"])/"evidence.txt").write_text("persistent")\n',
            timeout=120,
        )
        check("verifier scratch write", trace.exit_code == 0)
        scratch = executor.export_scratch()
        executor.close()
        executor = None
        check(
            "candidate restored after verification",
            verify_project_materials(vm, str(captured))["ok"],
        )
        executor = cls(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory", "/home/user/work"),
            scratch_archive=scratch,
        )
        trace = executor(
            "python",
            'import os,pathlib\nprint((pathlib.Path(os.environ["VERIFIER_SCRATCH"])/"evidence.txt").read_text())\n',
            timeout=120,
        )
        check(
            "verifier scratch persists across inspection",
            trace.exit_code == 0 and "persistent" in trace.stdout,
        )
        executor.close()
        executor = None
        vm.trusted_python(
            f'import pathlib\n(pathlib.Path({vm.home + "/.memory"!r})/"learned.md").write_text("Validate all rows before exporting.")\n'
        )
        learned = _pull_terminal_memory(hooks, vm)
        check(
            "learning retains prior memory and adds procedure",
            all(learned[k] == v for k, v in original.items())
            and "learned.md" in learned,
        )
        _atomic_install_memory(str(root / "frozen"), learned)
        frozen = memory_record(root / "frozen")
        lease.close()
        lease, fresh = pool.acquire()
        check(
            "fresh lease removes previous project",
            fresh.trusted_python(
                f"import pathlib\nprint(pathlib.Path({path!r}).exists())\n"
            ).strip()
            == "False",
        )
        check(
            "fresh lease removes prior memory",
            fresh.trusted_python(
                f"import pathlib\nprint(pathlib.Path({fresh.home + '/.memory'!r}).exists())\n"
            ).strip()
            == "False",
        )
        check(
            "frozen memory attached to fresh execution",
            hooks.push_memory(fresh, frozen["path"]),
        )
        check(
            "fresh execution sees learned procedure",
            _pull_terminal_memory(hooks, fresh) == learned,
        )
        verify_memory(frozen)
        check("host frozen memory unchanged", True)
        lease.close()
        result["status"] = "passed"
    except BaseException as exc:
        import traceback

        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        try:
            if executor is not None:
                executor.close()
            if pool is not None:
                pool.close()
        except Exception as exc:
            result.update(status="failed", cleanup_error=str(exc))
        write_json(root / "result.json", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("sandbox", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = exercise(json.loads(args.sandbox.read_text()), args.output)
    raise SystemExit(0 if result["status"] == "passed" else 1)
