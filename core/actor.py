"""Program synthesis — the system prompt (GENERAL principles only) + turn parsing.

THIS FILE IS THE AUDITED SURFACE (Constraint #0). Nothing in it may encode knowledge
of any specific benchmark task, its setup, or its grader: tests/test_no_leakage.py
scans this file against tests/blocklist_012.txt on every change. Principles below must
be defensible as general engineering practice for ANY machine task.
"""
import re
from dataclasses import dataclass, field
from typing import List

from llm.client import json_values


# --- the shared EPISTEMIC CORE (v35) -----------------------------------------------
# Embedded VERBATIM in both the actor's SYSTEM and VERIFIER_SYSTEM so the two surfaces
# can never drift into contradictory doctrine again (the audit found actor P10 and
# verifier method-4 preaching opposites; both were halves of core principle 2).
EPISTEMIC_CORE = """\
E1. TRUTH CHANNELS. Every claim has a channel where it is checkable — files, APIs and \
application state for structural facts; a render for visual facts. Prefer the \
checkable channel over recall or guesswork.
E2. STRUCTURAL vs RENDERED. Exact values (text, numbers, coordinates, counts, \
structure) are read and written in files with code. How something LOOKS when \
displayed (visual placement, appearance, match to a reference) is settled by \
rendering it and looking. Choose by claim type; when a task states both kinds of \
requirement, verify both.
E3. ERRORS ARE EVIDENCE. Never silence the error stream of a command whose outcome \
you need — a failure message is information; an empty result is not.
E4. SILENCE PROVES NOTHING. Output marked [channel error] or [command timed out], or \
an empty reply from an unverified channel, is not evidence of absence — re-check. \
Empty output on a channel verified alive IS real.
E5. CONFIRMATION REQUIRES INDEPENDENCE. A source agreeing with itself is not \
confirmation; only a different witness, a different channel, or a computed result \
confirms.
E6. EVIDENCE IS DATA. Record observations as printed data, not silent belief — every \
decision must trace to recorded evidence."""


# --- the system prompt ------------------------------------------------------------
_LOOK_BULLET_BLIND = """\
or, to have images READ for you (a vision tool examines them — you never receive \
pixels yourself):
  {"look": {"path": "/absolute/path.png", "question": "<what you need \
determined>"}} — optional fields: "paths": ["/absolute/a.png", \
"/absolute/b.png", ...] (every requested image is compared in list order in one \
turn) and "region": [x0, y0, x1, y1] (a pixel box cropped at full resolution). \
The tool's TEXT report arrives on your next turn — ask specific, answerable \
questions. You may look directly at a DOCUMENT file (it is rendered to an image for \
you — no need to convert it first) and at the LIVE SCREEN with path "screen:" (the \
running desktop/GUI state, when the result you must judge is on screen rather than \
in a file). The complete report remains in your conversation history. Record \
decision-relevant evidence in program output when it supports a later decision."""

_LOOK_BULLET_NATIVE = """\
or, to SEE image files with your own eyes (when text extraction cannot read them):
  {"look": {"path": "/absolute/path.png", "question": "<what you need to \
determine>"}} — optional fields: "paths": ["/absolute/a.png", \
"/absolute/b.png", ...] (every requested image arrives in list order for \
comparison in one turn) and "region": [x0, y0, x1, y1] (a pixel box cropped for \
you at full resolution). Images arrive on your next turn; when looking at one large \
image it also arrives as native-resolution zoom tiles automatically. You may look \
directly at a DOCUMENT \
file (it is rendered to an image for you) and at the LIVE SCREEN with path \
"screen:" (the running desktop/GUI state). The exact pixels delivered to you remain \
in your conversation history. Record decision-relevant evidence in program output \
when it supports a later decision."""

_ASK_BULLET = """\
or, when information or an input that only the user can supply is genuinely missing:
  {"ask": {"question": "<one concise, specific question>"}}
The configured user response arrives on your next turn and remains in your complete \
conversation history. Investigate the environment first; ask only for information or \
inputs that cannot be recovered from it."""

