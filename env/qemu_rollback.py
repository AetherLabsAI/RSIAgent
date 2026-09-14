"""QEMU-backed rollback transactions for full-observation verification.

The scored state is a QEMU checkpoint, all Verifier effects occur after it, and
``rollback`` restores disk, RAM, processes, sockets, and GUI state. External
services outside the QEMU guest are not covered by this transaction. Provider
launch adaptation belongs to the host harness, not this generic VM boundary.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger("rsiagent.qemu_rollback")

EFFECT_ISOLATED = "effect_isolated"
ROLLBACK_MIRROR = "rollback_mirror"
VERIFIER_EXECUTION_MODES = (EFFECT_ISOLATED, ROLLBACK_MIRROR)

class QemuRollbackError(RuntimeError):
    """The trusted QEMU checkpoint or rollback boundary failed."""


def normalize_verifier_execution_mode(value: str | None) -> str:
    mode = str(value or EFFECT_ISOLATED).strip().lower()
    if mode not in VERIFIER_EXECUTION_MODES:
        raise ValueError(
            f"unsupported verifier execution mode {value!r}; expected one of "
            f"{VERIFIER_EXECUTION_MODES}")
    return mode


_HMP_SCRIPT = r'''set -eu
command_text=$1
timeout_secs=$2
capture=$(mktemp /tmp/rsiagent_hmp_XXXXXX)
reader_pid=
cleanup() {
  if [ -n "$reader_pid" ]; then
    kill "$reader_pid" 2>/dev/null || true
    wait "$reader_pid" 2>/dev/null || true
  fi
  rm -f "$capture"
  exec 3>&- 3<&- || true
}
trap cleanup EXIT
exec 3<>/dev/tcp/127.0.0.1/7100
cat <&3 >"$capture" &
reader_pid=$!
deadline=$((SECONDS + timeout_secs))
prompt_count() { grep -ao '(qemu)' "$capture" 2>/dev/null | wc -l; }
while [ "$(prompt_count)" -lt 1 ]; do
  [ "$SECONDS" -lt "$deadline" ] || {
    echo 'RSIAGENT_HMP_INITIAL_PROMPT_TIMEOUT'
    exit 124
  }
  sleep 0.1
done
printf '%s\n' "$command_text" >&3
while [ "$(prompt_count)" -lt 2 ]; do
  [ "$SECONDS" -lt "$deadline" ] || {
    echo 'RSIAGENT_HMP_COMMAND_TIMEOUT'
    exit 124
  }
  sleep 0.1
done
kill "$reader_pid" 2>/dev/null || true
wait "$reader_pid" 2>/dev/null || true
reader_pid=
cat "$capture"
'''

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# OSWorld Docker forwards its Chromium interface to guest port 9222. Inspect
# that listener and the browser's actual debug listener without connecting to
# either: even a GET /json/version can change a task's connection audit log.
# The browser's port comes from its process arguments, not from task state.
_BROWSER_LISTENER_PROBE = r'''/usr/bin/python3 - <<'RSIAGENT_BROWSER_LISTENERS'
from pathlib import Path
import shlex

proc = Path('/proc')
listening = set()
for table in ('tcp', 'tcp6'):
    path = proc / 'net' / table
    if table == 'tcp6' and not path.exists():
        continue
    for line in path.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[3] == '0A':
            listening.add(int(fields[1].rsplit(':', 1)[1], 16))

browser_ports = set()
for path in proc.glob('[0-9]*/cmdline'):
    try:
        args = [arg for arg in path.read_bytes().decode('utf-8', 'replace').split('\0') if arg]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    # Chrome may rewrite argv into one process-title string in /proc/cmdline.
    if len(args) == 1:
        try:
            args = shlex.split(args[0])
        except ValueError:
            continue
    if not args or Path(args[0]).name not in {
        'chrome', 'chromium', 'chromium-browser', 'google-chrome',
        'google-chrome-stable',
    } or any(arg.startswith('--type=') for arg in args):
        continue
    for index, arg in enumerate(args):
        value = arg.partition('=')[2] if arg.startswith('--remote-debugging-port=') else (
            args[index + 1] if arg == '--remote-debugging-port' and index + 1 < len(args) else '')
        if value.isdecimal() and 1 <= int(value) <= 65535:
            browser_ports.add(int(value))

ready = 9222 in listening and bool(browser_ports & listening)
print('RSIAGENT_BROWSER_LISTENERS_READY' if ready else 'RSIAGENT_BROWSER_LISTENERS_ABSENT')
RSIAGENT_BROWSER_LISTENERS'''


def _clean_hmp_output(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    text = _ANSI.sub("", text)
    return "".join(
        char for char in text
        if char in "\n\r\t" or 32 <= ord(char) < 127)


@dataclass
class QemuRollbackTransaction:
    """One exact savevm/loadvm transaction around a Verifier inspection."""

    vm: object
    timeout: int = 300

    def __post_init__(self):
        self.tag = "rsiagent_verify_" + secrets.token_hex(12)
        self.active = False
        self.restored = False
        # Restore listeners that were present before the checkpoint. Never
        # contact browser endpoints: the probe itself can affect scored state.
        # The command controller can recover before the browser's listeners.
        self._required_interfaces: tuple[str, ...] = ()

    def _chromium_listeners_ready(self) -> bool:
        environment = getattr(self.vm, "env", None)
        host = str(getattr(environment, "vm_ip", "") or "").strip()
        port = getattr(environment, "chromium_port", None)
        if not host or not port:
            return False
        try:
            port = int(port)
        except (TypeError, ValueError):
            return False
        if not 1 <= port <= 65535:
            return False
        report = self.vm.run_command(_BROWSER_LISTENER_PROBE, timeout=10, cap=1000).strip()
        if report == "RSIAGENT_BROWSER_LISTENERS_READY":
            return True
        if report == "RSIAGENT_BROWSER_LISTENERS_ABSENT":
            return False
        raise QemuRollbackError(
            "passive browser listener probe did not complete: " + report[-400:])

    def _capture_required_interfaces(self) -> tuple[str, ...]:
        required = []
        if self._chromium_listeners_ready():
            required.append("chromium_listeners")
        return tuple(required)

    def _wait_for_required_interfaces(
            self, *, timeout: float = 180, probe_interval: float = 2.0,
            stable_probes: int = 2) -> tuple[bool, str]:
        if not self._required_interfaces:
            return True, "no optional external interface was healthy at checkpoint"
        deadline = time.monotonic() + max(0.0, float(timeout))
        stable = 0
        attempts = 0
        current: dict[str, bool] = {}
        last_error = ""
        while time.monotonic() < deadline:
            attempts += 1
            try:
                current = {"chromium_listeners": self._chromium_listeners_ready()}
                last_error = ""
            except QemuRollbackError as exc:
                # A command-channel flap after loadvm is not evidence that the
                # listeners disappeared. Retry within the original deadline.
                current = {"chromium_listeners": False}
                last_error = str(exc)
            healthy = all(current[name] for name in self._required_interfaces)
            stable = stable + 1 if healthy else 0
            if stable >= max(1, int(stable_probes)):
                return True, (
                    f"{', '.join(self._required_interfaces)} recovered after "
                    f"{attempts} probe(s)")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(max(0.0, probe_interval), remaining))
        detail = ", ".join(
            f"{name}={'healthy' if current.get(name) else 'unhealthy'}"
            for name in self._required_interfaces)
        if last_error:
            detail += "; " + last_error
        return False, (
            f"required checkpoint interfaces did not recover after {attempts} "
            f"probe(s): {detail}")

    def _container(self):
        environment = getattr(self.vm, "env", None)
        if getattr(environment, "provider_name", "") != "docker":
            raise QemuRollbackError(
                "rollback_mirror currently requires the OSWorld Docker provider")
        provider = getattr(environment, "provider", None)
        container = getattr(provider, "container", None)
        if container is None:
            raise QemuRollbackError("OSWorld Docker container is unavailable")
        return container

    def _hmp(self, command: str, *, timeout: int | None = None) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", command or ""):
            raise QemuRollbackError("unsafe QEMU monitor command")
        budget = max(1, int(timeout or self.timeout))
        result = self._container().exec_run(
            ["bash", "-c", _HMP_SCRIPT, "rsiagent-hmp", command, str(budget)])
        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", None)
        if exit_code is None and isinstance(result, tuple):
            exit_code, output = result
        cleaned = _clean_hmp_output(output or b"")
        if exit_code != 0:
            raise QemuRollbackError(
                f"QEMU monitor command failed ({command!r}, exit={exit_code}):\n"
                + cleaned[-4000:])
        if re.search(r"(?m)^Error:", cleaned):
            raise QemuRollbackError(
                f"QEMU rejected monitor command {command!r}:\n"
                + cleaned[-4000:])
        return cleaned

    def _snapshot_present(self) -> bool:
        report = self._hmp("info snapshots", timeout=60)
        return re.search(
            rf"(?m)^--\s+{re.escape(self.tag)}\s+", report) is not None

    def begin(self) -> None:
        if self.active:
            return
        if self.restored:
            raise QemuRollbackError("a rollback transaction cannot be reused")
        self._required_interfaces = self._capture_required_interfaces()
        self._hmp("savevm " + self.tag)
        # From this point onward rollback is mandatory.  Mark the transaction
        # active before the redundant inventory check so even a monitor/parsing
        # failure cannot make close() silently abandon a checkpoint that QEMU may
        # already have committed.
        self.active = True
        if not self._snapshot_present():
            raise QemuRollbackError(
                "QEMU reported no saved verification checkpoint after savevm")
        log.info(
            "saved verifier rollback checkpoint %s (required interfaces: %s)",
            self.tag, ", ".join(self._required_interfaces) or "controller only")

    def rollback(self) -> None:
        if self.restored:
            return
        if not self.active:
            self.restored = True
            return
        self._hmp("loadvm " + self.tag)
        recovered, detail = self.vm.wait_for_controller(
            timeout=180, probe_interval=2.0, stable_probes=2)
        if not recovered:
            raise QemuRollbackError(
                "QEMU loaded the verification checkpoint but the guest controller "
                "did not recover: " + detail)
        interfaces_ready, interface_detail = self._wait_for_required_interfaces()
        if not interfaces_ready:
            raise QemuRollbackError(
                "QEMU loaded the verification checkpoint and the guest controller "
                "recovered, but an interface that was healthy at checkpoint did "
                "not recover: " + interface_detail)
        self._hmp("delvm " + self.tag, timeout=120)
        if self._snapshot_present():
            raise QemuRollbackError(
                "verification checkpoint remained after rollback cleanup")
        self.active = False
        self.restored = True
        log.info(
            "restored verifier rollback checkpoint %s (%s)",
            self.tag, interface_detail)
