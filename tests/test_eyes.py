"""Offline tests for the v32 eyes ("two witnesses + judge") — no VM, no API.

chat() and prepare_look_images() are monkeypatched; each test scripts the witnesses'
and judge's outputs and asserts: single-shot passthrough, full/partial consensus,
SPLIT -> verbatim readings + resolution protocol + .split flag (and never a merged
answer), judge-failure -> readings delivered without a split claim, witness-error
filtering.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.eyes as eyes
from config.settings import Config


class FakeChat:
    """Scripted chat(): returns queued answers per model, records every call."""

    def __init__(self, script):
        self.script = dict(script)      # model -> list of answers (popped in order)
        self.calls = []                 # model, system head, temp, token budgets

    def __call__(self, model, system, user, max_tokens=0, temperature=0.0,
                 reasoning_effort="", history=None, image=None,
                 reasoning_max_tokens=0, **_kwargs):
        self.calls.append((model, system[:40], temperature, max_tokens,
                           reasoning_max_tokens))
        q = self.script.get(model)
        if not q:
            raise RuntimeError(f"unscripted model {model}")
        return q.pop(0)


def _cfg(**kw):
    c = Config()
    c.model = "primary"
    c.vision_model = "vlm"
    c.look_ensemble = 2
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _patch(monkeypatch, fake):
    monkeypatch.setattr(eyes, "chat", fake)
    monkeypatch.setattr(eyes, "prepare_look_images", lambda datas, region=None, max_side=None: ("IMG", ""))
    monkeypatch.setattr(eyes, "_vision_system", lambda: "VS")


def test_single_shot_passthrough(monkeypatch):
    fake = FakeChat({"vlm": ["a cat on a mat"]})
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(look_ensemble=1), "what is shown?", [b"x"])
    assert out == "a cat on a mat"
    assert getattr(out, "split", None) is False
    assert len(fake.calls) == 1 and fake.calls[0][2] == 0.0


def test_single_shot_forwards_configured_vision_budgets(monkeypatch):
    fake = FakeChat({"vlm": ["budgeted read"]})
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(
        _cfg(look_ensemble=1, vision_max_tokens=16000,
             reasoning_max_tokens=7000),
        "read the small labels", [b"x"])
    assert out == "budgeted read"
    assert len(fake.calls) == 1
    assert fake.calls[0][3:] == (16000, 7000)


def test_full_consensus(monkeypatch):
    fake = FakeChat({
        "vlm": ["reading one: filter5, rain", "reading two: it is filter5"],
        "primary": ["AGREEMENT: full\nANSWER: it is filter5, diagonal rain streaks"],
    })
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(), "which filter?", [b"x"])
    assert "filter5" in out
    assert "2 independent visual reads agree" in out
    assert out.split is False
    vlm_calls = [c for c in fake.calls if c[0] == "vlm"]
    assert len(vlm_calls) == 2
    assert vlm_calls[0][2] == 0.0 and vlm_calls[1][2] == 0.8   # canonical + hot skeptic
    assert len([c for c in fake.calls if c[0] == "primary"]) == 1  # one judge call


def test_partial_consensus(monkeypatch):
    fake = FakeChat({
        "vlm": ["theme X, dark bg", "theme X, blue bg"],
        "primary": ["AGREEMENT: partial\nANSWER: theme X\nDETAILS-DIFFER: background color (dark vs blue)"],
    })
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(), "which theme?", [b"x"])
    assert "theme X" in out and "core answer agreed" in out
    assert out.split is False


def test_split_delivers_verbatim_readings_and_protocol(monkeypatch):
    fake = FakeChat({
        "vlm": ["I count 2 flowers on the upper band",
                "I count 5 flowers on the upper band"],
        "primary": ["AGREEMENT: split"],
    })
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(), "how many flowers?", [b"x"])
    assert out.split is True
    # both readings verbatim, clearly labeled — and no merged/average answer
    assert "READING A (canonical):" in out and "I count 2 flowers" in out
    assert "READING B (independent skeptic):" in out and "I count 5 flowers" in out
    assert "READINGS CONFLICT" in out and "do not average" in out
    assert "ENUMERATE" in out and "COMPUTE" in out           # the resolution protocol
    # the judge must not have been asked twice / no consensus text leaked
    assert "reads agree" not in out


def test_judge_failure_no_split_claim(monkeypatch):
    class Boom(FakeChat):
        def __call__(self, model, *a, **kw):
            if model == "primary":
                raise RuntimeError("judge model down")
            return super().__call__(model, *a, **kw)
    fake = Boom({"vlm": ["r1", "r2"]})
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(), "q?", [b"x"])
    assert "[independent read 1] r1" in out and "[independent read 2] r2" in out
    assert "judge unavailable" in out
    assert out.split is False        # infra failure must not trigger the done-gate


def test_witness_errors_filtered(monkeypatch):
    calls = {"n": 0}

    def flaky(model, system, user, max_tokens=0, temperature=0.0,
              reasoning_effort="", history=None, image=None,
              reasoning_max_tokens=0, **_kwargs):
        if model == "vlm":
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("eyes down")
            return "only good read"
        raise AssertionError("judge must not run for a single surviving read")
    monkeypatch.setattr(eyes, "chat", flaky)
    monkeypatch.setattr(eyes, "prepare_look_images", lambda d, region=None, max_side=None: ("IMG", ""))
    monkeypatch.setattr(eyes, "_vision_system", lambda: "VS")
    out = eyes.ensemble_look(_cfg(), "q?", [b"x"])
    assert out == "only good read"
    assert out.split is False


def test_unresolved_splits_message_shape():
    msg = eyes.unresolved_splits_message(
        [{'turn': 7, 'path': '/tmp/a.png', 'q': 'how many?'},
         {'turn': 9, 'path': 'screen:', 'q': 'which theme?'}])
    assert "turn 7: /tmp/a.png" in msg and "turn 9: screen:" in msg
    assert "READINGS CONFLICT" in msg and "declare done again" in msg


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


def test_heterogeneous_second_witness(monkeypatch):
    fake = FakeChat({
        "vlm": ["reading A"],
        "vlm2": ["reading B"],
        "primary": ["AGREEMENT: full\nANSWER: same thing"],
    })
    _patch(monkeypatch, fake)
    out = eyes.ensemble_look(_cfg(vision_model_2="vlm2"), "q?", [b"x"])
    models = [c[0] for c in fake.calls]
    assert models.count("vlm") == 1 and models.count("vlm2") == 1, models
    assert "same thing" in out