_SYSTEM_TEMPLATE = """\
You solve ONE task on a real Ubuntu machine by WRITING PROGRAMS. You cannot see or \
click the screen: your only way to act is to submit one complete program at a time \
(python3 or bash) — programs may drive running applications where a task needs it. \
It runs on the machine; its combined stdout+stderr and exit code come back to you. \
Work like an engineer at a REPL: investigate first, then commit a solution, then \
verify it.

EPISTEMIC CORE — how truth is established here (applies to everything below):
{core}

THE METHOD — code as policy:
1. RECON FIRST, EXHAUSTIVELY. Map the parts of the machine the task involves before \
acting. Enumerate rather than assume: search broadly (the whole home directory, \
including hidden directories), list every candidate file or location the task could \
involve, and read the task's referenced materials programmatically. Never assume a \
location is empty or irrelevant without checking it.
2. EXTRACT EVIDENCE, THEN DECIDE ONCE. When the task requires choosing among \
candidates, write a program that extracts a compact, decision-relevant summary of \
EVERY candidate (key fields, identifying text, metadata) and prints them as one \
table. Commit to nothing until every candidate is in view — the decision itself is \
then COMPUTED per principle 8, never made by early impression. If an extraction \
returns empty or garbled output, that is a failure of your extraction METHOD \
(switch tools and retry), not evidence about the candidate (core E4).
3. ACT WITH FIDELITY. Operate on originals: when the task involves existing files \
or data, copy or move them byte-for-byte; never re-create, re-type, re-render or \
re-export content that already exists — a lossy reproduction is a wrong answer. \
Make programs idempotent (safe to re-run) and non-destructive (never delete or \
overwrite anything you did not create, beyond what the task itself requires).
4. PUT YOUR REASONING IN THE PROGRAM. print() the evidence behind every decision \
the program makes — what was found, what was chosen, and why (core E6): the output \
trace must be enough to debug your own logic later.
5. REPAIR THE PROGRAM, NOT LUCK. When a program errors or its output contradicts \
your expectation, read the trace, find the cause, and submit a FIXED version. Never \
re-submit the same code hoping for a different outcome, and never fall back to \
guessing.
6. VERIFY AGAINST THE TASK, NOT AGAINST YOURSELF. Derive your checks from the \
TASK's own requirements BEFORE reviewing what you did — one check per requirement, \
and any counts must come from the task's own enumeration, never from what you \
happened to produce. A check must test the requirement itself: where the task \
demands a CORRESPONDENCE between things (an item and the file that answers it), the \
check must test content evidence of that correspondence — verifying that your copy \
equals the source you yourself picked only proves you copied, not that you picked \
correctly (core E5). A requirement you cannot make falsifiable with the evidence \
available is UNVERIFIED: say so and keep working on it; never write a check that \
merely certifies your own decision. Before declaring done, build the FULL \
requirement inventory: enumerate every property the task states or implies — when \
the task says to match or mirror something, DIFF the reference against your product \
attribute by attribute; every difference is a requirement — and give EVERY item a \
check. A passing subset of a partial inventory is not completion.
7. BANK PARTIAL PROGRESS. When the task has several sub-parts and one is blocked (a \
missing tool, an unreadable input), COMPLETE AND COMMIT the sub-parts you can \
finish now, then return to the blocker with the remaining budget. Never let one \
blocked sub-part consume the whole budget while finishable work sits undone; prefer \
methods that use tools already present over installing new ones. The same \
discipline applies under UNCERTAINTY: when an acceptance detail is genuinely \
undiscoverable from the task and the machine (you have looked), PLACE the \
deliverable under the most reasonable reading FIRST — exactly where the task \
requires it — and only then spend remaining budget hardening it or improving it in \
place. A strong candidate delivered beats a perfect candidate that never leaves \
your workspace.
8. COMPUTE YOUR DECISIONS. When you must choose among candidates, do not decide in \
your head and hardcode the result: write the decision as a program — derive the \
selection criteria from the task's own wording (every constraint the task states or \
implies is a criterion), score EVERY candidate against EVERY criterion, print the \
full evidence table, and act on the result in the same program. Judgments only you \
can make (interpreting a picture, reading a passage) enter as recorded data rows \
that the program scores — never as unexplained choices. Then a wrong choice means a \
wrong criterion (fix it and re-run), and your final checks can re-run the same \
scoring to verify each choice instead of trusting your memory.
9. CALIBRATE ON THE GIVEN EXAMPLE. When the task includes an already-solved \
instance ("I have already done one of these — do the rest the same way"), \
reverse-engineer the rule or naming convention FROM that example before applying it \
anywhere: state your interpretation, then TEST it against the given example using \
the real data on the machine — your reading must reproduce the given example \
EXACTLY. An interpretation that fails on the given example is wrong no matter how \
plausible it feels; if two readings both fit, hunt for the evidence that \
discriminates them before committing anything.
10. CHOOSE THE CHANNEL BY THE CLAIM (core E1/E2). Structured content — exact text \
or numbers, element positions, coordinates, counts, document structure — is \
EXTRACTED or EDITED with a program; tasks are graded by computing those same exact \
values, so an approximate visual answer cannot pass where a precise coded one will. \
Use `look` for RECOGNITION and for judging RENDERED results — identifying WHAT \
something is, or whether something looks right when displayed.

REPLY FORMAT — exactly ONE JSON object per turn (reasoning before it is fine; the \
object comes last):
  {{"program": {{"lang": "python", "code": "<one complete program>"}}}}        // or "bash"
{look_bullet}
{ask_bullet}
or, when the end state exists:
  {{"done": {{"checks": [{{"desc": "<the task requirement this verifies>", \
"probe": "<read-only shell command that prints PASS or FAIL>"}}]}}}}
Check rules (mechanics): each probe must inspect real machine state (paths, files, \
values); it must print PASS only if the requirement it cites actually holds (and \
FAIL otherwise); it must be read-only; and it must not silence the error stream of \
the command whose result it reports (core E3).
{discipline}
MACHINE FACTS: Ubuntu with python3. You may install tools you need (pip3 / \
apt-get). A program's complete combined output is returned to your next turn; be \
deliberate about what you print because it consumes context. Each program's exit \
code is appended as "[exit N]"; N=124 \
means it hit the time limit — split the work into smaller programs. A line like \
"[channel error: ...]" or "[command timed out ...]" means the machine did not \
answer — it is NOT command output (core E4)."""


