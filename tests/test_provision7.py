"""E7 provisioning capture/replay (explore/provision7.py) — PREREG E7 §3 B7.

The mock VM emulates the guest with real tar/base64/sha256 semantics over an
in-memory file dict and CAPTURES the pushed bytes, so the round-trip proves
byte-verbatim transport, and a corrupted byte proves the in-guest verify
catches infra rot (-> provision_failed at the loop, never re-generation).
"""
import base64
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import pytest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from explore import provision7 as P                    # noqa: E402
from env.vm import _bound

_PRINTF = re.compile(r"printf %s '([^']*)' >> /tmp/proj_in\.b64")
_CAPTURE = re.compile(r"tar czf /tmp/proj_materials\.tgz -C /home/user (.+?) "
                      r"2>/dev/null")
_SHASUM = re.compile(r"cd /home/user && sha256sum (.+?) 2>&1")


def _tgz_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _GuestVM:
    """Mock guest: {relpath under /home/user: bytes} + real archive math."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.b64 = ""                      # accumulating pushed b64 chunks
        self.tar_out = b""                 # last capture tar built in-guest
        self.pushed_tgz = b""              # decoded bytes replay extracted
        self.cmds = []

    def run_command(self, cmd, timeout=None, cap=4000):
        self.cmds.append(cmd)
        m = _CAPTURE.search(cmd)
        if m:
            dirs = shlex.split(m.group(1))
            sub = {rel: d for rel, d in self.files.items()
                   if rel.split("/", 1)[0] in dirs}
            if not sub:
                return "TAR_RC=2"
            self.tar_out = _tgz_of(sub)
            return "TAR_RC=0"
        m = _PRINTF.search(cmd)
        if m:
            self.b64 += m.group(1)
            return ""
        if cmd.startswith("rm -rf"):
            for tgt in shlex.split(cmd)[2:]:
                if tgt.startswith("/home/user/"):
                    rel = tgt[len("/home/user/"):]
                    for k in [k for k in self.files
                              if k == rel or k.startswith(rel + "/")]:
                        del self.files[k]
            self.b64 = ""
            return ("PROJECT_WIPE_RC=0"
                    if "PROJECT_WIPE_RC" in cmd else "")
        if cmd.startswith("rm -f"):
            if P.GUEST_B64_IN in cmd:
                self.b64 = ""
            return ""
        if "PROJECT_SET_RC" in cmd:
            expected = set(base64.b64decode(self.b64).decode().splitlines())
            actual = set(self.files)
            return ("PROJECT_SET_RC=0" if expected == actual
                    else "PROJECT_SET_RC=1")
        if "base64 -d /tmp/proj_in.b64" in cmd:
            try:
                self.pushed_tgz = base64.b64decode(self.b64)
            except Exception:              # noqa: BLE001
                return "DECODE_RC=1"
            return "DECODE_RC=0"
        if cmd.startswith(f"stat -c %s {P.GUEST_TGZ_IN}"):
            return str(len(self.pushed_tgz)) if self.pushed_tgz else ""
        if cmd.startswith(f"sha256sum {P.GUEST_TGZ_IN}"):
            if not self.pushed_tgz:
                return ""
            return (f"{hashlib.sha256(self.pushed_tgz).hexdigest()}  "
                    f"{P.GUEST_TGZ_IN}")
        if cmd.startswith(f"tar xzf {P.GUEST_TGZ_IN}"):
            try:
                with tarfile.open(fileobj=io.BytesIO(self.pushed_tgz),
                                  mode="r:gz") as tf:
                    for mem in tf.getmembers():
                        if mem.isfile():
                            self.files[mem.name] = tf.extractfile(mem).read()
            except Exception:              # noqa: BLE001
                return "SYNC_RC=1"
            return "SYNC_RC=0"
        m = _SHASUM.search(cmd)
        if m:
            lines = []
            for rel in shlex.split(m.group(1)):
                if rel in self.files:
                    h = hashlib.sha256(self.files[rel]).hexdigest()
                    lines.append(f"{h}  {rel}")
                else:
                    lines.append(f"sha256sum: {rel}: No such file or directory")
            return _bound("\n".join(lines), cap)
        return ""

    def fetch_file(self, path):
        return (self.tar_out or None), ""  # (bytes|None, note) like env/vm.py


RAW = {"raw/clip0.mp4": b"\x00\x01video-zero\xff" * 40,
       "raw/clip1.mp4": b"\x00\x02video-one\xfe" * 55,
       "raw/notes.txt": b"generic provisioning note\n"}


# ----------------------------------------------------------------- capture --
def test_capture_writes_tgz_and_manifest(tmp_path):
    vm = _GuestVM(RAW)
    r = P.capture_project_materials(vm, "p01", str(tmp_path))
    assert r["ok"] and r["files"] == 3 and r["bytes"] > 0
    assert os.path.exists(tmp_path / "materials.tgz")
    man = P.read_manifest(str(tmp_path))
    assert P.read_roots(str(tmp_path)) == ["raw"]
    assert set(man) == set(RAW)
    for rel, data in RAW.items():          # manifest hashes = source bytes
        assert man[rel] == hashlib.sha256(data).hexdigest()


def test_capture_requests_unbounded_artifact_transport(tmp_path):
    class LimitAwareVM(_GuestVM):
        def __init__(self, files):
            super().__init__(files)
            self.seen_max_bytes = "not-called"

        def fetch_file(self, path, max_bytes=4_000_000):
            self.seen_max_bytes = max_bytes
            if max_bytes is not None and len(self.tar_out) > max_bytes:
                return None, "artificial size rejection"
            return (self.tar_out or None), ""

    vm = LimitAwareVM(RAW)

    result = P.capture_project_materials(vm, "unbounded", str(tmp_path))

    assert result["ok"]
    assert vm.seen_max_bytes is None


def test_chunked_capture_fallback_decodes_each_chunk_independently(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)
    payload = _tgz_of({"raw/large.bin": os.urandom(P._PULL_CHUNK + 17)})
    assert len(payload) > P._PULL_CHUNK

    class ChunkFallbackVM:
        def run_command(self, cmd, timeout=None, cap=None):
            if "tar czf" in cmd:
                return "TAR_RC=0"
            if cmd.startswith("stat -c %s"):
                return str(len(payload))
            if cmd.startswith("dd if="):
                match = re.search(r"bs=(\d+) skip=(\d+)", cmd)
                size, index = map(int, match.groups())
                chunk = payload[index * size:(index + 1) * size]
                return base64.b64encode(chunk).decode("ascii")
            return ""

        def fetch_file(self, _path, max_bytes=None):
            return None, "force fallback"

    result = P.capture_project_materials(
        ChunkFallbackVM(), "chunked", str(tmp_path), attempts=1)

    assert result["ok"]
    assert (tmp_path / P.MATERIALS).read_bytes() == payload


def test_capture_default_dirs_is_raw_only(tmp_path):
    vm = _GuestVM({**RAW, "instance_next.md": b"stray seat file"})
    r = P.capture_project_materials(vm, "p01", str(tmp_path))
    assert r["ok"] and set(P.read_manifest(str(tmp_path))) == set(RAW)


def test_capture_configurable_dirs(tmp_path):
    vm = _GuestVM({**RAW, "ref/target.mp4": b"reference artifact bytes"})
    r = P.capture_project_materials(vm, "p02", str(tmp_path),
                                    guest_dirs=["raw", "ref"])
    assert r["ok"] and r["files"] == 4
    assert "ref/target.mp4" in P.read_manifest(str(tmp_path))


@pytest.mark.parametrize('link_target', ['missing.txt', '/usr/bin/python3', '../../missing.txt'])
def test_broken_symlink_is_captured_replayed_and_verified_without_following(
        tmp_path, monkeypatch, link_target):
    """Adversarial filesystem fixtures may intentionally contain dead links."""

    guest_home = tmp_path / "guest-home"
    guest_tmp = tmp_path / "guest-tmp"
    project = guest_home / "evolution_project"
    project.mkdir(parents=True)
    guest_tmp.mkdir()
    (project / "source.txt").write_text("ground truth\n", encoding="utf-8")
    os.symlink(link_target, project / "broken-link.txt")

    monkeypatch.setattr(P, "GUEST_HOME", str(guest_home))
    monkeypatch.setattr(P, "GUEST_TGZ_OUT", str(guest_tmp / "out.tgz"))
    monkeypatch.setattr(P, "GUEST_TGZ_IN", str(guest_tmp / "in.tgz"))
    monkeypatch.setattr(P, "GUEST_B64_IN", str(guest_tmp / "in.b64"))
    monkeypatch.setattr(P, "GUEST_EXPECTED", str(guest_tmp / "expected"))
    monkeypatch.setattr(P, "GUEST_ACTUAL", str(guest_tmp / "actual"))

    class LocalVM:
        def run_command(self, command, timeout=None, cap=None):
            completed = subprocess.run(
                ["bash", "-lc", command], capture_output=True, text=True,
                timeout=timeout, check=False)
            return completed.stdout

        def fetch_file(self, path, max_bytes=None):
            payload = open(path, "rb").read()
            if max_bytes is not None and len(payload) > max_bytes:
                return None, "too large"
            return payload, ""

        def push_file(self, local_path, guest_path):
            os.makedirs(os.path.dirname(guest_path), exist_ok=True)
            shutil.copyfile(local_path, guest_path)
            return True, ""

    vm = LocalVM()
    host_state = tmp_path / "host-state"
    captured = P.capture_project_materials(
        vm, "link-fixture", str(host_state),
        guest_dirs=["evolution_project"], attempts=1)

    assert captured["ok"] and captured["files"] == 2
    manifest = P.read_manifest(str(host_state))
    link_path = "evolution_project/broken-link.txt"
    assert manifest[link_path] == P._symlink_digest(link_target)
    assert P.read_symlinks(str(host_state)) == {link_path: link_target}

    shutil.rmtree(project)
    assert P.replay_project_materials(vm, str(host_state)) == {
        "ok": True, "mismatches": []}
    restored = project / "broken-link.txt"
    assert restored.is_symlink()
    assert os.readlink(restored) == link_target
    if 'missing.txt' in link_target:
        assert not restored.exists()
    assert P.verify_project_materials(vm, str(host_state)) == {
        "ok": True, "mismatches": []}

    restored.unlink()
    os.symlink("different-missing.txt", restored)
    drift = P.verify_project_materials(vm, str(host_state))
    assert not drift["ok"]
    assert [item["file"] for item in drift["mismatches"]] == [link_path]


def test_owned_paths_are_structured_and_legacy_falls_back_to_roots(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    assert P.read_owned_paths(str(tmp_path)) == ["raw"]

    P.write_owned_paths(str(tmp_path), ["raw", "result.mp4"])
    assert P.read_owned_paths(str(tmp_path)) == ["raw", "result.mp4"]


def test_capture_empty_guest_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    vm = _GuestVM({})                      # provisioner never ran
    r = P.capture_project_materials(vm, "p03", str(tmp_path), attempts=1)
    assert not r["ok"]
    assert not os.path.exists(tmp_path / "materials.tgz")


# -------------------------------------------------------------- round trip --
def test_capture_replay_round_trip(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    vm2 = _GuestVM({})                     # fresh project-night boot
    r = P.replay_project_materials(vm2, str(tmp_path))
    assert r == {"ok": True, "mismatches": []}
    assert vm2.files == RAW                # same paths, same bytes
    host_tgz = (tmp_path / "materials.tgz").read_bytes()
    assert vm2.pushed_tgz == host_tgz      # byte-verbatim transport, no regen


def test_replay_prefers_streaming_upload_and_verifies_archive(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class StreamingGuestVM(_GuestVM):
        def push_file(self, local_path, guest_path):
            assert guest_path == P.GUEST_TGZ_IN
            self.pushed_tgz = open(local_path, "rb").read()
            return True, ""

    vm = StreamingGuestVM({})
    result = P.replay_project_materials(vm, str(tmp_path))

    assert result == {"ok": True, "mismatches": []}
    assert vm.files == RAW
    assert not any("DECODE_RC" in command for command in vm.cmds)


def test_replay_rejects_corrupted_streaming_upload_before_extract(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class CorruptStreamingGuestVM(_GuestVM):
        def push_file(self, local_path, _guest_path):
            payload = open(local_path, "rb").read()
            self.pushed_tgz = payload[:-1] + bytes([payload[-1] ^ 1])
            return True, ""

    vm = CorruptStreamingGuestVM({})
    result = P.replay_project_materials(vm, str(tmp_path))

    assert not result["ok"]
    assert "byte verification" in result["error"]
    assert vm.files == {}


def test_read_only_verify_accepts_exact_and_rejects_mutation(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    vm = _GuestVM({})
    assert P.replay_project_materials(vm, str(tmp_path))["ok"]
    assert P.verify_project_materials(vm, str(tmp_path)) == {
        "ok": True, "mismatches": []}

    vm.files["raw/clip0.mp4"] = b"verifier-side replacement"
    result = P.verify_project_materials(vm, str(tmp_path))
    assert not result["ok"]
    assert [item["file"] for item in result["mismatches"]] == [
        "raw/clip0.mp4"]


def test_replay_wipes_stale_guest_state(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    vm2 = _GuestVM({"raw/leftover.mp4": b"stale from a dead session"})
    r = P.replay_project_materials(vm2, str(tmp_path))
    assert r["ok"] and vm2.files == RAW    # captured set EXACTLY


def test_replay_owns_and_wipes_an_initially_empty_root(tmp_path):
    P.capture_project_materials(
        _GuestVM(RAW), "p01", str(tmp_path), guest_dirs=["raw", "output"])
    assert P.read_roots(str(tmp_path)) == ["output", "raw"]
    vm2 = _GuestVM({"output/late-research.bin": b"must not survive"})

    r = P.replay_project_materials(vm2, str(tmp_path))

    assert r["ok"]
    assert vm2.files == RAW


def test_replay_fails_closed_when_wipe_transport_is_ambiguous(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class WipeFailureVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_WIPE_RC" in cmd:
                return "[channel error: transport lost]"
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(
        WipeFailureVM({"raw/stale.bin": b"stale"}), str(tmp_path))
    assert not r["ok"]
    assert "wipe" in r["error"]


def test_replay_rejects_extra_file_even_when_expected_hashes_pass(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class ExtraFileVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                self.files["raw/late-writer.bin"] = b"contamination"
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(ExtraFileVM({}), str(tmp_path))
    assert not r["ok"]
    assert "file set" in r["error"]


def test_exact_set_command_rejects_any_non_directory_entry(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class SpecialEntryVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                assert "! -type d -print" in cmd
                return "PROJECT_SET_RC=1"
            return super().run_command(cmd, timeout=timeout, cap=cap)

    result = P.replay_project_materials(SpecialEntryVM({}), str(tmp_path))
    assert not result["ok"]
    assert "file set" in result["error"]


def test_replay_detects_late_writer_in_owned_empty_root(tmp_path):
    P.capture_project_materials(
        _GuestVM(RAW), "p01", str(tmp_path), guest_dirs=["raw", "output"])

    class EmptyRootWriterVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                self.files["output/late-writer.bin"] = b"contamination"
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(EmptyRootWriterVM({}), str(tmp_path))
    assert not r["ok"]
    assert "file set" in r["error"]


# ---------------------------------------------------------------- mismatch --
def test_long_path_checksums_are_lossless_and_still_detect_corruption(tmp_path):
    files = {
        f"raw/{'nested-directory/' * 12}artifact_{i:03d}.bin": str(i).encode()
        for i in range(50)
    }
    captured = P.capture_project_materials(_GuestVM(files), "long", str(tmp_path))
    assert captured["ok"]
    vm = _GuestVM()
    assert P.replay_project_materials(vm, str(tmp_path))["ok"]
    damaged = sorted(files)[12]  # in the middle that the old output cap omitted
    vm.files[damaged] = b"changed"
    result = P.verify_project_materials(vm, str(tmp_path))
    assert not result["ok"]
    assert [m["file"] for m in result["mismatches"]] == [damaged]


def test_corrupted_byte_reported_as_mismatch(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    rot = dict(RAW)                        # host-side rot: one byte flipped
    rot["raw/clip1.mp4"] = RAW["raw/clip1.mp4"][:-1] + b"\x00"
    (tmp_path / "materials.tgz").write_bytes(_tgz_of(rot))
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert r["ok"] is False and len(r["mismatches"]) == 1
    mm = r["mismatches"][0]
    assert mm["file"] == "raw/clip1.mp4"
    assert mm["expected"] == hashlib.sha256(RAW["raw/clip1.mp4"]).hexdigest()
    assert mm["got"] == hashlib.sha256(rot["raw/clip1.mp4"]).hexdigest()


def test_missing_file_reported_as_mismatch(tmp_path):
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    short = {k: v for k, v in RAW.items() if k != "raw/clip0.mp4"}
    (tmp_path / "materials.tgz").write_bytes(_tgz_of(short))
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert not r["ok"]
    assert [m["file"] for m in r["mismatches"]] == ["raw/clip0.mp4"]
    assert r["mismatches"][0]["got"] is None


def test_replay_without_host_state_is_infra_failure(tmp_path):
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert not r["ok"] and r["mismatches"] == [] and "error" in r
