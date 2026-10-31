import json

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import favorites as F
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store

EV = "reinvent2026"


@pytest.fixture
def synced(fake, store):
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)
    return store


def rules(body):
    return R.parse(body)


def status(res):
    return {o.session_id: o.status for o in res.outcomes}


def test_dedupes_backups_and_skips_already_favorited(fake, client, synced):
    fake.schedule.favorites.add("S-ARC2")
    r = rules("targets:\n- code: ARC301\n  backups: [SVS401]\n- code: SVS401\n")
    res = F.sync(client, synced, EV, r)
    assert [sid for sid, _ in res.wanted] == ["S-ARC1", "S-ARC2", "S-SVS1"]
    assert status(res) == {"S-ARC2": "already", "S-ARC1": "added", "S-SVS1": "added"}
    sent = [q for op, q in fake.params if op == F.OP]
    assert fake.counts[F.OP] == 1 and len(sent) == 1
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2", "S-SVS1"}


def test_batches_of_ten_split_across_the_quota_minute(clock):
    when = {"date": "2026-12-01", "time": "10:00", "length": "60"}
    sessions = [Session.model_validate({"sessionId": f"S{i:02d}", "title": "t",
                                        "abbreviation": f"X{i:02d}", "sessionTime": when})
                for i in range(25)]
    fake = FakeEventsApi(sessions=sessions)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, sessions, with_abstracts=False)
    client.quota.spend(F.OP, 25)                              # 5 left this minute
    t0 = clock.t
    r = rules("targets:\n" + "".join(f"- code: X{i:02d}\n" for i in range(25)))
    res = F.sync(client, store, EV, r)
    sizes = [len(e) for e in _sent_batches(store)]
    assert sizes == [5, 10, 10]
    assert clock.t - t0 >= 59
    assert res.count("added") == 25 and len(fake.schedule.favorites) == 25


def _sent_batches(store):
    rows = [r for r in store.journal_entries(EV) if r["op"] == F.OP]
    return [json.loads(r["request"]) for r in reversed(rows)]


def test_partial_failure_is_reported_per_session(fake, client, synced):
    fake.refuse["S-ARC1"] = "insufficientAccess"
    fake.refuse["S-SVS1"] = "someCodeAddedIn2027"
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n- code: SVS401\n"))
    assert status(res) == {"S-ARC1": "failed", "S-ARC2": "added", "S-SVS1": "failed"}
    codes = {o.session_id: o.code for o in res.outcomes}
    assert codes["S-SVS1"] == "someCodeAddedIn2027"
    assert fake.counts[F.OP] == 1                              # failures never re-sent


def test_already_favorited_race_counts_as_already(fake, client, synced):
    r = rules("targets:\n- code: SVS401\n")
    res = F.plan(r, synced, EV, favorites=set())               # stale view
    assert res.outcomes == []
    fake.schedule.favorites.add("S-SVS1")
    result = client.favorite(EV, ["S-SVS1"])
    assert F._outcomes(["S-SVS1"], result, {"S-SVS1": "SVS401"})[0].status == "already"


def test_read_back_disagreement_is_reported(fake, client, synced):
    fake.ghost.add("S-SVS1")
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert status(res) == {"S-SVS1": "unconfirmed"}
    assert res.disagreements and "does not list it" in res.disagreements[0]
    assert synced.journal_entries(EV)[0]["outcome"] == "readback-disagreement"


def test_409_stops_cleanly(fake, client, synced):
    fake.closed = True
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n- code: SVS401\n"))
    assert res.closed and fake.schedule.favorites == set()
    assert set(status(res).values()) == {"not_sent"}
    assert fake.counts[F.OP] == 1
    assert synced.journal_entries(EV)[0]["outcome"] == "closed"


def test_5xx_is_read_back_not_retried(fake, client, synced):
    fake.fail_next(F.OP, 503)
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert fake.counts[F.OP] == 1 and ": 503 " in res.error
    assert status(res) == {"S-SVS1": "failed"}                 # read-back says it did not land


