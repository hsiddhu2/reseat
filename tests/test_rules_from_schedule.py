"""reseat rules from-schedule: targets from the official schedule, for attendees who built it in
the AWS portal and never used a planner."""

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.auth import AuthError
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store

EV = "reinvent2026"


def mk(sid, abbr, date, time_, band="available"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


CATALOG = [
    mk("R2", "SEC201-R", "2026-12-01", "10:00"),
    mk("R1", "ARC301-R", "2026-12-01", "14:00"),
    mk("F2", "DOP302-R", "2026-12-02", "09:00"),
    mk("F1", "CMP401-R", "2026-12-02", "11:00"),
    mk("F3", "CMP401-R1", "2026-12-03", "11:00"),    # a later sitting of a favorited talk
]


@pytest.fixture
def env(clock, tmp_path, monkeypatch):
    fake = FakeEventsApi(sessions=CATALOG)
    fake.schedule.reserved |= {"R1", "R2"}
    fake.schedule.favorites |= {"F1", "F2", "R1"}           # R1 is reserved and a favorite
    fake.schedule.order = ["R2", "R1", "F2", "F1"]         # not sorted: the API's order is kept
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    return fake, store, tmp_path / "rules.yaml"


def run(*args, input=None):
    return CliRunner().invoke(cli.app, ["rules", "from-schedule", *args], input=input)


def test_reserved_first_then_favorites_in_api_order(env):
    _, _, path = env
    r = run()
    assert r.exit_code == 0, r.output
    rules = R.load(path)
    assert [t.session_id for t in rules.targets] == ["R2", "R1", "F2", "F1"]
    assert all(t.code is None for t in rules.targets)       # ids only: codes may not resolve
    assert all(t.repeats is False for t in rules.targets)   # the sitting the attendee picked
    assert "Wrote 2 reserved and 2 favorites" in r.output


def test_other_settings_are_the_rules_init_defaults(env):
    _, _, path = env
    run()
    got = yaml.safe_load(path.read_text())
    init = yaml.safe_load(R.EXAMPLE)
    for key in ("buffer_minutes", "max_per_day", "watch_cap", "home_venue"):
        assert got[key] == init[key], key
    assert got["meals"] == []                               # the example lunch stays a comment
    assert '{day: Tuesday, start: "12:00"' in path.read_text()


def test_works_with_an_empty_catalog(env):
    _, store, path = env
    assert store.get(EV, "R1") is None                     # nothing in the local catalog
    assert run().exit_code == 0
    assert len(R.load(path).targets) == 4


def test_codes_are_noted_when_the_catalog_knows_them(env):
    _, store, path = env
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    run()
    text = path.read_text()
    assert '"R2", repeats: false}   # reserved, SEC201-R' in text
    assert '"F1", repeats: false}   # favorite, CMP401-R' in text


def test_existing_file_is_refused_without_force(env):
    fake, _, path = env
    path.write_text("targets: []\n")
    r = run()
    assert r.exit_code == 1 and "--force" in r.output
    assert path.read_text() == "targets: []\n" and fake.counts.get("GetSchedule", 0) == 0
    path.write_text("targets: []\nserve_secret: abcdefghijklmnopqrstu\n")
    r = run("--force")
    assert r.exit_code == 0 and len(R.load(path).targets) == 4
    assert R.load(path).serve_secret is None
    assert "serve_secret" in (path.parent / "rules.yaml.bak").read_text()     # kept beside it
    assert "rules.yaml.bak" in " ".join(r.output.split()) and "reset" in r.output


def test_a_file_created_during_the_read_is_not_replaced(env, monkeypatch):
    fake, _, path = env
    real = cli._client()

    class Racy:
        def get_schedule(self, event):
            path.write_text("targets: []\n")                 # another command wrote it meanwhile
            return real.get_schedule(event)

    monkeypatch.setattr(cli, "_client", lambda: Racy())
    r = run()
    assert r.exit_code == 1 and path.read_text() == "targets: []\n"


def test_says_favorites_will_be_booked(env):
    r = run()
    assert "2 favorites are now targets" in " ".join(r.output.split())


def test_empty_schedule_writes_nothing(env):
    fake, _, path = env
    fake.schedule.reserved.clear()
    fake.schedule.favorites.clear()
    r = run()
    assert r.exit_code == 1 and "no reserved sessions and no favorites" in r.output
    assert not path.exists()


@pytest.mark.parametrize("status", [503, 500, 403, 401, 404])
def test_schedule_read_failure_writes_nothing(env, status):
    fake, _, path = env
    fake.fail_next("GetSchedule", status, times=3)
    r = run()
    assert r.exit_code == 2 and "Nothing written" in r.output
    assert not path.exists()


def test_past_watch_cap_is_written_as_comments_not_dropped(env):
    fake, _, path = env
    fake.schedule.favorites |= {f"X{i:02d}" for i in range(30)}
    r = run()
    assert r.exit_code == 0, r.output
    rules = R.load(path)
    assert len(rules.targets) == 25 and rules.targets[0].session_id == "R2"
    assert path.read_text().count("Over watch_cap") == 34 - 25
    assert "9 favorites are past watch_cap" in r.output


def test_reserved_seats_are_never_left_out(env):
    fake, _, path = env
    fake.schedule.reserved |= {f"Y{i:02d}" for i in range(28)}
    r = run()
    assert r.exit_code == 0, r.output
    rules = R.load(path)
    assert rules.watch_cap == 30 and {"R1", "R2"} | {f"Y{i:02d}" for i in range(28)} <= {
        t.session_id for t in rules.targets}
    assert "favorites are past watch_cap" in r.output       # F1 and F2, not reserved seats


def test_warns_about_days_already_at_max_per_day(env):
    fake, store, _ = env
    extra = [mk(f"D{i}", f"DAY10{i}-R", "2026-12-01", f"{8 + i}:00") for i in range(4)]
    store.apply_sweep(EV, [*CATALOG, *extra], with_abstracts=False)
    fake.schedule.reserved |= {f"D{i}" for i in range(4)}
    r = run()
    assert "2026-12-01 already holds max_per_day" in " ".join(r.output.split())
    assert "2026-12-02" not in r.output


def test_all_digit_ids_stay_strings():
    imp = R.from_schedule(["1784231800000001"], [])
    assert R.parse(imp.text).targets[0].session_id == "1784231800000001"


@pytest.mark.parametrize("bad", ["a b", "x\ny: 1", "x\"", "", "x" * 129, "R2\n", "R2\r", "R2 "])
def test_unexpected_ids_are_refused(bad):
    with pytest.raises(R.RulesError):
        R.from_schedule([bad], [])


def test_favorites_sync_and_book_work_from_the_written_file(env, monkeypatch):
    fake, store, path = env
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    assert run().exit_code == 0
    r = CliRunner().invoke(cli.app, ["favorites", "sync"])
    assert r.exit_code == 0, r.output
    assert {"R1", "R2", "F1", "F2"} <= fake.schedule.favorites
    r = CliRunner().invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 0, r.output
    assert fake.schedule.reserved == {"R1", "R2", "F1", "F2"}
    assert fake.counts.get("CancelReservation", 0) == 0     # held seats untouched
    assert fake.counts.get("DisassociateFavorites", 0) == 0
    ops = [op for op, _ in fake.log]
    last_reserve = max(i for i, op in enumerate(ops) if op == "ReserveSessions")
    assert "GetSchedule" in ops[last_reserve + 1:]            # read back after the write


def test_favorites_sync_adds_only_the_picked_sittings(env):
    fake, store, _ = env
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    run()
    assert CliRunner().invoke(cli.app, ["favorites", "sync"]).exit_code == 0
    assert fake.schedule.favorites == {"R1", "R2", "F1", "F2"}  # never F3, a sitting not picked


def test_a_full_favorite_is_not_moved_to_another_sitting(env):
    fake, store, _ = env
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    run()
    fake.full.add("F1")
    r = CliRunner().invoke(cli.app, ["book", "--yes"])
    assert r.exit_code == 0, r.output
    assert fake.schedule.reserved == {"R1", "R2", "F2"}      # F1 full, F3 never tried
    assert fake.counts.get("CancelReservation", 0) == 0


def test_catalog_text_cannot_add_keys_to_the_file(env):
    _, store, path = env
    evil = "X\nserve_secret: aaaaaaaaaaaaaaaaaaaa\nwatch_cap: 1"
    store.apply_sweep(EV, [mk("R2", evil, "2026-12-01", "10:00"), *CATALOG[1:]], with_abstracts=False)
    assert run().exit_code == 0
    rules = R.load(path)
    assert rules.serve_secret is None and rules.watch_cap == 25 and len(rules.targets) == 4


def test_a_trailing_newline_in_a_code_is_not_written(env):
    _, store, path = env
    store.apply_sweep(EV, [mk("R2", "SEC201-R\n", "2026-12-01", "10:00"), *CATALOG[1:]], with_abstracts=False)
    run()
    assert '"R2", repeats: false}   # reserved\n' in path.read_text()


def _client_with(fake, clock, handler, on_refresh=None):
    def route(req):
        return handler(req) or fake._handle(req)
    return EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                        sleep=clock.sleep, transport=httpx.MockTransport(route), on_refresh=on_refresh)


