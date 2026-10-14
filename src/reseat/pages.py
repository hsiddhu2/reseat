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
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Any

from . import decisions, guard
from .campus import CUTOFF_MINUTES, EVENT_DAYS, VEGAS, overlaps, room_label, walk_minutes
from .changes import GHOSTS
from .client import QUOTAS
from .models import PersonalTime, Session
from .router import target_name
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


# Share of a day's column for held seats when the day also has wanted or proposed sessions.
WEEK_HELD_AREA = 64.0

# Held blocks on the week grid are coloured by session type, in this order on the legend.
TYPE_CLASS = {"Lab": "lab", "Workshop": "workshop", "Bootcamp": "bootcamp", "Builders' session": "builders",
              "Code talk": "code", "Chalk talk": "chalk", "Breakout session": "breakout",
              "Lightning talk": "lightning", "Keynote": "keynote", "Gamified learning": "gamified",
              "Exam prep": "exam"}


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, VEGAS)


def hm(ts: float) -> str:
    return local(ts).strftime("%H:%M")


def stamp(ts: float, seconds: bool = False) -> str:
    """A logged time a person can place: day, date, time and the zone, such as Mon 12 Oct 22:40:06 PT.
    re:Seat keeps Las Vegas time for the whole event, and the viewer's own clock may differ."""
    d = local(ts)
    clock = d.strftime("%H:%M:%S" if seconds else "%H:%M")
    return f"{d:%a} {d.day} {d:%b} {clock} PT"


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
        "bookings": _bookings(app, now, set(held_ids)),
        "kept": _kept(app),
        "scenarios": list(app.scenarios),
        "last": dict(app.last_scenario) if app.last_scenario else None,
        "recent": [_change(c) for c in reversed(app.tracker.recent(now))],
        "week_log": [_change(c) for c in reversed(app.tracker.log)],
        "clock": f"{local(now):%a %d %b, %H:%M}",
        "tour": app.tour_label,
        "blocks": dict(app.blocks_state) if app.blocks_state else None,
        "targets": _targets(app, held_ids, pending),
        "health": _health(app, now, last_sweep),
    }


def _targets(app: App, held_ids: list[str], pending: list[Any]) -> list[dict[str, Any]]:
    """Every target in rule order: what is held or proposed, and each sitting with its band. Codes only:
    a target pinned by session id is named by its sitting's code."""
    held, proposed = set(held_ids), {p.wanted_id for p in pending}
    out = []
    for i, t in enumerate(app.rules.targets, 1):
        tree = app.watcher.router.tree(t)
        own = [s for s, b in tree if not b]             # the target's own sittings, not its backups
        first = own[0] if own else None
        name = t.code or (first.abbreviation if first else "A pinned sitting not in the catalog yet")
        got = next((s for s, _ in tree if s.session_id in held), None)
        offer = next((s for s, _ in tree if s.session_id in proposed), None)
        if got:
            state, text = "held", f"Held: {got.abbreviation}, {_when(got)}, {got.campus_venue or ''}"
        elif offer:
            state, text = "proposed", f"Swap proposed: {offer.abbreviation}, {_when(offer)}"
        elif not own:
            state, text = "none", "No sitting in the catalog yet" + (", watching the backups" if tree else "")
        else:
            state, text = "waiting", "Not held. Watching every sitting below"
        notes = []
        if not t.repeats:
            notes.append("only the sittings you picked")
        if t.auto_swap:
            notes.append("swaps without asking")
        if t.backups:
            notes.append(f"backups: {', '.join(t.backups)}")
        out.append({"n": i, "name": name, "title": t.title or (first.title if first else ""), "state": state,
                    "text": text, "notes": notes,
                    "sittings": [{"code": s.abbreviation, "when": _when(s), "venue": s.campus_venue or "",
                                  "band": s.seat_availability or "not shown", "backup": b,
                                  "held": s.session_id in held, "proposed": s.session_id in proposed}
                                 for s, b in tree]})
    return out


def _health(app: App, now: float, last_sweep: float | None) -> dict[str, Any]:
    """What re:Seat knows about itself. Never the push topic, the secret or the token."""
    w, r = app.watcher, app.rules
    rows = [
        ("Watcher", "Reading only until you sign in again" if w.read_only else "Watching"),
        ("Events API", f"Unreachable since {stamp(w.down_since)}" if w.down_since else "Reachable"),
        ("Bookings", f"Closed (409). Next try {hm(w.writes_closed_until)}" if w.writes_closed_until
         else "Open"),
        ("Last sweep", stamp(last_sweep, seconds=True) if last_sweep else "None yet"),
        ("Leave-now blocks", f"{sum(1 for p in w.personal_time if guard.is_mine(p))} in your schedule"
         + (", out of date" if app.blocks_state else "")),
        ("Push", ("On" + (", Swap now and Keep in the notification" if r.ntfy_approve else ""))
         if r.ntfy_topic else "Off. Set ntfy_topic in the rules file"),
        ("Quiet hours", f"{r.notify.quiet}, Las Vegas time" if r.notify.quiet else "None"),
        ("Muted", ", ".join(r.notify.mute) or "Nothing"),
    ]
    quota = [{"op": op, "left": w.client.quota.remaining(op), "max": n} for op, n in QUOTAS.items()]
    return {"rows": rows, "quota": quota}


