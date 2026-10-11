"""Push to the phone through ntfy. Off unless the rules file sets `ntfy_topic`.

What leaves the laptop, and nothing else: the event type, session codes and
titles, and plain status lines such as "re:Seat offline since 14:05". When the
web app listens on an address the phone can reach (not loopback), each message
also carries a link to the page to open, such as http://100.x.y.z:8490/approve.
That address only works inside the attendee's own Tailscale network. Never a
token, an abstract or a session id. The topic name is the only secret, so it
must be a long random string.

Approve from the notification, opt in with `ntfy_approve: true`. A swap proposal
push carries Swap now and Keep buttons. A tap posts "approve <plan id>" or
"skip <plan id>" to a second topic derived from the first, and ReplyListener on
the laptop reads it over an outgoing connection. Nothing connects to the laptop.
The plan id is random, works once and expires after 10 minutes, and approving it
runs the same checked swap as the web app. Repeated taps run it once. Anyone who
knows the push topic can work out the reply topic, so with this on, the topic
name gives control of proposals, not just a view of them. Keep it secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx

from .watcher import WatchEvent

NTFY = "https://ntfy.sh"


def message(ev: WatchEvent, code_title: Callable[[str], str]) -> tuple[str, str] | None:
    """(title, body) for an event worth a push, or None. `code_title` turns a session id
    into "CODE Title" so ids never leave the laptop."""
    d = ev.data
    if ev.kind == "booked":
        return "Seat booked", f"{d.get('code') or ''} {d.get('title') or ''}".strip()
    if ev.kind == "proposed":
        then = "Running it now (auto)." if d.get("auto") else "Approve it in the web app."
        return "Swap proposed", f"{code_title(d['wanted_id'])} instead of {code_title(d['held_id'])}. {then}"
    if ev.kind == "swap":
        state = d.get("state")
        if state == "verified":
            return "Swap done", "The new seat is held."
        if state == "rolled_back":
            return "Swap rolled back", "The new seat was not available. Your original seat is held again."
        if state == "failed":
            return "Swap failed", "Check the web app for what is held now."
        return None
    if ev.kind == "back" and d.get("down") == "catalog":
        return None                        # the catalog event says it better
    if ev.kind in ("offline", "back"):
        return d["message"], d["message"]
    if ev.kind == "catalog":
        return "The re:Invent catalog is back", d["message"]
    if ev.kind == "moved":
        b, a = d.get("before") or {}, d.get("after") or {}
        change = ", ".join(f"{b.get(k) or 'none'} to {a.get(k) or 'none'}"
                           for k in ("date", "time", "room", "venue") if b.get(k) != a.get(k))
        return f"{d.get('code') or 'A session you hold'} moved", change or "Details changed."
    if ev.kind == "removed":
        return f"{d.get('code') or 'A session you hold'} left the catalog", d["message"]
    if ev.kind == "unconfirmed":
        return "Check your schedule", f"{d.get('code') or 'A session'} {d.get('title') or ''}: {d['message']}"
    if ev.kind == "not_booked":
        return (f"{'New sitting' if d.get('new') else 'Seat opened'}: {d.get('code')}",
                f"Not booked: {d['reason']}.")
    if ev.kind == "signin" and d.get("state") == "needed":
        return "Sign in needed", "Run reseat login on the laptop. Nothing is booked until then."
    if ev.kind == "writes":
        return ("Booking paused" if d.get("state") == "closed" else "Booking resumed"), d["message"]
    if ev.kind == "leave":
        return f"Leave now for {d['code']}", d["message"]
    return None


PAGE = {"proposed": "/approve", "leave": "/today"}       # every other event opens the dashboard


def page_for(ev: WatchEvent) -> str:
    """The web app page a tap on this push should open."""
    return PAGE.get(ev.kind, "/")


COMMAND = re.compile(r"^(approve|skip) ([A-Za-z0-9_-]{16,64})$")


def reply_topic(topic: str) -> str:
    """The topic the phone's buttons post to. Derived from the push topic, so it is as hard to guess."""
    return "rs-" + hmac.new(topic.encode(), b"reseat reply topic", hashlib.sha256).hexdigest()[:32]


def actions(topic: str, plan_id: str) -> str:
    """ntfy action buttons for a swap proposal. Plan ids hold only [A-Za-z0-9_-], so the header is safe."""
    url = f"{NTFY}/{reply_topic(topic)}"
    return (f"http, Swap now, {url}, method=POST, body=approve {plan_id}, clear=true; "
            f"http, Keep, {url}, method=POST, body=skip {plan_id}, clear=true")


