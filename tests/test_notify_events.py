"""Notification group 2: the catalog is back, a held session moved or left the catalog, a booking the
read-back could not confirm, and a seat that opened but was not booked, with the reason. Each is a
watcher event against FakeEventsApi, and each turns into one push with codes and titles only."""

import pytest

from reseat import push
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.watcher import Watcher

EV = "reinvent2026"


def mk(sid, abbr, date="2026-12-01", time_="10:00", band="available", typ="Breakout session"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": typ,
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


def catalog():
    return [mk("A1", "ARC301-R", "2026-12-01", "10:00", band="unavailable"),
            mk("A2", "ARC301-R1", "2026-12-02", "10:00", band="unavailable"),
            mk("C1", "SVS401", "2026-12-01", "10:30", typ="Chalk talk"),
            mk("X1", "SEC201", "2026-12-03", "14:00")]


@pytest.fixture
def env(clock):
    fake = FakeEventsApi(sessions=catalog())
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    return fake, client, Store(":memory:"), clock


def start(env, body, baseline=True):
    fake, client, store, clock = env
    w = Watcher(client, store, R.parse(body), EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    if baseline:
        w.tick()
        clock.sleep(60)
    return w, events


def of(events, kind):
    return [e for e in events if e.kind == kind]


def pushed(ev):
    return push.message(ev, str)


def test_the_catalog_coming_back_after_an_empty_one_is_announced_once(env):
    fake, *_ = env
    saved = dict(fake.sessions)
    fake.sessions.clear()                                  # the API serves nothing, as on 8 October
    w, events = start(env, "targets:\n- code: ARC301\n", baseline=False)
    assert w.tick().failure == "catalog"
    fake.sessions.update(saved)
    w.clock.sleep(60)
    w.tick()
    [ev] = of(events, "catalog")
    assert ev.data["count"] == 4 and pushed(ev)[0] == "The re:Invent catalog is back"
    assert pushed(of(events, "back")[0]) is None           # one push, not "re:Seat back" as well
    w.clock.sleep(60)
    w.tick()
    assert len(of(events, "catalog")) == 1


def test_a_held_session_that_moves_is_pushed_with_the_change(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    w, events = start(env, "targets:\n- code: SEC201\n")
    fake.sessions["X1"] = fake.sessions["X1"].model_copy(update={"room": "Level 2 | Grand 214"})
    w.tick()
    title, body = pushed(of(events, "moved")[0])
    assert title == "SEC201 moved" and "Level 1 | Grand 117 to Level 2 | Grand 214" in body


def test_a_held_session_that_leaves_the_catalog_is_pushed(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    w, events = start(env, "targets:\n- code: SEC201\n")
    del fake.sessions["X1"]
    w.tick()
    title, body = pushed(of(events, "removed")[0])
    assert title == "SEC201 left the catalog" and "You held it" in body and "X1" not in body


def test_a_reserve_the_read_back_does_not_confirm_is_pushed(env):
    fake, *_ = env
    w, events = start(env, "targets:\n- code: ARC301\n")
    fake.ghost.add("A2")                                    # the API says yes, the schedule never shows it
    fake.set_band("A2", "limited")
    w.tick()
    title, body = pushed(of(events, "unconfirmed")[0])
    assert title == "Check your schedule" and "ARC301-R1" in body and "could not confirm" in body
    assert not of(events, "booked")


def test_a_seat_that_opens_but_the_rules_do_not_book_says_why(env):
    fake, *_ = env
    fake.add_session(mk("D1", "DOP302", "2026-12-02", "15:00", band="unavailable"))
    fake.schedule.reserved.add("A2")                        # Wednesday already has a held session
    w, events = start(env, "max_per_day: 1\ntargets:\n- code: ARC301\n- code: DOP302\n")
    fake.set_band("D1", "available")
    w.tick()
    [ev] = of(events, "not_booked")
    title, body = pushed(ev)
    assert title == "Seat opened: DOP302" and body.startswith("Not booked: ") and ev.data["reason"]
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_no_not_booked_push_for_a_talk_already_held_or_when_writes_are_closed(env):
    fake, *_ = env
    fake.schedule.reserved.add("A1")
    w, events = start(env, "targets:\n- code: ARC301\n")
    fake.set_band("A2", "available")                        # another sitting of a talk already held
    w.tick()
    assert not of(events, "not_booked")
    fake.add_session(mk("D1", "DOP302", "2026-12-02", "15:00", band="unavailable"))
    w.tick()
    assert not of(events, "not_booked")                     # a new sitting that is already full did not open


def test_no_not_booked_push_when_the_reserve_meets_a_409(env):
    fake, *_ = env
    w, events = start(env, "targets:\n- code: ARC301\n")
    fake.closed = True
    fake.set_band("A2", "available")
    w.tick()
    assert fake.counts.get("ReserveSessions", 0) == 1 and not of(events, "booked")
    assert not of(events, "not_booked")                     # a 409 is not an answer about the seat


def test_a_seat_that_fills_at_the_moment_of_booking_says_so(env):
    fake, *_ = env
    w, events = start(env, "targets:\n- code: ARC301\n")
    fake.full.add("A2")                                     # the band reads open, the reserve says full
    fake.set_band("A2", "limited")
    w.tick()
    [ev] = of(events, "not_booked")
    assert pushed(ev) == ("Seat opened: ARC301-R1", "Not booked: it filled before the reserve.")


def test_a_swap_proposal_is_not_also_a_not_booked_push(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")                        # SVS401 holds Tue 10:30, lower priority
    w, events = start(env, "targets:\n- code: ARC301\n- code: SVS401\n")
    fake.set_band("A1", "available")                        # ARC301-R Tue 10:00 overlaps it
    w.tick()
    assert of(events, "proposed") and not of(events, "not_booked")


def test_names_never_fall_back_to_a_session_id(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    fake.sessions["X1"] = fake.sessions["X1"].model_copy(update={"abbreviation": None})
    w, events = start(env, "targets:\n- session_id: X1\n")
    del fake.sessions["X1"]
    w.tick()
    title, body = pushed(of(events, "removed")[0])
    assert title == "A session you hold left the catalog" and "X1" not in title + body


def test_a_held_session_removed_along_with_its_reservation_is_still_pushed(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    w, events = start(env, "targets:\n- code: SEC201\n")
    del fake.sessions["X1"]
    fake.schedule.reserved.discard("X1")                    # the schedule drops it in the same moment
    w.tick()
    assert pushed(of(events, "removed")[0])[0] == "SEC201 left the catalog"


def test_a_session_the_attendee_cancelled_earlier_is_not_pushed_as_held_when_removed(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    w, events = start(env, "targets:\n- code: SEC201\n")
    fake.schedule.reserved.discard("X1")                    # cancelled in the official app
    w.tick()                                                # the read-back sees it go
    del fake.sessions["X1"]
    w.tick()
    w.tick()
    assert of(events, "removed") == []