def _change(c: Any) -> dict[str, str]:
    return {"kind": c.kind, "label": c.label, "text": c.text(), "at": stamp(c.at)}


def _status(app: App, now: float, last_sweep: float | None, sweeps: list[tuple[float, int]],
            held_ids: list[str], wanted: list[tuple[Any, list[Session]]]) -> dict[str, Any]:
    w = app.watcher
    if w.read_only:
        state, text = "signin", "Sign in needed. Reading only"
    elif w.down_since is not None:
        state, text = "offline", f"API unreachable since {stamp(w.down_since)}"
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
        "plan_id": p.plan_id, "target": target_name(p.target, app.store, app.event_id),
        "kind": "New sitting" if added else "Seat opened", "at": stamp(p.created),
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
    return [{"at": stamp(ts), "text": text} for ts, text in rows[:CHANGES]]


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
        out.append({"at": stamp(r["ts"], seconds=True), "op": r["op"], "status": _status_of(resp),
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


def _bookings(app: App, now: float, held: set[str]) -> list[dict[str, Any]]:
    """Seats booked in the last 24 hours that are still held. A seat cancelled since is left out,
    and the day is shown, because 24 hours spans two dates."""
    out, seen = [], set()
    for r in app.store.journal_entries(app.event_id, 200):        # newest first
        if r["op"] == "ReserveSessions" and r["ts"] > now - 86400:
            for sid in _booked(r):
                if sid not in held or sid in seen:
                    continue
                seen.add(sid)
                s = app.store.get(app.event_id, sid)
                out.append({"at": stamp(r["ts"]), "code": _code(app, sid),
                            "when": _when(s) if s else "",
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
                       "name": s.title, "time": f"{a:%H:%M}–{b:%H:%M}", "session": s,
                       "moved": moved.get(s.session_id) if s.session_id in moved else None,
                       "sub": sub, "pending": kind == "held" and s.session_id in swapping,
                       "top": _pct(max(top, DAY_START)),
                       "height": _pct(min(bottom, DAY_END)) - _pct(max(top, DAY_START)),
                       "start": a, "end": b, "venue": s.campus_venue, "badge": None,
                       "type": s.type_key})
    _ghosts(app, blocks, days, now)
    _lanes(blocks)
    _badges(blocks)
    leaves = []
    blocks_now, _ = guard.leave_blocks(guard.held_sessions(st, ev, [s.session_id for s in held]), app.rules)
    for blk in blocks_now:
        at = datetime.fromisoformat(blk.start).replace(tzinfo=UTC).astimezone(VEGAS)
        for b in blocks:
            if b["kind"] == "held" and b["session"].session_id == blk.session_id:
                b["leave"] = f"{at:%H:%M}"
        if at.date().isoformat() in days and _minutes(at) < DAY_END:      # 07:31 for an 08:00 start too
            leaves.append({"day": days.index(at.date().isoformat()), "top": _pct(_minutes(at)),
                           "title": f"Leave {at:%H:%M} for {blk.code}"})
    labels = [f"{datetime.fromisoformat(d):%a} {datetime.fromisoformat(d).day}" for d in days]
    first, last = datetime.fromisoformat(days[0]), datetime.fromisoformat(days[-1])
    leave_at = {b.session_id: b for b in blocks_now}
    held_ids = {s.session_id for s in held}
    swaps = {p.wanted_id: p.held_id for p in pending}
    for i, blk in enumerate(blocks):
        blk["i"] = i
        if blk["kind"] != "ghost":
            blk["detail"] = _detail(app, blk, leave_at.get(blk["session"].session_id), held_ids, swaps)
    for blk in blocks:
        blk.pop("start"), blk.pop("end"), blk.pop("venue"), blk.pop("session")
    t = local(now)
    now_line = ({"day": days.index(t.date().isoformat()), "top": _pct(_minutes(t)), "label": f"{t:%H:%M}"}
                if t.date().isoformat() in days and DAY_START <= _minutes(t) < DAY_END else None)
    return {"title": f"Week of {first.day} {first:%b} – {last.day} {last:%b}", "days": labels,
            "blocks": blocks, "leaves": leaves, "now": now_line,
            "hours": [{"label": f"{h}:00", "top": _pct(h * 60)} for h in range(8, 19, 2)]}


STATUS = {"held": "You hold this seat.", "wanted": "You want this talk and do not hold it.",
          "proposed": "A swap is proposed: re:Seat would book this instead of a seat you hold.",
          "fallback": "The fallback: if the proposed swap fails, re:Seat books this."}


def _detail(app: App, blk: dict[str, Any], leave: guard.Block | None, held: set[str],
            swaps: dict[str, str]) -> dict[str, Any]:
    """Everything the detail panel shows for one block, from the store, the guard and the rules."""
    st, ev = app.store, app.event_id
    s: Session = blk["session"]
    a = _start(s)
    status = STATUS[blk["kind"]]
    if s.session_id in swaps:
        status = f"A swap is proposed: re:Seat would book this instead of {_code(app, swaps[s.session_id])}."
    rows = [("When", f"{a:%A} {a.day} {a:%B}, {blk['time']}"),
            ("Where", ", ".join(x for x in (s.campus_venue, s.room_label) if x) or "Not set"),
            ("Type", s.type_key or "Not set"), ("Seats now", s.seat_availability or "Not shown")]
    if leave:
        at = datetime.fromisoformat(leave.start).replace(tzinfo=UTC).astimezone(VEGAS)
        walk = (f", about {leave.walk} min walk from {leave.origin}, {leave.allow} allowed (estimate)"
                if leave.walk is not None
                else "")
        rows.append(("Leave", f"{at:%H:%M}{walk}. Doors close {CUTOFF_MINUTES} min before the start."))
    if blk.get("moved"):
        rows.append(("Moved", f"Now in {blk['moved']}"))
    if blk.get("walk_note"):
        rows.append(("Tight walk", blk["walk_note"]))
    bands = st.band_history(ev, s.session_id)
    if s.session_id not in held:
        adv = guard.queue_or_go(s, bands)
        rows.append(("Queue or go", f"{VERDICT.get(adv.verdict, '')} {adv.basis}".strip()))
    history = [f"{stamp(ts)} {old or 'none'} → {new or 'none'}" for ts, old, new in bands[-5:]
               if old is not None]
    sittings = [{"code": x.abbreviation, "when": _when(x), "venue": x.campus_venue or "",
                 "band": x.seat_availability or "none", "held": x.session_id in held}
                for x in (st.by_base_code(ev, s.base_code) if s.base_code else [])
                if x.session_id != s.session_id]
    return {"code": s.abbreviation, "title": s.title, "status": status, "rows": rows,
            "history": history, "sittings": sittings}


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _pct(minutes: int) -> float:
    return round((minutes - DAY_START) / (DAY_END - DAY_START) * 100, 3)


def _ghosts(app: App, blocks: list[dict[str, Any]], days: list[str], now: float) -> None:
    """Label sessions that changed in the last few minutes, and keep a crossed-out ghost where a held
    session was released or removed, so the week shows what just happened."""
    recent = app.tracker.recent(now)
    latest = {c.place.code: c for c in recent}
    here = {b["code"] for b in blocks}
    for c in recent:
        p = c.place
        if c.kind not in GHOSTS or p.code in here or p.start.date().isoformat() not in days:
            continue
        top, bottom = _minutes(p.start), _minutes(p.end)
        if bottom <= DAY_START or top >= DAY_END:
            continue
        here.add(p.code)
        blocks.append({"day": days.index(p.start.date().isoformat()), "kind": "ghost", "code": p.code,
                       "title": f"{p.code} {p.title} · {c.label.lower()} at {hm(c.at)}",
                       "name": p.title, "time": f"{p.start:%H:%M}–{p.end:%H:%M}", "session": None,
                       "moved": None, "sub": f"{c.label.capitalize()} at {hm(c.at)}", "pending": False,
                       "top": _pct(max(top, DAY_START)),
                       "height": _pct(min(bottom, DAY_END)) - _pct(max(top, DAY_START)),
                       "start": p.start, "end": p.end, "venue": p.venue, "badge": None,
                       "detail": {"code": p.code, "title": p.title, "rows": [("Was", p.where())],
                                  "status": f"{c.text()}. {c.label.capitalize()} at {hm(c.at)}.",
                                  "history": [], "sittings": []}})
    for b in blocks:
        c = latest.get(b["code"])
        show = c and (c.kind != "released" or b["kind"] != "held")   # released: the seat is no longer held
        b["mark"] = {"label": c.label, "kind": c.kind} if show and c else None


def _lanes(blocks: list[dict[str, Any]]) -> None:
    """Side by side when blocks overlap in one day. Held seats share lanes only with each other, so a
    wanted talk never narrows a seat you hold. The rest go in a strip on the right of the day."""
    for b in blocks:
        b["strip"] = b["kind"] != "held"
    for day in {b["day"] for b in blocks}:
        for strip in (False, True):
            _lane_group([b for b in blocks if b["day"] == day and b["strip"] == strip])


def _lane_group(blocks: list[dict[str, Any]]) -> None:
    """Lanes for blocks that may overlap: each takes the first lane free at its start."""
    groups: list[list[dict[str, Any]]] = []
    for b in sorted(blocks, key=lambda b: b["start"]):
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
        if b["kind"] == "ghost":
            continue
        prev = [h for h in blocks if h["kind"] == "held" and h["day"] == b["day"] and h["end"] <= b["start"]
                and h is not b]
        if not prev:
            continue
        p = max(prev, key=lambda h: h["end"])
        walk = walk_minutes(p["venue"], b["venue"])
        gap = int((b["start"] - p["end"]).total_seconds() // 60)
        if walk is not None and walk > gap:
            b["badge"] = f"walk about {walk} m (estimate), gap {gap} m"
            b["walk_note"] = (f"About {walk} min walk (estimate) from {p['code']} at {p['venue']}, "
                              f"but only {gap} min between them.")


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
                    "room": s.room_label or "", "origin": b.origin or "", "walk": b.walk, "allow": b.allow,
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
                         "clash": _clash(s, today, app.rules.arrive_confidence)})
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


def _clash(s: Session, held_today: list[Session], confidence: float) -> str | None:
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
            return (f"After {h.abbreviation} the walk is about {walk} min (estimate). You would miss the "
                    f"first {int(late)} min.")
    if after:
        h = min(after, key=lambda x: _window(x)[0])  # type: ignore[index]
        walk = walk_minutes(s.campus_venue, h.campus_venue)
        allow = decisions.walk_allowance(walk, confidence) if walk is not None else 0
        leave = _window(h)[0] - timedelta(minutes=CUTOFF_MINUTES + allow)  # type: ignore[index]  # as the block
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
            + f'<main class="page"><p>{text}</p></main></body></html>')


