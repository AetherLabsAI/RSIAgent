"""Windows realization of RSIAgent's Program/Look and rollback verifier contracts."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

from core.verifier_runtime import (
    AgenticVerifierExecutor,
    AgenticVerifierInfrastructureError,
    _trace,
)
from env.vm import Trace, _decode_run_output_envelopes

from .transport import AleVM, CuaTransportError


class WindowsVM(AleVM):
    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.helper_root = self.home + "/.ale/rsiagent-runtime"
        self.bash = "C:/Program Files/Git/bin/bash.exe"
        self.model_python = ""

    def prepare(self):
        root = self.helper_root.replace("/", "\\")
        self.native_command(f'cmd /c if not exist "{root}" mkdir "{root}"')
        result = self.trusted_python(
            'import json,shutil,sys\nprint(json.dumps({"python":shutil.which("python"),"bash":shutil.which("bash")}))\n'
        )
        self.model_python = json.loads(result.strip())["python"] or self.python
        self.trusted_python(
            f'import pathlib\nassert pathlib.Path({self.bash!r}).is_file(), "Git Bash missing"\n'
        )
        self.trusted_python(
            "import importlib.util,subprocess,sys\n"
            'deps={"win32job":"pywin32==311", "fitz":"PyMuPDF==1.26.7"}\n'
            "missing=[package for module,package in deps.items() if importlib.util.find_spec(module) is None]\n"
            'if missing: subprocess.run([sys.executable,"-m","pip","install","--disable-pip-version-check","--no-input",*missing],check=True)\n',
            timeout=300,
        )
        if self.model_python.casefold().replace(
            "\\", "/"
        ) != self.python.casefold().replace("\\", "/"):
            self.trusted_python(
                "import subprocess\nsubprocess.run(["
                + repr(self.model_python)
                + ',"-m","pip","install","--disable-pip-version-check","--no-input","PyMuPDF==1.26.7"],check=True)\n',
                timeout=300,
            )
        self.write_text(
            self.helper_root + "/windows_job.py",
            Path(__file__).with_name("windows_job.py").read_text(),
        )
        self.write_text(
            self.helper_root + "/python3",
            '#!/bin/bash\nexec "' + self.model_python.replace("\\", "/") + '" "$@"\n',
        )
        self.trusted_python(
            "import pathlib\npathlib.Path("
            + repr(self.home + "/work/.rsiagent-actions")
            + ").mkdir(parents=True,exist_ok=True)\n"
        )
        self.trusted_python(
            "import pathlib\npathlib.Path("
            + repr(self.home + "/.ale/tmp")
            + ").mkdir(parents=True,exist_ok=True)\n"
        )

    def host_path(self, path):
        """Translate RSIAgent's host-authored POSIX paths for native Windows I/O."""
        value = path.replace("/home/user", self.home)
        return re.sub(r"(?<![\w/.:])/tmp/", self.home + "/.ale/tmp/", value)

    def write_text(self, path, content):
        return super().write_text(self.host_path(path), content)

    def fetch_file(self, path, max_bytes=None):
        return super().fetch_file(self.host_path(path), max_bytes=max_bytes)

    def push_file(self, local_path, guest_path, attempts=3, timeout=3600):
        return super().push_file(
            local_path, self.host_path(guest_path), attempts=attempts, timeout=timeout
        )

    def run_windows_program(
        self, lang, code, timeout=600, *, restricted=False, workspace=None
    ):
        started = time.monotonic()
        token = os.urandom(12).hex()
        base = (
            (workspace or (self.home + "/work/.rsiagent-actions")) + "/action_" + token
        )
        suffix = ".py" if str(lang).startswith("py") else ".sh"
        script = base + suffix
        specpath = base + ".json"
        output = base + ".log"
        try:
            self.write_text(script, code)
            if str(lang).startswith("py"):
                argv = [self.model_python or self.python, "-u", script]
            elif lang in ("bash", "sh"):
                argv = [self.bash, "--noprofile", "--norc", script]
            else:
                return Trace(f"Unsupported Program language: {lang}\n[exit 126]", 126)
            env = {
                "HOME": self.home,
                "PATH": self.helper_root
                + ";C:/Program Files/Git/usr/bin;"
                + self._guest_path(),
                "MSYS2_ARG_CONV_EXCL": "*",
                "TAR_OPTIONS": "--force-local",
            }
            if workspace:
                env["VERIFIER_SCRATCH"] = workspace
            spec = {
                "argv": argv,
                "timeout": max(1, timeout - 30) if timeout > 30 else max(1, timeout),
                "output": output,
                "restricted": restricted,
                "cwd": self.home,
                "env": env,
            }
            self.write_text(specpath, json.dumps(spec))
            result = self.native_command(
                f'"{self.python}" "{self.helper_root}/windows_job.py" "{specpath}"',
                timeout=timeout + 15,
            )
            out = result.get("stdout") or ""
            marker = "RSIAGENT_WINDOWS_RESULT:"
            if int(result.get("return_code", 0)) or marker not in out:
                raise CuaTransportError(
                    out + (result.get("stderr") or "Windows runner returned no result")
                )
            payload = json.loads(out.split(marker, 1)[1])
            rc = int(payload["exit_code"])
            archived, context = _decode_run_output_envelopes(
                "RSIAGENT_RUN_OUTPUT_BASE64:"
                + payload["output_b64"]
                + "\n"
                + f"\n[exit {rc}]"
            )
            return Trace(
                archived,
                rc,
                time.monotonic() - started,
                bool(payload["timed_out"]),
                False,
                context,
            )
        except Exception as exc:
            return Trace(
                f"[channel error: {type(exc).__name__}: {exc}]",
                None,
                time.monotonic() - started,
                False,
                True,
            )

    def _guest_path(self):
        if not hasattr(self, "_path_cache"):
            self._path_cache = self.trusted_python(
                'import os\nprint(os.environ["PATH"])\n'
            ).strip()
        return self._path_cache

    def run_script(self, lang, code, timeout=600, cap=0, allow_staging_fallback=False):
        return self.run_windows_program(lang, code, timeout)

    def run_command(self, cmd, timeout=30, cap=4000):
        trace = self.run_windows_program("bash", self.host_path(cmd), timeout)
        out = trace.context_stdout if trace.context_stdout is not None else trace.stdout
        out = re.sub(r"\n\[exit \d+\]$", "", out)
        return (
            out
            if not cap or len(out) <= cap
            else out[:cap] + f"\n[output truncated: {len(out)} chars]"
        )

    def fetch_look_image(self, path, executor=None):
        def read_file(target):
            if executor is None:
                return self.fetch_file(target)
            trace = executor(
                "python",
                'import pathlib,base64\nprint("RSIAGENT_LOOK_BASE64:"+base64.b64encode(pathlib.Path('
                + repr(target)
                + ").read_bytes()).decode())\n",
                timeout=120,
            )
            if trace.infra_fail or trace.exit_code != 0:
                return None, trace.stdout
            match = re.search(r"RSIAGENT_LOOK_BASE64:([A-Za-z0-9+/=]*)", trace.stdout)
            if match is None:
                return None, "Restricted file read returned no data"
            return base64.b64decode(match.group(1), validate=True), ""

        if path.lower().startswith("screen:"):
            display = path.split(":", 1)[1].strip()
            if display not in ("", "0", ":0"):
                return None, "Windows supports the primary desktop screen: or screen:0."
            try:
                data = self.rpc("screenshot", {}, timeout=30)
                if not data.get("success"):
                    return None, str(data.get("error") or "Screenshot failed")
                value = data.get("image_data")
                if not isinstance(value, str):
                    return None, "CUA returned an unsupported screenshot response"
                return base64.b64decode(value.split(",")[-1], validate=True), ""
            except Exception as exc:
                return None, str(exc)
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
            return read_file(path)
        token = os.urandom(12).hex()
        target = self.helper_root + "/render_" + token + ".png"
        # Rendering is generic format transport. Never inspect hidden answer data.
        code = f"""import pathlib, subprocess, shutil, tempfile
source=pathlib.Path({path!r})
target=pathlib.Path({target!r})
if source.suffix.lower()=='.pdf':
 import fitz
 with fitz.open(source) as doc: doc[0].get_pixmap(matrix=fitz.Matrix(120/72,120/72)).save(target)
else:
 directory=pathlib.Path(tempfile.mkdtemp(prefix='rsiagent_render_'))
 exe=shutil.which('soffice') or 'C:/Program Files/LibreOffice/program/soffice.exe'
 subprocess.run([exe,'--headless','-env:UserInstallation='+str((directory/'profile').as_uri()),'--convert-to','png','--outdir',str(directory),str(source)],check=True,timeout=110)
 images=list(directory.glob('*.png'))
 if not images: raise RuntimeError('Document renderer produced no PNG; use the application and screen:')
 shutil.copyfile(images[0],target)
print(target)
"""
        try:
            if executor is None:
                self.trusted_python(code, timeout=140)
            else:
                trace = executor("python", code, timeout=140)
                if trace.infra_fail or trace.exit_code != 0:
                    return None, trace.stdout
            return read_file(target)
        except Exception as exc:
            return None, str(exc)


