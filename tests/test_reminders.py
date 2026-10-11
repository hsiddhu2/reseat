"""On-site reminders: the morning briefing, leave in 10 minutes, leave now, and a wanted session soon.
Each is sent once, at the right Las Vegas time, with codes and titles only."""

import pathlib
from datetime import datetime

from reseat import guard, push
from reseat import rules as R
from reseat.campus import VEGAS
from reseat.models import Session
from reseat.reminders import Reminders, leave_time
from reseat.watcher import WatchEvent


def mk(sid, abbr, date="2026-12-01", time_="10:00", venue="MGM Grand", typ="Breakout session"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": typ, "venue": venue,
        "room": "Level 1 | Grand 117", "isReservable": True, "seatAvailability": "available",
        "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


def at(date, hm):
    return datetime.fromisoformat(f"{date}T{hm}").replace(tzinfo=VEGAS).timestamp()


HELD = [mk("h1", "ARC301", time_="09:00", venue="Wynn"),
        mk("h2", "SVS401", time_="13:00", venue="MGM Grand")]
RULES = R.parse("targets:\n- code: ARC301\n")


def run(now, held=HELD, wanted=(), waiting=0, rem=None):
    if rem is None:
        log = []
        rem = Reminders(lambda kind, **d: log.append(WatchEvent(kind, 0, d)))
        rem.log = log
    before = len(rem.log)
    blocks, _ = guard.leave_blocks(held, RULES)
    rem.check(now, list(held), blocks, list(wanted), waiting)
    return rem, rem.log[before:]


def leave_of(code, held=HELD):
    blocks, _ = guard.leave_blocks(held, RULES)
    return leave_time(next(b for b in blocks if b.code == code))


def kinds(evs):
    return [e.kind for e in evs]


def test_the_morning_briefing_lists_the_day_once_between_six_and_ten():
    _, early = run(at("2026-12-01", "05:59"))
    assert "briefing" not in kinds(early)
    rem, got = run(at("2026-12-01", "07:00"), waiting=1)
    [ev] = [e for e in got if e.kind == "briefing"]
    title, body = push.message(ev, str)
    assert title == "Today: 2 sessions"
    assert body.startswith("First: ARC301 at 09:00, Wynn. Leave at ") and "Then SVS401 13:00." in body
    assert body.endswith("1 swap waits for you.") and "h1" not in title + body
    _, again = run(at("2026-12-01", "08:00"), rem=rem)
    assert "briefing" not in kinds(again)
    _, late = run(at("2026-12-02", "10:00"))
    assert "briefing" not in kinds(late)


def test_a_free_day_and_a_day_off_the_conference():
    _, got = run(at("2026-12-02", "07:00"))
    [ev] = [e for e in got if e.kind == "briefing"]
    assert push.message(ev, str) == ("Today: a free day", "Nothing held for the rest of today.")
    _, off = run(at("2026-12-06", "07:00"))
    assert "briefing" not in kinds(off)


def test_leave_in_ten_minutes_then_leave_now_each_once():
    t = leave_of("ARC301")
    rem, got = run(t - 600)
    [soon] = [e for e in got if e.kind == "leave_soon"]
    title, body = push.message(soon, str)
    assert title == "Leave in 10 minutes for ARC301"
    assert body.startswith(f"Leave at {datetime.fromtimestamp(t, VEGAS):%H:%M}.")
    _, again = run(t - 300, rem=rem)
    assert "leave_soon" not in kinds(again)
    _, now = run(t + 10, rem=rem)
    assert kinds(now).count("leave") == 1 and "leave_soon" not in kinds(now)
    _, after = run(t + 20, rem=rem)
    assert "leave" not in kinds(after)


def test_no_ten_minute_warning_when_it_is_already_time_to_leave():
    t = leave_of("ARC301")
    rem, got = run(t + 5)
    assert "leave" in kinds(got) and "leave_soon" not in kinds(got)
    _, later = run(t + 30, rem=rem)
    assert "leave_soon" not in kinds(later)


def test_a_wanted_session_soon_says_whether_to_queue_once():
    want = mk("w1", "DOP302", date="2026-12-01", time_="15:00", venue="Encore", typ="Chalk talk")
    rem, early = run(at("2026-12-01", "14:39"), wanted=[want])
    assert "wanted_soon" not in kinds(early)
    _, got = run(at("2026-12-01", "14:41"), wanted=[want], rem=rem)
    [ev] = [e for e in got if e.kind == "wanted_soon"]
    title, body = push.message(ev, str)
    assert title == "DOP302 starts at 15:00"
    assert body.startswith("Title DOP302, Encore. You do not hold a seat. ") and "w1" not in title + body
    _, again = run(at("2026-12-01", "14:50"), wanted=[want], rem=rem)
    assert "wanted_soon" not in kinds(again)


def test_no_wanted_reminder_when_another_sitting_of_the_talk_is_held():
    other = mk("a2", "ARC301-R1", date="2026-12-01", time_="15:00")
    held = [mk("h1", "ARC301-R", time_="09:00")]
    _, got = run(at("2026-12-01", "14:45"), held=held, wanted=[other])
    assert "wanted_soon" not in kinds(got)


def test_on_site_pushes_open_the_today_view():
    for kind in ("briefing", "leave_soon", "leave", "wanted_soon"):
        assert push.page_for(WatchEvent(kind, 0, {})) == "/today"


def bare(sid, abbr=None, venue=None, time_=None, date="2026-12-01"):
    st = {"date": date, "time": time_, "length": "60"} if time_ else None
    return Session.model_validate({"sessionId": sid, "abbreviation": abbr, "title": "Untitled talk",
                                   "type": "Chalk talk", "venue": venue, "sessionTime": st})


def test_the_briefing_without_a_leave_block_and_with_two_swaps_waiting():
    log = []
    rem = Reminders(lambda kind, **d: log.append(WatchEvent(kind, 0, d)))
    rem.check(at("2026-12-01", "07:00"), [HELD[0]], [], [], 2)
    [ev] = [e for e in log if e.kind == "briefing"]
    title, body = push.message(ev, str)
    assert title == "Today: 1 session"
    assert body == "First: ARC301 at 09:00, Wynn. 2 swaps wait for you."


def test_a_wanted_session_with_no_code_or_venue_still_reads_well():
    want = bare("w9", time_="15:00")
    _, got = run(at("2026-12-01", "14:45"), wanted=[want])
    [ev] = [e for e in got if e.kind == "wanted_soon"]
    title, body = push.message(ev, str)
    assert title == "A session you want starts at 15:00"
    assert body.startswith("Untitled talk. You do not hold a seat.") and "None" not in title + body
    assert "w9" not in title + body


def test_a_wanted_session_without_a_time_is_skipped():
    _, got = run(at("2026-12-01", "14:45"), wanted=[bare("w8", "DOP302")])
    assert "wanted_soon" not in kinds(got)


def test_a_held_session_without_a_time_is_left_out_of_the_briefing():
    log = []
    rem = Reminders(lambda kind, **d: log.append(WatchEvent(kind, 0, d)))
    rem.check(at("2026-12-02", "07:00"), [bare("h9", "XYZ101", date="2026-12-03")], [], [], 0)
    [ev] = [e for e in log if e.kind == "briefing"]
    assert push.message(ev, str) == ("Today: a free day", "Nothing held for the rest of today.")


def test_each_reminder_is_sent_once_across_refreshes_with_fresh_objects():
    t = leave_of("ARC301")
    want = mk("w1", "DOP302", time_="09:10")
    rem, got = run(t + 5, wanted=[want])
    assert kinds(got).count("leave") == 1
    for _ in range(3):
        fresh = [mk("h1", "ARC301", time_="09:00", venue="Wynn"), mk("h2", "SVS401", time_="13:00")]
        _, again = run(t + 30, held=fresh, wanted=[mk("w1", "DOP302", time_="09:10")], rem=rem)
        assert not {"leave", "leave_soon", "wanted_soon", "briefing"} & set(kinds(again))


def test_no_leave_now_once_the_session_has_started():
    start = at("2026-12-01", "09:00")
    _, got = run(start + 1)
    assert "leave" not in kinds(got) and "leave_soon" not in kinds(got)


def test_reminders_make_no_api_calls_and_no_writes():
    import reseat.reminders as M
    names = set(vars(M))
    assert not {"httpx", "client", "EventsClient", "engine", "Store"} & names
    src = pathlib.Path(M.__file__).read_text()
    for word in ("reserve", "cancel", "favorite", "httpx", "EventsClient"):
        assert word not in src


# ---- the Engineer review's cases


def test_no_briefing_while_the_api_is_down_then_it_goes_out_once_back():
    rem, got = run(at("2026-12-01", "07:00"))
    assert "briefing" in kinds(got)
    log = []
    rem2 = Reminders(lambda kind, **d: log.append(kind))
    blocks, _ = guard.leave_blocks(HELD, RULES)
    rem2.check(at("2026-12-01", "07:00"), HELD, blocks, [], 0, fresh=False)
    assert "briefing" not in log
    rem2.check(at("2026-12-01", "07:01"), HELD, blocks, [], 0, fresh=True)
    assert log.count("briefing") == 1


def test_the_briefing_leaves_out_sessions_already_started():
    _, got = run(at("2026-12-01", "09:30"))
    [ev] = [e for e in got if e.kind == "briefing"]
    title, body = push.message(ev, str)
    assert title == "Today: 1 session" and body.startswith("First: SVS401 at 13:00") and "ARC301" not in body


def test_a_leave_time_that_moves_later_after_leave_now_does_not_send_leave_in_ten():
    held = [mk("h1", "ARC301", time_="09:00", venue="Wynn")]
    t = leave_of("ARC301", held)
    rem, got = run(t + 5, held=held)
    assert "leave" in kinds(got)
    moved = [mk("h1", "ARC301", time_="09:30", venue="Wynn")]       # the session moves later
    _, later = run(leave_of("ARC301", moved) - 300, held=moved, rem=rem)
    assert "leave_soon" not in kinds(later)


def test_one_wanted_reminder_per_talk_and_none_for_a_clashing_sitting():
    a = mk("w1", "DOP302-R", time_="15:00", venue="Encore")
    b = mk("w2", "DOP302-R1", time_="15:05", venue="Wynn")
    clash = mk("w3", "SEC201", time_="13:15")                        # overlaps SVS401 13:00-14:00
    _, got = run(at("2026-12-01", "14:50"), wanted=[a, b])
    assert kinds(got).count("wanted_soon") == 1
    _, none = run(at("2026-12-01", "13:00"), wanted=[clash])
    assert "wanted_soon" not in kinds(none)


def test_reminder_events_carry_no_session_id():
    want = mk("w1", "DOP302", time_="15:00")
    _, got = run(at("2026-12-01", "14:45"), wanted=[want])
    for ev in got:
        assert "session_id" not in ev.data and "w1" not in str(ev.data)
