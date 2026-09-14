"""Candidate-safe, code-as-policy runtimes for the Verifier Agent.

The historical ``effect_isolated`` boundary runs Verifier programs in Linux
namespaces where the candidate filesystem is read-only and Actor process/network/GUI
channels are absent.  The experimental ``rollback_mirror`` boundary instead saves
the complete QEMU state, lets the Verifier observe and test that live state, and
restores the checkpoint before grading.  In both modes Actor-private paths remain
hidden and Verifier-authored effects cannot enter the scored candidate.

The final report is published directly to the host harness and never enters the
guest. There are no verifier-specific observation, file-size, probe-count, or
program-count caps; the ordinary run wall clock remains the emergency ceiling.
"""
from __future__ import annotations

import base64
import os
import pathlib
import posixpath
import re
import secrets
import shlex
import tempfile
import time
from dataclasses import dataclass

AGENTIC_VERIFIER_SYSTEM = """\
You are an independent Verifier Agent inspecting one completed candidate on a real
Ubuntu machine. You decide how to investigate, what programs and tools to use, how
deeply to test, and when the evidence is sufficient. You do not repair the task.

Use exactly ONE plain JSON action object per reply. You may run arbitrary code with:
- {"program":{"lang":"python","code":"<complete Python program>"}}
- {"program":{"lang":"bash","code":"<complete Bash program>"}}
You may inspect pixels with:
- {"look":{"path":"/absolute/local/path","question":"..."}}
- {"look":{"path":"screen:","question":"..."}}
When your investigation is complete, publish your freely organized report with:
- {"program":{"lang":"verifier-report","code":"<complete report>"}}
The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.
Done does not publish a Verifier decision.

Your Python/Bash runs are mechanically effect-isolated from the Actor's candidate:
the machine filesystem is read-only, the Actor's processes, network, GUI, and IPC
channels are unavailable, and privilege escalation is disabled. Your private
network namespace has loopback only: you may start and query your own local service,
but cannot reach the Actor's service or any external endpoint. VERIFIER_SCRATCH
names your private writable tmpfs; it persists across your Program actions and may
hold copies, renders, extracted archives, databases, or any other investigative
artifacts. /tmp is also writable but lasts only for the current Program. Copy inputs
to VERIFIER_SCRATCH before using a tool that expects to write beside its input. A
normal Look may inspect files you generated in VERIFIER_SCRATCH. These mechanics
protect the scored state without prescribing your method.

Treat observations as evidence, not proof by assertion. Derive every binding
requirement from the authoritative task and falsify nearby plausible substitutes.
PASS only when every material requirement is affirmatively supported. If a material
requirement is violated or remains unsupported after investigation, publish FAIL
with the concrete evidence. Causal learning and repair remain the Actor Agent's role.
"""

AGENTIC_VERIFIER_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program when your decision is ready.')

AGENTIC_VERIFIER_STRICT_NUDGE = (
    'Your reply described an investigation step but performed none. Emit exactly '
    'one Python/Bash Program, Look, or final verifier-report action now.')

AGENTIC_VERIFIER_PREMATURE_DONE = (
    'Done does not publish a Verifier decision. Continue investigating with arbitrary '
    'Python/Bash or Look, then finish with one verifier-report Program containing '
    'your complete report and exactly one standalone VERDICT line.')

AGENTIC_VERIFIER_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT: You remain a full investigation Agent. Use one "
    "arbitrary Python/Bash Program or Look per turn; programs are mechanically "
    "effect-isolated and VERIFIER_SCRATCH persists. Publish the final decision with "
    "verifier-report, not Done.")


_EFFECT_ISOLATION_DESCRIPTION = """\
Your Python/Bash runs are mechanically effect-isolated from the Actor's candidate:
the machine filesystem is read-only, the Actor's processes, network, GUI, and IPC
channels are unavailable, and privilege escalation is disabled. Your private
network namespace has loopback only: you may start and query your own local service,
but cannot reach the Actor's service or any external endpoint. VERIFIER_SCRATCH
names your private writable tmpfs; it persists across your Program actions and may
hold copies, renders, extracted archives, databases, or any other investigative
artifacts. /tmp is also writable but lasts only for the current Program. Copy inputs
to VERIFIER_SCRATCH before using a tool that expects to write beside its input. A
normal Look may inspect files you generated in VERIFIER_SCRATCH. These mechanics
protect the scored state without prescribing your method."""

_ROLLBACK_MIRROR_DESCRIPTION = """\
Your Python/Bash runs execute in a transactional mirror of the exact live Actor
machine. At the beginning of this inspection the harness saved a complete QEMU
checkpoint. You can observe its files, processes, localhost services, network, GUI,
and IPC and may run arbitrary tests. When this inspection ends, the harness restores
that checkpoint before the candidate is graded, so none of your in-VM effects enter
the scored state. Privilege escalation is disabled and harness-owned Actor-private
paths are absent. VERIFIER_SCRATCH is a private writable tmpfs that persists across
your Program actions. State in an external service outside this QEMU guest cannot be
rolled back: observe such services without sending mutating requests. Establish the
original candidate's properties before destructive experiments; a property created
only by your own test is not candidate evidence. These mechanics protect the scored
state without prescribing your method."""


def agentic_verifier_system_for(
        execution_mode="effect_isolated", *, orienting=False,
        allow_evolve=False, allow_unverified=False):
    """Return the role prompt whose mechanics match the active executor."""
    from env.qemu_rollback import (
        ROLLBACK_MIRROR,
        normalize_verifier_execution_mode,
    )

    mode = normalize_verifier_execution_mode(execution_mode)
    base = (AGENTIC_VERIFIER_ORIENTATION_SYSTEM if orienting else
            (AGENTIC_VERIFIER_ROUTE_SYSTEM if allow_evolve
             else AGENTIC_VERIFIER_SYSTEM))
    if allow_unverified and not orienting and not allow_evolve:
        base = base.replace(
            "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
            "The report must contain exactly one standalone VERDICT: PASS, "
            "VERDICT: FAIL, or VERDICT: UNVERIFIED line.",
        ).replace(
            "PASS only when every material requirement is affirmatively supported. If a material\n"
            "requirement is violated or remains unsupported after investigation, publish FAIL\n"
            "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
            "PASS only when every material requirement is affirmatively supported. Publish FAIL\n"
            "when concrete evidence establishes a material violation. Publish UNVERIFIED when a\n"
            "material claim remains unresolved after investigation or credible instruments disagree.\n"
            "UNVERIFIED requests reproducible evidence from the same Actor Agent; it is neither\n"
            "acceptance nor proof of failure. Reconcile conflicting channels before PASS rather than\n"
            "silently privileging one instrument. Causal learning and repair remain the Actor's role.",
        )
    if mode == ROLLBACK_MIRROR:
        base = base.replace(
            _EFFECT_ISOLATION_DESCRIPTION, _ROLLBACK_MIRROR_DESCRIPTION)
    return base