@pytest.mark.parametrize("body", [b"not json", b'{"nope": 1}', b'{"schedule": {"reserved": [1, 2]}}',
                                  b'{"schedule": {"reserved": "R1"}}'])
def test_malformed_schedule_writes_nothing_and_says_so(env, clock, monkeypatch, body):
    fake, _, path = env
    c = _client_with(fake, clock, lambda req: httpx.Response(200, content=body)
                     if req.url.path.endswith("/schedule") else None)
    monkeypatch.setattr(cli, "_client", lambda: c)
    r = run()
    assert r.exit_code == 2 and "does not match the spec" in r.output and "Nothing written" in r.output
    assert not path.exists()


def test_connection_failure_writes_nothing(env, clock, monkeypatch):
    fake, _, path = env

    def down(req):
        raise httpx.ConnectError("Connection refused", request=req)
    monkeypatch.setattr(cli, "_client", lambda: _client_with(fake, clock, down))
    r = run()
    assert r.exit_code == 2 and not path.exists()


def test_expired_sign_in_writes_nothing(env, clock, monkeypatch):
    fake, _, path = env

    def failed_refresh():
        raise AuthError("refresh token expired")
    c = _client_with(fake, clock, lambda req: httpx.Response(401, json={"message": "expired"}),
                     on_refresh=failed_refresh)
    monkeypatch.setattr(cli, "_client", lambda: c)
    r = run()
    assert r.exit_code == 2 and not path.exists()


