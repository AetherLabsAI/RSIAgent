"""Capture and replay exact project materials.

At project ACCEPTANCE (during the authoring session's boot, AFTER the
provisioner has generated raw materials) the harness tars the material dirs
in-guest, pulls the tar into host-side project state, and writes a per-file
sha256 manifest. Every later project night pushes the SAME tar back
byte-verbatim and verifies the manifest in-guest BEFORE the gate.

Determinism: there is NO re-generation path here — ffmpeg
re-generation is not trusted for byte-stability (verified: mvhd/tkhd
timestamps differ). Replay is byte-verbatim by construction; any capture or
verify failure is an infra failure the loop records as provision_failed.

Transport uses the OSWorld server's trusted streaming artifact channel in
both directions.  A compatibility fallback retains chunked base64 replay for
lightweight VM adapters that do not expose ``push_file``.
"""
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import PurePosixPath
import re
import shlex
import tarfile
import time

from explore.commit import _CHUNK

log = logging.getLogger("rsiagent.explore.provisioning")

GUEST_HOME = "/home/user"
GUEST_TGZ_OUT = "/tmp/proj_materials.tgz"
GUEST_TGZ_IN = "/tmp/proj_in.tgz"
GUEST_B64_IN = "/tmp/proj_in.b64"
GUEST_EXPECTED = "/tmp/proj_expected.list"
GUEST_ACTUAL = "/tmp/proj_actual.list"
MATERIALS = "materials.tgz"
MANIFEST = "materials.MANIFEST.sha256"
ROOTS = "materials.ROOTS.json"
OWNED = "materials.OWNED.json"
SYMLINKS = "materials.SYMLINKS.json"
DEFAULT_GUEST_DIRS = ["raw"]

_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+?)\s*$")
_VERIFY_BATCH = 24                 # command transport batch, never an output cap
_PULL_CHUNK = 1_000_000            # transport frame, never a total-size limit
_ROOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _symlink_digest(target: str) -> str:
    """Bind a link target into the ordinary immutable manifest namespace."""

    return hashlib.sha256(b"symlink\0" + os.fsencode(target)).hexdigest()


