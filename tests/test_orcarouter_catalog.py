"""OrcaRouter model discovery and capability filtering.

The fixtures deliberately cover every capability shape the repository can be
asked about — text-only chat, image-input chat, embedding, image generation,
video, rerank — even though RSIAgent only has text and image-understanding entry
points today, so that a future entry point cannot silently inherit a list that
was never filtered for it.

The decisive assertions are about what reaches a *selector*: switching provider
re-reads the catalog, adding an image attachment leaves only models that declare
image input, and a failed catalog yields a labelled verified fallback rather than
free-text entry or an unverified example list.
"""
import json

import pytest

from llm import catalog as CAT
from llm import provider as P


def entry(model_id, endpoints=("openai", "openai-response"), modalities=None,
          **extra):
    record = {"id": model_id, "object": "model", "created": 0,
              "owned_by": "orcarouter", "supported_endpoint_types": list(endpoints)}
    if modalities is not None:
        record["architecture"] = {"input_modalities": list(modalities)}
    record.update(extra)
    return record


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name in ("ORCA_API_KEY", "ORCAROUTER_API_KEY", "OPENROUTER_API_KEY",
                 "RSIAGENT_LLM_PROVIDER", "ORCA_BASE_URL",
                 "ORCA_AUTH_BASE_URL", "ORCA_API_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCA_API_KEY", "sk-orca-catalog-test")
    CAT.reset_catalog_cache()
    P.reset_credential_state()
    yield
    CAT.reset_catalog_cache()
    P.reset_credential_state()


# One fixture per capability shape, including the ones this repository does not
# currently drive, so each filter has something it must accept and something it
# must reject.
CATALOG = {
    "data": [
        entry("noendpoint/x", endpoints=()),
        entry("plain/text-chat", modalities=["text"],
              context_length=128000,
              reasoning={"efforts": ["low", "high"]}),
        entry("imagemodel/vision-chat", modalities=["text", "image"],
              context_length=200000),
        entry("audiomodel/audio-chat", modalities=["text", "audio"]),
        entry("embed/model", endpoints=["embeddings"]),
        entry("draw/image-gen", endpoints=["image-generation"]),
        entry("movie/video-gen", endpoints=["openai-video"]),
        entry("rank/reranker", endpoints=["jina-rerank"]),
        entry("multiroute/everything", endpoints=["openai", "image-generation"]),
        entry("deepseek/deepseek-v4-flash-vision-exp"),   # name suggests vision
        {"id": ""},                                        # malformed
        "not-an-object",                                   # malformed
        entry("overlong/" + "x" * 400),                    # over the id ceiling
    ]
}


def fake_fetch(payload, *, calls=None):
    def fetch(**_kwargs):
        if calls is not None:
            calls.append(1)
        return CAT.parse_catalog(payload)
    return fetch


# --- parsing and bounds --------------------------------------------------------

def test_parse_keeps_ids_verbatim_and_drops_malformed_records():
    records = CAT.parse_catalog(CATALOG)
    ids = [record.id for record in records]
    # vendor/model namespaces are preserved exactly, never rewritten.
    assert "deepseek/deepseek-v4-flash-vision-exp" in ids
    assert "" not in ids
    assert not any(len(model_id) > CAT._MAX_ID_LENGTH for model_id in ids)
    assert len(ids) == len(set(ids)), "duplicates must be collapsed"


def test_parse_is_bounded_on_count():
    payload = {"data": [entry(f"vendor/model-{i}") for i in range(500)]}
    assert len(CAT.parse_catalog(payload, limit=10)) == 10


def test_parse_tolerates_a_missing_or_wrong_typed_field():
    records = CAT.parse_catalog({"data": [
        {"id": "vendor/one", "supported_endpoint_types": "openai"},
        {"id": "vendor/two", "architecture": "not-an-object",
         "context_length": "big"},
        {"id": "vendor/three", "context_length": True},
    ]})
    by_id = {record.id: record for record in records}
    assert by_id["vendor/one"].endpoint_types == ()
    assert by_id["vendor/two"].input_modalities == ()
    assert by_id["vendor/two"].context_length is None
    assert by_id["vendor/three"].context_length is None


def test_parse_handles_a_non_list_body():
    assert CAT.parse_catalog({"data": "nope"}) == []
    assert CAT.parse_catalog(None) == []


# --- capability filters --------------------------------------------------------

