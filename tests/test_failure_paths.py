"""Failure paths in favorites, guard, swap and watcher: 401, 403, 404, 429, 5xx, unknown codes,
read-back disagreement and read-back failure.

Every test drives FakeEventsApi as an httpx transport. No network.
"""

import pytest

from reseat import favorites as F
from reseat import guard as G
from reseat import rules as R
from reseat.client import ApiError, EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.swap import Swap, SwapBusy
from reseat.watcher import Watcher

EV = "reinvent2026"


def mk(sid, abbr, date, time_, band="available", venue="MGM Grand", reservable=True):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": venue, "room": "Level 1 | Room 1", "isReservable": reservable,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


def make_env(clock, catalog):
    fake = FakeEventsApi(sessions=catalog)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, catalog, with_abstracts=False)
    return fake, client, store


def fail_schedule_on_call(client, fake, nth, status=503):
    """Make the nth GetSchedule call (1-based) fail once. Earlier and later calls work."""
    real = client.get_schedule
    calls = {"n": 0}

    def flaky(event_id):
        calls["n"] += 1
        if calls["n"] == nth:
            fake.fail_next("GetSchedule", status)
        return real(event_id)

    client.get_schedule = flaky
    return calls


# ---------------------------------------------------------------------- favorites

FAV_RULES = "targets:\n- code: ARC301\n- code: SVS401\n"


@pytest.fixture
def fav(fake, client, store):
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)
    return fake, client, store


def fav_status(res):
    return {o.session_id: o.status for o in res.outcomes}


def test_fav_429_waits_once_then_sends(fav, clock):
    fake, client, store = fav
    fake.throttle_next(F.OP, retry_after=4)
    t0 = clock.t
    res = F.sync(client, store, EV, R.parse(FAV_RULES))
    assert clock.t - t0 >= 4
    assert fake.counts[F.OP] == 2                       # the refused one and the retry
    assert res.count("added") == 3 and not res.disagreements


@pytest.mark.parametrize("status", [401, 403, 500])
def test_fav_write_error_is_read_back_and_not_resent(fav, status):
    fake, client, store = fav
    fake.fail_next(F.OP, status)
    res = F.sync(client, store, EV, R.parse(FAV_RULES))
    assert fake.counts[F.OP] == 1
    assert str(status) in res.error
    assert set(fav_status(res).values()) == {"failed"}   # read back: none landed
    assert fake.counts["GetSchedule"] == 2


def test_fav_read_back_failure_is_reported(fav):
    fake, client, store = fav
    fail_schedule_on_call(client, fake, 2)
    res = F.sync(client, store, EV, R.parse(FAV_RULES))
    assert "read-back failed" in res.error
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2", "S-SVS1"}


def test_fav_error_on_second_batch_stops_and_keeps_first(clock):
    sessions = [mk(f"F{i:02d}", f"T{i:02d}1", "2026-12-01", "10:00") for i in range(12)]
    fake, client, store = make_env(clock, sessions)
    body = "targets:\n" + "".join(f"- code: T{i:02d}1\n" for i in range(12))
    real = client.favorite
    calls = {"n": 0}

    def second_fails(event_id, ids):
        calls["n"] += 1
        if calls["n"] == 2:
            fake.fail_next(F.OP, 503)
        return real(event_id, ids)

    client.favorite = second_fails
    res = F.sync(client, store, EV, R.parse(body))
    assert fake.counts[F.OP] == 2
    assert res.count("added") == 10 and res.count("failed") == 2
    assert len(fake.schedule.favorites) == 10


# ---------------------------------------------------------------------- guard

G_CATALOG = [
    mk("G1", "SEC201", "2026-12-01", "10:00", venue="MGM Grand"),
    mk("G2", "ARC301", "2026-12-01", "11:15", venue="Venetian"),
]
G_RULES = "home_venue: The Venetian\ntargets:\n- code: SEC201\n"


@pytest.fixture
def gheld(clock):
    fake, client, store = make_env(clock, G_CATALOG)
    fake.schedule.reserved.update({"G1", "G2"})
    return fake, client, store


