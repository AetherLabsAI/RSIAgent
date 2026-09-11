"""Host-only Verifier setup transport for guests whose desktop sudo is disabled.

The QEMU control socket is inside the provider container and bound to loopback;
the guest end is a root-only virtio port. No Actor API, sudo rule, task definition,
or grader is changed. Only trusted isolation wrappers use this transport. The
wrappers still drop to the desktop UID with no capabilities before model code.
"""
from __future__ import annotations

import base64
import io
import json
import secrets
import tarfile
import threading
import time

from env.vm import Trace, _decode_run_output_envelopes

QEMU_ARGUMENTS = (
    "-device virtio-serial-pci,id=forge_control "
    "-chardev socket,id=forge_control,host=127.0.0.1,port=7101,server=on,wait=off "
    "-device virtserialport,bus=forge_control.0,chardev=forge_control,"
    "name=org.forge.verifier.control"
)
DEVICE = "/dev/virtio-ports/org.forge.verifier.control"

# This loopback belongs to the provider container, not the guest. Port 7101 is
# never published, nor bound to the guest's container-gateway interface.
_CLIENT = r'''set -eu
request=$1
budget=$2
nonce=$3
trap 'rm -f -- "$request"' EXIT
exec 3<>/dev/tcp/127.0.0.1/7101
cat "$request" >&3
deadline=$((SECONDS + budget))
while IFS= read -r -t "$((deadline - SECONDS))" response <&3; do
  case "$response" in
    *\"nonce\":\""$nonce"\"*) printf '%s\n' "$response"; exit 0 ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || exit 124
done
exit 124
'''

_DAEMON = r'''import base64, json, os, pathlib, signal, subprocess, tempfile, time
device = "/dev/virtio-ports/org.forge.verifier.control"
os.chown(device, 0, 0)
os.chmod(device, 0o600)
os.umask(0o077)
with open(device, "r+b", buffering=0) as wire:
    pending = b""
    while True:
        try:
            chunk = os.read(wire.fileno(), 65536)
        except OSError:
            time.sleep(.1)
            continue
        if not chunk:
            time.sleep(.1)
            continue
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            req = {}
            try:
                req = json.loads(line)
                nonce = req["nonce"]
                if req.get("op") == "ping":
                    result = {"uid": os.geteuid(), "pid": os.getpid()}
                else:
                    assert req["op"] == "run"
                    code = base64.b64decode(req["code_b64"], validate=True)
                    budget = int(req["timeout"])
                    assert budget > 0
                    with tempfile.TemporaryDirectory(dir="/run/forge-verifier-control") as td:
                        script = pathlib.Path(td) / "wrapper.sh"
                        script.write_bytes(code)
                        script.chmod(0o600)
                        with open(pathlib.Path(td) / "output", "w+b") as output:
                            child = subprocess.Popen(["bash", str(script)], stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                env={"PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                     "HOME":"/root", "LANG":"C.UTF-8"})
                            timed_out = False
                            try:
                                child.wait(timeout=budget)
                            except subprocess.TimeoutExpired:
                                timed_out = True
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                            output.seek(0)
                            result = {"exit_code":124 if timed_out else child.returncode,
                                      "timed_out":timed_out,
                                      "output_b64":base64.b64encode(output.read()).decode("ascii")}
                answer = {"nonce":nonce, "result":result}
            except Exception as exc:
                answer = {"nonce":req.get("nonce", "") if isinstance(req, dict) else "",
                          "error":type(exc).__name__ + ": " + str(exc)}
            data = (json.dumps(answer, separators=(",", ":")) + "\n").encode("ascii")
            try:
                while data:
                    data = data[os.write(wire.fileno(), data):]
            except OSError:
                pass
'''