def test_chat_filter_requires_a_text_endpoint_and_excludes_non_text_models():
    ids = list(CAT.select_models(CAT.parse_catalog(CATALOG), CAT.CAPABILITY_CHAT))
    ids = [record.id for record in ids]
    assert "plain/text-chat" in ids
    assert "imagemodel/vision-chat" in ids      # text-capable *and* multimodal
    assert "multiroute/everything" in ids       # routes include a text route
    assert "noendpoint/x" not in ids
    assert "embed/model" not in ids
    assert "draw/image-gen" not in ids
    assert "movie/video-gen" not in ids
    assert "rank/reranker" not in ids


def test_chat_filter_admits_each_declared_text_route():
    for route in ("openai", "anthropic", "gemini", "openai-response"):
        records = CAT.parse_catalog({"data": [entry("v/m", endpoints=[route])]})
        assert CAT.select_models(records, CAT.CAPABILITY_CHAT), route


def test_multimodal_filter_is_fail_closed_on_undeclared_modality():
    """A model that does not *declare* image input is not offered for images."""
    records = CAT.parse_catalog(CATALOG)
    ids = [record.id for record in CAT.select_models(
        records, CAT.CAPABILITY_MULTIMODAL, modality="image")]
    assert ids == ["imagemodel/vision-chat"]
    # The vision-sounding slug whose catalog entry declares nothing is excluded.
    assert "deepseek/deepseek-v4-flash-vision-exp" not in ids
    # And a text-only chat model is excluded.
    assert "plain/text-chat" not in ids


def test_multimodal_filter_rejects_a_different_modality():
    records = CAT.parse_catalog(CATALOG)
    ids = [record.id for record in CAT.select_models(
        records, CAT.CAPABILITY_MULTIMODAL, modality="audio")]
    assert ids == ["audiomodel/audio-chat"]
    ids = [record.id for record in CAT.select_models(
        records, CAT.CAPABILITY_MULTIMODAL, modality="video")]
    assert ids == []


def test_multimodal_filter_without_a_modality_selects_nothing():
    """Failing closed beats guessing which modality the caller meant."""
    records = CAT.parse_catalog(CATALOG)
    assert CAT.select_models(records, CAT.CAPABILITY_MULTIMODAL) == ()


def test_embedding_image_video_and_rerank_match_their_own_endpoint_only():
    records = CAT.parse_catalog(CATALOG)
    assert [r.id for r in CAT.select_models(records, CAT.CAPABILITY_EMBEDDING)] \
        == ["embed/model"]
    assert [r.id for r in CAT.select_models(records, CAT.CAPABILITY_IMAGE)] \
        == ["draw/image-gen", "multiroute/everything"]
    assert [r.id for r in CAT.select_models(records, CAT.CAPABILITY_VIDEO)] \
        == ["movie/video-gen"]
    assert [r.id for r in CAT.select_models(records, CAT.CAPABILITY_RERANK)] \
        == ["rank/reranker"]


def test_an_unknown_capability_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        CAT.select_models(CAT.parse_catalog(CATALOG), "chat-ish")


# --- provider wiring -----------------------------------------------------------

def test_switching_provider_changes_the_catalog_origin(monkeypatch):
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    assert CAT.api_base() == "https://api.orcarouter.ai/v1"
    monkeypatch.setenv("ORCA_API_BASE_URL", "https://gateway.internal.example/v1")
    assert CAT.api_base() == "https://gateway.internal.example/v1"


def test_live_catalog_is_fetched_from_the_configured_origin(monkeypatch):
    monkeypatch.setenv("ORCA_API_BASE_URL", "https://gateway.internal.example/v1")
    seen = {}

    def fetch(**kwargs):
        seen["url_base"] = CAT.api_base(kwargs.get("environment"))
        return CAT.parse_catalog(CATALOG)

    result = CAT.discover(fetch=fetch)
    assert seen["url_base"] == "https://gateway.internal.example/v1"
    assert result.source == CAT.SOURCE_LIVE
    assert result.degraded is False


def test_selector_options_come_from_the_catalog_and_are_live_filtered():
    result = CAT.discover(fetch=fake_fetch(CATALOG))
    # Every text-capable model: the plain chat model, both multimodal chat
    # models, the multi-route model, the vision-named slug — admitted *because
    # its entry declares text routes*, not because of its name — and the
    # over-long id, truncated to the documented ceiling rather than dropped.
    assert result.ids == (
        "plain/text-chat", "imagemodel/vision-chat", "audiomodel/audio-chat",
        "multiroute/everything", "deepseek/deepseek-v4-flash-vision-exp",
        "overlong/" + "x" * (CAT._MAX_ID_LENGTH - len("overlong/")))
    assert result.source == CAT.SOURCE_LIVE


