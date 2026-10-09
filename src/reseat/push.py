"""Push to the phone through ntfy. Off unless the rules file sets `ntfy_topic`.

What leaves the laptop, and nothing else: the event type, session codes and
titles, and plain status lines such as "re:Seat offline since 14:05". Never a
token, an abstract, a session id or a plan id. The topic name is the only
secret, so it must be a long random string.
"""

from __future__ import annotations

import queue
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
    if ev.kind in ("offline", "back"):
        return d["message"], d["message"]
    if ev.kind == "signin" and d.get("state") == "needed":
        return "Sign in needed", "Run reseat login on the laptop. Nothing is booked until then."
    if ev.kind == "writes":
        return ("Booking paused" if d.get("state") == "closed" else "Booking resumed"), d["message"]
    if ev.kind == "leave":
        return f"Leave now for {d['code']}", d["message"]
    return None


class Pusher:
    """Sends on a background thread so a slow network never holds up the watcher."""

    def __init__(self, topic: str, transport: httpx.BaseTransport | None = None,
                 code_title: Callable[[str], str] = str):
        self.url = f"{NTFY}/{topic}"
        self.code_title = code_title
        self.http = httpx.Client(timeout=10, transport=transport)
        self.sent: list[tuple[str, str]] = []
        self.failures = 0
        self._q: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None

    def __call__(self, ev: WatchEvent) -> None:
        msg = message(ev, self.code_title)
        if msg:
            try:
                self._q.put_nowait(msg)
            except queue.Full:
                self.failures += 1

    def send(self, title: str, body: str) -> bool:
        """One plain-text POST. No auth header, nothing from the keychain."""
        safe_title = title.encode("ascii", "replace").decode()   # HTTP headers are ASCII
        try:
            r = self.http.post(self.url, content=body.encode(), headers={"Title": safe_title, "Tags": "seat"})
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
