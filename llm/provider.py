"""LLM provider selection, endpoint origins, and the credential seam.

RSIAgent historically spoke to exactly one endpoint: OpenRouter at
``https://openrouter.ai/api/v1`` with the key in ``$OPENROUTER_API_KEY``. Adding
OrcaRouter must not change a byte of an OpenRouter run, and it must not scatter a
second credential path through the transport, the catalog reader, the CLI and the
benchmark runners.

Two facts shape this module.

**A provider is a pair of origins, not one hostname.** OrcaRouter authenticates
on ``www.orcarouter.ai`` and serves inference on ``api.orcarouter.ai/v1``.
Deriving either from the other by swapping a hostname or appending ``/v1``
produces ``https://api.orcarouter.ai/v1/auth/keys``, which is a 404 — the most
common integration mistake, and one that reads like a routing fault on the
provider's side. :func:`auth_base` and :func:`api_base` therefore resolve
independently and only meet through the documented shared fallback.

**Both entry points end in the same durable key.** A pasted ``sk-orca-…`` key
and an OAuth 2.0 + PKCE authorization both produce an ordinary OrcaRouter API key
belonging to the user. Nothing downstream — transport, model catalog, or any AI
entry point — may care which one produced it. :class:`ApiKeyCredential` and
:class:`PkceCredential` are two adapters on one small seam,
:func:`acquire_credential`, and everything else reads credentials only through
that seam.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from config.runtime_paths import resolve_env_file, resolve_root

OPENROUTER = "openrouter"
ORCAROUTER = "orcarouter"
DEFAULT_PROVIDER = OPENROUTER
PROVIDERS = (OPENROUTER, ORCAROUTER)

PROVIDER_ENV = "RSIAGENT_LLM_PROVIDER"

# The public origins. Authentication and inference are deliberately different
# hosts; see the module docstring.
PUBLIC_AUTH_BASE = "https://www.orcarouter.ai"
PUBLIC_API_BASE = "https://api.orcarouter.ai/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

# The consent screen, and the console page where a user revokes every key a
# client was issued. Both are documentation surfaces the CLI prints verbatim.
AUTHORIZE_PATH = "/auth"
EXCHANGE_PATH = "/api/v1/auth/keys"
AUTHORIZED_APPS_URL = "https://www.orcarouter.ai/console/authorized-apps"

# A key the user pasted, and the alias the deployment harness exports. The
# canonical name is ORCA_API_KEY; ORCAROUTER_API_KEY is accepted because a
# self-hosted or CI environment may already provide the long spelling, and
# refusing to read it would look like a credential bug to the operator.
ORCA_KEY_ENV = "ORCA_API_KEY"
ORCA_KEY_ENV_ALIASES = ("ORCA_API_KEY", "ORCAROUTER_API_KEY")
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

SHARED_BASE_ENV = "ORCA_BASE_URL"
AUTH_BASE_ENV = "ORCA_AUTH_BASE_URL"
API_BASE_ENV = "ORCA_API_BASE_URL"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ProviderConfigError(RuntimeError):
    """A configured origin is unusable — never a silent fallback to a default."""


class CredentialUnavailable(RuntimeError):
    """No credential exists for the provider. Carries an actionable next step."""

    def __init__(self, message: str, *, provider_id: str, remedy: str = ""):
        self.provider_id = provider_id
        self.remedy = remedy
        super().__init__(f"{message}{' ' + remedy if remedy else ''}")


def _environment(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _origin(raw: str, *, name: str) -> str:
    """Validate a configured origin, requiring HTTPS off loopback.

    Credentials and prompts ride these origins. Pinning the scheme here means a
    plaintext remote base cannot be introduced by a typo in one env var, while
    a local reverse proxy on ``127.0.0.1`` still works for development.
    """
    text = (raw or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderConfigError(
            f"{name} must be an absolute http(s) origin, got {text!r}")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ProviderConfigError(
            f"{name} must use https for a non-loopback origin, got {text!r}")
    return text.rstrip("/")


def _shared_base(environment: Mapping[str, str] | None) -> str:
    raw = _environment(environment).get(SHARED_BASE_ENV, "")
    return _origin(raw, name=SHARED_BASE_ENV) if raw.strip() else ""


def auth_base(environment: Mapping[str, str] | None = None) -> str:
    """The OrcaRouter origin that serves consent and code exchange.

    Precedence: explicit ``ORCA_AUTH_BASE_URL``, then the shared self-hosted
    ``ORCA_BASE_URL``, then the public default. A shared base names one host, so
    the ``/v1`` API suffix some deployments include is stripped here.
    """
    values = _environment(environment)
    explicit = values.get(AUTH_BASE_ENV, "").strip()
    if explicit:
        return _origin(explicit, name=AUTH_BASE_ENV)
    shared = _shared_base(values)
    if shared:
        return shared[:-3].rstrip("/") if shared.endswith("/v1") else shared
    return PUBLIC_AUTH_BASE


def api_base(environment: Mapping[str, str] | None = None) -> str:
    """The OrcaRouter origin that serves inference and the model catalog.

    Precedence: explicit ``ORCA_API_BASE_URL``, then the shared self-hosted
    ``ORCA_BASE_URL`` (which is a host base, so ``/v1`` is appended unless the
    deployment already spelled it), then the public default.
    """
    values = _environment(environment)
    explicit = values.get(API_BASE_ENV, "").strip()
    if explicit:
        return _origin(explicit, name=API_BASE_ENV)
    shared = _shared_base(values)
    if shared:
        return shared + "/v1" if not shared.endswith("/v1") else shared
    return PUBLIC_API_BASE


def auth_url(path: str, environment: Mapping[str, str] | None = None) -> str:
    """An absolute URL on the auth origin. ``path`` is always supplied by us."""
    return auth_base(environment) + path


@dataclass(frozen=True)
class ProviderProfile:
    """Everything the transport needs to reach one provider."""

    provider_id: str
    label: str
    api_base: str
    key_env: str


def active_provider(environment: Mapping[str, str] | None = None) -> str:
    """The provider this process routes through.

    Absent or unrecognised values keep the historical OpenRouter behaviour
    byte-for-byte: an experiment that never sets the variable cannot be moved
    onto a different endpoint by this change.
    """
    value = _environment(environment).get(PROVIDER_ENV, "").strip().lower()
    return value if value in PROVIDERS else DEFAULT_PROVIDER


def provider_profile(provider_id: str | None = None,
                     environment: Mapping[str, str] | None = None) -> ProviderProfile:
    values = _environment(environment)
    provider = (provider_id or active_provider(values)).strip().lower()
    if provider == ORCAROUTER:
        return ProviderProfile(ORCAROUTER, "OrcaRouter", api_base(values),
                               ORCA_KEY_ENV)
    if provider == OPENROUTER:
        return ProviderProfile(OPENROUTER, "OpenRouter", OPENROUTER_API_BASE,
                               OPENROUTER_KEY_ENV)
    raise ProviderConfigError(
        f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")


def credential_generation(provider_id: str, source: str, api_key: str) -> str:
    """An opaque, stable identity for one stored credential.

    Two calls with the same provider, adapter and key yield the same generation;
    a re-login mints a new key and therefore a new generation. The value is a
    digest precisely so it can be logged and compared without ever exposing the
    key, and it is what the ``401`` reauthentication transition is keyed on —
    that is what stops a late failure from an old request from marking a freshly
    reauthorized credential as broken.
    """
    material = f"{provider_id}\0{source}\0{api_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


@dataclass(frozen=True)
class Credential:
    """One usable API key plus the provenance a user needs to reason about it."""

    provider_id: str
    api_key: str
    source: str
    generation: str

    def __repr__(self) -> str:
        # The key must never reach a log line, a traceback, a crash report or a
        # snapshot. Redacting in __repr__ covers every incidental format path.
        return (f"Credential(provider_id={self.provider_id!r}, "
                f"source={self.source!r}, generation={self.generation!r}, "
                f"api_key=<redacted>)")

    __str__ = __repr__


def _new_credential(provider_id: str, api_key: str, source: str) -> Credential:
    return Credential(provider_id, api_key, source,
                      credential_generation(provider_id, source, api_key))


def _read_credential_file(environment: Mapping[str, str] | None,
                          repo_root: Path | None) -> dict:
    """Read the project's own secret file into a ``NAME -> value`` mapping.

    ``.env`` is the mechanism RSIAgent already trusts for provider secrets
    (``RSIAGENT_ENV_FILE`` relocates it for secret mounts). OrcaRouter reuses it
    rather than introducing a second store.
    """
    values = _environment(environment)
    path = resolve_env_file(values, repo_root or resolve_root(values))
    stored: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return stored
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        stored[name.strip()] = value.strip()
    return stored


def _key_from_environment(provider_id: str,
                          environment: Mapping[str, str] | None = None,
                          repo_root: Path | None = None) -> tuple[str, str]:
    """Resolve ``(key, reference)`` for a provider from env, then the secret file."""
    values = _environment(environment)
    names = (ORCA_KEY_ENV_ALIASES if provider_id == ORCAROUTER
             else (OPENROUTER_KEY_ENV,))
    for name in names:
        if values.get(name, "").strip():
            return values[name].strip(), f"${name}"
    stored = _read_credential_file(values, repo_root)
    for name in names:
        if stored.get(name, "").strip():
            return stored[name].strip(), name
    return "", ""


def _store_credential_value(name: str, value: str, *,
                            environment: Mapping[str, str] | None = None,
                            repo_root: Path | None = None) -> Path:
    """Write one ``NAME=value`` line into the project's secret file.

    Rewrites the file atomically and preserves every unrelated line, because the
    same file carries the operator's other credentials. A blank ``value``
    removes the line. The file is created with owner-only permissions: it holds
    a live credential.
    """
    values = _environment(environment)
    path = resolve_env_file(values, repo_root or resolve_root(values))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise CredentialUnavailable(
            f"cannot read the credential file: {path}",
            provider_id=ORCAROUTER) from exc

    prefix = f"{name}="
    kept = [line for line in lines if not line.strip().startswith(prefix)]
    if value:
        kept.append(f"{name}={value}")
    rendered = "\n".join(kept) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CredentialUnavailable(
            f"cannot write the credential file: {path}",
            provider_id=ORCAROUTER) from exc
    return path


class ApiKeyCredential:
    """Adapter 1 — a key the user pasted into the project's own secret file.

    The user may already hold an ``sk-orca-…`` key, may be running somewhere a
    browser cannot reach, or may simply prefer an explicit credential. This path
    never opens a browser and never writes anything: it reads what the operator
    put there, through the project's existing mechanism.
    """

    provider_id = ORCAROUTER
    source_id = "api_key"

    def acquire(self, *, environment: Mapping[str, str] | None = None,
                repo_root: Path | None = None) -> Credential:
        key, _ = _key_from_environment(self.provider_id, environment,
                                       repo_root)
        if not key:
            raise CredentialUnavailable(
                "no OrcaRouter API key is configured",
                provider_id=self.provider_id,
                remedy=(f"set {ORCA_KEY_ENV} in the environment or in the "
                        f"RSIAGENT_ENV_FILE credential file, or run "
                        f"`python -m llm.connect login` to authorize with an "
                        f"OrcaRouter account."))
        return _new_credential(self.provider_id, key, self.source_id)


class PkceCredential:
    """Adapter 2 — a key minted by an OAuth 2.0 + PKCE authorization.

    Once the exchange returns, the result is an ordinary OrcaRouter API key, so
    it is persisted through exactly the same mechanism as a pasted one and read
    back the same way. What differs is only the provenance: this adapter also
    owns writing the credential and clearing it, and it is the only writer that
    mints a new generation.
    """

    provider_id = ORCAROUTER
    source_id = "pkce"

    def acquire(self, *, environment: Mapping[str, str] | None = None,
                repo_root: Path | None = None) -> Credential:
        key, _ = _key_from_environment(self.provider_id, environment,
                                       repo_root)
        if not key:
            raise CredentialUnavailable(
                "no OrcaRouter credential is stored for this account",
                provider_id=self.provider_id,
                remedy=("run `python -m llm.connect login` to authorize in a "
                        "browser, or set ORCA_API_KEY / OPENROUTER-style "
                        "ORCAROUTER_API_KEY to paste an existing key."))
        source = (self.source_id
                  if _read_credential_file(environment, repo_root).get(
                      CREDENTIAL_SOURCE_MARKER) == self.source_id
                  else "api_key")
        return _new_credential(self.provider_id, key, source)

    def store(self, api_key: str, *, scope: str = "api", user_id: str = "",
              environment: Mapping[str, str] | None = None,
              repo_root: Path | None = None) -> Credential:
        """Persist a freshly exchanged key and return its credential.

        A blank key is refused here rather than written, so a malformed exchange
        response cannot blank out a working credential and leave the operator
        with nothing. The caller replaces the old value only after this returns.
        """
        if not api_key.strip():
            raise CredentialUnavailable(
                "refusing to store an empty OrcaRouter key",
                provider_id=self.provider_id)
        _store_credential_value(ORCA_KEY_ENV, api_key.strip(),
                                environment=environment, repo_root=repo_root)
        _store_credential_value(CREDENTIAL_SOURCE_MARKER, self.source_id,
                                environment=environment, repo_root=repo_root)
        clear_needs_reauth(self.provider_id)
        credential = _new_credential(self.provider_id, api_key.strip(),
                                     self.source_id)
        record_authorization(credential, scope=scope, user_id=user_id)
        return credential

    def clear(self, *, environment: Mapping[str, str] | None = None,
              repo_root: Path | None = None) -> str:
        """Remove the stored key. Returns the removed key's generation, or ``''``."""
        key, _ = _key_from_environment(self.provider_id, environment, repo_root)
        removed = _new_credential(self.provider_id, key, self.source_id) if key else None
        _store_credential_value(ORCA_KEY_ENV, "", environment=environment,
                                repo_root=repo_root)
        _store_credential_value(CREDENTIAL_SOURCE_MARKER, "",
                                environment=environment, repo_root=repo_root)
        if removed is not None:
            mark_needs_reauth(removed, "credential removed by the operator")
        return removed.generation if removed else ""


