"""Order router: turns the rules into ReserveSessions calls.

Two entry points, so the watcher can drive it later:

    plan = router.plan(held, candidates=..., quota_left=...)   # no I/O
    result = execute(plan, client, store, event_id)             # one write, then read-back

`run_booking` loops the two: on sessionFull it plans the next sitting of that
target in the same run, inside the per-minute quota.

API rules this module exists to respect:
- ReserveSessions takes 1 to 10 sessions and counts each one against 30 per
  minute. A batch never exceeds the quota left.
- A 200 does not mean every session landed. Read `failed`, then read back
  GetSchedule and report any disagreement.
- Reserve is not idempotent. A session that failed is never re-sent in the
  same run. A 5xx on a write is never retried, only read back.
- Writes return 409 until 8 October 2026. Stop cleanly, send nothing more.
- Failure codes grow over time. An unknown code is a generic refusal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .campus import overlaps, session_start_utc
from .client import ApiError, EventsClient, NotRegistered, OperationClosed
from .models import BulkResult, FailureCode, Schedule, Session, normalize_type
from .rules import Rules, Target
from .store import Store

BATCH_MAX = 10
OP = "ReserveSessions"
WRITES_CLOSED = ("Reservation writes are closed (409). They open through the API on "
                 "8 October 2026. Nothing was reserved.")

# Formats that are not recorded fill first, so they are sent first. The four types
# the roadmap did not list, seen in the 1 Oct 2026 catalog: Lab and Bootcamp sit
# with Workshop. Gamified learning and Exam prep go with everything else.
# Lightning talks are mostly walk-up and go last.
SCARCITY: dict[str, int] = {
    "Workshop": 0, "Lab": 0, "Bootcamp": 0,
    "Builders' session": 1,
    "Chalk talk": 2,
    "Code talk": 3,
    "Breakout session": 4,
}
OTHER_RANK = 5

# Bands. The API had sent none as of 1 Oct 2026. Unknown or missing means try.
_FULL_BANDS = {"unavailable"}
_NO_RESERVATION_BANDS = {"walkUp"}


def scarcity_rank(type_: str | None) -> int:
    return SCARCITY.get(normalize_type(type_) or "", OTHER_RANK)


# ---------------------------------------------------------------------- plan


@dataclass
class Planned:
    session_id: str
    code: str
    title: str
    type: str | None
    target: str
    priority: int        # 0 is the attendee's first target
    backup: bool
    rank: int


@dataclass
class Skip:
    target: str
    session_id: str
    reason: str
    blocker: str | None = None   # the held or planned session it overlaps, if that was the reason


@dataclass
class Plan:
    batch: list[Planned] = field(default_factory=list)
    deferred: list[Planned] = field(default_factory=list)   # chosen but over the quota left
    skipped: list[Skip] = field(default_factory=list)
    held_targets: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)     # no sitting or backup left to try

    @property
    def ids(self) -> list[str]:
        return [p.session_id for p in self.batch]


Window = tuple[datetime, datetime]


class Router:
    def __init__(self, rules: Rules, store: Store, event_id: str):
        self.rules = rules
        self.store = store
        self.event_id = event_id

    # ---- trees

    def tree(self, t: Target) -> list[tuple[Session, bool]]:
        """The target's sittings by preference, then each backup's sittings. (session, is_backup)."""
        primary = self._sittings(t)
        out = [(s, False) for s in primary]
        for code in t.backups:
            out += [(s, True) for s in self._by_time(self.store.by_base_code(self.event_id, code),
                                                     t.prefer)]
        seen: set[str] = set()
        return [(s, b) for s, b in out if not (s.session_id in seen or seen.add(s.session_id))]

    def _sittings(self, t: Target) -> list[Session]:
        """Listed sittings first. With repeats on, any other sitting of the code follows.

        Planner exports list the sittings known on export day. AWS adds repeats
        later, and booking those is the point of the watcher, so `sittings` only
        restricts the tree when `repeats: false`.
        """
        pinned = self.store.get(self.event_id, t.session_id) if t.session_id else None
        listed = [x for x in (self.store.get(self.event_id, i) for i in t.sittings) if x]
        if not t.repeats:
            pool = listed or ([pinned] if pinned else [])
            extra: list[Session] = []
        else:
            code = t.code or (pinned.base_code if pinned else None)
            pool = listed or (self.store.by_base_code(self.event_id, code) if code else [])
            known = {s.session_id for s in pool}
            extra = [s for s in (self.store.by_base_code(self.event_id, code) if code else [])
                     if s.session_id not in known] if listed else []
        ordered = self._by_time(pool, t.prefer) + self._by_time(extra, t.prefer)
        if pinned:
            first = [s for s in ordered if s.session_id == pinned.session_id]
            ordered = first + [s for s in ordered if s.session_id != pinned.session_id]
        return ordered

    @staticmethod
    def _by_time(sessions: list[Session], prefer: str) -> list[Session]:
        def key(s: Session) -> tuple[str, str]:
            st = s.session_time
            return (st.date or "9999", st.time or "99:99") if st else ("9999", "99:99")
        return sorted(sessions, key=key, reverse=(prefer == "latest"))

    # ---- constraints

    def _window(self, s: Session) -> Window | None:
        st = s.session_time
        if not st or not st.date or not st.time:
            return None
        start = session_start_utc(st.date, st.time)
        return start, start + timedelta(minutes=st.minutes or 60)

    def _why_not(self, s: Session, booked: list[Session], codes: set[str], per_day: dict[str, int],
                 meals: list[Window]) -> tuple[str, str | None] | None:
        """(reason, blocking session id) when `s` cannot be planned, else None."""
        if s.is_reservable is False or s.seat_availability in _NO_RESERVATION_BANDS:
            return "not reservable (walk-up or no reservations)", None
        if s.seat_availability in _FULL_BANDS:
            return "full (band unavailable)", None
        if s.base_code and s.base_code in codes:
            return f"a sitting of {s.base_code} is already held or planned", None
        w = self._window(s)
        if w:
            for other in booked:
                ow = self._window(other)
                if ow and overlaps(w, ow):
                    return f"overlaps {other.abbreviation or other.session_id}", other.session_id
            if any(overlaps(w, m) for m in meals):
                return "overlaps a meal in the rules", None
            day = s.session_time.date if s.session_time else None
            if day and per_day.get(day, 0) >= self.rules.max_per_day:
                return f"max_per_day {self.rules.max_per_day} reached on {day}", None
        return None

    # ---- plan

    def plan(self, held: Iterable[str], candidates: Iterable[str] | None = None,
             quota_left: int = BATCH_MAX, exclude: Iterable[str] = ()) -> Plan:
        """Pick at most one sitting per unheld target, in priority order. No I/O.

        `candidates` limits which sessions may be picked (the watcher passes
        newly opened or added ones). `exclude` holds sessions that already
        failed in this run, so they are never re-sent.
        """
        held_ids = set(held)
        cand = set(candidates) if candidates is not None else None
        skip_ids = set(exclude)
        held_sessions = [x for x in (self.store.get(self.event_id, i) for i in held_ids) if x]
        booked = list(held_sessions)
        held_codes = {x.base_code for x in held_sessions if x.base_code}
        codes = set(held_codes)
        per_day: dict[str, int] = {}
        for x in held_sessions:
            if x.session_time and x.session_time.date:
                per_day[x.session_time.date] = per_day.get(x.session_time.date, 0) + 1
        meals = [m.window() for m in self.rules.meals]

        plan = Plan()
        chosen: list[Planned] = []
        for prio, t in enumerate(self.rules.targets):
            tree = self.tree(t)
            if any(s.session_id in held_ids for s, _ in tree) or (t.code and t.code in held_codes):
                plan.held_targets.append(t.label)
                continue
            pick = None
            for s, is_backup in tree:
                if s.session_id in skip_ids:
                    continue
                if cand is not None and s.session_id not in cand:
                    continue
                why = self._why_not(s, booked, codes, per_day, meals)
                if why:
                    plan.skipped.append(Skip(t.label, s.session_id, why[0], why[1]))
                    continue
                pick = Planned(s.session_id, s.abbreviation or s.session_id, s.title, s.type,
                               t.label, prio, is_backup, scarcity_rank(s.type))
                booked.append(s)
                if s.base_code:
                    codes.add(s.base_code)
                if s.session_time and s.session_time.date:
                    per_day[s.session_time.date] = per_day.get(s.session_time.date, 0) + 1
                break
            if pick:
                chosen.append(pick)
            elif cand is None:
                plan.exhausted.append(t.label)

        chosen.sort(key=lambda p: (p.rank, p.priority))
        room = max(0, min(BATCH_MAX, quota_left))
        plan.batch, plan.deferred = chosen[:room], chosen[room:]
        return plan


# ---------------------------------------------------------------------- execute


@dataclass
class Outcome:
    session_id: str
    target: str
    status: str            # reserved | full | conflict | already | refused | unconfirmed | not_sent
    code: str | None = None
    conflicts_with: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class Execution:
    outcomes: list[Outcome] = field(default_factory=list)
    schedule: Schedule | None = None
    closed: bool = False
    error: str | None = None
    disagreements: list[str] = field(default_factory=list)


_STATUS = {
    FailureCode.SESSION_FULL: "full",
    FailureCode.SCHEDULE_CONFLICT: "conflict",
    FailureCode.ALREADY_SCHEDULED: "already",
}


def execute(plan: Plan, client: EventsClient, store: Store, event_id: str) -> Execution:
    """Send one ReserveSessions call for the plan's batch, then read back GetSchedule.

    Never retries the write. On 409 sends nothing more. On any other error the
    write may or may not have landed, so the schedule is read back to find out.
    Request, response and read-back go to the journal together.
    """
    ex = Execution()
    if not plan.batch:
        return ex
    by_id = {p.session_id: p for p in plan.batch}
    result: BulkResult | None = None
    try:
        result = client.reserve(event_id, plan.ids)
    except OperationClosed:
        ex.closed = True
        ex.outcomes = [Outcome(i, by_id[i].target, "not_sent", note=WRITES_CLOSED) for i in plan.ids]
        store.journal(event_id, OP, plan.ids, {"status": 409}, "closed")
        return ex
    except NotRegistered as e:
        ex.error = str(e)
        ex.outcomes = [Outcome(i, by_id[i].target, "not_sent", note="403 not registered")
                       for i in plan.ids]
        store.journal(event_id, OP, plan.ids, {"status": 403}, "error")
        return ex
    except ApiError as e:
        ex.error = f"{e.status} {e}"

    try:
        ex.schedule = client.get_schedule(event_id)
    except ApiError as e:
        ex.error = (ex.error + "; " if ex.error else "") + f"read-back failed: {e.status} {e}"

    ex.outcomes = _reconcile(plan, result, ex)
    outcome = "error" if ex.error else ("partial" if result and result.failed else "success")
    if ex.disagreements:
        outcome = "disagreement"
    store.journal(event_id, OP, plan.ids, {
        "result": result.model_dump(by_alias=True) if result else None,
        "error": ex.error,
        "readBack": ex.schedule.model_dump(by_alias=True) if ex.schedule else None,
        "disagreements": ex.disagreements,
    }, outcome)
    return ex


def _reconcile(plan: Plan, result: BulkResult | None, ex: Execution) -> list[Outcome]:
    held = set(ex.schedule.reserved) if ex.schedule else None
    out: list[Outcome] = []
    for p in plan.batch:
        sid = p.session_id
        f = result.failure_for(sid) if result else None
        if result is None:
            if held is None:
                o = Outcome(sid, p.target, "unconfirmed", note="write failed and read-back failed")
            elif sid in held:
                o = Outcome(sid, p.target, "reserved", note="landed despite the error, per read-back")
            else:
                o = Outcome(sid, p.target, "refused", note="write failed, not in read-back")
        elif f is None:
            o = Outcome(sid, p.target, "reserved")
            if held is not None and sid not in held:
                o.status = "unconfirmed"
                o.note = "API said successful but GetSchedule does not list it"
                ex.disagreements.append(f"{p.code} ({sid}): {o.note}")
        else:
            status = _STATUS.get(f.known_code, "refused")
            o = Outcome(sid, p.target, status, code=f.code, conflicts_with=f.conflicts_with)
            if held is not None and sid in held and status != "already":
                o.note = f"API said {f.code} but GetSchedule lists it"
                ex.disagreements.append(f"{p.code} ({sid}): {o.note}")
        out.append(o)
    return out


# ---------------------------------------------------------------------- run


@dataclass
class BookingRun:
    executions: list[Execution] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    schedule: Schedule | None = None
    closed: bool = False

    @property
    def outcomes(self) -> list[Outcome]:
        return [o for e in self.executions for o in e.outcomes]


def run_booking(router: Router, client: EventsClient, store: Store, event_id: str,
                held: Iterable[str], candidates: Iterable[str] | None = None,
                max_rounds: int = 20, on_round: Callable[[Plan, Execution], None] | None = None,
                ) -> BookingRun:
    """Plan, send, read back, repeat until nothing is left to try.

    Each failed session is excluded for the rest of the run, so it is never
    re-sent: sessionFull moves the target down its tree, scheduleConflict
    records conflictsWith and moves on, an unknown code is recorded and skipped.
    When the minute's quota is spent the run waits for it, never overspends.
    Stops at the first 409 or other error, after the read-back.
    """
    run = BookingRun()
    held_now = set(held)
    tried: set[str] = set()
    unsure: set[str] = set()
    cand = list(candidates) if candidates is not None else None
    for _ in range(max_rounds):
        left = client.quota.remaining(OP)
        if left <= 0:
            client.sleep(client.quota.seconds_until(OP, 1))
            left = client.quota.remaining(OP)
        plan = router.plan(held_now, candidates=cand, quota_left=left, exclude=tried)
        run.plans.append(plan)
        if not plan.batch:
            break
        ex = execute(plan, client, store, event_id)
        run.executions.append(ex)
        if on_round:
            on_round(plan, ex)
        tried.update(plan.ids)
        # An unconfirmed reserve may have landed. Treat it as held so the run never
        # books a second sitting of the same talk.
        unsure |= {o.session_id for o in ex.outcomes if o.status == "unconfirmed"}
        if ex.schedule:
            run.schedule = ex.schedule
            held_now = set(ex.schedule.reserved) | unsure
        else:
            held_now |= unsure | {o.session_id for o in ex.outcomes if o.status in ("reserved", "already")}
        # 409 means closed. Any other error: read back done, stop rather than push on.
        if ex.closed or ex.error:
            run.closed = ex.closed
            break
    return run
