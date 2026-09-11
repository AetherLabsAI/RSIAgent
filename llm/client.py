"""Minimal OpenRouter client with robust JSON and durable multimodal history.

Forge remains a code-as-policy agent. Native Look attachments are transported as
exact image bytes and retained losslessly in the internal transcript so later turns
and recovered processes receive the same observations. The key comes from
``$OPENROUTER_API_KEY`` or a ``.env`` file.
"""
import base64
import hashlib
import json
import logging
import os
import threading
import time

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from config.runtime_paths import resolve_env_file


def _api_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY", "")
    if not k:
        src = resolve_env_file()
        try:
            with src.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("OPENROUTER_API_KEY="):
                        k = line.split("=", 1)[1].strip()
                        break
        except FileNotFoundError:
            pass
    return k


_client = None
# Phase1 branches and delegated eyes issue completions concurrently. The next
# pop must retrieve this execution thread's response, never another branch's.
_LAST_REASONING = threading.local()
_PROVIDER_COUNTS = {}              # model -> provider -> successful response count

# Internal transcript field for exact image bytes that a native-sighted agent has
# already received. The field is deliberately separate from ``content`` so existing
# text-history consumers and recovery validators keep their simple contract. ``chat``
# removes this private field and reconstructs an ordinary OpenAI multimodal message at
# the wire boundary. Transcripts therefore remain JSON-safe and lossless across turns
# and process recovery without asking the agent to summarize what it saw.
DURABLE_IMAGES_FIELD = "_forge_images"


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    # Look preparation normally emits PNG/JPEG. Preserve the old PNG fallback for
    # an uncommon provider-supported payload whose signature is not recognized.
    return "image/png"


def _image_list(image) -> list[bytes]:
    if image is None:
        return []
    values = image if isinstance(image, list) else [image]
    out = []
    for value in values:
        if isinstance(value, str):
            # A few transport tests historically use a short string as fake bytes.
            value = value.encode("utf-8")
        if isinstance(value, (bytearray, memoryview)):
            value = bytes(value)
        if not isinstance(value, bytes):
            raise TypeError("image attachments must be bytes or a list of bytes")
        out.append(value)
    return out


def durable_user_message(content: str, image=None) -> dict:
    """Build a JSON-safe transcript message for one successfully sent user turn.

    Images are stored in full, without resizing, truncation, OCR, or interpretation.
    A digest makes the archived bytes auditable; it does not select or rank evidence.
    """
    message = {"role": "user", "content": content}
    attachments = []
    for data in _image_list(image):
        attachments.append({
            "mime_type": _image_mime(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        })
    if attachments:
        message[DURABLE_IMAGES_FIELD] = attachments
    return message


def has_durable_images(message: dict) -> bool:
    return bool(isinstance(message, dict)
                and message.get(DURABLE_IMAGES_FIELD))


def durable_images(message: dict) -> list[dict]:
    """Return copied, schema-valid durable attachments from an internal message."""
    values = (message.get(DURABLE_IMAGES_FIELD, [])
              if isinstance(message, dict) else [])
    out = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        mime = item.get("mime_type")
        data = item.get("data")
        if isinstance(mime, str) and isinstance(data, str):
            copied = {"mime_type": mime, "data": data}
            if isinstance(item.get("sha256"), str):
                copied["sha256"] = item["sha256"]
            out.append(copied)
    return out


def with_durable_images(message: dict, attachments) -> dict:
    """Copy ``message`` and attach deduplicated archived observations."""
    result = dict(message)
    merged = []
    seen = set()
    for source in (durable_images(message), list(attachments or [])):
        for item in source:
            key = item.get("sha256") or (item.get("mime_type"), item.get("data"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    if merged:
        result[DURABLE_IMAGES_FIELD] = merged
    else:
        result.pop(DURABLE_IMAGES_FIELD, None)
    return result


def _wire_message(message: dict) -> dict:
    """Translate one internal history message into the provider wire schema."""
    result = dict(message)
    attachments = durable_images(result)
    result.pop(DURABLE_IMAGES_FIELD, None)
    if attachments:
        text = result.get("content", "")
        if not isinstance(text, str):
            raise TypeError("durable image history requires textual user content")
        parts = [{"type": "text", "text": text}]
        for item in attachments:
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": (f"data:{item['mime_type']};base64,"
                            f"{item['data']}")
                },
            })
        result["content"] = parts
    return result


def _request_messages(system: str, user: str, history=None, image=None) -> list:
    """Construct the provider request, rehydrating every durable observation."""
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(_wire_message(message) for message in history)
    messages.append(_wire_message(durable_user_message(user, image)))
    return messages


