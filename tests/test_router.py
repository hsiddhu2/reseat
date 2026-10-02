from collections import Counter
from pathlib import Path

import pytest

from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.fixtures import load_catalog
from reseat.models import OBSERVED_TYPES, Session
from reseat.router import (
    OP,
    OTHER_RANK,
    WRITES_CLOSED,
    Router,
    execute,
    run_booking,
    scarcity_rank,
)
from reseat.store import Store

EV = "reinvent2026"
FIXTURE = Path(__file__).parent / "fixtures" / "catalog-2026-10-01.json"


def mk(sid, abbr, typ="Breakout session", date="2026-12-01", time_="10:00", mins=60,
       band=None, reservable=True):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": typ,
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": reservable,
        "seatAvailability": band,
        "sessionTime": {"date": date, "time": time_, "length": str(mins)},
    })


CATALOG = [
    mk("A1", "ARC301-R", date="2026-12-01", time_="10:00"),
    mk("A2", "ARC301-R1", date="2026-12-02", time_="10:00"),
    mk("A3", "ARC301-R2", date="2026-12-03", time_="10:00"),
    mk("W1", "DOP302", typ="Workshop", date="2026-12-01", time_="13:00", mins=120),
    mk("B1", "BLD201-R", typ="Builders’ session", date="2026-12-01", time_="16:00"),
    mk("C1", "SVS401", typ="Chalk talk", date="2026-12-01", time_="10:30"),
    mk("C2", "SVS401-R1", typ="Chalk talk", date="2026-12-04", time_="09:00"),
    mk("L1", "LAB101", typ="Lab", date="2026-12-02", time_="13:00"),
    mk("K1", "KEY001", typ="Keynote", date="2026-12-02", time_="08:00", reservable=False),
    mk("X1", "SEC201", date="2026-12-03", time_="14:00"),
]


@pytest.fixture
def world(clock):
    fake = FakeEventsApi(sessions=CATALOG)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, CATALOG, with_abstracts=False)
    return fake, client, store


def router(store, body):
    return Router(R.parse(body), store, EV)


def codes(plan):
    return [p.code for p in plan.batch]


# ---------------------------------------------------------------------- plan


def test_scarcity_uses_real_catalog_types():
    # Every type in the 1 Oct 2026 fixture has a deliberate place.
    types = Counter(s.type for s in load_catalog(FIXTURE))
    assert set(types) == set(OBSERVED_TYPES)
    order = sorted(OBSERVED_TYPES, key=scarcity_rank)
    assert order[:3] == ["Workshop", "Lab", "Bootcamp"]
    assert order[3:7] == ["Builders' session", "Chalk talk", "Code talk", "Breakout session"]
    assert {scarcity_rank(t) for t in ("Lightning talk", "Gamified learning", "Exam prep")} == {OTHER_RANK}
    assert scarcity_rank("Builders’ session") == scarcity_rank("Builders' session")
    assert scarcity_rank("Some 2027 format") == OTHER_RANK


def test_batch_is_sent_scarcest_first_but_priority_wins_the_slot(world):
    _, _, store = world
    # ARC301 is first priority and takes Tue 10:00. SVS401 Tue 10:30 overlaps, so it
    # falls to its Friday repeat. The batch goes Workshop, Builders, Chalk, Breakout.
    r = router(store, "targets:\n- code: ARC301\n- code: SVS401\n- code: DOP302\n- code: BLD201\n")
    plan = r.plan(held=[])
    assert codes(plan) == ["DOP302", "BLD201-R", "SVS401-R1", "ARC301-R"]
    assert any(s.session_id == "C1" and "overlaps ARC301-R" in s.reason for s in plan.skipped)


def test_never_two_sittings_and_held_target_is_left_alone(world):
    _, _, store = world
    r = router(store, "targets:\n- code: ARC301\n- code: SVS401\n    \n")
    plan = r.plan(held=["A2"])
    assert plan.held_targets == ["ARC301"]
    assert "ARC301" not in "".join(codes(plan))
    # A backup that is another talk's sitting already planned is skipped, not doubled.
    r2 = router(store, "targets:\n- code: SVS401\n- code: SEC201\n  backups: [SVS401]\n")
    p2 = r2.plan(held=[])
    assert sorted(codes(p2)) == ["SEC201", "SVS401"]


