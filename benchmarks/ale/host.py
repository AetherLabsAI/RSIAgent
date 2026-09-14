"""ALE provisioning/grading lives only in this outer host process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .capacity import Capacity
from .protocol import (
    RSIAGENT,
    instruction_shingles,
    protocol,
    select_tasks,
    sha,
    source_fingerprint,
    verify_memory,
    write_json,
)

IMAGE_REVISION = "31374caa105f15c9cf3c20fe6abcf9e40ec1a636"
BASE_RUNNER = "agentslastexam/ale-qemu@sha256:cc3ead471566c27f7c394c8748ff2e69124f310dbe35fce5e4a5177bc947232f"
MODEL_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def provider_config(cache, runner_image):
    return {
        "snapshots": {
            tag: {
                "image": name,
                "disk_source": f"hf://agents-last-exam/ale-images-qcow2/{name}.qcow2",
                "hf_revision": IMAGE_REVISION,
                "root": str(cache),
                "runner_image": runner_image,
                "runner_pull_policy": "never",
            }
            for tag, name in [
                ("cpu-free-ubuntu", "ale-ubuntu22"),
                ("cpu-free", "ale-win10"),
            ]
        }
    }


def install_ale_path(root):
    root = Path(root).expanduser().resolve()
    # The model subprocess has its own RSIAgent environment. Keep this grader
    # process free of RSIAgent's top-level core/config/tools namespaces.
    core = sys.modules.get("core")
    if (
        core
        and getattr(core, "__file__", None)
        and Path(core.__file__).resolve().is_relative_to(RSIAGENT)
    ):
        raise RuntimeError(
            "ALE host already imported RSIAgent core; start a separate run_ale.py process"
        )
    sys.path[:] = [p for p in sys.path if Path(p or os.getcwd()).resolve() != RSIAGENT]
    sys.path.insert(0, str(root))
    return root


def plan(args):
    ale = install_ale_path(args.ale_root)
    lock = protocol(ale)
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.providers.qemu import QemuProvider
    from ale_run.tasks.loader import TaskLoader

    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    requested = select_tasks(lock, args.tasks)
    entries = []
    instructions = []
    for task in (ale / "selected_tasks/full/near-term.txt").read_text().splitlines():
        # Task cards contain public instructions and do not import graders.
        card = json.loads((ale / "tasks" / task / "task_card.json").read_text())
        instructions.append(card["taskPrompt"])
        if task not in requested:
            continue
        snapshot = card["vm"]["snapshot"]
        if snapshot == "gpu-free":
            entries.append(
                {
                    "task": task,
                    "variant": 0,
                    "os": "windows",
                    "snapshot": snapshot,
                    "status": "pending_gpu_runner",
                    "vcpus": 8,
                    "memory_gb": 32,
                }
            )
            continue
        meta = TaskLoader(str(ale / "tasks" / task)).load(variant_index=0)
        spec = SandboxSpec(
            snapshot=snapshot,
            os=meta["os_type"],
            machine_type=meta.get("machine_type"),
            vcpus=meta.get("vcpus"),
            memory_gb=meta.get("memory_gb"),
        )
        cpus, memory = provider._resolve_shape(
            provider.config.snapshots[snapshot], spec
        )
        entries.append(
            {
                "task": task,
                "variant": 0,
                "os": meta["os_type"],
                "snapshot": snapshot,
                "status": "ready",
                "vcpus": cpus,
                "memory_gb": memory,
            }
        )
        instructions.append(meta["description"])
    corpus = sorted(set().union(*(instruction_shingles(text) for text in instructions)))
    return {
        "schema_version": 2,
        "benchmark": "ale",
        "cohort": "full/near-term",
        "cohort_count": 67,
        "arm": args.arm,
        "tasks": entries,
        "protocol_sha256": sha(RSIAGENT / "config/ale/protocol.lock.json"),
        "source_sha256": source_fingerprint(),
        "ale_commit": lock["ale_commit"],
        "actor_watchdog_active_seconds": 36000,
        "phase1_project_budget": 8,
        "phase1_parallelism": 4,
        "phase2_stop_policy": "curriculum_review",
        "study_design": lock["study_design"],
        "image_revision": IMAGE_REVISION,
        "judge_base_url": args.judge_base_url,
        "provider_config": provider_config(args.cache, args.runner_image),
        "max_task_lineages": args.concurrency,
        "host_vm_limit": args.vm_limit,
        "corpus": corpus,
    }


def freeze_source(destination):
    destination = Path(destination)
    destination.mkdir()
    for name in ("benchmarks", "config", "core", "env", "explore", "llm", "tools"):
        shutil.copytree(
            RSIAGENT / name,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
        )
    for path in RSIAGENT.glob("*.py"):
        shutil.copy2(path, destination / path.name)


def public_task_audit(task, card, instruction, visible_roots):
    """Bind public card wording to its issued task, for host-side auditing only."""
    # The pinned cards use both identity spellings. A contradictory card is
    # never a source of additional authorized wording.
    identities = [card[key] for key in ("taskId", "task_id") if key in card]
    if (not identities or any(value != task for value in identities)
            or not isinstance(card.get("taskPrompt"), str) or not card["taskPrompt"]):
        raise ValueError("Public task card does not match the selected ALE task")
    return {
        "task_id": task,
        "task_prompt": card["taskPrompt"],
        "instruction": instruction,
        "visible_roots": list(visible_roots),
    }


async def run(args, manifest):
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.env import ALEEnv
    from ale_run.environments.output_pull import pull_to_host
    from ale_run.environments.providers.qemu import QemuProvider
    from ale_run.environments.task_data import baked_in_sandbox
    from ale_run.tasks.driver import TaskDriver
    from ale_run.tasks.loader import TaskLoader
    from dotenv import dotenv_values

    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    secrets = {
        **dotenv_values(args.env_file),
        **{k: os.environ[k] for k in MODEL_KEYS if k in os.environ},
    }
    if not secrets.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing from --env-file or the environment"
        )
    # The existing upstream OpenAI SDK graders keep their exact model aliases.
    # Endpoint changes are recorded; these host judge variables never enter VMs.
    if args.judge_base_url.rstrip("/") == "https://openrouter.ai/api/v1":
        os.environ["OPENAI_API_KEY"] = secrets["OPENROUTER_API_KEY"]
    elif secrets.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = secrets["OPENAI_API_KEY"]
    else:
        raise RuntimeError(
            "A custom judge endpoint requires OPENAI_API_KEY in --env-file"
        )
    os.environ["OPENAI_BASE_URL"] = args.judge_base_url
    benchmark_env = dotenv_values(Path(args.ale_root) / "secret/.env.example")
    os.environ["ALE_REFERENCE_ARCHIVE_PASSWORD"] = benchmark_env[
        "ALE_REFERENCE_ARCHIVE_PASSWORD"
    ]
    write_json(
        root / "manifest.json", {k: v for k, v in manifest.items() if k != "corpus"}
    )
    write_json(root / "corpus.json", manifest["corpus"])
    freeze_source(root / "release")
    if (
        source_fingerprint() != manifest["source_sha256"]
        or source_fingerprint(root / "release") != manifest["source_sha256"]
    ):
        raise RuntimeError("Source changed while freezing the run")
    provider = QemuProvider(manifest["provider_config"])
    tasks = {}
    children = {}
    stop = asyncio.Event()
    boot_semaphore = asyncio.Semaphore(4)
    lineage_semaphore = asyncio.Semaphore(args.concurrency)

    def publish(task, **values):
        tasks.setdefault(task, {}).update(values)
        write_json(
            root / "status.json",
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "tasks": tasks,
                "cohort_count": 67,
            },
        )

    def stop_all():
        stop.set()
        for child in children.values():
            if child.returncode is None:
                child.send_signal(signal.SIGTERM)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_all)

    async def phase(entry, name, input_memory=None):
        task = entry["task"]
        directory = root / task / name
        directory.mkdir(parents=True, exist_ok=False)
        practice_count = 4 if name == "phase1" else 1 if name == "phase2" else 0
        count = practice_count + (name != "phase1")
        reserve = count * (
            (2 if name in ("phase1", "phase2") else 1) * entry["memory_gb"] + 32
        )
        capacity = Capacity(args.pool_root, root, limit=args.vm_limit)
        publish(task, phase="waiting_capacity", active_phase=name)
        await capacity.acquire(
            count,
            reserve,
            stop,
            vcpus=count * entry["vcpus"],
            memory_gb=count * entry["memory_gb"],
        )
        environments = []
        driver = target_env = reset_service = None
        complete = False
        try:
            if stop.is_set():
                raise InterruptedError("Run stopped")
            meta = TaskLoader(str(Path(args.ale_root) / "tasks" / task)).load()
            data = meta["task_data"]

            async def boot(practice, index):
                spec = SandboxSpec(
                    snapshot=entry["snapshot"],
                    os=entry["os"],
                    vcpus=entry["vcpus"],
                    memory_gb=entry["memory_gb"],
                    task_id=task,
                    harness="rsiagent",
                    model_tag="rsi-practice" if practice else name,
                )
                env = ALEEnv(provider=provider, spec=spec)
                environments.append(env)
                async with boot_semaphore:
                    await env.reset_async()
                capacity.attach(env.sandbox.id)
                sandbox = asdict(env.sandbox)
                sandbox.setdefault("metadata", {})["rsiagent_practice"] = practice
                write_json(directory / f"sandbox-{index}.json", sandbox)
                return env, sandbox

            publish(task, phase="provisioning", active_phase=name)
            provisioned = await asyncio.gather(
                *(boot(i < practice_count, i) for i in range(count)),
                return_exceptions=True,
            )
            failures = [item for item in provisioned if isinstance(item, BaseException)]
            if failures:
                raise failures[0]
            if name != "phase1":
                target_env = provisioned[-1][0]
                await baked_in_sandbox.stage_input(
                    target_env.sandbox, data, source="baked_in_sandbox"
                )
                driver = TaskDriver(
                    str(Path(args.ale_root) / "tasks" / task),
                    target_env.session,
                    variant=0,
                    os_type=entry["os"],
                    session_rebuilder=target_env.reset_session,
                )
                await driver.setup()
            if name in ("phase1", "phase2"):
                from .lifecycle import ResetService

                owned_sandboxes = {env.sandbox.id: env for env in environments}

                async def reset_sandbox(sandbox_id):
                    nonlocal driver
                    if stop.is_set():
                        raise InterruptedError("Run stopped before a fresh reset")
                    env = owned_sandboxes.pop(sandbox_id, None)
                    if env is None:
                        raise RuntimeError(
                            "Reset does not name a sandbox owned by this phase"
                        )
                    is_target = env is target_env
                    if is_target:
                        await driver.close()
                    previous_sandbox = env.sandbox
                    await env.close_async(mode="keep")
                    await provider.release(previous_sandbox, mode="delete")
                    async with boot_semaphore:
                        await env.reset_async()
                    capacity.attach(env.sandbox.id)
                    owned_sandboxes[env.sandbox.id] = env
                    if is_target:
                        await baked_in_sandbox.stage_input(
                            env.sandbox, data, source="baked_in_sandbox"
                        )
                        driver = TaskDriver(
                            str(Path(args.ale_root) / "tasks" / task),
                            env.session,
                            variant=0,
                            os_type=entry["os"],
                            session_rebuilder=env.reset_session,
                        )
                        await driver.setup()
                    sandbox = asdict(env.sandbox)
                    sandbox.setdefault("metadata", {})[
                        "rsiagent_practice"
                    ] = not is_target
                    write_json(
                        directory / ("reset-" + env.sandbox.id + ".json"), sandbox
                    )
                    return sandbox

                reset_service = ResetService(reset_sandbox)
            worker_spec = {
                "phase": name,
                "instruction": meta["description"],
                "work_dir": str(directory / "rsiagent"),
                "corpus": str(root / "corpus.json"),
                "practice_sandboxes": [sb for env, sb in provisioned[:practice_count]],
                "visible_roots": [
                    p for p in (data.input_dir, data.remote_output_dir) if p
                ],
            }
            if name in ("phase1", "phase2"):
                card = json.loads(
                    (Path(args.ale_root) / "tasks" / task / "task_card.json").read_text()
                )
                # This metadata stays in Audit; the agent's instruction is unchanged.
                worker_spec["audit_public_task"] = public_task_audit(
                    task, card, meta["description"], worker_spec["visible_roots"]
                )
            if reset_service is not None:
                worker_spec["reset_endpoint"] = reset_service.path
            if target_env is not None:
                worker_spec["sandbox"] = provisioned[-1][1]
            if input_memory is not None:
                verify_memory(input_memory)
                worker_spec["input_memory"] = input_memory
            write_json(directory / "worker-spec.json", worker_spec)
            child_env = {
                k: os.environ[k]
                for k in (
                    "HOME",
                    "USER",
                    "LANG",
                    "PATH",
                    "TMPDIR",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                )
                if k in os.environ
            }
            child_env.update(
                {k: v for k, v in secrets.items() if k in MODEL_KEYS and v}
            )
            child_env.update(
                PYTHONPATH=str(root / "release"),
                RSIAGENT_ROOT=str(root / "release"),
                RSIAGENT_ENV_FILE=str(root / "release" / ".env"),
                PYTHONUNBUFFERED="1",
            )
            with (directory / "worker.log").open("wb") as output:
                child = await asyncio.create_subprocess_exec(
                    str(args.worker_python),
                    "-m",
                    "benchmarks.ale.worker",
                    str(directory / "worker-spec.json"),
                    cwd=str(root / "release"),
                    env=child_env,
                    stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                )
                children[task] = child
                publish(task, phase="agent_running", active_phase=name, pid=child.pid)
                code = await child.wait()
                children.pop(task, None)
            result = json.loads((directory / "rsiagent/worker-result.json").read_text())
            if code or stop.is_set():
                raise RuntimeError(
                    "Worker did not complete phase: "
                    + str(result.get("error") or result.get("status"))
                )
            if input_memory is not None:
                verify_memory(input_memory)
            if name in ("baseline", "phase3"):
                if not result.get("safe_to_grade"):
                    raise RuntimeError(
                        "Candidate rollback not confirmed; grading blocked"
                    )
                publish(task, phase="collecting_output", active_phase=name)
                pulled = await pull_to_host(
                    target_env.sandbox, data, dest_dir=directory / "output"
                )
                write_json(directory / "output-pull.json", pulled)
                if pulled.get("errors"):
                    raise RuntimeError("Output collection failed")
                publish(task, phase="grading", active_phase=name)
                await baked_in_sandbox.stage_reference(
                    target_env.sandbox, data, source="baked_in_sandbox"
                )
                evaluation = await asyncio.wait_for(driver.evaluate(), timeout=7200)
                write_json(directory / "eval-result.json", evaluation)
                from .report import valid_score

                if evaluation.get("error") or evaluation.get("score") is None:
                    raise RuntimeError(
                        "Official evaluator did not return a valid score"
                    )
                result.update(
                    score=valid_score(evaluation["score"]),
                    official_evaluation=evaluation,
                )
            elif (
                not result.get("transition_ready")
                or result.get("official_evaluator_calls") != 0
            ):
                raise RuntimeError("Learning phase is not transition-ready")
            write_json(directory / "result.json", result)
            complete = True
            return result
        finally:
            if reset_service is not None:
                await reset_service.close()
            if driver is not None:
                try:
                    await driver.close()
                except Exception:
                    logging.exception("Could not close ALE task driver")
            # Failed guests remain available for diagnosis; actual live containers
            # continue to count against capacity after their reservation releases.
            for env in environments:
                try:
                    await env.close_async(mode="delete" if complete else "keep")
                except Exception:
                    logging.exception("Could not close ALE environment")
            capacity.release()

    async def unit(entry):
        task = entry["task"]
        publish(task, phase="queued", os=entry["os"])
        if entry["status"] != "ready":
            publish(task, phase=entry["status"])
            return
        async with lineage_semaphore:
            try:
                if args.arm in ("baseline", "both"):
                    result = await phase(entry, "baseline")
                    publish(task, baseline_score=result["score"], baseline="completed")
                if args.arm in ("rsi", "both"):
                    p1 = await phase(entry, "phase1")
                    p2 = await phase(entry, "phase2", p1["memory"])
                    p3 = await phase(entry, "phase3", p2["memory"])
                    publish(task, rsi_score=p3["score"], rsi="completed")
                publish(task, phase="completed")
            except BaseException as exc:
                logging.exception("Task %s stopped; no automatic retry", task)
                publish(
                    task,
                    phase="interrupted" if stop.is_set() else "infrastructure_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                write_json(
                    root / task / "error.json",
                    {"error": str(exc), "traceback": traceback.format_exc()},
                )

    await asyncio.gather(*(unit(entry) for entry in manifest["tasks"]))
    write_json(
        root / "batch-result.json",
        {
            "tasks": tasks,
            "cohort_count": 67,
            "complete": all(t["phase"] == "completed" for t in tasks.values()),
        },
    )
