"""Mirror the rules' target sittings into favorites, so the official app shows the same plan.

API rules this module exists to respect:
- AssociateFavorites takes 1 to 10 sessions and counts each against 30 per minute.
  A batch never exceeds the quota left.
- A 200 does not mean every session was added. Read `failed`. Re-sending a
  session already favorited reports `alreadyFavorited`, so skip those first.
- Not idempotent. A failed session is never re-sent. After the last write,
  read back GetSchedule and report any disagreement.
- DisassociateFavorite removes one session and counts against its own 30 per minute.
  Removing a session that is not favorited is a 404, so it is not a safe blind retry.
  A 404 is recorded as already gone and never sent again. Any other failure stops the run.
- Favorites work before 8 October. A 409 still stops the run cleanly.

Good citizen: a favorite re:Seat added for a target that is no longer in the rules is
removed, so the official app does not keep a plan the attendee dropped. Only favorites
the journal shows re:Seat itself added are ever removed. A favorite the attendee added,
and any session the attendee holds a seat in, is never touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .client import ApiError, EventsClient, OperationClosed
from .models import BulkResult, FailureCode
from .router import Router
from .rules import Rules
from .store import Store

OP = "AssociateFavorites"
UNOP = "DisassociateFavorite"
BATCH_MAX = 10
CLOSED = "Favorites are closed (409). Nothing more was sent."
DROPPED = "no longer a target"
SOURCE = "favorites-sync"      # tags this module's journal rows, apart from `reseat favorite`


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
    removals: list[FavoriteOutcome] = field(default_factory=list)  # removed | gone | failed | unconfirmed
    #                                                                | not_sent
    kept_held: list[str] = field(default_factory=list)            # re:Seat's favorites kept for a held seat
    prune_skipped: bool = False                                   # a target is missing: remove nothing
    disagreements: list[str] = field(default_factory=list)
    closed: bool = False
    error: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    def removed(self, status: str) -> int:
        return sum(1 for o in self.removals if o.status == status)


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


def added_by_reseat(store: Store, event_id: str) -> set[str]:
    """Sessions this sync favorited and has not removed since, from the journal.

    Only rows this module wrote count, so a session the attendee favorited with
    `reseat favorite` is theirs. A batch counts only for the sessions its result listed as
    successful, and only while this module's next GetSchedule read-back listed them. A batch
    whose answer was lost (an error or a 409) counts for none, even if a read-back later shows
    it landed, because re:Seat cannot be sure it was the one that added it. A session the
    attendee had already favorited comes back as `alreadyFavorited`, so it is never re:Seat's.
    """
    mine: set[str] = set()
    for row in store.journal_ops(event_id, (OP, UNOP, "GetSchedule")):
        try:
            request, response = json.loads(row["request"]), json.loads(row["response"])
        except (ValueError, TypeError):
            continue
        if not isinstance(response, dict) or not isinstance(request, list):
            continue
        if row["op"] == OP and response.get("source") == SOURCE and row["outcome"] in ("success", "partial"):
            mine |= {s for s in response.get("successful", []) if isinstance(s, str)}
        elif row["op"] == UNOP and row["outcome"] in ("removed", "gone"):
            mine -= set(request)
        elif row["op"] == "GetSchedule" and response.get("source") == SOURCE and "favorites" in response:
            mine -= set(request) - set(response["favorites"])
    return mine


def plan(rules: Rules, store: Store, event_id: str, favorites: set[str],
         reserved: set[str] | None = None, prune: bool = True) -> FavoritesSync:
    """No I/O. Sessions already in favorites are recorded as `already` and never sent.

    With `prune`, re:Seat's own favorites that no target wants any more are listed for removal,
    except a session the attendee holds a seat in. While any target is missing from the local
    catalog, nothing is removed: its sittings cannot be told apart from a dropped target's.
    """
    res = FavoritesSync(wanted=wanted_sittings(rules, store, event_id))
    found = {label for _, label in res.wanted}
    res.missing = [t.label for t in rules.targets if t.label not in found]
    for sid, label in res.wanted:
        if sid in favorites:
            res.outcomes.append(FavoriteOutcome(sid, label, "already"))
    if prune and res.missing:
        res.prune_skipped = True
    elif prune:
        stale = (added_by_reseat(store, event_id) & favorites) - {sid for sid, _ in res.wanted}
        held = reserved or set()
        res.kept_held = sorted(stale & held)
        res.removals = [FavoriteOutcome(sid, DROPPED, "not_sent") for sid in sorted(stale - held)]
    return res


def sync(client: EventsClient, store: Store, event_id: str, rules: Rules,
         dry_run: bool = False, prune: bool = True) -> FavoritesSync:
    """Fresh GetSchedule, add what is missing, remove re:Seat's own unwanted favorites, read back once."""
    schedule = client.get_schedule(event_id)
    res = plan(rules, store, event_id, set(schedule.favorites), set(schedule.reserved), prune)
    labels = dict(res.wanted)
    todo = [sid for sid, _ in res.wanted if sid not in {o.session_id for o in res.outcomes}]
    if dry_run or (not todo and not res.removals):
        res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in todo]
        return res
    sent = _add(client, store, event_id, res, todo, labels)
    removed: list[str] = []
    if not res.closed and not res.error:
        removed = _remove(client, store, event_id, res)
    if sent or removed:
        _read_back(client, store, event_id, res, sent, removed)
    return res


