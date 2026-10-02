"""Builder ID sign-in for the AWS Events API.

OAuth 2.0 authorization code flow with PKCE. The callback listens on one of the
six reserved loopback ports. Tokens go to the OS keychain, never to disk or logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import keyring

from . import config


class AuthError(Exception):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds
    id_token: str | None = None

    @property
    def expired(self) -> bool:
        # 60 seconds of slack so a request in flight does not hit a dead token.
        return time.time() >= self.expires_at - 60


# --------------------------------------------------------------------------- storage


class TokenStore:
    """Keyring-backed store. Swap in a different backend for tests."""

    def __init__(self, service: str = config.KEYRING_SERVICE, user: str = config.KEYRING_USER):
        self.service, self.user = service, user

    def load(self) -> Tokens | None:
        raw = keyring.get_password(self.service, self.user)
        if not raw:
            return None
        return Tokens(**json.loads(raw))

    def save(self, tokens: Tokens) -> None:
        keyring.set_password(self.service, self.user, json.dumps(asdict(tokens)))

    def clear(self) -> None:
        try:
            keyring.delete_password(self.service, self.user)
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._t: Tokens | None = None

    def load(self) -> Tokens | None:
        return self._t

    def save(self, tokens: Tokens) -> None:
        self._t = tokens

    def clear(self) -> None:
        self._t = None


# --------------------------------------------------------------------------- PKCE


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """Return (verifier, challenge). Fresh every attempt, as the guide requires."""
    verifier = _b64url(secrets.token_bytes(64))[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _free_port() -> int:
    for port in config.CALLBACK_PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise AuthError("Ports 8484 to 8489 are all busy. Free one and try again.")


# --------------------------------------------------------------------------- callback server


class _Callback(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(url.query))
        _Callback.result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = "<h2>Signed in. You can close this tab and return to re:Seat.</h2>"
        if "error" in params:
            body = f"<h2>Sign-in failed: {params.get('error_description', params['error'])}</h2>"
        self.wfile.write(body.encode())

    def log_message(self, *_: object) -> None:  # silence
        pass


def _wait_for_code(port: int, timeout: float) -> dict[str, str]:
    _Callback.result = {}
    server = HTTPServer(("127.0.0.1", port), _Callback)
    server.timeout = 1
    deadline = time.time() + timeout
    while time.time() < deadline and not _Callback.result:
        server.handle_request()
    server.server_close()
    if not _Callback.result:
        raise AuthError("Timed out waiting for the browser to return.")
    return _Callback.result


# --------------------------------------------------------------------------- flows


def _token_request(data: dict[str, str]) -> Tokens:
    r = httpx.post(
        f"{config.OAUTH_BASE}/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code != 200:
        raise AuthError(f"Token endpoint returned {r.status_code}: {r.text[:200]}")
    body = r.json()
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", data.get("refresh_token", "")),
        expires_at=time.time() + float(body.get("expires_in", 3600)),
        id_token=body.get("id_token"),
    )


def login(store: TokenStore | None = None, open_browser: bool = True, timeout: float = 300) -> Tokens:
    """Interactive sign-in. Prints the URL if the browser cannot be opened."""
    store = store or TokenStore()
    port = _free_port()
    redirect_uri = f"http://localhost:{port}/callback"
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": config.SCOPES,
            "identity_provider": config.IDENTITY_PROVIDER,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    url = f"{config.OAUTH_BASE}/oauth2/authorize?{query}"

    # Start listening before opening the browser so a fast redirect is not lost.
    result: dict[str, str] = {}
    err: list[Exception] = []

    def _listen() -> None:
        try:
            result.update(_wait_for_code(port, timeout))
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    print("Open this URL to sign in with your AWS Builder ID:\n\n" + url + "\n")
    if open_browser:
        webbrowser.open(url)
    t.join()
    if err:
        raise err[0]
    if "error" in result:
        raise AuthError(result.get("error_description", result["error"]))
    if result.get("state") != state:
        raise AuthError("State mismatch. Possible cross-site attempt. Sign-in aborted.")
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": config.CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code": result["code"],
            "code_verifier": verifier,
        }
    )
    store.save(tokens)
    return tokens


def refresh(tokens: Tokens, store: TokenStore | None = None) -> Tokens:
    store = store or TokenStore()
    new = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": config.CLIENT_ID,
            "refresh_token": tokens.refresh_token,
        }
    )
    store.save(new)
    return new


def current_access_token(store: TokenStore | None = None) -> str:
    """Return a valid access token, refreshing silently if needed."""
    store = store or TokenStore()
    tokens = store.load()
    if tokens is None:
        raise AuthError("Not signed in. Run: reseat login")
    if tokens.expired:
        tokens = refresh(tokens, store)
    return tokens.access_token


def logout(store: TokenStore | None = None) -> None:
    """Revoke the refresh token and forget both tokens.

    This signs the attendee out of re:Seat. It does not end the Builder ID browser
    session. For that, point them to https://profile.aws.amazon.com.
    """
    store = store or TokenStore()
    tokens = store.load()
    if tokens:
        try:
            httpx.post(
                f"{config.OAUTH_BASE}/oauth2/revoke",
                data={"client_id": config.CLIENT_ID, "token": tokens.refresh_token},
                timeout=15,
            )
        except httpx.HTTPError:
            pass
    store.clear()
