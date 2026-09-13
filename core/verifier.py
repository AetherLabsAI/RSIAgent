"""Independent verifier — a fresh-context inspection at done-time (v8, system-level).

The semantic-entanglement failure (false-done species #3) showed that an actor's own
checks inherit the actor's wrong assumptions, so more prompting cannot catch them.
The structural fix is INDEPENDENCE: a zero-history model call that sees only the task
instruction and the live machine — none of the actor's reasoning, evidence or checks —
re-derives the requirements (including any conventions) on its own draw, probes the
machine read-only, and judges. Acceptance: the actor's checks must pass AND this
inspection must not find a violation — a "pass" accepts; repeated "unverified" may
accept late under the loop's budget rules (could-not-confirm never vetoes forever);
"wrong" always bounces with the FINDINGS (evidence, not authority) as repair context.

THIS FILE IS AN AUDITED SURFACE (Constraint #0): tests/test_no_leakage.py scans it
against the task blocklist on every change. Everything here must be general.
"""
import base64
import hashlib
import logging
import os
import re
import tempfile
import time

from core.actor import EPISTEMIC_CORE, Look, Program, parse_turn
from core.checks import _category, _nonscratch_mutations
from llm.client import (LLMTransportError, chat, durable_images, parse_object,
                        with_durable_images)

log = logging.getLogger("forge.verifier")

# Mechanical liveness only. An Agent can investigate across many productive
# segments; repeated segments without any Program/Look require runtime recovery,
# never a fabricated candidate verdict or Curriculum convergence decision.
_MAX_ACTIONLESS_VERIFIER_SEGMENTS = 3


_VERIFIER_LOOK_SECTION = """\
or, to check a VISUAL requirement no shell probe can settle (whether something is \
POSITIONED at, POINTS at, is SIZED/COLORED as, or LOOKS LIKE something), issue \
  {{"look": {{"path": "/abs/deliverable", "question": "<the visual thing to confirm>"}}}}
— ONE look per turn; point it STRAIGHT at the deliverable file (a document file is \
rendered to an image for you; you do NOT need to convert it yourself with a probe). \
When the deliverable is NOT a file but the state of a RUNNING app or browser session \
(a booking, a submitted form, an on-screen result), use path "screen:" to look at \
the LIVE screen instead — the work may be real yet leave no file the filesystem \
probes can find. The answer is an independent visual reading of the machine's \
current state; the exact delivered pixels remain in your persistent conversation \
history. Use them to confirm or refute visual requirements you cannot verify from \
text alone.
"""

_VERIFIER_NO_LOOK_SECTION = """\
(This configuration has NO visual channel: there is no "look" action — verify \
through text probes only, and report requirements that would need visual \
confirmation as could-not-confirm rather than guessing.)
"""

_VERIFIER_TEMPLATE = """\
You are an INDEPENDENT INSPECTOR. Someone else claims to have completed the task \
below on this machine. You have deliberately NOT been shown their work, their \
reasoning, or their assumptions — your value is an independent reading. Decide \
whether the machine's CURRENT state actually satisfies the task.

EPISTEMIC CORE — how truth is established here (applies to everything below):
{core}

METHOD:
1. Read the task and derive its FULL requirement inventory yourself — every property \
the task states or implies, including any naming or convention rules. If the task \
contains an already-solved instance, derive the convention FROM that instance and \
verify your reading against the real data on the machine before judging anything else.\
{dest_rule}
2. Probe the machine READ-ONLY to gather evidence for each requirement.
3. Judge each requirement on the evidence alone (core E6). Be suspicious: plausible \
is not verified, and a wrong interpretation that fails on the given instance is wrong.
4. Choose the verification channel by the claim type (core E2): structural evidence \
(coordinates, values, cell contents, attributes) proves STRUCTURAL facts; a \
requirement about a RENDERED result is settled by looking at the rendered \
deliverable, not by the numbers. WHEN A RENDER IS IMPOSSIBLE in this environment, \
MEASURE instead — derive the checkable quantities (dimensions, coordinates, counts, \
colors, text) from the source file with code (python standard library or the \
application's command-line/headless modules) and judge those against the requirement.
5. Before emitting `pass`, try to FALSIFY your own conclusion: identify the strongest \
plausible material way the COMPLETE result could still violate the task, including an \
interaction among parts that each look correct alone. If that possibility is unresolved, \
investigate it or return `wrong`/`unverified`; emit the one terminal `pass` only when \
your evidence settles it.

Each turn reply with ONLY ONE JSON object:
  {{"probes": ["<read-only shell command>", "..."]}}          // up to 4 per turn
{look_section}or your final verdict:
  {{"verdict": "pass" | "wrong" | "unverified",
{items_line}   "findings": "<per requirement: met / violated / could-not-confirm + the concrete \
evidence>",
   "doubts": "<ONLY unresolved uncertainty about a MATERIAL TASK requirement. Put \
incidental observations and non-blocking caveats in findings instead. This MUST be \
an empty string for pass; never omit a task-relevant doubt to make a pass smoother.>"}}

The three verdicts are NOT interchangeable:
- "pass": EVERY MATERIAL requirement is affirmatively evidenced on the machine.
- "wrong": you found CONCRETE evidence that some requirement is VIOLATED — cite that \
evidence in findings so it can be fixed. A required artifact shown ABSENT by a probe \
that ran successfully IS "wrong" (cite the listing that proves the absence).
- "unverified": you could not gather enough evidence to confirm or refute — that is \
NOT the same as wrong; say exactly what you could not confirm and why. Never report \
"wrong" on mere suspicion, and never report "pass" on hope.
Evidence rules are core E3/E4: errors are evidence (never silence the error stream \
of the command whose result you need); a timed-out probe, a "[channel error ...]" \
line, or empty output from an UNVERIFIED channel proves nothing — re-probe narrowly \
instead of concluding; empty output on a channel verified alive IS real evidence.

Rules: probes must be read-only (no writes, moves, deletions, installs). PROBE \
ECONOMY — timed-out probes are the main way inspections end unverified: start from \
the exact paths the task names; use cheap existence and attribute probes first (ls, \
test -e, head, wc -l) and read contents only where a requirement demands it; \
NEVER scan the whole filesystem or a whole home tree (find / grep -r over a large \
tree WILL time out and prove nothing) — target the narrowest directory that answers \
the requirement; batch several quick related probes into one round rather than \
spending a round on one big command; if a probe times out, split it into narrower \
probes, never repeat it. Probes are JSON strings — escape newlines as \\n, or write \
single-line commands joined by ';'."""


def _verifier_eyes_model(cfg) -> str:
    """v39.1: which model serves the INSPECTOR's looks. Explicit verifier_vision_model
    wins ('self' = the primary's SIGHTED lineage); else the actor's delegated eyes
    (vision_model); else none (blind).

    v40 eyes-fix (2026-07-22): 'self' resolves vision_model FIRST, then model. In the
    MAIN segment this is unchanged (native primary: vision_model='' -> model = the
    sighted primary). In an ESCALATED segment _replace has rewritten model to the
    text-only escalation model, but the demoted-primary-becomes-eyes rule parked the
    original sighted primary in vision_model — the old 'return cfg.model' branch
    ignored it and routed the inspector's looks to a model with no image endpoint
    (404 -> '(vision tool returned nothing)' -> probe round burned). Campaign forensics:
    main segments 108/108 looks answered; escalated segments ~0/63 — all 37 escalated
    tasks inspected blind. Same root cause as every role-relative-resolution bug:
    'self' must follow the sighted lineage, not the job title."""
    vvm = getattr(cfg, "verifier_vision_model", "") or ""
    if vvm == "self":
        return (getattr(cfg, "vision_model", "") or ""
                or getattr(cfg, "model", "") or "")
    return vvm or getattr(cfg, "vision_model", "") or ""


def build_verifier_system(cfg=None) -> str:
    """v35: the inspector's prompt is assembled per inspection so its mechanics are
    TRUE for the active configuration — the look action used to be advertised
    unconditionally and silently dropped when no vision model was configured."""
    vis = bool(_verifier_eyes_model(cfg)) and getattr(cfg, "vision_verify", True)
    dest = (" When the task names a deliverable file, verify THAT file at THAT exact "
            "path — work found at other paths is staging, not delivery."
            if getattr(cfg, "verifier_destination_note", False) else "")
    # E4-A1c (wenyi): the per-requirement judgement becomes DATA, not prose buried
    # in `findings`. It carries what the harness could never read before — which
    # requirement is unsettled — so a bounce can name the one thing to fix and
    # progress between inspections is measurable. The scalar verdict remains the
    # inspector's own call: items inform, they do not compute the ruling.
    items = ("""   "items": [{"req": "<one requirement, in your own words>",
              "status": "met" | "violated" | "could-not-confirm",
              "evidence": "<the concrete evidence, or what is missing>"}, ...],
"""
             if getattr(cfg, "verifier_items", False) else "")
    return _VERIFIER_TEMPLATE.format(
        core=EPISTEMIC_CORE,
        dest_rule=dest,
        items_line=items,
        look_section=_VERIFIER_LOOK_SECTION if vis else _VERIFIER_NO_LOOK_SECTION)


# Back-compat constant (vision-available variant).
class _V:
    vision_model = "x"
    vision_verify = True


VERIFIER_SYSTEM = build_verifier_system(_V())


