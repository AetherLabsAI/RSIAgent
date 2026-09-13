"""Regression tests for lossless native-Look conversation history.

The harness must preserve evidence, not interpret it: exact delivered pixels survive
ordinary turns, transcript serialization, and persistent-Verifier recovery while the
agent remains free to decide how to use them.
"""
import base64
import copy
import io
import json

from PIL import Image

import core.loop as loop
import core.verifier as verifier
from config.settings import load
from core.trace import ArtifactSink
from env.vm import Trace
from llm.client import (DURABLE_IMAGES_FIELD, _request_messages,
                        durable_user_message)


def _png(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (9, 7), color).save(buf, format="PNG")
    return buf.getvalue()


def test_exact_images_survive_json_and_rehydrate_at_wire_boundary():
    images = [_png((i, i + 1, i + 2)) for i in range(0, 24, 3)]
    recorded = durable_user_message("the prior native Look", images)

    # Transcript storage is complete and JSON-safe; there is no image-count or byte
    # cap hidden in the durable representation.
    restored = json.loads(json.dumps(recorded))
    attachments = restored[DURABLE_IMAGES_FIELD]
    assert len(attachments) == len(images)
    assert [base64.b64decode(item["data"]) for item in attachments] == images

    history = [restored, {"role": "assistant", "content": "next action"}]
    wire = _request_messages("system", "continue", history=history)
    historical_content = wire[1]["content"]
    assert historical_content[0] == {
        "type": "text", "text": "the prior native Look"}
    urls = [part["image_url"]["url"] for part in historical_content[1:]]
    assert len(urls) == len(images)
    assert all(url.startswith("data:image/png;base64,") for url in urls)
    assert DURABLE_IMAGES_FIELD not in wire[1]


def test_native_look_pixels_remain_in_later_agent_history(tmp_path, monkeypatch):
    cfg = load(None)
    cfg.model = "native/sighted"
    cfg.vision_model = ""
    cfg.max_iters = 4
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False
    cfg.wall_clock_secs = 60

    pixels = _png()
    replies = [
        '{"look":{"path":"/home/user/evidence.png","question":"inspect it"}}',
        '{"program":{"lang":"bash","code":"echo observed"}}',
        ('{"done":{"checks":[{"desc":"evidence remains present",'
         '"probe":"test -f /home/user/evidence.png && echo PASS"}]}}'),
    ]
    calls = []

    def fake_chat(_model, _system, _user, *, history=None, image=None, **_kwargs):
        calls.append({
            "history": copy.deepcopy(history or []),
            "image": copy.deepcopy(image),
        })
        return replies.pop(0)

    class VM:
        def run_command(self, _cmd, timeout=30, **_kwargs):
            return "PASS"

        def run_script(self, _lang, _code, timeout=600):
            return Trace(stdout="observed\n[exit 0]", exit_code=0, secs=0.1)

        def fetch_file(self, _path, max_bytes=0):
            return pixels, None

    monkeypatch.setattr(loop, "chat", fake_chat)
    _result, history = loop.run_attempt(
        "inspect", VM(), cfg, ArtifactSink(str(tmp_path / "run")))

    image_turns = [message for message in history
                   if message.get(DURABLE_IMAGES_FIELD)]
    assert len(image_turns) == 1
    archived = image_turns[0][DURABLE_IMAGES_FIELD]
    delivered = calls[1]["image"]
    delivered = delivered if isinstance(delivered, list) else [delivered]
    assert [base64.b64decode(item["data"]) for item in archived] == delivered

    # The Look is current-call input on call 2, then durable historical input—not a
    # newly injected image—on call 3.
    assert calls[1]["image"] is not None
    assert calls[2]["image"] is None
    assert any(message.get(DURABLE_IMAGES_FIELD)
               for message in calls[2]["history"])


def test_verifier_checkpoint_and_semantic_replay_preserve_look_pixels():
    pixels = _png()
    image_user = durable_user_message("the image is attached", pixels)
    raw = [
        {"role": "user", "content": "full verifier opening"},
        {"role": "assistant", "content":
         '{"look":{"path":"/tmp/reference.png","question":"read it"}}'},
        image_user,
        {"role": "assistant", "content": "I will inspect the image carefully."},
        {"role": "user", "content": "strict action-format recovery"},
        {"role": "assistant", "content": "I will now act."},
    ]

    checkpoint, pending, pending_images, last_turn = \
        verifier._agentic_substantive_checkpoint([], raw)
    assert len(checkpoint) == 2
    assert all("I will" not in message["content"] for message in checkpoint)
    assert pending == "the image is attached"
    assert verifier._agentic_image_bytes(pending_images) == [pixels]
    assert isinstance(last_turn, verifier.Look)

    replay, replay_pending, replay_images, replay_turn = \
        verifier._agentic_semantic_replay(raw)
    retained = [message for message in replay
                if message.get(DURABLE_IMAGES_FIELD)]
    assert retained == []
    assert all("I will" not in message["content"] for message in replay)
    assert replay_pending == "the image is attached"
    assert verifier._agentic_image_bytes(replay_images) == [pixels]
    assert isinstance(replay_turn, verifier.Look)


def test_recovery_opening_pixels_are_reattached_and_archived(tmp_path, monkeypatch):
    cfg = load(None)
    cfg.model = "native/sighted"
    cfg.vision_model = ""
    cfg.max_iters = 1
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False
    cfg.wall_clock_secs = 60
    pixels = _png((91, 92, 93))
    calls = []

    def fake_chat(_model, _system, _user, *, history=None, image=None, **_kwargs):
        calls.append({"history": copy.deepcopy(history or []), "image": image})
        return ('{"done":{"checks":[{"desc":"state exists",'
                '"probe":"echo PASS"}]}}')

    class VM:
        def run_command(self, _cmd, timeout=30, **_kwargs):
            return "PASS"

    monkeypatch.setattr(loop, "chat", fake_chat)
    _result, history = loop.run_attempt(
        "recover", VM(), cfg, ArtifactSink(str(tmp_path / "run")),
        opening_image=[pixels])

    assert calls[0]["image"] == [pixels]
    assert base64.b64decode(
        history[0][DURABLE_IMAGES_FIELD][0]["data"]) == pixels