# ----------------------------------------------------------------- capture --
def capture_project_materials(vm, project_key: str, host_dir: str,
                              guest_dirs=None, attempts: int = 3) -> dict:
    """Guest material dirs -> host_dir/{materials.tgz, MANIFEST}. Runs ONCE,
    at project acceptance, after the provisioner has generated raw materials
    on the authoring boot. Returns {ok, files, bytes}; not-ok = infra
    failure (provision_failed), never silently retried by re-generation."""
    dirs = sorted(set(guest_dirs or DEFAULT_GUEST_DIRS))
    if (not dirs or any(not isinstance(d, str) or not _ROOT_NAME.fullmatch(d)
                        or d.startswith(".") for d in dirs)):
        return {"ok": False, "files": 0, "bytes": 0,
                "error": "guest project roots must be safe top-level names"}
    dir_args = " ".join(shlex.quote(d) for d in dirs)
    data = None
    for att in range(attempts):
        out = vm.run_command(
            f"rm -f {GUEST_TGZ_OUT} && tar czf {GUEST_TGZ_OUT} "
            f"-C {GUEST_HOME} {dir_args} 2>/dev/null; echo TAR_RC=$?",
            timeout=120)
        if "TAR_RC=0" not in (out or ""):
            log.warning("capture attempt %d: guest tar failed (out=%r)",
                        att + 1, (out or "")[:120])
            time.sleep(15)
            continue
        # Project capture is lossless artifact transport, not a look action.
        # Never impose an arbitrary project-size ceiling here: candidate size
        # is determined by the Actor's work and the complete byte stream is
        # part of the experiment.  ``None`` explicitly selects unbounded
        # transfer while ordinary visual/file inspection remains bounded.
        try:
            data = vm.fetch_file(GUEST_TGZ_OUT, max_bytes=None)
        except TypeError:
            # Compatibility with lightweight/older VM transports that expose
            # fetch_file(path) but no size keyword.  Those transports own their
            # transfer semantics; the fallback below still preserves arbitrary
            # total sizes through independently decoded chunks.
            data = vm.fetch_file(GUEST_TGZ_OUT)
        if isinstance(data, tuple):        # fetch_file returns (bytes|None, note)
            data = data[0]
        if data:
            break
        log.warning("capture attempt %d: fetch returned empty", att + 1)
        time.sleep(15)
    if not data:
        # chunked-b64 fallback: pull the tgz through run_command in 1MB
        # slices (transport-agnostic; same channel replay pushes through)
        try:
            sz = int((vm.run_command(f"stat -c %s {GUEST_TGZ_OUT}",
                                     timeout=30) or "0").split()[0])
        except (ValueError, IndexError):
            sz = 0
        if sz:
            import base64 as _b64
            CH = _PULL_CHUNK
            blob = io.BytesIO()
            fallback_ok = True
            for k in range((sz + CH - 1) // CH):
                p = vm.run_command(
                    f"dd if={GUEST_TGZ_OUT} bs={CH} skip={k} count=1 "
                    "2>/dev/null | base64 -w0", timeout=180, cap=2_500_000)
                try:
                    chunk = _b64.b64decode((p or "").strip(), validate=True)
                except Exception as e:                     # noqa: BLE001
                    log.warning("capture b64 fallback chunk %d decode failed: %s",
                                k, e)
                    fallback_ok = False
                    break
                expected = min(CH, sz - k * CH)
                if len(chunk) != expected:
                    log.warning("capture b64 fallback chunk %d size mismatch "
                                "%d != %d", k, len(chunk), expected)
                    fallback_ok = False
                    break
                blob.write(chunk)
            if fallback_ok:
                pulled = blob.getvalue()
                if len(pulled) == sz:
                    data = pulled
                    log.info("capture: b64 fallback pulled %d bytes", sz)
                else:
                    log.warning("capture b64 fallback size mismatch "
                                "%d != %d", len(pulled), sz)
    if not data:
        log.warning("capture FAILED for project %s after %d attempts",
                    project_key, attempts)
        return {"ok": False, "files": 0, "bytes": 0}
    manifest, symlinks = _inventory_from_tgz(data)
    if not manifest:
        log.warning("capture for project %s produced an EMPTY tar "
                    "(dirs=%s)", project_key, dirs)
        return {"ok": False, "files": 0, "bytes": len(data)}
    os.makedirs(host_dir, exist_ok=True)
    with open(os.path.join(host_dir, MATERIALS), "wb") as f:
        f.write(data)
    with open(os.path.join(host_dir, MANIFEST), "w") as f:
        f.write(f"# project: {project_key}  dirs: {' '.join(dirs)}  "
                f"captured: {time.strftime('%F %T')}\n")
        for rel in sorted(manifest):
            f.write(f"{manifest[rel]}  {rel}\n")
    with open(os.path.join(host_dir, ROOTS), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "roots": dirs}, f,
                  indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(host_dir, SYMLINKS), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "links": symlinks}, f,
                  ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return {"ok": True, "files": len(manifest), "bytes": len(data)}


