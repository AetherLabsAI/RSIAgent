#!/usr/bin/env python3
"""E4-A1 tests: verifier continuity + the uv-accept predicate + message variants.

Run: python3 -m pytest tests/test_verifier_continuity.py -q
(also executable standalone: python3 tests/test_verifier_continuity.py)
"""
import sys
import types

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.loop import uv_accept_ok                    # noqa: E402
from core import verifier as V                        # noqa: E402


class Cfg:
    unverified_accept = 2
    budget_conditional_accept = False
    verifier_continuity = False
    # verify_independent surface (only what the mocked path touches)
    verifier_model = "m"
    model = "m"
    verifier_probes = 3
    verifier_max_tokens = 100
    temperature = 0.0
    reasoning_effort = "low"
    verifier_destination_note = False
    verifier_appearance_attach = False
    verifier_content_guard = True
    vision_verify = False
    reasoning_in_history = False
    verifier_probe_timeout = 30
    verifier_elastic_depth = False


def test_uv_predicate_legacy_accepts_regardless_of_wrongs():
    c = Cfg()
    assert uv_accept_ok(c, unverified=2, commit=False, wrongs=1)   # the E3 exit-door


def test_uv_predicate_budget_conditional_blocks_precommit_and_wrongs():
    c = Cfg(); c.budget_conditional_accept = True
    assert not uv_accept_ok(c, 2, commit=False, wrongs=0)   # pre-commit
    assert not uv_accept_ok(c, 2, commit=True, wrongs=1)    # dirty record
    assert uv_accept_ok(c, 2, commit=True, wrongs=0)        # clean + commit


def test_uv_predicate_continuity_revokes_trust_after_wrong():
    c = Cfg(); c.verifier_continuity = True
    assert not uv_accept_ok(c, 2, commit=True, wrongs=1)    # WRONG on record -> never
    assert uv_accept_ok(c, 2, commit=True, wrongs=0)        # clean record unchanged
    assert uv_accept_ok(c, 5, commit=False, wrongs=0)       # (bca off) legacy path


def test_agentic_verifier_never_launders_missing_report_into_acceptance():
    c = Cfg(); c.agentic_verifier_config = "config/roles/practice_verifier.yaml"
    assert not uv_accept_ok(c, 2, commit=True, wrongs=0)
    assert not uv_accept_ok(c, 200, commit=True, wrongs=0)


def test_unverified_message_default_byte_stable():
    m = V.unverified_message("X")
    assert "write it to a file next to the deliverable" in m      # legacy verbatim
    assert "INDEPENDENT CHANNEL" not in m


def test_unverified_message_evidence_request():
    m = V.unverified_message("X", evidence_request=True)
    assert "INDEPENDENT CHANNEL" in m
    assert "a file you author about your own work proves nothing" in m
    assert "write it to a file next to the deliverable" not in m  # disease removed


def test_agentic_unverified_returns_reproducible_checks_to_same_verifier(
        monkeypatch, tmp_path):
    from config.settings import Config
    from core import loop as actor_loop
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = Config()
    cfg.max_iters = 8
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_continuity = True
    cfg.verifier_unverified_evidence = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.strict_one_action = False

    replies = iter((
        '{"program":{"lang":"bash","code":"make candidate"}}',
        ('{"done":{"checks":[{"desc":"candidate is live",'
         '"probe":"test -e /tmp/candidate && echo PASS"}]}}'),
        ('{"program":{"lang":"bash","code":"independently inspect candidate; '
         'private actor narration marker"}}'),
        ('{"done":{"checks":[{"desc":"candidate is independently live",'
         '"probe":"test -e /tmp/candidate && echo PASS"}]}}'),
    ))
    actor_users = []

    def fake_chat(*args, **kwargs):
        actor_users.append(args[2])
        return next(replies)

    inspections = []

    def fake_verify(*args, **kwargs):
        inspections.append(kwargs)
        if len(inspections) == 1:
            return "unverified", "service liveness was not reproducible"
        return "pass", "independently reproduced\nVERDICT: PASS\n"

    monkeypatch.setattr(actor_loop, "chat", fake_chat)
    monkeypatch.setattr(actor_loop, "verify_independent", fake_verify)

    class _EvidenceVM:
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="worked\n[exit 0]", exit_code=0, secs=0.1)

        def run_command(self, command, timeout=30, **kwargs):
            if command == actor_loop._FIND:
                return ""
            return "PASS"

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"

    result, _history = actor_loop.run_attempt(
        "authoritative task", _EvidenceVM(), cfg,
        ArtifactSink(str(tmp_path / "run")))

    assert result.status == "done"
    assert result.verifier_route == "HANDOFF"
    assert len(inspections) == 2
    assert "actor_evidence_leads" not in inspections[0]
    leads = inspections[1]["actor_evidence_leads"]
    assert "candidate is independently live" in leads
    assert "test -e /tmp/candidate && echo PASS" in leads
    assert "private actor narration marker" not in leads
    assert any("could not CONFIRM" in user for user in actor_users)


