"""E7 reference-reproduction era — the project episode loop (PREREG E7
v2.2-final, EXP-2026-007).

Night flow: replay project materials (byte-verbatim, manifest-verified) ->
memory in -> tonight's stored card re-runs FULL intake on its own boot
(parse + fence + budget window + token screen + multi-GUARD dry-run gate)
-> actor sees ONLY the project target + tonight's card (per-night reveal,
B3) at the card's budget (harness-enforced) with the stall detector hooked
(B5) -> harness grades milestone (+ FINAL criteria on the last night, N7
re-gated) -> verifier -> memory restore -> scan v1.2 -> observe ledger ->
commit -> curve row v2 -> curriculum stage (archive pushed for it alone,
M7 tombstones; DECISION parsed; new projects ACCEPTED here: provision +
capture-once on this very boot, B7).

Projects end on final-criteria PASS or ABANDON — never on a counter,
never on the actor's claim (Sibo law).
"""
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import time

from core.actor import build_system
from core.loop import run_attempt
from core.trace import ArtifactSink
from explore import commit as mem
from explore import reward, stall
from explore.archive7 import GUEST_ARCHIVE, build_curriculum_archive
from explore.charter import (E7_MILESTONE_DISCLOSURE, E8_DISCLOSURE,
                             agentic_verifier_charter, e6_actor_charter,
                             e6_verifier_charter,
                             e7_curriculum_charter,
                             new_project_rejection_msg,
                             post_verdict_memory_msg,
                             preflight_repair_rejection_msg)
from explore.commit import push_dir, rm_guest_dir
from explore.e6_loop import (_guest_cat, _has_terminal_run_script_trailer,
                             _provision_shotcut,
                             _strip_run_script_trailer, _transcript_slice,
                             graded_block, listing_text)
from explore.e8_loop import GATE_STAMP, validate_card
from explore.e8_loop import run_dry_gate as _drill_gate
from explore.gate import (dry_run_verdict, find_guards, static_token_screen,
                          strip_guard)
from explore.provision7 import (audit_captured_materials,
                                capture_project_materials,
                                read_manifest, read_owned_paths, read_roots,
                                replay_project_materials,
                                write_owned_paths)
from explore.reward import DRILL_BUDGET, MILESTONE_BUDGET

from config.runtime_paths import resolve_forge_root

FORGE = str(resolve_forge_root())
CORPUS = os.path.join(FORGE, "results", "explore", "corpus_shingles.json")
_NUM = re.compile(r"^\s*\d+[.)]\s*")
_DECISION = re.compile(r"^DECISION:\s*(CONTINUE|REPAIR|ABANDON)\s*$", re.M)
_ARTIFACT_SEMANTICS = "reconstruct_from_initial_capture"


def _assert_target_null_task(vm, target_task: str) -> None:
    """Refuse a target-aware Agent turn on a task-configured OSWorld env.

    Lightweight test transports need not expose ``env.task_config``. When the
    production handle does expose it, however, a non-null value means setup and
    evaluator state may already be bound to the guest and the instruction-only
    contract no longer holds.
    """
    if not target_task.strip():
        return
    env = getattr(vm, "env", None)
    if env is not None and hasattr(env, "task_config") \
            and env.task_config is not None:
        raise ValueError(
            "instruction-only targeted mode requires a null-task environment")


def _provision_workspace(vm, ep_no: int, target_task: str = "") -> bool:
    """Provision the legacy Shotcut class or a domain-neutral target workspace."""
    if not target_task.strip():
        return _provision_shotcut(vm, ep_no)
    _assert_target_null_task(vm, target_task)
    out = vm.run_command(
        "mkdir -p /home/user/raw && test -d /home/user/raw; "
        "echo TARGET_WORKSPACE_RC=$?", timeout=60) or ""
    return "TARGET_WORKSPACE_RC=0" in out


def run_dry_gate(vm, criteria: list) -> dict:
    """Multi-GUARD gate for project nights (N4): e8's flow with
    find_guards + guard_indices. Drill nights keep e8's single-GUARD gate
    via validate_card -> _drill_gate at their call sites."""
    guard_idx = find_guards(criteria)
    cmds = [_NUM.sub("", strip_guard(c)).strip() for c in criteria]
    vm.run_command(f"touch {GATE_STAMP}", timeout=30)

    def _clean(out):
        return _strip_run_script_trailer(out)

    results = []
    for i, cmd in enumerate(cmds):
        runs = []
        for _ in range(2):
            try:
                tr = vm.run_script("bash", cmd, timeout=180)
                runs.append(_clean(tr.stdout))
            except Exception as e:                          # noqa: BLE001
                runs.append(f"[gate error] {e}")
        results.append({"n": i + 1, "run1": runs[0], "run2": runs[1]})
    verdict = dry_run_verdict(results, guard_indices=guard_idx)
    created = vm.run_command(
        f"find /home/user -newer {GATE_STAMP} -type f "
        "! -path '*/.*' ! -path '/home/user/moneroocean/*' "
        "2>/dev/null | head -20", timeout=60) or ""
    # /home/user/moneroocean = the golden image's RESIDENT xmrig miner —
    # it writes logs/configs continuously; background noise, not gate
    # pollution (ep004 voided on its scribbles). Same class as dotfiles.
    created = [l for l in created.splitlines()
               if l.strip() and "[exit" not in l]
    mutated = bool(created)
    reasons = list(verdict["reasons"])
    if mutated:
        reasons.append(f"mutation: gate created/modified {created[:5]}")
    return {"accept": verdict["verdict"] == "ACCEPT" and not mutated,
            "reasons": reasons, "mutated": mutated,
            "per_criterion": verdict["results"]}


# ---------------------------------------------------------- project state --
def live_project(root: str):
    pdir = os.path.join(root, "curriculum")
    for fn in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if fn.startswith("project_") and fn.endswith(".json"):
            pj = json.load(open(os.path.join(pdir, fn)))
            if pj.get("status") in ("accepted", "in_progress"):
                key = str(pj.get("key", ""))
                if (not reward.valid_project_name(key)
                        or pj.get("name") != key
                        or fn != f"project_{key}.json"):
                    raise ValueError(
                        "active project state has an unsafe or inconsistent key")
                return pj, os.path.join(pdir, fn)
    return None, None