# The unified loop changes only the terminal transport token. Investigation
# authority and effect isolation remain identical to the validated Agent runtime.
AGENTIC_VERIFIER_ROUTE_SYSTEM = AGENTIC_VERIFIER_SYSTEM.replace(
    "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
    "The report must contain exactly one standalone ROUTE: HANDOFF, ROUTE: REVISE, "
    "or ROUTE: EVOLVE line.",
).replace(
    "PASS only when every material requirement is affirmatively supported. If a material\n"
    "requirement is violated or remains unsupported after investigation, publish FAIL\n"
    "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
    "HANDOFF only when every material requirement is affirmatively supported. Otherwise,\n"
    "publish REVISE or EVOLVE as defined in the authoritative project charter, with the\n"
    "concrete evidence. Causal learning and repair remain the Actor Agent's role.",
)

AGENTIC_VERIFIER_ROUTE_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program containing one HANDOFF / REVISE / EVOLVE route '
    'when your decision is ready.')

AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE = (
    'Your reply described an investigation step but performed none. Emit exactly '
    'one Python/Bash Program, Look, or final verifier-report route action now.')

AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE = (
    'Done does not publish a Verifier route. Continue investigating with arbitrary '
    'Python/Bash or Look, then finish with one verifier-report Program containing '
    'your complete report and exactly one standalone ROUTE line.')

AGENTIC_VERIFIER_ROUTE_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT: You remain a full investigation Agent. Use one "
    "arbitrary Python/Bash Program or Look per turn; programs are mechanically "
    "effect-isolated and VERIFIER_SCRATCH persists. Publish HANDOFF, REVISE, or "
    "EVOLVE through verifier-report, not Done.")


AGENTIC_VERIFIER_ORIENTATION_SYSTEM = AGENTIC_VERIFIER_SYSTEM.replace(
    "one completed candidate",
    "the trusted task-start environment during private orientation before any "
    "Actor Agent action",
).replace(
    "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
    "The private report must contain exactly one standalone "
    "STAGE: ORIENTATION_READY line.",
).replace(
    "Treat observations as evidence, not proof by assertion. Derive every binding\n"
    "requirement from the authoritative task and falsify nearby plausible substitutes.\n"
    "PASS only when every material requirement is affirmatively supported. If a material\n"
    "requirement is violated or remains unsupported after investigation, publish FAIL\n"
    "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
    "No candidate exists in this stage. Freely learn the authoritative environment,\n"
    "preserve evidence, and use or author any private instruments you judge useful.\n"
    "An executable evaluator is optional. Do not grade, route, or repair anything.",
)

AGENTIC_VERIFIER_ORIENTATION_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program containing STAGE: ORIENTATION_READY when your '
    'private task-start orientation is ready.')

AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE = (
    'Done does not complete Verifier orientation. Continue with arbitrary Python/Bash '
    'or Look, then publish one verifier-report with exactly one standalone '
    'STAGE: ORIENTATION_READY line when you judge the orientation sufficient.')

AGENTIC_VERIFIER_ORIENTATION_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT — STAGE: ORIENTATION. Use one arbitrary "
    "Python/Bash Program or Look per turn; VERIFIER_SCRATCH persists. No candidate "
    "exists, so do not grade or route. Publish STAGE: ORIENTATION_READY through "
    "verifier-report when you judge your private task-start orientation sufficient.")


def agentic_verifier_user_message(
        message, allow_evolve=False, stage="candidate_verification",
        execution_mode="effect_isolated", allow_unverified=False):
    """Preserve every observation and append the role's truthful action contract."""
    if stage == "orientation":
        reminder = AGENTIC_VERIFIER_ORIENTATION_USER_REMINDER
    else:
        reminder = (AGENTIC_VERIFIER_ROUTE_USER_REMINDER if allow_evolve
                    else AGENTIC_VERIFIER_USER_REMINDER)
        if allow_unverified and not allow_evolve:
            reminder = reminder.replace(
                "Publish the final decision",
                "Publish PASS, FAIL, or UNVERIFIED")
    from env.qemu_rollback import ROLLBACK_MIRROR, normalize_verifier_execution_mode

    if normalize_verifier_execution_mode(execution_mode) == ROLLBACK_MIRROR:
        reminder = reminder.replace(
            "programs are mechanically effect-isolated",
            "programs run inside the checkpointed rollback mirror")
    return (message or "").rstrip() + "\n\n" + reminder


@dataclass
class VerifierTrace:
    """Duck-compatible result consumed by :func:`core.loop.run_attempt`."""

    stdout: str
    exit_code: int | None = None
    secs: float = 0.0
    timed_out: bool = False
    infra_fail: bool = False


def _trace(message, exit_code=0, *, infra_fail=False):
    suffix = f"\n[exit {exit_code}]"
    return VerifierTrace(
        stdout=(message or "") + suffix,
        exit_code=exit_code,
        infra_fail=infra_fail,
    )


class AgenticVerifierInfrastructureError(RuntimeError):
    """The Verifier's trusted execution boundary became unavailable.

    This is deliberately distinct from a Verifier verdict.  Callers must abort or
    mechanically recover the run; they may never expose it to an Agent as evidence
    about candidate correctness.
    """


class AgenticVerifierNoProgressError(AgenticVerifierInfrastructureError):
    """Repeated actionless segments need review of the preserved inspection.

    Rebuilding an execution boundary cannot repair an actionless model context.
    Task runners should preserve the VM for recovery and leave scoring sealed.
    """


def _run_script_with_staging_fallback(vm, lang, code, *, timeout, cap):
    """Use the new Verifier-only staging fallback without breaking live workers.

    Long benchmark waves can have already-imported ``VM`` instances when a newer
    verifier_runtime module is imported lazily. Such workers expose the previous
    run_script signature. Falling back to that exact legacy call preserves their
    pre-patch behavior instead of crashing at the Verifier handoff.
    """
    try:
        return vm.run_script(
            lang, code, timeout=timeout, cap=cap,
            allow_staging_fallback=True)
    except TypeError as exc:
        if "unexpected keyword argument 'allow_staging_fallback'" not in str(exc):
            raise
        return vm.run_script(lang, code, timeout=timeout, cap=cap)


def _normalize_private_paths(hide_actor_memory=False, private_paths=()):
    paths = list(private_paths or ())
    if hide_actor_memory:
        paths.append("/home/user/.memory")
    normalized = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Verifier private paths must be absolute guest paths")
        candidate = posixpath.normpath(path)
        if candidate == "/" or "\x00" in candidate or "\n" in candidate:
            raise ValueError("Verifier private path is unsafe")
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def _is_private_path(path: str, private_paths=()) -> bool:
    normalized = posixpath.normpath(path)
    return any(normalized == private
               or normalized.startswith(private.rstrip("/") + "/")
               for private in private_paths)


