"""Autopilot: the rulebook is the attendee's instructions. Targets already open are booked at start and
after the rulebook changes on disk, and a target with auto_swap swaps on its own, with the checks."""

import os

import httpx
import pytest
from test_watcher import EV, catalog, kinds, mk

from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.store import Store
from reseat.swap import Swap
from reseat.watcher import Watcher

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="file modes and symlinks: macOS and Linux only")


def stack(clock, tmp_path, body, sessions=None, held=(), hooks=None):
    """`hooks` maps an operation to fn(req, send): a dropped connection or a fault around the fake."""
    fake = FakeEventsApi(sessions=sessions or catalog())
    fake.schedule.reserved |= set(held)

    def handle(req):
        op = FakeEventsApi._op_name(req.method, [p for p in req.url.path.split("/") if p])
        hook = (hooks or {}).pop(op, None)
        return hook(req, fake._handle) if hook else fake._handle(req)

    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=httpx.MockTransport(handle))
    store = Store(":memory:")
    path = tmp_path / "rules.yaml"
    path.write_text(body, encoding="utf-8")
    w = Watcher(client, store, R.load(path), EV, clock=clock, sleep=clock.sleep, rules_path=path,
                swapper=lambda p, ok: Swap(client, store, w.rules, EV).run_plan(p, approved=ok))
    events = []
    w.subscribe(events.append)
    store.apply_sweep(EV, list(client.iter_sessions(EV, include_abstracts=False)), False, now=clock() - 60)
    return fake, w, events, path                     # a store that already has the catalog, as on a laptop


def rewrite(path, body):
    path.write_text(body, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))          # a later mtime, as a real save gives


