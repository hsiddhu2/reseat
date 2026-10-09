"""What the CLI prints. Catalog and schedule text comes from the API, so it is shown as text and
never read as terminal markup, and a person sees sitting codes, not session ids."""

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store

EV = "reinvent2026"
HOSTILE = "[/bold][link=http://x.test]Click[/link] :fire: [red]"


def mk(sid, abbr, date, time_, title=None, band="available"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": title or f"Title {abbr}",
        "type": "Breakout session", "venue": "MGM Grand", "room": "Level 1 | Grand 117",
        "isReservable": True, "seatAvailability": band,
        "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


CATALOG = [
    mk("H1", "SEC201-R", "2026-12-01", "10:00", title=HOSTILE),
    mk("H2", "SEC201-R1", "2026-12-02", "10:00"),
    mk("B1", "ARC301", "2026-12-01", "10:00"),
]


@pytest.fixture
def env(clock, tmp_path, monkeypatch):
    fake = FakeEventsApi(sessions=CATALOG)
    fake.schedule.reserved.add("H1")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    (tmp_path / "rules.yaml").write_text("targets:\n- code: ARC301\n- code: SEC201\n")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    return fake, store


def test_swap_output_names_sittings_by_code(env):
    fake, _ = env
    r = CliRunner().invoke(cli.app, ["swap", "H1", "B1", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Fallbacks if this fails: SEC201-R (2026-12-01 10:00), SEC201-R1 (2026-12-02 10:00)" in r.output
    assert "swap SEC201-R -> ARC301:" in r.output and "held now: ARC301" in r.output
    assert "H1" not in r.output and "B1" not in r.output


def test_code_falls_back_to_the_id(env):
    _, store = env
    assert cli._code(None, EV, "H1") == "H1"
    assert cli._code(store, EV, "not-stored") == "not-stored"
    store.apply_sweep(EV, [*CATALOG, mk("N1", None, "2026-12-03", "09:00")], with_abstracts=False)
    assert cli._code(store, EV, "N1") == "N1"
    assert cli._code(store, EV, "H1", when=True) == "SEC201-R (2026-12-01 10:00)"


@pytest.mark.parametrize("args", [["show", "H1"], ["search", "Click"], ["schedule"]])
def test_api_text_is_printed_as_text(env, args):
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert "[link=http://x.test]" in " ".join(r.output.split()) or "[/bold][link=" in r.output
    assert ":fire:" in r.output


def test_markup_in_a_freshly_synced_title_does_not_stop_sync(env):
    fake, _ = env
    fake.add_session(mk("N2", "NEW100", "2026-12-04", "09:00", title="[/red] new [bold"))
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts"])
    assert r.exit_code == 0, r.output
    assert "new NEW100 [/red] new [bold" in r.output


def test_control_characters_in_api_text_cannot_reach_the_terminal():
    assert cli._esc("X\x1b[2JY\x07") == "X?[2JY?"
    assert cli._esc("a\nb") == "a\nb"
