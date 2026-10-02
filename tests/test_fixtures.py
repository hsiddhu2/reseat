from datetime import date

from reseat.client import EventsClient
from reseat.fakeapi import FakeEventsApi
from reseat.fixtures import load_catalog, pull_catalog, save_catalog


def test_pull_strips_abstracts_and_speakers_and_round_trips(client, fake, tmp_path):
    data = pull_catalog(client, "reinvent2026", today=date(2026, 10, 1))
    assert data["totalCount"] == len(fake.sessions)
    assert all("abstract" not in i and "speakers" not in i for i in data["items"])
    # includeAbstracts=false on every page, so abstracts were never fetched.
    pages = [q for op, q in fake.params if op == "ListSessions"]
    assert pages and all(q.get("includeAbstracts") == "false" for q in pages)

    path = tmp_path / "catalog-2026-10-01.json"
    save_catalog(data, path)
    loaded = load_catalog(path)
    assert [s.session_id for s in loaded] == sorted(fake.sessions)
    assert all(s.abstract is None and s.speakers == [] for s in loaded)


def test_fake_serves_a_saved_fixture(fake, client, tmp_path):
    path = tmp_path / "c.json"
    save_catalog(pull_catalog(client, "reinvent2026"), path)
    f2 = FakeEventsApi.from_fixture(path, page_size=2)
    c2 = EventsClient(token_provider=lambda: f2.token, transport=f2.transport())
    got = list(c2.iter_sessions("reinvent2026", include_abstracts=False))
    assert len(got) == len(fake.sessions)
    assert f2.counts["ListSessions"] == 3
