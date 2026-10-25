"""Book offers: new sessions and seats that opened on favorites, booked only on the attendee's tap."""

import threading
import time

import httpx
import pytest

from reseat import push as P
from reseat import rules as R
from reseat import serve as S
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.offers import TAPPED
from reseat.store import Store
from reseat.watcher import OFFERS_PER_TICK, PROPOSAL_TTL, ApprovalFailed, Watcher

EV = "reinvent2026"
RULES = "targets:\n- code: ARC301\n"


def mk(sid, abbr, time_="10:00", band="available", date="2026-12-02"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Chalk talk",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True, "seatAvailability": band,
        "sessionTime": {"date": date, "time": time_, "length": "60"}})


class Env:
    """Holds H1 (SVS401, Wed 10:00). Target ARC301 (A1) is full. F1 is a favorite, full. Baseline done."""

    def __init__(self, clock):
        self.clock = clock
        self.fake = FakeEventsApi(sessions=[mk("H1", "SVS401"), mk("A1", "ARC301", "14:00", "unavailable"),
                                            mk("F1", "DOP302", "16:00", "unavailable")])
        self.fake.schedule.reserved.add("H1")
        self.fake.schedule.favorites |= {"F1", "A1"}
        self.hooks = {}            # op -> fn(req, send): a dropped connection or a fault around the fake
        self.sent = {}             # op -> requests sent, dropped ones included
        self.client = EventsClient(token_provider=lambda: self.fake.token, quota=QuotaTracker(clock=clock),
                                   sleep=clock.sleep, transport=httpx.MockTransport(self._handle))
        self.store = Store(":memory:")
        self.w = Watcher(self.client, self.store, R.parse(RULES), EV, clock=clock, sleep=clock.sleep)
        self.events = []
        self.w.subscribe(self.events.append)
        self.w.tick()
        clock.sleep(60)

    def _handle(self, req):
        op = FakeEventsApi._op_name(req.method, [p for p in req.url.path.split("/") if p])
        self.sent[op] = self.sent.get(op, 0) + 1
        hook = self.hooks.pop(op, None)
        return hook(req, self.fake._handle) if hook else self.fake._handle(req)

    def kinds(self, kind):
        return [e for e in self.events if e.kind == kind]

    def offer_for(self, sid):
        return next(o for o in self.w.pending_offers() if o.session_id == sid)


@pytest.fixture
def env(clock):
    return Env(clock)


