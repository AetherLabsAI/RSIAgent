"""OSWorld-specific pre-Actor boundary for removing evaluator source leaks.

OSWorld setup payloads may accidentally unpack task-class Python files beside the
authoritative user inputs. Those files are neither inputs nor candidate state: they
contain evaluator implementation details that no Agent should observe. This module
quarantines only files mechanically identified as OSWorld ``BaseTask`` sources (and
their compiled siblings), before S0 is captured and before Verifier orientation.
"""

from __future__ import annotations

import base64
import json
import posixpath
from typing import Any

_SCAN_SCRIPT = r"""\
import glob
import hashlib
import json
import os

root = "/home/user"
found = set()
for directory, directories, names in os.walk(root, followlinks=False):
    directories[:] = [
        name for name in directories
        if name not in {"node_modules", ".git"}
        and not os.path.islink(os.path.join(directory, name))
    ]
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path) or not name.endswith((".py", ".pyc")):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if (b"desktop_env.task_base" not in data
                or b"BaseTask" not in data):
            continue
        found.add(path)
        if name.endswith(".py"):
            stem = name[:-3]
            found.update(glob.glob(os.path.join(directory, stem + ".pyc")))
            found.update(glob.glob(
                os.path.join(directory, "__pycache__", stem + ".*.pyc")))

for path in sorted(found):
    if (not path.startswith(root + os.sep) or os.path.islink(path)
            or not os.path.isfile(path)):
        continue
    with open(path, "rb") as handle:
        data = handle.read()
    print(json.dumps({
        "path": path,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }, sort_keys=True))
"""


def _scan_records(stdout: str) -> list[dict[str, Any]]:
    records = []
    for raw in str(stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("[exit "):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "trusted evaluator-source scan returned malformed output"
            ) from exc
        path = str(record.get("path") or "")
        normalized = posixpath.normpath(path)
        if (
            not normalized.startswith("/home/user/")
            or normalized != path
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise RuntimeError(
                "trusted evaluator-source scan returned an invalid record"
            )
        records.append(
            {
                "path": path,
                "bytes": record["bytes"],
                "sha256": record["sha256"],
            }
        )
    return records


def _encoded_paths_payload(records: list[dict[str, Any]]) -> str:
    """Encode one newline-terminated record per path for a POSIX read loop."""
    return "".join(
        base64.b64encode(record["path"].encode("utf-8")).decode("ascii") + "\n"
        for record in records
    )


def quarantine_actor_visible_evaluators(vm) -> list[dict[str, Any]]:
    """Move mechanically recognized evaluator files behind a root-only boundary.

    The scan and move are trusted setup operations, never Agent actions. No content
    or path is placed in either Agent's prompt. Failure is fail-closed: a target
    attempt must not begin if detected evaluator bytes remain Actor-visible.
    """
    scan = vm.run_script("python", _SCAN_SCRIPT, timeout=180, cap=0)
    if getattr(scan, "infra_fail", False) or getattr(scan, "exit_code", None) != 0:
        raise RuntimeError(
            "could not establish the pre-Actor evaluator-isolation boundary: "
            + str(getattr(scan, "stdout", "") or "scan failed")
        )
    records = _scan_records(getattr(scan, "stdout", ""))
    if not records:
        return []

    paths_payload = _encoded_paths_payload(records)
    password = str(getattr(getattr(vm, "env", None), "client_password", "") or "")
    password_b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    payload_b64 = base64.b64encode(paths_payload.encode("ascii")).decode("ascii")
    script = f"""\
set -eu
sudo_password_b64={password_b64!r}
paths_payload_b64={payload_b64!r}
quarantine=/root/.rsiagent_actor_evaluator_quarantine
run_sudo() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- "$@"
}}
run_sudo install -d -m 0700 -- "$quarantine"
index=0
printf %s "$paths_payload_b64" | base64 -d | while IFS= read -r encoded; do
  test -n "$encoded" || continue
  path=$(printf %s "$encoded" | base64 -d)
  case "$path" in
    /home/user/*) ;;
    *) echo "invalid quarantine path"; exit 125 ;;
  esac
  test ! -L "$path" || {{ echo "refusing evaluator symlink"; exit 125; }}
  test -f "$path" || {{ echo "evaluator disappeared before quarantine"; exit 125; }}
  digest=$(printf %s "$path" | sha256sum | cut -d' ' -f1)
  run_sudo mv -- "$path" "$quarantine/$digest"
  run_sudo chmod 0600 -- "$quarantine/$digest"
  index=$((index + 1))
done
run_sudo find "$quarantine" -mindepth 1 -maxdepth 1 -type f -printf .
echo
echo __RSIAGENT_EVALUATOR_QUARANTINE_READY__
"""
    moved = vm.run_script("bash", script, timeout=180, cap=0)
    output = str(getattr(moved, "stdout", "") or "")
    if (
        getattr(moved, "infra_fail", False)
        or getattr(moved, "exit_code", None) != 0
        or "__RSIAGENT_EVALUATOR_QUARANTINE_READY__" not in output
    ):
        raise RuntimeError(
            "detected evaluator source but could not quarantine it: " + output
        )

    # Re-scan instead of trusting the move command's success text.
    verify = vm.run_script("python", _SCAN_SCRIPT, timeout=180, cap=0)
    if getattr(verify, "infra_fail", False) or getattr(verify, "exit_code", None) != 0:
        raise RuntimeError("could not verify the evaluator-isolation boundary")
    remaining = _scan_records(getattr(verify, "stdout", ""))
    if remaining:
        raise RuntimeError("evaluator source remained Actor-visible after quarantine")
    return records


__all__ = ["quarantine_actor_visible_evaluators"]