def _add(client: EventsClient, store: Store, event_id: str, res: FavoritesSync,
         todo: list[str], labels: dict[str, str]) -> list[str]:
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
            break
        except ApiError as e:
            res.error = f"{e.status} {e}"
            store.journal(event_id, OP, batch, {"error": res.error}, "error")
            res.outcomes += [FavoriteOutcome(s, labels[s], "not_sent") for s in todo]
            res.outcomes += [FavoriteOutcome(s, labels[s], "unconfirmed") for s in batch]
            sent += batch
            break
        store.journal(event_id, OP, batch, result.model_dump(by_alias=True) | {"source": SOURCE},
                      "partial" if result.failed else "success")
        res.outcomes += _outcomes(batch, result, labels)
        sent += batch
    return sent


def _remove(client: EventsClient, store: Store, event_id: str, res: FavoritesSync) -> list[str]:
    """One DisassociateFavorite per session, within quota. Never sent twice for one session."""
    sent: list[str] = []
    for o in res.removals:
        if client.quota.remaining(UNOP) <= 0:
            client.sleep(client.quota.seconds_until(UNOP, 1))
        if client.quota.remaining(UNOP) <= 0:
            res.error = "No DisassociateFavorite quota left after waiting. Stopped. Run it again in a minute."
            break
        try:
            client.unfavorite(event_id, o.session_id)
        except OperationClosed:
            res.closed = True
            store.journal(event_id, UNOP, [o.session_id], {"status": 409}, "closed")
            break
        except ApiError as e:
            if e.status == 404:
                o.status = "gone"
                store.journal(event_id, UNOP, [o.session_id], {"status": 404}, "gone")
                continue
            res.error = f"{e.status} {e}"
            o.status = "unconfirmed"
            store.journal(event_id, UNOP, [o.session_id], {"error": res.error}, "error")
            sent.append(o.session_id)
            break
        o.status = "removed"
        store.journal(event_id, UNOP, [o.session_id], {"status": 204}, "removed")
        sent.append(o.session_id)
    return sent


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
               sent: list[str], removed: list[str]) -> None:
    try:
        favs = set(client.get_schedule(event_id).favorites)
    except ApiError as e:
        res.error = (res.error + "; " if res.error else "") + f"read-back failed: {e}"
        store.journal(event_id, "GetSchedule", sent + removed, {"error": str(e)}, "readback-failed")
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
    for o in res.removals:
        if o.session_id not in removed:
            continue
        if o.status == "removed" and o.session_id in favs:
            res.disagreements.append(f"{o.session_id}: API said removed, GetSchedule still lists it. "
                                     "It stays a favorite, and re:Seat will not send the removal again.")
            o.status = "unconfirmed"
        elif o.status == "unconfirmed":
            o.status = "failed" if o.session_id in favs else "removed"
            if o.status == "removed":
                store.journal(event_id, UNOP, [o.session_id], {"readback": "not listed"}, "removed")
    store.journal(event_id, "GetSchedule", sent + removed, {"favorites": sorted(favs),
                  "disagreements": res.disagreements, "source": SOURCE},
                  "readback-disagreement" if res.disagreements else "readback-ok")
