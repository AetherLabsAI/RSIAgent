#!/usr/bin/env python3
"""Behavioral contract for the agentic Verifier's effect isolation."""
import base64
import re
import subprocess
from types import SimpleNamespace

import pytest

from config.settings import Config
from core import loop as L
from core import verifier as V


def _runtime_trace(stdout="", exit_code=0, *, infra_fail=False,
                   context_stdout=None):
    return SimpleNamespace(
        stdout=stdout, exit_code=exit_code, secs=0.0,
        timed_out=False, infra_fail=infra_fail,
        context_stdout=context_stdout)


def _decoded_shell_payload(script, variable="payload"):
    match = re.search(
        rf"(?m)^{re.escape(variable)}='([A-Za-z0-9+/=]+)'$", script)
    assert match is not None, f"missing {variable} payload"
    return base64.b64decode(match.group(1)).decode("utf-8")


class _RecordingVM:
    """Emulate trusted wrappers without ever executing their shell source."""

    def __init__(self, program_output="sandbox program output"):
        self.env = SimpleNamespace(client_password="harness-only-password")
        self.commands = []
        self.scripts = []
        self.program_output = program_output
        self.fail_preflight = False
        self.fail_health = False

    def run_command(self, command, timeout=30, cap=4000):
        self.commands.append({
            "command": command, "timeout": timeout, "cap": cap})
        return ""

    def run_script(self, lang, code, timeout=600, cap=0,
                   allow_staging_fallback=False):
        self.scripts.append({
            "lang": lang, "code": code, "timeout": timeout, "cap": cap,
            "allow_staging_fallback": allow_staging_fallback})
        if "FORGE_VERIFIER_NAMESPACE_HEALTHY" in code:
            if self.fail_health:
                return _runtime_trace(
                    "nsenter: cannot open /proc/43210/ns/mnt\n[exit 1]", 1)
            return _runtime_trace(
                "FORGE_VERIFIER_NAMESPACE_HEALTHY\n[exit 0]", 0)
        if "FORGE_VERIFIER_NAMESPACE=" in code:
            if self.fail_preflight:
                return _runtime_trace(
                    "sudo: permission denied\n[exit 1]", 1,
                    infra_fail=False)
            return _runtime_trace(
                "FORGE_VERIFIER_NAMESPACE=43210:1000:1000\n[exit 0]", 0)
        match = re.search(
            r"__FORGE_VERIFIER_(?:SANDBOX|MIRROR)_READY_[0-9a-f]+__", code)
        if match:
            return _runtime_trace(
                match.group(0) + "\n" + self.program_output + "\n[exit 0]", 0)
        return _runtime_trace("direct program\n[exit 0]", 0)


def _verifier_cfg():
    cfg = Config()
    cfg.model = "verifier-model"
    cfg.vision_model = ""
    cfg.agent_decided_stop = True
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 100
    return cfg


def test_agentic_executor_runs_arbitrary_code_only_inside_effect_sandbox():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    model_code = (
        "open('/home/user/candidate', 'w').write('changed')\n"
        "import requests; requests.post('http://127.0.0.1:8000')\n"
        "import subprocess; subprocess.run(['pkill', '-f', 'soffice'])\n")

    trace = executor("python", model_code, timeout=75)

    assert trace.exit_code == 0
    assert "sandbox program output" in trace.stdout
    assert len(vm.scripts) == 2
    preflight, wrapper = vm.scripts
    assert preflight["lang"] == "bash" and wrapper["lang"] == "bash"
    assert model_code not in wrapper["code"]
    assert base64.b64encode(model_code.encode()).decode() in wrapper["code"]
    for boundary in (
        "--mount", "--pid", "--net", "--ipc", "--uts",
        "ip link set dev lo up",
        "findmnt -Rrn --raw", "remount,bind,ro", "tmpfs /tmp", "tmpfs /run",
        "--clear-groups", "--no-new-privs", "--bounding-set=-all",
        "VERIFIER_SCRATCH",
    ):
        assert boundary in wrapper["code"]
    assert all(call["cap"] == 0 for call in vm.scripts)
    assert all(call["allow_staging_fallback"] for call in vm.scripts)


