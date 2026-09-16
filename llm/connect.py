"""`python -m llm.connect` — authenticate RSIAgent with an OrcaRouter account.

RSIAgent is driven over SSH, from a container, and from a CI runner far more often
than from a desktop session, and its install address differs on every deployment
(LAN box, NAS, moved port). That is exactly the case the out-of-band flow exists
for: there is no callback address to pre-register and nothing to make
predictable, and the code is typed by the person rather than delivered to a
listener.

So the default is **Flow B (out-of-band code)**, and **Flow A (loopback
redirect)** is an explicit opt-in for a local desktop session:

    python -m llm.connect login              # Flow B — browser, then paste a code
    python -m llm.connect login --loopback   # Flow A — a real 127.0.0.1 listener
    python -m llm.connect status             # what credential is stored, and from where
    python -m llm.connect logout             # remove it

Device grant (Flow C) is deliberately not implemented. It cannot substitute for
PKCE, and RSIAgent's users have a browser on some machine they can reach.

What comes back is not a refreshable access token. It is an ordinary, durable
OrcaRouter API key belonging to the user — billed to their account, listed in
their console, revocable by them — so it is stored through the project's existing
secret mechanism (:mod:`llm.provider`) and reused until it is revoked. There is
no refresh grant, and re-authorizing on every launch would exhaust the ten
PKCE-issued keys per user per day.

PKCE discipline, all of which the tests exercise through this module:

- the verifier comes from :func:`secrets.token_bytes` fresh on every attempt;
- only ``base64url(sha256(verifier))``, unpadded, reaches the authorize URL;
- the verifier stays in this process until the exchange and is never logged,
  printed, put in a URL, or included in an error;
- ``S256`` is always sent, never ``plain`` — the consent screen can hand the user
  a code to copy even in Flow A, and a displayed code redeemed with a ``plain``
  challenge is redeemable by anyone who saw it;
- ``state`` is compared in constant time before the code is used;
- every failure path — denial, mismatch, timeout, expired or reused code, 403,
  429, transport error — ends with a bounded, actionable message and no leaked
  response body.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass

from llm.provider import (
    AUTHORIZE_PATH,
    AUTHORIZED_APPS_URL,
    EXCHANGE_PATH,
    ORCA_KEY_ENV,
    ORCAROUTER,
    ApiKeyCredential,
    Credential,
    CredentialUnavailable,
    api_base,
    auth_base,
    auth_url,
    credential_adapters,
    provider_profile,
)
from llm.provider import PkceCredential

APP_NAME = "RSIAgent"
DEFAULT_SCOPE = "api"
DEFAULT_TIMEOUT = 300.0
LOOPBACK_TIMEOUT = 300.0
_CALLBACK_PATH = "/cb"
_LOOPBACK_HOST = "127.0.0.1"

# Error codes from the exchange endpoint. These are protocol facts, not guesses:
# 400 means the challenge method was refused or downgraded, 403 means the code is
# unknown/expired/already used or the verifier does not match.
_HTTP_REMEDY = {
    400: "the authorization request was refused (challenge method mismatch or "
         "downgrade). Start a new login.",
    403: "the code was rejected — it is unknown, expired, or already used, or it "
         "does not match this process's verifier. Start a new login.",
    429: "too many OrcaRouter authorizations were issued recently (the limit is "
         "10 keys per user per 24 hours). Wait before trying again, or paste an "
         "existing key with ORCA_API_KEY.",
}


class ConnectError(RuntimeError):
    """A login attempt failed. The message is safe to print to the user."""


class AuthorizationDenied(ConnectError):
    """The user declined on the consent screen."""


class StateMismatch(ConnectError):
    """The callback carried a state this process did not generate."""


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(verifier, challenge)``. Cryptographic randomness, every attempt.

    The verifier is never derived from anything guessable — not a timestamp, not
    a username, not a fixed salt — and never reused across attempts. The
    challenge is unpadded base64url of the SHA-256 digest, as S256 requires.
    """
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, challenge_for(verifier)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    """``base64url(sha256(verifier))`` with no padding. Never sends the verifier."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """An opaque CSRF token, fresh per attempt."""
    return _b64url(secrets.token_bytes(16))


def authorize_url(challenge: str, state: str, *, callback_url: str,
                  scope: str = DEFAULT_SCOPE, app_name: str = APP_NAME,
                  auth_base_url: str | None = None) -> str:
    """Build the consent URL.

    ``auth_base_url`` overrides the resolved auth origin for tests; production
    callers omit it and the configured origin is used. The URL carries the
    challenge and the state — never the verifier.
    """
    base = auth_base_url or auth_base()
    query = urllib.parse.urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "app_name": app_name,
        "scope": scope,
    })
    return f"{base}{AUTHORIZE_PATH}?{query}"


def exchange_url() -> str:
    """The code-exchange URL, always on the auth origin.

    Spelled out rather than derived so it cannot drift onto the inference origin:
    ``https://api.orcarouter.ai/v1/auth/keys`` is a 404, and the mistake is not
    visible from the URL shape.
    """
    return auth_url(EXCHANGE_PATH)


def exchange_code(code: str, verifier: str, *, timeout: float = 30.0,
                  auth_base_url: str | None = None) -> dict:
    """Redeem an auth code for a durable API key.

    Returns ``{"key", "user_id", "scope"}`. The response body is never included
    in a raised error: a 403 or 401 body can quote the credential material.
    """
    url = (f"{auth_base_url}{EXCHANGE_PATH}" if auth_base_url else exchange_url())
    body = json.dumps({
        "code": code,
        "code_verifier": verifier,
        "code_challenge_method": "S256",
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        remedy = _HTTP_REMEDY.get(
            exc.code, f"the authorization server returned HTTP {exc.code}.")
        raise ConnectError(f"code exchange failed: {remedy}") from None
    except Exception as exc:                        # noqa: BLE001
        raise ConnectError(
            f"code exchange failed: {type(exc).__name__}") from None
    key = payload.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ConnectError("code exchange returned no key")
    return payload


@dataclass
class Authorization:
    """One in-flight authorization, owning its verifier and its generation."""

    verifier: str
    state: str
    callback_url: str
    url: str
    generation: int = 0


class ConnectSession:
    """Drives one login attempt and owns all of its cancellation state.

    A monotonically increasing generation guards every asynchronous result. A
    response that belongs to a superseded attempt is discarded rather than
    applied, so a late callback or a slow exchange from a previous attempt can
    never overwrite a newer credential — and ``pagehide``/cancel semantics have a
    single place to live.
    """

    def __init__(self, *, scope: str = DEFAULT_SCOPE, app_name: str = APP_NAME,
                 auth_base_url: str | None = None,
                 loopback: bool = False, timeout: float = DEFAULT_TIMEOUT,
                 open_browser=webbrowser.open, echo=print, read_code=None):
        self.scope = scope
        self.app_name = app_name
        self.auth_base_url = auth_base_url
        self.loopback = loopback
        self.timeout = timeout
        self._open_browser = open_browser
        self._echo = echo
        # The code reader is a seam rather than a bare input() call so a front
        # end that already has a text field can supply the code without a
        # terminal, and so the flow is testable without one.
        self._read_code = read_code or (lambda: input("Code: ").strip())
        self._generation = 0
        self._busy = False
        self._hint = ""
        self._server = None
        self._listener = None
        self._pending_state = ""
        self._result = None
        self._done = threading.Event()
        self._code = ""
        self._payload: dict = {}

    # -- state the CLI and any GUI wrapper read -------------------------------
    @property
    def generation(self) -> int:
        return self._generation

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def hint(self) -> str:
        """The authorization URL currently displayed, or ``''``."""
        return self._hint

    def begin(self) -> Authorization:
        """Mint a fresh attempt: new verifier, new state, new generation."""
        self.cancel()
        self._generation += 1
        verifier, challenge = pkce_pair()
        state = new_state()
        if self.loopback:
            # Arm the callback state at the same moment the listener starts
            # accepting, so a redirect cannot arrive before there is anything to
            # compare its state against.
            self._pending_state = state
            self._result = None
            self._done = threading.Event()
            callback_url = self._start_listener()
        else:
            callback_url = "oob"
        url = authorize_url(challenge, state, callback_url=callback_url,
                            scope=self.scope, app_name=self.app_name,
                            auth_base_url=self.auth_base_url)
        self._busy = True
        self._hint = url
        return Authorization(verifier, state, callback_url, url,
                             self._generation)

    def owns(self, authorization: Authorization) -> bool:
        """Whether ``authorization`` is still the live attempt."""
        return authorization.generation == self._generation

    def complete(self) -> None:
        """Release UI state after a terminal outcome."""
        self._busy = False
        self._hint = ""
        self._close_listener()

    def cancel(self) -> None:
        """Abandon the current attempt and release every resource it holds.

        Called on explicit cancel, on timeout, on an exchange error, and when the
        user switches authentication method. Bumping the generation first means
        any response still in flight for the abandoned attempt is discarded.
        """
        self._generation += 1
        self._busy = False
        self._hint = ""
        self._close_listener()

    def _close_listener(self) -> None:
        server, self._server = self._server, None
        thread, self._listener = self._listener, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:                       # noqa: BLE001
                pass
        if thread is not None:
            thread.join(timeout=2.0)

    # -- Flow A ---------------------------------------------------------------
    def _start_listener(self) -> str:
        """Bind loopback *before* the browser opens, so the port is known.

        Binding first is what makes the redirect race-free: the address is on the
        authorize URL before anything can call back to it.
        """
        session = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):          # never log the query string
                return

            def do_GET(self):                       # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                params = urllib.parse.parse_qs(parsed.query)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<p>Connected. You can close this tab.</p>")
                session._deliver(params)

        server = http.server.HTTPServer((_LOOPBACK_HOST, 0), Handler)
        self._server = server
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._listener = thread
        return f"http://{_LOOPBACK_HOST}:{port}{_CALLBACK_PATH}"

    def _deliver(self, params: dict) -> None:
        """Accept one callback. Compare state before touching the code."""
        supplied = (params.get("state") or [""])[0]
        expected = self._pending_state
        if not secrets.compare_digest(str(supplied), str(expected)):
            self._result = StateMismatch(
                "the authorization response did not match this login attempt; "
                "it was ignored.")
        elif (params.get("error") or [""])[0]:
            self._result = AuthorizationDenied(
                "authorization was denied on the consent screen.")
        else:
            self._result = (params.get("code") or [""])[0]
        self._done.set()

    # -- driving --------------------------------------------------------------
    def run(self, *, exchange=exchange_code) -> tuple[Credential, dict]:
        """Run one full authorization and persist the resulting key.

        Returns ``(credential, exchange_response)``. The stored credential is
        written only after the exchange succeeds, so a failure never destroys a
        working key; :meth:`llm.provider.PkceCredential.store` is the single
        writer.
        """
        authorization = self.begin()
        try:
            self._run_attempt(authorization, exchange=exchange)
            return self._persist(authorization)
        except BaseException:
            # Every terminal path — denial, mismatch, timeout, exchange error,
            # cancel — releases the busy flag, the displayed URL and the
            # listener. Leaving any of them set would strand the next attempt.
            self.complete()
            raise

    def _persist(self, authorization: Authorization) -> tuple[Credential, dict]:
        code = self._code
        payload = self._payload
        if not self.owns(authorization):
            raise ConnectError("this login attempt was superseded")
        granted = str(payload.get("scope") or "")
        if granted and granted != self.scope:
            # Read the granted scope back rather than assuming the requested one
            # was approved: a workspace role can narrow it.
            self._echo(f"warning: OrcaRouter granted scope {granted!r}, "
                       f"not {self.scope!r}.")
        try:
            credential = PkceCredential().store(
                payload["key"], scope=granted or self.scope,
                user_id=str(payload.get("user_id") or ""))
        except CredentialUnavailable as exc:
            raise ConnectError(str(exc)) from exc
        self.complete()
        return credential, payload

    def _run_attempt(self, authorization: Authorization, *,
                     exchange) -> None:
        self._echo(f"Authorize RSIAgent in your browser:\n  {authorization.url}")
        try:
            self._open_browser(authorization.url)
        except Exception:                           # noqa: BLE001
            self._echo("(could not open a browser automatically; use the URL above)")
        if self.loopback:
            code = self._await_callback(authorization)
        else:
            code = self._await_pasted_code(authorization)
        if not self.owns(authorization):
            raise ConnectError("this login attempt was superseded")
        try:
            payload = exchange(code, authorization.verifier,
                               auth_base_url=self.auth_base_url)
        except ConnectError:
            raise
        except Exception as exc:                    # noqa: BLE001
            # A transport failure is a bounded, reportable outcome — never a
            # traceback that could carry the verifier or the code.
            raise ConnectError(
                f"code exchange failed: {type(exc).__name__}") from None
        if not self.owns(authorization):
            raise ConnectError("this login attempt was superseded")
        self._code, self._payload = code, payload

    def _await_pasted_code(self, authorization: Authorization) -> str:
        self._echo(f"\nIf the browser does not open, visit:\n  {authorization.url}\n")
        self._echo("The consent screen shows a code. Paste it here.")
        try:
            code = (self._read_code() or "").strip()
        except EOFError:
            raise ConnectError(
                "no code was provided; the login attempt was abandoned.") from None
        if not code:
            raise ConnectError("no code was provided.")
        return code

    def _await_callback(self, authorization: Authorization) -> str:
        if not self._done.wait(timeout=min(self.timeout, LOOPBACK_TIMEOUT)):
            raise ConnectError(
                "timed out waiting for the browser to return to this process.")
        result = self._result
        if isinstance(result, ConnectError):
            raise result
        if not result:
            raise ConnectError("the authorization response carried no code.")
        return result


def _cmd_login(args) -> int:
    loopback = bool(args.loopback)
    session = ConnectSession(loopback=loopback, scope=args.scope,
                            open_browser=(lambda url: None) if args.no_browser
                            else webbrowser.open)
    try:
        credential, payload = session.run()
    except AuthorizationDenied as exc:
        print(f"Login cancelled: {exc}")
        return 2
    except ConnectError as exc:
        print(f"Login failed: {exc}")
        return 1
    except KeyboardInterrupt:
        session.cancel()
        print("\nLogin cancelled.")
        return 130
    finally:
        session.complete()
    print(f"\nConnected. {ORCA_KEY_ENV} is stored for {APP_NAME}.")
    print(f"  credential: {credential.source} "
          f"(generation {credential.generation})")
    print(f"  inference:  {api_base()}")
    print(f"  manage or revoke this key: {AUTHORIZED_APPS_URL}")
    return 0


def _cmd_status(args) -> int:
    profile = provider_profile(ORCAROUTER)
    print(f"provider:   {profile.label}")
    print(f"inference:  {api_base()}")
    print(f"auth:       {auth_base()}")
    try:
        credential = ApiKeyCredential().acquire()
    except CredentialUnavailable:
        print(f"credential: none — set {ORCA_KEY_ENV} or run "
              f"`python -m llm.connect login`")
        return 1
    print(f"credential: stored ({credential.source}), "
          f"generation {credential.generation}, "
          f"key {_mask(credential.api_key)}")
    if args.verify:
        from llm.catalog import CatalogError, discover
        result = discover(refresh=True)
        print(f"catalog:    {result.label()}")
        for model_id in result.ids:
            print(f"  {model_id}")
    return 0


def _mask(api_key: str) -> str:
    """A short, non-reversible display form. Never the usable key."""
    if len(api_key) <= 12:
        return "*" * len(api_key)
    return f"{api_key[:8]}…{api_key[-4:]}"


def _cmd_logout(args) -> int:
    generation = credential_adapters()["pkce"].clear()
    if generation:
        print(f"Removed the stored {ORCA_KEY_ENV} "
              f"(generation {generation}). It remains valid on OrcaRouter until "
              f"you revoke it at {AUTHORIZED_APPS_URL}.")
    else:
        print(f"No stored {ORCA_KEY_ENV} to remove.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m llm.connect",
        description="Connect RSIAgent to an OrcaRouter account.")
    sub = p.add_subparsers(dest="command", required=True)

    login = sub.add_parser(
        "login", help="authorize in a browser and store the issued API key")
    login.add_argument(
        "--loopback", action="store_true",
        help="Flow A: listen on 127.0.0.1 for the redirect instead of pasting "
             "a code. Only useful when this machine has a browser.")
    login.add_argument("--no-browser", action="store_true",
                       help="print the URL only; do not try to open a browser")
    login.add_argument("--scope", default=DEFAULT_SCOPE,
                       choices=("api", "connector"),
                       help="requested grant (default: api)")
    login.set_defaults(func=_cmd_login)

    status = sub.add_parser("status", help="show the stored credential")
    status.add_argument("--verify", action="store_true",
                        help="also fetch the live model catalog")
    status.set_defaults(func=_cmd_status)

    logout = sub.add_parser("logout", help="remove the stored credential")
    logout.set_defaults(func=_cmd_logout)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
