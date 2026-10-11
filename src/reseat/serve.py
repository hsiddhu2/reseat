"""The web app: a small HTTP server on the laptop, for the laptop's browser and the phone.

The API only allows sign-in on the attendee's own machine, so the token stays on
the laptop and the laptop makes every API call. The browser only ever talks to
this server, which shows a snapshot and accepts approvals by plan id. Three views:
the dashboard at `/`, approve at `/approve` and today at `/today` (pages.py).

Rules this module exists to respect:
- No endpoint takes a session id. Approve and Skip take a plan id the watcher
  proposed. Plan ids are random, expire after 10 minutes and work once.
- Bound to loopback by default. Any other address needs `serve_secret` in the
  rules file, or the server refuses to start. 0.0.0.0 is always refused: name
  the one address to listen on, such as the laptop's Tailscale address.
- The secret never goes in a URL. The laptop prints a one-time link with a
  single-use code that sets a cookie and redirects to a clean URL. Signing in
  again from the phone is a POST form, rate limited, compared in constant time.
- Writes need the cookie and an `X-Reseat` header, which a cross-site form
  cannot send. Without a secret on loopback, the Host header must be the
  loopback address, so a DNS-rebinding page cannot reach the server.
- One thread owns the API client and the store: the watcher's. HTTP threads read
  a snapshot it builds and hand approvals to it as jobs.
- The access token is never in a page, a response, a log line or a push.
- Not a security product. It stops a neighbour on hotel Wi-Fi from approving swaps.
"""

from __future__ import annotations

import concurrent.futures
import hmac
import json
import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from . import guard, pages
from .campus import EVENT_DAYS, VEGAS
from .models import Session
from .rules import Rules
from .store import Store
from .watcher import ApprovalFailed, Watcher, WatchEvent

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 8490
COOKIE = "reseat_session"
COOKIE_DAYS = 7
LINK_TTL = 3600
LOGIN_TRIES = 5          # failed sign-ins allowed per minute
APPROVE_WAIT = 120       # seconds an approve may take: check, cancel, reserve, read-backs
PLAN_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_BODY = 2048
VIEWS = ("/", "/approve", "/today")
REFRESH_EVERY = 20       # seconds between snapshot refreshes, so leave-now never waits on a sweep


class ServeError(Exception):
    """The server cannot start as asked. The message says what to fix."""


def vegas(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, VEGAS)