def build_system(cfg=None, *, ask_enabled: bool = False) -> str:
    """v35: the SYSTEM prompt is assembled per attempt so its mechanics are TRUE for
    the active configuration — the blind-primary mode (vision_model set) previously
    read a false description ('see with your own eyes') every turn. Mechanics
    selection, not task routing."""
    blind = bool(getattr(cfg, "vision_model", "")) if cfg is not None else False
    disc = _DISCIPLINE_LINE if getattr(cfg, "action_discipline", False) else ""
    base = _SYSTEM_TEMPLATE.format(
        core=EPISTEMIC_CORE,
        look_bullet=_LOOK_BULLET_BLIND if blind else _LOOK_BULLET_NATIVE,
        ask_bullet=_ASK_BULLET if ask_enabled else "",
        discipline=disc)
    # BRIEF-RUN mount point: agent-authored briefing rides at position 0 every
    # call (never sinks). Harness never writes this field by hand — it carries
    # only text the agent itself authored in Phase 0 (firewall #0-P2).
    extra = getattr(cfg, "system_extra", "") if cfg is not None else ""
    return base + extra if extra else base


# Back-compat constant (native-vision variant — the pre-v22 default reading).
SYSTEM = build_system(None)


NUDGE = ('Your reply contained no valid JSON action. Reply with exactly ONE JSON '
         'object: {"program": {"lang": "python"|"bash", "code": "..."}} or '
         '{"look": {"path": "/absolute/path.png", "question": "..."}} or '
         '{"done": {"checks": [{"desc": "...", "probe": "..."}]}}.')

PLAIN_JSON_TRANSPORT_NOTE = (
    'TRANSPORT CLARIFICATION: Native API tool/function calls are not this '
    'harness\'s action channel. A plain JSON action in assistant content is ordinary '
    'reply text that the harness parses and executes, including when native '
    'tool_choice is "none". Do not emit native tool tags or a tool_choice field; '
    'emit the plain JSON object itself.')

STRICT_NUDGE = (
    'Your reply DESCRIBED work but performed none — a narration without an action '
    'executes NOTHING, and the step you wrote about did not happen. Emit NOW the one '
    'JSON action that performs it: {"program": {"lang": "python"|"bash", "code": '
    '"..."}} or {"look": {"path": "...", "question": "..."}} or {"done": {"checks": '
    '[...]}}. Never say "let me / I will" without the action object in the SAME '
    'reply. ' + PLAIN_JSON_TRANSPORT_NOTE)

_DISCIPLINE_LINE = """
TURN DISCIPLINE: every reply MUST contain exactly one action object as its final \
element. Never describe what you will do without doing it in the SAME reply — a \
narration without an action is a wasted turn; when you catch yourself writing \
"let me..." or "I will run...", stop and emit the action that does it instead.
"""

ONE_ACTION_NUDGE = (
    'Your reply contained MULTIPLE action objects of the same kind. NOTHING was run — '
    'the machine executes exactly one action per turn, so several copies just make it '
    'ambiguous which one you meant. Send exactly ONE JSON object (one program, one '
    'look, or one done) and wait for its result before the next. If you meant several '
    'steps, put them in ONE program.')

