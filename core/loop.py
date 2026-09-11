"""The forge attempt loop — code as policy, closed over execution traces.

One attempt = a bounded conversation in which the model repeatedly submits a whole
PROGRAM (executed in the VM, full trace fed back) until it declares done with
self-authored, instruction-derived checks that must pass in the live environment.

SEALED GRADER (Constraint #0): this loop receives only an instruction string and a VM
handle (run_script / run_command). The benchmark grader is structurally unreachable
from here — the runner invokes it once, after this function returns.
"""
import hashlib
import logging
import os
import time
from dataclasses import dataclass


def _normalize_program_transport_failure(trace) -> bool:
    """Fail closed when a legacy/duplicate VM adapter misses staging failure.

    ``VM.run_script`` is the primary authority for this bit. This narrow fallback
    recognizes only an absent execution trailer plus the trusted wrapper's exact
    mktemp/run-log failure shape. A model program that merely prints these words
    still receives a real ``[exit N]`` trailer and cannot forge infrastructure.
    """

    if bool(getattr(trace, "infra_fail", False)):
        return True
    stdout = str(getattr(trace, "stdout", "") or "")
    wrapper_failed = (
        getattr(trace, "exit_code", None) is None
        and "mktemp: failed to create file via template" in stdout
        and "/tmp/forge_" in stdout
        and "Read-only file system" in stdout)
    if wrapper_failed:
        trace.infra_fail = True
        log.warning(
            "normalized an unclassified Actor Program staging failure — "
            "model-authored code did not execute")
    return wrapper_failed