# This function is defined only inside a root-owned, host-supplied wrapper. It
# implements the existing wrapper's sudo spelling without consulting desktop
# sudoers. It is not exported; model programs start under setpriv and env -i.
_ROOT_WRAPPER = r'''set -eu
[ "$(id -u)" = 0 ] || exit 125
sudo() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -S|-k) shift ;;
      -p) shift 2 ;;
      -K) return 0 ;;
      --) shift; break ;;
      *) return 125 ;;
    esac
  done
  [ "$#" -gt 0 ] || return 125
  command "$@" </dev/null
}
'''


class VerifierControl:
    def __init__(self, desktop):
        self.desktop = desktop
        self._lock = threading.Lock()

    def _request(self, payload, timeout):
        container = self.desktop.provider.container
        nonce = secrets.token_hex(16)
        payload = {**payload, "nonce": nonce}
        data = (json.dumps(payload) + "\n").encode("ascii")
        name = "forge-control-request-" + nonce
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        with self._lock:
            container.client.api.timeout = max(
                container.client.api.timeout, int(timeout) + 60)
            container.put_archive("/tmp", stream.getvalue())
            result = container.exec_run(
                ["bash", "-c", _CLIENT, "forge-verifier-control", "/tmp/" + name,
                 str(int(timeout) + 15), nonce])
        if result.exit_code != 0:
            raise RuntimeError(f"Verifier control transport failed: exit={result.exit_code}")
        # Do not accept an old response buffered across a failed connection or
        # QEMU rollback as the result of a different trusted operation.
        answers = [json.loads(line) for line in result.output.splitlines() if line.strip()]
        matching = [a for a in answers if a.get("nonce") == nonce]
        if len(matching) != 1 or "result" not in matching[0]:
            raise RuntimeError("Verifier control response missing, stale, or failed")
        return matching[0]["result"]

    def ping(self):
        result = self._request({"op": "ping"}, 15)
        if result.get("uid") != 0:
            raise RuntimeError("Verifier control endpoint is not privileged")
        return result

    def run_script(self, lang, code, *, timeout=600, cap=0):
        if lang != "bash":
            raise ValueError("Verifier control accepts trusted Bash wrappers only")
        started = time.monotonic()
        try:
            result = self._request({"op": "run", "timeout": int(timeout),
                "code_b64": base64.b64encode((_ROOT_WRAPPER + code).encode()).decode()}, timeout)
            output, context = _decode_run_output_envelopes(
                "FORGE_RUN_OUTPUT_BASE64:" + result["output_b64"] + "\n"
                + f"[exit {result['exit_code']}]")
            return Trace(output, result["exit_code"], time.monotonic()-started,
                         result["timed_out"], False, context)
        except Exception as exc:
            return Trace(f"[trusted Verifier control failure: {type(exc).__name__}: {exc}]",
                         125, time.monotonic()-started, infra_fail=True)


def provision_verifier_control(desktop):
    """Install before task setup, while the pristine guest permits host sudo."""
    from env.vm import VM

    payload = base64.b64encode(_DAEMON.encode()).decode()
    password = base64.b64encode(desktop.client_password.encode()).decode()
    script = f'''set -eu
printf %s {password!r} | base64 -d | sudo -S -k -p '' -- bash -ceu '
  test -c {DEVICE}
  install -d -m 0700 -o root -g root /run/forge-verifier-control
  printf %s "$1" | base64 -d > /run/forge-verifier-control/server.py
  chmod 0400 /run/forge-verifier-control/server.py
  chmod 0600 {DEVICE}
  nohup env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /usr/bin/python3 \
    /run/forge-verifier-control/server.py \
    >/run/forge-verifier-control/server.log 2>&1 </dev/null &
' forge-control-bootstrap {payload!r}
sudo -K
'''
    trace = VM(desktop).run_script("bash", script, timeout=60, cap=0)
    if trace.exit_code != 0 or trace.infra_fail:
        raise RuntimeError("Verifier control bootstrap failed: " + trace.stdout)
    control = VerifierControl(desktop)
    control.ping()
    desktop._forge_verifier_control = control
    return control