def _inventory_from_tgz(data: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Return immutable digests and exact symlink targets from an archive.

    Inspect archive bytes directly, without extracting or dereferencing symlinks
    on the host. Absolute guest symlinks (for example a venv's Python executable)
    are ordinary opaque link targets. Unsafe member paths, special files, and
    children beneath a non-directory entry are rejected before replay.
    """

    manifest: dict[str, str] = {}
    symlinks: dict[str, str] = {}
    def safe_name(value: str) -> str:
        path = PurePosixPath(value)
        if (path.is_absolute() or ".." in path.parts
                or any(c in value for c in "\0\n\r") or not path.parts):
            raise ValueError(f"unsafe project archive path: {value!r}")
        return path.as_posix()

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = {}
        for member in tf:
            rel = safe_name(member.name)
            if rel in members:
                raise ValueError(f"duplicate project archive path: {rel}")
            if not (member.isdir() or member.isfile()
                    or member.issym() or member.islnk()):
                raise ValueError(f"project archive contains a special entry: {rel}")
            members[rel] = member
        for rel, member in members.items():
            for parent in PurePosixPath(rel).parents:
                entry = members.get(parent.as_posix())
                if entry is not None and not entry.isdir():
                    raise ValueError(f"project archive traverses a non-directory: {rel}")
            if member.isdir():
                continue
            if member.issym():
                symlinks[rel] = member.linkname
                manifest[rel] = _symlink_digest(member.linkname)
                continue
            seen = {rel}
            source = member
            while source.islnk():
                target = safe_name(source.linkname)
                if target in seen or target not in members:
                    raise ValueError(f"invalid project archive hardlink: {rel}")
                seen.add(target)
                source = members[target]
            if not source.isfile():
                raise ValueError(f"project hardlink does not name regular bytes: {rel}")
            h = hashlib.sha256()
            with tf.extractfile(source) as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    h.update(block)
            manifest[rel] = h.hexdigest()
    return dict(sorted(manifest.items())), dict(sorted(symlinks.items()))


def _manifest_from_tgz(data: bytes) -> dict:
    """Compatibility helper returning all immutable archive-entry digests."""

    return _inventory_from_tgz(data)[0]


def read_manifest(host_dir: str) -> dict:
    """{relpath: sha256hex} from host_dir/materials.MANIFEST.sha256 (empty
    dict if absent). '#'-prefixed lines are provenance comments."""
    p = os.path.join(host_dir, MANIFEST)
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _SHA_LINE.match(line)
            if m:
                out[m.group(2)] = m.group(1)
    return out


def read_symlinks(host_dir: str) -> dict[str, str]:
    """Return the exact link targets bound by a captured project manifest.

    Link targets are opaque guest strings, including absolute paths used by
    virtual environments. Verification compares readlink() without following
    the target. Only the link's own manifest path must stay inside the archive.
    Legacy archives have no sidecar and therefore return an empty mapping.
    Invalid sidecars also fail closed as empty: their symlink paths remain in
    the main manifest and regular-file verification will reject them.
    """

    path = os.path.join(host_dir, SYMLINKS)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    links = record.get("links") if isinstance(record, dict) else None
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "links"}
            or record.get("schema_version") != 1
            or not isinstance(links, dict)):
        return {}
    result: dict[str, str] = {}
    for rel, target in links.items():
        if (not isinstance(rel, str) or not rel or rel.startswith("/")
                or ".." in rel.split("/") or not isinstance(target, str)
                or any(c in rel for c in "\0\n\r")
                or not target or "\0" in target):
            return {}
        result[rel] = target
    return dict(sorted(result.items()))


def read_roots(host_dir: str) -> list[str]:
    """Return the owned top-level guest roots for an archive.

    New captures record roots structurally so empty directories remain inside
    the wipe/exact-set boundary. Legacy captures predate that file and fall
    back to the top-level components represented by their file manifest.
    """
    p = os.path.join(host_dir, ROOTS)
    if not os.path.exists(p):
        return sorted({rel.split("/", 1)[0]
                       for rel in read_manifest(host_dir) if rel})
    try:
        record = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    roots = record.get("roots") if isinstance(record, dict) else None
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "roots"}
            or record.get("schema_version") != 1
            or not isinstance(roots, list)
            or roots != sorted(set(roots))
            or not roots
            or any(not isinstance(d, str) or not _ROOT_NAME.fullmatch(d)
                   or d.startswith(".") for d in roots)):
        return []
    return roots


def write_owned_paths(host_dir: str, paths: list[str]) -> None:
    """Persist the immutable top-level paths a project may grade or mutate."""
    values = sorted(set(paths))
    if (not values
            or any(not isinstance(p, str) or not _ROOT_NAME.fullmatch(p)
                   or p.startswith(".") for p in values)):
        raise ValueError("owned project paths must be safe top-level names")
    with open(os.path.join(host_dir, OWNED), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "paths": values}, f,
                  indent=2, sort_keys=True)
        f.write("\n")


def read_owned_paths(host_dir: str) -> list[str]:
    """Return graded ownership scope; legacy archives fall back to roots."""
    p = os.path.join(host_dir, OWNED)
    if not os.path.exists(p):
        return read_roots(host_dir)
    try:
        record = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    paths = record.get("paths") if isinstance(record, dict) else None
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "paths"}
            or record.get("schema_version") != 1
            or not isinstance(paths, list)
            or paths != sorted(set(paths))
            or not paths
            or any(not isinstance(v, str) or not _ROOT_NAME.fullmatch(v)
                   or v.startswith(".") for v in paths)):
        return []
    return paths


def audit_captured_materials(host_dir: str,
                             authorized_instruction: str = "") -> list:
    """Fence captured names and payload bytes before an archive is replayed.

    Rich application artifacts are often binary, so opaque binaries cannot be
    rejected as a class without forbidding the work this pipeline is designed
    to learn. In target-aware mode we audit a loss-tolerant UTF-8 projection of
    every payload, which catches ordinary ASCII/UTF-8 signals embedded in a
    binary, as well as each archive member name. Compressed/encrypted content
    remains opaque and is therefore a documented detection boundary rather
    than a claim of semantic classification. Blind mode retains its historical
    strict-UTF-8 scan. Findings stay match-free.
    """
    from tools.exam_fence import audit_text
    tgz = os.path.join(host_dir, MATERIALS)
    if not os.path.isfile(tgz):
        return [{"file": MATERIALS, "kinds": ["missing-archive"]}]
    findings = []
    try:
        with tarfile.open(tgz, mode="r:gz") as tf:
            for member in tf.getmembers():
                if authorized_instruction:
                    # Target-aware mode strengthens the historical fixture
                    # check: ``errors=ignore`` preserves ASCII/UTF-8 substrings
                    # embedded in otherwise binary payloads, and the member
                    # name is part of the captured exposure surface too. It
                    # deliberately does not claim to unpack arbitrary formats.
                    hits = audit_text(
                        member.name, mode="practice",
                        authorized_instruction=authorized_instruction)
                    if member.issym() or member.islnk():
                        hits += audit_text(
                            member.linkname, mode="practice",
                            authorized_instruction=authorized_instruction)
                    elif member.isfile():
                        handle = tf.extractfile(member)
                        if handle is not None:
                            text = handle.read().decode(
                                "utf-8", errors="ignore")
                            hits += audit_text(
                                text, mode="practice",
                                authorized_instruction=authorized_instruction)
                else:
                    # Preserve the frozen blind-mode behavior exactly: only
                    # wholly UTF-8 fixture payloads are in that regime's scan.
                    if member.issym() or member.islnk():
                        hits = audit_text(member.linkname, mode="practice")
                    elif member.isfile():
                        handle = tf.extractfile(member)
                        if handle is None:
                            continue
                        try:
                            text = handle.read().decode(
                                "utf-8", errors="strict")
                        except UnicodeDecodeError:
                            continue
                        hits = audit_text(text, mode="practice")
                    else:
                        continue
                if hits:
                    findings.append({
                        "file": member.name,
                        "kinds": sorted({h.get("kind", "unknown")
                                         for h in hits})})
    except (OSError, tarfile.TarError) as exc:
        return [{"file": MATERIALS,
                 "kinds": [f"unreadable-archive:{type(exc).__name__}"]}]
    return findings


# ------------------------------------------------------------------ replay --
def replay_project_materials(vm, host_dir: str) -> dict:
    """host_dir/materials.tgz -> guest, byte-verbatim, then VERIFY in-guest
    (sha256sum each manifest file vs the manifest). Returns {ok, mismatches}
    with mismatches = [{file, expected, got}] (got=None => missing/unread).
    Any not-ok result is an infra failure -> the loop records
    provision_failed; there is no re-generation fallback (§3 B7)."""
    tgz = os.path.join(host_dir, MATERIALS)
    manifest = read_manifest(host_dir)
    roots = read_roots(host_dir)
    if not manifest or not roots or not os.path.exists(tgz):
        log.warning("replay: missing %s in %s",
                    MATERIALS if not os.path.exists(tgz) else MANIFEST,
                    host_dir)
        return {"ok": False, "mismatches": [],
                "error": "missing materials, manifest, or project-root state"}
    archive_bytes = os.path.getsize(tgz)
    archive_digest = hashlib.sha256()
    with open(tgz, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            archive_digest.update(block)
    archive_sha256 = archive_digest.hexdigest()
    # wipe the captured top-level dirs so replay restores EXACTLY the
    # captured file set (byte-verbatim by construction, no stale extras)
    wipe = " ".join(shlex.quote(f"{GUEST_HOME}/{d}") for d in roots)
    wipe_out = vm.run_command(
        f"rm -rf {GUEST_B64_IN} {GUEST_TGZ_IN} {GUEST_EXPECTED} "
        f"{GUEST_ACTUAL} {wipe} ; echo PROJECT_WIPE_RC=$?",
        timeout=120)
    if "PROJECT_WIPE_RC=0" not in (wipe_out or ""):
        log.warning("replay wipe failed (out=%r)", (wipe_out or "")[:160])
        return {"ok": False, "mismatches": [],
                "error": "could not wipe prior guest project state"}
    push_file = getattr(vm, "push_file", None)
    if callable(push_file):
        pushed = push_file(tgz, GUEST_TGZ_IN)
        if isinstance(pushed, tuple):
            push_ok, push_note = pushed
        else:
            push_ok, push_note = bool(pushed), ""
        if not push_ok:
            log.warning("replay streaming upload failed: %s", push_note)
            return {"ok": False, "mismatches": [],
                    "error": "streaming artifact upload failed"}
    else:
        # Compatibility for minimal/legacy VM adapters.  This is a transport
        # frame size, never a total artifact-size ceiling.
        with open(tgz, "rb") as source:
            while True:
                raw = source.read((_CHUNK // 4) * 3)
                if not raw:
                    break
                chunk = base64.b64encode(raw).decode("ascii")
                pushed = vm.run_command(
                    f"printf %s '{chunk}' >> {GUEST_B64_IN}", timeout=30)
                if (pushed or "").startswith(("[channel error:",
                                                "[command timed out")):
                    return {"ok": False, "mismatches": [],
                            "error": "chunked artifact upload failed"}
        decoded = vm.run_command(
            f"base64 -d {GUEST_B64_IN} > {GUEST_TGZ_IN} "
            f"2>/tmp/proj_err; echo DECODE_RC=$?", timeout=120)
        if "DECODE_RC=0" not in (decoded or ""):
            return {"ok": False, "mismatches": [],
                    "error": "chunked artifact decode failed"}

    size_out = vm.run_command(f"stat -c %s {GUEST_TGZ_IN}", timeout=30)
    sha_out = vm.run_command(f"sha256sum {GUEST_TGZ_IN}", timeout=120)
    try:
        guest_bytes = int((size_out or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        guest_bytes = -1
    match = re.search(r"(?m)^([0-9a-f]{64})\s+", sha_out or "")
    guest_sha256 = match.group(1) if match else ""
    if guest_bytes != archive_bytes or guest_sha256 != archive_sha256:
        log.warning("replay archive transport mismatch: bytes %s/%s sha %s/%s",
                    guest_bytes, archive_bytes, guest_sha256, archive_sha256)
        return {"ok": False, "mismatches": [],
                "error": "uploaded archive failed byte verification"}

    out = vm.run_command(
        f"tar xzf {GUEST_TGZ_IN} -C {GUEST_HOME} 2>>/tmp/proj_err; "
        f"echo SYNC_RC=$?", timeout=120)
    if "SYNC_RC=0" not in (out or ""):
        log.warning("replay push/extract failed (out=%r)", (out or "")[:120])
        return {"ok": False, "mismatches": [],
                "error": "push/extract failed in guest"}
    verified = verify_project_materials(vm, host_dir)
    mismatches = verified.get("mismatches", [])
    if mismatches:
        log.warning("replay verify: %d/%d files mismatched (first: %s)",
                    len(mismatches), len(manifest), mismatches[0]["file"])
        return verified
    if not verified.get("ok"):
        log.warning("replay exact-set verify failed (out=%r)",
                    verified.get("set_verify", "")[:160])
        return verified
    return verified


def verify_project_materials(vm, host_dir: str) -> dict:
    """Verify the live guest against a captured project without mutating it.

    This is the read-only counterpart to :func:`replay_project_materials`:
    every captured regular file must retain its exact hash, and no extra file
    or symlink may exist beneath any owned project root. It is suitable for
    detecting an Agent-side candidate mutation before accepting a handoff.
    """
    manifest = read_manifest(host_dir)
    symlinks = read_symlinks(host_dir)
    roots = read_roots(host_dir)
    if not manifest or not roots:
        return {"ok": False, "mismatches": [],
                "error": "missing manifest or project-root state"}
    if any(manifest.get(rel) != _symlink_digest(target)
           for rel, target in symlinks.items()):
        return {"ok": False, "mismatches": [],
                "error": "symlink sidecar is not bound by the manifest"}
    regular = {rel: digest for rel, digest in manifest.items()
               if rel not in symlinks}
    mismatches = _verify_in_guest(vm, regular)
    mismatches.extend(_verify_symlinks_in_guest(vm, symlinks))
    if mismatches:
        return {"ok": False, "mismatches": mismatches}
    exact = _verify_exact_file_set(vm, manifest, roots)
    if not exact.get("ok"):
        return {"ok": False, "mismatches": [],
                "error": "live project file set is not exact",
                "set_verify": exact.get("output", "")[:500]}
    return {"ok": True, "mismatches": []}


def _verify_in_guest(vm, manifest: dict) -> list:
    """sha256sum each manifest file in-guest; [{file, expected, got}] for
    every file whose hash differs or that cannot be read."""
    rels = sorted(manifest)
    mismatches = []
    for i in range(0, len(rels), _VERIFY_BATCH):
        batch = rels[i:i + _VERIFY_BATCH]
        args = " ".join(shlex.quote(r) for r in batch)
        out = vm.run_command(
            f"cd {GUEST_HOME} && sha256sum {args} 2>&1",
            timeout=300, cap=0) or ""
        got = {}
        for line in out.splitlines():
            m = _SHA_LINE.match(line.strip())
            if m:
                got[m.group(2)] = m.group(1)
        for rel in batch:
            if got.get(rel) != manifest[rel]:
                mismatches.append({"file": rel, "expected": manifest[rel],
                                   "got": got.get(rel)})
    return mismatches


def _verify_symlinks_in_guest(vm, symlinks: dict[str, str]) -> list:
    """Verify link identity without following either valid or broken links."""

    mismatches = []
    probe = (
        "import base64,json,os,sys;"
        "p,t=json.loads(base64.b64decode(sys.argv[1]));"
        "ok=os.path.islink(p) and os.readlink(p)==t;"
        "print('PROJECT_LINK_RC='+('0' if ok else '1'))")
    for rel, target in sorted(symlinks.items()):
        payload = base64.b64encode(json.dumps(
            [rel, target], ensure_ascii=False).encode("utf-8")).decode("ascii")
        out = vm.run_command(
            f"cd {GUEST_HOME} && python3 -c {shlex.quote(probe)} "
            f"{shlex.quote(payload)}",
            timeout=60) or ""
        if "PROJECT_LINK_RC=0" not in out:
            mismatches.append({
                "file": rel,
                "expected": _symlink_digest(target),
                "got": None,
            })
    return mismatches


def _verify_exact_file_set(vm, manifest: dict, roots: list[str]) -> dict:
    """Require exactly the regular manifest and no other non-directories."""
    expected = ("\n".join(sorted(manifest)) + "\n").encode("utf-8")
    encoded = base64.b64encode(expected).decode("ascii")
    vm.run_command(
        f"rm -f {GUEST_B64_IN} {GUEST_EXPECTED} {GUEST_ACTUAL}", timeout=30)
    for i in range(0, len(encoded), _CHUNK):
        chunk = encoded[i:i + _CHUNK]
        vm.run_command(
            f"printf %s '{chunk}' >> {GUEST_B64_IN}", timeout=30)
    args = " ".join(shlex.quote(top) for top in roots)
    out = vm.run_command(
        f"base64 -d {GUEST_B64_IN} > {GUEST_EXPECTED} && "
        f"cd {GUEST_HOME} && "
        f"find -- {args} ! -type d -print | "
        f"LC_ALL=C sort > {GUEST_ACTUAL} && "
        f"cmp -s {GUEST_EXPECTED} {GUEST_ACTUAL}; "
        f"echo PROJECT_SET_RC=$?",
        timeout=300) or ""
    return {"ok": "PROJECT_SET_RC=0" in out, "output": out}