class VerifierSession(list):
    """One Verifier Agent's conversation plus cumulative inspection telemetry.

    Conversation continuity preserves the Agent's own prior reasoning and findings.
    The counters are telemetry only: evidence freshness is deliberately local to each
    candidate inspection, because an earlier content probe may describe an artifact
    that the Actor Agent subsequently revised.
    """

    def __init__(self, messages=(), *, on_event=None):
        super().__init__(messages)
        self.informative_probes = 0
        self.content_probes = 0
        self.inspections = 0
        # Crash-safe agentic-verifier continuation metadata. The raw transcript is
        # authoritative; these fields carry only the environment result that follows
        # the last retained Program/Look action and therefore is not itself a complete
        # conversation pair.
        self.pending_observation = ""
        self.pending_images = []
        self.pending_turn = None
        self.transport_recurrence = False
        # Provider/API downtime accumulated inside this Verifier Agent.  The outer
        # Actor runtime reads deltas from these counters so a nested verifier outage
        # cannot consume the Actor's active-work wall clock either.
        self.infra_pause_secs = 0.0
        self.infra_pauses = 0
        # Unified target lifecycle metadata. Conversation is still the semantic
        # source of truth; these fields own only mechanical stage/executor state.
        self.stage = ""
        self.candidate_generation = 0
        self.orientation_runs = 0
        self.on_event = on_event
        self._program_executor = None
        self._scratch_archive = None

    def emit(self, event: str, **payload) -> None:
        callback = self.on_event
        if callable(callback):
            callback(event, payload)

    def acquire_executor(self, vm, *, hide_actor_memory=False,
                         private_paths=(), execution_mode="effect_isolated"):
        """Bind one private scratch lifecycle to the active target environment."""
        from core.verifier_runtime import (
            AgenticVerifierExecutor,
            _normalize_private_paths,
        )

        current = self._program_executor
        wanted_paths = _normalize_private_paths(
            hide_actor_memory, private_paths)
        if (current is not None and current.vm is vm
                and current.private_paths == wanted_paths
                and current.execution_mode == execution_mode):
            return current
        if current is not None:
            self.detach_executor(preserve=True)
        self._program_executor = AgenticVerifierExecutor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=wanted_paths,
            scratch_archive=self._scratch_archive,
            execution_mode=execution_mode)
        self._scratch_archive = None
        return self._program_executor

    def detach_executor(self, vm=None, *, preserve=True,
                        tolerate_archive_failure=False) -> None:
        """Detach before a target VM is discarded, optionally retaining scratch.

        During infrastructure recovery the guest controller may already be gone,
        making a final scratch export impossible.  The rollback performed by
        ``close`` remains mandatory; callers may only opt into losing the private
        scratch archive after that rollback succeeds.
        """
        from core.verifier_runtime import AgenticVerifierInfrastructureError

        executor = self._program_executor
        if executor is None or (vm is not None and executor.vm is not vm):
            return
        archive = None
        archive_error = None
        try:
            if preserve:
                try:
                    archive = executor.export_scratch()
                except AgenticVerifierInfrastructureError as exc:
                    if not tolerate_archive_failure:
                        raise
                    archive_error = exc
        finally:
            try:
                executor.close()
            finally:
                # Never retain a closed executor after either export or rollback
                # fails. A later recovery attempt must build a fresh trusted
                # boundary, not rediscover the poisoned cached object.
                self._program_executor = None
        if preserve:
            self._scratch_archive = archive
        if archive_error is not None:
            log.warning(
                "Verifier scratch could not be archived after a trusted-runtime "
                "failure; candidate rollback succeeded, so retrying with a fresh "
                "private scratch boundary: %s", archive_error)

    def close_executor(self) -> None:
        self.detach_executor(preserve=False)
        self._scratch_archive = None

_PASS_DOUBT_CLARIFICATION = """\
Your verdict says PASS, but `doubts` is non-empty. Resolve that contradiction \
YOURSELF inside this same inspection; do not hand an incidental note to the Actor \
Agent as repair work.

- If any stated doubt is unresolved and material to the TASK, continue probing or \
  return `wrong`/`unverified` as the evidence warrants.
- If the text is only a non-blocking caveat or unrelated observation, move it into \
  `findings` and return `pass` with `doubts` exactly `""`.

Reply with one normal probes/look object, or one revised verdict object."""


_CURRENT_CANDIDATE_EVIDENCE = """\
You proposed PASS without reading the CURRENT candidate during this inspection. \
Your earlier inspection remains useful context, but the Actor Agent may have revised \
the artifact since then. Probe or look at the current deliverable before ruling; then \
return the one normal terminal verdict when your evidence is sufficient."""


# v27: the JIT nudge that reminds the inspector it has EYES. It fires on a MODEL-INTRINSIC
# STALL signal (its probes keep returning no readable content) — NOT on any reading of the
# instruction. v25 used an instruction regex to detect "visual tasks", but that is a
# harness-side task-CLASSIFIER (contrary to forge's no-routing thesis) and it over-fired on
# ~1/3 of non-pixel tasks that merely use spatial words (position/align/center/placed…).
# Dropped entirely: WHETHER a requirement needs eyes is decided by the inspector via the
# general principle in VERIFIER_SYSTEM ("structural values do not prove a rendered result");
# this note only surfaces the capability WHEN text verification is visibly failing, on any
# task. Mirrors the actor's v23 look-redirect — static prompt text had ~0% uptake, only a
# JIT nudge binds (v13/v23 lesson).
_VISUAL_STEER = (
    "[INSPECTOR NOTE] You have not used your EYES yet — and text probes alone cannot "
    "settle rendered/visual requirements. You also have EYES: issue {\"look\": {\"path\": "
    "\"/abs/deliverable\", \"question\": \"<what to confirm>\"}} pointed STRAIGHT at the "
    "deliverable file (a document is rendered to an image for you — you do not need to "
    "convert it yourself). If the requirement is about how something LOOKS or WHERE it "
    "visually lands, the render settles it where structural values cannot.")


def _opening(instruction: str, rounds: int, context: str = "",
             tag_appearance: bool = False, elastic: bool = False) -> str:
    ctx = ("\n\n" + context) if context else ""
    tag = ("\n\nAs you derive your requirement inventory in your FIRST reply, tag "
           "each requirement [MEASURABLE] (settled by commands, parsing, counts, "
           "numbers) or [APPEARANCE] (settled only by viewing a rendered result: "
           "colors, layout, position, visual style, on-screen content)."
           if tag_appearance else "")
    # E4-A1b: with elastic depth the inspection ends on the inspector's judgment,
    # never on a counter — so the opening must not advertise a budget it would
    # otherwise pace itself against (every E3-era inspection spent its full
    # allowance and ruled on the last turn: budget-shaped stopping, not
    # judgment-shaped).
    budget = ("You decide your own depth: there is NO round budget. Keep probing "
              "(and looking) until the evidence settles every requirement, then "
              "rule — rule because YOUR evidence is sufficient, never because you "
              "are running out of turns."
              if elastic else f"You have {rounds} probe rounds.")
    return (f"TASK (as originally given):\n{instruction}{ctx}{tag}\n\n"
            f"{budget} The machine is live. Begin.")


class Findings(str):
    """The findings text, carrying the verdict's structured side-channels: `doubts` —
    what a PASS accepted without affirmative evidence (empty otherwise) — and
    `items` (E4-A1c) — the per-requirement judgement as data. A str subclass so
    every existing call site keeps working unchanged (ChatText precedent in
    llm.client)."""
    doubts: str = ""
    items: tuple = ()


def _clean_items(raw) -> tuple:
    """Normalise the inspector's items[] into (req, status, evidence) triples.
    Tolerant by design: a malformed entry is dropped, never fatal — the scalar
    verdict is always the authority, items are a side-channel."""
    ok = []
    for it in (raw or [])[:24]:
        if not isinstance(it, dict):
            continue
        req = str(it.get("req") or it.get("requirement") or "").strip()[:200]
        st = str(it.get("status") or "").strip().lower().replace("_", "-")
        if st in ("could not confirm", "couldnot-confirm", "unconfirmed"):
            st = "could-not-confirm"
        if st in ("partially-met", "partial"):
            st = "could-not-confirm"       # partial ⇒ not affirmatively settled
        if not req or st not in ("met", "violated", "could-not-confirm"):
            continue
        ok.append((req, st, str(it.get("evidence") or "").strip()[:300]))
    return tuple(ok)


def _mk_findings(txt: str, doubts="", items=None) -> Findings:
    f = Findings(txt)
    f.doubts = str(doubts or "")[:800]
    f.items = _clean_items(items)
    return f


def carry_findings(text: str, src) -> Findings:
    """Rebuild a Findings around NEW text while carrying `src`'s side-channels.

    E4-A1c bug (caught live on t034, 14 lost punch lists): the downgrade paths
    rebuilt findings by string concatenation, which returns a plain str and
    silently drops `.doubts`/`.items` — so every pass-with-doubts bounce, the
    most common bounce there is, reached the actor without its checklist. Any
    site that rewrites findings text must go through here."""
    f = Findings(text)
    f.doubts = str(getattr(src, "doubts", "") or "")
    f.items = tuple(getattr(src, "items", ()) or ())
    return f


def unsettled_items(findings) -> tuple:
    """The items a bounce should name: violated first, then could-not-confirm."""
    its = tuple(getattr(findings, "items", ()) or ())
    return (tuple(i for i in its if i[1] == "violated")
            + tuple(i for i in its if i[1] == "could-not-confirm"))


