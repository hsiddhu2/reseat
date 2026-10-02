"""A sweep over the real reinvent2026 catalog pulled on 1 Oct 2026.

Counts are what the live API returned that day. If a newer fixture replaces
this one, update the numbers from the new pull, not by hand.
"""

from collections import Counter
from pathlib import Path

import pytest

from reseat.campus import VENUES
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import OBSERVED_TYPES
from reseat.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-2026-10-01.json"


@pytest.fixture(scope="module")
def swept():
    fake = FakeEventsApi.from_fixture(FIXTURE)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(),
                          transport=fake.transport())
    sessions = list(client.iter_sessions("reinvent2026", include_abstracts=False))
    store = Store(":memory:")
    result = store.apply_sweep("reinvent2026", sessions, with_abstracts=False)
    return fake, store, result


def test_sweep_walks_every_page(swept):
    fake, _, result = swept
    assert result.count == 2068
    assert len(result.added) == 2068
    assert fake.counts["ListSessions"] == 9  # 250 per page


def test_venue_counts(swept):
    _, store, _ = swept
    venues = Counter(s.campus_venue for s in store.all("reinvent2026"))
    assert venues == {
        "MGM Grand": 609, "Wynn": 394, "Caesars Palace": 329, "Caesars Forum": 317,
        "The Venetian": 233, "Encore": 124, None: 62,
    }
    assert set(venues) - {None} <= set(VENUES)
    assert "Mandalay Bay" not in venues


def test_type_counts(swept):
    _, store, _ = swept
    types = Counter(s.type_key for s in store.all("reinvent2026"))
    assert types == {
        "Chalk talk": 640, "Breakout session": 485, "Workshop": 291, "Builders' session": 243,
        "Lightning talk": 213, "Code talk": 119, "Lab": 31, "Gamified learning": 26,
        "Bootcamp": 16, "Exam prep": 4,
    }
    assert set(types) == set(OBSERVED_TYPES)


def test_no_bands_before_reserved_seating_opens(swept):
    # Seating opens 6 Oct. On 1 Oct no session carried a band or was reservable.
    _, store, result = swept
    assert result.band_changes == []
    assert not any(s.is_reservable for s in store.all("reinvent2026"))


def test_repeats_group_by_base_code(swept):
    _, store, _ = swept
    codes = [s.abbreviation for s in store.by_base_code("reinvent2026", "API303")]
    assert codes == ["API303-R", "API303-R1"]
