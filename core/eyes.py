"""forge eyes — R3 "two witnesses + judge" (v32, 2026-07-15; supersedes the v30 vote).

FORENSIC BASIS (results/r1r2_effectiveness.md, 12-run deep-read): the v30 ensemble's
VOTING never beat a single read on a decisive perception (0/12) and its merged
consensus twice manufactured a wrong answer (t101 averaged a correct-low count into a
wrong compromise; t049's consensus was confidently wrong). The only component with a
demonstrated score win was the split-triggered NUDGE that routed the actor to a
ground-truth channel (t032: enumerate catalog -> side-by-side verify; t049: the
actor's own pixel-count code). Conclusion: models out-voting models does not create
truth — a checkable channel does.

v32 design:
- 2 independent witnesses (1 canonical + 1 hot skeptic). Enough to DETECT
  disagreement; the 3rd/5th votes bought nothing and multiplied cost.
- A JUDGE (small text call) classifies agreement only: full | partial | split.
  Comparing two texts is radically easier than reading pixels — the judge is never
  asked to decide what is true, only whether two answers are the same.
- On full/partial: the judge's consensus text is delivered (as before).
- On SPLIT: NO merged answer, ever. The harness delivers BOTH readings VERBATIM plus
  a directive resolution protocol (enumerate / compute / read-the-source). A
  disagreement is evidence about uncertainty; merging destroys it, averaging launders
  it into fake confidence.
- The report carries a machine-readable .split flag (str subclass) so the loop can
  ledger unresolved splits and gate done-declarations on them (R3 gate, loop.py).

Design rules preserved from v30: uniform for every look (no importance classifier);
no task/grader token anywhere (audited surface, tests/test_no_leakage.py); eyes
failing must never crash the run; infra failure must never leave the actor worse off
than a single read.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from core.imagery import prepare_look_images
from llm.client import LLMTransportError, chat

log = logging.getLogger("forge.eyes")

DEFAULT_Q = ("Describe everything visible in this image that could matter to a "
             "computer task, reading any text verbatim.")

# Witness 2..N system: same perception contract as VISION_SYSTEM (imported lazily to
# avoid an import cycle) plus an explicit independent-skeptic stance.
SKEPTIC_SUFFIX = (
    "\n4. You are ONE OF SEVERAL INDEPENDENT readers of this image; others answer the "
    "same question separately. Read with fresh, skeptical eyes: before committing, ask "
    "what a hasty reader could mistake this for (similar-looking names, styles, shapes; "
    "miscounts; misread digits), check those confusions against the pixels, then commit "
    "to YOUR OWN best answer with confidence. Do not hedge into a non-answer."
)

JUDGE_SYSTEM = (
    "You compare INDEPENDENT visual readings of the SAME image, given the question "
    "they answered. Decide ONLY whether they agree on the CORE answer — the "
    "identification, count, name, value, or judgment the question actually asks for. "
    "Wording differences are agreement; different names, counts, values, or opposite "
    "judgments are disagreement. You are NOT asked what is true — only whether the "
    "readings say the same thing.\n"
    "Reply in EXACTLY this shape (no preamble):\n"
    "AGREEMENT: full | partial | split\n"
    "ANSWER: <only for full/partial: the agreed core answer — direct, concrete, "
    "verbatim text/numbers preserved>\n"
    "DETAILS-DIFFER: <only for partial: the secondary details the readings disagree "
    "on, one line>\n"
    "Rules: 'split' means the CORE answer itself differs — and when the question "
    "asks for SEVERAL values (multiple images or sub-questions), a disagreement on "
    "ANY asked-for value IS a split, not 'partial'. 'partial' is reserved for "
    "agreement on every asked-for value with only incidental details differing. "
    "Output NO answer line for a split and do NOT attempt to reconcile, average, or "
    "pick a winner. Never invent content found in no reading."
)

# The resolution protocol delivered with every split — directive, not advisory.
# Each branch generalizes a demonstrated win: (a) t032 catalog-enumerate,
# (b) t049 pixel-count / t101-style compute, (c) file/DOM/app-state reads.
RESOLUTION_PROTOCOL = (
    "READINGS CONFLICT — do not average them, do not pick one. Resolve the answer "
    "through a channel where it is CHECKABLE, then record the winning evidence as "
    "data (program output or an evidence file):\n"
    "(a) WHICH-ONE / naming dispute -> ENUMERATE the full candidate set from the "
    "environment itself (catalog, registry, directory, dataset) and verify candidates "
    "by direct side-by-side comparison — the true answer may appear in NEITHER "
    "reading.\n"
    "(b) COUNT / position / size dispute -> COMPUTE it: crop the region and "
    "count/measure with code (image processing, pixel measurement), or read the "
    "coordinates/values from the source file.\n"
    "(c) file / app / page content dispute -> read the SOURCE directly (file bytes, "
    "extracted text, DOM, application state).\n"
    "(d) only if no such channel exists -> take ONE more close-up look at the "
    "contested region with a narrower question, then commit and state the residual "
    "uncertainty.\n"
    "Repetition of the same reading by the same witness across multiple views is NOT "
    "independent confirmation — a biased reader repeats its bias with perfect "
    "consistency. Only cross-witness agreement or a code-channel result confirms."
)


class LookReport(str):
    """The text delivered to the caller, carrying a machine-readable split flag so
    the loop can ledger unresolved disagreements (str subclass — every existing
    call site keeps working; ChatText/Findings precedent)."""
    split: bool = False


def _mk_report(txt: str, split: bool = False) -> LookReport:
    r = LookReport(txt)
    r.split = bool(split)
    return r


def unresolved_splits_message(entries) -> str:
    """R3 done-gate bounce (one-shot): the actor declared done while visual readings
    it acted on were never resolved to agreement. General text; entries are
    mechanical {turn, path, q} facts from the loop's ledger."""
    lines = "\n".join(f"- turn {e['turn']}: {e['path']} — {e['q']}" for e in entries)
    return (
        "Before this declaration can be accepted: independent visual readings "
        "DISAGREED at the following look(s) and were never resolved (no later "
        "reading of the same target reached agreement, and no recorded evidence "
        "settled the conflict):\n" + lines + "\n\n" + RESOLUTION_PROTOCOL + "\n\n"
        "For each conflict above, either resolve it through a checkable channel and "
        "correct the deliverable if needed, or — if it genuinely does not affect the "
        "task's end state — state why in your evidence. Then declare done again."
    )