# A non-secret marker recording which entry point produced the stored key. It
# exists so `status` and `logout` can explain what the user is holding, and so a
# test can prove both adapters converge on one credential. It is never used to
# choose an endpoint.
CREDENTIAL_SOURCE_MARKER = "ORCA_CREDENTIAL_SOURCE"


def credential_source_class(provider_id: str):
    """The adapter for one entry point — the seam's two implementations."""
    if provider_id == ORCAROUTER:
        return PkceCredential
    raise ProviderConfigError(
        f"provider {provider_id!r} has no OrcaRouter credential adapter")


def credential_adapters() -> dict:
    """Both OrcaRouter entry points, keyed by the ID each one reports."""
    return {adapter.source_id: adapter
            for adapter in (ApiKeyCredential(), PkceCredential())}


def acquire_credential(provider_id: str | None = None, *,
                       environment: Mapping[str, str] | None = None,
                       repo_root: Path | None = None) -> Credential:
    """The single seam: resolve the credential any consumer should use.

    Both adapters read the same stored value, so a caller cannot tell — and must
    not care — which entry point created it. Only the reported ``source``
    differs, and that is for user-facing status, not for routing.
    """
    values = _environment(environment)
    provider = (provider_id or active_provider(values)).strip().lower()
    if provider == ORCAROUTER:
        # The PKCE adapter is the reader of record: it reports the true
        # provenance of the stored key whichever entry point wrote it.
        return PkceCredential().acquire(environment=values, repo_root=repo_root)
    key, reference = _key_from_environment(OPENROUTER, values, repo_root)
    if not key:
        raise CredentialUnavailable(
            "no OpenRouter API key is configured",
            provider_id=OPENROUTER,
            remedy=f"set {OPENROUTER_KEY_ENV} in the environment or in the "
                   f"RSIAGENT_ENV_FILE credential file.")
    return _new_credential(OPENROUTER, key, "api_key")