def test_effect_sandbox_accepts_ready_token_preserved_outside_binary_context():
    from core.verifier_runtime import AgenticVerifierExecutor

    class ExternalizedVM(_RecordingVM):
        def run_script(self, lang, code, timeout=600, cap=0,
                       allow_staging_fallback=False):
            if "FORGE_VERIFIER_NAMESPACE=" in code:
                return super().run_script(
                    lang, code, timeout=timeout, cap=cap,
                    allow_staging_fallback=allow_staging_fallback)
            match = re.search(
                r"__FORGE_VERIFIER_SANDBOX_READY_[0-9a-f]+__", code)
            assert match is not None
            return _runtime_trace(
                "[FORGE NON-UTF8 PROGRAM OUTPUT — EXACT BASE64]\n[exit 0]",
                0,
                context_stdout=(match.group(0)
                                + "\n[PROGRAM OUTPUT EXTERNALIZED]\n[exit 0]"))

    trace = AgenticVerifierExecutor(ExternalizedVM())(
        "bash", "cat /usr/local/bin/shotcut")

    assert trace.exit_code == 0
    assert trace.infra_fail is False
    assert "SANDBOX_READY" not in trace.context_stdout
    assert "PROGRAM OUTPUT EXTERNALIZED" in trace.context_stdout


def test_agentic_executor_remains_compatible_with_already_loaded_legacy_vm():
    from core.verifier_runtime import AgenticVerifierExecutor

    class LegacyVM(_RecordingVM):
        def run_script(self, lang, code, timeout=600, cap=0):
            return super().run_script(lang, code, timeout=timeout, cap=cap)

    vm = LegacyVM()
    trace = AgenticVerifierExecutor(vm)("bash", "printf inspect")

    assert trace.exit_code == 0
    assert "sandbox program output" in trace.stdout
    assert len(vm.scripts) == 2
    assert all(not call["allow_staging_fallback"] for call in vm.scripts)


def test_agentic_executor_enables_only_private_loopback_before_privilege_drop():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm)(
        "bash", "python3 -m http.server 8765 & curl http://127.0.0.1:8765")

    wrapper = vm.scripts[-1]["code"]
    assert "--net" in wrapper
    assert "ip link set dev lo up" in wrapper
    assert wrapper.index("ip link set dev lo up") < wrapper.index("exec setpriv")
    # No veth, bridge, copied route, or host-network escape is provisioned.
    assert "nsenter --target 43210 --mount" in wrapper
    assert wrapper.index("nsenter --target 43210 --mount") < \
        wrapper.index("unshare")
    for forbidden in ("ip link add", "ip route add", "--net=/"):
        assert forbidden not in wrapper


def test_rollback_mirror_sees_live_namespaces_and_rolls_back(monkeypatch):
    from env import qemu_rollback
    from core.verifier_runtime import AgenticVerifierExecutor

    transactions = []

    class Transaction:
        def __init__(self, vm):
            self.vm = vm
            self.begun = False
            self.rolled_back = False
            transactions.append(self)

        def begin(self):
            self.begun = True

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(qemu_rollback, "QemuRollbackTransaction", Transaction)
    vm = _RecordingVM(program_output=(
        "candidate=ORIGINAL\nhttp=200\nserver_processes=1\n"))
    executor = AgenticVerifierExecutor(
        vm, execution_mode="rollback_mirror")

    trace = executor(
        "bash", "cat /candidate; curl http://127.0.0.1:8765; ps aux")
    executor.close()

    assert trace.exit_code == 0 and not trace.infra_fail
    assert transactions[0].begun and transactions[0].rolled_back
    wrapper = vm.scripts[-2]["code"]
    assert "__FORGE_VERIFIER_MIRROR_READY_" in wrapper
    assert "nsenter --target 43210 --mount" in wrapper
    assert "DISPLAY=:0" in wrapper
    assert "DBUS_SESSION_BUS_ADDRESS" in wrapper
    for absent in ("--pid", "--net", "--ipc", "--uts", "remount,bind,ro"):
        assert absent not in wrapper


def test_rollback_mirror_prompt_describes_observation_and_rollback():
    from core.verifier_runtime import agentic_verifier_system_for

    prompt = agentic_verifier_system_for("rollback_mirror")

    assert "transactional mirror of the exact live Actor" in prompt
    assert "processes, localhost services, network, GUI" in prompt
    assert "restores\nthat checkpoint before the candidate is graded" in prompt
    assert "processes, network, GUI, and IPC\nchannels are unavailable" not in prompt


