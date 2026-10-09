"""Shared setup for the demos.

Each demo runs the real re:Seat CLI and engine against FakeEventsApi, an in-process
fake of the AWS Events API, with a throwaway home folder. Session codes and titles
come from the real 1 October 2026 catalog. Nothing is sent to AWS, no sign-in is
used, and nothing outside the temporary folder is touched.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from reseat import cli, config
from reseat.client import EventsClient
from reseat.fakeapi import FakeEventsApi
from reseat.fixtures import load_catalog
from reseat.models import Session
from reseat.store import Store

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "catalog-2026-10-01.json"
EV = "reinvent2026"
con = Console(emoji=False)


@dataclass
class Demo:
    fake: FakeEventsApi
    client: EventsClient
    store: Store
    home: Path


def banner(title: str, what: str) -> None:
    con.print(Panel.fit(f"[bold]{title}[/bold]\n{what}\n\n[dim]DEMO: a fake Events API runs in this "
                        "process. Nothing is sent to AWS.[/dim]", border_style="cyan"))


def step(text: str, wait: float = 1.2) -> None:
    con.print(f"\n[bold cyan]> {text}[/bold cyan]")
    time.sleep(wait * float(os.environ.get("RESEAT_DEMO_PACE", "1")))   # 0 in the smoke test


def catalog(bands: dict[str, str | None]) -> list[Session]:
    """Real sessions from the 1 October catalog, reservable, with the bands given by code."""
    out = []
    for s in load_catalog(FIXTURE):
        if s.abbreviation in bands:
            out.append(s.model_copy(update={"is_reservable": True,
                                            "seat_availability": bands[s.abbreviation]}))
    missing = set(bands) - {s.abbreviation for s in out}
    assert not missing, f"not in the fixture: {missing}"
    return out


def setup(rules_text: str, sessions: list[Session], held: tuple[str, ...] = ()) -> Demo:
    home = Path(tempfile.mkdtemp(prefix="reseat-demo-"))
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    config.HOME, config.DB_PATH, config.RULES_PATH = home, home / "reseat.db", home / "rules.yaml"
    config.RULES_PATH.write_text(rules_text, encoding="utf-8")
    fake = FakeEventsApi(sessions=sessions)
    fake.schedule.reserved.update(held)
    client = EventsClient(token_provider=lambda: fake.token, transport=fake.transport())
    store = Store(config.DB_PATH)
    atexit.register(store.close)          # registered last, so it runs before the folder goes
    cli._client = lambda: client       # the real CLI, pointed at the fake
    cli._store = lambda: store
    return Demo(fake, client, store, home)


def run(*args: str) -> None:
    """Run one real CLI command and show it as typed."""
    con.print(f"[dim]$[/dim] [bold]reseat {' '.join(args)}[/bold]")
    try:
        cli.app(list(args), standalone_mode=False)
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001  typer.Exit and friends: the command printed its own result
        if e.__class__.__name__ != "Exit":
            raise


def sid(d: Demo, code: str) -> str:
    return next(s.session_id for s in d.fake.sessions.values() if s.abbreviation == code)
