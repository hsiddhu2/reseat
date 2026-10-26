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
    # At 50 percent confidence the allowance is the typical walk, so these are the table minutes.
    blocks, warnings = G.leave_blocks(G.held_sessions(store, EV, ["G3", "G2", "G1"]),
                                      rules(RULES + "arrive_confidence: 0.5\n"))
    got = {b.code: (b.start, b.end, b.location) for b in blocks}
    # G1 Tue 10:00 PST = 18:00Z. Home Venetian to MGM 50 min. 10:00 - 11 - 50 = 08:59 PST.
    assert got["SEC201"] == ("2026-12-01T16:59:00", "2026-12-01T17:04:00", "MGM Grand")
    # G2 11:15 at Venetian after MGM ends 11:00. 11:15 - 11 - 50 = 10:14, before 11:00, so tight:
    # minus the 30 min buffer, 09:44 PST = 17:44Z.
    assert got["ARC301"] == ("2026-12-01T17:44:00", "2026-12-01T17:49:00", "The Venetian")
    # G3 first of Wednesday. Home Venetian to Caesars Forum 20 min. 14:00 - 31 = 13:29 PST.
    assert got["DOP302"] == ("2026-12-02T21:29:00", "2026-12-02T21:34:00", "Caesars Forum")
    assert len(warnings) == 1
    assert "walk from MGM Grand is about 50 min (estimate), the gap is 15 min" in warnings[0]


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
        "Leave now by reseat. Walk MGM Grand to The Venetian, about 50 min, 59 allowed, estimate. "
        "Set by reseat")


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
    # 15:00 - 11 - 24 allowed for the typical 20 min walk = 14:25 PST.
    assert blocks_by_code(held)["DOP302"]["startDateTime"] == "2026-12-02T22:25:00"
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
    (row,) = store.journal_ops(EV, ("CreatePersonalTime",))
    assert row["outcome"] == "error" and '"status": 503' in row["response"]   # may have landed


def test_409_stops_cleanly(held, client, store):
    held.closed = True
    res = G.sync(client, store, EV, rules())
    assert res.closed and held.schedule.personal_time == {}


def test_no_home_venue_says_when_to_be_there(held, store):
    blocks, _ = G.leave_blocks(G.held_sessions(store, EV, ["G1"]), rules("targets: []\n"))
    assert blocks[0].start == "2026-12-01T17:49:00"                   # 10:00 - 11 min
    assert "Be at MGM Grand by 09.49." in blocks[0].description


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
    assert a.verdict == verdict
    if verdict == "unknown":
        assert a.basis == "No rule for this session type."
    else:
        assert a.basis.startswith("Walk-up about ") and "Session type rule." in a.basis


def test_queue_or_go_history_moves_the_odds_gradually():
    """A Beta-binomial: one freed seat moves a workshop's odds up a little, several move it to likely,
    and fills that never free a seat move it down. No hard cutover."""
    s = mk("Q", "Q1", "2026-12-01", "10:00", "Wynn", typ="Workshop")
    prior = G.queue_or_go(s, [])
    once = [(1.0, None, "available"), (2.0, "available", "unavailable"), (3.0, "unavailable", "limited")]
    one = G.queue_or_go(s, once)
    assert prior.odds.p < one.odds.p and one.verdict == "unlikely"
    many = [(0.0, None, "limited")] + [
        (float(i), "limited" if i % 2 else "unavailable", "unavailable" if i % 2 else "limited")
        for i in range(1, 13)]
    assert G.queue_or_go(s, many).verdict == "likely"
    stuck = [(1.0, None, "available"), (2.0, "available", "unavailable")]
    low = G.queue_or_go(s, stuck)
    assert low.odds.p < prior.odds.p and "fill 1 time and free a seat 0 times" in low.basis
    for a in (prior, one, low):
        assert 0 <= a.odds.lo <= a.odds.p <= a.odds.hi <= 1


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


def test_longest_venue_pair_fits_the_spec_limit_and_says_estimate(store):
    """PersonalTimeInput.description is 1 to 250 characters. The walk is never an official figure."""
    import itertools
    import json
    from pathlib import Path

    from reseat import campus
    spec = json.loads((Path(__file__).parents[1] / "docs" / "openapi.json").read_text())
    limit = spec["components"]["schemas"]["PersonalTimeInput"]["properties"]["description"]["maxLength"]
    pairs = [(a, b) for a, b in itertools.permutations(campus.VENUES, 2)
             if campus.walk_minutes(a, b) is not None]
    origin, dest = max(pairs, key=lambda p: len(p[0]) + len(p[1]))
    s = mk("L2", "LNG200", "2026-12-01", "10:00", dest)
    blocks, _ = G.leave_blocks([s], rules(f"home_venue: {origin}\ntargets: []\n"))
    d = blocks[0].description
    assert len(d) <= limit and ", estimate." in d and d.endswith(G.TAG)


