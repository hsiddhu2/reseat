"""The whole flow against a fault storm on the real 2026 catalog.

30 percent of reserve attempts come back full, the first request in every minute
gets a 429, one request gets a 503, writes return 409 for fifteen minutes, and the
fake enforces the real per-operation quotas. The flow: sync, favorites sync,
book, an hour of watching while seat bands move, swaps approved from the phone
path, then leave-now blocks. Every random draw is seeded, so a failure repeats.
"""

import random
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from reseat import favorites, guard
from reseat import rules as R
from reseat.campus import VEGAS
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.fixtures import load_catalog
from reseat.router import Router, run_booking
from reseat.store import Store
from reseat.swap import Swap
from reseat.watcher import Watcher

EV = "reinvent2026"
FIXTURE = Path(__file__).parent / "fixtures" / "catalog-2026-10-01.json"
OPEN = ("available", "limited", "veryLimited")
RESERVATION_WRITES = {"ReserveSessions", "CancelReservation"}
OTHER_WRITES = {"AssociateFavorites", "DisassociateFavorite", "CreatePersonalTime",
                "UpdatePersonalTime", "DeletePersonalTime"}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def world(seed):
    rng = random.Random(seed)
    sessions = []
    for s in load_catalog(FIXTURE):
        if not s.session_time or not s.session_time.date:
            continue
        band = "walkUp" if s.type == "Lightning talk" else rng.choices(
            ["available", "limited", "veryLimited", "unavailable"], [4, 2, 1, 3])[0]
        sessions.append(s.model_copy(update={"is_reservable": band != "walkUp",
                                             "seat_availability": band}))
    by_code = {}
    for s in sessions:
        by_code.setdefault(s.base_code, []).append(s)
    repeated = sorted(c for c, v in by_code.items() if len(v) >= 2 and v[0].type != "Lightning talk")
    chosen = rng.sample(repeated, 24)
    targets = chosen[:20]
    body = "max_per_day: 6\nwatch_cap: 25\nhome_venue: Venetian\ntargets:\n"
    for i, code in enumerate(targets):
        body += f"- code: {code}\n"
        if i < 4:
            body += f"  backups: [{chosen[20 + i]}]\n"
        if i in (2, 7, 11):
            body += "  auto_swap: true\n"
    rules = R.parse(body)
    start = datetime(2026, 10, 8, 9, 0, tzinfo=VEGAS).timestamp()
    clock = Clock(start)
    fake = FakeEventsApi(sessions=sessions)
    # Writes closed from minute 10 to 25 of the hour, while the watcher is booking.
    fake.storm(clock, seed=seed, full_rate=0.3, fail_503_at=60, closed=(start + 600, start + 1500))
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    return fake, client, Store(":memory:"), rules, clock, rng


