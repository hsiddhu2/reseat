"""Personal time is wall-clock time in the event's timezone (America/Los_Angeles), not UTC.
A trailing Z or an offset is refused, seconds are 00, and a block cannot cross midnight."""

from datetime import datetime

import pytest

from reseat import guard as G
from reseat import pages
from reseat import rules as R
from reseat.campus import VEGAS
from reseat.models import PersonalTime, PersonalTimeInput, Session
from reseat.reminders import leave_time

EV = "reinvent2026"
RULES = "home_venue: The Venetian\nbuffer_minutes: 30\ntargets:\n- code: SEC201\n"


def mk(sid, abbr, date, time_, venue):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": venue, "room": "Level 1 | Room 1", "isReservable": True, "seatAvailability": "available",
        "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


CATALOG = [
    mk("G1", "SEC201", "2026-12-01", "10:00", "MGM Grand"),
    mk("G2", "ARC301", "2026-12-01", "11:15", "Venetian"),
    mk("G3", "DOP302", "2026-12-02", "14:00", "Caesars Forum"),
]


def blocks_by_code(fake):
    return {p["title"].removeprefix("Leave for "): p for p in fake.schedule.personal_time.values()}


def _held(fake, store, catalog):
    for s in catalog:
        fake.add_session(s)
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)
    fake.schedule.reserved.update(s.session_id for s in catalog)


def _rules():
    return R.parse(RULES + "arrive_confidence: 0.5\n")


def test_sync_writes_las_vegas_wall_clock_and_reads_it_back(fake, client, store):
    _held(fake, store, CATALOG)
    res = G.sync(client, store, EV, _rules())
    assert len(res.done) == 3 and not res.problems
    got = blocks_by_code(fake)["SEC201"]
    # SEC201 10:00 PST. 10:00 - 11 - 50 = 08:59 Las Vegas, sent as is, with no Z and no offset.
    assert (got["startDateTime"], got["endDateTime"]) == ("2026-12-01T08:59:00", "2026-12-01T09:04:00")
    assert fake.calls[-1][0] == "GetSchedule"                  # the read-back follows the writes
    assert store.journal_entries(EV)[0]["outcome"] == "readback-ok"


def test_an_old_utc_block_is_moved_to_local_time_with_every_field(fake, client, store):
    _held(fake, store, CATALOG)
    want = {b.code: b for b in G.leave_blocks(G.held_sessions(store, EV, ["G1", "G2", "G3"]), _rules())[0]}
    old = dict(want["SEC201"].body().payload(), personalTimeId="pt-old",
               startDateTime="2026-12-01T16:59:00", endDateTime="2026-12-01T17:04:00")   # the old UTC form
    fake.schedule.personal_time["pt-old"] = old
    res = G.sync(client, store, EV, _rules())
    assert [(pid, b.code) for pid, b in res.plan.update] == [("pt-old", "SEC201")] and not res.problems
    assert fake.counts["UpdatePersonalTime"] == 1
    # The fake stores the PUT body whole, so every field must have been sent, location included.
    assert fake.schedule.personal_time["pt-old"] == {"personalTimeId": "pt-old",
                                                     **want["SEC201"].body().payload()}
    assert fake.schedule.personal_time["pt-old"]["startDateTime"] == "2026-12-01T08:59:00"
    assert fake.calls[-1][0] == "GetSchedule"
    assert store.journal_entries(EV)[0]["outcome"] == "readback-ok"


def test_an_evening_session_gets_a_block_on_its_own_date(fake, client, store):
    # 17:30 at MGM Grand from the Venetian: leave 16:29 PST, which is 00:29Z the next day.
    _held(fake, store, [mk("E1", "SEC201", "2026-12-01", "17:30", "MGM Grand")])
    res = G.sync(client, store, EV, _rules())
    assert not res.problems
    got = blocks_by_code(fake)["SEC201"]
    assert (got["startDateTime"], got["endDateTime"]) == ("2026-12-01T16:29:00", "2026-12-01T16:34:00")


@pytest.mark.parametrize("start, end", [
    ("2026-12-01T09:00:00-08:00", "2026-12-01T09:05:00-08:00"),   # an offset is refused, not converted
    ("2026-12-01T09:00:30", "2026-12-01T09:05:30"),               # seconds must be 00
])
def test_the_fake_refuses_an_offset_and_seconds(fake, client, start, end):
    from reseat.client import ApiError
    body = PersonalTimeInput(startDateTime=start, endDateTime=end, title="x", description="y")
    with pytest.raises(ApiError) as e:
        client.create_personal_time(EV, body)
    assert e.value.status == 400 and fake.schedule.personal_time == {}


def test_a_stored_local_block_shows_its_own_clock_time():
    p = PersonalTime.model_validate({"personalTimeId": "pt-1", "startDateTime": "2026-12-01T09:49:00",
                                     "endDateTime": "2026-12-01T09:54:00", "title": "Lunch",
                                     "description": "with team"})
    a, b = pages._pt_window(p)
    assert (f"{a:%H:%M}", f"{b:%H:%M}", a.date().isoformat()) == ("09:49", "09:54", "2026-12-01")
    blk = G.Block(code="ARC301", session_id="G2", start="2026-12-01T09:49:00", end="2026-12-01T09:54:00",
                  title="Leave for ARC301", description="d", location=None)
    assert leave_time(blk) == datetime(2026, 12, 1, 9, 49, tzinfo=VEGAS).timestamp()
