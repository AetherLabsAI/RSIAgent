"""OrcaRouter provider registration, credential seam, and terminal reauth.

The properties under test are the ones that decide whether a credential can leak
or a dead key can be retried forever:

- both entry points are registered and independently usable;
- both converge on one credential, and nothing downstream can tell them apart;
- the auth origin and the inference origin are never derived from each other;
- a rejected key marks exactly the generation that made the request;
- no key or verifier value reaches a log line, an error, or a repr.
"""
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm import catalog as CAT
from llm import client as C
from llm import provider as P


FAKE_KEY = "sk-orca-test-0000000000000000000000000000000000000000"
OTHER_KEY = "sk-orca-test-1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    # The campaign harness exports ORCAROUTER_API_KEY (the alias) for the live
    # check. An ambient credential must never leak into a hermetic unit test, so
    # every test starts from "nothing is configured" and opts in explicitly.
    for name in ("ORCA_API_KEY", "ORCAROUTER_API_KEY", "OPENROUTER_API_KEY",
                 "RSIAGENT_LLM_PROVIDER", "ORCA_BASE_URL",
                 "ORCA_AUTH_BASE_URL", "ORCA_API_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    P.reset_credential_state()
    CAT.reset_catalog_cache()
    C._client = None
    C._built_client = None
    C._client_base = None
    C._client_generation = None
    C._CURRENT_CREDENTIAL = None
    yield
    P.reset_credential_state()
    CAT.reset_catalog_cache()
    C._client = None
    C._built_client = None
    C._client_base = None
    C._client_generation = None
    C._CURRENT_CREDENTIAL = None


def env(**values):
    """A hermetic environment. No proxy, no ambient credential, no real home."""
    base = {"RSIAGENT_ENV_FILE": "/nonexistent/rsiagent-credentials"}
    base.update({k: v for k, v in values.items() if v is not None})
    return base


# --- provider registration -----------------------------------------------------

def test_orcarouter_is_a_first_class_named_provider():
    profile = P.provider_profile(P.ORCAROUTER, env())
    assert profile.provider_id == "orcarouter"
    assert profile.label == "OrcaRouter"
    assert profile.api_base == "https://api.orcarouter.ai/v1"
    assert profile.key_env == "ORCA_API_KEY"
    assert P.ORCAROUTER in P.PROVIDERS


def test_openrouter_remains_the_default_and_is_unchanged():
    assert P.active_provider(env()) == P.OPENROUTER
    # An unset or unrecognised value must not silently move the endpoint.
    assert P.active_provider(env(RSIAGENT_LLM_PROVIDER="")) == P.OPENROUTER
    assert P.active_provider(env(RSIAGENT_LLM_PROVIDER="nope")) == P.OPENROUTER
    assert (P.provider_profile(P.OPENROUTER, env()).api_base
            == "https://openrouter.ai/api/v1")


def test_selecting_orcarouter_routes_the_transport_to_its_own_origin(monkeypatch):
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.delenv("ORCA_API_BASE_URL", raising=False)
    assert C._client_base_url() == "https://api.orcarouter.ai/v1"


# --- origins: two hosts, never derived from one another -------------------------

def test_auth_and_inference_default_to_different_origins():
    assert P.auth_base(env()) == "https://www.orcarouter.ai"
    assert P.api_base(env()) == "https://api.orcarouter.ai/v1"
    # The classic mistake: the exchange path must never be reachable on the
    # inference origin.
    from llm import connect as CON
    assert not CON.exchange_url().startswith(P.api_base(env()))


def test_exchange_path_is_the_auth_origin_api_prefix():
    from llm import connect as CON
    assert CON.exchange_url() == "https://www.orcarouter.ai/api/v1/auth/keys"
    # Not https://api.orcarouter.ai/v1/auth/keys, which is a 404.
    assert "api.orcarouter.ai" not in CON.exchange_url()


def test_shared_self_hosted_base_fills_both_origins():
    values = env(ORCA_BASE_URL="https://gateway.internal.example")
    assert P.auth_base(values) == "https://gateway.internal.example"
    assert P.api_base(values) == "https://gateway.internal.example/v1"


def test_explicit_overrides_take_precedence_over_the_shared_base():
    values = env(ORCA_BASE_URL="https://shared.example",
                 ORCA_AUTH_BASE_URL="https://login.example",
                 ORCA_API_BASE_URL="https://infer.example/v1")
    assert P.auth_base(values) == "https://login.example"
    assert P.api_base(values) == "https://infer.example/v1"


def test_http_is_refused_off_loopback_and_permitted_on_it():
    with pytest.raises(P.ProviderConfigError):
        P.api_base(env(ORCA_API_BASE_URL="http://api.orcarouter.ai/v1"))
    assert P.api_base(env(ORCA_API_BASE_URL="http://127.0.0.1:8080/v1")) \
        == "http://127.0.0.1:8080/v1"
    assert P.auth_base(env(ORCA_AUTH_BASE_URL="http://localhost:3000")) \
        == "http://localhost:3000"


# --- the two adapters ----------------------------------------------------------

def test_both_entry_points_are_registered():
    adapters = P.credential_adapters()
    assert set(adapters) == {"api_key", "pkce"}


def test_api_key_adapter_reads_the_projects_own_secret_file(tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"OPENROUTER_API_KEY=other\nORCA_API_KEY={FAKE_KEY}\n")
    credential = P.ApiKeyCredential().acquire(
        environment=env(RSIAGENT_ENV_FILE=str(secret)))
    assert credential.api_key == FAKE_KEY
    assert credential.source == "api_key"


def test_api_key_adapter_accepts_the_exported_alias():
    credential = P.ApiKeyCredential().acquire(
        environment=env(ORCAROUTER_API_KEY=FAKE_KEY))
    assert credential.api_key == FAKE_KEY


def test_api_key_adapter_reports_an_actionable_remedy_when_absent():
    with pytest.raises(P.CredentialUnavailable) as raised:
        P.ApiKeyCredential().acquire(environment=env())
    assert "ORCA_API_KEY" in str(raised.value)
    assert "llm.connect login" in str(raised.value)


def test_both_adapters_yield_the_same_credential_result(tmp_path):
    """The seam's whole point: provenance differs, the credential does not."""
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    environment = env(RSIAGENT_ENV_FILE=str(secret))

    pasted = P.ApiKeyCredential().acquire(environment=environment)
    from_login = P.PkceCredential().acquire(environment=environment)
    through_seam = P.acquire_credential(P.ORCAROUTER, environment=environment)

    assert pasted.api_key == from_login.api_key == through_seam.api_key == FAKE_KEY
    assert pasted.generation == from_login.generation == through_seam.generation
    # Only the reported provenance differs, and that is for status, not routing.
    assert pasted.source == "api_key"
    assert through_seam.source in {"api_key", "pkce"}


def test_pkce_adapter_reports_its_own_provenance_after_login(tmp_path):
    secret = tmp_path / "rsi.env"
    environment = env(RSIAGENT_ENV_FILE=str(secret))
    stored = P.PkceCredential().store(
        FAKE_KEY, scope="api", user_id="12345", environment=environment)
    assert stored.source == "pkce"
    read_back = P.PkceCredential().acquire(environment=environment)
    assert read_back.source == "pkce"
    assert read_back.generation == stored.generation
    # The grant that was actually returned is recorded, not the one requested.
    assert P.authorization(stored)["scope"] == "api"


class _RecordingClient:
    """Stand-in for the SDK client, so the test never opens a socket."""

    def __init__(self, base_url="", api_key=""):
        self.base_url = base_url
        self.api_key = api_key


def test_storing_a_second_key_moves_the_client_off_the_old_one(tmp_path,
                                                              monkeypatch):
    """A re-login must actually take effect, not leave a cached dead client."""
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))

    monkeypatch.setattr(C, "OpenAI", _RecordingClient)
    first = P.acquire_credential(P.ORCAROUTER)
    C._CURRENT_CREDENTIAL = first
    built = C._c()
    assert C._client_generation == first.generation

    # Same credential -> the client is reused rather than rebuilt per call.
    assert C._c() is built

    second = P.PkceCredential().store(OTHER_KEY, scope="api")
    C._CURRENT_CREDENTIAL = second
    assert C._c() is not built
    assert C._client_generation == second.generation
    assert C._client_generation != first.generation


