"""Sealed agent worker. Receives public task inputs and VM transport, no grader."""

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .protocol import protocol, role, verify_memory, write_json
from .runtime import bind_platform, initialize_vm, make_vm


def evaluation(spec):
    from core import actor
    from core.loop import run_with_resume
    from core.trace import ArtifactSink
    from explore.charter import memory_preamble
    from explore.commit import push_memory
    from explore.practice_loop import _listing, _read_memory_tree

    root = Path(spec["work_dir"])
    cfg = role("baseline")
    vm = make_vm(spec["sandbox"])
    initialize_vm(vm)
    bind_platform(vm, spec.get("visible_roots", ()))
    opening = ""
    memory = spec.get("input_memory")
    if memory is not None:
        verify_memory(memory)
        if not push_memory(vm, memory["path"]):
            raise RuntimeError("Frozen evaluation memory upload failed")
        cfg.env_memory_dir = memory["path"]
        opening = memory_preamble(_listing(_read_memory_tree(memory["path"])))
    sink = ArtifactSink(str(root))
    write_json(root / "resolved-config.json", asdict(cfg))
    sessions = {}
    try:
        result, history = run_with_resume(
            spec["instruction"],
            vm,
            cfg,
            sink,
            opening_extra=opening,
            verifier_sessions=sessions,
        )
    finally:
        for session in sessions.values():
            session.close_executor()
    if memory is not None:
        verify_memory(memory)
    sink.save_transcript(actor.build_system(cfg), history)
    return {
        "status": result.status,
        "agent_result": asdict(result),
        "safe_to_grade": True,
        "memory": memory or "off",
        "memory_writeback": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    root = Path(spec["work_dir"])
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    protocol()
    started = time.time()
    write_json(
        root / "worker-result.json", {"status": "running", "safe_to_grade": False}
    )
    try:
        if spec["phase"] not in ("baseline", "phase1", "phase2", "phase3"):
            raise ValueError("Unknown ALE phase: " + str(spec["phase"]))
        if spec["phase"] in ("baseline", "phase3"):
            result = evaluation(spec)
        else:
            from .learning import phase1, phase2

            result = (phase1 if spec["phase"] == "phase1" else phase2)(spec)
            result["safe_to_grade"] = False
        from llm.client import provider_counts

        result.update(
            elapsed_seconds=time.time() - started, providers=provider_counts()
        )
        write_json(root / "worker-result.json", result)
        return 0 if result.get("safe_to_grade") or result.get("transition_ready") else 2
    except BaseException as exc:
        logging.exception("Worker stopped before phase completion")
        write_json(
            root / "worker-result.json",
            {
                "status": "infrastructure_error",
                "safe_to_grade": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": time.time() - started,
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
