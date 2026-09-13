"""Focused tests for opt-in, evidence-only multi-action recurrence reporting."""

import core.loop as loop
from config.settings import load
from core.trace import ArtifactSink
from env.vm import Trace


A = '{"program":{"lang":"bash","code":"echo A"}}'
B = '{"program":{"lang":"bash","code":"echo B"}}'
C = '{"program":{"lang":"bash","code":"echo C"}}'
X = '{"program":{"lang":"bash","code":"echo detour"}}'
DONE = '{"done":null}'


def test_recurrence_fingerprint_survives_an_unrelated_detour():
    tracker = loop._ActionRecurrence()
    for sig in ["A", "B", "C", "X", "A"]:
        assert tracker.add(sig) is None
    # The earlier A->B fingerprint remains available despite C->X in between.
    assert tracker.add("B") == (0, 4, 2)


def _drive(monkeypatch, tmp_path, replies, enabled):
    cfg = load(None)
    cfg.max_iters = 20
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = False
    cfg.practice_mode = True
    cfg.practice_done_requires = ""
    cfg.cycle_evidence = enabled
    seen_users = []
    script = list(replies)

    def fake_chat(_model, _system, user, **_kwargs):
        seen_users.append(user)
        return script.pop(0)

    class VM:
        calls = 0

        def run_command(self, *_args, **_kwargs):
            return ""

        def run_script(self, _lang, _code, timeout=600):
            self.calls += 1
            # Deliberately changing results: the observation claims only that the
            # action requests recur, never that machine state/output is unchanged.
            return Trace(stdout=f"result-{self.calls}\n[exit 0]", exit_code=0,
                         secs=0.1)

        def fetch_file(self, _path, max_bytes=0):
            return None, "not used"

    monkeypatch.setattr(loop, "chat", fake_chat)
    result, _ = loop.run_attempt(
        "practice freely", VM(), cfg, ArtifactSink(str(tmp_path / "trace")))
    return result, seen_users


def test_recurring_block_adds_evidence_without_gating_or_stalling(monkeypatch,
                                                                  tmp_path):
    result, users = _drive(monkeypatch, tmp_path,
                           [A, B, C, X, A, B, DONE], enabled=True)

    notes = [u for u in users if "LIVENESS OBSERVATION" in u]
    assert result.status == "done"
    assert result.programs_run == 6
    assert len(notes) == 1
    assert "actions 1-2 and 5-6" in notes[0]
    assert "same 2-action block" in notes[0]
    assert "not inferred whether the recurrence is useful" in notes[0]


def test_default_off_is_byte_inert_for_the_same_recurrence(monkeypatch, tmp_path):
    assert load(None).cycle_evidence is False
    result, users = _drive(monkeypatch, tmp_path,
                           [A, B, C, X, A, B, DONE], enabled=False)

    assert result.status == "done"
    assert all("LIVENESS OBSERVATION" not in u for u in users)


def test_e10_agentic_roles_enable_cycle_evidence():
    for name in ("actor", "verify", "curriculum", "memory"):
        assert load(f"config/e10_{name}.yaml").cycle_evidence is True
