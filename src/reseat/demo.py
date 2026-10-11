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
- Push is off unless a topic is given (`--push`). Then the same pusher as the real
  server posts demo session codes and titles to ntfy.sh/<topic>, nothing else.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from . import guard, push
from . import rules as R
from .campus import VEGAS
from .client import EventsClient
from .fakeapi import FakeEventsApi
from .fixtures import load_catalog
from .models import Session
from .serve import LOOPBACK, App
from .store import Store
from .swap import Swap
from .watcher import MIN_INTERVAL, ApprovalFailed, Watcher

EVENT = "reinvent2026"
START = datetime(2026, 11, 30, 10, 15, tzinfo=VEGAS).timestamp()
INTERVAL = MIN_INTERVAL
DEFAULT_PORT = 8491          # not the real page's 8490
COOKIE = "reseat_demo"       # cookies are not scoped by port: a demo sign-in must not replace the real one
HELD = ("ARC302-R", "CMP409-R", "DVT201", "API319", "COP335", "COM402", "CMP303", "SVS402-R", "COP301",
        "SEC351")
BANDS = {"SVS306-R": "unavailable", "SVS306-R1": "unavailable", "ANT335": "unavailable",
         "AIM310": "unavailable", "CMP409-R1": "available", "ARC302-R1": "limited",
         "SVS402-R1": "veryLimited", "IND424": "unavailable", "ARC306": "unavailable",
         "BIZ108": "unavailable"}
TARGETS = ("SVS306", "ARC302", "CMP409", "DVT201", "API319", "COP335", "ANT335", "COM402", "CMP303",
           "SVS402", "COP301", "SEC351", "AIM310", "IND424", "ARC306", "BIZ108")
SEATS, SCHEDULE, AWS = "Seats", "Your schedule", "AWS and the network"
TOUR = ("seat", "fill", "repeat", "close", "open")   # the guided tour, one scenario per press
PROBE = "demo-probe-never-held"   # a session id nobody can hold: the watcher asks it whether writes are open
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


TOPIC = re.compile(r"[A-Za-z0-9_-]{16,64}")


def rules_text(secret: str | None, push_topic: str | None = None) -> str:
    """Checked before it goes into the YAML, so a topic can never add or hide a setting."""
    if push_topic is not None and not TOPIC.fullmatch(push_topic):
        raise R.RulesError("The push topic must be 16 to 64 letters, digits, - or _.")
    head = f"serve_secret: {secret}\n" if secret else ""
    head += f"ntfy_topic: {push_topic}\nntfy_approve: true\n" if push_topic else ""
    return (head + f"home_venue: The Venetian\nprobe_session: {PROBE}\ntargets:\n"
            + "".join(f"  - code: {c}\n" for c in TARGETS))


