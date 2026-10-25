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
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import httpx

from .campus import VEGAS
from .watcher import WatchEvent

NTFY = "https://ntfy.sh"


def message(ev: WatchEvent, code_title: Callable[[str], str]) -> tuple[str, str] | None:
    """(title, body) for an event worth a push, or None. `code_title` turns a session id
    into "CODE Title" so ids never leave the laptop."""
    d = ev.data
    if ev.kind == "booked":
        return "Seat booked", f"{d.get('code') or ''} {d.get('title') or ''}".strip()
    if ev.kind == "offer":
        what = "New session" if d.get("reason") == "added" else "Seat opened on a favorite"
        band = {"available": "seats available", "limited": "limited seats",
                "veryLimited": "very few seats"}.get(d.get("band") or "", "seats open")
        return (f"{what}: {d.get('code') or ''}".strip(),
                f"{code_title(d['session_id'])}, {band}. Book it from the web app. re:Seat checks the seat "
                "and your day first.")
    if ev.kind == "rules":
        return "Rules changed", d["message"]
    if ev.kind == "new_sessions":
        return f"{d['count']} new sessions", f"{d['message']} Open the Activity page to see them."
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
    if ev.kind == "expiring":
        return ("Swap offer expires in 2 minutes",
                f"{code_title(d['wanted_id'])} instead of {code_title(d['held_id'])}. "
                "Tap Swap now or Keep, or it lapses and your seat stays as it is.")
    if ev.kind == "other_sitting":
        where = f", {d['venue']}" if d.get("venue") else ""
        return (f"{d.get('code') or 'Your sitting'} is full. {d.get('other')} has seats",
                f"{d.get('other')} {d.get('at')}{where}. Your rules allow only the sitting you picked, so "
                "re:Seat will not book it. Add it to the target, or set repeats: true.")
    if ev.kind == "tight":
        return f"A walk you cannot make in time, {d['day']}", d["message"]
    if ev.kind == "blocks":
        parts = [f"{n} {w}" for n, w in ((d["create"], "to add"), (d["update"], "to change"),
                                          (d["delete"], "to remove")) if n]
        return ("Leave-now blocks out of date",
                f"Your schedule changed. {', '.join(parts)}. Tap Update to rewrite them.")
    if ev.kind == "blocks_done":
        return ("Leave-now blocks updated" if d.get("ok") else "Leave-now blocks not updated"), d["message"]
    if ev.kind == "leave_soon":
        return f"Leave in 10 minutes for {d['code']}", f"Leave at {d['at']}. {d['message']}"
    if ev.kind == "briefing":
        return d["title"], d["message"]
    if ev.kind == "wanted_soon":
        where = f", {d['venue']}" if d.get("venue") else ""
        return (f"{d.get('code') or 'A session you want'} starts at {d['at']}",
                f"{d.get('title') or ''}{where}. You do not hold a seat. "
                f"{WALK_UP.get(d.get('verdict') or '', '')} {d.get('basis') or ''}".strip())
    return None


WALK_UP = {"likely": "Walk-up likely.", "early": "Go early.", "unlikely": "Walk-up unlikely.",
           "unknown": "No call on walk-ups."}
PAGE = {"proposed": "/approve", "expiring": "/approve", "offer": "/approve", "new_sessions": "/activity",
        "blocks": "/today", "leave": "/today",
        "leave_soon": "/today", "briefing": "/today", "wanted_soon": "/today"}   # others open the Week


CATEGORY = {
    "proposed": "swaps", "expiring": "swaps", "swap": "swaps",
    "booked": "bookings", "not_booked": "bookings", "unconfirmed": "bookings", "other_sitting": "bookings",
    "catalog": "bookings", "offer": "bookings", "new_sessions": "bookings", "rules": "health",
    "moved": "schedule", "removed": "schedule", "tight": "schedule", "blocks": "schedule",
    "blocks_done": "schedule",
    "briefing": "on_site", "leave_soon": "on_site", "leave": "on_site", "wanted_soon": "on_site",
    "offline": "health", "back": "health", "signin": "health", "writes": "health",
}


def urgent(ev: WatchEvent) -> bool:
    """Always sent, through quiet hours and a muted category: the attendee must act or check now.
    blocks_done answers a tap the attendee just made."""
    if ev.kind in ("signin", "blocks_done"):
        return True
    return ev.kind == "swap" and ev.data.get("state") not in ("verified", "rolled_back")


def in_quiet(quiet: str | None, ts: float) -> bool:
    """Quiet hours as "HH:MM-HH:MM" in Las Vegas time. The window may run past midnight."""
    if not quiet:
        return False
    a, b = quiet.split("-")
    now = datetime.fromtimestamp(ts, VEGAS).strftime("%H:%M")
    return a <= now < b if a < b else (now >= a or now < b)


def page_for(ev: WatchEvent) -> str:
    """The web app page a tap on this push should open."""
    return PAGE.get(ev.kind, "/")