# --- terminal reauthentication -------------------------------------------------
#
# A PKCE-issued key is durable but it is *not* a refresh token: OrcaRouter has no
# refresh grant, and re-authorizing on every launch would exhaust the 10-keys-
# per-user-per-24-hours cap. The correct reaction to an authentication 401 is
# therefore to stop and ask the user to sign in again — never to mint a silent
# replacement, and never to retry the dead credential in a loop.
#
# State is keyed on (provider, generation) and held in this process. Because a
# successful re-login produces a different key and therefore a different
# generation, a late 401 from a request issued before the re-login lands in the
# old bucket and cannot mark the new credential as broken.

_REAUTH: dict[tuple[str, str], str] = {}
_AUTHORIZATIONS: dict[tuple[str, str], dict] = {}


def mark_needs_reauth(credential: Credential, reason: str = "") -> None:
    _REAUTH[(credential.provider_id, credential.generation)] = reason or "denied"


def needs_reauth(credential: Credential) -> bool:
    return (credential.provider_id, credential.generation) in _REAUTH


def reauth_reason(credential: Credential) -> str:
    return _REAUTH.get((credential.provider_id, credential.generation), "")


def clear_needs_reauth(provider_id: str) -> None:
    """Drop every reauth mark for a provider. Called only after a new login."""
    for key in [k for k in _REAUTH if k[0] == provider_id]:
        del _REAUTH[key]


def record_authorization(credential: Credential, *, scope: str,
                         user_id: str = "") -> None:
    """Remember what the exchange actually granted, for `status` to report.

    ``scope`` is what the server granted, not what was requested; a client that
    asked for ``connector`` and was granted ``api`` must say so rather than
    assume it holds the wider grant.
    """
    _AUTHORIZATIONS[(credential.provider_id, credential.generation)] = {
        "scope": scope, "user_id": user_id}


def authorization(credential: Credential) -> dict:
    return dict(_AUTHORIZATIONS.get(
        (credential.provider_id, credential.generation), {}))


def reset_credential_state() -> None:
    """Clear in-process credential bookkeeping. For tests and for a new run."""
    _REAUTH.clear()
    _AUTHORIZATIONS.clear()
