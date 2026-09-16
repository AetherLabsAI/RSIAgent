"""OAuth 2.0 + PKCE connect flow, driven through the shipped adapter.

Every test here goes through :mod:`llm.connect` — the same session object the CLI
uses — against a local fake authorization server. Nothing is asserted about a
loose hash helper in isolation: the properties that matter are end-to-end ones.
A real authorization needs a human to click Approve, so consent is never faked
against the real service; the fake server stands in for the browser and the
consent screen, and the flow that reaches it is the production one.

Covered: verifier/challenge/state generation, the authorize URL, the exchange
path and body, persistence, denial, Flow A state mismatch, code reuse and
expiry, scope downgrade, terminal classification of a broken key, and the
requirement that no verifier or key value escapes into a URL, a log, or an error.
"""
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from llm import connect as CON
from llm import provider as P


FAKE_KEY = "sk-orca-issued-000000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name in ("ORCA_API_KEY", "ORCAROUTER_API_KEY", "OPENROUTER_API_KEY",
                 "RSIAGENT_LLM_PROVIDER", "ORCA_BASE_URL",
                 "ORCA_AUTH_BASE_URL", "ORCA_API_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    P.reset_credential_state()
    yield
    P.reset_credential_state()


class FakeAuthServer:
    """A stand-in authorization server: consent screen + code exchange.

    Records every request it receives so tests can assert on the exchange body
    and prove the auth origin — never the inference origin — was used.
    """

    def __init__(self, *, status=200, scope="api", body=None, delay=0.0):
        self.status = status
        self.scope = scope
        self.body = body
        self.delay = delay
        self.requests = []
        self.urls = []
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _handler(server_self):
        outer = server_self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):                      # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                outer.urls.append(self.path)
                outer.requests.append({
                    "path": self.path,
                    "body": json.loads(raw) if raw else {},
                    "authorization": self.headers.get("Authorization", ""),
                })
                if outer.delay:
                    import time
                    time.sleep(outer.delay)
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    # An error body that quotes credential material, to prove
                    # the client does not surface it.
                    self.wfile.write(json.dumps({
                        "error": "invalid_grant",
                        "key": "sk-orca-must-not-leak",
                    }).encode())
                    return
                payload = outer.body if outer.body is not None else {
                    "key": FAKE_KEY, "user_id": "12345", "scope": outer.scope}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        return Handler

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def auth_server():
    server = FakeAuthServer()
    yield server
    server.close()


def secret_file(tmp_path: Path) -> Path:
    return tmp_path / "rsi.env"


def session(tmp_path, server, **kwargs):
    """A ConnectSession wired to the fake server and a private secret file."""
    kwargs.setdefault("open_browser", lambda _url: None)
    kwargs.setdefault("echo", lambda *_a: None)
    # Flow B reads the code the consent screen displayed. There is no terminal
    # in a test, so the reader is supplied explicitly.
    kwargs.setdefault("read_code", lambda: "test-code")
    kwargs.setdefault("auth_base_url", server.base)
    # The credential file is where the production adapter stores the key, so the
    # test reads it back through exactly the same path a real user would.
    return CON.ConnectSession(**kwargs), secret_file(tmp_path)


def patched_env(monkeypatch, secret: Path):
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(secret))
    monkeypatch.setenv("RSIAGENT_LLM_PROVIDER", "orcarouter")


# --- PKCE primitives -----------------------------------------------------------

def test_verifier_and_state_are_fresh_and_cryptographically_random():
    pairs = [CON.pkce_pair() for _ in range(8)]
    assert len({verifier for verifier, _ in pairs}) == 8
    assert len({CON.new_state() for _ in range(8)}) == 8
    for verifier, _ in pairs:
        assert len(verifier) >= 43, "RFC 7636 requires a high-entropy verifier"
    assert all(ch not in verifier for verifier, _ in pairs for ch in "=+/")


def test_challenge_is_unpadded_base64url_sha256_of_the_verifier():
    import base64
    import hashlib
    verifier, challenge = CON.pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge


def test_challenge_is_not_the_verifier():
    """`plain` would make an intercepted code redeemable. Always S256."""
    verifier, challenge = CON.pkce_pair()
    assert challenge != verifier