class LLMTransportError(RuntimeError):
    """A completion request failed before an agent reply existed.

    ``recoverable`` means an external-state change or provider recovery can make the
    exact same request valid later.  Agent runtimes pause on those failures without
    manufacturing an empty assistant message.  Non-recoverable request/configuration
    errors remain ordinary exceptions so a bad experiment cannot wait forever.
    """

    def __init__(self, model: str, cause: Exception, *, recoverable: bool,
                 status_code: int = None, detail: str = ""):
        self.model = model
        self.cause = cause
        self.recoverable = bool(recoverable)
        self.status_code = status_code
        self.detail = detail
        status = f" HTTP {status_code}" if status_code is not None else ""
        suffix = f"{status}: {detail}" if detail else status
        super().__init__(f"{model} transport failure{suffix}")


class _CompletionResponseError(ValueError):
    """A successful HTTP exchange contained an error or no completion.

    OpenRouter can send an error envelope after committing HTTP 200. Preserve
    that envelope and generation ID; its embedded code drives the same retry
    policy as ordinary HTTP errors. An unclassified empty response stays
    retryable, since it does not establish a malformed request.
    """

    def __init__(self, response):
        error = getattr(response, "error", None)
        self.body = {"id": getattr(response, "id", None),
                     "provider": getattr(response, "provider", None),
                     "error": error}
        value = error.get("code") if isinstance(error, dict) else None
        try:
            code = int(value)
        except (TypeError, ValueError):
            code = None
        self.status_code = code if code is not None and 400 <= code <= 599 else None
        super().__init__("response contained a provider error" if error
                         else "response had no choices")


def _status_code(exc) -> int:
    """Best-effort status extraction across OpenAI SDK exception versions."""
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _recoverable_transport(exc) -> bool:
    """Whether retrying the unchanged request later can reasonably succeed."""
    status = _status_code(exc)
    if status is None:
        # Do not turn a local programming/configuration error (for example a bad SDK
        # keyword raising TypeError) into an infinite infrastructure wait. The SDK's
        # connection/API family, ordinary socket failures, and our explicit malformed
        # response sentinel are the status-less conditions retrying can repair.
        return (isinstance(exc, (_CompletionResponseError,
                                 APIConnectionError, APITimeoutError, APIError,
                                 ConnectionError, TimeoutError))
                or (isinstance(exc, ValueError)
                    and str(exc) == "response had no choices"))
    if status in {401, 402, 403, 408, 409, 425, 429}:
        return True
    if 500 <= status <= 599:
        return True
    # 400/404/405/422 generally identify a bad model slug, unsupported parameter,
    # or malformed request. Pausing cannot repair the unchanged request.
    return False


def reset_provider_counts() -> None:
    """Reset per-process routing provenance before one benchmark task starts."""
    _PROVIDER_COUNTS.clear()


def provider_counts() -> dict:
    """Return a JSON-safe snapshot of providers observed in successful responses."""
    return {model: dict(counts) for model, counts in _PROVIDER_COUNTS.items()}


def pop_last_reasoning() -> str:
    """Return-and-clear the thinking channel of the last chat() call. The official
    K3 protocol: 'add the COMPLETE assistant message to the next request. Do not
    keep only content' — the loop re-attaches this to history when
    cfg.reasoning_in_history is set."""
    r = getattr(_LAST_REASONING, "value", "")
    _LAST_REASONING.value = ""
    return r


def _c() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=_api_key())
    return _client