def validate_verifier_look_path(
        path, hide_actor_memory=False, private_paths=()):
    """Reject remote/option-like Look targets before a trusted renderer sees them."""
    if not isinstance(path, str) or "\x00" in path or "\n" in path:
        return False, "Verifier Look path is malformed."
    if path.lower().startswith("screen:"):
        display = path.split(":", 1)[1].strip()
        if not display or re.fullmatch(r":?\d+(?:\.\d+)?", display):
            return True, ""
        return False, "Verifier screen Look requires a numeric display."
    if not path.startswith("/"):
        return False, "Verifier Look requires an absolute local path or screen:."
    hidden = _normalize_private_paths(hide_actor_memory, private_paths)
    if _is_private_path(path, hidden):
        return False, "That path is private to the Actor Agent."
    return True, ""


class AgenticVerifierExecutor:
    """Execute Verifier-authored code without changing the scored candidate.

    Both modes give the Verifier an unbounded private tmpfs workspace and drop all
    capabilities. ``effect_isolated`` additionally makes the visible filesystem
    read-only and creates private PID/network/IPC/UTS namespaces.
    ``rollback_mirror`` instead exposes the exact live namespaces inside a QEMU
    transaction and restores the checkpoint when this executor closes.

    If the selected protection primitive is unavailable, execution fails closed as
    infrastructure. It never falls back to ordinary unprotected model execution.
    """

    def __init__(self, vm, *, hide_actor_memory=False, private_paths=(),
                 scratch_archive=None, execution_mode="effect_isolated"):
        from env.qemu_rollback import (
            ROLLBACK_MIRROR,
            QemuRollbackTransaction,
            normalize_verifier_execution_mode,
        )

        self._vm = vm
        self._execution_mode = normalize_verifier_execution_mode(execution_mode)
        self._hide_actor_memory = bool(hide_actor_memory)
        self._private_paths = _normalize_private_paths(
            hide_actor_memory, private_paths)
        self._scratch_archive = scratch_archive
        checkpoint_options = {}
        if hasattr(vm, "verifier_checkpoint_timeout"):
            checkpoint_options["timeout"] = vm.verifier_checkpoint_timeout
        self._rollback_transaction = (
            QemuRollbackTransaction(vm, **checkpoint_options)
            if self._execution_mode == ROLLBACK_MIRROR else None)
        self._rollback_private_paths_hidden = False
        password = str(getattr(getattr(vm, "env", None),
                               "client_password", "") or "")
        self._sudo_password_b64 = base64.b64encode(
            password.encode("utf-8")).decode("ascii")
        self._workspace = "/mnt/rsiagent_verifier_" + secrets.token_hex(16)
        self._namespace_pid = None
        self._desktop_uid = None
        self._desktop_gid = None
        self._initialized = False
        self._workspace_attempted = False
        self._closed = False
        self._init_reason = ""
        self._last_namespace_check = 0.0
        self.published_report = ""
        self._report_validator = None
        self._report_expectation = "the required standalone terminal token"

    @property
    def workspace(self):
        return self._workspace

    @property
    def vm(self):
        return self._vm

    @property
    def private_paths(self):
        return self._private_paths

    @property
    def execution_mode(self):
        return self._execution_mode

    def clear_published_report(self):
        self.published_report = ""

    def _run_harness_script(self, lang, code, *, timeout, cap):
        control = getattr(getattr(self._vm, "env", None),
                          "_rsiagent_verifier_control", None)
        if control is not None:
            return control.run_script(lang, code, timeout=timeout, cap=cap)
        return _run_script_with_staging_fallback(
            self._vm, lang, code, timeout=timeout, cap=cap)

    def set_report_validator(self, validator, expectation):
        """Configure lexical host-channel validation for the active Agent stage."""
        self._report_validator = validator
        self._report_expectation = str(
            expectation or "the required standalone terminal token")

    def __call__(self, lang, code, timeout=600, cap=0):
        language = (lang or "").strip().lower()
        if language == "verifier-report":
            if not isinstance(code, str) or not code.strip():
                return _trace("Verifier report is empty.", 2)
            self.published_report = code
            validator = self._report_validator
            if callable(validator) and validator(code) is None:
                return _trace(
                    "Verifier report was preserved but NOT accepted by the host "
                    "transport: it must contain " + self._report_expectation +
                    ". Correct only the report-channel syntax and publish again; "
                    "your investigation context and private scratch are unchanged.",
                    2,
                )
            return _trace("Verifier report published to the host harness.")

        if language.startswith("py"):
            interpreter = "python3"
            extension = "py"
        elif language in {"bash", "sh"}:
            interpreter = "bash"
            extension = "sh"
        else:
            return _trace(
                "Verifier Program language is unsupported. Use Python, Bash, Look, "
                "or verifier-report.",
                126,
            )

        ready = self._ensure_workspace(timeout=min(max(int(timeout), 30), 180))
        if ready is not None:
            return ready
        result = self._run_isolated(
            interpreter, extension, code or "", timeout=max(1, int(timeout)))
        return result

    def _ensure_workspace(self, timeout):
        if self._closed:
            return _trace(
                "Verifier isolation workspace is already closed.", 125,
                infra_fail=True)
        if self._initialized:
            # A target Verifier can be idle while the Actor works for hours.  A
            # cached PID is not proof that its private mount namespace still
            # exists.  Avoid an extra guest round trip for immediately adjacent
            # trusted operations, but revalidate after any real idle interval.
            if time.monotonic() - self._last_namespace_check < 5.0:
                return None
            return self._check_workspace(timeout)
        if self._init_reason:
            return _trace(self._init_reason, 125, infra_fail=True)

        if self._rollback_transaction is not None:
            try:
                self._rollback_transaction.begin()
                hidden = self._quarantine_rollback_private_paths(timeout)
                if hidden is not None:
                    self._init_reason = hidden.stdout
                    return hidden
            except Exception as exc:  # noqa: BLE001 - trusted boundary failure
                self._init_reason = (
                    "Verifier rollback-mirror preflight failed; model-authored code "
                    f"was not executed. {type(exc).__name__}: {exc}")
                return _trace(self._init_reason, 125, infra_fail=True)

        workspace = self._workspace
        if not self._sudo_password_b64:
            self._init_reason = (
                "Verifier execution-boundary preflight failed: the VM transport did "
                "not expose its harness-held privilege credential. Model-authored "
                "code was not executed.")
            return _trace(self._init_reason, 125, infra_fail=True)
        self._workspace_attempted = True
        token = secrets.token_hex(12)
        keeper_path = f"/tmp/rsiagent_verifier_keeper_{token}.sh"
        status_path = f"/tmp/rsiagent_verifier_keeper_{token}.status"
        log_path = f"/tmp/rsiagent_verifier_keeper_{token}.log"
        keeper = r'''#!/bin/bash
set -eu
umask 077
workspace_path=$1
status_path=$2
uid_value=$3
gid_value=$4
cleanup() {
  mountpoint -q "$workspace_path" && umount -l "$workspace_path" || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP
mount --make-rprivate /
mount -t tmpfs \
  -o nosuid,nodev,mode=0700,uid="$uid_value",gid="$gid_value" \
  rsiagent-verifier-scratch "$workspace_path"
test -w "$workspace_path"
printf 'READY %s\n' "$$" > "$status_path"
while :; do sleep 3600; done
'''
        keeper_b64 = base64.b64encode(keeper.encode("utf-8")).decode("ascii")
        script = f"""\
set -eu
workspace_path={workspace!r}
keeper_path={keeper_path!r}
status_path={status_path!r}
log_path={log_path!r}
keeper_payload={keeper_b64!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
clear_privilege_cache() {{ sudo -K >/dev/null 2>&1 || true; }}
trap clear_privilege_cache EXIT
command -v unshare >/dev/null
command -v nsenter >/dev/null
command -v setpriv >/dev/null
if [ -L "$workspace_path" ] || {{ [ -e "$workspace_path" ] && [ ! -d "$workspace_path" ]; }}; then
  echo 'workspace path is not a directory'
  exit 125
fi
if mountpoint -q "$workspace_path"; then
  echo 'Verifier scratch unexpectedly exists in the Actor-visible mount namespace'
  exit 125
fi
run_privileged install -d -m 0000 -o root -g root "$workspace_path"
printf %s "$keeper_payload" | base64 -d > "$keeper_path"
chmod 0700 "$keeper_path"
rm -f -- "$log_path"
run_privileged nohup unshare --mount -- bash "$keeper_path" \
  "$workspace_path" "$status_path" "$uid_value" "$gid_value" \
  >"$log_path" 2>&1 </dev/null &
for _attempt in $(seq 1 300); do
  run_privileged test -s "$status_path" && break
  sleep 0.1
done
if ! run_privileged test -s "$status_path"; then
  echo 'private mount-namespace keeper did not become ready'
  cat "$log_path" 2>/dev/null || true
  exit 125
fi
keeper_status=$(run_privileged cat "$status_path")
namespace_pid=${{keeper_status#READY }}
case "$namespace_pid" in
  ''|*[!0-9]*) echo 'private mount-namespace keeper returned an invalid PID'; exit 125 ;;
esac
run_privileged kill -0 "$namespace_pid"
run_privileged nsenter --target "$namespace_pid" --mount -- \
  mountpoint -q "$workspace_path"
run_privileged nsenter --target "$namespace_pid" --mount -- \
  test -w "$workspace_path"
if mountpoint -q "$workspace_path" || run_privileged setpriv \
    --reuid="$uid_value" --regid="$gid_value" --clear-groups \
    test -r "$workspace_path"; then
  echo 'Verifier scratch leaked into the Actor-visible mount namespace'
  run_privileged kill "$namespace_pid" 2>/dev/null || true
  exit 125
fi
printf 'RSIAGENT_VERIFIER_NAMESPACE=%s:%s:%s\n' \
  "$namespace_pid" "$uid_value" "$gid_value"
run_privileged rm -f -- "$status_path"
rm -f -- "$keeper_path" "$log_path"
"""
        result = self._run_harness_script(
            "bash", script, timeout=timeout, cap=0)
        if result.exit_code != 0 or result.infra_fail:
            self._init_reason = (
                "Verifier private-workspace preflight failed; model-authored code was "
                "not executed.\n" + (result.stdout or "(no diagnostic output)"))
            return _trace(self._init_reason, 125, infra_fail=True)
        marker = re.search(
            r"(?m)^RSIAGENT_VERIFIER_NAMESPACE=(\d+):(\d+):(\d+)$",
            result.stdout or "")
        if marker is None:
            self._init_reason = (
                "Verifier private-workspace preflight failed: the private mount "
                "namespace did not publish its identity.\n" +
                (result.stdout or "(no diagnostic output)"))
            return _trace(self._init_reason, 125, infra_fail=True)
        self._namespace_pid = int(marker.group(1))
        self._desktop_uid = int(marker.group(2))
        self._desktop_gid = int(marker.group(3))
        self._initialized = True
        self._last_namespace_check = time.monotonic()
        if self._scratch_archive is not None:
            restored = self._restore_scratch(self._scratch_archive)
            if restored is not None:
                self._initialized = False
                self._init_reason = restored.stdout
                return restored
            self._scratch_archive = None
        return None

    def _quarantine_rollback_private_paths(self, timeout):
        """Remove Actor-private files from the disposable mirror before Programs.

        A mount-only mask can be bypassed through another process's ``/proc/PID/root``
        when process observation is intentionally enabled.  Moving the paths into a
        root-only directory after the QEMU checkpoint hides the bytes from every
        guest process; rollback restores their exact names and contents afterward.
        """
        if self._rollback_private_paths_hidden or not self._private_paths:
            self._rollback_private_paths_hidden = True
            return None
        encoded = base64.b64encode(
            "\n".join(self._private_paths).encode("utf-8")).decode("ascii")
        token = secrets.token_hex(16)
        script = f"""\
set -eu
sudo_password_b64={self._sudo_password_b64!r}
hidden_payload={encoded!r}
quarantine=/root/.rsiagent_verifier_hidden_{token}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- "$@"
}}
trap 'sudo -K >/dev/null 2>&1 || true' EXIT
run_privileged install -d -m 0700 -o root -g root "$quarantine"
index=0
printf %s "$hidden_payload" | base64 -d | while IFS= read -r hidden_path \
    || [ -n "$hidden_path" ]; do
  [ -n "$hidden_path" ] || continue
  index=$((index + 1))
  if [ -e "$hidden_path" ] || [ -L "$hidden_path" ]; then
    run_privileged mv -- "$hidden_path" "$quarantine/$index"
    run_privileged chown -R root:root "$quarantine/$index"
    run_privileged chmod -R go-rwx "$quarantine/$index"
  fi
done
printf RSIAGENT_ROLLBACK_PRIVATE_PATHS_HIDDEN
"""
        trace = self._run_harness_script(
            "bash", script, timeout=max(30, int(timeout)), cap=0)
        if (trace.exit_code != 0 or trace.infra_fail
                or "RSIAGENT_ROLLBACK_PRIVATE_PATHS_HIDDEN" not in trace.stdout):
            return _trace(
                "Verifier rollback mirror could not quarantine Actor-private "
                "paths.\n" + trace.stdout, 125, infra_fail=True)
        self._rollback_private_paths_hidden = True
        return None

    def _check_workspace(self, timeout):
        """Revalidate the cached keeper PID and private scratch mount."""
        if not self._namespace_pid:
            return _trace(
                "Verifier private mount namespace has no keeper identity.", 125,
                infra_fail=True)
        marker = "RSIAGENT_VERIFIER_NAMESPACE_HEALTHY"
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
sudo_password_b64={self._sudo_password_b64!r}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
trap 'sudo -K >/dev/null 2>&1 || true' EXIT
run_privileged kill -0 {int(self._namespace_pid)}
run_privileged nsenter --target {int(self._namespace_pid)} --mount -- \
  mountpoint -q "$workspace_path"