def _vision_system() -> str:
    from core.actor import VISION_SYSTEM          # lazy: avoid import cycle
    return VISION_SYSTEM


def _one_read(model: str, system: str, question: str, image, temperature: float,
              max_tokens: int = 2000, reasoning_max_tokens: int = 0) -> str:
    """One witness; provider failures remain transport, never visual evidence."""
    try:
        ans = chat(model, system, question, max_tokens=max_tokens, temperature=temperature,
                   reasoning_effort="", history=[], image=image,
                   reasoning_max_tokens=reasoning_max_tokens)
    except LLMTransportError:
        raise
    except Exception as e:                        # noqa: BLE001 — eyes must not crash the run
        return f"(vision tool error: {e})"
    return (ans or "").strip()


def _parallel_reads(jobs, question: str, image, max_tokens: int = 2000,
                    reasoning_max_tokens: int = 0) -> list:
    """jobs = [(model, system, temperature), ...] -> answers in job order."""
    if len(jobs) == 1:
        m, s, t = jobs[0]
        return [_one_read(m, s, question, image, t, max_tokens,
                          reasoning_max_tokens)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(_one_read, m, s, question, image, t, max_tokens,
                          reasoning_max_tokens)
                for (m, s, t) in jobs]
        return [f.result() for f in futs]


_AGREE_RE = re.compile(r"AGREEMENT:\s*(full|partial|split)", re.IGNORECASE)

_QUANT_RE = re.compile(  # pre-108 item 4: questions where witness-2 EARNS its cost —
    r"how many|count|number of|closer|closest|farther|larger|smaller|bigger|"  # counting,
    r"position|located|aligned|angle|degrees|coordinates|which of|left or right|"  # comparison,
    r"above or below|distance|spacing|how far",  # spatial relations
    re.IGNORECASE)


def _wants_ensemble(question: str) -> bool:
    """Route by the question's linguistic shape (general — no task knowledge):
    quantitative/spatial questions keep the full witness ensemble; identify/describe
    questions run single-witness (campaign split-rates: 30-33% vs ~1-7%)."""
    return bool(_QUANT_RE.search(question or ""))


