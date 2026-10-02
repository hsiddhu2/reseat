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
    assert fake.counts[F.OP] == 1 and res.error.startswith("503")
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
