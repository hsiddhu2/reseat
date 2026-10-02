"""Real catalog pulls saved as test fixtures.

A fixture is one full ListSessions walk with includeAbstracts=false, so the
abstracts are never fetched. Speaker names are dropped too. The repo is public
and re:Seat does not redistribute the catalog, so a fixture keeps only what
tests need: ids, codes, titles, types, levels, venues, rooms, times and bands.

File shape:
    {"eventId": ..., "pulledAt": "YYYY-MM-DD", "totalCount": n,
     "stripped": ["abstract", "speakers"], "items": [Session JSON, ...]}
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .client import EventsClient
from .models import Session

STRIPPED = ("abstract", "speakers")


def pull_catalog(client: EventsClient, event_id: str, today: date | None = None) -> dict[str, Any]:
    """Walk every page without abstracts. Fields keep the exact shape the API sent."""
    items = []
    for s in client.iter_sessions(event_id, include_abstracts=False):
        d = s.model_dump(by_alias=True, exclude_unset=True)
        for k in STRIPPED:
            d.pop(k, None)
        items.append(d)
    return {
        "eventId": event_id,
        "pulledAt": (today or date.today()).isoformat(),
        "totalCount": len(items),
        "stripped": list(STRIPPED),
        "items": items,
    }


def default_path(root: Path, today: date | None = None) -> Path:
    return root / "tests" / "fixtures" / f"catalog-{(today or date.today()).isoformat()}.json"


def save_catalog(data: dict[str, Any], path: Path) -> None:
    """One session per line so a later pull diffs cleanly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {k: v for k, v in data.items() if k != "items"}
    lines = [json.dumps(i, ensure_ascii=False, sort_keys=True) for i in data["items"]]
    body = json.dumps(head, ensure_ascii=False)[:-1] + ', "items": [\n' + ",\n".join(lines) + "\n]}\n"
    path.write_text(body, encoding="utf-8")


def load_catalog(path: Path | str) -> list[Session]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Session.model_validate(i) for i in data["items"]]


def latest_catalog(fixtures_dir: Path) -> Path | None:
    found = sorted(fixtures_dir.glob("catalog-*.json"))
    return found[-1] if found else None