MAIN = [("week", "/", "Week", "Your calendar, and swaps to approve"),
        ("today", "/today", "Today", "When to leave, what is next"),
        ("approve", "/approve", "Approve", "One swap at a time"),
        ("activity", "/activity", "Activity", "Every change and every write")]
MORE = [("targets", "/targets", "Targets", "Your rules, in order, and each sitting"),
        ("status", "/status", "Status", "Watcher, quota left, push settings")]
DEMO = ("demo", "/demo", "Demo", "Try every scenario on demo data")


def _nav(view: str, demo: bool = False, waiting: int = 0) -> str:
    """One menu for every page. Under the header on a laptop, every page in one row. At phone width a
    bottom bar: the four main pages and More, which opens the rest."""
    more = MORE + ([DEMO] if demo else [])

    def link(key: str, href: str, label: str, cls: str) -> str:
        badge = f' <span class="count">{waiting}</span>' if key == "approve" and waiting else ""
        cur = " aria-current=page" if key == view else ""
        return f'<a class="{cls}" href="{href}"{cur}>{e(label)}{badge}</a>'
    main = "".join(link(k, h, lbl, "t-main") for k, h, lbl, _ in MAIN)
    wide = "".join(link(k, h, lbl, "t-wide") for k, h, lbl, _ in more)
    here = " here" if view in {k for k, *_ in more} else ""
    items = "".join(f'<a href="{h}"{" aria-current=page" if k == view else ""}><b>{e(lbl)}</b>'
                    f"<span>{e(why)}</span></a>" for k, h, lbl, why in more)
    return (f'<nav class="tabs" aria-label="Pages">{main}{wide}<details class="more{here}"><summary>More'
            f'</summary><div class="menu">{items}</div></details></nav>')