def test_qemu_transaction_saves_restores_and_deletes_exact_snapshot():
    from env.qemu_rollback import QemuRollbackTransaction

    class Container:
        def __init__(self):
            self.snapshot = ""
            self.commands = []

        def exec_run(self, argv):
            command = argv[4]
            self.commands.append(command)
            if command.startswith("savevm "):
                self.snapshot = command.split(" ", 1)[1]
                output = "(qemu)\n(qemu)\n"
            elif command == "info snapshots":
                row = (f"--      {self.snapshot} 3.66 GiB 2026-09-03 "
                       "17:00:00 0000:00:01 --\n" if self.snapshot else "")
                output = "(qemu)\nList of snapshots present on all disks:\n" + row + "(qemu)\n"
            elif command.startswith("loadvm "):
                assert command.endswith(self.snapshot)
                output = "(qemu)\n(qemu)\n"
            elif command.startswith("delvm "):
                assert command.endswith(self.snapshot)
                self.snapshot = ""
                output = "(qemu)\n(qemu)\n"
            else:
                raise AssertionError(command)
            return SimpleNamespace(exit_code=0, output=output.encode())

    container = Container()
    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=container))

    class VM:
        env = environment

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"

    transaction = QemuRollbackTransaction(VM())
    transaction.begin()
    assert transaction.active
    transaction.rollback()

    assert transaction.restored and not transaction.active
    assert container.commands == [
        "savevm " + transaction.tag,
        "info snapshots",
        "loadvm " + transaction.tag,
        "delvm " + transaction.tag,
        "info snapshots",
    ]


def test_qemu_transaction_restores_browser_listeners_present_at_checkpoint(
        monkeypatch):
    from env import qemu_rollback

    responses = iter([
        "FORGE_BROWSER_LISTENERS_READY",
        # rollback(): a channel flap, missing listeners, then two ready probes.
        "[channel error: temporary guest controller outage]",
        "FORGE_BROWSER_LISTENERS_ABSENT",
        "FORGE_BROWSER_LISTENERS_READY",
        "FORGE_BROWSER_LISTENERS_READY",
    ])
    monkeypatch.setattr(qemu_rollback.time, "sleep", lambda _seconds: None)

    commands = []

    class Transaction(qemu_rollback.QemuRollbackTransaction):
        def _hmp(self, command, **_kwargs):
            commands.append(command)
            if command == "info snapshots":
                return (f"--      {self.tag} 3.66 GiB now 00:00:01 --\n"
                        if not any(c.startswith("delvm ") for c in commands)
                        else "")
            return "(qemu)\n(qemu)\n"

    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=object()),
        vm_ip="localhost",
        chromium_port=9222)

    class VM:
        env = environment

        @staticmethod
        def run_command(*_args, **_kwargs):
            return next(responses)

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"

    transaction = Transaction(VM())
    transaction.begin()
    assert transaction._required_interfaces == ("chromium_listeners",)
    transaction.rollback()
    assert transaction.restored


def test_qemu_transaction_does_not_require_browser_absent_at_checkpoint():
    from env import qemu_rollback

    class Transaction(qemu_rollback.QemuRollbackTransaction):
        def _hmp(self, command, **_kwargs):
            if command == "info snapshots":
                return (f"--      {self.tag} 3.66 GiB now 00:00:01 --\n"
                        if self.active else "")
            if command.startswith("delvm "):
                self.active = False
            return "(qemu)\n(qemu)\n"

    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=object()),
        vm_ip="localhost",
        chromium_port=9222)

    class VM:
        env = environment

        @staticmethod
        def run_command(*_args, **_kwargs):
            return "FORGE_BROWSER_LISTENERS_ABSENT"

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"

    transaction = Transaction(VM())
    transaction.begin()
    assert transaction._required_interfaces == ()
    transaction.rollback()
    assert transaction.restored


def test_agentic_executor_uses_separate_tmpfs_for_persistent_scratch():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    executor("bash", "printf evidence > \"$VERIFIER_SCRATCH/e.txt\"")

    preflight = vm.scripts[0]["code"]
    wrapper = vm.scripts[1]["code"]
    keeper = _decoded_shell_payload(preflight, "keeper_payload")
    assert executor.workspace.startswith("/mnt/forge_verifier_")
    assert "mount -t tmpfs" in keeper
    assert "nosuid,nodev" in keeper
    assert "install -d -m 0000 -o root -g root" in preflight
    assert "Verifier scratch leaked into the Actor-visible" in preflight
    assert "findmnt -T /home/user" in wrapper
    assert 'findmnt -T "$1"' in wrapper
    # A distinct tmpfs makes hard links back into the candidate filesystem fail
    # with EXDEV; a writable bind directory on the candidate filesystem would not.
    assert "forge-verifier-scratch" in keeper