def test_one_429_is_waited_out_then_the_file_is_written(env, clock):
    fake, _, path = env
    fake.throttle_next("GetSchedule", retry_after=3)
    t0 = clock()
    assert run().exit_code == 0 and len(R.load(path).targets) == 4
    assert clock() - t0 >= 3


def test_refusal_makes_no_api_call_at_all(env, monkeypatch):
    _, _, path = env
    path.write_text("targets: []\n")
    monkeypatch.setattr(cli, "_client", lambda: pytest.fail("no client for a refused run"))
    assert run().exit_code == 1


def test_force_keeps_the_old_file_when_the_read_fails(env):
    fake, _, path = env
    path.write_text("targets: []\nserve_secret: abcdefghijklmnopqrstu\n")
    fake.fail_next("GetSchedule", 503, times=3)
    assert run("--force").exit_code == 2
    assert "serve_secret" in path.read_text()


def test_ids_up_to_the_spec_limit_are_accepted():
    imp = R.from_schedule(["x" * 128], [])
    assert R.parse(imp.text).targets[0].session_id == "x" * 128


def test_write_new_never_leaves_a_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    assert R.write_new(path, "a: 1\n") and path.read_text() == "a: 1\n"
    assert not R.write_new(path, "b: 2\n") and path.read_text() == "a: 1\n"
    assert R.write_new(path, "b: 2\n", replace=True) and path.read_text() == "b: 2\n"
    assert [p.name for p in tmp_path.iterdir()] == ["rules.yaml"]       # no temp file left
