"""On-site reminders: the morning briefing, leave in 10 minutes, leave now, and a wanted session soon.

Each is an event on the watcher, so the phone gets it through the same push as everything else. They
are reads of the local store only: nothing here calls the Events API or writes anything.

Rules this module exists to respect:
- Once each. A reminder is keyed by what it is about, so a second refresh never sends it twice.
  The keys live in memory: a restart inside a reminder's window can send that one again.
- Codes and titles only. Session ids stay on the laptop.
- Session times are Las Vegas local time, leave-now blocks are UTC. Both go through campus helpers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import guard
from .campus import EVENT_DAYS, VEGAS
from .models import Session

LEAVE_SOON = 10 * 60          # leave in 10 minutes
WANTED_SOON = 20 * 60         # a wanted session you do not hold starts in 20 minutes
EXPIRING = 2 * 60             # a swap proposal that asks first lapses in 2 minutes
BRIEF_FROM, BRIEF_UNTIL = 6, 10   # the morning briefing goes out between 06:00 and 10:00 Las Vegas time

MAX_SENT = 5000
Emit = Callable[..., None]
History = Callable[[str], list[tuple[float, str | None, str | None]]]


def vegas(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, VEGAS)


def starts_at(s: Session) -> float | None:
    win = guard._window(s)
    return win[0].timestamp() if win else None


def how(b: guard.Block) -> str:
    """The leave-now block's own description, without the re:Seat prefix and tag."""
    return b.description.removeprefix(guard.PREFIX).removesuffix(guard.TAG).strip()


def leave_time(b: guard.Block) -> float:
    return datetime.fromisoformat(b.start).replace(tzinfo=UTC).timestamp()