def test_unified_verifier_mechanically_masks_actor_memory():
    from core.verifier_runtime import (
        AgenticVerifierExecutor,
        validate_verifier_look_path,
    )

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm, hide_actor_memory=True)(
        "bash", "find /home/user -maxdepth 2 -type f")

    wrapper = vm.scripts[-1]["code"]
    assert "mount -t tmpfs -o mode=000" in wrapper
    assert base64.b64encode(b"/home/user/.memory").decode() in wrapper
    assert validate_verifier_look_path(
        "/home/user/.memory/skill.md", hide_actor_memory=True)[0] is False
    assert validate_verifier_look_path(
        "/home/user/Desktop/../.memory/skill.md",
        hide_actor_memory=True)[0] is False
    assert validate_verifier_look_path(
        "/home/user/report.pptx", hide_actor_memory=True) == (True, "")


def test_unified_verifier_masks_additional_actor_private_paths():
    from core.verifier_runtime import (
        AgenticVerifierExecutor,
        validate_verifier_look_path,
    )

    vm = _RecordingVM()
    private = ("/home/user/work",)
    AgenticVerifierExecutor(vm, private_paths=private)(
        "bash", "find /home/user -type f")

    wrapper = vm.scripts[-1]["code"]
    assert base64.b64encode(b"/home/user/work").decode() in wrapper
    assert validate_verifier_look_path(
        "/home/user/work/actor_notes.txt",
        private_paths=private)[0] is False
    assert validate_verifier_look_path(
        "/home/user/Desktop/candidate.odt",
        private_paths=private) == (True, "")


def test_verifier_private_scratch_can_archive_and_restore_without_size_cap():
    from core.verifier_runtime import AgenticVerifierExecutor

    class TransferVM(_RecordingVM):
        def __init__(self):
            super().__init__()
            self.pushed = []

        def fetch_file(self, path, max_bytes=None):
            assert max_bytes is None
            assert path.startswith("/tmp/forge_verifier_export_")
            assert path.endswith(".tar")
            return b"lossless-private-scratch-archive", ""

        def push_file(self, local_path, guest_path):
            with open(local_path, "rb") as handle:
                self.pushed.append((guest_path, handle.read()))
            return True, ""

    first_vm = TransferVM()
    first = AgenticVerifierExecutor(first_vm)
    first("bash", "printf evidence > \"$VERIFIER_SCRATCH/evidence.txt\"")
    archive = first.export_scratch()
    export_wrapper = first_vm.scripts[-1]["code"]
    export_program = _decoded_shell_payload(export_wrapper)
    first.close()

    second_vm = TransferVM()
    second = AgenticVerifierExecutor(second_vm, scratch_archive=archive)
    trace = second("bash", "test -f \"$VERIFIER_SCRATCH/evidence.txt\"")

    assert trace.exit_code == 0
    assert second_vm.pushed[0][0].startswith(
        "/tmp/forge_verifier_restore_")
    assert second_vm.pushed[0][0].endswith(".tar")
    assert second_vm.pushed[0][1] == b"lossless-private-scratch-archive"
    assert "/tmp/forge_verifier_export_" in export_program
    assert first.workspace + "/.forge_export.tar" not in export_program
    assert all(call["cap"] == 0 for call in second_vm.scripts)


