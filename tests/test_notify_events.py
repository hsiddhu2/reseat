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


# ---- which pushes reach the phone: quiet hours and categories


from datetime import datetime  # noqa: E402

import httpx  # noqa: E402

from reseat.campus import VEGAS  # noqa: E402
from reseat.watcher import WatchEvent  # noqa: E402


def vegas_ts(hm, date="2026-12-01"):
    return datetime.fromisoformat(f"{date}T{hm}").replace(tzinfo=VEGAS).timestamp()


def quiet_pusher(now, **kw):
    seen = []

    def handler(req):
        seen.append((req.headers["title"], req.content.decode()))
        return httpx.Response(200)
    t = {"now": now}
    p = push.Pusher("reseat-demo-7f3k9q2m", transport=httpx.MockTransport(handler),
                    clock=lambda: t["now"], **kw)
    return p, seen, t


def booked(code="ARC301-R"):
    return WatchEvent("booked", 0, {"code": code, "title": "T"})


def test_notify_settings_parse_and_reject_nonsense():
    r = R.parse('notify:\n  quiet: "22:00-07:00"\n  mute: [on_site, health]\n')
    assert r.notify.quiet == "22:00-07:00" and r.notify.mute == ["on_site", "health"]
    for bad in ('notify:\n  quiet: "25:00-07:00"\n', 'notify:\n  quiet: "07:00-07:00"\n',
                'notify:\n  mute: [everything]\n', 'notify:\n  quiet: "10pm-7am"\n'):
        with pytest.raises(R.RulesError):
            R.parse(bad)
    assert R.parse("").notify.quiet is None


def test_quiet_hours_wrap_midnight_in_las_vegas_time():
    assert push.in_quiet("22:00-07:00", vegas_ts("23:30")) and push.in_quiet("22:00-07:00", vegas_ts("06:59"))
    assert not push.in_quiet("22:00-07:00", vegas_ts("07:00")) and not push.in_quiet(None, vegas_ts("03:00"))
    assert push.in_quiet("12:00-13:00", vegas_ts("12:30"))
    assert not push.in_quiet("12:00-13:00", vegas_ts("13:00"))


def test_pushes_wait_out_quiet_hours_and_arrive_as_one_summary():
    p, seen, t = quiet_pusher(vegas_ts("23:00"), quiet="22:00-07:00")
    p(booked("ARC301-R"))
    p(booked("SVS401"))
    p(WatchEvent("signin", 0, {"state": "needed"}))                 # urgent: comes through at once
    p.flush()
    assert [s[0] for s in seen] == ["Sign in needed"]
    t["now"] = vegas_ts("07:01", "2026-12-02")
    p(WatchEvent("sweep", 0, {}))                                   # the next minute's sweep
    p.flush()
    title, body = seen[-1]
    assert title == "While it was quiet: 2 updates"
    assert body == "Seat booked: ARC301-R T; Seat booked: SVS401 T."


def test_an_off_category_is_dropped_but_a_failed_swap_still_comes_through():
    p, seen, _ = quiet_pusher(vegas_ts("12:00"), mute=["bookings", "swaps"])
    p(booked())
    p(WatchEvent("swap", 0, {"state": "verified"}))
    p(WatchEvent("swap", 0, {"state": "failed"}))
    p(WatchEvent("moved", 0, {"code": "X", "before": {"room": "a"}, "after": {"room": "b"}}))
    p.flush()
    assert [s[0] for s in seen] == ["Swap failed", "X moved"]


def test_every_push_kind_has_a_category():
    kinds = set(push.CATEGORY)
    for kind in ("proposed", "expiring", "swap", "booked", "not_booked", "unconfirmed", "other_sitting",
                 "catalog", "moved", "removed", "tight", "blocks", "blocks_done", "briefing", "leave_soon",
                 "leave", "wanted_soon", "offline", "back", "signin", "writes"):
        assert kind in kinds
    assert set(push.CATEGORY.values()) == set(R.CATEGORIES)


# ---- QA: quiet hours and categories, edge cases


def header_pusher(now, **kw):
    seen = []

    def handler(req):
        seen.append({"title": req.headers["title"], "body": req.content.decode(),
                     "actions": req.headers.get("actions")})
        return httpx.Response(200)
    t = {"now": now}
    p = push.Pusher("reseat-demo-7f3k9q2m", transport=httpx.MockTransport(handler),
                    clock=lambda: t["now"], **kw)
    return p, seen, t