class App:
    """State and rules for the web app. No HTTP here, so tests can drive it directly."""

    def __init__(self, watcher: Watcher, store: Store, rules: Rules, event_id: str,
                 host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 clock: Callable[[], float] = time.time, demo: bool = False, cookie: str = COOKIE):
        if host == "0.0.0.0":
            raise ServeError("Refusing to listen on every interface (0.0.0.0). Use 127.0.0.1, or the one "
                             "address the phone reaches, such as the laptop's Tailscale address.")
        if ":" in host:
            raise ServeError(f"{host}: IPv6 addresses are not supported. Use 127.0.0.1 or an IPv4 "
                             "address such as the laptop's Tailscale address.")
        if host not in LOOPBACK and not rules.serve_secret:
            raise ServeError(f"Refusing to listen on {host} without serve_secret in the rules file. "
                             "Loopback (127.0.0.1) needs none.")
        self.watcher, self.store, self.rules, self.event_id = watcher, store, rules, event_id
        self.host, self.port, self.clock, self.demo = host, port, clock, demo
        self.cookie = cookie       # browsers share cookies across ports, so the demo uses its own name
        # Demo mode only: the control panel's scenarios, (key, label, what you should see), and a runner.
        self.scenarios: list[dict[str, str]] = []
        self.run_scenario: Callable[[str], str] | None = None
        self.last_scenario: dict[str, Any] | None = None
        self.tour_label = "Start the guided tour"
        self.secret = rules.serve_secret
        self.auth_required = bool(self.secret)
        self._tokens: dict[str, float] = {}     # cookie token -> expiry. Cleared on restart.
        self._lock = threading.Lock()
        self._link_code = secrets.token_urlsafe(24)
        self._link_expires = clock() + LINK_TTL
        self._failures: deque[float] = deque()
        self._snapshot: dict[str, Any] = {}
        self._leave_sent: set[str] = set()
        self._replied: deque[str] = deque(maxlen=200)     # live plan ids already tapped
        watcher.subscribe(self._on_event)

    # ---- access

    def one_time_link(self, display_host: str) -> str:
        return f"http://{display_host}:{self.port}/open?k={self._link_code}"

    def open_link(self, code: str) -> str | None:
        """Exchange the printed single-use code for a cookie token. Works once."""
        with self._lock:
            ok = (self._link_code and self.clock() < self._link_expires
                  and hmac.compare_digest(code.encode(), self._link_code.encode()))
            if not ok:
                return None
            self._link_code = ""              # spent
            return self._new_token()

    def login(self, secret: str) -> str | None | bool:
        """Token on success, None on a wrong secret, False when rate limited."""
        now = self.clock()
        with self._lock:
            while self._failures and self._failures[0] < now - 60:
                self._failures.popleft()
            if len(self._failures) >= LOGIN_TRIES:
                return False
            if self.secret and hmac.compare_digest(secret.encode(), self.secret.encode()):
                return self._new_token()
            self._failures.append(now)
            return None

    def _new_token(self) -> str:
        tok = secrets.token_urlsafe(32)
        now = self.clock()
        self._tokens = {t: e for t, e in self._tokens.items() if e > now}
        self._tokens[tok] = now + COOKIE_DAYS * 86400
        return tok

    def authorized(self, cookie_header: str | None, host_header: str | None) -> bool:
        if not self.auth_required:
            host = (host_header or "").rsplit(":", 1)[0].strip("[]")
            return host in LOOPBACK
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(cookie_header or "")
        except Exception:  # noqa: BLE001  a malformed cookie is just no cookie
            return False
        tok = jar[self.cookie].value if self.cookie in jar else ""
        now = self.clock()
        with self._lock:
            return any(hmac.compare_digest(tok.encode(), t.encode()) and e > now
                       for t, e in self._tokens.items())

    # ---- actions from HTTP threads, run on the watcher's thread

    def approve(self, plan_id: str, wait: float | None = None) -> dict[str, Any] | None:
        """None for an unknown, used or expired plan. Otherwise a dict with `state`, or with
        `error` when it did not run, or `running` when it is still going after `wait`."""
        if not PLAN_ID.match(plan_id):
            return None
        fut = self.watcher.submit(lambda: self._approve_view(plan_id))
        try:
            return fut.result(timeout=APPROVE_WAIT if wait is None else wait)
        except concurrent.futures.TimeoutError:
            if fut.cancel():
                return {"busy": True, "kept": True,
                        "error": "The laptop was busy and did not start it. Nothing was sent. Try again."}
            return {"running": True,
                    "error": "Still running on the laptop. Check the journal before doing anything else."}

    def _approve_view(self, plan_id: str) -> dict[str, Any] | None:
        """Runs on the watcher thread: the swap and every store read for the reply."""
        try:
            result = self.watcher.approve(plan_id)
        except ApprovalFailed as e:
            self.refresh()
            return {"error": str(e), "kept": True}
        self.refresh()
        if result is None:
            return None
        return {"state": getattr(result, "state", None), "alert": getattr(result, "alert", None),
                "reasons": getattr(result, "reasons", []),
                "held_now": [self._code(s) for s in getattr(result, "held_now", [])]}

    def handle_reply(self, verb: str, plan_id: str) -> tuple[str, str] | None:
        """A Swap now or Keep tap from the notification. Runs the same approve and skip as the page.

        Returns a push to send back, or None when the swap's own result push says it all. Only a plan
        id the watcher is holding right now is acted on, and only once: a phone that gives no feedback
        gets tapped again and again, and a stranger's made-up id must cost nothing and send nothing.
        """
        if not self.watcher.is_pending(plan_id):
            return None                  # unknown, used or expired: no push, no work for the watcher
        with self._lock:
            if plan_id in self._replied:
                return None
            self._replied.append(plan_id)
        if verb == "skip":
            if not self.skip(plan_id):
                return None
            return "Kept your seat", "The swap is dismissed. Nothing was sent."
        out = self.approve(plan_id)
        if out is None:
            return None
        if out.get("kept") or out.get("busy"):
            with self._lock:             # it did not run and the proposal waits: a later tap may retry
                if plan_id in self._replied:
                    self._replied.remove(plan_id)
        if out.get("error"):
            return "Swap not run", str(out["error"])
        return None                      # the swap event pushes done, rolled back or failed

    def skip(self, plan_id: str) -> bool:
        if not PLAN_ID.match(plan_id):
            return False

        def drop() -> bool:
            taken = self.watcher.take(plan_id) is not None
            self.refresh()
            return taken
        try:
            return bool(self.watcher.submit(drop).result(timeout=30))
        except concurrent.futures.TimeoutError:
            return False

    # ---- the snapshot, built on the watcher's thread

    def _on_event(self, ev: WatchEvent) -> None:
        if ev.kind in ("sweep", "booked", "proposed", "swap", "signin", "back", "offline"):
            self.refresh()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def refresh(self) -> None:
        now = self.clock()
        held = sorted(self.watcher.last_held)
        sessions = guard.held_sessions(self.store, self.event_id, held)
        day = self._day(sessions, now)
        blocks, warnings = guard.leave_blocks(sessions, self.rules)
        by_id = {b.session_id: b for b in blocks}
        today = [s for s in sessions if s.session_time and s.session_time.date == day]
        next_leave = None
        for s in today:
            b = by_id.get(s.session_id)
            if not b:
                continue
            leave_at = datetime.fromisoformat(b.start).replace(tzinfo=UTC).timestamp()
            if leave_at >= now - 300:
                next_leave = {"code": b.code, "leave_at": leave_at,
                              "leave_local": vegas(leave_at).strftime("%H:%M"), "how": _how(b)}
                break
        self._announce_leave(blocks, now)
        snap = {
            "now": now, "day": day,
            "status": {"online": self.watcher.down_since is None, "read_only": self.watcher.read_only,
                       "down_since": self.watcher.down_since,
                       "last_sweep": self.store.last_sweep(self.event_id)},
            "held": [self._row(s) for s in today],
            "next_leave": next_leave,
            "advice": self._advice(held, day, now),
            "proposals": [{"plan_id": p.plan_id, "target": p.target, "held": self._code(p.held_id),
                           "wanted": self._code(p.wanted_id), "expires": p.expires}
                          for p in self.watcher.pending()],
            "warnings": [w for w in warnings if w.startswith(day)],
            "journal": [{"at": vegas(r["ts"]).strftime("%a %H:%M"), "op": r["op"], "outcome": r["outcome"]}
                        for r in self.store.journal_entries(self.event_id, 10)],
        }
        try:
            snap["view"] = pages.build(self, now)
        except Exception as e:  # noqa: BLE001  a view bug must not freeze the phone keys or the watcher
            snap["view_error"] = f"{type(e).__name__}: {e}"[:200]
            self.watcher.emit("error", message=f"The web app view failed to build: {snap['view_error']}")
        with self._lock:
            self._snapshot = snap

    def _day(self, sessions: list[Session], now: float) -> str:
        """Today in Las Vegas if anything is held today, else the next day with a hold."""
        today = vegas(now).date().isoformat()
        days = sorted({s.session_time.date for s in sessions if s.session_time and s.session_time.date})
        if today in days or not days:
            return today
        later = [d for d in days if d > today]
        return later[0] if later else today

    def _row(self, s: Session) -> dict[str, Any]:
        st = s.session_time
        return {"code": s.abbreviation, "title": s.title, "start": st.time if st else None,
                "minutes": st.minutes if st else None, "venue": s.campus_venue, "room": s.room_label,
                "band": s.seat_availability}

    def _code(self, sid: str) -> str:
        s = self.store.get(self.event_id, sid)
        return f"{s.abbreviation} {s.title}" if s else "a session not in the local catalog"

    def _advice(self, held: list[str], day: str, now: float) -> dict[str, Any] | None:
        """Queue-or-go for the next wanted session that day that is not held."""
        held_codes = {s.base_code for s in (self.store.get(self.event_id, i) for i in held) if s}
        held_set = set(held)
        best: tuple[float, Session] | None = None
        for t in self.rules.targets:
            tree = self.watcher.router.tree(t)
            if any(s.session_id in held_set for s, _ in tree):
                continue                       # this target is already held
            for s, _ in tree:
                st = s.session_time
                if (s.session_id in held or s.base_code in held_codes or not st or st.date != day
                        or not st.time):
                    continue
                start = datetime.fromisoformat(f"{st.date}T{st.time}").replace(tzinfo=VEGAS).timestamp()
                if start > now and (best is None or start < best[0]):
                    best = (start, s)
        if not best:
            return None
        s = best[1]
        a = guard.queue_or_go(s, self.store.band_history(self.event_id, s.session_id))
        return {**self._row(s), "verdict": a.verdict, "basis": a.basis}

    def _announce_leave(self, blocks: list[guard.Block], now: float) -> None:
        """Once per block, from its leave time until the session starts."""
        for b in blocks:
            leave_at = datetime.fromisoformat(b.start).replace(tzinfo=UTC).timestamp()
            s = self.store.get(self.event_id, b.session_id)
            starts = guard._window(s)[0].timestamp() if s and guard._window(s) else leave_at + 300
            key = f"{b.session_id}@{b.start}"
            if leave_at <= now < starts and key not in self._leave_sent:
                self._leave_sent.add(key)
                self.watcher.emit("leave", code=b.code, message=_how(b))

    def onsite_day(self) -> str | None:
        today = vegas(self.clock()).date().isoformat()
        return today if today in EVENT_DAYS.values() else None