def test_verifier_export_falls_back_to_dev_shm_after_guest_root_is_read_only():
    from core.verifier_runtime import AgenticVerifierExecutor

    class ReadOnlyRootVM(_RecordingVM):
        def __init__(self):
            super().__init__()
            self.fetched = []

        def run_script(self, lang, code, timeout=600, cap=0,
                       allow_staging_fallback=False):
            if "FORGE_VERIFIER_NAMESPACE=" not in code:
                try:
                    payload = _decoded_shell_payload(code)
                except AssertionError:
                    payload = ""
                if "/tmp/forge_verifier_export_" in payload:
                    self.scripts.append({
                        "lang": lang, "code": code, "timeout": timeout,
                        "cap": cap,
                        "allow_staging_fallback": allow_staging_fallback,
                    })
                    return _runtime_trace(
                        "OSError: [Errno 30] Read-only file system: "
                        "'/tmp/forge_verifier_export_test.tar'\n[exit 1]", 1)
            return super().run_script(
                lang, code, timeout=timeout, cap=cap,
                allow_staging_fallback=allow_staging_fallback)

        def fetch_file(self, path, max_bytes=None):
            self.fetched.append((path, max_bytes))
            assert path.startswith("/dev/shm/forge_verifier_export_")
            assert max_bytes is None
            return b"lossless-emergency-archive", ""

    vm = ReadOnlyRootVM()
    executor = AgenticVerifierExecutor(vm)
    executor("bash", "printf evidence > \"$VERIFIER_SCRATCH/e.txt\"")

    archive = executor.export_scratch()

    assert archive == b"lossless-emergency-archive"
    export_payloads = [
        _decoded_shell_payload(call["code"])
        for call in vm.scripts
        if re.search(r"(?m)^payload='[A-Za-z0-9+/=]+'$", call["code"])
        and "forge_verifier_export_" in _decoded_shell_payload(call["code"])
    ]
    assert any("/tmp/forge_verifier_export_" in p for p in export_payloads)
    assert any("/dev/shm/forge_verifier_export_" in p for p in export_payloads)
    assert all(
        "/dev/shm/forge_verifier_host_" in call["code"]
        for call in vm.scripts
        if "forge_verifier_export_" in call["code"])


def test_agentic_executor_transports_observations_losslessly():
    from core.verifier_runtime import AgenticVerifierExecutor

    observation = "证据\n" + "x" * 500_000
    vm = _RecordingVM(program_output=observation)

    trace = AgenticVerifierExecutor(vm)("bash", "printf anything")

    assert observation in trace.stdout
    assert "bounded" not in trace.stdout
    assert "truncat" not in trace.stdout.lower()
    assert vm.scripts[-1]["cap"] == 0


def test_agentic_executor_treats_model_source_as_data_not_wrapper_source():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    injected = "'; sudo mount -o remount,rw /; touch /home/user/escaped; #"

    executor("bash", injected)

    wrapper = vm.scripts[-1]["code"]
    assert injected not in wrapper
    assert base64.b64encode(injected.encode()).decode() in wrapper


