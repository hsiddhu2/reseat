"""The web app's views: what the dashboard, approve and today pages show, and their HTML.

`build` runs on the watcher's thread, the only thread that touches the store. It turns
the store, the journal, the rules, the guard and the watcher's memory into a plain
dict. The render functions turn that dict into HTML on an HTTP thread. Nothing here
calls the API or writes anything.

Rules this module exists to respect:
- Every value shown comes from the store, the journal, the rules, the guard or the
  watcher. Nothing is invented. A swap card's checks come from the local catalog
  and say so: approving runs swap.py's fresh checks.
- No session id reaches a page or the JSON. Pages show codes and titles. Actions
  carry a plan id only.
- Every catalog string is escaped. Titles and rooms come from the API.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Any

from . import guard
from .campus import CUTOFF_MINUTES, EVENT_DAYS, VEGAS, overlaps, room_label, walk_minutes
from .client import QUOTAS
from .models import PersonalTime, Session
from .swap import Swap
from .watcher import ONSITE_CAP, ONSITE_EVERY

if TYPE_CHECKING:
    from .serve import App

DAY_START, DAY_END = 8 * 60, 18 * 60          # the week grid runs 8:00 to 18:00
SLOT = 15                                     # minutes per grid row
CHANGES, JOURNAL = 10, 20
MOVED_NOTE_HOURS = 24
SEQUENCE = ("Cancel {a}, reserve {b}, read back. If {b} is refused, {a} is reserved again. "
            "If writes close or the read-back fails, nothing more is sent and you are told what is known.")
VERDICT = {"likely": "Walk-up likely.", "early": "Go early.", "unlikely": "Walk-up unlikely.",
           "unknown": "No call."}


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, VEGAS)


def hm(ts: float) -> str:
    return local(ts).strftime("%H:%M")


def _start(s: Session) -> datetime | None:
    st = s.session_time
    if not st or not st.date or not st.time:
        return None
    return datetime.fromisoformat(f"{st.date}T{st.time}").replace(tzinfo=VEGAS)


def _window(s: Session) -> tuple[datetime, datetime] | None:
    a = _start(s)
    if not a:
        return None
    return a, a + timedelta(minutes=(s.session_time.minutes if s.session_time else None) or 60)


def _when(s: Session) -> str:
    a = _start(s)
    return a.strftime("%a %H:%M") if a else "time not set"


def _pt_window(p: PersonalTime) -> tuple[datetime, datetime] | None:
    try:
        a = datetime.fromisoformat(p.start_date_time).replace(tzinfo=UTC).astimezone(VEGAS)
        b = datetime.fromisoformat(p.end_date_time).replace(tzinfo=UTC).astimezone(VEGAS)
    except ValueError:
        return None
    return a, b


# ---------------------------------------------------------------------- build, on the watcher thread


def build(app: App, now: float) -> dict[str, Any]:
    """Everything the three views show. Runs on the watcher's thread."""
    st, ev, w = app.store, app.event_id, app.watcher
    held_ids = sorted(w.last_held)
    held = [s for s in (st.get(ev, i) for i in held_ids) if s]
    sweeps = st.sweeps(ev, 1)
    last_sweep = sweeps[0][0] if sweeps else None
    pending = w.pending()
    wanted = _wanted(app, set(held_ids))
    return {
        "status": _status(app, now, last_sweep, sweeps, held_ids, wanted),
        "cards": [_card(app, p, held_ids, last_sweep) for p in pending],
        "changes": _changes(app),
        "log": _journal(app),
        "week": _week(app, held, wanted, pending, now),
        "today": _today(app, held, wanted, now),
        "bookings": _bookings(app, now),
        "kept": _kept(app),
        "scenarios": [{"key": k, "label": label, "see": see} for k, label, see in app.scenarios],
    }


def _status(app: App, now: float, last_sweep: float | None, sweeps: list[tuple[float, int]],
            held_ids: list[str], wanted: list[tuple[Any, list[Session]]]) -> dict[str, Any]:
    w = app.watcher
    if w.read_only:
        state, text = "signin", "Sign in needed. Reading only"
    elif w.down_since is not None:
        state, text = "offline", f"API unreachable since {hm(w.down_since)}"
    elif w.writes_closed_until:
        state, text = "paused", f"Writes closed (409). Next try {hm(w.writes_closed_until)}"
    else:
        state, text = "watching", "Watching"
    quota = QUOTAS.get("ListSessions", 0)
    return {
        "state": state, "text": text, "where": "in this process",
        "last_sweep": hm(last_sweep) + local(last_sweep).strftime(":%S") if last_sweep else None,
        "sessions": sweeps[0][1] if sweeps else None,
        "next_at": (last_sweep + w.next_delay()) if last_sweep else None,
        "held": len(held_ids), "wanted": len(wanted),
        "quota_left": w.client.quota.remaining("ListSessions"), "quota_max": quota,
    }