def items_punchlist(findings, cap: int = 6) -> str:
    """Render the unsettled items as a numbered punch list for the actor."""
    rows = unsettled_items(findings)[:cap]
    if not rows:
        return ""
    out = ["", "THE UNSETTLED ITEMS (address these specifically):"]
    for n, (req, st, ev) in enumerate(rows, 1):
        label = "VIOLATED" if st == "violated" else "COULD NOT CONFIRM"
        out.append(f"  {n}. [{label}] {req}" + (f" — {ev}" if ev else ""))
    settled = sum(1 for i in (getattr(findings, "items", ()) or ()) if i[1] == "met")
    out.append(f"  ({settled} other requirement(s) it judged met — leave those alone.)")
    return "\n".join(out)


def _probe_ok(p: str) -> bool:
    """Read-only guard for inspector probes (same v16 lint as check probes: quoted
    interpreter bodies are masked, so a python '>' comparison is no longer misread as
    a shell redirect; /tmp scratch writes are allowed)."""
    if not isinstance(p, str) or not p.strip():
        return False
    return not _nonscratch_mutations(p)


def _probe_informative(out: str) -> bool:
    """Did a probe actually return usable evidence? Timeouts, empty output, and
    command-not-found are NON-informative — an inspection built entirely on these has
    verified nothing and (v16) may not emit 'pass' (a metadata-only 'pass' on a task
    whose content it never read is a false accept)."""
    o = (out or "").strip()
    if not o or "timed out" in o[:60] or o.startswith("[channel error"):
        return False                     # v32.1: a dead transport proves nothing
    low = o.lower()
    return not (low.endswith("not found") or "command not found" in low
                or "no such file" in low and len(o) < 80)


def _vision_probe(cfg, vm, path: str, question: str) -> str:
    """v24: give the text-blind inspector EYES for one probe. Fetch an image from the
    machine and ask the vision model, returning its text observation — so VISUAL
    requirements (positioned-at, points-at, sized/colored-as, looks-like) can be
    confirmed or refuted. Returns '(...)' on failure, else the visual reading."""
    try:
        from core.imagery import fetch_look_image           # lazy: avoid import cycle
        data, err = fetch_look_image(vm, path)              # v26: auto-render documents
    except Exception as e:                                  # noqa: BLE001
        return f"(could not read {path}: {e})"
    if not data:
        return f"(could not read {path}: {err})"
    try:
        from core.eyes import ensemble_look                 # lazy: avoid import cycle
        from dataclasses import replace as _dc_replace
        eyes = _verifier_eyes_model(cfg)
        if eyes and eyes != getattr(cfg, "vision_model", ""):
            cfg = _dc_replace(cfg, vision_model=eyes)        # v39.1: inspector's own route
        q = question or ("Describe what is shown and whether it satisfies the stated "
                         "requirement; read any text verbatim.")
        # v30: the inspector's eyes route through the same look-ensemble as the actor's
        # — a single inspector misread can derail a correct run (t063 seed990: one
        # reader called the rain overlay "triangles" and the agent churned on it).
        ans = ensemble_look(cfg, q, [data])
        return (ans or "").strip() or "(vision returned nothing)"
    except LLMTransportError:
        raise                         # provider state is not a visual observation
    except Exception as e:                                  # noqa: BLE001
        return f"(vision probe error: {e})"


# ---- R2a appearance-gate (verifier_appearance_attach) ----------------------------
# Mechanical sight for [APPEARANCE] requirements the inspector tagged itself but is
# not settling: harness renders run-touched deliverables and injects the visual
# reading. General by construction: paths come from provenance (surface_delta in
# ``context``), the instruction (task input), and the inspector's own probe output —
# never from harness-known names. Render machinery (filetype table, command
# builders) lives in core/render.py — pure infra, prompt-free by contract; every
# model-facing string stays HERE, in the audited surface.

_APPEAR_TAG_RE = re.compile(r"^.*\[APPEARANCE\].*$", re.MULTILINE)


def _destination_notes(instruction: str, context: str) -> str:
    """P1: neutral FYI lines for files the TASK TEXT names that the run never touched.
    Pure string-match (named_files is lexical; the ledger is mtime fact) — the
    inspector judges whether an untouched named file is damning (deliverable) or
    expected (read-only input). Capped at 3 lines."""
    from core.render import named_files
    notes = []
    ctx = context or ""
    for base in named_files(instruction):
        if base not in ctx:
            notes.append(f"FYI: the task text mentions '{base}' — this file was NOT "
                         "created or modified by this run.")
        if len(notes) >= 3:
            break
    return ("\n" + "\n".join(notes)) if notes else ""


def _appearance_reqs(history) -> list:
    """The [APPEARANCE]-tagged requirement lines from the inspector's FIRST reply."""
    for m in history:
        if isinstance(m, dict) and m.get("role") == "assistant":
            return [l.strip()[:240] for l in
                    _APPEAR_TAG_RE.findall(str(m.get("content", "")))][:4]
    return []


def _appearance_attach(cfg, vm, instruction, context, history):
    """Render up to appearance_attach_max deliverables and read them through the
    inspector's eyes. Returns (blocks, n_informative)."""
    reqs = _appearance_reqs(history)
    if not reqs:
        return [], 0
    from core.render import harvest_paths, render_cmd
    vm.run_command("mkdir -p /tmp/forge_insp", timeout=10)
    blocks, n = [], 0
    for i, p in enumerate(harvest_paths(instruction, context, history)):
        if n >= getattr(cfg, "appearance_attach_max", 2):
            break
        out_png = f"/tmp/forge_insp/attach_{i}.png"
        cmd = render_cmd(p, out_png)
        if not cmd:
            continue
        vm.run_command(cmd, timeout=90)
        chk = vm.run_command(f"test -s {out_png!r} && echo RENDER_OK", timeout=10)
        if "RENDER_OK" not in (chk or ""):
            continue
        q = ("APPEARANCE requirements under inspection:\n- " + "\n- ".join(reqs) +
             "\nDescribe exactly what is visually present that bears on these; "
             "read any visible text verbatim; note position/color/layout facts.")
        va = _vision_probe(cfg, vm, out_png, q)
        if va and not va.startswith("("):
            n += 1
            blocks.append(f"[HARNESS-ATTACHED VISUAL EVIDENCE — mechanical render "
                          f"of run-touched deliverable {p}]\n{va}"[:1800])
    return blocks, n


_AGENTIC_VERDICT_TOKEN = re.compile(
    r"VERDICT:\s*(PASS|FAIL|UNVERIFIED)", re.IGNORECASE)
_AGENTIC_ROUTE_TOKEN = re.compile(
    r"ROUTE:\s*(HANDOFF|REVISE|EVOLVE)", re.IGNORECASE)
_AGENTIC_STAGE_TOKEN = re.compile(
    r"STAGE:\s*(ORIENTATION_READY)", re.IGNORECASE)


def _report_transport_lines(text: str):
    """Yield a lexical line view without rewriting archived report bytes.

    Some chat transports leave JSON-escaped newlines as the two literal bytes
    ``\\n`` inside a Program's ``code`` string. A report channel is line-oriented,
    so treat only escaped CR/LF spellings as separators for token recognition. Do
    not unescape other content or mutate the report retained as evidence.
    """
    lexical = str(text or "")
    lexical = lexical.replace(r"\r\n", "\n")
    lexical = lexical.replace(r"\n", "\n").replace(r"\r", "\n")
    return lexical.splitlines()


def _parse_agentic_verifier_report(
        text: str, allow_evolve: bool = False,
        allow_unverified: bool = False):
    """Return one internal route for an unambiguous free-form report token.

    The report belongs to the Verifier Agent.  This parser is deliberately only a
    transport parser: it ignores examples inside Markdown fences/indented code and
    requires one unambiguous standalone decision. Repeating the same decision is
    transport-idempotent; conflicting decisions remain invalid. The parser does not
    inspect, summarize, cap, or reinterpret any other byte of the report.
    """
    token = _AGENTIC_ROUTE_TOKEN if allow_evolve else _AGENTIC_VERDICT_TOKEN
    matches = []
    fence = None
    for line in _report_transport_lines(text):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or line.startswith(("\t", "    ")):
            continue
        match = token.fullmatch(stripped)
        if match:
            matches.append(match.group(1).upper())
    if not matches or len(set(matches)) != 1:
        return None
    if allow_evolve:
        return {
            "HANDOFF": "pass",
            "REVISE": "wrong",
            "EVOLVE": "evolve",
        }[matches[0]]
    if matches[0] == "UNVERIFIED":
        return "unverified" if allow_unverified else None
    return "pass" if matches[0] == "PASS" else "wrong"


def _parse_agentic_orientation_report(text: str):
    """Transport-only parser for the candidate-blind orientation token."""
    matches = []
    fence = None
    for line in _report_transport_lines(text):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or line.startswith(("\t", "    ")):
            continue
        match = _AGENTIC_STAGE_TOKEN.fullmatch(stripped)
        if match:
            matches.append(match.group(1).upper())
    return ("orientation_ready"
            if matches and set(matches) == {"ORIENTATION_READY"}
            else None)


