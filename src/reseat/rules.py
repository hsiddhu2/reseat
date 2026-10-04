"""The attendee's rules file, ~/.reseat/rules.yaml.

re:Seat reserves only what this file asks for. Targets are in priority order.
The file is capped (watch_cap) so re:Seat never watches or books a long list
on one attendee's behalf.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .campus import VENUES, event_date, normalize_venue, session_window
from .store import Store

_REPEAT_SUFFIX = re.compile(r"-R\d*$")


class RulesError(Exception):
    """The rules file cannot be used. The message says what to fix."""


def base_code(code: str) -> str:
    """ARC301-R1 -> ARC301. Same rule as Session.base_code."""
    return _REPEAT_SUFFIX.sub("", code.strip()).upper()


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    session_id: str | None = None
    repeats: bool = True
    sittings: list[str] = Field(default_factory=list)
    backups: list[str] = Field(default_factory=list)
    auto_swap: bool = False
    prefer: Literal["earliest", "latest"] = "earliest"
    seat_capacity: int | None = None
    title: str | None = None

    @field_validator("code")
    @classmethod
    def _base(cls, v: str | None) -> str | None:
        return base_code(v) if v else v

    @field_validator("backups")
    @classmethod
    def _backup_bases(cls, v: list[str]) -> list[str]:
        return [base_code(c) for c in v]

    @model_validator(mode="after")
    def _needs_id(self) -> Target:
        if not self.code and not self.session_id:
            raise ValueError("each target needs a code or a session_id")
        if not self.repeats and not self.session_id and not self.sittings:
            raise ValueError("repeats: false needs a session_id or sittings to say which sitting")
        return self

    @property
    def label(self) -> str:
        return self.code or self.session_id or "?"


class Meal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    start: str
    end: str

    @field_validator("day", mode="before")
    @classmethod
    def _day_text(cls, v: Any) -> Any:
        # YAML reads an unquoted 2026-12-01 as a date.
        return v.isoformat() if hasattr(v, "isoformat") else v

    @field_validator("start", "end", mode="before")
    @classmethod
    def _clock_text(cls, v: Any) -> Any:
        # YAML 1.1 reads an unquoted 12:00 as the base-60 integer 720.
        if isinstance(v, int) and not isinstance(v, bool):
            return f"{v // 60:02d}:{v % 60:02d}"
        return v

    @model_validator(mode="after")
    def _times(self) -> Meal:
        if event_date(self.day) is None:
            raise ValueError(f"meal day {self.day!r} is not a weekday name or YYYY-MM-DD")
        for t in (self.start, self.end):
            datetime.strptime(t, "%H:%M")
        if self.end <= self.start:
            raise ValueError(f"meal on {self.day} ends before it starts")
        return self

    def window(self) -> tuple[datetime, datetime]:
        date = event_date(self.day) or self.day
        a = datetime.strptime(self.start, "%H:%M")
        b = datetime.strptime(self.end, "%H:%M")
        return session_window(date, self.start, int((b - a).total_seconds() // 60))


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[Target] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)
    buffer_minutes: int = Field(default=30, ge=0, le=180)
    max_per_day: int = Field(default=5, ge=1, le=20)
    watch_cap: int = Field(default=25, ge=1, le=100)
    home_venue: str | None = None   # where the first walk of each day starts, e.g. your hotel
    serve_secret: str | None = Field(default=None, min_length=16, max_length=200)
    ntfy_topic: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{16,64}$")
    probe_session: str | None = None   # a session you can never hold, used to check if writes are open

    @field_validator("home_venue")
    @classmethod
    def _venue(cls, v: str | None) -> str | None:
        if v is None:
            return v
        name = normalize_venue(v)
        if name not in VENUES:
            raise ValueError(f"home_venue {v!r} is not on the 2026 campus: {', '.join(VENUES)}")
        return name

    @model_validator(mode="after")
    def _limits(self) -> Rules:
        if len(self.targets) > self.watch_cap:
            raise ValueError(f"{len(self.targets)} targets, watch_cap is {self.watch_cap}. "
                             "Trim the list. re:Seat caps watch lists to be a good citizen")
        seen: dict[str, int] = {}
        for i, t in enumerate(self.targets, 1):
            key = t.code or t.session_id or ""
            if key in seen:
                raise ValueError(f"target {i} repeats {key}, already target {seen[key]}. "
                                 "List each talk once and use sittings or repeats for its sittings")
            seen[key] = i
        return self


# ---------------------------------------------------------------------- load and save


def parse(text: str, source: str = "rules") -> Rules:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise RulesError(f"{source} is not valid YAML{where}: {getattr(e, 'problem', e)}") from None
    if not isinstance(raw, dict):
        raise RulesError(f"{source} must be a mapping with a targets list")
    try:
        return Rules.model_validate(raw)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(p) for p in err['loc']) or 'rules'}: {err['msg']}" for err in e.errors()]
        raise RulesError(f"{source} has problems:\n  " + "\n  ".join(msgs)) from None


def load(path: Path) -> Rules:
    if not path.exists():
        raise RulesError(f"No rules file at {path}. Run: reseat rules init")
    return parse(path.read_text(encoding="utf-8"), str(path))


def dump(rules: Rules) -> str:
    data = rules.model_dump(exclude_defaults=True)
    data.setdefault("targets", [])
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def save(rules: Rules, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(rules), encoding="utf-8")


EXAMPLE = """\
# re:Seat rules. Targets are in priority order: the first one wins a time slot.
# re:Seat reserves only what is listed here, never two sittings of one talk.