def _wanted(app: App, held: set[str]) -> list[tuple[Any, list[Session]]]:
    """Targets not held, each with the sittings re:Seat would book, in rule order."""
    out = []
    held_codes = {s.base_code for s in (app.store.get(app.event_id, i) for i in held) if s}
    for t in app.rules.targets:
        tree = [s for s, _ in app.watcher.router.tree(t)]
        if any(s.session_id in held for s in tree):
            continue
        out.append((t, [s for s in tree if s.base_code not in held_codes]))
    return out


def _code(app: App, sid: str) -> str:
    s = app.store.get(app.event_id, sid)
    return (s.abbreviation or "a session") if s else "a session not in the local catalog"


def _side(s: Session | None) -> dict[str, Any]:
    if not s:
        return {"code": "unknown", "when": "", "venue": ""}
    return {"code": s.abbreviation, "title": s.title, "when": _when(s), "venue": s.campus_venue or "",
            "day": _start(s).strftime("%A") if _start(s) else ""}


def _card(app: App, p: Any, held_ids: list[str], last_sweep: float | None) -> dict[str, Any]:
    st, ev = app.store, app.event_id
    a, b = st.get(ev, p.held_id), st.get(ev, p.wanted_id)
    pv = Swap(app.watcher.client, st, app.rules, ev).preview(p.held_id, p.wanted_id, held_ids)
    since = max(p.created - 3600, st.first_sweep(ev) or 0.0)      # the first sweep's rows are a baseline
    added = any(r["session_id"] == p.wanted_id and r["kind"] == "added"
                for r in st.latest_changes(ev, since, 50))
    sa, sb = _side(a), _side(b)
    same = bool(a and b and a.base_code and a.base_code == b.base_code)
    read = f", read {hm(last_sweep)}" if last_sweep else ""
    checks = [{"ok": "yes" if pv.band_open else "no", "text": f"Target band: {pv.band or 'none'}{read}"}]
    if pv.fallbacks:
        others = [f for f in pv.fallbacks if f[0] != p.held_id]
        fid, fband = (others or pv.fallbacks)[0]     # re-reserving A itself is in the sequence line
        f = st.get(ev, fid)
        name = f"{sa['code']} itself" if fid == p.held_id else f"{_code(app, fid)}, {_when(f) if f else ''}"
        checks.append({"ok": "yes", "text": f"Fallback if it fails: {name}, {fband}"})
    else:
        checks.append({"ok": "no", "text": f"No open fallback for {sa['code']}"})
    checks += ([{"ok": "no", "text": c} for c in pv.clashes]
               or [{"ok": "yes", "text": "No overlap with anything else held"}])
    target = next((t for t in app.rules.targets if t.label == p.target), None)
    auto = bool(target and target.auto_swap)
    checks.append({"ok": "yes", "text": "Rules allow auto swap for this target"} if auto
                  else {"ok": "ask", "text": "Rules say ask first for this target"})
    base = b.base_code if b else sb["code"]
    return {
        "plan_id": p.plan_id, "target": p.target,
        "kind": "New sitting" if added else "Seat opened", "at": hm(p.created),
        "title": f"Move {base} to {sb['day']}" if same else f"Replace {sa['code']} with {sb['code']}",
        "question": (f"Move {base} from {sa['day']} to {sb['day']}?" if same
                     else f"Replace {sa['code']} with {sb['code']}?"),
        "keep": f"Keep {sa['day']}" if same else f"Keep {sa['code']}",
        "held": sa, "wanted": sb, "checks": checks,
        "sequence": SEQUENCE.format(a=sa["code"], b=sb["code"]),
        "note": "From the last sweep. Checked again with fresh reads when you press Swap now.",
        "expires": p.expires,
    }


def _changes(app: App) -> list[dict[str, Any]]:
    st, ev = app.store, app.event_id
    first = st.first_sweep(ev) or 0.0
    rows: list[tuple[float, str]] = []
    for r in st.latest_changes(ev, first, CHANGES):
        code = _code(app, r["session_id"])
        if r["kind"] == "added":
            s = st.get(ev, r["session_id"])
            rows.append((r["ts"], f"{code} added, {_when(s) if s else ''}"))
        elif r["kind"] == "moved":
            rows.append((r["ts"], f"{code} moved: {_moved_text(r['detail'])}"))
        else:
            rows.append((r["ts"], f"{r['detail'] or 'a session'} removed from the catalog"))
    for r in st.latest_band_changes(ev, CHANGES):
        if r["ts"] > first:
            code = _code(app, r["session_id"])
            rows.append((r["ts"], f"{code} {r['old_band']} → {r['new_band'] or 'none'}"))
    for r in st.journal_entries(ev, 200):
        if r["op"] == "ReserveSessions":
            for sid in _booked(r):
                rows.append((r["ts"], f"{_code(app, sid)} booked, read back ok"))
        elif r["op"] in ("swap.verified", "swap.rolled_back", "swap.failed"):
            req = _json(r["request"]) or {}
            a, b = _code(app, req.get("held", "")), _code(app, req.get("wanted", ""))
            rows.append((r["ts"], f"Swap {a} → {b} {r['op'].removeprefix('swap.').replace('_', ' ')}"))
    rows.sort(key=lambda x: -x[0])
    return [{"at": hm(ts), "text": text} for ts, text in rows[:CHANGES]]


