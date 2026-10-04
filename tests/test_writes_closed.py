"""Writes closed. A 409 means "Intentionally disabled. Retrying will not help until it is
re-enabled." So after a 409 the watcher sends no write of any kind for 15 minutes, keeps
every opening queued, then tries once. Every path that can send a write is checked here,
plus the clash check that decides whether a swap is proposed at all.
"""

import threading
import time

import pytest

from reseat import rules as R
from reseat import serve as S
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.swap import Swap
from reseat.watcher import CLOSED_RECHECK, ApprovalFailed, Watcher

EV = "reinvent2026"
WRITES = ("ReserveSessions", "CancelReservation", "AssociateFavorites", "DisassociateFavorite",
          "CreatePersonalTime", "UpdatePersonalTime", "DeletePersonalTime")
RULES = ("serve_secret: correct-horse-battery-staple\n"
         "targets:\n- code: ARC301\n- code: SVS401\n- code: SEC201\n")


def mk(sid, abbr, date="2026-12-01", time_="10:00", band="available", timed=True):
    body = {"sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
            "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
            "seatAvailability": band}
    if timed:
        body["sessionTime"] = {"date": date, "time": time_, "length": "60"}
    return Session.model_validate(body)


def catalog():
    return [
        mk("A1", "ARC301-R", "2026-12-01", "10:00", band="unavailable"),
        mk("A2", "ARC301-R1", "2026-12-02", "10:00", band="unavailable"),
        mk("C1", "SVS401", "2026-12-01", "10:30"),
        mk("X1", "SEC201", "2026-12-03", "14:00", band="unavailable"),
    ]


class Env:
    """Held C1 (SVS401). ARC301 outranks it. Baseline sweep done, one minute later."""

    def __init__(self, clock, body=RULES, swapper=True):
        self.clock = clock
        self.fake = FakeEventsApi(sessions=catalog())
        self.fake.schedule.reserved.add("C1")
        self.client = EventsClient(token_provider=lambda: self.fake.token, quota=QuotaTracker(clock=clock),
                                   sleep=clock.sleep, transport=self.fake.transport())
        self.store = Store(":memory:")
        self.rules = R.parse(body)
        sw = (lambda p, ok: Swap(self.client, self.store, self.rules, EV).run_plan(p, approved=ok)) \
            if swapper else None
        self.w = Watcher(self.client, self.store, self.rules, EV, clock=clock, sleep=clock.sleep, swapper=sw)
        self.events = []
        self.w.subscribe(self.events.append)
        self.w.tick()
        clock.sleep(60)

    def writes(self):
        return sum(self.fake.counts.get(op, 0) for op in WRITES)

    def messages(self, text):
        return [e for e in self.events if e.kind in ("error", "writes") and text in e.data.get("message", "")]

    def close_with_x1(self):
        """X1 opens while writes are closed: one 409, the hold starts."""
        self.fake.closed = True
        self.fake.set_band("X1", "available")
        res = self.w.tick()
        assert "409" in res.error and self.w.writes_closed_until is not None
        self.fake.closed = False        # the API reopens at once: only the hold keeps writes back


@pytest.fixture
def env(clock):
    return Env(clock)


# ---- full sweep booking


def test_recheck_409_rearms_the_hold_and_one_write_per_window(env):
    env.fake.closed = True
    env.fake.set_band("X1", "available")
    for _ in range(45):                       # 45 minutes of ticks while the API stays closed
        env.w.tick()
        env.clock.sleep(60)
    assert env.fake.counts["ReserveSessions"] == 3      # minute 0, 15 and 30, nothing between
    assert len(env.messages("writes are closed (409)")) == 1
    env.fake.closed = False
    env.w.tick()
    assert env.fake.counts["ReserveSessions"] == 4 and "X1" in env.fake.schedule.reserved


def test_writes_open_again_clears_the_hold_and_is_said(env):
    env.close_with_x1()
    env.clock.sleep(CLOSED_RECHECK)
    assert env.w.tick().booked == ["X1"]
    assert env.w.writes_closed_until is None
    assert len(env.messages("writes are open")) == 1
    rows = [r for r in env.store.journal_entries(EV, 100) if r["op"] == "watcher.writes"]
    assert sorted(r["outcome"] for r in rows) == ["closed", "open"]


def test_a_new_409_after_reopening_is_said_again(env):
    env.close_with_x1()
    env.clock.sleep(CLOSED_RECHECK)
    env.w.tick()
    env.fake.closed = True
    env.fake.set_band("A2", "available")
    env.clock.sleep(60)
    env.w.tick()
    assert len(env.messages("writes are closed (409)")) == 2


