import json

import pytest
from typer.testing import CliRunner

from reseat import cancel as C
from reseat import cli, config
from reseat.client import AuthRequired, NetworkError
from reseat.fakeapi import sample_sessions
from reseat.store import Store

EV = "reinvent2026"
runner = CliRunner()


# ---------------------------------------------------------------------- cancel engine


def test_cancel_reads_back_and_journals(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    chk = C.check(client, EV, "S-ARC2")
    assert chk.held and chk.band_open and chk.band == "limited"
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert res.status == "cancelled" and "S-ARC2" not in res.schedule.reserved
    assert fake.counts["CancelReservation"] == 1
    assert store.journal_entries(EV)[0]["outcome"] == "cancelled"


def test_cancel_not_held_is_404_and_not_retried(fake, client, store):
    res = C.cancel(client, store, EV, "S-ARC2", was_held=False)
    assert res.status == "not_held" and fake.counts["CancelReservation"] == 1


def test_cancel_not_held_400_is_read_back_and_reported_not_held(fake, client, store):
    """Live API, 12 October 2026: 400 where the spec documents 404. Read back, never retried."""
    fake.fail_next("CancelReservation", 400)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=False)
    assert res.status == "not_held" and "Cancelled" not in res.message
    assert fake.counts["CancelReservation"] == 1 and fake.counts["GetSchedule"] == 1


def test_cancel_400_on_a_held_seat_is_an_error_not_a_cancel(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 400)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert res.status == "error" and "still lists it" in res.message
    assert fake.counts["CancelReservation"] == 1 and "S-ARC2" in fake.schedule.reserved


def test_cancel_closed_before_october_8(fake, client, store):
    fake.closed = True
    fake.schedule.reserved.add("S-ARC2")
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert res.status == "closed" and "8 October 2026" in res.message
    assert "S-ARC2" in fake.schedule.reserved


def test_cancel_5xx_is_read_back_not_retried(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 503)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
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


def test_probe_reads_a_400_as_open(cli_env):
    """Live API, 12 October 2026: a cancel on a session not held came back 400, not 404."""
    fake, _, _ = cli_env
    fake.fail_next("CancelReservation", 400)
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 0 and "Writes look open" in r.output and "Traceback" not in r.output
    assert fake.schedule.reserved == set()


@pytest.mark.parametrize("status", [403, 500, 503])
def test_probe_says_unclear_on_any_other_answer(cli_env, status):
    fake, _, _ = cli_env
    fake.fail_next("CancelReservation", status)
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 1 and f"answered {status}" in r.output and "unchanged" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert fake.schedule.reserved == set()


def test_book_dry_run_names_pinned_targets_by_code(cli_env):
    """A target pinned by session id is shown by its code, never the id."""
    fake, store, home = cli_env
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    fake.schedule.reserved.add("S-SVS1")
    write_rules(home / "rules.yaml", "targets:\n- session_id: S-SVS1\n- session_id: S-SEC1\n")
    r = runner.invoke(cli.app, ["book", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "Already held: SVS401" in r.output and "SEC201" in r.output
    assert "S-SVS1" not in r.output and "S-SEC1" not in r.output
    assert fake.counts["ReserveSessions"] == 0


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


# ---------------------------------------------------------------------- 400 on cancel, probe, pinned targets


def test_cancel_400_not_held_is_journaled_with_its_read_back(fake, client, store):
    fake.fail_next("CancelReservation", 400)
    C.cancel(client, store, EV, "S-ARC2", was_held=False)
    entry = store.journal_entries(EV)[0]
    assert entry["outcome"] == "not_held"
    blob = json.dumps({k: entry[k] for k in entry.keys()}, default=str)
    assert "400" in blob and "readBack" in blob


def test_cancel_400_with_a_failed_read_back_is_unconfirmed_not_not_held(fake, client, store):
    """A 400 alone never proves 'not held'. Only the read-back may say so."""
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 400)
    fake.fail_next("GetSchedule", 503)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert res.status == "unconfirmed" and "read-back failed" in res.message
    assert fake.counts["CancelReservation"] == 1 and "S-ARC2" in fake.schedule.reserved


def test_cli_cancel_on_a_held_seat_answered_400_exits_4(cli_env):
    fake, _, _ = cli_env
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 400)
    r = runner.invoke(cli.app, ["cancel", "S-ARC2", "--yes"])
    assert r.exit_code == 4 and "still lists it" in r.output
    assert fake.counts["CancelReservation"] == 1 and fake.schedule.reserved == {"S-ARC2"}


