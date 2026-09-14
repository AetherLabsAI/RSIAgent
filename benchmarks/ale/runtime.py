"""Process-local ALE platform bindings shared by all VMs in one task lineage."""

from __future__ import annotations

import json
import ntpath
import re

from .transport import AleVM
from .windows import WindowsVerifierExecutor, WindowsVM

_platform = None


def make_vm(sandbox):
    return WindowsVM(sandbox) if sandbox["os"] == "windows" else AleVM(sandbox)


def initialize_vm(vm):
    if vm.is_windows:
        vm.prepare()
    else:
        result = vm.native_command(
            "mkdir -p /home/user/.ale && sudo -n true && command -v unshare && command -v nsenter && command -v setpriv"
        )
        if result.get("return_code"):
            raise RuntimeError("ALE Ubuntu lacks verifier privilege or namespace tools")


def bind_platform(vm, visible_roots=()):
    """A worker handles one OS; snapshots and Looks dispatch on the actual VM."""
    global _platform
    vm.visible_roots = tuple(visible_roots)
    if _platform is not None:
        if _platform != vm.os_type:
            raise RuntimeError("Mixed operating systems require separate workers")
        return
    _platform = vm.os_type
    from core import actor, imagery, loop, verifier, verifier_runtime

    original_snapshot = loop._snapshot

    def snapshot(target_vm, excluded_paths=()):
        if not isinstance(target_vm, AleVM):
            return original_snapshot(target_vm, excluded_paths)
        roots = list(
            dict.fromkeys(
                [
                    target_vm.home,
                    target_vm.home + "/Desktop",
                    target_vm.home + "/Documents",
                    *getattr(target_vm, "visible_roots", ()),
                ]
            )
        )
        code = f"""import os,json,pathlib
out={{}}
for root in {roots!r}:
 for current,dirs,files in os.walk(root,followlinks=False):
  relative=pathlib.Path(current).relative_to(root)
  dirs[:]=[d for d in dirs if not d.startswith('.') and not pathlib.Path(current,d).is_symlink()]
  if len(relative.parts)>=2: dirs[:]=[]
  for name in files:
   if name.startswith('.'): continue
   p=pathlib.Path(current,name)
   if p.is_symlink(): continue
   try: out[p.as_posix()]=str(p.stat().st_mtime_ns)
   except OSError: pass
print(json.dumps(out))
"""
        result = json.loads(target_vm.trusted_python(code, timeout=60))
        excluded = [
            p.replace("/home/user", target_vm.home, 1).replace("\\", "/").casefold()
            for p in excluded_paths
        ]
        return {
            p: m
            for p, m in result.items()
            if not any(
                p.casefold() == x or p.casefold().startswith(x + "/") for x in excluded
            )
        }

    loop._snapshot = snapshot
    if not vm.is_windows:
        return
    original_build = actor.build_system

    def windows_system(cfg=None, **kwargs):
        result = original_build(cfg, **kwargs).replace(
            "real Ubuntu machine", "real Windows machine"
        )
        result = result.replace(
            "MACHINE FACTS: Ubuntu with python3. You may install tools you need (pip3 / apt-get).",
            "MACHINE FACTS: Windows with native Python and Git Bash. You may install tools you need with pip or native Windows installers.",
        )
        return (
            result.replace("/home/user", vm.home)
            + "\nWindows transport: Python runs natively; Bash runs through Git for Windows. Use C:/... or E:/... paths in Python and Looks. Home is C:/Users/User.\n"
        )

    actor.build_system = loop.build_system = windows_system
    original_look = imagery.fetch_look_image

    def fetch_look(target_vm, path):
        if isinstance(target_vm, WindowsVM):
            return target_vm.fetch_look_image(path)
        return original_look(target_vm, path)

    imagery.fetch_look_image = loop.fetch_look_image = fetch_look
    if hasattr(verifier, "fetch_look_image"):
        verifier.fetch_look_image = fetch_look
    verifier_runtime.AgenticVerifierExecutor = WindowsVerifierExecutor
    if hasattr(verifier, "AgenticVerifierExecutor"):
        verifier.AgenticVerifierExecutor = WindowsVerifierExecutor
    original_verifier_system = verifier_runtime.agentic_verifier_system_for

    def verifier_system(*args, **kwargs):
        result = original_verifier_system(*args, **kwargs).replace(
            "tmpfs", "directory inside the rollback mirror"
        )
        return (
            result.replace("/home/user", vm.home)
            + "\nThis is Windows. Python runs natively; Bash uses Git for Windows. Use absolute drive paths. VERIFIER_SCRATCH is private and persists across inspections. Programs run with a restricted Windows token with administrator rights disabled.\n"
        )

    verifier_runtime.agentic_verifier_system_for = verifier_system

    def validate_look(path, hide_actor_memory=False, private_paths=()):
        if not isinstance(path, str) or any(c in path for c in ("\x00", "\n", "\r")):
            return False, "Malformed Look path"
        if path.lower().startswith("screen:"):
            return path.split(":", 1)[1].strip() in (
                "",
                "0",
                ":0",
            ), "Windows supports the primary desktop only"
        if not re.match(r"^[A-Za-z]:[/\\]", path):
            return False, "Windows Look requires an absolute drive path or screen:"
        actual = ntpath.normcase(ntpath.normpath(path))
        mapped = [p.replace("/home/user", vm.home, 1) for p in private_paths]
        if hide_actor_memory:
            mapped.append(vm.home + "/.memory")
        if any(
            actual == (p := ntpath.normcase(ntpath.normpath(x)))
            or actual.startswith(p.rstrip("\\") + "\\")
            for x in mapped
        ):
            return False, "That path is private to the Actor"
        # File reads/rendering execute with the restricted token. ACLs also
        # enforce the boundary when an allowed path aliases a private file.
        return True, ""

    verifier_runtime.validate_verifier_look_path = validate_look