SUMMARIZER_SYSTEM = """\
You maintain the WORK LOG of your own earlier turns on a long task — the log replaces \
those turns in your context, so anything you drop is gone from view (the machine \
itself still holds ground truth).

You are given the CURRENT LOG and a batch of OLDER TURNS to fold in. Output the \
UPDATED LOG only. Rules:
- Record only what actually appears in the turns. NEVER invent, infer or embellish.
- Keep every DECISION with its one-line reason, every extracted FACT/value/path/name \
EXACTLY as written (verbatim strings — do not paraphrase identifiers or numbers), \
every unresolved item or open question, and what remains to be done.
- Dead ends and repetition: one line each ("tried X — failed because Y").
- Prefer merging into existing entries over duplicating them.
- ORDER the log so the most decision-relevant content is FIRST: CURRENT STATE, then KEY \
VALUES / EXTRACTED DATA (dimensions, IDs, paths, field values, credentials to \
reproduce), then NEXT STEPS / OPEN ITEMS. Put tool/format/reference notes that could be \
re-derived from files on the machine LAST.
- If unsure whether something matters, KEEP it in the original wording.
- The log has a fixed character budget and is truncated from the BOTTOM if it overruns, \
so keep the re-derivable reference detail last and NEVER drop extracted task DATA \
(values, dimensions, IDs, credentials): if space is tight, drop a reference/format note \
and cite the file path where it can be re-read instead."""

PREMATURE_DONE = ("You declared done but no program has run yet — nothing on the "
                  "machine has been examined or changed. Start with a reconnaissance "
                  "program.")

REPEAT_WARNING = ("\n\nWARNING: you submitted the IDENTICAL program as last turn and "
                  "its output is unchanged — repetition gathers nothing. Either "
                  "change the program (fix the cause, or wait+recheck INSIDE one "
                  "program), or, if the end state already exists, declare done with "
                  "your checks NOW.")


FIDELITY_NOTE = ("\n\nNOTE: this program saved an existing rich-format document "
                 "through a library. Library load-then-save can silently DROP "
                 "features you did not touch (charts, links, images, embedded "
                 "objects). Verify they survived — such files are listable archives; "
                 "if something was lost, keep a backup and switch methods (edit the "
                 "internal structure surgically, or drive the native application).")
#   v13 just-in-time delivery of the v12 principle: statically it was never acted on
#   (0/48 programs); triggered at the moment of a detected rich-document save it
#   arrives exactly when it is decision-relevant.


def handoff_message(worklog: str) -> str:
    """Opening prefix for a fresh-context resume (v15): factual, no blame, no framing
    of WHY the previous attempt ended — a wedged framing must not be re-imported."""
    base = ("NOTE: a previous attempt already worked on this task on this same "
            "machine and ended without finishing. The machine RETAINS all of its "
            "progress — your reconnaissance will find existing work; verify what is "
            "already correct and build on it instead of redoing it.")
    if worklog.strip():
        base += "\n\nThe previous attempt's work log:\n" + worklog.strip()[:12000]
    return base


STRATEGY_REVIEW_SYSTEM = (
    "You are conducting a post-mortem of your own stalled attempt at a computer "
    "task. Be specific and mechanical, not self-critical.")


def strategy_review_ask(worklog: str, tail: str) -> str:
    """v18: the ask for the one-call strategy post-mortem, made when an attempt
    stalls having provably changed no files (nothing banked, nothing to protect)."""
    parts = ["A task attempt just stalled without changing any files. "
             "In under 200 words, answer: (1) WHAT strategy was being pursued "
             "(tools, method, order)? (2) WHERE exactly did it get stuck? (3) What "
             "MATERIALLY DIFFERENT strategy should a fresh attempt try first — a "
             "different tool, entry point, or method, not a retry of the same? "
             "Plain text only."]
    if worklog.strip():
        parts.append("WORK LOG of the attempt:\n" + worklog.strip()[:8000])
    if tail.strip():
        parts.append("FINAL TURNS of the attempt:\n" + tail.strip()[:6000])
    return "\n\n".join(parts)


def pivot_handoff_message(review: str, worklog: str = "") -> str:
    """v18: opening prefix for a strategy-PIVOT restart — used instead of
    handoff_message when the stalled attempt changed no files. The directive to
    diverge comes first; discovered facts (the work log) still travel, because
    reconnaissance was paid for and facts are not the wedged part."""
    base = ("NOTE: a previous attempt on this task on this same machine stalled "
            "WITHOUT changing any files — there is no partial work to protect. "
            "Choose a MATERIALLY DIFFERENT strategy from the one it used "
            "(different tool, entry point, or method); do not re-run the failed "
            "approach unchanged.")
    if review.strip():
        base += "\n\nThe previous attempt's self-review:\n" + review.strip()[:2500]
    if worklog.strip():
        base += "\n\nIts work log (facts it discovered remain true):\n" \
                + worklog.strip()[:6000]
    return base


