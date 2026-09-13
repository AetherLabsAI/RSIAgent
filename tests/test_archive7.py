"""E7 curriculum archive (explore/archive7.py + the commit.py transport +
the P6 absence proof) — PREREG E7 v2.2 §4 N3/M7.

Pins: the allowlist admits exactly the clean artifact classes; quarantine
residue and episode transcripts stay out by construction; a fence-hit file
is excluded AND logged without reproducing the matched text (the log rides
in the pushed dir); push_dir/rm_guest_dir speak the push_memory transport
dialect; assert_no_archive accepts only an explicit absence proof.
"""
import json
import os
import sys
import pytest

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from explore import archive7 as AR                    # noqa: E402
from explore import commit as mem                     # noqa: E402
from tools.e6_audit import assert_no_archive          # noqa: E402
from tools import exam_fence

CLEAN = ("night went fine, the crossfade rendered and the second clip "
         "held its audio through the cut, escalate the mask work next")


@pytest.fixture(autouse=True)
def host_only_audit_fixture(monkeypatch):
    # Exercise real path/URL rejection without reading external benchmark files.
    monkeypatch.setattr(exam_fence, "_GRAMS_CACHE", set())
    monkeypatch.setattr(exam_fence, "_load_constants", lambda: {})


def _era(tmp_path):
    c = tmp_path / "curriculum"; c.mkdir()
    v = tmp_path / "verdicts"; v.mkdir()
    e = tmp_path / "episodes" / "ep001"; e.mkdir(parents=True)
    (c / "journal_ep001_notes.md").write_text(CLEAN)
    (c / "journal_ep001_instance.md").write_text("TARGET: xfade\n" + CLEAN)
    (c / "curriculum_notes.md").write_text(CLEAN)
    (v / "ep001.txt").write_text("outcome: pass\n" + CLEAN)
    # never-archivable residue (N3)
    (c / "curriculum_notes.md.REJECTED").write_text("see OSWorld-V2 grader")
    (c / "instance_cur_shotcut.md.VOID").write_text("void card")
    (c / "instance_cur_shotcut.md").write_text("live card, not a journal")
    (tmp_path / "audit_rejects.jsonl").write_text('{"file": "x"}\n')
    (e / "transcript.md").write_text("episode transcript " + CLEAN)
    return tmp_path


def test_allowlist_in_residue_out(tmp_path):
    root = _era(tmp_path)
    out_dir = tmp_path / "_archive"
    r = AR.build_curriculum_archive(str(root), str(out_dir))
    assert sorted(r["files"]) == [
        "curriculum/curriculum_notes.md",
        "curriculum/journal_ep001_instance.md",
        "curriculum/journal_ep001_notes.md",
        "verdicts/ep001.txt"]
    assert r["excluded"] == [] and r["bytes"] > 0
    staged = [os.path.relpath(os.path.join(dp, fn), out_dir)
              for dp, _, fns in os.walk(out_dir) for fn in fns]
    assert sorted(staged) == sorted(r["files"])       # nothing extra staged
    assert not (out_dir / "ARCHIVE_EXCLUSIONS.log").exists()


def test_audit_hit_excluded_and_logged_sanitized(tmp_path):
    root = _era(tmp_path)
    dirty = root / "curriculum" / "journal_ep002_instance.md"
    dirty.write_text("mine /home/sibo/OSWorld-V2/evaluation_examples for it")
    out_dir = tmp_path / "_archive"
    r = AR.build_curriculum_archive(str(root), str(out_dir))
    assert r["excluded"] == ["curriculum/journal_ep002_instance.md"]
    assert "curriculum/journal_ep002_instance.md" not in r["files"]
    assert not (out_dir / "curriculum" /
                "journal_ep002_instance.md").exists()
    rec = json.loads((out_dir / "ARCHIVE_EXCLUSIONS.log").read_text())
    assert rec["file"] == "curriculum/journal_ep002_instance.md"
    assert rec["reason"] == "content-boundary"
    assert "hits" not in rec and "seat" not in rec
    # the log is pushed with the archive — it must never quote the leak
    assert "OSWorld-V2" not in (out_dir / "ARCHIVE_EXCLUSIONS.log").read_text()


def test_rebuild_removes_a_now_rejected_stale_stage_file(tmp_path):
    root = _era(tmp_path)
    out_dir = tmp_path / "_archive"
    AR.build_curriculum_archive(str(root), str(out_dir))
    staged = out_dir / "curriculum" / "curriculum_notes.md"
    assert staged.exists()

    (root / "curriculum" / "curriculum_notes.md").write_text(
        "mine /home/sibo/OSWorld-V2/evaluation_examples")
    result = AR.build_curriculum_archive(str(root), str(out_dir))

    assert "curriculum/curriculum_notes.md" in result["excluded"]
    assert not staged.exists()


class FakeVM:
    def __init__(self, absent_out="ls: cannot access '/home/user/"
                                   "curriculum_archive': No such file or "
                                   "directory"):
        self.cmds, self.absent_out = [], absent_out

    def run_command(self, cmd, timeout=30, **kw):
        self.cmds.append(cmd)
        if "SYNC_RC" in cmd:
            return "SYNC_RC=0\njournal_ep001_notes.md"
        if cmd.startswith("rm -rf") and "; ls " in cmd:
            return self.absent_out
        return ""


def test_push_dir_transport_dialect(tmp_path):
    host = tmp_path / "stage"; host.mkdir()
    (host / "a.md").write_text(CLEAN)
    vm = FakeVM()
    assert mem.push_dir(vm, str(host), "~/curriculum_archive")
    assert vm.cmds[0].startswith("rm -rf ~/curriculum_archive")
    assert "mkdir -p ~/curriculum_archive" in vm.cmds[0]
    assert all("/tmp/dir_in.b64" in c for c in vm.cmds[1:-1])  # chunked b64
    assert "tar xzf" in vm.cmds[-1] and "~/curriculum_archive" in vm.cmds[-1]
    assert not any("/tmp/mem_in" in c for c in vm.cmds)  # no push_memory clash


def test_rm_guest_dir_returns_absence_proof():
    vm = FakeVM()
    out = mem.rm_guest_dir(vm, "~/curriculum_archive")
    assert "No such file" in out                       # the loggable proof
    assert assert_no_archive(out) is True


def test_assert_no_archive_judges_strictly():
    assert assert_no_archive(
        "ls: cannot access '/home/user/curriculum_archive': "
        "No such file or directory") is True
    assert assert_no_archive(["ls: cannot access '~/curriculum_archive': "
                              "No such file or directory"]) is True
    assert assert_no_archive("journal_ep001_notes.md\nverdicts") is False
    assert assert_no_archive("") is False              # empty dir != absent
    assert assert_no_archive(
        "No such file or directory\njournal_ep001_notes.md") is False
