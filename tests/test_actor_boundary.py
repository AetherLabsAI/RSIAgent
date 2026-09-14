"""Pre-Actor evaluator-source isolation validity boundary."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.osworld.actor_boundary import (
    _encoded_paths_payload,
    _scan_records,
    quarantine_actor_visible_evaluators,
)
from env.vm import Trace


def test_scan_records_accepts_lossless_metadata_and_rejects_other_output():
    record = (
        '{"bytes": 17, "path": "/home/user/cache/task_174.py", '
        '"sha256": "' + "a" * 64 + '"}\n[exit 0]\n')
    assert _scan_records(record) == [{
        "bytes": 17,
        "path": "/home/user/cache/task_174.py",
        "sha256": "a" * 64,
    }]
    with pytest.raises(RuntimeError):
        _scan_records("arbitrary scan chatter\n")


def test_quarantine_path_payload_keeps_the_last_record():
    payload = _encoded_paths_payload([
        {"path": "/home/user/a.py"},
        {"path": "/home/user/b.py"},
    ])
    assert payload.endswith("\n")
    assert len(payload.splitlines()) == 2


def test_detected_evaluator_is_quarantined_and_boundary_is_rechecked():
    record = (
        '{"bytes": 17, "path": "/home/user/cache/task_174.py", '
        '"sha256": "' + "b" * 64 + '"}\n[exit 0]\n')

    class _VM:
        env = SimpleNamespace(client_password="guest-password")

        def __init__(self):
            self.calls = []

        def run_script(self, lang, code, timeout=600, cap=0):
            self.calls.append((lang, code, cap))
            if len(self.calls) == 1:
                return Trace(stdout=record, exit_code=0)
            if len(self.calls) == 2:
                return Trace(
                    stdout=".\n__RSIAGENT_EVALUATOR_QUARANTINE_READY__\n[exit 0]",
                    exit_code=0)
            return Trace(stdout="[exit 0]", exit_code=0)

    vm = _VM()
    isolated = quarantine_actor_visible_evaluators(vm)

    assert isolated[0]["path"] == "/home/user/cache/task_174.py"
    assert len(vm.calls) == 3
    assert vm.calls[0][0] == vm.calls[2][0] == "python"
    assert "desktop_env.task_base" in vm.calls[0][1]
    assert "/root/.rsiagent_actor_evaluator_quarantine" in vm.calls[1][1]
    assert all(call[2] == 0 for call in vm.calls)


def test_self_evolving_runner_isolates_before_s0_and_orientation():
    source = (Path(__file__).resolve().parents[1] /
              "benchmarks/osworld/phase2.py").read_text(encoding="utf-8")
    isolation = source.index("quarantine_actor_visible_evaluators(vm)")
    surface_s0 = source.index("baseline = _snapshot", isolation)
    orientation = source.index("def verifier_orient(", surface_s0)
    assert isolation < surface_s0 < orientation
