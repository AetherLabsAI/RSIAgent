"""Fresh guest leases and ALE-specific adapters for RSIAgent's RSI hooks."""

from __future__ import annotations

import dataclasses
import json
import queue
import re
import tarfile
import threading
from pathlib import Path

from .runtime import bind_platform, initialize_vm, make_vm


class CleanPool:
    """Bounded guest slots; every reused lease gets a freshly provisioned VM."""

    def __init__(self, sandboxes, *, practice, reset_endpoint):
        self.available = queue.Queue()
        self.vms = []
        self._lock = threading.Lock()
        self.leased = set()
        self.first = set()
        self.practice = practice
        self.reset_endpoint = reset_endpoint
        for sandbox in sandboxes:
            vm = make_vm(sandbox)
            initialize_vm(vm)
            bind_platform(vm)
            if practice:
                clean_practice(vm)
            self.vms.append(vm)
            self.first.add(id(vm))
            self.available.put(vm)

    def acquire(self):
        vm = self.available.get(timeout=3600)
        with self._lock:
            self.leased.add(id(vm))
        try:
            if id(vm) in self.first:
                self.first.remove(id(vm))
            else:
                self.reset(vm)
        except BaseException:
            self.release(vm)
            raise
        return Lease(self, vm), vm

    def reset(self, vm):
        from .lifecycle import request_reset

        sandbox = request_reset(self.reset_endpoint, vm.sandbox["id"])
        visible = getattr(vm, "visible_roots", ())
        replacement = make_vm(sandbox)
        vm.client.close()
        vm.__dict__.clear()
        vm.__dict__.update(replacement.__dict__)
        initialize_vm(vm)
        bind_platform(vm, visible)
        if self.practice:
            clean_practice(vm)
        return {"ok": True}

    def release(self, vm):
        with self._lock:
            if id(vm) not in self.leased:
                return
            self.leased.remove(id(vm))
        self.available.put(vm)

    def close(self):
        for vm in self.vms:
            vm.client.close()


class Lease:
    def __init__(self, pool, vm):
        self.pool, self.vm = pool, vm

    def close(self):
        self.pool.release(self.vm)


def clean_practice(vm):
    """Remove baked benchmark payloads only inside newly allocated practice VMs."""
    if not vm.sandbox.get("metadata", {}).get("rsiagent_practice", False):
        raise RuntimeError(
            "Practice cleaning requires an explicitly marked disposable guest"
        )
    roots = ["E:/agenthle"] if vm.is_windows else ["/media/user/data/agenthle"]
    if not vm.is_windows:
        result = vm.native_command(
            "sudo -n rm -rf -- /media/user/data/agenthle", timeout=1800
        )
        if result.get("return_code"):
            raise RuntimeError(
                "Could not remove baked task payloads from the disposable practice guest"
            )
    code = f"""import pathlib,shutil,os,stat,json
roots={roots!r}
def retry(fn,path,exc):
 os.chmod(path,stat.S_IWRITE|stat.S_IREAD|stat.S_IEXEC);fn(path)
for name in roots:
 p=pathlib.Path(name)
 if p.is_symlink(): p.unlink()
 elif p.exists(): shutil.rmtree(p,onerror=retry)
 assert not p.exists(), name
home=pathlib.Path({vm.home!r})
for name in ['.memory','evolution_project','evolution_wave','actor_handoff.md','learning_diagnosis.md','project_next.md','curriculum_notes.md','phase1_wave.json']:
 p=home/name
 if p.is_symlink() or p.is_file(): p.unlink()
 elif p.exists(): shutil.rmtree(p,onerror=retry)
print(json.dumps({{'removed_payload_roots':roots}}))
"""
    vm.trusted_python(code, timeout=1800)


class Audit:
    """Host-only public-instruction leakage audit; no grader constants or scores."""

    def __init__(self, corpus):
        self.path = Path(corpus)
        self.grams = set(json.loads(self.path.read_text()))
        if not self.grams or any(not isinstance(v, str) for v in self.grams):
            raise RuntimeError("ALE instruction corpus is missing or malformed")

    def validate(self, path):
        if (
            Path(path).resolve() != self.path.resolve()
            or set(json.loads(self.path.read_text())) != self.grams
        ):
            raise RuntimeError("ALE instruction corpus drift")
        return {"benchmark": "ALE", "shingles": len(self.grams)}

    def text(self, text, mode="practice", authorized_instruction="", **kwargs):
        from explore.commit import _shingles

        hits = []
        if re.search(
            r"github\.com[/\\]+rdi-berkeley[/\\]+agents-last-exam|ALE_REFERENCE_ARCHIVE_PASSWORD|secret[/\\]+eval_time",
            text,
            re.I,
        ):
            hits.append({"kind": "benchmark-private-source"})
        if (_shingles(text) & self.grams) - _shingles(authorized_instruction):
            hits.append({"kind": "other-task-instruction"})
        return hits

    def transcripts(self, root, mode="practice", authorized_instruction="", **kwargs):
        findings = []

        def inspect(value):
            if isinstance(value, str):
                findings.extend(self.text(value, mode, authorized_instruction))
            elif isinstance(value, dict):
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        for path in Path(root).rglob("transcript.json"):
            inspect(json.loads(path.read_text()))
        return findings

    def captured(self, root, authorized_instruction=""):
        findings = []
        with tarfile.open(Path(root) / "materials.tgz", "r:gz") as tf:
            for member in tf:
                findings.extend(
                    self.text(
                        member.name, authorized_instruction=authorized_instruction
                    )
                )
                if member.isfile():
                    findings.extend(
                        self.text(
                            tf.extractfile(member).read().decode("utf-8", "ignore"),
                            authorized_instruction=authorized_instruction,
                        )
                    )
        return findings

    def memory(
        self,
        files,
        corpus_path,
        reject_log,
        authorized_instruction="",
        require_corpus=False,
    ):
        self.validate(corpus_path)
        return {
            name: data
            for name, data in files.items()
            if not self.text(
                name + "\n" + data.decode("utf-8", "ignore"),
                authorized_instruction=authorized_instruction,
            )
        }


def hooks_for(pool, corpus):
    from core.loop import run_attempt
    from explore.practice_loop import PracticeHooks

    audit = Audit(corpus)
    windows = pool.vms[0].is_windows
    home = pool.vms[0].home
    if windows:
        from explore import (
            phase1_wave,
            practice_loop,
            target_learning,
            unified_evolution,
        )

        for module in (practice_loop, target_learning, phase1_wave, unified_evolution):
            for name, value in vars(module).copy().items():
                if (
                    name.isupper()
                    and isinstance(value, str)
                    and value.startswith("/home/user/")
                ):
                    setattr(module, name, value.replace("/home/user", home, 1))

    def attempt(prompt, vm, cfg, sink, **kwargs):
        if windows:
            prompt = prompt.replace("/home/user", home)
            cfg = dataclasses.replace(
                cfg,
                system_extra=cfg.system_extra.replace("/home/user", home),
                practice_done_requires=cfg.practice_done_requires.replace(
                    "/home/user", home
                ),
            )
        return run_attempt(prompt, vm, cfg, sink, **kwargs)

    return PracticeHooks(
        run_attempt=attempt,
        reset_vm=lambda vm, target: pool.reset(vm),
        validate_corpus=audit.validate,
        audit_text=audit.text,
        audit_transcripts=audit.transcripts,
        audit_captured=audit.captured,
        audit_memory=audit.memory,
    )
