import json

import pytest
from typer.testing import CliRunner

from reseat import cancel as C
from reseat import cli, config
from reseat.fakeapi import sample_sessions
from reseat.store import Store

EV = "reinvent2026"
runner = CliRunner()


# ---------------------------------------------------------------------- cancel engine


def test_cancel_reads_back_and_journals(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    chk = C.check(client, EV, "S-ARC2")
    assert chk.held and chk.band_open and chk.band == "limited"
    res = C.cancel(client, store, EV, "S-ARC2")
    assert res.status == "cancelled" and "S-ARC2" not in res.schedule.reserved
    assert fake.counts["CancelReservation"] == 1
    assert store.journal_entries(EV)[0]["outcome"] == "cancelled"


def test_cancel_not_held_is_404_and_not_retried(fake, client, store):
    res = C.cancel(client, store, EV, "S-ARC2")
    assert res.status == "not_held" and fake.counts["CancelReservation"] == 1


def test_cancel_closed_before_october_8(fake, client, store):
    fake.closed = True
    fake.schedule.reserved.add("S-ARC2")
    res = C.cancel(client, store, EV, "S-ARC2")
    assert res.status == "closed" and "8 October 2026" in res.message
    assert "S-ARC2" in fake.schedule.reserved


def test_cancel_5xx_is_read_back_not_retried(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 503)
    res = C.cancel(client, store, EV, "S-ARC2")
    assert res.status == "error" and "still lists it" in res.message
    assert fake.counts["CancelReservation"] == 1 and fake.counts["GetSchedule"] == 1


# ---------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(fake, client, tmp_path, monkeypatch):
    store = Store(tmp_path / "reseat.db")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    return fake, store, tmp_path


def write_rules(path, body):
    path.write_text(body, encoding="utf-8")


def test_book_dry_run_sends_nothing(cli_env):
    fake, _, home = cli_env
    write_rules(home / "rules.yaml", "targets:\n- code: SVS401\n- code: SEC201\n")
    r = runner.invoke(cli.app, ["book", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "SVS401" in r.output and "SEC201" in r.output
    assert fake.counts["ReserveSessions"] == 0


def test_book_closed_exits_3_with_october_message(cli_env):
    fake, store, home = cli_env
    fake.closed = True
    write_rules(home / "rules.yaml", "targets:\n- code: SVS401\n")
    r = runner.invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 3
    assert "8 October 2026" in " ".join(r.output.split())
    assert fake.counts["ReserveSessions"] == 1


def test_book_reserves_and_reports(cli_env):
    fake, _, home = cli_env
    write_rules(home / "rules.yaml", "targets:\n- code: SVS401\n")
    r = runner.invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 0, r.output
    assert fake.schedule.reserved == {"S-SVS1"}
    assert "Reserved 1 of 1 targets" in r.output


def test_book_without_rules_file_says_how_to_start(cli_env):
    r = runner.invoke(cli.app, ["book", "--dry-run"])
    assert r.exit_code == 2 and "reseat rules init" in r.output


def test_cancel_refuses_yes_when_band_not_open(cli_env):
    fake, _, _ = cli_env
    fake.schedule.reserved.add("S-ARC1")      # band unavailable in the sample
    r = runner.invoke(cli.app, ["cancel", "S-ARC1", "--yes"])
    assert r.exit_code == 1 and "Refusing --yes" in r.output
    assert fake.counts["CancelReservation"] == 0


def test_cancel_asks_first_and_respects_no(cli_env):
    fake, _, _ = cli_env
    fake.schedule.reserved.add("S-ARC2")
    r = runner.invoke(cli.app, ["cancel", "S-ARC2"], input="n\n")
    assert r.exit_code == 1 and fake.counts["CancelReservation"] == 0
    r = runner.invoke(cli.app, ["cancel", "S-ARC2"], input="y\n")
    assert r.exit_code == 0 and fake.schedule.reserved == set()


def test_probe_refuses_a_held_session(cli_env):
    fake, _, _ = cli_env
    fake.schedule.reserved.add("S-KEY1")
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 2 and fake.counts["CancelReservation"] == 0
    assert fake.schedule.reserved == {"S-KEY1"}


def test_probe_says_closed_then_open(cli_env):
    fake, _, _ = cli_env
    fake.closed = True
    assert "closed (409)" in runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"]).output
    fake.closed = False
    assert "open" in runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"]).output


def test_favorite_closed_and_read_back(cli_env):
    fake, store, _ = cli_env
    r = runner.invoke(cli.app, ["favorite", "S-SVS1"])
    assert r.exit_code == 0 and fake.schedule.favorites == {"S-SVS1"}
    assert store.journal_entries(EV)[0]["outcome"] == "readback-ok"
    fake.closed = True
    r = runner.invoke(cli.app, ["favorite", "S-SEC1"])
    assert r.exit_code == 3 and "closed (409)" in r.output


def test_rules_init_import_check(cli_env, tmp_path):
    fake, store, home = cli_env
    assert runner.invoke(cli.app, ["rules", "init"]).exit_code == 0
    assert runner.invoke(cli.app, ["rules", "init"]).exit_code == 1          # no silent overwrite
    export = tmp_path / "export.json"
    export.write_text(json.dumps([{"id": "S-SVS1", "shortId": "SVS401"},
                                  {"id": "S-ARC1", "shortId": "ARC301-R1", "repeats": [{"id": "S-ARC2"}]}]))
    r = runner.invoke(cli.app, ["rules", "import", str(export)])
    assert r.exit_code == 1 and "--force" in r.output                       # example has targets
    r = runner.invoke(cli.app, ["rules", "import", str(export), "--force"])
    assert r.exit_code == 0 and "Imported 2 targets" in r.output
    assert "meals" in (home / "rules.yaml").read_text()                      # meals kept

    r = runner.invoke(cli.app, ["rules", "check"])
    assert r.exit_code == 1 and "SVS401 not in catalog" in r.output         # empty local catalog
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    r = runner.invoke(cli.app, ["rules", "check"])
    assert r.exit_code == 0, r.output