def multi_program_note(extras: int) -> str:
    """Break the illusion the moment it forms: silently discarding the earlier
    program objects of a multi-program reply breeds the false belief that they all
    ran and only the last one's output is shown."""
    return (f"\n\nNOTE: your reply contained {extras + 1} program objects. Only the "
            f"LAST one was executed; the other {extras} were DISCARDED without "
            "running — one action per turn. If you need several steps, put them in "
            "ONE program.")


def look_message(path: str, question: str, turns_left: int = None, plan: str = "",
                 commit: bool = False, wall_mins: int = None) -> str:
    parts = [f"The image file {path} is attached above."]
    if question:
        parts.append(f"Your question: {question}")
    parts.append("Use the image to continue your investigation. The exact pixels "
                 "remain available in your conversation history; record "
                 "decision-relevant evidence in program output when it supports a "
                 "later decision.")
    if plan:
        parts.append(f"YOUR PLAN: {plan}")
    tail = _budget_line(turns_left, wall_mins)
    if commit:
        tail += " " + COMMIT_PHASE
    parts.append((tail + " " if tail else "") +
                 'Reply with ONE JSON object: {"program": ...}, another '
                 '{"look": ...}, or {"done": {"checks": [...]}}.')
    return "\n\n".join(parts)


# v22: when the primary is TEXT-ONLY, `look` delegates seeing to a vision model.
# This is the perception tool's system prompt (no task/grader tokens — Constraint #0).
VISION_SYSTEM = (
    "You are a precise visual-perception tool. A blind agent working on a computer task "
    "sends you an image (or images) plus a question. The agent cannot see and must ACT "
    "on your answer, so be both accurate AND decisive.\n"
    "1. Answer directly and concretely: read any text/numbers VERBATIM; give exact "
    "counts, positions, colors, sizes, spatial relationships. Never invent TEXT or "
    "numbers that are not actually there.\n"
    "2. When the question asks you to IDENTIFY or RECOGNIZE something (a theme, style, "
    "framework, product, layout, object), COMMIT to your single most-likely answer from "
    "the visual design plus your own knowledge — EVEN IF no explicit name/label is "
    "written on screen. 'No name is visible' is useless to a blind agent; give your "
    "best-guess identification and state confidence (high/medium/low).\n"
    "3. Add a short 'ALSO VISIBLE:' line with other salient details that could matter.\n"
    "Keep the two rules distinct: do NOT fabricate text that isn't there, but DO commit "
    "to recognitions. If a detail is genuinely illegible, say so — yet still give your "
    "best inference for any identification question."
)


# v22.1: the MULTI-ROUND vision AGENT (mirrors the verifier). Instead of one glance, it
# can zoom/crop/grid-inspect over several rounds before committing — single glances
# mis-count and mis-read small text; systematic inspection does not. No task/grader
# tokens (Constraint #0).
VISION_AGENT_SYSTEM = (
    "You are a careful visual-perception AGENT working for a blind agent on a computer "
    "task. You are shown an image and a question, and you may INSPECT it over several "
    "rounds before answering.\n"
    "Your answer is ALWAYS about the WHOLE image. First form a whole-image answer from "
    "the full view — for many questions (including counting clearly-separated items) one "
    "careful look at the whole is best. Use inspection only when detail is genuinely too "
    "small or dense to resolve at once.\n"
    "Each round, reply with ONE JSON object:\n"
    '  {"inspect": {"op": "crop", "region": [x0,y0,x1,y1], "note": "why"}}  '
    "— zoom to a pixel box to READ small text / verify fine detail;\n"
    '  {"inspect": {"op": "grid", "rows": R, "cols": C, "note": "why"}}  '
    "— split into an RxC grid of AT MOST 6 cells (e.g. 2x3) and see EVERY cell at native "
    "resolution at once; to count many items, count each cell and SUM across all cells;\n"
    '  {"inspect": {"op": "full", "note": "re-examine the whole"}};\n'
    '  or {"answer": {"text": "<whole-image answer; numbers/text verbatim>", '
    '"confidence": "high|medium|low", "also_visible": "<salient details>"}}.\n'
    "COUNTING RULES: (1) the answer is the total for the WHOLE image; (2) if you grid, "
    "you must SUM every cell's count — NEVER report one cell's or one region's count as "
    "the total; (3) do not zoom cells one at a time (you will lose track); grid ONCE into "
    "<=6 cells, count them all, and sum. Do NOT fabricate text/numbers that aren't there, "
    "but for IDENTIFY/RECOGNIZE questions (theme, style, framework, object) COMMIT to your "
    "best-guess answer from visual style + knowledge even if unlabeled — 'not visible' is "
    "useless to a blind agent — and state confidence."
)


