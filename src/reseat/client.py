"""HTTP client for the AWS Events API.

Design rules, all from the developer guide:
- Every operation has its own per-minute quota. Reserve and favorite count sessions,
  not requests. We keep a token bucket per operation and never send more than we have.
- 429 carries Retry-After. We wait that long, once, then retry. A refused request
  spends no quota.
- 401 means refresh once and retry once. A second 401 means sign in again.
- 409 means the operation is closed. Retrying will not help. We surface it.
- Reserve and favorite return 200 with per-session failures. Never treat 200 as done.
- Never blind-retry a write. The caller reads back GetSchedule instead.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from . import config
from .auth import AuthError
from .models import (
    BulkResult,
    Event,
    ListSessionsPage,
    PersonalTimeInput,
    Schedule,
    Session,
)

# Per-attendee quotas, requests (or sessions) per minute.
TOKEN_HOST = "https://api.awsevents.com"

QUOTAS: dict[str, int] = {
    "GetSession": 120,
    "ListSessions": 120,
    "GetSchedule": 60,
    "ReserveSessions": 30,
    "CancelReservation": 30,
    "AssociateFavorites": 30,
    "DisassociateFavorite": 30,
    "CreatePersonalTime": 30,
    "UpdatePersonalTime": 30,
    "DeletePersonalTime": 30,
}
BATCH_MAX = 10


class ApiError(Exception):
    def __init__(self, status: int, message: str, operation: str):
        super().__init__(f"{operation}: {status} {message}")
        self.status, self.message, self.operation = status, message, operation


class OperationClosed(ApiError):
    """409. Writes are closed until 8 October, or the operation is disabled."""


class AuthRequired(ApiError):
    """401 after a refresh. Sign in again."""


class NotRegistered(ApiError):
    """403. Signed in but not registered for this event."""


class NetworkError(ApiError):
    """No HTTP answer: refused, reset or timed out. Status 0. On a write it may or may not
    have landed, so callers read back exactly as they do for a 5xx."""


class Throttled(ApiError):
    """429 after waiting once."""


class QuotaTracker:
    """Sliding one-minute window per operation. Units are sessions for bulk ops."""

    def __init__(self, quotas: dict[str, int] | None = None, clock: Callable[[], float] = time.time):
        self.quotas = quotas or QUOTAS
        self.clock = clock
        self._used: dict[str, deque[tuple[float, int]]] = {k: deque() for k in self.quotas}

    def _prune(self, op: str) -> None:
        cutoff = self.clock() - 60
        q = self._used[op]
        while q and q[0][0] <= cutoff:  # a spend expires exactly 60 s later, as seconds_until assumes
            q.popleft()

    def remaining(self, op: str) -> int:
        if op not in self.quotas:
            return 10**6
        self._prune(op)
        return self.quotas[op] - sum(n for _, n in self._used[op])

    def seconds_until(self, op: str, units: int) -> float:
        """How long until `units` fit inside the quota. 0 if they fit now."""
        if self.remaining(op) >= units:
            return 0.0
        q = self._used[op]
        need = units - self.remaining(op)
        freed = 0
        for ts, n in q:
            freed += n
            if freed >= need:
                return max(0.0, ts + 60 - self.clock())
        return 60.0

    def spend(self, op: str, units: int = 1) -> None:
        if op in self.quotas:
            self._used[op].append((self.clock(), units))


class EventsClient:
    def __init__(
        self,
        token_provider: Callable[[], str] | None = None,
        base_url: str = config.API_BASE,
        quota: QuotaTracker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        on_refresh: Callable[[], str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.on_refresh = on_refresh
        self.quota = quota or QuotaTracker()
        self.sleep = sleep
        self.http = httpx.Client(base_url=self.base_url, timeout=30, transport=transport)

    # ------------------------------------------------------------------ core

    def _headers(self, auth: bool) -> dict[str, str]:
        """The access token goes only to https://api.awsevents.com, whatever the base URL says."""
        h = {"Accept": "application/json"}
        if auth and self.token_provider:
            if self.base_url != TOKEN_HOST:
                raise AuthRequired(0, f"Refusing to send the access token to {self.base_url}. "
                                      f"It goes only to {TOKEN_HOST}.", "auth")
            h["Authorization"] = f"Bearer {self.token_provider()}"
        return h

    def _request(
        self,
        op: str,
        method: str,
        path: str,
        *,
        auth: bool = True,
        units: int = 1,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        wait = self.quota.seconds_until(op, units)
        if wait > 0:
            self.sleep(wait)
        refreshed = False
        for _attempt in range(3):
            try:
                r = self.http.request(method, path, params=params, json=json, headers=self._headers(auth))
            except httpx.TransportError as e:
                raise NetworkError(0, f"{type(e).__name__}: {e}", op) from None
            except AuthError as e:
                raise AuthRequired(401, str(e), op) from None
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "60"))
                if _attempt == 0:
                    self.sleep(retry_after)
                    continue
                raise Throttled(429, _msg(r), op)
            if r.status_code == 401 and auth and not refreshed and self.on_refresh:
                try:
                    self.on_refresh()
                except AuthError as e:
                    raise AuthRequired(401, f"refresh failed: {e}", op) from None
                except httpx.TransportError as e:
                    raise NetworkError(0, f"refresh: {type(e).__name__}: {e}", op) from None
                refreshed = True
                continue
            break
        self.quota.spend(op, units)
        self._raise_for(r, op)
        return r

    @staticmethod
    def _raise_for(r: httpx.Response, op: str) -> None:
        if r.status_code < 400:
            return
        m = _msg(r)
        if r.status_code == 401:
            raise AuthRequired(401, m, op)
        if r.status_code == 403:
            raise NotRegistered(403, m, op)
        if r.status_code == 409:
            raise OperationClosed(409, m, op)
        raise ApiError(r.status_code, m, op)

    # ------------------------------------------------------------------ reads

    def list_events(self, include_past: bool = False) -> list[Event]:
        r = self._request("ListEvents", "GET", "/v1/events", auth=False,
                          params={"includePast": "true"} if include_past else None)
        return [Event.model_validate(e) for e in r.json()["items"]]

    def get_event(self, event_id: str) -> Event:
        r = self._request("GetEvent", "GET", f"/v1/events/{event_id}", auth=False)
        return Event.model_validate(r.json()["event"])

    def list_sessions_page(
        self, event_id: str, *, next_token: str | None = None,
        include_abstracts: bool = True, locale: str | None = None,
    ) -> ListSessionsPage:
        params: dict[str, Any] = {}
        if next_token:
            params["nextToken"] = next_token
        if not include_abstracts:
            params["includeAbstracts"] = "false"
        if locale:
            params["locale"] = locale
        r = self._request("ListSessions", "GET", f"/v1/events/{event_id}/sessions", params=params)
        return ListSessionsPage.model_validate(r.json())

    def iter_sessions(self, event_id: str, *, include_abstracts: bool = True,
                      locale: str | None = None) -> Iterator[Session]:
        token: str | None = None
        while True:
            page = self.list_sessions_page(event_id, next_token=token,
                                           include_abstracts=include_abstracts, locale=locale)
            yield from page.items
            token = page.next_token
            if not token:
                return

    def get_session(self, event_id: str, session_id: str, locale: str | None = None) -> Session:
        r = self._request("GetSession", "GET", f"/v1/events/{_seg(event_id)}/sessions/{_seg(session_id)}",
                          params={"locale": locale} if locale else None)
        return Session.model_validate(r.json()["session"])

    def get_schedule(self, event_id: str) -> Schedule:
        r = self._request("GetSchedule", "GET", f"/v1/events/{event_id}/schedule")
        return Schedule.model_validate(r.json()["schedule"])

    # ------------------------------------------------------------------ writes

    def reserve(self, event_id: str, session_ids: list[str]) -> BulkResult:
        """One call, at most 10 sessions. Quota is spent per session."""
        ids = _check_batch(session_ids)
        r = self._request("ReserveSessions", "POST", f"/v1/events/{event_id}/reservations",
                          units=len(ids), json={"sessionIds": ids})
        return BulkResult.model_validate(r.json()["result"])

    def cancel(self, event_id: str, session_id: str) -> None:
        self._request("CancelReservation", "DELETE",
                      f"/v1/events/{_seg(event_id)}/reservations/{_seg(session_id)}")

    def favorite(self, event_id: str, session_ids: list[str]) -> BulkResult:
        ids = _check_batch(session_ids)
        r = self._request("AssociateFavorites", "POST", f"/v1/events/{event_id}/favorites",
                          units=len(ids), json={"sessionIds": ids})
        return BulkResult.model_validate(r.json()["result"])

    def unfavorite(self, event_id: str, session_id: str) -> None:
        self._request("DisassociateFavorite", "DELETE",
                      f"/v1/events/{_seg(event_id)}/favorites/{_seg(session_id)}")

    def create_personal_time(self, event_id: str, body: PersonalTimeInput) -> None:
        self._request("CreatePersonalTime", "POST", f"/v1/events/{event_id}/personal-time",
                      json=body.payload())

    def update_personal_time(self, event_id: str, pt_id: str, body: PersonalTimeInput) -> None:
        self._request("UpdatePersonalTime", "PUT",
                      f"/v1/events/{_seg(event_id)}/personal-time/{_seg(pt_id)}", json=body.payload())

    def delete_personal_time(self, event_id: str, pt_id: str) -> None:
        self._request("DeletePersonalTime", "DELETE",
                      f"/v1/events/{_seg(event_id)}/personal-time/{_seg(pt_id)}")

    # ------------------------------------------------------------------ probes

    def writes_open(self, event_id: str, probe_session_id: str) -> bool:
        """Cancel a session we do not hold. 409 means closed, 404 means open.

        Never changes the schedule. Pick a probe session the attendee will never hold.
        """
        try:
            self.cancel(event_id, probe_session_id)
        except OperationClosed:
            return False
        except ApiError as e:
            if e.status == 404:
                return True
            raise
        # A 204 would mean we held it. Treat as open but warn upstream.
        return True


def _seg(value: str) -> str:
    """One URL path segment. An id from the API or a user never adds or escapes a path segment."""
    return urllib.parse.quote(value, safe="")


def _check_batch(ids: list[str]) -> list[str]:
    clean = [i.strip() for i in ids if i and i.strip()]
    if not clean:
        raise ValueError("No session IDs given.")
    if len(clean) > BATCH_MAX:
        raise ValueError(f"At most {BATCH_MAX} sessions per call.")
    if len(set(clean)) != len(clean):
        raise ValueError("Duplicate session IDs in one call.")
    return clean


def _msg(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message", r.text[:200]))
    except ValueError:
        return r.text[:200]