# ---- on-site polling


def test_onsite_opening_during_the_hold_is_queued_with_the_sweep_opening(env):
    env.close_with_x1()
    env.fake.set_band("A2", "available")                # ARC301 on Wednesday, no clash
    sent = env.writes()
    assert env.w.onsite_tick("2026-12-02").booked == []
    assert env.writes() == sent
    env.clock.sleep(CLOSED_RECHECK)
    booked = env.w.onsite_tick("2026-12-02").booked
    assert sorted(booked) == ["A2", "X1"]               # both queued openings, after the hold


# ---- swaps


def test_auto_swap_waits_out_the_hold_then_runs(clock):
    body = RULES.replace("- code: ARC301\n", "- code: ARC301\n  auto_swap: true\n")
    e = Env(clock, body)
    e.close_with_x1()
    e.fake.set_band("A1", "available")                  # better sitting over held C1
    for _ in range(10):
        e.w.tick()
        clock.sleep(60)
    assert e.fake.counts.get("CancelReservation", 0) == 0 and e.w.pending() == []
    clock.sleep(CLOSED_RECHECK)
    e.w.tick()
    assert e.fake.schedule.reserved == {"A1", "X1"}


def test_swap_after_the_hold_clears_it(clock):
    body = RULES.replace("- code: ARC301\n", "- code: ARC301\n  auto_swap: true\n")
    e = Env(clock, body)
    e.fake.closed = True
    e.fake.set_band("A1", "available")
    e.w.tick()                                          # the auto swap's cancel meets the 409
    assert e.w.writes_closed_until is not None
    e.fake.closed = False
    clock.sleep(CLOSED_RECHECK)
    e.w.tick()
    assert e.fake.schedule.reserved == {"A1"}
    assert e.w.writes_closed_until is None


def test_manual_approve_during_the_hold_sends_nothing_and_keeps_the_proposal(env):
    env.fake.set_band("A1", "available")
    p = env.w.tick().proposals[0]
    env.fake.closed = True
    env.fake.set_band("X1", "available")
    env.clock.sleep(60)
    env.w.tick()
    env.fake.closed = False
    sent = env.writes()
    with pytest.raises(ApprovalFailed, match="409"):
        env.w.approve(p.plan_id)
    assert env.writes() == sent and env.fake.counts.get("GetSession", 0) == 0
    assert [x.plan_id for x in env.w.pending()] == [p.plan_id]


def test_manual_approve_cancel_409_starts_the_hold(env):
    env.fake.set_band("A1", "available")
    p = env.w.tick().proposals[0]
    env.fake.closed = True
    out = env.w.approve(p.plan_id)
    assert out.state == "failed" and out.closed and env.fake.schedule.reserved == {"C1"}
    assert env.w.writes_closed_until is not None
    env.fake.closed = False
    env.fake.set_band("X1", "available")
    env.clock.sleep(60)
    sent = env.writes()
    env.w.tick()
    assert env.writes() == sent                         # X1 waits for the hold
    env.clock.sleep(CLOSED_RECHECK)
    assert env.w.tick().booked == ["X1"]


def test_409_on_the_swap_reserve_starts_the_hold(env):
    env.fake.set_band("A1", "available")
    p = env.w.tick().proposals[0]
    env.fake.fail_next("ReserveSessions", 409, times=5)
    env.w.approve(p.plan_id)
    assert env.w.writes_closed_until is not None


def test_proposal_that_expires_during_the_hold_is_raised_again(env):
    env.fake.set_band("A1", "available")
    assert env.w.tick().proposals
    env.close_with_x1()
    for _ in range(16):
        env.clock.sleep(60)
        env.w.tick()
    assert [(x.held_id, x.wanted_id) for x in env.w.pending()] == [("C1", "A1")]


# ---- phone approve


def test_phone_approve_during_the_hold_sends_nothing(env):
    env.fake.set_band("A1", "available")
    p = env.w.tick().proposals[0]
    env.fake.closed = True
    env.fake.set_band("X1", "available")
    env.clock.sleep(60)
    env.w.tick()
    env.fake.closed = False
    app = S.App(env.w, env.store, env.rules, EV, host="127.0.0.1", port=0, clock=env.clock)
    sent = env.writes()
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            env.w.drain()
            time.sleep(0.005)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        out = app.approve(p.plan_id, wait=5)
    finally:
        stop.set()
        t.join(2)
    assert out["kept"] is True and "409" in out["error"]
    assert env.writes() == sent and env.fake.schedule.reserved == {"C1"}