class Pusher:
    """Sends on a background thread so a slow network never holds up the watcher."""

    def __init__(self, topic: str, transport: httpx.BaseTransport | None = None,
                 code_title: Callable[[str], str] = str, click_base: str | None = None,
                 approve: bool = False):
        self.url, self.topic, self.approve = f"{NTFY}/{topic}", topic, approve
        self.click_base = click_base       # e.g. http://100.64.0.7:8490, or None on loopback
        self.code_title = code_title
        self.http = httpx.Client(timeout=10, transport=transport)
        self.sent: list[tuple[str, str]] = []
        self.failures = 0
        self._q: queue.Queue[tuple[str, str, str, str | None] | None] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None

    def __call__(self, ev: WatchEvent) -> None:
        msg = message(ev, self.code_title)
        if msg:
            buttons = None
            if self.approve and ev.kind == "proposed" and not ev.data.get("auto") and ev.data.get("plan_id"):
                buttons = actions(self.topic, ev.data["plan_id"])
            try:
                self._q.put_nowait((*msg, page_for(ev), buttons))
            except queue.Full:
                self.failures += 1

    def send(self, title: str, body: str, page: str = "/", buttons: str | None = None) -> bool:
        """One plain-text POST. No auth header, nothing from the keychain. A tap opens `page`."""
        headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "seat"}   # headers are ASCII
        if self.click_base:
            headers["Click"] = self.click_base + page
        if buttons:
            headers["Actions"] = buttons
            headers["Priority"] = "high"
        try:
            r = self.http.post(self.url, content=body.encode(), headers=headers)
            ok = r.status_code < 300
        except Exception:  # noqa: BLE001  push is best effort and must never kill its thread
            ok = False
        if ok:
            self.sent.append((title, body))
        else:
            self.failures += 1
        return ok

    def flush(self) -> None:
        """Send everything queued, on the caller's thread. For tests and shutdown."""
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if item:
                self.send(*item)

    def start(self) -> None:
        def loop() -> None:
            while True:
                item = self._q.get()
                if item is None:
                    return
                self.send(*item)
        self._thread = threading.Thread(target=loop, name="reseat-push", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:       # stuck on a dead network: the daemon thread ends with the process
            pass

    def info(self) -> dict[str, Any]:
        return {"sent": len(self.sent), "failures": self.failures}


class ReplyListener:
    """Reads Swap now and Keep taps from the reply topic over one outgoing streaming connection.

    Only "approve <plan id>" and "skip <plan id>" are acted on. Anything else is ignored. A message
    older than when the listener started is never acted on, and after a reconnect it resumes from
    the last message id it saw, so a tap is handled at most once by this process.
    """

    def __init__(self, topic: str, handle: Callable[[str, str], None],
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] | None = None):
        import time
        self.url = f"{NTFY}/{reply_topic(topic)}/json"
        self.handle = handle
        self.http = httpx.Client(timeout=httpx.Timeout(10, read=90), transport=transport)
        self.since: str = str(int((clock or time.time)()))
        self.stopped = threading.Event()
        self.handled = 0
        self.errors = 0
        self._thread: threading.Thread | None = None

    def feed(self, line: str) -> None:
        """One line of ntfy's JSON stream."""
        try:
            m = json.loads(line)
        except ValueError:
            return
        if not isinstance(m, dict) or m.get("event") != "message":
            return
        if m.get("id"):
            self.since = str(m["id"])
        cmd = COMMAND.match(str(m.get("message") or "").strip())
        if cmd:
            self.handled += 1
            try:
                self.handle(cmd.group(1), cmd.group(2))
            except Exception:  # noqa: BLE001  one bad approval must not end the listener
                self.errors += 1

    def listen_once(self) -> None:
        with self.http.stream("GET", self.url, params={"since": self.since}) as r:
            if r.status_code >= 300:
                raise httpx.HTTPStatusError("ntfy refused", request=r.request, response=r)
            for line in r.iter_lines():
                if self.stopped.is_set():
                    return
                self.feed(line)

    def start(self) -> None:
        def loop() -> None:
            wait = 2.0
            while not self.stopped.is_set():
                try:
                    self.listen_once()
                    wait = 2.0
                except Exception:  # noqa: BLE001  network trouble: wait and reconnect
                    self.errors += 1
                    wait = min(wait * 2, 60.0)
                self.stopped.wait(wait)
        self._thread = threading.Thread(target=loop, name="reseat-replies", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped.set()
        try:
            self.http.close()
        except Exception:  # noqa: BLE001
            pass
