"""The watcher runs all week in a hotel room. Outages, timeouts, 5xx storms and a failed
token refresh must never end the loop."""

import httpx
import pytest

from reseat import rules as R
from reseat.auth import AuthError
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.watcher import BACKOFF_CAP, OFFLINE_AFTER, Watcher

EV = "reinvent2026"


def mk(sid, abbr, date, time_, band):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


def catalog():
    # Six sessions with a page size of 2, so a sweep takes three ListSessions calls.
    return [mk(f"A{i}", f"ARC30{i}-R", "2026-12-01", f"{8 + i:02d}:00", "unavailable") for i in range(6)]


class Net:
    """A transport that can go down: refused, timing out, or refused only on one page."""

    def __init__(self, fake):
        self.fake, self.mode, self.calls = fake, None, 0

    def __call__(self, req):
        self.calls += 1
        if self.mode == "refused":
            raise httpx.ConnectError("Connection refused", request=req)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=req)
        if self.mode == "refused-page-2" and req.url.params.get("nextToken") == "2":
            raise httpx.ConnectError("Connection refused", request=req)
        return self.fake._handle(req)


@pytest.fixture
def env(clock):
    fake = FakeEventsApi(sessions=catalog(), page_size=2)
    net = Net(fake)
    state = {"token": fake.token, "refresh_ok": True}

    def refresh():
        if not state["refresh_ok"]:
            raise AuthError("Refresh token expired. Run: reseat login")
        state["token"] = fake.token
        return fake.token

    client = EventsClient(token_provider=lambda: state["token"], on_refresh=refresh,
                          quota=QuotaTracker(clock=clock), sleep=clock.sleep,
                          transport=httpx.MockTransport(net))
    store = Store(":memory:")
    w = Watcher(client, store, R.parse("targets:\n- code: ARC301\n- code: ARC302\n"), EV,
                clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    w.tick()                                    # baseline
    return fake, net, state, w, events, store, clock


def kinds(events, *want):
    return [e.kind for e in events if e.kind in want]


def test_connection_refused_mid_sweep_saves_nothing_and_recovers(env):
    fake, net, _, w, events, store, clock = env
    before = store.last_sweep(EV)
    fake.set_band("A1", "available")
    net.mode = "refused-page-2"                  # page 1 of 3 works, page 2 refused
    res = w.tick()
    assert res.failure == "outage" and "ConnectError" in res.error
    assert store.last_sweep(EV) == before        # a partial sweep is never saved
    assert kinds(events, "outage") == ["outage"]
    net.mode = None
    clock.sleep(w.next_delay())
    res = w.tick()
    assert res.failure is None and res.booked == ["A1"]   # the opening was not lost
    assert kinds(events, "back") == ["back"]
    log = [r["outcome"] for r in store.journal_entries(EV) if r["op"] == "watcher.outage"]
    assert log == ["end", "start"]


def test_503_storm_across_three_sweeps_backs_off_then_resumes(env):
    fake, _, _, w, events, _, clock = env
    fake.fail_next("ListSessions", 503, times=3)
    t0 = clock.t
    w.run(max_ticks=4)                           # three failed sweeps, then one good one
    assert kinds(events, "outage", "back") == ["outage", "back"]
    assert clock.t - t0 == 60 + 120 + 240        # doubling from the 60 s interval
    assert w.next_delay() == 60                  # normal interval after recovery
    assert kinds(events, "offline") == []        # under 10 minutes: no offline push


def test_long_outage_caps_backoff_announces_offline_and_back(env):
    _, net, _, w, events, _, clock = env
    net.mode = "timeout"
    delays = []
    for _ in range(6):
        res = w.tick()
        assert res.failure == "outage"
        delays.append(w.next_delay())
        clock.sleep(w.next_delay())
    assert delays == [60, 120, 240, BACKOFF_CAP, BACKOFF_CAP, BACKOFF_CAP]
    offline = [e for e in events if e.kind == "offline"]
    assert len(offline) == 1 and offline[0].data["message"].startswith("re:Seat offline since ")
    assert offline[0].at - offline[0].data["since"] >= OFFLINE_AFTER
    net.mode = None
    w.tick()
    back = [e for e in events if e.kind == "back"]
    assert back[0].data["message"] == "re:Seat back" and back[0].data["announced"] is True


def test_refresh_failure_goes_read_only_and_recovers(env):
    fake, _, state, w, events, _, clock = env
    fake.token = "rotated"                       # the stored access token no longer works
    state["refresh_ok"] = False
    fake.schedule.reserved.add("A0")
    res = w.tick()
    assert res.failure == "auth" and w.read_only
    signin = [e for e in events if e.kind == "signin"]
    assert signin[0].data["state"] == "needed"
    assert w.approve("any-plan") is None         # no writes while signed out
    clock.sleep(w.next_delay())
    assert w.tick().failure == "auth"            # loop alive, still one signin event
    assert len([e for e in events if e.kind == "signin"]) == 1
    state["refresh_ok"] = True                   # the attendee ran reseat login
    fake.set_band("A1", "available")
    clock.sleep(w.next_delay())
    res = w.tick()
    assert res.failure is None and not w.read_only and res.booked == ["A1"]
    assert [e.data["state"] for e in events if e.kind == "signin"] == ["needed", "ok"]
    assert fake.counts.get("ReserveSessions", 0) == 1


def test_not_signed_in_at_all_never_crashes(clock):
    fake = FakeEventsApi(sessions=catalog())

    def no_token():
        raise AuthError("Not signed in. Run: reseat login")

    client = EventsClient(token_provider=no_token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    w = Watcher(client, Store(":memory:"), R.parse("targets: []\n"), EV, clock=clock, sleep=clock.sleep)
    w.run(max_ticks=3)
    assert w.read_only


def test_loop_survives_any_exception_in_a_tick(env):
    _, _, _, w, events, _, _ = env

    def boom():
        raise RuntimeError("disk full")

    w._tick = lambda res, now: boom()
    w.run(max_ticks=2)
    assert len([e for e in events if e.kind == "error" and "disk full" in e.data["message"]]) == 2
