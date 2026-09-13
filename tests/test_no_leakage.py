"""Constraint #0 audit — mechanical no-leakage checks. Run on EVERY prompt change.

1. The agent packages (core/, env/, llm/, config/) must not reference the benchmark:
   no imports or use of the env/task/grader plumbing (those live only in run_task.py).
2. The audited surfaces (core/actor.py + core/verifier.py + core/eyes.py — every file
   holding model-facing prompt literals) must contain no task-derived token from
   tests/blocklist_012.txt. The blocklist IS grader/setup-derived, but it is
   reject-only: it never enters the agent; it can only force prompts to be MORE
   general.
3. run_task.py must call the grader exactly once and hand the agent only the
   instruction string.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKGS = ("core", "env", "llm", "config")
SURFACES = ("core/actor.py", "core/verifier.py", "core/eyes.py")

FORBIDDEN = ("desktop_env", "task_loader", "evaluators", "getters.", "task_class",
             ".evaluate(", "cache/")


def test_packages_never_touch_benchmark():
    hits = []
    for pkg in PKGS:
        d = os.path.join(ROOT, pkg)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(d, fn)).read()
            for tok in FORBIDDEN:
                if tok in src:
                    hits.append(f"{pkg}/{fn}: {tok}")
    assert not hits, f"benchmark plumbing referenced inside agent packages: {hits}"


def test_prompts_contain_no_task_tokens():
    blocklist = [l.strip().lower() for l in
                 open(os.path.join(ROOT, "tests", "blocklist_012.txt"))
                 if l.strip() and not l.startswith("#")]
    for surface in SURFACES:
        src = open(os.path.join(ROOT, surface)).read().lower()
        hits = [t for t in blocklist if re.search(r"\b" + re.escape(t) + r"\b", src)]
        assert not hits, f"task-derived tokens in {surface}: {hits}"

    charter = open(os.path.join(ROOT, "explore", "charter.py")).read().lower()
    agentic = charter.split("def agent_harness_verifier_charter(", 1)[1].split(
        "def self_evolving_target_verifier_charter(", 1)[0]
    hits = [t for t in blocklist if re.search(r"\b" + re.escape(t) + r"\b", agentic)]
    assert not hits, f"task-derived tokens in agentic verifier charter: {hits}"

    runtime = open(os.path.join(ROOT, "core", "verifier_runtime.py")).read().lower()
    runtime_prompts = runtime.split("agentic_verifier_system =", 1)[1].split(
        "@dataclass", 1)[0]
    hits = [t for t in blocklist
            if re.search(r"\b" + re.escape(t) + r"\b", runtime_prompts)]
    assert not hits, f"task-derived tokens in verifier runtime prompts: {hits}"


def test_runner_seals_the_grader():
    src = open(os.path.join(ROOT, "run_task.py")).read()
    calls = src.count(".evaluate(")
    assert calls == 1, f"run_task.py must call the grader exactly once (found {calls})"
    # P2-v2: the loop may additionally receive opening_extra — but ONLY the
    # agent's OWN practice memory (cfg.env_memory_dir -> memory_preamble),
    # never anything task_config-derived. The seal's real property: nothing
    # task-derived flows into the loop beyond the instruction string.
    tree = ast.parse(src)
    calls_to_loop = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_with_resume"]
    assert len(calls_to_loop) == 1, \
        "run_task must invoke run_with_resume exactly once"
    positional = calls_to_loop[0].args
    assert [node.id for node in positional[:4]
            if isinstance(node, ast.Name)] == ["instruction", "vm", "cfg", "sink"], \
        "run_with_resume must receive only the instruction string and harness objects"
    _extra = src.split('opening_extra = ""', 1)[1].split("run_with_resume(", 1)[0]
    assert "task_config" not in _extra, \
        "opening_extra construction must never touch task_config"
    assert "memory_preamble" in _extra, \
        "opening_extra may only carry the agent's own memory preamble"
    call_source = ast.get_source_segment(src, calls_to_loop[0]) or ""
    assert "task_config" not in call_source


if __name__ == "__main__":
    test_packages_never_touch_benchmark()
    test_prompts_contain_no_task_tokens()
    test_runner_seals_the_grader()
    print("test_no_leakage: OK")
    sys.exit(0)
