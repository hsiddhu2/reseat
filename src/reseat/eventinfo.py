"""Check the event itself with GetEvent before trusting any session time.

API rules this module exists to respect:
- Session times carry no time zone: `sessionTime.timezone` is never sent. The event's
  `timezone` from GetEvent is the only place the API states it. re:Seat computes every
  leave time and personal-time block in America/Los_Angeles, so an event in another
  zone is refused rather than given wrong times.
- Use the IANA `timezone`, never `timezoneAbbreviation`. On 21 October 2026 the live API
  answered "PDT" for an event that runs in December, when Las Vegas is on PST.
- The day names re:Seat accepts (Monday to Friday) assume 30 November to 4 December 2026.
  The event's own dates are checked against them.
- GetEvent needs no sign-in. An unknown event id is a 404.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import campus
from .client import ApiError, EventsClient
from .models import Event


@dataclass
class EventCheck:
    event: Event | None = None
    problems: list[str] = field(default_factory=list)   # times or days would be wrong: stop
    notes: list[str] = field(default_factory=list)      # worth saying, nothing wrong

    @property
    def ok(self) -> bool:
        return not self.problems

    def line(self) -> str:
        """One line a person reads, such as 're:Invent 2026, Mon 30 Nov to Fri 4 Dec 2026, Las Vegas'."""
        e = self.event
        if e is None:
            return ""
        where = e.address.city if e.address and e.address.city else ("online" if e.is_online else "")
        days = f"{_day(e.start_date)} to {_day(e.end_date)} {e.end_date[:4]}"
        zone = e.timezone or "Las Vegas time"
        return ", ".join(p for p in (e.name, days, where) if p) + f". Times in {zone}."


def _local(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(campus.VEGAS)


def _day(stamp: str) -> str:
    try:
        d = _local(stamp)
    except ValueError:
        return stamp[:10]
    return f"{d:%a} {d.day} {d:%b}"


def check(event: Event) -> EventCheck:
    """No I/O. Compare the event with the zone and days re:Seat's times are built on."""
    res = EventCheck(event=event)
    zone = campus.VEGAS.key
    if not event.timezone:
        res.notes.append(f"The API gave no time zone for {event.event_id}. re:Seat uses {zone}.")
    elif event.timezone != zone:
        res.problems.append(f"{event.event_id} is in {event.timezone}, and re:Seat computes every time in "
                            f"{zone}. Leave times and personal time would be wrong, so nothing was done.")
    try:
        first, last = _local(event.start_date).date().isoformat(), _local(event.end_date).date().isoformat()
    except ValueError:
        res.notes.append(f"The event dates could not be read: {event.start_date} to {event.end_date}.")
    else:
        days = sorted(campus.EVENT_DAYS.values())
        if (first, last) != (days[0], days[-1]):
            res.problems.append(f"{event.event_id} runs {first} to {last}, and re:Seat's day names assume "
                                f"{days[0]} to {days[-1]}. Day names would point at the wrong dates.")
    if event.timezone == zone and event.timezone_abbreviation:
        try:
            actual = _local(event.start_date).tzname()
        except ValueError:
            actual = None
        if actual and event.timezone_abbreviation != actual:
            res.notes.append(f"The API labels the zone {event.timezone_abbreviation}, but on the first day "
                             f"Las Vegas is on {actual}. re:Seat uses the zone, not the label.")
    return res


def fetch(client: EventsClient, event_id: str) -> EventCheck:
    """One GetEvent. A 404 is a problem. Any other failure is a note, so a sync can still run."""
    try:
        event = client.get_event(event_id)
    except ApiError as e:
        if e.status == 404:
            return EventCheck(problems=[f"No event with id {event_id}. Run reseat events to list them."])
        return EventCheck(notes=[f"GetEvent failed, so the event was not checked: {e}"])
    return check(event)