def _moved_text(detail: str | None) -> str:
    d = _json(detail) or {}
    a, b = d.get("from") or {}, d.get("to") or {}
    parts = []
    for k in ("venue", "room", "date", "time"):
        if a.get(k) != b.get(k):
            parts.append(f"{a.get(k) or 'none'} → {b.get(k) or 'none'}")
    return ", ".join(parts) or "details changed"


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _booked(row: Any) -> list[str]:
    """Ids a ReserveSessions call asked for that its read-back showed held."""
    asked = _json(row["request"]) or []
    resp = _json(row["response"]) or {}
    back = set(((resp.get("readBack") or {}).get("reserved")) or [])
    return [s for s in asked if isinstance(s, str) and s in back]


def _journal(app: App) -> list[dict[str, Any]]:
    out = []
    for r in app.store.journal_entries(app.event_id, JOURNAL):
        resp, req = _json(r["response"]), _json(r["request"])
        out.append({"at": local(r["ts"]).strftime("%a %H:%M:%S"), "op": r["op"], "status": _status_of(resp),
                    "outcome": (r["outcome"] or "").removeprefix("state:"), "what": _what(app, req)})
    return out


def _status_of(resp: Any) -> str:
    """The HTTP status a journal row recorded, where it recorded one."""
    if not isinstance(resp, dict):
        return ""
    inner = resp.get("response")
    st = resp.get("status") or (inner.get("status") if isinstance(inner, dict) else None)
    if st is None and (resp.get("result") is not None
                       or (isinstance(inner, dict) and ("successful" in inner or "failed" in inner))):
        st = 200                  # a bulk result came back: the call itself answered 200
    return "" if st is None else str(st)


def _what(app: App, req: Any) -> str:
    if isinstance(req, dict) and "held" in req:
        return f"{_code(app, req['held'])} → {_code(app, req['wanted'])}"
    if isinstance(req, list):
        return ", ".join(_code(app, s) for s in req[:4] if isinstance(s, str))
    return ""


def _kept(app: App) -> dict[str, Any]:
    """What re:Seat has done for the attendee, counted from the journal: seats booked and read
    back, swaps verified, and seats restored after a swap's target was refused (the held seat
    re-reserved, or a fallback sitting booked in its place)."""
    booked = swapped = restored = 0
    first = None
    for r in app.store.journal_entries(app.event_id, 100_000):
        if r["op"] == "ReserveSessions":
            booked += len(_booked(r))
        elif r["op"] == "swap.verified":
            swapped += 1
        elif r["op"] == "swap.rolled_back" or (
                r["op"] == "swap.failed" and (_json(r["response"]) or {}).get("fallback_held")):
            restored += 1                       # the held seat came back, or a fallback sitting was booked
        else:
            continue
        first = r["ts"]                                  # entries come newest first
    since = local(first).strftime("%a %d %b") if first else None
    return {"booked": booked, "swapped": swapped, "restored": restored, "since": since}


def _bookings(app: App, now: float) -> list[dict[str, Any]]:
    out = []
    for r in app.store.journal_entries(app.event_id, 200):
        if r["op"] == "ReserveSessions" and r["ts"] > now - 86400:
            for sid in _booked(r):
                s = app.store.get(app.event_id, sid)
                out.append({"at": hm(r["ts"]), "code": _code(app, sid), "when": _when(s) if s else "",
                            "venue": (s.campus_venue or "") if s else ""})
    return out[:3]


# ---------------------------------------------------------------------- the week grid