def chat(model: str, system: str, user: str,
         max_tokens: int = 10000, temperature: float = 0.0,
         reasoning_effort: str = None, history: list = None,
         image=None, reasoning_max_tokens: int = 0, top_p: float = -1.0,
         provider_order=None, provider_allow_fallbacks: bool = True,
         provider_require_parameters: bool = False,
         json_object: bool = False) -> str:
    """One chat completion. ``history`` is the growing conversation (working memory —
    dropping it was anchor's biggest bug). ``image`` (png/jpg bytes, or a LIST of
    them — v13 auto-tiling attaches an overview plus native-resolution tiles) rides
    the current call. Callers record that exact attachment with
    :func:`durable_user_message`, allowing later calls and process recovery to see the
    same pixels without an agent-authored transcription. ``reasoning_effort`` is
    passed to the provider: "none" DISABLES reasoning where the model supports that;
    effort GRADATIONS (including model-specific ``max``) are honored by some models but
    IGNORED by MiniMax-M3 (verified 2026-07-06: low==high, M3 scales reasoning with
    problem difficulty, not the knob). GLM-5.3 does not support disabling thinking, so
    its truncation recovery uses ``low``. None omits the field. ``provider_order`` is
    an optional OpenRouter route preference; when present, it is never silently dropped
    after an API error because that would change the frozen experiment mid-run."""
    messages = _request_messages(system, user, history=history, image=image)
    kwargs = {}
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}
    if top_p is not None and top_p >= 0:
        kwargs["top_p"] = float(top_p)             # pinned explicitly (K3 report:
        #                                            "temperature = 1.0 and top-p = 1.0").
        #                                            -1 omits the field -> provider
        #                                            default, byte-identical to every
        #                                            run before 2026-08-09.
    extra_body = {}
    if reasoning_effort:
        extra_body["reasoning"] = ({"enabled": False} if reasoning_effort == "none"
                                   else {"effort": reasoning_effort})  # effort inert on M3
    elif reasoning_max_tokens:                     # v37 (wenyi: MAX reasoning, accuracy-only):
        extra_body["reasoning"] = {"max_tokens": int(reasoning_max_tokens)}
        # k3: reasoning is MANDATORY (enabled:false -> 400); depth IS tunable — this caps
        # thinking high instead of disabling. 0 = omit (GLM/M3 requests byte-identical).
    order = provider_order
    if isinstance(order, str):
        order = [part.strip() for part in order.split(",") if part.strip()]
    elif order:
        order = list(order)
    else:
        order = []
    if order:
        extra_body["provider"] = {
            "order": order,
            "allow_fallbacks": bool(provider_allow_fallbacks),
            "require_parameters": bool(provider_require_parameters),
        }
    if extra_body:
        kwargs["extra_body"] = extra_body
    log = logging.getLogger("forge.llm")
    resp = None
    for attempt in range(4):                       # robust to TRANSIENT API errors (non-JSON body,
        try:                                       # 5xx, rate-limit): a flaky response must not kill a run
            resp = _c().chat.completions.create(model=model, messages=messages,
                                                max_tokens=max_tokens, temperature=temperature, **kwargs)
            if (getattr(resp, "error", None)
                    or not getattr(resp, "choices", None)):
                raise _CompletionResponseError(resp)
            break
        except Exception as e:                     # noqa: BLE001
            name = type(e).__name__
            if isinstance(e, _CompletionResponseError):
                error = e.body.get("error")
                metadata = error.get("metadata") if isinstance(error, dict) else None
                log.warning("%s completion response failure: id=%s provider=%s "
                            "embedded_code=%s error_type=%s", model,
                            e.body["id"], e.body["provider"], e.status_code,
                            metadata.get("error_type") if isinstance(metadata, dict) else None)
            if kwargs and not order and not isinstance(e, _CompletionResponseError):
                # A returned error/empty completion is not evidence that optional
                # fields are unsupported. Preserve the exact request on its retry.
                log.warning("%s erred with optional request fields (%s); dropping them "
                            "and retrying", model, name)
                kwargs = {}
                continue
            body = getattr(e, "body", None)        # v35.1: surface the provider's actual
            detail = f" body={str(body)[:300]}" if body else ""  # complaint — a 400's
            if attempt == 3:                       # class name alone hides the cause
                status = _status_code(e)
                recoverable = _recoverable_transport(e)
                log.warning("%s failed after retries (%s)%s; raising %s transport "
                            "failure (no agent turn created)", model, name, detail,
                            "recoverable" if recoverable else "fatal")
                raise LLMTransportError(
                    model, e, recoverable=recoverable, status_code=status,
                    detail=str(body)[:300] if body else name) from e
            log.warning("%s API error (%s)%s; retry %d after backoff", model, name, detail,
                        attempt + 1)
            time.sleep(2 * (attempt + 1))
    if resp is None or not getattr(resp, "choices", None):
        # Defensive fallback: the retry loop above should already have raised.
        cause = ValueError("response had no choices after retries")
        raise LLMTransportError(model, cause, recoverable=True,
                                detail=str(cause)) from cause
    provider = _response_provider(resp)
    model_counts = _PROVIDER_COUNTS.setdefault(model, {})
    model_counts[provider] = model_counts.get(provider, 0) + 1
    log.info("%s provider: %s", model, provider)
    u = getattr(resp, "usage", None)               # v35.1 observability: per-call token
    if u is not None:                              # usage — cross-model context-compression
        det = getattr(u, "completion_tokens_details", None)   # parity is uninspectable
        rt = getattr(det, "reasoning_tokens", None) if det else None  # without it
        log.info("%s usage: in=%s out=%s%s", model,
                 getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None),
                 f" reasoning={rt}" if rt is not None else "")
    choice = resp.choices[0]
    truncated = getattr(choice, "finish_reason", None) == "length"
    txt = choice.message.content or ""
    native_calls = getattr(choice.message, "tool_calls", None) or []
    if native_calls:
        # Forge requests text JSON actions, not native provider tools. Surface this
        # protocol drift explicitly instead of misdiagnosing an empty content field
        # as an ordinary decoder dry turn. Never execute an unsolicited native call.
        names = []
        for call in native_calls:
            function = getattr(call, "function", None)
            names.append(str(getattr(function, "name", "unknown")))
        log.warning("%s returned %d unsolicited native tool call(s) (%s); "
                    "they were recorded as transport drift and not executed",
                    model, len(native_calls), ", ".join(names))
    # Keep the thinking channel, including empty-content turns, associated with
    # this caller until its own run loop appends the complete assistant message.
    _LAST_REASONING.value = getattr(choice.message, "reasoning", None) or ""
    if not txt.strip() and not truncated:          # v36.1: EMPTY-AT-STOP — the model ended
        rsn = getattr(choice.message, "reasoning", None) or ""   # its turn inside the hidden
        if rsn:                                    # channel. Log the tail for diagnosis
            log.warning("%s empty content at stop — reasoning tail: %r",   # (never execute
                        model, rsn[-300:])         # thinking; it holds rejected candidates)
    cut = txt.rfind("</think>")               # drop inlined thinking from thinking models
    if cut >= 0:
        txt = txt[cut + len("</think>"):]
    txt = txt.strip()
    mandatory_reasoning = _reasoning_cannot_disable(model)
    retry_ceiling = 131072 if mandatory_reasoning else 40000
    if truncated and max_tokens < retry_ceiling and (not txt or _looks_cut(txt)):
        # TOKEN-LIMIT TRUNCATION: the reply hit the length cap with either NO visible
        # text (hidden reasoning ate the budget) or a program CUT MID-STREAM (its JSON
        # action never closed). A same-budget retry truncates identically — retry once
        # with a larger budget and cheaper reasoning. GLM-5.3 and Kimi K3 cannot
        # disable thinking, so they retry at their supported ``low`` effort up to a
        # 131072-token recovery ceiling; reasoning-optional models preserve the
        # reasoning-off recovery. (v16 extends the
        # v8.5 empty-reply retry to non-empty cut-off programs: one run wasted ~50 turns resubmitting a
        # program severed at the same byte.)
        why = "empty reply" if not txt else "program cut mid-stream"
        retry_tokens = (min(max_tokens * 2, retry_ceiling)
                        if mandatory_reasoning else max_tokens * 2)
        retry_effort = "low" if mandatory_reasoning else "none"
        log.warning("%s truncated (%s at %d tokens) — retrying with %d + reasoning %s",
                    model, why, max_tokens, retry_tokens, retry_effort)
        return chat(model, system, user, max_tokens=retry_tokens,
                    temperature=temperature, reasoning_effort=retry_effort,
                    history=history, image=image,
                    reasoning_max_tokens=reasoning_max_tokens, top_p=top_p,
                    provider_order=order,
                    provider_allow_fallbacks=provider_allow_fallbacks,
                    provider_require_parameters=provider_require_parameters,
                    json_object=json_object)
    if truncated:
        log.warning("%s hit the token limit; output may be cut", model)
    return txt