def test_a_new_open_session_is_offered_and_booked_only_on_the_tap(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    assert env.fake.counts.get("ReserveSessions", 0) == 0          # offered, not booked
    [ev] = env.kinds("offer")
    assert ev.data["reason"] == "added" and ev.data["code"] == "NEW101"
    res = env.w.approve(env.offer_for("N1").plan_id)
    assert res.state == "booked" and "N1" in env.fake.schedule.reserved and "N1" in res.held_now
    assert env.fake.counts["ReserveSessions"] == 1
    ops = [r["op"] for r in env.store.journal_entries(EV)]
    assert ops[0] == "ReserveSessions"                             # journaled with its read-back
    assert env.kinds("booked")[-1].data["target"] == TAPPED
    assert env.w.pending_offers() == []                            # one use


def test_a_plan_id_books_once(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.w.approve(pid)
    assert env.w.approve(pid) is None and env.fake.counts["ReserveSessions"] == 1


def test_a_new_session_that_clashes_or_repeats_a_held_talk_is_not_offered(env):
    env.fake.add_session(mk("N1", "NEW101", "10:30"))              # overlaps H1
    env.fake.add_session(mk("N2", "SVS401-R1", "18:00"))           # a second sitting of the held talk
    env.w.tick()
    assert env.kinds("offer") == []
    [summary] = env.kinds("new_sessions")
    assert summary.data["count"] == 2 and summary.data["offered"] == 0


def test_a_full_new_session_is_counted_but_not_offered(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00", band="unavailable"))
    env.w.tick()
    assert env.kinds("offer") == [] and env.kinds("new_sessions")[0].data["count"] == 1


def test_a_seat_opening_on_a_favorite_is_offered_but_not_on_other_sessions_or_targets(env):
    env.fake.add_session(mk("O1", "OTH101", "18:00", band="unavailable"))
    env.w.tick()
    env.events.clear()
    env.fake.set_band("F1", "veryLimited")                         # favorite
    env.fake.set_band("O1", "available")                           # not a favorite
    env.fake.set_band("A1", "limited")                             # a target: the rules handle it
    env.clock.sleep(60)
    env.w.tick()
    assert [e.data["session_id"] for e in env.kinds("offer")] == ["F1"]
    assert env.kinds("offer")[0].data["reason"] == "favorite"
    assert "A1" in env.fake.schedule.reserved                      # the rules booked the target themselves


def test_book_reads_the_seat_first_and_sends_nothing_when_it_filled(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    env.fake.set_band("N1", "unavailable")
    res = env.w.approve(env.offer_for("N1").plan_id)
    assert res.state == "refused" and env.fake.counts.get("ReserveSessions", 0) == 0
    assert env.kinds("not_booked")[-1].data["session_id"] == "N1"


def test_book_rechecks_against_a_seat_held_since_the_offer(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    env.fake.add_session(mk("N9", "LAT101", "12:30"))
    env.fake.schedule.reserved.add("N9")                           # booked in the official app meanwhile
    env.store.update_session(EV, mk("N9", "LAT101", "12:30"))
    res = env.w.approve(env.offer_for("N1").plan_id)
    assert res.state == "refused" and "overlaps" in res.reasons[0]
    assert env.fake.counts.get("ReserveSessions", 0) == 0


def test_reserve_answering_session_full_says_so_and_does_not_retry(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    env.fake.full.add("N1")
    res = env.w.approve(env.offer_for("N1").plan_id)
    assert res.state == "full" and env.fake.counts["ReserveSessions"] == 1
    assert env.kinds("not_booked")[-1].data["reason"] == "full"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_failed_check_sends_nothing_and_keeps_the_offer(env, status):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.fake.fail_next("GetSession", status, times=3)
    with pytest.raises(ApprovalFailed, match="Nothing was sent"):
        env.w.approve(pid)
    assert env.fake.counts.get("ReserveSessions", 0) == 0 and env.w.is_pending(pid)


def test_a_409_on_the_reserve_holds_writes_and_keeps_the_offer(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.fake.closed = True
    res = env.w.approve(pid)
    assert res.state == "closed" and env.w.is_pending(pid)
    assert env.kinds("writes")[-1].data["state"] == "closed"
    with pytest.raises(ApprovalFailed, match="closed"):
        env.w.approve(pid)                                         # held: nothing more is sent
    assert env.fake.counts["ReserveSessions"] == 1


def test_signed_out_keeps_the_offer_and_sends_nothing(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    pid = env.offer_for("N1").plan_id
    env.w.read_only = True
    with pytest.raises(ApprovalFailed, match="Sign in"):
        env.w.approve(pid)
    assert env.w.is_pending(pid) and env.fake.counts.get("ReserveSessions", 0) == 0


def test_offers_expire_and_dismiss_takes_them(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.fake.add_session(mk("N2", "NEW201", "13:00"))
    env.w.tick()
    one, two = env.offer_for("N1").plan_id, env.offer_for("N2").plan_id
    assert env.w.take(one) is not None and not env.w.is_pending(one)
    env.clock.sleep(PROPOSAL_TTL + 1)
    assert env.w.approve(two) is None and env.fake.counts.get("ReserveSessions", 0) == 0


def test_many_new_sessions_give_a_few_offers_and_one_summary(env):
    for i in range(OFFERS_PER_TICK + 3):
        env.fake.add_session(mk(f"N{i}", f"NEW{i}01", date="2026-12-03", time_=f"{8 + i:02d}:00"))
    env.w.tick()
    assert len(env.kinds("offer")) == OFFERS_PER_TICK
    [summary] = env.kinds("new_sessions")
    assert (summary.data["count"], summary.data["offered"]) == (OFFERS_PER_TICK + 3, OFFERS_PER_TICK)


def test_offer_push_has_book_buttons_and_no_session_id(env):
    env.fake.add_session(mk("N1", "NEW101", "12:00"))
    env.w.tick()
    [ev] = env.kinds("offer")
    title, body = P.message(ev, lambda sid: "NEW101 Title NEW101" if sid == "N1" else sid)
    assert title == "New session: NEW101" and "N1," not in body and "NEW101 Title NEW101" in body
    buttons = P.book_actions("topic-x", ev.data["plan_id"])
    assert "http, Book," in buttons and f"body=approve {ev.data['plan_id']}" in buttons
    assert "Dismiss" in buttons and "topic-x" not in buttons           # posts to the derived reply topic
    assert P.page_for(ev) == "/approve" and P.CATEGORY["offer"] == "bookings"


# ---- every failure path the Book tap can hit, against the fake


def offered(env, sid="N1", code="NEW101", time_="12:00"):
    env.fake.add_session(mk(sid, code, time_))
    env.w.tick()
    return env.offer_for(sid).plan_id


def dropped(req, send):
    raise httpx.ConnectError("Connection reset", request=req)


def landed_then_dropped(req, send):
    send(req)                                                      # the server applied it, the answer is lost
    raise httpx.ReadTimeout("timed out", request=req)


def reserve_rows(env):
    return [r for r in env.store.journal_entries(EV) if r["op"] == "ReserveSessions"]


@pytest.mark.parametrize("op", ["GetSchedule", "GetSession"])
@pytest.mark.parametrize("fault", [403, 500, 503, "dropped"])
def test_a_failed_or_dropped_check_on_either_read_sends_nothing_and_keeps_the_offer(env, op, fault):
    pid = offered(env)
    if fault == "dropped":
        env.hooks[op] = dropped
    else:
        env.fake.fail_next(op, fault)
    with pytest.raises(ApprovalFailed, match="Nothing was sent"):
        env.w.approve(pid)
    assert env.fake.counts.get("ReserveSessions", 0) == 0 and env.w.is_pending(pid)
    assert reserve_rows(env) == []


@pytest.mark.parametrize("op", ["GetSchedule", "GetSession", "ReserveSessions"])
def test_a_429_waits_retry_after_once_then_books_one_seat(env, op):
    pid = offered(env)
    env.fake.throttle_next(op, retry_after=7)
    t0 = env.clock()
    res = env.w.approve(pid)
    assert res.state == "booked" and env.clock() - t0 >= 7
    assert "N1" in env.fake.schedule.reserved and len(reserve_rows(env)) == 1
    assert env.fake.log.count(("ReserveSessions", 200)) == 1       # a 429 is refused, so one landed


def test_a_429_twice_on_the_check_sends_nothing(env):
    pid = offered(env)
    env.fake.fail_next("GetSession", 429, times=2)
    with pytest.raises(ApprovalFailed, match="429"):
        env.w.approve(pid)
    assert env.fake.counts.get("ReserveSessions", 0) == 0 and env.w.is_pending(pid)


def test_the_session_gone_from_the_catalog_is_refused_and_nothing_sent(env):
    pid = offered(env)
    del env.fake.sessions["N1"]                                    # GetSession now answers 404
    res = env.w.approve(pid)
    assert res.state == "refused" and "no longer in the catalog" in res.reasons[0]
    assert env.fake.counts.get("ReserveSessions", 0) == 0 and reserve_rows(env) == []


@pytest.mark.parametrize("fault, landed, state", [
    (500, False, "failed"), (503, False, "failed"),
    ("dropped", False, "failed"), ("landed", True, "booked")])
def test_a_5xx_or_dropped_reserve_is_read_back_and_journaled_never_retried(env, fault, landed, state):
    pid = offered(env)
    reads = env.fake.counts["GetSchedule"]
    if fault == "dropped":
        env.hooks["ReserveSessions"] = dropped
    elif fault == "landed":
        env.hooks["ReserveSessions"] = landed_then_dropped
    else:
        env.fake.fail_next("ReserveSessions", fault)
    res = env.w.approve(pid)
    assert env.sent["ReserveSessions"] == 1                        # never retried
    assert env.fake.counts["GetSchedule"] == reads + 2             # the check, then the read-back
    assert res.state == state and ("N1" in env.fake.schedule.reserved) is landed
    [row] = reserve_rows(env)
    assert row["outcome"] == "error"
    kind = "booked" if landed else "not_booked"
    assert env.kinds(kind)[-1].data["session_id"] == "N1"
    assert env.w.is_pending(pid) is not landed      # nothing landed: the offer waits for another tap


def test_a_5xx_reserve_with_a_failed_read_back_is_unconfirmed_with_an_alert(env):
    pid = offered(env)

    def fail_then_break_read_back(req, send):
        env.fake.fail_next("GetSchedule", 503, times=3)
        return send(req)                                           # the reserve itself lands
    env.hooks["ReserveSessions"] = fail_then_break_read_back
    res = env.w.approve(pid)
    assert res.state == "unconfirmed" and "Check the official app" in res.alert
    assert env.fake.counts["ReserveSessions"] == 1 and len(reserve_rows(env)) == 1
    assert env.kinds("unconfirmed")[-1].data["session_id"] == "N1"


def test_a_403_on_the_reserve_is_journaled_and_not_retried(env):
    pid = offered(env)
    env.fake.fail_next("ReserveSessions", 403)
    res = env.w.approve(pid)
    assert res.state not in ("booked", "unconfirmed") and env.fake.counts["ReserveSessions"] == 1
    [row] = reserve_rows(env)
    assert row["outcome"] == "error" and "N1" not in env.fake.schedule.reserved


@pytest.mark.parametrize("code, state", [
    ("scheduleConflict", "conflict"), ("alreadyScheduled", "already"), ("sessionFull", "full"),
    ("seatHeldByCrew", "refused")])                                # a code the spec may add later
def test_each_reserve_failure_code_is_read_back_journaled_and_not_retried(env, code, state):
    pid = offered(env)
    reads = env.fake.counts["GetSchedule"]
    env.fake.refuse["N1"] = code
    res = env.w.approve(pid)
    assert res.state == state and env.fake.counts["ReserveSessions"] == 1
    assert env.fake.counts["GetSchedule"] == reads + 2
    [row] = reserve_rows(env)
    assert row["outcome"] == "partial" and not env.w.is_pending(pid)
    assert env.kinds("not_booked")[-1].data["reason"] == state


def test_a_seat_already_held_is_reported_without_a_write(env):
    pid = offered(env)
    env.fake.schedule.reserved.add("N1")                          # booked in the official app meanwhile
    res = env.w.approve(pid)
    assert res.state == "already" and env.fake.counts.get("ReserveSessions", 0) == 0


# ---- end to end: the real HTTP handler and the ntfy reply, against the fake

SECRET = "correct-horse-battery-staple"


def _served(extra=""):
    fake = FakeEventsApi(sessions=[mk("H1", "SVS401")])
    fake.schedule.reserved.add("H1")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(), transport=fake.transport())
    store = Store(":memory:")
    rules = R.parse(f"serve_secret: {SECRET}\nntfy_topic: reseat-test-7f3k9q2m\n{extra}{RULES}")
    w = Watcher(client, store, rules, EV)
    w.tick()
    fake.add_session(mk("N1", "NEW101", "12:00"))
    fake.add_session(mk("N2", "NEW201", "14:00"))
    w.tick()
    app = S.App(w, store, rules, EV, host="127.0.0.1", port=0)
    server = S.make_server(app)
    S.run(app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{app.port}", follow_redirects=False, timeout=30)
    return fake, w, app, http, server


@pytest.fixture(params=[""])
def served(request):
    fake, w, app, http, server = _served(request.param)
    yield fake, w, app, http
    w.stop()
    server.shutdown()
    server.server_close()


def _signed_in(app, http):
    r = http.get(f"/open?k={app._link_code}")
    assert r.status_code == 303
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])


def _pid(w, sid):
    return next(o.plan_id for o in w.pending_offers() if o.session_id == sid)


def test_book_through_the_http_handler_needs_cookie_and_header_and_books_once(served):
    fake, w, app, http = served
    pid = _pid(w, "N1")
    h = {"X-Reseat": "1"}
    assert http.post(f"/approve/{pid}", headers=h).status_code in (302, 303, 401, 403)   # no cookie
    _signed_in(app, http)
    assert http.post(f"/approve/{pid}").status_code == 403                               # no X-Reseat
    assert fake.counts.get("ReserveSessions", 0) == 0
    r = http.post(f"/approve/{pid}", headers=h)
    assert r.status_code == 200 and r.json()["kind"] == "book" and r.json()["state"] == "booked"
    assert "N1" not in str(r.json()) and "NEW101" in str(r.json()["held_now"])
    assert http.post(f"/approve/{pid}", headers=h).status_code == 404                    # one use
    assert fake.counts["ReserveSessions"] == 1 and "N1" in fake.schedule.reserved
    for path in ("/approve/N2", "/skip/N2", "/reserve/N2"):                              # never a session id
        assert http.post(path, headers=h).status_code == 404
    assert http.post(f"/skip/{_pid(w, 'N2')}", headers=h).json() == {"skipped": True}
    assert fake.counts["ReserveSessions"] == 1 and "N2" not in fake.schedule.reserved


@pytest.mark.parametrize("served", ["ntfy_book: true\n"], indirect=True)
def test_ntfy_reply_dismiss_and_book_an_offer(served):
    fake, w, app, _ = served
    one, two = _pid(w, "N1"), _pid(w, "N2")
    assert app.handle_reply("skip", one) == ("Dismissed", "Not booked. Nothing was sent.")
    assert app.handle_reply("approve", one) is None and fake.counts.get("ReserveSessions", 0) == 0
    w.read_only = True                                             # sign-in expired: held back, kept
    title, body = app.handle_reply("approve", two)
    assert title == "Not booked" and "Sign in needed" in body and w.is_pending(two)
    w.read_only = False
    results = [app.handle_reply("approve", two) for _ in range(3)]
    assert results == [None] * 3 and fake.counts["ReserveSessions"] == 1
    assert "N2" in fake.schedule.reserved and "N1" not in fake.schedule.reserved
    deadline = time.time() + 2
    while time.time() < deadline and w.is_pending(two):
        time.sleep(0.01)
    assert not w.is_pending(two)


def test_a_book_tap_from_ntfy_does_nothing_unless_ntfy_book_is_on(served):
    fake, w, app, _ = served                                       # ntfy_book is off
    pid = _pid(w, "N1")
    assert app.handle_reply("approve", pid) is None and app.handle_reply("skip", pid) is None
    assert w.is_pending(pid) and fake.counts.get("ReserveSessions", 0) == 0


@pytest.mark.parametrize("served", ["ntfy_book: true\n"], indirect=True)
def test_ntfy_book_taps_stop_at_the_daily_cap_and_the_offer_is_kept(served, monkeypatch):
    fake, w, app, _ = served
    monkeypatch.setattr(S, "NTFY_BOOKS_PER_DAY", 1)
    one, two = _pid(w, "N1"), _pid(w, "N2")
    assert app.handle_reply("approve", one) is None and fake.counts["ReserveSessions"] == 1
    title, body = app.handle_reply("approve", two)
    assert title == "Not booked" and "web app" in body
    assert w.is_pending(two) and fake.counts["ReserveSessions"] == 1
