"""In-process fake of the AWS Events API for tests and dry runs.

It implements every operation in /v1/openapi.json with the same status codes,
error bodies and per-session bulk results. Faults can be scripted per session:

    fake.full.add("S1")                 -> reserve S1 returns sessionFull
    fake.closed = True                  -> all writes return 409
    fake.throttle_next("ReserveSessions", retry_after=3)
    fake.fail_next("ReserveSessions", 503)  -> one 5xx, nothing applied. times=3 for a storm
    fake.ghost.add("S1")                -> reserve or favorite says successful, GetSchedule omits S1
    fake.refuse["S1"] = "seatHeldByCrew" -> any failure code on reserve or favorite, known or not
    fake.schedule.reserved.add("S9")    -> conflicts computed from session times
    FakeEventsApi.from_fixture("tests/fixtures/catalog-2026-10-01.json")  -> real shapes
    fake.storm(clock, full_rate=0.3, fail_503_at=40, closed=(t0, t1))  -> a fault storm, see storm()

Use it as an httpx transport:  EventsClient(transport=fake.transport())
"""

from __future__ import annotations

import json
import random
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from .campus import overlaps, session_start_utc
from .models import Session


@dataclass
class FakeSchedule:
    reserved: set[str] = field(default_factory=set)
    favorites: set[str] = field(default_factory=set)
    personal_time: dict[str, dict] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)   # GetSchedule lists these first, in this order

    def listed(self, ids: set[str]) -> list[str]:
        """The API's order is not documented as sorted. Tests set `order` to prove it is kept."""
        rank = {sid: i for i, sid in enumerate(self.order)}
        return sorted(ids, key=lambda sid: (rank.get(sid, len(rank)), sid))