run_privileged nsenter --target {int(self._namespace_pid)} --mount -- \
  test -w "$workspace_path"
printf '{marker}\n'
"""
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        if (result.exit_code != 0 or result.infra_fail
                or marker not in (result.stdout or "")):
            return _trace(
                "Verifier persistent isolation namespace became unavailable; "
                "model-authored code was not executed and scratch persistence "
                "cannot be assumed. This is infrastructure, not candidate "
                "evidence.\n" + (result.stdout or "(no diagnostic output)"),
                125, infra_fail=True)
        self._last_namespace_check = time.monotonic()
        return None

    def _run_in_private_mount_namespace(
            self, lang, code, *, timeout=600, as_desktop=True):
        """Run trusted harness code where the private scratch mount exists.

        This helper is never exposed as an Agent action.  Model-authored programs
        still go through :meth:`_run_isolated`, which applies the selected candidate
        protection boundary. Keeping this transport primitive separate makes it
        impossible for the ordinary Actor namespace to resolve scratch contents
        between Verifier turns.
        """
        if not self._namespace_pid:
            return _trace(
                "Verifier private mount namespace is unavailable.", 125,
                infra_fail=True)
        interpreter = "python3" if str(lang).startswith("py") else "bash"
        extension = "py" if interpreter == "python3" else "sh"
        token = secrets.token_hex(12)
        # This trusted helper must remain available for postmortem preservation
        # after the guest's ext4 root has remounted read-only. /dev/shm is a
        # separate writable tmpfs in the OSWorld guest; model-authored programs
        # still execute only inside the nested effect-isolation namespace.
        script_path = f"/dev/shm/rsiagent_verifier_host_{token}.{extension}"
        encoded = base64.b64encode((code or "").encode("utf-8")).decode("ascii")
        identity = ""
        if as_desktop:
            identity = (
                f"setpriv --reuid={int(self._desktop_uid)} "
                f"--regid={int(self._desktop_gid)} --clear-groups "
                "--bounding-set=-all --inh-caps=-all --ambient-caps=-all "
                "--no-new-privs ")
        wrapper = f"""\