CONTROL = re.compile(r"[\x00-\x1f\x7f]")    # a catalog title never splits a header
COMMAND = re.compile(r"^(approve|skip|update) ([A-Za-z0-9_-]{16,64})$")


def reply_topic(topic: str) -> str:
    """The topic the phone's buttons post to. Derived from the push topic, so it is as hard to guess."""
    return "rs-" + hmac.new(topic.encode(), b"reseat reply topic", hashlib.sha256).hexdigest()[:32]


def actions(topic: str, plan_id: str) -> str:
    """ntfy action buttons for a swap proposal. Plan ids hold only [A-Za-z0-9_-], so the header is safe."""
    url = f"{NTFY}/{reply_topic(topic)}"
    return (f"http, Swap now, {url}, method=POST, body=approve {plan_id}, clear=true; "
            f"http, Keep, {url}, method=POST, body=skip {plan_id}, clear=true")


def book_actions(topic: str, plan_id: str) -> str:
    """ntfy action buttons for a Book offer. The same reply topic and verbs as a swap."""
    url = f"{NTFY}/{reply_topic(topic)}"
    return (f"http, Book, {url}, method=POST, body=approve {plan_id}, clear=true; "
            f"http, Dismiss, {url}, method=POST, body=skip {plan_id}, clear=true")


def update_action(topic: str, token: str) -> str:
    """One Update button for out-of-date leave-now blocks. The token is single use, like a plan id."""
    return f"http, Update, {NTFY}/{reply_topic(topic)}, method=POST, body=update {token}, clear=true"


class Pusher:
    """Sends on a background thread so a slow network never holds up the watcher."""

    def __init__(self, topic: str, transport: httpx.BaseTransport | None = None,
                 code_title: Callable[[str], str] = str, click_base: str | None = None,
                 approve: bool = False, quiet: str | None = None, mute: Iterable[str] = (),
                 book: bool = False,
                 clock: Callable[[], float] = time.time):
        self.url, self.topic, self.approve, self.book = f"{NTFY}/{topic}", topic, approve, book
        self.quiet, self.mute, self.clock = quiet, set(mute), clock
        self._held: list[str] = []         # titles that waited out quiet hours
        self._held_lock = threading.Lock()  # events come from the watcher, HTTP and reply threads
        self.click_base = click_base       # e.g. http://100.64.0.7:8490, or None on loopback
        self.code_title = code_title
        self.http = httpx.Client(timeout=10, transport=transport)
        self.sent: list[tuple[str, str]] = []
        self.failures = 0
        self._q: queue.Queue[tuple[str, str, str, str | None] | None] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None

    def __call__(self, ev: WatchEvent) -> None:
        quiet = in_quiet(self.quiet, self.clock())
        if not quiet:
            self._digest()
        msg = message(ev, self.code_title)
        if msg and not urgent(ev):
            if CATEGORY.get(ev.kind) in self.mute:
                return
            if quiet:
                what = msg[1].split(". ")[0].strip(" .")[:60]
                with self._held_lock:
                    self._held.append(f"{msg[0]}: {what}" if what and what != msg[0] else msg[0])
                return
        if msg:
            buttons = None
            if (self.approve and ev.kind in ("proposed", "expiring") and not ev.data.get("auto")
                    and ev.data.get("plan_id")):
                buttons = actions(self.topic, ev.data["plan_id"])
                tap = "Long-press for Swap now or Keep, or approve it in the web app."
                msg = (msg[0], msg[1].replace("Approve it in the web app.", tap))
            if self.book and ev.kind == "offer" and ev.data.get("plan_id"):
                buttons = book_actions(self.topic, ev.data["plan_id"])
                msg = (msg[0], msg[1].replace("Book it from the web app.",
                                              "Long-press for Book, or book it in the web app."))
            if self.approve and ev.kind == "blocks" and ev.data.get("token"):
                buttons = update_action(self.topic, ev.data["token"])
            try:
                self._q.put_nowait((*msg, page_for(ev), buttons))
            except queue.Full:
                self.failures += 1

    def _digest(self) -> None:
        """Quiet hours are over: one push listing what waited, newest last, then the Activity page."""
        with self._held_lock:
            held, self._held = self._held, []
        if not held:
            return
        titles = list(dict.fromkeys(held))
        body = "; ".join(titles[:8]) + (f"; and {len(titles) - 8} more" if len(titles) > 8 else "") + "."
        try:
            self._q.put_nowait((f"While it was quiet: {len(held)} update{'s' if len(held) > 1 else ''}",
                                body, "/activity", None))
        except queue.Full:
            self.failures += 1

    def send(self, title: str, body: str, page: str = "/", buttons: str | None = None) -> bool:
        """One plain-text POST. No auth header, nothing from the keychain. A tap opens `page`."""
        clean = CONTROL.sub(" ", title).encode("ascii", "replace").decode()      # headers are ASCII
        headers = {"Title": clean, "Tags": "seat"}
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
        self._digest()           # re:Seat is stopping: what waited out quiet hours goes now, not never
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