def test_overlap_with_held_session_and_meal_and_day_cap(world):
    _, _, store = world
    r = router(store, "targets:\n- code: SVS401\nmeals:\n- {day: Friday, start: '08:30', end: '09:30'}\n")
    plan = r.plan(held=["A1"])  # A1 holds Tue 10:00. C1 overlaps it. C2 overlaps the meal.
    assert plan.batch == [] and plan.exhausted == ["SVS401"]
    reasons = {s.session_id: s.reason for s in plan.skipped}
    assert reasons == {"C1": "overlaps ARC301-R", "C2": "overlaps a meal in the rules"}

    capped = router(store, "max_per_day: 1\ntargets:\n- code: DOP302\n")
    assert "max_per_day 1" in capped.plan(held=["A1"]).skipped[0].reason


def test_bands_unknown_try_unavailable_falls_through_walkup_skipped(world):
    _, _, store = world
    store.apply_sweep(EV, [mk(s.session_id, s.abbreviation, s.type, s.session_time.date,
                              s.session_time.time, s.session_time.minutes or 60,
                              band={"A1": "unavailable", "A2": "someNewBand"}.get(s.session_id),
                              reservable=s.is_reservable)
                           for s in CATALOG], with_abstracts=False)
    plan = router(store, "targets:\n- code: ARC301\n- session_id: K1\n  repeats: false\n").plan(held=[])
    assert codes(plan) == ["ARC301-R1"]          # unknown band value is tried
    reasons = {s.session_id: s.reason for s in plan.skipped}
    assert reasons["A1"] == "full (band unavailable)"
    assert reasons["K1"].startswith("not reservable")


def test_candidates_limit_what_the_watcher_may_book(world):
    _, _, store = world
    r = router(store, "targets:\n- code: ARC301\n- code: SEC201\n")
    plan = r.plan(held=[], candidates=["A3"])
    assert codes(plan) == ["ARC301-R2"] and plan.exhausted == []


def test_prefer_latest_and_pinned_sitting_first(world):
    _, _, store = world
    assert codes(router(store, "targets:\n- code: ARC301\n  prefer: latest\n").plan([])) == ["ARC301-R2"]
    assert codes(router(store, "targets:\n- code: ARC301\n  session_id: A2\n").plan([])) == ["ARC301-R1"]


def test_batch_never_exceeds_quota_left_or_ten(world):
    _, _, store = world
    r = router(store, "targets:\n- code: ARC301\n- code: DOP302\n- code: BLD201\n- code: LAB101\n")
    plan = r.plan(held=[], quota_left=2)
    assert len(plan.batch) == 2 and len(plan.deferred) == 2
    assert codes(plan) == ["DOP302", "LAB101"]


# ---------------------------------------------------------------------- execute and run


def test_full_batch_success_reads_back_and_journals(world):
    fake, client, store = world
    r = router(store, "targets:\n- code: ARC301\n- code: DOP302\n")
    run = run_booking(r, client, store, EV, held=[])
    assert {o.session_id: o.status for o in run.outcomes} == {"A1": "reserved", "W1": "reserved"}
    assert set(run.schedule.reserved) == {"A1", "W1"}
    assert fake.counts[OP] == 1 and fake.counts["GetSchedule"] == 1
    j = store.journal_entries(EV)[0]
    assert j["op"] == OP and j["outcome"] == "success" and "readBack" in j["response"]


def test_partial_failure_falls_back_to_next_sitting_same_run(world):
    fake, client, store = world
    fake.full.add("A1")
    run = run_booking(router(store, "targets:\n- code: ARC301\n- code: DOP302\n"), client, store, EV, held=[])
    statuses = [(o.session_id, o.status) for o in run.outcomes]
    assert statuses == [("W1", "reserved"), ("A1", "full"), ("A2", "reserved")]
    assert set(run.schedule.reserved) == {"W1", "A2"}
    sent = [b for op, b in fake.params if op == OP]
    assert len(sent) == 2                      # A1 never re-sent
    assert store.journal_entries(EV)[1]["outcome"] == "partial"


