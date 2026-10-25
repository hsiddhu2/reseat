"""Book one session the attendee tapped Book for: a session just added, or a seat that opened
on a favorite. The watcher offers it with a plan id. Nothing is booked without that tap.

API rules this module exists to respect:
- Reads before writes. A fresh GetSchedule and GetSession come first. A full or walk-up band,
  a clash with a held seat, a second sitting of a held talk, a meal or the daily cap stops it,
  and nothing is sent.
- ReserveSessions is not idempotent. One call for one session, never retried, then a
  GetSchedule read-back. router.execute sends it and journals the request, answer and read-back.
- Writes can return 409. That stops cleanly with nothing more sent.
- Good citizen: only the one session the attendee tapped, never two sittings of one talk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import ApiError, EventsClient
from .router import Plan, Planned, Router, execute, scarcity_rank
from .store import Store

TAPPED = "tapped Book"      # the target label a tapped booking carries in the journal


@dataclass
class Offer:
    """A session the attendee may book with one tap. Approve by plan id, like a swap."""

    plan_id: str
    session_id: str
    reason: str            # added | favorite
    created: float
    expires: float

    def expired(self, now: float) -> bool:
        return now >= self.expires


@dataclass
class BookResult:
    session_id: str
    state: str             # booked | already | refused | full | conflict | closed | unconfirmed | failed
    reasons: list[str] = field(default_factory=list)
    held_now: list[str] = field(default_factory=list)
    alert: str | None = None


def book(client: EventsClient, store: Store, event_id: str, router: Router, session_id: str) -> BookResult:
    """Check, then reserve one session and read it back. ApiError before the reserve means nothing
    was sent, and is raised so the offer can be kept."""
    sched = client.get_schedule(event_id)
    held = set(sched.reserved)
    if session_id in held:
        return BookResult(session_id, "already", held_now=sorted(held))
    try:
        s = client.get_session(event_id, session_id)
    except ApiError as e:
        if e.status == 404:
            return BookResult(session_id, "refused", ["it is no longer in the catalog"], sorted(held))
        raise
    store.update_session(event_id, s)
    why = router.why_not_now(s, held)
    if why:
        return BookResult(session_id, "refused", [why], sorted(held))
    plan = Plan(batch=[Planned(s.session_id, s.abbreviation or s.session_id, s.title, s.type, TAPPED, 0,
                               False, scarcity_rank(s.type))])
    ex = execute(plan, client, store, event_id)
    held_now = sorted(ex.schedule.reserved) if ex.schedule else sorted(held)
    if ex.closed:
        return BookResult(session_id, "closed", ["reservation writes are closed (409). Nothing was sent"],
                          held_now)
    o = ex.outcomes[0] if ex.outcomes else None
    if ex.error and ex.schedule is not None and session_id not in ex.schedule.reserved:
        # The API failed, and the read-back shows no seat: nothing landed, so another tap is safe.
        return BookResult(session_id, "failed", [f"the API answered {ex.error}. The read-back shows no "
                                                 "seat, so tap Book again to retry"], held_now)
    state = {"reserved": "booked", "not_sent": "refused"}.get(o.status, o.status) if o else "unconfirmed"
    alert = None
    if state == "unconfirmed":
        alert = "The reserve was sent but the read-back could not confirm it. Check the official app."
    why = (o.note or (f"the API refused it ({o.code})" if o.code else None)) if o else None
    return BookResult(session_id, state, [why] if why else [], held_now, alert)
