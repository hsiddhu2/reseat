"""Safe swap: replace held session A with wanted session B without losing a seat.

The API has no swap. Reserving B while A overlaps it is refused, so A must be
cancelled first, and in that gap the seat in A can go to someone else. This
module makes that gap as safe as the API allows:

    proposed -> checked -> cancelled -> reserved -> verified
                                     \\-> rolled_back   (B failed, A re-reserved)
                                     \\-> failed        (B failed, A gone, A's fallbacks tried)
    any step can end in failed, with what is held now read back and reported

API rules this module exists to respect:
- Reads before writes. GetSession(B) fresh with an open band, and at least one
  fallback for A (A's own band open, or another open sitting of A that clashes
  with nothing else held) before any cancel.
- Ask first. A cancel happens only when the caller passes approved=True: the
  attendee confirmed, or the target allows auto_swap. Remote surfaces (phone
  page, MCP) reach a swap only through `run_plan` with a plan id from the watcher.
- Never blind-retry. Each write is sent once. GetSchedule after each reserve decides.
  When that read-back fails the state is unknown, so no further write is sent.
  The read-back after reserving B also confirms the cancel of A, so nothing slows
  the gap between the two.
- Never hold two sittings of one talk. B and any fallback must not repeat a held
  code or overlap any held session but A.
- One swap in flight at a time, across processes, through a lock in the store.
  Every transition is journaled before and after the call.
- Writes return 409 until 8 October 2026. A 409 on the cancel leaves A held.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from .campus import overlaps, session_start_utc
from .client import ApiError, EventsClient, OperationClosed
from .models import Schedule, Session
from .rules import Rules
from .store import Store

if TYPE_CHECKING:
    from .watcher import Proposal

_IN_FLIGHT = threading.Lock()
LOCK_NAME = "swap"


class SwapBusy(Exception):
    """Another swap is in flight. One at a time."""


@dataclass
class Preview:
    """What `check` would find, judged from the local catalog. Display only."""

    band: str | None
    band_open: bool
    fallbacks: list[tuple[str, str | None]] = field(default_factory=list)   # (session id, band)
    clashes: list[str] = field(default_factory=list)


@dataclass
class SwapResult:
    held_id: str
    wanted_id: str
    state: str = "proposed"
    steps: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)   # why preconditions failed
    fallbacks: list[str] = field(default_factory=list)
    held_now: list[str] = field(default_factory=list)
    alert: str | None = None
    closed: bool = False      # the API answered 409: writes are closed, nothing changed

    @property
    def fallback_id(self) -> str | None:
        return self.fallbacks[0] if self.fallbacks else None

    @property
    def ok(self) -> bool:
        return self.state == "verified"


class Swap:
    def __init__(self, client: EventsClient, store: Store, rules: Rules, event_id: str):
        self.client, self.store, self.rules, self.event_id = client, store, rules, event_id

    # ---- journal

    def _log(self, res: SwapResult, step: str, phase: str, response: object = None) -> None:
        self.store.journal(self.event_id, f"swap.{step}", {"held": res.held_id, "wanted": res.wanted_id},
                           response, f"{phase}:{res.state}")

    def _go(self, res: SwapResult, state: str, response: object = None) -> None:
        res.state = state
        res.steps.append(state)
        self._log(res, state, "state", response)

    # ---- preconditions

    def check(self, held_id: str, wanted_id: str, *, approved: bool) -> SwapResult:
        """Fresh reads only. No writes. Fills `reasons` when the cancel must not happen.

        A read that fails is journaled as failed, then raised. Nothing was cancelled.
        """
        res = SwapResult(held_id, wanted_id)
        self._go(res, "proposed")
        try:
            self._checks(res, approved)
        except Exception as e:  # noqa: BLE001  journal the end state, then let the caller see it
            self._go(res, "failed", {"read": getattr(e, "operation", type(e).__name__),
                                     "status": getattr(e, "status", None)})
            raise
        return res

    def decline(self, res: SwapResult, why: object = None) -> None:
        """The attendee said no after the checks. Journal it so the swap has an end state."""
        self._go(res, "failed", why if why is not None else {"declined": True})

    def end_proposal(self, res: SwapResult, state: str, why: object) -> None:
        """A proposer stopped after the checks, with nothing sent: `planned` (a plan id waits for
        approval, which runs its own checked swap) or `declined` (the checks failed)."""
        self._go(res, state, why)

    def _checks(self, res: SwapResult, approved: bool) -> None:
        held_id, wanted_id = res.held_id, res.wanted_id
        sched = self.client.get_schedule(self.event_id)
        b = self.client.get_session(self.event_id, wanted_id)
        a = self.client.get_session(self.event_id, held_id)
        if held_id not in sched.reserved:
            res.reasons.append(f"{held_id} is not held")
        if wanted_id in sched.reserved:
            res.reasons.append(f"{wanted_id} is already held")
        if not (b.band and b.band.open):
            band = b.seat_availability or "none"
            res.reasons.append(f"{b.abbreviation or wanted_id} band is {band}, not open")
        if b.is_reservable is False:
            res.reasons.append(f"{b.abbreviation or wanted_id} does not take reservations")
        res.reasons += self._clashes(b, held_id, sched.reserved)
        res.fallbacks = self._fallbacks(a, sched)
        if not res.fallbacks:
            res.reasons.append(f"{a.abbreviation or held_id} has no fallback: its band is not open and "
                               "no other sitting is open and free of clashes")
        if not approved:
            res.reasons.append("not approved, and the target does not allow auto_swap")
        if not res.reasons:
            self._go(res, "checked", {"band": b.seat_availability, "fallbacks": res.fallbacks})

    def _clashes(self, s: Session, except_id: str, held: Iterable[str]) -> list[str]:
        """Reasons `s` cannot be held next to everything held except `except_id`."""
        out = []
        ws = _window(s)
        for sid in held:
            if sid == except_id:
                continue
            other = self.store.get(self.event_id, sid)
            if not other:
                out.append(f"held {sid} is not in the local catalog. Run reseat sync first")
                continue
            if s.base_code and other.base_code == s.base_code:
                out.append(f"a sitting of {s.base_code} is already held ({sid})")
            wo = _window(other)
            if ws and wo and overlaps(ws, wo):
                out.append(f"{s.abbreviation or s.session_id} also overlaps held {other.abbreviation or sid}")
        return out

    def _fallbacks(self, a: Session, sched: Schedule) -> list[str]:
        """A itself when its band is open, then every other open sitting of A that clashes
        with nothing held but A. Each read fresh."""
        out = [a.session_id] if a.band and a.band.open else []
        for s in self.store.by_base_code(self.event_id, a.base_code or ""):
            if s.session_id == a.session_id:
                continue
            fresh = self.client.get_session(self.event_id, s.session_id)
            if not (fresh.band and fresh.band.open) or fresh.is_reservable is False:
                continue
            if self._clashes(fresh, a.session_id, sched.reserved):
                continue
            out.append(fresh.session_id)
        return out

    def preview(self, held_id: str, wanted_id: str, held: Iterable[str]) -> Preview:
        """The same checks as `check`, from the local catalog only. No API call, no journal.

        For a page that shows a proposal before anyone approves it. Approving still runs
        `check` with fresh reads, so the preview never decides anything.
        """
        held = list(held)
        a, b = self.store.get(self.event_id, held_id), self.store.get(self.event_id, wanted_id)
        pv = Preview(band=b.seat_availability if b else None, band_open=bool(b and b.band and b.band.open))
        if not a or not b:
            pv.clashes.append("not in the local catalog. Run reseat sync")
            return pv
        pv.clashes = self._clashes(b, held_id, held)
        if a.band and a.band.open:
            pv.fallbacks.append((a.session_id, a.seat_availability))
        for s in self.store.by_base_code(self.event_id, a.base_code or ""):
            if (s.session_id != a.session_id and s.band and s.band.open and s.is_reservable is not False
                    and not self._clashes(s, a.session_id, held)):
                pv.fallbacks.append((s.session_id, s.seat_availability))
        return pv

    # ---- the swap

    def run_plan(self, plan: Proposal, *, approved: bool) -> SwapResult:
        """The only entry for remote surfaces: a proposal the watcher made and handed over."""
        return self.run(plan.held_id, plan.wanted_id, approved=approved)

    def run(self, held_id: str, wanted_id: str, *, approved: bool) -> SwapResult:
        owner = f"{os.getpid()}:{threading.get_ident()}"
        if not _IN_FLIGHT.acquire(blocking=False):
            raise SwapBusy("Another swap is in flight. One at a time.")
        try:
            if not self.store.acquire_lock(LOCK_NAME, owner):
                raise SwapBusy("Another re:Seat process is running a swap. One at a time.")
            try:
                res = self.check(held_id, wanted_id, approved=approved)
                if res.reasons:
                    self._go(res, "failed", {"preconditions": res.reasons})
                    res.held_now = self._held() or []
                    return res
                return self._execute(res)
            finally:
                self.store.release_lock(LOCK_NAME, owner)
        finally:
            _IN_FLIGHT.release()

    def _execute(self, res: SwapResult) -> SwapResult:
        a, b = res.held_id, res.wanted_id
        self._log(res, "cancel", "before")
        try:
            self.client.cancel(self.event_id, a)
        except OperationClosed:
            res.closed = True
            self._log(res, "cancel", "after", {"status": 409})
            self._go(res, "failed", {"cancel": 409})
            res.alert = f"Writes are closed (409). Nothing changed. {a} is still held."
            res.held_now = self._held() or []
            return res
        except ApiError as e:
            held = self._held()
            self._log(res, "cancel", "after", {"status": e.status, "readBack": held})
            if held is None or a in held:
                self._go(res, "failed", {"cancel": e.status, "readBack": held})
                res.alert = (f"Cancel failed ({e.status}). " + (f"{a} is still held." if held is not None
                             else "Could not read the schedule back. Check it before doing anything."))
                res.held_now = held or []
                return res
            # The cancel landed despite the error. Carry on to reserve B.
        else:
            self._log(res, "cancel", "after", {"status": 200})
        self._go(res, "cancelled")

        landed, response = self._reserve(res, b, "reserve")
        if landed is None:
            return self._unknown(res, b)
        if landed:
            self._go(res, "reserved", response)
            self._go(res, "verified")
            res.held_now = self._held() or []
            return res
        if res.closed:
            return self._stopped(res, b)

        landed, response = self._reserve(res, a, "rollback")
        if landed is None:
            return self._unknown(res, a)
        if res.closed:
            return self._stopped(res, a)
        if landed:
            self._go(res, "rolled_back", response)
            res.held_now = self._held() or []
            return res

        tried: list[str] = []
        for fb in [f for f in res.fallbacks if f != a]:
            tried.append(fb)
            landed, response = self._reserve(res, fb, "fallback")
            if landed is None:
                return self._unknown(res, fb)
            if res.closed:
                return self._stopped(res, fb)
            if landed:
                return self._failed(res, f"Fallback {fb} is now held", fb)
        then = f"Fallback {', '.join(tried)} also failed" if tried else "No other sitting to fall back to"
        return self._failed(res, then, None)

    def _failed(self, res: SwapResult, then: str, got: str | None) -> SwapResult:
        self._go(res, "failed", {"fallback_held": got})
        res.held_now = self._held() or []
        res.alert = (f"SWAP FAILED. {res.held_id} was released and could not be re-reserved. {then}. "
                     f"Held now: {', '.join(res.held_now) or 'nothing'}.")
        return res

    def _stopped(self, res: SwapResult, sid: str) -> SwapResult:
        """Writes closed (409) after A was cancelled. Every further write would get the same 409,
        so none is sent. A is released. Say exactly that."""
        a = res.held_id
        self._go(res, "failed", {"reserve": 409, "session": sid})
        res.held_now = self._held() or []
        res.alert = (f"SWAP STOPPED. Reservation writes closed (409) after {a} was cancelled, on the "
                     f"reserve of {sid}. {a} is released and nothing more was sent. Reserve {a} again "
                     f"when writes reopen. Held now: {', '.join(res.held_now) or 'nothing'}.")
        return res

    def _unknown(self, res: SwapResult, sid: str) -> SwapResult:
        """The read-back after reserving `sid` failed. Send nothing more."""
        self._go(res, "failed", {"unknown_after": sid})
        res.alert = (f"SWAP STATE UNKNOWN. {res.held_id} was cancelled and {sid} was sent, but the schedule "
                     "could not be read back. Nothing more was sent. Check your schedule now.")
        return res

    def _reserve(self, res: SwapResult, sid: str, step: str) -> tuple[bool | None, object]:
        """One reserve, then read back. True if GetSchedule lists `sid`, None if the read failed."""
        self._log(res, step, "before", {"session": sid})
        response: object
        try:
            response = self.client.reserve(self.event_id, [sid]).model_dump(by_alias=True)
        except OperationClosed as e:
            res.closed = True                 # writes were switched off mid-swap
            response = {"status": 409, "message": str(e)}
        except ApiError as e:
            response = {"status": e.status, "message": str(e)}
        held = self._held()
        landed = None if held is None else sid in held
        self._log(res, step, "after", {"response": response, "readBack": held, "landed": landed})
        return landed, response

    def _held(self) -> list[str] | None:
        """Read back. None when the read itself failed: unknown, not empty."""
        try:
            return sorted(self.client.get_schedule(self.event_id).reserved)
        except ApiError:
            return None


def _window(s: Session):
    st = s.session_time
    if not st or not st.date or not st.time:
        return None
    start = session_start_utc(st.date, st.time)
    return start, start + timedelta(minutes=st.minutes or 60)
