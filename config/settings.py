"""forge configuration — one model, loop budgets. Plain dataclass, optional YAML overlay."""
from dataclasses import dataclass


@dataclass
class Config:
    model: str = "minimax/minimax-m3"   # primary PoC model (cheap; the flywheel-thesis target)
    max_tokens: int = 10000             # M3 truncation floor is ~8000; headroom for code
    temperature: float = 0.0
    provider_order: tuple = ()          # Optional OpenRouter provider preference,
    #                                     e.g. ("Z.AI",). Empty preserves normal
    #                                     routing. When set, the route stays attached
    #                                     to Actor calls, compaction, and truncation
    #                                     recovery calls.
    provider_allow_fallbacks: bool = True
    provider_require_parameters: bool = False
    env_memory_dir: str = ""            # P2 eval flag: path to a FROZEN practice
    #                                     memory bank; synced read-only into the
    #                                     guest (~/.memory); only a mechanical
    #                                     file listing is shown. The Actor freely
    #                                     searches/previews/reads/re-reads; no file
    #                                     body or privileged index is injected. "" = OFF
    #                                     (byte-identical eval; the default).
    env_memory_orient: bool = False     # ORIENT arm (fallback-1+, wenyi): ask the
    #                                     agent to review its memory FIRST and
    #                                     state whether/how it applies to this
    #                                     task, then proceed. Judgment stays its
    #                                     own; only the looking is asked.
    env_memory_brief: bool = False      # BRIEF-RUN arm (fallback-2, wenyi 07-30):
    #                                     Phase 0 = briefing session on the live VM
    #                                     (task + memory; agent-decided length,
    #                                     shared budget) -> agent writes
    #                                     ~/briefing.md -> mounted VERBATIM into
    #                                     the main run's system prompt; main run
    #                                     starts with FRESH history. Fail-open:
    #                                     no briefing -> normal preamble run.
    system_extra: str = ""              # appended to build_system() output —
    #                                     carrier for the BRIEF-RUN mount (agent-
    #                                     authored text only; never set by hand).
    done_witness_gates: bool = False    # P2-v9 D-1/D-2 (self-report probe
    #                                     rejection + one-shot witness bounce).
    #                                     Default OFF = legacy done-path (v41
    #                                     flagship behavior preserved exactly);
    #                                     ON in the E2 ablation/exam configs
    verifier_items: bool = False        # E4-A1c (wenyi 08-08): the inspector's
    #                                     per-requirement judgement as DATA
    #                                     ("items": [{req,status,evidence}]) beside
    #                                     the prose findings — bounces name the
    #                                     unsettled requirement instead of handing
    #                                     the actor a paragraph, and progress
    #                                     between inspections becomes measurable.
    #                                     The scalar verdict stays the inspector's
    #                                     own call. Default OFF = byte-exact.
    verifier_elastic_depth: bool = False  # E4-A1b (wenyi 08-08: "let verifier
    #                                     decide its own depth, we are not cap
    #                                     anything"): the inspection ends when the
    #                                     VERIFIER rules, not when a round counter
    #                                     expires. The 6-round budget becomes a
    #                                     runaway backstop only (200), the prompt
    #                                     stops advertising a budget, and the
    #                                     "N rounds left" pressure line is replaced
    #                                     by "rule when YOUR evidence settles it".
    #                                     Default OFF = capped legacy behavior.
    verifier_continuity: bool = False   # E4-A1 (wenyi 08-08, E3 exit-door fix):
    #                                     the verifier becomes ONE continuing
    #                                     session per run (its verdicts = its own
    #                                     history, RIH-preserved); after a WRONG,
    #                                     unverified endings no longer auto-accept
    #                                     (trust revoked until evidence restores
    #                                     it) and the unverified bounce becomes an
    #                                     independent-channel EVIDENCE REQUEST.
    #                                     Default OFF = E3-and-earlier byte-exact.
    #                                     where the fix was validated.
    agentic_verifier_config: str = ""   # EXP: nonempty path opts the normal task
    #                                     harness into the same full Agent runtime
    #                                     used by self-evolution's Verifier Agent.
    #                                     The Verifier keeps one context across
    #                                     candidate revisions, freely runs programs
    #                                     and looks, and publishes a lossless
    #                                     PASS/FAIL report when it decides it is done.
    #                                     Empty preserves the incumbent JSON-probe
    #                                     inspector byte-for-byte.
    verifier_evolve_route: bool = False  # Unified-loop opt-in. The persistent
    #                                     Verifier Agent publishes exactly one
    #                                     HANDOFF / REVISE / EVOLVE route instead
    #                                     of PASS / FAIL. EVOLVE returns control to
    #                                     the self-evolving pipeline; it never
    #                                     invokes or receives an external grader.
    #                                     False preserves the benchmark harness.
    verifier_local_verdict_only: bool = False
    #                                     Separated unified-loop authority. Keep
    #                                     the explicit terminal transport above,
    #                                     but restrict the target Verifier Agent
    #                                     itself to PASS / FAIL. A separately
    #                                     injected persistent Curriculum Agent
    #                                     chooses HANDOFF / VERIFY_MORE after PASS
    #                                     and REVISE / EVOLVE after FAIL.
    #                                     False preserves the earlier unified
    #                                     Verifier-owned routing experiment.
    verifier_unverified_evidence: bool = False
    #                                     Agentic three-way local verdict. When a
    #                                     material claim remains unresolved (or
    #                                     credible instruments disagree), the same
    #                                     persistent Verifier may publish UNVERIFIED.
    #                                     Control returns to the same Actor solely
    #                                     to expose reproducible evidence leads;
    #                                     the same Verifier then re-investigates.
    #                                     False preserves PASS/FAIL transport.
    verifier_failure_starts_evolution: bool = False
    #                                     Self-evolving benchmark opt-in. A
    #                                     concrete local Verifier Agent FAIL ends
    #                                     the current target attempt and enters
    #                                     the learning/practice lifecycle directly.
    #                                     The Verifier still owns only PASS/FAIL;
    #                                     this transition is the declared protocol,
    #                                     not a Curriculum or Verifier route. False
    #                                     preserves same-Actor repair behavior.
    verifier_hide_actor_memory: bool = False
    #                                     Unified-loop role boundary. Hide
    #                                     ~/.memory from Verifier-authored Programs
    #                                     and file Looks so independent verification
    #                                     can inspect the candidate but not the
    #                                     Actor Agent's private durable policy.
    #                                     False preserves historical benchmark runs.
    verifier_stage_lifecycle: bool = False
    #                                     Unified-loop opt-in. A persistent target
    #                                     Verifier model identity freely studies a
    #                                     post-setup/pre-Actor environment and may
    #                                     author a private executable evaluator,
    #                                     then remains in the same context for
    #                                     candidate evaluation and later revisions.
    verifier_persist_scratch: bool = False
    #                                     Keep the Verifier Agent's private scratch
    #                                     across its stage changes and candidate
    #                                     revisions. Unified target renewal archives
    #                                     and restores it without a size ceiling.
    verifier_private_paths: tuple = ()
    #                                     Additional harness-owned Actor-private
    #                                     guest paths hidden from Verifier Programs
    #                                     and file Looks. The authoritative candidate
    #                                     surface remains readable and read-only.
    verifier_execution_mode: str = "effect_isolated"
    #                                     Agentic Verifier execution boundary.
    #                                     "effect_isolated" preserves the historical
    #                                     read-only filesystem plus private PID/net/IPC
    #                                     namespaces. "rollback_mirror" checkpoints the
    #                                     complete Docker/QEMU guest, lets the Verifier
    #                                     inspect its live processes/network/GUI, then
    #                                     restores the exact checkpoint before grading.
    #                                     Actor-private paths remain hidden in both.
    actor_evaluator_isolation: bool = False
    #                                     OSWorld target validity boundary. Before S0
    #                                     and before either Agent exists, quarantine
    #                                     guest-visible task-class evaluator source
    #                                     and compiled artifacts. The official sealed
    #                                     evaluator remains host-only. False preserves
    #                                     historical target setup byte-for-byte.
    practice_done_requires: str = ""    # P2 (BRIEF Phase 0): a guest path that must
    #                                     EXIST before a practice_mode done is
    #                                     accepted (existence-only probe; content
    #                                     never read — firewall-clean). Bounded by
    #                                     max_consec_done -> stall -> fail-open.
    #                                     "" = OFF (practice episodes unaffected).
    practice_mode: bool = False         # P2 practice profile: done = session end
    #                                     (no task checks/inspection — no task
    #                                     exists); pair with independent_verify
    #                                     false. Inert everywhere else.
    practice_stall_iters: int = 0       # E7 stall detector (PREREG_E7 §5, B5):
    #                                     consecutive no-progress probes (find
    #                                     -newer stamp, ~/.memory INCLUDED, live
    #                                     melt/ffmpeg = progress) before the e7
    #                                     loop ends the night as
    #                                     stalled_quiescent. Hooked at the e7
    #                                     loop between iterations — core/loop.py
    #                                     never reads it. 0 = OFF (the default;
    #                                     load() drops unknown YAML keys, so the
    #                                     field must exist here to reach cfg).
    top_p: float = -1.0                 # >=0 SENDS top_p explicitly; -1 omits the
    #                                     field entirely (provider default). K3's
    #                                     report pins "temperature = 1.0 and
    #                                     top-p = 1.0" — we were compliant only by
    #                                     accident (never sent it). wenyi 08-09:
    #                                     "top p is crucial for k3 setting" -> pin it.
    primary_temperature: float = -1.0   # >=0 overrides temperature for the PRIMARY
    #                                     actor's decode loop ONLY (K3 report 07-27:
    #                                     official evals run temp=1.0/top-p=1.0; our 0.0
    #                                     is an M3-era inheritance). -1 = off (byte-
    #                                     identical v41). Neutralized on escalation
    #                                     _replace so GLM segments keep proven settings;
    #                                     verifier/eyes/summarizer sites untouched.
    retry_temperature: float = 0.7      # used on dry-turn retries: breaks the temp-0
    #                                     repeated-token attractor (decoder degeneration)
    reasoning_effort: str = "low"       # M3 IGNORES effort gradations (low==high,
    #                                     verified 2026-07-06: it scales reasoning with
    #                                     problem difficulty, not this knob) — inert for
    #                                     M3, honored by models that support it. "none" =
    #                                     the only EFFECTIVE knob (disables reasoning,
    #                                     used by the truncation retry); "" = omit field
    # loop budgets
    agent_decided_stop: bool = False     # EXP: normal completion belongs to the
    #                                     agent.  max_iters/wall_clock_secs remain
    #                                     emergency watchdogs, are not disclosed in
    #                                     prompts, never trigger commit pressure, and
    #                                     surface as safety_ceiling rather than a
    #                                     valid budget ending.  Default OFF preserves
    #                                     every registered harness/config.
    max_iters: int = 24                 # program turns + done attempts (nudges don't count).
    #                                     10 proved too small: seed0 burned all 10 on a good
    #                                     recon->extract trajectory and died mid-flight.
    script_timeout: int = 600           # HTTP; in-VM kill fires 30s earlier (partial trace survives)
    controller_recovery_secs: int = 180 # A guest OOM can restart OSWorld's command
    #                                     controller after an Actor action has already
    #                                     failed. Pause sampling and wait for a stable
    #                                     channel so the SAME Actor can observe that
    #                                     failure and continue. This never replays the
    #                                     action. 0 restores the legacy rapid-retry path.
    controller_recovery_probe_secs: float = 5.0
    controller_recovery_stable_probes: int = 2
    llm_infra_retry_secs: float = 30.0  # recoverable API/provider failures pause
    #                                      without creating an agent turn or spending
    #                                      the active-work wall clock
    check_timeout: int = 30             # per verification probe
    max_checks: int = 16            # capacity only — overflow evicted least-valuable-
    #                                 first (v16), never vetoes done (12 was too tight:
    #                                 a run's content checks were evicted for proxies)
    max_nudges: int = 0                 # DEPRECATED/inert compatibility field:
    #                                      cumulative nudges never terminate an Agent
    max_consec_dry: int = 3             # consecutive unparseable turns -> stalled
    strict_one_action: bool = True      # v21 (A): a reply with >=2 objects of ONE
    #                                     action kind is decoder degeneration — run
    #                                     NOTHING, re-ask for one action (not the old
    #                                     run-last+note, which ran ritual/duplicate
    #                                     objects and bred a false world-model). False
    #                                     = pre-v21 behavior (the A/B control arm).
    max_consec_degen: int = 3           # consecutive degenerate multi-action turns
    #                                     before escalating to the fresh-context pivot
    #                                     (which reliably breaks the decoder loop)
    budget_conditional_accept: bool = False  # v21 (B): an 'unverified' verdict only
    #                                     auto-accepts LATE (commit phase) and only if
    #                                     no confirmed-wrong preceded it — else it
    #                                     bounces for repair (closes the soft-accept
    #                                     leak: 47% of unverified-accepts scored <0.1,
    #                                     self-truncation with budget left). False =
    #                                     pre-v21 (accept on 2nd unverified regardless).
    max_wrong_before_pivot: int = 3     # DEPRECATED/inert compatibility field.
    #                                     Verifier WRONG is semantic feedback and now
    #                                     always returns to the same Actor Agent/context;
    #                                     it never triggers a fresh-model pivot.
    max_consec_done: int = 3            # consecutive failed done-declarations -> stalled
    max_resumes: int = 1                # v15: stall w/ budget left -> fresh-context
    #                                     attempt on the SAME machine (stalls are
    #                                     conversation-local; the machine is sound).
    #                                     0 = off
    resume_with_worklog: bool = True    # hand the old WORK LOG to the fresh attempt
    escalation_model: str = ""          # v21: when a stuck run pivots/resumes, run the
    #                                     FRESH attempt on a DIFFERENT model (M3 stays
    #                                     primary; escalation fires only on the rare stuck
    #                                     restart). A genuine different draw — the one
    #                                     lever left after the sweep proved harness
    #                                     margins tapped out. "" = same model (no escalation)
    escalation_agentic_verifier_config: str = ""  # Optional full-Agent verifier
    #                                     config for the escalated Actor segment. This
    #                                     preserves role asymmetry when the escalation
    #                                     model is the main segment's Verifier model.
    #                                     A model/config switch starts a separate
    #                                     persistent Verifier Agent context; histories
    #                                     are never replayed across different models.
    escalation_primary_temperature: float = -1.0  # Actor-only decode override for
    #                                     the escalation model. Sampling points are
    #                                     model-specific; -1 uses general temperature.
    escalation_provider_order: tuple = ()         # Provider routing is model-specific.
    escalation_provider_allow_fallbacks: bool = True
    escalation_provider_require_parameters: bool = False
    vision_model: str = ""              # v22: when set, the `look` action routes the
    #                                     image + the model's question to THIS (vision)
    #                                     model and returns its TEXT answer into the
    #                                     primary's context — so a TEXT-ONLY primary
    #                                     (e.g. GLM-5.2) can orchestrate vision tasks by
    #                                     delegating perception ("eyes as a tool"). ""
    #                                     = legacy behavior (attach the image to the
    #                                     primary itself, for a multimodal primary like M3).
    vision_rounds: int = 1              # v22.1: 1 = single-shot eyes (one glance -> one
    #                                     answer). >1 = the vision AGENT: the VLM may
    #                                     zoom/crop/grid-inspect over up to this many
    #                                     rounds before committing to a confident answer
    #                                     (mirrors the verifier's multi-probe loop).
    #                                     Single glances mis-COUNT and mis-read small
    #                                     text; systematic inspection fixes it (t101).
    vision_verify: bool = True          # v24: when vision_model is set, the INDEPENDENT
    #                                     inspector may issue a look-probe (render the
    #                                     deliverable -> eyes) to confirm/refute VISUAL
    #                                     requirements a text-blind probe cannot — the
    #                                     t063 false-done fix (circular self-checks passed
    #                                     + blind verifier -> wrong geometry accepted at 0).
    #                                     False = text-only inspection (the A/B control).
    look_ensemble: int = 1              # v30/v32: N independent eyes reads per look.
    #                                     1 = the v22 single-shot path unchanged.
    #                                     >=2 (v32 "two witnesses + judge"): witness 1
    #                                     canonical, witnesses 2..N hotter + skeptic
    #                                     stance; a JUDGE classifies agreement only.
    #                                     full/partial -> consensus text; SPLIT ->
    #                                     BOTH readings VERBATIM + the resolution
    #                                     protocol (never a merged/averaged answer —
    #                                     the v30 vote was forensically inert: 0/12
    #                                     decisive wins, 2 manufactured-wrong merges).
    #                                     Uniform for every look; no classifier.
    look_ensemble_temp: float = 0.8     # temperature for witnesses 2..N (witness 1
    #                                     stays at 0.0 — identical zero-temp samples
    #                                     would be degenerate).
    vision_model_alt: str = ""          # DEPRECATED v32: the v30 cross-model vote
    #                                     extension was removed (voting never resolved
    #                                     a split; code channels did). Field retained
    #                                     so older configs still load; ignored.
    archive_eyes: bool = False          # v32.5: save every image the eyes receive
    #                                     under <run>/eyes/NNN_label.{png,jpg} so a
    #                                     human can inspect exactly what the model
    #                                     saw (the alpha-flatten bug hid for 3 runs
    #                                     because nobody could open those pixels).
    vision_model_2: str = ""            # v33 (wenyi): the SECOND witness's model.
    #                                     Two same-model reads share one set of blind
    #                                     spots — agreement can be one wrong opinion
    #                                     counted twice (t003 unanimous-wrong). A
    #                                     different architecture decorrelates the
    #                                     errors at ZERO added cost (same 2 calls).
    #                                     "" = witness 2 uses vision_model (incumbent).
    look_max_side: int = 1400           # v32.3: long-side bound for images sent to
    #                                     the eyes. PROBE-CALIBRATED (resprobe): m3's
    #                                     serving stack downsamples internally, so
    #                                     text legibility does NOT improve past 1400
    #                                     (3/9 codes at 1400 vs 2/9 at native-3200) —
    #                                     BUT very large sources (>4MP) over-shrink at
    #                                     1400 and lose discriminative SHAPE (18MP
    #                                     snow filter: hedge at 1400, confident flake
    #                                     ID at 2560). 2560 preserves that at ~2-4x
    #                                     payload. Crops remain the dominant lever.
    look_merge_model: str = ""          # text model for the agreement JUDGE. "" = the
    #                                     primary model (it sees only the witnesses'
    #                                     TEXT readings — the primary stays blind; it
    #                                     judges same-or-different, never truth).
    cycle_evidence: bool = False        # Exact recurring Program/Look block -> neutral
    #                                     history evidence. No gate, counter, strategy,
    #                                     temperature change, or stop. Default OFF keeps
    #                                     ordinary/eval configurations inert.
    action_discipline: bool = False     # v36 (k3fit): STRICT turn discipline — adds
    #                                     one SYSTEM line + a narration-aware nudge for
    #                                     models that describe steps without acting
    #                                     (kimi-k3's 26-nudge stall pattern, t032/t059).
    resume_synthesize_worklog: bool = False  # v36 (k3fit): when a stall-resume finds an
    #                                     EMPTY work log (short segment never folded),
    #                                     synthesize one from the discarded history so
    #                                     the fresh attempt doesn't re-recon (t032 s900
    #                                     resume1 re-derived the whole hexo state).
    verifier_vision_model: str = ""     # v39.1 (wenyi: FULL MULTIMODAL EVERY RUN):
    #                                     the INSPECTOR's eyes route. "" = legacy
    #                                     (vision_model, i.e. blind when native);
    #                                     "self" = the PRIMARY serves the inspector's
    #                                     looks (native-k3: GLM verifier + k3 eyes =
    #                                     cross-model, sighted); or an explicit model.
    look_witness_router: bool = False   # pre-108 item 4: question-type witness routing.
    #                                     Campaign data: witness-2 disagrees 30-33% on
    #                                     QUANTITATIVE/SPATIAL questions (counting, 3D)
    #                                     but ~1-7% on describe/identify — ~40% of eyes
    #                                     calls bought nothing. ON: quantitative/spatial
    #                                     questions get the full ensemble; other looks
    #                                     run a single witness. Routing is by the
    #                                     QUESTION's linguistic shape only (general).
    reasoning_in_history: bool = False  # v38 (OFFICIAL K3 PROTOCOL): re-attach each
    #                                     turn's reasoning to its assistant message in
    #                                     history ('add the COMPLETE assistant message
    #                                     ... Do not keep only content' — Kimi docs).
    #                                     We violated this all campaign; narration and
    #                                     empty-at-stop are plausible SYMPTOMS. Actor
    #                                     main loop only. False = content-only (legacy).
    reasoning_max_tokens: int = 0       # v37 (wenyi: max reasoning, accuracy-only):
    #                                     cap for the model's THINKING channel, sent as
    #                                     {"reasoning":{"max_tokens":N}} when
    #                                     reasoning_effort is empty. k3: reasoning is
    #                                     mandatory + depth-tunable (E1 study). 0 = omit.
    vision_max_tokens: int = 0          # v36: witness reply budget; 0 = m3-era default
    #                                     (2000). Reasoning-first eyes (kimi-k3) burn
    #                                     ~16k thinking before answering — without the
    #                                     headroom every hard look walks the client's
    #                                     truncation ladder (3 wasted round-trips).
    doubts_block_pass: bool = False     # v32.2/v41: when enabled, PASS plus non-empty
    #                                     doubts is resolved by the SAME Verifier Agent:
    #                                     task-blocking uncertainty becomes wrong/
    #                                     unverified; incidental notes move to findings
    #                                     and PASS returns with empty doubts. The harness
    #                                     never classifies free-form caveats or bounces
    #                                     them mechanically to the Actor Agent.
    split_done_gate: bool = False       # R3/v32: ONE-SHOT gate — a done declared
    #                                     while some look's readings SPLIT and were
    #                                     never re-read to agreement bounces ONCE with
    #                                     the open-split ledger + resolution protocol
    #                                     (fires with turns left — detection finally
    #                                     GATES). The second declaration passes: cost
    #                                     is bounded at one bounce per attempt. Open
    #                                     splits are also handed to the inspector as
    #                                     mechanical context. False = incumbent.
    doubt_escalation: bool = False      # R2/v31: route DOUBTFUL accept decisions
    #                                     through a second inspection on the
    #                                     escalation model. Triggers: (A) the
    #                                     unverified-accept rule fires (the path that
    #                                     laundered could-not-confirm into done-at-0:
    #                                     t063, t032 x2); (B) inspector PASS with
    #                                     non-empty doubts (material requirements
    #                                     judged without affirmative evidence); (C) a
    #                                     HOLLOW re-declare (no program since the last
    #                                     bounced done). Only a CONCRETE-evidence
    #                                     "wrong" from the reviewer blocks acceptance.
    #                                     Requires escalation_model; default OFF = the
    #                                     incumbent (stall-gated escalation only).
    doubt_reviews_max: int = 2          # R2: second inspections per attempt (cap — a
    #                                     review disagreement must not loop forever).
    verifier_content_guard: bool = True # v20 Phase-0: a verifier 'pass' requires >=1
    #                                     CONTENT-reading probe (not just existence).
    #                                     True = the shipped fix; False = pre-Phase-0
    #                                     behavior (any informative probe cleared it) —
    #                                     the A/B control arm to measure the real effect.
    strategy_pivot: bool = True         # v18: zero-file-delta stall -> self-review +
    #                                     diverge directive instead of continuation.
    #                                     False = v15 behavior on every stall (the
    #                                     A/B control arm)
    max_consec_repeat: int = 16         # consecutive IDENTICAL (program, output) ->
    #                                     stalled: warnings and temp both proven
    #                                     insufficient at depth (497x observed); legit
    #                                     wait-loops have changing output and never
    #                                     accumulate — fail fast, stall-inspection
    #                                     still verifies whatever was achieved
    commit_frac: float = 0.75           # past this fraction of iters, trace msgs demand COMMIT
    # v8: independent fresh-context verification at done-time (system-level)
    independent_verify: bool = True     # done needs actor checks AND the inspection to pass
    verifier_model: str = ""            # "" = same as model; set for a heterogeneous verifier
    verifier_appearance_attach: bool = False  # R2a appearance-gate: when the inspector's OWN
    #                                     round-1 inventory tags [APPEARANCE] requirements and
    #                                     2 probe rounds settle none of them by look, the
    #                                     HARNESS renders the run-touched deliverable(s) and
    #                                     injects the visual reading mechanically (the v27
    #                                     written reminder was declined on camera — t019/t086
    #                                     postfix; mechanics bind, invitations don't).
    #                                     Capability, not authority: verdict rules unchanged.
    appearance_attach_max: int = 2      # max images per inspection (cost bound)
    verifier_destination_note: bool = False  # P1 destination-binding (07-24): (a) neutral
    #                                     FYI lines in the inspection briefing for files the
    #                                     TASK TEXT names that the run never touched (pure
    #                                     string-match vs the file ledger — the harness
    #                                     computes the fact, the inspector judges it: a named
    #                                     deliverable untouched is damning, a named INPUT
    #                                     untouched is expected); (b) one contract sentence
    #                                     binding verification to instruction-named paths.
    #                                     Root cause: t079-980-r2a marooned 4.2h of work in
    #                                     work/ -> grader read the untouched named template
    #                                     -> 0.0, and every layer bound to the run-touched
    #                                     path. Capability-not-authority; #0-clean (task
    #                                     input + own ledger only).
    escalation_verifier_cross_model: bool = False  # EXP (xverify): if the escalated actor
    #                                     equals the configured verifier (k3-flagship: both
    #                                     GLM), fall the verifier back to the ORIGINAL primary
    #                                     so the escalated segment stays CROSS-model instead of
    #                                     self-reviewing. Default False = current behavior (no-op).
    verifier_probes: int = 6            # probe rounds for the inspector (4 was too tight)
    verifier_probe_timeout: int = 60    # per-probe seconds (30 truncated pptx-style probes)
    verifier_max_tokens: int = 6000
    unverified_accept: int = 2          # Nth could-not-confirm inspection ACCEPTS the done
    #                                     (confirmed-wrong always bounces; a shallow
    #                                     inspection must not veto deep work forever)
    inspect_extension: int = 8          # extra turns granted when an inspection bounces a
    max_inspect_extensions: int = 2     # done — reacting to independent findings needs
    #                                     recon+fix+redeclare; without this, late dones
    #                                     convert into budget-deaths (seeds 9/10 proved it)
    wall_clock_secs: int = 7200         # loop only (excludes VM boot + evaluation).
    #                                     Sized for the 100-turn horizon under box-load
    #                                     latency (~30-100s/turn); the cap exists to
    #                                     bound runaways, not to race the scheduler
    trace_head: int = 0                 # 0/0 = lossless program-output transport into
    trace_tail: int = 0                 # Actor context. Positive values are an explicit
    #                                     opt-in legacy head/tail bound.
    trace_context_max_chars: int = 0    # 0 = lossless. Positive values explicitly cap
    #                                     one textual Program observation in the live
    #                                     model request; repair profiles opt in. The
    #                                     complete trace is always archived first; only
    #                                     the live context gets an explicit head/tail
    #                                     externalization notice. This prevents one
    #                                     accidental multi-megabyte dump from exceeding
    #                                     the provider window before history compaction
    #                                     has a chance to run.
    fold_batch: int = 10                # summary mode: pairs folded per summarizer call
    #                                     (and, when ctx_high_water=0, the legacy count
    #                                     trigger: fold whenever this many accumulate)
    worklog_max_chars: int = 40000      # v21 (context): WORK LOG output cap. Was a
    #                                     hardcoded 14000 that BLIND-chopped 22% of folds
    #                                     mid-sentence, silently destroying whole sections
    #                                     (t094 lost its extracted dims -> placeholders ->
    #                                     0). Raised (fits: 150k verbatim + 40k log < 300k
    #                                     high-water, far under the v14 bloat floor) and
    #                                     truncated at a LINE boundary, keeping the top.
    ctx_high_water: int = 0             # v9.2 EVALUATED trigger (chars): >0 = fold only
    #                                     when the rendered context (work log + verbatim
    #                                     tail) exceeds this; 0 = legacy count trigger.
    #                                     A VALUE per config, model-related — size it to
    #                                     the model's usable window; mechanism agnostic.
    ctx_low_water: int = 0              # fold down to this (0 = ctx_high_water // 2) —
    #                                     hysteresis so folding is not per-turn
    keep_chars: int = 0                 # v14: the verbatim window becomes a CHARACTER
    #                                     budget — keep the most recent pairs that fit
    #                                     (floor 6 pairs, cap history_keep_pairs).
    #                                     Fat traces -> narrow window, thin -> wide.
    #                                     0 = legacy fixed-pair window. Fixes the
    #                                     fat-window defect: 16k traces x 30 pairs made
    #                                     the untouchable window ALONE 0.8-1.5M chars,
    #                                     folds could never reach low water, context
    #                                     bloat suppressed capability (t040 evidence)
    history_keep_pairs: int = 0         # 0 = NEVER compact (default: full history — keeps
    #                                     run conditions identical to all pre-v9 data).
    #                                     Set >0 (recent exchanges kept verbatim, older
    #                                     turns digested) ONLY where the horizon demands
    #                                     it — e.g. m3_long.yaml's 500 turns, which
    #                                     physically exceed the context window otherwise.
    #                                     Full history always persists to the transcript.
    verifier_infra_retries: int = 0     # Retry complete trusted Verifier inspections
    #                                     after rolling back and rebuilding a failed
    #                                     isolation boundary. Zero preserves fail-fast;
    #                                     repaired benchmark profiles opt in explicitly.


def load(path: str = None) -> Config:
    cfg = Config()
    if path:
        import yaml
        for k, v in (yaml.safe_load(open(path)) or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg
