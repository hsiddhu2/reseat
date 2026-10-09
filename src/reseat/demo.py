"""Demo mode: `reseat serve --demo` runs the web app on a fake Events API.

The real watcher, router, swap and guard code run against FakeEventsApi, an
in-process fake of the API, with a week of real sessions from the 1 October 2026
catalog. A short script then changes the fake catalog, so the page shows each
thing re:Seat reacts to within two minutes:

    about 30 s   a seat opens in a talk that clashes with a lower-priority hold: a swap proposal
    about 60 s   a new sitting of a wanted talk appears: booked at once, read back
    about 90 s   a held session changes room: a moved note

Approving the swap runs swap.py against the fake, so the journal shows real state
transitions. The clock reads Monday 30 November 2026, 10:15 in Las Vegas.

Rules this module exists to respect:
- Never reads the keychain, never touches ~/.reseat or the rules file. The store is
  in memory and the rules come from this file. Nothing reaches the network.
- Every page says "Demo data".
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime
from importlib import resources
from typing import Any

from . import rules as R
from .campus import VEGAS
from .client import EventsClient
from .fakeapi import FakeEventsApi
from .fixtures import load_catalog
from .models import Session
from .serve import LOOPBACK, App
from .store import Store
from .swap import Swap
from .watcher import MIN_INTERVAL, Watcher

EVENT = "reinvent2026"
START = datetime(2026, 11, 30, 10, 15, tzinfo=VEGAS).timestamp()
INTERVAL = MIN_INTERVAL
DEFAULT_PORT = 8491          # not the real page's 8490
COOKIE = "reseat_demo"       # cookies are not scoped by port: a demo sign-in must not replace the real one
HELD = ("ARC302-R", "CMP409-R", "DVT201", "API319", "COP335", "COM402", "CMP303", "SVS402-R", "COP301",
        "SEC351")
BANDS = {"SVS306-R": "unavailable", "SVS306-R1": "unavailable", "ANT335": "unavailable",
         "AIM310": "unavailable", "CMP409-R1": "available", "ARC302-R1": "limited",
         "SVS402-R1": "veryLimited"}
TARGETS = ("SVS306", "ARC302", "CMP409", "DVT201", "API319", "COP335", "ANT335", "COM402", "CMP303",
           "SVS402", "COP301", "SEC351", "AIM310")
NEW_SITTING_ID = "demo-AIM310-R1"
NEW_ROOM = "Level 3 | Premier 319"
STEPS = (25.0, 55.0, 85.0)        # seconds after start: each lands before the next 30-second sweep


def catalog() -> list[Session]:
    """The bundled slice of the real 1 October catalog, reservable, with demo bands."""
    path = resources.files("reseat").joinpath("demo_catalog.json")
    out = []
    for s in load_catalog(str(path)):
        band = BANDS.get(s.abbreviation or "", "available")
        out.append(s.model_copy(update={"is_reservable": True, "seat_availability": band}))
    return out


def rules_text(secret: str | None) -> str:
    head = f"serve_secret: {secret}\n" if secret else ""
    return head + "home_venue: The Venetian\ntargets:\n" + "".join(f"  - code: {c}\n" for c in TARGETS)


class Demo:
    """The fake API, the store, the watcher and the web app, wired as `reseat serve` wires them."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 started: float | None = None, monotonic: Callable[[], float] = time.monotonic):
        self.offset = START - (started if started is not None else time.time())
        self.secret = None if host in LOOPBACK else secrets.token_urlsafe(24)
        self.fake = FakeEventsApi(sessions=catalog())
        by_code = {s.abbreviation: s.session_id for s in self.fake.sessions.values()}
        self.fake.schedule.reserved.update(by_code[c] for c in HELD)
        self.fake.schedule.personal_time["demo-dinner"] = {
            "personalTimeId": "demo-dinner", "startDateTime": "2026-12-01T01:30:00",
            "endDateTime": "2026-12-01T03:00:00", "title": "Team dinner", "description": "Booked by hand",
            "location": "Wynn"}
        self.client = EventsClient(token_provider=lambda: self.fake.token, transport=self.fake.transport())
        self.store = Store(":memory:", clock=self.clock)       # journal times on the demo clock
        self.rules = R.parse(rules_text(self.secret))
        self.watcher = Watcher(self.client, self.store, self.rules, EVENT, interval=INTERVAL,
                               clock=self.clock, swapper=self._swap)
        self.app = App(self.watcher, self.store, self.rules, EVENT, host=host, port=port, clock=self.clock,
                       demo=True, cookie=COOKIE)
        self._codes = by_code
        self._monotonic = monotonic
        self.done: list[str] = []

    def clock(self) -> float:
        return time.time() + self.offset

    def _swap(self, p: Any, approved: bool) -> Any:
        return Swap(self.client, self.store, self.rules, EVENT).run_plan(p, approved=approved)

    # ---- the script, run on the watcher's thread so the fake and the store have one user

    def seat_opens(self) -> None:
        self.fake.set_band(self._codes["SVS306-R"], "limited")
        self.done.append("seat")

    def new_sitting(self) -> None:
        base = self.fake.sessions[self._codes["AIM310"]]
        self.fake.add_session(base.model_copy(update={
            "session_id": NEW_SITTING_ID, "abbreviation": "AIM310-R1", "seat_availability": "available",
            "session_time": base.session_time.model_copy(update={"date": "2026-12-03", "time": "15:00"})
            if base.session_time else None}))
        self.done.append("sitting")

    def room_moves(self) -> None:
        sid = self._codes["CMP303"]
        self.fake.sessions[sid] = self.fake.sessions[sid].model_copy(update={"room": NEW_ROOM})
        self.done.append("moved")

    def start_script(self) -> threading.Thread:
        """Hand each change to the watcher's thread at its time. Stops with the watcher."""
        steps = list(zip(STEPS, (self.seat_opens, self.new_sitting, self.room_moves), strict=True))
        t0 = self._monotonic()

        def run() -> None:
            for at, fn in steps:
                if self.watcher.stopped.wait(max(0.0, at - (self._monotonic() - t0))):
                    return
                self.watcher.submit(fn)

        t = threading.Thread(target=run, name="reseat-demo-script", daemon=True)
        t.start()
        return t