def _week(app: App, held: list[Session], wanted: list[tuple[Any, list[Session]]],
          pending: list[Any], now: float) -> dict[str, Any]:
    st, ev = app.store, app.event_id
    days = sorted(set(EVENT_DAYS.values()))[:5]
    proposed = {p.wanted_id for p in pending}
    swapping = {p.held_id for p in pending}
    fallbacks: set[str] = set()
    for p in pending:
        sw = Swap(app.watcher.client, st, app.rules, ev)
        pv = sw.preview(p.held_id, p.wanted_id, app.watcher.last_held)
        fallbacks |= {f for f, _ in pv.fallbacks if f != p.held_id}
    moved = {}
    for r in st.latest_changes(ev, max(st.first_sweep(ev) or 0.0, now - MOVED_NOTE_HOURS * 3600), 200):
        if r["kind"] == "moved" and r["session_id"] not in moved:
            to = (_json(r["detail"]) or {}).get("to") or {}
            moved[r["session_id"]] = room_label(to.get("venue"), to.get("room"))
    items: list[tuple[str, Session]] = [("held", s) for s in held]
    seen = {s.session_id for s in held}
    for _t, sittings in wanted:
        for s in sittings:
            if s.session_id not in seen:
                seen.add(s.session_id)
                items.append(("proposed" if s.session_id in proposed else "wanted", s))
    for sid in fallbacks - seen:
        s = st.get(ev, sid)
        if s:
            items.append(("fallback", s))
    for sid in proposed - seen:
        s = st.get(ev, sid)
        if s:
            items.append(("proposed", s))
    blocks = []
    for kind, s in items:
        win = _window(s)
        if not win or win[0].date().isoformat() not in days:
            continue
        a, b = win
        top, bottom = _minutes(a), _minutes(b)
        if bottom <= DAY_START or top >= DAY_END:
            continue
        sub = {"held": s.campus_venue or "", "wanted": f"Wanted · {s.campus_venue or ''}",
               "proposed": f"Proposed · {s.campus_venue or ''}",
               "fallback": f"Fallback · {s.campus_venue or ''}"}[kind]
        if kind == "held" and s.session_id in swapping:
            sub += " · swap pending"
        if kind == "held" and s.session_id in moved:
            sub += f" · moved to {moved[s.session_id] or 'a new room'}"
        blocks.append({"day": days.index(a.date().isoformat()), "kind": kind, "code": s.abbreviation,
                       "title": f"{s.abbreviation} {s.title} · {_when(s)} · {s.campus_venue or ''}",
                       "sub": sub, "pending": kind == "held" and s.session_id in swapping,
                       "top": _pct(max(top, DAY_START)),
                       "height": _pct(min(bottom, DAY_END)) - _pct(max(top, DAY_START)),
                       "start": a, "end": b, "venue": s.campus_venue, "badge": None})
    _lanes(blocks)
    _badges(blocks)
    leaves = []
    blocks_now, _ = guard.leave_blocks(guard.held_sessions(st, ev, [s.session_id for s in held]), app.rules)
    for blk in blocks_now:
        at = datetime.fromisoformat(blk.start).replace(tzinfo=UTC).astimezone(VEGAS)
        if at.date().isoformat() in days and DAY_START <= _minutes(at) < DAY_END:
            leaves.append({"day": days.index(at.date().isoformat()), "top": _pct(_minutes(at)),
                           "title": f"Leave {at:%H:%M} for {blk.code}"})
    labels = [f"{datetime.fromisoformat(d):%a} {datetime.fromisoformat(d).day}" for d in days]
    first, last = datetime.fromisoformat(days[0]), datetime.fromisoformat(days[-1])
    for blk in blocks:
        blk.pop("start"), blk.pop("end"), blk.pop("venue")
    return {"title": f"Week of {first.day} {first:%b} – {last.day} {last:%b}", "days": labels,
            "blocks": blocks, "leaves": leaves,
            "hours": [{"label": f"{h}:00", "top": _pct(h * 60)} for h in range(8, 19, 2)]}


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _pct(minutes: int) -> float:
    return round((minutes - DAY_START) / (DAY_END - DAY_START) * 100, 3)


def _lanes(blocks: list[dict[str, Any]]) -> None:
    """Side by side when blocks overlap in one day."""
    for day in {b["day"] for b in blocks}:
        mine = sorted((b for b in blocks if b["day"] == day), key=lambda b: (b["start"], b["kind"] != "held"))
        groups: list[list[dict[str, Any]]] = []
        for b in mine:
            if groups and any(overlaps((b["start"], b["end"]), (o["start"], o["end"])) for o in groups[-1]):
                groups[-1].append(b)
            else:
                groups.append([b])
        for g in groups:
            ends: list[datetime] = []
            for b in g:
                lane = next((i for i, end in enumerate(ends) if end <= b["start"]), len(ends))
                if lane < len(ends):
                    ends[lane] = b["end"]
                else:
                    ends.append(b["end"])
                b["lane"] = lane
            for b in g:
                b["lanes"] = max(len(ends), 1)