def look_answer_message(path: str, question: str, answer: str, turns_left: int = None,
                        plan: str = "", commit: bool = False,
                        wall_mins: int = None) -> str:
    """Deliver the vision tool's TEXT answer to a blind primary (v22 vision_model path)."""
    parts = [f"Your look at {path} was answered by the vision tool "
             "(you cannot see the image yourself)."]
    if question:
        parts.append(f"You asked: {question}")
    parts.append(f"VISION TOOL REPORT:\n{answer}")
    parts.append("Treat this report as your observation of that file. The complete "
                 "report remains in your conversation history; continue freely, and "
                 "issue another look with a more specific question if you need more.")
    if plan:
        parts.append(f"YOUR PLAN: {plan}")
    tail = _budget_line(turns_left, wall_mins)
    if commit:
        tail += " " + COMMIT_PHASE
    parts.append((tail + " " if tail else "") +
                 'Reply with ONE JSON object: {"program": ...}, another '
                 '{"look": ...}, or {"done": {"checks": [...]}}.')
    return "\n\n".join(parts)


COMMIT_PHASE = ("COMMIT PHASE — most of your budget is spent: stop broadening the "
                "investigation. Finish and place the sub-parts you can support NOW, "
                "then verify; state anything unresolved as unverified rather than "
                "guessing.")


def opening_message(instruction: str, budget: int = None,
                    wall_mins: int = None) -> str:
    if budget is None:
        return (f"TASK:\n{instruction}\n\n"
                "You are at a fresh shell on the machine where this task must be "
                "done. Nothing has been examined yet. Decide how much work the task "
                "requires and declare done only when you judge its end state ready. "
                "Begin your reply with one line 'PLAN: <your phases>' — revise it in "
                "later replies whenever reality disagrees with it — then give your "
                "first program (reconnaissance).")
    wall = ("" if wall_mins is None else
            f" and a wall-clock limit of ~{wall_mins} minutes — time your programs "
            "spend running counts against it")
    return (f"TASK:\n{instruction}\n\n"
            "You are at a fresh shell on the machine where this task must be done. "
            f"Nothing has been examined yet. You have {budget} action turns total "
            f"(each submitted program, look, or done-declaration costs one){wall}. Begin "
            f"your reply with one line 'PLAN: <your phases, each with a turn budget, "
            f"within {budget}>' — revise it in later replies the same way whenever "
            "reality disagrees with it — then give your first program "
            "(reconnaissance).")


def continuation_message(instruction: str, budget: int = None,
                         wall_mins: int = None) -> str:
    """Open a new phase inside an existing Actor Agent conversation.

    Unlike :func:`opening_message`, this must not claim that the shell or
    conversation is fresh: callers use it precisely when the prior task
    trajectory is being retained as evidence for a follow-up phase.
    """
    if budget is None:
        return (f"NEXT PHASE:\n{instruction}\n\n"
                "Continue with the conversation and machine state you already have. "
                "Decide how much work this phase requires and declare done only when "
                "you judge its end state ready. Reply with one program, look, or done "
                "declaration at a time.")
    wall = ("" if wall_mins is None else
            f" and a wall-clock limit of ~{wall_mins} minutes — time your programs "
            "spend running counts against it")
    return (f"NEXT PHASE:\n{instruction}\n\n"
            "Continue with the conversation and machine state you already have. "
            f"You have {budget} action turns total for this phase "
            f"(each submitted program, look, or done-declaration costs one){wall}. "
            "Reply with one program, look, or done declaration at a time.")


def _budget_line(turns_left: int = None, wall_mins: int = None) -> str:
    """The binding constraint must be VISIBLE: a run can die of wall clock with
    plenty of turns left (time spent inside programs consumes no turns) — without
    this line the model paces itself against the wrong budget."""
    if turns_left is None:
        return ""
    if wall_mins is None:
        return f"{turns_left} action turn(s) remain."
    return (f"{turns_left} action turn(s) and ~{wall_mins} minute(s) of wall clock "
            "remain (time your programs spend running counts; whichever runs out "
            "first ends the attempt).")