def test_agentic_executor_trusted_wrapper_is_valid_bash():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm)("bash", "printf inspected")

    checked = subprocess.run(
        ["bash", "-n"], input=vm.scripts[-1]["code"], text=True,
        capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr


def test_agentic_executor_fails_closed_when_namespace_preflight_fails():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    vm.fail_preflight = True
    trace = AgenticVerifierExecutor(vm)("python", "print('must not run')")

    assert trace.exit_code == 125
    assert trace.infra_fail is True
    assert "not executed" in trace.stdout
    assert len(vm.scripts) == 1
    assert "must not run" not in vm.scripts[0]["code"]


def test_agentic_executor_revalidates_cached_keeper_after_idle_and_fails_closed():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    assert executor("bash", "printf first").exit_code == 0
    vm.fail_health = True
    executor._last_namespace_check = 0.0

    trace = executor("bash", "printf must-not-run")

    assert trace.exit_code == 125 and trace.infra_fail is True
    assert "infrastructure, not candidate evidence" in trace.stdout
    assert "must-not-run" not in vm.scripts[-1]["code"]


def test_specialized_verifier_executor_infra_aborts_on_first_failure(
        monkeypatch, tmp_path):
    from core.trace import ArtifactSink

    cfg = _verifier_cfg()
    cfg.max_iters = 20
    cfg.script_timeout = 30
    calls = []

    def fake_chat(*args, **kwargs):
        calls.append((args, kwargs))
        return '{"program":{"lang":"bash","code":"printf inspect"}}'

    monkeypatch.setattr(L, "chat", fake_chat)
    result, _ = L.run_attempt(
        "verifier task", _RecordingVM(), cfg,
        ArtifactSink(str(tmp_path / "infra")),
        program_executor=lambda *args, **kwargs: _runtime_trace(
            "trusted namespace missing", 125, infra_fail=True))

    assert result.status == "infra"
    assert result.iters == 1
    assert len(calls) == 1


def test_verify_agentic_propagates_infrastructure_instead_of_unverified(
        monkeypatch, tmp_path):
    from core.verifier_runtime import AgenticVerifierInfrastructureError

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    monkeypatch.setattr(
        L, "run_attempt",
        lambda *args, **kwargs: (SimpleNamespace(
            status="infra", infra_pause_secs=0, infra_pauses=0), []))

    with pytest.raises(
            AgenticVerifierInfrastructureError,
            match="emergency/infrastructure status infra"):
        V.verify_agentic(
            "task", _RecordingVM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=None, turn_no=1, session=V.VerifierSession(), wall_budget=50)


def test_agentic_executor_rejects_unknown_language_without_guest_execution():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    trace = AgenticVerifierExecutor(vm)("powershell", "Remove-Item candidate")

    assert trace.exit_code == 126
    assert vm.scripts == []


def test_agentic_executor_publishes_report_only_to_host_harness():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    report = "# Independent evidence\nVERDICT: PASS\n"

    trace = executor("verifier-report", report)

    assert trace.exit_code == 0
    assert executor.published_report == report
    assert vm.scripts == [] and vm.commands == []


def test_agentic_executor_explains_rejected_report_without_losing_it():
    from core.verifier_runtime import AgenticVerifierExecutor

    executor = AgenticVerifierExecutor(_RecordingVM())
    executor.set_report_validator(
        lambda report: "ready" if report == "READY" else None,
        "a standalone READY line",
    )

    rejected = executor("verifier-report", "almost ready")
    assert rejected.exit_code == 2
    assert "NOT accepted" in rejected.stdout
    assert executor.published_report == "almost ready"

    accepted = executor("verifier-report", "READY")
    assert accepted.exit_code == 0


def test_agentic_executor_cleanup_unmounts_only_its_random_workspace():
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    executor("python", "print('inspect')")
    workspace = executor.workspace

    executor.close()
    executor.close()

    cleanup = vm.scripts[-1]["code"]
    assert cleanup.count(workspace) >= 1
    assert "kill -TERM" in cleanup and "rmdir" in cleanup
    assert "umount" in _decoded_shell_payload(
        vm.scripts[0]["code"], "keeper_payload")
    assert len(vm.scripts) == 3


@pytest.mark.parametrize("failed_step", ["archive_helper", "archive_fetch"])
@pytest.mark.parametrize("tolerate,rollback_fails", [
    (True, False), (False, False), (True, True),
])
def test_scratch_export_transport_failure_recovers_only_after_rollback(
        monkeypatch, failed_step, tolerate, rollback_fails):
    from env import qemu_rollback
    from core.verifier_runtime import (
        AgenticVerifierExecutor, AgenticVerifierInfrastructureError,
    )

    transactions = []

    class Transaction:
        def __init__(self, _vm):
            self.rolled_back = False
            transactions.append(self)

        def begin(self):
            pass

        def rollback(self):
            if rollback_fails:
                raise RuntimeError("rollback transport unavailable")
            self.rolled_back = True

    class VM(_RecordingVM):
        def fetch_file(self, *_args, **_kwargs):
            return None, "controller unavailable during archive fetch"

    monkeypatch.setattr(qemu_rollback, "QemuRollbackTransaction", Transaction)
    vm = VM()
    executor = AgenticVerifierExecutor(vm, execution_mode="rollback_mirror")
    assert executor("bash", "printf inspect").exit_code == 0
    monkeypatch.setattr(
        executor, "_run_in_private_mount_namespace",
        lambda *_a, **_kw: _runtime_trace(
            "controller unavailable during archive helper",
            125 if failed_step == "archive_helper" else 0,
            infra_fail=failed_step == "archive_helper"))
    session = V.VerifierSession()
    session._program_executor = executor

    if rollback_fails or not tolerate:
        reason = "rollback failed" if rollback_fails else "could not archive"
        with pytest.raises(AgenticVerifierInfrastructureError, match=reason):
            session.detach_executor(vm, tolerate_archive_failure=tolerate)
    else:
        session.detach_executor(vm, tolerate_archive_failure=True)

    assert transactions[0].rolled_back is (not rollback_fails)
    assert session._program_executor is None
    assert session._scratch_archive is None


def test_verifier_document_look_routes_renderer_into_isolated_scratch(
        monkeypatch):
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    observed = {}

    def fake_fetch_look_image(render_vm, path):
        observed["path"] = path
        observed["output"] = render_vm.run_command(
            "rm -rf /tmp/forge_render; mkdir -p /tmp/forge_render; "
            "printf /tmp/forge_render/page.png")
        return b"pixels", ""

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    data, reason = executor.fetch_look_image("/home/user/deck.pptx")

    assert data == b"pixels" and reason == ""
    assert observed["path"] == "/home/user/deck.pptx"
    wrapper = vm.scripts[-1]["code"]
    payload = re.search(r"^payload='([^']+)'$", wrapper, re.MULTILINE).group(1)
    render_program = base64.b64decode(payload).decode()
    assert "/tmp/forge_render" not in render_program
    assert executor.workspace + "/look_render" in render_program
    assert "--net" in wrapper and "remount,bind,ro" in wrapper


def test_verifier_private_scratch_look_uses_namespace_bridge_not_global_resolver(
        monkeypatch):
    from core.verifier_runtime import AgenticVerifierExecutor

    class BridgeVM(_RecordingVM):
        def __init__(self):
            super().__init__()
            self.fetched = []

        def fetch_file(self, path, max_bytes=None):
            self.fetched.append((path, max_bytes))
            assert path.startswith("/tmp/forge_verifier_fetch_")
            assert path.endswith(".bin")
            return b"private pixels", ""

    vm = BridgeVM()
    executor = AgenticVerifierExecutor(
        vm, private_paths=("/home/user/work",))
    private_image = executor.workspace + "/evidence/frame.png"

    def fake_fetch_look_image(render_vm, path):
        assert path == private_image
        return render_vm.fetch_file(path, max_bytes=None)

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    data, reason = executor.fetch_look_image(private_image)

    assert data == b"private pixels" and reason == ""
    assert len(vm.fetched) == 1
    # The Actor-visible mount namespace deliberately cannot resolve scratch.
    assert all("readlink -f" not in call["command"] for call in vm.commands)
    bridge_program = _decoded_shell_payload(vm.scripts[-1]["code"])
    assert private_image in bridge_program
    assert "resolved = source.resolve(strict=True)" in bridge_program
    assert "root not in resolved.parents" in bridge_program
    assert any(
        "rm -f -- /tmp/forge_verifier_fetch_" in call["command"]
        for call in vm.commands)


def test_verifier_screen_look_uses_only_trusted_capture_and_scratch(monkeypatch):
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    observed = {}

    def fake_fetch_look_image(passed_vm, path):
        observed.update(vm=passed_vm, path=path)
        observed["output"] = passed_vm.run_command(
            "mkdir -p /tmp/forge_render; printf /tmp/forge_render/screen.png")
        return b"screen pixels", ""

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    executor = AgenticVerifierExecutor(vm)
    data, reason = executor.fetch_look_image("screen:")

    assert data == b"screen pixels" and reason == ""
    assert observed["path"] == "screen:"
    assert observed["vm"] is not vm
    assert "/tmp/forge_verifier_screen_" in vm.commands[-1]["command"]
    assert "rm -rf --" in vm.commands[-1]["command"]
    assert executor.workspace not in vm.commands[-1]["command"]
    assert "/tmp/forge_render" not in vm.commands[-1]["command"]
    # Workspace setup is trusted; no model-authored Program ran for the capture.
    assert len(vm.scripts) == 1


def test_verifier_look_guard_allows_only_local_absolute_or_numeric_screen():
    from core.verifier_runtime import validate_verifier_look_path

    assert validate_verifier_look_path("/home/user/report.pptx") == (True, "")
    assert validate_verifier_look_path("screen:") == (True, "")
    assert validate_verifier_look_path("screen:1.0") == (True, "")
    for path in (
        "http://127.0.0.1:8000/report.pptx",
        "file:///home/user/report.pptx",
        "--headless.pptx",
        "relative/report.pdf",
        "screen:0; touch /tmp/escaped",
    ):
        allowed, reason = validate_verifier_look_path(path)
        assert allowed is False and reason


def test_verify_agentic_injects_effect_isolated_executor_and_host_report(
        monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _RecordingVM()
    observed = {}

    def fake_run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        observed["same_vm"] = passed_vm is vm
        allowed, _ = kwargs["look_path_validator"](
            "http://127.0.0.1:8000/report.pptx")
        assert allowed is False
        executor = kwargs["program_executor"]
        program = executor(
            "python", "open('/home/user/calendar.ics', 'w').write('bad')", 5)
        assert program.exit_code == 0
        executor(
            "verifier-report",
            "# Checked current candidate\nVERDICT: FAIL\n"
            "conflicting event remains\n",
            5,
        )
        assert kwargs["terminal_handoff_ready"]()
        history = list(kwargs.get("initial_history") or [])
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"published"}}'},
        ]
        return SimpleNamespace(
            status="done", infra_pause_secs=0, infra_pauses=0), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=None, turn_no=1, session=V.VerifierSession(), wall_budget=50)

    assert observed["same_vm"] is True
    assert verdict == "wrong"
    assert "conflicting event remains" in str(findings)
    assert any("--net" in call["code"] for call in vm.scripts)
    assert "kill -TERM" in vm.scripts[-1]["code"]


