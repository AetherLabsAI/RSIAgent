#!/usr/bin/env python3
"""Run one OSWorld task with sealed post-Agent evaluation.

The Agent receives the public instruction and a VM handle. Official evaluation
runs only after the action/verification loop returns. Evaluator transport retries
retain the completed VM and never re-enter an Agent.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_path,
    resolve_root,
    resolve_osworld_root,
)
from config.benchmark_runtime import configure_associated_benchmark_lock

signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # kill -> finally -> de.close()
#                                                           (SIGKILLed runs still leak;
#                                                            prefer TERM when stopping)

RSIAGENT_ROOT = resolve_root()
OSWORLD_ROOT = resolve_osworld_root(repo_root=RSIAGENT_ROOT)
from config.settings import load as load_config          # noqa: E402
from core.loop import run_with_resume                    # noqa: E402
from core.actor import build_system                  # noqa: E402
from core.trace import ArtifactSink                  # noqa: E402
from core.grading import GradingBlocked, run_until_success  # noqa: E402
from core.verifier_runtime import AgenticVerifierNoProgressError  # noqa: E402
from env.vm import VM                               # noqa: E402
from llm.client import provider_counts, reset_provider_counts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s %(name)s] %(message)s")
log = logging.getLogger("rsiagent.run")


def smoke(vm) -> bool:
    """Infra check: command roundtrip, program roundtrip, timeout-recovery path."""
    ok = True
    out = vm.run_command("echo rsiagent_smoke_ok && whoami")
    print("run_command:", out)
    ok &= "rsiagent_smoke_ok" in out
    tr = vm.run_script("python", "print('py_roundtrip_ok')")
    print("run_script:", tr.stdout.strip()[:200], "| exit", tr.exit_code)
    ok &= tr.exit_code == 0 and "py_roundtrip_ok" in tr.stdout
    tr = vm.run_script("bash", "echo partial_before_sleep; sleep 99", timeout=45)
    print("timeout path:", tr.stdout.strip()[:200], "| exit", tr.exit_code,
          "| timed_out", tr.timed_out)
    ok &= tr.timed_out and "partial_before_sleep" in tr.stdout
    return bool(ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", help="e.g. task_012")
    ap.add_argument("--seed", type=int, default=0,
                    help="run label only (independent draw; the API has no seed)")
    ap.add_argument("--config", default="config/osworld/baseline.yaml", help="Actor configuration")
    ap.add_argument("--tag", default=None,
                    help="output-dir suffix -> results/<task>/seed<S>_<tag> (for A/B arms)")
    ap.add_argument("--max-iters", type=int, default=None)
    ap.add_argument(
        "--memory-dir", default="",
        help=("immutable host memory snapshot to copy into the Actor environment; "
              "the runner never syncs guest changes back"))
    ap.add_argument("--smoke", action="store_true", help="infra check only, no agent")
    ap.add_argument("--evaluator-correction", default="",
                    choices=["task102-missing-paragraph-style-v1"],
                    help="explicit versioned post-Agent evaluator correction")
    args = ap.parse_args()
    from benchmarks.osworld.runtime import _install_paths
    _install_paths()
    if args.evaluator_correction and args.task_id != "task_102":
        ap.error("this evaluator correction applies only to task_102")
    config_reference = args.config or os.environ.get("RSIAGENT_CONFIG")
    config_path = (resolve_path(config_reference, repo_root=RSIAGENT_ROOT)
                   if config_reference else None)
    benchmark_lock_record = configure_associated_benchmark_lock(
        config_path, repo_root=RSIAGENT_ROOT)
    if benchmark_lock_record:
        log.info(
            "bound direct run to %s evaluator %s through associated lock %s",
            benchmark_lock_record["benchmark_release"],
            benchmark_lock_record["evaluator_model"],
            benchmark_lock_record["path"])
    cfg = load_config(str(config_path) if config_path else None)
    normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
    memory_record = {
        "mode": "off",
        "source": "",
        "files": 0,
        "bytes": 0,
        "tree_sha256": "",
        "host_writeback": False,
    }
    if args.memory_dir:
        from explore.practice_loop import _manifest, _read_memory_tree  # noqa: E402
        from explore.target_learning import _memory_tree_sha256       # noqa: E402

        raw_memory_path = Path(args.memory_dir).expanduser()
        if raw_memory_path.is_symlink():
            raise RuntimeError("--memory-dir may not be a symlink")
        memory_path = raw_memory_path.resolve(strict=True)
        if not memory_path.is_dir():
            raise RuntimeError("--memory-dir must be one real directory")
        configured = str(getattr(cfg, "env_memory_dir", "") or "").strip()
        if configured and Path(configured).expanduser().resolve() != memory_path:
            raise RuntimeError(
                "--memory-dir conflicts with env_memory_dir in the selected config")
        frozen_memory = _read_memory_tree(str(memory_path))
        manifest = _manifest(frozen_memory)
        cfg.env_memory_dir = str(memory_path)
        memory_record = {
            "mode": "frozen_host_snapshot_copy_in_only",
            "source": str(memory_path),
            "files": len(frozen_memory),
            "bytes": sum(len(data) for data in frozen_memory.values()),
            "tree_sha256": _memory_tree_sha256(frozen_memory),
            "manifest_sha256": hashlib.sha256(json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "host_writeback": False,
        }
    from env.qemu_rollback import (                         # noqa: E402
        normalize_verifier_execution_mode,
    )
    from benchmarks.osworld.provider import (                             # noqa: E402
        prepare_checkpointable_docker_provider,
    )
    verifier_execution_mode = normalize_verifier_execution_mode(
        cfg.verifier_execution_mode)
    prepare_checkpointable_docker_provider(verifier_execution_mode)
    if getattr(cfg, "verifier_evolve_route", False):
        raise RuntimeError(
            "verifier_evolve_route belongs to the unified-loop entrypoint; "
            "benchmarks/osworld/task.py would cross the sealed grader boundary after EVOLVE")
    if args.max_iters:
        cfg.max_iters = args.max_iters

    from task_loader import load_task_config, resolve_task_json_path   # noqa: E402
    from desktop_env.desktop_env import DesktopEnv                     # noqa: E402
    cf = resolve_task_json_path(task_id=args.task_id, base_dir="evaluation_examples",
                                eval_version="v2")
    task_config = load_task_config(cf, task_id=args.task_id,
                                   base_dir="evaluation_examples", eval_version="v2")
    instruction = str(getattr(task_config, "instruction", None)
                      or task_config["instruction"])
    # GitLab-backed tasks (026/041) mint per-run credentials in user_config and their
    # own __init__ intends them INSIDE the instruction ("My GitLab account is '...'");
    # a BaseTask-level attribute shadow drops that line, so the loaded instruction says
    # "use my credentials" while naming none. Restore the task-authored credential line
    # from the SAME task_config object de.reset() provisions the account with, so the
    # agent receives exactly the input the official runner delivers. This is task input
    # (author-placed, in-instruction), never grader/gold signal — Constraint #0 intact.
    _uc = getattr(task_config, "user_config", None)
    if isinstance(_uc, dict) and _uc.get("username") and _uc.get("password") \
            and "password is" not in instruction:
        instruction = (f"{instruction} My GitLab account is '{_uc['username']}' "
                       f"and the password is '{_uc['password']}'.")

    de = DesktopEnv(provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
                    screen_size=(1920, 1080), headless=True, require_a11y_tree=False,
                    cache_dir=os.environ.get("RSIAGENT_OSWORLD_CACHE_DIR", "cache"),
                    volume_size=60)   # 60G: the near-full v2 image needs the guest fs grown
    preserve_completed_vm = False
    try:
        log.info("resetting env for %s (VM boot + task setup; takes minutes) ...",
                 args.task_id)
        t0 = time.time()
        de.reset(task_config=task_config)
        boot = time.time() - t0
        log.info("env ready in %.0fs", boot)
        vm = VM(de)

        quarantined_evaluators = []
        if getattr(cfg, "actor_evaluator_isolation", False):
            from benchmarks.osworld.actor_boundary import (                 # noqa: E402
                quarantine_actor_visible_evaluators,
            )
            quarantined_evaluators = quarantine_actor_visible_evaluators(vm)
            log.info(
                "quarantined %d guest-visible evaluator source file(s) before "
                "Actor work", len(quarantined_evaluators))

        if args.smoke:
            passed = smoke(vm)
            print("SMOKE", "PASS" if passed else "FAIL")
            return 0 if passed else 1

        _seed_dir = f"seed{args.seed}" + (f"_{args.tag}" if args.tag else "")
        sink = ArtifactSink(os.path.join(RSIAGENT_ROOT, "results", args.task_id, _seed_dir))
        reset_provider_counts()
        print(f"\n=== {args.task_id} seed{args.seed} model={cfg.model} ===\n"
              f"{instruction}\n")
        opening_extra = ""
        if getattr(cfg, "env_memory_dir", ""):
            # P2 eval flag: FROZEN practice memory rides along read-only —
            # synced IN, never synced out (write-lock by construction). Only a
            # mechanical recursive file inventory is shown. Retrieval is entirely
            # the Actor Agent's decision; no body or filename (including INDEX.md)
            # receives privileged treatment.
            from explore.commit import push_memory       # noqa: E402 (our code,
            from explore.charter import memory_preamble  # no benchmark contact)
            if push_memory(vm, cfg.env_memory_dir):
                _files = sorted(
                    os.path.relpath(os.path.join(root, name), cfg.env_memory_dir)
                    for root, _dirs, names in os.walk(cfg.env_memory_dir)
                    for name in names
                    if os.path.isfile(os.path.join(root, name)))
                _ls = "\n".join(
                    f"  {os.path.getsize(os.path.join(cfg.env_memory_dir, f)):>7}  {f}"
                    for f in _files)
                opening_extra = memory_preamble(_ls)
                log.info("env memory attached (%s, files %d)", cfg.env_memory_dir, len(_files))
            else:
                log.warning("env memory push FAILED — running memory-OFF")
        ask_user = (de.user_simulator.respond
                    if de.user_simulator is not None else None)
        try:
            res, history = run_with_resume(
                instruction, vm, cfg, sink, opening_extra=opening_extra,
                ask_user=ask_user)
        except AgenticVerifierNoProgressError as blocked:
            # The persistent Verifier may still own a rollback checkpoint and
            # private scratch on this VM. Keep that complete recovery surface;
            # an operator must restore/review the boundary before continuing.
            preserve_completed_vm = True
            container = getattr(getattr(de, "provider", None), "container", None)
            try:
                sink.save_recovery({
                    "status": "blocked", "stage": "agentic_verifier",
                    "reason": str(blocked), "vm_preserved": True,
                    "container_id": getattr(container, "id", None),
                    "controller_url": de.controller.http_server,
                    "verifier_execution_mode": verifier_execution_mode,
                    "recovery_requires_verifier_boundary_review": True,
                    "verifier_root": os.path.join(sink.root, "verifier_agent"),
                    "memory": memory_record,
                    "official_evaluator_calls": 0,
                })
            except OSError as state_error:
                log.warning("could not persist blocked verification: %s", state_error)
            log.error("%s; VM retained for Verifier recovery; scoring remains sealed",
                      blocked)
            raise SystemExit(75) from blocked
        sink.save_transcript(
            build_system(cfg, ask_enabled=ask_user is not None), history)
        preserve_completed_vm = True

        # Save everything needed to diagnose/regrade a completed candidate before
        # entering the sealed evaluator. This is never returned to either Agent.
        container = getattr(getattr(de, "provider", None), "container", None)
        sink._json(os.path.join(sink.root, "grading_checkpoint.json"), {
            "task": args.task_id, "seed": args.seed, "tag": args.tag,
            "model": cfg.model, "agent_result": asdict(res),
            "llm_providers": provider_counts(), "memory": memory_record,
            "benchmark_lock": benchmark_lock_record,
            "boot_secs": round(boot, 1),
            "container_id": getattr(container, "id", None),
            "controller_url": de.controller.http_server,
            "evaluator_correction_requested": args.evaluator_correction,
            "actor_visible_evaluator_files_quarantined": len(quarantined_evaluators),
            "official_evaluator_feedback_entered_agent": False,
        })

        # SEALED: grading starts only after both Agents have terminated. Evaluator
        # transport/configuration failures must never vaporize the completed VM and
        # force a stochastic Actor rerun, so the exact same state remains alive until
        # grading succeeds or an operator explicitly interrupts the process.
        try:
            grade_delay = max(0.0, float(os.environ.get(
                "OSWORLD_EVAL_OUTER_RETRY_DELAY",
                os.environ.get("OSWORLD_EVAL_MODEL_RETRY_DELAY", "30"))))
        except (TypeError, ValueError):
            grade_delay = 30.0
        grade_failures = 0

        def _grade_error(attempt, error):
            nonlocal grade_failures
            grade_failures = attempt
            log.warning(
                "evaluate() attempt %d failed; completed VM preserved: %s",
                attempt, error)
            try:
                sink.save_grading_state({
                    "status": "pending",
                    "attempt": attempt,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "retry_delay_seconds": grade_delay,
                })
            except Exception as state_error:  # noqa: BLE001
                # Even a full/temporarily unavailable artifact disk must not turn a
                # recoverable grader outage into candidate destruction.
                log.warning("could not persist grading retry state: %s", state_error)

        def _sealed_evaluate():
            return de.evaluate()

        evaluator_correction_record = None
        if args.evaluator_correction:
            from benchmarks.osworld.evaluator_corrections import apply_evaluator_correction
            evaluator_correction_record = apply_evaluator_correction(
                task_config, args.evaluator_correction)

        try:
            r = run_until_success(
                _sealed_evaluate, on_error=_grade_error, retry_delay=grade_delay)
        except GradingBlocked as blocked:
            preserve_completed_vm = True
            sink.save_grading_state({
                "status": "blocked", "attempt": blocked.attempt,
                "error_type": type(blocked.error).__name__,
                "error": str(blocked.error),
                "candidate_preserved": True,
                "container_id": getattr(container, "id", None),
            })
            log.error("%s; completed candidate preserved for operator recovery", blocked)
            raise SystemExit(75) from blocked
        sink.save_grading_state({
            "status": "complete", "failed_attempts": grade_failures})
        score = r.get("score", r) if isinstance(r, dict) else r
        payload = {"task": args.task_id, "seed": args.seed, "tag": args.tag,
                   "model": cfg.model,
                   "score": float(score), "status": res.status, "iters": res.iters,
                   "turns": res.turns, "programs_run": res.programs_run,
                   "asks": res.asks,
                   "checks_passed": res.checks_passed,
                   "inspections": res.inspections, "resumes": res.resumes,
                   "llm_providers": provider_counts(),
                   "wall_secs": round(res.wall_secs, 1),
                   "infra_pause_secs": round(res.infra_pause_secs, 1),
                   "infra_pauses": res.infra_pauses,
                   "boot_secs": round(boot, 1),
                   "official_evaluator_successful_calls": 1,
                   "official_evaluator_failed_attempts": grade_failures,
                   "official_evaluator_attempts": grade_failures + 1,
                   "memory": memory_record,
                   "benchmark_lock": benchmark_lock_record,
                   "evaluator_correction": evaluator_correction_record,
                   "actor_visible_evaluator_files_quarantined":
                       len(quarantined_evaluators),
                   "official_evaluator_feedback_entered_agent": False}
        sink.save_result(payload)
        preserve_completed_vm = False
        print("\n=== RESULT " + json.dumps(payload))
    finally:
        if not preserve_completed_vm:
            try:
                de.close()
            except Exception:   # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
