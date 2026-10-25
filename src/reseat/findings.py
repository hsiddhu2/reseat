"""What the watcher saw, in counts: the proof that it works behind the scenes.

Read-only over the local store. No I/O beyond SQLite, nothing sent to AWS.

Rules this module exists to respect:
- Counts by session type and seat band only. No session code, title, room or time, so the
  result can be published without showing anyone's timetable. Days are the day a seat
  opened or a session was added, in Las Vegas time, never the day a session runs.
- A sweep that loaded, emptied or re-keyed the whole catalog is a baseline, not news. Its rows
  are left out, the way the watcher leaves them out. A load is told apart from a real batch of
  new sessions by size: its rows number at least half of that sweep's catalog.
- "Full" is the band `unavailable`. An opening is a change from full to any other band.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from .campus import VEGAS
from .store import Store

BASELINE = 100            # at least this many rows, and half the sweep, in one sweep is a catalog load
FULL = "unavailable"
WRITES = ("ReserveSessions", "CancelReservation", "AssociateFavorites", "DisassociateFavorite",
          "CreatePersonalTime", "UpdatePersonalTime", "DeletePersonalTime")
REFILL = (("under 10 min", 10), ("10 to 60 min", 60), ("1 to 6 h", 360), ("over 6 h", None))


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, VEGAS).date().isoformat()


def _days(first: float, last: float) -> list[str]:
    d, end = datetime.fromtimestamp(first, VEGAS).date(), datetime.fromtimestamp(last, VEGAS).date()
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _per_day(days: list[str], stamps: list[float]) -> list[list[Any]]:
    n = Counter(_day(t) for t in stamps)
    return [[d, n.get(d, 0)] for d in days]


def _loads(store: Store, event_id: str, rows: list[Any]) -> set[tuple[float, str]]:
    """(sweep time, kind) groups that are a whole-catalog load, empty or re-key, not changes."""
    size = dict(store.sweep_sizes(event_id))
    groups = Counter((r["ts"], r["kind"]) for r in rows)
    return {g for g, n in groups.items() if n >= BASELINE and n * 2 >= max(size.get(g[0], 0), n)}


def baseline_end(store: Store, event_id: str) -> float:
    """The last sweep that loaded the whole catalog: the start of the window worth counting."""
    loads = [ts for ts, kind in _loads(store, event_id, store.change_rows(event_id)) if kind == "added"]
    return max(loads) if loads else (store.first_sweep(event_id) or 0.0)


def compute(store: Store, event_id: str, now: float, targets: set[str] | None = None,
            since: float | None = None) -> dict[str, Any]:
    """Every count the Findings page shows. JSON-safe."""
    start = baseline_end(store, event_id) if since is None else since
    sweeps = store.sweep_times(event_id, start)
    last = sweeps[-1] if sweeps else now
    days = _days(start, max(last, start))
    facts = store.session_facts(event_id)
    return {
        "since": datetime.fromtimestamp(start, VEGAS).strftime("%Y-%m-%d %H:%M"),
        "until": datetime.fromtimestamp(last, VEGAS).strftime("%Y-%m-%d %H:%M"),
        "days": len(days),
        "sweeps": len(sweeps),
        "sweeps_per_day": _per_day(days, sweeps),
        "openings": _openings(store, event_id, start, days, facts, targets or set()),
        "catalog": _catalog(store, event_id, start, days, facts),
        "bands_now": _bands(facts),
        "outages": _outages(store, event_id, start),
        "writes": _writes(store, event_id, start),
    }


def _openings(store: Store, event_id: str, start: float, days: list[str],
              facts: dict[str, tuple[str | None, str | None]], targets: set[str]) -> dict[str, Any]:
    by_session: dict[str, list[Any]] = defaultdict(list)
    for r in store.band_changes(event_id, start):
        by_session[r["session_id"]].append(r)
    opens: list[tuple[str, float, str]] = []
    refill = Counter()
    fastest: float | None = None
    for sid, rows in by_session.items():
        for i, r in enumerate(rows):
            if r["old_band"] != FULL or r["new_band"] == FULL:
                continue
            opens.append((sid, r["ts"], r["new_band"] or "none"))
            again = next((x["ts"] for x in rows[i + 1:] if x["new_band"] == FULL), None)
            if again is None:
                refill["still open"] += 1
                continue
            mins = (again - r["ts"]) / 60
            fastest = mins if fastest is None else min(fastest, mins)
            refill[next(label for label, top in REFILL if top is None or mins < top)] += 1
    types: dict[str, list[set[str] | int]] = defaultdict(lambda: [0, set()])
    for sid, _, _ in opens:
        t = types[facts.get(sid, (None, None))[0] or "Unknown"]
        t[0] += 1                                 # type: ignore[operator]
        t[1].add(sid)                             # type: ignore[union-attr]
    per_day = _per_day(days, [ts for _, ts, _ in opens])
    return {
        "total": len(opens),
        "sessions": len({sid for sid, _, _ in opens}),
        "by_band": dict(Counter(b for _, _, b in opens).most_common()),
        "by_type": sorted(([k, v[0], len(v[1])] for k, v in types.items()),  # type: ignore[arg-type]
                          key=lambda x: (-x[1], x[0])),
        "per_day": per_day,
        "days_with": sum(1 for _, n in per_day if n),
        "refilled": {label: refill.get(label, 0) for label, _ in (*REFILL, ("still open", None))},
        "fastest_min": None if fastest is None else round(fastest, 1),
        "on_targets": len({sid for sid, _, _ in opens} & targets),
    }


def _catalog(store: Store, event_id: str, start: float, days: list[str],
             facts: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]:
    rows = [r for r in store.change_rows(event_id, start) if r["ts"] > start]
    loads = _loads(store, event_id, rows)
    rows = [r for r in rows if (r["ts"], r["kind"]) not in loads]
    added = list(dict.fromkeys(r["session_id"] for r in rows if r["kind"] == "added"))
    listed = [sid for sid in added if sid in facts]
    first_add = {r["session_id"]: r["ts"] for r in reversed(rows) if r["kind"] == "added"}
    return {
        "added": len(added),
        "still_listed": len(listed),
        "added_by_type": sorted(Counter(facts[s][0] or "Unknown" for s in listed).items(),
                                key=lambda x: (-x[1], x[0])),
        "added_per_day": _per_day(days, [first_add[s] for s in added]),
        "removed": len({r["session_id"] for r in rows if r["kind"] == "removed"}),
        "moved": sum(1 for r in rows if r["kind"] == "moved"),
    }


def _bands(facts: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]:
    n = Counter(band or "none" for _, band in facts.values())
    reservable = sum(v for k, v in n.items() if k != "none")
    return {"counts": dict(n.most_common()), "reservable": reservable, "full": n.get(FULL, 0),
            "total": sum(n.values())}


def _outages(store: Store, event_id: str, start: float) -> dict[str, Any]:
    count, minutes, longest = 0, 0, 0
    for r in store.journal_ops(event_id, ("watcher.outage",)):
        if r["ts"] < start or r["outcome"] != "end":
            continue
        try:
            m = int((json.loads(r["response"]) or {}).get("minutes", 0))
        except (ValueError, TypeError, AttributeError):
            m = 0
        count, minutes, longest = count + 1, minutes + m, max(longest, m)
    return {"count": count, "minutes": minutes, "longest_min": longest}


def _writes(store: Store, event_id: str, start: float) -> dict[str, int]:
    """Writes sent to the Events API, by operation. The writes-open probe is counted apart."""
    n: Counter[str] = Counter()
    for r in store.journal_ops(event_id, WRITES):
        if r["ts"] < start:
            continue
        n["probe" if (r["outcome"] or "").startswith("probe") else r["op"]] += 1
    return {op: n.get(op, 0) for op in (*WRITES, "probe")}