def _judge(cfg, question: str, reads: list):
    """Classify agreement across 2+ reads -> (verdict, judge_text). verdict in
    {'full','partial','split',None}; None = judge unavailable (infra)."""
    model = getattr(cfg, "look_merge_model", "") or cfg.model
    body = f"QUESTION: {question}\n\n" + "\n\n".join(
        f"READING {i + 1}:\n{r}" for i, r in enumerate(reads))
    try:
        out = (chat(model, JUDGE_SYSTEM, body, max_tokens=1200, temperature=0.0,
                    reasoning_effort="", history=[]) or "").strip()
    except LLMTransportError:
        raise
    except Exception as e:                        # noqa: BLE001
        log.warning("look-judge failed: %s", e)
        return None, ""
    m = _AGREE_RE.search(out)
    if not m:
        return None, out
    return m.group(1).lower(), _AGREE_RE.sub("", out, count=1).strip()


def _split_report(reads: list) -> LookReport:
    labels = ["READING A (canonical)", "READING B (independent skeptic)"]
    parts = []
    for i, r in enumerate(reads):
        lab = labels[i] if i < len(labels) else f"READING {i + 1} (independent)"
        parts.append(f"{lab}:\n{r}")
    return _mk_report("\n\n".join(parts) + "\n\n" + RESOLUTION_PROTOCOL, split=True)


def ensemble_look(cfg, question: str, datas: list, region=None) -> LookReport:
    """R3 entry point. look_ensemble=1 reproduces the v22 single-shot path exactly.
    N>=2: N independent witnesses; a judge classifies agreement; consensus is
    delivered on full/partial; a SPLIT delivers both readings verbatim + the
    resolution protocol and is flagged (.split) for the loop's ledger/done-gate.
    Judge unavailable (infra) -> both readings delivered, NOT flagged as split
    (never leave the actor worse off than a single read on an outage)."""
    n = max(1, int(getattr(cfg, "look_ensemble", 1)))
    if n >= 2 and getattr(cfg, "look_witness_router", False) and not _wants_ensemble(question):
        n = 1                                     # item 4: describe/identify -> one witness
    image, _ = prepare_look_images(datas, region=region,
                                   max_side=getattr(cfg, "look_max_side", 0) or None)
    q = question or DEFAULT_Q
    vs = _vision_system()
    mt = int(getattr(cfg, "vision_max_tokens", 0) or 2000)
    rmt = int(getattr(cfg, "reasoning_max_tokens", 0))   # v36: witness budget —
    #      reasoning-first eyes (kimi-k3) need ~16k to answer; default = m3-era 2000
    if n == 1:
        ans = _one_read(cfg.vision_model, vs, q, image, 0.0, mt, rmt)
        return _mk_report(ans or "(vision tool returned nothing)")

    temp = float(getattr(cfg, "look_ensemble_temp", 0.8))
    # v33 (wenyi): HETEROGENEOUS witnesses. Two same-model reads only decorrelate
    # sampling noise — a systematic (architecture-shaped) misread is repeated twice
    # and its agreement launders wrongness into confidence (t003: unanimous-wrong).
    # A second MODEL has different blind spots: agreement means far more, and splits
    # can catch errors the same-model pair is structurally blind to. Same call
    # count, zero added cost. "" = incumbent single-model behavior.
    second = getattr(cfg, "vision_model_2", "") or cfg.vision_model
    jobs = [(cfg.vision_model, vs, 0.0)]
    jobs += [(second, vs + SKEPTIC_SUFFIX, temp)] * (n - 1)
    reads = [r for r in _parallel_reads(jobs, q, image, mt, rmt)
             if r and not r.startswith("(vision tool error")]
    if not reads:
        return _mk_report("(vision tool returned nothing)")
    if len(reads) == 1:                           # the other witness errored out
        return _mk_report(reads[0])

    verdict, judge_text = _judge(cfg, q, reads)
    if verdict == "split":
        log.info("look-judge: SPLIT on core answer (%d reads)", len(reads))
        return _split_report(reads)
    if verdict in ("full", "partial"):
        tag = (f"[{len(reads)} independent visual reads agree]" if verdict == "full"
               else f"[core answer agreed by {len(reads)} independent reads; "
                    "secondary details differ — noted above]")
        return _mk_report(f"{judge_text}\n{tag}")
    # judge unavailable/unparseable: deliver the information, do not claim a conflict
    joined = "\n\n".join(f"[independent read {i + 1}] {r}" for i, r in enumerate(reads))
    return _mk_report("[agreement judge unavailable — independent readings follow]\n"
                      + joined)
