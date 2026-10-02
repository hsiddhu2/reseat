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

Proposals live in memory with a random plan id and expire after 10 minutes. An
expired proposal is raised again only when that sitting changes again. Approval
goes through `approve(plan_id)`, never through raw session ids.

The CLI prints them. The phone page and push subscribe the same way.

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
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from .client import ApiError, EventsClient
from .router import Router, run_booking
from .rules import Rules
from .store import Store

MIN_INTERVAL = 30
DEFAULT_INTERVAL = 60
PROPOSAL_TTL = 600   # seconds a proposed plan id stays valid


class WatchError(Exception):
    """The watcher cannot start. The message says what to fix."""


EventKind = Literal["sweep", "booked", "proposed", "swap", "moved", "error"]
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
        self.sleep: Callable[[float], Any] = sleep or self._stop.wait
        self.router = Router(rules, store, event_id)
        self.proposals: dict[str, Proposal] = {}
        self._lock = threading.RLock()
        self._retry: set[str] = set()   # openings a transient failure kept from booking
        self._subscribers: list[Subscriber] = []
        self.subscriber_errors: deque[str] = deque(maxlen=100)

    # ---- events

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

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
                del self.proposals[pid]
            return list(self.proposals.values())

    def take(self, plan_id: str) -> Proposal | None:
        """Remove and return an unexpired proposal. One use only."""
        with self._lock:
            p = self.proposals.pop(plan_id, None)
        return p if p and not p.expired(self.clock()) else None

    def approve(self, plan_id: str) -> Any:
        """Run a pending proposal the attendee approved. The only way in for the phone page and MCP.

        Returns the swapper's result, or None for an unknown, used or expired plan id.
        """
        p = self.take(plan_id)
        if p is None or self.swapper is None:
            return None
        return self._swap(p, approved=True)

    def stop(self) -> None:
        self._stop.set()

    # ---- the loop

    def run(self, max_ticks: int | None = None) -> None:
        """Tick until stop() or max_ticks. A tick that raises anything is reported, not fatal."""
        n = 0
        while not self._stop.is_set() and (max_ticks is None or n < max_ticks):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  the watcher runs all week. Report and go on.
                self._emit("error", message=f"tick failed: {type(e).__name__}: {e}")
            n += 1
            if max_ticks is None or n < max_ticks:
                self.sleep(self.interval)

    def tick(self) -> TickResult:
        now = self.clock()
        res = TickResult(at=now)
        try:
            self._tick(res, now)
        except _ERRORS as e:
            self._problem(res, f"{getattr(e, 'status', type(e).__name__)} {e}")
        self._emit("sweep", count=res.count, added=len(res.added), opened=len(res.opened),
                   moved=len(res.moved), booked=len(res.booked), proposed=len(res.proposals),
                   error=res.error)
        return res

    def _problem(self, res: TickResult, message: str) -> None:
        res.error = f"{res.error}; {message}" if res.error else message
        self._emit("error", message=message)

    def _tick(self, res: TickResult, now: float) -> None:
        baseline = self.store.last_sweep(self.event_id) is None
        sessions = list(self.client.iter_sessions(self.event_id, include_abstracts=False))
        # Read the schedule before saving the sweep. If it fails, the changes stay
        # unsaved and the next tick sees them again, so no opening is lost.
        held = set() if baseline else set(self.client.get_schedule(self.event_id).reserved)
        sweep = self.store.apply_sweep(self.event_id, sessions, with_abstracts=False, now=now)
        res.count = sweep.count
        if baseline:
            return    # first sweep ever: everything looks new. Record it, act from the next one.
        res.added, res.opened, res.moved = sweep.added, sweep.opened, sweep.moved
        self._warn_moved(sweep.moved, held, now)
        prio = self._priorities()
        retry, self._retry = self._retry - held, set()
        candidates = [s for s in dict.fromkeys(sweep.opened + sweep.added + sorted(retry)) if s in prio]
        if not candidates:
            return
        self._propose(candidates, held, prio, res, now)
        run = run_booking(self.router, self.client, self.store, self.event_id, held, candidates=candidates)
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
            self._retry = {c for c in candidates if c not in answered} - set(res.booked)
        if run.closed:
            self._problem(res, "Reservation writes are closed (409). Nothing was reserved.")
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
            t = self.rules.targets[mine]
            p = Proposal(secrets.token_urlsafe(16), t.label, blocker, sk.session_id, now,
                         now + PROPOSAL_TTL, t.auto_swap)
            with self._lock:
                self.proposals[p.plan_id] = p
            open_pairs.add((blocker, sk.session_id))
            res.proposals.append(p)
            self._emit("proposed", plan_id=p.plan_id, target=p.target, held_id=blocker,
                       wanted_id=sk.session_id, auto=p.auto, expires=p.expires)
            if p.auto and self.swapper:
                with self._lock:
                    self.proposals.pop(p.plan_id, None)
                if self._swap(p, approved=True) is None:
                    with self._lock:
                        self.proposals[p.plan_id] = p      # keep it for a manual approve
                    self._problem(res, f"Auto swap for {t.label} did not run. Proposal {p.plan_id} kept.")

    def _swap(self, p: Proposal, approved: bool) -> Any:
        """Run the swapper. Any failure becomes an error event, never a dead watcher."""
        assert self.swapper is not None
        try:
            outcome = self.swapper(p, approved)
        except Exception as e:  # noqa: BLE001  SwapBusy, ApiError, anything: report it
            self._emit("error", message=f"Swap {p.plan_id} not run: {type(e).__name__}: {e}")
            return None
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