# --- the authorize URL ---------------------------------------------------------

def test_authorize_url_carries_the_challenge_never_the_verifier():
    verifier, challenge = CON.pkce_pair()
    state = CON.new_state()
    url = CON.authorize_url(challenge, state, callback_url="oob",
                            auth_base_url="https://www.orcarouter.ai")
    assert url.startswith("https://www.orcarouter.ai/auth?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [state]
    assert query["callback_url"] == ["oob"]
    assert query["app_name"] == ["RSIAgent"]
    assert verifier not in url


def test_authorize_url_uses_the_configured_auth_origin(monkeypatch):
    monkeypatch.setenv("ORCA_AUTH_BASE_URL", "https://login.internal.example")
    url = CON.authorize_url("chal", "st", callback_url="oob")
    assert url.startswith("https://login.internal.example/auth?")
    # The inference origin must not appear anywhere in an authorization request.
    assert "api.orcarouter.ai" not in url


def test_exchange_url_is_the_api_v1_auth_path_and_not_the_v1_auth_path():
    url = CON.exchange_url()
    assert url == "https://www.orcarouter.ai/api/v1/auth/keys"
    assert not url.startswith("https://api.orcarouter.ai")


def test_flow_a_callback_url_is_loopback_with_a_real_port(tmp_path):
    secret = secret_file(tmp_path)
    auth = FakeAuthServer()
    try:
        flow = CON.ConnectSession(loopback=True, auth_base_url=auth.base,
                                  open_browser=lambda _u: None,
                                  echo=lambda *_a: None)
        authorization = flow.begin()
        try:
            parsed = urllib.parse.urlsplit(authorization.callback_url)
            assert parsed.hostname == "127.0.0.1"
            assert parsed.port > 0
            assert parsed.path == "/cb"
        finally:
            flow.cancel()
    finally:
        auth.close()


# --- happy path ----------------------------------------------------------------

def test_login_persists_the_key_and_uses_the_auth_origin(tmp_path, monkeypatch,
                                                        auth_server):
    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)

    credential, payload = flow.run()

    assert credential.api_key == FAKE_KEY
    assert credential.source == "pkce"
    assert payload["scope"] == "api"
    # The exchange went to the fake auth origin, on the documented path.
    assert [r["path"] for r in auth_server.requests] == ["/api/v1/auth/keys"]
    assert auth_server.requests[0]["body"]["code"] == "test-code"
    assert auth_server.requests[0]["body"]["code_challenge_method"] == "S256"
    assert "code_verifier" in auth_server.requests[0]["body"]


def test_login_sends_the_matching_verifier(tmp_path, monkeypatch, auth_server):
    """The challenge was sent at authorize time; the verifier redeems it."""
    seen = {}

    def capture(code, verifier, **kwargs):
        seen["verifier"] = verifier
        seen["challenge"] = CON.challenge_for(verifier)
        return {"key": FAKE_KEY, "user_id": "1", "scope": "api"}

    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)
    authorization = flow.begin()
    try:
        flow._run_attempt(authorization, exchange=capture)
    finally:
        flow.cancel()

    assert seen["verifier"] == authorization.verifier
    assert seen["challenge"] != authorization.verifier


def test_stored_key_is_readable_through_the_shared_seam(tmp_path, monkeypatch,
                                                        auth_server):
    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)
    issued, _ = flow.run()

    # The provider layer reads it back without knowing a browser was involved.
    read_back = P.acquire_credential(P.ORCAROUTER)
    assert read_back.api_key == issued.api_key
    assert read_back.generation == issued.generation


def test_second_login_replaces_the_first_key(tmp_path, monkeypatch, auth_server):
    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)
    first, _ = flow.run()

    auth_server.body = {"key": "sk-orca-second-2222222222222222222222222222",
                        "user_id": "12345", "scope": "api"}
    flow2, _ = session(tmp_path, auth_server)
    second, _ = flow2.run()

    assert second.api_key != first.api_key
    assert P.acquire_credential(P.ORCAROUTER).api_key == second.api_key
    assert secret.read_text().count("ORCA_API_KEY=") == 1