def test_old_wording_is_updated_in_place_once(held, client, store):
    G.sync(client, store, EV, rules())
    for p in held.schedule.personal_time.values():
        p["description"] = p["description"].replace(", estimate", "")
    res = G.sync(client, store, EV, rules())
    assert len(res.plan.update) == 3 and not res.plan.create and not res.plan.delete
    assert G.sync(client, store, EV, rules()).plan.empty


# ---- QA: arrive_confidence, the default leave time, and the queue-or-go text on every surface


def test_arrive_confidence_is_bounded_and_defaults_to_80_percent():
    assert R.parse("targets: []\n").arrive_confidence == 0.8
    for bad in ("0.49", "0.995", "1", "0", "-0.5"):
        with pytest.raises(R.RulesError):
            R.parse(f"arrive_confidence: {bad}\n")
    assert R.parse("arrive_confidence: 0.5\n").arrive_confidence == 0.5
    assert R.parse("arrive_confidence: 0.99\n").arrive_confidence == 0.99


def test_half_confidence_allows_exactly_the_table_minutes(held, store):
    blocks, _ = G.leave_blocks(G.held_sessions(store, EV, ["G1"]), rules(RULES + "arrive_confidence: 0.5\n"))
    assert (blocks[0].walk, blocks[0].allow) == (50, 50)


def test_default_leave_time_for_a_tight_change_takes_the_allowance_and_the_buffer(held, store):
    blocks, warnings = G.leave_blocks(G.held_sessions(store, EV, ["G1", "G2"]), rules())
    got = {b.code: b for b in blocks}
    # G1: 10:00 - 11 - 59 allowed for the 50 min walk = 08:50 PST = 16:50Z.
    assert got["SEC201"].start == "2026-12-01T16:50:00" and got["SEC201"].allow == 59
    # G2: 11:15 - 11 - 59 = 10:05, before G1 ends at 11:00, so tight: minus 30 = 09:35 PST = 17:35Z.
    assert got["ARC301"].start == "2026-12-01T17:35:00" and got["ARC301"].allow == 59
    assert len(warnings) == 1


def test_longest_walk_at_the_highest_confidence_keeps_the_whole_description(store):
    far = [mk("F1", "LNG101", "2026-12-01", "10:00", "MGM Grand"),
           mk("F2", "LNG102", "2026-12-01", "11:00", "Encore")]
    store.apply_sweep(EV, far, with_abstracts=False)
    body = "home_venue: Encore\nbuffer_minutes: 30\narrive_confidence: 0.99\ntargets: []\n"
    blocks, _ = G.leave_blocks(G.held_sessions(store, EV, ["F1", "F2"]), rules(body))
    for b in blocks:
        assert b.walk == 60 and b.allow == 94
        assert len(b.description) <= 250 and b.description.endswith(G.TAG)
        assert "about 60 min, 94 allowed, estimate." in b.description


def test_queue_or_go_push_text_shows_the_odds_and_basis():
    from reseat import push
    from reseat.watcher import WatchEvent
    s = mk("Q", "Q1", "2026-12-01", "10:00", "Wynn", typ="Chalk talk")
    a = G.queue_or_go(s, [(0.0, None, "limited"), (1.0, "limited", "unavailable"),
                          (2.0, "unavailable", "available"), (3.0, "available", "unavailable")])
    ev = WatchEvent("wanted_soon", 0, {"code": "Q1", "title": "T", "at": "10:00", "venue": "Wynn",
                                       "verdict": a.verdict, "basis": a.basis})
    _, body = push.message(ev, str)
    assert "Go early. Walk-up about 47% (" in body and "Session type rule." in body
    assert "fill 2 times and free a seat 1 time." in body