def moved(code):
    return WatchEvent("moved", 0, {"code": code, "before": {"room": "a"}, "after": {"room": "b"}})


def test_a_digest_of_more_than_eight_lists_eight_and_counts_the_rest():
    p, seen, t = header_pusher(vegas_ts("23:00"), quiet="22:00-07:00")
    for i in range(10):
        p(moved(f"ARC3{i:02d}"))
    p(moved("ARC300"))                                              # a repeat title is listed once
    p.flush()
    assert seen == []
    t["now"] = vegas_ts("07:00", "2026-12-02")
    p(WatchEvent("sweep", 0, {}))
    p.flush()
    assert len(seen) == 1 and seen[0]["title"] == "While it was quiet: 11 updates"
    body = seen[0]["body"]
    assert body.startswith("ARC300 moved: a to b; ARC301 moved: a to b") and body.endswith("; and 2 more.")
    assert "ARC308" not in body and seen[0]["actions"] is None


def test_a_quiet_window_that_does_not_wrap_holds_inside_and_sends_outside():
    assert push.in_quiet("01:00-05:00", vegas_ts("01:00"))
    assert not push.in_quiet("01:00-05:00", vegas_ts("00:59"))
    assert not push.in_quiet("01:00-05:00", vegas_ts("23:00"))
    p, seen, t = header_pusher(vegas_ts("12:30"), quiet="12:00-13:00")
    p(booked())
    p.flush()
    assert seen == []
    t["now"] = vegas_ts("13:00")
    p(booked("SVS401"))
    p.flush()
    assert [s["title"] for s in seen] == ["While it was quiet: 1 update", "Seat booked"]


def test_a_muted_category_during_quiet_hours_is_dropped_not_summarised():
    p, seen, t = header_pusher(vegas_ts("23:00"), quiet="22:00-07:00", mute=["on_site", "schedule"])
    p(moved("ARC301"))                                              # muted: never sent, never held
    p(booked())                                                      # not muted: held for the digest
    p(WatchEvent("swap", 0, {"state": "failed"}))                   # urgent: through quiet and mute
    p.flush()
    assert [s["title"] for s in seen] == ["Swap failed"]
    t["now"] = vegas_ts("08:00", "2026-12-02")
    p(WatchEvent("sweep", 0, {}))
    p.flush()
    assert seen[-1]["title"] == "While it was quiet: 1 update" and "moved" not in seen[-1]["body"]


def test_a_swap_in_an_unknown_state_is_urgent_but_never_crashes_or_sends_noise():
    for state in ("unknown", None, "checked"):
        assert push.urgent(WatchEvent("swap", 0, {"state": state}))
    assert not push.urgent(WatchEvent("swap", 0, {"state": "verified"}))
    p, seen, _ = header_pusher(vegas_ts("23:00"), quiet="22:00-07:00", mute=["swaps"])
    p(WatchEvent("swap", 0, {"state": "unknown"}))
    p(WatchEvent("swap", 0, {"state": "rolled_back"}))              # muted and not urgent
    p.flush()
    assert seen == [] and p._held == []


def test_a_proposal_with_buttons_held_by_quiet_hours_reaches_the_digest_without_a_plan_id():
    p, seen, t = header_pusher(vegas_ts("23:00"), quiet="22:00-07:00", approve=True)
    plan = "P" * 20
    p(WatchEvent("proposed", 0, {"plan_id": plan, "wanted_id": "w", "held_id": "h"}))
    p.flush()
    assert seen == []
    t["now"] = vegas_ts("07:30", "2026-12-02")
    p(WatchEvent("sweep", 0, {}))
    p.flush()
    assert seen[0]["title"] == "While it was quiet: 1 update" and "Swap proposed" in seen[0]["body"]
    assert seen[0]["actions"] is None and plan not in seen[0]["body"]


def test_a_pusher_without_notify_settings_sends_everything_at_once():
    p, seen, _ = header_pusher(vegas_ts("03:00"), approve=True)
    plan = "Q" * 20
    p(booked())
    p(moved("ARC301"))
    p(WatchEvent("swap", 0, {"state": "verified"}))
    p(WatchEvent("proposed", 0, {"plan_id": plan, "wanted_id": "w", "held_id": "h"}))
    p.flush()
    assert [s["title"] for s in seen] == ["Seat booked", "ARC301 moved", "Swap done", "Swap proposed"]
    assert plan in seen[-1]["actions"] and p._held == []
