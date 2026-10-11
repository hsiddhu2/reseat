"""The change tracker: what changed on the week between two looks, with no API call."""

from datetime import datetime

from reseat.changes import MARK_TTL, Tracker
from reseat.models import Session


def mk(sid, abbr, time_="10:00", room="Level 1 | Grand 117", date="2026-12-01"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": room, "isReservable": True, "seatAvailability": "available",
        "sessionTime": {"date": date, "time": time_, "length": "60"}})


def kinds(cs):
    return [(c.kind, c.place.code) for c in cs]


def test_the_first_look_is_a_baseline():
    t = Tracker()
    assert t.look(0, [mk("a", "A1")], []) == [] and list(t.log) == []


def test_booked_released_swapped_moved_proposed_and_removed():
    t = Tracker()
    t.look(0, [mk("a", "A1"), mk("x", "X1", "15:00")], [])
    assert kinds(t.look(1, [mk("a", "A1"), mk("x", "X1", "15:00")], [mk("b", "B1")])) == [("proposed", "B1")]
    assert kinds(t.look(2, [mk("b", "B1"), mk("x", "X1", "15:00")], [])) == [("released", "A1"),
                                                                             ("swapped_in", "B1")]
    assert kinds(t.look(3, [mk("b", "B1"), mk("x", "X1", "15:00"), mk("n", "N1", "17:00")], [])) == [
        ("new", "N1")]
    [moved] = t.look(4, [mk("b", "B1"), mk("x", "X1", "15:00", room="Level 3 | Premier 319"),
                         mk("n", "N1", "17:00")], [])
    assert moved.kind == "moved" and "Level 1 | Grand 117" in moved.before and "Premier 319" in moved.text()
    t.removed.add("n")
    assert kinds(t.look(5, [mk("b", "B1"), mk("x", "X1", "15:00", room="Level 3 | Premier 319")], [])) == [
        ("removed", "N1")]


def test_a_new_seat_that_overlaps_a_released_one_is_a_swap_and_times_are_vegas():
    t = Tracker()
    t.look(0, [mk("a", "A1", "13:00")], [])
    [rel, sw] = t.look(1, [mk("b", "B1", "13:00")], [])
    assert (rel.kind, sw.kind) == ("released", "swapped_in")
    assert sw.place.start.hour == 13
    assert sw.text() == "B1 swapped in, Tue 13:00, MGM Grand, Level 1 | Grand 117"


def test_changes_are_recent_for_fifteen_minutes():
    t = Tracker()
    t.look(0, [], [])
    t.look(100, [mk("a", "A1")], [])
    assert len(t.recent(100 + MARK_TTL - 1)) == 1 and t.recent(100 + MARK_TTL) == []
    assert isinstance(t.log[0].place.start, datetime)


# ---- QA: the remaining branches of Tracker._diff and the demo's net and before/after


def test_a_held_session_aws_removed_but_still_on_the_schedule_is_removed():
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    t.removed.add("a")
    assert kinds(t.look(1, [mk("a", "A1")], [])) == [("removed", "A1")]
    assert t.removed == set()                            # the removed set is cleared after each look


def test_a_removed_session_that_was_only_proposed_is_not_reported_removed():
    t = Tracker()
    t.look(0, [], [mk("b", "B1")])
    t.removed.add("b")
    assert t.look(1, [], [mk("b", "B1")]) == []


def test_a_proposal_that_goes_away_is_no_change_and_leaves_no_ghost():
    t = Tracker()
    t.look(0, [mk("a", "A1")], [mk("b", "B1", "15:00")])
    assert t.look(1, [mk("a", "A1")], []) == []


def test_a_held_seat_turned_proposal_and_an_unchanged_proposal_are_no_change():
    t = Tracker()
    t.look(0, [], [mk("b", "B1")])
    assert t.look(1, [], [mk("b", "B1", "16:00")]) == []    # proposals that move are not labelled


def test_a_moved_time_is_a_move_and_a_session_without_a_time_is_skipped():
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    [mv] = t.look(1, [mk("a", "A1", "11:00")], [])
    assert mv.kind == "moved" and mv.before.startswith("Tue 10:00")
    untimed = Session.model_validate({"sessionId": "z", "abbreviation": "Z1", "title": "Z"})
    assert t.look(2, [mk("a", "A1", "11:00"), untimed], []) == []