def test_queue_or_go_text_is_escaped_on_the_today_page():
    from reseat import demo, pages
    d = demo.Demo(port=0)
    d.watcher.tick()
    d.app.refresh()
    snap = d.app.snapshot()
    text = "Session at Wynn. Go early. Basis: Walk-up about 47% (23 to 71%). <script>x</script>"
    snap["view"]["today"]["wanted"] = [{"code": "<b>X</b>", "time": "10:00", "band": "available",
                                        "open": True, "clash": "", "text": text}]
    html = pages.render_today(snap)
    assert "<script>x" not in html and "<b>X</b>" not in html
    assert "Basis: Walk-up about 47% (23 to 71%)." in html


def test_mcp_queue_or_go_gives_odds_per_sitting(fake, client, store, clock):
    from reseat import mcp_server as M
    from reseat.fakeapi import sample_sessions
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    tools = M.Tools(client, store, R.parse("targets: []\n"), EV, clock=clock)
    out = tools.queue_or_go("ARC301").splitlines()
    assert len(out) == 2
    for line in out:
        assert line.startswith(("ARC301-R1 ", "ARC301-R2 "))
        assert re.search(r": (likely|early)\. Walk-up about \d+% \(\d+ to \d+%\)\. Session type rule\.", line)
        assert "S-ARC" not in line
    assert re.search(r"SVS401 .*: \w+\. Walk-up about \d+% \(", tools.queue_or_go("SVS401"))


# ---- Live 25 October 2026: CreatePersonalTime refused a description holding : ( ) [ ]


def test_descriptions_never_hold_characters_the_live_api_refuses(held, client, store):
    from reseat.fakeapi import PT_REFUSED_CHARS
    res = G.sync(client, store, EV, rules())
    assert len(res.done) == 3 and not res.problems
    for p in held.schedule.personal_time.values():
        assert not any(ch in p["description"] for ch in PT_REFUSED_CHARS)
    assert G._safe("Be at Wynn/Encore by 08:19 (estimate) [x]") == "Be at Wynn Encore by 08.19 estimate x"


def test_blocks_in_the_old_tagged_form_are_still_ours_and_rewritten(held, client, store):
    G.sync(client, store, EV, rules())
    for p in held.schedule.personal_time.values():
        p["description"] = "re:Seat leave-now. Walk. [reseat]"
    res = G.sync(client, store, EV, rules())
    assert len(res.plan.update) == 3 and not res.plan.create and not res.plan.delete and not res.problems
    assert all(p["description"].endswith(G.TAG) for p in held.schedule.personal_time.values())
    assert G.sync(client, store, EV, rules()).plan.empty


def test_each_refused_create_is_journaled_once_and_the_next_block_is_tried(held, client, store, monkeypatch):
    monkeypatch.setattr(G, "_safe", lambda t: t + " (estimate)")
    res = G.sync(client, store, EV, rules())
    assert not res.done and sum("could not be applied" in x for x in res.problems) == 3
    rows = store.journal_ops(EV, ("CreatePersonalTime",))
    assert len(rows) == 3 and all(r["outcome"] == "refused" for r in rows)
    assert len({r["request"] for r in rows}) == 3      # three different blocks, none sent twice
    assert store.journal_entries(EV)[0]["outcome"] == "readback-disagreement"   # one read-back at the end
    assert "(estimate)" in rows[0]["request"] and '"status": 400' in rows[0]["response"]
    assert not held.schedule.personal_time



def _rows(store, op):
    return [(r["outcome"], r["response"]) for r in store.journal_ops(EV, (op,))]


def test_409_on_create_is_journaled_refused_and_read_back(held, client, store):
    held.closed = True
    res = G.sync(client, store, EV, rules())
    assert res.closed and not res.done
    assert held.counts["CreatePersonalTime"] == 1                    # stopped at the first refusal
    assert [o for o, _ in _rows(store, "CreatePersonalTime")] == ["refused"]
    assert "409" in _rows(store, "CreatePersonalTime")[0][1]
    assert [o for o, _ in _rows(store, "GetSchedule")] == ["readback-disagreement"]


def test_refusal_mid_batch_keeps_the_creates_that_landed(held, client, store, monkeypatch):
    """Two creates land, the third is refused with 400. Nothing retried. Read-back finds one missing."""
    safe = G._safe
    monkeypatch.setattr(G, "_safe", lambda t: safe(t) + (" (x)" if "Caesars Forum" in t else ""))
    res = G.sync(client, store, EV, rules())
    assert len(res.done) == 2 and len(held.schedule.personal_time) == 2
    assert held.counts["CreatePersonalTime"] == 3
    assert [o for o, _ in _rows(store, "CreatePersonalTime")] == ["sent", "sent", "refused"]
    assert "could not be applied" in _rows(store, "CreatePersonalTime")[2][1]
    assert any("1 missing" in p for p in res.problems)
    assert [o for o, _ in _rows(store, "GetSchedule")] == ["readback-disagreement"]
    monkeypatch.setattr(G, "_safe", safe)                            # fixed: next run creates the one
    res = G.sync(client, store, EV, rules())
    assert len(res.plan.create) == 1 and not res.problems and held.counts["CreatePersonalTime"] == 4


