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


_PUBLIC_PATH = re.compile(
    r"(?<![\w./\\-])(?:[A-Za-z]:)?[\\/]?(?:[\w.-]+[\\/]+)+[\w.-]*"
)
_LOCAL_VENV_INSTALL = re.compile(
    r"(?<![\w./-])(?:python(?:3(?:\.\d+)?)?\s+-m\s+venv|uv\s+venv)"
    r"\s+(?P<venv>\.?venv)\s*&&\s*uv\s+pip\s+install\s+--python\s+"
    r"(?P=venv)/bin/python(?:3(?:\.\d+)?)?(?![\w./-])"
)


def _path_spelling(value):
    value = re.sub(r"[\\/]+", "/", value)
    if any(part in (".", "..") for part in value.split("/")):
        return ""
    # A sentence-ending period is not part of its quoted filename.
    return value.rstrip("./")


class _PublicTaskText:
    """Task-bound public wording and declared path spellings, never agent prompts."""

    def __init__(self, record):
        from explore.commit import _shingles

        self.instruction = record["instruction"]
        task = record["task_id"]
        prompt = record["task_prompt"]
        if (not isinstance(self.instruction, str) or not self.instruction
                or not isinstance(prompt, str) or not prompt
                or not isinstance(task, str) or not re.fullmatch(r"[\w-]+/[\w-]+", task)):
            raise ValueError("Invalid ALE public task binding")
        texts = (prompt, self.instruction)
        declared = [_path_spelling(match.group()) for text in texts
                    for match in _PUBLIC_PATH.finditer(text)]
        visible = [_path_spelling(path) for path in record["visible_roots"]]
        task_root = re.compile(
            r"^(?:/media/user/data/agenthle/|[A-Za-z]:/agenthle/)"
            + re.escape(task) + r"/([^/]+)(?:/|$)"
        )
        self.roots = {}
        for path in visible + declared:
            match = task_root.match(path)
            if match:
                self.roots[path[:match.end()].rstrip("/")] = match.group(1)
        # A broad ancestor such as /home/user or /media/user/data grants nothing.
        self.visible = tuple(path for path in visible if task_root.match(path))
        self.aliases = set()
        for path in declared:
            self.aliases.update(self._aliases(path))
        self.grams = set()
        for text in texts:
            self.grams.update(_shingles(text))
            for spelling in range(4):
                equivalent = _PUBLIC_PATH.sub(
                    lambda match: self._aliases(_path_spelling(match.group()))[spelling], text
                )
                self.grams.update(_shingles(equivalent))

    def _aliases(self, path):
        """Expand only this task's declared variant/input/output path syntax."""
        if not path:
            return (path,) * 4
        for root, variant in self.roots.items():
            if path.startswith(root + "/"):
                suffix = path[len(root) + 1:]
            elif path.startswith(variant + "/"):
                suffix = path[len(variant) + 1:]
            elif path.lstrip("/").startswith(("input/", "output/")):
                suffix = path.lstrip("/")
            else:
                continue
            short = suffix.split("/", 1)[1] if suffix.startswith(("input/", "output/")) else suffix
            return root + "/" + suffix, variant + "/" + suffix, suffix, short
        return (path,) * 4

    def occurrence_spans(self, text):
        paths = []
        for match in _PUBLIC_PATH.finditer(text):
            path = _path_spelling(match.group())
            if path and (path in self.aliases or any(
                    path == root or path.startswith(root + "/") for root in self.visible)):
                paths.append((match.start(), match.end()))
        # Only the syntactic venv/install prefix is ordinary setup. Package names,
        # trailing commands, and the same words elsewhere still face the audit.
        setup = [(match.start(), match.end()) for match in _LOCAL_VENV_INSTALL.finditer(text)]
        return paths, setup


class Audit:
    """Host-only public-instruction leakage audit; no grader constants or scores."""

    def __init__(self, corpus, public_task=None):
        self.path = Path(corpus)
        self.grams = set(json.loads(self.path.read_text()))
        if not self.grams or any(not isinstance(v, str) for v in self.grams):
            raise RuntimeError("ALE instruction corpus is missing or malformed")
        self.public_task = _PublicTaskText(public_task) if public_task is not None else None

    def validate(self, path):
        if (
            Path(path).resolve() != self.path.resolve()
            or set(json.loads(self.path.read_text())) != self.grams
        ):
            raise RuntimeError("ALE instruction corpus drift")
        return {"benchmark": "ALE", "shingles": len(self.grams)}

    def text(self, text, mode="practice", authorized_instruction="", **kwargs):
        from explore.commit import _N, _WORD, _shingles

        hits = []
        if re.search(
            r"github\.com[/\\]+rdi-berkeley[/\\]+agents-last-exam|ALE_REFERENCE_ARCHIVE_PASSWORD|secret[/\\]+eval_time",
            text,
            re.I,
        ):
            hits.append({"kind": "benchmark-private-source"})
        allowed = _shingles(authorized_instruction)
        binding = self.public_task if authorized_instruction else None
        if binding is not None:
            if authorized_instruction != binding.instruction:
                raise ValueError("ALE audit does not match its bound instruction")
            allowed |= binding.grams
        suspect = (_shingles(text) & self.grams) - allowed
        if binding is not None and suspect:
            paths, setup = binding.occurrence_spans(text)
            folded = text.lower()
            if len(folded) != len(text):
                # Unicode lowercasing can change offsets (for example İ).
                paths, setup = [
                    [(len(text[:start].lower()), len(text[:end].lower()))
                     for start, end in spans] for spans in (paths, setup)
                ]
            words = list(_WORD.finditer(folded))
            unexplained = False
            for index in range(len(words) - _N + 1):
                group = words[index:index + _N]
                if " ".join(word.group() for word in group) not in suspect:
                    continue
                if any(start <= group[0].start() and group[-1].end() <= end
                       for start, end in setup):
                    continue
                if all(any(start <= word.start() and word.end() <= end
                           for start, end in paths) for word in group):
                    continue
                unexplained = True
                break
            suspect = suspect if unexplained else set()
        if suspect:
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


def hooks_for(pool, corpus, public_task=None):
    from core.loop import run_attempt
    from explore.practice_loop import PracticeHooks

    audit = Audit(corpus, public_task=public_task)
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