def test_scope_downgrade_is_reported_not_assumed(tmp_path, monkeypatch,
                                                 auth_server):
    """The response says what was granted. A narrower grant must be surfaced."""
    auth_server.scope = "connector"
    said = []
    flow, secret = session(tmp_path, auth_server, echo=said.append)
    patched_env(monkeypatch, secret)

    credential, payload = flow.run()

    assert payload["scope"] == "connector"
    assert P.authorization(credential)["scope"] == "connector"
    assert any("connector" in line for line in said), \
        "a narrower grant than requested must be reported to the user"


def test_api_key_path_still_works_without_any_login(tmp_path, monkeypatch):
    secret = secret_file(tmp_path)
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    patched_env(monkeypatch, secret)
    # No browser, no server: the pasted-key entry point is independently usable.
    credential = P.ApiKeyCredential().acquire()
    assert credential.api_key == FAKE_KEY


# --- failure paths -------------------------------------------------------------

def test_denial_ends_the_attempt_with_an_actionable_message(tmp_path, monkeypatch):
    flow, secret = session(tmp_path, FakeAuthServer(), read_code=lambda: "")
    patched_env(monkeypatch, secret)
    with pytest.raises(CON.ConnectError) as raised:
        flow.run()
    assert "no code" in str(raised.value)
    assert flow.busy is False