def save_project(path: str, pj: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, candidate = tempfile.mkstemp(prefix=".project-state-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(candidate, path)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)


def _full_guest_file(vm, path: str) -> tuple[str, str]:
    data, err = vm.fetch_file(path, max_bytes=None)
    if data is None:
        return "", err or "fetch failed"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return "", f"report is not UTF-8: {exc}"


def _guest_text_lossless(vm, path: str, max_bytes: int | None = None) -> str:
    """Read a UTF-8 guest handoff without ``run_script`` output bounding.

    Production ``VM.fetch_file`` transports exact bytes.  The legacy cat
    reader remains only as a compatibility seam for focused duck-typed tests
    that do not implement file fetching. A failed or non-UTF-8 production fetch
    becomes an empty/stale handoff and therefore fails closed.
    """
    fetch = getattr(vm, "fetch_file", None)
    if not callable(fetch):
        return _guest_cat(vm, path)
    fetch_path = ("/home/user/" + path[2:]
                  if path.startswith("~/") else path)
    try:
        fetched = fetch(fetch_path, max_bytes=max_bytes)
    except TypeError:
        # Compatibility with older test transports whose fetch_file accepts
        # only the path. Production always takes max_bytes.
        fetched = fetch(fetch_path)
    if isinstance(fetched, tuple):
        data = fetched[0]
    else:
        data = fetched
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return bytes(data).decode("utf-8")
    except (TypeError, UnicodeDecodeError):
        return ""


def _collect_verifier_report(vm, report_path: str, res, eproot: str) -> dict:
    """Gate only lossless transport and the Verifier Agent's own completion."""
    report, fetch_error = _full_guest_file(vm, report_path)
    errors = []
    if fetch_error:
        errors.append(fetch_error)
    if not report.strip():
        errors.append("report missing or empty")
    if report:
        with open(os.path.join(eproot, "verifier_report.md"), "w",
                  encoding="utf-8") as f:
            f.write(report)
    return {"ok": res.status == "done" and not errors,
            "status": res.status, "report": report, "errors": errors,
            "iters": res.iters, "turns": res.turns,
            "wall_secs": round(res.wall_secs)}


def _owned_project_paths(text: str, fixture_roots=()) -> list[str]:
    """Extract safe top-level file or directory paths from an accepted card."""
    paths = set(fixture_roots)
    paths.update(re.findall(
        r"/home/user/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:/|\b)", text or ""))
    paths.update(re.findall(
        r"~/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:/|\b)", text or ""))
    paths.update(re.findall(
        r"\$(?:HOME|\{HOME\})/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:/|\b)",
        text or ""))
    blocked = {".memory", "curriculum_archive", "verifier_report.md",
               "instance_next.md", "instance_rejected.md",
               "curriculum_notes.md"}
    return sorted(p for p in paths
                  if p not in blocked and not p.startswith("."))


_CURRICULUM_PRIVATE_DEPENDENCY = re.compile(
    r"(?:(?<![A-Za-z0-9_])~|\$(?:HOME|\{HOME\})|/home/user)"
    r"/(?:curriculum_archive(?=/|\b)|"
    r"(?:instance_next|instance_rejected|curriculum_notes)\.md\b)")
_ACTOR_MEMORY_DEPENDENCY = re.compile(
    r"(?:(?<![A-Za-z0-9_])~|\$(?:HOME|\{HOME\})|/home/user)"
    r"/\.memory(?=/|\b)")


def _has_curriculum_private_dependency(text: str) -> bool:
    """Whether an Actor-facing card depends on Curriculum-private storage."""
    return bool(_CURRICULUM_PRIVATE_DEPENDENCY.search(text or ""))


def _has_actor_memory_dependency(text: str) -> bool:
    """Whether Curriculum-authored transport reaches Actor-private memory."""
    return bool(_ACTOR_MEMORY_DEPENDENCY.search(text or ""))


def _reauthor_recapture_scope(
        pj: dict, card_text: str) -> tuple[list[str], list[str]]:
    """Include corrected-card roots when recapturing a reauthored project."""
    transport_text = (
        str(pj.get("target_prose") or "")
        + "".join(str(text) for text in (pj.get("night_cards") or []))
        + str(pj.get("final_text") or "")
        + str(card_text or "")
    )
    dirs = set(re.findall(
        r"/home/user/([A-Za-z0-9][A-Za-z0-9_.-]*)/", transport_text))
    dirs.update(re.findall(
        r"~/([A-Za-z0-9][A-Za-z0-9_.-]*)/", transport_text))
    dirs.update(re.findall(
        r"\$(?:HOME|\{HOME\})/([A-Za-z0-9][A-Za-z0-9_.-]*)/",
        transport_text))
    dirs.add("raw")
    dirs = sorted(dirs)
    return dirs, _owned_project_paths(transport_text, dirs)


_PROJECT_PATH_REF = re.compile(
    r"(?:(?<![A-Za-z0-9_])~|\$(?:HOME|\{HOME\})|/home/user)"
    r"/([A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*)")


def _guarded_project_refs(pj: dict) -> list[str]:
    """Home-relative paths that corrected criteria declare pre-existing."""
    guarded = set()
    texts = list(pj.get("night_cards") or [])
    texts.append(str(pj.get("final_text") or ""))
    for text in texts:
        criteria = [line for line in str(text).splitlines()
                    if _NUM.match(line)]
        for index in find_guards(criteria):
            guarded.update(_PROJECT_PATH_REF.findall(criteria[index]))
    return sorted(guarded)


def _replay_project_for_fresh_night(vm, project_dir: str) -> dict:
    """Wipe every historically owned output, then replay only fixtures."""
    if os.path.isdir(project_dir):
        roots = set(read_roots(project_dir))
        owned = set(read_owned_paths(project_dir))
        if not roots or not owned or not roots.issubset(owned):
            return {"ok": False, "mismatches": [],
                    "error": "invalid project fixture/ownership boundary"}
        outputs = sorted(owned - roots)
        if outputs:
            paths = " ".join(
                shlex.quote(f"/home/user/{name}") for name in outputs)
            wiped = vm.run_command(
                f"rm -rf {paths}; echo PROJECT_OUTPUT_WIPE_RC=$?",
                timeout=120) or ""
            if "PROJECT_OUTPUT_WIPE_RC=0" not in wiped:
                return {"ok": False, "mismatches": [],
                        "error": "could not wipe prior Actor output roots"}
    # The underlying replay validates the archive and remains the compatibility
    # seam for legacy/test transports whose project directory is unavailable.
    return replay_project_materials(vm, project_dir)


def _graded_project_dirs(root: str, pj, card_text: str) -> list[str]:
    """Resolve the live top-level project paths whose graded bytes must freeze.

    Accepted projects use their capture manifest as the ownership boundary;
    arbitrary path mentions must never expand a destructive restore scope.
    Standalone legacy drills have no manifest, so their explicit card paths are
    the fallback boundary.
    """
    dirs = set()
    texts = []
    if pj:
        pdir = os.path.join(root, "projects", pj["key"])
        dirs.update(read_owned_paths(pdir))
    else:
        # Legacy standalone drills have no accepted-project manifest.  Their
        # card is therefore the only ownership declaration available.
        texts.append(card_text or "")
    for text in texts:
        dirs.update(_owned_project_paths(text))
    return sorted(dirs)


def _capture_graded_artifact(vm, root: str, eproot: str, pj,
                             card_text: str,
                             authorized_instruction: str = "") -> dict:
    """Freeze the already-graded project tree before another Agent can edit it."""
    dirs = _graded_project_dirs(root, pj, card_text)
    if not dirs:
        return {"ok": False, "roots": [],
                "error": "no graded project roots could be resolved"}
    host_dir = os.path.join(eproot, "graded_artifact")
    os.makedirs(host_dir, exist_ok=True)
    present = []
    probe_errors = []
    for name in dirs:
        path = shlex.quote(f"/home/user/{name}")
        out = vm.run_command(
            f"test -e {path}; echo PROJECT_ROOT_RC=$?", timeout=30) or ""
        if "PROJECT_ROOT_RC=0" in out:
            present.append(name)
        elif "PROJECT_ROOT_RC=1" not in out:
            probe_errors.append({"root": name, "output": out[:160]})
    if probe_errors:
        return {"ok": False, "roots": dirs, "present": present,
                "error": "graded project root presence probe failed",
                "probe_errors": probe_errors}
    absent = sorted(set(dirs) - set(present))
    key = f"ep{os.path.basename(eproot).removeprefix('ep')}-graded"
    captured = ({"ok": True, "files": 0, "bytes": 0}
                if not present else
                capture_project_materials(
                    vm, key, host_dir, guest_dirs=present))
    if not captured.get("ok"):
        return {**captured, "roots": dirs, "present": present,
                "absent": absent, "host_dir": host_dir}
    if authorized_instruction and present:
        findings = audit_captured_materials(
            host_dir, authorized_instruction=authorized_instruction)
        if findings:
            return {**captured, "ok": False, "roots": dirs,
                    "present": present, "absent": absent,
                    "host_dir": host_dir,
                    "error": "graded artifact failed the content boundary",
                    "audit": findings}
    state = {"roots": dirs, "present": present, "absent": absent}
    with open(os.path.join(host_dir, "snapshot.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    return {**captured, **state, "host_dir": host_dir}


def _restore_graded_artifact(vm, host_dir: str,
                             authorized_instruction: str = "") -> dict:
    """Restore and hash-verify the canonical graded project tree."""
    state_path = os.path.join(host_dir, "snapshot.json")
    if not os.path.exists(state_path):
        return {"ok": False, "error": "graded snapshot state is missing"}
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    if authorized_instruction and state.get("present"):
        findings = audit_captured_materials(
            host_dir, authorized_instruction=authorized_instruction)
        if findings:
            return {"ok": False,
                    "error": "canonical artifact failed the content boundary",
                    "audit": findings}
    replay = ({"ok": True, "mismatches": []}
              if not state.get("present") else
              replay_project_materials(vm, host_dir))
    if not replay.get("ok"):
        return replay
    absent = state.get("absent", [])
    if absent:
        paths = " ".join(shlex.quote(f"/home/user/{name}")
                         for name in absent)
        vm.run_command(f"rm -rf {paths}", timeout=60)
        still_present = []
        for name in absent:
            path = shlex.quote(f"/home/user/{name}")
            out = vm.run_command(
                f"test ! -e {path}; echo PROJECT_ABSENT_RC=$?",
                timeout=30) or ""
            if "PROJECT_ABSENT_RC=0" not in out:
                still_present.append(name)
        if still_present:
            return {"ok": False, "mismatches": [],
                    "error": "could not restore absent project roots",
                    "still_present": still_present}
    return {**replay, "absent_restored": absent}


def _reset_to_canonical_artifact(vm, host_dir: str,
                                 authorized_instruction: str = "") -> dict:
    """Cross a fresh null-task VM boundary, then replay the canonical trees.

    This prevents post-verdict processes, temporary files, application state,
    or hidden home state from influencing the Curriculum Agent.  Only the
    explicitly frozen project archive is replayed into the fresh machine.
    """
    env = getattr(vm, "env", None)
    reset = getattr(env, "reset", None)
    if not callable(reset):
        return {"ok": False, "error": "VM environment has no reset boundary"}
    try:
        # Forge actions go directly through the guest HTTP controller rather
        # than DesktopEnv.step(), so DesktopEnv cannot infer that the machine
        # was mutated.  Force its reset optimization onto the snapshot-revert
        # path; otherwise reset(None) can incorrectly declare the dirty VM
        # clean and preserve the research branch we are trying to discard.
        env.is_environment_used = True
        reset(task_config=None)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False,
                "error": f"fresh phase VM reset failed: {exc}"}
    if hasattr(vm, "_conda"):
        vm._conda = ""                                   # reset shell state
    try:
        _assert_target_null_task(vm, authorized_instruction)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # Preserve the legacy two-argument seam for blind-mode transports and
    # test doubles; targeted mode alone needs the additional legality input.
    restored = (_restore_graded_artifact(
        vm, host_dir, authorized_instruction=authorized_instruction)
        if authorized_instruction else
        _restore_graded_artifact(vm, host_dir))
    if not restored.get("ok"):
        return {"ok": False, "error": "canonical replay after reset failed",
                "restore": restored}
    return {"ok": True, "restore": restored}


def _reset_after_reauthor_for_actor(
        vm, ep_no: int, root: str, pj, memory_dir: str,
        authorized_instruction: str = "") -> dict:
    """Cross a fresh boundary after Curriculum repairs Actor-facing input.

    A successful Curriculum turn may have touched arbitrary guest state,
    including ``~/.memory``. Replaying only the validated project archive and
    canonical host memory prevents that private phase from becoming an
    accidental teaching or fixture channel into the Actor turn.
    """
    env = getattr(vm, "env", None)
    reset = getattr(env, "reset", None)
    if not callable(reset):
        return {"ok": False, "error": "VM environment has no reset boundary"}
    try:
        env.is_environment_used = True
        reset(task_config=None)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False,
                "error": f"fresh Actor phase VM reset failed: {exc}"}
    if hasattr(vm, "_conda"):
        vm._conda = ""
    try:
        _assert_target_null_task(vm, authorized_instruction)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not _provision_workspace(vm, ep_no, authorized_instruction):
        return {"ok": False,
                "error": "fresh Actor phase workspace provisioning failed"}
    replay = None
    if pj is not None:
        replay = replay_project_materials(
            vm, os.path.join(root, "projects", pj["key"]))
        if not replay.get("ok"):
            return {"ok": False,
                    "error": "fresh Actor phase project replay failed",
                    "replay": replay}
    if not mem.push_memory(vm, memory_dir):
        return {"ok": False,
                "error": "fresh Actor phase memory restore failed"}
    try:
        _assert_target_null_task(vm, authorized_instruction)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "replay": replay}


def _validate_night_card(text: str, is_final_pack=None,
                         agent_decided_stop: bool = False,
                         authorized_instruction: str = "") -> dict:
    """FULL intake for one night section (M9: stored cards re-gate on their
    own boot). Host-side half; the VM gate runs separately."""
    from tools.exam_fence import audit_text
    reasons = []
    card = reward.parse_night_section(text, agent_decided_stop)
    if _has_terminal_run_script_trailer(text):
        reasons.append("transport: terminal [exit N] trailer")
    if not card["ok"]:
        reasons.append("parse: night section incomplete")
    if _has_actor_memory_dependency(text):
        reasons.append("v11b: memory reference (hard reject)")
    if _has_curriculum_private_dependency(text):
        reasons.append("curriculum-private archive dependency (hard reject)")
    hits = audit_text(
        text, mode="practice",
        authorized_instruction=authorized_instruction)
    if hits:
        reasons.append("fence: content outside the authorized boundary")
    if card["ok"]:
        if not agent_decided_stop:
            lo, hi = (MILESTONE_BUDGET if card["mode"] == "MILESTONE"
                      else DRILL_BUDGET)
            if not (lo <= card.get("budget", 0) <= hi):
                reasons.append(f"budget {card.get('budget')} outside [{lo},{hi}]")
        viol = static_token_screen(card["criteria"])
        if viol:
            reasons.append(f"token-screen: {viol}")
    return {"ok": not reasons, "card": card, "reasons": reasons,
            "fence_hit": bool(hits)}


def _project_transport_text(pj: dict) -> str:
    """Serialize only the Actor-facing fields of a live project."""
    n = pj["n_nights"]
    chunks = [f"PROJECT: {pj['name']}", f"NIGHTS: {n}",
              str(pj.get("target_prose") or "")]
    for i, night in enumerate(pj.get("night_cards") or [], 1):
        chunks.extend((f"NIGHT: {i}/{n}", str(night)))
    chunks.extend(("FINAL:", str(pj.get("final_text") or "")))
    return "\n".join(chunks).strip() + "\n"


def _validate_project_transport(
        text: str, expected_name: str, expected_nights: int,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Validate a complete, canonical Actor-facing project replacement.

    The result retains issue scopes so intake can distinguish a defect local
    to tonight's card from one that requires a complete project replacement.
    """
    from tools.exam_fence import audit_text

    parsed = reward.split_project_card(text)
    issues = []
    fence_hit = False
    hard_reject = False

    def reject(scope: str, reason: str, night: int = 0) -> None:
        issues.append({"scope": scope, "night": night, "reason": reason})

    if _has_terminal_run_script_trailer(text):
        reject("project", "project: terminal [exit N] transport trailer")
    if not parsed["ok"]:
        for reason in parsed["reasons"]:
            reject("project", reason)
        return {"ok": False, "project": parsed, "issues": issues,
                "reasons": [i["reason"] for i in issues],
                "fence_hit": False, "hard_reject": False,
                "quarantine": False}
    if parsed["name"] != expected_name:
        reject("project", f"project name {parsed['name']!r} does not match "
               f"live project {expected_name!r}")
    if parsed["n_nights"] != expected_nights:
        reject("project", f"NIGHTS {parsed['n_nights']} does not match live "
               f"project {expected_nights}")
    repairs = [n for n in parsed["nights"] if n["repair"]]
    if repairs:
        reject("project", "complete replacement must contain canonical "
               "NIGHT k/N sections only")

    semantic = reward.validate_project(parsed, agent_decided_stop)
    for reason in semantic["reasons"]:
        match = re.match(r"night (\d+)(?:-R)?:\s*(.*)", reason)
        if match:
            reject("night", match.group(2), int(match.group(1)))
        elif reason.startswith("final:"):
            reject("final", reason)
        else:
            reject("project", reason)

    # Keep one whole-transport fence pass in addition to the scoped passes
    # below. Tuple and shingle detectors may depend on tokens that straddle
    # target/night/final boundaries and must not be weakened by parsing.
    if audit_text(text, mode="practice",
                  authorized_instruction=authorized_instruction):
        fence_hit = True
        reject("project", "project: content outside the authorized boundary")

    fields = [("target", 0, parsed["target_prose"])]
    fields.extend(("night", n["k"], n["text"])
                  for n in parsed["nights"] if not n["repair"])
    fields.append(("final", 0, parsed["final_text"]))
    for scope, night, field_text in fields:
        label = f"night {night}" if scope == "night" else scope
        if _has_actor_memory_dependency(field_text):
            hard_reject = True
            reject(scope, f"{label}: memory reference (hard reject)", night)
        if _has_curriculum_private_dependency(field_text):
            hard_reject = True
            reject(scope, f"{label}: curriculum-private archive dependency "
                   "(hard reject)", night)
        if audit_text(field_text, mode="practice",
                      authorized_instruction=authorized_instruction):
            fence_hit = True
            reject(scope, f"{label}: content outside the authorized boundary",
                   night)
        if scope == "night":
            card = reward.parse_night_section(
                field_text, agent_decided_stop=agent_decided_stop)
            if card["ok"]:
                violations = static_token_screen(card["criteria"])
                if violations:
                    reject(scope, f"{label}: token-screen: {violations}", night)
        elif scope == "final":
            criteria = [line for line in field_text.splitlines()
                        if _NUM.match(line)]
            violations = static_token_screen(criteria)
            if violations:
                reject(scope, f"final: token-screen: {violations}")
    return {"ok": not issues, "project": parsed, "issues": issues,
            "reasons": [i["reason"] for i in issues],
            "fence_hit": fence_hit, "hard_reject": hard_reject,
            "quarantine": fence_hit}


_REAUTHOR_DECISION = re.compile(r"\A\s*DECISION:\s*REPAIR\s*\n")
_REAUTHOR_NIGHT = re.compile(
    r"\ANIGHT:\s*(\d+)\s*/\s*(\d+)\s*-\s*R\s*$", re.M)


def _parse_exact_repair_section(
        text: str, night_no: int, n_nights: int,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Validate one repair envelope and return its canonical card body.

    ``NIGHT: k/N-R`` is transport metadata.  At rest, ``pending_repair``
    contains only the MODE..SUCCESS body; the live project pointer is the
    authoritative identity.  Keeping this parser shared prevents ordinary
    rulings, legacy state, and intake reauthoring from assigning different
    meanings to the same repair card.
    """
    body = (text or "").strip()
    header = _REAUTHOR_NIGHT.match(body)
    if not header:
        return {"ok": False, "reasons": [
            "repair must start with exact NIGHT: k/N-R"]}
    if int(header.group(1)) != night_no or \
            int(header.group(2)) != n_nights:
        return {"ok": False, "reasons": [
            "repair NIGHT header does not match the live project pointer"]}
    section = body[header.end():].strip()
    if re.search(r"^(?:PROJECT:|NIGHT:|FINAL:)", section, re.M):
        return {"ok": False, "reasons": [
            "repair contains an extra project delimiter"]}
    checked = _validate_night_card(
        section, agent_decided_stop=agent_decided_stop,
        authorized_instruction=authorized_instruction)
    if not checked["ok"]:
        return {"ok": False, "reasons": checked["reasons"],
                "fence_hit": checked.get("fence_hit", False)}
    return {"ok": True, "section": section, "reasons": [],
            "card": checked["card"], "fence_hit": False}


def _parse_stored_pending_repair(
        text: str, pj: dict, agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Validate canonical pending state, accepting one safe legacy envelope.

    Older ordinary rulings stored the NIGHT transport header in project
    state.  An exact current header can be normalized losslessly; a wrong
    header or any trailing project section must fail before the dry gate.
    """
    raw = (text or "").strip()
    if raw.startswith("NIGHT:"):
        parsed = _parse_exact_repair_section(
            raw, int(pj["next_night"]), int(pj["n_nights"]),
            agent_decided_stop, authorized_instruction)
        if parsed["ok"]:
            parsed["legacy_header"] = True
        return parsed
    if re.search(r"^(?:PROJECT:|NIGHT:|FINAL:)", raw, re.M):
        return {"ok": False, "reasons": [
            "stored repair body contains a project delimiter"],
            "fence_hit": False}
    checked = _validate_night_card(
        raw, agent_decided_stop=agent_decided_stop,
        authorized_instruction=authorized_instruction)
    return {"ok": checked["ok"], "section": raw,
            "reasons": checked["reasons"], "card": checked["card"],
            "fence_hit": checked.get("fence_hit", False),
            "legacy_header": False}


def _parse_reauthor_output(
        text: str, pj: dict, night_no: int, require_full_project: bool,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Parse one strict Curriculum-authored intake correction.

    A PROJECT prefix is accepted only as a complete same-identity project;
    arbitrary wrappers are never searched for a night-shaped substring.
    """
    match = _REAUTHOR_DECISION.match(text or "")
    if not match:
        return {"ok": False, "reasons": [
            "reauthor output must start with DECISION: REPAIR"]}
    body = (text or "")[match.end():].strip()
    candidate = copy.deepcopy(pj)
    if body.startswith("PROJECT:"):
        checked = _validate_project_transport(
            body, pj["name"], pj["n_nights"], agent_decided_stop,
            authorized_instruction)
        if not checked["ok"]:
            return {"ok": False, "reasons": checked["reasons"]}
        project = checked["project"]
        historical = max(0, int(pj.get("next_night", 1)) - 1)
        for index in range(historical):
            if project["nights"][index]["text"] != \
                    pj["night_cards"][index]:
                return {"ok": False, "reasons": [
                    f"complete replacement changes historical night "
                    f"{index + 1}"]}
        candidate["target_prose"] = project["target_prose"]
        candidate["night_cards"] = [n["text"] for n in project["nights"]]
        candidate["final_text"] = project["final_text"]
        if candidate.get("pending_repair") is not None:
            # The full replacement's current NIGHT is the corrected live
            # transport; never retain and then execute a stale repair body.
            candidate["pending_repair"] = \
                candidate["night_cards"][night_no - 1]
        card_text = (candidate.get("pending_repair") or
                     candidate["night_cards"][night_no - 1])
        return {"ok": True, "kind": "project", "project": candidate,
                "card_text": card_text, "reasons": []}
    if body.startswith("NIGHT:"):
        if require_full_project:
            return {"ok": False, "reasons": [
                "stored project transport is invalid; reauthor must return "
                "a complete same-name PROJECT/NIGHTS/NIGHT/FINAL replacement"]}
        parsed = _parse_exact_repair_section(
            body, night_no, pj["n_nights"], agent_decided_stop,
            authorized_instruction)
        if not parsed["ok"]:
            return {"ok": False, "reasons": parsed["reasons"]}
        section = parsed["section"]
        if candidate.get("pending_repair") is not None:
            candidate["pending_repair"] = section
        else:
            candidate["night_cards"][night_no - 1] = section
        complete = _validate_project_transport(
            _project_transport_text(candidate), candidate["name"],
            candidate["n_nights"], agent_decided_stop,
            authorized_instruction)
        if not complete["ok"]:
            return {"ok": False, "reasons": complete["reasons"]}
        return {"ok": True, "kind": "night", "project": candidate,
                "card_text": section, "reasons": []}
    return {"ok": False, "reasons": [
        "reauthor body must be either a complete PROJECT replacement or "
        "an exact NIGHT: k/N-R card"]}


def _parse_curriculum_repair_ruling(
        text: str, pj: dict, agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Parse a normal REPAIR ruling without mutating live project state.

    A local repair is one exact current-night envelope.  A Curriculum Agent
    that also needs to alter target/future-night/FINAL transport must return a
    complete same-name project replacement, which receives the existing
    history and whole-project validation checks.
    """
    night_no = int(pj["next_night"])
    parsed = _parse_reauthor_output(
        text, pj, night_no, False, agent_decided_stop,
        authorized_instruction)
    if not parsed["ok"]:
        return parsed
    candidate = (parsed["project"] if parsed["kind"] == "project"
                 else copy.deepcopy(pj))
    candidate["pending_repair"] = parsed["card_text"]
    return {"ok": True, "kind": parsed["kind"], "project": candidate,
            "card_text": parsed["card_text"], "reasons": []}


_CONTINUE_DECISION = re.compile(
    r"\A\s*DECISION:\s*CONTINUE\s*(?:\n|\Z)")
_CONTINUE_NIGHT = re.compile(
    r"^NIGHT:\s*(\d+)\s*/\s*(\d+)\s*$", re.M)
_CONTINUE_FINAL = re.compile(r"^FINAL:\s*$", re.M)


def _parse_curriculum_continue_ruling(
        text: str, pj: dict, agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Validate a scoped amendment to the *upcoming* night on CONTINUE.

    The Curriculum Agent may naturally restate the same PROJECT/NIGHTS/target
    wrapper, as E13 did, but it may author exactly one future ``NIGHT: k/N``
    and an optional FINAL amendment. Historical/current nights and project
    identity are never inferred from a partial transport. The returned
    candidate has passed the same whole-project content and static gates as a
    complete replacement; the next episode still performs its fresh-VM dry
    gate before exposing the card to the Actor Agent.
    """
    match = _CONTINUE_DECISION.match(text or "")
    if not match:
        return {"ok": False, "attempted": False, "reasons": [
            "continue output must start with DECISION: CONTINUE"]}
    body = (text or "")[match.end():].strip()
    lines = body.splitlines()
    while lines and lines[0].strip() in ("", "---"):
        lines.pop(0)
    while lines and lines[-1].strip() in ("", "---"):
        lines.pop()
    body = "\n".join(lines).strip()

    delimiter_attempt = bool(re.search(
        r"^(?:PROJECT:|NIGHTS:|NIGHT:|FINAL:)", body, re.M))
    if not delimiter_attempt:
        # Ordinary rationale after a ruling is journal material, not an
        # implicit mutation of stored Actor-facing transport.
        return {"ok": True, "attempted": False,
                "project": copy.deepcopy(pj), "reasons": [],
                "night_amended": False, "final_amended": False}

    project_headers = list(re.finditer(r"^PROJECT:\s*(.*?)\s*$", body, re.M))
    nights_headers = list(re.finditer(r"^NIGHTS:\s*(.*?)\s*$", body, re.M))
    night_headers = list(re.finditer(r"^NIGHT:.*$", body, re.M))
    final_headers = list(_CONTINUE_FINAL.finditer(body))
    if len(project_headers) > 1 or len(nights_headers) > 1 or \
            len(night_headers) > 1 or len(final_headers) > 1:
        return {"ok": False, "attempted": True, "reasons": [
            "CONTINUE amendment permits at most one PROJECT, NIGHTS, "
            "upcoming NIGHT, and FINAL delimiter"]}
    if project_headers and project_headers[0].group(1) != pj["name"]:
        return {"ok": False, "attempted": True, "reasons": [
            "CONTINUE amendment PROJECT does not match the live project"]}
    if nights_headers:
        try:
            declared_nights = int(nights_headers[0].group(1))
        except ValueError:
            declared_nights = -1
        if declared_nights != int(pj["n_nights"]):
            return {"ok": False, "attempted": True, "reasons": [
                "CONTINUE amendment NIGHTS does not match the live project"]}

    upcoming = _CONTINUE_NIGHT.search(body)
    if night_headers and upcoming is None:
        return {"ok": False, "attempted": True, "reasons": [
            "CONTINUE amendment NIGHT must be exact NIGHT: k/N without -R"]}
    if upcoming and (int(upcoming.group(1)) != int(pj["next_night"]) + 1
                     or int(upcoming.group(2)) != int(pj["n_nights"])):
        return {"ok": False, "attempted": True, "reasons": [
            "CONTINUE amendment NIGHT is not the exact upcoming pointer"]}
    if upcoming is not None and int(pj["next_night"]) >= int(pj["n_nights"]):
        return {"ok": False, "attempted": True, "reasons": [
            "final live night has no upcoming NIGHT to amend"]}

    if upcoming is not None and final_headers and \
            final_headers[0].start() < upcoming.start():
        return {"ok": False, "attempted": True, "reasons": [
            "FINAL cannot precede the upcoming NIGHT"]}
    sections = [item for item in (
        upcoming, final_headers[0] if final_headers else None)
                if item is not None]
    sections.sort(key=lambda item: item.start())
    prefix_end = sections[0].start() if sections else len(body)
    prefix = body[:prefix_end].strip()
    prefix_lines = prefix.splitlines()
    if prefix_lines and prefix_lines[0].startswith("PROJECT:"):
        prefix_lines.pop(0)
    if prefix_lines and prefix_lines[0].startswith("NIGHTS:"):
        prefix_lines.pop(0)
    wrapper_prose = "\n".join(prefix_lines).strip()
    if wrapper_prose and wrapper_prose != \
            str(pj.get("target_prose") or "").strip():
        return {"ok": False, "attempted": True, "reasons": [
            "partial CONTINUE wrapper changes or adds target prose"]}
    if prefix and not (project_headers or nights_headers or wrapper_prose):
        return {"ok": False, "attempted": True, "reasons": [
            "unexpected text before CONTINUE amendment"]}

    candidate = copy.deepcopy(pj)
    night_amended = False
    if upcoming is not None:
        end = final_headers[0].start() if final_headers else len(body)
        section = body[upcoming.end():end].strip()
        checked = _validate_night_card(
            section, agent_decided_stop=agent_decided_stop,
            authorized_instruction=authorized_instruction)
        if not checked["ok"]:
            return {"ok": False, "attempted": True,
                    "reasons": checked["reasons"]}
        candidate["night_cards"][int(upcoming.group(1)) - 1] = section
        night_amended = True

    final_amended = False
    if final_headers:
        new_final = body[final_headers[0].end():].strip()
        if not new_final:
            return {"ok": False, "attempted": True, "reasons": [
                "CONTINUE FINAL amendment is empty"]}
        candidate["final_text"] = new_final
        final_amended = True

    checked = _validate_project_transport(
        _project_transport_text(candidate), candidate["name"],
        candidate["n_nights"], agent_decided_stop,
        authorized_instruction)
    if not checked["ok"]:
        return {"ok": False, "attempted": True,
                "reasons": checked["reasons"]}
    return {"ok": True, "attempted": True, "project": candidate,
            "reasons": [], "night_amended": night_amended,
            "final_amended": final_amended}


def _reauthor_contract(pj: dict, night_no: int, card_text: str,
                       require_full_project: bool) -> str:
    """Tell Curriculum which one of the two strict handoff forms is legal."""
    gate_law = (
        "DRY-GATE LAW: every work-dependent criterion must print FAIL before "
        "Actor work. A legitimate precondition that should already print "
        "PASS must end with # GUARD. The returned current NIGHT, plus FINAL "
        "when this is the final night, will be dry-gated before persistence.\n\n")
    if require_full_project:
        return gate_law + (
            "STORED PROJECT TRANSPORT IS INVALID. Write exactly DECISION: "
            "REPAIR followed by a COMPLETE replacement PROJECT card with the "
            "same PROJECT name and NIGHTS count, including target prose, every "
            "NIGHT section, and FINAL. The replacement must remove every "
            "reported dependency; partial PROJECT wrappers are rejected.\n\n"
            "CURRENT COMPLETE PROJECT TRANSPORT:\n" +
            _project_transport_text(pj))
    return gate_law + (
        f"ONLY THE ACTIVE NIGHT NEEDS CORRECTION. The live pointer remains "
        f"{night_no}/{pj['n_nights']}. Write exactly DECISION: REPAIR as the "
        f"first nonempty line, then literal NIGHT: "
        f"{night_no}/{pj['n_nights']}-R as the next line, then the complete "
        "corrected night body. Keep RATIONALE to its own one-line field and "
        "include at least one separate task-prose line before SUCCESS; prose "
        "folded into RATIONALE is an incomplete card. The REP field counts "
        "attempts; it never changes the NIGHT pointer. Do not wrap it or add "
        "FINAL, PROJECT, TARGET, REPAIR, or CARD headings.\n\n"
        "CURRENT ACTIVE NIGHT BODY:\n" +
        card_text)


def validate_new_project_candidate(root: str, text: str,
                                   agent_decided_stop: bool = False,
                                   authorized_instruction: str = "") -> dict:
    """Validate a new-project draft without reading or changing the guest.

    This is deliberately separate from fixture capture.  Curriculum may use
    the exact ordered reasons to revise an invalid draft repeatedly, while a
    rejected draft remains incapable of creating project lineage state.
    """
    from tools.exam_fence import audit_text
    if audit_text(text, mode="practice",
                  authorized_instruction=authorized_instruction):
        return {"ok": False, "reasons": [
            "project: content outside the authorized boundary"],
            "fence_hit": True, "hard_reject": False,
            "quarantine": True}
    if _has_curriculum_private_dependency(text):
        return {"ok": False, "reasons": [
            "curriculum-private archive dependency (hard reject)"],
            "fence_hit": False, "hard_reject": True,
            "quarantine": False}
    if _has_actor_memory_dependency(text):
        return {"ok": False,
                "reasons": ["memory reference (hard reject)"],
                "fence_hit": False, "hard_reject": True,
                "quarantine": False}
    pc = reward.split_project_card(text)
    if not pc["ok"]:
        return {"ok": False, "reasons": pc["reasons"],
                "fence_hit": False, "hard_reject": False,
                "quarantine": False}
    transport = _validate_project_transport(
        text, pc["name"], pc["n_nights"], agent_decided_stop,
        authorized_instruction)
    if not transport["ok"]:
        return {"ok": False, "reasons": transport["reasons"],
                "fence_hit": transport.get("fence_hit", False),
                "hard_reject": transport.get("hard_reject", False),
                "quarantine": transport.get("quarantine", False)}
    key = pc["name"]
    pdir = os.path.join(root, "projects", key)
    ppath = os.path.join(root, "curriculum", f"project_{key}.json")
    # A project key is immutable lineage state. Capture into disposable
    # staging first: neither a rejected fixture nor a repeated name may alter
    # the last accepted project archive.
    if os.path.lexists(pdir) or os.path.lexists(ppath):
        return {"ok": False,
                "reasons": ["project key already exists in this lineage"],
                "fence_hit": False, "hard_reject": False,
                "quarantine": False}
    return {"ok": True, "reasons": [], "card": pc, "key": key,
            "candidate_sha256": hashlib.sha256(
                text.encode("utf-8")).hexdigest(),
            "project_dir": pdir, "project_path": ppath,
            "fence_hit": False, "hard_reject": False,
            "quarantine": False}


def _publish_validated_new_project(vm, ep_no: int, root: str, text: str,
                                   checked: dict,
                                   authorized_instruction: str = "",
                                   journal_label: str = "") -> dict:
    """Capture and publish one already statically validated candidate."""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != \
            checked.get("candidate_sha256"):
        return {"ok": False, "reasons": [
            "validated project candidate bytes changed before capture"]}
    pc = checked["card"]
    key = checked["key"]
    pdir = checked["project_dir"]
    ppath = checked["project_path"]
    projects_dir = os.path.dirname(pdir)
    os.makedirs(projects_dir, exist_ok=True)
    os.makedirs(os.path.dirname(ppath), exist_ok=True)
    candidate = tempfile.mkdtemp(prefix=f".{key}.candidate-",
                                 dir=projects_dir)
    # Capture every referenced top-level fixture root, including tilde and
    # $HOME spellings (E12 used ~/Desktop exclusively).
    dirs, owned_paths = _reauthor_recapture_scope({
        "target_prose": pc["target_prose"],
        "night_cards": [n["text"] for n in pc["nights"]],
        "final_text": pc["final_text"],
    }, "")
    try:
        cap = capture_project_materials(
            vm, key, candidate, guest_dirs=dirs)
        if not cap.get("ok"):
            return {"ok": False, "reasons": [f"capture failed: {cap}"]}
        fixture_hits = audit_captured_materials(
            candidate, authorized_instruction=authorized_instruction)
        if fixture_hits:
            return {
                "ok": False,
                "reasons": ["captured fixture failed the content boundary"],
                "fence_hit": True,
                "quarantine": True,
                "quarantine_kind": "captured-fixture",
            }
        write_owned_paths(candidate, owned_paths)
        # Recheck after the potentially slow guest capture. os.rename then
        # publishes the already-audited directory as one host operation.
        if os.path.lexists(pdir) or os.path.lexists(ppath):
            return {"ok": False,
                    "reasons": ["project key already exists in this lineage"]}
        os.rename(candidate, pdir)
        candidate = ""
    except (OSError, ValueError) as exc:
        return {"ok": False,
                "reasons": [f"project archive promotion failed: {exc}"]}
    finally:
        if candidate:
            shutil.rmtree(candidate, ignore_errors=True)
    pj = {"name": key, "n_nights": pc["n_nights"], "key": key,
          "target_prose": pc["target_prose"],
          "night_cards": [n["text"] for n in pc["nights"]],
          "final_text": pc["final_text"], "decisions": [],
          "status": "accepted", "next_night": 1, "pending_repair": None,
          "born_ep": ep_no,
          "artifact_semantics": _ARTIFACT_SEMANTICS}
    save_project(ppath, pj)
    project_journal = (f"{journal_label}_project.md" if journal_label else
                       f"journal_ep{ep_no:03d}_project.md")
    open(os.path.join(root, "curriculum", project_journal), "w").write(text)
    return {"ok": True, "project": pj, "path": ppath,
            "captured": cap.get("files")}


def accept_new_project(vm, ep_no: int, root: str, text: str,
                       agent_decided_stop: bool = False,
                       authorized_instruction: str = "") -> dict:
    """B7 acceptance: validate, then capture materials once on THIS boot."""
    checked = validate_new_project_candidate(
        root, text, agent_decided_stop, authorized_instruction)
    if not checked["ok"]:
        return {"ok": False, "reasons": checked["reasons"]}
    return _publish_validated_new_project(
        vm, ep_no, root, text, checked, authorized_instruction)


def _recapture_project_materials(vm, project_key: str, project_dir: str,
                                 guest_dirs: list[str],
                                 owned_paths: list[str],
                                 authorized_instruction: str = "",
                                 retain_previous: bool = False) -> dict:
    """Capture and audit a candidate before replacing canonical state."""
    parent = os.path.dirname(project_dir)
    os.makedirs(parent, exist_ok=True)
    candidate = tempfile.mkdtemp(
        prefix=f".{project_key}.candidate-", dir=parent)
    backup = candidate + ".previous"
    promoted = False
    preserve_backup = False
    try:
        result = capture_project_materials(
            vm, project_key, candidate, guest_dirs=guest_dirs)
        if not result.get("ok"):
            return result
        if audit_captured_materials(
                candidate,
                authorized_instruction=authorized_instruction):
            return {**result, "ok": False,
                    "error": "captured fixture failed the content boundary"}
        write_owned_paths(candidate, owned_paths)
        try:
            os.replace(project_dir, backup)
            os.replace(candidate, project_dir)
            promoted = True
        except OSError as exc:
            if os.path.exists(backup) and not os.path.exists(project_dir):
                try:
                    os.replace(backup, project_dir)
                except OSError as rollback_exc:
                    # The canonical pathname is unavailable, but the last
                    # accepted bytes remain recoverable at ``backup``. Never
                    # delete them in the cleanup path.
                    preserve_backup = True
                    return {**result, "ok": False,
                            "error": ("candidate archive promotion and "
                                      f"rollback failed: {exc}; "
                                      f"rollback: {rollback_exc}"),
                            "recovery_backup": backup}
            return {**result, "ok": False,
                    "error": f"candidate archive promotion failed: {exc}"}
        if retain_previous:
            preserve_backup = True
            return {**result, "previous_archive": backup}
        shutil.rmtree(backup)
        return result
    finally:
        if not promoted:
            shutil.rmtree(candidate, ignore_errors=True)
        if os.path.exists(backup) and not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)


def _republish_project_materials_from_baseline(
        project_key: str, project_dir: str, owned_paths: list[str],
        guarded_refs: list[str] | tuple[str, ...] = (),
        authorized_instruction: str = "",
        retain_previous: bool = False) -> dict:
    """Reframe ownership without reading any existing-project guest tree.

    The accepted tar, manifest, and fixture-root set are immutable for the
    project lifetime.  A repaired transport may add safe Actor-output paths to
    the wipe/grade boundary, but those paths begin absent on every fresh night;
    they are never captured from Actor or Curriculum guest edits.
    """
    parent = os.path.dirname(project_dir)
    os.makedirs(parent, exist_ok=True)
    candidate = tempfile.mkdtemp(
        prefix=f".{project_key}.candidate-", dir=parent)
    backup = candidate + ".previous"
    promoted = False
    preserve_backup = False
    try:
        try:
            shutil.copytree(project_dir, candidate, dirs_exist_ok=True)
        except OSError as exc:
            return {"ok": False,
                    "error": f"accepted fixture baseline copy failed: {exc}"}
        roots = read_roots(candidate)
        manifest = read_manifest(candidate)
        previous_owned = read_owned_paths(candidate)
        if not roots or not manifest or not previous_owned:
            return {"ok": False,
                    "error": "accepted fixture baseline is incomplete"}
        findings = audit_captured_materials(
            candidate, authorized_instruction=authorized_instruction)
        if findings:
            return {"ok": False,
                    "error": "accepted fixture failed the content boundary",
                    "audit": findings}
        try:
            with tarfile.open(os.path.join(candidate, "materials.tgz"),
                              mode="r:gz") as archive:
                fixture_paths = {
                    member.name.removeprefix("./").rstrip("/")
                    for member in archive.getmembers()
                    if member.name.removeprefix("./").rstrip("/")}
        except (OSError, tarfile.TarError) as exc:
            return {"ok": False,
                    "error": ("accepted fixture baseline is unreadable: "
                              f"{type(exc).__name__}")}
        missing_guards = sorted(ref for ref in set(guarded_refs)
                                if ref not in fixture_paths
                                and not any(path.startswith(ref + "/")
                                            for path in fixture_paths))
        if missing_guards:
            return {
                "ok": False,
                "error": ("repair introduces a GUARD dependency outside the "
                          f"accepted fixture baseline: {missing_guards}"),
            }
        # Ownership is monotone: a later card cannot release a path that an
        # earlier Actor may have populated. Fixture roots are included even
        # for legacy archives whose old OWNED record omitted them.
        effective_owned = sorted(
            set(previous_owned) | set(roots) | set(owned_paths))
        try:
            write_owned_paths(candidate, effective_owned)
        except ValueError as exc:
            return {"ok": False,
                    "error": f"repaired ownership boundary is invalid: {exc}"}
        try:
            os.replace(project_dir, backup)
            os.replace(candidate, project_dir)
            promoted = True
        except OSError as exc:
            if os.path.exists(backup) and not os.path.exists(project_dir):
                try:
                    os.replace(backup, project_dir)
                except OSError as rollback_exc:
                    preserve_backup = True
                    return {
                        "ok": False,
                        "error": ("baseline archive promotion and rollback "
                                  f"failed: {exc}; rollback: {rollback_exc}"),
                        "recovery_backup": backup,
                    }
            return {"ok": False,
                    "error": f"baseline archive promotion failed: {exc}"}
        result = {
            "ok": True,
            "files": len(manifest),
            "fixture_source": "accepted_baseline",
            "roots": roots,
            "owned_paths": effective_owned,
        }
        if retain_previous:
            preserve_backup = True
            return {**result, "previous_archive": backup}
        shutil.rmtree(backup)
        return result
    finally:
        if not promoted:
            shutil.rmtree(candidate, ignore_errors=True)
        if os.path.exists(backup) and not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)


def _persist_reauthored_project(
        _vm, root: str, ppath: str, live_project_state: dict,
        candidate_project_state: dict, card_text: str,
        authorized_instruction: str = "") -> dict:
    """Publish corrected transport over immutable accepted fixtures.

    Every existing-project correction is card/ownership policy only. Neither
    a verdict-time Actor tree nor an intake-time Curriculum edit is a legal
    fixture source; changing fixture bytes requires a new project lineage key.
    """
    _, owned_paths = _reauthor_recapture_scope(
        candidate_project_state, card_text)
    project_dir = os.path.join(
        root, "projects", live_project_state["key"])
    captured = _republish_project_materials_from_baseline(
        live_project_state["key"], project_dir, owned_paths,
        _guarded_project_refs(candidate_project_state),
        authorized_instruction=authorized_instruction,
        retain_previous=True)
    if not captured.get("ok"):
        return {**captured, "persisted": False}
    try:
        save_project(ppath, candidate_project_state)
    except OSError as exc:
        previous = captured.get("previous_archive")
        rollback_error = ""
        if previous and os.path.isdir(previous):
            rejected = tempfile.mkdtemp(
                prefix=f".{live_project_state['key']}.rejected-",
                dir=os.path.dirname(project_dir))
            os.rmdir(rejected)
            try:
                os.replace(project_dir, rejected)
                os.replace(previous, project_dir)
                shutil.rmtree(rejected, ignore_errors=True)
            except OSError as rollback_exc:
                rollback_error = f"; archive rollback failed: {rollback_exc}"
        return {**captured, "ok": False, "persisted": False,
                "error": (f"project transport persistence failed: {exc}"
                          f"{rollback_error}")}
    previous = captured.get("previous_archive")
    if previous:
        shutil.rmtree(previous, ignore_errors=True)
    live_project_state.clear()
    live_project_state.update(candidate_project_state)
    return {**captured, "persisted": True}


# -------------------------------------------------------------- preflight --
_PREFLIGHT_RETRYABLE = {
    "preflight_incomplete",
    "preflight_provision_failed",
    "preflight_replay_failed",
}

# A successful REPAIR may itself expose a stronger counterexample before any
# Actor work begins.  An explicit follow-up preflight is therefore a semantic
# refinement, not a retry of a failed Agent boundary.  KEEP remains terminal:
# it is Curriculum's ruling that the currently served card survived review.
_PREFLIGHT_REFINABLE = {
    "preflight_repaired",
}


def _read_jsonl_strict(path: str) -> list[tuple[dict, str]]:
    """Read an Agent-boundary ledger without repairing or dropping bytes."""
    if not os.path.lexists(path):
        return []
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"unsafe ledger path: {path}")
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            raw = line.rstrip("\r\n")
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid ledger row {line_no}: {path}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"non-object ledger row {line_no}: {path}")
            rows.append((row, raw))
    return rows