def test_adding_an_image_attachment_leaves_only_image_declaring_models():
    """The options handed to the selector shrink; the old value is invalid."""
    text = CAT.discover(fetch=fake_fetch(CATALOG))
    chosen = "plain/text-chat"
    assert CAT.require_compatible(chosen, text) == chosen

    CAT.reset_catalog_cache()
    multimodal = CAT.discover(CAT.CAPABILITY_MULTIMODAL, modality="image",
                              fetch=fake_fetch(CATALOG))
    assert multimodal.ids == ("imagemodel/vision-chat",)
    with pytest.raises(CAT.IncompatibleModel):
        CAT.require_compatible(chosen, multimodal, modality="image")


def test_resolve_model_clears_a_stale_selection_instead_of_keeping_it():
    model_id, result = CAT.resolve_model(
        CAT.CAPABILITY_MULTIMODAL, preferred="plain/text-chat", modality="image",
        fetch=fake_fetch(CATALOG))
    assert model_id == "imagemodel/vision-chat"
    assert "plain/text-chat" not in result.ids


def test_resolve_model_reports_an_empty_state_when_nothing_is_compatible():
    model_id, result = CAT.resolve_model(
        CAT.CAPABILITY_MULTIMODAL, preferred="plain/text-chat", modality="video",
        fetch=fake_fetch(CATALOG))
    assert model_id == ""
    assert result.ids == ()


# --- degradation, caching, and the verified fallback ---------------------------

def test_live_discovery_is_authoritative_and_is_not_merged_with_the_seed():
    result = CAT.discover(fetch=fake_fetch(CATALOG))
    seed_ids = {record.id for record in CAT.verified_seed()}
    assert not seed_ids & set(result.ids), \
        "a successful live catalog must replace the seed, not be mixed with it"


def test_a_catalog_outage_falls_back_to_the_labelled_verified_seed():
    def broken(**_kwargs):
        raise CAT.CatalogError("catalog request failed: URLError")

    result = CAT.discover(fetch=broken)
    assert result.degraded is True
    assert result.source == CAT.SOURCE_SEED
    assert set(result.ids) == {record.id for record in CAT.verified_seed()}
    assert "URLError" in result.detail
    assert "fallback" in result.label()
    assert "live" not in result.label().split("(")[0]


def test_the_verified_seed_keeps_reasoning_and_modality_metadata():
    """Degrading must not quietly strip capabilities the seed advertises."""
    gpt = next(record for record in CAT.verified_seed()
               if record.id == "openai/gpt-5.5")
    assert set(gpt.reasoning_efforts) == {"low", "medium", "high", "xhigh"}
    assert gpt.is_text_capable
    # The public catalog publishes no modality field, so the seed declares none
    # — which keeps these models out of the image selector rather than guessing.
    for record in CAT.verified_seed():
        assert record.input_modalities == ()


def test_last_known_good_is_preferred_over_the_seed_after_a_later_failure():
    CAT.discover(fetch=fake_fetch(CATALOG))
    # A refresh that fails must keep the previously fetched catalog, not revert
    # to the seed.
    result = CAT.discover(refresh=True,
                          fetch=lambda **_kw: (_ for _ in ()).throw(
                              CAT.CatalogError("offline")))
    assert result.source == CAT.SOURCE_LAST_KNOWN_GOOD
    assert result.degraded is True
    assert "imagemodel/vision-chat" in result.ids


def test_an_outage_never_falls_back_to_free_text_or_an_unverified_list():
    result = CAT.discover(fetch=lambda **_kw: (_ for _ in ()).throw(
        CAT.CatalogError("offline")))
    # Every offered option is a real, verified ID — no placeholder, no "*".
    for model_id in result.ids:
        assert "/" in model_id
        assert model_id != "*"
    assert result.ids, "the seed must keep a fresh installation usable"


def test_cache_avoids_a_second_fetch_within_the_ttl():
    calls = []
    CAT.discover(fetch=fake_fetch(CATALOG, calls=calls))
    CAT.discover(fetch=fake_fetch(CATALOG, calls=calls))
    assert len(calls) == 1
    CAT.discover(refresh=True, fetch=fake_fetch(CATALOG, calls=calls))
    assert len(calls) == 2


