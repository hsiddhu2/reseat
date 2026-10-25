"""Cancel one reservation, safely.

API rules this module exists to respect:
- Reads before writes. GetSession first, so the attendee sees the band before
  giving the seat up. A seat released while the band is unavailable is likely gone.
- CancelReservation is not idempotent. 404 means not held. Never retried.
  When checked on 12 October 2026 the live API answered 400 for a session not held. The read-back decides.
- After the write, read back GetSchedule and journal request, response and read-back.
- Writes return 409 until 8 October 2026.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import ApiError, EventsClient, OperationClosed
from .models import Schedule, Session
from .router import WRITES_CLOSED
from .store import Store

OP = "CancelReservation"


@dataclass
class CancelCheck:
    session: Session
    held: bool
    band_open: bool

    @property
    def band(self) -> str:
        return self.session.seat_availability or "none"


@dataclass
class CancelResult:
    status: str                # cancelled | not_held | closed | error | unconfirmed
    message: str
    schedule: Schedule | None = None


def check(client: EventsClient, event_id: str, session_id: str) -> CancelCheck:
    """Fresh GetSession and GetSchedule, read before any cancel."""
    s = client.get_session(event_id, session_id)
    sched = client.get_schedule(event_id)
    band = s.band
    return CancelCheck(s, session_id in sched.reserved, bool(band and band.open))


def cancel(client: EventsClient, store: Store, event_id: str, session_id: str, *,
           was_held: bool) -> CancelResult:
    """One DELETE, then a read-back. Never retried. `was_held` is what check() saw just before."""
    try:
        client.cancel(event_id, session_id)
    except OperationClosed:
        msg = WRITES_CLOSED.replace("Nothing was reserved.", "Nothing was cancelled.")
        store.journal(event_id, OP, session_id, {"status": 409}, "closed")
        return CancelResult("closed", msg)
    except ApiError as e:
        if e.status == 404:
            store.journal(event_id, OP, session_id, {"status": 404}, "not_held")
            return CancelResult("not_held", "Not held, so nothing was cancelled (404).")
        err, refused = f"{e.status} {e}", e.status == 400
    else:
        err, refused = None, False
    try:
        sched: Schedule | None = client.get_schedule(event_id)
    except ApiError as e:
        sched = None
        err = (err + "; " if err else "") + f"read-back failed: {e.status} {e}"
    gone = sched is not None and session_id not in sched.reserved
    if refused and gone and not was_held:
        res = CancelResult("not_held", f"Not cancelled: the API answered {err}, and GetSchedule does "
                            "not list it.", sched)
    elif err is None and gone:
        res = CancelResult("cancelled", "Cancelled. GetSchedule no longer lists it.", sched)
    elif gone:
        res = CancelResult("cancelled", f"Cancelled despite an error ({err}), per read-back.", sched)
    elif sched is None:
        res = CancelResult("unconfirmed", f"Could not confirm: {err}")
    else:
        res = CancelResult("unconfirmed" if err is None else "error",
                           f"GetSchedule still lists it{'' if err is None else f' ({err})'}.", sched)
    store.journal(event_id, OP, session_id, {
        "error": err, "readBack": sched.model_dump(by_alias=True) if sched else None}, res.status)
    return res


def probe(client: EventsClient, store: Store, event_id: str, session_id: str,
          last: str | None = None) -> str:
    """Ask whether writes are open by cancelling a session the attendee does not hold. Journaled.

    Returns "closed", "open" or "cancelled". The probe is a real CancelReservation call, so it is
    journaled with its answer: 409 is closed, 400 or 404 is open (the live API answered 400 on
    12 October 2026). A watcher probing every minute through a long hold passes the last answer
    as `last`, and a repeat of it is not journaled again, so the journal keeps its real writes.
    A 204 means the session was held and is now cancelled. Callers check the schedule first so
    that never happens. If it does, it is journaled and read back like any cancel. Any other
    answer is journaled and raised. Never retried.
    """
    try:
        client.cancel(event_id, session_id)
    except OperationClosed:
        if last != "closed":
            store.journal(event_id, OP, session_id, {"status": 409, "probe": True}, "probe-closed")
        return "closed"
    except ApiError as e:
        if e.status in (400, 404):
            if last != "open":
                store.journal(event_id, OP, session_id, {"status": e.status, "probe": True}, "probe-open")
            return "open"
        store.journal(event_id, OP, session_id, {"status": e.status, "error": str(e), "probe": True},
                      "probe-unclear")
        raise
    store.journal(event_id, OP, session_id, {"status": 204, "probe": True}, "probe-cancelled")
    try:
        after = client.get_schedule(event_id)
        store.journal(event_id, "GetSchedule", session_id,
                      {"held": session_id in after.reserved, "probe": True}, "readback-ok")
    except ApiError as e:
        store.journal(event_id, "GetSchedule", session_id, {"error": str(e)}, "readback-failed")
    return "cancelled"