def test_conflict_is_recorded_and_not_retried(world):
    fake, client, store = world
    fake.schedule.reserved.add("C1")           # held in the portal, unknown to the local view
    run = run_booking(router(store, "targets:\n- code: ARC301\n"), client, store, EV, held=[])
    first = run.outcomes[0]
    assert (first.session_id, first.status, first.conflicts_with) == ("A1", "conflict", ["C1"])
    assert [o.session_id for o in run.outcomes].count("A1") == 1
    assert run.outcomes[1].session_id == "A2" and run.outcomes[1].status == "reserved"


def test_already_scheduled_marks_target_held(world):
    fake, client, store = world
    fake.schedule.reserved.add("A1")
    r = router(store, "targets:\n- code: ARC301\n")
    plan = r.plan(held=[])                     # stale view: does not know A1 is held
    ex = execute(plan, client, store, EV)
    assert ex.outcomes[0].status == "already" and not ex.disagreements
    assert r.plan(held=ex.schedule.reserved).held_targets == ["ARC301"]


def test_unknown_failure_code_is_recorded_and_moved_past(world):
    fake, client, store = world
    fake.refuse["A1"] = "seatHeldByCrew"
    run = run_booking(router(store, "targets:\n- code: ARC301\n"), client, store, EV, held=[])
    o = run.outcomes[0]
    assert (o.status, o.code) == ("refused", "seatHeldByCrew")
    assert run.outcomes[1].session_id == "A2" and run.outcomes[1].status == "reserved"


def test_quota_split_across_two_minutes(world, clock):
    fake, client, store = world
    client.quota.spend(OP, 25)                 # 5 left this minute
    body = "max_per_day: 10\ntargets:\n" + "".join(
        f"- code: {c}\n" for c in ["ARC301", "DOP302", "BLD201", "LAB101", "SEC201", "SVS401"])
    t0 = clock.t
    run = run_booking(router(store, body), client, store, EV, held=[])
    sizes = [len(p.batch) for p in run.plans if p.batch]
    assert sizes == [5, 1]
    assert clock.t - t0 >= 59                  # waited for the window, never overspent
    assert fake.counts[OP] == 2
    assert all(o.status == "reserved" for o in run.outcomes)


def test_409_surfaces_cleanly(world):
    fake, client, store = world
    fake.closed = True
    run = run_booking(router(store, "targets:\n- code: ARC301\n- code: DOP302\n"), client, store, EV, held=[])
    assert run.closed
    assert {o.status for o in run.outcomes} == {"not_sent"}
    assert run.outcomes[0].note == WRITES_CLOSED
    assert fake.counts[OP] == 1 and fake.schedule.reserved == set()
    assert store.journal_entries(EV)[0]["outcome"] == "closed"


def test_read_back_disagreement_is_reported_not_hidden(world):
    fake, client, store = world
    fake.ghost.add("A1")
    run = run_booking(router(store, "targets:\n- code: ARC301\n"), client, store, EV, held=[])
    o = run.outcomes[0]
    assert o.status == "unconfirmed" and "does not list it" in o.note
    assert run.executions[0].disagreements
    assert store.journal_entries(EV)[0]["outcome"] == "disagreement"
    assert len(run.outcomes) == 1              # no second sitting booked while unsure


def test_5xx_on_write_is_read_back_not_retried(world):
    fake, client, store = world
    fake.fail_next(OP, 503)
    run = run_booking(router(store, "targets:\n- code: ARC301\n"), client, store, EV, held=[])
    assert fake.counts[OP] == 1 and fake.counts["GetSchedule"] == 1
    o = run.outcomes[0]
    assert o.status == "refused" and "not in read-back" in o.note
    assert run.executions[0].error.startswith("503")
    assert store.journal_entries(EV)[0]["outcome"] == "error"