targets:
  - code: ARC301            # base code. Any sitting (ARC301-R, -R1, -R2) may be booked.
    backups: [ARC302]       # tried in order if no sitting of ARC301 can be held
  - code: DOP302
    prefer: latest          # try later sittings first. Default: earliest
  - session_id: 1780442277219001cKCh   # one exact sitting, never a repeat
    repeats: false
    auto_swap: false        # ask before any cancel. This is the default

meals:
  - {day: Tuesday, start: "12:00", end: "13:00"}   # Las Vegas local time

buffer_minutes: 30   # extra time before a session after a venue change
max_per_day: 5       # reserved sessions per day, at most
watch_cap: 25        # targets, at most
home_venue: Venetian # first walk of each day starts here. Any 2026 campus venue

# Phone remote. Both optional.
# serve_secret: a long random string. Needed to open the phone page from another device.
# ntfy_topic: a random 16 to 64 character name. Push goes to https://ntfy.sh/<topic>.
# probe_session: id of a session that takes no reservations, so it can never be held.
#   While writes are closed, re:Seat checks with it once a minute and resumes at once.
"""


# ---------------------------------------------------------------------- planner import


def from_planner_export(items: list[dict[str, Any]], watch_cap: int = 25) -> Rules:
    """Read a reinvent-planner.cloud JSON export, keeping its order.

    `id` is the Events API sessionId. `shortId` gives the code. `repeats[].id`
    are the other allowed sittings. `seatCapacity` is kept because the Events
    API does not expose room size and queue-or-go advice can use it. Two
    entries for one talk merge into one target at the first one's position.
    """
    targets: dict[str, Target] = {}
    for item in items:
        sid, short = item.get("id"), item.get("shortId")
        if not sid or not short:
            raise RulesError("planner export entry without id or shortId")
        code = base_code(short)
        sittings = [sid] + [r["id"] for r in item.get("repeats", []) if r.get("id")]
        if code in targets:
            t = targets[code]
            t.sittings = list(dict.fromkeys(t.sittings + sittings))
            continue
        targets[code] = Target(code=code, session_id=sid, sittings=sittings,
                               seat_capacity=item.get("seatCapacity"), title=item.get("title"))
    try:
        return Rules(targets=list(targets.values()), watch_cap=watch_cap)
    except ValidationError as e:
        raise RulesError(e.errors()[0]["msg"].removeprefix("Value error, ")) from None


def read_planner_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RulesError(f"{path} is not JSON: {e}") from None
    if not isinstance(data, list):
        raise RulesError(f"{path} must be a JSON list, as reinvent-planner.cloud exports it")
    return data


# ---------------------------------------------------------------------- check


def unresolved(rules: Rules, store: Store, event_id: str) -> list[str]:
    """Codes and ids in the rules that the local catalog does not have."""
    out: list[str] = []
    for t in rules.targets:
        if t.code and not store.by_base_code(event_id, t.code):
            out.append(f"{t.label}: code {t.code} not in catalog")
        if t.session_id and not store.get(event_id, t.session_id):
            out.append(f"{t.label}: session_id {t.session_id} not in catalog")
        for sid in t.sittings:
            if not store.get(event_id, sid):
                out.append(f"{t.label}: sitting {sid} not in catalog")
        for b in t.backups:
            if not store.by_base_code(event_id, b):
                out.append(f"{t.label}: backup {b} not in catalog")
    return out
