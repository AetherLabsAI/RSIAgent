"""Lossless guest-file and run_script trailer transport."""
import base64
import re
from types import SimpleNamespace

import pytest

from core.actor import trace_message
from env.vm import VM as ForgeVM, _bound
from explore import e6_loop, e7_loop, e8_loop
from explore.e6_loop import _guest_cat, _strip_run_script_trailer
from tools import exam_fence


_NIGHT = """MODE: MILESTONE
TARGET: terminal-transport
REP: 1
BUDGET: 60
RATIONALE: reconstruct a small artifact from clean fixtures
Create the requested output file with the specified contents.
SUCCESS:
1. test -f /home/user/output.txt && echo PASS || echo FAIL"""

_PROJECT = f"""PROJECT: terminal-transport
NIGHTS: 2
Create a small output from the supplied generic fixture.
NIGHT: 1/2
{_NIGHT}
NIGHT: 2/2
{_NIGHT}
FINAL:
1. test -f /home/user/output.txt && echo PASS || echo FAIL"""

_E6_CARD = """INSTANCE
CLASS: zotero
RUNG: 1
RATIONALE: exercise a small local collection operation
Create a collection containing one locally chosen item.
SUCCESS:
1. test -f /home/user/item.txt && echo PASS || echo FAIL"""

_E8_CARD = """INSTANCE
CLASS: reconstruction
MODE: DRILL
TARGET: local-output
REP: 1
BUDGET: 10
RATIONALE: verify a clean reconstruction from a pinned fixture
Create the requested local output.
SUCCESS:
1. test -f /home/user/output.txt && echo PASS || echo FAIL
2. test -f /home/user/raw/source.txt && echo PASS || echo FAIL # GUARD"""


def test_vm_fetch_file_supports_explicit_unbounded_artifact_transfer():
    payload = b"artifact" * 600_000
    controller = SimpleNamespace(get_file=lambda _path: payload)
    vm = ForgeVM(SimpleNamespace(controller=controller))

    assert vm.fetch_file("/tmp/artifact") == (payload, "")
    assert vm.fetch_file("/tmp/artifact", max_bytes=None) == (payload, "")
    bounded, reason = vm.fetch_file("/tmp/artifact", max_bytes=8)
    assert bounded is None
    assert "limit 8" in reason


def test_actor_program_transport_is_lossless_by_default(monkeypatch):
    payload = "A" * 12000 + "MEMORY-MIDDLE-MUST-SURVIVE" + "Z" * 12000
    vm = ForgeVM(SimpleNamespace())
    seen = {}

    def fake_run_command(_command, timeout, cap):
        seen.update(timeout=timeout, cap=cap)
        return payload + "\n[exit 0]"

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "printf memory")

    assert seen["cap"] == 0
    assert payload in trace.stdout
    assert "MEMORY-MIDDLE-MUST-SURVIVE" in trace.stdout
    assert _bound(payload, 0) == payload


def test_program_transport_externalizes_non_utf8_bytes_from_context(
        monkeypatch):
    vm = ForgeVM(SimpleNamespace())
    ready = b"__FORGE_VERIFIER_SANDBOX_READY_a1b2c3__"
    raw = ready + b"\ntext-before\n\xc8\xff\x00\ntext-after"
    envelope = (
        "FORGE_RUN_OUTPUT_BASE64:"
        + base64.b64encode(raw).decode("ascii")
        + "\n[exit 0]")
    seen = {}

    def fake_run_command(command, timeout, cap):
        seen["command"] = command
        return envelope

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "cat /usr/local/bin/tool")

    assert "base64 -w0" in seen["command"]
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    digest = __import__("hashlib").sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    assert encoded in trace.stdout
    assert f"bytes={len(raw)} sha256={digest}" in trace.stdout
    assert trace.context_stdout is not None
    assert f"bytes={len(raw)} sha256={digest}" in trace.context_stdout
    assert encoded not in trace.context_stdout
    assert "re-inspect deliberately" in trace.context_stdout
    assert ready.decode("ascii") in trace.context_stdout
    rendered = trace_message(trace)
    assert encoded not in rendered
    assert "PROGRAM OUTPUT EXTERNALIZED" in rendered


def test_run_command_forwards_timeout_to_guest_server(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "success", "output": "ok", "error": ""}

    def fake_post(url, *, json, timeout):
        seen.update(url=url, payload=json, client_timeout=timeout)
        return Response()

    monkeypatch.setattr("env.vm.requests.post", fake_post)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = ForgeVM(SimpleNamespace(controller=controller))

    assert vm.run_command("sleep 1", timeout=600) == "ok"
    assert seen == {
        "url": "http://controller.invalid/execute",
        "payload": {"command": "sleep 1", "shell": True, "timeout": 600},
        "client_timeout": 600,
    }


