"""Rollback readiness must not create browser traffic or hide lost listeners."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from env import qemu_rollback as rollback


@pytest.mark.parametrize(
    "ports,browser_args,expected",
    [
        ([9222, 1337], ["chrome", "--remote-debugging-port=1337"], True),
        ([9222, 1337], ["/opt/google/chrome/chrome --remote-debugging-port=1337"], True),
        ([9222, 1337], ["/opt/google/chrome/chrome --type=renderer --remote-debugging-port=1337"], False),
        ([9222, 1337], ["/opt/google/chrome/chrome --remote-debugging-port='"], False),
        ([9222], ["chromium", "--remote-debugging-port=9222"], True),
        ([9222, 3456], ["google-chrome", "--remote-debugging-port", "3456"], True),
        ([1337], ["chrome", "--remote-debugging-port=1337"], False),
        ([9222], ["chrome", "--remote-debugging-port=1337"], False),
        ([9222, 1337], ["chrome", "--remote-debugging-port=0"], False),
        ([9222, 1337], ["chrome", "--remote-debugging-port=invalid"], False),
        ([9222, 1337], ["chrome", "--type=renderer", "--remote-debugging-port=1337"], False),
        ([9222, 1337], ["python3", "--remote-debugging-port=1337"], False),
        ([], ["chrome", "--remote-debugging-port=1337"], False),
    ],
)
def test_passive_probe_reads_listeners_without_network(
        tmp_path, monkeypatch, capsys, ports, browser_args, expected):
    import pathlib
    import socket

    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "123").mkdir()
    # Put the browser on IPv6 and the forwarder on IPv4. Established sockets
    # must not count as listeners.
    for table, selected in [("tcp", ports[:1]), ("tcp6", ports[1:])]:
        rows = [f"0: 00000000:{port:04X} 00000000:0000 0A" for port in selected]
        rows.append("1: 00000000:2406 00000000:0000 01")
        (proc / "net" / table).write_text("header\n" + "\n".join(rows))
    (proc / "123" / "cmdline").write_bytes("\0".join(browser_args).encode())
    (proc / "124").mkdir()  # A process may disappear while /proc is scanned.

    path_type = type(tmp_path)
    monkeypatch.setattr(pathlib, "Path", lambda value: proc if value == "/proc" else path_type(value))

    def forbidden_connection(*_args, **_kwargs):
        pytest.fail("A readiness probe must not contact a browser endpoint")

    monkeypatch.setattr(socket, "socket", forbidden_connection)
    script = rollback._BROWSER_LISTENER_PROBE.split("\n", 1)[1].rsplit("\n", 1)[0]
    exec(compile(script, "<passive-browser-listeners>", "exec"), {})
    wanted = "READY" if expected else "ABSENT"
    assert capsys.readouterr().out.strip() == "RSIAGENT_BROWSER_LISTENERS_" + wanted


@pytest.mark.parametrize("reply", ["", "[channel error: HTTP 500]", "READY", "RSIAGENT_BROWSER_LISTENERS_READY\n[stderr] error"])
def test_unknown_probe_result_cannot_silently_disable_recovery(reply):
    vm = SimpleNamespace(
        env=SimpleNamespace(vm_ip="localhost", chromium_port=9234),
        run_command=lambda *_args, **_kwargs: reply,
    )
    transaction = rollback.QemuRollbackTransaction(vm)
    with pytest.raises(rollback.QemuRollbackError, match="did not complete"):
        transaction.begin()
    assert not transaction.active


def test_lost_browser_listener_keeps_checkpoint_and_blocks_release(monkeypatch):
    calls = []
    ticks = iter(index / 10 for index in range(100))
    monkeypatch.setattr(rollback.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rollback.time, "sleep", lambda _seconds: None)
    replies = iter(["RSIAGENT_BROWSER_LISTENERS_READY"] + ["RSIAGENT_BROWSER_LISTENERS_ABSENT"] * 10)
    vm = SimpleNamespace(
        env=SimpleNamespace(vm_ip="localhost", chromium_port=9234),
        run_command=lambda *_args, **_kwargs: next(replies),
        wait_for_controller=lambda **_kwargs: (True, "ready"),
    )

    class Transaction(rollback.QemuRollbackTransaction):
        def _hmp(self, command, **_kwargs):
            calls.append(command)
            return f"-- {self.tag} snapshot" if command == "info snapshots" else ""

        def _wait_for_required_interfaces(self, **_kwargs):
            return super()._wait_for_required_interfaces(timeout=0.5, probe_interval=0)

    transaction = Transaction(vm)
    transaction.begin()
    with pytest.raises(rollback.QemuRollbackError, match="did not recover"):
        transaction.rollback()
    assert transaction.active and not transaction.restored
    assert not any(command.startswith("delvm ") for command in calls)