def test_guard_429_waits_and_completes(gheld, clock):
    fake, client, store = gheld
    fake.throttle_next("CreatePersonalTime", retry_after=2)
    t0 = clock.t
    res = G.sync(client, store, EV, R.parse(G_RULES))
    assert clock.t - t0 >= 2 and not res.problems
    assert len(fake.schedule.personal_time) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_guard_auth_errors_stop_and_read_back(gheld, status):
    fake, client, store = gheld
    fake.fail_next("CreatePersonalTime", status)
    res = G.sync(client, store, EV, R.parse(G_RULES))
    assert fake.counts["CreatePersonalTime"] == 1        # stopped at the first error
    assert any("Stopped" in p for p in res.problems)
    assert any("Read-back differs" in p for p in res.problems)


def test_guard_404_on_update_when_entry_vanished(gheld):
    fake, client, store = gheld
    G.sync(client, store, EV, R.parse(G_RULES))
    fake.sessions["G2"] = fake.sessions["G2"].model_copy(update={"venue": "Caesars Forum"})
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)
    real = client.update_personal_time

    def vanish_first(event_id, pid, body):
        fake.schedule.personal_time.pop(pid, None)      # attendee deleted it in the app
        return real(event_id, pid, body)

    client.update_personal_time = vanish_first
    res = G.sync(client, store, EV, R.parse(G_RULES))
    assert fake.counts["UpdatePersonalTime"] == 1
    assert any("404" in p or "Stopped" in p for p in res.problems)
    assert any("1 missing" in p for p in res.problems)


def test_guard_read_back_failure_is_reported(gheld):
    fake, client, store = gheld
    fail_schedule_on_call(client, fake, 2)
    res = G.sync(client, store, EV, R.parse(G_RULES))
    assert len(fake.schedule.personal_time) == 2
    assert any("read-back failed" in p for p in res.problems)


# ---------------------------------------------------------------------- swap

S_CATALOG = [
    mk("H1", "SEC201-R", "2026-12-01", "10:00"),
    mk("H2", "SEC201-R1", "2026-12-02", "10:00"),
    mk("B1", "ARC301", "2026-12-01", "10:00"),
]
S_RULES = "targets:\n- code: ARC301\n- code: SEC201\n"


@pytest.fixture
def senv(clock):
    fake, client, store = make_env(clock, S_CATALOG)
    fake.schedule.reserved.add("H1")
    return fake, client, store


def sw(senv, body=S_RULES):
    _, client, store = senv
    return Swap(client, store, R.parse(body), EV)


def test_swap_404_on_wanted_session_raises_before_any_cancel(senv):
    fake, *_ = senv
    s = sw(senv)
    with pytest.raises(ApiError) as e:
        s.run("H1", "NOPE", approved=True)
    assert e.value.status == 404
    assert fake.counts.get("CancelReservation", 0) == 0 and fake.schedule.reserved == {"H1"}
    assert s.run("H1", "B1", approved=True).ok          # the in-flight lock was released


@pytest.mark.parametrize("status", [401, 403])
def test_swap_auth_error_on_cancel_reads_back_and_stops(senv, status):
    fake, *_ = senv
    fake.fail_next("CancelReservation", status)
    res = sw(senv).run("H1", "B1", approved=True)
    assert res.state == "failed" and "H1 is still held" in res.alert
    assert fake.counts.get("ReserveSessions", 0) == 0


@pytest.mark.parametrize("fault", ["503", "401", "unknown-code", "ghost"])
def test_swap_reserve_b_fails_then_a_rolled_back(senv, fault):
    fake, *_ = senv
    if fault == "unknown-code":
        fake.refuse["B1"] = "seatHeldByCrew"
    elif fault == "ghost":
        fake.ghost.add("B1")                               # said successful, not stored
    else:
        fake.fail_next("ReserveSessions", int(fault))
    res = sw(senv).run("H1", "B1", approved=True)
    assert res.state == "rolled_back"
    assert fake.schedule.reserved == {"H1"} and res.held_now == ["H1"]


def test_swap_read_back_failure_after_b_does_not_book_more(senv):
    fake, client, _ = senv
    fake.set_band("H1", "unavailable")                     # fallback is H2
    s = sw(senv)
    fail_schedule_on_call(client, fake, 2)                 # 1 = check, 2 = read-back after B
    res = s.run("H1", "B1", approved=True)
    assert fake.counts["ReserveSessions"] == 1             # no rollback or fallback after unknown
    assert fake.schedule.reserved == {"B1"}
    assert res.state == "failed" and res.alert.startswith("SWAP STATE UNKNOWN")