def _preflight_attempt_slot(root: str, ep_no: int, project_name: str,
                            refine_repaired: bool = False) -> dict:
    """Allocate the next immutable preflight attempt for one episode slot.

    The original pilot used ``preflights/epNNN`` directly.  That directory is
    retained as attempt 1; retries use children named ``attempt_002`` onward.
    A correction retry is legal after a recorded non-terminal infrastructure
    or authoring failure.  A semantic refinement is also legal after REPAIR,
    but only while the wrapper separately proves that the repaired Night 1 has
    still never reached an Actor.  KEEP and quarantined rulings are terminal.
    """
    ledger_path = os.path.join(root, "preflights.jsonl")
    ledger = _read_jsonl_strict(ledger_path)
    relevant = []
    for row, raw in ledger:
        if row.get("record_type") != "curriculum_preflight":
            continue
        try:
            row_ep = int(row.get(
                "preflight_for_episode", row.get("episode", -1)))
        except (TypeError, ValueError):
            raise ValueError("invalid Curriculum preflight episode")
        if row_ep == ep_no:
            relevant.append((row, raw))

    base_rel = os.path.join("preflights", f"ep{ep_no:03d}")
    base = os.path.join(root, base_rel)
    if not relevant:
        if refine_repaired:
            raise ValueError(
                "Curriculum preflight refinement requires a repaired prior")
        if os.path.lexists(base):
            raise ValueError(
                "orphan Curriculum preflight artifacts lack a ledger row")
        return {
            "attempt": 1,
            "artifact_root": base,
            "artifact_rel": base_rel,
            "journal_label": f"preflight_ep{ep_no:03d}",
            "prior": None,
            "prior_sha256": None,
            "continuation_kind": "initial",
            "repair_refinement_from_attempt": None,
            "refinement_source": None,
        }

    if os.path.islink(base) or not os.path.isdir(base):
        raise ValueError("Curriculum preflight attempt-1 artifacts are unsafe")
    previous_raw = None
    for index, (row, raw) in enumerate(relevant, 1):
        declared = row.get("preflight_attempt", 1 if index == 1 else None)
        if declared != index:
            raise ValueError(
                "Curriculum preflight attempts are duplicate or out of order")
        if row.get("project_before") != project_name:
            raise ValueError("Curriculum preflight project identity drifted")
        expected_rel = (base_rel if index == 1 else os.path.join(
            base_rel, f"attempt_{index:03d}"))
        declared_rel = row.get("preflight_artifact_root", expected_rel)
        if declared_rel != expected_rel:
            raise ValueError("Curriculum preflight artifact root drifted")
        expected = os.path.join(root, expected_rel)
        if os.path.islink(expected) or not os.path.isdir(expected):
            raise ValueError(
                "Curriculum preflight ledger lacks its attempt artifacts")
        if previous_raw is not None:
            parent_hash = hashlib.sha256(
                previous_raw.encode("utf-8")).hexdigest()
            claimed_parent = (
                row.get("preflight_parent_row_sha256") or
                row.get("retry_parent_row_sha256") or
                row.get("refinement_parent_row_sha256"))
            if claimed_parent != parent_hash:
                raise ValueError(
                    "Curriculum preflight parent row hash drifted")
        previous_raw = raw

    prior, prior_raw = relevant[-1]
    prior_status = prior.get("status")
    if prior_status not in (_PREFLIGHT_RETRYABLE | _PREFLIGHT_REFINABLE):
        raise ValueError(
            "Curriculum preflight episode already has a terminal ruling")
    refinement_source = None
    refinement_from_attempt = None
    if prior_status in _PREFLIGHT_REFINABLE:
        if not refine_repaired:
            raise ValueError(
                "Curriculum preflight episode already has a terminal ruling")
        refinement_source = (prior, prior_raw)
        refinement_from_attempt = len(relevant)
        continuation_kind = "repair_refinement"
    else:
        refinement_from_attempt = prior.get(
            "repair_refinement_from_attempt")
        if refinement_from_attempt is not None:
            if not refine_repaired:
                raise ValueError(
                    "Curriculum preflight refinement retry requires explicit "
                    "authorization")
            if (not isinstance(refinement_from_attempt, int) or
                    refinement_from_attempt < 1 or
                    refinement_from_attempt > len(relevant)):
                raise ValueError(
                    "Curriculum preflight refinement source is invalid")
            refinement_source = relevant[refinement_from_attempt - 1]
        elif refine_repaired:
            raise ValueError(
                "Curriculum preflight refinement requires a repaired prior")
        continuation_kind = "failure_retry"
    attempt = len(relevant) + 1
    artifact_rel = os.path.join(base_rel, f"attempt_{attempt:03d}")
    artifact_root = os.path.join(root, artifact_rel)
    if os.path.lexists(artifact_root):
        raise ValueError("orphan Curriculum preflight retry artifacts exist")
    return {
        "attempt": attempt,
        "artifact_root": artifact_root,
        "artifact_rel": artifact_rel,
        "journal_label": (
            f"preflight_ep{ep_no:03d}_attempt{attempt:03d}"),
        "prior": prior,
        "prior_sha256": hashlib.sha256(
            prior_raw.encode("utf-8")).hexdigest(),
        "continuation_kind": continuation_kind,
        "repair_refinement_from_attempt": refinement_from_attempt,
        "refinement_source": refinement_source,
    }