def _badges(blocks: list[dict[str, Any]]) -> None:
    """Walk minutes over the gap, against the held session that ends before this one starts."""
    for b in blocks:
        prev = [h for h in blocks if h["kind"] == "held" and h["day"] == b["day"] and h["end"] <= b["start"]
                and h is not b]
        if not prev:
            continue
        p = max(prev, key=lambda h: h["end"])
        walk = walk_minutes(p["venue"], b["venue"])
        gap = int((b["start"] - p["end"]).total_seconds() // 60)
        if walk is not None and walk > gap:
            b["badge"] = f"walk {walk} m, gap {gap} m"


# ---------------------------------------------------------------------- today


def _today(app: App, held: list[Session], wanted: list[tuple[Any, list[Session]]],
           now: float) -> dict[str, Any]:
    st, ev = app.store, app.event_id
    sessions = guard.held_sessions(st, ev, [s.session_id for s in held])
    day = app._day(sessions, now)
    blocks, _ = guard.leave_blocks(sessions, app.rules)
    by_id = {b.session_id: b for b in blocks}
    today = [s for s in sessions if s.session_time and s.session_time.date == day]
    hero = None
    for s in today:
        b = by_id.get(s.session_id)
        a = _start(s)
        if not b or not a:
            continue
        leave_at = datetime.fromisoformat(b.start).replace(tzinfo=UTC).timestamp()
        if a.timestamp() > now:                  # held until it starts: a missed leave time shows as now
            hero = {"code": s.abbreviation, "start": f"{a:%H:%M}", "venue": s.campus_venue or "",
                    "room": s.room_label or "", "origin": b.origin or "", "walk": b.walk,
                    "leave_at": leave_at, "doors": (a - timedelta(minutes=CUTOFF_MINUTES)).strftime("%H:%M")}
            break
    nxt: list[tuple[datetime, dict[str, Any]]] = []
    for s in today:
        a = _start(s)
        if a and a.timestamp() > now and s.abbreviation != (hero or {}).get("code"):
            b = by_id.get(s.session_id)
            leave = (datetime.fromisoformat(b.start).replace(tzinfo=UTC).astimezone(VEGAS).strftime("%H:%M")
                     if b else None)
            sub = f"{s.campus_venue or ''} · held" + (f" · leave {leave}" if leave else "")
            nxt.append((a, {"time": f"{a:%H:%M}", "name": f"{s.abbreviation} · {s.type_key or 'Session'}",
                            "sub": sub}))
    for p in app.watcher.personal_time:
        win = _pt_window(p)
        if win and win[0].date().isoformat() == day and win[1].timestamp() > now and not guard.is_mine(p):
            nxt.append((win[0], {"time": f"{win[0]:%H:%M}", "name": f"Personal · {p.title}",
                                 "sub": p.location or ""}))
    nxt.sort(key=lambda x: x[0])
    want = []
    for _t, sittings in wanted:
        for s in sittings:
            a = _start(s)
            if not a or a.date().isoformat() != day or a.timestamp() <= now:
                continue
            adv = guard.queue_or_go(s, st.band_history(ev, s.session_id))
            want.append({"code": s.abbreviation, "time": f"{a:%H:%M}", "band": s.seat_availability or "none",
                         "open": bool(s.band and s.band.open),
                         "text": f"{s.type_key or 'Session'} at {s.campus_venue or 'a venue not set'}. "
                                 f"{VERDICT.get(adv.verdict, '')} Basis: {adv.basis}",
                         "clash": _clash(s, today)})
    on_site = day in EVENT_DAYS.values() and local(now).date().isoformat() == day
    if on_site:
        wanted_today = [x for _t, sittings in wanted for x in sittings
                        if x.session_time and x.session_time.date == day]
        n = min(len(today) + len(wanted_today), ONSITE_CAP)      # what the watcher polls, past ones included
        cover = (f"Laptop sweeps the catalog every {app.watcher.interval} s and checks today's {n} sessions "
                 f"between sweeps, every {ONSITE_EVERY} s when there is time")
    else:
        cover = f"Laptop sweeps the whole catalog every {app.watcher.interval} s"
    d = datetime.fromisoformat(day)
    return {"label": f"{d:%a} {d.day} {d:%b}", "clock": hm(now), "hero": hero, "next": [x for _, x in nxt],
            "wanted": want, "cover": cover}


def _clash(s: Session, held_today: list[Session]) -> str | None:
    """Whether attending `s` collides with a held session's walk, in plain minutes."""
    win = _window(s)
    if not win:
        return None
    for h in held_today:
        hw = _window(h)
        if hw and overlaps(win, hw):
            return f"Overlaps held {h.abbreviation}."
    before = [h for h in held_today if _window(h) and _window(h)[1] <= win[0]]  # type: ignore[index]
    after = [h for h in held_today if _window(h) and _window(h)[0] >= win[1]]   # type: ignore[index]
    if before:
        h = max(before, key=lambda x: _window(x)[1])  # type: ignore[index]
        walk = walk_minutes(h.campus_venue, s.campus_venue)
        late = (_window(h)[1] + timedelta(minutes=walk or 0) - win[0]).total_seconds() // 60  # type: ignore[index]
        if walk is not None and late > 0:
            return f"After {h.abbreviation} the walk is {walk} min. You would miss the first {int(late)} min."
    if after:
        h = min(after, key=lambda x: _window(x)[0])  # type: ignore[index]
        walk = walk_minutes(s.campus_venue, h.campus_venue)
        leave = _window(h)[0] - timedelta(minutes=CUTOFF_MINUTES + (walk or 0))  # type: ignore[index]
        early = (win[1] - leave).total_seconds() // 60
        if walk is not None and early > 0:
            return f"To reach {h.abbreviation} you would leave {int(early)} min early."
    return None


# ---------------------------------------------------------------------- HTML


def e(v: Any) -> str:
    return escape("" if v is None else str(v), quote=True)


def _head(title: str, view: str, demo: bool, plans: Iterable[str], now: float, sweep: str | None = None,
          empty: bool = False) -> str:
    ids = ",".join(plans)
    banner = ('<div class="demo" role="status">Demo data. A fake Events API runs in this process. '
              "Nothing is sent to AWS.</div>") if demo else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{e(title)}</title><link rel="stylesheet" href="/static/app.css">'
            '<link rel="manifest" href="/static/manifest.webmanifest">'
            '<link rel="apple-touch-icon" href="/static/icon-180.png">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-title" content="re:Seat">'
            '<meta name="theme-color" content="#1b1b1f">'
            f'<script src="/static/app.js" defer></script></head>'
            f'<body class="v-{view}" data-plans="{e(ids)}" data-now="{now:.0f}" data-sweep="{e(sweep or "")}"'
            f'{" data-empty=1" if empty else ""}>{banner}')