def test_dry_run_sends_nothing(fake, client, synced):
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n"), dry_run=True)
    assert fake.counts[F.OP] == 0
    assert set(status(res).values()) == {"not_sent"}


def test_cli_favorites_sync(fake, client, synced, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: synced)
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n")
    r = CliRunner().invoke(cli.app, ["favorites", "sync", "--dry-run"])
    assert r.exit_code == 0 and "would add" in r.output and fake.counts[F.OP] == 0
    r = CliRunner().invoke(cli.app, ["favorites", "sync"])
    assert r.exit_code == 0 and "2 added" in r.output
    fake.closed = True
    fake.schedule.favorites.clear()
    r = CliRunner().invoke(cli.app, ["favorites", "sync"])
    assert r.exit_code == 3


def test_targets_missing_from_the_catalog_are_reported(fake, client, synced):
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n- code: NOPE999\n"))
    assert res.missing == ["NOPE999"] and res.count("added") == 2


def test_no_quota_after_waiting_stops_instead_of_sending(fake, client, synced, monkeypatch):
    monkeypatch.setattr(client.quota, "remaining", lambda op: 0)
    monkeypatch.setattr(client.quota, "seconds_until", lambda op, n: 1.0)
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n"))
    assert fake.counts.get(F.OP, 0) == 0 and "quota" in res.error
    assert set(status(res).values()) == {"not_sent"}


# ---------------------------------------------------------------- removing re:Seat's own stale favorites


def _mirror(fake, client, synced, body):
    """Favorite these targets through re:Seat, so the journal shows re:Seat added them."""
    F.sync(client, synced, EV, rules(body))


