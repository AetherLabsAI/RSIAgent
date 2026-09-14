"""Synchronous CUA transport; no task metadata or grader enters this module."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
from pathlib import Path
from types import SimpleNamespace

import docker
import requests

from env.vm import VM


class CuaTransportError(RuntimeError):
    pass


class AleVM(VM):
    def __init__(self, sandbox: dict):
        self.sandbox = dict(sandbox)
        self.os_type = sandbox["os"]
        self.endpoint = sandbox["endpoint"].rstrip("/")
        self.python = sandbox["python"]
        self.is_windows = self.os_type == "windows"
        self.home = "C:/Users/User" if self.is_windows else "/home/user"
        # A populated 32 GiB ALE guest can need more than five minutes to save
        # its RAM under concurrent disk load. This is a host-operation deadline;
        # the Actor/Verifier active-work watchdog remains unchanged.
        self.verifier_checkpoint_timeout = 1800
        self.client = docker.from_env()
        container = self.client.containers.get(sandbox["id"])
        env = SimpleNamespace(
            provider_name="docker",
            provider=SimpleNamespace(container=container),
            client_password="ale-harness-nopasswd",
            controller=SimpleNamespace(),
        )
        super().__init__(env)

    def rpc(self, command: str, params: dict, timeout=60):
        # Do not replay mutating commands following an ambiguous transport failure.
        deadline = time.monotonic() + timeout
        with requests.post(
            self.endpoint + "/cmd",
            json={"command": command, "params": params},
            stream=True,
            timeout=(10, timeout),
        ) as response:
            response.raise_for_status()
            pending = bytearray()
            start = 0
            scan = 0
            for chunk in response.iter_content(chunk_size=None):
                if time.monotonic() > deadline:
                    raise TimeoutError("CUA result deadline exceeded")
                pending.extend(chunk)
                while (newline := pending.find(b"\n", scan)) >= 0:
                    line = bytes(pending[start:newline]).rstrip(b"\r")
                    start = scan = newline + 1
                    if line.startswith(b"data:"):
                        return self._parse_rpc(line[5:])
                scan = len(pending)
            tail = bytes(pending[start:]).strip()
            if tail.startswith(b"data:"):
                return self._parse_rpc(tail[5:])
        raise CuaTransportError("CUA returned no complete result event")

    @staticmethod
    def _parse_rpc(raw):
        data = json.loads(raw.strip().removeprefix(b"\xef\xbb\xbf"))
        if not isinstance(data, dict):
            raise CuaTransportError("CUA result is not an object")
        return data

    def native_command(self, command, timeout=60):
        if self.is_windows:
            data = self.rpc("run_command", {"command": command}, timeout=timeout)
        else:
            # CUA captures pipe EOF, which a persistent namespace keeper can
            # inherit. Capture into ordinary files and wait for the direct shell
            # process instead. Child daemons cannot keep the RPC response open.
            helper = (
                f"command={command!r}\ntimeout_value={float(timeout)!r}\n"
                + """import base64,json,os,signal,subprocess,tempfile
with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
 p=subprocess.Popen(['/bin/bash','-lc',command],stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True,close_fds=True)
 try: rc=p.wait(timeout=timeout_value)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL);p.wait();rc=124
 out.seek(0);err.seek(0)
 print('RSIAGENT_CUA_COMMAND:'+json.dumps({'return_code':rc,'stdout_b64':base64.b64encode(out.read()).decode(),'stderr_b64':base64.b64encode(err.read()).decode()}))