def _on_top(html: str, view: str, demo: bool) -> str:
    """Put the menu right under the header, with the number of swaps waiting on Approve."""
    m = re.search(r'data-plans="([^"]*)"', html)
    waiting = len([p for p in (m.group(1) if m else "").split(",") if p])
    nav = _nav(view, demo, waiting)
    i = html.find("</header>")
    if i < 0:
        return html.replace("</body>", nav + "</body>", 1)
    i += len("</header>")
    return html[:i] + nav + html[i:]


def _status_line(s: dict[str, Any]) -> str:
    return _status_bar(s)


def _status_bar(s: dict[str, Any]) -> str:
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
    """The Week view: what needs approval, and the week with what just changed on it."""
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
    out.append('<p class="fine">Bookings within your rules happen at once. Changed sessions are labelled on '
               'the week for 15 minutes. Everything is listed under <a href="/activity">Activity</a>.</p>')
    recent = v.get("recent") or []
    if recent:
        out.append('<h2>Just changed</h2><ul class="list changed">')
        out += [f'<li><em class="mk mk-{e(c["kind"])}">{e(c["label"])}</em> {e(c["text"])}</li>'
                for c in recent]
        out.append("</ul>")
    out.append("</aside><main class='main'>")
    out.append(_week_html(v["week"]))
    out.append("</main></div></body></html>")
    return _on_top("".join(out), "week", demo)


def render_activity(snap: dict[str, Any], demo: bool = False) -> str:
    """Activity: what re:Seat has done, what changed in the catalog, and every write in the journal."""
    v = snap.get("view") or {}
    out = [_head("re:Seat · Activity", "activity", demo, [c["plan_id"] for c in v.get("cards", [])],
                 snap["now"], v["status"]["last_sweep"]),
           _status_line(v["status"]), '<main class="page"><h1>Activity</h1><p class="intro">What re:Seat '
           "did and saw, newest first: changes to your week, changes in the catalog, and every write it "
           "sent to the Events API with its read-back.</p>", _kept_html(v.get("kept")),
           '<section class="panel"><h2>Changes on your week</h2><ul class="list changed">']
    out += [f'<li><span class="t mono">{e(c["at"])}</span> <em class="mk mk-{e(c["kind"])}">{e(c["label"])}'
            f'</em> {e(c["text"])}</li>' for c in v.get("week_log", [])] or [
        "<li>No change to your week since re:Seat started.</li>"]
    out.append('</ul></section><section class="panel"><h2>Catalog changes</h2>'
               '<p class="fine">Seat bands, new sittings, moves and bookings from the sweeps.</p>'
               '<ul class="mono list">')
    out += [f'<li>{e(x["at"])} {e(x["text"])}</li>' for x in v.get("changes", [])] or [
        '<li>No changes since the first sweep.</li>']
    out.append('</ul></section><section class="panel"><h2>Journal</h2><p class="fine">Every write to the '
               'Events API and its read-back.</p><ul class="mono list journal">')
    out += [f'<li><span class="t">{e(j["at"])}</span> {e(j["op"])} <span>{e(j["status"])}</span> '
            f'<span class="o o-{e(j["outcome"].split(":")[-1])}">{e(j["outcome"])}</span> {e(j["what"])}</li>'
            for j in v.get("log", [])] or ["<li>The journal is empty.</li>"]
    out.append("</ul></section></main></body></html>")
    return _on_top("".join(out), "activity", demo)