def _agentic_substantive_checkpoint(previous: list, raw: list):
    """Keep one Verifier Agent's history through its last real machine action.

    ``run_attempt`` appends complete user/assistant pairs. A Program or Look is a
    substantive action; narration that merely promises a future action is not. The
    environment observation produced by the last action is the following user
    message, so return its complete text and durable image attachments separately
    for lossless delivery in the recovery prompt.

    The complete ``raw`` history remains archived by the segment's ArtifactSink.
    This helper only chooses what is allowed back into the live semantic context.
    """
    base = list(previous or [])
    full = list(raw or [])
    if len(base) % 2 or len(full) % 2:
        raise ValueError("Verifier Agent histories must contain complete pairs")
    if full[:len(base)] != base:
        raise ValueError("Verifier Agent segment did not preserve its input history")

    last_end = None
    last_turn = None
    for assistant_index in range(len(base) + 1, len(full), 2):
        message = full[assistant_index]
        if message.get("role") != "assistant":
            continue
        turn = parse_turn(str(message.get("content", "")))
        if isinstance(turn, (Program, Look)):
            last_end = assistant_index + 1
            last_turn = turn

    if last_end is None:
        return base, "", [], None
    observation = ""
    observation_images = []
    if last_end < len(full) and full[last_end].get("role") == "user":
        observation = str(full[last_end].get("content", ""))
        observation_images = durable_images(full[last_end])
    # Stop exactly after the valid action.  Keeping the observation requires a
    # following assistant pair; if that reply is actionless, retaining the pair
    # teaches the recovered decoder to repeat the malformed narration.  The caller
    # instead reattaches the exact observation to the next recovery user turn.
    return full[:last_end], observation, observation_images, last_turn


def _agentic_semantic_replay(raw: list):
    """Re-encode all real actions and their results without actionless turns.

    This is an event-triggered decoder recovery, not context-length compaction: no
    Program, Look, program trace, textual look result, or valid action reasoning is
    truncated or summarized. Repetitive narration/nudges that never caused a machine
    action remain in the archived raw transcripts but are removed from live context.
    """
    full = list(raw or [])
    if len(full) % 2:
        raise ValueError("Verifier Agent history must contain complete pairs")

    records = []
    for assistant_index in range(1, len(full), 2):
        assistant = full[assistant_index]
        if assistant.get("role") != "assistant":
            continue
        turn = parse_turn(str(assistant.get("content", "")))
        user = full[assistant_index - 1]
        # Only a real machine action is semantic Verifier history.  A user turn may
        # carry valuable Look pixels, but an actionless assistant reply must never be
        # retained merely to satisfy user/assistant pairing; the pixels travel as the
        # pending observation after the preceding Look instead.
        if not isinstance(turn, (Program, Look)):
            continue
        observation = None
        if (assistant_index + 1 < len(full)
                and full[assistant_index + 1].get("role") == "user"):
            observation = dict(full[assistant_index + 1])
        records.append((dict(user), dict(assistant), observation,
                        turn if isinstance(turn, (Program, Look)) else None))

    if not records:
        return [], "", [], None

    replay = []
    previous_observation = None
    last_turn = None
    for index, (original_user, assistant, observation, turn) in enumerate(records):
        original_text = str(original_user.get("content", ""))
        previous_text = (str(previous_observation.get("content", ""))
                         if previous_observation else "")
        if index == 0:
            transport_text = original_text
        else:
            parts = []
            if previous_text:
                parts.append(
                    "LOSSLESS RESULT OF YOUR PREVIOUS SUBSTANTIVE ACTION:\n---\n"
                    + previous_text + "\n---")
            if original_text and original_text != previous_text:
                parts.append(
                    "ORIGINAL CONTEXT THAT PRECEDED YOUR NEXT RECORDED ACTION:\n---\n"
                    + original_text + "\n---")
            transport_text = "\n\n".join(parts) or original_text
        transport_user = {"role": "user", "content": transport_text}
        attachments = []
        if previous_observation:
            attachments.extend(durable_images(previous_observation))
        attachments.extend(durable_images(original_user))
        transport_user = with_durable_images(transport_user, attachments)
        replay += [transport_user, assistant]
        previous_observation = observation
        if turn is not None:
            last_turn = turn

    pending = (str(previous_observation.get("content", ""))
               if previous_observation else "")
    pending_images = (durable_images(previous_observation)
                      if previous_observation else [])
    return replay, pending, pending_images, last_turn


