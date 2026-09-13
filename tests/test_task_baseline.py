"""Contracts for the trusted pre-Actor baseline capture."""

import json
from types import SimpleNamespace

from core.task_baseline import (
    capture_task_start_baseline,
    stable_task_start_identity,
)


def test_task_start_baseline_is_lossless_mechanical_manifest():
    entries = [
        {
            "path": "/home/user/Desktop/input.bin",
            "type": "file",
            "mode": 420,
            "uid": 1000,
            "gid": 1000,
            "size": 10_000_000_000,
            "mtime_ns": 123,
            "sha256": "a" * 64,
        },
        {
            "path": "/home/user/Desktop",
            "type": "directory",
            "mode": 493,
            "uid": 1000,
            "gid": 1000,
            "size": 4096,
            "mtime_ns": 122,
        },
    ]
    payload = json.dumps({"entries": entries}, separators=(",", ":"))

    class VM:
        def run_script(self, lang, code, timeout=0, cap=1):
            assert lang == "python"
            assert timeout == 3600
            assert cap == 0
            assert "/home/user/.memory" in code
            return SimpleNamespace(
                stdout=("__FORGE_TASK_BASELINE_BEGIN__\n" + payload
                        + "\n__FORGE_TASK_BASELINE_END__\n[exit 0]"),
                exit_code=0,
                infra_fail=False,
            )

    baseline = capture_task_start_baseline(VM())

    assert baseline["entries"] == entries
    assert baseline["entry_count"] == 2
    assert len(baseline["content_sha256"]) == 64
    assert len(baseline["exact_sha256"]) == 64
    assert baseline["external_service_state"].startswith("unavailable")
    serialized = json.dumps(baseline)
    assert "grader" not in serialized and "golden" not in serialized


def test_task_start_baseline_fails_closed_on_incomplete_transport():
    class VM:
        def run_script(self, *_args, **_kwargs):
            return SimpleNamespace(
                stdout="partial manifest", exit_code=0, infra_fail=False)

    try:
        capture_task_start_baseline(VM())
    except RuntimeError as exc:
        assert "no complete manifest" in str(exc)
    else:
        raise AssertionError("incomplete baseline transport was accepted")


def test_stable_s0_identity_ignores_volatile_home_audit_state_by_construction():
    fields = {
        "task_id": "task_080",
        "instruction": "repair the supplied workbook",
        "setup_projection": "reviewed-setup-v1",
        "setup_manifest": {"asset_sha256": {"book.xlsx": "a" * 64}},
        "target_input_fingerprint": (
            "/home/user/Desktop/book.xlsx  " + "a" * 64 + "\n"),
    }

    first = stable_task_start_identity(**fields)
    second = stable_task_start_identity(**dict(fields))

    assert first == second
    assert len(first["sha256"]) == 64
    assert "mtime" not in json.dumps(first).lower()


def test_stable_s0_identity_changes_when_task_visible_input_changes():
    fields = {
        "task_id": "task_080",
        "instruction": "repair the supplied workbook",
        "setup_projection": "reviewed-setup-v1",
        "setup_manifest": {"asset_sha256": {"book.xlsx": "a" * 64}},
        "target_input_fingerprint": (
            "/home/user/Desktop/book.xlsx  " + "a" * 64 + "\n"),
    }
    original = stable_task_start_identity(**fields)
    fields["target_input_fingerprint"] = (
        "/home/user/Desktop/book.xlsx  " + "b" * 64 + "\n")
    changed = stable_task_start_identity(**fields)

    assert original["sha256"] != changed["sha256"]
