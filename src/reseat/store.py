"""SQLite catalog store.

Holds the latest copy of every session, a history of seat-band changes, and a
log of sessions that appeared or disappeared between sweeps. That history is
what the watcher, the swap engine and the queue-or-go advice read.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    event_id     TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    abbreviation TEXT,
    base_code    TEXT,
    title        TEXT NOT NULL,
    type         TEXT,
    level        TEXT,
    venue        TEXT,
    room         TEXT,
    date         TEXT,
    time         TEXT,
    minutes      INTEGER,
    reservable   INTEGER,
    band         TEXT,
    abstract     TEXT,
    raw          TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    PRIMARY KEY (event_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_base ON sessions(event_id, base_code);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(event_id, date, time);

CREATE TABLE IF NOT EXISTS band_history (
    event_id   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    old_band   TEXT,
    new_band   TEXT
);
CREATE INDEX IF NOT EXISTS idx_band_session ON band_history(event_id, session_id, ts);

CREATE TABLE IF NOT EXISTS catalog_changes (
    event_id   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,   -- added | removed | moved
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS sweeps (
    event_id  TEXT NOT NULL,
    ts        REAL NOT NULL,
    count     INTEGER NOT NULL,
    with_abstracts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    name  TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    ts        REAL NOT NULL,
    event_id  TEXT NOT NULL,
    op        TEXT NOT NULL,
    request   TEXT,
    response  TEXT,
    outcome   TEXT
);
"""


SHRINK_LIMIT = 0.5    # a sweep may not drop more than this share of the catalog at once


class SweepRefused(Exception):
    """A sweep that would wipe or gut the local catalog. Nothing was changed."""


@dataclass
class SweepResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    band_changes: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    count: int = 0
    baseline: bool = False   # a fresh or re-keyed catalog: recorded, but nothing new in it is news
    reason: str | None = None  # fresh | rekeyed | forced, when baseline

    @property
    def opened(self) -> list[str]:
        """Sessions whose band went from closed (or absent) to an open band."""
        out = []
        for sid, old, new in self.band_changes:
            was_open = old in ("available", "limited", "veryLimited")
            now_open = new in ("available", "limited", "veryLimited")
            if now_open and not was_open:
                out.append(sid)
        return out