class Demo:
    """The fake API, the store, the watcher and the web app, wired as `reseat serve` wires them."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 started: float | None = None, monotonic: Callable[[], float] = time.monotonic,
                 push_topic: str | None = None, require_secret: bool = False):
        self.offset = START - (started if started is not None else time.time())
        self.secret = secrets.token_urlsafe(24) if require_secret or host not in LOOPBACK else None
        self.fake = FakeEventsApi(sessions=catalog())
        by_code = {s.abbreviation: s.session_id for s in self.fake.sessions.values()}
        self.fake.schedule.reserved.update(by_code[c] for c in HELD)
        self.fake.schedule.personal_time["demo-dinner"] = {
            "personalTimeId": "demo-dinner", "startDateTime": "2026-12-01T01:30:00",
            "endDateTime": "2026-12-01T03:00:00", "title": "Team dinner", "description": "Booked by hand",
            "location": "Wynn"}
        self.client = EventsClient(token_provider=lambda: self.fake.token, transport=self.fake.transport())
        self.store = Store(":memory:", clock=self.clock)       # journal times on the demo clock
        self.rules = R.parse(rules_text(self.secret, push_topic))     # RulesError on a bad topic
        self.watcher = Watcher(self.client, self.store, self.rules, EVENT, interval=INTERVAL,
                               clock=self.clock, swapper=self._swap)
        self.app = App(self.watcher, self.store, self.rules, EVENT, host=host, port=port, clock=self.clock,
                       demo=True, cookie=COOKIE)
        self._codes = by_code
        self._monotonic = monotonic
        self.done: list[str] = []
        self._busy = threading.Lock()                  # one scenario at a time
        self._repeats = 0
        self._removed: Session | None = None
        self._notes: list[str] = []
        self._tour_at = 0
        self.app.scenarios = [{k: v for k, v in c.items() if k != "fn"} for c in self.catalog()]
        self.app.run_scenario = self.run_scenario

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

    # ---- the control panel: one scenario per button. Each runs on the watcher's thread, puts its own
    # sessions back where they started (so any button can be pressed again, in any order), changes the
    # fake API the way the real one could, runs one real sweep, and reports only what really happened.

    def catalog(self) -> list[dict[str, Any]]:
        """Every scenario: its group, button label, what happens at AWS, what you should see, action."""
        rows = [
            ("seat", SEATS, "A seat opens",
             "Someone cancels a seat in SVS306-R, a talk you rank above CMP409-R, which you hold at the "
             "same time.", "A swap offer on the dashboard and your phone, with Swap now and Keep.",
             self._sc_seat),
            ("fill", SEATS, "The new seat fills mid-swap",
             "You approve that swap, and in the same second someone else takes the new seat.",
             "re:Seat books your old seat back. Swap rolled back.", self._sc_fill),
            ("repeat", SEATS, "AWS adds a repeat sitting",
             "AWS adds another sitting of AIM310, a talk you want.",
             "Booked at once and read back. Seat booked.", self._sc_repeat),
            ("blocked", SEATS, "A seat opens that clashes",
             "A seat opens in BIZ108, but it is at the same time as COP301, which you rank higher.",
             "Not booked, and you are told why.", self._sc_blocked),
            ("unsure", SEATS, "A booking cannot be confirmed",
             "AWS says yes to a booking, but your schedule never shows it.",
             "Check your schedule. Nothing more is sent.", self._sc_unsure),
            ("move", SCHEDULE, "A held session changes room",
             "AWS moves CMP303, which you hold, to another room.", "CMP303 moved, with the new room.",
             self._sc_move),
            ("remove", SCHEDULE, "AWS removes a held session",
             "AWS drops SEC351, which you hold, from the catalog.", "SEC351 left the catalog.",
             self._sc_remove),
            ("leave", SCHEDULE, "Jump to the next leave time",
             "The demo clock jumps to when you must leave for your next session.",
             "Leave now, with the walk.", self._sc_leave),
            ("close", AWS, "AWS switches writes off",
             "AWS stops accepting bookings for a while, as it did before 8 October.",
             "Booking paused. The seat it wanted waits in a queue.", self._sc_close),
            ("open", AWS, "AWS switches writes back on", "AWS accepts bookings again.",
             "Booking resumed, then the queued seat is booked.", self._sc_open),
            ("down", AWS, "The API goes down for 11 minutes", "AWS's API stops answering for 11 minutes.",
             "re:Seat offline.", self._sc_down),
            ("up", AWS, "The API comes back", "AWS's API answers again.", "re:Seat back.", self._sc_up),
            ("empty", AWS, "AWS serves an empty catalog, then restores it",
             "AWS serves no sessions at all, as it did on 8 October, then fixes it.",
             "re:Seat keeps its own copy. The re:Invent catalog is back.", self._sc_empty),
            ("signout", AWS, "Your sign-in expires", "Your AWS sign-in runs out.",
             "Sign in needed. Nothing is booked until you sign in again.", self._sc_signout),
            ("signin", AWS, "Sign in again", "You sign in again.", "Bookings resume.", self._sc_signin),
        ]
        return [{"key": k, "group": g, "label": label, "story": story, "see": see, "fn": fn}
                for k, g, label, story, see, fn in rows]

    def scenarios(self) -> list[tuple[str, str, str, Callable[[], str]]]:
        """(key, button label, what you should see, action), each action reported and recorded."""
        out = [(c["key"], c["label"], c["see"], self._reported(c)) for c in self.catalog()]
        return out + [("tour", "Guided tour: next step", "Five scenarios in order", self._tour)]

    def run_scenario(self, key: str) -> str:
        """From an HTTP thread: one scenario at a time, run on the watcher's thread."""
        if not self._busy.acquire(blocking=False):
            return "Another scenario is still running. Try again in a moment."
        try:
            for k, _label, _see, fn in self.scenarios():
                if k == key:
                    return str(self.watcher.submit(fn).result(timeout=60))
            raise KeyError(key)
        finally:
            self._busy.release()

    def _reported(self, c: dict[str, Any], step: str = "") -> Callable[[], str]:
        """Run the scenario, append what reached the phone (from the events it really caused), and keep
        the result for the banner at the top of the dashboard."""
        def run() -> str:
            seen: list[Any] = []
            self._notes = []
            self.watcher.subscribe(seen.append)
            try:
                said = c["fn"]()
            finally:
                self.watcher.unsubscribe(seen.append)
            if self._notes:
                said = f"Demo reset first: {'; '.join(self._notes)}. {said}"
            pushed = (push.message(ev, self._code_title) for ev in seen)
            titles = list(dict.fromkeys(m[0] for m in pushed if m))
            msg = (f"{said} Notifications: {'; '.join(titles)}." if titles
                   else f"{said} No notification this time.")
            self.app.last_scenario = {
                "at": self.clock(), "label": c["label"], "story": c["story"], "did": said,
                "phone": titles, "codes": self._codes_in(seen), "step": step,
                "next": self._tour_next_label()}
            at = self._tour_at % len(TOUR)
            self.app.tour_label = (f"Guided tour: step {at + 1} of {len(TOUR)}" if at
                                   else "Start the guided tour")
            self.app.refresh()
            return msg
        return run

    def _codes_in(self, events: list[Any]) -> list[str]:
        """Session codes the run touched, for highlighting on the week grid."""
        codes: list[str] = []
        for ev in events:
            d = ev.data
            for sid in (d.get("session_id"), d.get("held_id"), d.get("wanted_id")):
                s = self.store.get(EVENT, sid) if sid else None
                if s and s.abbreviation:
                    codes.append(s.abbreviation)
            if d.get("code"):
                codes.append(d["code"])
        return list(dict.fromkeys(codes))

    def _tour(self) -> str:
        """The guided tour: one press runs the next of five scenarios, with what to look for."""
        by_key = {c["key"]: c for c in self.catalog()}
        key = TOUR[self._tour_at % len(TOUR)]
        self._tour_at += 1
        step = f"Tour step {(self._tour_at - 1) % len(TOUR) + 1} of {len(TOUR)}"
        return self._reported(by_key[key], step)()

    def _tour_next_label(self) -> str:
        nxt = TOUR[self._tour_at % len(TOUR)]
        if self._tour_at and self._tour_at % len(TOUR) == 0:
            return "The tour is done. Press Guided tour to start it again."
        return next(c["label"] for c in self.catalog() if c["key"] == nxt)

    def _code_title(self, sid: str) -> str:
        s = self.store.get(EVENT, sid)
        return f"{s.abbreviation} {s.title}" if s else "a session"

    def _sweep(self) -> None:
        self.watcher.tick()
        self.app.refresh()

    def _blocked(self) -> str | None:
        """Why a write cannot happen right now, or None."""
        if self.watcher.read_only:
            return "You are signed out, so re:Seat only reads. Press Sign in again first."
        if self.watcher.down_since is not None:
            return "The API is down, so nothing can change. Press The API comes back first."
        return None

    def _hold(self, sid: str, held: bool) -> None:
        """Demo set-up only: put a reservation back as a scenario needs it, and say so in the journal,
        so a seat never appears or vanishes without a line explaining it."""
        if (sid in self.fake.schedule.reserved) == held:
            return
        (self.fake.schedule.reserved.add if held else self.fake.schedule.reserved.discard)(sid)
        code = self.fake.sessions[sid].abbreviation if sid in self.fake.sessions else "a session"
        note = f"{code} {'held again' if held else 'released'}"
        self._notes.append(note)
        self.store.journal(EVENT, "demo.reset", None, {"session": code}, "held" if held else "released")

    def _reset_band(self, code: str, band: str = "unavailable") -> str:
        """Put a session back to full, unheld, and let a sweep record it, so it can open again."""
        sid = self._codes[code]
        self._hold(sid, False)
        self.fake.full.discard(sid)
        self.fake.set_band(sid, band)
        self._sweep()
        return sid

    def _sc_seat(self) -> str:
        if self.watcher.pending():
            return "A swap is already waiting. Approve it, keep it, or press The new seat fills mid-swap."
        why = self._blocked()
        if why:
            return why
        held, wanted = self._codes["CMP409-R"], self._codes["SVS306-R"]
        self._hold(held, True)                           # back to the start: CMP409-R held, SVS306-R full
        self._reset_band("SVS306-R")
        self.fake.set_band(wanted, "limited")
        self._sweep()
        if self.watcher.pending():
            return "SVS306-R has a seat. It clashes with CMP409-R, which matters less to you: a swap waits."
        return "SVS306-R has a seat, but no swap was proposed: writes may be paused. See the journal."

    def _sc_fill(self) -> str:
        if not self.watcher.pending():
            said = self._sc_seat()
            if not self.watcher.pending():
                return said
        p = self.watcher.pending()[0]
        self.fake.full.add(p.wanted_id)                  # the band still reads open, the reserve says full
        try:
            result = self.watcher.approve(p.plan_id)
        except ApprovalFailed as e:
            return f"Swap now did not run: {e}"
        finally:
            self.fake.full.discard(p.wanted_id)
            self.app.refresh()
        if getattr(result, "state", None) == "rolled_back":
            return ("You approved. CMP409-R was cancelled, SVS306-R filled in that moment, and re:Seat "
                    "reserved CMP409-R again.")
        return f"Swap now ran and ended {getattr(result, 'state', 'unknown')}. See the journal."

    def _sc_repeat(self) -> str:
        why = self._blocked()
        if why:
            return why
        for s in self.fake.sessions.values():           # back to the start: no AIM310 sitting held
            if s.base_code == "AIM310":
                self._hold(s.session_id, False)
        self._sweep()
        self._repeats += 1
        sid, code = f"{NEW_SITTING_ID}-{self._repeats}", f"AIM310-R{self._repeats + 1}"
        base = self.fake.sessions[self._codes["AIM310"]]
        self.fake.add_session(base.model_copy(update={
            "session_id": sid, "abbreviation": code, "seat_availability": "available",
            "session_time": base.session_time.model_copy(update={"date": "2026-12-03", "time": "15:00"})
            if base.session_time else None}))
        self._sweep()
        if sid in self.fake.schedule.reserved:
            return f"AWS added {code} on Thursday. re:Seat booked it and read it back."
        return f"AWS added {code} on Thursday, but it was not booked. See the journal."

    def _reopen(self, code: str) -> str | None:
        why = self._blocked()
        if why:
            return why
        sid = self._reset_band(code)
        self.fake.set_band(sid, "available")
        self._sweep()
        return None

    def _sc_blocked(self) -> str:
        return self._reopen("BIZ108") or ("BIZ108 has a seat, but it overlaps COP301, which matters more to "
                                          "you, so it was not booked.")

    def _sc_unsure(self) -> str:
        self.fake.ghost.add(self._codes["ARC306"])       # the API says yes, the schedule never shows it
        return self._reopen("ARC306") or ("ARC306 opened. The reserve said yes but the read-back did not "
                                          "show it, so re:Seat asks you to check and sends nothing more.")

    def _sc_move(self) -> str:
        why = self._blocked()
        if why:
            return why
        sid = self._codes["CMP303"]
        room = "Level 2 | Premier 214" if self.fake.sessions[sid].room == NEW_ROOM else NEW_ROOM
        self.fake.sessions[sid] = self.fake.sessions[sid].model_copy(update={"room": room})
        self._sweep()
        return f"CMP303 on Wednesday moved to {room}."

    def _sc_remove(self) -> str:
        why = self._blocked()
        if why:
            return why
        sid = self._codes["SEC351"]
        if sid not in self.fake.sessions:               # back to the start: SEC351 listed and held
            self.fake.add_session(self._removed)
            self._hold(sid, True)
            self._sweep()
        self._removed = self.fake.sessions.pop(sid)
        self.fake.schedule.reserved.discard(sid)
        self._sweep()
        return "AWS took SEC351 out of the catalog. re:Seat tells you and changes nothing else."

    def _sc_leave(self) -> str:
        now = self.clock()
        held = guard.held_sessions(self.store, EVENT, sorted(self.watcher.last_held))
        blocks, _ = guard.leave_blocks(held, self.rules)
        later = sorted(b for b in ((_utc(b.start), b.code) for b in blocks) if b[0] > now + 30)
        if not later:
            return "No leave time is left this week."
        at, code = later[0]
        waiting = bool(self.watcher.pending())
        self.offset += at + 5 - now                     # forward only: the demo clock never goes back
        self.app.refresh()
        return f"The demo clock jumped to the leave time for {code}.{self._expired(waiting)}"

    def _sc_close(self) -> str:
        if self.fake.closed:
            return "Writes are already off. Press AWS switches writes back on."
        why = self._blocked()
        if why:
            return why
        sid = self._reset_band("IND424")
        self.fake.closed = True
        self.fake.set_band(sid, "available")
        self._sweep()
        return "AWS switched writes off. IND424 opened, its reserve got a 409, and re:Seat queued it."

    def _sc_open(self) -> str:
        if not self.fake.closed:
            return "Writes are already on. Press AWS switches writes off first."
        why = self._blocked()
        if why:
            return why
        self.fake.closed = False
        self._sweep()
        booked = self._codes["IND424"] in self.fake.schedule.reserved
        resumed = self.watcher.writes_closed_until is None
        return ("AWS switched writes back on. "
                + ("re:Seat asked its probe session and resumed" if resumed
                   else "re:Seat has not resumed yet")
                + (", then booked the queued IND424." if booked else "."))

    def _sc_down(self) -> str:
        if self.watcher.down_since is not None:
            return "The API is already down. Press The API comes back."
        waiting = bool(self.watcher.pending())
        self.fake.fail_next("ListSessions", 503, times=10_000)
        self._sweep()
        self.offset += 11 * 60                          # the demo clock moves on 11 minutes
        self._sweep()
        return f"The catalog answers 503 and the demo clock moved on 11 minutes.{self._expired(waiting)}"

    def _expired(self, waiting: bool) -> str:
        return " The waiting swap expired with the jump." if waiting and not self.watcher.pending() else ""

    def _sc_up(self) -> str:
        if self.watcher.down_since is None:
            return "The API is already up. Press The API goes down first."
        self.fake.clear_fail("ListSessions")
        self._sweep()
        return "The API answers again, and re:Seat catches up on anything that changed."

    def _sc_empty(self) -> str:
        if self.watcher.down_since is not None:
            return "The API is down. Press The API comes back first."
        saved = dict(self.fake.sessions)
        self.fake.sessions.clear()                      # as AWS served on 8 October
        self._sweep()
        self.fake.sessions.update(saved)
        self._sweep()
        return ("The API served an empty catalog. re:Seat refused it and kept its copy, then the catalog "
                "came back.")

    def _sc_signout(self) -> str:
        if self.watcher.read_only:
            return "You are already signed out. Press Sign in again."
        self.fake.fail_next("GetSchedule", 401, times=10_000)
        self._sweep()
        return "The API answers 401. re:Seat only reads now: nothing is booked until you sign in again."

    def _sc_signin(self) -> str:
        if not self.watcher.read_only:
            return "You are already signed in."
        self.fake.clear_fail("GetSchedule")
        self._sweep()
        return "Signed in again. re:Seat books and swaps as before."


def _utc(start: str) -> float:
    """A personal-time start (UTC, no offset) as a timestamp."""
    return datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