def test_dropped_target_favorites_re_seat_added_are_removed_and_read_back(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n- code: SVS401\n")
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2", "S-SVS1"}
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert {o.session_id: o.status for o in res.removals} == {"S-ARC1": "removed", "S-ARC2": "removed"}
    assert fake.schedule.favorites == {"S-SVS1"}
    assert fake.counts[F.UNOP] == 2
    assert synced.journal_entries(EV)[0]["op"] == "GetSchedule"           # one read-back after the removes
    assert synced.journal_entries(EV)[0]["outcome"] == "readback-ok"


def test_a_favorite_the_attendee_added_is_never_removed(fake, client, synced):
    fake.schedule.favorites |= {"S-SEC1", "S-ARC1"}                       # the attendee's own
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")          # re:Seat adds only S-ARC2
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert [o.session_id for o in res.removals] == ["S-ARC2"]
    assert {"S-SEC1", "S-ARC1"} <= fake.schedule.favorites


def test_a_favorite_for_a_held_seat_is_never_removed(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.schedule.reserved.add("S-ARC1")
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert [o.session_id for o in res.removals] == ["S-ARC2"]
    assert res.kept_held == ["S-ARC1"]
    assert "S-ARC1" in fake.schedule.favorites


def test_already_favorited_answers_are_not_counted_as_re_seats(fake, client, synced):
    synced.journal(EV, F.OP, ["S-SEC1"], {"successful": [], "failed": [
        {"sessionId": "S-SEC1", "code": "alreadyFavorited"}]}, "partial")
    synced.journal(EV, F.OP, ["S-WRK1"], {"error": "503"}, "error")      # answer lost: not counted
    synced.journal(EV, F.OP, ["S-KEY1"], {"status": 409}, "closed")
    assert F.added_by_reseat(synced, EV) == set()


def test_a_404_on_remove_is_recorded_gone_and_never_resent(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.schedule.favorites.discard("S-SVS1")
    res = F.plan(rules("targets: []\n"), synced, EV, favorites={"S-SVS1"})   # a stale view
    sent = F._remove(client, synced, EV, res)
    assert res.removals[0].status == "gone" and sent == []
    assert synced.journal_entries(EV)[0]["outcome"] == "gone"
    assert F.added_by_reseat(synced, EV) == set()                         # never tried again
    again = F.sync(client, synced, EV, rules("targets: []\n"))
    assert again.removals == [] and fake.counts[F.UNOP] == 1


def test_remove_said_204_but_still_listed_is_a_disagreement(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.stuck.add("S-SVS1")
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals[0].status == "unconfirmed"
    assert "still lists it" in res.disagreements[0]
    assert synced.journal_entries(EV)[0]["outcome"] == "readback-disagreement"
    again = F.sync(client, synced, EV, rules("targets: []\n"))
    assert again.removals == [] and fake.counts[F.UNOP] == 1             # never sent twice


def test_5xx_on_remove_stops_and_the_read_back_settles_it(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.fail_next(F.UNOP, 503)
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert fake.counts[F.UNOP] == 1 and ": 503 " in res.error
    assert {o.session_id: o.status for o in res.removals} == {"S-ARC1": "failed", "S-ARC2": "not_sent"}


def test_409_on_remove_stops_cleanly(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.closed = True
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.closed and fake.counts[F.UNOP] == 1
    assert {o.status for o in res.removals} == {"not_sent"}
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2"}


def test_dry_run_and_no_prune_remove_nothing(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    res = F.sync(client, synced, EV, rules("targets: []\n"), dry_run=True)
    assert [o.status for o in res.removals] == ["not_sent", "not_sent"]
    res = F.sync(client, synced, EV, rules("targets: []\n"), prune=False)
    assert res.removals == [] and fake.counts.get(F.UNOP, 0) == 0
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2"}


def test_no_unfavorite_quota_after_waiting_stops(fake, client, synced, monkeypatch):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    monkeypatch.setattr(client.quota, "remaining", lambda op: 0 if op == F.UNOP else 30)
    monkeypatch.setattr(client.quota, "seconds_until", lambda op, n: 1.0)
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert fake.counts.get(F.UNOP, 0) == 0 and "quota" in res.error
    assert res.removals[0].status == "not_sent"


def test_cli_favorites_sync_prints_removals(fake, client, synced, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: synced)
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n- code: SVS401\n")
    fake.schedule.reserved.add("S-ARC1")
    (tmp_path / "rules.yaml").write_text("targets:\n- code: SVS401\n")
    r = CliRunner().invoke(cli.app, ["favorites", "sync", "--dry-run"])
    assert r.exit_code == 0 and "would remove" in r.output and "ARC301-R2" in r.output
    assert "1 favorite re:Seat added for dropped targets to remove" in r.output
    assert "kept, because you hold a seat there" in r.output and fake.counts.get(F.UNOP, 0) == 0
    r = CliRunner().invoke(cli.app, ["favorites", "sync"])
    assert r.exit_code == 0 and "removed" in r.output and fake.schedule.favorites == {"S-ARC1", "S-SVS1"}


# ---------------------------------------------------------------- removal failure paths (QA pass)


def _net_client(fake, clock, on_delete, on_refresh=None, token=None):
    """A client whose transport runs `on_delete(req)` for each DisassociateFavorite first."""
    import httpx

    def route(req):
        if req.method == "DELETE" and "/favorites/" in req.url.path:
            on_delete(req)
        return fake._handle(req)

    return EventsClient(token_provider=token or (lambda: fake.token), on_refresh=on_refresh,
                        base_url="https://api.awsevents.com", quota=QuotaTracker(clock=clock),
                        sleep=clock.sleep, transport=httpx.MockTransport(route))


def test_429_on_remove_waits_retry_after_once_then_removes(fake, client, synced, clock):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.throttle_next(F.UNOP, retry_after=7)
    t0 = clock.t
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert clock.t - t0 >= 7 and fake.counts[F.UNOP] == 2          # refused one, then one retry
    assert res.removals[0].status == "removed" and fake.schedule.favorites == set()


def test_429_twice_on_remove_stops_and_is_read_back(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.fail_next(F.UNOP, 429, times=2)
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert fake.counts[F.UNOP] == 2 and ": 429 " in res.error
    assert res.removals[0].status == "failed"                       # read-back still lists it
    assert synced.journal_entries(EV)[0]["op"] == "GetSchedule"


@pytest.mark.parametrize("code", [400, 403, 500])
def test_other_errors_on_remove_stop_and_are_read_back(fake, client, synced, code):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.fail_next(F.UNOP, code)
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert fake.counts[F.UNOP] == 1 and str(code) in res.error
    assert {o.session_id: o.status for o in res.removals} == {"S-ARC1": "failed", "S-ARC2": "not_sent"}


def test_400_for_not_favorited_as_the_live_api_answers_settles_as_removed(fake, client, synced):
    """api-facts: the live API answered 400, not 404, for a cancel not held. Same may hold here."""
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.schedule.favorites.discard("S-SVS1")                       # gone between read and write
    res = F.plan(rules("targets: []\n"), synced, EV, favorites={"S-SVS1"})
    fake.fail_next(F.UNOP, 400)
    sent = F._remove(client, synced, EV, res)
    F._read_back(client, synced, EV, res, [], sent)
    assert res.removals[0].status == "removed"
    assert F.added_by_reseat(synced, EV) == set()                   # never sent again


def test_dropped_connection_on_remove_is_read_back_not_retried(fake, synced, clock):
    import httpx
    calls = {"n": 0}

    def refuse(req):
        calls["n"] += 1
        raise httpx.ConnectError("Connection refused", request=req)

    client = _net_client(fake, clock, refuse)
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert calls["n"] == 1 and ": 0 " in res.error and "ConnectError" in res.error
    assert {o.session_id: o.status for o in res.removals} == {"S-ARC1": "failed", "S-ARC2": "not_sent"}


def test_timeout_after_the_remove_landed_is_settled_by_read_back(fake, synced, clock):
    import httpx

    def lands_then_times_out(req):
        fake.schedule.favorites.discard(req.url.path.rsplit("/", 1)[-1])
        raise httpx.ReadTimeout("timed out", request=req)

    client = _net_client(fake, clock, lands_then_times_out)
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals[0].status == "removed" and not res.disagreements
    assert F.added_by_reseat(synced, EV) == set()
    again = F.sync(client, synced, EV, rules("targets: []\n"))
    assert again.removals == [] and fake.counts.get(F.UNOP, 0) == 0   # the fake never saw it: raised first


def test_401_on_remove_refreshes_once_then_removes(fake, synced, clock):
    state = {"t": fake.token}

    def refresh():
        state["t"] = fake.token
        return fake.token

    client = _net_client(fake, clock, lambda req: None, on_refresh=refresh, token=lambda: state["t"])
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    state["t"] = "expired"
    res = F.sync(client, synced, EV, rules("targets: []\n"))     # GetSchedule refreshes first
    assert res.removals[0].status == "removed" and fake.schedule.favorites == set()
    state["t"] = "expired"
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    state["t"] = fake.token
    fake.fail_next(F.UNOP, 401)                                     # 401 on the delete itself
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals[0].status == "removed" and fake.counts[F.UNOP] == 3


def test_401_after_refresh_on_remove_stops_and_reads_back(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    fake.fail_next(F.UNOP, 401)                                     # no on_refresh: AuthRequired
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert ": 401 " in res.error and res.removals[0].status == "failed"


def test_read_back_failure_after_remove_is_reported(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    real = client.unfavorite

    def then_fail_read(ev, sid):
        real(ev, sid)
        fake.fail_next("GetSchedule", 503)

    client.unfavorite = then_fail_read
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert "read-back failed" in res.error
    assert synced.journal_entries(EV)[0]["outcome"] == "readback-failed"
    assert json.loads(synced.journal_entries(EV)[0]["request"]) == ["S-SVS1"]


def test_unconfirmed_remove_left_by_a_failed_read_back_is_not_resent_blind(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    real = client.unfavorite

    def lands_then_errors(ev, sid):
        real(ev, sid)
        fake.fail_next("GetSchedule", 503)
        from reseat.client import ApiError
        raise ApiError(500, "lost answer", F.UNOP)

    client.unfavorite = lands_then_errors
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals[0].status == "unconfirmed" and "read-back failed" in res.error
    client.unfavorite = real
    again = F.sync(client, synced, EV, rules("targets: []\n"))     # fresh GetSchedule decides
    assert again.removals == [] and fake.counts[F.UNOP] == 1


def test_quota_partly_spent_waits_for_the_minute_then_removes(fake, client, synced, clock):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    client.quota.spend(F.UNOP, 29)
    t0 = clock.t
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert {o.status for o in res.removals} == {"removed"} and clock.t - t0 >= 59
    assert fake.counts[F.UNOP] == 2


def test_an_add_error_sends_no_removal(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.fail_next(F.OP, 503)
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert fake.counts.get(F.UNOP, 0) == 0
    assert {o.status for o in res.removals} == {"not_sent"}
    assert fake.counts["GetSchedule"] == 4                  # 2 for the mirror, then fresh read and read-back


def test_a_409_on_add_sends_no_removal(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.closed = True
    res = F.sync(client, synced, EV, rules("targets:\n- code: SVS401\n"))
    assert res.closed and fake.counts.get(F.UNOP, 0) == 0
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2"}


def test_a_404_mid_run_does_not_stop_the_next_removal(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    fake.schedule.favorites.discard("S-ARC1")
    res = F.plan(rules("targets: []\n"), synced, EV, favorites={"S-ARC1", "S-ARC2"})
    sent = F._remove(client, synced, EV, res)
    assert {o.session_id: o.status for o in res.removals} == {"S-ARC1": "gone", "S-ARC2": "removed"}
    assert sent == ["S-ARC2"] and fake.schedule.favorites == set()


def test_a_backup_sitting_still_wanted_is_never_removed(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n- code: SVS401\n")
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n  backups: [SVS401]\n"))
    assert res.removals == [] and fake.counts.get(F.UNOP, 0) == 0


def test_every_remove_is_followed_by_a_read_back(fake, client, synced):
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n")
    F.sync(client, synced, EV, rules("targets: []\n"))
    ops = [op for op, _ in fake.calls]
    last_delete = max(i for i, op in enumerate(ops) if op == F.UNOP)
    assert "GetSchedule" in ops[last_delete + 1:]


def test_a_target_still_in_the_rules_but_missing_from_the_catalog_keeps_its_favorites(fake, client, synced):
    # Found by QA: a target still in the rules but missing from the local catalog had its
    # favorites removed. Now nothing is removed while any target is missing.
    _mirror(fake, client, synced, "targets:\n- code: ARC301\n- code: SVS401\n")
    keep = [s for s in fake.sessions.values() if s.session_id not in ("S-ARC1", "S-ARC2")]
    synced.apply_sweep(EV, keep, with_abstracts=False)
    res = F.sync(client, synced, EV, rules("targets:\n- code: ARC301\n- code: SVS401\n"))
    assert res.missing == ["ARC301"]
    assert res.removals == [], "a favorite was removed for a target that is still in the rules"
    assert res.prune_skipped and fake.counts.get(F.UNOP, 0) == 0


def test_a_favorite_made_with_reseat_favorite_is_the_attendees(fake, client, synced, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: synced)
    r = CliRunner().invoke(cli.app, ["favorite", "S-SEC1"])
    assert r.exit_code == 0 and "S-SEC1" in fake.schedule.favorites
    assert F.added_by_reseat(synced, EV) == set()
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals == [] and "S-SEC1" in fake.schedule.favorites


def test_a_ghost_add_the_read_back_did_not_list_is_never_counted(fake, client, synced):
    fake.ghost.add("S-SVS1")
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    assert F.added_by_reseat(synced, EV) == set()
    fake.schedule.favorites.add("S-SVS1")                     # the attendee favorites it later
    res = F.sync(client, synced, EV, rules("targets: []\n"))
    assert res.removals == [] and "S-SVS1" in fake.schedule.favorites


def test_a_lost_add_answer_is_never_counted_even_if_it_landed(fake, client, synced):
    fake.fail_next(F.OP, 503)
    _mirror(fake, client, synced, "targets:\n- code: SVS401\n")
    assert F.added_by_reseat(synced, EV) == set()
