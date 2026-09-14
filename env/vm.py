"""VM — the machine rsiagent's agent acts on. Duck-typed over a v2 ``DesktopEnv`` (never
imports it): only ``env.controller.http_server`` is used. Two verbs, both code:

- ``run_command``: one shell command (probes, checks).
- ``run_script``: a WHOLE program (python3/bash), base64-piped in — the code-as-policy
  primitive. The program's combined output is written to an in-VM log first and then
  cat'd back, so a timeout still leaves a recoverable partial trace (anchor's adapter
  lost the entire trace on timeout — the trace is our core signal, so that's fixed here).

No GUI, no grounder, no screenshots — rsiagent is a pure text+code agent.
"""
import base64
import hashlib
import ipaddress
import logging
import os
import posixpath
import re
import secrets
import shlex
import socket
import tempfile
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("rsiagent.vm")

_RUN_LOG_PREFIX = "/tmp/rsiagent_run_"      # primary program trace location
_SHM_RUN_LOG_PREFIX = "/dev/shm/rsiagent_run_"  # survives a read-only guest root
_RUN_OUTPUT_B64_PREFIX = "RSIAGENT_RUN_OUTPUT_BASE64:"
_STAGING_FALLBACK = "[RSIAGENT STAGING FALLBACK: /tmp unavailable; using /dev/shm]"
_STAGING_UNAVAILABLE = "[RSIAGENT STAGING UNAVAILABLE: /tmp and /dev/shm both failed]"
# Linux counts the shell command, environment, and argv against one execve limit.
# Keep ordinary short actions on the low-latency inline path, but stream larger
# programs losslessly rather than imposing a content ceiling or relying on the
# guest's environment-dependent ARG_MAX headroom.
_INLINE_PROGRAM_B64_LIMIT = 32 * 1024


def _atomic_guest_part_path(destination: str, token: str) -> str:
    """Return a hidden staging path on the destination's own filesystem."""
    normalized = str(destination or "").rstrip("/")
    basename = posixpath.basename(normalized)
    if not basename:
        return ""
    directory = posixpath.dirname(normalized) or "."
    return posixpath.join(directory, f".{basename}.rsiagent-part-{token}")


def _decode_run_output_envelopes(output: str) -> tuple[str, str | None]:
    """Decode raw Program bytes carried safely through OSWorld's JSON endpoint.

    The guest execute server decodes subprocess output as strict UTF-8 before it
    constructs its response. A perfectly valid read-only probe such as ``cat`` on
    an executable can therefore become HTTP 500 and masquerade as a dead command
    channel. Program logs cross that boundary as ASCII base64. Valid UTF-8 remains
    byte-for-byte text and fully enters Agent context. Non-text bytes remain exactly
    reproducible as base64 in the archived trace, while Agent context receives a
    content-addressed notice instead of an arbitrarily large binary payload.

    Returns ``(archived_output, context_output)``. ``context_output`` is ``None``
    when the archived bytes are ordinary UTF-8 and need no externalization.
    """
    if _RUN_OUTPUT_B64_PREFIX not in output:
        # Compatibility with already-running workers and test doubles that return
        # the historical plain-text envelope.
        return output, None
    pattern = re.compile(
        re.escape(_RUN_OUTPUT_B64_PREFIX) + r"([A-Za-z0-9+/=]*)\n")
    archived_parts = []
    context_parts = []
    cursor = 0
    externalized = False
    for match in pattern.finditer(output):
        prefix = output[cursor:match.start()]
        archived_parts.append(prefix)
        context_parts.append(prefix)
        try:
            raw = base64.b64decode(match.group(1), validate=True)
        except Exception as exc:  # noqa: BLE001 - malformed trusted envelope
            error = ("[channel error: malformed Program output envelope — "
                     f"{type(exc).__name__}]")
            archived_parts.append(error)
            context_parts.append(error)
            cursor = match.end()
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            externalized = True
            encoded = base64.b64encode(raw).decode("ascii")
            digest = hashlib.sha256(raw).hexdigest()
            archived_parts.append(
                "[RSIAGENT NON-UTF8 PROGRAM OUTPUT — EXACT BASE64; "
                f"bytes={len(raw)} sha256={digest}]\n{encoded}\n"
                "[END RSIAGENT NON-UTF8 PROGRAM OUTPUT]")
            context_notice = (
                "[PROGRAM OUTPUT EXTERNALIZED: non-UTF8/binary data; "
                f"bytes={len(raw)} sha256={digest}. The exact bytes are preserved "
                "as base64 in this turn's archived trace.txt. Do not infer binary "
                "contents from this notice; re-inspect deliberately with file, "
                "readelf, strings, or xxd as appropriate.]" )
            # Effect-isolated execution emits an unpredictable host-authored token
            # before model code begins. It is control-plane evidence, not binary
            # payload, and must remain visible to the trusted sandbox validator.
            control_tokens = re.findall(
                rb"__RSIAGENT_[A-Za-z0-9_]+__", raw)
            if control_tokens:
                context_notice += "\n" + "\n".join(
                    token.decode("ascii") for token in control_tokens)
            context_parts.append(context_notice)
        else:
            archived_parts.append(decoded)
            context_parts.append(decoded)
        cursor = match.end()
    if not archived_parts:
        error = ("[channel error: incomplete Program output envelope — the "
                 "machine response cannot be trusted]")
        return error, error
    suffix = output[cursor:]
    archived_parts.append(suffix)
    context_parts.append(suffix)
    archived = "".join(archived_parts)
    context = "".join(context_parts) if externalized else None
    return archived, context