def _page(snap: dict[str, Any], title: str, view: str, demo: bool, body: str) -> str:
    v = snap.get("view") or {}
    return _on_top(_head(f"re:Seat · {title}", view, demo, [c["plan_id"] for c in v.get("cards", [])],
                         snap["now"], v["status"]["last_sweep"])
                   + _status_line(v["status"]) + f'<main class="page">{body}</main></body></html>',
                   view, demo)


def render_targets(snap: dict[str, Any], demo: bool = False) -> str:
    """Targets: the rules file read back, in order, with what re:Seat holds or watches for each."""
    v = snap.get("view") or {}
    out = ['<h1>Targets</h1><p class="intro">In the order of your rules file. re:Seat books the first '
           "sitting that has a seat and fits your day, and asks before any swap.</p>"]
    for t in v.get("targets", []):
        rows = "".join(
            f'<li class="r-{"held" if x["held"] else "proposed" if x["proposed"] else "plain"}">'
            f'<b>{e(x["code"])}</b> <span>{e(x["when"])}, {e(x["venue"])}</span> '
            f'<span class="band">{e(x["band"])}</span>{" · backup" if x["backup"] else ""}'
            f'{" · held" if x["held"] else ""}{" · proposed" if x["proposed"] else ""}</li>'
            for x in t["sittings"])
        notes = f'<p class="fine">{e("; ".join(t["notes"]))}</p>' if t["notes"] else ""
        out.append(f'<section class="panel target t-{e(t["state"])}"><div class="kind">#{t["n"]} '
                   f'{e(t["name"])}</div><h3>{e(t["title"])}</h3><p>{e(t["text"])}</p>{notes}'
                   f'<ul class="rows">{rows}</ul></section>')
    if not v.get("targets"):
        out.append('<p class="empty">No targets yet. Add some to the rules file, or run reseat rules init.'
                   "</p>")
    return _page(snap, "Targets", "targets", demo, "".join(out))


def _share(left: int, most: int) -> int:
    return max(0, min(100, round(100 * left / most))) if most else 0


def render_status(snap: dict[str, Any], demo: bool = False, version: str = "") -> str:
    """Status: how re:Seat is doing, the per-minute quotas left, and the push settings."""
    h = (snap.get("view") or {}).get("health") or {"rows": [], "quota": []}
    rows = "".join(f"<dt>{e(k)}</dt><dd>{e(val)}</dd>" for k, val in h["rows"])
    quota = "".join(f'<li><span class="mono">{e(q["op"])}</span> <b>{q["left"]}</b> of {q["max"]} left'
                    f'<i style="width:{_share(q["left"], q["max"])}%"></i></li>' for q in h["quota"])
    body = ('<h1>Status</h1><p class="intro">How re:Seat is doing right now. Check here when a push said '
            "it is offline, paused or needs a sign-in.</p>"
            f'<section class="panel"><dl class="facts">{rows}</dl>'
            f'<p class="fine">re:Seat {e(version)}. Push topic, sign-in and secrets are never shown here.</p>'
            '</section><section class="panel"><h2>Quota left this minute</h2><p class="fine">AWS counts each '
            "operation per minute, and bulk calls by session. re:Seat never sends more than is left.</p>"
            f'<ul class="quota">{quota}</ul></section>')
    return _page(snap, "Status", "status", demo, body)


def render_day(snap: dict[str, Any], index: int, demo: bool = False) -> str:
    """One day of the week in full: every session in time order with its details, and the leave times."""
    w = (snap.get("view") or {}).get("week") or {"days": [], "blocks": [], "leaves": []}
    if not 0 <= index < len(w["days"]):
        index = 0
    items = sorted((b for b in w["blocks"] if b["day"] == index), key=lambda b: b["top"])
    leaves = sorted((lv for lv in w["leaves"] if lv["day"] == index), key=lambda lv: lv["top"])
    days = "".join(f'<a href="/day?d={i}"{" aria-current=page" if i == index else ""}>{e(d)}</a>'
                   for i, d in enumerate(w["days"]))
    out = [f'<nav class="daytabs" aria-label="Days">{days}</nav>'
           f'<h1>{e(w["days"][index]) if w["days"] else ""}</h1>']
    rows_in_time = sorted([(lv["top"], 0, lv) for lv in leaves] + [(b["top"], 1, b) for b in items],
                          key=lambda x: (x[0], x[1]))
    for _, is_session, b in rows_in_time:
        if not is_session:
            out.append(f'<p class="leave-row">{e(b["title"])}</p>')
            continue
        d = b["detail"]
        rows = "".join(f"<dt>{e(k)}</dt><dd>{e(val)}</dd>" for k, val in d["rows"])
        m = b.get("mark")
        mark = f'<em class="mk mk-{e(m["kind"])}">{e(m["label"])}</em> ' if m else ""
        out.append(f'<section class="panel day-item d-{e(b["kind"])}"><div class="kind">{mark}'
                   f'{e(b["time"])} · {e(b["code"])} · {e(b["kind"])}</div><h3>{e(d["title"])}</h3>'
                   f'<p>{e(d["status"])}</p>'
                   f'<dl class="facts">{rows}</dl></section>')
    if not items:
        out.append('<p class="empty">Nothing held or wanted on this day.</p>')
    return _page(snap, "Day", "week", demo, "".join(out))