# ---- which clashes block a proposal


def clashes(env, extra, held, wanted="A1", blocker="C1"):
    for s in extra:
        env.fake.add_session(s)
    env.store.apply_sweep(EV, list(env.fake.sessions.values()), with_abstracts=False)
    return env.w._other_clashes(wanted, blocker, set(held))


def test_clash_same_code_held(env):
    assert clashes(env, [], {"C1", "A2"}) is True        # A2 is another sitting of ARC301


def test_clash_overlap_with_a_second_hold(env):
    assert clashes(env, [mk("D1", "DOP302", "2026-12-01", "10:15")], {"C1", "D1"}) is True


def test_no_clash_with_a_second_hold_at_another_time(env):
    assert clashes(env, [mk("D1", "DOP302", "2026-12-01", "14:00")], {"C1", "D1"}) is False


def test_held_session_missing_locally_counts_as_a_clash(env):
    assert clashes(env, [], {"C1", "GONE"}) is True
    assert clashes(env, [], {"C1"}, wanted="GONE") is True


def test_sessions_with_no_time_clash_only_on_code(env):
    untimed = [mk("U1", "DOP302", timed=False), mk("U2", "ARC301-R2", timed=False)]
    assert clashes(env, untimed, {"C1", "U1"}) is False
    assert clashes(env, [], {"C1", "U2"}) is True
    assert clashes(env, [], {"C1", "X1"}, wanted="U1") is False


def test_second_hold_at_another_time_still_gets_a_proposal(env):
    env.fake.add_session(mk("D1", "DOP302", "2026-12-01", "14:00"))
    env.fake.schedule.reserved.add("D1")
    env.w.tick()
    env.clock.sleep(60)
    env.fake.set_band("A1", "available")
    assert [(p.held_id, p.wanted_id) for p in env.w.tick().proposals] == [("C1", "A1")]


# ---- probing with a session that can never be held


def probe_env(clock, probe_held=False):
    from reseat.client import EventsClient, QuotaTracker
    from reseat.fakeapi import FakeEventsApi
    from reseat.models import Session
    from reseat.store import Store
    from reseat.watcher import Watcher

    def mk(sid, abbr, band, reservable=True, time_="10:00"):
        return Session.model_validate({
            "sessionId": sid, "abbreviation": abbr, "title": abbr, "type": "Breakout session",
            "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": reservable,
            "seatAvailability": band, "sessionTime": {"date": "2026-12-01", "time": time_, "length": "60"}})

    sessions = [mk("A1", "ARC301", "unavailable"), mk("K1", "KEY001", None, reservable=False, time_="08:00")]
    fake = FakeEventsApi(sessions=sessions)
    if probe_held:
        fake.schedule.reserved.add("K1")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    w = Watcher(client, store, R.parse("probe_session: K1\ntargets:\n- code: ARC301\n"), EV,
                clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    w.tick()
    clock.sleep(60)
    return fake, w, events


def test_probe_resumes_booking_the_moment_writes_open(clock):
    fake, w, events = probe_env(clock)
    fake.closed = True
    fake.set_band("A1", "available")
    w.tick()
    assert w.writes_closed_until is not None
    clock.sleep(60)
    w.tick()                                    # still closed: the probe gets 409, no reserve
    assert fake.counts["ReserveSessions"] == 1
    fake.closed = False
    clock.sleep(60)
    assert w.tick().booked == ["A1"]            # minute 2, not minute 15
    assert [e.data["state"] for e in events if e.kind == "writes"] == ["closed", "open"]
    assert fake.schedule.reserved == {"A1"}     # the probe changed nothing


def test_probe_never_uses_a_session_that_is_held(clock):
    fake, w, events = probe_env(clock, probe_held=True)
    fake.closed = True
    fake.set_band("A1", "available")
    w.tick()
    fake.closed = False
    for _ in range(3):
        clock.sleep(60)
        w.tick()
    assert fake.counts.get("CancelReservation", 0) == 0    # never a cancel on a held seat
    assert "K1" in fake.schedule.reserved


def test_paused_and_resumed_are_pushed():
    from reseat import push
    from reseat.watcher import WatchEvent
    def title(state):
        return push.message(WatchEvent("writes", 0, {"state": state, "message": "m"}), str)[0]
    assert title("closed") == "Booking paused" and title("open") == "Booking resumed"