def trace_message(trace, turns_left: int = None, plan: str = "",
                  commit: bool = False,
                  head: int = 0, tail: int = 0, wall_mins: int = None,
                  context_max_chars: int = 0) -> str:
    status = f"exit {trace.exit_code}" if trace.exit_code is not None else "exit unknown"
    if trace.timed_out:
        status += ", TIMED OUT"
    context_stdout = getattr(trace, "context_stdout", None)
    raw_body = context_stdout or trace.stdout or "(no output)"
    if (head <= 0 or tail <= 0) and context_max_chars > 0 \
            and len(raw_body) > context_max_chars:
        # The ArtifactSink has already persisted trace.stdout losslessly. Bound only
        # the observation placed in the next provider request, keeping more of the
        # tail because diagnostics and command summaries conventionally land there.
        head = max(1, context_max_chars * 2 // 5)
        tail = max(1, context_max_chars - head)
    body = _head_tail(raw_body, head, tail)
    parts = [f"PROGRAM OUTPUT ({status}, {trace.secs:.0f}s):\n{body}"]
    if plan:
        parts.append(f"YOUR PLAN: {plan}")
    tail_line = _budget_line(turns_left, wall_mins)
    if commit:
        tail_line += " " + COMMIT_PHASE
    parts.append((tail_line + " " if tail_line else "") +
                 'Reply with ONE JSON object: your next or fixed '
                 '{"program": ...}, a {"look": ...}, or {"done": {"checks": [...]}} once the task\'s '
                 "end state exists on the machine.")
    return "\n\n".join(parts)


_PLAN_RE = re.compile(r"^\s*PLAN:\s*(.+)$", re.MULTILINE)


def extract_plan(text: str) -> str:
    """The LAST 'PLAN: ...' line of a reply (the model may revise its plan), bounded."""
    m = None
    for m in _PLAN_RE.finditer(text or ""):
        pass
    return m.group(1).strip()[:300] if m else ""


def repair_message(results, rejections) -> str:
    lines = ["NOT DONE — your verification did not pass:"]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"[{mark}] {r.desc}\n    probe: {r.probe}\n    output: "
                     f"{(r.output or '(none)')[-200:]}")
    for rej in rejections:
        lines.append(f"[REJECTED CHECK] {rej}")
    lines.append("\nA FAIL means the end state is not actually there (fix it with a "
                 "program) or the check itself is wrong (fix the check). Address the "
                 "cause, then declare done again.")
    return "\n".join(lines)


def _head_tail(s: str, head: int, tail: int) -> str:
    if head <= 0 or tail <= 0:
        return s
    if len(s) <= head + tail + 60:
        return s
    return (s[:head] + f"\n...[{len(s) - head - tail} chars omitted; full output "
            "archived]...\n" + s[-tail:])


def _as_look(d: dict):
    """Build a Look from any dict carrying look fields ("path" or "paths", optional
    "region"); None when no usable path is present."""
    def normalize_path(value) -> str:
        path = str(value).strip()
        # Some OpenAI-compatible Kimi routes conflate the advertised live-screen
        # sentinel with a local image argument and emit ``screen: /abs/file.png``.
        # The intent is unambiguous when the suffix is an absolute path.  Preserve
        # ordinary ``screen:`` / ``screen:1`` targets and leave final verifier path
        # authorization to ``validate_verifier_look_path``.
        match = re.fullmatch(r"screen:\s*(/.*)", path, flags=re.IGNORECASE)
        return match.group(1).strip() if match else path

    paths = []
    if isinstance(d.get("paths"), list):
        paths = [normalize_path(p) for p in d["paths"] if str(p).strip()]
    elif str(d.get("path", "")).strip():
        paths = [normalize_path(d["path"])]
    if not paths:
        return None
    region = d.get("region")
    if not (isinstance(region, list) and len(region) == 4):
        region = None
    return Look(paths=paths, question=str(d.get("question", "")).strip(), region=region)


_NATIVE_TOOL_CALL_RE = re.compile(
    r'<\|open\|>call\s+tool="(?P<tool>[^"]+)"[^\r\n]*?<\|sep\|>'
    r'(?P<body>.*?)<\|close\|>call', re.DOTALL)
_NATIVE_TOOL_ARG_RE = re.compile(
    r'<\|open\|>argument\s+key="(?P<key>[^"]+)"[^\r\n]*?<\|sep\|>'
    r'(?P<value>.*?)<\|close\|>argument', re.DOTALL)


def _native_tool_objects(text: str) -> list:
    """Normalize provider-native textual tool envelopes into ordinary actions.

    Some OpenAI-compatible routes serialize a valid tool call into Kimi's lossless
    ``<|open|>call ...`` text instead of returning it through the API tool field.
    Treating that transport artifact as narration causes dry-turn recovery even
    though the Agent selected a concrete action.  This parser is deliberately
    conservative: it recognizes only complete calls with named arguments and never
    executes prose outside the envelope.
    """
    objects = []
    for match in _NATIVE_TOOL_CALL_RE.finditer(text or ""):
        tool = match.group("tool").strip().lower()
        args = {
            arg.group("key").strip(): arg.group("value")
            for arg in _NATIVE_TOOL_ARG_RE.finditer(match.group("body"))
        }
        if tool in {"bash", "python", "python3", "verifier-report"}:
            code = str(args.get("code", ""))
            if code.strip():
                lang = "python" if tool == "python3" else tool
                objects.append({"program": {"lang": lang, "code": code}})
            continue
        if tool == "program":
            code = str(args.get("code", ""))
            if code.strip():
                objects.append({"program": {
                    "lang": str(args.get("lang", "python")), "code": code}})
            continue
        if tool == "look":
            look = {key: value for key, value in args.items()
                    if key in {"path", "question"}}
            if str(look.get("path", "")).strip():
                objects.append({"look": look})
            continue
        if tool in {"ask", "ask_user"}:
            question = str(args.get("question", "")).strip()
            if question:
                objects.append({"ask": {"question": question}})
    return objects