def test_run_command_surfaces_guest_execution_failure(monkeypatch):
    class Response:
        status_code = 500

        @staticmethod
        def json():
            return {"status": "error", "message": "command timed out"}

    monkeypatch.setattr(
        "env.vm.requests.post", lambda *_args, **_kwargs: Response())
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = ForgeVM(SimpleNamespace(controller=controller))

    output = vm.run_command("sleep 999", timeout=45)

    assert output.startswith("[channel error: guest execute HTTP 500")
    assert "command timed out" in output


def test_controller_recovery_requires_stable_nonmutating_probes(monkeypatch):
    vm = ForgeVM(SimpleNamespace())
    replies = [
        "[channel error: ConnectionError — unavailable]",
        "FORGE_CONTROLLER_READY",
        "[channel error: ConnectionError — restart flap]",
        "FORGE_CONTROLLER_READY",
        "FORGE_CONTROLLER_READY",
    ]
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        return replies.pop(0)

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    monkeypatch.setattr("env.vm.time.sleep", lambda _seconds: None)

    recovered, report = vm.wait_for_controller(
        timeout=30, probe_interval=1, stable_probes=2)

    assert recovered is True
    assert len(calls) == 5
    assert all(call[0] == "printf FORGE_CONTROLLER_READY" for call in calls)
    assert "2 consecutive probes passed" in report


def test_actor_programs_use_unique_logs_and_force_kill_escalation(monkeypatch):
    vm = ForgeVM(SimpleNamespace())
    commands = []

    def fake_run_command(command, timeout, cap):
        commands.append(command)
        return "done\n[exit 0]"

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    vm.run_script("bash", "true", timeout=120)
    vm.run_script("python", "print('ok')", timeout=120)

    assert all("timeout --signal=TERM --kill-after=5s 90s" in command
               for command in commands)
    logs = [re.search(r"/tmp/forge_run_[0-9a-f]{24}\.log", command).group(0)
            for command in commands]
    assert logs[0] != logs[1]
    assert all("/tmp/forge_run.log" not in command for command in commands)


def test_large_program_streams_losslessly_instead_of_using_shell_argv(
        monkeypatch):
    payload = "BEGIN-LARGE-PROGRAM\n" + ("x = 1\n" * 20_000) + "END-LARGE-PROGRAM\n"
    vm = ForgeVM(SimpleNamespace())
    staged = {}
    commands = []

    def fake_push_file(local_path, guest_path, attempts=3, timeout=3600):
        with open(local_path, "rb") as source:
            staged["bytes"] = source.read()
        staged.update(guest_path=guest_path, attempts=attempts, timeout=timeout)
        return True, ""

    def fake_run_command(command, timeout, cap):
        commands.append(command)
        raw = b"large program completed"
        return ("FORGE_RUN_OUTPUT_BASE64:"
                + base64.b64encode(raw).decode("ascii")
                + "\n[exit 0]")

    monkeypatch.setattr(vm, "push_file", fake_push_file)
    monkeypatch.setattr(vm, "run_command", fake_run_command)

    trace = vm.run_script("python", payload, timeout=600)

    assert staged["bytes"] == payload.encode("utf-8")
    assert re.fullmatch(
        r"/dev/shm/forge_program_[0-9a-f]{24}\.py",
        staged["guest_path"])
    assert staged["timeout"] == 600
    assert len(commands) == 1
    assert "BEGIN-LARGE-PROGRAM" not in commands[0]
    assert base64.b64encode(payload.encode("utf-8")).decode("ascii") not in commands[0]
    assert staged["guest_path"] in commands[0]
    assert 'rm -f "$f" "$run_log"' in commands[0]
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    assert "large program completed" in trace.stdout


def test_large_program_staging_failure_is_explicit_infrastructure(monkeypatch):
    payload = "x = 1\n" * 20_000
    vm = ForgeVM(SimpleNamespace())
    monkeypatch.setattr(
        vm, "push_file",
        lambda *_args, **_kwargs: (False, "verified stream unavailable"))
    monkeypatch.setattr(
        vm, "run_command",
        lambda *_args, **_kwargs: pytest.fail("unstaged program was executed"))

    trace = vm.run_script("python", payload, timeout=600)

    assert trace.exit_code is None
    assert trace.infra_fail is True
    assert "program was not executed" in trace.stdout
    assert "verified stream unavailable" in trace.stdout