def render_demo(snap: dict[str, Any]) -> str:
    """Demo mode only. Scenarios on the left. On the right, what the last one did: at AWS, in re:Seat,
    on your week before and after, and on your phone."""
    v = snap.get("view") or {}
    last = v.get("last")
    out = [_head("re:Seat · Demo", "demo", True, [c["plan_id"] for c in v.get("cards", [])], snap["now"],
                 v["status"]["last_sweep"]),
           _status_line(v["status"]), '<div class="demo-page">',
           _scenarios_html(v.get("scenarios", []), v.get("tour", "Start the guided tour")),
           _result_html(last, v.get("clock", "")), "</div>", "</body></html>"]
    return _on_top("".join(out), "demo", True)


def _scenarios_html(scenarios: list[dict[str, str]], tour: str) -> str:
    groups: dict[str, list[dict[str, str]]] = {}
    for sc in scenarios:
        groups.setdefault(sc["group"], []).append(sc)
    out = ['<section class="scenarios" aria-label="Demo scenarios"><h2>Try a scenario</h2>'
           '<p class="fine">Each one makes something happen at the fake AWS, the way the real one can during '
           're:Invent, or moves the demo clock. re:Seat reacts as it would for real.</p>'
           f'<button class="primary tour" data-scenario="tour">{e(tour)}</button>'
           '<p class="result" role="status" aria-live="polite"></p>']
    for group, items in groups.items():
        out.append(f'<h3>{e(group)}</h3><ul class="sc-list">')
        out += [f'<li><div><b>{e(sc["label"])}</b><span>{e(sc["story"])}</span></div>'
                f'<button data-scenario="{e(sc["key"])}">Run</button></li>' for sc in items]
        out.append("</ul>")
    out.append("</section>")
    return "".join(out)


def _result_html(last: dict[str, Any] | None, clock: str) -> str:
    if not last:
        return ('<section class="outcome empty-outcome"><h2>What happens</h2><p>Pick a scenario. This side '
                'then shows four things: what AWS did, what re:Seat did, how your week changed, before and '
                f'after, and what reached your phone.</p><p class="fine">Demo clock: {e(clock)}.</p>'
                "</section>")
    step = f'<div class="kind">{e(last["step"])}</div>' if last.get("step") else ""
    out = [f'<section class="outcome" aria-live="polite">{step}<h2>{e(last["label"])}</h2>'
           f'<p class="fine">Demo clock: {e(last.get("clock") or clock)}</p>',
           f'<div class="step"><i>1</i><div><h3>What AWS did</h3><p>{e(last["story"])}</p></div></div>',
           f'<div class="step"><i>2</i><div><h3>What re:Seat did</h3><p>{e(last["did"])}</p></div></div>',
           '<div class="step"><i>3</i><div><h3>Your week</h3>']
    changes = last.get("changes") or []
    if not changes:
        out.append("<p>Nothing on your week changed.</p>")
    else:
        out.append('<ul class="list changed">' + "".join(
            f'<li><em class="mk mk-{e(c["kind"])}">{e(c["label"])}</em> {e(c["text"])}</li>' for c in changes)
            + "</ul>")
        for d in last.get("days") or []:
            out.append(f'<div class="ba"><h4>{e(d["label"])}</h4><div class="ba-cols">'
                       f'<div><h5>Before</h5>{_rows_html(d["before"])}</div>'
                       f'<div><h5>After</h5>{_rows_html(d["after"])}</div></div></div>')
        out.append('<p><a href="/">See it on the week</a>. Changed sessions are labelled there for 15 '
                   'minutes.</p>')
    out.append('</div></div><div class="step"><i>4</i><div><h3>Your phone</h3>')
    phone = last.get("phone") or []
    if not phone:
        out.append("<p>No notification this time.</p>")
    for n in phone:
        buttons = ('<div class="nb"><span>Swap now</span><span>Keep</span></div>' if n.get("buttons") else "")
        out.append(f'<div class="notif"><div class="na">ntfy · re:Seat</div><b>{e(n["title"])}</b>'
                   f'<p>{e(n["body"])}</p>{buttons}</div>')
    if phone:
        out.append('<p class="fine">Run with --push and these reach your phone through ntfy.</p>')
    if last.get("next"):
        out.append(f'</div></div><p class="fine">Next on the tour: {e(last["next"])}</p></section>'
                   if last.get("step") else "</div></div></section>")
    else:
        out.append("</div></div></section>")
    return "".join(out)


def _rows_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="fine">Nothing held.</p>'
    return '<ul class="rows">' + "".join(
        f'<li class="r-{e(r["state"])}"><span class="mono">{e(r["time"])}</span> <b>{e(r["code"])}</b> '
        f'<span>{e(r["where"])}</span>'
        + (f' <em class="mk mk-{e(r["mark"]["kind"])}">{e(r["mark"]["label"])}</em>' if r.get("mark") else "")
        + "</li>" for r in rows) + "</ul>"


def _kept_html(k: dict[str, Any] | None) -> str:
    if not k:
        return ""
    since = f"since {e(k['since'])}" if k["since"] else "nothing yet"
    cells = "".join(f'<div><b>{n}</b><span>{label}</span></div>' for n, label in (
        (k["booked"], "seats booked"), (k["swapped"], "swaps verified"), (k["restored"], "seats restored")))
    return (f'<section class="kept" aria-label="What re:Seat has done"><div class="kept-row">{cells}</div>'
            f'<p class="fine">From the journal, {since}. Every one read back from your schedule.</p>'
            "</section>")


