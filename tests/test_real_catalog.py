"""A sweep over a sample of the real reinvent2026 catalog pulled on 1 Oct 2026.

The sample keeps 200 of the 2,068 sessions, with every type and venue, repeats
kept with all their sittings, and sessions with no venue or time. Counts are
computed from the sample file. If the sample is rebuilt, recount from the file.
"""

from collections import Counter
from pathlib import Path

import pytest

from reseat.campus import VENUES
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import OBSERVED_TYPES
from reseat.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-sample-2026-10-01.json"


@pytest.fixture(scope="module")
def swept():
    fake = FakeEventsApi.from_fixture(FIXTURE, page_size=50)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(),
                          transport=fake.transport())
    sessions = list(client.iter_sessions("reinvent2026", include_abstracts=False))
    store = Store(":memory:")
    result = store.apply_sweep("reinvent2026", sessions, with_abstracts=False)
    return fake, store, result


def test_sweep_walks_every_page(swept):
    fake, _, result = swept
    assert result.count == 200
    assert len(result.added) == 200
    assert fake.counts["ListSessions"] == 4  # 50 per page


def test_venue_counts(swept):
    _, store, _ = swept
    venues = Counter(s.campus_venue for s in store.all("reinvent2026"))
    assert venues == {
        "MGM Grand": 56, "Caesars Forum": 36, "Caesars Palace": 35, "Wynn": 33,
        "Encore": 24, "The Venetian": 8, None: 8,
    }
    assert set(venues) - {None} <= set(VENUES)
    assert "Mandalay Bay" not in venues


def test_type_counts(swept):
    _, store, _ = swept
    types = Counter(s.type_key for s in store.all("reinvent2026"))
    assert types == {
        "Chalk talk": 96, "Builders' session": 32, "Workshop": 32, "Code talk": 18,
        "Breakout session": 8, "Lab": 7, "Lightning talk": 3, "Gamified learning": 2,
        "Bootcamp": 1, "Exam prep": 1,
    }
    assert set(types) == set(OBSERVED_TYPES)


def test_no_bands_before_reserved_seating_opens(swept):
    # Seating opens 6 Oct. On 1 Oct no session carried a band or was reservable.
    _, store, result = swept
    sessions = store.all("reinvent2026")
    assert all(s.seat_availability is None for s in sessions)        # the API sent no band at all
    assert not any(s.is_reservable for s in sessions)


def test_repeats_group_by_base_code(swept):
    _, store, _ = swept
    codes = [s.abbreviation for s in store.by_base_code("reinvent2026", "API303")]
    assert codes == ["API303-R", "API303-R1"]