def test_actor_program_falls_back_to_dev_shm_and_captures_diagnostics(monkeypatch):
    vm = ForgeVM(SimpleNamespace())
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        if len(calls) == 1:
            return ("[FORGE STAGING FALLBACK: /tmp unavailable; using /dev/shm]\n"
                    "mktemp: /tmp: Read-only file system\n"
                    "program completed\n[exit 0]")
        return ("mount state:\n/dev/sda3 / ext4 ro,relatime\n"
                "/dev/shm tmpfs tmpfs rw,nosuid,nodev")

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script(
        "bash", "printf completed", allow_staging_fallback=True)

    assert "/dev/shm/forge_XXXXXX.sh" in calls[0][0]
    assert "/dev/shm/forge_run_" in calls[0][0]
    assert "findmnt -T /home/user" in calls[1][0]
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    assert "program completed" in trace.stdout
    assert "HARNESS FILESYSTEM DIAGNOSTIC" in trace.stdout
    assert trace.stdout.rstrip().endswith("[exit 0]")


def test_actor_program_marks_infra_only_when_both_staging_mounts_fail(monkeypatch):
    vm = ForgeVM(SimpleNamespace())

    monkeypatch.setattr(
        vm, "run_command",
        lambda *_args, **_kwargs: (
            "[FORGE STAGING UNAVAILABLE: /tmp and /dev/shm both failed]\n"
            "/tmp: Read-only file system\n/dev/shm: No space left on device"))

    trace = vm.run_script(
        "python", "print('never executed')", allow_staging_fallback=True)

    assert trace.exit_code is None
    assert trace.infra_fail is True


def test_program_output_cannot_forge_a_transport_failure(monkeypatch):
    """A live-process listing may quote wrapper sentinels as ordinary evidence."""
    vm = ForgeVM(SimpleNamespace())
    program_output = (
        "ps: wrapper contains "
        "[FORGE STAGING UNAVAILABLE: /tmp and /dev/shm both failed] "
        "and mktemp: Read-only file system")
    envelope = (
        "FORGE_RUN_OUTPUT_BASE64:"
        + base64.b64encode(program_output.encode("utf-8")).decode("ascii")
        + "\n[exit 0]")
    monkeypatch.setattr(
        vm, "run_command", lambda *_args, **_kwargs: envelope)

    trace = vm.run_script(
        "bash", "ps aux", allow_staging_fallback=True)

    assert trace.exit_code == 0
    assert trace.infra_fail is False
    assert program_output in trace.stdout


def test_http_timeout_recovers_only_its_unique_partial_log(monkeypatch):
    vm = ForgeVM(SimpleNamespace())
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        if len(calls) == 1:
            return "[command timed out after 120s]"
        if len(calls) == 2:
            return "isolated partial output"
        return ""

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "sleep 999", timeout=120)

    match = re.search(r"/tmp/forge_run_[0-9a-f]{24}\.log", calls[0][0])
    assert match is not None
    run_log = match.group(0)
    assert calls[1][1:] == (30, 0)
    assert f"base64 -w0 {run_log}" in calls[1][0]
    assert "FORGE_RUN_OUTPUT_BASE64:" in calls[1][0]
    assert calls[2] == (f"rm -f {run_log}", 30, 1000)
    assert trace.timed_out is True
    assert "isolated partial output" in trace.stdout


def test_vm_push_file_streams_through_osworld_setup_endpoint(
        tmp_path, monkeypatch):
    payload = b"large-in-principle-artifact" * 64
    source = tmp_path / "candidate.tgz"
    source.write_bytes(payload)
    seen = {}

    def fake_post(url, *, headers, data, timeout):
        seen.update(url=url, headers=headers, timeout=timeout,
                    body=data.to_string())
        return SimpleNamespace(status_code=200, text="uploaded")

    monkeypatch.setattr("env.vm.requests.post", fake_post)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = ForgeVM(SimpleNamespace(controller=controller))

    assert vm.push_file(str(source), "/tmp/candidate.tgz") == (True, "")
    assert seen["url"] == "http://controller.invalid/setup/upload"
    assert seen["timeout"] == (10, 3600)
    assert seen["headers"]["Content-Type"].startswith(
        "multipart/form-data; boundary=")
    assert payload in seen["body"]
    assert b'/tmp/candidate.tgz' in seen["body"]


def test_vm_push_file_prefers_verified_direct_stream_on_docker(
        tmp_path, monkeypatch):
    source = tmp_path / "candidate.tgz"
    source.write_bytes(b"candidate bytes")
    container = SimpleNamespace(attrs={
        "NetworkSettings": {
            "IPAddress": "172.17.0.42",
            "Networks": {},
        }})
    provider = SimpleNamespace(container=container)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = ForgeVM(SimpleNamespace(
        provider_name="docker", provider=provider, controller=controller))
    calls = []

    def fake_direct(local_path, guest_path, docker_ip, timeout):
        calls.append((local_path, guest_path, docker_ip, timeout))
        return True, ""

    monkeypatch.setattr(vm, "_push_file_direct_docker", fake_direct)
    monkeypatch.setattr(
        "env.vm.requests.post",
        lambda *_args, **_kwargs: pytest.fail("multipart fallback was used"))

    assert vm.push_file(str(source), "/tmp/candidate.tgz") == (True, "")
    assert calls == [(
        str(source), "/tmp/candidate.tgz", "172.17.0.42", 3600)]