def _how(b: guard.Block) -> str:
    return b.description.removeprefix(guard.PREFIX).removesuffix(guard.TAG).strip()


# ---------------------------------------------------------------------- HTTP


STATIC = {"app.css": "text/css", "app.js": "text/javascript",
          "manifest.webmanifest": "application/manifest+json",
          "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}


def static(name: str) -> bytes:
    return resources.files("reseat").joinpath("static", name).read_bytes()


def page(app: App, path: str) -> bytes:
    snap = app.snapshot()
    if not snap.get("view"):
        return pages.render_starting(app.clock(), app.demo, snap.get("view_error")).encode()
    snap["now"] = app.clock()        # the countdowns start from the laptop's clock now, not the snapshot's
    if path == "/approve":
        return pages.render_approve(snap, app.demo, push=bool(app.rules.ntfy_topic)).encode()
    if path == "/today":
        return pages.render_today(snap, app.demo).encode()
    return pages.render_dashboard(snap, app.demo).encode()


LOGIN_PAGE = b"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>re:Seat</title><style>body{font:17px system-ui;margin:2rem;max-width:30rem}
input,button{font:inherit;padding:.6rem;width:100%;margin:.4rem 0}</style>
<h1>re:Seat</h1><p>Enter the serve_secret from your rules file.</p>
<form method=post action=/login><input type=password name=secret autocomplete=current-password
autofocus required><button>Open</button></form>"""


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "reseat"
        sys_version = ""
        timeout = 10              # a slow or silent client cannot hold a thread

        def log_message(self, *_: object) -> None:   # never log paths: the one-time code is in one
            pass

        def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            text = not ctype.startswith("image/")
            self.send_header("Content-Type", f"{ctype}; charset=utf-8" if text else ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            # Scripts only from /static/app.js. Inline style attributes place the week grid's blocks.
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
                             "manifest-src 'self'; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode())

        def _cookie(self, tok: str) -> dict[str, str]:
            return {"Set-Cookie": f"{app.cookie}={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                  f"Max-Age={COOKIE_DAYS * 86400}", "Location": "/"}

        def _authorized(self) -> bool:
            return app.authorized(self.headers.get("Cookie"), self.headers.get("Host"))

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if url.path == "/open" and app.auth_required:
                code = parse_qs(url.query).get("k", [""])[0]
                tok = app.open_link(code)
                if tok:
                    self._send(303, headers=self._cookie(tok))
                else:
                    self._send(303, headers={"Location": "/login"})
                return
            if url.path == "/login" and app.auth_required:
                self._send(200, LOGIN_PAGE, "text/html")
                return
            name = url.path.removeprefix("/static/")
            if url.path.startswith("/static/") and name in STATIC:    # no data in these: no sign-in needed
                self._send(200, static(name), STATIC[name])
                return
            if not self._authorized():
                if url.path in VIEWS and app.auth_required:
                    self._send(303, headers={"Location": "/login"})
                else:
                    self._json(401 if app.auth_required else 403, {"error": "not allowed"})
                return
            if url.path in VIEWS:
                self._send(200, page(app, url.path), "text/html")
            elif url.path == "/api/state":
                self._json(200, app.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._json(400, {"error": "bad Content-Length"})
                return
            if length > MAX_BODY:
                self._json(413, {"error": "too large"})
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                self._json(403, {"error": "cross-site request"})
                return
            body = self.rfile.read(length) if length else b""
            if url.path == "/login" and app.auth_required:
                secret = parse_qs(body.decode(errors="replace")).get("secret", [""])[0]
                tok = app.login(secret)
                if tok is False:
                    self._send(429, b"Too many tries. Wait a minute.", "text/plain")
                elif tok is None:
                    self._send(401, b"Wrong secret.", "text/plain")
                else:
                    self._send(303, headers=self._cookie(str(tok)))
                return
            if not self._authorized() or self.headers.get("X-Reseat") != "1":
                self._json(403, {"error": "not allowed"})
                return
            parts = [p for p in url.path.split("/") if p]
            if len(parts) == 2 and parts[0] == "approve":
                out = app.approve(parts[1])
                if out is None:
                    self._json(404, {"error": "unknown, used or expired plan"})
                elif out.get("running"):
                    self._json(202, out)
                elif out.get("busy"):
                    self._json(503, out)
                elif "error" in out:
                    self._json(409, out)
                else:
                    self._json(200, out)
            elif len(parts) == 2 and parts[0] == "demo" and app.demo and app.run_scenario:
                if parts[1] not in {s["key"] for s in app.scenarios} | {"tour"}:
                    self._json(404, {"error": "no such scenario"})
                    return
                try:
                    self._json(200, {"message": app.run_scenario(parts[1])})
                except Exception as e:  # noqa: BLE001  a demo scenario must never take the server down
                    self._json(500, {"error": f"The scenario did not finish: {type(e).__name__}"})
            elif len(parts) == 2 and parts[0] == "skip":
                ok = app.skip(parts[1])
                self._json(200, {"skipped": True}) if ok else self._json(404, {"error": "unknown plan"})
            else:
                self._json(404, {"error": "not found"})

    return Handler


DROPPED = (ConnectionError, TimeoutError)   # reset, broken pipe, aborted, timed out
ENOTCONN = {57, 107}                        # macOS and Linux: "socket is not connected"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that hangs up early, or a socket the OS cut off, is not a server fault: no traceback.
        Browsers pre-connect and drop sockets all the time."""
        e = sys.exc_info()[1]
        if isinstance(e, DROPPED) or (isinstance(e, OSError) and e.errno in ENOTCONN):
            return
        super().handle_error(request, client_address)


def self_check(host: str, port: int, timeout: float = 3.0) -> bool:
    """Can this machine reach its own server on `host`? Fetches the stylesheet, which needs no sign-in.
    On macOS a terminal without Local Network permission accepts the connection and then loses it."""
    try:
        return httpx.get(f"http://{host}:{port}/static/app.css", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


LOCAL_NETWORK_HINT = (
    "Devices cannot reach this address. With Tailscale, use reseat serve --tailscale instead of --host: "
    "it listens on 127.0.0.1 and Tailscale forwards to it, so macOS's local network rules never apply. "
    "Otherwise, on macOS, allow your terminal app under System Settings > Privacy & Security > "
    "Local Network, quit and reopen it, and start reseat serve again.")


def make_server(app: App) -> ThreadingHTTPServer:
    server = Server((app.host, app.port), make_handler(app))
    app.port = server.server_address[1]
    return server


def run(app: App, server: ThreadingHTTPServer) -> threading.Thread:
    """Start the watcher on its own thread, plus a 20-second snapshot refresh. The caller then
    runs server.serve_forever() and, on the way out, stop() and join()s the returned thread
    so a swap in progress finishes before the process exits."""
    app.watcher.submit(app.refresh)

    def refresher() -> None:
        while not app.watcher.stopped.wait(REFRESH_EVERY):
            app.watcher.submit(app.refresh)

    threading.Thread(target=refresher, name="reseat-refresh", daemon=True).start()
    t = threading.Thread(target=app.watcher.run, kwargs={"onsite_day": app.onsite_day},
                         name="reseat-watcher", daemon=False)
    t.start()
    return t
