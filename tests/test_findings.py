import json

from typer.testing import CliRunner

from reseat import cli, config, findings, pages
from reseat.models import Session

EV = "reinvent2026"
DAY = 86400.0
T0 = 1_796_000_000.0          # 2026-11-30, a fixed start


def _sweep(store, client, now):
    return store.apply_sweep(EV, list(client.iter_sessions(EV, include_abstracts=False)), False, now=now)


def _new(sid, typ="Chalk talk"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": f"NEW{sid[-1]}01", "title": "Added later", "type": typ,
        "venue": "Wynn", "isReservable": True, "seatAvailability": "unavailable",
        "sessionTime": {"date": "2026-12-03", "time": "09:00", "length": "60"}})


def test_baseline_is_left_out_and_openings_are_counted_by_type(store, client, fake):
    _sweep(store, client, T0)                        # a baseline: 6 adds, none of them news
    fake.set_band("S-ARC1", "veryLimited")           # full -> very limited: an opening
    _sweep(store, client, T0 + 60)
    fake.set_band("S-ARC1", "unavailable")           # full again after 5 minutes
    _sweep(store, client, T0 + 360)
    fake.set_band("S-ARC1", "available")             # opens again, stays open
    _sweep(store, client, T0 + DAY)
    f = findings.compute(store, EV, T0 + DAY, targets={"S-ARC1"})
    o = f["openings"]
    assert (o["total"], o["sessions"], o["on_targets"]) == (2, 1, 1)
    assert o["by_band"] == {"veryLimited": 1, "available": 1}
    assert o["by_type"] == [["Breakout session", 2, 1]]
    assert o["refilled"]["under 10 min"] == 1 and o["refilled"]["still open"] == 1
    assert o["fastest_min"] == 5.0
    assert f["catalog"]["added"] == 0 and f["sweeps"] == 4 and f["days"] == 2
    assert [n for _, n in o["per_day"]] == [1, 1] and o["days_with"] == 2


def test_a_change_into_or_within_open_bands_is_not_an_opening(store, client, fake):
    _sweep(store, client, T0)
    fake.set_band("S-ARC2", "available")             # limited -> available: not from full
    fake.set_band("S-SVS1", "unavailable")           # filled: not an opening
    _sweep(store, client, T0 + 60)
    assert findings.compute(store, EV, T0 + 60)["openings"]["total"] == 0


def test_sessions_added_after_the_baseline_are_counted_and_removed_ones_say_so(store, client, fake):
    _sweep(store, client, T0)
    fake.add_session(_new("S-NEW1"))
    fake.add_session(_new("S-NEW2", "Workshop"))
    _sweep(store, client, T0 + 60)
    del fake.sessions["S-NEW2"]
    _sweep(store, client, T0 + DAY)
    c = findings.compute(store, EV, T0 + DAY)["catalog"]
    assert (c["added"], c["still_listed"], c["removed"]) == (2, 1, 1)
    assert c["added_by_type"] == [("Chalk talk", 1)]
    assert [n for _, n in c["added_per_day"]] == [2, 0]


def test_outages_and_writes_come_from_the_journal_and_probes_count_apart(store, client, fake):
    _sweep(store, client, T0)
    store.clock = lambda: T0 + 100
    store.journal(EV, "watcher.outage", None, None, "start")
    store.journal(EV, "watcher.outage", None, {"minutes": 12, "ticks": 4}, "end")
    store.journal(EV, "ReserveSessions", ["S-SVS1"], {}, "success")
    store.journal(EV, "CancelReservation", "S-KEY1", {"status": 400, "probe": True}, "probe-open")
    store.journal(EV, "watcher.outage", None, {"minutes": "x"}, "end")      # unreadable: counted, 0 min
    f = findings.compute(store, EV, T0 + 200)
    assert f["outages"] == {"count": 2, "minutes": 12, "longest_min": 12}
    assert f["writes"]["ReserveSessions"] == 1 and f["writes"]["CancelReservation"] == 0
    assert f["writes"]["probe"] == 1


def test_empty_store_gives_zeros_not_an_error(store):
    f = findings.compute(store, EV, T0)
    assert f["sweeps"] == 0 and f["openings"]["total"] == 0 and f["openings"]["fastest_min"] is None
    assert "No session" not in pages.findings_body(f)


def test_findings_hold_no_session_ids_codes_titles_or_rooms(store, client, fake):
    _sweep(store, client, T0)
    fake.set_band("S-ARC1", "limited")
    fake.add_session(_new("S-NEW1"))
    _sweep(store, client, T0 + 60)
    f = findings.compute(store, EV, T0 + 60, targets={"S-ARC1"})
    text = json.dumps(f) + pages.findings_body(f)
    for leak in ("S-ARC1", "S-NEW1", "ARC301", "NEW101", "Resilient", "Added later", "Room 1", "Wynn"):
        assert leak not in text


def test_findings_page_escapes_type_names(store, client, fake):
    _sweep(store, client, T0)
    fake.add_session(_new("S-NEW1", typ="<script>x</script>"))
    _sweep(store, client, T0 + 60)
    html = pages.findings_body(findings.compute(store, EV, T0 + 60))
    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_cli_findings_prints_counts_and_writes_json_from_a_patched_store_only(store, client, fake,
                                                                             tmp_path, monkeypatch):
    _sweep(store, client, T0)
    fake.set_band("S-ARC1", "veryLimited")
    _sweep(store, client, T0 + 60)
    real_db = config.DB_PATH
    opened = []
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "never.db")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_store", lambda: (opened.append(1), store)[1])
    monkeypatch.setattr(cli, "_client", lambda: (_ for _ in ()).throw(AssertionError("no API call")))
    out = tmp_path / "f.json"
    r = CliRunner().invoke(cli.app, ["findings", "--json", str(out)])      # no rules file: no targets
    assert r.exit_code == 0, r.output
    assert "Seats opened on full sessions: 1 on 1 sessions" in r.output and "On targets: 0" in r.output
    assert "Breakout session: 1 openings on 1 sessions" in r.output
    f = json.loads(out.read_text(encoding="utf-8"))
    assert f["openings"]["total"] == 1 and "S-ARC1" not in out.read_text(encoding="utf-8")
    assert opened == [1] and not (tmp_path / "never.db").exists() and config.DB_PATH != real_db
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n", encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["findings"])
    assert r.exit_code == 0 and "On targets: 1" in r.output