@dataclass
class Reminders:
    emit: Emit
    history: History = lambda sid: []
    sent: set[str] = field(default_factory=set)
    wanted_at: dict[str, float] = field(default_factory=dict)

    def check(self, now: float, held: list[Session], blocks: list[guard.Block],
              wanted: Iterable[Session], waiting: int, fresh: bool = True) -> None:
        """Send whatever is due at `now`. `wanted` is every sitting of a target not held, `waiting`
        the number of swaps that wait for approval. `fresh` is False while the API is unreachable: the
        briefing then waits, since what is held may have changed, and goes out once a sweep succeeds."""
        if fresh:
            self._briefing(now, held, blocks, waiting)
        self._leave(now, held, blocks)
        self._wanted(now, held, wanted)

    def expiring(self, now: float, pending: Iterable[Any]) -> None:
        """A swap proposal that asks first is about to lapse: one last push, with its buttons."""
        for p in pending:
            if not p.auto and 0 < p.expires - now <= EXPIRING and self._once(f"expiring:{p.plan_id}"):
                self.emit("expiring", plan_id=p.plan_id, held_id=p.held_id, wanted_id=p.wanted_id,
                          left=int(p.expires - now))

    def tight(self, now: float, warnings: Iterable[str]) -> None:
        """Two held sessions with a walk longer than the gap between them, today or later. Once each."""
        today = vegas(now).date().isoformat()
        for w in warnings:
            day, _, text = w.partition(": ")
            if day >= today and self._once(f"tight:{w}"):
                self.emit("tight", day=f"{datetime.fromisoformat(day):%A}", message=text)

    def other_sittings(self, found: Iterable[tuple[Session, Session]]) -> None:
        """(pinned, other): the only sitting the rules allow is full, another sitting has seats."""
        for pinned, other in found:
            start = starts_at(other)
            if start is None or not self._once(f"other:{pinned.session_id}:{other.session_id}"):
                continue
            self.emit("other_sitting", code=pinned.abbreviation, other=other.abbreviation,
                      at=f"{vegas(start):%a %H:%M}", venue=other.campus_venue, band=other.seat_availability)

    def _once(self, key: str) -> bool:
        if key in self.sent:
            return False
        if len(self.sent) >= MAX_SENT:
            self.sent.clear()          # a week holds a few hundred keys; this only bounds a runaway
        self.sent.add(key)
        return True

    def _briefing(self, now: float, held: list[Session], blocks: list[guard.Block], waiting: int) -> None:
        """Once per conference day, the first time re:Seat looks between 06:00 and 10:00."""
        local = vegas(now)
        day = local.date().isoformat()
        if day not in EVENT_DAYS.values() or not BRIEF_FROM <= local.hour < BRIEF_UNTIL:
            return
        if not self._once(f"brief@{day}"):
            return
        today = sorted((s for s in held if s.session_time and s.session_time.date == day
                        and (starts_at(s) or 0.0) > now), key=lambda s: starts_at(s) or 0.0)
        if not today:
            body = "Nothing held for the rest of today."
        else:
            first = today[0]
            leave = next((b for b in blocks if b.session_id == first.session_id), None)
            body = (f"First: {first.abbreviation} at {vegas(starts_at(first) or now):%H:%M}, "
                    f"{first.campus_venue or 'venue not set'}.")
            if leave and leave_time(leave) > now:
                body += f" Leave at {vegas(leave_time(leave)):%H:%M}."
            rest = ", ".join(f"{s.abbreviation} {vegas(starts_at(s) or now):%H:%M}" for s in today[1:])
            if rest:
                body += f" Then {rest}."
        if waiting:
            body += f" {waiting} swap{'s' if waiting > 1 else ''} wait{'' if waiting > 1 else 's'} for you."
        n = len(today)
        self.emit("briefing", count=n, message=body,
                  title=f"Today: {n} session{'' if n == 1 else 's'}" if n else "Today: a free day")

    def _leave(self, now: float, held: list[Session], blocks: list[guard.Block]) -> None:
        """Leave in 10 minutes, then leave now, each once per block, until the session starts."""
        by_id = {s.session_id: s for s in held}
        for b in blocks:
            at = leave_time(b)
            s = by_id.get(b.session_id)
            start = (starts_at(s) if s else None) or at + 300
            key = f"{b.session_id}@{b.start}"
            left = f"left:{b.session_id}"                # leave now already went out for this session
            if (at - LEAVE_SOON <= now < at - 60 and left not in self.sent
                    and self._once(f"soon:{key}")):
                self.emit("leave_soon", code=b.code, at=f"{vegas(at):%H:%M}", message=how(b))
            if at <= now < start and self._once(key):
                self.sent.add(left)                     # a leave time that later moves never reads backwards
                self.emit("leave", code=b.code, message=how(b))

    def _wanted(self, now: float, held: list[Session], wanted: Iterable[Session]) -> None:
        """A sitting of a target you do not hold starts within 20 minutes: say so, with queue-or-go.
        Once per talk per window. Skipped when another sitting of the talk is held, or when the sitting
        overlaps a session you hold: you are in that one."""
        for s in free_to_attend(held, wanted):
            start = starts_at(s) or 0.0
            if not start - WANTED_SOON <= now < start:
                continue
            talk = s.base_code or s.session_id
            if now - self.wanted_at.get(talk, -WANTED_SOON) < WANTED_SOON:
                continue                       # this talk was just announced for another sitting
            if self._once(f"wanted:{talk}@{start:.0f}"):
                self.wanted_at[talk] = now
                a = guard.queue_or_go(s, self.history(s.session_id))
                self.emit("wanted_soon", code=s.abbreviation, title=s.title, at=f"{vegas(start):%H:%M}",
                          venue=s.campus_venue, verdict=a.verdict, basis=a.basis)


def free_to_attend(held: list[Session], wanted: Iterable[Session]) -> list[Session]:
    """Wanted sittings with a time, whose talk is not held and which overlap no held session."""
    held_codes = {s.base_code for s in held if s.base_code}
    busy = [w for w in (guard._window(s) for s in held) if w]
    out = []
    for s in wanted:
        win = guard._window(s)
        if win and s.base_code not in held_codes and not any(a < win[1] and win[0] < b for a, b in busy):
            out.append(s)
    return out