def _agentic_image_bytes(attachments: list) -> list[bytes]:
    """Decode and integrity-check exact archived Look pixels for reattachment."""
    out = []
    for item in list(attachments or []):
        try:
            data = base64.b64decode(str(item["data"]), validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed durable Verifier image attachment") from exc
        expected = str(item.get("sha256", ""))
        actual = hashlib.sha256(data).hexdigest()
        if expected and expected != actual:
            raise ValueError("durable Verifier image attachment failed integrity check")
        out.append(data)
    return out


def _agentic_actionless_fingerprints(history: list, start: int) -> set[str]:
    """Normalized identities of malformed/actionless assistant replies.

    These are transport diagnostics only. They neither grade the candidate nor
    impose a retry count; recurrence selects a stronger format-recovery message.
    """
    fingerprints = set()
    for assistant_index in range(start + 1, len(history or []), 2):
        message = history[assistant_index]
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content", ""))
        if parse_turn(content) is not None:
            continue
        normalized = " ".join(content.lower().split()) or "(empty reply)"
        fingerprints.add(hashlib.sha256(normalized.encode(
            "utf-8", "replace")).hexdigest())
    return fingerprints


def _agentic_recovery_prompt(report_channel: str, reason: str,
                              observation: str = "", last_turn=None,
                              observation_images=(),
                              recurrent: bool = False,
                              allow_evolve: bool = False,
                              allow_unverified: bool = False,
                              stage: str = "candidate_verification") -> str:
    """Transport-only continuation after a segment failed to publish a report."""
    recurrence = (
        "A normalized actionless response has recurred. Those raw responses remain "
        "archived for audit but were not fed back into your working context. "
        if recurrent else
        "The actionless tail remains archived for audit but was not fed back into "
        "your working context. ")
    evidence = ""
    if observation:
        evidence = (
            "\n\nLOSSLESS RESULT OF YOUR LAST SUBSTANTIVE ACTION:\n---\n"
            + observation + "\n---")
    if isinstance(last_turn, Look):
        if observation_images:
            evidence += (
                "\n\nThe exact pixels from that successfully delivered Look are "
                "reattached to this recovery turn. If you need a newly captured "
                "state, reissue the Look before deciding.")
        else:
            evidence += (
                "\n\nNo pixel attachment accompanied that Look result. If pixels "
                "are needed, reissue the Look before deciding.")
    if stage == "orientation":
        judgment = "candidate or routing"
        publication = (
            "your private orientation handoff with STAGE: ORIENTATION_READY")
    else:
        judgment = ("HANDOFF, REVISE, EVOLVE, or routing" if allow_evolve
                    else ("PASS, FAIL, UNVERIFIED, or correctness"
                          if allow_unverified else
                          "PASS, FAIL, or correctness"))
        publication = ("one HANDOFF / REVISE / EVOLVE route" if allow_evolve
                       else ("one PASS / FAIL / UNVERIFIED report"
                             if allow_unverified else "the complete report"))
    return (
        "VERIFIER AGENT — SAME-CONTEXT TRANSPORT RECOVERY.\n\n"
        + recurrence
        + f"No {judgment} judgment was inferred from the transport "
          "failure, and your authority over the investigation is unchanged. "
          f"The prior segment did not publish a usable report through "
          f"{report_channel}: "
          f"{reason}. Continue from your last substantive checkpoint. Take your "
          "next real step with one ordinary Program or Look JSON action. When your "
          "decision is ready, use a verifier-report Program action to publish the "
          f"complete report with {publication} to the host harness; do not merely "
          "announce that you "
          "will do so."
        + evidence)


def verify_agentic(instruction: str, vm, cfg, sink=None, turn_no: int = 0,
                    context: str = "", session: list = None,
                    wall_budget: float = None,
                    stage: str = "candidate_verification",
                    continue_candidate: bool = False,
                    curriculum_review: str = "",
                    actor_evidence_leads: str = "",
                    iters_budget: int | None = None):
    """Run one persistent, effect-isolated Verifier Agent inspection.

    The Verifier keeps the ordinary persistent Agent conversation, arbitrary
    code-as-policy, and Look channel. Its Program executor makes the candidate
    filesystem read-only and isolates process/network/GUI effects while retaining a
    private writable scratch filesystem. Numeric limits in the selected config and
    ``wall_budget`` are emergency watchdogs, not stopping instructions.
    """
    from config.settings import load
    from core.loop import run_attempt
    from core.trace import ArtifactSink
    from core.verifier_runtime import (
        AGENTIC_VERIFIER_NUDGE, AGENTIC_VERIFIER_PREMATURE_DONE,
        AGENTIC_VERIFIER_ROUTE_NUDGE,
        AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE,
        AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE,
        AGENTIC_VERIFIER_ROUTE_SYSTEM,
        AGENTIC_VERIFIER_ORIENTATION_NUDGE,
        AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE,
        AGENTIC_VERIFIER_ORIENTATION_SYSTEM,
        AGENTIC_VERIFIER_STRICT_NUDGE, AGENTIC_VERIFIER_SYSTEM,
        AgenticVerifierExecutor, AgenticVerifierInfrastructureError,
        AgenticVerifierNoProgressError,
        agentic_verifier_system_for, agentic_verifier_user_message,
        validate_verifier_look_path)
    from explore.charter import (
        agent_harness_verifier_charter,
        agent_harness_verifier_orientation_charter,
    )

    if stage not in {"orientation", "candidate_verification"}:
        raise ValueError(f"unsupported Verifier Agent stage: {stage!r}")
    if stage == "orientation" and (continue_candidate or curriculum_review):
        raise ValueError("orientation cannot continue a candidate review")
    if curriculum_review and not continue_candidate:
        raise ValueError(
            "a Curriculum review may only continue the unchanged candidate")

    verifier_cfg = load(getattr(cfg, "agentic_verifier_config", ""))
    # Generic handoff transport, matching self-evolution's Agent phases.  These
    # switches do not choose how to investigate or when the evidence is sufficient.
    verifier_cfg.practice_mode = True
    verifier_cfg.independent_verify = False
    verifier_cfg.agent_decided_stop = True
    verifier_cfg.max_resumes = 0
    # ``verifier_evolve_route`` keeps the unified harness's fail-closed terminal
    # transport enabled.  In the separated design, however, local correctness and
    # high-level search are different authorities: this Agent says only PASS/FAIL
    # and a persistent Curriculum Agent routes each FAIL afterward.
    allow_evolve = (
        bool(getattr(cfg, "verifier_evolve_route", False))
        and not bool(getattr(cfg, "verifier_local_verdict_only", False)))
    orienting = stage == "orientation"
    allow_unverified = (
        bool(getattr(cfg, "verifier_unverified_evidence", False))
        and not allow_evolve and not orienting)
    hide_actor_memory = bool(getattr(
        cfg, "verifier_hide_actor_memory", False))
    private_paths = tuple(getattr(cfg, "verifier_private_paths", ()) or ())
    execution_mode = str(getattr(
        cfg, "verifier_execution_mode", "effect_isolated")
        or "effect_isolated")
    terminal_token = ("stage token" if orienting else
                      ("route" if allow_evolve else "verdict"))

    persistent = session if session is not None else VerifierSession()
    stage_lifecycle = bool(getattr(cfg, "verifier_stage_lifecycle", False))
    if stage_lifecycle and not isinstance(persistent, VerifierSession):
        raise TypeError(
            "the staged Verifier lifecycle requires one VerifierSession")
    if isinstance(persistent, VerifierSession):
        persistent.inspections += 1
        inspection_no = persistent.inspections
        persistent.stage = stage.upper()
        if orienting:
            persistent.orientation_runs += 1
        else:
            if continue_candidate:
                if persistent.candidate_generation < 1:
                    raise ValueError(
                        "cannot continue a candidate before its first inspection")
            else:
                persistent.candidate_generation += 1
        candidate_generation = max(1, persistent.candidate_generation)
        persistent.emit(
            "VERIFIER_STAGE_STARTED", stage=persistent.stage,
            inspection=inspection_no,
            candidate_generation=(None if orienting
                                  else candidate_generation))
    else:
        inspection_no = 1
        candidate_generation = inspection_no
    current_history = list(persistent)
    report_channel = (f"verifier-report channel for turn {turn_no}, "
                      f"inspection {inspection_no}")
    verifier_cfg.practice_done_requires = ""
    if orienting:
        inspection_prompt = agent_harness_verifier_orientation_charter(
            instruction, report_channel, mechanical_context=context,
            execution_mode=execution_mode)
    else:
        inspection_prompt = agent_harness_verifier_charter(
            instruction, report_channel, mechanical_context=context,
            allow_evolve=allow_evolve,
            candidate_generation=candidate_generation,
            curriculum_review=curriculum_review,
            allow_unverified=allow_unverified,
            execution_mode=execution_mode,
            actor_evidence_leads=actor_evidence_leads)
    prompt = inspection_prompt
    persist_scratch = bool(getattr(cfg, "verifier_persist_scratch", False))
    if persist_scratch and isinstance(persistent, VerifierSession):
        program_executor = persistent.acquire_executor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=private_paths,
            execution_mode=execution_mode)
        owns_executor = False
    else:
        program_executor = AgenticVerifierExecutor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=private_paths,
            execution_mode=execution_mode)
        owns_executor = True
    program_executor.clear_published_report()
    if orienting:
        program_executor.set_report_validator(
            _parse_agentic_orientation_report,
            "exactly one unambiguous standalone STAGE: ORIENTATION_READY line",
        )
    elif allow_evolve:
        program_executor.set_report_validator(
            lambda report: _parse_agentic_verifier_report(
                report, allow_evolve=True),
            "one unambiguous standalone ROUTE: HANDOFF, ROUTE: REVISE, or "
            "ROUTE: EVOLVE line",
        )
    else:
        program_executor.set_report_validator(
            lambda report: _parse_agentic_verifier_report(
                report, allow_unverified=allow_unverified),
            ("one unambiguous standalone VERDICT: PASS, VERDICT: FAIL, or "
             "VERDICT: UNVERIFIED line" if allow_unverified else
             "one unambiguous standalone VERDICT: PASS or VERDICT: FAIL line"),
        )
    verifier_system = agentic_verifier_system_for(
        execution_mode, orienting=orienting, allow_evolve=allow_evolve,
        allow_unverified=allow_unverified)
    if hide_actor_memory or private_paths:
        verifier_system += (
            "\nHarness-owned Actor-private paths are mechanically hidden from your "
            "Programs and file Looks. Verify authoritative inputs and the candidate "
            "surface; private policy/scratch contents are not correctness evidence.\n")
    if orienting:
        action_nudge = AGENTIC_VERIFIER_ORIENTATION_NUDGE
        strict_action_nudge = AGENTIC_VERIFIER_ORIENTATION_NUDGE
        premature_done_nudge = \
            AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE
    else:
        action_nudge = (AGENTIC_VERIFIER_ROUTE_NUDGE if allow_evolve
                        else AGENTIC_VERIFIER_NUDGE)
        strict_action_nudge = (
            AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE if allow_evolve
            else AGENTIC_VERIFIER_STRICT_NUDGE)
        premature_done_nudge = (
            AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE if allow_evolve
            else AGENTIC_VERIFIER_PREMATURE_DONE)

    configured_wall = float(getattr(verifier_cfg, "wall_clock_secs", 86400) or 86400)
    available_wall = configured_wall if wall_budget is None else min(
        configured_wall, max(0.0, float(wall_budget)))
    deadline = time.time() + available_wall
    continuing = bool(current_history)
    temp_dir = None
    if sink is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="forge-agentic-verifier-")
        verifier_root = temp_dir.name
    else:
        verifier_root = os.path.join(
            sink.root, "verifier_agent", f"inspection_{inspection_no:03d}")

    # A crashed process may be resumed into the same sink. Never overwrite its raw
    # segment archive; append after the greatest persisted segment number.
    segment = 0
    if os.path.isdir(verifier_root):
        persisted = []
        for name in os.listdir(verifier_root):
            match = re.fullmatch(r"segment_(\d+)", name)
            if match:
                persisted.append(int(match.group(1)))
        if persisted:
            segment = max(persisted) + 1

    last_reason = "Verifier Agent did not publish a valid report"
    infrastructure_failure = False
    progress_blocked = False
    pending_observation = str(getattr(
        persistent, "pending_observation", "") or "")
    pending_images = list(getattr(persistent, "pending_images", []) or [])
    pending_turn = getattr(persistent, "pending_turn", None)
    semantic_replay_active = bool(getattr(
        persistent, "transport_recurrence", False))
    if semantic_replay_active and current_history:
        replayed, replay_observation, replay_images, replay_turn = \
            _agentic_semantic_replay(current_history)
        if replayed:
            current_history = replayed
            pending_observation = replay_observation or pending_observation
            pending_images = replay_images or pending_images
            pending_turn = replay_turn or pending_turn
            persistent[:] = current_history
    # ``inspection_prompt`` is the immutable authority envelope for this candidate:
    # it carries the verbatim task, mechanical candidate context, and route charter.
    # It is not part of ``current_history`` until this inspection produces its first
    # substantive Program/Look.  A dry first segment must therefore re-present it in
    # full; otherwise a transport recovery starts with only "continue" and forces the
    # Verifier to guess the task from the filesystem.
    inspection_anchored = False
    if current_history and (pending_observation or pending_images
                            or pending_turn is not None):
        recovery_prompt = _agentic_recovery_prompt(
            report_channel,
            "the prior process stopped after your last substantive action",
            observation=pending_observation, last_turn=pending_turn,
            observation_images=pending_images,
            allow_evolve=allow_evolve,
            allow_unverified=allow_unverified, stage=stage)
        prompt = inspection_prompt + "\n\n" + recovery_prompt
    seen_actionless = set()
    actionless_segments = 0
    remaining_iters = iters_budget
    try:
        while True:
            if remaining_iters is not None and remaining_iters <= 0:
                last_reason = "Verifier Agent reached its remaining iteration ceiling"
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                last_reason = "Verifier Agent reached the run's emergency wall ceiling"
                break
            segment_root = os.path.join(verifier_root, f"segment_{segment:03d}")
            segment_sink = ArtifactSink(segment_root)
            published = {}
            segment_base = list(current_history)

            def terminal_report_ready():
                report = program_executor.published_report
                verdict = (_parse_agentic_orientation_report(report)
                           if orienting else
                           _parse_agentic_verifier_report(
                               report, allow_evolve=allow_evolve,
                               allow_unverified=allow_unverified))
                if verdict is None:
                    return False
                published.update(report=report, verdict=verdict)
                return True

            result, raw_history = run_attempt(
                prompt, vm, verifier_cfg, segment_sink,
                wall_budget=remaining,
                **({"iters_budget": remaining_iters} if remaining_iters is not None else {}),
                initial_history=current_history,
                continue_context=continuing,
                terminal_handoff_ready=terminal_report_ready,
                program_executor=program_executor,
                look_path_validator=lambda path: validate_verifier_look_path(
                    path, hide_actor_memory=hide_actor_memory,
                    private_paths=private_paths),
                look_fetcher=program_executor.fetch_look_image,
                system_prompt=verifier_system,
                action_nudge=action_nudge,
                strict_action_nudge=strict_action_nudge,
                premature_done_nudge=premature_done_nudge,
                instruction_is_complete_opening=True,
                user_message_transform=lambda message: agentic_verifier_user_message(
                    message, allow_evolve=allow_evolve,
                    stage=("orientation" if orienting else
                           "candidate_verification"),
                    execution_mode=execution_mode,
                    allow_unverified=allow_unverified),
                opening_image=(_agentic_image_bytes(pending_images)
                               if pending_images else None))
            if remaining_iters is not None:
                remaining_iters -= max(1, int(getattr(result, "turns", 0)
                                              or getattr(result, "iters", 0)))
            # run_attempt excludes recoverable provider/API downtime from its active
            # work clock. Keep this outer inspection deadline on the same clock so a
            # 402/429 outage cannot silently exhaust Verifier authority.
            pause_secs = float(
                getattr(result, "infra_pause_secs", 0.0) or 0.0)
            pause_events = int(getattr(result, "infra_pauses", 0) or 0)
            deadline += pause_secs
            if isinstance(persistent, VerifierSession):
                persistent.infra_pause_secs += pause_secs
                persistent.infra_pauses += pause_events
            current_history, new_observation, new_images, new_turn = \
                _agentic_substantive_checkpoint(segment_base, raw_history)
            advanced = new_turn is not None
            actionless_segments = 0 if advanced else actionless_segments + 1
            if advanced:
                # The checkpoint now contains the user turn that carried the full
                # inspection envelope immediately before this action.  Subsequent
                # recoveries can continue from history without duplicating it.
                inspection_anchored = True
                pending_observation = new_observation
                pending_images = new_images
                pending_turn = new_turn
            actionless = _agentic_actionless_fingerprints(
                raw_history, len(segment_base))
            recurrent = bool(actionless & seen_actionless)
            seen_actionless.update(actionless)
            semantic_replay_activated = False
            if recurrent and not advanced and not semantic_replay_active:
                replayed, replay_observation, replay_images, replay_turn = \
                    _agentic_semantic_replay(current_history)
                if replayed and len(replayed) < len(current_history):
                    current_history = replayed
                    pending_observation = (
                        replay_observation or pending_observation)
                    pending_images = replay_images or pending_images
                    pending_turn = replay_turn or pending_turn
                    semantic_replay_active = True
                    semantic_replay_activated = True
                    log.warning(
                        "recurring actionless verifier transport -> semantic replay "
                        "(%d -> %d pairs; all actions/results preserved)",
                        len(segment_base) // 2, len(current_history) // 2)
            if isinstance(persistent, VerifierSession):
                persistent.pending_observation = pending_observation
                persistent.pending_images = list(pending_images)
                persistent.pending_turn = pending_turn
                persistent.transport_recurrence = semantic_replay_active
            persistent[:] = current_history

            # The raw transcript already contains every discarded byte. This compact
            # sidecar makes the recovery boundary directly auditable without copying
            # or truncating any conversation content.
            recovery = {
                "status": str(getattr(result, "status", "infra")),
                "history_pairs_before": len(segment_base) // 2,
                "raw_history_pairs": len(raw_history) // 2,
                "checkpoint_history_pairs": len(current_history) // 2,
                "checkpoint_advanced": advanced,
                "last_substantive_action": (
                    type(new_turn).__name__ if new_turn is not None else None),
                "pending_observation_chars": len(pending_observation),
                "pending_observation_images": len(pending_images),
                "discarded_tail_pairs": (
                    len(raw_history) - len(current_history)) // 2,
                "normalized_actionless_recurrence": recurrent,
                "semantic_replay_activated": semantic_replay_activated,
                "semantic_replay_active": semantic_replay_active,
                "actionless_segments": actionless_segments,
                "max_actionless_segments": _MAX_ACTIONLESS_VERIFIER_SEGMENTS,
                "raw_transcript": "transcript.json",
            }
            segment_sink.save_recovery(recovery)

            report = published.get("report", "")
            verdict = published.get("verdict")
            if verdict is None:
                report = program_executor.published_report
                read_reason = ("report was not published"
                               if not report else
                               f"report lacks one unambiguous standalone "
                               f"{terminal_token} token")
                verdict = (_parse_agentic_orientation_report(report)
                           if orienting else
                           _parse_agentic_verifier_report(
                               report, allow_evolve=allow_evolve,
                               allow_unverified=allow_unverified))
            else:
                read_reason = ""
            if verdict is not None:
                if orienting:
                    persistent.pending_observation = ""
                    persistent.pending_images = []
                    persistent.pending_turn = None
                    persistent.stage = "ORIENTATION_READY"
                    persistent.emit(
                        "VERIFIER_STAGE_COMPLETED",
                        stage="ORIENTATION",
                        inspection=inspection_no,
                        candidate_generation=None)
                    findings = Findings(report)
                    findings.doubts = ""
                    findings.items = ()
                    if sink is not None:
                        sink.save_verify2(
                            turn_no, "orientation_ready", findings,
                            list(persistent))
                    return "orientation_ready", findings

                findings = Findings(report)
                findings.doubts = ""
                findings.items = ()
                if isinstance(persistent, VerifierSession):
                    persistent.pending_observation = ""
                    persistent.pending_images = []
                    persistent.pending_turn = None
                    persistent.stage = (
                        "ROUTE_COMMITTED" if allow_evolve
                        else ("EVIDENCE_REQUESTED"
                              if verdict == "unverified"
                              else "VERDICT_COMMITTED"))
                    completion = {
                        "stage": "CANDIDATE_VERIFICATION",
                        "inspection": inspection_no,
                        "candidate_generation": candidate_generation,
                    }
                    if allow_evolve:
                        completion["route"] = {
                            "pass": "HANDOFF", "wrong": "REVISE",
                            "evolve": "EVOLVE"}.get(verdict, verdict)
                    else:
                        completion["verdict"] = {
                            "pass": "PASS", "wrong": "FAIL",
                            "unverified": "UNVERIFIED",
                        }.get(verdict, verdict)
                    persistent.emit("VERIFIER_STAGE_COMPLETED", **completion)
                if sink is not None:
                    sink.save_verify2(turn_no, verdict, findings,
                                      list(persistent))
                return verdict, findings

            status = str(getattr(result, "status", "infra"))
            if status == "done":
                last_reason = (read_reason or
                               f"report lacks one unambiguous standalone "
                               f"{terminal_token} token")
            elif status in {"stalled", "stalled_quiescent", "budget"}:
                last_reason = "Verifier Agent ended a segment before publishing its decision"
            else:
                last_reason = (f"Verifier Agent ended at emergency/infrastructure "
                               f"status {status}")
                if status == "infra":
                    infrastructure_failure = True
                break
            if actionless_segments >= _MAX_ACTIONLESS_VERIFIER_SEGMENTS:
                last_reason = (
                    "Verifier Agent produced no Program or Look actions in "
                    f"{actionless_segments} consecutive segments; runtime recovery "
                    "is required before verification can continue")
                infrastructure_failure = True
                progress_blocked = True
                recovery.update(
                    status="infra", segment_status=status,
                    stop_reason="no_substantive_progress", reason=last_reason)
                segment_sink.save_recovery(recovery)
                log.error(last_reason)
                break
            recovery_prompt = _agentic_recovery_prompt(
                report_channel, last_reason,
                observation=pending_observation,
                last_turn=pending_turn,
                observation_images=pending_images, recurrent=recurrent,
                allow_evolve=allow_evolve,
                allow_unverified=allow_unverified,
                stage=stage)
            prompt = (recovery_prompt if inspection_anchored else
                      inspection_prompt + "\n\n" + recovery_prompt)
            continuing = True
            segment += 1
    finally:
        if owns_executor:
            program_executor.close()
        if temp_dir is not None:
            temp_dir.cleanup()

    if progress_blocked:
        raise AgenticVerifierNoProgressError(last_reason)
    if infrastructure_failure:
        raise AgenticVerifierInfrastructureError(last_reason)

    findings = Findings(last_reason)
    findings.doubts = last_reason
    findings.items = ()
    if sink is not None:
        sink.save_verify2(turn_no, "unverified", findings, list(persistent))
    return "unverified", findings


def verify_independent(instruction: str, vm, cfg, sink=None, turn_no: int = 0,
                       context: str = "", session: list = None,
                       wall_budget: float = None,
                       continue_candidate: bool = False,
                       curriculum_review: str = "",
                       actor_evidence_leads: str = ""):
    """Run the independent inspection. Returns (verdict, findings) where verdict is
    "pass" | "wrong" | "unverified". The opt-in unified Agent route may also
    return "evolve"; ordinary benchmark configurations cannot produce it.

    Three-state by design: "wrong" requires concrete violating evidence (must-fix);
    "unverified" means could-not-confirm — NOT the same as wrong (a shallow inspection
    must not veto deep work it cannot re-derive). Format nudges do not consume probe
    rounds. ``context`` (v17) is mechanical file-change evidence appended to the task —
    it never reveals the actor's reasoning, so independence is preserved. The full
    inspector conversation is persisted via ``sink`` for autopsy.

    ``session`` (E4-A1, wenyi's design): with cfg.verifier_continuity, the caller
    passes ONE :class:`VerifierSession` per run. The verifier's exchanges and its
    prior probes/verdicts accumulate as its own history (like the Actor's), so a
    WRONG issued at inspection N is remembered at inspection N+1. Mechanical
    evidence freshness does NOT accumulate: every candidate inspection must read
    the current artifact before PASS, while cumulative counters remain telemetry.
    Actor-conversation blindness is untouched: the session holds only the
    Verifier's own work. A legacy list still preserves conversation history only.
    Flag off or session None -> fresh context, byte-identical to E3."""
    if getattr(cfg, "agentic_verifier_config", ""):
        return verify_agentic(
            instruction, vm, cfg, sink=sink, turn_no=turn_no,
            context=context, session=session, wall_budget=wall_budget,
            continue_candidate=continue_candidate,
            curriculum_review=curriculum_review,
            actor_evidence_leads=actor_evidence_leads)

    model = cfg.verifier_model or cfg.model
    vsys = build_verifier_system(cfg)                    # v35: config-true mechanics
    continuity = session is not None and getattr(cfg, "verifier_continuity", False)
    history: list = session if continuity else []
    evidence_session = (session
                        if continuity and isinstance(session, VerifierSession)
                        else None)
    if evidence_session is not None:
        evidence_session.inspections += 1
    if getattr(cfg, "verifier_destination_note", False):
        context = (context or "") + _destination_notes(instruction, context)
    elastic = getattr(cfg, "verifier_elastic_depth", False)
    # E4-A1b: the counter becomes a runaway backstop (a model that never emits a
    # verdict must not spin forever); 200 is ~33x the old cap and no inspection
    # has ever exceeded 7 rounds. The wall clock still bounds the run itself.
    budget_rounds = 200 if elastic else cfg.verifier_probes
    nudge_cap = 20 if elastic else 6
    user = _opening(instruction, cfg.verifier_probes, context,
                    tag_appearance=getattr(cfg, "verifier_appearance_attach", False),
                    elastic=elastic)
    if continuity and history:
        # E4-A1 bridge: this is a RE-inspection of the same run — the verifier's
        # own earlier work is in its history. Prior WRONGs stand until NEW
        # evidence resolves them; do not re-derive what is already established.
        user += ("\n\nCONTINUING INSPECTION: you have inspected this run before — "
                 "your prior probes, findings and verdicts are in your conversation "
                 "above. Weigh them: a WRONG you issued stands until NEW evidence "
                 "resolves it; re-check the previously-failing points first. Prior "
                 "evidence guides you but may describe an earlier candidate: read "
                 "the current deliverable during this inspection before PASS.")
    verdict = "unverified"
    findings = ("independent inspection reached no verdict within its probe budget — "
                "completion could not be confirmed or refuted")
    concluded = False
    informative = 0
    content_seen = 0
    # v16/v20: freshness is local to THIS candidate inspection. The persistent
    # Verifier remembers prior evidence semantically, but an older read cannot clear
    # the content guard for a candidate the Actor may have revised.
    rounds_used, nudges = 0, 0        # v11: format nudges no longer consume the call
    canary_refunds = 0                # v32.2: outage-refunded rounds (capped at 2)
    #                                   budget that bounds probe rounds — a format-taxed
    #                                   model must not get LESS evidence (they are
    #                                   separately bounded to prevent loops)
    vis_on = getattr(cfg, "vision_verify", True) and bool(_verifier_eyes_model(cfg))  # v24+v39.1
    looked = steered = False          # v27: has the inspector used its eyes / been nudged
    attached = False                  # R2a: harness-attached visual evidence delivered
    while rounds_used < budget_rounds and nudges < nudge_cap:
        out = chat(model, vsys, user, max_tokens=cfg.verifier_max_tokens,
                   temperature=cfg.temperature, top_p=getattr(cfg, "top_p", -1.0),
                   reasoning_effort=cfg.reasoning_effort,
                   history=history,
                   reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0))
        amsg = {"role": "assistant", "content": out or "(empty reply)"}
        if continuity and getattr(cfg, "reasoning_in_history", False):
            from llm.client import pop_last_reasoning   # E4-A1 (wenyi): the
            _rsn = pop_last_reasoning()                  # verifier's session runs
            if _rsn:                                     # under RIH like the actor's
                amsg["reasoning"] = _rsn
        history += [{"role": "user", "content": user}, amsg]
        obj = parse_object(out) or {}
        v = obj.get("verdict")
        if v == "fail":                                   # legacy phrasing tolerance
            v = "wrong"
        if v in ("pass", "wrong", "unverified"):
            candidate_findings = _mk_findings(
                str(obj.get("findings", ""))[:2000], obj.get("doubts", ""),
                obj.get("items"))
            if (v == "pass" and candidate_findings.doubts.strip()
                    and getattr(cfg, "doubts_block_pass", False)):
                # A free-text field is not a reliable task classifier. Let the same
                # Verifier Agent resolve whether its caveat is actually task-blocking
                # instead of mechanically bouncing a correct candidate to the Actor.
                findings = candidate_findings
                nudges += 1
                user = _PASS_DOUBT_CLARIFICATION
                log.info("verifier pass carried doubts -> same-agent clarification")
                continue
            _blind = (content_seen == 0 if getattr(cfg, "verifier_content_guard", True)
                      else informative == 0)   # A/B: Phase-0 guard (content) vs pre-P0
            if v == "pass" and _blind:
                # Candidate-local Phase 0: preserve the same Verifier Agent and let it
                # gather fresh evidence instead of bouncing a good candidate merely
                # because its first answer leaned on an older inspection.
                findings = candidate_findings
                nudges += 1
                user = _CURRENT_CANDIDATE_EVIDENCE
                log.info("verifier pass with 0 current-candidate content probes -> "
                         "same-agent reinspection")
                continue
            verdict = v
            findings = candidate_findings
            concluded = True
            break
        probes = [p for p in (obj.get("probes") or []) if _probe_ok(p)][:4]
        look = obj.get("look") if vis_on else None        # v24 (vis_on hoisted above)
        look = look if (isinstance(look, dict) and look.get("path")) else None
        if not probes and not look:                       # format nudge — no round spent
            nudges += 1
            user = ('Reply with ONE JSON object: {"probes": [...]} (read-only; escape '
                    'newlines as \\n inside the JSON strings, or join commands with '
                    "';')"
                    + (' or {"look": {"path": "...", "question": "..."}}' if vis_on else '')
                    + ' or {"verdict": "pass"|"wrong"|"unverified", '
                    '"findings": "...", "doubts": "..."}.')
            continue
        rounds_used += 1
        outs = []
        round_informative = 0
        for p in probes:
            po = vm.run_command(p, timeout=cfg.verifier_probe_timeout)
            if _probe_informative(po):
                informative += 1
                round_informative += 1
                if _category(p) == "content":     # v20 Phase-0: did it read content?
                    content_seen += 1
            outs.append(f"$ {p}\n{po}"[:1200])
        if probes and round_informative == 0 and canary_refunds < 2:
            # v32.2 CANARY: a whole round of nothing has two very different causes —
            # "the machine has nothing there" (evidence) vs "the channel is deaf"
            # (outage). The guest server can be alive-but-broken (HTTP 200, empty
            # body for EVERYTHING — t003 s994's final inspection: listing,
            # metadata, PIL and look probes all 'returned no results'; no
            # exception, so the v32.1 marker never fired). Ask the machine to say a word; silence = outage, and nothing
            # in this round is evidence. The round is refunded (capped, so a
            # permanently dead channel cannot loop forever).
            cv = vm.run_command("echo __forge_canary__", timeout=10)
            if "__forge_canary__" not in (cv or ""):
                canary_refunds += 1
                rounds_used -= 1
                outs.append("[CHANNEL OUTAGE detected: even a trivial `echo` "
                            "returned nothing — the machine's command channel is "
                            "not answering. NOTHING in this round is evidence "
                            "about the machine (round not counted). Re-run these "
                            "probes.]")
                log.warning("verifier canary silent — round refunded (%d/2)",
                            canary_refunds)
            else:
                outs.append("[channel verified alive by canary: the empty outputs "
                            "above are REAL — those commands genuinely printed "
                            "nothing]")
        if look:                                          # v24: a vision probe (eyes on the deliverable)
            looked = True                                 # v25: inspector used its eyes
            va = _vision_probe(cfg, vm, look["path"], look.get("question", ""))
            if not va.startswith("("):                    # a real visual reading, not an error
                informative += 1
                content_seen += 1                         # a visual READ of the deliverable's content
            outs.append(f"[VISION look {look['path']}]\n{va}"[:1500])
        if (getattr(cfg, "verifier_appearance_attach", False) and vis_on
                and not looked and not attached and rounds_used >= 2):
            # R2a appearance-gate: the model tagged [APPEARANCE] requirements itself
            # and two rounds haven't produced a look — stop inviting, deliver the
            # evidence. Counters mirror the look branch (a visual READ of content).
            blocks, n_att = _appearance_attach(cfg, vm, instruction, context, history)
            if blocks:
                attached = steered = True                  # reminder superseded
                informative += n_att
                content_seen += n_att
                outs.extend(blocks)
                log.info("verifier round %d: appearance-gate attached %d render(s)",
                         rounds_used, n_att)
        steer = ""                                        # v27: JIT eyes-reminder on STALL
        if vis_on and not looked and not steered and (
                (rounds_used >= 2 and content_seen == 0)   # text-blind after 2 rounds
                or rounds_used >= 3):                      # v32.1: OR simply never looked
            # v27 fired only when TEXT probing was failing — so an inspection whose
            # text probes succeeded never got reminded it has eyes, and passed on
            # structural proxies while the decisive requirement was visual (t003:
            # filter identified as rain from filenames/metadata, never viewed; graded
            # 0). Both triggers are model-intrinsic behavior (probe outcomes + look
            # usage), never instruction classification; the reminder itself stays
            # conditional ("IF the requirement is about how something LOOKS...").
            steer, steered = "\n\n" + _VISUAL_STEER, True
            log.info("verifier round %d: JIT eyes-reminder injected (%s)", rounds_used,
                     "text-blind" if content_seen == 0 else "never-looked")
        tail = ("\n\nYou decide when the evidence is enough: more probes, a look, "
                "or the verdict object — there is no round budget and no time "
                "pressure." if elastic else
                f"\n\n{cfg.verifier_probes - rounds_used} round(s) left. More probes, "
                "or the verdict object now.")
        user = "PROBE OUTPUT:\n" + "\n".join(outs) + tail + steer
    if not concluded:
        # v11 FINAL DEMAND — v34 FIX: the last round's PROBE OUTPUT used to be built
        # but never sent (the loop exits before delivering it), so the model judged on
        # rounds 1..N-1 while round N's evidence — usually its most targeted probe —
        # was executed and DISCARDED. Audit: 280 of 400 recent inspections lost an
        # executed round; 92 verdicts complained about evidence that existed. The
        # pending output is now delivered WITH the demand, so the verdict rests on
        # everything that actually ran.
        pending = user if user.startswith("PROBE OUTPUT:") else ""
        demand = ((pending + "\n\n") if pending else "") + (
            "Probe budget exhausted — no more probes will be run. Based ONLY on "
            "the evidence already gathered (including the probe output above, if "
            "any), output the verdict object "
            'NOW: {"verdict": "pass"|"wrong"|"unverified", "findings": "<per '
            'requirement: met / violated / could-not-confirm + evidence>", '
            '"doubts": "<unresolved material TASK uncertainty; MUST be empty for '
            'pass>"}.')
        out = chat(model, vsys, demand,
                   max_tokens=cfg.verifier_max_tokens, temperature=cfg.temperature,
                   reasoning_effort=cfg.reasoning_effort, history=history,
                   reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0))
        history += [{"role": "user", "content": demand},
                    {"role": "assistant", "content": out or "(empty reply)"}]
        obj = parse_object(out) or {}
        v = obj.get("verdict")
        if v == "fail":
            v = "wrong"
        if v in ("pass", "wrong", "unverified"):
            candidate_findings = (_mk_findings(
                str(obj.get("findings", ""))[:2000], obj.get("doubts", ""),
                obj.get("items")) if obj.get("findings") else findings)
            if (v == "pass"
                    and str(getattr(candidate_findings, "doubts", "") or "").strip()
                    and getattr(cfg, "doubts_block_pass", False)):
                # No probe budget remains, but verdict semantics are still the
                # Verifier Agent's decision. Give it one final same-context chance to
                # classify its own caveat instead of having the harness do so.
                clarification = (_PASS_DOUBT_CLARIFICATION
                                 + "\n\nNo more probes can be run now; return only a "
                                   "revised verdict object.")
                out2 = chat(model, vsys, clarification,
                            max_tokens=cfg.verifier_max_tokens,
                            temperature=cfg.temperature,
                            reasoning_effort=cfg.reasoning_effort, history=history,
                            reasoning_max_tokens=getattr(
                                cfg, "reasoning_max_tokens", 0))
                history += [{"role": "user", "content": clarification},
                            {"role": "assistant", "content": out2 or "(empty reply)"}]
                obj2 = parse_object(out2) or {}
                v2 = "wrong" if obj2.get("verdict") == "fail" \
                    else obj2.get("verdict")
                revised = (_mk_findings(
                    str(obj2.get("findings", ""))[:2000], obj2.get("doubts", ""),
                    obj2.get("items")) if obj2.get("findings") else candidate_findings)
                if (v2 in ("wrong", "unverified")
                        or (v2 == "pass" and not str(
                            getattr(revised, "doubts", "") or "").strip())):
                    v, candidate_findings = v2, revised
                else:
                    v = "unverified"
                    candidate_findings = carry_findings(
                        "Verifier Agent did not resolve its contradictory PASS and "
                        "task doubts before the inspection safety ceiling. "
                        + str(candidate_findings), candidate_findings)
            verdict = v
            findings = candidate_findings
            _blind = (content_seen == 0
                      if getattr(cfg, "verifier_content_guard", True)
                      else informative == 0)
            if verdict == "pass" and _blind:
                verdict = "unverified"
                findings = carry_findings(
                    "[downgraded pass->unverified: no probe READ the current "
                    "candidate — prior-candidate evidence alone cannot confirm] "
                    + str(findings), findings)
                log.info("final verifier pass with 0 current-candidate content probes -> "
                         "unverified")
    if evidence_session is not None:
        evidence_session.informative_probes += informative
        evidence_session.content_probes += content_seen
    if sink is not None:
        sink.save_verify2(turn_no, verdict, findings, history)
    return verdict, findings