def _owner_only_file(path: Path) -> None:
    """The database holds the catalog, the attendee's schedule and the write journal. Create it
    owner-only (0600), and tighten one re:Seat made before this rule. SQLite gives its journal
    and WAL files the database file's mode."""
    if os.name != "posix":
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for p, create in ((path, True), *((Path(f"{path}{s}"), False) for s in ("-journal", "-wal", "-shm"))):
        if p.is_symlink() or (not create and not p.exists()):
            continue
        try:
            fd = os.open(p, os.O_RDWR | nofollow | (os.O_CREAT if create else 0), 0o600)
            try:
                os.fchmod(fd, 0o600)             # the file just opened, never a swapped-in link
            finally:
                os.close(fd)
        except OSError:
            pass                 # SQLite reports any real problem opening it


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            _owner_only_file(Path(self.path))
        # One thread owns a Store at a time. `reseat serve` opens it on the main thread
        # and hands it to the watcher thread, which then does all store work. HTTP
        # threads never touch it. So the connection may move between threads.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ sync

    def apply_sweep(self, event_id: str, sessions: list[Session],
                    with_abstracts: bool, now: float | None = None, force: bool = False) -> SweepResult:
        """Merge one full walk of the catalog and report what changed.

        Guards, seen live on 8 October 2026 when ListSessions answered 200 with no items:
        - An empty sweep is refused (SweepRefused), never read as "everything removed".
        - A sweep that would drop more than half the catalog is refused, unless it is about
          as big as the stored catalog and most of its ids are new. Then the catalog was
          re-keyed: the sweep is applied as a baseline. New ids are not news. Sessions that
          kept their id still report openings.
        - A sweep into an empty store is a baseline.
        `force` applies any sweep as a baseline.
        """
        now = now or time.time()
        res = SweepResult(count=len(sessions))
        cur = self.db.cursor()
        existing = {
            r["session_id"]: r
            for r in cur.execute(
                "SELECT session_id, band, date, time, room, venue, abstract FROM sessions "
                "WHERE event_id=?", (event_id,))
        }
        incoming = {s.session_id for s in sessions}
        lost = len(set(existing) - incoming)
        hint = " Run reseat sync --force if the catalog really changed that much."
        if not force:
            if not sessions:
                raise SweepRefused(f"The API returned an empty catalog. The {len(existing)} sessions "
                                   "stored locally were kept." + hint)
            if existing and lost > len(existing) * SHRINK_LIMIT:
                new_share = len(incoming - set(existing)) / max(1, len(incoming))
                if len(incoming) < len(existing) * SHRINK_LIMIT or new_share <= SHRINK_LIMIT:
                    raise SweepRefused(f"The sweep would drop {lost} of {len(existing)} sessions and bring "
                                       f"only {len(incoming)}. Treated as a partial answer. Nothing was "
                                       "changed." + hint)
        reason = ("forced" if force else "fresh" if not existing
                  else "rekeyed" if lost > len(existing) * SHRINK_LIMIT else None)
        seen: set[str] = set()
        for s in sessions:
            seen.add(s.session_id)
            st = s.session_time
            band = s.seat_availability
            old = existing.get(s.session_id)
            # Keep an abstract we already have when this sweep omitted them.
            abstract = s.abstract if with_abstracts else (old["abstract"] if old else None)
            if old is None:
                res.added.append(s.session_id)
                cur.execute("INSERT INTO catalog_changes VALUES (?,?,?,?,?)",
                            (event_id, s.session_id, now, "added", s.abbreviation))
                if band:
                    res.band_changes.append((s.session_id, None, band))
                    cur.execute("INSERT INTO band_history VALUES (?,?,?,?,?)",
                                (event_id, s.session_id, now, None, band))
            else:
                if old["band"] != band:
                    res.band_changes.append((s.session_id, old["band"], band))
                    cur.execute("INSERT INTO band_history VALUES (?,?,?,?,?)",
                                (event_id, s.session_id, now, old["band"], band))
                moved = (old["date"], old["time"], old["room"], old["venue"]) != (
                    st.date if st else None, st.time if st else None, s.room, s.venue)
                if moved:
                    res.moved.append(s.session_id)
                    cur.execute("INSERT INTO catalog_changes VALUES (?,?,?,?,?)",
                                (event_id, s.session_id, now, "moved",
                                 json.dumps({"from": dict(old), "to": {
                                     "date": st.date if st else None,
                                     "time": st.time if st else None,
                                     "room": s.room, "venue": s.venue}}, default=str)))
            cur.execute(
                """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id, session_id) DO UPDATE SET
                     abbreviation=excluded.abbreviation, base_code=excluded.base_code,
                     title=excluded.title, type=excluded.type, level=excluded.level,
                     venue=excluded.venue, room=excluded.room, date=excluded.date,
                     time=excluded.time, minutes=excluded.minutes,
                     reservable=excluded.reservable, band=excluded.band,
                     abstract=excluded.abstract, raw=excluded.raw,
                     last_seen=excluded.last_seen""",
                (event_id, s.session_id, s.abbreviation, s.base_code, s.title, s.type,
                 s.level, s.venue, s.room, st.date if st else None, st.time if st else None,
                 st.minutes if st else None, int(bool(s.is_reservable)), band, abstract,
                 s.model_dump_json(by_alias=True), now, now),
            )
        for sid in existing:
            if sid not in seen:
                res.removed.append(sid)
                cur.execute("INSERT INTO catalog_changes VALUES (?,?,?,?,?)",
                            (event_id, sid, now, "removed", None))
                cur.execute("DELETE FROM sessions WHERE event_id=? AND session_id=?",
                            (event_id, sid))
        cur.execute("INSERT INTO sweeps VALUES (?,?,?,?)",
                    (event_id, now, len(sessions), int(with_abstracts)))
        self.db.commit()
        if reason:
            res.baseline, res.reason = True, reason
            # A new id's band is history, not an opening. A session that kept its id can still open.
            res.band_changes = [c for c in res.band_changes if c[0] in existing]
        return res

    def update_session(self, event_id: str, s: Session, now: float | None = None) -> SweepResult:
        """Merge one fresh GetSession result. Same bookkeeping as a sweep, for one session.

        Used on site, where GetSession every 20 seconds replaces full sweeps. Never
        marks anything removed, because it only sees one session.
        """
        now = now or time.time()
        cur = self.db.cursor()
        old = cur.execute("SELECT band, date, time, room, venue, abstract FROM sessions "
                          "WHERE event_id=? AND session_id=?", (event_id, s.session_id)).fetchone()
        res = SweepResult(count=1)
        st = s.session_time
        band = s.seat_availability
        if old is None:
            res.added.append(s.session_id)
        else:
            if old["band"] != band:
                res.band_changes.append((s.session_id, old["band"], band))
                cur.execute("INSERT INTO band_history VALUES (?,?,?,?,?)",
                            (event_id, s.session_id, now, old["band"], band))
            if (old["date"], old["time"], old["room"], old["venue"]) != (
                    st.date if st else None, st.time if st else None, s.room, s.venue):
                res.moved.append(s.session_id)
                cur.execute("INSERT INTO catalog_changes VALUES (?,?,?,?,?)",
                            (event_id, s.session_id, now, "moved", json.dumps({
                                "from": {k: old[k] for k in ("date", "time", "room", "venue")},
                                "to": {"date": st.date if st else None, "time": st.time if st else None,
                                       "room": s.room, "venue": s.venue}})))
        abstract = s.abstract if s.abstract is not None else (old["abstract"] if old else None)
        cur.execute(
            """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id, session_id) DO UPDATE SET
                 abbreviation=excluded.abbreviation, base_code=excluded.base_code,
                 title=excluded.title, type=excluded.type, level=excluded.level,
                 venue=excluded.venue, room=excluded.room, date=excluded.date,
                 time=excluded.time, minutes=excluded.minutes,
                 reservable=excluded.reservable, band=excluded.band,
                 abstract=excluded.abstract, raw=excluded.raw,
                 last_seen=excluded.last_seen""",
            (event_id, s.session_id, s.abbreviation, s.base_code, s.title, s.type,
             s.level, s.venue, s.room, st.date if st else None, st.time if st else None,
             st.minutes if st else None, int(bool(s.is_reservable)), band, abstract,
             s.model_dump_json(by_alias=True), now, now),
        )
        self.db.commit()
        return res

    # ------------------------------------------------------------------ reads

    def get(self, event_id: str, session_id: str) -> Session | None:
        r = self.db.execute("SELECT raw, abstract FROM sessions WHERE event_id=? AND session_id=?",
                            (event_id, session_id)).fetchone()
        if not r:
            return None
        s = Session.model_validate_json(r["raw"])
        if s.abstract is None and r["abstract"]:
            s.abstract = r["abstract"]
        return s

    def all(self, event_id: str) -> list[Session]:
        rows = self.db.execute("SELECT raw FROM sessions WHERE event_id=? ORDER BY date, time",
                               (event_id,)).fetchall()
        return [Session.model_validate_json(r["raw"]) for r in rows]

    def by_base_code(self, event_id: str, base_code: str) -> list[Session]:
        rows = self.db.execute(
            "SELECT raw FROM sessions WHERE event_id=? AND base_code=? ORDER BY date, time",
            (event_id, base_code)).fetchall()
        return [Session.model_validate_json(r["raw"]) for r in rows]

    def search(self, event_id: str, text: str, limit: int = 50) -> list[Session]:
        like = f"%{text}%"
        rows = self.db.execute(
            "SELECT raw FROM sessions WHERE event_id=? AND (title LIKE ? OR abbreviation LIKE ? "
            "OR abstract LIKE ?) ORDER BY date, time LIMIT ?",
            (event_id, like, like, like, limit)).fetchall()
        return [Session.model_validate_json(r["raw"]) for r in rows]

    def band_history(self, event_id: str, session_id: str) -> list[tuple[float, str | None, str | None]]:
        rows = self.db.execute(
            "SELECT ts, old_band, new_band FROM band_history WHERE event_id=? AND session_id=? "
            "ORDER BY ts", (event_id, session_id)).fetchall()
        return [(r["ts"], r["old_band"], r["new_band"]) for r in rows]

    def recent_changes(self, event_id: str, since: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM catalog_changes WHERE event_id=? AND ts>=? ORDER BY ts",
            (event_id, since)).fetchall()

    def last_sweep(self, event_id: str) -> float | None:
        r = self.db.execute("SELECT MAX(ts) AS ts FROM sweeps WHERE event_id=?",
                            (event_id,)).fetchone()
        return r["ts"] if r and r["ts"] else None

    # ------------------------------------------------------------------ locks

    def acquire_lock(self, name: str, owner: str, ttl: float = 300, now: float | None = None) -> bool:
        """Advisory lock shared by every process using this database. False if someone holds it.

        A lock older than `ttl` seconds is treated as left behind by a crashed process.
        """
        now = now or time.time()
        try:
            with self.db:
                self.db.execute("DELETE FROM locks WHERE name=? AND ts<?", (name, now - ttl))
                self.db.execute("INSERT INTO locks VALUES (?,?,?)", (name, owner, now))
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lock(self, name: str, owner: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    # ------------------------------------------------------------------ journal

    def journal(self, event_id: str, op: str, request: object, response: object,
                outcome: str) -> None:
        self.db.execute("INSERT INTO journal VALUES (?,?,?,?,?,?)",
                        (time.time(), event_id, op, json.dumps(request, default=str),
                         json.dumps(response, default=str), outcome))
        self.db.commit()

    def journal_entries(self, event_id: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM journal WHERE event_id=? ORDER BY rowid DESC LIMIT ?",
            (event_id, limit)).fetchall()