def test_verify_agentic_real_loop_accepts_python_then_host_report(monkeypatch):
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _RecordingVM(program_output="nine events\n")
    replies = iter((
        '{"program":{"lang":"python","code":"from pathlib import Path; '
        'print(Path(\'/home/user/calendar.txt\').read_text())"}}',
        '{"program":{"lang":"verifier-report","code":"# Evidence\\n'
        'Nine events were independently observed.\\nVERDICT: PASS\\n"}}',
    ))
    calls = []

    def fake_chat(model, system, user, **kwargs):
        calls.append({"system": system, "user": user})
        return next(replies)

    monkeypatch.setattr(L, "chat", fake_chat)

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=None, turn_no=2, session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass"
    assert "Nine events" in str(findings)
    assert all("verifier-read" not in call["system"] for call in calls)
    assert all("arbitrary Python/Bash" in call["user"] for call in calls)
    assert "remount,bind,ro" in vm.scripts[1]["code"]


def test_real_verifier_loop_blocks_repeated_empty_provider_replies(monkeypatch, tmp_path):
    from core.trace import ArtifactSink
    from core.verifier_runtime import AgenticVerifierNoProgressError

    cfg = _verifier_cfg()
    cfg.max_consec_dry = 2
    monkeypatch.setattr("config.settings.load", lambda _path: cfg)
    calls = []

    def empty_chat(*_args, **_kwargs):
        calls.append(1)
        assert len(calls) <= 6, "the real loop must stop after three empty segments"
        return ""

    monkeypatch.setattr(L, "chat", empty_chat)
    root = tmp_path / "run"
    with pytest.raises(AgenticVerifierNoProgressError):
        V.verify_agentic(
            "task", _RecordingVM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=ArtifactSink(str(root)), session=V.VerifierSession(), wall_budget=50)

    assert len(calls) == 6
    assert len(list(root.rglob("transcript.json"))) == 3
    assert not list(root.rglob("verify2.json"))