def render_starting(now: float, demo: bool = False, error: str | None = None) -> str:
    """Before the watcher's first snapshot, or when the view failed to build. Reloads itself."""
    text = (f"The page could not be built: {e(error)}. The watcher keeps running. The CLI and the journal "
            "still work. This page tries again in a few seconds." if error
            else "Starting. The first sweep is running. This page reloads in a few seconds.")
    return (_head("re:Seat", "week", demo, [], now, empty=True)
            + f'<main class="phone"><p>{text}</p></main></body></html>')


def _nav(view: str) -> str:
    links = [("approve", "/approve", "Approve"), ("today", "/today", "Today"), ("week", "/", "Week")]
    a = "".join(f'<a href="{href}"{" aria-current=page" if key == view else ""}>{label}</a>'
                for key, href, label in links)
    return f'<nav class="tabs" aria-label="Views">{a}</nav>'


def _status_line(s: dict[str, Any]) -> str:
    sweep = (f"<span>· last sweep {e(s['last_sweep'])}</span><span>· {s['sessions']:,} sessions</span>"
             if s["last_sweep"] else "<span>· no sweep yet</span>")
    nxt = f'<span>· next in <b data-until="{s["next_at"]:.0f}">…</b></span>' if s["next_at"] else ""
    return (f'<header class="top"><a class="brand" href="/">re:Seat</a>'
            f'<div class="status" data-state="{e(s["state"])}"><i class="dot"></i>'
            f'<span data-field="text">{e(s["text"])}</span><span>{e(s["where"])}</span>{sweep}{nxt}</div>'
            f'<div class="grow"></div><div class="counts">{s["held"]} held · {s["wanted"]} wanted · '
            f'quota ListSessions {s["quota_left"]}/{s["quota_max"]}</div></header>')


def _checks(checks: list[dict[str, Any]]) -> str:
    mark = {"yes": "✓", "no": "✗", "ask": "!"}
    li = "".join(f'<li class="c-{e(c["ok"])}"><span aria-hidden="true">{mark[c["ok"]]}</span>'
                 f'<span>{e(c["text"])}</span></li>' for c in checks)
    return f'<ul class="checks">{li}</ul>'


def _buttons(c: dict[str, Any]) -> str:
    return (f'<div class="actions"><button class="primary" data-approve="{e(c["plan_id"])}">Swap now</button>'
            f'<button data-skip="{e(c["plan_id"])}">{e(c["keep"])}</button></div>'
            f'<p class="result" role="status" aria-live="polite"></p>')