def type_class(type_key: str | None) -> str:
    """The colour class of a held block. A type the catalog adds later gets the plain held colour."""
    return TYPE_CLASS.get(type_key or "", "other")


def _type_legend(blocks: list[dict[str, Any]]) -> str:
    """One legend entry per session type held this week, in TYPE_CLASS order. Types re:Seat does
    not know share one Other entry, as they share one colour."""
    held = {type_class(b.get("type")) for b in blocks if b["kind"] == "held"}
    names = {cls: name for name, cls in TYPE_CLASS.items()} | {"other": "Other"}
    order = [*TYPE_CLASS.values(), "other"]
    types = "".join(f'<span class="lg lg-held ty-{c}"><i aria-hidden="true"></i>{e(names[c])}</span>'
                    for c in order if c in held)
    return f'<span class="lg-group">Held, by type: {types}</span>' if types else ""


def _week_html(w: dict[str, Any]) -> str:
    legend = _type_legend(w["blocks"]) + "".join(
        f'<span class="lg lg-{k}"><i aria-hidden="true"></i>{label}</span>' for k, label in (
            ("wanted", "Wanted"), ("proposed", "Proposed"), ("leave", "Leave by, for the walk there"),
            ("fallback", "Fallback"), ("ghost", "Released or removed")))
    hours = "".join(f'<div class="hr" style="top:{h["top"]}%"><span>{e(h["label"])}</span></div>'
                    for h in w["hours"])
    cols = []
    for i, label in enumerate(w["days"]):
        inner = []
        mine = [x for x in w["blocks"] if x["day"] == i]
        held_area = WEEK_HELD_AREA if any(x["strip"] for x in mine) else 100.0
        for b in mine:
            area, left0 = (100.0 - held_area - 1, held_area + 1) if b["strip"] else (held_area, 0.0)
            width = area / b["lanes"]
            badge = (f'<em class="badge" title="{e(b["badge"])}" aria-label="Tight walk: {e(b["badge"])}">'
                     '!</em>' if b["badge"] else "")
            hl = " hl" if b.get("mark") else ""
            mk = (f'<em class="mk mk-{e(b["mark"]["kind"])}">{e(b["mark"]["label"])}</em>'
                  if b.get("mark") else "")
            narrow = (" narrow" if b["lanes"] > 1 else "") + (" in-strip" if b["strip"] else "")
            short = b["lanes"] > 1 or b["strip"]             # start time only where space is tight
            ty = f' ty-{type_class(b.get("type"))}' if b["kind"] == "held" else ""
            pend = " pending" if b["pending"] else ""
            lv = (f'<small class="lv" title="Leave by {e(b["leave"])} for the walk there">'
                  f'leave {e(b["leave"])}</small>' if b.get("leave") else "")
            inner.append(f'<a class="blk b-{e(b["kind"])}{ty}{pend}{hl}{narrow}" '
                         f'href="#s-{b["i"]}" title="{e(b["title"])}" style="top:{b["top"]}%;'
                         f'height:{b["height"]}%;left:{left0 + b["lane"] * width:.2f}%;width:{width:.2f}%">'
                         f'{badge}{mk}<b>{e(b["code"])}</b>{lv}'
                         f'<small class="tm">{e(b["time"].split("–")[0] if short else b["time"])}</small>'
                         f'<span class="nm">{e(b["name"])}</span><span>{e(b["sub"])}</span></a>')
        if w.get("now") and w["now"]["day"] == i:
            inner.append(f'<div class="now" style="top:{w["now"]["top"]}%"><span>now {e(w["now"]["label"])}'
                         '</span></div>')
        cols.append(f'<div class="col"><a class="dh" href="/day?d={i}" title="Open {e(label)} as a list">'
                    f'{e(label)} <span aria-hidden="true">›</span></a>'
                    f'<div class="slots">{"".join(inner)}'
                    f'</div></div>')
    return (f'<div class="weekhead"><h1>{e(w["title"])}</h1><div class="legend">{legend}</div></div>'
            f'<div class="scroll"><div class="week"><div class="axis"><div class="dh"></div>'
            f'<div class="slots">{hours}</div></div>{"".join(cols)}</div></div>'
            f'<p class="fine">Click a session for its details.</p>{_details_html(w)}')


def _details_html(w: dict[str, Any]) -> str:
    """One panel per block, shown by its #s-N link with CSS :target. Works without the script."""
    out = []
    for b in w["blocks"]:
        d = b["detail"]
        rows = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in d["rows"])
        hist = "".join(f"<li>{e(h)}</li>" for h in d["history"]) or "<li>No change seen yet.</li>"
        sits = "".join(f'<li><b>{e(x["code"])}</b> {e(x["when"])}, {e(x["venue"])} · {e(x["band"])}'
                       f'{" · you hold this" if x["held"] else ""}</li>' for x in d["sittings"])
        out.append(f'<section class="detail" id="s-{b["i"]}" aria-label="{e(d["code"])} details">'
                   f'<a class="close" href="#">Close</a><div class="kind">{e(d["code"])}</div>'
                   f'<h3>{e(d["title"])}</h3><p>{e(d["status"])}</p><dl>{rows}</dl>'
                   f'<h4>Seat band changes</h4><ul class="mono list">{hist}</ul>'
                   + (f'<h4>Other sittings of this talk</h4><ul class="list">{sits}</ul>' if sits else "")
                   + "</section>")
    return "".join(out)