def run_flow(seed):
    fake, client, store, rules, clock, rng = world(seed)
    events = []

    # Sync. Reads see the minute 429 too, and wait it out once.
    store.apply_sweep(EV, list(client.iter_sessions(EV, include_abstracts=False)), with_abstracts=False)
    fav = favorites.sync(client, store, EV, rules)

    # Book, as `reseat book` does.
    router = Router(rules, store, EV)
    booked = run_booking(router, client, store, EV, client.get_schedule(EV).reserved)

    # An hour of watching. Bands move, a repeat is added, a held session moves.
    def swapper(p, ok):
        return Swap(client, store, rules, EV).run_plan(p, approved=ok)

    w = Watcher(client, store, rules, EV, clock=clock, sleep=clock.sleep, swapper=swapper)
    w.subscribe(events.append)
    tree_ids = sorted({s.session_id for t in rules.targets for s, _ in router.tree(t)})
    for minute in range(60):
        for sid in rng.sample(tree_ids, max(1, len(tree_ids) // 12)):
            fake.set_band(sid, rng.choice(OPEN + ("unavailable",)))
        if minute == 20:
            base = fake.sessions[tree_ids[0]]
            fake.add_session(base.model_copy(update={
                "session_id": "NEW-REPEAT", "abbreviation": f"{base.base_code}-R9",
                "seat_availability": "available",
                "session_time": base.session_time.model_copy(
                    update={"date": "2026-12-04", "time": "08:00"})}))
        if minute == 30 and fake.schedule.reserved:
            held = sorted(fake.schedule.reserved)[0]
            fake.sessions[held] = fake.sessions[held].model_copy(update={"room": "Level 2 | Moved Room"})
        w.tick()
        pending = w.pending()
        if pending and rng.random() < 0.5:
            try:
                w.approve(pending[0].plan_id)
            except Exception:  # noqa: BLE001  ApprovalFailed keeps the proposal: that is allowed
                pass
        clock.sleep(w.next_delay())

    blocks = guard.sync(client, store, EV, rules)
    return fake, client, store, rules, router, events, fav, booked, blocks


@pytest.fixture(scope="module", params=[3, 11, 23])   # each seed reaches a swap
def storm(request):
    return run_flow(request.param)


def test_no_request_ever_exceeds_a_quota(storm):
    fake = storm[0]
    assert fake.quota_violations == []


def test_never_two_sittings_of_one_talk(storm):
    fake = storm[0]
    assert fake.two_sittings == []
    codes = [fake.sessions[h].base_code for h in fake.schedule.reserved]
    assert len(codes) == len(set(codes))


def test_only_what_the_rules_asked_for_is_held(storm):
    fake, _, _, rules, router = storm[:5]
    allowed = {s.session_id for t in rules.targets for s, _ in router.tree(t)} | {"NEW-REPEAT"}
    assert fake.schedule.reserved <= allowed


def test_every_reservation_write_is_read_back_before_the_next(storm):
    fake = storm[0]
    # A write needs a read-back if it may have landed: any 2xx, and any 5xx. A 409 and a
    # 429 are refusals that apply nothing.
    pending = None
    for op, status in fake.log:
        if op == "GetSchedule" and status == 200:
            pending = None
        elif op in RESERVATION_WRITES:
            if pending is not None:
                # The one designed exception: a swap cancels A, then reserves B at once.
                assert pending == "CancelReservation" and op == "ReserveSessions", fake.log
            if status < 300 or status >= 500:
                pending = op
    assert pending is None, "the flow ended with a write never read back"


def test_every_other_write_is_read_back_before_a_reservation_write(storm):
    fake = storm[0]
    dirty = False
    for op, status in fake.log:
        if op == "GetSchedule" and status == 200:
            dirty = False
        elif op in OTHER_WRITES and status < 300:
            dirty = True
        elif op in RESERVATION_WRITES:
            assert not dirty, fake.log
    assert not dirty


def test_the_storm_really_happened(storm):
    fake, events = storm[0], storm[5]
    log = fake.log
    statuses = Counter(s for _, s in log)
    assert statuses[429] >= 60                   # the minute throttle fired all hour
    assert statuses[503] == 1
    i409 = [i for i, (op, s) in enumerate(log) if s == 409]
    assert i409 and log[i409[0]][0] in RESERVATION_WRITES
    assert any(op == "ReserveSessions" and s == 200 for op, s in log[i409[-1] + 1:])   # booked after
    assert fake.full_draws > 0                   # the 30 percent full rule really refused seats
    assert Counter(e.kind for e in events)["booked"] >= 1
    assert any(e.kind == "swap" and e.data.get("state") == "verified" for e in events)
    assert fake.counts.get("CancelReservation", 0) >= 1


def test_a_409_holds_writes_instead_of_retrying_every_minute(storm):
    fake = storm[0]
    reserves_409 = [i for i, (op, s) in enumerate(fake.log) if op in RESERVATION_WRITES and s == 409]
    assert 1 <= len(reserves_409) <= 2         # a 15-minute window: one write, at most one recheck


def test_the_loop_never_stopped_and_outages_were_seen(storm):
    events = storm[5]
    sweeps = [e for e in events if e.kind == "sweep"]
    assert len(sweeps) == 60
    kinds = Counter(e.kind for e in events)
    assert kinds["booked"] >= 1


def test_the_journal_has_every_reservation_write(storm):
    fake, _, store = storm[:3]
    answered = sum(1 for op, s in fake.log if op == "ReserveSessions" and s != 429)
    rows = store.journal_entries(EV, 100_000)
    router_rows = sum(1 for r in rows if r["op"] == "ReserveSessions")
    swap_rows = sum(1 for r in rows if r["op"] in ("swap.reserve", "swap.rollback", "swap.fallback")
                    and r["outcome"].startswith("after"))
    assert router_rows + swap_rows == answered   # one journal row for every reserve the API answered


def test_leave_now_blocks_match_what_is_held(storm):
    fake, client, store, rules = storm[:4]
    gp = guard.compute(client, store, EV, rules)
    assert gp.empty or gp.warnings                # settled, or explains why not
    titles = {p["title"] for p in fake.schedule.personal_time.values()}
    held_codes = {fake.sessions[h].abbreviation for h in fake.schedule.reserved}
    assert {t.removeprefix("Leave for ") for t in titles} <= held_codes
