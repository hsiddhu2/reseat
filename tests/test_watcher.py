import httpx
import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.watcher import PROPOSAL_TTL, Watcher, WatchError

EV = "reinvent2026"


def mk(sid, abbr, date="2026-12-01", time_="10:00", band="available", typ="Breakout session"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": typ,
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


def catalog():
    return [
        mk("A1", "ARC301-R", "2026-12-01", "10:00", band="unavailable"),
        mk("A2", "ARC301-R1", "2026-12-02", "10:00", band="unavailable"),
        mk("C1", "SVS401", "2026-12-01", "10:30", typ="Chalk talk"),
        mk("X1", "SEC201", "2026-12-03", "14:00"),
    ]


@pytest.fixture
def env(clock):
    fake = FakeEventsApi(sessions=catalog())
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    return fake, client, store, clock


def watcher(env, body, **kw):
    fake, client, store, clock = env
    w = Watcher(client, store, R.parse(body), EV, clock=clock, sleep=clock.sleep, **kw)
    events = []
    w.subscribe(events.append)
    w.tick()                                   # baseline sweep
    clock.sleep(60)
    return w, events


def kinds(events):
    return [e.kind for e in events]


def test_first_sweep_is_a_baseline_and_books_nothing(env):
    fake, *_ = env
    fake.set_band("A1", "available")
    w, events = watcher(env, "targets:\n- code: ARC301\n")
    assert fake.counts.get("ReserveSessions", 0) == 0
    assert kinds(events) == ["sweep"] and events[0].data["count"] == 4


def test_opened_target_gets_booked(env):
    fake, *_ = env
    w, events = watcher(env, "targets:\n- code: ARC301\n")
    fake.set_band("A2", "limited")
    res = w.tick()
    assert res.opened == ["A2"] and res.booked == ["A2"]
    assert fake.schedule.reserved == {"A2"}
    booked = [e for e in events if e.kind == "booked"][0]
    assert booked.data["code"] == "ARC301-R1" and booked.data["target"] == "ARC301"


def test_new_repeat_gets_booked(env):
    fake, *_ = env
    w, events = watcher(env, "targets:\n- code: ARC301\n")
    fake.add_session(mk("A3", "ARC301-R2", "2026-12-04", "09:00"))
    res = w.tick()
    assert "A3" in res.added and res.booked == ["A3"]
    assert fake.schedule.reserved == {"A3"}


def test_new_repeat_booked_for_planner_import_with_listed_sittings(env):
    fake, *_ = env
    w, _ = watcher(env, "targets:\n- code: ARC301\n  session_id: A1\n  sittings: [A1, A2]\n")
    fake.add_session(mk("A3", "ARC301-R2", "2026-12-04", "09:00"))
    assert w.tick().booked == ["A3"]


def test_moved_held_session_warns_and_does_not_act(env):
    fake, *_ = env
    fake.schedule.reserved.add("X1")
    w, events = watcher(env, "targets:\n- code: SEC201\n")
    fake.sessions["X1"] = fake.sessions["X1"].model_copy(update={"room": "Level 3 | Room 306"})
    calls = dict(fake.counts)
    res = w.tick()
    assert res.moved == ["X1"]
    moved = [e for e in events if e.kind == "moved"][0]
    assert moved.data["before"]["room"] == "Level 1 | Grand 117"
    assert moved.data["after"]["room"] == "Level 3 | Room 306"
    assert fake.counts.get("ReserveSessions", 0) == calls.get("ReserveSessions", 0)
    assert fake.counts.get("CancelReservation", 0) == 0


def test_cap_refused_and_interval_floor(env):
    _, client, store, _ = env
    over = R.Rules.model_construct(targets=[R.Target(code=f"X{i}") for i in range(3)], meals=[],
                                   buffer_minutes=30, max_per_day=5, watch_cap=2)
    with pytest.raises(WatchError, match="3 targets, watch_cap is 2"):
        Watcher(client, store, over, EV)
    with pytest.raises(WatchError, match="30s floor"):
        Watcher(client, store, R.parse("targets: []\n"), EV, interval=10)


def test_sweep_failure_is_retried_next_tick_not_crashed(env):
    fake, *_ = env
    w, events = watcher(env, "targets:\n- code: ARC301\n")
    fake.fail_next("ListSessions", 503)
    res = w.tick()
    assert res.error and res.error.startswith("503")
    assert "error" in kinds(events)
    fake.set_band("A2", "available")
    assert w.tick().booked == ["A2"]


def test_network_error_is_retried_next_tick(clock):
    fake = FakeEventsApi(sessions=catalog())
    state = {"down": False}

    def handler(req):
        if state["down"]:
            raise httpx.ConnectError("no route", request=req)
        return fake._handle(req)

    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=httpx.MockTransport(handler))
    w = Watcher(client, Store(":memory:"), R.parse("targets: []\n"), EV, clock=clock)
    w.tick()
    state["down"] = True
    assert "ConnectError" in w.tick().error
    state["down"] = False
    assert w.tick().error is None


