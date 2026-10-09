"""Seen live on 8 October 2026: ListSessions answered 200 with totalCount 0, and GetSession
answered 404 for held sessions, while GetSchedule still worked. A sweep must never read
that as "every session was removed", and a re-keyed catalog must not look like news."""

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.models import Session
from reseat.store import Store, SweepRefused
from reseat.watcher import Watcher

EV = "reinvent2026"


def mk(sid, abbr, band="available", time_="10:00"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": abbr, "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": "2026-12-01", "time": time_, "length": "60"}})


CATALOG = [mk(f"OLD{i}", f"ARC30{i}") for i in range(10)]


def test_first_sweep_into_an_empty_store_is_a_baseline():
    st = Store(":memory:")
    res = st.apply_sweep(EV, CATALOG, with_abstracts=False)
    assert res.baseline and res.opened == [] and len(st.all(EV)) == 10


def test_empty_sweep_is_refused_and_nothing_changes():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    with pytest.raises(SweepRefused, match="empty catalog"):
        st.apply_sweep(EV, [], with_abstracts=False)
    assert len(st.all(EV)) == 10


def test_partial_sweep_is_refused():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    with pytest.raises(SweepRefused, match="partial answer"):
        st.apply_sweep(EV, CATALOG[:3], with_abstracts=False)          # 7 of 10 gone, none new
    assert len(st.all(EV)) == 10


def test_normal_churn_is_reported_as_news():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    res = st.apply_sweep(EV, CATALOG[:8] + [mk("NEW1", "ARC301-R9", band="limited")], with_abstracts=False)
    assert not res.baseline and res.added == ["NEW1"] and sorted(res.removed) == ["OLD8", "OLD9"]
    assert "NEW1" in res.opened


def test_rekeyed_catalog_is_a_baseline_not_news():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    rekeyed = [mk(f"NEW{i}", f"ARC30{i}") for i in range(10)]
    res = st.apply_sweep(EV, rekeyed, with_abstracts=False)
    assert res.baseline and len(res.removed) == 10
    assert res.opened == []                                          # 10 "new" sessions are not openings
    assert {s.session_id for s in st.all(EV)} == {f"NEW{i}" for i in range(10)}


def test_force_applies_an_empty_sweep():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    res = st.apply_sweep(EV, [], with_abstracts=False, force=True)
    assert res.baseline and st.all(EV) == []


@pytest.fixture
def live(fake, client, clock):
    fake.sessions = {s.session_id: s.model_copy() for s in CATALOG}
    fake.set_band("OLD1", "unavailable")
    store = Store(":memory:")
    w = Watcher(client, store, R.parse("targets:\n- code: ARC301\n"), EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    w.tick()
    clock.sleep(60)
    return fake, store, w, events


def test_watcher_keeps_the_catalog_and_books_nothing_when_the_api_serves_none(live, clock):
    fake, store, w, events = live
    fake.sessions = {}                                               # what the API served on 8 October
    res = w.tick()
    assert res.failure == "catalog" and "empty catalog" in res.error
    assert len(store.all(EV)) == 10
    assert fake.counts.get("ReserveSessions", 0) == 0
    assert [e.kind for e in events if e.kind == "outage"] == ["outage"]


def test_watcher_records_a_rekeyed_catalog_and_acts_on_nothing(live, clock):
    fake, store, w, events = live
    fake.sessions = {f"NEW{i}": mk(f"NEW{i}", f"ARC30{i}") for i in range(10)}
    res = w.tick()
    assert res.failure is None and res.added == [] and res.booked == []
    assert fake.counts.get("ReserveSessions", 0) == 0
    assert any("re-keyed" in e.data.get("message", "") for e in events if e.kind == "error")
    fake.set_band("NEW1", "unavailable")
    clock.sleep(60)
    w.tick()
    fake.set_band("NEW1", "limited")                                 # a real opening after the baseline
    clock.sleep(60)
    assert w.tick().booked == ["NEW1"]


def test_cli_sync_and_book_refuse_an_empty_catalog(fake, client, tmp_path, monkeypatch):
    store = Store(tmp_path / "reseat.db")
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n")
    fake.sessions = {}
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts"])
    assert r.exit_code == 4 and "empty catalog" in r.output and len(store.all(EV)) == 10
    r = CliRunner().invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 4 and "Not booking" in " ".join(r.output.split())
    assert fake.counts.get("ReserveSessions", 0) == 0


# ---- guards added after the 8 October finding


def test_a_partial_answer_of_new_ids_is_refused_not_taken_for_a_rekey():
    st = Store(":memory:")
    st.apply_sweep(EV, CATALOG, with_abstracts=False)
    with pytest.raises(SweepRefused, match="partial answer"):
        st.apply_sweep(EV, [mk(f"NEW{i}", f"ARC30{i}") for i in range(3)], with_abstracts=False)
    assert len(st.all(EV)) == 10                     # the other 7 can never come back as "added"


def test_a_short_catalog_walk_is_refused(fake, client):
    from reseat.client import IncompleteCatalog
    fake.sessions = {s.session_id: s for s in CATALOG}
    fake.page_size = 4
    real = fake._list_sessions

    def drop_the_last_page(req):
        r = real(req)
        if req.url.params.get("nextToken") == "4":
            body = r.json()
            body.pop("nextToken", None)               # the walk would stop after 8 of 10
            import httpx
            return httpx.Response(200, json=body)
        return r

    fake._list_sessions = drop_the_last_page
    with pytest.raises(IncompleteCatalog, match="8 of 10"):
        list(client.iter_sessions(EV))


def test_nothing_is_planned_while_a_held_session_is_missing_locally(fake, client, store, clock):
    from reseat.mcp_server import Tools
    from reseat.router import Router, run_booking
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    fake.sessions = {s.session_id: s for s in CATALOG}
    fake.schedule.reserved.add("17842318-NOT-LOCAL")          # held in a range the store lacks
    rules = R.parse("targets:\n- code: ARC301\n")
    plan = Router(rules, store, EV).plan(held=fake.schedule.reserved)
    assert plan.batch == [] and "not in the local catalog" in plan.blocked
    run = run_booking(Router(rules, store, EV), client, store, EV, fake.schedule.reserved)
    assert run.executions == [] and fake.counts.get("ReserveSessions", 0) == 0
    assert "Nothing planned" in Tools(client, store, rules, EV, clock=clock).propose_changes()


def test_watcher_pauses_booking_while_a_held_session_is_missing_and_keeps_the_opening(live, clock):
    fake, store, w, events = live
    fake.schedule.reserved.add("17842318-NOT-LOCAL")
    fake.set_band("OLD1", "available")
    res = w.tick()
    assert res.booked == [] and fake.counts.get("ReserveSessions", 0) == 0
    assert any("not in the local catalog" in e.data.get("message", "") for e in events if e.kind == "error")
    fake.schedule.reserved.discard("17842318-NOT-LOCAL")      # a sync found it, or it was released
    clock.sleep(60)
    assert w.tick().booked == ["OLD1"]                        # the queued opening was kept


def test_rekey_clears_proposals_and_still_acts_on_sessions_that_kept_their_id(fake, client, clock):
    keep = [mk("KEEP1", "ARC301", band="unavailable", time_="10:00"), mk("C1", "SVS401", time_="10:30")]
    old = [mk(f"OLD{i}", f"DOP30{i}", time_=f"{12 + i}:00") for i in range(8)]
    fake.sessions = {s.session_id: s for s in keep + old}
    fake.schedule.reserved.add("C1")
    store = Store(":memory:")
    w = Watcher(client, store, R.parse("targets:\n- code: ARC301\n- code: SVS401\n"), EV,
                clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    w.tick()
    clock.sleep(60)
    w.proposals["stale-plan-id-0000000"] = None  # type: ignore[assignment]
    new = [mk(f"NEW{i}", f"DOP30{i}", time_=f"{12 + i}:00") for i in range(8)]
    fake.sessions = {s.session_id: s for s in keep + new}
    fake.set_band("KEEP1", "available")                       # opens in the re-key sweep itself
    res = w.tick()
    assert "stale-plan-id-0000000" not in w.proposals
    assert res.opened == ["KEEP1"] and [p.wanted_id for p in res.proposals] == ["KEEP1"]
    assert any("re-keyed" in e.data.get("message", "") for e in events if e.kind == "error")


def test_an_empty_catalog_list_still_lets_onsite_polling_run(live, clock):
    fake, store, w, events = live
    fake.sessions_backup = dict(fake.sessions)
    real = fake._list_sessions
    import httpx
    fake._list_sessions = lambda req: httpx.Response(200, json={"items": [], "totalCount": 0})
    w.tick()
    assert w.down_since is not None and w._down_kind == "catalog"
    polled = []
    w.onsite_tick = lambda day: polled.append(day)            # type: ignore[assignment]
    w.run(max_ticks=2, onsite_day=lambda: "2026-12-01")
    assert polled                                             # GetSession polling was not stopped
    fake._list_sessions = real


def test_offline_push_says_the_catalog_is_the_problem(live, clock):
    fake, store, w, events = live
    import httpx
    fake._list_sessions = lambda req: httpx.Response(200, json={"items": [], "totalCount": 0})
    for _ in range(8):
        w.tick()
        clock.sleep(w.next_delay())
    offline = [e for e in events if e.kind == "offline"]
    assert offline and "no usable catalog" in offline[0].data["message"]


def test_cli_book_says_why_when_a_held_session_is_missing(fake, client, tmp_path, monkeypatch):
    store = Store(tmp_path / "reseat.db")
    fake.sessions = {s.session_id: s for s in CATALOG}
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    fake.schedule.reserved.add("17842318-NOT-LOCAL")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n")
    r = CliRunner().invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 4 and "not in the local catalog" in " ".join(r.output.split())
    assert fake.counts.get("ReserveSessions", 0) == 0