def second_opinion(instruction: str, vm, cfg, sink, turn_no: int, reason: str,
                   first_report: str, context: str = "", session=None):
    """R2/v31: a DOUBT-GATED second inspection on the escalation model — the reviewer
    of last resort before an acceptance the first inspection could not solidly ground.

    Motivation (tested-47 sweep + t032 smokes): runs end confidently WRONG and the only
    model-escalation trigger is a stall — a wrong-but-confident run sails through the
    unverified-accept rule to done-at-0 (t063, t032 x2 live; 8/46 tasks = 1.49 pts).
    This routes the ACCEPT DECISION itself through a different, stronger model draw.

    Independence is preserved: the reviewer sees the task, the machine, and the FIRST
    INSPECTOR's report (inspector-to-inspector — never the actor's reasoning). It runs
    the same probe/look machinery via verify_independent on a cfg whose verifier_model
    is the escalation model; its verdict is decided on its own evidence.
    Returns (verdict, findings, review_model)."""
    from dataclasses import replace as _replace
    review_model = (getattr(cfg, "escalation_model", "") or cfg.verifier_model
                    or cfg.model)
    rcfg = _replace(cfg, verifier_model=review_model)
    ctx = (
        (context + "\n\n" if context else "")
        + "A FIRST independent inspection " + reason + ". Its report:\n"
        + str(first_report)[:1500]
        + "\n\nYou are the SECOND and DECIDING reviewer, on a different model. Do not "
          "inherit the first report's conclusions — re-derive the requirements and "
          "adversarially probe the machine yourself, with special attention to "
          "everything the first inspection could not affirmatively confirm. Where a "
          "requirement is about a rendered or on-screen result, LOOK at it (render or "
          "screen:) rather than inferring from structural values."
    )
    class _Capture:
        """Shim sink: catches the reviewer's verify2 payload so its probe transcript
        can be persisted under review.json (v32.1 — it was silently discarded, which
        made reviewer probe failures un-autopsiable in the AB-12 forensics)."""
        transcript: list = []

        def save_verify2(self, n, v, f, transcript=None):
            self.transcript = transcript or []

    cap = _Capture()
    verdict, findings = verify_independent(
        instruction, vm, rcfg, sink=cap, turn_no=turn_no, context=ctx,
        session=session)
    if sink is not None:
        try:
            sink.save_review(turn_no, verdict, str(findings), reason, review_model,
                             transcript=cap.transcript)
        except Exception:                                   # noqa: BLE001 — never fatal
            log.warning("could not persist review artifact for turn %d", turn_no)
    return verdict, findings, review_model