set -eu
script_path={script_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
cleanup() {{ rm -f -- "$script_path"; sudo -K >/dev/null 2>&1 || true; }}
trap cleanup EXIT
printf %s "$payload" | base64 -d > "$script_path"
chmod 0600 "$script_path"
if [ "$(id -u)" = 0 ]; then chown user:user "$script_path"; fi
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
  nsenter --target {int(self._namespace_pid)} --mount -- \
  {identity}{interpreter} "$script_path"
"""
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        if not result.infra_fail and result.exit_code == 0:
            self._last_namespace_check = time.monotonic()
        return result

    def _restore_scratch(self, archive):
        """Restore a prior private workspace without trusting archive paths.

        The archive was produced by :meth:`export_scratch`, but its contents were
        ultimately influenced by Verifier-authored programs.  Restore only ordinary
        relative files/directories and reject links, devices, traversal, and absolute
        members before writing anything.
        """
        if not archive:
            return None
        push_file = getattr(self._vm, "push_file", None)
        if not callable(push_file):
            return _trace(
                "Verifier scratch restore is unavailable on this VM transport.",
                125, infra_fail=True)
        local = tempfile.NamedTemporaryFile(
            prefix="rsiagent-verifier-scratch-", suffix=".tar", delete=False)
        try:
            local.write(archive)
            local.flush()
            os.fsync(local.fileno())
            local.close()
            # Stage the transport bundle on the ordinary guest filesystem, not
            # inside the private tmpfs it is about to populate.  A lossless
            # restore necessarily contains the same bytes as the destination;
            # putting both copies on one tmpfs made large, valid Verifier
            # evidence fail with ENOSPC before extraction could begin.
            guest_archive = (
                "/tmp/rsiagent_verifier_restore_" + secrets.token_hex(12) + ".tar")
            ok, reason = push_file(local.name, guest_archive)
            if not ok:
                return _trace(
                    "Verifier scratch restore upload failed: " + str(reason),
                    125, infra_fail=True)
            code = f'''import os, pathlib, tarfile
root = pathlib.Path({self._workspace!r}).resolve()
archive = pathlib.Path({guest_archive!r})
with tarfile.open(archive, "r:") as tf:
    members = tf.getmembers()
    for member in members:
        name = member.name
        target = (root / name).resolve()
        if (name.startswith("/") or target == root.parent
                or root not in target.parents
                or not (member.isdir() or member.isfile())):
            raise RuntimeError("unsafe Verifier scratch archive member")
    tf.extractall(root, members=members, filter="data")
archive.unlink(missing_ok=True)
'''
            trace = self._run_in_private_mount_namespace(
                "python", code, timeout=600)
            if trace.exit_code != 0 or trace.infra_fail:
                return _trace(
                    "Verifier scratch restore failed:\n" + trace.stdout,
                    125, infra_fail=True)
            return None
        finally:
            if 'guest_archive' in locals():
                self._vm.run_command(
                    "rm -f -- " + shlex.quote(guest_archive),
                    timeout=120, cap=0)
            try:
                local.close()
            except Exception:
                pass
            try:
                os.unlink(local.name)
            except FileNotFoundError:
                pass

    def _export_regular_tree(self, source_path, *, archive_prefix=""):
        """Return one lossless regular-file tree as tar bytes.

        The tar is staged outside ``source_path``.  This is a transport detail,
        not a content policy: every regular file and directory below the source
        is included, with no byte/count ceiling or semantic filtering.
        """
        # This privileged transport is restricted to the private tmpfs created
        # by this executor. It must never become a general root file reader.
        if source_path != self._workspace:
            raise AgenticVerifierInfrastructureError(
                "Verifier export source escaped its private workspace")
        prefix_path = pathlib.PurePosixPath(archive_prefix)
        if prefix_path.is_absolute() or ".." in prefix_path.parts:
            raise AgenticVerifierInfrastructureError(
                "unsafe Verifier export archive prefix")
        archive_token = secrets.token_hex(12)
        failures = []
        # Prefer the ordinary guest filesystem because it may have substantially
        # more free space. If ext4 has failed read-only, retry the same read-only
        # export through the independent /dev/shm tmpfs. This is a transport
        # fallback, not a byte or file-count cap.
        for transport_root in ("/tmp", "/dev/shm"):
            archive_path = (
                transport_root + "/rsiagent_verifier_export_" +
                archive_token + ".tar")
            code = f'''import os, pathlib, stat, tarfile
root = {source_path!r}
archive = {archive_path!r}
prefix = {archive_prefix!r}
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
root_fd = os.open(root, directory_flags)
root_device = os.fstat(root_fd).st_dev

def info(name, metadata, directory=False):
    item = tarfile.TarInfo(name)
    item.mode = stat.S_IMODE(metadata.st_mode)
    item.uid, item.gid = metadata.st_uid, metadata.st_gid
    item.mtime = metadata.st_mtime
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.size = 0 if directory else metadata.st_size
    return item

def visit(tf, directory_fd, relative):
    for name in sorted(os.listdir(directory_fd)):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        is_directory = stat.S_ISDIR(before.st_mode)
        if not (is_directory or stat.S_ISREG(before.st_mode)):
            continue
        flags = directory_flags if is_directory else (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            actual = os.fstat(fd)
            if ((actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) !=
                    (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
                    or actual.st_dev != root_device):
                raise RuntimeError("Verifier scratch changed during export")
            child = "/".join(part for part in (relative, name) if part)
            archive_name = "/".join(part for part in (prefix, child) if part)
            if is_directory:
                tf.addfile(info(archive_name, actual, directory=True))
                visit(tf, fd, child)
            else:
                # Every hard-linked regular file is stored as ordinary bytes.
                # Descriptor-relative, no-follow opens keep concurrent link
                # replacement from redirecting this trusted reader elsewhere.
                with os.fdopen(os.dup(fd), "rb") as content:
                    tf.addfile(info(archive_name, actual), content)
        finally:
            os.close(fd)

try:
    archive_fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
    with os.fdopen(archive_fd, "wb") as outgoing:
        # Allow the desktop controller to remove even a partially written tar.
        os.fchown(outgoing.fileno(), {int(self._desktop_uid)}, {int(self._desktop_gid)})
        with tarfile.open(fileobj=outgoing, mode="w") as tf:
            if prefix:
                tf.addfile(info(prefix, os.fstat(root_fd), directory=True))
            visit(tf, root_fd, "")
        outgoing.flush()
        os.fsync(outgoing.fileno())
finally:
    os.close(root_fd)
'''
            # Unzip and similar tools can legitimately leave mode-000 files or
            # non-searchable directories in the Verifier's private scratch.
            # Archive their exact bytes using the confined trusted reader;
            # model-authored Programs retain their existing desktop identity.
            trace = self._run_in_private_mount_namespace(
                "python", code, timeout=3600, as_desktop=False)
            if trace.exit_code == 0 and not trace.infra_fail:
                try:
                    archive, reason = self._vm.fetch_file(
                        archive_path, max_bytes=None)
                    if archive is not None:
                        return archive
                    failures.append(
                        f"{transport_root}: fetch failed: {reason}")
                finally:
                    self._vm.run_command(
                        "rm -f -- " + shlex.quote(archive_path),
                        timeout=120, cap=0)
            else:
                failures.append(
                    f"{transport_root}: " +
                    (trace.stdout or "archive helper failed without output"))
                self._vm.run_command(
                    "rm -f -- " + shlex.quote(archive_path),
                    timeout=120, cap=0)
                # A healthy /tmp failure such as a missing source will fail the
                # same way in /dev/shm. Only transport/filesystem failures warrant
                # consuming the emergency tmpfs.
                diagnostic = (trace.stdout or "").lower()
                if not any(marker in diagnostic for marker in (
                        "read-only file system", "no space left on device",
                        "permission denied", "staging fallback")):
                    break
        raise AgenticVerifierInfrastructureError(
            "could not archive persistent Verifier data: " +
            "\n".join(failures))

    def export_scratch(self):
        """Return a lossless archive of private regular files/directories.

        No candidate path is included, no semantic selection is performed, and no
        file-size ceiling is imposed. Symlinks and special files are intentionally
        omitted so a model-influenced archive cannot escape the next private root.
        """
        if not self._initialized or self._closed:
            return self._scratch_archive
        ready = self._ensure_workspace(timeout=180)
        if ready is not None:
            raise AgenticVerifierInfrastructureError(ready.stdout)
        return self._export_regular_tree(self._workspace)

    def _fetch_private_file(self, path, max_bytes=None):
        """Bridge one regular scratch file to the trusted guest controller.

        The controller intentionally lives outside the Verifier-only mount
        namespace. A fixed harness helper copies only a resolved regular file
        below the private root to a randomized transport path, which is erased
        immediately after the lossless fetch.
        """
        requested = posixpath.normpath(str(path))
        root = posixpath.normpath(self._workspace)
        if not (requested == root or requested.startswith(root + "/")):
            return None, "private Verifier fetch path escaped its workspace"
        token = secrets.token_hex(16)
        staged = f"/tmp/rsiagent_verifier_fetch_{token}.bin"
        code = f'''import os, pathlib, shutil, stat
root = pathlib.Path({root!r}).resolve()
source = pathlib.Path({requested!r})
if source.is_symlink():
    raise RuntimeError("Verifier Look source may not be a symbolic link")
resolved = source.resolve(strict=True)
if root not in resolved.parents:
    raise RuntimeError("Verifier Look source escaped private scratch")
if not stat.S_ISREG(resolved.stat().st_mode):
    raise RuntimeError("Verifier Look source is not a regular file")
destination = pathlib.Path({staged!r})
with resolved.open("rb") as incoming, destination.open("xb") as outgoing:
    shutil.copyfileobj(incoming, outgoing, length=1 << 20)
    outgoing.flush()
    os.fsync(outgoing.fileno())
destination.chmod(0o600)
'''
        trace = self._run_in_private_mount_namespace(
            "python", code, timeout=600)
        if trace.exit_code != 0 or trace.infra_fail:
            return None, "could not bridge private Verifier pixels: " + trace.stdout
        try:
            return self._vm.fetch_file(staged, max_bytes=max_bytes)
        finally:
            self._vm.run_command(
                "rm -f -- " + shlex.quote(staged), timeout=120, cap=0)

    def _run_isolated(self, interpreter, extension, code, timeout):
        from env.qemu_rollback import ROLLBACK_MIRROR

        if self._execution_mode == ROLLBACK_MIRROR:
            return self._run_rollback_mirror(
                interpreter, extension, code, timeout)
        action_token = secrets.token_hex(12)
        ready_token = "__RSIAGENT_VERIFIER_SANDBOX_READY_" + action_token + "__"
        program_path = f"{self._workspace}/program_{action_token}.{extension}"
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")

        # All interpolated values are harness-generated tokens or base64 data. The
        # model program is decoded to a file and never parsed as wrapper source.
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
program_path={program_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
cleanup_program() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
    nsenter --target {int(self._namespace_pid)} --mount -- \
    rm -f -- "$program_path" >/dev/null 2>&1 || true
  sudo -K >/dev/null 2>&1 || true
}}
trap cleanup_program EXIT
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
  nsenter --target {int(self._namespace_pid)} --mount -- \
  unshare \
  --mount --pid --fork --kill-child=KILL --mount-proc \
  --net --ipc --uts \
  bash -ceu '
    printf %s "$8" | base64 -d > "$6"
    chown "$2:$3" "$6"
    chmod 0600 "$6"
    mount --make-rprivate /
    # A private network namespace starts with loopback down. Enable only loopback
    # so the Verifier can exercise a server it starts in this same Program. There
    # is no external interface or route, and Actor localhost lives in a
    # different namespace.
    if ! command -v ip >/dev/null; then
      echo "ip command unavailable"
      exit 125
    fi
    ip link set dev lo up
    # The unified loop treats durable memory as private Actor Agent policy,
    # not candidate evidence. Mask it only inside the private Verifier Program
    # mount namespace; the live Actor filesystem is untouched.
    printf %s "$7" | base64 -d | while IFS= read -r hidden_path; do
      [ -n "$hidden_path" ] || continue
      if [ -L "$hidden_path" ]; then
        echo "Actor-private path is a symbolic link: $hidden_path"
        exit 125
      fi
      if [ -d "$hidden_path" ]; then
        mount -t tmpfs -o mode=000,nosuid,nodev,noexec tmpfs "$hidden_path"
      elif [ -e "$hidden_path" ]; then
        mount --bind /dev/null "$hidden_path"
        mount -o remount,bind,ro "$hidden_path"
      fi
    done
    # Hide filesystem-backed IPC before freezing the remaining mount tree. The
    # OSWorld util-linux predates mount_setattr(2) recursive read-only support,
    # so every visible pre-existing mount is bind-remounted independently below.
    mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
    mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs /run
    mkdir -p "/run/user/$2"
    chown "$2:$3" "/run/user/$2"
    chmod 0700 "/run/user/$2"
    if mountpoint -q /dev/shm; then
      mount -t tmpfs -o mode=1777,nosuid,nodev,noexec tmpfs /dev/shm
    fi
    declare -A seen_mounts=()
    while IFS= read -r target; do
      if [[ ${{seen_mounts[$target]+present}} == present ]]; then
        continue
      fi
      seen_mounts[$target]=1
      case "$target" in
        "$1"|/tmp|/tmp/*|/run|/run/*|/proc|/proc/*|/dev/shm|/dev/shm/*) continue ;;
      esac
      mountpoint -q "$target" || continue
      if ! mount -o remount,bind,ro "$target" 2>/tmp/remount_error; then
        # Stacked autofs/pseudo mounts occasionally reject a bind remount. Detach
        # every layer in this private namespace; the read-only parent then becomes
        # the only reachable filesystem at that path.
        while mountpoint -q "$target"; do
          umount -l "$target" >/dev/null 2>&1 || break
        done
      fi
    done < <(findmnt -Rrn --raw -o TARGET / | tac)
    while IFS= read -r target; do
      case "$target" in
        "$1"|/tmp|/tmp/*|/run|/run/*|/proc|/proc/*|/dev/shm|/dev/shm/*) continue ;;
      esac
      resolved_options=$(findmnt -T "$target" -n -o VFS-OPTIONS 2>/dev/null || true)
      case ",$resolved_options," in
        *,rw,*) echo "writable mount remained visible: $target"; exit 125 ;;
      esac
    done < <(findmnt -Rrn --raw -o TARGET /)
    case ",$(findmnt -T /home/user -n -o OPTIONS)," in
      *,ro,*) ;;
      *) echo "candidate filesystem did not become read-only"; exit 125 ;;
    esac
    case ",$(findmnt -T "$1" -n -o OPTIONS)," in
      *,rw,*) ;;
      *) echo "Verifier scratch did not remain writable"; exit 125 ;;
    esac
    mkdir -p "$1/home"
    set -o pipefail
    {{ chown "$2:$3" /proc/self/fd/1 || exit 125
       printf "%s\\n" "$5"
       exec setpriv \
      --reuid="$2" --regid="$3" --clear-groups \
      --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
      --no-new-privs \
      env -i \
        HOME="$1/home" USER=user LOGNAME=user \
        LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        TMPDIR=/tmp XDG_RUNTIME_DIR="/run/user/$2" VERIFIER_SCRATCH="$1" \
        "$4" "$6"; }} 2>&1 | cat
  ' rsiagent-verifier \
    "$workspace_path" "$uid_value" "$gid_value" \
    {interpreter!r} {ready_token!r} "$program_path" \
    {base64.b64encode(chr(10).join(self._private_paths).encode()).decode()!r} \
    "$payload"
"""
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        visible_output = (getattr(result, "context_stdout", None)
                          or result.stdout or "")
        if ready_token not in visible_output:
            return VerifierTrace(
                stdout=(
                    "[verifier effect-isolation readiness was not confirmed; "
                    "program execution is unknown; output may be incomplete]\n" +
                    (result.stdout or "(no diagnostic output)")),
                exit_code=125,
                secs=result.secs,
                timed_out=result.timed_out,
                infra_fail=True,
            )
        self._last_namespace_check = time.monotonic()
        if getattr(result, "context_stdout", None) is not None:
            result.context_stdout = result.context_stdout.replace(
                ready_token + "\n", "", 1)
            result.context_stdout = result.context_stdout.replace(
                ready_token, "", 1)
        else:
            result.stdout = result.stdout.replace(ready_token + "\n", "", 1)
        return result

    def _run_rollback_mirror(self, interpreter, extension, code, timeout):
        """Run model code with full live-state visibility inside QEMU rollback."""
        action_token = secrets.token_hex(12)
        ready_token = "__RSIAGENT_VERIFIER_MIRROR_READY_" + action_token + "__"
        program_path = f"{self._workspace}/program_{action_token}.{extension}"
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
program_path={program_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
cleanup_program() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \\
    nsenter --target {int(self._namespace_pid)} --mount -- \\
    rm -f -- "$program_path" >/dev/null 2>&1 || true
  sudo -K >/dev/null 2>&1 || true
}}
trap cleanup_program EXIT
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \\
  nsenter --target {int(self._namespace_pid)} --mount -- \\
  bash -ceu '
    printf %s "$8" | base64 -d > "$6"
    chown "$2:$3" "$6"
    chmod 0600 "$6"
    set -o pipefail
    {{ chown "$2:$3" /proc/self/fd/1 || exit 125
       printf "%s\\n" "$5"
       exec setpriv \\
      --reuid="$2" --regid="$3" --clear-groups \\
      --bounding-set=-all --inh-caps=-all --ambient-caps=-all \\
      --no-new-privs \\
      env -i \\
        HOME=/home/user USER=user LOGNAME=user \\
        LANG=C.UTF-8 LC_ALL=C.UTF-8 \\
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
        DISPLAY=:0 XAUTHORITY=/home/user/.Xauthority \\
        XDG_RUNTIME_DIR="/run/user/$2" \\
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$2/bus" \\
        TMPDIR=/tmp VERIFIER_SCRATCH="$1" \\
        "$4" "$6"; }} 2>&1 | cat
  ' rsiagent-verifier-mirror \\
    "$workspace_path" "$uid_value" "$gid_value" \\
    {interpreter!r} {ready_token!r} "$program_path" ignored "$payload"