def test_a_cached_catalog_is_still_filtered_per_call():
    """Cache the authority list; never hand a selector an unfiltered set.

    One fetch backs both selectors, and changing the attached modality re-filters
    the cached records rather than reusing the previous answer.
    """
    calls = []
    text = CAT.discover(fetch=fake_fetch(CATALOG, calls=calls))
    images = CAT.discover(CAT.CAPABILITY_MULTIMODAL, modality="image",
                          fetch=fake_fetch(CATALOG, calls=calls))
    audio = CAT.discover(CAT.CAPABILITY_MULTIMODAL, modality="audio",
                         fetch=fake_fetch(CATALOG, calls=calls))

    assert len(calls) == 1, "the chat and multimodal selectors share one fetch"
    assert images.ids == ("imagemodel/vision-chat",)
    assert audio.ids == ("audiomodel/audio-chat",)
    assert set(images.ids) < set(text.ids)


# --- HTTP layer ----------------------------------------------------------------

def test_fetch_sends_the_credential_and_the_capability_query(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def read(self, _limit):
            return json.dumps(CATALOG).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(CAT.urllib.request, "urlopen", urlopen)
    records = CAT.fetch_catalog(capability=CAT.CAPABILITY_CHAT, timeout=3.0)

    assert seen["url"] == "https://api.orcarouter.ai/v1/models?capability=chat"
    assert seen["authorization"] == "Bearer sk-orca-catalog-test"
    assert seen["timeout"] == 3.0
    assert records


def test_fetch_uses_the_auth_credentials_without_exposing_them(monkeypatch):
    import urllib.error

    def urlopen(_request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.orcarouter.ai/v1/models", 401, "Unauthorized", {},
            None)

    monkeypatch.setattr(CAT.urllib.request, "urlopen", urlopen)
    with pytest.raises(CAT.CatalogError) as raised:
        CAT.fetch_catalog()
    # The status is reported; the body (which can quote the key) is not.
    assert "401" in str(raised.value)
    assert "sk-orca-catalog-test" not in str(raised.value)


def test_fetch_refuses_an_oversized_response(monkeypatch):
    class Response:
        status = 200

        def read(self, limit):
            return b"x" * (limit + 1)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(CAT.urllib.request, "urlopen",
                        lambda *_a, **_kw: Response())
    with pytest.raises(CAT.CatalogError) as raised:
        CAT.fetch_catalog(max_bytes=64)
    assert "size ceiling" in str(raised.value)


def test_fetch_reports_invalid_json_without_echoing_it(monkeypatch):
    class Response:
        status = 200

        def read(self, _limit):
            return b"{not json"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(CAT.urllib.request, "urlopen",
                        lambda *_a, **_kw: Response())
    with pytest.raises(CAT.CatalogError) as raised:
        CAT.fetch_catalog()
    assert "not valid JSON" in str(raised.value)


# --- the selection surface (`python -m llm.models`) -----------------------------
#
# These drive the command a user actually runs, so the filtered options are
# proven to reach a real selector rather than existing only inside a helper.

from llm import models as MODELS   # noqa: E402


def test_list_prints_the_filtered_options_a_user_selects_from(monkeypatch,
                                                              capsys):
    monkeypatch.setattr(CAT, "discover",
                        lambda *a, **kw: CAT.CatalogResult(
                            CAT.select_models(CAT.parse_catalog(CATALOG), "chat"),
                            CAT.SOURCE_LIVE, "chat", False))
    assert MODELS.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "plain/text-chat" in out
    assert "imagemodel/vision-chat" in out
    # A non-text model must never appear in a text selector.
    assert "draw/image-gen" not in out
    assert "rank/reranker" not in out
    assert "embed/model" not in out


def test_list_for_images_shows_only_models_declaring_image_input(monkeypatch,
                                                                 capsys):
    # Capture the real discover before patching the module attribute it lives on.
    real_discover = CAT.discover
    monkeypatch.setattr(CAT, "discover",
                        lambda capability, modality=None, **kw:
                        real_discover(capability, modality=modality,
                                      fetch=fake_fetch(CATALOG)))
    assert MODELS.main(["list", "--capability", "multimodal",
                        "--modality", "image"]) == 0
    out = capsys.readouterr().out
    assert "imagemodel/vision-chat" in out
    assert "plain/text-chat" not in out
    assert "deepseek/deepseek-v4-flash-vision-exp" not in out


def test_list_reports_the_empty_state_rather_than_offering_free_text(monkeypatch,
                                                                    capsys):
    real_discover = CAT.discover
    monkeypatch.setattr(CAT, "discover",
                        lambda *a, **kw: real_discover(
                            "multimodal", modality="video",
                            fetch=fake_fetch(CATALOG)))
    assert MODELS.main(["list", "--capability", "multimodal",
                        "--modality", "video"]) == 0
    out = capsys.readouterr().out
    assert "No model is compatible" in out
    # No placeholder entry, and no invitation to type a model name.
    assert "movie/video-gen" not in out


def test_a_degraded_list_says_the_models_are_not_the_workspace_list(monkeypatch,
                                                                    capsys):
    real_discover = CAT.discover
    monkeypatch.setattr(CAT, "discover",
                        lambda *a, **kw: real_discover(
                            fetch=lambda **_k: (_ for _ in ()).throw(
                                CAT.CatalogError("offline"))))
    assert MODELS.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "verified fallback catalog" in out
    assert "not your workspace list" in out
    assert "openai/gpt-5.5" in out


def test_check_accepts_a_compatible_model_and_rejects_a_stale_one(monkeypatch,
                                                                  capsys):
    monkeypatch.setattr(CAT, "discover",
                        lambda *a, **kw: CAT.CatalogResult(
                            CAT.select_models(CAT.parse_catalog(CATALOG), "chat"),
                            CAT.SOURCE_LIVE, "chat", False))
    assert MODELS.main(["check", "plain/text-chat"]) == 0
    assert "is offered" in capsys.readouterr().out
    # Exit 2 = "not offered", distinct from exit 1 = "could not check".
    assert MODELS.main(["check", "draw/image-gen"]) == 2
    assert "incompatible" in capsys.readouterr().err


def test_list_json_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(CAT, "discover",
                        lambda *a, **kw: CAT.CatalogResult(
                            CAT.select_models(CAT.parse_catalog(CATALOG), "chat"),
                            CAT.SOURCE_LIVE, "chat", False))
    assert MODELS.main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["degraded"] is False
    assert payload["source"] == "live"
    assert payload["api_base"] == "https://api.orcarouter.ai/v1"
    assert "plain/text-chat" in [m["id"] for m in payload["models"]]
    plain = next(m for m in payload["models"] if m["id"] == "plain/text-chat")
    # Metadata survives the trip into the selector payload.
    assert plain["context_length"] == 128000
    assert plain["reasoning_efforts"] == ["low", "high"]
    assert "draw/image-gen" not in [m["id"] for m in payload["models"]]


# --- shapes observed in the real catalog ---------------------------------------
#
# This fixture mirrors the field shapes the public OrcaRouter catalog actually
# returns, so the filter is exercised against reality rather than against a
# convenient simplification. Verified 2026-09-16 against
# https://api.orcarouter.ai/v1/models?capability=chat.
#
# - an entry that declares text only,
# - an entry that declares text + image,
# - an entry that omits `architecture` entirely,
# - an entry with no context_length,
# - both `orcarouter/*` and `vendor/model` id namespaces.
LIVE_SHAPES = {"data": [
    {"id": "orcarouter/auto", "object": "model", "created": 0,
     "owned_by": "orcarouter",
     "supported_endpoint_types": ["openai", "openai-response", "anthropic",
                                  "gemini"]},
    {"id": "orcarouter/fusion-mini", "object": "model", "created": 0,
     "owned_by": "orcarouter", "context_length": 1000000,
     "supported_endpoint_types": ["openai", "openai-response", "anthropic",
                                  "gemini"]},
    {"id": "deepseek/deepseek-v4-flash", "object": "model",
     "created": 1626777600, "owned_by": "custom", "context_length": 1048576,
     "max_completion_tokens": 384000,
     "supported_endpoint_types": ["openai", "openai-response"],
     "architecture": {"input_modalities": ["text"], "output_modalities": None}},
    {"id": "deepseek/deepseek-v4-flash-vision-exp", "object": "model",
     "created": 1626777600, "owned_by": "DeepSeek", "context_length": 1048576,
     "supported_endpoint_types": ["openai", "openai-response", "anthropic"],
     "architecture": {"input_modalities": ["text", "image"],
                      "output_modalities": ["text"]}},
]}


def test_real_catalog_shapes_filter_exactly_as_the_live_service_requires():
    records = CAT.parse_catalog(LIVE_SHAPES)
    chat = [r.id for r in CAT.select_models(records, CAT.CAPABILITY_CHAT)]
    images = [r.id for r in CAT.select_models(
        records, CAT.CAPABILITY_MULTIMODAL, modality="image")]

    # Every entry declares text routes, so all four are chat-selectable.
    assert chat == ["orcarouter/auto", "orcarouter/fusion-mini",
                    "deepseek/deepseek-v4-flash",
                    "deepseek/deepseek-v4-flash-vision-exp"]
    # Only the entry that declares image input, even though a sibling declares
    # its modality explicitly as text-only and two declare no modality at all.
    assert images == ["deepseek/deepseek-v4-flash-vision-exp"]
    assert "deepseek/deepseek-v4-flash" not in images   # declared text-only
    assert "orcarouter/auto" not in images              # declared nothing


def test_declared_modality_metadata_survives_parsing():
    records = {r.id: r for r in CAT.parse_catalog(LIVE_SHAPES)}
    assert records["deepseek/deepseek-v4-flash"].input_modalities == ("text",)
    assert records["deepseek/deepseek-v4-flash-vision-exp"].input_modalities \
        == ("text", "image")
    assert records["orcarouter/auto"].input_modalities == ()
    assert records["orcarouter/fusion-mini"].context_length == 1000000
    assert records["orcarouter/auto"].context_length is None


# --- the pre-send guard, and why it is not the requirement ---------------------
#
# The selector below is what satisfies the capability requirement: the options a
# caller binds to are already filtered. The guard in llm.client is a second line
# of defence for a caller that bypassed the selector. These tests pin both, and
# pin the guard's failure mode: it must never block a run.

from llm import client as CLIENT   # noqa: E402


def test_the_guard_warns_when_an_undeclared_model_is_sent_an_image(monkeypatch,
                                                                   caplog):
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.discover(fetch=fake_fetch(CATALOG))          # populate the cache
    with caplog.at_level("WARNING", logger="rsiagent.llm"):
        CLIENT._warn_if_model_cannot_read_images("plain/text-chat", b"\x89PNG")
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "not in the image-capable catalog" in warning
    assert "imagemodel/vision-chat" in warning


def test_the_guard_is_silent_for_a_declared_image_model(monkeypatch, caplog):
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.discover(fetch=fake_fetch(CATALOG))
    with caplog.at_level("WARNING", logger="rsiagent.llm"):
        CLIENT._warn_if_model_cannot_read_images("imagemodel/vision-chat",
                                                 b"\x89PNG")
    assert not [r for r in caplog.records if "image-capable" in r.getMessage()]


def test_the_guard_makes_no_request_and_never_blocks(monkeypatch):
    """A cold cache means silence, not a fetch and certainly not a failure."""
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.reset_catalog_cache()
    monkeypatch.setattr(CAT, "api_base", lambda *a, **kw: "https://invalid.example")
    # No network is reachable here; a guard that fetched would raise.
    CLIENT._warn_if_model_cannot_read_images("plain/text-chat", b"\x89PNG")


def test_the_guard_ignores_non_image_sends(monkeypatch, caplog):
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.discover(fetch=fake_fetch(CATALOG))
    with caplog.at_level("WARNING", logger="rsiagent.llm"):
        CLIENT._warn_if_model_cannot_read_images("plain/text-chat", None)
    assert not caplog.records


def test_the_guard_is_inert_on_openrouter(monkeypatch, caplog):
    """It must not touch the historical provider's behaviour at all."""
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.discover(fetch=fake_fetch(CATALOG))
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "openrouter")
    with caplog.at_level("WARNING", logger="rsiagent.llm"):
        CLIENT._warn_if_model_cannot_read_images("plain/text-chat", b"\x89PNG")
    assert not caplog.records


def test_cached_result_never_fetches():
    CAT.reset_catalog_cache()
    assert CAT.cached_result("chat") is None


def test_the_guard_is_also_inert_when_no_provider_is_selected(monkeypatch,
                                                              caplog):
    """The default provider is OpenRouter, so an unset value must stay silent."""
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    CAT.discover(fetch=fake_fetch(CATALOG))
    monkeypatch.delenv("RSIAGENT_LLM_PROVIDER", raising=False)
    with caplog.at_level("WARNING", logger="rsiagent.llm"):
        CLIENT._warn_if_model_cannot_read_images("plain/text-chat", b"\x89PNG")
    assert not caplog.records