class WindowsVerifierExecutor(AgenticVerifierExecutor):
    def __init__(self, vm, **kwargs):
        super().__init__(vm, **kwargs)
        if self._execution_mode != "rollback_mirror":
            raise AgenticVerifierInfrastructureError(
                "Windows requires the configured rollback_mirror boundary"
            )
        self._workspace = vm.home + "/.ale/rsiagent-verifier-" + os.urandom(16).hex()

    def _ensure_workspace(self, timeout):
        if self._closed:
            return _trace("Verifier workspace is closed", 125, infra_fail=True)
        if self._initialized:
            return None
        if self._init_reason:
            return _trace(self._init_reason, 125, infra_fail=True)
        try:
            self._rollback_transaction.begin()
            mapped = [
                self._vm.home + p[len("/home/user") :]
                if p.startswith("/home/user/")
                else p
                for p in self._private_paths
            ]
            quarantine = "C:/ProgramData/rsiagent-private-" + os.urandom(16).hex()
            code = f"""import os,pathlib,shutil,win32security,ntsecuritycon,win32api
root=pathlib.Path({quarantine!r});root.mkdir()
system=win32security.CreateWellKnownSid(win32security.WinLocalSystemSid,None)
admins=win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid,None)
token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32security.TOKEN_ADJUST_PRIVILEGES|win32security.TOKEN_QUERY)
win32security.AdjustTokenPrivileges(token,False,[(win32security.LookupPrivilegeValue(None,'SeRestorePrivilege'),win32security.SE_PRIVILEGE_ENABLED)])
def protect(p):
 acl=win32security.ACL()
 for sid in (system,admins): acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,3,ntsecuritycon.FILE_ALL_ACCESS,sid)
 win32security.SetNamedSecurityInfo(str(p),win32security.SE_FILE_OBJECT,win32security.OWNER_SECURITY_INFORMATION|win32security.DACL_SECURITY_INFORMATION|win32security.PROTECTED_DACL_SECURITY_INFORMATION, system,None,acl,None)
protect(root)
for i,p in enumerate({mapped!r}):
 p=pathlib.Path(p)
 if p.exists() or p.is_symlink():
  dst=root/str(i);shutil.move(p,dst);protect(dst)
  if dst.is_dir():
   for current,dirs,files in os.walk(dst,followlinks=False):
    for name in dirs+files:
     child=pathlib.Path(current,name)
     if child.is_symlink(): raise RuntimeError('Actor-private tree has an unsupported reparse point')
     protect(child)
pathlib.Path({self._workspace!r}).mkdir(parents=True)
"""
            self._vm.trusted_python(code, timeout=timeout)
            self._initialized = True
            self._rollback_private_paths_hidden = True
            if self._scratch_archive:
                import tempfile

                with tempfile.NamedTemporaryFile() as incoming:
                    incoming.write(self._scratch_archive)
                    incoming.flush()
                    archive = self._workspace + "/.restore.tar"
                    ok, why = self._vm.push_file(incoming.name, archive)
                    if not ok:
                        raise CuaTransportError(why)
                self._vm.trusted_python(
                    f"""import pathlib,tarfile
root=pathlib.Path({self._workspace!r}).resolve()
archive=pathlib.Path({archive!r})
with tarfile.open(archive) as tf:
 for m in tf.getmembers():
  dest=(root/m.name).resolve()
  if not (m.isfile() or m.isdir()) or root not in dest.parents: raise RuntimeError('Unsafe scratch archive')
 tf.extractall(root,filter='data')
archive.unlink()
""",
                    timeout=600,
                )
                self._scratch_archive = None
            return None
        except Exception as exc:
            self._initialized = False
            self._init_reason = (
                f"Windows verifier preflight failed: {type(exc).__name__}: {exc}"
            )
            return _trace(self._init_reason, 125, infra_fail=True)

    def _run_isolated(self, interpreter, extension, code, timeout):
        return self._vm.run_windows_program(
            "python" if extension == "py" else "bash",
            code,
            timeout,
            restricted=True,
            workspace=self._workspace,
        )

    def export_scratch(self):
        if not self._initialized or self._closed:
            return self._scratch_archive
        archive = self._vm.helper_root + "/scratch-" + os.urandom(16).hex() + ".tar"
        self._vm.trusted_python(
            f"""import pathlib,tarfile,os
root=pathlib.Path({self._workspace!r})
with tarfile.open({archive!r},'w') as tf:
 for current,dirs,files in os.walk(root,followlinks=False):
  dirs[:]=sorted(d for d in dirs if not (pathlib.Path(current)/d).is_symlink())
  for name in dirs+sorted(files):
   p=pathlib.Path(current)/name
   if not p.is_symlink() and (p.is_dir() or p.is_file()): tf.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
""",
            timeout=3600,
        )
        data, why = self._vm.fetch_file(archive)
        if data is None:
            raise AgenticVerifierInfrastructureError(
                "Could not preserve verifier scratch: " + why
            )
        return data

    def fetch_look_image(self, path):
        ready = self._ensure_workspace(180)
        if ready:
            raise AgenticVerifierInfrastructureError(ready.stdout)
        return self._vm.fetch_look_image(path, executor=self)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._rollback_transaction.rollback()
        except Exception as exc:
            raise AgenticVerifierInfrastructureError(
                "Windows candidate rollback failed; grading is blocked"
            ) from exc