def test_swap_fallback_that_overlaps_a_held_session_is_not_a_fallback(clock):
    catalog = S_CATALOG + [mk("Z1", "DOP999", "2026-12-02", "10:00")]
    fake, client, store = make_env(clock, catalog)
    fake.schedule.reserved.update({"H1", "Z1"})
    fake.set_band("H1", "unavailable")
    fake.full.add("B1")
    res = Swap(client, store, R.parse(S_RULES), EV).run("H1", "B1", approved=True)
    assert fake.counts.get("CancelReservation", 0) == 0
    assert "H1" in fake.schedule.reserved and res.reasons


def test_swap_a_fills_in_the_gap_other_sitting_is_tried(senv):
    fake, *_ = senv
    fake.full.update({"B1", "H1"})                         # bands still read open
    res = sw(senv).run("H1", "B1", approved=True)
    assert res.state == "failed"
    assert fake.schedule.reserved == {"H2"}


# ---------------------------------------------------------------------- watcher

W_CATALOG = [
    mk("A1", "ARC301-R", "2026-12-01", "10:00", band="unavailable"),
    mk("A2", "ARC301-R1", "2026-12-02", "10:00", band="unavailable"),
    mk("C1", "SVS401", "2026-12-01", "10:30"),
]


@pytest.fixture
def wenv(clock):
    fake = FakeEventsApi(sessions=list(W_CATALOG))
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    return fake, client, Store(":memory:")


def watcher(wenv, clock, body="targets:\n- code: ARC301\n", **kw):
    fake, client, store = wenv
    w = Watcher(client, store, R.parse(body), EV, clock=clock, sleep=clock.sleep, **kw)
    events = []
    w.subscribe(events.append)
    w.tick()
    clock.sleep(60)
    return w, events


def test_watch_429_on_reserve_waits_and_books(wenv, clock):
    fake, *_ = wenv
    w, _ = watcher(wenv, clock)
    fake.throttle_next("ReserveSessions", retry_after=5)
    fake.set_band("A2", "available")
    res = w.tick()
    assert res.booked == ["A2"] and fake.schedule.reserved == {"A2"}


def test_watch_unknown_code_and_full_are_not_booked_and_not_crashed(wenv, clock):
    fake, *_ = wenv
    w, _ = watcher(wenv, clock)
    fake.refuse["A2"] = "seatHeldByCrew"
    fake.set_band("A2", "available")
    res = w.tick()
    assert res.error is None and res.booked == [] and fake.counts["ReserveSessions"] == 1


def test_watch_ghost_reserve_is_not_reported_booked(wenv, clock):
    fake, *_ = wenv
    w, events = watcher(wenv, clock)
    fake.ghost.add("A2")
    fake.set_band("A2", "available")
    res = w.tick()
    assert res.booked == [] and not [e for e in events if e.kind == "booked"]


def test_watch_schedule_read_failure_does_not_lose_the_opening(wenv, clock):
    fake, client, _ = wenv
    w, _ = watcher(wenv, clock)
    fake.set_band("A2", "available")
    fake.fail_next("GetSchedule", 503)
    assert w.tick().error
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"A2"}


@pytest.mark.parametrize("status", [403, 503])
def test_watch_reserve_error_emits_an_error_event(wenv, clock, status):
    fake, *_ = wenv
    w, events = watcher(wenv, clock)
    fake.fail_next("ReserveSessions", status)
    fake.set_band("A2", "available")
    res = w.tick()
    assert res.error or any(e.kind == "error" for e in events[1:])


def test_watch_swapper_exception_does_not_crash_tick(wenv, clock):
    fake, *_ = wenv
    fake.schedule.reserved.add("C1")

    def busy(p, approved):
        raise SwapBusy("Another swap is in flight. One at a time.")

    w, _ = watcher(wenv, clock, body="targets:\n- code: ARC301\n  auto_swap: true\n- code: SVS401\n",
                   swapper=busy)
    fake.set_band("A1", "available")
    res = w.tick()
    assert res.error