def test_a_target_added_to_the_rulebook_is_booked_on_the_next_sweep_without_a_restart(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    assert fake.counts.get("ReserveSessions", 0) == 0                  # ARC301 is full
    rewrite(path, "targets:\n- code: ARC301\n- code: SEC201\n")         # X1 is open
    clock.sleep(60)
    w.tick()
    assert "X1" in fake.schedule.reserved and fake.counts["ReserveSessions"] == 1
    assert "rules" in kinds(events)
    assert [e for e in events if e.kind == "booked"][0].data["session_id"] == "X1"
    rows = [r for r in w.store.journal_entries(EV) if r["op"] == "watcher.rules"]
    assert rows and rows[0]["outcome"] == "reloaded"


def test_a_broken_rulebook_is_refused_and_the_old_rules_stay(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    rewrite(path, "targets:\n- code: [unclosed\n")
    clock.sleep(60)
    w.tick()
    assert [t.code for t in w.rules.targets] == ["ARC301"]
    assert any(e.kind == "error" and "old rules stay" in e.data["message"] for e in events)
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_a_rulebook_over_its_watch_cap_is_refused(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    rewrite(path, "watch_cap: 1\ntargets:\n- code: ARC301\n- code: SEC201\n")
    clock.sleep(60)
    w.tick()
    assert len(w.rules.targets) == 1 and "X1" not in fake.schedule.reserved


def test_catch_up_books_open_targets_in_rank_order_and_says_nothing_about_clashes(clock, tmp_path):
    sessions = catalog() + [mk("C2", "SVS402", "2026-12-01", "10:30")]   # open, clashes with C1
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SVS401\n- code: SVS402\n", sessions)
    w.tick()
    assert fake.schedule.reserved == {"C1"}                              # rank 1 wins the slot
    assert [e for e in events if e.kind == "not_booked"] == []           # the clash is not news


def test_auto_swap_moves_to_a_higher_ranked_sitting_on_its_own_when_it_frees(clock, tmp_path):
    """The owner's case: hold the open lower-ranked talk, swap up by rank when the full one frees."""
    sessions = [mk("F1", "AIM353", "2026-11-30", "15:00", band="unavailable"),
                mk("O1", "AIM433", "2026-11-30", "15:00", band="veryLimited")]
    body = "targets:\n- {code: AIM353, auto_swap: true}\n- code: AIM433\n"
    fake, w, events, _ = stack(clock, tmp_path, body, sessions)
    w.tick()
    assert fake.schedule.reserved == {"O1"}                              # the open one, booked at start
    fake.set_band("F1", "available")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"F1"}                              # swapped up, no tap
    assert fake.counts["CancelReservation"] == 1
    assert any(e.kind == "swap" and e.data.get("state") == "verified" for e in events)


def test_without_auto_swap_the_higher_sitting_waits_for_approval(clock, tmp_path):
    sessions = [mk("F1", "AIM353", "2026-11-30", "15:00", band="unavailable"),
                mk("O1", "AIM433", "2026-11-30", "15:00", band="veryLimited")]
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: AIM353\n- code: AIM433\n", sessions)
    w.tick()
    fake.set_band("F1", "available")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"O1"} and fake.counts.get("CancelReservation", 0) == 0
    assert [e.kind for e in events].count("proposed") == 1


def test_a_target_cancelled_in_the_official_app_is_booked_again_at_start(clock, tmp_path):
    """Documented: the rulebook is the instruction. To drop a talk, take it out of the rules."""
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    w.tick()
    assert "X1" in fake.schedule.reserved


# ---------------------------------------------------------------- catch-up: each failure path of the reserve

def ops(fake):
    return [op for op, _ in fake.log]


def test_catch_up_met_by_409_is_held_and_booked_once_writes_open(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    fake.closed = True
    w.tick()
    assert "X1" not in fake.schedule.reserved and "writes" in kinds(events)
    assert [e for e in events if e.kind == "not_booked"] == []
    fake.closed = False
    clock.sleep(901)                                                    # past the recheck hold
    w.tick()
    assert "X1" in fake.schedule.reserved                               # queued, not lost
    assert fake.two_sittings == []


def test_catch_up_429_waits_retry_after_once_then_books(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    fake.throttle_next("ReserveSessions", retry_after=3)
    t0 = clock()
    w.tick()
    assert "X1" in fake.schedule.reserved and clock() - t0 >= 3
    assert [s for op, s in fake.log if op == "ReserveSessions"] == [429, 200]


def test_catch_up_5xx_on_the_reserve_is_read_back_not_retried_in_the_tick(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    fake.fail_next("ReserveSessions", 503)
    w.tick()
    log = ops(fake)
    assert log.count("ReserveSessions") == 1 and "GetSchedule" in log[log.index("ReserveSessions"):]
    assert "X1" not in fake.schedule.reserved
    clock.sleep(60)
    w.tick()                                                            # a fresh read showed it not held
    assert "X1" in fake.schedule.reserved and ops(fake).count("ReserveSessions") == 2


@pytest.mark.parametrize("landed", [False, True])
def test_catch_up_dropped_connection_on_the_reserve_is_read_back_never_resent(clock, tmp_path, landed):
    def drop(req, send):
        if landed:
            send(req)                                                   # applied, the answer is lost
        raise httpx.ReadTimeout("timed out", request=req)

    hooks = {}
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n", hooks=hooks)
    hooks["ReserveSessions"] = drop
    w.tick()
    log = ops(fake)
    assert log.count("ReserveSessions") == (1 if landed else 0)        # the fake saw it only if it landed
    assert log.count("GetSchedule") >= 2                                # the sweep read, then the read-back
    assert ("X1" in fake.schedule.reserved) == landed
    clock.sleep(60)
    w.tick()
    assert "X1" in fake.schedule.reserved and fake.two_sittings == []
    assert ops(fake).count("ReserveSessions") == 1                      # landed: never sent again


def test_catch_up_target_full_at_the_reserve_is_an_answer_quiet_and_not_retried(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    fake.full.add("X1")                                                 # the store still says open
    w.tick()
    clock.sleep(60)
    w.tick()
    assert "X1" not in fake.schedule.reserved and ops(fake).count("ReserveSessions") == 1
    assert [e for e in events if e.kind == "not_booked"] == []


def test_catch_up_while_signed_out_sends_nothing_and_runs_after_sign_in(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n")
    w.client.token_provider = lambda: "expired"
    w.tick()
    assert w.read_only and "ReserveSessions" not in ops(fake)
    w.client.token_provider = lambda: fake.token
    clock.sleep(60)
    w.tick()
    assert not w.read_only and "X1" in fake.schedule.reserved          # the catch-up was not used up


def test_catch_up_with_a_held_session_missing_from_the_catalog_books_nothing(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: SEC201\n", held=["GONE"])
    w.tick()
    clock.sleep(60)
    w.tick()
    assert "ReserveSessions" not in ops(fake) and fake.schedule.reserved == {"GONE"}
    assert sum("not in the local catalog" in e.data.get("message", "") for e in events) == 1


def test_catch_up_respects_max_per_day(clock, tmp_path):
    sessions = catalog() + [mk("D2", "DOP202", "2026-12-01", "15:00")]   # C1 and D2, both Tue, both open
    fake, w, events, _ = stack(clock, tmp_path, "max_per_day: 1\ntargets:\n- code: SVS401\n- code: DOP202\n",
                               sessions)
    w.tick()
    assert fake.schedule.reserved == {"C1"}


def test_catch_up_never_holds_two_sittings_of_one_talk(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    fake.set_band("A1", "available")
    fake.set_band("A2", "available")
    both = list(w.client.iter_sessions(EV, include_abstracts=False))
    w.store.apply_sweep(EV, both, False, now=clock() - 30)              # both open before the start
    w.tick()
    clock.sleep(60)
    w.tick()
    assert len(fake.schedule.reserved & {"A1", "A2"}) == 1 and fake.two_sittings == []


# ---------------------------------------------------------------- reloads never cancel

AIM = [mk("F1", "AIM353", "2026-11-30", "15:00", band="unavailable"),
       mk("O1", "AIM433", "2026-11-30", "15:00", band="veryLimited")]


def test_a_reload_that_removes_a_held_target_never_cancels_it(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- {code: AIM353, auto_swap: true}\n"
                                  "- code: AIM433\n", list(AIM))
    w.tick()
    assert fake.schedule.reserved == {"O1"}
    rewrite(path, "targets:\n- {code: AIM353, auto_swap: true}\n")      # AIM433 dropped, still held
    fake.set_band("F1", "available")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"O1"} and fake.counts.get("CancelReservation", 0) == 0
    assert w.pending() == []                                            # never proposed: not a covered seat


def test_a_pending_proposal_for_a_target_the_reload_removed_does_not_cancel(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: AIM353\n- code: AIM433\n", list(AIM))
    w.tick()
    fake.set_band("F1", "available")
    clock.sleep(60)
    w.tick()
    (p,) = w.pending()
    rewrite(path, "targets:\n- code: AIM433\n")                         # AIM353 is no longer wanted
    clock.sleep(60)
    w.tick()
    try:
        w.approve(p.plan_id)
    except Exception:  # noqa: BLE001  a refusal is fine, a cancel is not
        pass
    assert fake.schedule.reserved == {"O1"} and fake.counts.get("CancelReservation", 0) == 0


def test_a_rulebook_deleted_mid_run_keeps_the_old_rules_on_every_os(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    path.unlink()
    clock.sleep(60)
    w.tick()
    assert [t.code for t in w.rules.targets] == ["ARC301"] and "ReserveSessions" not in ops(fake)
    assert fake.counts.get("CancelReservation", 0) == 0


@POSIX_ONLY
def test_a_rulebook_deleted_or_unreadable_mid_run_keeps_the_old_rules(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    path.unlink()
    clock.sleep(60)
    w.tick()
    assert [t.code for t in w.rules.targets] == ["ARC301"] and "ReserveSessions" not in ops(fake)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return                                                          # root reads any file
    path.write_text("targets:\n- code: SEC201\n", encoding="utf-8")
    path.chmod(0)
    try:
        clock.sleep(60)
        w.tick()
    finally:
        path.chmod(0o600)
    assert [t.code for t in w.rules.targets] == ["ARC301"] and "X1" not in fake.schedule.reserved
    assert any(e.kind == "error" and "old rules stay" in e.data["message"] for e in events)


def test_auto_swap_whose_fallback_check_fails_never_cancels(clock, tmp_path):
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- {code: AIM353, auto_swap: true}\n"
                               "- code: AIM433\n", list(AIM))
    w.tick()
    assert fake.schedule.reserved == {"O1"}
    fake.set_band("O1", "unavailable")                                  # no way back to O1
    fake.set_band("F1", "available")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"O1"} and fake.counts.get("CancelReservation", 0) == 0
    assert any(e.kind == "swap" and e.data.get("state") == "failed" for e in events)


@POSIX_ONLY
def test_a_symlinked_or_group_writable_rules_file_is_refused_on_reload(clock, tmp_path):
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    real = tmp_path / "other.yaml"
    real.write_text("targets:\n- code: SEC201\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(real)
    clock.sleep(60)
    w.tick()
    assert [t.code for t in w.rules.targets] == ["ARC301"] and "X1" not in fake.schedule.reserved
    assert any("symbolic link" in e.data.get("message", "") for e in events if e.kind == "error")
    path.unlink()
    path.write_text("targets:\n- code: SEC201\n", encoding="utf-8")
    os.chmod(path, 0o664)
    clock.sleep(60)
    w.tick()
    assert [t.code for t in w.rules.targets] == ["ARC301"] and "X1" not in fake.schedule.reserved


def test_a_rules_change_is_pushed_and_emailed_with_the_autopilot_count(clock, tmp_path):
    from reseat import push, sns
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: ARC301\n")
    w.tick()
    rewrite(path, "targets:\n- {code: ARC301, auto_swap: true}\n- code: SEC201\n")
    clock.sleep(60)
    w.tick()
    [ev] = [e for e in events if e.kind == "rules"]
    title, body = push.message(ev, str)
    assert title == "Rules changed" and "2 targets, 1 on autopilot" in body
    assert "rules" in sns.KINDS and push.CATEGORY["rules"] == "health"


def test_open_targets_in_different_slots_go_in_one_reserve_call(clock, tmp_path):
    """The live proof's log line: two open ranked targets booked in one ReserveSessions, read back once."""
    sessions = [mk("O1", "AIM433", "2026-11-30", "15:00", band="veryLimited"),
                mk("O2", "INV510-R1", "2026-12-04", "09:00", band="available")]
    fake, w, events, _ = stack(clock, tmp_path, "targets:\n- code: AIM433\n- code: INV510-R1\n", sessions)
    reads = fake.counts.get("GetSchedule", 0)
    w.tick()
    assert fake.schedule.reserved == {"O1", "O2"} and fake.counts["ReserveSessions"] == 1
    assert fake.counts["GetSchedule"] - reads == 2                     # the sweep's read, then the read-back
    assert sorted(e.data["session_id"] for e in events if e.kind == "booked") == ["O1", "O2"]


def test_a_rules_edit_while_running_starts_an_auto_swap_there_and_back(clock, tmp_path):
    """The 26 October live proof: no seat frees. A reload ranks an open auto_swap target above a held
    overlapping one, the catch-up proposes, the swap runs with its checks and reads back. Then back."""
    sessions = [mk("B1", "INV508-R1", "2026-12-04", "09:00"), mk("A1", "INV510-R1", "2026-12-04", "09:00")]
    fake, w, events, path = stack(clock, tmp_path, "targets:\n- code: INV510-R1\n", sessions, held=("A1",))
    w.tick()
    assert fake.schedule.reserved == {"A1"} and fake.counts.get("CancelReservation", 0) == 0
    rewrite(path, "targets:\n- {code: INV508-R1, auto_swap: true}\n- code: INV510-R1\n")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"B1"} and fake.counts["CancelReservation"] == 1
    rewrite(path, "targets:\n- {code: INV510-R1, auto_swap: true}\n- {code: INV508-R1, auto_swap: true}\n")
    clock.sleep(60)
    w.tick()
    assert fake.schedule.reserved == {"A1"} and fake.counts["CancelReservation"] == 2
    assert fake.counts["ReserveSessions"] == 2                         # one reserve per swap, nothing resent
    assert [e.data["state"] for e in events if e.kind == "swap"] == ["verified", "verified"]
    rows = [(r["op"], r["outcome"]) for r in reversed(w.store.journal_entries(EV))]   # oldest first
    swaps = [o for o, _ in rows if o.startswith(("swap.", "watcher.rules"))]
    assert swaps.count("watcher.rules") == 2 and swaps.count("swap.verified") == 2
    assert swaps.index("swap.checked") < swaps.index("swap.cancel")    # fallback checked before the cancel