def test_a_new_seat_that_does_not_overlap_a_released_one_is_new_not_swapped():
    t = Tracker()
    t.look(0, [mk("a", "A1", "09:00")], [])
    assert kinds(t.look(1, [mk("b", "B1", "15:00")], [])) == [("released", "A1"), ("new", "B1")]


def _places(t):
    return dict(t.prev or {})


def test_net_drops_a_release_rebooked_in_the_same_run():
    from reseat.demo import _net
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    before = _places(t)
    t.look(1, [], [])
    t.look(2, [mk("a", "A1")], [])
    log = list(t.log)
    assert [c.kind for c in log] == ["released", "new"]
    assert _net(before, _places(t), log) == []


def test_net_drops_a_proposal_that_was_approved_and_rolled_back():
    from reseat.demo import _net
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    before = _places(t)
    t.look(1, [mk("a", "A1")], [mk("b", "B1", "15:00")])
    t.look(2, [mk("a", "A1"), mk("b", "B1", "15:00")], [])
    t.look(3, [mk("a", "A1")], [])
    assert _net(before, _places(t), list(t.log)) == []


def test_net_keeps_one_line_per_session_the_latest():
    from reseat.demo import _net
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    before = _places(t)
    t.look(1, [mk("a", "A1", "11:00")], [])
    t.look(2, [mk("a", "A1", "12:00")], [])
    [mv] = _net(before, _places(t), list(t.log))
    assert mv.kind == "moved" and mv.place.start.hour == 12


def test_net_keeps_removed_and_released_that_stay_gone():
    from reseat.demo import _net
    t = Tracker()
    t.look(0, [mk("a", "A1"), mk("r", "R1", "15:00")], [])
    before = _places(t)
    t.removed.add("r")
    t.look(1, [], [])
    assert {(c.kind, c.place.code) for c in _net(before, _places(t), list(t.log))} == {
        ("released", "A1"), ("removed", "R1")}


def test_before_after_shows_a_ghost_row_for_what_left_and_marks_a_move_on_both_sides():
    from reseat.demo import _before_after, _net
    t = Tracker()
    t.look(0, [mk("a", "A1"), mk("m", "M1", "15:00")], [])
    before = _places(t)
    t.look(1, [mk("m", "M1", "15:00", room="Level 3 | Premier 319")], [])
    days = _before_after(before, _places(t), _net(before, _places(t), list(t.log)))
    [day] = days
    assert day["label"] == "Tuesday 1 December"
    b = {r["code"]: r for r in day["before"]}
    a = {r["code"]: r for r in day["after"]}
    assert b["A1"]["mark"]["kind"] == "released" and b["M1"]["mark"]["kind"] == "moved"
    assert a["A1"]["state"] == "gone" and a["A1"]["mark"]["label"] == "RELEASED"
    assert a["M1"]["mark"]["kind"] == "moved" and a["M1"]["state"] == "held"
    assert all("m" != r["code"] and "a" != r["code"] for r in day["before"] + day["after"])  # codes only


def test_before_after_does_not_mark_a_new_seat_on_the_before_side():
    from reseat.demo import _before_after, _net
    t = Tracker()
    t.look(0, [mk("a", "A1")], [])
    before = _places(t)
    t.look(1, [mk("a", "A1"), mk("n", "N1", "15:00")], [])
    [day] = _before_after(before, _places(t), _net(before, _places(t), list(t.log)))
    assert all(r["mark"] is None for r in day["before"])
    assert [r["code"] for r in day["after"] if r["mark"]] == ["N1"]
    assert _before_after(before, before, []) == []


def test_net_moves_to_another_day_show_both_days():
    from reseat.demo import _before_after, _net
    t = Tracker()
    t.look(0, [mk("m", "M1", "15:00")], [])
    before = _places(t)
    t.look(1, [mk("m", "M1", "15:00", date="2026-12-03")], [])
    net = _net(before, _places(t), list(t.log))
    assert [c.kind for c in net] == ["moved"]
    assert [d["label"] for d in _before_after(before, _places(t), net)] == [
        "Tuesday 1 December", "Thursday 3 December"]
