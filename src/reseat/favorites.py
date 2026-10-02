"""Mirror the rules' target sittings into favorites, so the official app shows the same plan.

API rules this module exists to respect:
- AssociateFavorites takes 1 to 10 sessions and counts each against 30 per minute.
  A batch never exceeds the quota left.
- A 200 does not mean every session was added. Read `failed`. Re-sending a
  session already favorited reports `alreadyFavorited`, so skip those first.
- Not idempotent. A failed session is never re-sent. After the last batch,
  read back GetSchedule and report any disagreement.
- Favorites work before 8 October. A 409 still stops the run cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import ApiError, EventsClient, OperationClosed
from .models import BulkResult, FailureCode
from .router import Router
from .rules import Rules
from .store import Store

OP = "AssociateFavorites"
BATCH_MAX = 10
CLOSED = "Favorites are closed (409). Nothing more was sent."


@dataclass
class FavoriteOutcome:
    session_id: str
    target: str
    status: str            # added | already | failed | unconfirmed | not_sent
    code: str | None = None


@dataclass
class FavoritesSync:
    wanted: list[tuple[str, str]] = field(default_factory=list)   # (session id, target label)
    missing: list[str] = field(default_factory=list)              # targets with nothing in the catalog
    outcomes: list[FavoriteOutcome] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    closed: bool = False
    error: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


def wanted_sittings(rules: Rules, store: Store, event_id: str) -> list[tuple[str, str]]:
    """Every target's sittings, then its backups' sittings, in priority order, each once."""
    router = Router(rules, store, event_id)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for t in rules.targets:
        for s, _backup in router.tree(t):
            if s.session_id not in seen:
                seen.add(s.session_id)
                out.append((s.session_id, t.label))
    return out


def plan(rules: Rules, store: Store, event_id: str, favorites: set[str]) -> FavoritesSync:
    """No I/O. Sessions already in favorites are recorded as `already` and never sent."""
    res = FavoritesSync(wanted=wanted_sittings(rules, store, event_id))
    found = {label for _, label in res.wanted}
    res.missing = [t.label for t in rules.targets if t.label not in found]
    for sid, label in res.wanted:
        if sid in favorites:
            res.outcomes.append(FavoriteOutcome(sid, label, "already"))
    return res


def sync(client: EventsClient, store: Store, event_id: str, rules: Rules,
         dry_run: bool = False) -> FavoritesSync:
    """Fresh GetSchedule, send what is missing in quota-sized batches, read back once."""
    res = plan(rules, store, event_id, set(client.get_schedule(event_id).favorites))
    labels = dict(res.wanted)
    todo = [sid for sid, _ in res.wanted if sid not in {o.session_id for o in res.outcomes}]
    if dry_run or not todo:
        res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in todo]
        return res
    sent: list[str] = []
    while todo:
        left = client.quota.remaining(OP)
        if left <= 0:
            client.sleep(client.quota.seconds_until(OP, 1))
            left = client.quota.remaining(OP)
        if left <= 0:
            res.error = "No AssociateFavorites quota left after waiting. Stopped. Run it again in a minute."
            res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in todo]
            break
        batch, todo = todo[:min(BATCH_MAX, left)], todo[min(BATCH_MAX, left):]
        try:
            result = client.favorite(event_id, batch)
        except OperationClosed:
            res.closed = True
            store.journal(event_id, OP, batch, {"status": 409}, "closed")
            res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in batch + todo]
            return res
        except ApiError as e:
            res.error = f"{e.status} {e}"
            store.journal(event_id, OP, batch, {"error": res.error}, "error")
            res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in todo]
            res.outcomes += [FavoriteOutcome(s, labels[s], "unconfirmed") for s in batch]
            todo = []
            sent += batch
            break
        store.journal(event_id, OP, batch, result.model_dump(by_alias=True),
                      "partial" if result.failed else "success")
        res.outcomes += _outcomes(batch, result, labels)
        sent += batch
    _read_back(client, store, event_id, res, sent)
    return res


def _outcomes(batch: list[str], result: BulkResult, labels: dict[str, str]) -> list[FavoriteOutcome]:
    out = []
    for sid in batch:
        f = result.failure_for(sid)
        if f is None:
            out.append(FavoriteOutcome(sid, labels[sid], "added"))
        elif f.known_code is FailureCode.ALREADY_FAVORITED:
            out.append(FavoriteOutcome(sid, labels[sid], "already", f.code))
        else:
            out.append(FavoriteOutcome(sid, labels[sid], "failed", f.code))
    return out


def _read_back(client: EventsClient, store: Store, event_id: str, res: FavoritesSync,
               sent: list[str]) -> None:
    try:
        favs = set(client.get_schedule(event_id).favorites)
    except ApiError as e:
        res.error = (res.error + "; " if res.error else "") + f"read-back failed: {e}"
        store.journal(event_id, "GetSchedule", sent, {"error": str(e)}, "readback-failed")
        return
    for o in res.outcomes:
        if o.session_id not in sent:
            continue
        if o.status in ("added", "unconfirmed") and o.session_id not in favs:
            if o.status == "added":
                res.disagreements.append(f"{o.session_id}: API said added, GetSchedule does not list it")
            o.status = "unconfirmed" if o.status == "added" else "failed"
        elif o.status == "unconfirmed" and o.session_id in favs:
            o.status = "added"
    store.journal(event_id, "GetSchedule", sent, {"favorites": sorted(favs),
                  "disagreements": res.disagreements},
                  "readback-disagreement" if res.disagreements else "readback-ok")