def _preflight_journal_label(ep_no: int, attempt: int) -> str:
    return (f"preflight_ep{ep_no:03d}" if attempt == 1 else
            f"preflight_ep{ep_no:03d}_attempt{attempt:03d}")


def _verify_preflight_refinement_source(
        root: str, ep_no: int, pj: dict, slot: dict,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Bind a repair refinement to its immutable accepted candidate.

    A nonempty ``pending_repair`` is not enough: host state could have drifted
    after publication.  The exact accepted instance journal, its durable
    receipt, the prior ledger ruling, and the currently served project state
    must all describe the same detached REPAIR transition.
    """
    source = slot.get("refinement_source")
    if not source:
        raise ValueError(
            "Curriculum preflight refinement lacks a source repair")
    row, _row_raw = source
    source_attempt = slot.get("repair_refinement_from_attempt")
    required = {
        "graded": False,
        "status": "preflight_repaired",
        "decision": "REPAIR",
        "repair_card": "accepted",
        "repair_recaptured": True,
        "repair_fixture_source": "accepted_baseline",
        "preflight_repair_terminal": "validated",
        "project_before": pj.get("name"),
        "project_after": pj.get("name"),
        "night_before": 1,
        "night_after": 1,
        "actor_ran": False,
        "verifier_ran": False,
        "evaluator_ran": False,
        "curve_written": False,
    }
    if any(row.get(key) != value for key, value in required.items()):
        raise ValueError(
            "Curriculum preflight refinement lacks a proven repair")
    if row.get("repair_transport") not in {"night", "project"}:
        raise ValueError(
            "Curriculum preflight refinement repair transport is invalid")
    if row.get("preflight_attempt", 1) != source_attempt:
        raise ValueError(
            "Curriculum preflight refinement source attempt drifted")

    receipts = row.get("preflight_repair_candidate_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ValueError(
            "Curriculum preflight refinement receipt is missing")
    receipt = receipts[-1]
    candidate_sha = receipt.get("candidate_sha256")
    if (receipt.get("status") != "validated" or
            receipt.get("decision") != "REPAIR" or
            receipt.get("expected_decision") != "REPAIR" or
            receipt.get("repair_kind") != row.get("repair_transport") or
            not isinstance(candidate_sha, str) or
            re.fullmatch(r"[0-9a-f]{64}", candidate_sha) is None):
        raise ValueError(
            "Curriculum preflight refinement receipt is invalid")

    artifact_rel = row.get("preflight_artifact_root")
    receipt_rel = row.get("preflight_repair_candidate_receipt_path")
    if not isinstance(artifact_rel, str) or not isinstance(receipt_rel, str):
        raise ValueError(
            "Curriculum preflight refinement receipt path is missing")
    if (os.path.isabs(receipt_rel) or
            os.path.normpath(receipt_rel).startswith(".." + os.sep)):
        raise ValueError(
            "Curriculum preflight refinement receipt path is unsafe")
    artifact_root = os.path.abspath(os.path.join(root, artifact_rel))
    receipt_path = os.path.abspath(os.path.join(artifact_root, receipt_rel))
    if os.path.commonpath((artifact_root, receipt_path)) != artifact_root:
        raise ValueError(
            "Curriculum preflight refinement receipt path escapes attempt")
    if (os.path.islink(receipt_path) or
            not os.path.isfile(receipt_path)):
        raise ValueError(
            "Curriculum preflight refinement receipt path is unsafe")
    try:
        with open(receipt_path, encoding="utf-8") as stream:
            durable_receipts = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Curriculum preflight refinement receipt is unreadable") from exc
    if durable_receipts != receipts:
        raise ValueError(
            "Curriculum preflight refinement receipt bytes drifted")

    label = _preflight_journal_label(ep_no, int(source_attempt))
    instance_rel = os.path.join("curriculum", f"{label}_instance.md")
    notes_rel = os.path.join("journal", f"{label}_notes.md")
    instance_path = os.path.join(root, instance_rel)
    notes_path = os.path.join(root, notes_rel)
    if os.path.islink(instance_path) or not os.path.isfile(instance_path):
        raise ValueError(
            "Curriculum preflight refinement instance is unsafe")
    if os.path.lexists(notes_path) and (
            os.path.islink(notes_path) or not os.path.isfile(notes_path)):
        raise ValueError(
            "Curriculum preflight refinement notes are unsafe")
    try:
        with open(instance_path, "rb") as stream:
            instance_bytes = stream.read()
        instance = instance_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "Curriculum preflight refinement instance is unreadable") from exc
    if hashlib.sha256(instance_bytes).hexdigest() != candidate_sha:
        raise ValueError(
            "Curriculum preflight refinement candidate hash drifted")

    parsed = _parse_curriculum_repair_ruling(
        instance, pj, agent_decided_stop, authorized_instruction)
    if (not parsed.get("ok") or
            parsed.get("kind") != row.get("repair_transport") or
            parsed.get("project") != pj):
        raise ValueError(
            "Curriculum preflight refinement project state drifted")
    state_hash = hashlib.sha256(json.dumps(
        pj, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    prior_state_hash = row.get("project_state_after_sha256")
    if prior_state_hash and prior_state_hash != state_hash:
        raise ValueError(
            "Curriculum preflight refinement project hash drifted")
    return {
        "source_attempt": source_attempt,
        "source_candidate_sha256": candidate_sha,
        "instance_journal_path": instance_rel,
        "notes_journal_path": (
            notes_rel if os.path.isfile(notes_path) else None),
        "project_state_sha256": state_hash,
    }


def _assert_preflight_episode_slot_unused(root: str, ep_no: int,
                                          born_ep: int) -> None:
    """Prove the requested number has never entered ordinary lifecycle."""
    collisions = []
    episode_dir = os.path.join(root, "episodes", f"ep{ep_no:03d}")
    if os.path.lexists(episode_dir):
        collisions.append(os.path.relpath(episode_dir, root))
    for suffix in (
            os.path.join("journal", f"ep{ep_no:03d}.tgz"),
            os.path.join("verdicts", f"ep{ep_no:03d}.txt")):
        if os.path.lexists(os.path.join(root, suffix)):
            collisions.append(suffix)

    episode_rows = _read_jsonl_strict(os.path.join(root, "episodes.jsonl"))
    curve_rows = _read_jsonl_strict(os.path.join(root, "curve.jsonl"))
    if any(row.get("episode") == ep_no for row, _ in episode_rows):
        collisions.append("episodes.jsonl")
    if any(row.get("episode") == ep_no for row, _ in curve_rows):
        collisions.append("curve.jsonl")
    if collisions:
        raise ValueError(
            "Curriculum preflight episode slot is already used: " +
            ", ".join(collisions))

    observed = [born_ep]
    for row, _raw in episode_rows:
        value = row.get("episode")
        if isinstance(value, int):
            observed.append(value)
    if ep_no != max(observed, default=-1) + 1:
        raise ValueError(
            "Curriculum preflight must target the next ordinary episode")


def e7_curriculum_preflight(
        vm, ep_no: int, ccfg, root: str, preflight_note: str,
        curriculum_research_direction: str = "",
        curriculum_target_task: str = "",
        refine_repaired: bool = False) -> dict:
    """Run one formal pre-Night-1 Curriculum ruling and no other role.

    This deliberately does not read or push Actor memory and has no path to
    Actor, Verifier, grading, evaluator, curve, or episode-lifecycle code.
    The CLI owns the lineage lock and separate preflight ledger append.
    """
    target_task = curriculum_target_task
    _assert_target_null_task(vm, target_task)
    if not preflight_note.strip():
        raise ValueError("Curriculum preflight note is empty")
    from tools.exam_fence import audit_text
    if audit_text(preflight_note, mode="practice",
                  authorized_instruction=target_task):
        raise ValueError("Curriculum preflight note failed content boundary")

    curriculum_dir = os.path.join(root, "curriculum")
    active = []
    for name in sorted(os.listdir(curriculum_dir)) \
            if os.path.isdir(curriculum_dir) else []:
        if not (name.startswith("project_") and name.endswith(".json")):
            continue
        path = os.path.join(curriculum_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("Curriculum preflight project state is unsafe")
        state = json.load(open(path, encoding="utf-8"))
        if state.get("status") in ("accepted", "in_progress"):
            active.append((state, path))
    if len(active) != 1:
        raise ValueError(
            "Curriculum preflight requires exactly one live project")
    pj, ppath = active[0]
    # Reuse the canonical key/filename validation used by ordinary episodes.
    canonical, canonical_path = live_project(root)
    if canonical != pj or canonical_path != ppath:
        raise ValueError("Curriculum preflight live project is inconsistent")
    curve_rows = [row for row in reward.read_curve(root)
                  if row.get("project") == pj.get("name")]
    later_sessions = []
    episodes_dir = os.path.join(root, "episodes")
    born_ep = int(pj.get("born_ep", -1))
    _assert_preflight_episode_slot_unused(root, ep_no, born_ep)
    if os.path.isdir(episodes_dir):
        for name in sorted(os.listdir(episodes_dir)):
            match = re.fullmatch(r"ep(\d+)", name)
            if not match or int(match.group(1)) <= born_ep:
                continue
            session = os.path.join(episodes_dir, name, "session")
            if os.path.isdir(session) and any(os.scandir(session)):
                later_sessions.append(name)
    ordinary_decisions = [
        row for row in pj.get("decisions", [])
        if row.get("preflight") is not True]
    legacy_preflight_decisions = [
        row for row in pj.get("decisions", [])
        if row.get("preflight") is True]
    unstarted = (
        pj.get("status") == "accepted" and
        int(pj.get("next_night", 0)) == 1 and
        not ordinary_decisions and
        pj.get("artifact_semantics") == _ARTIFACT_SEMANTICS and
        not curve_rows and not later_sessions)
    if not unstarted:
        raise ValueError(
            "Curriculum preflight requires a provably accepted, unstarted "
            "project at Night 1")
    project_dir = os.path.join(root, "projects", pj["key"])
    if not os.path.isdir(project_dir):
        raise ValueError("Curriculum preflight project archive is missing")

    for directory in ("curriculum", "journal", "verdicts", "preflights"):
        os.makedirs(os.path.join(root, directory), exist_ok=True)
    slot = _preflight_attempt_slot(
        root, ep_no, pj["name"], refine_repaired=refine_repaired)
    prior = slot.get("prior") or {}
    refining = slot.get("repair_refinement_from_attempt") is not None
    refinement_proof = None
    if refining:
        if pj.get("pending_repair") is None:
            raise ValueError(
                "Curriculum preflight refinement lost its pending repair")
        refinement_proof = _verify_preflight_refinement_source(
            root, ep_no, pj, slot,
            agent_decided_stop=bool(getattr(
                ccfg, "agent_decided_stop", False)),
            authorized_instruction=target_task)
    elif pj.get("pending_repair") is not None:
        raise ValueError(
            "Curriculum preflight requires an unserved Night-1 card")
    # A tagged decision is accepted only as compatibility evidence for a
    # recorded legacy attempt on this same project/episode. New preflights do
    # not write project decisions at all.
    if legacy_preflight_decisions:
        if slot["attempt"] == 1 or any(
                row.get("ep") != ep_no
                for row in legacy_preflight_decisions):
            raise ValueError("ambiguous legacy Curriculum preflight decision")
    eproot = slot["artifact_root"]
    os.mkdir(eproot)

    note_bytes = preflight_note.encode("utf-8")
    direction_bytes = curriculum_research_direction.encode("utf-8")
    target_bytes = target_task.encode("utf-8")
    config_bytes = json.dumps(
        vars(ccfg), sort_keys=True, default=str).encode("utf-8")
    meta = {
        "record_type": "curriculum_preflight",
        "graded": False,
        "episode": ep_no,
        "preflight_for_episode": ep_no,
        "preflight_attempt": slot["attempt"],
        "preflight_attempt_kind": slot["continuation_kind"],
        "preflight_artifact_root": slot["artifact_rel"],
        "project_before": pj["name"],
        "night_before": pj["next_night"],
        "t_start": time.strftime("%F %T"),
        "curriculum_only": True,
        "curriculum_mode": ("instruction_only_targeted"
                            if target_task else "blind"),
        "curriculum_model": str(getattr(ccfg, "model", "")),
        "curriculum_agent_decided_stop": bool(getattr(
            ccfg, "agent_decided_stop", False)),
        "curriculum_config_sha256": hashlib.sha256(
            config_bytes).hexdigest(),
        "actor_memory_pushed": False,
        "actor_ran": False,
        "verifier_ran": False,
        "evaluator_ran": False,
        "curve_written": False,
        "preflight_note_sha256": hashlib.sha256(note_bytes).hexdigest(),
        "preflight_note_bytes": len(note_bytes),
        "research_direction_sha256": hashlib.sha256(
            direction_bytes).hexdigest(),
        "unstarted_proof": {
            "status": "accepted", "next_night": 1,
            "decisions": 0,
            "legacy_preflight_decisions": len(legacy_preflight_decisions),
            "curve_rows": 0, "later_sessions": 0,
            "pending_repair": bool(pj.get("pending_repair")),
            "prior_preflight_status": prior.get("status"),
        },
        "project_state_before_sha256": hashlib.sha256(json.dumps(
            pj, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
        "pending_repair_before_sha256": (
            hashlib.sha256(pj["pending_repair"].encode("utf-8")).hexdigest()
            if pj.get("pending_repair") is not None else None),
        "preflight_instance_journal_path": os.path.join(
            "curriculum", f"{slot['journal_label']}_instance.md"),
        "preflight_notes_journal_path": os.path.join(
            "journal", f"{slot['journal_label']}_notes.md"),
    }
    if slot["prior"] is not None:
        meta["preflight_parent_attempt"] = slot["attempt"] - 1
        meta["preflight_parent_row_sha256"] = slot["prior_sha256"]
        if slot["continuation_kind"] == "repair_refinement":
            meta["refinement_of_status"] = slot["prior"].get("status")
            meta["refinement_parent_row_sha256"] = slot["prior_sha256"]
        else:
            meta["retry_of_status"] = slot["prior"].get("status")
            meta["retry_parent_row_sha256"] = slot["prior_sha256"]
    if refinement_proof:
        meta["repair_refinement_from_attempt"] = refinement_proof[
            "source_attempt"]
        meta["repair_refinement_source_candidate_sha256"] = \
            refinement_proof["source_candidate_sha256"]
        meta["repair_refinement_source_instance_path"] = \
            refinement_proof["instance_journal_path"]
    if target_task:
        meta["target_instruction_sha256"] = hashlib.sha256(
            target_bytes).hexdigest()
        meta["target_instruction_bytes"] = len(target_bytes)
    meta["provisioned"] = _provision_workspace(vm, ep_no, target_task)
    if not meta["provisioned"]:
        meta["status"] = "preflight_provision_failed"
        meta["t_end"] = time.strftime("%F %T")
        return meta
    replay = _replay_project_for_fresh_night(vm, project_dir)
    meta["fixture_replay_ok"] = bool(replay.get("ok"))
    meta["fixture_replay_mismatches"] = len(replay.get("mismatches", []))
    if not replay.get("ok"):
        meta["status"] = "preflight_replay_failed"
        meta["t_end"] = time.strftime("%F %T")
        return meta

    _curriculum_stage(
        vm, ep_no, root, os.path.join(root, "curriculum"),
        os.path.join(root, "journal"), eproot, preflight_note, meta,
        ccfg, pj, ppath, preflight_only=True,
        preflight_journal_label=slot["journal_label"],
        agent_decided_stop=bool(getattr(ccfg, "agent_decided_stop", False)),
        research_direction=curriculum_research_direction,
        target_task=target_task)
    after, _ = live_project(root)
    meta["project_after"] = after.get("name") if after else None
    meta["night_after"] = after.get("next_night") if after else None
    if after:
        meta["project_state_after_sha256"] = hashlib.sha256(json.dumps(
            after, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        meta["pending_repair_after_sha256"] = (
            hashlib.sha256(after["pending_repair"].encode(
                "utf-8")).hexdigest()
            if after.get("pending_repair") is not None else None)
    quarantined = any(meta.get(key) for key in (
        "new_project_candidate_quarantined",
        "new_project_fixture_quarantined",
        "preflight_repair_candidate_quarantined",
        "curriculum_transcript_quarantined",
        "curriculum_notebook_quarantined",
    ))
    decision = meta.get("decision")
    if quarantined:
        meta["status"] = "preflight_quarantined"
    elif decision in ("KEEP", "CONTINUE") and \
            meta.get("continue_transition") == "accepted":
        meta["status"] = "preflight_kept"
    elif decision == "REPAIR" and meta.get("repair_card") == "accepted":
        meta["status"] = "preflight_repaired"
    elif decision == "ABANDON" and after is not None and \
            after.get("name") != pj.get("name"):
        meta["status"] = "preflight_replaced"
    elif decision == "ABANDON":
        meta["status"] = "preflight_abandoned"
    else:
        meta["status"] = "preflight_incomplete"
    meta["t_end"] = time.strftime("%F %T")
    return meta


# ---------------------------------------------------------------- episode --
def e7_episode(vm, ep_no: int, cfg, vcfg, ccfg, root: str,
               reflection_cfg=None, require_complete_feedback: bool = False,
               curriculum_research_direction: str = "",
               curriculum_target_task: str = "",
               open_memory_research: bool = False) -> dict:
    target_task = curriculum_target_task
    has_target_task = bool(target_task.strip())
    from explore.targeting import (TARGET_AWARE_MODE, TargetingError,
                                   read_lineage_metadata)
    try:
        lineage_targeting = read_lineage_metadata(root)
    except TargetingError as exc:
        # Every episode requires prior registration. Missing and malformed
        # registries both fail closed: otherwise deleting a target-aware
        # registry could relabel its descendants as blind.
        raise ValueError(f"invalid episode target registry: {exc}") from exc
    if (lineage_targeting
            and lineage_targeting.get("mode") == TARGET_AWARE_MODE
            and not has_target_task):
        raise ValueError(
            "target-aware lineage cannot run an episode without its target")

    MEMORY = os.path.join(root, "memory")
    JOURNAL = os.path.join(root, "journal")
    CURR = os.path.join(root, "curriculum")
    VERD = os.path.join(root, "verdicts")
    eproot = os.path.join(root, "episodes", f"ep{ep_no:03d}")
    for d in (MEMORY, JOURNAL, CURR, VERD):
        os.makedirs(d, exist_ok=True)
    if has_target_task:
        from explore.targeting import validate_lineage_instruction
        try:
            validate_lineage_instruction(root, target_task)
        except TargetingError as exc:
            raise ValueError(f"unregistered episode target: {exc}") from exc
    corpus_record = None
    if has_target_task:
        try:
            corpus_record = mem.validate_instruction_corpus(CORPUS)
        except RuntimeError as exc:
            raise ValueError(
                "instruction-only targeted mode requires the validated "
                "host-side corpus; run tools/build_p2_corpus.py before "
                "launch") from exc
    meta = {"episode": ep_no, "t_start": time.strftime("%F %T"),
            "curriculum_mode": ("instruction_only_targeted" if has_target_task
                                else "blind"),
            "memory_research_mode": ("open" if open_memory_research
                                     else "settlement_only")}
    if has_target_task:
        encoded_target = target_task.encode("utf-8")
        meta.update(target_instruction_sha256=hashlib.sha256(
                        encoded_target).hexdigest(),
                    target_instruction_bytes=len(encoded_target),
                    instruction_corpus_sha256=(
                        corpus_record["corpus_sha256"]),
                    instruction_corpus_tasks=(
                        corpus_record["instruction_count"]))
    agent_decided_stop = bool(getattr(cfg, "agent_decided_stop", False))
    if open_memory_research and not require_complete_feedback:
        raise ValueError("open memory research requires complete verifier "
                         "feedback and agent-decided stopping")
    if require_complete_feedback:
        roles = (cfg, vcfg, ccfg, reflection_cfg)
        if reflection_cfg is None or not all(
                getattr(c, "agent_decided_stop", False) for c in roles):
            raise ValueError("complete-feedback mode requires agent-decided "
                             "stopping for every role")

    # 0 what is tonight? drill (if the curriculum authored one) else project
    drill_path = os.path.join(CURR, "instance_cur_drill.md")
    pj, ppath = live_project(root)
    is_drill = os.path.exists(drill_path)
    if not is_drill and pj is None:
        # v1.1 (EXP-2026-007, ep10-12 spin): between projects the episode
        # is an AUTHORING night — the curriculum designs the next project
        # on THIS boot, so fixtures are built and captured on the same
        # machine (B7) and acceptance can actually fire. No actor night.
        meta["provisioned"] = _provision_workspace(
            vm, ep_no, target_task)
        if not meta["provisioned"]:
            meta["status"] = "provision_failed"
            return meta
        _curriculum_stage(vm, ep_no, root, CURR, JOURNAL, eproot,
                          "(no live project — author the next project; "
                          "build and validate its fixtures on this machine "
                          "before shipping the card)",
                          meta, ccfg, None, None,
                          agent_decided_stop=agent_decided_stop,
                          research_direction=curriculum_research_direction,
                          target_task=target_task)
        np = str(meta.get("new_project") or "")
        quarantine_flags = (
            "new_project_candidate_quarantined",
            "new_project_fixture_quarantined",
            "curriculum_transcript_quarantined",
            "curriculum_notebook_quarantined",
        )
        if any(meta.get(flag) for flag in quarantine_flags):
            meta["status"] = "target_card_quarantined"
            meta["memory_promoted"] = False
            meta["void_reason"] = "Curriculum authoring content boundary"
        else:
            meta["status"] = ("project_authored"
                              if np and not np.startswith("rejected")
                              else "authoring_failed")
        meta["t_end"] = time.strftime("%F %T")
        return meta

    # 1 provision (generic presence + clips; projects replay on top)
    meta["provisioned"] = _provision_workspace(vm, ep_no, target_task)
    if not meta["provisioned"]:
        meta["status"] = "provision_failed"
        return meta
    if not is_drill:
        rp = _replay_project_for_fresh_night(
            vm, os.path.join(root, "projects", pj["key"]))
        if not rp.get("ok"):
            meta["status"] = "provision_failed"
            meta["replay"] = str(rp)[:200]
            return meta

    # 2 memory in (grade-time env for the gate)
    before = {}
    for r_, _, fns in os.walk(MEMORY):
        for fn in fns:
            p = os.path.join(r_, fn)
            before[os.path.relpath(p, MEMORY)] = open(p, "rb").read()
    if not mem.push_memory(vm, MEMORY):
        meta["status"] = "sync_failed"
        return meta

    # 3 tonight's card + full intake on this boot
    if is_drill:
        card_text = open(drill_path, encoding="utf-8").read()
        v = validate_card(card_text)               # E8 drill intake, unchanged
        night_kind, night_no, n_nights = "drill", 0, 0
    else:
        rep = pj.get("pending_repair")
        night_no = pj["next_night"]
        n_nights = pj["n_nights"]
        is_final = (rep is None) and (night_no == n_nights)
        if rep is not None:
            stored_repair = _parse_stored_pending_repair(
                rep, pj, agent_decided_stop, target_task)
            if stored_repair["ok"]:
                card_text = stored_repair["section"]
                v = {"ok": True, "card": stored_repair["card"],
                     "reasons": [], "fence_hit": False}
                if stored_repair.get("legacy_header"):
                    # Identity-preserving migration only: the validated
                    # envelope is discarded and its exact body is persisted.
                    candidate = copy.deepcopy(pj)
                    candidate["pending_repair"] = card_text
                    save_project(ppath, candidate)
                    pj.clear()
                    pj.update(candidate)
                    meta["pending_repair_normalized"] = True
            else:
                # Preserve the rejected bytes for Curriculum diagnosis, but
                # never parse or dry-run them as an Actor card.
                card_text = str(rep)
                v = {"ok": False,
                     "card": reward.parse_night_section(
                         "", agent_decided_stop=agent_decided_stop),
                     "reasons": stored_repair["reasons"],
                     "fence_hit": stored_repair.get("fence_hit", False)}
        else:
            card_text = pj["night_cards"][night_no - 1]
            v = _validate_night_card(
                card_text, agent_decided_stop=agent_decided_stop,
                authorized_instruction=target_task)
        night_kind = ("repair" if rep is not None else
                      ("final" if is_final else "milestone"))
        transport_v = _validate_project_transport(
            _project_transport_text(pj), pj["name"], pj["n_nights"],
            agent_decided_stop, target_task)
        require_full_project = any(
            issue["scope"] != "night" or issue["night"] != night_no
            for issue in transport_v["issues"])
        if not transport_v["ok"]:
            meta["project_transport"] = {
                "ok": False, "requires_full_project": require_full_project,
                "reasons": transport_v["reasons"][:8]}
            for reason in transport_v["reasons"]:
                labelled = f"stored project transport: {reason}"
                if labelled not in v["reasons"]:
                    v["reasons"].append(labelled)
            v["ok"] = False
            if any("content outside the authorized boundary" in reason
                   for reason in transport_v["reasons"]):
                v["fence_hit"] = True
    meta.update(kind=night_kind, project=(None if is_drill else pj["name"]),
                night=night_no, n_nights=n_nights)

    ok = v["ok"]
    gate = {"accept": False, "reasons": v["reasons"], "mutated": False}
    if ok:
        crits = v["card"]["criteria"]
        if (not is_drill) and night_kind == "final":
            # N7: FINAL criteria re-gated on night N alongside the milestone
            fin = [l for l in pj["final_text"].splitlines()
                   if _NUM.match(l)]
            crits = crits + fin
            meta["final_criteria_n"] = len(fin)
        gate = run_dry_gate(vm, crits)
    if (not is_drill) and night_kind == "final" and not gate["accept"]:
        # A combined final-night gate cannot safely attribute a failure to the
        # NIGHT versus FINAL slice. Require a complete replacement so
        # Curriculum may correct either without harness guesswork.
        require_full_project = True
    meta["gate"] = {"accept": gate["accept"], "reasons": gate["reasons"][:4]}

    reauthor_ran = False
    if not gate["accept"] and not gate.get("mutated"):
        if has_target_task and v.get("fence_hit"):
            meta["status"] = "target_card_quarantined"
            meta["memory_promoted"] = False
            meta["void_reason"] = "content outside authorized boundary"
            return meta
        # ONE re-author pass (F6 lineage), fed the verbatim reasons. A defect
        # outside the active night requires a complete canonical replacement;
        # a partial PROJECT-shaped wrapper is never interpreted as a night.
        reason = "; ".join(v["reasons"] + gate["reasons"])
        if not is_drill:
            reauthor_contract = _reauthor_contract(
                pj, night_no, card_text, require_full_project)
        else:
            reauthor_contract = f"CARD:\n{card_text}"
        _cur = _curriculum_stage(vm, ep_no, root, CURR, JOURNAL, eproot,
                                 f"(intake rejection — fix exactly this and "
                                 f"re-author)\n{reauthor_contract}\n"
                                 f"REJECTED: {reason}", meta, ccfg, pj, ppath,
                                 reauthor_only=True,
                                 agent_decided_stop=agent_decided_stop,
                                 research_direction=curriculum_research_direction,
                                 target_task=target_task)
        reauthor_ran = True
        if has_target_task and meta.get("curriculum_transcript_quarantined"):
            meta["status"] = "target_card_quarantined"
            meta["memory_promoted"] = False
            meta["void_reason"] = "curriculum transcript content boundary"
            meta["t_end"] = time.strftime("%F %T")
            return meta
        card_text = _cur.get("card_text", "")
        reauthored_project = None
        if is_drill:
            v = validate_card(card_text)
        else:
            reauthored = _parse_reauthor_output(
                card_text, pj, night_no, require_full_project,
                agent_decided_stop, target_task)
            if reauthored["ok"]:
                reauthored_project = reauthored["project"]
                card_text = reauthored["card_text"]
                meta["reauthor_transport"] = reauthored["kind"]
                v = _validate_night_card(
                    card_text, agent_decided_stop=agent_decided_stop,
                    authorized_instruction=target_task)
            else:
                meta["reauthor_transport"] = "rejected"
                v = {"ok": False,
                     "card": reward.parse_night_section(
                         "", agent_decided_stop=agent_decided_stop),
                     "reasons": reauthored["reasons"], "fence_hit": False}
        if v["ok"]:
            crits = list(v["card"]["criteria"])
            if (not is_drill) and night_kind == "final":
                final_source = reauthored_project or pj
                crits.extend(line for line in
                             final_source["final_text"].splitlines()
                             if _NUM.match(line))
            gate = run_dry_gate(vm, crits)
        else:
            gate = {"accept": False, "reasons": v["reasons"],
                    "mutated": False}
        meta["gate2"] = {"accept": gate["accept"],
                        "reasons": gate["reasons"][:4]}
        if gate["accept"] and not is_drill and reauthored_project is not None:
            # Republish the immutable fixture baseline with the corrected,
            # monotone ownership boundary. Curriculum edits on this guest are
            # intentionally discarded before the replacement transport saves.
            rc = _persist_reauthored_project(
                vm, root, ppath, pj, reauthored_project, card_text,
                authorized_instruction=target_task)
            if not rc.get("ok"):
                gate["accept"] = False
                if not gate["reasons"]:
                    gate["reasons"].append(
                        "project baseline failed content/transport boundary")
                meta["gate2"]["accept"] = False
                meta["gate2"]["reasons"] = gate["reasons"][:4]
            meta["recaptured"] = rc.get("ok")
            meta["reauthor_fixture_source"] = rc.get("fixture_source", "")
            if rc.get("persisted"):
                meta["project_transport_persisted"] = True
    if gate["accept"] and reauthor_ran:
        clean = _reset_after_reauthor_for_actor(
            vm, ep_no, root, None if is_drill else pj, MEMORY, target_task)
        meta["actor_vm_reset_after_reauthor"] = clean.get("ok", False)
        if not clean.get("ok"):
            meta["status"] = "actor_phase_reset_failed"
            meta["memory_promoted"] = False
            meta["actor_phase_reset_error"] = clean.get("error", "")
            meta["t_end"] = time.strftime("%F %T")
            return meta
        # Rehearse once more on the exact clean state the Actor will inherit,
        # not only on the Curriculum Agent's prior VM.
        gate = run_dry_gate(vm, crits)
        meta["gate_after_actor_reset"] = {
            "accept": gate["accept"], "reasons": gate["reasons"][:4]}
    if not gate["accept"]:
        meta["status"] = "drill_void"
        meta["void_reason"] = ("mutation" if gate.get("mutated")
                               else "; ".join(gate["reasons"])[:300])
        if is_drill and os.path.exists(drill_path):
            os.rename(drill_path, drill_path + ".VOID")
        reward.append_curve(root, {
            "episode": ep_no, "project": meta.get("project") or "-",
            "night": night_no, "n_nights": n_nights, "kind": night_kind,
            "outcome": "drill_void", "iters": 0})
        if reauthor_ran:
            # The Curriculum Agent already received the precise rejection and
            # one unrestricted correction session.  A second ordinary ruling
            # in the same void episode used to bypass the strict parser and
            # poison pending state.  Leave state untouched and retry the same
            # audited boundary on the next fresh episode instead.
            meta["post_void_curriculum"] = "skipped_after_failed_reauthor"
        else:
            _curriculum_stage(vm, ep_no, root, CURR, JOURNAL, eproot,
                              "(void night — the actor did not run)", meta,
                              ccfg, pj, ppath,
                              agent_decided_stop=agent_decided_stop,
                              research_direction=curriculum_research_direction,
                              target_task=target_task)
        meta["t_end"] = time.strftime("%F %T")
        return meta

    card = v["card"]
    if is_drill:
        os.remove(drill_path)

    # 4 actor — per-night reveal (B3) at the card budget (B4), stall-hooked
    fb_path = os.path.join(VERD, f"ep{ep_no - 1:03d}.txt")
    feedback = open(fb_path, encoding="utf-8").read() \
        if os.path.exists(fb_path) else ""
    night_cfg = copy.deepcopy(cfg)
    if not agent_decided_stop:
        night_cfg.max_iters = card["budget"]
    disclosure = "" if agent_decided_stop else (
        E8_DISCLOSURE if is_drill else E7_MILESTONE_DISCLOSURE
    ).format(n=card["budget"])
    shown_card_text = (re.sub(r"^BUDGET:.*$", "", card_text,
                              flags=re.M).strip()
                       if agent_decided_stop else card_text)
    if is_drill:
        instance = shown_card_text + disclosure
    else:
        instance = (f"PROJECT GOAL:\n{pj['target_prose']}\n\n"
                    "SESSION STATE:\nThis is a fresh reconstruction session. "
                    "The project's original accepted fixtures were replayed; "
                    "Actor-created outputs from prior nights were not carried "
                    "forward. Recreate any needed intermediate state from "
                    "the goal, tonight's task, live evidence, and your own "
                    "durable memory.\n\n"
                    f"TONIGHT (night {night_no} of {n_nights}"
                    f"{'-REPAIR' if night_kind == 'repair' else ''}):\n"
                    f"{shown_card_text}{disclosure}")
    stall.night_stamp(vm)
    tracker = stall.StallTracker(
        threshold=getattr(cfg, "practice_stall_iters", 25))

    def _hook(_iters):
        tracker.update(stall.progress_probe(vm))
        return tracker.stalled

    _assert_target_null_task(vm, target_task)
    res, actor_history = run_attempt(
        e6_actor_charter(instance, feedback, listing_text(MEMORY)),
        vm, night_cfg, ArtifactSink(os.path.join(eproot, "session")),
        iter_hook=_hook)
    meta.update(status=res.status, iters=res.iters,
                session_secs=round(res.wall_secs))
    if res.status == "infra":
        return meta

    # 5 harness grade (v11c) — milestone; FINAL separately on final nights
    mil_crits = [_NUM.sub("", strip_guard(c)).strip()
                 for c in card["criteria"]]
    grade = reward.grade_instance(vm, mil_crits)
    meta["outcome"] = grade["outcome"]
    final_grade = None
    if (not is_drill) and night_kind == "final":
        fin = [_NUM.sub("", strip_guard(l)).strip()
               for l in pj["final_text"].splitlines() if _NUM.match(l)]
        final_grade = reward.grade_instance(vm, fin)
        meta["final_outcome"] = final_grade["outcome"]

    # In open-research mode, this host-side archive is the canonical graded
    # artifact.  The Verifier and the post-verdict Actor may inspect or mutate
    # the live tree, but neither can retroactively change what earned the grade.
    graded_snapshot = None
    if open_memory_research:
        graded_snapshot = _capture_graded_artifact(
            vm, root, eproot, pj, card_text,
            authorized_instruction=target_task)
        meta["graded_artifact"] = {
            "captured": graded_snapshot.get("ok", False),
            "roots": graded_snapshot.get("roots", []),
            "files": graded_snapshot.get("files", 0),
            "bytes": graded_snapshot.get("bytes", 0)}
        if not graded_snapshot.get("ok"):
            meta["status"] = "graded_snapshot_failed"
            meta["memory_promoted"] = False
            return meta

    # 6 provisional actor memory + verifier. The verifier never edits durable
    # memory: its machine changes are discarded by restoring this snapshot.
    after_actor = mem.pull_memory(vm)
    if not after_actor and before:
        meta["status"] = "pull_failed"
        return meta
    gblock = graded_block(grade)
    if final_grade:
        gblock += "\n--- PROJECT FINAL ---\n" + graded_block(final_grade)
    if has_target_task:
        from tools.exam_fence import audit_text as _audit_target_grade
        grade_hits = _audit_target_grade(
            gblock, mode="practice", authorized_instruction=target_task)
        if grade_hits:
            meta["status"] = "target_grade_quarantined"
            meta["memory_promoted"] = False
            meta["target_grade_fence_kinds"] = sorted(
                {hit.get("kind", "unknown") for hit in grade_hits})
            return meta
    _assert_target_null_task(vm, target_task)
    verify_sink = ArtifactSink(os.path.join(eproot, "verify"))
    report_path = "/home/user/verifier_report.md"
    review_cfg = vcfg
    verifier_prompt = e6_verifier_charter(instance, gblock)
    if require_complete_feedback:
        vm.run_command(f"rm -f {report_path}", timeout=30)
        review_cfg = copy.deepcopy(vcfg)
        review_cfg.practice_mode = True
        review_cfg.independent_verify = False
        review_cfg.practice_done_requires = report_path
        review_cfg.max_resumes = 0
        verifier_prompt = agentic_verifier_charter(
            instance, gblock, report_path)
    rres, rhist = run_attempt(
        verifier_prompt, vm, review_cfg, verify_sink)
    if agent_decided_stop:
        verify_sink.save_transcript(build_system(review_cfg), rhist)
    report_result = None
    if require_complete_feedback:
        report_result = _collect_verifier_report(
            vm, report_path, rres, eproot)
        review = report_result["report"]
        meta.update(verifier_report_status=report_result["status"],
                    verifier_report_complete=report_result["ok"],
                    verifier_report_errors=report_result["errors"],
                    verifier_report_iters=report_result["iters"],
                    verifier_report_turns=report_result["turns"])
    else:
        review = reward.last_review(rhist, cap=4000)
    meta["verify_status"] = rres.status

    if graded_snapshot is not None:
        clean = _reset_to_canonical_artifact(
            vm, graded_snapshot["host_dir"],
            authorized_instruction=target_task)
        meta["research_vm_reset"] = clean.get("ok", False)
        meta["graded_restore_after_verifier"] = clean.get("ok", False)
        if not clean.get("ok"):
            meta["status"] = "research_reset_after_verifier_failed"
            meta["memory_promoted"] = False
            meta["research_reset_error"] = clean.get("error", "")
            return meta

    if has_target_task:
        from tools.exam_fence import audit_text as _audit_target_text
        feedback_hits = _audit_target_text(
            review, mode="practice", authorized_instruction=target_task)
        if feedback_hits:
            meta["status"] = "target_feedback_quarantined"
            meta["memory_promoted"] = False
            meta["target_feedback_fence_hits"] = [
                {k: v for k, v in hit.items() if k != "match"}
                for hit in feedback_hits]
            return meta

    tmp = os.path.join(eproot, "_mem_mid")
    mem.write_memory(tmp, after_actor)
    restore_ok = mem.push_memory(vm, tmp)

    verdict = gblock + "\nreviewer: " + review

    if require_complete_feedback and not report_result["ok"]:
        meta["status"] = "feedback_incomplete"
        meta["memory_promoted"] = False
        open(os.path.join(VERD, f"ep{ep_no:03d}.txt"), "w").write(
            gblock + "\nreviewer: (complete report unavailable; provisional "
            "memory was not promoted)")
        meta["t_end"] = time.strftime("%F %T")
        return meta

    # EXP post-verdict reflection (opt-in; None preserves frozen E7 behavior):
    # continue the SAME Actor Agent conversation, append the outcome evidence,
    # then let the Actor freely settle memory before anything is committed.
    after = after_actor
    if reflection_cfg is not None:
        if not restore_ok:
            meta["status"] = "reflection_sync_failed"
            return meta
        _assert_target_null_task(vm, target_task)
        reflection_sink = ArtifactSink(
            os.path.join(eproot, "memory_reflection"))
        if open_memory_research:
            reflection_instruction = post_verdict_memory_msg(
                verdict, open_memory_research=open_memory_research,
                disposable_roots=tuple(
                    f"/home/user/{d}"
                    for d in ((graded_snapshot or {}).get("roots", []))))
        else:
            reflection_instruction = post_verdict_memory_msg(verdict)
        mres, reflection_history = run_attempt(
            reflection_instruction, vm, reflection_cfg,
            reflection_sink,
            initial_history=actor_history, continue_context=True,
            allow_noop_done=True)
        if agent_decided_stop:
            reflection_sink.save_transcript(
                build_system(reflection_cfg), reflection_history)
        meta.update(reflection_status=mres.status,
                    reflection_iters=mres.iters,
                    reflection_turns=mres.turns,
                    reflection_secs=round(mres.wall_secs))
        if graded_snapshot is not None:
            restored = (_restore_graded_artifact(
                vm, graded_snapshot["host_dir"],
                authorized_instruction=target_task)
                if target_task else
                _restore_graded_artifact(vm, graded_snapshot["host_dir"]))
            meta["graded_restore_after_memory"] = restored.get("ok", False)
            if not restored.get("ok"):
                meta["status"] = "graded_restore_after_memory_failed"
                meta["memory_promoted"] = False
                return meta
        after = mem.pull_memory(vm)
        if not after and after_actor:
            meta["status"] = "reflection_pull_failed"
            return meta
        if require_complete_feedback and mres.status != "done":
            partial = os.path.join(eproot, "_mem_reflection_incomplete")
            mem.write_memory(partial, after)
            meta["status"] = "memory_not_ready"
            meta["memory_promoted"] = False
            open(os.path.join(VERD, f"ep{ep_no:03d}.txt"), "w").write(verdict)
            meta["t_end"] = time.strftime("%F %T")
            return meta

    if has_target_task:
        from tools.exam_fence import audit_transcripts
        transcript_hits = audit_transcripts(
            eproot, mode="practice",
            authorized_instruction=target_task)
        if transcript_hits:
            meta["status"] = "target_transcript_quarantined"
            meta["memory_promoted"] = False
            meta["target_transcript_fence_hits"] = [
                {"file": os.path.relpath(hit["file"], eproot),
                 "kinds": sorted({h.get("kind", "unknown")
                                  for h in hit.get("hits", [])})}
                for hit in transcript_hits[:20]]
            return meta

    # Open research crosses a fresh-machine boundary before either durable
    # promotion or Curriculum.  Project-tree replay alone cannot remove hidden
    # app state, /tmp residue, services, or detached phase processes.
    if graded_snapshot is not None:
        clean = _reset_to_canonical_artifact(
            vm, graded_snapshot["host_dir"],
            authorized_instruction=target_task)
        meta["curriculum_vm_reset"] = clean.get("ok", False)
        if not clean.get("ok"):
            partial = os.path.join(eproot, "_mem_reset_unpromoted")
            mem.write_memory(partial, after)
            meta["status"] = "curriculum_reset_failed"
            meta["memory_promoted"] = False
            meta["curriculum_reset_error"] = clean.get("error", "")
            return meta

    # 7 scan + observe ledger + commit + telemetry
    inv = reward.scan_invocations(eproot, known_files=set(after))
    meta.update(invoked_n=inv["distinct"],
                exec_n=len(inv.get("exec_counts", {})),
                read_n=len(inv.get("read_counts", {})))
    lpath = os.path.join(root, "observe_ledger.json")
    ledger = json.load(open(lpath)) if os.path.exists(lpath) else {}
    kept, pruned, ledger2 = reward.observe_ledger(
        after, set(inv["counts"]), ledger, ep_no - 1)
    json.dump(ledger2, open(lpath, "w"))
    accepted = mem.silent_audit(
        kept, CORPUS, os.path.join(root, "audit_rejects.jsonl"),
        authorized_instruction=target_task,
        require_corpus=has_target_task)
    mem.journal(JOURNAL, ep_no, accepted, {"kind": night_kind,
                                           "project": meta.get("project")})
    prev_bytes = sum(len(d) for d in before.values())
    mem.write_memory(MEMORY, accepted)
    if require_complete_feedback:
        meta["memory_promoted"] = True
    now_bytes = sum(len(d) for d in accepted.values())
    meta["artifact"] = {"bytes": now_bytes, "files": len(accepted),
                        "delta": now_bytes - prev_bytes}

    # 8 project bookkeeping BEFORE the ruling: final PASS completes it
    if (not is_drill) and final_grade and final_grade["outcome"] == "pass":
        pj["status"] = "complete"
        save_project(ppath, pj)

    # 9 verdict + curve row v2
    open(os.path.join(VERD, f"ep{ep_no:03d}.txt"), "w").write(verdict)
    reward.append_curve(root, {
        "episode": ep_no, "project": meta.get("project") or "-",
        "night": night_no, "n_nights": n_nights, "kind": night_kind,
        "outcome": (meta.get("final_outcome") if night_kind == "final"
                    else grade["outcome"]),
        "milestone_outcome": grade["outcome"],
        "iters": res.iters,
        "budget": ("agent-decided" if agent_decided_stop else card["budget"]),
        "invoked_n": inv["distinct"], "exec_n": meta["exec_n"],
        "mem_delta": meta["artifact"]["delta"],
        "stalled": res.status == "stalled_quiescent"})

    # 10 curriculum stage: ruling + next authorship
    _curriculum_stage(vm, ep_no, root, CURR, JOURNAL, eproot, verdict,
                      meta, ccfg, pj, ppath,
                      agent_decided_stop=agent_decided_stop,
                      research_direction=curriculum_research_direction,
                      target_task=target_task)
    meta["t_end"] = time.strftime("%F %T")
    return meta


# ------------------------------------------------------- curriculum stage --
def _effective_project_transport_text(pj: dict) -> str:
    """Serialize the exact card an Actor would receive from pending state."""
    effective = copy.deepcopy(pj)
    if effective.get("pending_repair") is not None:
        index = int(effective["next_night"]) - 1
        effective["night_cards"][index] = effective["pending_repair"]
    return _project_transport_text(effective)


def _project_block(pj, include_transport: bool = False) -> str:
    if not pj:
        return ""
    block = (f"PROJECT: {pj['name']}  night {pj['next_night']}/"
             f"{pj['n_nights']}  status {pj['status']}\n"
             f"LIVE POINTER: {pj['next_night']}/{pj['n_nights']}\n"
             "ARTIFACT SEMANTICS: fresh reconstruction from the original "
             "accepted fixture capture; no Actor-created output from a prior "
             "night is carried forward.\n"
             f"EXACT REPAIR HEADER: NIGHT: {pj['next_night']}/"
             f"{pj['n_nights']}-R\nTARGET:\n"
             f"{pj['target_prose']}")
    if not include_transport:
        return block
    night_index = int(pj["next_night"]) - 1
    served = (pj.get("pending_repair") or
              pj.get("night_cards", [])[night_index])
    return (block + "\n\nCURRENT EFFECTIVE PROJECT TRANSPORT:\n---\n" +
            _effective_project_transport_text(pj).rstrip() +
            "\n---\n\nCURRENT NIGHT-1 CARD THAT AN ACTOR WOULD RECEIVE:\n"
            "---\n" + str(served).strip() + "\n---")


def _load_notes(CURR: str) -> str:
    p = os.path.join(CURR, "curriculum_notes.md")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _persist_curriculum_notes(CURR: str, JOURNAL: str, ep_no: int,
                              notes: str, meta: dict,
                              authorized_instruction: str = "",
                              journal_label: str = "") -> None:
    """Fence and persist the latest notebook from a Curriculum continuation."""
    if not notes:
        return
    from tools.exam_fence import audit_text
    if audit_text(notes, mode="practice",
                  authorized_instruction=authorized_instruction):
        open(os.path.join(CURR, "curriculum_notes.md.REJECTED"),
             "w").write(notes)
        meta["notebook"] = "fence-quarantined"
    else:
        open(os.path.join(CURR, "curriculum_notes.md"), "w").write(notes)
        notes_journal = (f"{journal_label}_notes.md" if journal_label else
                         f"journal_ep{ep_no:03d}_notes.md")
        open(os.path.join(JOURNAL, notes_journal),
             "w").write(notes)
        meta["notebook"] = "ok"


def _new_project_body(text: str) -> str:
    """Remove the ruling envelope while preserving exact candidate bytes."""
    return _DECISION.sub("", text or "", count=1).strip()


def _new_project_decision(text: str) -> str | None:
    match = _DECISION.search(text or "")
    return match.group(1) if match else None


def accept_new_project_with_feedback(
        vm, ep_no: int, root: str, initial_text: str, cfg, sink_root: str,
        initial_history=None, initial_result=None,
        initial_iters_used=None, initial_wall_used=None,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "",
        expected_decision: str | None = None,
        journal_label: str = "") -> dict:
    """Validate and, when needed, let Curriculum correct a project draft.

    Corrections continue in the original Curriculum conversation.  There is
    no attempt-count limit: the only normal bound is the remaining aggregate
    Curriculum iteration/wall-clock safety budget.  Static rejection is
    side-effect free; capture/publication is attempted exactly once, for the
    first candidate that passes all static host validation.
    """
    from tools.exam_fence import audit_text, audit_transcripts

    history = list(initial_history or [])
    latest_result = initial_result
    total_iters = int(
        getattr(initial_result, "iters", 0) or 0
        if initial_iters_used is None else initial_iters_used)
    total_wall = float(
        getattr(initial_result, "wall_secs", 0.0) or 0.0
        if initial_wall_used is None else initial_wall_used)
    max_iters = int(getattr(cfg, "max_iters", 0) or 0)
    max_wall = float(getattr(cfg, "wall_clock_secs", 0.0) or 0.0)
    raw = initial_text or ""
    candidate = _new_project_body(raw)
    expected_decision = (expected_decision or
                         _new_project_decision(raw) or "CONTINUE")
    receipts = []
    receipt_path = os.path.join(
        sink_root, "new_project_candidate_receipts.json")
    seen = {}
    continuation = 0

    def persist_receipts():
        os.makedirs(sink_root, exist_ok=True)
        staged = receipt_path + ".tmp"
        with open(staged, "w", encoding="utf-8") as f:
            json.dump(receipts, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, receipt_path)
        directory_fd = os.open(sink_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def finish(acceptance, terminal, transcript_hits=None,
               notebook_hits=None):
        return {
            "ok": bool(acceptance and acceptance.get("ok")),
            "acceptance": acceptance or {"ok": False, "reasons": []},
            "candidate_text": candidate, "raw_text": raw,
            "history": history, "result": latest_result,
            "receipts": receipts, "iters": total_iters,
            "wall_secs": total_wall, "continuations": continuation,
            "terminal": terminal,
            "transcript_hits": list(transcript_hits or []),
            "notebook_hits": list(notebook_hits or []),
            "candidate_quarantined": terminal == "candidate-fence-quarantined",
            "fixture_quarantined": terminal == (
                "captured-fixture-fence-quarantined"),
            "receipt_path": receipt_path,
        }

    # These receipts are audit-durable, not a conversation-resume WAL.  A
    # caller recalling the helper after a crash must not overwrite or mingle
    # the prior Agent boundary; recovery must resolve that recorded attempt.
    if os.path.lexists(receipt_path):
        try:
            if os.path.islink(receipt_path) or not os.path.isfile(receipt_path):
                raise ValueError("unsafe receipt path")
            with open(receipt_path, encoding="utf-8") as f:
                prior = json.load(f)
            if not isinstance(prior, list):
                raise ValueError("receipt ledger is not a list")
            receipts.extend(prior)
        except (OSError, ValueError, json.JSONDecodeError):
            return finish({"ok": False, "reasons": [
                "existing new-project feedback receipt is unreadable"]},
                "existing-feedback-receipt-unreadable")
        return finish({"ok": False, "reasons": [
            "existing new-project feedback receipt requires recovery"]},
            "existing-feedback-receipt-requires-recovery")

    while True:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        actual_decision = _new_project_decision(raw)
        # A contaminated notebook is never carried into another Curriculum
        # turn and is never allowed to coexist with fixture publication.
        latest_notes = _guest_text_lossless(vm, "~/curriculum_notes.md")
        notebook_hits = (audit_text(
            latest_notes, mode="practice",
            authorized_instruction=authorized_instruction)
            if latest_notes else [])
        if notebook_hits:
            receipt = {
                "attempt": len(receipts) + 1,
                "candidate_sha256": digest,
                "reasons": ["curriculum notebook content boundary"],
                "phase": "curriculum-notebook-fence",
                "status": "curriculum-notebook-quarantined",
                "decision": actual_decision,
                "expected_decision": expected_decision,
                "fence_hit": True,
            }
            receipts.append(receipt)
            persist_receipts()
            vm.run_command(
                "rm -f /home/user/instance_next.md "
                "/home/user/instance_rejected.md", timeout=30)
            return finish({"ok": False, "reasons": receipt["reasons"]},
                          receipt["status"],
                          notebook_hits=notebook_hits)

        checked = validate_new_project_candidate(
            root, candidate, agent_decided_stop, authorized_instruction)
        envelope_reasons = ([] if actual_decision == expected_decision else [
            f"corrected ruling must preserve DECISION: {expected_decision}"])
        reasons = envelope_reasons + list(checked.get("reasons", []))
        candidate_ok = checked["ok"] and not envelope_reasons
        receipt = {
            "attempt": len(receipts) + 1,
            "candidate_sha256": digest,
            "reasons": reasons,
            "phase": "static-validation",
            "status": "valid" if candidate_ok else "rejected",
            "decision": actual_decision,
            "expected_decision": expected_decision,
            "fence_hit": bool(checked.get("fence_hit", False)),
        }
        receipts.append(receipt)
        persist_receipts()
        if checked.get("quarantine"):
            receipt["status"] = "candidate-fence-quarantined"
            persist_receipts()
            vm.run_command(
                "rm -f /home/user/instance_next.md "
                "/home/user/instance_rejected.md", timeout=30)
            return finish({"ok": False, "reasons": reasons},
                          receipt["status"])
        if candidate_ok:
            receipt["status"] = "validated-awaiting-publish"
            persist_receipts()
            vm.run_command(
                "rm -f /home/user/instance_rejected.md", timeout=30)
            accepted = _publish_validated_new_project(
                vm, ep_no, root, candidate, checked,
                authorized_instruction, journal_label)
            receipt["phase"] = "publish"
            if accepted.get("quarantine"):
                receipt["status"] = "captured-fixture-fence-quarantined"
            else:
                receipt["status"] = ("accepted" if accepted.get("ok")
                                     else "publish-rejected")
            receipt["reasons"] = list(accepted.get("reasons", []))
            receipt["fence_hit"] = bool(accepted.get("fence_hit", False))
            persist_receipts()
            if accepted.get("quarantine"):
                vm.run_command(
                    "rm -f /home/user/instance_next.md "
                    "/home/user/instance_rejected.md", timeout=30)
                return finish(
                    accepted, "captured-fixture-fence-quarantined")
            return finish(accepted, receipt["status"])

        signature = (digest, tuple(reasons), actual_decision)
        seen[signature] = seen.get(signature, 0) + 1
        receipt["recurrence"] = seen[signature]
        persist_receipts()

        iters_left = max_iters - total_iters
        wall_left = max_wall - total_wall
        if iters_left <= 0 or wall_left <= 0:
            return finish({"ok": False, "reasons": reasons},
                          "curriculum-safety-budget-exhausted")

        continuation += 1
        sink_dir = os.path.join(
            sink_root, f"project_reauthor_{continuation:03d}")
        candidate_dir = os.path.join(sink_root, "new_project_candidates")
        os.makedirs(candidate_dir, exist_ok=True)
        candidate_path = os.path.join(
            candidate_dir, f"candidate_{len(receipts):03d}.md")
        with open(candidate_path, "w", encoding="utf-8") as f:
            f.write(candidate)
            f.flush()
            os.fsync(f.fileno())
        candidate_dir_fd = os.open(candidate_dir, os.O_RDONLY)
        try:
            os.fsync(candidate_dir_fd)
        finally:
            os.close(candidate_dir_fd)
        history_bytes = json.dumps(
            history, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        receipt["candidate_artifact_path"] = os.path.relpath(
            candidate_path, sink_root)
        receipt["continuation_sink"] = os.path.relpath(
            sink_dir, sink_root)
        receipt["parent_history_sha256"] = hashlib.sha256(
            history_bytes).hexdigest()
        receipt["status"] = "feedback-pending"
        persist_receipts()

        # Keep the exact rejected candidate available to the same agent, but
        # remove the sole accepted handoff path before asking for a replacement.
        import base64
        encoded = base64.b64encode(candidate.encode("utf-8")).decode("ascii")
        vm.run_command(
            f"echo {encoded} | base64 -d > /home/user/instance_rejected.md; "
            "rm -f /home/user/instance_next.md", timeout=60)
        t_mark = vm.run_command("date +%s", timeout=30) or "0"
        latest_result, history = run_attempt(
            new_project_rejection_msg(
                digest, reasons, expected_decision), vm, cfg,
            ArtifactSink(sink_dir), iters_budget=iters_left,
            wall_budget=wall_left, initial_history=history,
            continue_context=True)
        total_iters += int(getattr(latest_result, "iters", 0) or 0)
        total_wall += float(getattr(latest_result, "wall_secs", 0.0) or 0.0)
        receipt["continuation_status"] = getattr(
            latest_result, "status", "infra")
        receipt["status"] = (
            "feedback-completed" if receipt["continuation_status"] == "done"
            else "feedback-incomplete")
        persist_receipts()

        hits = audit_transcripts(
            sink_dir, mode="practice",
            authorized_instruction=authorized_instruction)
        if hits:
            receipt["status"] = "curriculum-transcript-quarantined"
            receipt["terminal_reason"] = (
                "curriculum transcript content boundary")
            persist_receipts()
            vm.run_command("rm -f /home/user/instance_next.md", timeout=30)
            return finish({"ok": False, "reasons": [
                "curriculum transcript content boundary"]},
                "curriculum-transcript-quarantined", hits)
        if getattr(latest_result, "status", "infra") != "done":
            return finish({"ok": False, "reasons": [
                f"curriculum correction status={latest_result.status}"]},
                "curriculum-correction-incomplete")

        next_raw = _guest_text_lossless(vm, "~/instance_next.md")
        mt = vm.run_command(
            "stat -c %Y /home/user/instance_next.md 2>/dev/null",
            timeout=30) or "0"
        try:
            fresh = bool(next_raw) and int(mt.split()[0]) >= \
                int(t_mark.split()[0])
        except (ValueError, IndexError):
            fresh = False
        if not fresh:
            return finish({"ok": False, "reasons": [
                "curriculum correction handoff is missing or stale"]},
                "curriculum-correction-stale")
        raw = next_raw
        candidate = _new_project_body(raw)


def _preflight_repair_with_feedback(
        vm, pj: dict, initial_text: str, cfg, sink_root: str,
        initial_history=None, initial_result=None,
        initial_iters_used=None, initial_wall_used=None,
        agent_decided_stop: bool = False,
        authorized_instruction: str = "") -> dict:
    """Let the same Curriculum context correct a rejected preflight REPAIR.

    This is the existing new-project correction law applied to a detached
    same-identity repair candidate: every host rejection is receipt-bound to
    the exact candidate bytes, ordinary validation errors return verbatim to
    the same conversation, and only the aggregate Curriculum safety budget
    bounds recurrence.  No project state is published by this helper.
    """
    from tools.exam_fence import audit_text, audit_transcripts

    history = list(initial_history or [])
    latest_result = initial_result
    total_iters = int(
        getattr(initial_result, "iters", 0) or 0
        if initial_iters_used is None else initial_iters_used)
    total_wall = float(
        getattr(initial_result, "wall_secs", 0.0) or 0.0
        if initial_wall_used is None else initial_wall_used)
    max_iters = int(getattr(cfg, "max_iters", 0) or 0)
    max_wall = float(getattr(cfg, "wall_clock_secs", 0.0) or 0.0)
    raw = initial_text or ""
    receipts = []
    receipt_path = os.path.join(
        sink_root, "preflight_repair_candidate_receipts.json")
    seen = {}
    continuation = 0
    latest_parsed = {"ok": False, "reasons": [
        "preflight repair candidate was not validated"]}

    def persist_receipts():
        os.makedirs(sink_root, exist_ok=True)
        staged = receipt_path + ".tmp"
        with open(staged, "w", encoding="utf-8") as f:
            json.dump(receipts, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, receipt_path)
        directory_fd = os.open(sink_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def finish(parsed, terminal, transcript_hits=None, notebook_hits=None):
        return {
            "ok": bool(parsed and parsed.get("ok")),
            "parsed": parsed or {"ok": False, "reasons": []},
            "raw_text": raw, "history": history, "result": latest_result,
            "receipts": receipts, "iters": total_iters,
            "wall_secs": total_wall, "continuations": continuation,
            "terminal": terminal,
            "transcript_hits": list(transcript_hits or []),
            "notebook_hits": list(notebook_hits or []),
            "candidate_quarantined": terminal == (
                "candidate-fence-quarantined"),
            "receipt_path": receipt_path,
        }

    # As with new-project feedback, receipts are durable audit evidence rather
    # than a conversation-resume WAL. Never mingle a restarted Agent boundary.
    if os.path.lexists(receipt_path):
        try:
            if os.path.islink(receipt_path) or not os.path.isfile(receipt_path):
                raise ValueError("unsafe receipt path")
            with open(receipt_path, encoding="utf-8") as f:
                prior = json.load(f)
            if not isinstance(prior, list):
                raise ValueError("receipt ledger is not a list")
            receipts.extend(prior)
        except (OSError, ValueError, json.JSONDecodeError):
            return finish({"ok": False, "reasons": [
                "existing preflight-repair receipt is unreadable"]},
                "existing-feedback-receipt-unreadable")
        return finish({"ok": False, "reasons": [
            "existing preflight-repair receipt requires recovery"]},
            "existing-feedback-receipt-requires-recovery")

    while True:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        actual_decision = _new_project_decision(raw)
        latest_notes = _guest_text_lossless(vm, "~/curriculum_notes.md")
        notebook_hits = (audit_text(
            latest_notes, mode="practice",
            authorized_instruction=authorized_instruction)
            if latest_notes else [])
        if notebook_hits:
            receipt = {
                "attempt": len(receipts) + 1,
                "candidate_sha256": digest,
                "reasons": ["curriculum notebook content boundary"],
                "phase": "curriculum-notebook-fence",
                "status": "curriculum-notebook-quarantined",
                "decision": actual_decision,
                "expected_decision": "REPAIR",
                "fence_hit": True,
            }
            receipts.append(receipt)
            persist_receipts()
            vm.run_command(
                "rm -f /home/user/instance_next.md "
                "/home/user/instance_rejected.md", timeout=30)
            return finish({"ok": False, "reasons": receipt["reasons"]},
                          receipt["status"], notebook_hits=notebook_hits)

        candidate_hits = audit_text(
            raw, mode="practice",
            authorized_instruction=authorized_instruction)
        if candidate_hits:
            reasons = ["preflight repair content outside authorized boundary"]
            receipt = {
                "attempt": len(receipts) + 1,
                "candidate_sha256": digest,
                "reasons": reasons,
                "phase": "static-validation",
                "status": "candidate-fence-quarantined",
                "decision": actual_decision,
                "expected_decision": "REPAIR",
                "fence_hit": True,
            }
            receipts.append(receipt)
            persist_receipts()
            vm.run_command(
                "rm -f /home/user/instance_next.md "
                "/home/user/instance_rejected.md", timeout=30)
            return finish({"ok": False, "reasons": reasons},
                          receipt["status"])

        latest_parsed = _parse_curriculum_repair_ruling(
            raw, pj, agent_decided_stop, authorized_instruction)
        reasons = list(latest_parsed.get("reasons", []))
        receipt = {
            "attempt": len(receipts) + 1,
            "candidate_sha256": digest,
            "reasons": reasons,
            "phase": "static-validation",
            "status": "validated" if latest_parsed.get("ok") else "rejected",
            "decision": actual_decision,
            "expected_decision": "REPAIR",
            "fence_hit": False,
        }
        if latest_parsed.get("ok"):
            receipt["repair_kind"] = latest_parsed.get("kind")
        receipts.append(receipt)
        persist_receipts()
        if latest_parsed.get("ok"):
            vm.run_command(
                "rm -f /home/user/instance_rejected.md", timeout=30)
            return finish(latest_parsed, "validated")

        signature = (digest, tuple(reasons), actual_decision)
        seen[signature] = seen.get(signature, 0) + 1
        receipt["recurrence"] = seen[signature]
        persist_receipts()

        iters_left = max_iters - total_iters
        wall_left = max_wall - total_wall
        if iters_left <= 0 or wall_left <= 0:
            return finish(latest_parsed, "curriculum-safety-budget-exhausted")

        continuation += 1
        sink_dir = os.path.join(
            sink_root, f"preflight_repair_reauthor_{continuation:03d}")
        candidate_dir = os.path.join(
            sink_root, "preflight_repair_candidates")
        os.makedirs(candidate_dir, exist_ok=True)
        candidate_path = os.path.join(
            candidate_dir, f"candidate_{len(receipts):03d}.md")
        with open(candidate_path, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        candidate_dir_fd = os.open(candidate_dir, os.O_RDONLY)
        try:
            os.fsync(candidate_dir_fd)
        finally:
            os.close(candidate_dir_fd)
        history_bytes = json.dumps(
            history, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        receipt["candidate_artifact_path"] = os.path.relpath(
            candidate_path, sink_root)
        receipt["continuation_sink"] = os.path.relpath(
            sink_dir, sink_root)
        receipt["parent_history_sha256"] = hashlib.sha256(
            history_bytes).hexdigest()
        receipt["status"] = "feedback-pending"
        persist_receipts()

        import base64
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        vm.run_command(
            f"echo {encoded} | base64 -d > /home/user/instance_rejected.md; "
            "rm -f /home/user/instance_next.md", timeout=60)
        t_mark = vm.run_command("date +%s", timeout=30) or "0"
        latest_result, history = run_attempt(
            preflight_repair_rejection_msg(
                digest, reasons, pj["name"], int(pj["next_night"]),
                int(pj["n_nights"])),
            vm, cfg, ArtifactSink(sink_dir),
            iters_budget=iters_left, wall_budget=wall_left,
            initial_history=history, continue_context=True)
        total_iters += int(getattr(latest_result, "iters", 0) or 0)
        total_wall += float(getattr(latest_result, "wall_secs", 0.0) or 0.0)
        receipt["continuation_status"] = getattr(
            latest_result, "status", "infra")
        receipt["status"] = (
            "feedback-completed" if receipt["continuation_status"] == "done"
            else "feedback-incomplete")
        persist_receipts()

        hits = audit_transcripts(
            sink_dir, mode="practice",
            authorized_instruction=authorized_instruction)
        if hits:
            receipt["status"] = "curriculum-transcript-quarantined"
            receipt["terminal_reason"] = (
                "curriculum transcript content boundary")
            persist_receipts()
            vm.run_command("rm -f /home/user/instance_next.md", timeout=30)
            return finish({"ok": False, "reasons": [
                "curriculum transcript content boundary"]},
                receipt["status"], transcript_hits=hits)
        if getattr(latest_result, "status", "infra") != "done":
            return finish({"ok": False, "reasons": [
                "curriculum correction status=" + str(
                    getattr(latest_result, "status", "infra"))]},
                "curriculum-correction-incomplete")

        next_raw = _guest_text_lossless(vm, "~/instance_next.md")
        mt = vm.run_command(
            "stat -c %Y /home/user/instance_next.md 2>/dev/null",
            timeout=30) or "0"
        try:
            fresh = bool(next_raw) and int(mt.split()[0]) >= \
                int(t_mark.split()[0])
        except (ValueError, IndexError):
            fresh = False
        if not fresh:
            return finish({"ok": False, "reasons": [
                "curriculum correction handoff is missing or stale"]},
                "curriculum-correction-stale")
        raw = next_raw


def _curriculum_stage(vm, ep_no, root, CURR, JOURNAL, eproot, verdict,
                      meta, ccfg, pj, ppath, reauthor_only=False,
                      preflight_only: bool = False,
                      preflight_journal_label: str = "",
                      agent_decided_stop: bool = False,
                      research_direction: str = "",
                      target_task: str = ""):
    """Archive push -> tombstones -> session -> pull -> DECISION + cards ->
    archive removal with absence proof (M7)."""
    _assert_target_null_task(vm, target_task)
    journal_label = ((preflight_journal_label or
                      f"preflight_ep{ep_no:03d}")
                     if preflight_only else "")
    from tools.exam_fence import audit_text, audit_transcripts
    stage = os.path.join(root, "_archive_stage")
    arc = build_curriculum_archive(root, stage, target_task)
    push_dir(vm, stage, GUEST_ARCHIVE)
    notes_prior = _load_notes(CURR)
    import base64
    vm.run_command("rm -f /home/user/instance_next.md "
                   "/home/user/curriculum_notes.md", timeout=30)
    t_mark = vm.run_command("date +%s", timeout=30) or "0"
    vm.run_command(
        f"echo {base64.b64encode(notes_prior.encode()).decode()} | "
        "base64 -d > /home/user/curriculum_notes.md", timeout=60)
    sink_dirs = []
    curriculum_history = []
    curriculum_iters = 0
    curriculum_wall = 0.0
    for attempt in range(2):
        remaining_iters = int(getattr(ccfg, "max_iters", 0) or 0) - \
            curriculum_iters
        remaining_wall = float(getattr(
            ccfg, "wall_clock_secs", 0.0) or 0.0) - curriculum_wall
        if attempt and (remaining_iters <= 0 or remaining_wall <= 0):
            break
        sink_dir = os.path.join(
            eproot, ("reauthor" if reauthor_only else "curriculum") +
            ("" if attempt == 0 else "_retry"))
        sink_dirs.append(sink_dir)
        budget_kwargs = ({"iters_budget": remaining_iters,
                          "wall_budget": remaining_wall}
                         if attempt else {})
        cres, curriculum_history = run_attempt(
            e7_curriculum_charter(_project_block(
                                      pj, include_transport=preflight_only),
                                  verdict,
                                  curve_tail_text(root),
                                  _transcript_slice(eproot), notes_prior,
                                  agent_decided_stop=agent_decided_stop,
                                  research_direction=(
                                      "" if reauthor_only
                                      else research_direction),
                                  target_task=target_task,
                                  preflight=preflight_only),
            vm, ccfg, ArtifactSink(sink_dir), **budget_kwargs)
        curriculum_iters += int(getattr(cres, "iters", 0) or 0)
        curriculum_wall += float(getattr(cres, "wall_secs", 0.0) or 0.0)
        if cres.status == "done":
            break
        # channel-flake resilience: one retry on infra/stalled (the
        # curriculum's own notebook documents the flake; ep001/ep002
        # both lost their rulings to it)
    meta["curriculum_status" + ("_r" if reauthor_only else "")] = cres.status
    if target_task:
        transcript_hits = []
        for sink_dir in sink_dirs:
            transcript_hits.extend(audit_transcripts(
                sink_dir, mode="practice",
                authorized_instruction=target_task))
        if transcript_hits:
            vm.run_command("rm -f /home/user/instance_next.md "
                           "/home/user/curriculum_notes.md", timeout=30)
            rm_guest_dir(vm, GUEST_ARCHIVE)
            meta["curriculum_transcript_quarantined"] = True
            meta["next_card_status"] = (
                "rejected: curriculum transcript content boundary")
            meta["archive"] = {
                "files": arc["files"], "excluded": arc["excluded"],
                "removed": "curriculum_archive" not in
                (vm.run_command("ls /home/user/ 2>/dev/null",
                                timeout=30) or "")}
            return {"card_text": ""}
    nxt = _guest_text_lossless(vm, "~/instance_next.md")
    mt = vm.run_command("stat -c %Y /home/user/instance_next.md 2>/dev/null",
                        timeout=30) or "0"
    notes = _guest_text_lossless(vm, "~/curriculum_notes.md")
    proof = rm_guest_dir(vm, GUEST_ARCHIVE)
    meta["archive"] = {"files": arc["files"], "excluded": arc["excluded"],
                       "removed": "curriculum_archive" not in
                       (vm.run_command(f"ls /home/user/ 2>/dev/null",
                                       timeout=30) or "")}
    # mtime freshness (M7): stale residue is never accepted
    try:
        fresh = int(mt.split()[0]) >= int(t_mark.split()[0])
    except (ValueError, IndexError):
        fresh = False
    if cres.status != "done" or not fresh:
        meta["next_card_status"] = f"rejected: status={cres.status} fresh={fresh}"
        # v1.3 (bug #14, ep21): the registered default-continue must fire
        # even when the ruling SESSION is lost (channel stall) — not only
        # when a completed session omits DECISION. ep21: a PASSED repair
        # was left unruled ($32 stalled night), pending_repair stayed set,
        # ep22 re-served the already-passed card. The curriculum remains
        # sovereign on fails/finals/abandons — those still block.
        if (not reauthor_only) and pj and \
                pj.get("status") in ("accepted", "in_progress") and \
                meta.get("outcome") == "pass" and \
                meta.get("kind") in ("milestone", "repair"):
            meta["decision"] = "default-continue(no-ruling)"
            pj["decisions"].append(
                {"ep": ep_no, "ruling": "default-continue(no-ruling)",
                 "note": "ruling session lost (bug #14 path)"})
            pj["pending_repair"] = None
            if meta["kind"] == "milestone" and \
                    pj["next_night"] < pj["n_nights"]:
                pj["next_night"] += 1
            pj["status"] = ("in_progress" if pj["status"] == "accepted"
                            else pj["status"])
            pj["artifact_semantics"] = _ARTIFACT_SEMANTICS
            save_project(ppath, pj)
        return {"card_text": ""}

    new_project_feedback = None

    def reauthor_new_project(candidate_raw: str,
                             expected_decision: str | None = None):
        feedback = accept_new_project_with_feedback(
            vm, ep_no, root, candidate_raw, ccfg, sink_dirs[-1],
            initial_history=curriculum_history, initial_result=cres,
            initial_iters_used=curriculum_iters,
            initial_wall_used=curriculum_wall,
            agent_decided_stop=agent_decided_stop,
            authorized_instruction=target_task,
            expected_decision=expected_decision,
            journal_label=journal_label)
        meta["new_project_candidate_receipts"] = feedback["receipts"]
        meta["new_project_candidate_receipt_path"] = os.path.relpath(
            feedback["receipt_path"], eproot)
        meta["new_project_reauthor_continuations"] = feedback["continuations"]
        meta["new_project_reauthor_terminal"] = feedback["terminal"]
        meta["curriculum_iters_total"] = feedback["iters"]
        meta["curriculum_wall_secs_total"] = feedback["wall_secs"]
        acceptance = feedback["acceptance"]
        reasons = list(acceptance.get("reasons", []))
        meta["new_project_rejection_reasons"] = reasons
        meta["new_project"] = (
            acceptance["project"]["name"] if acceptance.get("ok")
            else "rejected: " + "; ".join(reasons))
        return feedback

    # When this stage began without a live project, correct host-rejected
    # drafts now, in the same Curriculum conversation.  This includes a
    # missing/malformed PROJECT transport; a drill remains an explicit,
    # separately registered rejection below.
    live_before_ruling, _ = live_project(root)
    initial_body = _new_project_body(nxt)
    if live_before_ruling is None and not (
            initial_body.startswith("INSTANCE") and
            "MODE: DRILL" in initial_body):
        new_project_feedback = reauthor_new_project(nxt, "CONTINUE")
        if (new_project_feedback["candidate_quarantined"] or
                new_project_feedback["fixture_quarantined"] or
                new_project_feedback["transcript_hits"] or
                new_project_feedback["notebook_hits"]):
            vm.run_command("rm -f /home/user/instance_next.md "
                           "/home/user/instance_rejected.md "
                           "/home/user/curriculum_notes.md", timeout=30)
            if new_project_feedback["candidate_quarantined"]:
                meta["new_project_candidate_quarantined"] = True
                boundary = "new-project candidate content boundary"
            elif new_project_feedback["fixture_quarantined"]:
                meta["new_project_fixture_quarantined"] = True
                boundary = "captured fixture content boundary"
            elif new_project_feedback["transcript_hits"]:
                meta["curriculum_transcript_quarantined"] = True
                boundary = "curriculum transcript content boundary"
            else:
                meta["curriculum_notebook_quarantined"] = True
                meta["notebook"] = "fence-quarantined"
                boundary = "curriculum notebook content boundary"
            meta["next_card_status"] = "rejected: " + boundary
            return {"card_text": ""}
        nxt = new_project_feedback["raw_text"]
        notes = _guest_text_lossless(vm, "~/curriculum_notes.md")

    # notebook (uncapped, fence-audited, archived — F16/N5)
    _persist_curriculum_notes(
        CURR, JOURNAL, ep_no, notes, meta, target_task, journal_label)
    if reauthor_only:
        return {"card_text": nxt}

    # DECISION + what follows.  In formal preflight, KEEP is a readable alias
    # for CONTINUE-without-advancement; the stored raw ruling remains intact.
    ruling_nxt = nxt or ""
    keep_alias = bool(preflight_only and re.search(
        r"^DECISION:\s*KEEP\s*$", ruling_nxt, re.M))
    if keep_alias:
        ruling_nxt = re.sub(r"^DECISION:\s*KEEP\s*$",
                            "DECISION: CONTINUE", ruling_nxt,
                            count=1, flags=re.M)
    m = _DECISION.search(ruling_nxt)
    decision = m.group(1) if m else None
    if decision is None and meta.get("outcome") == "pass" and \
            meta.get("kind") in ("milestone", "repair"):
        # registered harness default (deviation, 08-17): a PASSED milestone
        # with a lost ruling advances as CONTINUE — the curriculum stays
        # sovereign wherever judgment matters (fails, finals, abandons)
        decision = "CONTINUE"
        meta["decision"] = "default-continue(no-ruling)"
    elif keep_alias:
        meta["decision"] = "KEEP"
    else:
        meta["decision"] = decision or "missing"
    body = _DECISION.sub("", ruling_nxt, count=1).strip()
    if pj and pj.get("status") in ("accepted", "in_progress"):
        if decision == "REPAIR":
            # A REPAIR ruling is an atomic state transition.  Never search a
            # free-form response for a NIGHT-looking substring: validate the
            # exact live identity and complete body (or a complete same-name
            # project replacement), then republish the accepted fixture
            # baseline before publishing it.
            repair_feedback = None
            if preflight_only:
                repair_feedback = _preflight_repair_with_feedback(
                    vm, pj, ruling_nxt, ccfg, sink_dirs[-1],
                    initial_history=curriculum_history,
                    initial_result=cres,
                    initial_iters_used=curriculum_iters,
                    initial_wall_used=curriculum_wall,
                    agent_decided_stop=agent_decided_stop,
                    authorized_instruction=target_task)
                meta["preflight_repair_candidate_receipts"] = \
                    repair_feedback["receipts"]
                meta["preflight_repair_candidate_receipt_path"] = \
                    os.path.relpath(repair_feedback["receipt_path"], eproot)
                meta["preflight_repair_continuations"] = \
                    repair_feedback["continuations"]
                meta["preflight_repair_terminal"] = \
                    repair_feedback["terminal"]
                meta["curriculum_iters_total"] = repair_feedback["iters"]
                meta["curriculum_wall_secs_total"] = \
                    repair_feedback["wall_secs"]
                nxt = repair_feedback["raw_text"]
                ruling_nxt = nxt
                repaired = repair_feedback["parsed"]
                meta["preflight_repair_rejection_reasons"] = list(
                    repaired.get("reasons", []))
                if (repair_feedback["candidate_quarantined"] or
                        repair_feedback["transcript_hits"] or
                        repair_feedback["notebook_hits"]):
                    vm.run_command(
                        "rm -f /home/user/instance_next.md "
                        "/home/user/instance_rejected.md "
                        "/home/user/curriculum_notes.md", timeout=30)
                    if repair_feedback["candidate_quarantined"]:
                        meta["preflight_repair_candidate_quarantined"] = True
                        boundary = "preflight repair candidate content boundary"
                    elif repair_feedback["transcript_hits"]:
                        meta["curriculum_transcript_quarantined"] = True
                        boundary = "curriculum transcript content boundary"
                    else:
                        meta["curriculum_notebook_quarantined"] = True
                        meta["notebook"] = "fence-quarantined"
                        boundary = "curriculum notebook content boundary"
                    meta["repair_card"] = "rejected: " + boundary
                    meta["repair_transport"] = "rejected"
                    meta["next_card_status"] = "rejected: " + boundary
                    return {"card_text": ""}
                if repair_feedback["ok"]:
                    latest_notes = _guest_text_lossless(
                        vm, "~/curriculum_notes.md")
                    _persist_curriculum_notes(
                        CURR, JOURNAL, ep_no, latest_notes, meta,
                        target_task, journal_label)
                    if meta.get("notebook") == "fence-quarantined":
                        meta["curriculum_notebook_quarantined"] = True
                        meta["repair_card"] = (
                            "rejected: curriculum notebook content boundary")
                        meta["repair_transport"] = "rejected"
                        return {"card_text": ""}
            else:
                repaired = _parse_curriculum_repair_ruling(
                    ruling_nxt, pj, agent_decided_stop, target_task)
            if not repaired["ok"]:
                meta["repair_card"] = (
                    "rejected: " + "; ".join(repaired["reasons"])[:300])
                meta["repair_transport"] = "rejected"
            else:
                candidate = repaired["project"]
                if not preflight_only:
                    candidate["decisions"].append(
                        {"ep": ep_no, "ruling": decision})
                candidate["status"] = (
                    "in_progress" if not preflight_only and
                    candidate["status"] == "accepted"
                    else candidate["status"])
                persisted = _persist_reauthored_project(
                    vm, root, ppath, pj, candidate,
                    repaired["card_text"],
                    authorized_instruction=target_task)
                meta["repair_transport"] = repaired["kind"]
                meta["repair_recaptured"] = persisted.get("ok", False)
                meta["repair_fixture_source"] = persisted.get(
                    "fixture_source", "")
                if not persisted.get("persisted"):
                    meta["repair_card"] = (
                        "rejected: " + str(persisted.get(
                            "error", "project recapture failed"))[:300])
                    meta["repair_transport"] = "rejected"
                else:
                    meta["repair_card"] = "accepted"
        elif decision == "CONTINUE":
            # CONTINUE may revise exactly the upcoming NIGHT and/or FINAL.
            # Parse into a detached candidate so a malformed or out-of-scope
            # amendment cannot partially advance the live pointer.
            amended = (_parse_curriculum_continue_ruling(
                ruling_nxt, pj, agent_decided_stop, target_task)
                if m is not None else
                {"ok": True, "attempted": False,
                 "project": copy.deepcopy(pj), "reasons": [],
                 "night_amended": False, "final_amended": False})
            if preflight_only and amended.get("attempted"):
                meta["continue_amendment"] = (
                    "rejected: preflight CONTINUE/KEEP cannot amend project "
                    "transport; use REPAIR")
                meta["continue_transition"] = "rejected"
            elif not amended["ok"]:
                meta["continue_amendment"] = (
                    "rejected: " + "; ".join(amended["reasons"])[:300])
                meta["continue_transition"] = "rejected"
            else:
                candidate = amended["project"]
                project_dir = os.path.join(root, "projects", pj["key"])
                owned = set(read_owned_paths(project_dir))
                needed = set(_owned_project_paths(
                    _project_transport_text(candidate)))
                introduced = sorted(needed - owned)
                if amended["attempted"] and introduced:
                    # A scoped amendment cannot silently enlarge the replay
                    # or graded-artifact ownership boundary. Curriculum can
                    # use a complete REPAIR transport, whose recapture is an
                    # atomic audited transition, when a new root is needed.
                    meta["continue_amendment"] = (
                        "rejected: ownership expansion requires complete "
                        f"REPAIR transport: {introduced[:4]}")
                    meta["continue_transition"] = "rejected"
                else:
                    if not preflight_only:
                        candidate["decisions"].append(
                            {"ep": ep_no, "ruling": decision})
                    if not preflight_only:
                        candidate["pending_repair"] = None
                    if meta.get("kind") in ("milestone", "repair") and \
                            meta.get("outcome") in ("pass", "fail") and \
                            candidate["next_night"] < candidate["n_nights"]:
                        candidate["next_night"] += 1
                    candidate["status"] = (
                        "in_progress" if not preflight_only and
                        candidate["status"] == "accepted"
                        else candidate["status"])
                    candidate["artifact_semantics"] = _ARTIFACT_SEMANTICS
                    save_project(ppath, candidate)
                    pj.clear()
                    pj.update(candidate)
                    meta["continue_transition"] = "accepted"
                    meta["continue_amendment"] = (
                        "accepted" if amended["attempted"] else "none")
                    if amended["night_amended"]:
                        meta["upcoming_night_amended"] = pj["next_night"]
                    if amended["final_amended"]:
                        meta["final_amended"] = len([
                            line for line in pj["final_text"].splitlines()
                            if _NUM.match(line)])
        elif not preflight_only or decision == "ABANDON":
            if not preflight_only:
                pj["decisions"].append(
                    {"ep": ep_no, "ruling": decision})
            if decision == "ABANDON":
                pj["status"] = "failed_abandoned"
            pj["status"] = ("in_progress" if not preflight_only and
                            pj["status"] == "accepted"
                            else pj["status"])
            pj["artifact_semantics"] = _ARTIFACT_SEMANTICS
            save_project(ppath, pj)
    # a NEW PROJECT card (when none live, or after complete/abandon)
    live, _ = live_project(root)
    if body.strip().startswith("INSTANCE") and "MODE: DRILL" in body:
        # PURE-PREVIEW ERA (Sibo ruling 08-17): review drills are EXCLUDED
        # from E7 — whether/how a review objective exists is decided AFTER
        # this era's verdict. The machinery stays built but disabled.
        meta["drill_card"] = "rejected: drills excluded this era (registered)"
    elif live is None and new_project_feedback is None:
        # This covers a project that became non-live because of the ruling
        # above (for example ABANDON followed by a replacement).  It uses the
        # same uncapped, cumulative-budget correction path as authoring-only.
        new_project_feedback = reauthor_new_project(
            nxt, decision or _new_project_decision(nxt) or "CONTINUE")
        if (new_project_feedback["candidate_quarantined"] or
                new_project_feedback["fixture_quarantined"] or
                new_project_feedback["transcript_hits"] or
                new_project_feedback["notebook_hits"]):
            vm.run_command("rm -f /home/user/instance_next.md "
                           "/home/user/instance_rejected.md "
                           "/home/user/curriculum_notes.md", timeout=30)
            if new_project_feedback["candidate_quarantined"]:
                meta["new_project_candidate_quarantined"] = True
                boundary = "new-project candidate content boundary"
            elif new_project_feedback["fixture_quarantined"]:
                meta["new_project_fixture_quarantined"] = True
                boundary = "captured fixture content boundary"
            elif new_project_feedback["transcript_hits"]:
                meta["curriculum_transcript_quarantined"] = True
                boundary = "curriculum transcript content boundary"
            else:
                meta["curriculum_notebook_quarantined"] = True
                meta["notebook"] = "fence-quarantined"
                boundary = "curriculum notebook content boundary"
            meta["next_card_status"] = "rejected: " + boundary
            return {"card_text": ""}
        nxt = new_project_feedback["raw_text"]
        body = new_project_feedback["candidate_text"]
        latest_notes = _guest_text_lossless(vm, "~/curriculum_notes.md")
        _persist_curriculum_notes(
            CURR, JOURNAL, ep_no, latest_notes, meta, target_task,
            journal_label)
    if nxt.strip():
        instance_journal = (f"{journal_label}_instance.md"
                            if journal_label else
                            f"journal_ep{ep_no:03d}_instance.md")
        open(os.path.join(CURR, instance_journal),
             "w").write(nxt)
    vm.run_command("rm -f /home/user/instance_rejected.md", timeout=30)
    return {"card_text": body}


def curve_tail_text(root: str, k: int = 14) -> str:
    rows = reward.read_curve(root)[-k:]
    return "\n".join(
        f"{r.get('project','-')}  n{r.get('night',0)}/{r.get('n_nights',0)}"
        f"  {r.get('kind','?')}  {r.get('outcome','?')}"
        f"  {r.get('iters','?')} it / {r.get('budget','?')}"
        for r in rows)