class FakeEventsApi:
    def __init__(self, event_id: str = "reinvent2026", sessions: list[Session] | None = None,
                 page_size: int = 250, require_auth: bool = True):
        self.event_id = event_id
        self.sessions: dict[str, Session] = {s.session_id: s for s in (sessions or [])}
        self.page_size = page_size
        self.require_auth = require_auth
        self.schedule = FakeSchedule()
        self.closed = False            # 409 on every write
        self.full: set[str] = set()    # sessionFull
        self.not_reservable: set[str] = set()
        self.no_access: set[str] = set()
        self.time_passed: set[str] = set()
        self.ghost: set[str] = set()   # reported successful, never stored
        self.refuse: dict[str, str] = {}  # session -> any failure code, known or not
        self._throttle: dict[str, int] = {}
        self._fail: dict[str, tuple[int, int]] = {}
        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict[str, str]]] = []
        self.counts: dict[str, int] = defaultdict(int)
        self._pt_seq = 0
        self.token = "test-token"
        self.log: list[tuple[str, int]] = []          # (operation, status) for every request
        self._storm: dict[str, object] | None = None
        self.quota_violations: list[str] = []
        self.two_sittings: list[str] = []             # held states with two sittings of one code
        self.full_draws = 0                           # reserves the storm refused as full

    @classmethod
    def from_fixture(cls, path: str | Path, **kwargs: Any) -> FakeEventsApi:
        """Serve a real catalog pull saved by `reseat save-fixture`."""
        from .fixtures import load_catalog
        return cls(sessions=load_catalog(path), **kwargs)

    # ------------------------------------------------------------------ helpers

    def throttle_next(self, op: str, retry_after: int = 5) -> None:
        self._throttle[op] = retry_after

    def storm(self, clock: Callable[[], float], seed: int = 7, full_rate: float = 0.3,
              fail_503_at: int | None = 40, closed: tuple[float, float] | None = None,
              throttle_every_minute: bool = True) -> None:
        """A fault storm. Every random draw comes from `seed`, so a failing run repeats.

        - `full_rate` of reserve attempts come back sessionFull, even when the band is open.
        - The first request in each minute of `clock` gets a 429 with Retry-After 2.
        - Request number `fail_503_at` gets a 503.
        - Writes return 409 while `closed[0] <= clock() < closed[1]`.
        - The real per-operation quotas are enforced: a request over the quota left in
          the last 60 seconds gets a 429 and is recorded in `quota_violations`, because
          a correct client never sends one.
        """
        self._storm = {"clock": clock, "rng": random.Random(seed), "full_rate": full_rate,
                       "fail_503_at": fail_503_at, "closed": closed, "minutes": set(),
                       "throttle": throttle_every_minute, "used": defaultdict(deque), "n": 0}

    def _storm_check(self, req: httpx.Request, op: str) -> httpx.Response | None:
        st = self._storm
        if st is None:
            return None
        from .client import QUOTAS
        now = st["clock"]()  # type: ignore[operator]
        st["n"] = int(st["n"]) + 1  # type: ignore[call-overload]
        minute = int(now // 60)
        if st["fail_503_at"] is not None and st["n"] == st["fail_503_at"]:
            return self._err(503, "Service unavailable. Back off and retry.")
        if st["throttle"] and minute not in st["minutes"]:  # type: ignore[operator]
            st["minutes"].add(minute)  # type: ignore[union-attr]
            return self._err(429, "ThrottlingException", headers={"Retry-After": "2"})
        closed = st["closed"]
        if closed and closed[0] <= now < closed[1] and req.method != "GET":  # type: ignore[index]
            return self._err(409, "This operation is not accepting requests at this time.")
        if op in QUOTAS:
            units = 1
            if op in ("ReserveSessions", "AssociateFavorites"):
                units = len(json.loads(req.content or b"{}").get("sessionIds", [])) or 1
            used = st["used"][op]  # type: ignore[index]
            while used and used[0][0] <= now - 60:
                used.popleft()
            if sum(u for _, u in used) + units > QUOTAS[op]:
                self.quota_violations.append(f"{op} at {now:.0f}: {units} over the quota left")
                return self._err(429, "ThrottlingException", headers={"Retry-After": "60"})
            used.append((now, units))
        return None

    def _full_draw(self) -> bool:
        st = self._storm
        full = bool(st and st["rng"].random() < st["full_rate"])  # type: ignore[union-attr,operator]
        self.full_draws += full
        return full

    def fail_next(self, op: str, status: int = 503, times: int = 1) -> None:
        """The next `times` calls to `op` return `status`, with nothing applied."""
        self._fail[op] = (status, times)

    def transport(self) -> httpx.BaseTransport:
        return httpx.MockTransport(self._handle)

    def set_band(self, session_id: str, band: str | None) -> None:
        s = self.sessions[session_id]
        self.sessions[session_id] = s.model_copy(update={"seat_availability": band})

    def add_session(self, s: Session) -> None:
        self.sessions[s.session_id] = s

    def _window(self, sid: str):
        s = self.sessions[sid]
        st = s.session_time
        if not st or not st.date or not st.time:
            return None
        start = session_start_utc(st.date, st.time)
        return start, start + timedelta(minutes=st.minutes or 60)

    def _conflicts(self, sid: str) -> list[str]:
        w = self._window(sid)
        if not w:
            return []
        out = []
        for held in self.schedule.reserved:
            if held == sid:
                continue
            hw = self._window(held)
            if hw and overlaps(w, hw):
                out.append(held)
        return sorted(out)

    # ------------------------------------------------------------------ dispatch

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        parts = [p for p in path.split("/") if p]
        op = self._op_name(req.method, parts)
        self.calls.append((op, path))
        self.params.append((op, dict(req.url.params)))
        self.counts[op] += 1
        r = self._storm_check(req, op) or self._dispatch(req, op, parts, path)
        self.log.append((op, r.status_code))
        return r

    def _dispatch(self, req: httpx.Request, op: str, parts: list[str], path: str) -> httpx.Response:
        if op in self._throttle:
            ra = self._throttle.pop(op)
            return self._err(429, "ThrottlingException", headers={"Retry-After": str(ra)})
        if op in self._fail:
            status, left = self._fail[op]
            if left <= 1:
                del self._fail[op]
            else:
                self._fail[op] = (status, left - 1)
            return self._err(status, "Service unavailable. Back off and retry.")

        needs_auth = op not in ("ListEvents", "GetEvent")
        if needs_auth and self.require_auth:
            if req.headers.get("Authorization") != f"Bearer {self.token}":
                return self._err(401, "Sign in required.")

        if op == "ListEvents":
            return self._json(200, {"items": [self._event()]})
        if op == "GetEvent":
            if parts[2] != self.event_id:
                return self._err(404, "No such event.")
            return self._json(200, {"event": self._event()})
        if parts[2] != self.event_id:
            return self._err(404, "No such event.")

        if op == "ListSessions":
            return self._list_sessions(req)
        if op == "GetSession":
            s = self.sessions.get(parts[4])
            return self._json(200, {"session": _dump(s)}) if s else self._err(404, "No such session.")
        if op == "GetSchedule":
            return self._json(200, {"schedule": {
                "reserved": self.schedule.listed(self.schedule.reserved),
                "favorites": self.schedule.listed(self.schedule.favorites),
                "personalTime": list(self.schedule.personal_time.values()),
            }})

        # ---- writes
        if self.closed:
            return self._err(409, "This operation is not accepting requests at this time.")
        body = json.loads(req.content or b"{}")

        if op == "ReserveSessions":
            return self._bulk(body.get("sessionIds", []), self._reserve_one)
        if op == "CancelReservation":
            sid = parts[4]
            if sid not in self.schedule.reserved:
                return self._err(404, "Not reserved.")
            self.schedule.reserved.discard(sid)
            return httpx.Response(204)
        if op == "AssociateFavorites":
            return self._bulk(body.get("sessionIds", []), self._favorite_one)
        if op == "DisassociateFavorite":
            sid = parts[4]
            if sid not in self.schedule.favorites:
                return self._err(404, "Not favorited.")
            self.schedule.favorites.discard(sid)
            return httpx.Response(204)
        if op == "CreatePersonalTime":
            err = _validate_pt(body)
            if err:
                return self._err(400, err)
            self._pt_seq += 1
            pid = f"pt-{self._pt_seq}"
            self.schedule.personal_time[pid] = {"personalTimeId": pid, **body}
            return httpx.Response(204)
        if op == "UpdatePersonalTime":
            pid = parts[4]
            if pid not in self.schedule.personal_time:
                return self._err(404, "No such personal time entry.")
            err = _validate_pt(body)
            if err:
                return self._err(400, err)
            self.schedule.personal_time[pid] = {"personalTimeId": pid, **body}
            return httpx.Response(204)
        if op == "DeletePersonalTime":
            self.schedule.personal_time.pop(parts[4], None)  # idempotent per spec
            return httpx.Response(204)
        return self._err(404, f"Unknown route {path}")

    # ------------------------------------------------------------------ ops

    def _list_sessions(self, req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        ids = sorted(self.sessions)
        start = int(q.get("nextToken", "0"))
        page = ids[start:start + self.page_size]
        include_abs = q.get("includeAbstracts", "true") != "false"
        items = []
        for sid in page:
            d = _dump(self.sessions[sid])
            if not include_abs:
                d.pop("abstract", None)
            items.append(d)
        out = {"items": items, "totalCount": len(ids)}
        if start + self.page_size < len(ids):
            out["nextToken"] = str(start + self.page_size)
        return self._json(200, out, headers={"Content-Language": q.get("locale", "en-US")})

    def _reserve_one(self, sid: str) -> dict | None:
        if sid in self.refuse:
            return {"sessionId": sid, "code": self.refuse[sid]}
        if sid not in self.sessions:
            return {"sessionId": sid, "code": "other"}
        if sid in self.schedule.reserved:
            return {"sessionId": sid, "code": "alreadyScheduled"}
        if sid in self.time_passed:
            return {"sessionId": sid, "code": "timePassed"}
        if sid in self.no_access:
            return {"sessionId": sid, "code": "insufficientAccess"}
        if sid in self.not_reservable or self.sessions[sid].is_reservable is False:
            return {"sessionId": sid, "code": "sessionNotReservable"}
        c = self._conflicts(sid)
        if c:
            return {"sessionId": sid, "code": "scheduleConflict", "conflictsWith": c}
        if sid in self.full or self.sessions[sid].seat_availability == "unavailable" or self._full_draw():
            return {"sessionId": sid, "code": "sessionFull"}
        if sid in self.ghost:
            return None
        self.schedule.reserved.add(sid)
        codes = [self.sessions[h].base_code for h in self.schedule.reserved if h in self.sessions]
        if len(codes) != len(set(codes)):
            self.two_sittings.append(f"after reserving {sid}: {sorted(self.schedule.reserved)}")
        return None

    def _favorite_one(self, sid: str) -> dict | None:
        if sid in self.refuse:
            return {"sessionId": sid, "code": self.refuse[sid]}
        if sid not in self.sessions:
            return {"sessionId": sid, "code": "other"}
        if sid in self.schedule.favorites:
            return {"sessionId": sid, "code": "alreadyFavorited"}
        if sid in self.ghost:
            return None
        self.schedule.favorites.add(sid)
        return None

    def _bulk(self, ids: list[str], fn) -> httpx.Response:
        if not ids or len(ids) > 10 or len(set(ids)) != len(ids):
            return self._err(400, "sessionIds must have 1 to 10 unique items.")
        ok, failed = [], []
        for sid in ids:
            f = fn(sid)
            (failed if f else ok).append(f or sid)
        return self._json(200, {"result": {"successful": ok, "failed": failed}})

    # ------------------------------------------------------------------ plumbing

    def _event(self) -> dict:
        return {
            "eventId": self.event_id, "name": "re:Invent 2026", "eventType": "AWS re:Invent",
            "startDate": "2026-11-30T00:00:00-08:00", "endDate": "2026-12-04T23:59:59-08:00",
            "isOnline": False, "timezone": "America/Los_Angeles", "timezoneAbbreviation": "PST",
            "timeFormat": "12 hour", "address": {"city": "Las Vegas"},
            "supportedLanguageCodes": ["en-US", "pt-BR", "ja-JP"], "authenticationRequired": True,
        }

    @staticmethod
    def _op_name(method: str, parts: list[str]) -> str:
        # parts: ["v1","events",eventId?,resource?,id?]
        n = len(parts)
        if n == 2:
            return "ListEvents"
        if n == 3:
            return "GetEvent"
        res = parts[3]
        if res == "sessions":
            return "ListSessions" if n == 4 else "GetSession"
        if res == "schedule":
            return "GetSchedule"
        if res == "reservations":
            return "ReserveSessions" if method == "POST" else "CancelReservation"
        if res == "favorites":
            return "AssociateFavorites" if method == "POST" else "DisassociateFavorite"
        if res == "personal-time":
            if method == "POST":
                return "CreatePersonalTime"
            return "UpdatePersonalTime" if method == "PUT" else "DeletePersonalTime"
        return "Unknown"

    @staticmethod
    def _json(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers)

    @staticmethod
    def _err(status: int, message: str, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json={"message": message}, headers=headers)


def _dump(s: Session) -> dict:
    return json.loads(s.model_dump_json(by_alias=True, exclude_none=True))


def _validate_pt(body: dict) -> str | None:
    from datetime import datetime
    for k in ("startDateTime", "endDateTime", "title", "description"):
        if not body.get(k):
            return f"{k} is required."
    try:
        a = datetime.strptime(body["startDateTime"], "%Y-%m-%dT%H:%M:%S")
        b = datetime.strptime(body["endDateTime"], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return "Times must be YYYY-MM-DDTHH:MM:SS in UTC with no offset."
    if a.second or b.second:
        return "Seconds must be 00."
    if b <= a:
        return "endDateTime must be after startDateTime."
    if int((b - a).total_seconds()) % 300:
        return "Length must be a whole number of 5-minute increments."
    return None


# ---------------------------------------------------------------------- fixtures


def sample_sessions() -> list[Session]:
    """A small catalog that covers repeats, overlaps, venues and every band."""
    def s(sid, abbr, title, typ, venue, date, time_, mins=60, band="available", reservable=True):
        return Session.model_validate({
            "sessionId": sid, "abbreviation": abbr, "title": title, "type": typ,
            "level": "300 - Advanced", "venue": venue, "room": f"{venue}, Room 1",
            "isReservable": reservable, "seatAvailability": band,
            "sessionTime": {"date": date, "time": time_, "length": str(mins),
                            "timezone": "America/Los_Angeles"},
            "abstract": f"Abstract for {abbr}.", "topics": ["Architecture"],
        })
    return [
        s("S-ARC1", "ARC301-R1", "Resilient architectures", "Breakout session", "The Venetian",
          "2026-12-01", "10:00", band="unavailable"),
        s("S-ARC2", "ARC301-R2", "Resilient architectures", "Breakout session", "Caesars Forum",
          "2026-12-02", "14:00", band="limited"),
        s("S-SVS1", "SVS401", "Serverless at scale", "Chalk talk", "Wynn",
          "2026-12-01", "10:30", band="available"),
        s("S-WRK1", "DOP302", "CI/CD workshop", "Workshop", "MGM Grand",
          "2026-12-01", "13:00", mins=120, band="veryLimited"),
        s("S-KEY1", "KEY001", "Keynote", "Keynote", "The Venetian",
          "2026-12-01", "08:00", mins=120, band="walkUp", reservable=False),
        s("S-SEC1", "SEC201", "Security basics", "Breakout session", "Caesars Palace",
          "2026-12-02", "14:30", band="available"),
    ]
