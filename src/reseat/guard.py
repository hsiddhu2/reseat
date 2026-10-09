"""Cutoff guard: leave-now blocks, venue-switch warnings, queue-or-go advice.

Reserved seats are released shortly before a session starts. re:Seat uses an
11-minute cutoff (campus.CUTOFF_MINUTES) and the walking table to work out
when to leave, and writes that as a 5-minute personal time block in the
official schedule so the phone app shows it.

API rules this module exists to respect:
- Personal time is UTC, no offset, seconds 00, length a whole number of
  5-minute steps. campus.to_personal_time formats it. Never by hand.
- Title 1 to 128 characters, description 1 to 250, location up to 255.
- UpdatePersonalTime is a full replacement, so every field is sent.
- CreatePersonalTime is not idempotent. Each create is sent once. One read-back
  after all writes confirms the result. DeletePersonalTime is the only
  idempotent write.
- The guard only ever touches its own entries: description starts with
  "re:Seat leave-now." and ends with the [reseat] tag.
- A held session missing from the local catalog blocks every delete, so a stale
  store never wipes the attendee's leave-now blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .campus import CUTOFF_MINUTES, session_start_utc, to_personal_time, walk_minutes
from .client import ApiError, EventsClient, OperationClosed
from .models import PersonalTime, PersonalTimeInput, Session
from .rules import Rules
from .store import Store

TAG = "[reseat]"
PREFIX = "re:Seat leave-now."
BLOCK_MINUTES = 5


@dataclass
class Block:
    """One leave-now block, as it should exist."""

    code: str
    session_id: str
    start: str          # UTC, YYYY-MM-DDTHH:MM:00
    end: str
    title: str
    description: str
    location: str | None
    origin: str | None = None     # where the walk starts: the previous held venue, or home_venue
    walk: int | None = None       # walking minutes from origin, None when unknown

    def body(self) -> PersonalTimeInput:
        return PersonalTimeInput(startDateTime=self.start, endDateTime=self.end, title=self.title,
                                 description=self.description, location=self.location)

    def same_as(self, pt: PersonalTime) -> bool:
        return (pt.start_date_time, pt.end_date_time, pt.title, pt.description, pt.location or None) == (
            self.start, self.end, self.title, self.description, self.location)


@dataclass
class GuardPlan:
    create: list[Block] = field(default_factory=list)
    update: list[tuple[str, Block]] = field(default_factory=list)    # (personalTimeId, block)
    delete: list[PersonalTime] = field(default_factory=list)
    keep: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.delete)


@dataclass
class GuardResult:
    plan: GuardPlan
    done: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    closed: bool = False


# ---------------------------------------------------------------------- leave times


def _window(s: Session) -> tuple[datetime, datetime] | None:
    st = s.session_time
    if not st or not st.date or not st.time:
        return None
    start = session_start_utc(st.date, st.time)
    return start, start + timedelta(minutes=st.minutes or 60)


def held_sessions(store: Store, event_id: str, held: list[str]) -> list[Session]:
    """Held sessions with a time, in start order."""
    out = [s for s in (store.get(event_id, i) for i in held) if s and _window(s)]
    return sorted(out, key=lambda s: _window(s)[0])  # type: ignore[index]


def leave_blocks(sessions: list[Session], rules: Rules) -> tuple[list[Block], list[str]]:
    """One block per held session, plus venue-switch warnings. No I/O.

    Leave at: start - 11 min cutoff - walk from the previous held session's venue
    (or home_venue for the first session of the day). If the previous session
    ends after that moment, the change is tight, so buffer_minutes come off too.
    """
    blocks: list[Block] = []
    warnings: list[str] = []
    prev: Session | None = None
    for s in sessions:
        start, _end = _window(s)  # type: ignore[misc]
        day = s.session_time.date if s.session_time else None
        same_day = prev is not None and prev.session_time is not None and prev.session_time.date == day
        origin = prev.campus_venue if same_day and prev else rules.home_venue
        dest = s.campus_venue
        walk = walk_minutes(origin, dest) if origin and dest else None
        leave = start - timedelta(minutes=CUTOFF_MINUTES + (walk or 0))
        if same_day and prev:
            prev_end = _window(prev)[1]  # type: ignore[index]
            if prev_end > leave:
                leave -= timedelta(minutes=rules.buffer_minutes)
                gap = int((start - prev_end).total_seconds() // 60)
                if walk is not None and walk > gap:
                    warnings.append(f"{day}: {prev.abbreviation} ends at {_local(prev_end)}, "
                                    f"{s.abbreviation} starts at {_local(start)} at {dest}. "
                                    f"The walk from {origin} is {walk} min, the gap is {gap} min.")
        code = s.abbreviation or s.session_id
        if walk is None:
            how = f"Be at {dest or 'the venue'} by {_local(start - timedelta(minutes=CUTOFF_MINUTES))}."
        else:
            how = f"Walk {origin} to {dest}, {walk} min."
        blocks.append(Block(
            code=code, session_id=s.session_id,
            start=to_personal_time(leave), end=to_personal_time(leave + timedelta(minutes=BLOCK_MINUTES)),
            title=f"Leave for {code}"[:128],
            description=f"{PREFIX} {how[:250 - len(PREFIX) - len(TAG) - 2]} {TAG}",
            location=(dest or None) and dest[:255], origin=origin, walk=walk))
        prev = s
    return blocks, warnings


def _local(dt: datetime) -> str:
    from .campus import VEGAS
    return dt.astimezone(VEGAS).strftime("%H:%M")


# ---------------------------------------------------------------------- sync


def is_mine(p: PersonalTime) -> bool:
    d = p.description or ""
    return d.startswith(PREFIX) and d.endswith(TAG)


def plan(blocks: list[Block], existing: list[PersonalTime], allow_delete: bool = True) -> GuardPlan:
    """Diff wanted blocks against the schedule. Entries that are not ours are never touched."""
    mine = [p for p in existing if is_mine(p)]
    by_title: dict[str, PersonalTime] = {}
    gp = GuardPlan()
    for p in mine:
        if p.title in by_title:
            gp.delete.append(p)            # a duplicate of our own block
        else:
            by_title[p.title] = p
    for b in blocks:
        cur = by_title.pop(b.title, None)
        if cur is None:
            gp.create.append(b)
        elif b.same_as(cur):
            gp.keep.append(b)
        else:
            gp.update.append((cur.personal_time_id, b))
    gp.delete += list(by_title.values())
    if not allow_delete:
        gp.delete = []
    return gp


def compute(client: EventsClient, store: Store, event_id: str, rules: Rules) -> GuardPlan:
    sched = client.get_schedule(event_id)
    missing = [i for i in sched.reserved if store.get(event_id, i) is None]
    blocks, warnings = leave_blocks(held_sessions(store, event_id, sched.reserved), rules)
    gp = plan(blocks, sched.personal_time, allow_delete=not missing)
    if missing:
        warnings.append(f"{len(missing)} held sessions are not in the local catalog ({', '.join(missing)}). "
                        "Run reseat sync. No block was deleted.")
    gp.warnings = warnings
    return gp


def sync(client: EventsClient, store: Store, event_id: str, rules: Rules,
         dry_run: bool = False) -> GuardResult:
    """Make the tagged blocks match current holds. Idempotent: a second run sends nothing."""
    gp = compute(client, store, event_id, rules)
    res = GuardResult(gp)
    if dry_run or gp.empty:
        return res
    try:
        for p in gp.delete:
            client.delete_personal_time(event_id, p.personal_time_id)
            store.journal(event_id, "DeletePersonalTime", p.personal_time_id, None, "sent")
            res.done.append(f"deleted {p.title}")
        for pid, b in gp.update:
            client.update_personal_time(event_id, pid, b.body())
            store.journal(event_id, "UpdatePersonalTime", {"id": pid, **b.body().payload()}, None, "sent")
            res.done.append(f"updated {b.title}")
        for b in gp.create:
            client.create_personal_time(event_id, b.body())
            store.journal(event_id, "CreatePersonalTime", b.body().payload(), None, "sent")
            res.done.append(f"created {b.title}")
    except OperationClosed:
        res.closed = True
        res.problems.append("Personal time writes are closed (409). Stopped.")
    except ApiError as e:
        res.problems.append(f"{e}. Stopped. Read back below.")
    _read_back(client, store, event_id, rules, res)
    return res


def _read_back(client: EventsClient, store: Store, event_id: str, rules: Rules, res: GuardResult) -> None:
    try:
        after = compute(client, store, event_id, rules)
    except ApiError as e:
        res.problems.append(f"read-back failed: {e.status} {e}")
        return
    if not after.empty:
        res.problems.append(f"Read-back differs: {len(after.create)} missing, {len(after.update)} stale, "
                            f"{len(after.delete)} extra. Run guard sync again to settle.")
    store.journal(event_id, "GetSchedule", "guard read-back",
                  {"missing": len(after.create), "stale": len(after.update), "extra": len(after.delete)},
                  "readback-ok" if after.empty else "readback-disagreement")


# ---------------------------------------------------------------------- queue or go


@dataclass
class Advice:
    verdict: str        # likely | early | unlikely | unknown
    basis: str


_TYPE_RULES = {
    "Breakout session": ("likely", "Breakouts are large rooms. Walk-ups are usually admitted."),
    "Lightning talk": ("likely", "Lightning talks are mostly walk-up."),
    "Chalk talk": ("early", "Chalk talks are small rooms. Join the walk-up line 30 minutes early."),
    "Code talk": ("early", "Code talks are small rooms. Join the walk-up line 30 minutes early."),
    "Workshop": ("unlikely", "Workshops seat few walk-ups."),
    "Builders' session": ("unlikely", "Builders' sessions seat very few walk-ups."),
    "Lab": ("unlikely", "Labs seat few walk-ups."),
    "Bootcamp": ("unlikely", "Bootcamps seat few walk-ups."),
}
HISTORY_MIN = 3
_OPEN = {"available", "limited", "veryLimited"}


def queue_or_go(s: Session, history: list[tuple[float, str | None, str | None]]) -> Advice:
    """A rule by session type, overridden by this session's band history once it has 3 changes.

    A heuristic, never a prediction. The basis is always returned.
    """
    if len(history) >= HISTORY_MIN:
        reopened = sum(1 for _, old, new in history if old == "unavailable" and new in _OPEN)
        if reopened:
            return Advice("likely", f"Band history: seats freed {reopened} times in {len(history)} changes.")
        return Advice("unlikely", f"Band history: {len(history)} changes, seats never freed after filling.")
    verdict, basis = _TYPE_RULES.get(s.type_key or "", ("unknown", "No rule for this session type."))
    return Advice(verdict, f"Session type rule. {basis}")
