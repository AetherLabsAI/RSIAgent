"""Live OrcaRouter check — real credential, real endpoint, real model call.

This is the one test file that talks to the network. It is skipped unless a real
OrcaRouter credential is present, so the portable suite stays offline and
deterministic; the campaign's live check runs it explicitly with the credential
in the environment.

It drives the *implemented provider path* rather than a bare HTTP call: the
credential is resolved through :mod:`llm.provider`, the endpoint through
:func:`llm.client._client_base_url`, and the completion through
:func:`llm.client.chat` with the same OpenAI SDK client the agent loop uses. A
separate ``curl`` returning 200 would prove nothing about the wiring.

    RSIAGENT_LLM_PROVIDER=orcarouter ORCAROUTER_API_KEY=sk-orca-… \
        python -m pytest -q tests/test_orcarouter_live.py
"""
import os

import pytest

from llm import catalog as CAT
from llm import client as C
from llm import provider as P


KEY_ENV = next((name for name in P.ORCA_KEY_ENV_ALIASES if os.environ.get(name)),
               "")

pytestmark = [
    pytest.mark.skipif(not KEY_ENV,
                       reason=f"no live OrcaRouter credential in "
                              f"{' / '.join(P.ORCA_KEY_ENV_ALIASES)}"),
    pytest.mark.skipif(
        os.environ.get("RSIAGENT_SKIP_LIVE") == "1",
        reason="live checks disabled for this run"),
]


@pytest.fixture(autouse=True)
def live_provider(monkeypatch):
    """Route the harness at OrcaRouter for the duration of one live test."""
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    P.reset_credential_state()
    CAT.reset_catalog_cache()
    yield
    P.reset_credential_state()
    CAT.reset_catalog_cache()


def test_live_credential_resolves_through_the_shared_seam():
    credential = P.acquire_credential(P.ORCAROUTER)
    assert credential.api_key.startswith("sk-orca-")
    assert credential.provider_id == "orcarouter"
    assert credential.source in {"api_key", "pkce"}
    # The generation identifies it without exposing it.
    assert credential.api_key not in credential.generation


def test_live_inference_and_catalog_use_their_own_origins():
    assert C._client_base_url() == "https://api.orcarouter.ai/v1"
    assert P.auth_base() == "https://www.orcarouter.ai"
    from llm import connect as CON
    assert CON.exchange_url() == "https://www.orcarouter.ai/api/v1/auth/keys"


def test_live_catalog_lists_the_workspace_models():
    result = CAT.discover(CAT.CAPABILITY_CHAT, refresh=True)
    assert result.degraded is False, (
        f"live discovery failed: {result.detail}")
    assert result.models, "the workspace catalog returned no chat models"
    # Every id keeps its vendor namespace verbatim.
    for model_id in result.ids:
        assert "/" in model_id
    # The live result is the workspace's own catalog, not the offline seed
    # standing in for it. `orcarouter/auto` legitimately appears in both, so the
    # check is on the whole set: a live fetch that returned only seed entries
    # would mean discovery silently degraded.
    seed_ids = {record.id for record in CAT.verified_seed()}
    assert set(result.ids) != seed_ids
    for record in result.models:
        assert record.source == CAT.SOURCE_LIVE


def test_live_multimodal_selector_only_offers_declared_image_models():
    text = CAT.discover(CAT.CAPABILITY_CHAT, refresh=True)
    images = CAT.discover(CAT.CAPABILITY_MULTIMODAL, modality="image")
    assert set(images.ids) <= set(text.ids), \
        "an image selector must be a subset of the chat catalog"
    for record in images.models:
        assert "image" in {m.lower() for m in record.input_modalities}, \
            f"{record.id} was offered for images without declaring image input"


def test_live_chat_completion_through_the_provider_path():
    """One real completion, through the exact client the agent loop uses."""
    result = CAT.discover(CAT.CAPABILITY_CHAT, refresh=True)
    assert not result.degraded, result.detail
    model = os.environ.get("ORCA_LIVE_MODEL") or next(
        (model_id for model_id in result.ids if model_id.startswith("deepseek/")),
        result.ids[0])

    answer = C.chat(model, "Answer with the single word: pong.",
                    "ping", max_tokens=512, temperature=0.0)

    assert isinstance(answer, str)
    assert answer.strip(), f"{model} returned an empty completion"
    # Provenance: the transport records which credential generation this process
    # used, without the credential itself ever being handled by the caller.
    assert C.active_credential_generation() == P.acquire_credential(
        P.ORCAROUTER).generation


def test_live_rejected_key_is_terminal_and_not_retried(monkeypatch, tmp_path):
    """The real service's 401 must classify as reauth, not as a retry.

    The credential is deliberately fake. Nothing here burns a real key or
    attempts a fake refresh: the point is that the *shipped* transport maps a
    genuine upstream rejection onto the terminal path.
    """
    secret = tmp_path / "rsi.env"
    secret.write_text("ORCA_API_KEY=sk-orca-invalid-live-probe-000000\n")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    P.reset_credential_state()

    result = CAT.discover(CAT.CAPABILITY_CHAT, refresh=True)
    model = result.ids[0] if result.ids else "orcarouter/auto"

    with pytest.raises(C.ReauthRequired) as raised:
        C.chat(model, "system", "user", max_tokens=16)

    assert raised.value.status_code in (401, 403)
    assert raised.value.recoverable is False
    # The rejected generation is marked, and the message tells the user what to
    # do next instead of looping on a dead credential.
    assert "llm.connect login" in str(raised.value)
    assert "sk-orca-invalid-live-probe" not in str(raised.value)
