from reseat.fakeapi import sample_sessions
from reseat.models import Session


def _sync(store, client, fake, with_abstracts=True, now=None):
    sessions = list(client.iter_sessions("reinvent2026", include_abstracts=with_abstracts))
    return store.apply_sweep("reinvent2026", sessions, with_abstracts, now=now)


def test_first_sweep_adds_everything(store, client, fake):
    res = _sync(store, client, fake, now=1.0)
    assert len(res.added) == 6 and res.removed == []
    assert store.get("reinvent2026", "S-ARC1").abstract == "Abstract for ARC301-R1."


def test_second_sweep_keeps_abstract_when_omitted(store, client, fake):
    _sync(store, client, fake, now=1.0)
    _sync(store, client, fake, with_abstracts=False, now=2.0)
    assert store.get("reinvent2026", "S-ARC1").abstract == "Abstract for ARC301-R1."


def test_band_change_is_recorded_and_opened_detected(store, client, fake):
    _sync(store, client, fake, now=1.0)
    fake.set_band("S-ARC1", "limited")
    res = _sync(store, client, fake, with_abstracts=False, now=2.0)
    assert res.opened == ["S-ARC1"]
    hist = store.band_history("reinvent2026", "S-ARC1")
    assert [(h[1], h[2]) for h in hist] == [(None, "unavailable"), ("unavailable", "limited")]


def test_new_repeat_is_detected(store, client, fake):
    _sync(store, client, fake, now=1.0)
    fake.add_session(Session.model_validate({
        "sessionId": "S-ARC3", "abbreviation": "ARC301-R3", "title": "Resilient architectures",
        "type": "Breakout session", "venue": "Wynn", "isReservable": True,
        "seatAvailability": "available",
        "sessionTime": {"date": "2026-12-03", "time": "09:00", "length": "60"}}))
    res = _sync(store, client, fake, with_abstracts=False, now=2.0)
    assert res.added == ["S-ARC3"]
    repeats = store.by_base_code("reinvent2026", "ARC301")
    assert [s.abbreviation for s in repeats] == ["ARC301-R1", "ARC301-R2", "ARC301-R3"]


def test_moved_session_is_recorded(store, client, fake):
    _sync(store, client, fake, now=1.0)
    s = fake.sessions["S-SEC1"]
    fake.sessions["S-SEC1"] = s.model_copy(update={"room": "Caesars Palace, Room 9"})
    res = _sync(store, client, fake, with_abstracts=False, now=2.0)
    assert res.moved == ["S-SEC1"]
    kinds = [r["kind"] for r in store.recent_changes("reinvent2026", since=1.5)]
    assert kinds == ["moved"]


def test_removed_session(store, client, fake):
    _sync(store, client, fake, now=1.0)
    del fake.sessions["S-KEY1"]
    res = _sync(store, client, fake, with_abstracts=False, now=2.0)
    assert res.removed == ["S-KEY1"]
    assert store.get("reinvent2026", "S-KEY1") is None


def test_search_and_journal(store, client, fake):
    _sync(store, client, fake)
    assert [s.abbreviation for s in store.search("reinvent2026", "Serverless")] == ["SVS401"]
    store.journal("reinvent2026", "ReserveSessions", {"ids": ["S-SVS1"]}, {"ok": True}, "success")
    assert store.journal_entries("reinvent2026")[0]["op"] == "ReserveSessions"


def test_sample_fixture_has_every_band():
    bands = {s.seat_availability for s in sample_sessions()}
    assert bands == {"available", "limited", "veryLimited", "unavailable", "walkUp"}


def test_base_code_strips_r_suffixes():
    def mk(abbr):
        return Session.model_validate({"sessionId": abbr, "title": "t", "abbreviation": abbr})
    assert mk("COP324-R").base_code == "COP324"
    assert mk("COP324-R1").base_code == "COP324"
    assert mk("COP324-R12").base_code == "COP324"
    assert mk("COP324").base_code == "COP324"
    assert mk("COP324-R").is_repeat is False
    assert mk("COP324-R1").is_repeat is True