def test_store_refuses_an_empty_key_so_a_bad_response_cannot_erase_one(tmp_path):
    secret = tmp_path / "rsi.env"
    environment = env(RSIAGENT_ENV_FILE=str(secret))
    P.PkceCredential().store(FAKE_KEY, environment=environment)
    with pytest.raises(P.CredentialUnavailable):
        P.PkceCredential().store("", environment=environment)
    # The working credential survives the rejected write.
    assert P.PkceCredential().acquire(environment=environment).api_key == FAKE_KEY


def test_logout_removes_only_orcarouter_lines_and_reports_the_generation(tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text("OPENROUTER_API_KEY=keep-me\n")
    environment = env(RSIAGENT_ENV_FILE=str(secret))
    stored = P.PkceCredential().store(FAKE_KEY, environment=environment)

    removed = P.PkceCredential().clear(environment=environment)
    assert removed == stored.generation == P.credential_generation(
        P.ORCAROUTER, "pkce", FAKE_KEY)
    remaining = secret.read_text()
    assert "OPENROUTER_API_KEY=keep-me" in remaining
    assert FAKE_KEY not in remaining
    with pytest.raises(P.CredentialUnavailable):
        P.PkceCredential().acquire(environment=environment)


def test_credential_file_is_written_owner_only(tmp_path):
    secret = tmp_path / "nested" / "rsi.env"
    P.PkceCredential().store(FAKE_KEY, environment=env(
        RSIAGENT_ENV_FILE=str(secret)))
    assert secret.stat().st_mode & 0o777 == 0o600


# --- secret hygiene ------------------------------------------------------------

def test_credential_repr_and_str_never_expose_the_key():
    credential = P.Credential("orcarouter", FAKE_KEY, "api_key",
                              P.credential_generation("orcarouter", "api_key",
                                                      FAKE_KEY))
    for rendered in (repr(credential), str(credential), f"{credential}"):
        assert FAKE_KEY not in rendered
        assert "redacted" in rendered
    # The generation is a digest: comparable and loggable, not the key.
    assert FAKE_KEY not in credential.generation


def test_no_key_or_verifier_value_reaches_the_logs(tmp_path, caplog):
    from llm import connect as CON
    secret = tmp_path / "rsi.env"
    environment = env(RSIAGENT_ENV_FILE=str(secret))
    verifier, challenge = CON.pkce_pair()
    with caplog.at_level(logging.DEBUG):
        P.PkceCredential().store(FAKE_KEY, environment=environment)
        P.acquire_credential(P.ORCAROUTER, environment=environment)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_KEY not in logged
    assert verifier not in logged
    assert challenge not in logged


# --- terminal reauthentication -------------------------------------------------

class _StatusError(Exception):
    def __init__(self, status_code, body="denied"):
        super().__init__(body)
        self.status_code = status_code
        self.body = body


def _install_failing_client(monkeypatch, status):
    calls = {"n": 0}

    def create(**_kwargs):
        calls["n"] += 1
        raise _StatusError(status)

    monkeypatch.setattr(C, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)
    return calls


def test_revoked_orcarouter_key_requires_reauth_without_fake_refresh(
        monkeypatch, tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    calls = _install_failing_client(monkeypatch, 401)

    with pytest.raises(C.ReauthRequired) as raised:
        C.chat("model", "system", "user")

    assert raised.value.recoverable is False
    assert raised.value.status_code == 401
    # One attempt: the dead key is not retried.
    assert calls["n"] == 1
    # Exactly the rejected generation is marked, and no refresh was attempted.
    credential = P.acquire_credential(P.ORCAROUTER)
    assert P.needs_reauth(credential) is True
    assert not hasattr(C, "_refresh"), "there is no refresh grant to call"


def test_flagged_credential_is_refused_before_a_request_is_sent(monkeypatch,
                                                               tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    calls = _install_failing_client(monkeypatch, 401)
    credential = P.acquire_credential(P.ORCAROUTER)
    P.mark_needs_reauth(credential, "revoked")

    with pytest.raises(C.ReauthRequired):
        C.chat("model", "system", "user")
    assert calls["n"] == 0, "a needsReauth credential must not be re-sent"


def test_late_failure_cannot_mark_a_newly_reauthorized_credential(monkeypatch,
                                                                 tmp_path):
    """The 401 recovery must be generation-safe."""
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))

    stale = P.acquire_credential(P.ORCAROUTER)
    fresh = P.PkceCredential().store(OTHER_KEY, scope="api")
    assert stale.generation != fresh.generation

    # A late failure arrives for the credential that was replaced.
    P.mark_needs_reauth(stale, "HTTP 401")

    assert P.needs_reauth(stale) is True
    assert P.needs_reauth(fresh) is False
    # And the live credential still works.
    _install_failing_client(monkeypatch, 429)
    with pytest.raises(C.LLMTransportError) as raised:
        C.chat("model", "system", "user")
    assert not isinstance(raised.value, C.ReauthRequired)


def test_a_successful_login_clears_the_reauth_flag(tmp_path):
    secret = tmp_path / "rsi.env"
    environment = env(RSIAGENT_ENV_FILE=str(secret))
    first = P.PkceCredential().store(FAKE_KEY, environment=environment)
    P.mark_needs_reauth(first, "HTTP 401")
    assert P.needs_reauth(P.acquire_credential(P.ORCAROUTER,
                                               environment=environment)) is True

    P.PkceCredential().store(OTHER_KEY, environment=environment)
    assert P.needs_reauth(P.acquire_credential(P.ORCAROUTER,
                                               environment=environment)) is False


def test_orcarouter_401_is_terminal_but_other_statuses_keep_their_policy(
        monkeypatch, tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))

    # 402 and 429 stay recoverable: a billing or rate-limit condition is not a
    # credential failure and must not force a re-login.
    for status, recoverable in ((402, True), (429, True), (400, False),
                                (500, True)):
        P.reset_credential_state()
        calls = _install_failing_client(monkeypatch, status)
        with pytest.raises(C.LLMTransportError) as raised:
            C.chat("model", "system", "user")
        assert not isinstance(raised.value, C.ReauthRequired)
        assert raised.value.recoverable is recoverable
        assert calls["n"] > 1 or not recoverable


