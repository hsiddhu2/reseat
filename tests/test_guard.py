import re
from datetime import datetime

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import guard as G
from reseat import rules as R
from reseat.models import Session

EV = "reinvent2026"
PT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00$")


def mk(sid, abbr, date, time_, venue, mins=60, typ="Breakout session"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": typ,
        "venue": venue, "room": "Level 1 | Room 1", "isReservable": True, "seatAvailability": "available",
        "sessionTime": {"date": date, "time": time_, "length": str(mins)},
    })


CATALOG = [
    mk("G1", "SEC201", "2026-12-01", "10:00", "MGM Grand"),
    mk("G2", "ARC301", "2026-12-01", "11:15", "Venetian"),
    mk("G3", "DOP302", "2026-12-02", "14:00", "Caesars Forum"),
]
RULES = "home_venue: The Venetian\nbuffer_minutes: 30\ntargets:\n- code: SEC201\n"


@pytest.fixture
def held(fake, store):
    for s in CATALOG:
        fake.add_session(s)
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)
    fake.schedule.reserved.update({"G1", "G2", "G3"})
    return fake


def rules(body=RULES):
    return R.parse(body)


def blocks_by_code(fake):
    return {p["title"].removeprefix("Leave for "): p for p in fake.schedule.personal_time.values()}


def test_leave_times_walk_cutoff_and_buffer(held, store):
    blocks, warnings = G.leave_blocks(G.held_sessions(store, EV, ["G3", "G2", "G1"]), rules())
    got = {b.code: (b.start, b.end, b.location) for b in blocks}
    # G1 Tue 10:00 PST = 18:00Z. Home Venetian to MGM 50 min. 10:00 - 11 - 50 = 08:59 PST.
    assert got["SEC201"] == ("2026-12-01T16:59:00", "2026-12-01T17:04:00", "MGM Grand")
    # G2 11:15 at Venetian after MGM ends 11:00. 11:15 - 11 - 50 = 10:14, before 11:00, so tight:
    # minus the 30 min buffer, 09:44 PST = 17:44Z.
    assert got["ARC301"] == ("2026-12-01T17:44:00", "2026-12-01T17:49:00", "The Venetian")
    # G3 first of Wednesday. Home Venetian to Caesars Forum 20 min. 14:00 - 31 = 13:29 PST.
    assert got["DOP302"] == ("2026-12-02T21:29:00", "2026-12-02T21:34:00", "Caesars Forum")
    assert len(warnings) == 1 and "walk from MGM Grand is 50 min, the gap is 15 min" in warnings[0]


def test_blocks_are_utc_seconds_zero_five_minutes_and_tagged(held, client, store):
    res = G.sync(client, store, EV, rules())
    assert len(res.done) == 3 and not res.problems
    for p in held.schedule.personal_time.values():
        assert PT.match(p["startDateTime"]) and PT.match(p["endDateTime"])
        a = datetime.fromisoformat(p["startDateTime"])
        b = datetime.fromisoformat(p["endDateTime"])
        assert (b - a).total_seconds() == 300
        assert p["description"].endswith(G.TAG) and len(p["description"]) <= 250
    assert blocks_by_code(held)["ARC301"]["description"] == (
        "re:Seat leave-now. Walk MGM Grand to The Venetian, 50 min. [reseat]")


def test_sync_is_idempotent(held, client, store):
    G.sync(client, store, EV, rules())
    before = held.counts.get("CreatePersonalTime", 0)
    res = G.sync(client, store, EV, rules())
    assert res.plan.empty and len(res.plan.keep) == 3 and res.done == []
    assert held.counts.get("CreatePersonalTime", 0) == before
    assert held.counts.get("UpdatePersonalTime", 0) == 0


def test_update_on_move_keeps_the_entry(held, client, store):
    G.sync(client, store, EV, rules())
    ids = {k: v["personalTimeId"] for k, v in blocks_by_code(held).items()}
    held.sessions["G3"] = held.sessions["G3"].model_copy(update={"session_time": mk(
        "G3", "DOP302", "2026-12-02", "15:00", "Caesars Forum").session_time})
    store.apply_sweep(EV, list(held.sessions.values()), with_abstracts=False)
    res = G.sync(client, store, EV, rules())
    assert [b.code for _, b in res.plan.update] == ["DOP302"] and not res.plan.create
    assert blocks_by_code(held)["DOP302"]["startDateTime"] == "2026-12-02T22:29:00"
    assert blocks_by_code(held)["DOP302"]["personalTimeId"] == ids["DOP302"]


def test_delete_on_cancel(held, client, store):
    G.sync(client, store, EV, rules())
    held.schedule.reserved.discard("G3")
    res = G.sync(client, store, EV, rules())
    assert [p.title for p in res.plan.delete] == ["Leave for DOP302"]
    assert "DOP302" not in blocks_by_code(held)


