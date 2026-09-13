"""Agent-controlled memory retrieval and lifecycle reminder tests."""

from __future__ import annotations

import tempfile
from types import SimpleNamespace

import core.loop as L
from config.settings import load
from core.trace import ArtifactSink
from env.vm import Trace
from explore.charter import memory_preamble


class VM:
    def run_script(self, lang, code, timeout=600):
        return Trace(stdout="same output\n[exit 0]", exit_code=0, secs=0.1)

    def run_command(self, cmd, timeout=30, **kwargs):
        return "PASS"

    def fetch_file(self, path, max_bytes=0):
        return None, "not needed"


def _sink():
    return ArtifactSink(tempfile.mkdtemp(prefix="memory_lifecycle_"))


def test_preamble_exposes_inventory_without_forcing_retrieval():
    rendered = memory_preamble("  10  video.md\n  20  cad.md")
    assert "video.md" in rendered and "cad.md" in rendered
    assert "available under ~/.memory/" in rendered
    assert "local work-phase edits are\ndiscarded" in rendered
    assert "inspect memory as you judge useful" in rendered
    assert "search across, preview, re-read, or fully read" in rendered
    assert "read every .md" not in rendered
    assert "Do not use head, tail" not in rendered


def test_preamble_exposes_empty_durable_interface_without_inventing_memory():
    rendered = memory_preamble("")
    assert "available under ~/.memory/" in rendered
    assert "empty (0 files)" in rendered
    assert "no prior durable memory to retrieve" in rendered
    assert "helpful memory" not in rendered


def test_memory_revisit_is_inert_without_attached_memory():
    assert L._memory_revisit(SimpleNamespace(env_memory_dir=""), "verifier") == ""
    note = L._memory_revisit(
        SimpleNamespace(env_memory_dir="/host/frozen"), "verifier")
    assert "~/.memory" in note
    assert "re-reading previously useful files" in note
    assert "inspecting unread files" in note
    assert "/host/frozen" not in note


def test_repeated_action_offers_one_agentic_retrieval_opportunity():
    cfg = load(None)
    cfg.env_memory_dir = "/host/frozen"
    cfg.max_iters = 3
    cfg.max_consec_repeat = 3
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 60
    seen = []
    actor = '{"program":{"lang":"bash","code":"echo same"}}'

    def fake_chat(model, system, user, **kwargs):
        seen.append(user)
        return actor

    old = L.chat
    L.chat = fake_chat
    try:
        L.run_attempt("task", VM(), cfg, _sink())
    finally:
        L.chat = old

    notes = [message for message in seen
             if "[MEMORY RETRIEVAL OPPORTUNITY]" in message]
    assert len(notes) == 1
    assert "recurring or low-information pattern" in notes[0]


def test_verifier_wrong_returns_to_same_actor_with_memory_opportunity():
    cfg = load(None)
    cfg.env_memory_dir = "/host/frozen"
    cfg.max_iters = 4
    cfg.independent_verify = True
    cfg.done_witness_gates = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 60
    replies = iter([
        '{"program":{"lang":"bash","code":"echo build"}}',
        '{"done":{"checks":[{"desc":"artifact exists",'
        '"probe":"test -e /tmp && echo PASS || echo FAIL"}]}}',
        '{"program":{"lang":"bash","code":"echo repair"}}',
        '{"done":{"checks":[{"desc":"artifact exists",'
        '"probe":"test -e /tmp && echo PASS || echo FAIL"}]}}',
    ])
    seen = []
    verdicts = iter([("wrong", "candidate mismatch"), ("pass", "current match")])

    def fake_chat(model, system, user, **kwargs):
        seen.append(user)
        return next(replies)

    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0,
                    context="", session=None):
        return next(verdicts)

    old_chat, old_verify = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        result, _ = L.run_attempt("task", VM(), cfg, _sink())
    finally:
        L.chat, L.verify_independent = old_chat, old_verify

    assert result.status == "done"
    assert [v for _, v in result.inspections] == ["wrong", "pass"]
    feedback = [message for message in seen
                if "[MEMORY RETRIEVAL OPPORTUNITY]" in message]
    assert len(feedback) == 1
    assert "candidate mismatch" in feedback[0]


def test_context_fold_offers_retrieval_on_the_next_actor_turn():
    cfg = load(None)
    cfg.env_memory_dir = "/host/frozen"
    cfg.max_iters = 1
    cfg.independent_verify = False
    cfg.history_keep_pairs = 8
    cfg.wall_clock_secs = 60
    seen = []

    def fake_chat(model, system, user, **kwargs):
        seen.append(user)
        return '{"program":{"lang":"bash","code":"echo work"}}'

    old_chat, old_fold = L.chat, L._fold_summary
    L.chat = fake_chat
    L._fold_summary = lambda history, covered, summary, cfg, sink, turn_no: (
        "folded work", covered + 1)
    try:
        L.run_attempt("task", VM(), cfg, _sink())
    finally:
        L.chat, L._fold_summary = old_chat, old_fold

    assert len(seen) == 1
    assert "Some earlier conversation context was compressed" in seen[0]
