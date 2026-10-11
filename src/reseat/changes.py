"""What changed on the attendee's week between two looks.

The web app keeps the last placement of every held and proposed session. Each refresh compares the new
placement with it and records a Change: a seat booked, a swap that went through, a seat released, a
session that moved room or time, a held session AWS removed, a swap proposed. The week grid labels the
changed sessions for a while and keeps a ghost of what left, and the demo page shows before and after.

Rules this module exists to respect:
- It only compares what re:Seat already read. It makes no API call.
- The first look is a baseline, never a change: a restart does not report the whole week as new.
- Codes and titles only reach the page. Session ids stay in this module's keys.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from . import guard
from .campus import VEGAS
from .models import Session

MARK_TTL = 15 * 60          # how long a change stays labelled on the week
LABEL = {"new": "NEW", "swapped_in": "SWAPPED IN", "released": "RELEASED", "moved": "MOVED",
         "removed": "LEFT CATALOG", "proposed": "PROPOSED"}
GHOSTS = ("released", "removed")    # these leave a crossed-out block where the session was


@dataclass(frozen=True)
class Place:
    code: str
    title: str
    start: datetime
    end: datetime
    venue: str
    room: str
    state: str              # held | proposed

    def where(self) -> str:
        return f"{self.start:%a} {self.start:%H:%M}, {', '.join(x for x in (self.venue, self.room) if x)}"


@dataclass(frozen=True)
class Change:
    kind: str
    at: float
    place: Place
    before: str | None = None       # where it was, for a move

    @property
    def label(self) -> str:
        return LABEL[self.kind]

    def text(self) -> str:
        p = self.place
        return {
            "new": f"{p.code} booked, {p.where()}",
            "swapped_in": f"{p.code} swapped in, {p.where()}",
            "released": f"{p.code} released, the seat is given back",
            "moved": f"{p.code} moved from {self.before} to {p.where()}",
            "removed": f"{p.code} left the catalog",
            "proposed": f"{p.code} proposed as a swap, {p.where()}",
        }[self.kind]


def place(s: Session, state: str) -> Place | None:
    win = guard._window(s)
    if not win:
        return None
    a, b = win[0].astimezone(VEGAS), win[1].astimezone(VEGAS)      # shown in Las Vegas time
    return Place(s.abbreviation or "a session", s.title or "", a, b, s.campus_venue or "",
                 s.room_label or "", state)


class Tracker:
    def __init__(self) -> None:
        self.prev: dict[str, Place] | None = None
        self.log: deque[Change] = deque(maxlen=200)
        self.removed: set[str] = set()      # ids the watcher reported gone from the catalog

    def look(self, now: float, held: Iterable[Session], proposed: Iterable[Session]) -> list[Change]:
        """Compare this look with the last one. Returns the changes and keeps them in the log."""
        cur: dict[str, Place] = {}
        for s in proposed:
            p = place(s, "proposed")
            if p:
                cur[s.session_id] = p
        for s in held:
            p = place(s, "held")
            if p:
                cur[s.session_id] = p
        if self.prev is None:
            self.prev, self.removed = cur, set()
            return []
        out = self._diff(now, self.prev, cur)
        self.prev, self.removed = cur, set()
        self.log.extend(out)
        return out

    def _diff(self, now: float, prev: dict[str, Place], cur: dict[str, Place]) -> list[Change]:
        out: list[Change] = []
        gone = [(sid, p) for sid, p in prev.items() if sid not in cur and p.state == "held"]
        for sid, p in gone:
            out.append(Change("removed" if sid in self.removed else "released", now, p))
        for sid, p in cur.items():
            old = prev.get(sid)
            if sid in self.removed and old and old.state == "held":
                out.append(Change("removed", now, p))
            elif old is None:
                if p.state == "proposed":
                    out.append(Change("proposed", now, p))
                elif any(_overlap(p, g) for _, g in gone):
                    out.append(Change("swapped_in", now, p))
                else:
                    out.append(Change("new", now, p))
            elif old.state == "proposed" and p.state == "held":
                out.append(Change("swapped_in", now, p))
            elif p.state == "held" and (old.start, old.end, old.venue, old.room) != (
                    p.start, p.end, p.venue, p.room):
                out.append(Change("moved", now, p, before=old.where()))
        return out

    def recent(self, now: float) -> list[Change]:
        return [c for c in self.log if 0 <= now - c.at < MARK_TTL]


def _overlap(a: Place, b: Place) -> bool:
    return a.start < b.end and b.start < a.end
