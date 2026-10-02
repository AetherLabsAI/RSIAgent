"""OrcaRouter model discovery and capability filtering.

The model list a user may pick from is not a hardcoded menu. The authority is
``GET {api_base}/models`` on the *configured* origin — for the public service,
``https://api.orcarouter.ai/v1/models`` — read with the user's own credential so
the catalog is the set of models their workspace can actually call.

Three rules govern everything here.

**Model IDs are opaque.** ``vendor/model`` namespaces are preserved byte-for-byte
and never rewritten, reordered, or normalised. Rewriting an ID produces a slug
the provider does not recognise, and the failure surfaces much later as a 404 on
a generation request.

**Capability is declared, never inferred.** A model is text-capable because it
advertises a text endpoint, and image-capable because it declares an ``image``
input modality. Nothing is admitted because its *name* suggests it. A model that
does not state a capability fails closed and is left out of that selector; that is
the only safe default when the alternative is sending a user's attachment to a
model that cannot read it.

The live catalog makes this distinction load-bearing rather than theoretical. In
one response, ``deepseek/deepseek-v4.1-flash`` declares
``architecture.input_modalities: ["text", "image"]``, ``deepseek/deepseek-v4-flash``
declares ``["text"]``, and ``orcarouter/auto`` omits ``architecture`` altogether.
Only the first may appear in an image selector. A name-based rule would have
admitted all three.

**Discovery improves an entry, it does not gate it.** A catalog outage must not
leave a fresh installation with no models at all, so a small, verified fallback
seed is kept — and is labelled as a fallback everywhere it surfaces. A successful
fetch is authoritative and *replaces* the seed; the two are never merged, because
a merged list would present unverified entries as if they came from the account.

RSIAgent's AI entry points and the capability each one needs:

- text chat / agent completion (Actor, Verifier, Curriculum, compaction) —
  :data:`CAPABILITY_CHAT`
- native Look image understanding (``core/eyes.py`` via ``chat(image=...)``) —
  :data:`CAPABILITY_MULTIMODAL`

There is no embedding, rerank, image-generation, or video-generation entry point
in this repository; those filters exist so the mapping is complete and so a future
entry point cannot silently inherit the wrong list.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice

from llm.provider import ORCAROUTER, acquire_credential, api_base, needs_reauth

log = logging.getLogger("rsiagent.catalog")

CAPABILITY_CHAT = "chat"
CAPABILITY_MULTIMODAL = "multimodal"
CAPABILITY_EMBEDDING = "embedding"
CAPABILITY_IMAGE = "image"
CAPABILITY_VIDEO = "video"
CAPABILITY_RERANK = "rerank"

CAPABILITIES = (CAPABILITY_CHAT, CAPABILITY_MULTIMODAL, CAPABILITY_EMBEDDING,
                CAPABILITY_IMAGE, CAPABILITY_VIDEO, CAPABILITY_RERANK)

# Wire routes this client can actually speak. A record advertising only routes
# outside this set is not selectable however well-known its name is.
TEXT_ENDPOINT_TYPES = ("openai", "anthropic", "gemini", "openai-response")
# Routes dedicated to a non-text workload. A record reachable *only* through
# these is excluded from the text selector.
NON_TEXT_ENDPOINT_TYPES = ("image-generation", "openai-video", "jina-rerank",
                           "embeddings")
EMBEDDING_ENDPOINT_TYPE = "embeddings"
IMAGE_ENDPOINT_TYPE = "image-generation"
VIDEO_ENDPOINT_TYPE = "openai-video"
RERANK_ENDPOINT_TYPE = "jina-rerank"

SOURCE_LIVE = "live"
SOURCE_SEED = "seed"
SOURCE_LAST_KNOWN_GOOD = "last_known_good"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_MODELS = 2000
CACHE_TTL_SECONDS = 300.0
_MAX_ID_LENGTH = 200
_MAX_ENDPOINT_TYPES = 16
_MAX_MODALITIES = 16


@dataclass(frozen=True)
class ModelRecord:
    """One model as the catalog describes it, plus where that description came from.

    ``endpoint_types`` and ``input_modalities`` are *declared* values. An empty
    ``input_modalities`` means the catalog did not state the modalities, which is
    emphatically not the same as "text only" — it means any modality-filtered
    selector must leave the record out.
    """

    id: str
    endpoint_types: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()
    context_length: int | None = None
    reasoning_efforts: tuple[str, ...] = ()
    source: str = SOURCE_LIVE

    @property
    def is_text_capable(self) -> bool:
        declared = set(self.endpoint_types)
        if not declared & set(TEXT_ENDPOINT_TYPES):
            return False
        # Reachable exclusively through a non-text workload route, so it is a
        # dedicated image/video/rerank/embedding model rather than a chat model
        # that happens to expose several routes.
        return bool(declared - set(NON_TEXT_ENDPOINT_TYPES))


@dataclass(frozen=True)
class CatalogResult:
    """A model list plus the provenance a UI must show alongside it."""

    models: tuple[ModelRecord, ...]
    source: str
    capability: str
    degraded: bool
    detail: str = ""

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(record.id for record in self.models)

    def label(self) -> str:
        if not self.degraded:
            return f"live catalog ({len(self.models)} models)"
        return (f"verified fallback catalog ({len(self.models)} models) — "
                f"{self.detail}")


# Verified fallback seed. Used ONLY when live discovery fails, and always
# reported as degraded. These five slugs are the set this integration verified
# against the public OrcaRouter catalog; `openai/gpt-5.5` carries the reasoning
# ladder that was observed for it.
#
# `input_modalities` is deliberately empty on every seed entry. These five are a
# cold-start list, not a description of the calling workspace's catalog, and an
# entry with no declared modality is excluded from any modality-filtered
# selector. That is the intent: an outage must not silently widen what the image
# selector offers. Context windows are omitted on the same principle — an
# invented number is worse than an absent one, and the live catalog supplies the
# real values.
#
# Note the asymmetry with the live path, which does publish this metadata: in the
# public catalog, entries such as `deepseek/deepseek-v4.1-flash` declare
# `architecture.input_modalities: ["text", "image"]`, while sibling entries
# declare only `["text"]` or omit `architecture` entirely. All three cases occur
# in the same response, which is precisely why the filter reads the field rather
# than the model name.
_VERIFIED_SEED = (
    ModelRecord("openai/gpt-5.5", TEXT_ENDPOINT_TYPES, (),
                reasoning_efforts=("low", "medium", "high", "xhigh"),
                source=SOURCE_SEED),
    ModelRecord("anthropic/claude-opus-4.8", TEXT_ENDPOINT_TYPES, (),
                source=SOURCE_SEED),
    ModelRecord("google/gemini-3.5-flash", TEXT_ENDPOINT_TYPES, (),
                source=SOURCE_SEED),
    ModelRecord("deepseek/deepseek-v4-pro", TEXT_ENDPOINT_TYPES, (),
                source=SOURCE_SEED),
    ModelRecord("orcarouter/auto", TEXT_ENDPOINT_TYPES, (),
                source=SOURCE_SEED),
)


def verified_seed() -> tuple[ModelRecord, ...]:
    """The cold-start catalog, for callers that need it without a network read."""
    return _VERIFIED_SEED


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _string_tuple(value, limit: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in islice(value, limit):
        text = _text(item, item_limit)
        if text and text not in out:
            out.append(text)
    return tuple(out)


def parse_model(entry, *, source: str = SOURCE_LIVE) -> ModelRecord | None:
    """One catalog entry, or ``None`` when it is not a shape we can route to.

    Bounded on every axis — id length, number of endpoint types and modalities,
    accepted scalar types — so a hostile or broken catalog response cannot make
    the harness allocate or advertise its way out of a fixed budget.
    """
    if not isinstance(entry, Mapping):
        return None
    model_id = _text(entry.get("id"), _MAX_ID_LENGTH)
    if not model_id:
        return None
    architecture = entry.get("architecture")
    modalities = (architecture.get("input_modalities")
                  if isinstance(architecture, Mapping) else None)
    reasoning = entry.get("reasoning")
    efforts = reasoning.get("efforts") if isinstance(reasoning, Mapping) else None
    context = entry.get("context_length")
    return ModelRecord(
        id=model_id,
        endpoint_types=_string_tuple(entry.get("supported_endpoint_types"),
                                     _MAX_ENDPOINT_TYPES, 64),
        input_modalities=_string_tuple(modalities, _MAX_MODALITIES, 32),
        context_length=(context if isinstance(context, int)
                        and not isinstance(context, bool) and context > 0
                        else None),
        reasoning_efforts=_string_tuple(efforts, 8, 32),
        source=source,
    )


def parse_catalog(payload, *, source: str = SOURCE_LIVE,
                  limit: int = DEFAULT_MAX_MODELS) -> list[ModelRecord]:
    """Parse a ``/v1/models`` body, keeping only well-formed, bounded records."""
    if isinstance(payload, Mapping):
        entries = payload.get("data")
    else:
        entries = payload
    if not isinstance(entries, (list, tuple)):
        return []
    out = []
    seen = set()
    for entry in islice(entries, limit):
        record = parse_model(entry, source=source)
        if record is None or record.id in seen:
            continue
        seen.add(record.id)
        out.append(record)
    return out


def _http_get_json(url: str, *, api_key: str, timeout: float,
                   max_bytes: int):
    """A bounded JSON GET. Returns ``(payload, error_message)``.

    Bounded in every direction: a wall-clock timeout, a hard cap on the number of
    bytes read, and a JSON parse that is allowed to fail. Errors are returned
    rather than raised so the caller can fall back to the seed with a specific
    reason to display.
    """
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None, f"catalog returned HTTP {response.status}"
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        # Never echo the body: it can quote the rejected credential.
        return None, f"catalog returned HTTP {exc.code}"
    except Exception as exc:                        # noqa: BLE001
        return None, f"catalog request failed: {type(exc).__name__}"
    if len(raw) > max_bytes:
        return None, "catalog response exceeded the size ceiling"
    try:
        return json.loads(raw.decode("utf-8")), ""
    except (UnicodeDecodeError, ValueError):
        return None, "catalog response was not valid JSON"


_cache: dict[str, tuple[float, tuple[ModelRecord, ...]]] = {}
_last_known_good: dict[str, tuple[ModelRecord, ...]] = {}


def reset_catalog_cache() -> None:
    """Drop cached catalogs. For tests and for an explicit user refresh."""
    _cache.clear()
    _last_known_good.clear()


def _cache_key(capability: str, base: str) -> str:
    # Keyed on the wire capability, so the chat authority list and the
    # multimodal selector share one cache entry and one fetch.
    return f"{base}\0{_wire_capability(capability)}"


def fetch_catalog(*, capability: str = CAPABILITY_CHAT,
                  environment: Mapping[str, str] | None = None,
                  timeout: float = DEFAULT_TIMEOUT_SECONDS,
                  max_bytes: int = DEFAULT_MAX_BYTES,
                  max_models: int = DEFAULT_MAX_MODELS,
                  acknowledge_reauth: bool = False) -> list[ModelRecord]:
    """Read ``/v1/models`` for one capability on the configured API origin.

    Raises on failure; :func:`discover` is the resilient entry point that turns a
    failure into the verified seed.
    """
    base = api_base(environment)
    capability = _check_capability(capability)
    url = f"{base}/models?capability={_wire_capability(capability)}"
    credential = acquire_credential(ORCAROUTER, environment=environment)
    if needs_reauth(credential) and not acknowledge_reauth:
        raise CatalogError(
            "the stored OrcaRouter credential was rejected; re-authorize with "
            "`python -m llm.connect login` before refreshing the catalog")
    payload, error = _http_get_json(url, api_key=credential.api_key,
                                    timeout=timeout, max_bytes=max_bytes)
    if error:
        raise CatalogError(error)
    records = parse_catalog(payload, source=SOURCE_LIVE, limit=max_models)
    if not records:
        raise CatalogError("catalog contained no usable model records")
    return records


class CatalogError(RuntimeError):
    """Live discovery failed. The message is safe to show a user."""


def _wire_capability(capability: str) -> str:
    """The capability to ask the catalog for, as opposed to the one we filter for.

    Multimodal understanding *is* chat with a non-text input, so its authority
    list is the chat list; the modality narrowing happens here, against declared
    ``architecture.input_modalities``. Requesting a ``multimodal`` capability
    from the server would ask for a filter the catalog does not define and
    return nothing.
    """
    return CAPABILITY_CHAT if capability == CAPABILITY_MULTIMODAL else capability


def _check_capability(capability: str) -> str:
    if capability not in CAPABILITIES:
        raise ValueError(
            f"unknown capability {capability!r}; expected one of "
            f"{', '.join(CAPABILITIES)}")
    return capability


def matches_capability(record: ModelRecord, capability: str, *,
                       modality: str | None = None) -> bool:
    """Whether a record may appear in one capability's selector.

    ``modality`` names the non-text input the caller actually uploads (``image``,
    ``audio``, ``video``). For :data:`CAPABILITY_MULTIMODAL` it is required, and
    a record is admitted only when it *declares* that modality. A record that
    declares no modalities at all is excluded rather than assumed to be text-only.
    """
    declared = set(record.endpoint_types)
    if capability == CAPABILITY_CHAT:
        return record.is_text_capable
    if capability == CAPABILITY_MULTIMODAL:
        if not modality:
            return False
        return (record.is_text_capable
                and modality.strip().lower() in
                {item.lower() for item in record.input_modalities})
    if capability == CAPABILITY_EMBEDDING:
        return EMBEDDING_ENDPOINT_TYPE in declared
    if capability == CAPABILITY_IMAGE:
        return IMAGE_ENDPOINT_TYPE in declared
    if capability == CAPABILITY_VIDEO:
        return VIDEO_ENDPOINT_TYPE in declared
    if capability == CAPABILITY_RERANK:
        return RERANK_ENDPOINT_TYPE in declared
    return False


def select_models(records, capability: str = CAPABILITY_CHAT, *,
                  modality: str | None = None) -> tuple[ModelRecord, ...]:
    """The options a model selector may offer for one entry point.

    This is the function a UI binds its dropdown to, so that the *options
    themselves* are the filtered set. Guarding a send after the fact is a second
    layer, not a substitute: an incompatible model must never be selectable.
    """
    capability = _check_capability(capability)
    return tuple(record for record in records
                 if matches_capability(record, capability, modality=modality))


def discover(capability: str = CAPABILITY_CHAT, *,
             modality: str | None = None,
             environment: Mapping[str, str] | None = None,
             timeout: float = DEFAULT_TIMEOUT_SECONDS,
             max_bytes: int = DEFAULT_MAX_BYTES,
             max_models: int = DEFAULT_MAX_MODELS,
             refresh: bool = False,
             cache_ttl: float = CACHE_TTL_SECONDS,
             fetch=None) -> CatalogResult:
    """The resilient entry point: live catalog when possible, verified seed when not.

    A successful fetch is authoritative and is cached. A failure falls back to the
    last known-good live catalog, then to the verified seed — never to free-text
    model entry and never to an unverified example list dressed up as a catalog.
    Every degraded result says so in :attr:`CatalogResult.detail`, so the caller
    can show the state rather than silently presenting stale options as live.
    """
    capability = _check_capability(capability)
    base = api_base(environment)
    key = _cache_key(capability, base)

    if not refresh:
        cached = _cache.get(key)
        if cached and (cached[0] - time.monotonic()) > 0:
            # The cache holds the raw authority list; the capability and
            # modality filters are applied on every read so a selector can never
            # be handed an unfiltered set.
            return CatalogResult(
                select_models(cached[1], capability, modality=modality),
                SOURCE_LIVE, capability, False)

    reader = fetch or (lambda: fetch_catalog(
        capability=capability, environment=environment, timeout=timeout,
        max_bytes=max_bytes, max_models=max_models))
    try:
        live = tuple(reader())
    except Exception as exc:                        # noqa: BLE001
        detail = str(exc) or type(exc).__name__
        fallback = _last_known_good.get(key)
        source = SOURCE_LAST_KNOWN_GOOD if fallback else SOURCE_SEED
        records = fallback if fallback else _VERIFIED_SEED
        log.warning("OrcaRouter catalog unavailable (%s); using %s",
                    detail, source)
        return CatalogResult(select_models(records, capability, modality=modality),
                             source, capability, True, detail)

    _cache[key] = (time.monotonic() + cache_ttl, live)
    _last_known_good[key] = live
    return CatalogResult(select_models(live, capability, modality=modality),
                         SOURCE_LIVE, capability, False)


def cached_result(capability: str = CAPABILITY_CHAT, *,
                  modality: str | None = None,
                  environment: Mapping[str, str] | None = None):
    """The last fetched catalog for this capability, or ``None``.

    A cache-only read with no network call, for callers on a hot path that must
    not pay for a fetch. Used by the pre-send capability check in
    :mod:`llm.client`, which is advisory and must never block a run.
    """
    base = api_base(environment)
    cached = _cache.get(_cache_key(capability, base))
    if not cached or (cached[0] - time.monotonic()) <= 0:
        return None
    return CatalogResult(select_models(cached[1], capability, modality=modality),
                         SOURCE_LIVE, capability, False)


def require_compatible(model_id: str, result: CatalogResult, *,
                       modality: str | None = None) -> str:
    """Validate a model ID against the current options, or explain the mismatch.

    Called before a stored model ID is restored from config or from persisted
    state: a value that is no longer compatible is cleared with a reason rather
    than silently kept and sent.
    """
    if not model_id:
        return ""
    if model_id in {record.id for record in result.models}:
        return model_id
    raise IncompatibleModel(
        f"{model_id!r} is not available for this entry point on the "
        f"{result.source} catalog"
        + (f" (requires declared {modality} input)" if modality else "")
        + f". Available: {', '.join(result.ids) or 'none'}.")


class IncompatibleModel(ValueError):
    """A previously selected model is no longer offered for this entry point."""


def resolve_model(capability: str = CAPABILITY_CHAT, *, preferred: str = "",
                  modality: str | None = None,
                  environment: Mapping[str, str] | None = None,
                  **kwargs) -> tuple[str, CatalogResult]:
    """Pick a model for one entry point, revalidating any stored preference.

    Returns ``(model_id, catalog)`` with ``model_id`` empty when nothing is
    compatible — the caller must then show the empty state rather than send.
    """
    result = discover(capability, modality=modality, environment=environment,
                      **kwargs)
    if preferred:
        try:
            return require_compatible(preferred, result, modality=modality), result
        except IncompatibleModel as exc:
            log.warning("clearing stale model selection: %s", exc)
    return (result.ids[0] if result.ids else ""), result