def _mock_chat_factory(replies):
    it = iter(replies)
    def _chat(model, system, user, **kw):
        return next(it)
    return _chat


def test_session_continuity_accumulates(monkeypatch=None):
    """Two inspections sharing one session: the second sees the first's history."""
    replies = ['{"verdict": "wrong", "findings": "bad", "doubts": ""}',
               '{"verdict": "unverified", "findings": "cannot confirm", "doubts": ""}']
    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_continuity = True
        session = V.VerifierSession()
        v1, _ = V.verify_independent("task", vm=None, cfg=c, session=session)
        n1 = len(session)
        v2, _ = V.verify_independent("task", vm=None, cfg=c, session=session)
        assert v1 == "wrong" and v2 == "unverified"
        assert n1 == 2 and len(session) == 4          # history grew across calls
        # the second call's opening carried the continuity bridge
        assert "CONTINUING INSPECTION" in session[2]["content"]
    finally:
        V.chat = orig


def test_session_keeps_context_but_refreshes_candidate_content_evidence():
    """Prior evidence guides the same Agent but cannot bless a revised candidate."""
    replies = [
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "content confirmed", "doubts": ""}',
        '{"verdict": "pass", "findings": "prior evidence still settles it", '
        '"doubts": ""}',
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "current content confirmed", '
        '"doubts": ""}',
    ]

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_continuity = True
        session = V.VerifierSession()
        v1, _ = V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert v1 == "pass" and session.content_probes == 1
        v2, _ = V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert v2 == "pass"
        assert session.content_probes == 2
        assert session.inspections == 2
        assert any("CURRENT candidate" in message["content"]
                   for message in session if message["role"] == "user")
    finally:
        V.chat = orig


def test_verifier_system_requires_pre_pass_falsification():
    system = V.build_verifier_system(Cfg())
    assert "Before emitting `pass`, try to FALSIFY" in system
    assert "one terminal `pass`" in system


def test_pass_doubts_are_resolved_by_same_verifier_agent():
    """Incidental caveats are clarified in-session, never bounced to the Actor."""
    replies = [
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "task met", '
        '"doubts": "an unrelated process exists"}',
        '{"verdict": "pass", "findings": "task met; unrelated process noted", '
        '"doubts": ""}',
    ]

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_continuity = True; c.doubts_block_pass = True
        session = V.VerifierSession()
        verdict, findings = V.verify_independent(
            "task", vm=VM(), cfg=c, session=session)
        assert verdict == "pass" and findings.doubts == ""
        assert any("Resolve that contradiction" in message["content"]
                   for message in session if message["role"] == "user")
    finally:
        V.chat = orig


def test_elastic_depth_runs_past_the_legacy_cap():
    """E4-A1b: with elastic depth the inspection ends on the VERDICT, not on the
    6-round counter — 9 probe rounds then a verdict must all be honored."""
    replies = (['{"probes": ["ls /tmp"]}'] * 9
               + ['{"verdict": "pass", "findings": "ok", "doubts": ""}'])

    class VM:
        def run_command(self, p, timeout=None):
            return "real output line"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_elastic_depth = True; c.verifier_content_guard = False
        v, _ = V.verify_independent("task", vm=VM(), cfg=c)
        assert v == "pass"            # reached its own verdict on round 10
    finally:
        V.chat = orig


def test_capped_depth_still_stops_at_budget():
    """Flag OFF: the legacy counter still terminates the inspection (byte-exact)."""
    replies = ['{"probes": ["ls /tmp"]}'] * 20 + ['{"verdict": "pass", "findings": "x"}']

    class VM:
        def run_command(self, p, timeout=None):
            return "real output line"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg()                      # elastic OFF, verifier_probes = 3
        c.verifier_probes = 3
        v, _ = V.verify_independent("task", vm=VM(), cfg=c)
        assert v in ("pass", "unverified")   # forced demand path, not 20 rounds
    finally:
        V.chat = orig


def test_opening_advertises_no_budget_when_elastic():
    o_cap = V._opening("t", 6, elastic=False)
    o_ela = V._opening("t", 6, elastic=True)
    assert "You have 6 probe rounds" in o_cap
    assert "NO round budget" in o_ela and "probe rounds." not in o_ela