def disagreement_message(findings: str) -> str:
    """Fed to the actor when the inspection found CONCRETE violating evidence.

    E4-A1c: when the inspector reported structured items, the unsettled ones are
    appended as a numbered punch list — the actor no longer has to parse a
    paragraph to learn which requirement is actually open."""
    return ("An INDEPENDENT INSPECTION of the machine state (performed without "
            "seeing your work or assumptions) found concrete evidence that the task "
            "is NOT complete. Its findings:\n" + findings
            + items_punchlist(findings) + "\n\nReconcile these "
            "findings with your own evidence — where you and the inspection "
            "interpret the task differently, hunt for the evidence that "
            "discriminates the two readings (especially any already-solved instance "
            "the task provides) — fix what is actually wrong, then declare done "
            "again.")


def unverified_message(findings: str, evidence_request: bool = False) -> str:
    """Fed to the actor when the inspection could NOT confirm (≠ found wrong).

    ``evidence_request`` (E4-A1): the continuity-era wording — demand proof
    through an INDEPENDENT CHANNEL. The legacy wording advised writing an
    evidence file, which is the exact self-report ritual the E2 gates reject
    and t039 followed to a confident 0.0; it is preserved verbatim below only
    for flag-off byte-exactness."""
    if evidence_request:
        return ("An INDEPENDENT INSPECTION could not CONFIRM the task is complete — "
                "it did not find it wrong; it could not gather enough evidence "
                "either way. What it could not confirm:\n" + findings +
                items_punchlist(findings) +
                "\n\nDEMONSTRATE the unconfirmed parts through an INDEPENDENT "
                "CHANNEL the inspection can re-run from the machine itself: render "
                "the deliverable and check the result, query the live service's OWN "
                "state, re-open the artifact with a different tool, show the "
                "process/server actually holding the work. Evidence the WORLD "
                "produces — a file you author about your own work proves nothing "
                "and will not count. Then declare done again with those "
                "independent-channel probes among your checks; do NOT change "
                "correct work just because it was hard to confirm. If your "
                "evidence is actually thin there, strengthen the work first.")
    return ("An INDEPENDENT INSPECTION could not CONFIRM the task is complete — it "
            "did not find it wrong; it could not gather enough evidence either way. "
            "What it could not confirm:\n" + findings + "\n\nIf your own evidence "
            "for the unconfirmed parts is solid, make it easy to check FROM THE "
            "MACHINE: write it to a file next to the deliverable (an evidence file "
            "with the exact values/paths to re-check) — a fresh inspection sees "
            "files, never your printed output — and declare done again; do "
            "NOT change correct work just because it was hard to confirm. If your "
            "evidence is actually thin there, strengthen it first.")
