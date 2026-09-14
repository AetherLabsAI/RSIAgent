"""Bind a direct benchmark run to the runtime declared by its config lock.

The batch runner exports evaluator and task-user-simulator routing
before it starts :mod:`run_task`.  Researchers also launch individual tasks
directly while recovering or rerunning a result.  A config such as
``foo.yaml`` may therefore carry a sibling ``foo.lock.json``; this module makes
that lock authoritative in both launch paths.

Only transport metadata and credentials are mounted.  Task content, evaluator
source, scores, and grader output remain outside the Actor/Verifier lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import MutableMapping
from pathlib import Path

from config.runtime_paths import resolve_env_file, resolve_path


class BenchmarkRuntimeError(RuntimeError):
    """The declared benchmark runtime is missing, inconsistent, or unusable."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_secret(environment: MutableMapping[str, str], name: str,
                 *, repo_root: Path) -> None:
    if environment.get(name):
        return
    env_path = resolve_env_file(environment, repo_root)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BenchmarkRuntimeError(
            f"benchmark credential {name!r} is absent and the credential file "
            f"cannot be read: {env_path}") from exc
    prefix = f"{name}="
    for line in lines:
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
            if value:
                environment[name] = value
                return
    raise BenchmarkRuntimeError(
        f"benchmark credential {name!r} is absent from the environment and "
        f"credential file: {env_path}")


def _bind(environment: MutableMapping[str, str], name: str, value: object,
          *, lock_path: Path) -> None:
    rendered = str(value)
    current = environment.get(name, "")
    if current and current != rendered:
        raise BenchmarkRuntimeError(
            f"{name} conflicts with associated benchmark lock {lock_path}: "
            f"environment={current!r}, lock={rendered!r}")
    environment[name] = rendered


def configure_associated_benchmark_lock(
        config_path: Path | None, *, repo_root: Path,
        environment: MutableMapping[str, str] | None = None) -> dict | None:
    """Apply the evaluator/user-channel runtime from a config's sibling lock.

    ``RSIAGENT_BENCHMARK_LOCK`` explicitly selects a lock.  Otherwise ``foo.yaml``
    discovers ``foo.lock.json``.  Configs without an associated lock preserve
    the historical generic ``run_task.py`` behavior.
    """
    if config_path is None:
        return None
    values = os.environ if environment is None else environment
    explicit = values.get("RSIAGENT_BENCHMARK_LOCK", "").strip()
    lock_path = (resolve_path(explicit, values, repo_root)
                 if explicit else config_path.with_suffix(".lock.json"))
    if not lock_path.is_file():
        if explicit:
            raise BenchmarkRuntimeError(
                f"explicit benchmark lock does not exist: {lock_path}")
        return None

    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkRuntimeError(
            f"cannot read associated benchmark lock: {lock_path}") from exc

    actor = lock.get("actor_agent")
    if not isinstance(actor, dict):
        raise BenchmarkRuntimeError(
            f"associated benchmark lock has no actor_agent object: {lock_path}")
    declared_config = resolve_path(
        str(actor.get("config", "")), values, repo_root)
    if declared_config != config_path.resolve():
        raise BenchmarkRuntimeError(
            f"associated lock binds a different Actor config: {declared_config}")
    declared_hash = str(actor.get("sha256", ""))
    observed_hash = _sha256(config_path)
    if not declared_hash or declared_hash != observed_hash:
        raise BenchmarkRuntimeError(
            "Actor config does not match its associated benchmark lock: "
            f"expected={declared_hash!r}, observed={observed_hash!r}")

    evaluator = lock.get("evaluator")
    if not isinstance(evaluator, dict):
        raise BenchmarkRuntimeError(
            f"associated benchmark lock has no evaluator object: {lock_path}")
    required_eval = (
        "provider", "model", "base_url", "api_key_env",
        "retry_attempts", "retry_delay_seconds")
    missing = [key for key in required_eval if evaluator.get(key) in (None, "")]
    if missing:
        raise BenchmarkRuntimeError(
            f"associated evaluator runtime lacks {missing}: {lock_path}")
    key_env = str(evaluator["api_key_env"])
    _load_secret(values, key_env, repo_root=repo_root)
    evaluator_bindings = {
        "OSWORLD_EVAL_MODEL_PROVIDER": evaluator["provider"],
        "OSWORLD_EVAL_MODEL_NAME": evaluator["model"],
        "OSWORLD_EVAL_MODEL_BASE_URL": evaluator["base_url"],
        "OSWORLD_EVAL_MODEL_API_KEY_ENV": key_env,
        "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS": evaluator["retry_attempts"],
        "OSWORLD_EVAL_MODEL_RETRY_DELAY": evaluator["retry_delay_seconds"],
    }
    if evaluator.get("website_host_suffix"):
        evaluator_bindings["WEBSITE_HOST_SUFFIX"] = \
            evaluator["website_host_suffix"]
    for name, value in evaluator_bindings.items():
        _bind(values, name, value, lock_path=lock_path)

    user_simulator = lock.get("user_simulator")
    if user_simulator is not None:
        if not isinstance(user_simulator, dict):
            raise BenchmarkRuntimeError(
                f"associated user_simulator runtime is not an object: {lock_path}")
        required_user = (
            "provider", "model", "base_url", "api_key_env", "max_tokens")
        missing = [key for key in required_user
                   if user_simulator.get(key) in (None, "")]
        if missing:
            raise BenchmarkRuntimeError(
                f"associated user-simulator runtime lacks {missing}: {lock_path}")
        user_key_env = str(user_simulator["api_key_env"])
        _load_secret(values, user_key_env, repo_root=repo_root)
        for name, value in {
                "OSWORLD_USER_SIM_PROVIDER": user_simulator["provider"],
                "OSWORLD_USER_SIM_MODEL": user_simulator["model"],
                "OSWORLD_USER_SIM_BASE_URL": user_simulator["base_url"],
                "OSWORLD_USER_SIM_API_KEY_ENV": user_key_env,
                "OSWORLD_USER_SIM_MAX_TOKENS": user_simulator["max_tokens"],
        }.items():
            _bind(values, name, value, lock_path=lock_path)

    lock_hash = _sha256(lock_path)
    values["RSIAGENT_BENCHMARK_LOCK_RESOLVED"] = str(lock_path.resolve())
    values["RSIAGENT_BENCHMARK_LOCK_SHA256"] = lock_hash
    return {
        "path": str(lock_path.resolve()),
        "sha256": lock_hash,
        "benchmark_release": str(lock.get("benchmark_release", "")),
        "evaluator_provider": str(evaluator["provider"]),
        "evaluator_model": str(evaluator["model"]),
    }