def render_dashboard(snap: dict[str, Any], demo: bool = False) -> str:
    v = snap.get("view") or {}
    cards = v.get("cards", [])
    plans = [c["plan_id"] for c in cards]
    out = [_head("re:Seat", "week", demo, plans, snap["now"], v["status"]["last_sweep"]),
           _status_line(v["status"]),
           '<div class="layout"><aside class="side">', _kept_html(v.get("kept"))
           + f'<h2>Needs your approval <span class="count">{len(cards)}</span></h2>']
    for c in cards:
        out.append(f'<article class="card"><div class="kind">{e(c["kind"]).upper()} · {e(c["at"])}</div>'
                   f'<h3>{e(c["title"])}</h3><p>You hold <strong>{e(c["held"]["code"])}</strong>, '
                   f'{e(c["held"]["when"])}, {e(c["held"]["venue"])}. '
                   f'{"A new sitting appeared" if c["kind"] == "New sitting" else "A seat opened"} for '
                   f'<strong>{e(c["wanted"]["code"])}</strong>, {e(c["wanted"]["when"])}, '
                   f'{e(c["wanted"]["venue"])}.</p>{_checks(c["checks"])}{_buttons(c)}'
                   f'<p class="fine">{e(c["sequence"])}</p><p class="fine">{e(c["note"])}</p></article>')
    if not cards:
        out.append('<p class="empty">Nothing waits for you. Swaps that would cancel a seat appear here.</p>')
    out.append('<p class="fine">Bookings within your rules happen at once and show under Last changes.</p>')
    out.append('<h2>Last changes</h2><ul class="mono list">')
    out += [f'<li>{e(x["at"])} {e(x["text"])}</li>' for x in v.get("changes", [])] or [
        '<li>No changes since the first sweep.</li>']
    out.append("</ul>")
    if v.get("scenarios"):
        out.append('<section class="panel scenarios" aria-label="Demo scenarios"><h2>Try a scenario</h2>'
                   '<p class="fine">Each one changes the fake Events API the way the real one could, runs a '
                   'real sweep, and shows you what happens. With --push, watch your phone too.</p><ul>')
        out += [f'<li><button data-scenario="{e(s["key"])}">{e(s["label"])}</button>'
                f'<span class="fine">You should see: {e(s["see"])}</span></li>' for s in v["scenarios"]]
        out.append('</ul><p class="result" role="status" aria-live="polite"></p></section>')
    out.append("</aside><main class='main'>")
    out.append(_week_html(v["week"]))
    out.append('<section class="panel"><h2>Journal</h2><ul class="mono list journal">')
    out += [f'<li><span class="t">{e(j["at"])}</span> {e(j["op"])} <span>{e(j["status"])}</span> '
            f'<span class="o o-{e(j["outcome"].split(":")[-1])}">{e(j["outcome"])}</span> {e(j["what"])}</li>'
            for j in v.get("log", [])] or ["<li>The journal is empty.</li>"]
    out.append("</ul></section></main></div>" + _nav("week") + "</body></html>")
    return "".join(out)


def _kept_html(k: dict[str, Any] | None) -> str:
    if not k:
        return ""
    since = f"since {e(k['since'])}" if k["since"] else "nothing yet"
    cells = "".join(f'<div><b>{n}</b><span>{label}</span></div>' for n, label in (
        (k["booked"], "seats booked"), (k["swapped"], "swaps verified"), (k["restored"], "seats restored")))
    return (f'<section class="kept" aria-label="What re:Seat has done"><div class="kept-row">{cells}</div>'
            f'<p class="fine">From the journal, {since}. Every one read back from your schedule.</p>'
            "</section>")


def _week_html(w: dict[str, Any]) -> str:
    legend = "".join(f'<span class="lg lg-{k}"><i></i>{label}</span>' for k, label in (
        ("held", "Held"), ("wanted", "Wanted"), ("proposed", "Proposed"), ("leave", "Leave now"),
        ("fallback", "Fallback")))
    hours = "".join(f'<div class="hr" style="top:{h["top"]}%"><span>{e(h["label"])}</span></div>'
                    for h in w["hours"])
    cols = []
    for i, label in enumerate(w["days"]):
        inner = []
        for b in (x for x in w["blocks"] if x["day"] == i):
            width = 100 / b["lanes"]
            badge = f'<em class="badge">{e(b["badge"])}</em>' if b["badge"] else ""
            inner.append(f'<div class="blk b-{e(b["kind"])}{" pending" if b["pending"] else ""}" '
                         f'title="{e(b["title"])}" style="top:{b["top"]}%;height:{b["height"]}%;'
                         f'left:{b["lane"] * width:.2f}%;width:{width:.2f}%"><b>{e(b["code"])}</b>'
                         f'<span>{e(b["sub"])}</span>{badge}</div>')
        for lv in (x for x in w["leaves"] if x["day"] == i):
            inner.append(f'<div class="leave" style="top:{lv["top"]}%" title="{e(lv["title"])}">'
                         f'<span class="sr">{e(lv["title"])}</span></div>')
        cols.append(f'<div class="col"><div class="dh">{e(label)}</div><div class="slots">{"".join(inner)}'
                    f'</div></div>')
    return (f'<div class="weekhead"><h1>{e(w["title"])}</h1><div class="legend">{legend}</div></div>'
            f'<div class="scroll"><div class="week"><div class="axis"><div class="dh"></div>'
            f'<div class="slots">{hours}</div></div>{"".join(cols)}</div></div>')


def _phone_top(left: str, right: str, state: str) -> str:
    return (f'<header class="ptop"><div class="brand">{e(left)}</div><div class="grow"></div>'
            f'<div class="status" data-state="{e(state)}"><i class="dot"></i>{e(right)}</div></header>')


