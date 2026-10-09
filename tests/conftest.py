import pytest
from rich.console import Console

from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi, sample_sessions
from reseat.store import Store


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake():
    return FakeEventsApi(sessions=sample_sessions())


@pytest.fixture
def client(fake, clock):
    return EventsClient(
        token_provider=lambda: fake.token,
        base_url="https://api.awsevents.com",
        quota=QuotaTracker(clock=clock),
        sleep=clock.sleep,
        transport=fake.transport(),
    )


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture(autouse=True)
def plain_cli_output(monkeypatch):
    """CLI tests read the printed text. Colour codes and terminal width must not change it."""
    from reseat import cli
    monkeypatch.setattr(cli, "con", Console(color_system=None, highlight=False, width=200, emoji=False))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