def test_never_touches_a_block_without_the_tag(held, client, store):
    mine = {"personalTimeId": "pt-user1", "startDateTime": "2026-12-01T20:00:00",
            "endDateTime": "2026-12-01T21:00:00", "title": "Leave for SEC201",
            "description": "My own reminder", "location": "MGM Grand"}
    lunch = {"personalTimeId": "pt-user2", "startDateTime": "2026-12-01T20:00:00",
             "endDateTime": "2026-12-01T21:00:00", "title": "Lunch", "description": "with team"}
    held.schedule.personal_time.update({"pt-user1": mine, "pt-user2": lunch})
    G.sync(client, store, EV, rules())
    held.schedule.reserved.clear()
    G.sync(client, store, EV, rules())                 # deletes every tagged block
    assert held.schedule.personal_time == {"pt-user1": mine, "pt-user2": lunch}


def test_duplicate_tagged_block_is_removed(held, client, store):
    G.sync(client, store, EV, rules())
    dup = dict(blocks_by_code(held)["SEC201"], personalTimeId="pt-dup")
    held.schedule.personal_time["pt-dup"] = dup
    res = G.sync(client, store, EV, rules())
    assert len(res.plan.delete) == 1 and len(held.schedule.personal_time) == 3


def test_dry_run_sends_nothing(held, client, store):
    res = G.sync(client, store, EV, rules(), dry_run=True)
    assert len(res.plan.create) == 3 and held.counts.get("CreatePersonalTime", 0) == 0


def test_5xx_on_create_stops_and_read_back_reports_missing(held, client, store):
    held.fail_next("CreatePersonalTime", 503)
    res = G.sync(client, store, EV, rules())
    assert held.counts["CreatePersonalTime"] == 1                    # never retried
    assert any("503" in p for p in res.problems)
    assert any("3 missing" in p for p in res.problems)
    assert store.journal_entries(EV)[0]["outcome"] == "readback-disagreement"


def test_409_stops_cleanly(held, client, store):
    held.closed = True
    res = G.sync(client, store, EV, rules())
    assert res.closed and held.schedule.personal_time == {}


def test_no_home_venue_says_when_to_be_there(held, store):
    blocks, _ = G.leave_blocks(G.held_sessions(store, EV, ["G1"]), rules("targets: []\n"))
    assert blocks[0].start == "2026-12-01T17:49:00"                   # 10:00 - 11 min
    assert "Be at MGM Grand by 09:49." in blocks[0].description


def test_home_venue_must_be_on_campus():
    with pytest.raises(R.RulesError, match="not on the 2026 campus"):
        R.parse("home_venue: Mandalay Bay\n")
    assert R.parse("home_venue: venetian\n").home_venue == "The Venetian"


@pytest.mark.parametrize("typ,verdict", [
    ("Breakout session", "likely"), ("Chalk talk", "early"), ("Workshop", "unlikely"),
    ("Builders’ session", "unlikely"), ("Lab", "unlikely"), ("Exam prep", "unknown"),
])
def test_queue_or_go_type_rules(typ, verdict):
    a = G.queue_or_go(mk("Q", "Q1", "2026-12-01", "10:00", "Wynn", typ=typ), [])
    assert a.verdict == verdict and a.basis.startswith("Session type rule.")


def test_queue_or_go_history_overrides_type_after_three_changes():
    s = mk("Q", "Q1", "2026-12-01", "10:00", "Wynn", typ="Workshop")
    freed = [(1.0, None, "available"), (2.0, "available", "unavailable"), (3.0, "unavailable", "limited")]
    assert G.queue_or_go(s, freed).verdict == "likely"
    stuck = [(1.0, None, "available"), (2.0, "available", "limited"), (3.0, "limited", "unavailable")]
    assert G.queue_or_go(s, stuck).verdict == "unlikely"
    assert G.queue_or_go(s, stuck[:2]).basis.startswith("Session type rule.")


def test_cli_guard_sync(held, client, store, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    (tmp_path / "rules.yaml").write_text(RULES)
    r = CliRunner().invoke(cli.app, ["guard", "sync", "--dry-run"])
    assert r.exit_code == 0 and "would create" in r.output and "venue switch" in r.output
    assert held.counts.get("CreatePersonalTime", 0) == 0
    r = CliRunner().invoke(cli.app, ["guard", "sync"])
    assert r.exit_code == 0 and "3 to create" in r.output


def test_tag_survives_a_long_description(store):
    s = mk("L1", "LNG100", "2026-12-01", "10:00", "V" * 400)
    blocks, _ = G.leave_blocks([s], rules("targets: []\n"))
    d = blocks[0].description
    assert len(d) <= 250 and d.startswith(G.PREFIX) and d.endswith(G.TAG)


def test_hand_written_entry_mentioning_the_tag_is_not_ours(held, client, store):
    note = {"personalTimeId": "pt-note", "startDateTime": "2026-12-01T20:00:00",
            "endDateTime": "2026-12-01T20:05:00", "title": "Leave for DOP302",
            "description": "Notes about [reseat] tool"}
    held.schedule.personal_time["pt-note"] = note
    G.sync(client, store, EV, rules())
    assert held.schedule.personal_time["pt-note"] == note


def test_stale_store_never_deletes_blocks(held, client, store):
    G.sync(client, store, EV, rules())
    held.schedule.reserved.add("NOT-IN-STORE")
    held.schedule.reserved.discard("G3")       # would normally delete Leave for DOP302
    res = G.sync(client, store, EV, rules())
    assert res.plan.delete == [] and "DOP302" in blocks_by_code(held)
    assert any("not in the local catalog" in w for w in res.plan.warnings)