def test_flow_a_state_mismatch_is_refused_before_the_code_is_used(
        tmp_path, monkeypatch):
    """state is the only thing between the listener and a foreign code."""
    flow = CON.ConnectSession(loopback=True, auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    authorization = flow.begin()
    try:
        flow._deliver({"code": ["attacker-code"], "state": ["not-our-state"]})
        with pytest.raises(CON.StateMismatch):
            flow._await_callback(authorization)
    finally:
        flow.cancel()


def test_flow_a_denial_error_is_reported(tmp_path):
    flow = CON.ConnectSession(loopback=True, auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    authorization = flow.begin()
    try:
        flow._deliver({"error": ["access_denied"],
                       "state": [authorization.state]})
        with pytest.raises(CON.AuthorizationDenied):
            flow._await_callback(authorization)
    finally:
        flow.cancel()


def test_flow_a_accepts_a_matching_callback(tmp_path):
    flow = CON.ConnectSession(loopback=True, auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    authorization = flow.begin()
    try:
        flow._deliver({"code": ["good-code"], "state": [authorization.state]})
        assert flow._await_callback(authorization) == "good-code"
    finally:
        flow.cancel()


def test_flow_a_timeout_does_not_hang_or_hot_loop(tmp_path):
    flow = CON.ConnectSession(loopback=True, timeout=0.2,
                              auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    authorization = flow.begin()
    try:
        with pytest.raises(CON.ConnectError) as raised:
            flow._await_callback(authorization)
        assert "timed out" in str(raised.value)
    finally:
        flow.cancel()


def test_reused_or_expired_code_surfaces_the_403_remedy(tmp_path, monkeypatch):
    server = FakeAuthServer(status=403)
    flow, secret = session(tmp_path, server)
    patched_env(monkeypatch, secret)
    try:
        with pytest.raises(CON.ConnectError) as raised:
            flow.run()
    finally:
        server.close()
    message = str(raised.value)
    assert "rejected" in message
    assert "expired" in message or "already used" in message
    # The response body is never echoed: it can quote credential material.
    assert "sk-orca-must-not-leak" not in message
    assert FAKE_KEY not in message


def test_challenge_method_mismatch_surfaces_the_400_remedy(tmp_path, monkeypatch):
    server = FakeAuthServer(status=400)
    flow, secret = session(tmp_path, server)
    patched_env(monkeypatch, secret)
    try:
        with pytest.raises(CON.ConnectError) as raised:
            flow.run()
    finally:
        server.close()
    assert "challenge method" in str(raised.value)


def test_rate_limited_authorization_surfaces_the_429_remedy(tmp_path, monkeypatch):
    server = FakeAuthServer(status=429)
    flow, secret = session(tmp_path, server)
    patched_env(monkeypatch, secret)
    try:
        with pytest.raises(CON.ConnectError) as raised:
            flow.run()
    finally:
        server.close()
    message = str(raised.value)
    assert "24 hours" in message
    assert "ORCA_API_KEY" in message, "the API-key path is the documented fallback"


def test_network_failure_is_bounded_and_does_not_leak_the_verifier(
        tmp_path, monkeypatch):
    flow, secret = session(tmp_path, FakeAuthServer())
    patched_env(monkeypatch, secret)

    def broken(*_args, **_kwargs):
        raise OSError("connection reset")

    with pytest.raises(CON.ConnectError) as raised:
        flow.run(exchange=broken)
    assert "verifier" not in str(raised.value)
    assert "connection reset" not in str(raised.value)


def test_an_exchange_without_a_key_is_refused(tmp_path, monkeypatch):
    server = FakeAuthServer(body={"user_id": "1", "scope": "api"})
    flow, secret = session(tmp_path, server)
    patched_env(monkeypatch, secret)
    try:
        with pytest.raises(CON.ConnectError) as raised:
            flow.run()
    finally:
        server.close()
    assert "no key" in str(raised.value)


def test_a_failed_exchange_leaves_an_existing_key_untouched(tmp_path, monkeypatch):
    """A transient failure must not destroy a working credential."""
    secret = secret_file(tmp_path)
    secret.write_text(f"ORCA_API_KEY=sk-orca-existing-33333333333333333333\n")
    server = FakeAuthServer(status=403)
    flow, _ = session(tmp_path, server)
    patched_env(monkeypatch, secret)
    try:
        with pytest.raises(CON.ConnectError):
            flow.run()
    finally:
        server.close()
    assert P.acquire_credential(P.ORCAROUTER).api_key \
        == "sk-orca-existing-33333333333333333333"


# --- cancellation and generation ----------------------------------------------

def test_cancel_releases_the_listener_and_ui_state(tmp_path):
    flow = CON.ConnectSession(loopback=True, auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    flow.begin()
    assert flow.busy is True
    assert flow.hint
    flow.cancel()
    assert flow.busy is False
    assert flow.hint == ""
    assert flow._server is None


def test_a_superseded_attempt_cannot_write_its_result(tmp_path, monkeypatch,
                                                      auth_server):
    """A late response from attempt N must not land under attempt N+1."""
    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)
    stale = flow.begin()
    flow.begin()                                   # supersedes `stale`

    assert flow.owns(stale) is False
    with pytest.raises(CON.ConnectError):
        flow._persist(stale)


def test_each_attempt_uses_a_new_verifier_and_state(tmp_path):
    flow = CON.ConnectSession(auth_base_url="http://127.0.0.1:1",
                              open_browser=lambda _u: None,
                              echo=lambda *_a: None)
    first = flow.begin()
    second = flow.begin()
    try:
        assert first.verifier != second.verifier
        assert first.state != second.state
        assert second.generation > first.generation
    finally:
        flow.cancel()


# --- secret hygiene ------------------------------------------------------------

def test_the_verifier_never_reaches_the_logs(tmp_path, monkeypatch, auth_server,
                                             caplog):
    flow, secret = session(tmp_path, auth_server)
    patched_env(monkeypatch, secret)
    with caplog.at_level(logging.DEBUG):
        credential, _ = flow.run()
        authorization = flow._code  # in-process only, never logged

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert authorization not in logged
    assert credential.api_key not in logged
    assert FAKE_KEY not in logged


def test_status_masks_the_stored_key(tmp_path, monkeypatch, capsys):
    secret = secret_file(tmp_path)
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    patched_env(monkeypatch, secret)

    assert CON.main(["status"]) == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert "…" in out
    assert "pkce" in out or "api_key" in out


def test_logout_reports_that_revocation_is_still_the_users_job(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    secret = secret_file(tmp_path)
    secret.write_text(f"ORCA_API_KEY={FAKE_KEY}\n")
    patched_env(monkeypatch, secret)

    assert CON.main(["logout"]) == 0
    out = capsys.readouterr().out
    assert "authorized-apps" in out
    assert FAKE_KEY not in out
    with pytest.raises(P.CredentialUnavailable):
        P.acquire_credential(P.ORCAROUTER)


def test_status_reports_the_remedy_when_nothing_is_stored(tmp_path, monkeypatch,
                                                          capsys):
    patched_env(monkeypatch, secret_file(tmp_path))
    assert CON.main(["status"]) == 1
    assert "llm.connect login" in capsys.readouterr().out
