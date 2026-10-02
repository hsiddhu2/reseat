"""Typed models for the AWS Events API. Mirrors /v1/openapi.json.

Unknown enum values are kept as strings on purpose. The spec says values are
added over time and a client that switches exhaustively will break.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .campus import normalize_venue, room_label


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class SeatBand(StrEnum):
    AVAILABLE = "available"
    LIMITED = "limited"
    VERY_LIMITED = "veryLimited"
    UNAVAILABLE = "unavailable"
    WALK_UP = "walkUp"

    @property
    def open(self) -> bool:
        return self in (SeatBand.AVAILABLE, SeatBand.LIMITED, SeatBand.VERY_LIMITED)


class FailureCode(StrEnum):
    SESSION_NOT_RESERVABLE = "sessionNotReservable"
    SCHEDULE_CONFLICT = "scheduleConflict"
    ALREADY_SCHEDULED = "alreadyScheduled"
    SESSION_FULL = "sessionFull"
    INSUFFICIENT_ACCESS = "insufficientAccess"
    TIME_PASSED = "timePassed"
    ALREADY_FAVORITED = "alreadyFavorited"
    NOT_FAVORITED = "notFavorited"
    OTHER = "other"


# Session type strings seen in the reinvent2026 catalog on 1 Oct 2026, by count.
# The API sends a straight apostrophe in "Builders' session". The planner UI
# shows a curly one (U+2019). normalize_type folds both.
OBSERVED_TYPES = (
    "Chalk talk", "Breakout session", "Workshop", "Builders' session", "Lightning talk",
    "Code talk", "Lab", "Gamified learning", "Bootcamp", "Exam prep",
)


def normalize_type(value: str | None) -> str | None:
    """Fold curly apostrophes and stray spaces so planner and API strings compare equal."""
    if value is None:
        return None
    return " ".join(value.replace("\u2019", "'").replace("\u2018", "'").split())


class EventAddress(_Model):
    city: str | None = None


class Event(_Model):
    event_id: str = Field(alias="eventId")
    name: str
    event_type: str = Field(alias="eventType")
    start_date: str = Field(alias="startDate")
    end_date: str = Field(alias="endDate")
    is_online: bool = Field(alias="isOnline")
    timezone: str | None = None
    timezone_abbreviation: str | None = Field(default=None, alias="timezoneAbbreviation")
    time_format: str | None = Field(default=None, alias="timeFormat")
    address: EventAddress | None = None
    supported_language_codes: list[str] = Field(default_factory=list, alias="supportedLanguageCodes")
    authentication_required: bool = Field(alias="authenticationRequired")


class SessionTime(_Model):
    date: str | None = None
    time: str | None = None
    length: str | None = None
    timezone: str | None = None

    @property
    def minutes(self) -> int | None:
        try:
            return int(self.length) if self.length else None
        except ValueError:
            return None


class Speaker(_Model):
    name: str | None = None


class Session(_Model):
    session_id: str = Field(alias="sessionId")
    title: str
    abbreviation: str | None = None
    abstract: str | None = None
    type: str | None = None
    level: str | None = None
    venue: str | None = None
    room: str | None = None
    is_all_day_session: bool | None = Field(default=None, alias="isAllDaySession")
    is_reservable: bool | None = Field(default=None, alias="isReservable")
    seat_availability: str | None = Field(default=None, alias="seatAvailability")
    session_time: SessionTime | None = Field(default=None, alias="sessionTime")
    speakers: list[Speaker] = Field(default_factory=list)
    tracks: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    areas_of_interest: list[str] = Field(default_factory=list, alias="areasOfInterest")
    roles: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    customer_personas: list[str] = Field(default_factory=list, alias="customerPersonas")
    experiences: list[str] = Field(default_factory=list)
    additional_activities: list[str] = Field(default_factory=list, alias="additionalActivities")
    focus_areas: list[str] = Field(default_factory=list, alias="focusAreas")

    @property
    def band(self) -> SeatBand | None:
        try:
            return SeatBand(self.seat_availability) if self.seat_availability else None
        except ValueError:
            return None

    @property
    def base_code(self) -> str | None:
        """COP324-R -> COP324, COP324-R1 -> COP324. Sittings share a base code.

        Seen in a real planner export on 1 Oct 2026: the first sitting is
        COP324-R and repeats are COP324-R1, COP324-R2. Plain codes have no suffix.
        """
        if not self.abbreviation:
            return None
        return re.sub(r"-R\d*$", "", self.abbreviation.strip())

    @property
    def type_key(self) -> str | None:
        return normalize_type(self.type)

    @property
    def campus_venue(self) -> str | None:
        """One of campus.VENUES. The 2026 catalog leaves venue null and puts it in room."""
        return normalize_venue(self.venue, self.room)

    @property
    def room_label(self) -> str | None:
        return room_label(self.venue, self.room)

    @property
    def is_repeat(self) -> bool:
        return bool(self.abbreviation and re.search(r"-R\d+$", self.abbreviation.strip()))


class PersonalTime(_Model):
    personal_time_id: str = Field(alias="personalTimeId")
    start_date_time: str = Field(alias="startDateTime")
    end_date_time: str = Field(alias="endDateTime")
    title: str
    description: str
    location: str | None = None


class PersonalTimeInput(_Model):
    """UTC, seconds must be 00, length a whole number of 5-minute steps."""

    start_date_time: str = Field(alias="startDateTime")
    end_date_time: str = Field(alias="endDateTime")
    title: str = Field(max_length=128, min_length=1)
    description: str = Field(max_length=250, min_length=1)
    location: str | None = Field(default=None, max_length=255)

    def payload(self) -> dict[str, str]:
        d = self.model_dump(by_alias=True, exclude_none=True)
        return d


class Schedule(_Model):
    reserved: list[str] = Field(default_factory=list)
    favorites: list[str] = Field(default_factory=list)
    personal_time: list[PersonalTime] = Field(default_factory=list, alias="personalTime")


class BulkFailure(_Model):
    session_id: str = Field(alias="sessionId")
    code: str
    conflicts_with: list[str] = Field(default_factory=list, alias="conflictsWith")

    @property
    def known_code(self) -> FailureCode:
        try:
            return FailureCode(self.code)
        except ValueError:
            return FailureCode.OTHER


class BulkResult(_Model):
    successful: list[str] = Field(default_factory=list)
    failed: list[BulkFailure] = Field(default_factory=list)

    def failure_for(self, session_id: str) -> BulkFailure | None:
        return next((f for f in self.failed if f.session_id == session_id), None)


class ListSessionsPage(_Model):
    items: list[Session]
    total_count: float = Field(alias="totalCount")
    next_token: str | None = Field(default=None, alias="nextToken")