"""
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        visible_output = (getattr(result, "context_stdout", None)
                          or result.stdout or "")
        if ready_token not in visible_output:
            return VerifierTrace(
                stdout=(
                    "[verifier rollback-mirror readiness was not confirmed; "
                    "program execution is unknown; output may be incomplete]\n" +
                    (result.stdout or "(no diagnostic output)")),
                exit_code=125,
                secs=result.secs,
                timed_out=result.timed_out,
                infra_fail=True,
            )
        self._last_namespace_check = time.monotonic()
        if getattr(result, "context_stdout", None) is not None:
            result.context_stdout = result.context_stdout.replace(
                ready_token + "\n", "", 1).replace(ready_token, "", 1)
        else:
            result.stdout = result.stdout.replace(
                ready_token + "\n", "", 1).replace(ready_token, "", 1)
        return result

    def fetch_look_image(self, path):
        """Fetch pixels while routing any document renderer through the sandbox.

        Direct image reads and live-screen capture are trusted harness operations.
        Office/PDF rendering normally starts guest processes and writes a profile;
        a small VM facade sends that command through this executor and rewrites the
        renderer's temporary directory into the private persistent tmpfs.
        """
        from core.imagery import fetch_look_image

        ready = self._ensure_workspace(timeout=180)
        if ready is not None:
            raise AgenticVerifierInfrastructureError(ready.stdout)

        if str(path).lower().startswith("screen:"):
            # Screen capture must reach the real display, so it is a fixed trusted
            # harness action rather than model code. Candidate pixels are not
            # private Verifier state, so use a randomized transport directory in
            # the desktop namespace and erase it immediately.
            trusted_vm = self._vm
            capture_dir = (
                "/tmp/rsiagent_verifier_screen_" + secrets.token_hex(16))

            class _VerifierScreenVM:
                def run_command(self, command, timeout=30, cap=4000):
                    isolated = command.replace("/tmp/rsiagent_render", capture_dir)
                    return trusted_vm.run_command(
                        isolated, timeout=timeout, cap=cap)

                def fetch_file(self, target, max_bytes=None):
                    return trusted_vm.fetch_file(target, max_bytes=max_bytes)

            try:
                return fetch_look_image(_VerifierScreenVM(), path)
            finally:
                self._vm.run_command(
                    "rm -rf -- " + shlex.quote(capture_dir),
                    timeout=120, cap=0)

        normalized_look = posixpath.normpath(str(path))
        private_root = posixpath.normpath(self._workspace)
        look_is_private_scratch = (
            normalized_look == private_root
            or normalized_look.startswith(private_root + "/"))
        if self._private_paths and not look_is_private_scratch:
            quoted = shlex.quote(str(path))
            resolved = self._vm.run_command(
                "resolved=$(readlink -f -- " + quoted
                + ") || exit 2; printf 'RSIAGENT_RESOLVED=%s' \"$resolved\"",
                timeout=30, cap=8192) or ""
            marker = "RSIAGENT_RESOLVED="
            actual = resolved.split(marker, 1)[1].strip() \
                if marker in resolved else ""
            if not actual:
                return None, "Verifier Look path could not be resolved safely."
            if _is_private_path(actual, self._private_paths):
                return None, "That path is private to the Actor Agent."

        isolated_executor = self
        render_dir = self._workspace + "/look_render"

        class _VerifierLookVM:
            def run_command(self, command, timeout=30, cap=4000):
                isolated = command.replace("/tmp/rsiagent_render", render_dir)
                trace = isolated_executor(
                    "bash", isolated, timeout=timeout, cap=0)
                return trace.stdout

            def fetch_file(self, target, max_bytes=None):
                normalized = posixpath.normpath(str(target))
                private_root = posixpath.normpath(
                    isolated_executor._workspace)
                if (normalized == private_root
                        or normalized.startswith(private_root + "/")):
                    return isolated_executor._fetch_private_file(
                        normalized, max_bytes=max_bytes)
                return isolated_executor._vm.fetch_file(
                    normalized, max_bytes=max_bytes)

        return fetch_look_image(_VerifierLookVM(), path)

    def close(self):
        """Destroy private state and, in mirror mode, restore the scored VM."""
        if self._closed:
            return
        self._closed = True
        script = f"""\
