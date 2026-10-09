
"""Boundaries of the sweep guards: the shrink limit, tiny catalogs, partial re-keys,
the watcher across a long refusal, and the CLI paths for --force and partial answers."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store, SweepRefused
from reseat.watcher import BACKOFF_CAP, OFFLINE_AFTER, Watcher

EV = "reinvent2026"
WRITES = ("ReserveSessions", "CancelReservation", "AssociateFavorites", "DisassociateFavorite",
          "CreatePersonalTime", "UpdatePersonalTime", "DeletePersonalTime")


def mk(sid, abbr, band="available", time_="10:00"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": abbr, "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": "2026-12-01", "time": time_, "length": "60"}})


def old(n):
    return [mk(f"OLD{i}", f"ARC30{i}") for i in range(n)]


def new(n, start=0):
    return [mk(f"NEW{i}", f"SVS40{i}") for i in range(start, start + n)]


def seeded(n=10):
    st = Store(":memory:")
    st.apply_sweep(EV, old(n), with_abstracts=False, now=100.0)
    return st


def no_writes(fake):
    return all(fake.counts.get(op, 0) == 0 for op in WRITES)


# ---------------------------------------------------------------- shrink limit


def test_exactly_half_removed_is_normal_churn():
    st = seeded(10)
    res = st.apply_sweep(EV, old(10)[:5], with_abstracts=False)
    assert not res.baseline and len(res.removed) == 5 and len(st.all(EV)) == 5


def test_just_over_half_removed_is_refused():
    st = seeded(10)
    with pytest.raises(SweepRefused, match="drop 6 of 10"):
        st.apply_sweep(EV, old(10)[:4], with_abstracts=False)
    assert len(st.all(EV)) == 10


def test_refused_sweep_records_no_sweep_and_no_changes():
    st = seeded(10)
    for bad in ([], old(10)[:2]):
        with pytest.raises(SweepRefused):
            st.apply_sweep(EV, bad, with_abstracts=False, now=200.0)
    assert st.last_sweep(EV) == 100.0
    changes = st.db.execute("SELECT COUNT(*) FROM catalog_changes WHERE kind='removed'").fetchone()[0]
    assert changes == 0


def test_force_on_a_partial_sweep_applies_it_as_a_baseline():
    st = seeded(10)
    res = st.apply_sweep(EV, old(10)[:2] + [mk("NEW0", "SVS400", band="limited")],
                         with_abstracts=False, force=True)
    assert res.baseline and res.opened == [] and len(st.all(EV)) == 3


# ---------------------------------------------------------------- tiny catalogs


def test_one_session_catalog_empty_is_refused():
    st = seeded(1)
    with pytest.raises(SweepRefused, match="empty catalog"):
        st.apply_sweep(EV, [], with_abstracts=False)


def test_one_session_catalog_replaced_is_a_baseline():
    st = seeded(1)
    res = st.apply_sweep(EV, new(1), with_abstracts=False)
    assert res.baseline and res.removed == ["OLD0"] and res.opened == []


def test_two_session_catalog_one_kept_one_new_is_churn():
    st = seeded(2)
    res = st.apply_sweep(EV, old(2)[:1] + new(1), with_abstracts=False)
    assert not res.baseline and res.added == ["NEW0"] and res.opened == ["NEW0"]


def test_two_session_catalog_one_kept_none_new_is_churn():
    st = seeded(2)
    res = st.apply_sweep(EV, old(2)[:1], with_abstracts=False)
    assert not res.baseline and res.removed == ["OLD1"]


def test_three_session_catalog_one_kept_none_new_is_refused():
    st = seeded(3)
    with pytest.raises(SweepRefused):
        st.apply_sweep(EV, old(3)[:1], with_abstracts=False)


# ---------------------------------------------------------------- partial re-key


def test_rekey_with_survivors_is_a_baseline():
    st = seeded(10)
    res = st.apply_sweep(EV, old(10)[:4] + new(6), with_abstracts=False)  # 6 lost, 6 of 10 new
    assert res.baseline and len(res.removed) == 6 and res.opened == []
    assert {s.session_id for s in st.all(EV)} == {f"OLD{i}" for i in range(4)} | {f"NEW{i}" for i in range(6)}


def test_rekey_with_exactly_half_new_is_refused():
    st = seeded(10)
    with pytest.raises(SweepRefused, match="partial answer"):
        st.apply_sweep(EV, old(10)[:4] + new(4), with_abstracts=False)    # 6 lost, 4 of 8 new


def test_half_replaced_is_churn_not_a_baseline():
    st = seeded(10)
    res = st.apply_sweep(EV, old(10)[:5] + new(5), with_abstracts=False)   # 5 lost: at the limit
    assert not res.baseline and len(res.added) == 5 and len(res.opened) == 5


def test_rekey_keeps_an_opening_on_a_surviving_session():
    st = Store(":memory:")
    st.apply_sweep(EV, [mk("OLD0", "ARC300", band="unavailable")] + old(10)[1:], with_abstracts=False)
    res = st.apply_sweep(EV, [mk("OLD0", "ARC300", band="limited")] + new(9), with_abstracts=False)
    assert res.baseline and "OLD0" in res.opened


# ---------------------------------------------------------------- emptied store


def test_store_emptied_by_an_earlier_wipe_takes_the_next_sweep_as_a_baseline():
    st = seeded(10)
    st.apply_sweep(EV, [], with_abstracts=False, force=True)               # the 8 October state
    assert st.last_sweep(EV) is not None and st.all(EV) == []
    res = st.apply_sweep(EV, new(10), with_abstracts=False)
    assert res.baseline and res.removed == [] and res.opened == []


def test_empty_sweep_into_an_empty_store_is_refused():
    # A fresh install, or a store emptied earlier, must still hear that the API served nothing.
    st = Store(":memory:")
    with pytest.raises(SweepRefused, match="empty catalog"):
        st.apply_sweep(EV, [], with_abstracts=False)


def test_store_guards_are_per_event():
    st = seeded(10)
    res = st.apply_sweep("other2026", [mk("Z1", "ZZZ101")], with_abstracts=False)
    assert res.baseline and res.reason == "fresh" and len(st.all(EV)) == 10


# ---------------------------------------------------------------- watcher


@pytest.fixture
def wenv(clock):
    fake = FakeEventsApi(sessions=[mk("A1", "ARC301-R", band="unavailable")] + old(9)[1:])
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    w = Watcher(client, store, R.parse("targets:\n- code: ARC301\n"), EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    w.tick()
    clock.sleep(60)
    return fake, store, w, events


def kinds(events, *want):
    return [e.kind for e in events if e.kind in want]


def test_watcher_refusal_goes_offline_after_ten_minutes_and_back_on_recovery(wenv, clock):
    fake, store, w, events = wenv
    saved = dict(fake.sessions)
    fake.sessions = {}
    start = clock()
    while clock() - start < OFFLINE_AFTER:
        res = w.tick()
        assert res.failure == "catalog"
        delay = w.next_delay()
        assert delay <= BACKOFF_CAP
        clock.sleep(delay)
    w.tick()
    assert kinds(events, "outage", "offline") == ["outage", "offline"]
    assert len(store.all(EV)) == 9 and no_writes(fake)
    fake.sessions = saved
    fake.set_band("A1", "limited")                                         # opened while down
    clock.sleep(w.next_delay())
    res = w.tick()
    assert res.failure is None and res.booked == ["A1"]
    assert kinds(events, "back") == ["back"] and w.next_delay() == w.interval
    ops = [r["op"] for r in store.journal_entries(EV)]
    assert "watcher.outage" in ops


def test_watcher_backs_off_on_repeated_refusal(wenv, clock):
    fake, _, w, _ = wenv
    fake.sessions = {}
    delays = []
    for _ in range(4):
        w.tick()
        delays.append(w.next_delay())
        clock.sleep(delays[-1])
    assert delays == sorted(delays) and delays[-1] > delays[0]


def test_watcher_treats_a_partial_catalog_as_an_outage(wenv, clock):
    fake, store, w, events = wenv
    fake.sessions = {"A1": fake.sessions["A1"]}
    res = w.tick()
    assert res.failure == "catalog" and "partial answer" in res.error
    assert len(store.all(EV)) == 9 and no_writes(fake)


def test_watcher_on_an_emptied_store_books_nothing_and_warns_nothing(clock):
    fake = FakeEventsApi(sessions=old(5) + [mk("A1", "ARC301-R", band="available")])
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = seeded(10)
    store.apply_sweep(EV, [], with_abstracts=False, force=True)
    w = Watcher(client, store, R.parse("targets:\n- code: ARC301\n"), EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    res = w.tick()
    assert res.failure is None and res.booked == [] and no_writes(fake)
    assert not any("re-keyed" in e.data.get("message", "") for e in events)


def test_watcher_rekey_warning_is_emitted_once(wenv, clock):
    fake, _, w, events = wenv
    fake.sessions = {s.session_id: s for s in new(9)}
    w.tick()
    clock.sleep(60)
    w.tick()
    warned = [e for e in events if "re-keyed" in e.data.get("message", "")]
    assert len(warned) == 1 and no_writes(fake)


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cenv(fake, client, tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "reseat.db")
    store.apply_sweep(EV, old(10), with_abstracts=False)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n")
    return fake, store


def flat(out):
    return " ".join(out.split())


def test_cli_sync_force_applies_an_empty_catalog(cenv):
    fake, store = cenv
    fake.sessions = {}
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts", "--force"])
    assert r.exit_code == 0 and store.all(EV) == []


def test_cli_sync_refuses_a_partial_catalog(cenv):
    fake, store = cenv
    fake.sessions = {s.session_id: s for s in old(10)[:2]}
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts"])
    assert r.exit_code == 4 and "partial answer" in flat(r.output) and len(store.all(EV)) == 10


def test_cli_sync_reports_a_rekey(cenv):
    fake, store = cenv
    fake.sessions = {s.session_id: s for s in new(10)}
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts"])
    assert r.exit_code == 0 and "re-keyed" in flat(r.output) and "newly open 0" in flat(r.output)


def test_cli_book_refuses_a_partial_catalog(cenv):
    fake, store = cenv
    fake.sessions = {s.session_id: s for s in old(10)[:2]}
    r = CliRunner().invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 4 and "Not booking" in flat(r.output)
    assert no_writes(fake) and len(store.all(EV)) == 10


def test_cli_sync_help_lists_force():
    r = CliRunner().invoke(cli.app, ["sync", "--help"])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", r.output)   # CI terminals add colour codes
    assert "--force" in plain


# ---------------------------------------------------------------- 1 October catalog


def test_first_sweep_of_the_1_october_catalog_stores_no_band():
    """A first sweep now clears band_changes, so check the stored bands directly."""
    fixture = Path(__file__).parent / "fixtures" / "catalog-2026-10-01.json"
    fake = FakeEventsApi.from_fixture(fixture)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(),
                          transport=fake.transport())
    st = Store(":memory:")
    res = st.apply_sweep(EV, list(client.iter_sessions(EV, include_abstracts=False)), with_abstracts=False)
    assert res.baseline
    assert not any(s.seat_availability for s in st.all(EV))
    assert st.db.execute("SELECT COUNT(*) FROM band_history").fetchone()[0] == 0