def _md5(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8", "replace")).hexdigest()


def _digest_lines(msgs: list, digest_chars: int = 220) -> str:
    # retained as summary-mode's STARVATION FALLBACK only (the digest compaction MODE
    # was removed 2026-07-03, user decision: superseded by the model-written WORK LOG,
    # which beat it 0.9-1.0 vs 0.2-0.8 in the t030 A/B)
    return "\n".join(
        f"[{'you' if m['role'] == 'assistant' else 'env'}] "
        + " ".join(str(m["content"]).split())[:digest_chars] for m in msgs)


def _cap_worklog(text: str, limit: int) -> str:
    """v21 (context): bound the WORK LOG at a LINE boundary (never mid-sentence or
    mid-value, unlike the old blind [:14000] that silently amputated whole sections).
    Keeps the TOP — the summarizer is told to put CURRENT STATE / KEY VALUES / NEXT
    STEPS first and re-derivable reference detail last — and flags the drop."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:                                  # no usable line break near the
        cut = limit                                       # cap -> hard cut as last resort
    return (text[:cut].rstrip()
            + "\n[... WORK LOG truncated at the char cap — least-important (re-derivable) "
              "detail dropped; the machine still holds ground truth, re-probe if needed]")


def _fold_input(content, head: int = 1500, tail: int = 2500) -> str:
    """v21 (context): render a folded message as head+tail (was a [:4000] PREFIX that
    hid the last ~4k of a fat trace — where tracebacks and final error lines land)."""
    s = str(content)
    if len(s) <= head + tail:
        return s
    return s[:head] + "\n…[trace middle elided]…\n" + s[-tail:]


def _fold_once(history: list, covered: int, n_pairs: int, summary: str, cfg, sink,
               turn_no: int):
    """Fold the ``n_pairs`` oldest unfolded pairs into the WORK LOG (one summarizer
    call). Falls back to appending deterministic digest lines when the summarizer
    returns nothing (progress is guaranteed either way). Returns (summary, covered)."""
    batch = history[covered * 2:(covered + n_pairs) * 2]
    rendered = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n"
        + _fold_input(m["content"]) for m in batch)
    out = chat(cfg.model, SUMMARIZER_SYSTEM,
               f"CURRENT LOG:\n{summary or '(empty)'}\n\nOLDER TURNS TO FOLD IN:\n"
               f"{rendered}\n\nOutput the updated log only.",
               max_tokens=cfg.max_tokens, temperature=0.0,
               reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    cap = cfg.worklog_max_chars
    if out.strip():
        summary = _cap_worklog(out.strip(), cap)          # v21: line-boundary, keep top
    else:                                   # summarizer starved -> deterministic fallback
        combined = summary + "\n" + _digest_lines(batch)
        if len(combined) > cap:                           # keep the TAIL (newest) here,
            b = combined.find("\n", len(combined) - cap)  # at a line boundary
            combined = combined[b + 1:] if b >= 0 else combined[-cap:]
        summary = combined
        log.warning("summarizer returned empty — folded batch as digest lines")
    covered += n_pairs
    if sink is not None:
        sink.save_summary(turn_no, summary)
    log.info("work log updated: %d pairs folded (total %d), %d chars",
             n_pairs, covered, len(summary))
    return summary, covered


def _effective_keep(history: list, cfg) -> int:
    """v14: size the verbatim window by a CHARACTER budget (keep_chars), not a fixed
    pair count. Walk newest-to-oldest accumulating chars; stop when the budget is
    spent. Floor of 6 pairs (the work in progress must stay verbatim), ceiling of
    history_keep_pairs. keep_chars=0 -> legacy fixed-pair behavior."""
    if cfg.keep_chars <= 0:
        return cfg.history_keep_pairs
    pairs = len(history) // 2
    tot, k = 0, 0
    for i in range(pairs - 1, -1, -1):
        c = (len(str(history[2 * i]["content"]))
             + len(str(history[2 * i + 1]["content"])))
        if k >= 6 and tot + c > cfg.keep_chars:
            break
        tot += c
        k += 1
        if k >= cfg.history_keep_pairs:
            break
    return max(6, min(k, cfg.history_keep_pairs)) if pairs >= 6 else pairs


def _fold_summary(history: list, covered: int, summary: str, cfg, sink, turn_no: int):
    """Summary-mode compaction. The trigger is EVALUATED, not scheduled (v9.2):
    with ctx_high_water set, folding happens only when the rendered context (WORK
    LOG + verbatim tail) actually exceeds the high-water mark, then folds oldest
    pairs — in fold_batch chunks, never into the last history_keep_pairs — until
    back under the low-water mark. The high/low hysteresis prevents folding every
    turn at the boundary; a turn producing small traces may go dozens of rounds
    without a fold, one ingesting huge traces folds immediately. The marks are
    per-config VALUES (model-related: size to the model's usable window); the
    mechanism is model-agnostic. ctx_high_water=0 keeps the legacy count trigger
    (fold whenever fold_batch unfolded pairs accumulate). Returns (summary, covered)."""
    pairs = len(history) // 2
    keep = _effective_keep(history, cfg)    # v14: char-budgeted verbatim window

    def size() -> int:                      # what the model would see this turn
        return len(summary) + sum(len(str(m["content"])) + len(m.get("reasoning", ""))
                                  for m in history[covered * 2:])   # v38: reasoning counts

    if cfg.ctx_high_water > 0:
        if size() <= cfg.ctx_high_water:
            return summary, covered
        low = cfg.ctx_low_water or cfg.ctx_high_water // 2
        s0 = size()
        folded = False
        # FLOOR GUARD (v11): fold only in full fold_batch chunks. When the keep-window
        # alone outweighs the high-water mark (fat traces + low marks), a >=1-pair
        # condition folds every single turn — 70 folds/76 turns observed: per-turn log
        # rewrites and summarizer spend the hysteresis was meant to prevent. Requiring
        # a full batch bounds worst-case fold frequency to once per fold_batch turns;
        # with sane marks (keep-window << high water) behavior is unchanged.
        while size() > low and pairs - keep - covered >= cfg.fold_batch:
            summary, covered = _fold_once(history, covered, cfg.fold_batch, summary,
                                          cfg, sink, turn_no)
            folded = True
        if folded:
            log.info("size-triggered fold: %d -> %d chars (high %d / low %d, "
                     "keep %d pairs)", s0, size(), cfg.ctx_high_water, low, keep)
        return summary, covered

    unfolded = pairs - keep - covered
    if unfolded < cfg.fold_batch:
        return summary, covered
    return _fold_once(history, covered, unfolded, summary, cfg, sink, turn_no)


import re as _re

from core.checks import run_checks, validate
from core.eyes import ensemble_look, unresolved_splits_message
from core.imagery import fetch_look_image, grid_cells, prepare_look_images
from llm.client import LLMTransportError, chat, durable_user_message
from llm.client import pop_last_reasoning as _pop_last_reasoning, parse_object
from core.actor import (FIDELITY_NOTE, NUDGE, ONE_ACTION_NUDGE, PREMATURE_DONE, REPEAT_WARNING,
                         STRICT_NUDGE, SUMMARIZER_SYSTEM, SYSTEM, VISION_AGENT_SYSTEM, VISION_SYSTEM, Ask, Done, Look, Program, build_system,
                         continuation_message, extract_plan, look_answer_message, look_message, multi_program_note,
                         opening_message, parse_turn, repair_message, trace_message)

# JIT fidelity trigger (v13): a program that saves through a rich-document library
# gets the round-trip-loss reminder IN ITS TRACE — the static-prompt version of this
# guidance measured 0% uptake, delivery at the decision moment is the fix. The
# pattern is library-level (not task-level): a .save( call near a rich-doc library
# or format token.
_RICHSAVE_RE = _re.compile(
    r"\.save\s*\(|save_workbook|SaveAs", _re.IGNORECASE)
_RICHLIB_RE = _re.compile(
    r"openpyxl|python-docx|python-pptx|\bdocx\b|\bpptx\b|\bxlsx\b|\bods\b|\bodt\b"
    r"|Workbook\s*\(|Presentation\s*\(|Document\s*\(", _re.IGNORECASE)
# v28: the v23 `_look_redirect` JIT (a keyword classifier on file extension + question
# words) was REMOVED — it merely reinforced SYSTEM principle 10 ("prefer code for
# precision; look only for recognition; read the file"), the A/B proved it non-load-
# bearing (fired 56x on t063 yet ON did not beat OFF), and it was the last task/behavior
# regex classifier in the harness. The principle carries it; the model decides.
from core.verifier import (VerifierSession, carry_findings, disagreement_message,
                            second_opinion, unverified_message,
                            verify_independent)
from core.verifier_runtime import (
    AgenticVerifierInfrastructureError, AgenticVerifierNoProgressError,
)

log = logging.getLogger("forge.loop")


def _provider_request(cfg) -> dict:
    """Forward an explicitly frozen OpenRouter route; default configs stay inert."""
    order = getattr(cfg, "provider_order", ())
    if not order:
        return {}
    return {
        "provider_order": order,
        "provider_allow_fallbacks": getattr(
            cfg, "provider_allow_fallbacks", True),
        "provider_require_parameters": getattr(
            cfg, "provider_require_parameters", False),
    }


_MEMORY_REVISIT = {
    "verifier": "The Verifier Agent has provided new evidence about the current candidate.",
    "fold": "Some earlier conversation context was compressed into the work log.",
    "recovery": "The conversation or machine channel has just recovered from an interruption.",
    "stagnation": "Recent actions show a recurring or low-information pattern.",
}


def _memory_revisit(cfg, event: str) -> str:
    """Offer retrieval at lifecycle boundaries without choosing for the Actor.

    This is inert when no frozen memory is attached. It never names, ranks, reads,
    or summarizes a memory file; the Actor Agent may ignore it entirely.
    """
    if not getattr(cfg, "env_memory_dir", ""):
        return ""
    return ("\n\n[MEMORY RETRIEVAL OPPORTUNITY] " + _MEMORY_REVISIT[event]
            + " Durable memory remains available at ~/.memory. Consider re-reading "
              "previously useful files or inspecting unread files that may now be "
              "relevant; decide yourself what, if anything, to retrieve.")


def _action_signature(turn):
    """Identity of a Program/Look request, deliberately excluding its result."""
    if isinstance(turn, Program):
        code_sha256 = hashlib.sha256(
            (turn.code or "").encode("utf-8", "replace")).hexdigest()
        return ("program", str(turn.lang).strip().lower(), code_sha256)
    if isinstance(turn, Look):
        return ("look", tuple(turn.paths), turn.question,
                tuple(turn.region) if turn.region is not None else None)
    return None


class _ActionRecurrence:
    """Observe exact recurring contiguous action blocks without controlling them.

    A two-action block is the minimum: one-action repeats already have a separate,
    output-aware detector.  Pair indexing makes observation O(1) in normal histories;
    when a pair recurs, the reported block is extended backward while it remains an
    exact, non-overlapping match.  Earlier fingerprints remain available across
    unrelated detours.
    """

    def __init__(self):
        self.signatures = []
        self.first_pair = {}

    def add(self, signature):
        """Return (earlier_start, current_start, width), or None."""
        self.signatures.append(signature)
        if len(self.signatures) < 2:
            return None
        current_start = len(self.signatures) - 2
        pair = tuple(self.signatures[-2:])
        earlier_start = self.first_pair.setdefault(pair, current_start)
        # Adjacent blocks may touch but must not overlap.
        if earlier_start == current_start or earlier_start + 2 > current_start:
            return None
        earlier_end = earlier_start + 1
        width = 2
        while (earlier_start > 0 and current_start - 1 > earlier_end
               and self.signatures[earlier_start - 1]
               == self.signatures[current_start - 1]):
            earlier_start -= 1
            current_start -= 1
            width += 1
        return earlier_start, current_start, width


def _cycle_observation(action_iters: list, match) -> str:
    """Plain evidence only: no requested response shape or prescribed decision."""
    earlier_start, current_start, width = match
    earlier = action_iters[earlier_start:earlier_start + width]
    current = action_iters[current_start:current_start + width]
    return (
        "\n\nLIVENESS OBSERVATION: parsed actions "
        f"{earlier[0]}-{earlier[-1]} and {current[0]}-{current[-1]} are the same "
        f"{width}-action block (matching action kind and program-code/look-request "
        "fingerprints). This is execution-history evidence only; the harness has not "
        "inferred whether the recurrence is useful. Account for it when choosing "
        "your next action."
    )


def _vlm_look(vision_model: str, question: str, datas: list, region, cfg) -> str:
    """v22: route a look's image(s) + question to the vision model; return its TEXT
    answer. The (blind) primary never receives pixels — it reasons over this text
    observation. Constraint #0 holds: the VLM sees only env content + the actor's
    question. v22.1: with vision_rounds>1 this is a multi-round AGENT (zoom/crop/grid
    before committing); <=1 is the single-shot tool — since v30 routed through the
    look-ensemble (core/eyes.py), which reproduces the single-shot path exactly at
    look_ensemble=1 and runs N independent witnesses + consensus/split merge above it."""
    if getattr(cfg, "vision_rounds", 1) > 1:
        return _vision_agent(vision_model, question, datas, cfg, region=region)
    return ensemble_look(cfg, question, datas, region=region)


def _fmt_vision_answer(a) -> str:
    """Render the agent's answer object (text + confidence + salient context) as text."""
    if isinstance(a, dict):
        txt = str(a.get("text", "")).strip()
        if a.get("confidence"):
            txt += f"\n[confidence: {a['confidence']}]"
        if a.get("also_visible"):
            txt += f"\nALSO VISIBLE: {a['also_visible']}"
        return txt
    return str(a).strip()


def _vision_agent(vision_model: str, question: str, datas: list, cfg, region=None) -> str:
    """v22.1: multi-round perception agent (mirrors verify_independent). The VLM may
    zoom/crop/grid-inspect the image over up to cfg.vision_rounds rounds, then commits
    to a confident answer. A single glance mis-counts / mis-reads small text; systematic
    inspection does not. A final-demand call forces an answer if rounds run out."""
    rounds = max(2, int(getattr(cfg, "vision_rounds", 5)))
    q = question or "Describe everything relevant to a computer task; read text verbatim."
    history: list = []
    view, note = prepare_look_images(datas, region=region)        # v35: honor the actor's
    #                                                                requested crop (was
    #                                                                silently ignored)
    user = (f"QUESTION: {q}\n\n{note}\nInspect as needed, then answer. You have up to "
            f"{rounds} inspection rounds.")
    answer, used, nudges = None, 0, 0
    try:
        while used < rounds and nudges < 3:
            out = chat(vision_model, VISION_AGENT_SYSTEM, user, max_tokens=1500,
                       temperature=0.0, reasoning_effort="", history=history, image=view)
            history += [{"role": "user", "content": user},
                        {"role": "assistant", "content": out or "(empty reply)"}]
            obj = parse_object(out) or {}
            if "answer" in obj:
                answer = _fmt_vision_answer(obj["answer"]); break
            insp = obj.get("inspect")
            if not isinstance(insp, dict):                       # nudge — no round spent
                nudges += 1
                user = ('Reply with ONE JSON object: {"inspect": {...}} or '
                        '{"answer": {"text": "...", "confidence": "...", "also_visible": "..."}}.')
                continue
            used += 1
            op = str(insp.get("op", "full"))
            if op == "grid":
                view, note = grid_cells(datas, insp.get("rows", 2), insp.get("cols", 2),
                                        insp.get("cell"))
            elif op in ("crop", "zoom") and insp.get("region"):
                view, note = prepare_look_images(datas, region=insp["region"])
            else:
                view, note = prepare_look_images(datas, region=None)
            user = (f"{note}\n\n{rounds - used} inspection round(s) left. Inspect again, "
                    "or give your answer object now.")
        if answer is None:                                       # final demand (verifier pattern)
            out = chat(vision_model, VISION_AGENT_SYSTEM,
                       "Inspection budget exhausted. Based ONLY on what you have already "
                       'seen, give your final answer NOW: {"answer": {"text": "...", '
                       '"confidence": "...", "also_visible": "..."}}.',
                       max_tokens=1500, temperature=0.0, reasoning_effort="",
                       history=history, image=view)
            answer = _fmt_vision_answer((parse_object(out) or {}).get("answer", out))
    except LLMTransportError:
        raise                              # caller pauses; transport is not perception
    except Exception as e:                                       # eyes must not crash the run
        return f"(vision agent error: {e})"
    return (answer or "").strip() or "(vision agent returned nothing)"


_FIND = (r"find /home/user/Desktop /home/user/Documents /home/user -maxdepth 2 "
         r"-type f -not -path '*/.*' -printf '%T@ %p\n' 2>/dev/null | sort -rn")


def _path_within(path: str, roots=()) -> bool:
    normalized = os.path.normpath(path)
    return any(
        normalized == os.path.normpath(root)
        or normalized.startswith(os.path.normpath(root).rstrip("/") + "/")
        for root in (roots or ()))


def _snapshot(vm, excluded_paths=()) -> dict:
    """v17: map of {path -> mtime} for candidate deliverable surfaces at a moment in
    time. Diffed at done to tell the inspector which instruction-relevant files the run
    actually CREATED or MODIFIED — the fabrication/circular family (renamed copies,
    from-scratch documents, planted evidence) leaves the instruction-named surface
    UNTOUCHED, which the inspector can now see and reason about (advisory context, not
    a hard gate: raise the inspector's capability, not its authority)."""
    # This is mechanical context, not model-selected retrieval. Silently dropping
    # older paths can erase the authoritative original or the actual deliverable,
    # so transport the complete manifest and let the Verifier decide relevance.
    out = vm.run_command(_FIND, timeout=30, cap=0) or ""
    m = {}
    for line in out.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and not _path_within(parts[1], excluded_paths):
            m[parts[1]] = parts[0]
    return m


def _surface_delta(vm, baseline: dict, excluded_paths=()) -> str:
    """Context string for the inspector: files created / modified since ``baseline``."""
    if not baseline:
        return ""
    now = _snapshot(vm, excluded_paths=excluded_paths)
    created = [p for p in now if p not in baseline]
    modified = [p for p in now if p in baseline and now[p] != baseline[p]]
    if not created and not modified:
        return ""
    parts = ["FILE-CHANGE EVIDENCE (mechanical, since this attempt began — use it to "
             "judge whether the surface the task NAMES was actually acted on; a task "
             "that says to edit/fill an existing file is NOT satisfied by a brand-new "
             "differently-named file left beside an untouched original):"]
    if created:
        parts.append("CREATED this run: " + ", ".join(sorted(created)))
    if modified:
        parts.append("MODIFIED this run: " + ", ".join(sorted(modified)))
    return "\n".join(parts)


def _actor_evidence_leads(accepted, results) -> str:
    """Losslessly transport only reproducible Done probes after UNVERIFIED.

    The Verifier remains independent: Actor reasoning, conclusions, and private
    transcript stay hidden. These exact commands are merely leads the Verifier can
    rerun or falsify through a different channel.
    """
    blocks = []
    for index, check in enumerate(accepted, start=1):
        result = results[index - 1] if index <= len(results) else None
        observed = ("PASS" if result is not None and result.passed else
                    "FAIL" if result is not None else "NOT_RUN")
        blocks.append(
            f"LEAD {index}\n"
            f"Actor-stated claim: {check.desc}\n"
            f"Exact reproducible probe proposed by Actor:\n"
            f"--- BEGIN PROBE ---\n{check.probe}\n--- END PROBE ---\n"
            f"Harness execution classification: {observed}")
    return "\n\n".join(blocks)


@dataclass
class LoopResult:
    status: str = "budget"      # done | evolve | budget | safety_ceiling | stalled | infra
    iters: int = 0              # parsed action turns consumed (programs + looks + dones)
    turns: int = 0              # total model calls (incl. dry/nudged turns)
    programs_run: int = 0
    looks: int = 0              # look actions taken (on-demand vision)
    asks: int = 0               # questions sent through a configured user channel
    checks_passed: int = 0      # checks that passed on the accepted done
    wall_secs: float = 0.0
    infra_pause_secs: float = 0.0  # provider/API wait excluded from the work clock
    infra_pauses: int = 0          # recoverable transport batches, for auditability
    inspections: list = None    # [(turn, verdict)] — every independent-inspection event
    resumes: int = 0            # v15: fresh-context resumes consumed
    worklog: str = ""           # final WORK LOG (resume handoff / autopsy)
    verifier_route: str = ""    # unified-loop transport route; empty when disabled
    verifier_report: str = ""   # complete lossless local Verifier Agent report
    curriculum_route: str = ""  # HANDOFF/VERIFY_MORE after PASS; REVISE/EVOLVE after FAIL
    curriculum_report: str = "" # complete lossless Curriculum Agent routing report
    surface_delta: str = None   # v18: file-delta at a stall exit ("" = provably no
    #                             file was created/modified; None = unknown/not taken)


class UserChannelInfrastructureError(RuntimeError):
    """A task-provided user-response channel failed to produce a response.

    Once ``ask_user`` is attached, its failure is an environment/transport failure,
    not evidence the Actor may replace with a guess.  Raising keeps the official
    evaluator sealed and prevents an infra-damaged candidate from receiving a score.
    """


def _call_with_transport_pause(call, cfg, *, label: str = "LLM",
                               on_pause=None):
    """Run one semantic model operation, pausing on recoverable transport failure.

    A provider/API exception is not an assistant message.  The unchanged operation
    is retried after a mechanical delay; callers can shift their active-work clock
    through ``on_pause``. Fatal request/configuration errors still raise immediately.
    """
    delay = max(0.0, float(getattr(cfg, "llm_infra_retry_secs", 30.0) or 0.0))
    while True:
        started = time.monotonic()
        try:
            return call()
        except LLMTransportError as exc:
            if not exc.recoverable:
                log.error("%s transport failure is not recoverable: %s", label, exc)
                raise
            status = (f"HTTP {exc.status_code}"
                      if exc.status_code is not None else type(exc.cause).__name__)
            log.warning("%s infrastructure pause (%s); preserving the same Agent "
                        "context and retrying unchanged in %.0fs", label, status, delay)
            time.sleep(delay)
            paused = max(0.0, time.monotonic() - started)
            if on_pause is not None:
                on_pause(paused)


def uv_accept_ok(cfg, unverified: int, commit: bool, wrongs: int) -> bool:
    """E4-A1: the unverified-accept decision, extracted for testability.

    Legacy (both flags off): Nth could-not-confirm accepts, whatever the run's
    record — the E3 exit-door (t005 -1.0 / t039 -.55 / t035 -.50 all left here).
    budget_conditional_accept (v21, built but never shipped ON): accepts only in
    the commit phase with a clean record. verifier_continuity (wenyi 08-08):
    a WRONG revokes benefit-of-the-doubt for the rest of the run — evidence,
    not exhausted patience, closes the case."""
    # The full-Agent verifier arm has a binary terminal contract.  A missing or
    # invalid report is an infrastructure/transport absence, never evidence that the
    # candidate is correct, regardless of how many times it happens.
    if getattr(cfg, "agentic_verifier_config", ""):
        return False
    return (unverified >= cfg.unverified_accept
            and (not cfg.budget_conditional_accept or (commit and wrongs == 0))
            and not (getattr(cfg, "verifier_continuity", False) and wrongs > 0))


def run_attempt(instruction: str, vm, cfg, sink, iters_budget: int = None,
                wall_budget: float = None, opening_extra: str = "",
                iter_hook=None, initial_history=None,
                continue_context: bool = False,
                allow_noop_done: bool = False,
                terminal_handoff_ready=None, verifier_session=None,
                program_executor=None, look_path_validator=None,
                look_fetcher=None,
                system_prompt=None, action_nudge=None,
                strict_action_nudge=None, premature_done_nudge=None,
                user_message_transform=None, surface_baseline=None,
                instruction_is_complete_opening: bool = False,
                verifier_failure_router=None, verifier_pass_router=None,
                ask_user=None, opening_image=None):
    """Run one attempt; returns (LoopResult, history). ``sink`` is an ArtifactSink.
    ``iters_budget``/``wall_budget`` override the config budgets (v15 resume passes
    the remainder); ``opening_extra`` is prepended to the opening message (the
    resume handoff). ``initial_history`` + ``continue_context`` opt into a follow-up
    phase in the same Actor Agent conversation. ``allow_noop_done`` lets such a
    phase preserve the current state without forcing a meaningless program.
    ``terminal_handoff_ready`` is an optional transport-only callback: after a
    completed program action, a true value means the Agent has published its
    canonical terminal handoff and the attempt ends before another stochastic
    model call. It does not inspect task correctness. ``verifier_session`` carries
    one Verifier Agent while that Verifier identity is active; the resume controller
    reuses it for same-role resumes and supplies a distinct session after a Verifier
    model/config switch. ``program_executor`` optionally replaces only model-authored
    Program execution; it defaults to ``vm.run_script``. ``look_path_validator`` can
    reject model-selected Look targets before rendering, and ``look_fetcher`` can
    execute trusted rendering through a role-specific isolation boundary. It defaults
    to the normal VM renderer. All preserve every Actor call site when omitted.
    ``system_prompt`` and the three nudge overrides let a restricted role describe
    its real action contract without inheriting Actor-only Python/Bash instructions.
    ``user_message_transform`` can losslessly wrap every outgoing observation with
    that role's authoritative action reminder.
    ``instruction_is_complete_opening`` lets a restricted role supply its own
    complete first/recovery envelope without inheriting Actor-only PLAN/done
    language. It defaults off for every Actor call site.
    ``opening_image`` optionally reattaches an exact previously delivered visual
    observation to the first user turn of a recovered segment.  It is transport
    state, not a new observation, and defaults off for every ordinary Actor call.
    All of these default off, preserving existing call sites.
    ``verifier_failure_router`` is an optional separated-authority callback used
    only after a concrete Verifier Agent FAIL. It receives the complete report and
    returns ``(REVISE|EVOLVE, complete Curriculum Agent report)``. With no callback,
    the historical same-Actor revision behavior is byte-identical.
    ``verifier_pass_router`` is the complementary unified-loop callback. It receives
    a complete Verifier PASS and returns ``(HANDOFF|VERIFY_MORE, complete Curriculum
    Agent report)``. VERIFY_MORE resumes the same Verifier Agent on the unchanged
    candidate; it never returns control to the Actor or changes machine state.
    ``iter_hook`` (PREREG E7
    B5, OFF-inert default None):
    called once per loop iteration with res.iters; a truthy return ends the attempt
    with status "stalled_quiescent" — the practice-side stall detector's hook. Eval
    configs never pass it; behavior is byte-identical when None."""
    t0 = time.time()
    budget = iters_budget or cfg.max_iters
    wall_cap = wall_budget or cfg.wall_clock_secs
    agent_decided_stop = bool(getattr(cfg, "agent_decided_stop", False))
    res = LoopResult()
    res.inspections = []

    def _record_transport_pause(seconds: float, events: int = 1) -> None:
        nonlocal t0
        # Move the active-work origin forward so provider downtime consumes neither
        # the Actor's nor the Verifier's emergency wall budget.
        t0 += seconds
        res.infra_pause_secs += seconds
        res.infra_pauses += max(0, int(events))

    def _llm_operation(label: str, call):
        return _call_with_transport_pause(
            call, cfg, label=label, on_pause=_record_transport_pause)

    history: list = list(initial_history or [])
    if len(history) % 2:
        raise ValueError("initial_history must contain complete user/assistant pairs")
    if instruction_is_complete_opening:
        user = instruction
    else:
        _opening = continuation_message if continue_context else opening_message
        user = _opening(
            instruction, None if agent_decided_stop else budget,
            wall_mins=None if agent_decided_stop else int(wall_cap // 60))
    if opening_extra:
        user = opening_extra + "\n\n" + user
    dry = 0          # consecutive unparseable turns
    degen = 0        # v21 (A): consecutive degenerate multi-action turns (decoder loop)
    nudges = 0       # cumulative telemetry only; never a completion/stall cap
    dones = 0        # consecutive failed done declarations
    plan = ""        # the model's own latest 'PLAN:' line — pinned into every trace msg
    image = opening_image  # bytes attached to the NEXT chat call after a Look action;
    #                  recovery may initialize this with exact archived Look pixels
    #                  exact successfully delivered bytes are then retained in history
    prev_sig = None  # (program md5, stdout md5) — repetition detector
    repeats = 0      # consecutive REPEAT detections: the warning alone does not break
    #                  a temp-0 attractor (a run's own log can diagnose the loop while
    #                  the actor continues it), so a second offense gets the same heat
    #                  as a dry retry
    cycle_tracker = (_ActionRecurrence()
                     if getattr(cfg, "cycle_evidence", False) else None)
    cycle_iters = []
    cycle_active = False  # suppress duplicate notes while one recurrence continues
    extra = 0        # inspection-repair extension turns granted (bounded by config)
    unverified = 0   # inspections that could not confirm; at cfg.unverified_accept
    #                  the declaration is accepted (shallow inspection ≠ veto power)
    wrongs = 0       # lifetime CONFIRMED-wrong verdicts; they block laundering a later
    #                  unverified verdict into acceptance, but never replace the Actor
    #                  Agent or discard its semantic-repair context.
    forced = False   # the one end-of-budget forced inspection has been spent
    summary = ""     # summary-mode compaction: the model's own running WORK LOG
    covered = 0      # pairs already folded into the summary
    fidelity_sent = False   # the JIT rich-save reminder fires once per attempt
    infra_streak = 0        # v17: consecutive run-wrapper failures (dead-VM detector)
    doubt_reviews = 0       # R2/v31: doubt-gated second inspections spent (capped)
    had_done_bounce = False # R2/v31: some done declaration has been bounced...
    progs_since_bounce = 0  # ...and this many programs ran since — 0 at the next
    #                         accept = a HOLLOW re-declare (nothing could have changed;
    #                         the actor re-rolled or weakened its checks) -> review
    splits = []             # R3/v32: ledger of looks whose readings SPLIT —
    #                         {'turn','path','q','open'}. A later look on the same
    #                         path that reaches agreement closes its entries; open
    #                         entries gate the FIRST done (one-shot) and are handed
    #                         to the inspector as mechanical context.
    split_gate_spent = False  # R3: the one-shot done-gate has fired
    witness_gate_spent = False  # Fix D-2 (E2 autopsy): one-shot witness bounce
    actor_evidence_requested = False  # Agentic UNVERIFIED asks only for reproducible
    #                                   leads on the Actor's next Done declaration.
    vsession = (verifier_session if isinstance(verifier_session, VerifierSession)
                else VerifierSession())  # E4-A1: ONE Verifier Agent per project.
    # Its conversation and cumulative telemetry share the project lifetime; evidence
    # freshness resets inside each verify_independent call.
    if getattr(cfg, "archive_eyes", False) and sink is not None:
        # v32.5: archive every image handed to the eyes under <run>/eyes/ — a human
        # referee must be able to open exactly what the model saw (the alpha-flatten
        # bug hid for 3 runs because nobody could).
        import os as _os
        from core import imagery as _imagery
        _imagery.ARCHIVE_DIR = _os.path.join(getattr(sink, "root", "."), "eyes")
    system = system_prompt or build_system(
        cfg, ask_enabled=ask_user is not None)  # mechanics match the active role
    private_surface_paths = ()
    if getattr(cfg, "verifier_evolve_route", False):
        private_surface_paths = tuple(dict.fromkeys(
            tuple(getattr(cfg, "verifier_private_paths", ()) or ())
            + (("/home/user/.memory",)
               if getattr(cfg, "verifier_hide_actor_memory", False) else ())))
    baseline = (_snapshot(vm, excluded_paths=private_surface_paths)
                if surface_baseline is None
                else dict(surface_baseline))
    # v17: deliverable-surface manifest at recon (post-setup). Unified target runs
    # capture it before the Actor exists and reuse that exact S0 projection across
    # fresh-context Actor resumes.

    def _split_context() -> str:
        """R3: mechanical note about unresolved visual disagreements, appended to the
        inspector's context (v17-style — facts about the loop's own observations,
        never the actor's reasoning)."""
        op = [s for s in splits if s['open']]
        if not op:
            return ""
        lines = "; ".join(f"{s['path']} ({s['q']})" for s in op[:6])
        return ("Mechanical note: independent visual readings of the machine "
                "DISAGREED and were never re-read to agreement on: " + lines +
                ". If any of these bears on a requirement, verify that point "
                "directly (render/look/measure) rather than trusting either reading.")

    def _with_splits(base: str) -> str:
        sctx = _split_context()
        return (base + "\n\n" + sctx) if sctx else base

    def _verifier_wall_budget() -> float:
        """The Verifier shares, and cannot silently extend, this run's wall clock."""
        return max(0.0, wall_cap - (time.time() - t0))

    def _route_verified_pass(f) -> tuple[str, str]:
        """Let the high-level authority audit whether a PASS may terminate."""
        if verifier_pass_router is None:
            return "HANDOFF", ""
        routed = verifier_pass_router(str(f))
        if (not isinstance(routed, tuple) or len(routed) != 2
                or not isinstance(routed[0], str)
                or not isinstance(routed[1], str)):
            raise RuntimeError(
                "verifier_pass_router must return (HANDOFF|VERIFY_MORE, report)")
        route, report = routed[0].strip().upper(), routed[1]
        if route not in {"HANDOFF", "VERIFY_MORE"} or not report.strip():
            raise RuntimeError(
                "Curriculum Agent PASS routing must be HANDOFF/VERIFY_MORE "
                "with a report")
        res.curriculum_route = route
        res.curriculum_report = report
        res.inspections.append((res.turns, f"curriculum:{route.lower()}"))
        log.info("Verifier PASS -> persistent Curriculum Agent -> %s", route)
        return route, report

    def _inspect(context: str, actor_evidence_leads: str = ""):
        """Run local verification, then obtain the independent terminal key.

        Every committed Verifier report checkpoints and detaches private scratch
        before either the Curriculum or Actor may spend substantial time.  This
        removes the long-idle namespace keeper while preserving its exact bytes.
        """
        curriculum_review = ""
        same_candidate = False
        infra_retries = 0
        max_infra_retries = max(
            0, int(getattr(cfg, "verifier_infra_retries", 0) or 0))
        while True:
            kwargs = {
                "context": context,
                "session": vsession,
            }
            # Preserve the incumbent verifier call surface byte-for-byte unless a
            # Curriculum Agent has actually requested another inspection of this
            # unchanged candidate.  Historical/custom verifier callables therefore
            # need no awareness of the opt-in two-key lifecycle.
            if same_candidate:
                kwargs.update(
                    continue_candidate=True,
                    curriculum_review=curriculum_review)
            if actor_evidence_leads:
                kwargs["actor_evidence_leads"] = actor_evidence_leads
            if getattr(cfg, "agentic_verifier_config", ""):
                kwargs.update(wall_budget=_verifier_wall_budget())
            nested_secs_before = vsession.infra_pause_secs
            nested_events_before = vsession.infra_pauses
            try:
                inspected = _llm_operation(
                    "Verifier Agent",
                    lambda: verify_independent(
                        instruction, vm, cfg, sink, res.turns, **kwargs))
            except AgenticVerifierNoProgressError:
                # This requires inspection/context recovery, not another VM
                # boundary rebuild with the same actionless checkpoint.
                try:
                    sink.save_transcript(system, history)
                except Exception:
                    log.exception("Could not flush Actor context at Verifier guard")
                raise
            except AgenticVerifierInfrastructureError:
                if (not getattr(cfg, "agentic_verifier_config", "")
                        or not getattr(cfg, "verifier_persist_scratch", False)
                        or not isinstance(vsession, VerifierSession)
                        or infra_retries >= max_infra_retries):
                    raise
                # The executor owns the QEMU checkpoint. Detaching closes it and
                # restores the exact candidate before any retry. A dead controller
                # may prevent the optional private-scratch export; that archive can
                # be discarded for an infrastructure retry, but rollback failure
                # still raises and remains fail-closed.
                recovery_started = time.time()
                vsession.detach_executor(
                    vm, preserve=True, tolerate_archive_failure=True)
                recovery_secs = time.time() - recovery_started
                if recovery_secs > 0:
                    _record_transport_pause(recovery_secs, 1)
                infra_retries += 1
                same_candidate = True
                log.warning(
                    "Verifier trusted runtime failed; candidate rolled back, "
                    "rebuilding boundary for retry %d/%d",
                    infra_retries, max_infra_retries)
                continue
            # The agentic Verifier is a nested run_attempt. It already excludes its
            # own provider pauses and records them on the persistent session;
            # propagate only this inspection's delta to the enclosing Actor clock.
            nested_secs = max(
                0.0, vsession.infra_pause_secs - nested_secs_before)
            nested_events = max(
                0, vsession.infra_pauses - nested_events_before)
            if nested_secs or nested_events:
                _record_transport_pause(nested_secs, nested_events)
            if (getattr(cfg, "agentic_verifier_config", "")
                    and getattr(cfg, "verifier_persist_scratch", False)
                    and isinstance(vsession, VerifierSession)):
                vsession.detach_executor(vm, preserve=True)

            verdict, findings = inspected
            if verdict != "pass" or verifier_pass_router is None:
                return inspected
            route, curriculum_report = _route_verified_pass(findings)
            if route == "HANDOFF":
                return inspected
            # The candidate did not change.  The same Verifier Agent receives only
            # the high-level report-sufficiency challenge and remains free to decide
            # how to investigate and whether its local verdict still holds.
            curriculum_review = curriculum_report
            same_candidate = True

    # ---- R2/v31 helpers: doubt-gated second inspection at the accept decision ----
    def _review_ok() -> bool:
        return (getattr(cfg, "doubt_escalation", False) and bool(cfg.escalation_model)
                and doubt_reviews < getattr(cfg, "doubt_reviews_max", 2))

    def _route_verified_failure(f) -> str:
        """Let the high-level authority route one local correctness failure."""
        if verifier_failure_router is None:
            if getattr(cfg, "verifier_failure_starts_evolution", False):
                res.inspections.append((res.turns, "protocol:evolve_after_fail"))
                log.info(
                    "Verifier FAIL -> self-evolving benchmark protocol -> EVOLVE")
                return "EVOLVE"
            return "REVISE"
        routed = verifier_failure_router(str(f))
        if (not isinstance(routed, tuple) or len(routed) != 2
                or not isinstance(routed[0], str)
                or not isinstance(routed[1], str)):
            raise RuntimeError(
                "verifier_failure_router must return (REVISE|EVOLVE, report)")
        route, report = routed[0].strip().upper(), routed[1]
        if route not in {"REVISE", "EVOLVE"} or not report.strip():
            raise RuntimeError(
                "Curriculum Agent routing must be REVISE/EVOLVE with a report")
        res.curriculum_route = route
        res.curriculum_report = report
        res.inspections.append((res.turns, f"curriculum:{route.lower()}"))
        log.info("Verifier FAIL -> persistent Curriculum Agent -> %s", route)
        return route

    def _wrong_bounce(f) -> str:
        """Mirror of the inspector-wrong branch, for a review 'wrong' verdict.
        Semantic feedback always returns to the same Actor Agent/context."""
        nonlocal wrongs, extra, user, had_done_bounce, progs_since_bounce
        wrongs += 1
        if _route_verified_failure(f) == "EVOLVE":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(f)
            return "evolve"
        had_done_bounce, progs_since_bounce = True, 0
        if extra < cfg.inspect_extension * cfg.max_inspect_extensions:
            extra += cfg.inspect_extension
            log.info("review bounce -> +%d turns (extension %d)",
                     cfg.inspect_extension, extra)
        user = disagreement_message(f) + _memory_revisit(cfg, "verifier")
        return "bounced"

    def _review(reason: str, first_report: str) -> str:
        """Run the second opinion; return accept, bounced, evolve, or stall."""
        nonlocal doubt_reviews
        doubt_reviews += 1
        review_session = VerifierSession()
        v2, f2, rmodel = _llm_operation(
            "Verifier second opinion",
            lambda: second_opinion(
                instruction, vm, cfg, sink, res.turns, reason, first_report,
                context=_surface_delta(
                    vm, baseline, excluded_paths=private_surface_paths),
                session=review_session))
        # A full-Agent second opinion is another nested run_attempt. Account for
        # provider pauses it handled internally just as we do for the primary
        # persistent Verifier Agent.
        if review_session.infra_pause_secs or review_session.infra_pauses:
            _record_transport_pause(
                review_session.infra_pause_secs, review_session.infra_pauses)
        res.inspections.append((res.turns, f"review:{v2}"))
        log.info("iter %d: doubt-review #%d (%s) -> %s",
                 res.iters, doubt_reviews, rmodel, v2)
        if v2 == "wrong":
            return _wrong_bounce(f2)
        if v2 == "evolve":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(f2)
            return "evolve"
        return "accept"          # pass, or a second could-not-confirm: only CONCRETE
    #                              violating evidence may block an acceptance here

    while True:
        if iter_hook is not None and iter_hook(res.iters):
            res.status = "stalled_quiescent"     # E7 B5: harness-terminated on
            log.info("stall detector ended the session at iter %d",  # quiescence;
                     res.iters)                  # graded as-is by the caller
            break
        if res.iters >= budget + extra:          # budget exhausted -> one FORCED
            if agent_decided_stop:
                res.status = "safety_ceiling"
                log.warning("emergency iteration ceiling reached before agent done")
                break
            if (cfg.independent_verify and not forced   # inspection: most weak-model
                    and time.time() - t0 <= wall_cap):   # runs never declare,
                forced = True                           # so the inspector never got to
                verdict, findings = _inspect(  # help them at all
                    _with_splits(_surface_delta(
                        vm, baseline, excluded_paths=private_surface_paths)))
                res.inspections.append((res.turns, f"forced:{verdict}"))
                log.info("forced end-of-budget inspection -> %s", verdict)
                if verdict == "pass":
                    res.status = "done"                 # inspected-complete, undeclared
                    if getattr(cfg, "verifier_evolve_route", False):
                        res.verifier_route = "HANDOFF"
                        res.verifier_report = str(findings)
                    break
                if verdict == "evolve":
                    res.status = "evolve"
                    res.verifier_route = "EVOLVE"
                    res.verifier_report = str(findings)
                    break
                if (verdict == "wrong" and
                        extra < cfg.inspect_extension * cfg.max_inspect_extensions):
                    if _wrong_bounce(findings) == "evolve":
                        break
                    continue
            break
        if time.time() - t0 > wall_cap:
            if agent_decided_stop:
                res.status = "safety_ceiling"
                log.warning("emergency wall ceiling reached before agent done")
            else:
                log.info("wall clock exhausted")
            break
        if cfg.history_keep_pairs > 0:      # compaction = model-written WORK LOG (the
            before_fold = covered
            summary, covered = _llm_operation(
                "Actor work-log compaction",
                lambda: _fold_summary(                          # only mode
                    history, covered, summary, cfg, sink, res.turns))
            if covered > before_fold:
                user += _memory_revisit(cfg, "fold")
            ctx = ([{"role": "user", "content":
                     # v21 (context): pin the TASK verbatim above the log — the opening
                     # message is pair 0, the FIRST thing folded, so the goal used to
                     # survive only as a summarizer paraphrase (some runs lost it).
                     "TASK (verbatim, unchanged — this is the actual goal):\n"
                     + instruction + "\n\nWORK LOG — your running summary of earlier "
                     "turns (full details live in the machine; re-probe if you need "
                     "specifics):\n" + summary}]
                   + history[covered * 2:]) if summary else history
        else:
            ctx = history                   # 0 = full history (short-horizon default)
        if getattr(cfg, "primary_temperature", -1.0) >= 0:
            temp = cfg.primary_temperature   # official K3 sampling point; heat always
            #                                  on -> subsumes the dry-retry temp bump
        else:
            temp = cfg.retry_temperature if (dry or degen or repeats >= 2) else cfg.temperature
        sent_user = (user_message_transform(user)
                     if user_message_transform is not None else user)
        sent_image = image
        transport_options = {}
        if dry and os.environ.get("FORGE_JSON_ACTION_RETRY") == "1":
            # Formatting retry: preserve model, sampling, context and parser.
            # Enable only after checking the route's response_format support.
            transport_options["json_object"] = True
            log.info("retrying unparseable action with JSON object response format")
        out = _llm_operation(
            "Actor Agent",
            lambda: chat(
                cfg.model, system, sent_user, max_tokens=cfg.max_tokens,
                temperature=temp, top_p=getattr(cfg, "top_p", -1.0),
                reasoning_effort=cfg.reasoning_effort,
                history=ctx,
                image=sent_image,
                reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
                **transport_options,
                **_provider_request(cfg)))                      # dry retry -> temp bump: at temp 0 a
        image = None                               # degenerated decoder re-samples the
        #                                            SAME dry output forever (the ~25%
        #                                            wipeout class); heat breaks the cycle
        amsg = {"role": "assistant", "content": out or "(empty reply)"}
        if getattr(cfg, "reasoning_in_history", False):
            _rsn = _pop_last_reasoning()           # v38 official K3 protocol: the
            if _rsn:                               # COMPLETE assistant message returns
                amsg["reasoning"] = _rsn           # to context, not content alone
        # Preserve exact native visual observations rather than only the sentence
        # saying an image was attached. Delegated-eye reports are already durable
        # text and arrive here with ``sent_image is None``.
        history += [durable_user_message(sent_user, sent_image), amsg]
        res.turns += 1
        sink.save_turn(res.turns, out)
        plan = extract_plan(out) or plan          # latest revision wins; survives turns
        turn = parse_turn(out)

        if turn is None:                                   # genuine model dry turn
            dry += 1
            nudges += 1
            if dry >= cfg.max_consec_dry:
                res.status = "stalled"
                log.info("stalled: %d consecutive dry turns / %d nudges", dry, nudges)
                break
            user = ((strict_action_nudge or STRICT_NUDGE)
                    if getattr(cfg, "action_discipline", False)
                    else (action_nudge or NUDGE))     # v36 k3fit: narration-aware nudge
            continue
        dry = 0
        if cfg.strict_one_action and getattr(turn, "dup", 1) >= 2:
            # v21 (A): decoder degeneration — the reply carried >=2 objects of ONE
            # action kind, and last-wins would run a ritual/duplicate object (17
            # consecutive `echo done` no-ops, the invisible 34-turn build loop). Run
            # NOTHING and re-ask for one action; consecutive wedges escalate to the
            # fresh-context pivot, which reliably breaks the decoder loop.
            degen += 1
            nudges += 1
            if degen >= cfg.max_consec_degen:
                res.status = "stalled"
                log.info("stalled: %d consecutive degenerate multi-action turns "
                         "(%dx one kind) -> stall/pivot", degen, getattr(turn, "dup", 1))
                break
            user = ONE_ACTION_NUDGE
            log.info("iter %d: degenerate turn (%dx one kind) -> re-ask, ran nothing",
                     res.iters, getattr(turn, "dup", 1))
            continue
        degen = 0
        res.iters += 1
        cycle_note = ""
        action_sig = _action_signature(turn) if cycle_tracker is not None else None
        if action_sig is not None:
            cycle_iters.append(res.iters)
            cycle_match = cycle_tracker.add(action_sig)
            if cycle_match is not None and not cycle_active:
                cycle_note = _cycle_observation(cycle_iters, cycle_match)
                a, b, width = cycle_match
                log.info("iter %d: recurring %d-action block observed (%d-%d repeats "
                         "%d-%d); evidence delivered without gating",
                         res.iters, width,
                         cycle_iters[a], cycle_iters[a + width - 1],
                         cycle_iters[b], cycle_iters[b + width - 1])
            cycle_active = cycle_match is not None
        elif cycle_tracker is not None:
            # Done/check boundaries start a new action sequence. A later lifecycle
            # phase should not be called contiguous with actions before a declaration.
            cycle_tracker = _ActionRecurrence()
            cycle_iters = []
            cycle_active = False

        if isinstance(turn, Program):
            dones = 0
            res.programs_run += 1
            progs_since_bounce += 1                        # R2: state may change now
            sink.save_program(res.turns, turn.lang, turn.code)
            log.info("iter %d: program (%s, %d chars)", res.iters, turn.lang, len(turn.code))
            execute_program = program_executor or vm.run_script
            trace = execute_program(
                turn.lang, turn.code, timeout=cfg.script_timeout)
            _normalize_program_transport_failure(trace)
            recovered_channel = False
            channel_failure = (trace.infra_fail
                               and trace.stdout.startswith("[channel error:"))
            recovery_wait = getattr(vm, "wait_for_controller", None)
            recovery_secs = getattr(cfg, "controller_recovery_secs", 0)
            if channel_failure and callable(recovery_wait) and recovery_secs > 0:
                # A resource-heavy Actor action may restart the guest controller.
                # Do not spend stochastic Actor turns probing a temporarily dead
                # channel, and never replay the failed action. Wait mechanically,
                # then let the SAME conversation reason from the explicit failure.
                recovered_channel, recovery_report = recovery_wait(
                    timeout=recovery_secs,
                    probe_interval=getattr(
                        cfg, "controller_recovery_probe_secs", 5.0),
                    stable_probes=getattr(
                        cfg, "controller_recovery_stable_probes", 2))
                disposition = (
                    "The failed program was NOT replayed. Its result is unknown; "
                    "inspect live state before relying on any partial effects."
                    if recovered_channel else
                    "The failed program was NOT replayed and the environment remains "
                    "unavailable.")
                trace.stdout += (
                    "\n[HARNESS CHANNEL RECOVERY: " + recovery_report + ". "
                    + disposition + "]")
            sink.save_trace(res.turns, trace)
            log.info("iter %d: exit=%s secs=%.0f out=%d chars", res.iters,
                     trace.exit_code, trace.secs, len(trace.stdout))
            if trace.infra_fail:                           # v17: dead-VM detector — the
                if program_executor is not None:
                    # A trusted Verifier sandbox failure means its authored action
                    # did not execute.  Do not spend further Agent turns interpreting
                    # infrastructure as candidate evidence.
                    res.status = "infra"
                    log.warning(
                        "aborting Verifier phase on trusted sandbox failure — "
                        "infra, not task")
                    break
                if recovered_channel:
                    infra_streak = 0
                    log.info("iter %d: controller stable again; same Actor resumes "
                             "without action replay", res.iters)
                elif channel_failure and callable(recovery_wait) and recovery_secs > 0:
                    # One complete recovery window is stronger evidence of a dead
                    # channel than three immediate model-generated probes.
                    res.status = "infra"
                    log.warning("aborting: controller failed its full %.0fs recovery "
                                "window — infra, not task", recovery_secs)
                    break
                else:
                    infra_streak += 1                      # run wrapper couldn't stage
                if infra_streak >= 3:                      # the program (guest fs
                    res.status = "infra"                   # read-only). No program has
                    log.warning("aborting: %d consecutive run-wrapper failures "  # run
                                "(guest fs read-only?) — infra, not task", infra_streak)
                    break                                  # since the streak began.
            else:
                infra_streak = 0
            # Some agentic phases publish a free-form terminal handoff from a
            # program action. Publication must be atomic with stopping:
            # asking for a ceremonial second ``done`` sample leaves the Agent's
            # already-authored decision mutable in a stochastic gap. The caller
            # owns the purely lexical readiness predicate; core knows neither the
            # token vocabulary nor its meaning.
            if (terminal_handoff_ready is not None
                    and not trace.infra_fail
                    and terminal_handoff_ready()):
                res.status = "done"
                log.info("iter %d: canonical terminal handoff published",
                         res.iters)
                break
            spent = time.time() - t0                       # commit phase triggers on
            commit = (False if agent_decided_stop else
                      (res.iters >= max(1, int(budget * cfg.commit_frac))
                       or spent >= wall_cap * cfg.commit_frac))   # EITHER budget
            wall_mins = (None if agent_decided_stop else
                         max(0, int((wall_cap - spent) // 60)))
            turns_left = None if agent_decided_stop else budget + extra - res.iters
            user = trace_message(trace, turns_left, plan, commit,
                                 cfg.trace_head, cfg.trace_tail, wall_mins=wall_mins,
                                 context_max_chars=getattr(
                                     cfg, "trace_context_max_chars", 0))
            if recovered_channel:
                user += _memory_revisit(cfg, "recovery")
            if turn.extras:                                # multi-program reply: only
                user += multi_program_note(turn.extras)    # the last ran — SAY so, or
                log.info("iter %d: %d extra program object(s) discarded",  # the model
                         res.iters, turn.extras)           # builds a false env-model
            if (not fidelity_sent and _RICHSAVE_RE.search(turn.code)
                    and _RICHLIB_RE.search(turn.code)):
                user += FIDELITY_NOTE                      # JIT delivery (v13): fires
                fidelity_sent = True                       # at the save moment, once
                log.info("iter %d: rich-save detected -> fidelity note", res.iters)
            sig = (_md5(turn.code), _md5(trace.stdout))    # repetition detector:
            if sig == prev_sig:                            # same code AND same output
                repeats += 1
                user += REPEAT_WARNING                     # 1st offense: warn;
                if repeats == 1:
                    user += _memory_revisit(cfg, "stagnation")
                log.info("iter %d: REPEAT detected (x%d)%s", res.iters, repeats,
                         " -> temp escalation" if repeats >= 2 else "")
                if repeats >= cfg.max_consec_repeat:       # 2nd+: heat the decoder;
                    res.status = "stalled"                 # at the cap: fail FAST —
                    log.info("stalled: %d consecutive identical program+output",
                             repeats)                      # warnings and heat both
                    break                                  # failed (497x proven);
            else:                                          # legit polls have CHANGING
                repeats = 0                                # output and never get here
            prev_sig = sig
            user += cycle_note
            if cycle_note:
                user += _memory_revisit(cfg, "stagnation")
            continue

        if isinstance(turn, Look):
            dones = 0
            res.looks += 1
            datas, errs = [], []
            for p in turn.paths:
                if look_path_validator is not None:
                    allowed, reason = look_path_validator(p)
                    if not allowed:
                        errs.append(f"{p}: {reason}")
                        continue
                fetch_image = look_fetcher or (
                    lambda target: fetch_look_image(vm, target))
                d, e = fetch_image(p)                      # v26: auto-render documents
                if d is None:
                    errs.append(f"{p}: {e}")
                else:
                    datas.append(d)
            sink.save_look(res.turns, turn.path, turn.question, ok=bool(datas))
            log.info("iter %d: look %s (%d attached%s)", res.iters, turn.path,
                     len(datas), f", errors: {'; '.join(errs)}" if errs else "")
            spent = time.time() - t0
            commit = (False if agent_decided_stop else
                      (res.iters >= max(1, int(budget * cfg.commit_frac))
                       or spent >= wall_cap * cfg.commit_frac))
            wall_mins = (None if agent_decided_stop else
                         max(0, int((wall_cap - spent) // 60)))
            turns_left = None if agent_decided_stop else budget + extra - res.iters
            if not datas:
                user = (f"Could not read {turn.path}: {'; '.join(errs)}. Document files "
                        "are rendered to an image automatically — check the path names a "
                        "real, non-empty file, then continue."
                        + ("" if agent_decided_stop else
                           f" {budget + extra - res.iters} action turn(s) remain."))
            elif cfg.vision_model:                             # v22: delegate seeing to
                answer = _llm_operation(
                    "visual reader",
                    lambda: _vlm_look(                              # a vision model;
                        cfg.vision_model, turn.question,             # primary stays blind
                        datas, turn.region, cfg))
                log.info("iter %d: vision tool (%s) -> %d chars",
                         res.iters, cfg.vision_model, len(answer))
                if getattr(answer, "split", None) is True:     # R3: ledger the conflict
                    # (v34: `is True` — on a plain str, .split is a bound METHOD and
                    # truthy; the vision-agent arm returns plain str and was ledgering
                    # a phantom split for every look)
                    splits.append({'turn': res.turns, 'path': turn.path,
                                   'q': (turn.question or "")[:80], 'open': True})
                    log.info("iter %d: split LEDGERED (%d open)", res.iters,
                             sum(1 for s in splits if s['open']))
                else:                                          # agreement on this path
                    closed = 0                                 # closes its open splits
                    for s in splits:
                        if s['open'] and s['path'] == turn.path:
                            s['open'] = False
                            closed += 1
                    if closed:
                        log.info("iter %d: %d split(s) on %s closed by agreeing re-read",
                                 res.iters, closed, turn.path)
                image = None                                   # NOT attached to primary
                user = look_answer_message(turn.path, turn.question, answer,
                                           turns_left, plan, commit,
                                           wall_mins=wall_mins)
                if errs:
                    user = f"(Some paths not attached — {'; '.join(errs)}.)\n\n" + user
            else:
                image, att_note = prepare_look_images(datas, region=turn.region,
                                                     max_side=getattr(cfg, "look_max_side", 0) or None)  # pre-108 item 5: native parity with the eyes path
                user = look_message(turn.path, turn.question,   # attach on NEXT call;
                                                               # retain thereafter
                                    turns_left, plan, commit,
                                    wall_mins=wall_mins)
                if att_note:
                    user = att_note + "\n\n" + user
                if errs:
                    user = f"(Not attached — {'; '.join(errs)}.)\n\n" + user
            user += cycle_note
            if cycle_note:
                user += _memory_revisit(cfg, "stagnation")
            continue

        if isinstance(turn, Ask):
            res.asks += 1
            question = turn.question.strip()
            if ask_user is None:
                answer = ("No user-response channel is configured for this task. "
                          "Continue from evidence available in the environment.")
                ok = False
            else:
                try:
                    answer = str(ask_user(question))
                    ok = bool(answer.strip())
                except Exception as exc:  # make channel failure explicit, never silence it
                    answer = ("[USER CHANNEL ERROR: " + type(exc).__name__ +
                              ": " + str(exc) + "]")
                    ok = False
            sink.save_ask(res.turns, question, answer, ok)
            log.info("iter %d: ask user -> %s", res.iters,
                     "answered" if ok else "unavailable")
            if ask_user is not None and not ok:
                # A configured task channel that errors or returns an empty answer is
                # broken infrastructure.  Do not turn that absence into a synthetic
                # user answer, let the Actor guess, or invoke the sealed evaluator on
                # the resulting candidate.
                raise UserChannelInfrastructureError(
                    "configured user-response channel failed; task attempt is "
                    "infra-invalid and must be retried from a clean environment")
            user = ("USER RESPONSE to your question:\n" + answer +
                    "\n\nContinue the same task. Reply with one program, look, ask, "
                    "or done action.")
            continue

        # --- Done declaration ---
        if res.programs_run == 0 and not allow_noop_done:  # done before doing anything
            dones += 1
            if dones >= cfg.max_consec_done:
                res.status = "stalled"
                break
            user = premature_done_nudge or PREMATURE_DONE
            continue
        if getattr(cfg, "practice_mode", False):
            # P2 practice profile: done ENDS THE SESSION — no task checks, no
            # inspection (there is no task). claim->verify->curate runs OUTSIDE
            # the loop (run_explore.py) on the memory diff, pre-revert.
            _req = getattr(cfg, "practice_done_requires", "")
            if _req:
                # BRIEF Phase-0 gate (wenyi 07-31, after the t012 task-in-prep
                # bypass): the session's declared deliverable must EXIST before
                # done is accepted. Existence probe only — content never read.
                # Repeated refusals hit max_consec_done -> stalled -> fail-open.
                _probe = vm.run_command(
                    f"test -f {_req} && echo GATE_YES || echo GATE_NO")
                if "GATE_YES" not in _probe:
                    dones += 1
                    if dones >= cfg.max_consec_done:
                        res.status = "stalled"
                        break
                    user = (f"Not finished yet: {_req} does not exist. This "
                            "session's one required product is that file — "
                            "write it first (even a minimal one stating your "
                            "honest judgment, e.g. that your memory does not "
                            "apply, is valid), then declare done again.")
                    continue
            res.status = "done"
            log.info("iter %d: practice session declared complete", res.iters)
            break
        accepted, rejections, dropped = validate(
            turn.checks, cfg.max_checks, instruction=instruction,
            witness_gates=getattr(cfg, "done_witness_gates", False))
        results = run_checks(accepted, vm, timeout=cfg.check_timeout)
        sink.save_checks(res.turns, accepted, rejections + dropped, results)
        n_pass = sum(1 for r in results if r.passed)
        log.info("iter %d: done declared — %d/%d checks pass, %d rejected, %d dropped",
                 res.iters, n_pass, len(results), len(rejections), len(dropped))
        all_pass = bool(accepted) and all(r.passed for r in results)
        arbiter = all_pass and bool(rejections) and cfg.independent_verify
        # R3/v32 ONE-SHOT split gate: a done declared while ledgered visual
        # disagreements remain open bounces ONCE with the ledger + resolution
        # protocol — detection finally GATES, and it fires WITH TURNS LEFT (the
        # forensic finding: doubt surfaced at terminal accept changes nothing).
        # The second declaration passes regardless: cost bounded at one bounce.
        open_splits = [s for s in splits if s['open']]
        if (all_pass and (not rejections or arbiter) and open_splits
                and getattr(cfg, 'split_done_gate', False) and not split_gate_spent):
            # v34 FIX: the gate bounce is HARNESS-initiated guidance, not a failed
            # declaration — it must NOT count toward max_consec_done. The old
            # `dones += 1` + stall-check made the gate the third strike and KILLED a
            # healthy run (t063 s995: two check-fail dones, then a 9/9-pass done ->
            # gate fired -> stalled; the actor never saw the resolve-first message).
            # One-shot by construction (split_gate_spent), so no loop risk.
            split_gate_spent = True
            had_done_bounce, progs_since_bounce = True, 0
            res.inspections.append((res.turns, f"splitgate:{len(open_splits)}"))
            log.info("iter %d: done gated — %d unresolved visual split(s)",
                     res.iters, len(open_splits))
            user = unresolved_splits_message(open_splits)
            continue
        # Gate-friction fix: the falsifiability regex can misjudge a harmless read-only
        # probe as mutating; requiring zero rejections then blocks done FOREVER (a run
        # can stall out repeating "all pass, 1 rejected"). When every accepted check
        # passes and only the gate objects, the independent inspector ARBITRATES
        # instead — the regex keeps its veto on actually-run probes, not on done.
        # Fix D-2 (E2 autopsy, one-shot): a done whose accepted probes contain
        # no independent-channel witness (render/convert/screenshot/live-proc/
        # http) verifies records, not work — the losing runs' signature. Bounce
        # ONCE asking for one witness probe; second declaration passes
        # regardless (v34: harness guidance, not a failed done — no stall count).
        _WITNESS = ("soffice", "convert", "pdftoppm", "screenshot", "import -",
                    "xwd", "xdotool", "curl", "wget", "http", "ps ", "pgrep",
                    "qpdf", "pdftotext", "identify", "compare ")
        if (all_pass and (not rejections or arbiter) and accepted
                and getattr(cfg, "done_witness_gates", False)
                and not witness_gate_spent
                and not any(w in c.probe.lower() for c in accepted
                            for w in _WITNESS)):
            witness_gate_spent = True
            had_done_bounce, progs_since_bounce = True, 0
            res.inspections.append((res.turns, "witnessgate"))
            log.info("iter %d: done gated once — no independent-channel witness "
                     "among %d accepted checks", res.iters, len(accepted))
            user = ("Your checks all pass, but every one of them re-reads files "
                    "or state this run produced — none witnesses the work "
                    "through an independent channel. Add AT LEAST ONE check "
                    "that verifies the deliverable from outside your own "
                    "records: render/convert it and inspect the result, query "
                    "the live system, or measure the artifact with a tool that "
                    "did not build it. Then declare done again (this request "
                    "fires only once).")
            continue
        if all_pass and (not rejections or arbiter):
            if cfg.independent_verify:                 # v8: fresh-context inspection —
                if arbiter:
                    log.info("iter %d: %d rejected check(s), all accepted pass -> "
                             "inspector as arbiter", res.iters, len(rejections))
                evidence_leads = (
                    _actor_evidence_leads(accepted, results)
                    if (actor_evidence_requested
                        and getattr(cfg, "verifier_unverified_evidence", False))
                    else "")
                actor_evidence_requested = False
                verdict, findings = _inspect(
                    _with_splits(_surface_delta(
                        vm, baseline, excluded_paths=private_surface_paths)),
                    actor_evidence_leads=evidence_leads)
                res.inspections.append(
                    (res.turns, f"arbiter:{verdict}" if arbiter else verdict))
                log.info("iter %d: independent inspection -> %s", res.iters, verdict)
                if verdict == "evolve":
                    res.status = "evolve"
                    res.verifier_route = "EVOLVE"
                    res.verifier_report = str(findings)
                    break
                if verdict == "wrong":                 # CONFIRMED violation: must fix.
                    if _wrong_bounce(findings) == "evolve":
                        break
                    continue
                if verdict == "unverified":            # could-not-confirm ≠ wrong: a
                    unverified += 1                    # shallow inspection must not
                    # v21 (B): "could not confirm" only auto-accepts LATE (commit phase)
                    # and only if no confirmed-wrong preceded it; otherwise bounce for
                    # repair. The old rule accepted the 2nd unverified regardless of
                    # budget/wrongs — 47% of those runs scored <0.1 (self-truncation
                    # with budget left; wrong-streaks laundered to acceptance).
                    spent = time.time() - t0
                    commit = (res.iters >= max(1, int(budget * cfg.commit_frac))
                              or spent >= wall_cap * cfg.commit_frac)
                    # The unified loop requires an explicit HANDOFF route. A
                    # transport/infrastructure failure that produced no route may
                    # never be laundered into completion by the legacy late-budget
                    # could-not-confirm rule.
                    accept_uv = (not getattr(cfg, "verifier_evolve_route", False)
                                 and uv_accept_ok(
                                     cfg, unverified, commit, wrongs))
                    if not accept_uv:
                        if getattr(cfg, "verifier_unverified_evidence", False):
                            actor_evidence_requested = True
                        dones += 1
                        had_done_bounce, progs_since_bounce = True, 0   # R2
                        if dones >= cfg.max_consec_done:
                            res.status = "stalled"
                            break
                        user = unverified_message(
                            findings,
                            evidence_request=getattr(cfg, "verifier_continuity",
                                                     False))
                        continue
                    if _review_ok():
                        # R2/v31 trigger A: the unverified-accept rule is about to
                        # launder a could-not-confirm into done — the exact path that
                        # shipped confidently-wrong runs at 0 (t063; t032 x2 live).
                        # Route the accept decision through the escalation model.
                        st = _review(
                            "could not CONFIRM completion (could-not-confirm is not "
                            "wrong), and the budget-phase acceptance rule was about "
                            "to accept the declaration anyway", str(findings))
                        if st == "bounced":
                            continue
                        if st == "evolve":
                            break
                        if st == "stall":
                            res.status = "stalled"
                            break
                    else:
                        # R3: reviewer off/capped — keep the diagnostic marker (the
                        # forensic audit lived on these artifacts)
                        res.inspections.append((res.turns, "doubt:uv_accept"))
                    log.info("unverified x%d (commit=%s wrongs=%d) -> accepting",
                             unverified, commit, wrongs)
                if verdict == "pass":
                    # R2/v31 triggers B/C: a pass that rests on stated DOUBTS (material
                    # requirements judged without affirmative evidence — t021-class), or
                    # a HOLLOW re-declare (no program since the last bounced done:
                    # nothing on the machine can have changed, so either the checks got
                    # weaker or the first bounce was noise — let a second model decide).
                    doubts = str(getattr(findings, "doubts", "") or "").strip()
                    hollow = had_done_bounce and progs_since_bounce == 0
                    if (doubts or hollow) and _review_ok():
                        why = []
                        if doubts:
                            why.append("PASSED while listing requirements it could "
                                       "not affirmatively confirm")
                        if hollow:
                            why.append("accepted a declaration re-issued with NO "
                                       "program executed since the last bounced "
                                       "declaration (machine state cannot have "
                                       "changed in between)")
                        first = str(findings) + (f"\nITS STATED DOUBTS: {doubts}"
                                                 if doubts else "")
                        st = _review(" and ".join(why), first)
                        if st == "bounced":
                            continue
                        if st == "evolve":
                            break
                        if st == "stall":
                            res.status = "stalled"
                            break
                    elif doubts or hollow:
                        # R3: reviewer off/capped — diagnostic marker only
                        res.inspections.append(
                            (res.turns,
                             "doubt:" + ("pass_with_doubts" if doubts else "hollow")))
            if (cfg.independent_verify
                    and getattr(cfg, "verifier_evolve_route", False)):
                res.verifier_route = "HANDOFF"
                res.verifier_report = str(findings)
            res.checks_passed = len(results)
            res.status = "done"
            break
        dones += 1
        had_done_bounce, progs_since_bounce = True, 0      # R2: checks-fail bounce
        if dones >= cfg.max_consec_done:
            res.status = "stalled"
            log.info("stalled: %d consecutive failed done declarations", dones)
            break
        user = repair_message(results, rejections)

    if res.status == "stalled":
        # v18: one mechanical file-delta at the stall exit. "" is a PROOF that no
        # candidate deliverable was created or modified — the signal that separates
        # "banked work, continue it" from "doomed strategy draw, pivot" upstream.
        res.surface_delta = (_surface_delta(
            vm, baseline, excluded_paths=private_surface_paths)
            if baseline else None)

    if (res.status == "stalled" and cfg.independent_verify and not forced
            and time.time() - t0 <= wall_cap):
        # v11: a stall is EXACTLY the situation the forced inspection exists for —
        # work possibly complete (or well underway) but never declared/accepted; runs
        # were dying wedged with real state banked and nobody ever looked. Verdict is
        # recorded; "pass" upgrades the status. (No extension/re-entry: a wedged run
        # re-entering its attractor has no expected value — conservative by design.)
        forced = True
        verdict, findings = _inspect(
            _with_splits(res.surface_delta or ""))
        res.inspections.append((res.turns, f"stall:{verdict}"))
        log.info("stall-exit inspection -> %s", verdict)
        if verdict == "pass":
            res.status = "done"
            if getattr(cfg, "verifier_evolve_route", False):
                res.verifier_route = "HANDOFF"
                res.verifier_report = str(findings)
        elif verdict == "evolve":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(findings)
        elif verdict == "wrong" and _route_verified_failure(findings) == "EVOLVE":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(findings)

    res.wall_secs = time.time() - t0
    res.worklog = summary
    if agent_decided_stop and sink is not None:
        sink.save_transcript(system, history)
    return res, history


def _synthesize_worklog(history: list, cfg) -> str:
    """v36 (k3fit): a stall-resume with an EMPTY work log hands the fresh attempt
    nothing — short segments never trigger context folding, so everything learned is
    discarded and re-reconned (t032 s900). One summarizer call over the tail of the
    discarded history builds the handoff the folding would have built."""
    tail = history[-60:]
    rendered = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n" + _fold_input(m["content"])
        for m in tail)
    out = chat(cfg.model, SUMMARIZER_SYSTEM,
               "CURRENT LOG:\n(empty)\n\nOLDER TURNS TO FOLD IN:\n" + rendered
               + "\n\nOutput the updated log only.",
               max_tokens=cfg.max_tokens, temperature=0.0,
               reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    return _cap_worklog((out or "").strip(), cfg.worklog_max_chars)


def _strategy_review(history: list, worklog: str, cfg) -> str:
    """v18: one bounded self-post-mortem call (same actor model — single-model rule)
    over the stalled attempt's own log and final turns. Returns plain text ("" on a
    starved call; the pivot message stands on its own without it)."""
    from core.actor import STRATEGY_REVIEW_SYSTEM, strategy_review_ask
    tail = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n"
        + str(m["content"])[:1500] for m in history[-6:])
    out = chat(cfg.model, STRATEGY_REVIEW_SYSTEM,
               strategy_review_ask(worklog, tail), max_tokens=cfg.max_tokens,
               temperature=cfg.temperature, reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    return (out or "").strip()[:2500]


def escalation_cfg(cfg):
    """Build the fresh-segment config when a stuck run escalates to a DIFFERENT
    model (v21 model-escalation). PURE — no side effects, so it is unit-testable
    (this swap silently stripped RIH from every escalated GLM segment for years
    because it lived inline in a VM-bound loop and nothing could test it).

    Role-relative swaps happen here, and the distinction is the whole point:
      * vision_model -> role-relative sight (v39 native-coherence): when a NATIVE
        primary escalates to a text model, the demoted primary becomes its eyes.
        In the reverse direction, when the delegated eye itself becomes Actor, it
        uses native vision instead of delegating a redundant call to itself.
      * verifier_model -> restored to cross-model (xverify): if the configured
        Verifier becomes the escalated Actor, move the original Actor model into
        the Verifier seat instead of allowing self-review.
      * agentic_verifier_config -> the escalation role's dedicated config when set.
        A caller must keep its conversation in a verifier-model-specific session.
      * sampling/provider routing -> escalation-specific values. Sampling points and
        provider constraints are model-specific; a Z.AI route must never follow a
        GLM Actor into a K3 request.

    What is NOT reset: reasoning_in_history. RIH is model-AGNOSTIC — decisive for
    BOTH (v38 K3 + GLM ladder 07-28; DECISIONS_LEDGER rule "rih: always"). The old
    code reset it to False on the pre-ladder belief that RIH was "k3's protocol",
    silently stripping it from every escalated GLM segment (wenyi 08-12: "this is
    our bug"). It now inherits the base config's value."""
    from dataclasses import replace as _replace
    if (getattr(cfg, "vision_model", "")
            and cfg.escalation_model == cfg.vision_model):
        esc_vision = ""  # the former delegated eye is now the native-sighted Actor
    else:
        esc_vision = cfg.vision_model or cfg.model
    esc_verifier = cfg.verifier_model or cfg.model
    if (getattr(cfg, "escalation_verifier_cross_model", False)
            and esc_verifier == cfg.escalation_model):
        esc_verifier = cfg.model
    esc_agentic_verifier = (
        getattr(cfg, "escalation_agentic_verifier_config", "")
        or getattr(cfg, "agentic_verifier_config", ""))
    return _replace(
        cfg, model=cfg.escalation_model,
        vision_model=esc_vision,
        primary_temperature=getattr(
            cfg, "escalation_primary_temperature", -1.0),
        provider_order=getattr(cfg, "escalation_provider_order", ()),
        provider_allow_fallbacks=getattr(
            cfg, "escalation_provider_allow_fallbacks", True),
        provider_require_parameters=getattr(
            cfg, "escalation_provider_require_parameters", False),
        verifier_model=esc_verifier,
        agentic_verifier_config=esc_agentic_verifier)


def _verifier_session_identity(cfg) -> tuple[str, str]:
    """Conversation identity for a persistent Verifier Agent.

    A role/model switch must not feed one model another model's private reasoning
    history. Same-identity Actor resumes keep the exact existing session.
    """
    agentic = str(getattr(cfg, "agentic_verifier_config", "") or "")
    if agentic:
        return "agentic", agentic
    return "legacy", str(getattr(cfg, "verifier_model", "") or cfg.model)


def run_with_resume(instruction: str, vm, cfg, sink, opening_extra: str = "",
                    verifier_sessions: dict | None = None,
                    runtime_state: dict | None = None,
                    surface_baseline: dict | None = None,
                    verifier_failure_router=None, verifier_pass_router=None,
                    verifier_session_prepare=None, ask_user=None):
    """v15: a stall is a CONVERSATION disease, never a machine disease — every
    autopsied stall (repeat attractors, multi-program wedges, dry deaths) lived in a
    wedged decoder context while the machine state stayed sound. Fresh context is the
    founding principle of the inspector; this applies it to the actor: when an
    attempt stalls (after the stall-inspection fails to rescue) with budget left,
    discard the conversation, keep the machine, and start a fresh attempt on the
    remaining budget with a factual handoff. Bounded by cfg.max_resumes.

    ``verifier_session_prepare`` is an opt-in lifecycle boundary invoked with the
    active config and its model-private :class:`VerifierSession` before each Actor
    segment. It lets a unified caller establish candidate-blind orientation for a
    newly activated cross-model Verifier without exposing that policy to ordinary
    benchmark runs.

    v18 (strategy-diverse restart): the handoff now branches on the mechanical
    file-delta taken at the stall exit. Delta EMPTY = the attempt provably banked
    nothing — the draw itself was the failure (strategy lottery), so the fresh
    attempt gets a self-post-mortem plus an explicit directive to take a materially
    different approach. Delta non-empty (or unknown) = work is banked — the proven
    v15 continuation handoff is kept unchanged. Self-signals only: loop status,
    file mtimes, the model's own words — nothing grader-derived (Constraint #0)."""
    import os as _os

    from core.actor import handoff_message, pivot_handoff_message
    from core.trace import ArtifactSink

    # A unified-loop caller passes the same registry after an EVOLVE cycle so the
    # target Verifier Agent retains its own investigation context while the Actor
    # Agent and environment are refreshed. Ordinary benchmark runs omit it and get
    # the historical one-task-local lifetime.
    if verifier_sessions is None:
        verifier_sessions = {}
    verifier_sessions.setdefault(
        _verifier_session_identity(cfg), VerifierSession())
    verifier_session = verifier_sessions[_verifier_session_identity(cfg)]
    if verifier_session_prepare is not None:
        # Unified callers use this opt-in boundary to ensure that each independent
        # Verifier model identity has performed its own candidate-blind S0
        # orientation before it can inspect an Actor-shaped environment. Ordinary
        # harness callers omit it and retain their historical lifecycle exactly.
        verifier_session_prepare(cfg, verifier_session)
    res, history = run_attempt(instruction, vm, cfg, sink,
                               opening_extra=opening_extra,
                               verifier_session=verifier_session,
                               surface_baseline=surface_baseline,
                               verifier_failure_router=verifier_failure_router,
                               verifier_pass_router=verifier_pass_router,
                               ask_user=ask_user)
    active_history = history
    active_cfg = cfg

    def _record_resume_transport_pause(seconds: float) -> None:
        res.infra_pause_secs += seconds
        res.infra_pauses += 1

    while (res.status == "stalled" and res.resumes < cfg.max_resumes):
        iters_left = cfg.max_iters - res.iters
        wall_left = cfg.wall_clock_secs - res.wall_secs
        if iters_left < 10 or wall_left < 300:
            break
        n = res.resumes + 1
        wl = res.worklog if cfg.resume_with_worklog else ""
        if not wl and getattr(cfg, "resume_synthesize_worklog", False) and history:
            wl = _call_with_transport_pause(
                lambda: _synthesize_worklog(history, cfg), cfg,
                label="resume work-log synthesis",
                on_pause=_record_resume_transport_pause)  # never hand off empty-handed
        if cfg.strategy_pivot and res.surface_delta == "":   # v18: nothing banked -> pivot
            review = _call_with_transport_pause(
                lambda: _strategy_review(history, res.worklog, cfg), cfg,
                label="resume strategy review",
                on_pause=_record_resume_transport_pause)
            extra = pivot_handoff_message(review, wl)
            tag = f"pivot{n}"
        else:                                  # banked or unknown -> v15 continuation
            extra = handoff_message(wl)
            tag = f"resume{n}"
        extra += _memory_revisit(cfg, "recovery")
        log.info("stalled with budget left -> fresh-context %s "
                 "(%d iters, %.0fs wall remain)", tag, iters_left, wall_left)
        # v21 (model-escalation): a stuck run's fresh attempt runs on a DIFFERENT
        # model when configured. Escalation fires only on this rare stuck restart;
        # Constraint #0 holds because both segments receive the same sealed inputs.
        # The Actor becomes the escalation model, while full asymmetry moves the
        # original Actor model into the Verifier seat. A dedicated Agentic Verifier
        # config may also switch here; its conversation is intentionally separate
        # from the prior model's private Verifier history. Two different models must
        # still agree that the candidate is done.
        rcfg = cfg
        if cfg.escalation_model and cfg.escalation_model != cfg.model:
            rcfg = escalation_cfg(cfg)     # pure + unit-tested (see escalation_cfg)
            tag += ":esc"
            log.info("escalating stuck run -> %s (eyes=%s, verifier pinned to %s)",
                     cfg.escalation_model, rcfg.vision_model, rcfg.verifier_model)
        active_verifier_session = verifier_sessions.setdefault(
            _verifier_session_identity(rcfg), VerifierSession())
        if verifier_session_prepare is not None:
            verifier_session_prepare(rcfg, active_verifier_session)
        sink2 = ArtifactSink(_os.path.join(sink.root, f"resume{n}"))
        if tag.startswith("pivot"):
            sink2.save_summary(0, "PIVOT SELF-REVIEW:\n" + extra)
        r2, h2 = run_attempt(instruction, vm, rcfg, sink2, iters_budget=iters_left,
                             wall_budget=wall_left, opening_extra=extra,
                             verifier_session=active_verifier_session,
                             surface_baseline=surface_baseline,
                             verifier_failure_router=verifier_failure_router,
                             verifier_pass_router=verifier_pass_router,
                             ask_user=ask_user)
        active_history = h2
        active_cfg = rcfg
        r2.inspections = (res.inspections + [(res.turns, tag)]
                          + [(t + res.turns, v) for t, v in r2.inspections])
        r2.iters += res.iters
        r2.turns += res.turns
        r2.programs_run += res.programs_run
        r2.looks += res.looks
        r2.asks += res.asks
        r2.wall_secs += res.wall_secs
        r2.infra_pause_secs += res.infra_pause_secs
        r2.infra_pauses += res.infra_pauses
        r2.resumes = n
        res, history = r2, history + h2
    if runtime_state is not None:
        runtime_state.clear()
        runtime_state.update(
            active_history=active_history,
            active_cfg=active_cfg,
            active_verifier_identity=_verifier_session_identity(active_cfg),
        )
    return res, history