def test_direct_stream_stages_atomic_part_on_destination_filesystem():
    from env.vm import _atomic_guest_part_path

    assert _atomic_guest_part_path(
        "/dev/shm/forge_program_abc.sh", "123") == (
            "/dev/shm/.forge_program_abc.sh.forge-part-123")
    assert _atomic_guest_part_path(
        "/tmp/candidate.tgz", "456") == (
            "/tmp/.candidate.tgz.forge-part-456")
    assert _atomic_guest_part_path("/", "789") == ""


@pytest.mark.parametrize(("stdout", "payload"), [
    ("echo FAIL[exit 0]", "echo FAIL"),
    ("echo FAIL\n[exit 0]\n", "echo FAIL"),
    ("failed command[exit 17]", "failed command"),
    ("before [exit 4]\nafter[exit 0]", "before [exit 4]\nafter"),
    ("legitimate terminal [exit 7][exit 0]",
     "legitimate terminal [exit 7]"),
    ("prefix[exit 0]suffix", "prefix[exit 0]suffix"),
    ("not numeric [exit x]", "not numeric [exit x]"),
    ("[exit 124]", ""),
])
def test_strip_run_script_trailer_removes_only_one_terminal_transport(
        stdout, payload):
    assert _strip_run_script_trailer(stdout) == payload


def test_guest_cat_handles_trailer_joined_to_file_without_final_newline():
    class VM:
        def run_script(self, lang, code):
            assert lang == "bash"
            assert code == "cat ~/instance_next.md 2>/dev/null"
            return SimpleNamespace(stdout="FINAL:\n9. echo FAIL[exit 0]")

    assert _guest_cat(VM(), "~/instance_next.md") == \
        "FINAL:\n9. echo FAIL"


def test_terminal_transport_suffix_is_correctable_card_validation_error(
        monkeypatch):
    monkeypatch.setattr(exam_fence, "audit_text",
                        lambda *_args, **_kwargs: [])
    monkeypatch.setattr(e8_loop, "audit_text",
                        lambda *_args, **_kwargs: [])

    project = e7_loop._validate_project_transport(
        _PROJECT + "[exit 0]", "terminal-transport", 2)
    night = e7_loop._validate_night_card(_NIGHT + "[exit 9]")
    drill = e8_loop.validate_card(_E8_CARD + "[exit 2]")

    assert "project: terminal [exit N] transport trailer" in \
        project["reasons"]
    assert "transport: terminal [exit N] trailer" in night["reasons"]
    assert "transport: terminal [exit N] trailer" in drill["reasons"]
    assert e6_loop.accept_card(_E6_CARD + "[exit 3]")["status"] == \
        "malformed"


def test_interior_exit_literal_is_not_a_transport_validation_error(
        monkeypatch):
    monkeypatch.setattr(exam_fence, "audit_text",
                        lambda *_args, **_kwargs: [])
    project = _PROJECT.replace(
        "supplied generic fixture.",
        "supplied generic fixture; preserve the literal [exit 7] label.")

    checked = e7_loop._validate_project_transport(
        project, "terminal-transport", 2)

    assert not any("terminal [exit N]" in reason
                   for reason in checked["reasons"])


class _GateVM:
    def run_command(self, _command, **_kwargs):
        return ""

    def run_script(self, _lang, code, **_kwargs):
        output = "PASS" if "fixture_guard" in code else "FAIL"
        return SimpleNamespace(stdout=output + "[exit 0]")


def test_e7_gate_uses_shared_joined_trailer_parser():
    result = e7_loop.run_dry_gate(
        _GateVM(), ["1. test -f /home/user/output && echo PASS || echo FAIL"])

    assert result["accept"] is True
    assert result["per_criterion"][0]["run1"] == "FAIL"
    assert result["per_criterion"][0]["run2"] == "FAIL"


def test_e8_gate_uses_shared_joined_trailer_parser():
    result = e8_loop.run_dry_gate(_GateVM(), [
        "1. test -f /home/user/output && echo PASS || echo FAIL",
        "2. fixture_guard && echo PASS || echo FAIL # GUARD",
    ])

    assert result["accept"] is True
    assert result["per_criterion"][0]["run1"] == "FAIL"
    assert result["per_criterion"][1]["run1"] == "PASS"
