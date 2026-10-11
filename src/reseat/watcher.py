"""The watcher: sweep the catalog, book targets as seats free or new sittings appear.

One `tick()` is one sweep. `run()` ticks every `interval` seconds. Anything
that wants to know what happened subscribes a callback and gets typed events:

    sweep     every tick: counts of sessions, added, opened, moved, booked, proposed
    booked    a target sitting was reserved and read back
    proposed  a better sitting overlaps a held lower-priority target: swap proposal
    swap      an auto swap ran, with its final state
    moved     a held session changed room, venue or time. re:Seat does not act
    error     the tick failed. The next tick tries again

The first sweep into an empty catalog is a baseline. It records state and acts
on nothing, so the watcher never mistakes the whole catalog for new sessions.
Initial booking is `reseat book`.

Outages. The laptop runs all week in a hotel room, so the loop never ends
on a lost connection, a timeout or a 5xx. The retry delay doubles from the normal
interval up to 5 minutes and drops back to the normal interval on recovery. Each
outage is journaled. After 10 minutes down an `offline` event fires, and `back`
on recovery, for push. A failed token refresh fires `signin` and switches to
read-only: nothing is booked or approved until a sweep succeeds again.

Writes closed. A 409 means the API is "intentionally disabled" for writes and
retrying will not help until it is re-enabled. So after a 409 the watcher keeps
sweeping and keeps every opening queued, but sends no reserve and runs no swap for
15 minutes, then tries once. With `probe_session` set it checks every tick with
EventsClient.writes_open() on that session, which can never be held, and resumes
the moment writes open. It emits one `writes` event when writes close and one when
they open again.

Proposals live in memory with a random plan id and expire after 10 minutes. An
expired proposal is raised again only when that sitting changes again. Approval
goes through `approve(plan_id)`, never through raw session ids.

The CLI prints them. The web app and push subscribe the same way.

API rules this module exists to respect:
- ListSessions is 120 per minute. A full sweep is about 9 calls, so the
  interval has a floor of 30 seconds.
- Watch lists are capped by the rules file. A list over the cap is refused.
- Booking goes through the router, so every reserve is quota-sized and read back.
- A held seat is never cancelled here. Swaps are proposed, and run only when the
  target allows auto_swap, through the swap module's checked sequence.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from .auth import AuthError
from .campus import overlaps
from .client import ApiError, AuthRequired, EventsClient, IncompleteCatalog, NetworkError, Throttled
from .models import PersonalTime
from .router import Router, run_booking
from .rules import Rules, unresolved
from .store import Store, SweepRefused

MIN_INTERVAL = 30
DEFAULT_INTERVAL = 60
PROPOSAL_TTL = 600   # seconds a proposed plan id stays valid
BACKOFF_CAP = 300    # longest wait between sweeps during an outage
ONSITE_EVERY = 20    # seconds between on-site GetSession polls
ONSITE_CAP = 40      # sessions polled on site: 40 x 3 a minute stays inside GetSession's 120
CLOSED_RECHECK = 900  # after a 409, wait this long before sending another write
OFFLINE_AFTER = 600  # seconds down before the offline event


class WatchError(Exception):
    """The watcher cannot start. The message says what to fix."""


class ApprovalFailed(Exception):
    """An approved proposal did not run. The proposal is kept, so it can be approved again."""


EventKind = Literal["sweep", "booked", "proposed", "swap", "moved", "error",
                    "outage", "offline", "back", "signin", "leave", "writes"]
_ERRORS = (ApiError, httpx.HTTPError, ValidationError)


@dataclass
class WatchEvent:
    kind: EventKind
    at: float
    data: dict[str, Any]


@dataclass
class Proposal:
    """Replace held session `held_id` with `wanted_id` for `target`. Approve by plan id."""

    plan_id: str
    target: str
    held_id: str
    wanted_id: str
    created: float
    expires: float
    auto: bool

    def expired(self, now: float) -> bool:
        return now >= self.expires


@dataclass
class TickResult:
    at: float
    count: int = 0
    added: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    booked: list[str] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    error: str | None = None
    failure: str | None = None   # outage | auth | other, when the sweep itself failed

    def line(self) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(self.at))
        if self.error:
            return f"{when} sweep failed: {self.error}. Retrying next tick."
        return (f"{when} {self.count} sessions, {len(self.added)} added, {len(self.opened)} opened, "
                f"{len(self.moved)} moved, {len(self.booked)} booked, {len(self.proposals)} proposed")


Subscriber = Callable[[WatchEvent], None]
Swapper = Callable[[Proposal, bool], Any]   # (proposal, approved) -> result with a .state


class Watcher:
    def __init__(self, client: EventsClient, store: Store, rules: Rules, event_id: str,
                 interval: int = DEFAULT_INTERVAL, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None, swapper: Swapper | None = None):
        if interval < MIN_INTERVAL:
            raise WatchError(f"Interval {interval}s is under the {MIN_INTERVAL}s floor. A full sweep "
                             "is about 9 ListSessions calls.")
        if len(rules.targets) > rules.watch_cap:
            raise WatchError(f"{len(rules.targets)} targets, watch_cap is {rules.watch_cap}. "
                             "Trim the rules file.")
        self.client, self.store, self.rules, self.event_id = client, store, rules, event_id
        self._stop = threading.Event()
        self.interval, self.clock, self.swapper = interval, clock, swapper
        self._custom_sleep = sleep
        self._jobs: queue.Queue[tuple[Callable[[], Any], Future[Any]] | None] = queue.Queue()
        self.last_held: set[str] = set()
        self.personal_time: list[PersonalTime] = []   # from the GetSchedule each sweep already reads
        self.router = Router(rules, store, event_id)
        self.proposals: dict[str, Proposal] = {}
        self._lock = threading.RLock()
        self._retry: set[str] = set()   # openings a transient failure kept from booking
        self._cap_warned: str | None = None
        self._down_kind: str | None = None
        self._missing_warned = False
        self.writes_closed_until: float | None = None
        self._closed_announced = False
        self.read_only = False
        self._down_since: float | None = None
        self._down_ticks = 0
        self._offline_sent = False
        self._subscribers: list[Subscriber] = []
        self.subscriber_errors: deque[str] = deque(maxlen=100)

    # ---- events

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    @property
    def stopped(self) -> threading.Event:
        """Set once stop() is called. Other threads wait on it."""
        return self._stop

    @property
    def down_since(self) -> float | None:
        """When the current outage started, or None while the API is reachable."""
        return self._down_since

    def emit(self, kind: EventKind, **data: Any) -> None:
        """Announce an event to every subscriber. The phone server uses it for leave-now."""
        self._emit(kind, **data)

    def _emit(self, kind: EventKind, **data: Any) -> None:
        ev = WatchEvent(kind, self.clock(), data)
        for fn in self._subscribers:
            try:
                fn(ev)
            except Exception as e:  # noqa: BLE001  a broken subscriber must not stop the watcher
                self.subscriber_errors.append(f"{kind}: {e!r}")

    # ---- proposals

    def pending(self) -> list[Proposal]:
        with self._lock:
            now = self.clock()
            for pid in [p for p, x in self.proposals.items() if x.expired(now)]:
                gone = self.proposals.pop(pid)
                if self._closed_announced:
                    # It expired while writes were closed, so it could not be approved.
                    # Queue its sitting so it is proposed again once that is possible.
                    self._retry.add(gone.wanted_id)
            return list(self.proposals.values())

    def is_pending(self, plan_id: str) -> bool:
        """Read-only, safe from any thread: is this plan id a live, unexpired proposal right now?"""
        with self._lock:
            p = self.proposals.get(plan_id)
            return p is not None and not p.expired(self.clock())

    def take(self, plan_id: str) -> Proposal | None:
        """Remove and return an unexpired proposal. One use only."""
        with self._lock:
            p = self.proposals.pop(plan_id, None)
        return p if p and not p.expired(self.clock()) else None

    def approve(self, plan_id: str) -> Any:
        """Run a pending proposal the attendee approved. The only way in for the web app.

        Returns the swapper's result, or None for an unknown, used or expired plan id.
        """
        p = self.take(plan_id)
        if p is None or self.swapper is None:
            return None
        if self.read_only:
            self._keep(p)
            raise ApprovalFailed("Sign in needed on the laptop. Nothing was sent. The proposal is kept.")
        if self._writes_closed():
            self._keep(p)
            raise ApprovalFailed("Reservation writes are closed (409) right now. Nothing was sent. "
                                 "The proposal is kept.")
        outcome = self._swap(p, approved=True)
        if outcome is None:
            self._keep(p)
            raise ApprovalFailed("The swap did not run, nothing was sent. The proposal is kept. "
                                 "Try again in a moment.")
        return outcome

    def _keep(self, p: Proposal) -> None:
        if not p.expired(self.clock()):
            with self._lock:
                self.proposals[p.plan_id] = p

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(None)          # wake the loop

    # ---- jobs: work other threads hand to the loop's thread

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        """Run `fn` on the watcher's own thread between polls, so the API client and the
        store are only ever used from one thread. The HTTP server approves this way."""
        fut: Future[Any] = Future()
        self._jobs.put((fn, fut))
        return fut

    def _run_job(self, item: tuple[Callable[[], Any], Future[Any]] | None) -> None:
        if item is None:
            return
        fn, fut = item
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001  the caller gets the error, the loop goes on
            fut.set_exception(e)

    def drain(self) -> None:
        while True:
            try:
                self._run_job(self._jobs.get_nowait())
            except queue.Empty:
                return

    def _pause(self, seconds: float) -> None:
        """Wait, running submitted jobs as they arrive. A test clock just advances."""
        if self._custom_sleep is not None:
            self._custom_sleep(seconds)
            self.drain()
            return
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                self._run_job(self._jobs.get(timeout=left))
            except queue.Empty:
                return

    # ---- the loop

    def run(self, max_ticks: int | None = None,
            onsite_day: Callable[[], str | None] | None = None) -> None:
        """Sweep until stop() or max_ticks. Between sweeps, if `onsite_day()` names an event
        day, poll that day's sessions every 20 seconds. Nothing raised in a tick is fatal."""
        n = 0
        while not self._stop.is_set() and (max_ticks is None or n < max_ticks):
            self._safely(self.tick)
            n += 1
            if max_ticks is not None and n >= max_ticks:
                return
            left = self.next_delay()
            while left > 0 and not self._stop.is_set():
                day = onsite_day() if onsite_day else None
                # A catalog-only outage still lets GetSession polling run on site.
                if day and (self._down_since is None or self._down_kind == "catalog"):
                    step = min(ONSITE_EVERY, left)
                    self._pause(step)
                    left -= step
                    if left > 0:
                        self._safely(lambda d=day: self.onsite_tick(d))
                else:
                    self._pause(left)
                    left = 0

    def _safely(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001  the watcher runs all week. Report and go on.
            self._emit("error", message=f"tick failed: {type(e).__name__}: {e}")

    def next_delay(self) -> float:
        """Normal interval, or during an outage doubling from it up to 5 minutes."""
        if self._down_since is None:
            return self.interval
        return float(min(self.interval * 2 ** (self._down_ticks - 1), BACKOFF_CAP))

    def tick(self) -> TickResult:
        """One full sweep of the catalog."""
        now = self.clock()
        res = TickResult(at=now)
        self._guarded(res, now, lambda: self._tick(res, now))
        return res

    def onsite_tick(self, day: str) -> TickResult:
        """On site: GetSession for the day's held and wanted sessions, at most 40, instead of a sweep."""
        now = self.clock()
        res = TickResult(at=now)
        self._guarded(res, now, lambda: self._onsite(res, now, day))
        return res

    def _guarded(self, res: TickResult, now: float, body: Callable[[], None]) -> None:
        try:
            body()
        except (AuthError, AuthRequired) as e:
            res.failure = "auth"
            self._problem(res, f"Sign in needed: {e}")
        except (httpx.TransportError, NetworkError, Throttled) as e:
            res.failure = "outage"
            self._problem(res, f"{type(e).__name__}: {e}")
        except ApiError as e:
            res.failure = "outage" if e.status >= 500 else "other"
            self._problem(res, f"{e.status} {e}")
        except (SweepRefused, IncompleteCatalog) as e:
            res.failure = "catalog"       # the API answered, but with no usable catalog
            self._problem(res, str(e))
        except _ERRORS as e:
            res.failure = "other"
            self._problem(res, f"{type(e).__name__}: {e}")
        self._track(res, now)
        self._emit("sweep", count=res.count, added=len(res.added), opened=len(res.opened),
                   moved=len(res.moved), booked=len(res.booked), proposed=len(res.proposals),
                   error=res.error)

    def _track(self, res: TickResult, now: float) -> None:
        """Outage and sign-in state across ticks. Journaled so the attendee can see what happened."""
        if res.failure == "auth" and not self.read_only:
            self.read_only = True
            self.store.journal(self.event_id, "watcher.signin", None, {"error": res.error}, "needed")
            self._emit("signin", state="needed", message="Sign in needed. Run reseat login on the laptop. "
                       "Reads only until then.")
        if res.failure in ("outage", "catalog"):
            self._down_kind = res.failure
            if self._down_since is None:
                self._down_since, self._down_ticks, self._offline_sent = now, 0, False
                self.store.journal(self.event_id, "watcher.outage", None, {"error": res.error}, "start")
                self._emit("outage", since=now, message=res.error)
            self._down_ticks += 1
            if not self._offline_sent and now - self._down_since >= OFFLINE_AFTER:
                self._offline_sent = True
                since = time.strftime("%H:%M", time.localtime(self._down_since))
                why = " (the API serves no usable catalog)" if self._down_kind == "catalog" else ""
                self._emit("offline", since=self._down_since, message=f"re:Seat offline since {since}{why}")
        elif self._down_since is not None and res.failure is None:
            minutes = int((now - self._down_since) // 60)
            self.store.journal(self.event_id, "watcher.outage", None,
                               {"minutes": minutes, "ticks": self._down_ticks}, "end")
            self._emit("back", since=self._down_since, minutes=minutes, announced=self._offline_sent,
                       message="re:Seat back")
            self._down_since, self._down_ticks, self._offline_sent = None, 0, False

    def _problem(self, res: TickResult, message: str) -> None:
        res.error = f"{res.error}; {message}" if res.error else message
        self._emit("error", message=message)

    def _tick(self, res: TickResult, now: float) -> None:
        sessions = list(self.client.iter_sessions(self.event_id, include_abstracts=False))
        self._signed_in()
        # Read the schedule before saving the sweep. If it fails, the changes stay
        # unsaved and the next tick sees them again, so no opening is lost.
        sched = self.client.get_schedule(self.event_id)
        held = set(sched.reserved)
        sweep = self.store.apply_sweep(self.event_id, sessions, with_abstracts=False, now=now)
        res.count = sweep.count
        self.last_held = held
        self.personal_time = list(sched.personal_time)
        if sweep.baseline:
            # New ids are not news: a first, re-keyed or forced catalog. Sessions that kept
            # their id can still open or move, so those are handled as usual.
            self._baseline(sweep, now)
            res.opened, res.moved = sweep.opened, sweep.moved
            self._warn_moved(sweep.moved, held, now)
            self._act(sweep.opened, held, res, now)
            return
        res.added, res.opened, res.moved = sweep.added, sweep.opened, sweep.moved
        self._warn_moved(sweep.moved, held, now)
        self._act(sweep.opened + sweep.added, held, res, now)

    def _baseline(self, sweep: Any, now: float) -> None:
        if sweep.reason == "rekeyed":
            with self._lock:
                self.proposals.clear()         # their ids are gone
            self.store.journal(self.event_id, "watcher.catalog", None,
                               {"replaced": len(sweep.removed), "new": len(sweep.added)}, "rekeyed")
            self._emit("error", message=f"The catalog was re-keyed: {len(sweep.removed)} sessions replaced. "
                       "Recorded as a new baseline. New ids are not treated as new sessions.")
        dead = unresolved(self.rules, self.store, self.event_id)
        if dead:
            self._emit("error", message=f"{len(dead)} ids in the rules file are not in the catalog, for "
                       f"example {dead[0]}. Run reseat rules check.")

    def _onsite(self, res: TickResult, now: float, day: str) -> None:
        sched = self.client.get_schedule(self.event_id)
        held = set(sched.reserved)
        self._signed_in()
        self.last_held = held
        self.personal_time = list(sched.personal_time)
        prio = self._priorities()
        held_codes = {s.base_code for s in (self.store.get(self.event_id, i) for i in held) if s}

        def on_day(sid: str) -> bool:
            s = self.store.get(self.event_id, sid)
            return bool(s and s.session_time and s.session_time.date == day)

        def wanted(sid: str) -> bool:
            s = self.store.get(self.event_id, sid)
            return bool(s and s.base_code not in held_codes)

        ids = sorted(i for i in held if on_day(i)) + [i for i in prio if i not in held and on_day(i)
                                                       and wanted(i)]
        if len(ids) > ONSITE_CAP:
            if self._cap_warned != day:
                self._cap_warned = day
                self._emit("error", message=f"{len(ids)} sessions today, polling the first {ONSITE_CAP} "
                           "to stay inside the GetSession quota.")
            ids = ids[:ONSITE_CAP]
        try:
            for sid in ids:
                try:
                    fresh = self.client.get_session(self.event_id, sid)
                except ApiError as e:
                    if e.status == 404:     # withdrawn: the next full sweep removes it. Poll the rest.
                        continue
                    raise
                change = self.store.update_session(self.event_id, fresh, now=now)
                res.opened += change.opened
                res.moved += change.moved
        except BaseException:
            # The openings already saved would never look new again. Keep them for the next poll.
            self._retry |= set(res.opened)
            raise
        res.count = len(ids)
        self._warn_moved(res.moved, held, now)
        self._act(res.opened, held, res, now)

    def _signed_in(self) -> None:
        if self.read_only:             # an authenticated read just worked: signed in again
            self.read_only = False
            self.store.journal(self.event_id, "watcher.signin", None, None, "ok")
            self._emit("signin", state="ok", message="Signed in again. Booking resumes.")

    def _writes_closed(self) -> bool:
        if self.writes_closed_until is None:
            return False
        now = self.clock()
        # A clock that jumped backwards must not stretch the hold past one recheck.
        self.writes_closed_until = min(self.writes_closed_until, now + CLOSED_RECHECK)
        return now < self.writes_closed_until

    def _closed(self, now: float) -> None:
        """A 409 arrived. Hold reserves and swaps for CLOSED_RECHECK seconds. Say so once."""
        self.writes_closed_until = now + CLOSED_RECHECK
        if not self._closed_announced:
            self._closed_announced = True
            self.store.journal(self.event_id, "watcher.writes", None, {"status": 409}, "closed")
            self._emit("writes", state="closed", message="Booking paused: reservation writes are closed "
                       "(409). re:Seat keeps watching and keeps every opening queued.")

    def _opened(self) -> None:
        """A write was answered by the API without a 409: writes are open."""
        self.writes_closed_until = None
        if self._closed_announced:
            self._closed_announced = False
            self.store.journal(self.event_id, "watcher.writes", None, None, "open")
            self._emit("writes", state="open", message="Booking resumed: reservation writes are open.")

    def _probe(self, held: set[str]) -> None:
        """During a hold, ask the API whether writes are open, with a session that cannot be held."""
        sid = self.rules.probe_session
        if not sid or not self._writes_closed() or sid in held:
            return
        try:
            if self.client.writes_open(self.event_id, sid):
                self._opened()
        except ApiError:
            pass                                     # unclear answer: keep the hold

    def _act(self, changed: list[str], held: set[str], res: TickResult, now: float) -> None:
        """Book or propose for target sittings that opened or appeared, plus any retries."""
        prio = self._priorities()
        retry, self._retry = self._retry - held, set()
        candidates = [s for s in dict.fromkeys(changed + sorted(retry)) if s in prio]
        if not candidates:
            return
        missing = self.router.missing_held(held)
        if missing:
            # Booking around held sessions it cannot see could add a second sitting of a held talk.
            self._retry |= set(candidates)
            if not self._missing_warned:
                self._missing_warned = True
                self._emit("error", message=f"{len(missing)} held sessions are not in the local catalog. "
                           "Booking is paused until a sync finds them.")
            return
        self._missing_warned = False
        self._probe(held)
        if self._writes_closed():
            # Nothing is sent until the recheck time. Proposals for the phone are still made,
            # because making one sends nothing. Auto swaps wait.
            self._propose(candidates, held, prio, res, now)
            self._retry |= set(candidates)
            return
        self._propose(candidates, held, prio, res, now)
        if self._writes_closed():                   # an auto swap just met a 409
            self._retry |= set(candidates)
            return
        run = run_booking(self.router, self.client, self.store, self.event_id, held, candidates=candidates)
        if run.schedule is not None:
            self.last_held = set(run.schedule.reserved)
        for o in run.outcomes:
            if o.status == "reserved":
                res.booked.append(o.session_id)
                s = self.store.get(self.event_id, o.session_id)
                self._emit("booked", session_id=o.session_id, target=o.target,
                           code=s.abbreviation if s else None, title=s.title if s else None)
        if run.closed or any(ex.error for ex in run.executions):
            # A 409 or an error is not an answer about the seat. Try these again next tick.
            # Full, conflict and other refusals are answers, so they are not carried over.
            answered = {o.session_id for o in run.outcomes
                        if o.status not in ("not_sent",) and o.code is not None}
            self._retry |= {c for c in candidates if c not in answered} - set(res.booked)
        if run.closed:
            res.error = res.error or "Reservation writes are closed (409). Nothing was reserved."
            self._closed(now)
        elif any(not ex.error and not ex.closed and ex.schedule is not None for ex in run.executions):
            self._opened()                          # the API answered a write: writes are open
        for ex in run.executions:
            if ex.error and not ex.closed:
                self._problem(res, f"Reserve failed: {ex.error}")
        for o in run.outcomes:
            if o.status == "unconfirmed":
                self._problem(res, f"{o.session_id} may be held: {o.note}. Check your schedule.")

    def _priorities(self) -> dict[str, int]:
        """Session id -> priority index of the first target whose tree holds it."""
        out: dict[str, int] = {}
        for i, t in enumerate(self.rules.targets):
            for s, _ in self.router.tree(t):
                out.setdefault(s.session_id, i)
        return out

    def _propose(self, candidates: list[str], held: set[str], prio: dict[str, int],
                 res: TickResult, now: float) -> None:
        plan = self.router.plan(held, candidates=candidates, quota_left=0)
        settled = set(plan.held_targets) | {p.target for p in plan.deferred}   # held, or bookable now
        index = {t.label: i for i, t in enumerate(self.rules.targets)}
        open_pairs = {(p.held_id, p.wanted_id) for p in self.pending()}
        for sk in plan.skipped:
            if sk.target in settled:
                continue
            blocker = sk.blocker
            if not blocker or blocker not in held or blocker not in prio:
                continue      # never propose cancelling a seat the rules do not cover
            mine = index[sk.target]    # the target that wanted this sitting, not the first tree holding it
            if prio[blocker] <= mine or (blocker, sk.session_id) in open_pairs:
                continue
            if self._other_clashes(sk.session_id, blocker, held):
                continue      # replacing the blocker alone would not free the slot: the swap could never run
            t = self.rules.targets[mine]
            if t.auto_swap and self._writes_closed():
                continue          # an auto swap waits out the hold. The opening stays queued.
            p = Proposal(secrets.token_urlsafe(16), t.label, blocker, sk.session_id, now,
                         now + PROPOSAL_TTL, t.auto_swap)
            with self._lock:
                self.proposals[p.plan_id] = p
            open_pairs.add((blocker, sk.session_id))
            res.proposals.append(p)
            self._emit("proposed", plan_id=p.plan_id, target=p.target, held_id=blocker,
                       wanted_id=sk.session_id, auto=p.auto, expires=p.expires)
            if p.auto and self.swapper and not self._writes_closed():
                with self._lock:
                    self.proposals.pop(p.plan_id, None)
                if self._swap(p, approved=True) is None:
                    with self._lock:
                        self.proposals[p.plan_id] = p      # keep it for a manual approve
                    self._problem(res, f"Auto swap for {t.label} did not run. Proposal {p.plan_id} kept.")

    def _other_clashes(self, wanted: str, blocker: str, held: set[str]) -> bool:
        """True if `wanted` overlaps, or repeats the code of, any held session but `blocker`."""
        w = self.store.get(self.event_id, wanted)
        if not w:
            return True
        ww = self.router._window(w)
        for sid in held - {blocker}:
            other = self.store.get(self.event_id, sid)
            if not other:
                return True
            if w.base_code and other.base_code == w.base_code:
                return True
            wo = self.router._window(other)
            if ww and wo and overlaps(ww, wo):
                return True
        return False

    def _swap(self, p: Proposal, approved: bool) -> Any:
        """Run the swapper. Any failure becomes an error event, never a dead watcher."""
        assert self.swapper is not None
        try:
            outcome = self.swapper(p, approved)
        except Exception as e:  # noqa: BLE001  SwapBusy, ApiError, anything: report it
            self._emit("error", message=f"Swap {p.plan_id} not run: {type(e).__name__}: {e}")
            return None
        if getattr(outcome, "closed", False):
            self._closed(self.clock())
        elif "cancelled" in getattr(outcome, "steps", []):
            self._opened()                          # the cancel was answered: writes are open
        held_now = getattr(outcome, "held_now", None)
        if held_now is not None and getattr(outcome, "state", None) != "proposed":
            self.last_held = set(held_now)
        self._emit("swap", plan_id=p.plan_id, state=getattr(outcome, "state", None),
                   alert=getattr(outcome, "alert", None))
        return outcome

    def _warn_moved(self, moved: list[str], held: set[str], now: float) -> None:
        if not moved:
            return
        rows = {r["session_id"]: r for r in self.store.recent_changes(self.event_id, now)
                if r["kind"] == "moved"}
        for sid in moved:
            if sid not in held:
                continue
            detail = json.loads(rows[sid]["detail"]) if sid in rows and rows[sid]["detail"] else {}
            s = self.store.get(self.event_id, sid)
            keys = ("date", "time", "room", "venue")
            before = {k: detail.get("from", {}).get(k) for k in keys}
            after = {k: detail.get("to", {}).get(k) for k in keys}
            self._emit("moved", session_id=sid, code=s.abbreviation if s else None,
                       before=before, after=after)