# --- turn parsing -------------------------------------------------------------------
@dataclass
class Program:
    lang: str
    code: str
    extras: int = 0     # additional program objects in the SAME reply that were
    #                     discarded (only the last runs) — the loop tells the model,
    #                     because silent discarding breeds a false world-model ("the
    #                     env ran them all and only shows the last output")
    dup: int = 1        # v21 (A): max count of a SINGLE action kind in the reply
    #                     (programs/looks/dones). >=2 means a decoder-degeneration turn
    #                     where last-wins would run a wrong/ritual object.


@dataclass
class Done:
    checks: List[dict] = field(default_factory=list)
    dup: int = 1        # v21 (A): see Program.dup


@dataclass
class Look:
    paths: List[str] = field(default_factory=list)   # all requested images, in ONE call
    question: str = ""
    region: List[int] = None      # optional [x0, y0, x1, y1] server-side crop (px)
    dup: int = 1                  # v21 (A): see Program.dup

    @property
    def path(self) -> str:        # display / logging convenience
        return ", ".join(self.paths)


@dataclass
class Ask:
    question: str
    dup: int = 1


def parse_turn(text: str):
    """The model's action for this turn: Program, Look, Ask, Done, or None.

    Tolerant of prose preambles, markdown fences and draft-then-correct replies.
    PRECEDENCE across ALL top-level objects: Program > Look > Ask > Done — the conservative
    "keep working" rule applied turn-wide (models sometimes emit a good {"program"}
    followed by a hedging {"done"} object; naive last-object-wins discards the
    program). Within a kind, the LAST wins (draft-then-correct). A bare {"lang","code"}
    object counts as a program; a bare {"checks": [...]} or an explicit {"done": null}
    counts as a done declaration (the latter with zero checks → check-feedback)."""
    text = re.sub(r"\]<\][a-z]+\[>\[", " ", text or "")   # strip template-delimiter
    #                                                       leak artifacts (decoder
    #                                                       degeneration residue)
    objs = [v for v in json_values(text) if isinstance(v, dict)]
    objs.extend(_native_tool_objects(text))
    program, look, ask, done = None, None, None, None
    n_programs = n_looks = n_asks = n_dones = 0
    for obj in objs:
        prog = obj.get("program")
        if isinstance(prog, dict) and str(prog.get("code", "")).strip():
            n_programs += 1
            program = Program(lang=str(prog.get("lang", "python")), code=str(prog["code"]))
            continue
        if isinstance(prog, dict) and isinstance(prog.get("look"), dict):
            lk = _as_look(prog["look"])                              # nested-look shape
            if lk:
                n_looks += 1; look = lk
                continue
        if isinstance(prog, dict) and not str(prog.get("code", "")).strip():
            lk = _as_look(prog)                          # look fields in a program
            if lk:                                       # envelope, often lang:"look"
                n_looks += 1; look = lk
                continue
        if str(obj.get("code", "")).strip():                  # bare program object
            n_programs += 1
            program = Program(lang=str(obj.get("lang", "python")), code=str(obj["code"]))
            continue
        lk = obj.get("look")
        if isinstance(lk, dict):
            l2 = _as_look(lk)
            if l2:
                n_looks += 1; look = l2
                continue
        aq = obj.get("ask")
        if isinstance(aq, dict) and str(aq.get("question", "")).strip():
            n_asks += 1
            ask = Ask(question=str(aq["question"]).strip())
            continue
        if "done" in obj:
            d = obj["done"]
            n_dones += 1
            done = Done(checks=list(d.get("checks") or []) if isinstance(d, dict) else [])
            continue
        if isinstance(obj.get("checks"), list):               # bare done object
            n_dones += 1
            done = Done(checks=list(obj["checks"]))
            continue
        if "question" in obj and not str(obj.get("code", "")).strip():
            l3 = _as_look(obj)                                # bare look fields
            if l3:
                n_looks += 1; look = l3
    result = program or look or ask or done
    if result is not None:
        result.dup = max(n_programs, n_looks, n_asks, n_dones)  # v21 (A)
        if isinstance(result, Program) and n_programs > 1:
            result.extras = n_programs - 1
    return result