def render_approve(snap: dict[str, Any], demo: bool = False, push: bool = False) -> str:
    v = snap.get("view") or {}
    cards, s = v.get("cards", []), v["status"]
    plans = [c["plan_id"] for c in cards]
    out = [_head("re:Seat approve", "approve", demo, plans, snap["now"], s["last_sweep"]),
           _status_line(s), '<main class="page"><h1>Approve</h1><p class="intro">A swap would cancel a seat '
           "you hold, so it waits here for you, one at a time. Bookings within your rules happen on their "
           'own and are listed beside it.</p><div class="two"><div class="col">'
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
    out.append('</div><div class="col"><h2>Booked in the last day</h2>')
    if not v.get("bookings"):
        out.append('<p class="muted">Nothing booked in the last day.</p>')
    for b in v.get("bookings", []):
        out.append(f'<article class="card booked"><div class="kind ok">BOOKED {e(b["at"])}</div>'
                   f'<div><strong>{e(b["code"])}</strong>, {e(b["when"])}, {e(b["venue"])}</div>'
                   f'<div class="muted">Within your rules. Read back ok.</div></article>')
    out.append("</div></div></main></body></html>")
    return _on_top("".join(out), "approve", demo)


def _side_html(label: str, side: dict[str, Any], cls: str) -> str:
    return (f'<div class="side-{cls}"><div class="lbl">{e(label)}</div>'
            f'<div class="mono">{e(side["code"])}</div>'
            f'<div>{e(side["when"])}</div><div class="muted">{e(side["venue"])}</div></div>')


def wait_text(mins: int) -> str:
    """A countdown a person reads at a glance: 35 min, 5 h 20 min, 2 days 5 h, 48 days.
    static/app.js counts down with the same rule."""
    if mins < 60:
        return f"{mins} min"
    if mins < 1440:
        h, m = divmod(mins, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(mins // 60, 24)
    days = f"{d} day" + ("s" if d > 1 else "")
    return f"{days} {h} h" if d < 7 and h else days


def render_today(snap: dict[str, Any], demo: bool = False) -> str:
    v = snap.get("view") or {}
    t, s = v["today"], v["status"]
    out = [_head("re:Seat today", "today", demo, [c["plan_id"] for c in v.get("cards", [])], snap["now"],
                 s["last_sweep"]),
           _status_line(s), f'<main class="page"><h1>Today, {e(t["label"])}</h1><p class="intro">When to '
           "leave for your next session, the rest of your day, and the talks you want but hold no seat for."
           '</p><div class="two"><div class="col">']
    h = t["hero"]
    if h:
        walk = (f"Walk {h['origin']} → {h['venue']}, about {h['walk']} min, {h['allow']} allowed (estimate). "
                if h["walk"] is not None
                else f"Be at {h['venue']} by the doors. ")
        mins = math.ceil(max(0, h["leave_at"] - snap["now"]) / 60)
        label, big = ("LEAVE IN", wait_text(mins)) if mins > 0 else ("LEAVE NOW", "now")
        out.append(f'<section class="hero"><div class="mono" data-leave-label>{label}</div>'
                   f'<div class="big" data-leave="{h["leave_at"]:.0f}">{big}</div>'
                   f'<div>for <strong>{e(h["code"])}</strong> at {e(h["start"])}, {e(h["venue"])}'
                   f'{", " + e(h["room"]) if h["room"] else ""}</div>'
                   f'<div class="small">{e(walk)}Doors close {e(h["doors"])}.</div></section>')
    else:
        out.append('<section class="panel"><p>No walk to start today. Nothing held is still ahead.</p>'
                   "</section>")
    b = v.get("blocks")
    if b:
        parts = ", ".join(f"{n} {w}" for n, w in ((b["create"], "to add"), (b["update"], "to change"),
                                                   (b["delete"], "to remove")) if n)
        out.append(f'<section class="card blocks-card"><div class="kind">LEAVE-NOW BLOCKS</div>'
                   f'<h3>Out of date</h3><p>Your schedule changed since they were written: {e(parts)}.</p>'
                   f'<div class="actions"><button class="primary" data-update="{e(b["token"])}">'
                   'Update</button></div><p class="result" role="status" aria-live="polite"></p></section>')
    out.append('<section class="panel"><h2>Next</h2>')
    out += [f'<div class="row"><div class="mono t">{e(n["time"])}</div><div><strong>{e(n["name"])}</strong>'
            f'<div class="muted">{e(n["sub"])}</div></div></div>' for n in t["next"]] or [
        '<p class="muted">Nothing else today.</p>']
    out.append('</section></div><div class="col"><section class="panel"><h2>Wanted today, not held</h2>')
    for w in t["wanted"]:
        clash = f'<div class="bad">{e(w["clash"])}</div>' if w["clash"] else ""
        out.append(f'<div class="want"><div class="split"><strong>{e(w["code"])} · {e(w["time"])}</strong>'
                   f'<span class="mono band band-{e(w["band"])}">{e(w["band"])}</span></div>'
                   f'<div class="muted">{e(w["text"])}</div>{clash}</div>')
    if not t["wanted"]:
        out.append('<p class="muted">Nothing wanted today that is not held.</p>')
    out.append(f'</section><section class="panel foot"><i class="dot"></i><span class="mono">{e(t["cover"])}'
               f'</span></section></div></div></main></body></html>')
    return _on_top("".join(out), "today", demo)