def test_items_parsed_normalised_and_punchlisted():
    """E4-A1c: items[] become data; partial/looses statuses normalise; the
    punch list names ONLY the unsettled ones, violated first."""
    f = V._mk_findings("prose", "", [
        {"req": "clips exist", "status": "met", "evidence": "three on Desktop"},
        {"req": "watermark on ep2", "status": "PARTIALLY_MET", "evidence": "never seen"},
        {"req": "merged in order", "status": "violated", "evidence": "concat shows 1→3→2"},
        {"req": "junk entry"},                       # malformed -> dropped
        "not-a-dict",                                # malformed -> dropped
    ])
    assert len(f.items) == 3                          # two malformed dropped
    assert dict((r, s) for r, s, _ in f.items)["watermark on ep2"] == "could-not-confirm"
    pl = V.items_punchlist(f)
    assert pl.index("VIOLATED") < pl.index("COULD NOT CONFIRM")   # violated first
    assert "clips exist" not in pl                    # met items are not punch-listed
    assert "1 other requirement(s) it judged met" in pl


def test_punchlist_empty_without_items_and_messages_still_work():
    f = V._mk_findings("prose only", "")
    assert V.items_punchlist(f) == ""
    assert "prose only" in V.disagreement_message(f)
    assert "prose only" in V.unverified_message(f, evidence_request=True)


def test_messages_carry_the_punchlist_when_items_exist():
    f = V._mk_findings("prose", "", [
        {"req": "the deck is at the named path", "status": "violated",
         "evidence": "found only in /tmp"}])
    for msg in (V.disagreement_message(f),
                V.unverified_message(f, evidence_request=True)):
        assert "THE UNSETTLED ITEMS" in msg
        assert "the deck is at the named path" in msg


def test_items_schema_only_advertised_when_flag_on():
    class C:
        vision_model = "k3"; vision_verify = True; verifier_vision_model = ""
        verifier_destination_note = False; verifier_items = True
    class Coff(C):
        verifier_items = False
    assert '"items"' in V.build_verifier_system(C())
    assert '"items"' not in V.build_verifier_system(Coff())


def test_carry_findings_preserves_side_channels():
    """E4-A1c REGRESSION (t034, 14 punch lists lost): rewriting findings text
    must not strip .items/.doubts. String concatenation returns a plain str;
    carry_findings() is the only legal way to rewrite."""
    src = V._mk_findings("orig", "a doubt", [
        {"req": "the named file exists at the named path", "status": "violated",
         "evidence": "found only in /tmp"}])
    plain = "[downgraded] " + src                     # the OLD (buggy) shape
    assert getattr(plain, "items", ()) == ()          # proves the loss
    fixed = V.carry_findings("[downgraded] " + str(src), src)
    assert fixed.items == src.items and fixed.doubts == src.doubts
    assert "THE UNSETTLED ITEMS" in V.unverified_message(fixed, evidence_request=True)


def test_content_guard_same_agent_reinspection_keeps_items():
    """A blind PASS stays with the Verifier until it reads current content."""
    item = '"items": [{"req": "r1", "status": "met", "evidence": "e"}]'
    replies = [
        '{"verdict": "pass", "findings": "old evidence", ' + item + '}',
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "current evidence", ' + item + '}',
    ]

    class VM:
        def run_command(self, p, timeout=None):
            return "x"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_content_guard = True
        v, f = V.verify_independent("task", vm=VM(), cfg=c)
        assert v == "pass"
        assert len(getattr(f, "items", ())) == 1
    finally:
        V.chat = orig


def test_doubts_gate_downgrade_keeps_items():
    """Downgrading a pass carrying doubts must preserve its evidence items."""
    src = V._mk_findings("findings text", "an unresolved doubt", [
        {"req": "budget cap has a source", "status": "could-not-confirm",
         "evidence": "field blank in the source document"}])
    rebuilt = V.carry_findings(
        "[inspection passed but listed unresolved doubts — downgraded] "
        "Its doubts: " + src.doubts + "\nIts findings: " + str(src), src)
    assert rebuilt.items == src.items
    msg = V.unverified_message(rebuilt, evidence_request=True)
    assert "budget cap has a source" in msg and "COULD NOT CONFIRM" in msg


def test_session_off_stays_fresh():
    replies = ['{"probes": ["cat /etc/hosts"]}',
               '{"verdict": "pass", "findings": "ok", "doubts": ""}']

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg()                                     # continuity OFF
        session = []
        V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert session == []                          # untouched -> byte-exact legacy
    finally:
        V.chat = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("ALL E4-A1 TESTS PASS")