def _is_glm53(model: str) -> bool:
    """True for the canonical GLM-5.3 slug and dated/variant descendants."""
    return model == "z-ai/glm-5.3" or model.startswith("z-ai/glm-5.3-")


def _reasoning_cannot_disable(model: str) -> bool:
    """Models in this harness whose APIs reject ``reasoning.enabled=false``."""
    return _is_glm53(model) or model == "moonshotai/kimi-k3" \
        or model.startswith("moonshotai/kimi-k3-")


def _response_provider(resp) -> str:
    """Extract OpenRouter's provider provenance without depending on SDK version."""
    value = getattr(resp, "provider", None)
    if not value:
        extra = getattr(resp, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get("provider") or extra.get("provider_name")
    if not value:
        try:
            dumped = resp.model_dump()
        except (AttributeError, TypeError):
            dumped = {}
        if isinstance(dumped, dict):
            value = dumped.get("provider") or dumped.get("provider_name")
    return str(value or "<unreported>")


def _looks_cut(txt: str) -> bool:
    """A truncated reply whose JSON action never closed — more open braces/brackets
    than close in the tail — is a program severed mid-stream, worth one bigger retry."""
    tail = txt[-6000:]
    return tail.count("{") + tail.count("[") > tail.count("}") + tail.count("]")


def json_values(text: str) -> list:
    """Every balanced top-level JSON value in ``text``, in order (tolerant of prose,
    markdown fences, and draft-then-correct replies)."""
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] in "[{":
            try:
                val, end = dec.raw_decode(text, i)
                out.append(val)
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    return out


def parse_object(text: str):
    """The LAST well-formed JSON object (the model's final answer), or None."""
    objs = [v for v in json_values(text) if isinstance(v, dict)]
    return objs[-1] if objs else None
