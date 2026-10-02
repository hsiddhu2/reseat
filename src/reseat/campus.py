"""The re:Invent 2026 campus and time conversion.

Venues from the official 2026 FAQ: Caesars Forum, Caesars Palace, Encore,
MGM Grand, The Venetian, Wynn. Mandalay Bay is not on the 2026 campus.

Walking minutes are conservative attendee estimates, not official figures.
They include casino floors and crowds. Attendees report MGM to Wynn takes the
better part of an hour on foot and the north-south shuttle took an hour in 2024.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

VEGAS = ZoneInfo("America/Los_Angeles")

VENUES = ["Caesars Forum", "Caesars Palace", "Encore", "MGM Grand", "The Venetian", "Wynn"]

# Aliases seen in room names and older tools.
_ALIASES = {
    "venetian": "The Venetian",
    "the venetian": "The Venetian",
    "palazzo": "The Venetian",
    "wynn": "Wynn",
    "encore": "Encore",
    "caesars forum": "Caesars Forum",
    "forum": "Caesars Forum",
    "caesars palace": "Caesars Palace",
    "caesars": "Caesars Palace",
    "mgm": "MGM Grand",
    "mgm grand": "MGM Grand",
}

# Minutes on foot between venues, door to door, at conference pace.
_WALK: dict[frozenset[str], int] = {
    frozenset({"The Venetian", "Wynn"}): 15,
    frozenset({"The Venetian", "Encore"}): 20,
    frozenset({"Wynn", "Encore"}): 8,
    frozenset({"The Venetian", "Caesars Forum"}): 20,
    frozenset({"Caesars Forum", "Caesars Palace"}): 12,
    frozenset({"The Venetian", "Caesars Palace"}): 25,
    frozenset({"Wynn", "Caesars Forum"}): 30,
    frozenset({"Encore", "Caesars Forum"}): 35,
    frozenset({"Wynn", "Caesars Palace"}): 35,
    frozenset({"Encore", "Caesars Palace"}): 40,
    frozenset({"MGM Grand", "Caesars Palace"}): 35,
    frozenset({"MGM Grand", "Caesars Forum"}): 40,
    frozenset({"MGM Grand", "The Venetian"}): 50,
    frozenset({"MGM Grand", "Wynn"}): 55,
    frozenset({"MGM Grand", "Encore"}): 60,
}
# re:Invent 2026 runs Monday 30 November to Friday 4 December, Las Vegas local dates.
EVENT_DAYS = {
    "monday": "2026-11-30", "tuesday": "2026-12-01", "wednesday": "2026-12-02",
    "thursday": "2026-12-03", "friday": "2026-12-04",
}
SAME_VENUE_MINUTES = 10
CUTOFF_MINUTES = 11  # AWS says 10. Veterans say 11 to avoid arguing at the door.


# The 2026 catalog names one combined venue "Wynn/Encore". Encore's meeting
# rooms carry composer names and the Encore Ballroom. Wynn's carry wine names.
# Only rooms seen in the 1 Oct 2026 pull are listed. Anything else stays Wynn.
_ENCORE_ROOMS = ("encore ballroom", "brahms", "chopin", "debussy")
_ROOM_SEP = " | "


def _alias(text: str) -> str | None:
    t = text.lower()
    for alias, name in sorted(_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in t:
            return name
    return None


def _wynn_or_encore(room: str | None) -> str:
    r = (room or "").lower()
    return "Encore" if any(k in r for k in _ENCORE_ROOMS) else "Wynn"


def normalize_venue(venue: str | None, room: str | None = None) -> str | None:
    """Map catalog venue and room strings to one of VENUES.

    Seen in the 1 Oct 2026 catalog: `venue` is "MGM Grand", "Caesars Forum",
    "Venetian" or null. When null, the venue is the first segment of `room`,
    as in "Caesars Palace | Promenade Level | Trevi" or
    "Wynn/Encore | Level 1 | Chopin 2".
    """
    for text in (venue, room):
        if not text:
            continue
        head = text.split(_ROOM_SEP, 1)[0]
        if "wynn" in head.lower() and "encore" in head.lower():
            return _wynn_or_encore(room)
        name = _alias(head) or _alias(text)
        if name:
            return name
    return venue


def room_label(venue: str | None, room: str | None) -> str | None:
    """Room without a leading venue segment. "Wynn/Encore | Level 1 | Chopin 2" -> "Level 1 | Chopin 2"."""
    if not room:
        return room
    head, sep, rest = room.partition(_ROOM_SEP)
    if sep and not venue and normalize_venue(head) in VENUES:
        return rest
    return room


def walk_minutes(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    if a == b:
        return SAME_VENUE_MINUTES
    return _WALK.get(frozenset({a, b}))


def event_date(day: str) -> str | None:
    """'Tuesday' or '2026-12-01' -> '2026-12-01'. None if neither."""
    d = day.strip().lower()
    if d in EVENT_DAYS:
        return EVENT_DAYS[d]
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def session_start_utc(date: str, time_: str, tz: ZoneInfo = VEGAS) -> datetime:
    """Catalog gives local date and HH:MM. Return an aware UTC datetime."""
    local = datetime.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    return local.astimezone(UTC)


def session_window(date: str, time_: str, minutes: int) -> tuple[datetime, datetime]:
    start = session_start_utc(date, time_)
    return start, start + timedelta(minutes=minutes)


def to_personal_time(dt: datetime) -> str:
    """Personal time wants YYYY-MM-DDTHH:MM:SS in UTC, no offset, seconds 00."""
    u = dt.astimezone(UTC).replace(second=0, microsecond=0)
    return u.strftime("%Y-%m-%dT%H:%M:%S")


def round_to_5(dt: datetime, up: bool = False) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    r = dt.minute % 5
    if r == 0:
        return dt
    return dt + timedelta(minutes=5 - r) if up else dt - timedelta(minutes=r)


def overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