def render_approve(snap: dict[str, Any], demo: bool = False, push: bool = False) -> str:
    v = snap.get("view") or {}
    cards, s = v.get("cards", []), v["status"]
    online = "laptop online" if s["state"] == "watching" else s["text"]
    plans = [c["plan_id"] for c in cards]
    out = [_head("re:Seat approve", "approve", demo, plans, snap["now"], s["last_sweep"]),
           _phone_top("re:Seat", f"{online} · {hm(snap['now'])}", s["state"]), '<main class="phone">',
           f'<div class="mono muted">{len(cards)} waiting</div>']
    for i, c in enumerate(cards):
        if i == 0:
            after = ("Runs on your laptop. You will get a push when it is verified." if push
                     else "Runs on your laptop. This page shows the result when it is verified.")
            out.append(f'<article class="card big"><div class="kind">{e(c["kind"]).upper()}</div>'
                       f'<h3>{e(c["question"])}</h3><div class="pair">'
                       f'{_side_html("You hold", c["held"], "hold")}'
                       f'{_side_html("Opened", c["wanted"], "opened")}'
                       f'</div>{_checks(c["checks"])}{_buttons(c)}<p class="fine center">{e(after)}</p>'
                       f'<p class="fine">{e(c["sequence"])}</p></article>')
        else:
            out.append(f'<article class="card dim"><div class="kind">{e(c["kind"]).upper()}</div>'
                       f'<div><strong>{e(c["wanted"]["code"])}</strong>, {e(c["wanted"]["when"])}, '
                       f'{e(c["wanted"]["venue"])}</div><div class="mono muted">next</div></article>')
    if not cards:
        out.append('<p class="empty">Nothing waits for your approval.</p>')
    for b in v.get("bookings", []):
        out.append(f'<article class="card booked"><div class="kind ok">BOOKED {e(b["at"])}</div>'
                   f'<div><strong>{e(b["code"])}</strong>, {e(b["when"])}, {e(b["venue"])}</div>'
                   f'<div class="muted">Within your rules. Read back ok.</div></article>')
    out.append("</main>" + _nav("approve") + "</body></html>")
    return "".join(out)


def _side_html(label: str, side: dict[str, Any], cls: str) -> str:
    return (f'<div class="side-{cls}"><div class="lbl">{e(label)}</div>'
            f'<div class="mono">{e(side["code"])}</div>'
            f'<div>{e(side["when"])}</div><div class="muted">{e(side["venue"])}</div></div>')


def render_today(snap: dict[str, Any], demo: bool = False) -> str:
    v = snap.get("view") or {}
    t, s = v["today"], v["status"]
    out = [_head("re:Seat today", "today", demo, [c["plan_id"] for c in v.get("cards", [])], snap["now"],
                 s["last_sweep"]),
           _phone_top(t["label"], hm(snap["now"]), s["state"]), '<main class="phone">']
    h = t["hero"]
    if h:
        walk = (f"Walk {h['origin']} → {h['venue']}, {h['walk']} min. " if h["walk"] is not None
                else f"Be at {h['venue']} by the doors. ")
        mins = math.ceil(max(0, h["leave_at"] - snap["now"]) / 60)
        label, big = ("LEAVE IN", f"{mins} min") if mins > 0 else ("LEAVE NOW", "now")
        out.append(f'<section class="hero"><div class="mono" data-leave-label>{label}</div>'
                   f'<div class="big" data-leave="{h["leave_at"]:.0f}">{big}</div>'
                   f'<div>for <strong>{e(h["code"])}</strong> at {e(h["start"])}, {e(h["venue"])}'
                   f'{", " + e(h["room"]) if h["room"] else ""}</div>'
                   f'<div class="small">{e(walk)}Doors close {e(h["doors"])}.</div></section>')
    else:
        out.append('<section class="panel"><p>No walk to start today. Nothing held is still ahead.</p>'
                   "</section>")
    out.append('<section class="panel"><h2>Next</h2>')
    out += [f'<div class="row"><div class="mono t">{e(n["time"])}</div><div><strong>{e(n["name"])}</strong>'
            f'<div class="muted">{e(n["sub"])}</div></div></div>' for n in t["next"]] or [
        '<p class="muted">Nothing else today.</p>']
    out.append('</section><section class="panel"><h2>Wanted today, not held</h2>')
    for w in t["wanted"]:
        clash = f'<div class="bad">{e(w["clash"])}</div>' if w["clash"] else ""
        out.append(f'<div class="want"><div class="split"><strong>{e(w["code"])} · {e(w["time"])}</strong>'
                   f'<span class="mono band band-{e(w["band"])}">{e(w["band"])}</span></div>'
                   f'<div class="muted">{e(w["text"])}</div>{clash}</div>')
    if not t["wanted"]:
        out.append('<p class="muted">Nothing wanted today that is not held.</p>')
    out.append(f'</section><section class="panel foot"><i class="dot"></i><span class="mono">{e(t["cover"])}'
               f'</span></section></main>' + _nav("today") + "</body></html>")
    return "".join(out)