def test_refused_update_keeps_the_old_entry_and_is_journaled(held, client, store, monkeypatch):
    G.sync(client, store, EV, rules())
    for p in held.schedule.personal_time.values():
        p["description"] = "re:Seat leave-now. Walk. [reseat]"
    before = {k: dict(v) for k, v in held.schedule.personal_time.items()}
    safe = G._safe
    monkeypatch.setattr(G, "_safe", lambda t: safe(t) + " (x)")
    res = G.sync(client, store, EV, rules())
    assert held.counts["UpdatePersonalTime"] == 1 and not res.done
    assert held.schedule.personal_time == before                     # nothing lost, nothing deleted
    assert [o for o, _ in _rows(store, "UpdatePersonalTime")] == ["refused"]
    assert any("3 stale" in p for p in res.problems)


def test_409_on_update_stops_and_is_journaled(held, client, store):
    G.sync(client, store, EV, rules())
    for p in held.schedule.personal_time.values():
        p["description"] = "re:Seat leave-now. Walk. [reseat]"
    held.closed = True
    res = G.sync(client, store, EV, rules())
    assert res.closed and held.counts["UpdatePersonalTime"] == 1
    assert [o for o, _ in _rows(store, "UpdatePersonalTime")] == ["refused"]
    assert _rows(store, "GetSchedule")[-1][0] == "readback-disagreement"


def test_refused_delete_stops_before_updates_and_creates(held, client, store):
    G.sync(client, store, EV, rules())
    held.schedule.reserved.discard("G3")
    held.fail_next("DeletePersonalTime", 400)
    res = G.sync(client, store, EV, rules())
    assert not res.done and "DOP302" in blocks_by_code(held)
    assert [o for o, _ in _rows(store, "DeletePersonalTime")] == ["refused"]
    assert any("1 extra" in p for p in res.problems)
    assert held.counts.get("UpdatePersonalTime", 0) == 0


def test_409_on_delete_stops_and_is_journaled(held, client, store):
    G.sync(client, store, EV, rules())
    held.schedule.reserved.discard("G3")
    held.closed = True
    res = G.sync(client, store, EV, rules())
    assert res.closed and "DOP302" in blocks_by_code(held)
    assert [o for o, _ in _rows(store, "DeletePersonalTime")] == ["refused"]
    assert _rows(store, "GetSchedule")[-1][0] == "readback-disagreement"


def test_every_successful_write_is_followed_by_one_read_back(held, client, store):
    G.sync(client, store, EV, rules())
    held.schedule.reserved.discard("G3")
    G.sync(client, store, EV, rules())
    assert [o for o, _ in _rows(store, "GetSchedule")] == ["readback-ok", "readback-ok"]
    assert [o for o, _ in _rows(store, "CreatePersonalTime")] == ["sent"] * 3
    assert [o for o, _ in _rows(store, "DeletePersonalTime")] == ["sent"]


def test_a_new_entry_in_the_old_form_with_a_hand_written_middle_is_still_ours(held, client, store):
    """The old form is recognised only with both the old prefix and the old tag."""
    G.sync(client, store, EV, rules())
    held.schedule.personal_time["pt-x"] = {
        "personalTimeId": "pt-x", "startDateTime": "2026-12-03T20:00:00",
        "endDateTime": "2026-12-03T20:05:00", "title": "Leave for OLD1",
        "description": "re:Seat leave-now. Walk. [reseat]"}
    held.schedule.personal_time["pt-y"] = {
        "personalTimeId": "pt-y", "startDateTime": "2026-12-03T20:00:00",
        "endDateTime": "2026-12-03T20:05:00", "title": "Leave for OLD2",
        "description": "re:Seat leave-now. Walk. Set by reseat"}
    G.sync(client, store, EV, rules())
    assert "pt-x" not in held.schedule.personal_time                 # ours, no held session, deleted
    assert "pt-y" in held.schedule.personal_time                     # mixed form, not ours, untouched