def test_openrouter_401_is_unchanged(monkeypatch, tmp_path):
    """The OrcaRouter reauth policy must not alter the OpenRouter path."""
    secret = tmp_path / "rsi.env"
    secret.write_text("OPENROUTER_API_KEY=sk-or-v1-whatever\n")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    monkeypatch.delenv("RSIAGENT_LLM_PROVIDER", raising=False)
    calls = _install_failing_client(monkeypatch, 401)

    with pytest.raises(C.LLMTransportError) as raised:
        C.chat("model", "system", "user")
    assert not isinstance(raised.value, C.ReauthRequired)
    assert raised.value.recoverable is True
    assert calls["n"] == 4


def test_reauth_error_message_is_actionable_and_carries_no_key(monkeypatch,
                                                              tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    _install_failing_client(monkeypatch, 401)
    with pytest.raises(C.ReauthRequired) as raised:
        C.chat("model", "system", "user")

    message = str(raised.value)
    assert FAKE_KEY not in message
    assert "llm.connect login" in message
    assert raised.value.generation


# --- request provenance --------------------------------------------------------

def test_active_credential_generation_reports_which_key_a_request_used(
        monkeypatch, tmp_path):
    secret = tmp_path / "rsi.env"
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))

    from openai.types.chat import ChatCompletion
    reply = ChatCompletion.model_construct(
        id="gen", object="chat.completion", created=1, model="m", provider="p",
        choices=[SimpleNamespace(finish_reason="stop", tool_calls=[],
                                 message=SimpleNamespace(content="hi",
                                                         reasoning="",
                                                         tool_calls=[]))])
    monkeypatch.setattr(C, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kw: reply))))
    monkeypatch.setattr(C, "_PROVIDER_COUNTS", {})

    assert C.chat("model", "system", "user") == "hi"
    assert C.active_credential_generation() == P.credential_generation(
        P.ORCAROUTER, "api_key", FAKE_KEY)


# --- no mis-wired exchange path in source or config ----------------------------

def test_no_source_derives_the_inference_origin_from_the_auth_origin():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / "llm").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        # Documentation of the trap is not a violation; a constructed URL is.
        for line in text.splitlines():
            if "api.orcarouter.ai/v1/auth" in line and "404" not in line \
                    and "never" not in line and "#" not in line.split("api.orcarouter")[0]:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, f"mis-wired auth path: {offenders}"


def test_declared_env_names_are_documented_in_the_example_file():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert "ORCA_API_KEY" in example
    assert "RSIAGENT_LLM_PROVIDER" in example