@dataclass
class Trace:
    """What one program execution produced."""
    stdout: str                          # complete combined stdout+stderr, incl. "[exit N]" line
    exit_code: int = None                # parsed from the trailing [exit N]; None if unknown
    secs: float = 0.0
    timed_out: bool = False
    infra_fail: bool = False             # v17: the RUN WRAPPER itself failed (e.g. guest
    #                                      /tmp went read-only -> mktemp can't stage the
    #                                      program). No program actually ran; a streak of
    #                                      these = a dead VM, not a task failure.
    context_stdout: str | None = None    # only set when non-text bytes are kept in the
    #                                      archive but intentionally externalized from
    #                                      the next model request.


class VM:
    def __init__(self, de):
        self.env = de         # duck-typed handle; only .controller.http_server is used
        self._conda = ""      # persistent shell state: the controller runs each command in a
        #                       FRESH shell, so a `conda activate` would not survive to the next
        #                       command. We re-apply the last one (proven necessary in anchor).
        self._staging_fallback_reported = False

    # ------------------------------------------------------------------ commands
    def run_command(self, command: str, timeout: int = 30, cap: int = 4000) -> str:
        """One shell command in the VM; returns combined output (bounded to ``cap``)."""
        cmd = command
        if self._conda and "conda activate" not in command:
            cmd = (f"source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                   f"conda activate {self._conda} 2>/dev/null; {command}")
        try:
            # The OSWorld guest endpoint has its own 120-second default.  Merely
            # setting requests' timeout does not change that server-side limit:
            # the endpoint kills only its immediate ``shell=True`` wrapper when
            # the default expires, allowing grandchildren such as ffmpeg to keep
            # mutating the VM after RSIAgent has advanced to the next turn.  Forward
            # the action timeout explicitly.  The run_script wrapper's GNU
            # ``timeout`` still fires 30 seconds earlier, kills its process group,
            # and returns the partial trace before this transport ceiling.
            r = requests.post(f"{self.env.controller.http_server}/execute",
                              json={"command": cmd, "shell": True,
                                    "timeout": max(1, int(timeout))},
                              timeout=timeout)
            d = r.json()
            if r.status_code >= 400 or d.get("status") == "error":
                detail = str(d.get("message") or d.get("error")
                             or f"HTTP {r.status_code}")
                log.warning("guest execute failed: %s", detail)
                return (f"[channel error: guest execute HTTP {r.status_code} — "
                        "the machine did not complete the command; "
                        f"detail: {detail[:240]}]")
            out = (d.get("output") or "").rstrip()
            if d.get("error"):
                out = (out + "\n[stderr] " + d["error"]).strip()
        except requests.exceptions.Timeout:
            return f"[command timed out after {timeout}s]"
        except Exception as e:  # noqa: BLE001
            # v32.1: NEVER return "" on a transport failure — silent empties made a
            # whole inspection go blind (t063 iter_66: every probe incl. `echo`
            # "returned nothing" during a transient guest-server outage and the
            # inspector concluded from the absence). An explicit marker converts
            # silent blindness into a diagnosed outage the caller can reason about.
            log.warning("run_command failed: %s", e)
            return (f"[channel error: {type(e).__name__} — the machine did not "
                    "answer; this is NOT command output and proves nothing]")
        # The guest API reports shell signal termination as HTTP 200/success.
        # Its negative returncode is transport evidence, not model stdout. A
        # killed outer shell can lose the output envelope after code has run.
        returncode = d.get("returncode")
        if isinstance(returncode, int) and returncode < 0:
            marker = (f"[channel error: guest execute terminated by signal {-returncode}; "
                      "output may be incomplete; program execution may have occurred]")
            log.warning("guest execute terminated by signal %s", -returncode)
            out = marker + ("\n" + out if out else "")
        m = re.search(r"conda activate (\S+)", command)   # remember for subsequent commands
        if m:
            self._conda = m.group(1)
        return _bound(out, cap)

    def wait_for_controller(self, timeout: float = 180,
                            probe_interval: float = 5.0,
                            stable_probes: int = 2):
        """Wait for the guest command channel to become stably responsive.

        This is recovery after a failed Actor action, not an action retry: only a
        non-mutating ``printf`` readiness probe is sent. Requiring consecutive
        healthy replies avoids returning control during a restart flap. Returns
        ``(recovered, report)`` for archival and for the Actor's next observation.
        """
        timeout = max(0.0, float(timeout))
        probe_interval = max(0.0, float(probe_interval))
        stable_needed = max(1, int(stable_probes))
        started = time.monotonic()
        deadline = started + timeout
        attempts = 0
        stable = 0
        last = "no readiness probe was sent"

        while timeout > 0 and time.monotonic() < deadline:
            attempts += 1
            remaining = max(1.0, deadline - time.monotonic())
            probe_timeout = max(1, min(10, int(remaining)))
            last = self.run_command(
                "printf RSIAGENT_CONTROLLER_READY", timeout=probe_timeout, cap=1000)
            if (last or "").strip() == "RSIAGENT_CONTROLLER_READY":
                stable += 1
                if stable >= stable_needed:
                    elapsed = time.monotonic() - started
                    return True, (
                        f"controller recovered after {elapsed:.1f}s and {attempts} "
                        f"readiness probe(s); {stable_needed} consecutive probes passed")
            else:
                stable = 0
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(probe_interval, remaining))

        elapsed = time.monotonic() - started
        compact = " ".join(str(last).split())[-240:]
        return False, (
            f"controller did not recover within {elapsed:.1f}s after {attempts} "
            f"readiness probe(s); last probe: {compact}")

    # ------------------------------------------------------------------ programs
    def run_script(self, lang: str, code: str, timeout: int = 600, cap: int = 0,
                   allow_staging_fallback: bool = False) -> Trace:
        """Run a whole program in the VM.

        Short programs use the historical base64-in-command transport. Large ones
        are streamed with exact size/SHA-256 verification to avoid ``execve``'s
        combined argv/environment limit; this is a transport choice, never a size
        cap. The program is killed in-VM 30s before the HTTP timeout (partial output
        survives in the log); output comes back with an ``[exit N]`` line (124 =
        in-VM timeout). ``cap=0`` is deliberately lossless: an Agent's observations
        must not be silently shortened before they enter its context. Each action
        owns a unique log, and ``timeout`` escalates to SIGKILL so an expired action
        cannot keep writing into later actions.
        """
        if not code:
            return Trace(stdout="[empty program]", exit_code=None)
        b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        run_token = secrets.token_hex(12)
        run_log = f"{_RUN_LOG_PREFIX}{run_token}.log"
        shm_run_log = f"{_SHM_RUN_LOG_PREFIX}{run_token}.log"
        kill = max(30, timeout - 30)               # in-VM timeout fires first -> log survives
        timeout_cmd = f"timeout --signal=TERM --kill-after=5s {kill}s"
        python = (lang or "").startswith("py")
        extension = "py" if python else "sh"
        interpreter = "python3 -u" if python else "bash"
        streamed_script = ""
        if len(b64) > _INLINE_PROGRAM_B64_LIMIT:
            streamed_script = (
                f"/dev/shm/rsiagent_program_{run_token}.{extension}")
            local = tempfile.NamedTemporaryFile(
                prefix="rsiagent-program-", suffix=f".{extension}", delete=False)
            try:
                local.write(code.encode("utf-8"))
                local.flush()
                os.fsync(local.fileno())
                local.close()
                ok, reason = self.push_file(
                    local.name, streamed_script,
                    timeout=max(30, int(timeout)))
            finally:
                try:
                    local.close()
                except Exception:  # noqa: BLE001 - best-effort host cleanup
                    pass
                try:
                    os.unlink(local.name)
                except FileNotFoundError:
                    pass
            if not ok:
                return Trace(
                    stdout=("[channel error: lossless Program staging failed; "
                            "the model-authored program was not executed; "
                            f"detail: {str(reason)[-240:]}]"),
                    exit_code=None, infra_fail=True)
        # Never stream arbitrary Program bytes directly through OSWorld's strict
        # UTF-8 /execute response. The host decodes this uncapped ASCII envelope
        # immediately below, before parsing the transport trailer.
        emit_log = (
            f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
            'base64 -w0 "$run_log"; printf "\\n"')
        if streamed_script:
            # The verified stream already lives on the guest's independent tmpfs,
            # so the execution request contains only a small fixed-size wrapper.
            # This also remains usable for Verifier postmortems if ext4 is read-only.
            cmd = (
                f'f={shlex.quote(streamed_script)}; run_log={shm_run_log}; '
                f'{timeout_cmd} {interpreter} "$f" > "$run_log" 2>&1; e=$?; '
                f'{emit_log}; echo "[exit $e]"; rm -f "$f" "$run_log"')
        elif allow_staging_fallback:
            # The OSWorld guest occasionally remounts its ext4 root read-only after
            # an I/O/filesystem fault. /tmp then cannot stage even a diagnostic
            # Verifier Program, although /dev/shm remains a separate writable tmpfs.
            # Select the location before decoding or executing model code, so the
            # fallback never replays an action. Actor execution deliberately retains
            # the normal fail-fast path: it must not pretend it can repair a candidate
            # whose filesystem is read-only.
            cmd = (
                f'tmp_candidate=$(mktemp /tmp/rsiagent_XXXXXX.{extension} 2>&1); '
                'tmp_status=$?; '
                f'if [ "$tmp_status" -eq 0 ]; then f="$tmp_candidate"; run_log={run_log}; '
                'else tmp_error="$tmp_candidate"; '
                f'shm_candidate=$(mktemp /dev/shm/rsiagent_XXXXXX.{extension} 2>&1); '
                'shm_status=$?; '
                'if [ "$shm_status" -ne 0 ]; then '
                f'printf "%s\\n/tmp: %s\\n/dev/shm: %s\\n" '
                f'"{_STAGING_UNAVAILABLE}" "$tmp_error" "$shm_candidate" >&2; exit 125; '
                f'fi; f="$shm_candidate"; run_log={shm_run_log}; '
                f'printf "%s\\n%s\\n" "{_STAGING_FALLBACK}" "$tmp_error"; fi; '
                f'printf %s {b64} | base64 -d > "$f"; '
                f'{timeout_cmd} {interpreter} "$f" > "$run_log" 2>&1; e=$?; '
                f'{emit_log}; echo "[exit $e]"; rm -f "$f" "$run_log"')
        else:
            cmd = (f'f=$(mktemp /tmp/rsiagent_XXXXXX.{extension}); '
                   f'printf %s {b64} | base64 -d > "$f"; '
                   f'{timeout_cmd} {interpreter} "$f" > {run_log} 2>&1; e=$?; '
                   f'run_log={run_log}; {emit_log}; echo "[exit $e]"; '
                   f'rm -f "$f" {run_log}')
        t0 = time.time()
        out = self.run_command(cmd, timeout=timeout, cap=cap)
        secs = time.time() - t0
        if out.startswith("[command timed out"):   # HTTP timeout: recover the partial trace
            if streamed_script:
                partial = self.run_command(
                    f'if test -f {shlex.quote(shm_run_log)}; then '
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
                    f'base64 -w0 {shlex.quote(shm_run_log)}; printf "\\n"; fi',
                    timeout=30, cap=cap)
                self.run_command(
                    f"rm -f {shlex.quote(shm_run_log)} "
                    f"{shlex.quote(streamed_script)}", timeout=30, cap=1000)
            elif allow_staging_fallback:
                candidates = f"{run_log} {shm_run_log}"
                partial = self.run_command(
                    f'for p in {candidates}; do if test -f "$p"; then '
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; base64 -w0 "$p"; '
                    f'printf "\\n"; fi; done',
                    timeout=30, cap=cap)
                self.run_command(f"rm -f {candidates}", timeout=30, cap=1000)
            else:
                partial = self.run_command(
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
                    f'base64 -w0 {run_log}; printf "\\n"',
                    timeout=30, cap=cap)
                self.run_command(f"rm -f {run_log}", timeout=30, cap=1000)
            partial, partial_context = _decode_run_output_envelopes(partial)
            prefix = "[program timed out — partial output:]\n"
            return Trace(
                stdout=prefix + partial, exit_code=None, secs=secs,
                timed_out=True,
                context_stdout=(prefix + partial_context
                                if partial_context is not None else None))
        # Infrastructure sentinels are emitted outside the base64 Program-output
        # envelope. The guest API appends wrapper stderr after stdout, so inspect
        # both the prefix and suffix. In a full-observation Verifier the
        # model can legitimately inspect the wrapper process itself, whose command
        # line contains those same literal sentinel strings. Never classify bytes
        # decoded from model Program output as transport state.
        transport_preamble = re.sub(
            re.escape(_RUN_OUTPUT_B64_PREFIX) + r"[A-Za-z0-9+/=]*\n", "", out)
        out, context_out = _decode_run_output_envelopes(out)
        fallback_used = _STAGING_FALLBACK in transport_preamble
        if (fallback_used or "Read-only file system" in transport_preamble) \
                and not self._staging_fallback_reported:
            # run_command does not stage through /tmp, so it remains usable for a
            # read-only postmortem. Capture this once before the VM is torn down.
            diagnostic = self.run_command(
                "echo 'mount state:'; "
                "findmnt -T / -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /tmp -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /home/user -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /dev/shm -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "echo 'space:'; df -h / /tmp /home/user /dev/shm 2>&1; "
                "echo 'kernel warnings:'; dmesg --level=err,warn 2>&1 | tail -40",
                timeout=30, cap=8000)
            self._staging_fallback_reported = True
            log.warning("guest filesystem staging fault; fallback used=%s", fallback_used)
            marker = re.search(r"\[exit \d+\]\s*$", out)
            report = "\n[HARNESS FILESYSTEM DIAGNOSTIC]\n" + diagnostic + "\n"
            if marker:
                out = out[:marker.start()] + report + out[marker.start():]
            else:
                out += report
            if context_out is not None:
                context_marker = re.search(r"\[exit \d+\]\s*$", context_out)
                if context_marker:
                    context_out = (context_out[:context_marker.start()] + report
                                   + context_out[context_marker.start():])
                else:
                    context_out += report
        exit_code = None
        m = re.search(r"\[exit (\d+)\]\s*$", out)
        if m:
            exit_code = int(m.group(1))
        # v17: the wrapper stages the program via `mktemp /tmp/...`; if the guest fs
        # went read-only (disk-full / I/O fault on the shared box) mktemp fails and NO
        # program ran. This signature = broken machine, not a bad program.
        # P2-era addition, same class: a [channel error] marker means the guest never
        # answered at all (container/server death — ep003 burned 30 iters against a
        # dead channel because only the RO signature counted as infra).
        infra = ((_STAGING_UNAVAILABLE in transport_preamble)
                 or (not fallback_used
                     and "Read-only file system" in transport_preamble
                     and ("mktemp" in transport_preamble
                          or run_log in transport_preamble))
                 or transport_preamble.startswith("[channel error:"))
        return Trace(stdout=out, exit_code=exit_code, secs=secs,
                     timed_out=(exit_code == 124), infra_fail=infra,
                     context_stdout=context_out)


    # ------------------------------------------------------------------ files
    def fetch_file(self, path: str, max_bytes: int | None = None):
        """Pull a file's raw bytes out of the VM.

        Transport is lossless by default. ``max_bytes`` remains available only
        as an explicit caller policy; a project snapshot, memory bank, image, or
        agent-authored handoff must not be rejected by an implicit harness ceiling.
        Returns (bytes, "") on success or (None, reason) on failure.
        """
        try:
            data = self.env.controller.get_file(path)
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_file(%s) failed: %s", path, e)
            return None, f"error reading the file ({type(e).__name__})"
        if not data:
            return None, "file not found or empty"
        if max_bytes is not None and len(data) > max_bytes:
            return None, (f"file is {len(data)} bytes (limit {max_bytes}) — "
                          "downscale or crop it with a program first")
        return data, ""

    def push_file(self, local_path: str, guest_path: str,
                  attempts: int = 3, timeout: int = 3600):
        """Stream one host artifact into the guest without a size ceiling.

        On the Docker provider, a one-shot harness-owned receiver writes the
        raw TCP stream directly to a temporary guest file and atomically
        publishes it only after exact size and SHA-256 verification. This
        avoids both shell-argument base64 expansion and OSWorld's WSGI adapter,
        which buffers request bodies in memory. Other providers retain the
        normal OSWorld task-setup upload endpoint as a compatibility path.
        Total artifact size is never a policy limit.

        Returns ``(True, "")`` on success or ``(False, reason)`` after all
        attempts fail.  The source is reopened for every attempt so a retry
        always starts from byte zero.
        """
        if (not isinstance(local_path, str) or not local_path
                or os.path.islink(local_path) or not os.path.isfile(local_path)):
            return False, "host artifact is not an ordinary file"
        if not isinstance(guest_path, str) or not guest_path:
            return False, "guest artifact path is empty"

        docker_ip = self._docker_container_ip()
        if docker_ip:
            last_reason = "direct stream did not run"
            for attempt in range(max(1, attempts)):
                ok, last_reason = self._push_file_direct_docker(
                    local_path, guest_path, docker_ip, timeout)
                if ok:
                    return True, ""
                log.warning(
                    "push_file direct stream (%s -> %s) attempt %d/%d "
                    "failed: %s", local_path, guest_path, attempt + 1,
                    max(1, attempts), last_reason)
                if attempt + 1 < max(1, attempts):
                    time.sleep(min(4, 2 ** attempt))
            return False, last_reason

        return self._push_file_multipart(
            local_path, guest_path, attempts=attempts, timeout=timeout)

    def _docker_container_ip(self) -> str:
        """Return this environment's outer Docker IP, or empty if inapplicable."""

        if getattr(self.env, "provider_name", "") != "docker":
            return ""
        provider = getattr(self.env, "provider", None)
        container = getattr(provider, "container", None)
        if container is None:
            return ""
        try:
            reload_container = getattr(container, "reload", None)
            if callable(reload_container):
                reload_container()
            network = container.attrs.get("NetworkSettings", {})
            candidates = [network.get("IPAddress", "")]
            candidates.extend(
                value.get("IPAddress", "")
                for value in network.get("Networks", {}).values()
                if isinstance(value, dict))
            for candidate in candidates:
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if address.version == 4 and address.is_private:
                    return str(address)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not resolve Docker artifact route: %s", exc)
        return ""

    def _push_file_direct_docker(self, local_path: str, guest_path: str,
                                 docker_ip: str, timeout: int):
        """Push one verified stream through the Docker outer/guest NAT."""

        source_size = os.path.getsize(local_path)
        source_digest = hashlib.sha256()
        with open(local_path, "rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                source_digest.update(block)
        source_sha256 = source_digest.hexdigest()
        token = secrets.token_hex(12)
        prefix = f"/tmp/rsiagent_push_{token}"
        # ``os.replace`` is atomic only within one filesystem. Program payloads
        # intentionally target /dev/shm when the root filesystem is impaired, so
        # staging their part file in /tmp would fail with EXDEV/OSError. Keep the
        # private part beside its destination; status/control files remain in /tmp.
        part_path = _atomic_guest_part_path(guest_path, token)
        if not part_path:
            return False, "guest artifact path does not name a file"
        status_path = prefix + ".status"
        port_path = prefix + ".port"
        log_path = prefix + ".log"
        receiver = r'''import hashlib
import os
import socket
import sys

destination, part, status, port_file, size_text, expected, timeout_text = sys.argv[1:]
size = int(size_text)
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.settimeout(int(timeout_text))
listener.bind(("0.0.0.0", 0))
listener.listen(1)
with open(port_file, "w", encoding="ascii") as handle:
    handle.write(str(listener.getsockname()[1]))
    handle.flush()
    os.fsync(handle.fileno())
try:
    connection, _peer = listener.accept()
    connection.settimeout(int(timeout_text))
    digest = hashlib.sha256()
    received = 0
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    with connection, open(part, "wb") as output:
        while received < size:
            chunk = connection.recv(min(1 << 20, size - received))
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
            received += len(chunk)
        extra = connection.recv(1)
        output.flush()
        os.fsync(output.fileno())
    actual = digest.hexdigest()
    if received != size or extra or actual != expected:
        raise RuntimeError("stream size or digest mismatch")
    os.replace(part, destination)
    result = f"OK {received} {actual}"
except Exception as exc:
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    result = f"ERROR {type(exc).__name__}"
finally:
    listener.close()
with open(status, "w", encoding="ascii") as handle:
    handle.write(result)
    handle.flush()
    os.fsync(handle.fileno())
'''
        quoted_paths = " ".join(shlex.quote(value) for value in (
            guest_path, part_path, status_path, port_path,
            str(source_size), source_sha256, str(max(1, timeout))))
        launch = self.run_command(
            f"rm -f {shlex.quote(part_path)} {shlex.quote(status_path)} "
            f"{shlex.quote(port_path)} {shlex.quote(log_path)}; "
            f"nohup python3 -c {shlex.quote(receiver)} {quoted_paths} "
            f">{shlex.quote(log_path)} 2>&1 </dev/null & "
            "echo RSIAGENT_PUSH_PID=$!", timeout=30)
        pid_match = re.search(r"RSIAGENT_PUSH_PID=(\d+)", launch or "")
        pid = pid_match.group(1) if pid_match else ""

        def cleanup(*, terminate: bool) -> None:
            kill = f"kill {pid} 2>/dev/null || true; " if terminate and pid else ""
            self.run_command(
                kill + f"rm -f {shlex.quote(part_path)} "
                f"{shlex.quote(status_path)} {shlex.quote(port_path)} "
                f"{shlex.quote(log_path)}", timeout=30)

        if not pid:
            cleanup(terminate=False)
            return False, "guest receiver did not publish a process id"
        port_out = self.run_command(
            f"for i in $(seq 1 100); do test -s {shlex.quote(port_path)} "
            f"&& cat {shlex.quote(port_path)} && exit 0; sleep 0.1; done; "
            "echo RSIAGENT_PUSH_PORT_TIMEOUT", timeout=20)
        port_match = re.search(r"(?m)^([0-9]{2,5})$", port_out or "")
        if not port_match or not 1 <= int(port_match.group(1)) <= 65535:
            cleanup(terminate=True)
            return False, "guest receiver did not publish a valid port"

        try:
            with socket.create_connection(
                    (docker_ip, int(port_match.group(1))), timeout=10) as stream:
                stream.settimeout(max(1, timeout))
                with open(local_path, "rb") as source:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        stream.sendall(block)
                stream.shutdown(socket.SHUT_WR)
            after_size = os.path.getsize(local_path)
            if after_size != source_size:
                cleanup(terminate=True)
                return False, "host artifact changed size during streaming"
        except Exception as exc:  # noqa: BLE001
            cleanup(terminate=True)
            return False, f"{type(exc).__name__}: {exc}"

        status_out = self.run_command(
            f"for i in $(seq 1 600); do test -s {shlex.quote(status_path)} "
            f"&& cat {shlex.quote(status_path)} && exit 0; sleep 0.1; done; "
            "echo RSIAGENT_PUSH_STATUS_TIMEOUT", timeout=90)
        expected_status = f"OK {source_size} {source_sha256}"
        if expected_status not in (status_out or "").splitlines():
            cleanup(terminate=True)
            return False, (status_out or "guest receiver returned no status")[-240:]
        cleanup(terminate=False)
        return True, ""

    def _push_file_multipart(self, local_path: str, guest_path: str,
                             attempts: int, timeout: int):
        """Compatibility transport for providers without the Docker route."""

        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder
        except Exception as exc:  # noqa: BLE001
            return False, ("streaming multipart transport is unavailable "
                           f"({type(exc).__name__})")

        url = f"{self.env.controller.http_server}/setup/upload"
        last_reason = "upload did not run"
        for attempt in range(max(1, attempts)):
            try:
                with open(local_path, "rb") as source:
                    form = MultipartEncoder({
                        "file_path": guest_path,
                        "file_data": (
                            os.path.basename(guest_path) or "artifact",
                            source, "application/octet-stream"),
                    })
                    response = requests.post(
                        url, headers={"Content-Type": form.content_type},
                        data=form, timeout=(10, timeout))
                if response.status_code == 200:
                    return True, ""
                last_reason = (f"upload returned HTTP {response.status_code}: "
                               f"{response.text[:160]}")
            except Exception as exc:  # noqa: BLE001
                last_reason = f"{type(exc).__name__}: {exc}"
            log.warning("push_file(%s -> %s) attempt %d/%d failed: %s",
                        local_path, guest_path, attempt + 1,
                        max(1, attempts), last_reason)
            if attempt + 1 < max(1, attempts):
                time.sleep(min(4, 2 ** attempt))
        return False, last_reason


def _bound(text: str, cap: int) -> str:
    """Optionally bound ``text``; a non-positive cap means lossless transport."""
    if cap is None or cap <= 0:
        return text
    if len(text) <= cap:
        return text
    head, tail = cap // 3, cap - cap // 3
    return (text[:head] + f"\n...[{len(text) - cap} chars omitted]...\n" + text[-tail:])