set -u
workspace_path={self._workspace!r}
namespace_pid={str(self._namespace_pid or '')!r}
sudo_password_b64={self._sudo_password_b64!r}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
if [ -n "$namespace_pid" ]; then
  run_privileged kill -TERM "$namespace_pid" 2>/dev/null || true
  for _attempt in $(seq 1 50); do
    run_privileged kill -0 "$namespace_pid" 2>/dev/null || break
    sleep 0.1
  done
  run_privileged kill -KILL "$namespace_pid" 2>/dev/null || true
fi
run_privileged rmdir "$workspace_path" 2>/dev/null || true
sudo -K >/dev/null 2>&1 || true
"""
        try:
            if self._workspace_attempted:
                self._run_harness_script("bash", script, timeout=120, cap=0)
        except Exception:
            # The QEMU rollback below is the authoritative cleanup in mirror mode;
            # in the historical namespace mode the randomized mount disappears with
            # the VM.  Cleanup transport errors must not replace a completed report.
            pass
        finally:
            if self._rollback_transaction is not None:
                try:
                    self._rollback_transaction.rollback()
                except Exception as exc:  # noqa: BLE001
                    raise AgenticVerifierInfrastructureError(
                        "Verifier rollback failed; the candidate is not safe to "
                        f"grade. {type(exc).__name__}: {exc}") from exc