"""
            )
            wrapped = "python3 -c " + shlex.quote(helper)
            data = self.rpc("run_command", {"command": wrapped}, timeout=timeout + 20)
            if data.get("success") and "RSIAGENT_CUA_COMMAND:" in (
                data.get("stdout") or ""
            ):
                payload = json.loads(
                    data["stdout"].split("RSIAGENT_CUA_COMMAND:", 1)[1]
                )
                data = {
                    "success": True,
                    "return_code": int(payload["return_code"]),
                    "stdout": base64.b64decode(
                        payload["stdout_b64"], validate=True
                    ).decode("utf-8", errors="backslashreplace"),
                    "stderr": base64.b64decode(
                        payload["stderr_b64"], validate=True
                    ).decode("utf-8", errors="backslashreplace"),
                }
            else:
                raise CuaTransportError(
                    str(
                        data.get("stderr")
                        or data.get("error")
                        or "Linux command helper returned no result"
                    )
                )
        if not data.get("success"):
            raise CuaTransportError(str(data.get("error") or "CUA command failed"))
        return data

    def run_command(self, cmd: str, timeout=30, cap=4000):
        try:
            data = self.native_command(cmd, timeout=timeout)
            out = (data.get("stdout") or "") + (data.get("stderr") or "")
            code = int(data.get("return_code", 0))
            if code and not out.strip():
                out = f"[command exited {code} without output]"
            if cap and len(out) > cap:
                out = out[:cap] + f"\n[output truncated: {len(out)} chars]"
            return out
        except Exception as exc:
            return f"[channel error: {type(exc).__name__}: {exc}]"

    def write_text(self, path, content):
        # CUA's legacy write_text command uses the Windows locale encoding.
        # Its current SDK uses write_bytes for UTF-8; follow that binary contract.
        encoded = content.encode("utf-8")
        for offset in range(0, max(1, len(encoded)), 1024 * 1024):
            chunk = encoded[offset : offset + 1024 * 1024]
            result = self.rpc(
                "write_bytes",
                {
                    "path": path,
                    "content_b64": base64.b64encode(chunk).decode("ascii"),
                    "append": offset > 0,
                },
                timeout=120,
            )
            if not result.get("success"):
                raise CuaTransportError(str(result.get("error") or "CUA write failed"))

    def fetch_file(self, path, max_bytes=None):
        try:
            parts = []
            offset = 0
            chunk_size = 8 * 1024 * 1024
            while True:
                result = self.rpc(
                    "read_bytes",
                    {"path": path, "offset": offset, "length": chunk_size},
                    timeout=120,
                )
                if not result.get("success"):
                    return None, str(result.get("error") or "CUA read failed")
                raw = base64.b64decode(result.get("content_b64") or "", validate=True)
                parts.append(raw)
                offset += len(raw)
                if max_bytes is not None and offset > max_bytes:
                    return None, f"file exceeds the explicit {max_bytes}-byte limit"
                if len(raw) < chunk_size:
                    return b"".join(parts), ""
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def push_file(self, local_path, guest_path, attempts=3, timeout=3600):
        # Fixed-size base64 chunks keep command lines small without a file-size cap.
        if not Path(local_path).is_file() or Path(local_path).is_symlink():
            return False, "source is not an ordinary file"
        token = os.urandom(12).hex()
        staged = guest_path + ".rsiagent-stage-" + token
        deadline = time.monotonic() + timeout
        try:
            import hashlib

            digest = hashlib.sha256()
            with open(local_path, "rb") as incoming:
                offset = 0
                while True:
                    data = incoming.read(512 * 1024)
                    if not data and offset:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("upload deadline exceeded")
                    digest.update(data)
                    encoded_path = staged + ".b64"
                    self.write_text(encoded_path, base64.b64encode(data).decode())
                    code = (
                        "import base64,pathlib\n"
                        f"p=pathlib.Path({staged!r});p.parent.mkdir(parents=True,exist_ok=True)\n"
                        f"b=base64.b64decode(pathlib.Path({encoded_path!r}).read_text(),validate=True)\n"
                        f"with p.open({'wb' if offset == 0 else 'r+b'!r}) as f: f.seek({offset});f.write(b);f.truncate()\n"
                        f"pathlib.Path({encoded_path!r}).unlink()\n"
                    )
                    self.trusted_python(code, timeout=120)
                    offset += len(data)
                    if not data:
                        break
            code = (
                "import pathlib,hashlib,os\n"
                f"p=pathlib.Path({staged!r})\n"
                "h=hashlib.sha256()\n"
                'with p.open("rb") as f:\n'
                " while b:=f.read(8388608): h.update(b)\n"
                f"assert p.stat().st_size=={offset} and h.hexdigest()=={digest.hexdigest()!r}\n"
                f"os.replace(p,{guest_path!r})\n"
            )
            self.trusted_python(
                code, timeout=max(120, int(deadline - time.monotonic()))
            )
            return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def trusted_python(self, code, timeout=120):
        # File staging also works on Windows without cmd.exe's 8 KiB argument limit.
        token = os.urandom(12).hex()
        path = f"{self.home}/.ale/rsiagent-host-{token}.py"
        self.write_text(path, code)
        command = (
            f'"{self.python}" "{path}"'
            if self.is_windows
            else f"{shlex.quote(self.python)} {shlex.quote(path)}"
        )
        try:
            result = self.native_command(command, timeout=timeout)
            if int(result.get("return_code", 0)):
                raise CuaTransportError(
                    (result.get("stdout") or "")
                    + (result.get("stderr") or "trusted Python failed")
                )
            return result.get("stdout") or ""
        finally:
            cleanup = (
                f'cmd /c del /q "{path.replace(chr(47), chr(92))}"'
                if self.is_windows
                else "rm -f -- " + shlex.quote(path)
            )
            self.native_command(cleanup, timeout=30)

    def wait_for_controller(self, timeout=180, probe_interval=5.0, stable_probes=2):
        deadline = time.monotonic() + timeout
        stable = 0
        last = ""
        while time.monotonic() < deadline:
            try:
                data = self.native_command("echo RSIAGENT_CONTROLLER_READY", timeout=15)
                last = data.get("stdout") or ""
                stable = stable + 1 if "RSIAGENT_CONTROLLER_READY" in last else 0
                if stable >= max(1, stable_probes):
                    return True, last
            except Exception as exc:
                stable, last = 0, str(exc)
            time.sleep(min(probe_interval, max(0, deadline - time.monotonic())))
        return False, last