def test_actor_default_program_path_remains_writable(monkeypatch, tmp_path):
    from core.trace import ArtifactSink

    vm = _RecordingVM()
    cfg = _verifier_cfg()
    cfg.practice_done_requires = ""
    cfg.max_iters = 2
    cfg.max_consec_done = 2
    cfg.strict_one_action = False
    actor_source = "open('/tmp/actor-ok', 'w').write('x')"
    replies = iter((
        '{"program":{"lang":"python","code":"' + actor_source + '"}}',
        '{"done":null}',
    ))
    monkeypatch.setattr(L, "chat", lambda *args, **kwargs: next(replies))

    result, _ = L.run_attempt(
        "actor task", vm, cfg, ArtifactSink(str(tmp_path / "actor")))

    assert result.status == "done"
    assert len(vm.scripts) == 1
    assert vm.scripts[0]["lang"] == "python"
    assert vm.scripts[0]["code"] == actor_source


def test_verifier_program_artifacts_use_truthful_extensions(tmp_path):
    from core.trace import ArtifactSink

    sink = ArtifactSink(str(tmp_path / "run"))
    sink.save_program(1, "python", "print('inspect')")
    sink.save_program(2, "bash", "ffprobe candidate.mp4")
    sink.save_program(3, "verifier-report", "# Report\nVERDICT: PASS\n")

    assert (tmp_path / "run" / "iter_01" / "program.py").is_file()
    assert (tmp_path / "run" / "iter_02" / "program.sh").is_file()
    assert (tmp_path / "run" / "iter_03" / "program.md").is_file()
