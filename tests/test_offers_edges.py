"""Edges found in review: re-keyed catalogs, `reseat watch` without a web app, a seat already held,
and a real batch of new sessions that must not be mistaken for a catalog load."""

from test_offers import Env, mk

from reseat import findings
from reseat.models import Session

EV = "reinvent2026"


def test_a_rekeyed_catalog_clears_offers_too(clock):
    env = Env(clock)
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.w._baseline(type("S", (), {"reason": "rekeyed", "removed": [], "added": []})(), clock())
    assert not env.w.is_pending(pid) and env.w.approve(pid) is None


def test_no_offers_when_nothing_can_take_the_tap(clock):
    env = Env(clock)
    env.w.offering = False
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    assert env.kinds("offer") == [] and env.w.offers == {}


def test_a_seat_already_held_says_so_not_refused(clock):
    env = Env(clock)
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.fake.schedule.reserved.add("N1")
    res = env.w.approve(pid)
    assert res.state == "already" and env.fake.counts.get("ReserveSessions", 0) == 0
    assert env.kinds("not_booked")[-1].data["message"].endswith("you already hold it.")


def _session(i):
    return Session.model_validate({
        "sessionId": f"B{i:03d}", "abbreviation": f"BAT{i:03d}", "title": "Batch", "type": "Workshop",
        "venue": "Wynn", "isReservable": True, "seatAvailability": "available",
        "sessionTime": {"date": "2026-12-03", "time": "09:00", "length": "60"}})


def test_a_real_batch_of_over_a_hundred_new_sessions_is_counted_not_taken_for_a_load(store, client, fake):
    sessions = list(client.iter_sessions(EV, include_abstracts=False))
    big = sessions + [_session(i) for i in range(300)]
    store.apply_sweep(EV, big, False, now=1_796_000_000.0)                 # the load: 306 sessions
    fake.set_band("S-ARC1", "limited")
    store.apply_sweep(EV, [s.model_copy(update={"seat_availability": "limited"}) if s.session_id == "S-ARC1"
                           else s for s in big], False, now=1_796_000_060.0)
    batch = big + [_session(i) for i in range(300, 420)]                  # 120 real new sessions
    store.apply_sweep(EV, batch, False, now=1_796_000_120.0)
    f = findings.compute(store, EV, 1_796_000_200.0)
    assert f["catalog"]["added"] == 120                                   # counted as news
    assert f["since"].startswith("2026-11-") and f["sweeps"] == 3          # the window kept its start