def test_lower_priority_overlap_proposes_a_swap_not_a_booking(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")           # SVS401, Tue 10:30, second priority
    w, events = watcher(env, "targets:\n- code: ARC301\n- code: SVS401\n")
    fake.set_band("A1", "available")            # ARC301 Tue 10:00 overlaps C1
    res = w.tick()
    assert res.booked == [] and len(res.proposals) == 1
    p = res.proposals[0]
    assert (p.target, p.held_id, p.wanted_id, p.auto) == ("ARC301", "C1", "A1", False)
    assert fake.counts.get("CancelReservation", 0) == 0
    assert w.tick().proposals == []             # not proposed twice while pending
    assert [x.plan_id for x in w.pending()] == [p.plan_id]


def test_no_proposal_when_blocker_is_higher_priority(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")
    w, _ = watcher(env, "targets:\n- code: SVS401\n- code: ARC301\n")
    fake.set_band("A1", "available")
    res = w.tick()
    assert res.opened == ["A1"] and res.proposals == [] and res.booked == []


def test_no_proposal_when_blocker_is_outside_the_rules(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")           # held by hand, not a target
    w, _ = watcher(env, "targets:\n- code: ARC301\n")
    fake.set_band("A1", "available")
    res = w.tick()
    assert res.opened == ["A1"] and res.proposals == [] and res.booked == []
    assert fake.counts.get("CancelReservation", 0) == 0


def test_auto_swap_target_calls_the_swapper(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")
    calls = []

    class Done:
        state = "verified"

    w, events = watcher(env, "targets:\n- code: ARC301\n  auto_swap: true\n- code: SVS401\n",
                        swapper=lambda p, ok: calls.append(p) or Done())
    fake.set_band("A1", "available")
    w.tick()
    assert [(c.held_id, c.wanted_id) for c in calls] == [("C1", "A1")]
    assert [e.data["state"] for e in events if e.kind == "swap"] == ["verified"]
    assert w.pending() == []


def test_proposal_expires_and_take_is_one_shot(env):
    fake, client, store, clock = env
    fake.schedule.reserved.add("C1")
    w, _ = watcher(env, "targets:\n- code: ARC301\n- code: SVS401\n")
    fake.set_band("A1", "available")
    p = w.tick().proposals[0]
    assert w.take("not-a-plan") is None
    assert w.take(p.plan_id).wanted_id == "A1"
    assert w.take(p.plan_id) is None
    fake.set_band("A1", "unavailable")
    w.tick()
    fake.set_band("A1", "available")
    p2 = w.tick().proposals[0]
    clock.sleep(PROPOSAL_TTL)
    assert w.take(p2.plan_id) is None and w.pending() == []


def test_subscribers_get_events_and_a_broken_one_does_not_stop_the_watcher(env):
    fake, *_ = env
    w, events = watcher(env, "targets:\n- code: ARC301\n")

    def broken(_):
        raise RuntimeError("boom")

    w.subscribe(broken)
    fake.set_band("A2", "available")
    assert w.tick().booked == ["A2"]
    assert kinds(events)[-2:] == ["booked", "sweep"]
    assert w.subscriber_errors and "boom" in w.subscriber_errors[0]


def test_run_sleeps_the_interval_between_ticks(env):
    _, client, store, clock = env
    w = Watcher(client, store, R.parse("targets: []\n"), EV, interval=45, clock=clock, sleep=clock.sleep)
    t0 = clock.t
    w.run(max_ticks=3)
    assert clock.t - t0 == 90


def test_writes_closed_is_reported_not_crashed(env):
    fake, *_ = env
    w, events = watcher(env, "targets:\n- code: ARC301\n")
    fake.closed = True
    fake.set_band("A2", "available")
    res = w.tick()
    assert res.booked == [] and "409" in res.error
    assert any("409" in e.data.get("message", "") for e in events if e.kind == "error")
    fake.closed = False
    assert w.tick().booked == ["A2"]            # no band change needed: carried over and retried


def test_cli_watch_once(env, tmp_path, monkeypatch):
    fake, client, store, _ = env
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n")
    r = CliRunner().invoke(cli.app, ["watch", "--once"])
    assert r.exit_code == 0 and "4 sessions" in r.output
    r = CliRunner().invoke(cli.app, ["watch", "--interval", "5"])
    assert r.exit_code == 2 and "floor" in r.output


def test_transient_reserve_error_is_retried_next_tick_but_full_is_not(env):
    fake, *_ = env
    w, _ = watcher(env, "targets:\n- code: ARC301\n")
    fake.fail_next("ReserveSessions", 503)
    fake.set_band("A2", "available")
    assert w.tick().booked == []
    assert w.tick().booked == ["A2"]               # 503 was not an answer about the seat
    w2, _ = watcher(env, "targets:\n- code: SEC201\n")
    fake.full.add("X1")
    fake.set_band("X1", "limited")
    w2.tick()
    sent = fake.counts["ReserveSessions"]
    w2.tick()
    assert fake.counts["ReserveSessions"] == sent  # sessionFull is an answer: not re-sent


def test_approve_runs_a_pending_proposal_once(env):
    fake, *_ = env
    fake.schedule.reserved.add("C1")
    calls = []
    w, _ = watcher(env, "targets:\n- code: ARC301\n- code: SVS401\n",
                   swapper=lambda p, ok: calls.append((p.plan_id, ok)) or "done")
    fake.set_band("A1", "available")
    p = w.tick().proposals[0]
    assert len(p.plan_id) >= 20                    # unguessable
    assert w.approve("nope") is None
    assert w.approve(p.plan_id) == "done" and calls == [(p.plan_id, True)]
    assert w.approve(p.plan_id) is None            # one use


def test_auto_swap_uses_the_target_that_wanted_the_sitting(env):
    # SEC201 lists ARC301 as a backup and allows auto. ARC301 itself does not.
    fake, *_ = env
    fake.schedule.reserved.update({"X1", "C1"})
    calls = []
    w, _ = watcher(env, "targets:\n- code: SEC201\n  backups: [ARC301]\n  auto_swap: true\n"
                        "- code: ARC301\n- code: SVS401\n", swapper=lambda p, ok: calls.append(p))
    fake.set_band("A1", "available")
    props = w.tick().proposals
    assert [(p.target, p.auto) for p in props] == [("ARC301", False)]
    assert calls == []


def test_stop_ends_run(env):
    _, client, store, _ = env
    w = Watcher(client, store, R.parse("targets: []\n"), EV, interval=30)
    w.subscribe(lambda ev: w.stop())
    w.run()                                        # returns after the first tick, no real sleep