def test_probe_reads_a_404_as_open(cli_env):
    fake, _, _ = cli_env
    fake.fail_next("CancelReservation", 404)
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 0 and "open" in r.output and "closed" not in r.output


def test_probe_throttled_twice_is_unclear_and_waits_once(cli_env):
    """429: wait Retry-After once, then fail. The cancel is sent at most twice and nothing changes."""
    fake, _, _ = cli_env
    fake.fail_next("CancelReservation", 429, times=2)
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 1 and "answered 429" in r.output and "Traceback" not in r.output
    assert fake.counts["CancelReservation"] == 2 and fake.schedule.reserved == set()


def test_book_run_prints_pinned_targets_by_code(cli_env):
    """_print_round after a real round: no session id reaches the screen."""
    fake, store, home = cli_env
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    write_rules(home / "rules.yaml", "targets:\n- session_id: S-SEC1\n")
    r = runner.invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 0, r.output
    assert fake.schedule.reserved == {"S-SEC1"} and fake.counts["GetSchedule"] >= 1
    assert "SEC201" in r.output and "S-SEC1" not in r.output


def test_favorites_sync_dry_run_prints_pinned_targets_by_code(cli_env):
    fake, store, home = cli_env
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    write_rules(home / "rules.yaml", "targets:\n- session_id: S-SEC1\n- code: SVS401\n")
    r = runner.invoke(cli.app, ["favorites", "sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would add" in r.output and "SEC201" in r.output and "SVS401" in r.output
    assert "S-SEC1" not in r.output and "S-SVS1" not in r.output
    assert fake.counts["AssociateFavorites"] == 0


def test_target_and_who_without_a_store_never_show_an_id(store):
    assert cli._target(None, EV, "ARC301") == "ARC301"
    assert cli._target(None, EV, "1780441463028001GEdS") == "a pinned sitting"
    assert cli._target(None, EV, "") == "a pinned sitting"
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    assert cli._target(store, EV, "S-SEC1") == "SEC201"
    assert cli._who(store, EV, "S-SEC1", "S-SEC1") == "SEC201"
    assert cli._who(store, EV, "SVS401", "S-SVS1") == "SVS401"


def test_cancel_400_on_a_seat_check_saw_held_and_read_back_dropped_is_cancelled(fake, client, store):
    """check() saw it held. If the read-back no longer lists it, the seat is gone, whatever the 400 said."""
    fake.schedule.reserved.add("S-ARC2")
    chk = C.check(client, EV, "S-ARC2")
    fake.fail_next("CancelReservation", 400)
    fake.schedule.reserved.discard("S-ARC2")        # dropped by the server between check and cancel
    res = C.cancel(client, store, EV, "S-ARC2", was_held=chk.held)
    assert res.status == "cancelled" and "400" in res.message
    assert fake.counts["CancelReservation"] == 1 and fake.counts["GetSchedule"] == 2


def test_probe_refuses_a_session_it_cannot_read_and_sends_nothing(cli_env):
    """A typo in the probe id must not read as 'open'. GetSession first, before any write."""
    fake, _, _ = cli_env
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-NOPE"])
    assert r.exit_code == 2 and "Nothing was sent" in r.output
    assert fake.counts["CancelReservation"] == 0


@pytest.mark.parametrize("err", [
    NetworkError(0, "ConnectError: no route", "CancelReservation"),
    AuthRequired(401, "sign in again", "CancelReservation"),
])
def test_probe_without_an_answer_says_so(cli_env, monkeypatch, client, err):
    def boom(*_a):
        raise err
    monkeypatch.setattr(client, "writes_open", boom)
    r = runner.invoke(cli.app, ["probe", "--session-id", "S-KEY1"])
    assert r.exit_code == 1 and "No answer about writes" in r.output and "answered 0" not in r.output
