import json

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.client import ApiError, EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.swap import _IN_FLIGHT, Swap, SwapBusy

EV = "reinvent2026"


def mk(sid, abbr, date, time_, band="available"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


CATALOG = [
    mk("H1", "SEC201-R", "2026-12-01", "10:00"),       # held A
    mk("H2", "SEC201-R1", "2026-12-02", "10:00"),      # A's other sitting
    mk("B1", "ARC301", "2026-12-01", "10:00"),         # wanted B, same slot as A
    mk("O1", "DOP302", "2026-12-01", "10:30"),         # another held session, overlaps B
    mk("O2", "SEC201-R2", "2026-12-03", "15:00"),
]
RULES = "targets:\n- code: ARC301\n- code: SEC201\n"


@pytest.fixture
def env(clock):
    fake = FakeEventsApi(sessions=CATALOG)
    fake.schedule.reserved.add("H1")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    return fake, client, store, clock


def swap(env, body=RULES):
    _, client, store, _ = env
    return Swap(client, store, R.parse(body), EV)


def cancels(fake):
    return fake.counts.get("CancelReservation", 0)


def journal_ops(store):
    return [(r["op"], r["outcome"]) for r in reversed(store.journal_entries(EV, 100))]


def test_happy_path_verified_and_every_transition_journaled(env):
    fake, _, store, _ = env
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "verified" and res.ok
    assert res.steps == ["proposed", "checked", "cancelled", "reserved", "verified"]
    assert fake.schedule.reserved == {"B1"} and res.held_now == ["B1"]
    ops = journal_ops(store)
    assert ("swap.cancel", "before:checked") in ops
    assert ("swap.reserve", "before:cancelled") in ops and ("swap.reserve", "after:cancelled") in ops
    assert ops[-1] == ("swap.verified", "state:verified")


def test_every_read_comes_before_the_cancel(env):
    fake, *_ = env
    swap(env).run("H1", "B1", approved=True)
    ops = [op for op, _ in fake.log]
    cut = ops.index("CancelReservation")
    assert ops[:cut] == ["GetSchedule"] + ["GetSession"] * 4   # B, A, then A's other sittings H2 and O2
    assert "GetSession" not in ops[cut:] and "ReserveSessions" not in ops[:cut]


def test_b_full_after_cancel_then_a_recovered(env):
    fake, _, store, _ = env
    fake.full.add("B1")                        # band still reads open, the reserve says full
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "rolled_back" and res.alert is None
    assert res.steps == ["proposed", "checked", "cancelled", "rolled_back"]
    assert fake.schedule.reserved == {"H1"}
    assert fake.counts["ReserveSessions"] == 2   # B once, A once. Nothing re-sent.
    ops = journal_ops(store)
    assert ("swap.rollback", "before:cancelled") in ops and ("swap.rollback", "after:cancelled") in ops
    assert ops[-1] == ("swap.rolled_back", "state:rolled_back")


def test_b_full_and_a_gone_then_fallback_booked(env):
    fake, _, store, _ = env
    fake.set_band("H1", "unavailable")         # A's room is full: once released it is gone
    fake.full.add("B1")
    res = swap(env).run("H1", "B1", approved=True)
    assert res.fallback_id == "H2"
    assert res.state == "failed" and fake.schedule.reserved == {"H2"}
    assert res.alert.startswith("SWAP FAILED") and "H2 is now held" in res.alert
    ops = journal_ops(store)
    assert ("swap.fallback", "after:cancelled") in ops
    assert ops[-1] == ("swap.failed", "state:failed")
    assert json.loads(store.journal_entries(EV, 1)[0]["response"]) == {"fallback_held": "H2"}


def test_409_on_the_reserve_after_the_cancel_stops_and_says_a_is_released(env):
    fake, _, store, _ = env
    fake.fail_next("ReserveSessions", 409)
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "failed" and res.closed
    assert fake.counts["ReserveSessions"] == 1   # no rollback into a 409: nothing more is sent
    assert fake.schedule.reserved == set() and res.held_now == []
    assert res.alert.startswith("SWAP STOPPED") and "H1 is released" in res.alert
    assert journal_ops(store)[-1] == ("swap.failed", "state:failed")


def test_a_failed_read_in_the_checks_is_journaled_and_nothing_cancelled(env):
    fake, _, store, _ = env
    fake.fail_next("GetSession", 503)
    with pytest.raises(ApiError):
        swap(env).run("H1", "B1", approved=True)
    assert cancels(fake) == 0 and fake.schedule.reserved == {"H1"}
    assert journal_ops(store)[-1] == ("swap.failed", "state:failed")
    assert json.loads(store.journal_entries(EV, 1)[0]["response"]) == {"read": "GetSession", "status": 503}


def test_b_full_a_gone_and_every_fallback_gone_alerts_with_nothing_held(env):
    fake, *_ = env
    fake.set_band("H1", "unavailable")
    fake.full.update({"B1", "H2", "O2"})       # both other sittings fill in the gap
    res = swap(env).run("H1", "B1", approved=True)
    assert res.fallbacks == ["H2", "O2"]
    assert res.state == "failed" and fake.schedule.reserved == set()
    assert "H2, O2 also failed" in res.alert and "Held now: nothing" in res.alert
    assert fake.counts["ReserveSessions"] == 4  # B, A, H2, O2: each once


@pytest.mark.parametrize("setup,reason", [
    (lambda f: f.set_band("B1", "unavailable"), "band is unavailable, not open"),
    (lambda f: f.set_band("B1", None), "band is none, not open"),
    (lambda f: (f.set_band("H1", "unavailable"), f.set_band("H2", "unavailable"),
                f.set_band("O2", "unavailable")), "has no fallback"),
    (lambda f: f.schedule.reserved.add("O1"), "also overlaps held DOP302"),
    (lambda f: f.schedule.reserved.discard("H1"), "H1 is not held"),
    (lambda f: f.schedule.reserved.add("B1"), "B1 is already held"),
])
def test_precondition_failures_block_the_cancel(env, setup, reason):
    fake, *_ = env
    setup(fake)
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "failed" and any(reason in r for r in res.reasons), res.reasons
    assert cancels(fake) == 0 and res.alert is None


def test_not_approved_and_no_auto_swap_blocks_the_cancel(env):
    fake, *_ = env
    res = swap(env).run("H1", "B1", approved=False)
    assert "not approved" in res.reasons[0] and cancels(fake) == 0


def test_same_code_already_held_blocks_two_sittings(env):
    fake, *_ = env
    fake.schedule.reserved.add("O2")           # SEC201-R2 held too
    res = swap(env, "targets:\n- code: SEC201\n").check("H1", "H2", approved=True)
    assert any("a sitting of SEC201 is already held" in r for r in res.reasons)


def test_429_mid_swap_waits_and_continues(env):
    fake, _, _, clock = env
    fake.throttle_next("ReserveSessions", retry_after=3)
    t0 = clock.t
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "verified" and clock.t - t0 >= 3


def test_409_mid_swap_leaves_journaled_failed_with_a_still_held(env):
    fake, _, store, _ = env
    fake.closed = True
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "failed" and "still held" in res.alert
    assert fake.schedule.reserved == {"H1"} and res.held_now == ["H1"]
    assert ("swap.failed", "state:failed") in journal_ops(store)


def test_cancel_5xx_with_a_still_held_stops(env):
    fake, *_ = env
    fake.fail_next("CancelReservation", 503)
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "failed" and "H1 is still held" in res.alert
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_cancel_5xx_with_unreadable_schedule_stops_and_says_so(env):
    fake, *_ = env
    sw = swap(env)
    fake.fail_next("CancelReservation", 503)
    real = sw.client.get_schedule
    calls = {"n": 0}

    def flaky(event_id):
        calls["n"] += 1
        if calls["n"] >= 2:                    # the check read works, the read-back does not
            fake.fail_next("GetSchedule", 503)
        return real(event_id)

    sw.client.get_schedule = flaky
    res = sw.run("H1", "B1", approved=True)
    assert res.state == "failed" and "Could not read the schedule back" in res.alert
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_one_swap_in_flight(env):
    assert _IN_FLIGHT.acquire(blocking=False)
    try:
        with pytest.raises(SwapBusy):
            swap(env).run("H1", "B1", approved=True)
    finally:
        _IN_FLIGHT.release()


def test_cli_swap_asks_first_and_reports(env, tmp_path, monkeypatch):
    fake, client, store, _ = env
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text(RULES)
    r = CliRunner().invoke(cli.app, ["swap", "H1", "B1"], input="n\n")
    assert r.exit_code == 1 and cancels(fake) == 0
    r = CliRunner().invoke(cli.app, ["swap", "H1", "B1"], input="y\n")
    assert r.exit_code == 0 and "verified" in r.output and fake.schedule.reserved == {"B1"}
    fake.schedule.reserved = {"H1"}
    fake.set_band("B1", "unavailable")
    r = CliRunner().invoke(cli.app, ["swap", "H1", "B1", "--yes"])
    assert r.exit_code == 1 and "not open" in r.output and cancels(fake) == 1


def test_watcher_auto_swap_runs_the_real_swap(env):
    from reseat.watcher import Watcher
    fake, client, store, clock = env
    fake.set_band("B1", "unavailable")
    rules = R.parse("targets:\n- code: ARC301\n  auto_swap: true\n- code: SEC201\n")
    w = Watcher(client, store, rules, EV, clock=clock, sleep=clock.sleep,
                swapper=lambda p, ok: Swap(client, store, rules, EV).run_plan(p, approved=ok))
    events = []
    w.subscribe(events.append)
    w.tick()
    fake.set_band("B1", "available")
    w.tick()
    assert fake.schedule.reserved == {"B1"}
    assert [e.data["state"] for e in events if e.kind == "swap"] == ["verified"]


def test_swap_lock_holds_across_processes(env):
    fake, _, store, _ = env
    assert store.acquire_lock("swap", "other-process:1")      # as if another reseat process
    with pytest.raises(SwapBusy, match="Another re:Seat process"):
        swap(env).run("H1", "B1", approved=True)
    assert cancels(fake) == 0
    store.release_lock("swap", "other-process:1")
    assert swap(env).run("H1", "B1", approved=True).state == "verified"


def test_stale_lock_from_a_crashed_process_expires(env):
    _, _, store, _ = env
    assert store.acquire_lock("swap", "dead:1", now=1000.0)
    assert not store.acquire_lock("swap", "me:1", now=1100.0)
    assert store.acquire_lock("swap", "me:1", now=1000.0 + 301)


def test_held_session_missing_from_local_catalog_blocks_the_cancel(env):
    fake, *_ = env
    fake.schedule.reserved.add("UNKNOWN-1")
    res = swap(env).run("H1", "B1", approved=True)
    assert any("not in the local catalog" in r for r in res.reasons) and cancels(fake) == 0


def fault_on_call(fake, op, n, outcome):
    """The nth call to `op` meets `outcome`. 'timeout' and 'garbled' apply the call first,
    as when the server acts and the response is lost or mangled. 'fail' and 'closed' (409)
    apply nothing."""
    import httpx
    real, seen = fake._dispatch, {"n": 0}

    def wrapped(req, op_, parts, path):
        if op_ == op:
            seen["n"] += 1
            if seen["n"] == n:
                if outcome == "fail":
                    return httpx.Response(503, json={"message": "unavailable"})
                if outcome == "closed":
                    return httpx.Response(409, json={"message": "not accepting requests"})
                real(req, op_, parts, path)
                if outcome == "timeout":
                    raise httpx.ReadTimeout("response lost")
                return httpx.Response(200, json={"unexpected": True})
        return real(req, op_, parts, path)

    fake._dispatch = wrapped


def test_garbled_reserve_body_after_the_cancel_is_read_back_not_raised(env):
    fake, _, store, _ = env
    fault_on_call(fake, "ReserveSessions", 1, "garbled")    # B landed, body off-spec
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "verified" and fake.schedule.reserved == {"B1"}
    assert journal_ops(store)[-1] == ("swap.verified", "state:verified")


def test_dropped_connection_on_a_cancel_that_landed_carries_on_to_b(env):
    fake, *_ = env
    fault_on_call(fake, "CancelReservation", 1, "timeout")
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "verified" and fake.schedule.reserved == {"B1"}
    assert fake.counts["CancelReservation"] == 1 and fake.counts["ReserveSessions"] == 1


def test_timeout_on_a_reserve_of_b_that_landed_is_verified_not_rolled_back(env):
    fake, *_ = env
    fault_on_call(fake, "ReserveSessions", 1, "timeout")
    res = swap(env).run("H1", "B1", approved=True)
    assert res.state == "verified" and fake.schedule.reserved == {"B1"}
    assert fake.counts["ReserveSessions"] == 1             # A not re-reserved next to B


def test_read_back_failure_after_the_rollback_sends_nothing_more(env):
    fake, _, store, _ = env
    fake.full.add("B1")
    fault_on_call(fake, "GetSchedule", 3, "fail")          # 1 check, 2 after B, 3 after A
    res = swap(env).run("H1", "B1", approved=True)
    assert fake.counts["ReserveSessions"] == 2             # B, then A. No fallback after unknown
    assert res.state == "failed" and res.alert.startswith("SWAP STATE UNKNOWN") and "H1 was sent" in res.alert
    assert journal_ops(store)[-1] == ("swap.failed", "state:failed")


def test_409_on_the_rollback_stops_before_any_fallback(env):
    fake, _, store, _ = env
    fake.full.add("B1")
    fault_on_call(fake, "ReserveSessions", 2, "closed")    # B full, then writes close on A
    res = swap(env).run("H1", "B1", approved=True)
    assert fake.counts["ReserveSessions"] == 2              # B, A. No fallback into the 409
    assert res.state == "failed" and res.closed and fake.schedule.reserved == set()
    assert res.alert.startswith("SWAP STOPPED") and "on the reserve of H1" in res.alert
    assert journal_ops(store)[-1] == ("swap.failed", "state:failed")


def test_409_on_a_fallback_stops_before_the_next_one(env):
    fake, *_ = env
    fake.set_band("H1", "unavailable")
    fake.full.update({"B1", "H1"})
    fault_on_call(fake, "ReserveSessions", 3, "closed")    # B full, A full, then 409 on H2
    res = swap(env).run("H1", "B1", approved=True)
    assert res.fallbacks == ["H2", "O2"]
    assert fake.counts["ReserveSessions"] == 3              # O2 never sent
    assert res.state == "failed" and res.closed and "on the reserve of H2" in res.alert


def test_the_cancel_is_journaled_before_and_after(env):
    _, _, store, _ = env
    swap(env).run("H1", "B1", approved=True)
    ops = journal_ops(store)
    assert ops.index(("swap.cancel", "before:checked")) < ops.index(("swap.cancel", "after:checked")) \
        < ops.index(("swap.cancelled", "state:cancelled"))


def test_cli_swap_declined_is_journaled_as_failed(env, tmp_path, monkeypatch):
    fake, client, store, _ = env
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text(RULES)
    r = CliRunner().invoke(cli.app, ["swap", "H1", "B1"], input="n\n")
    assert r.exit_code == 1 and cancels(fake) == 0
    assert journal_ops(store)[-1] == ("swap.failed", "state:failed")
    assert json.loads(store.journal_entries(EV, 1)[0]["response"]) == {"declined": True}


def test_garbled_favorites_body_is_an_api_error(env):
    fake, client, *_ = env
    fault_on_call(fake, "AssociateFavorites", 1, "garbled")
    with pytest.raises(ApiError, match="does not match the spec"):
        client.favorite(EV, ["B1"])
