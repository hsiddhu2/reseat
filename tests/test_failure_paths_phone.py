"""Failure paths for the phone page, the MCP tools, push and on-site polling.

Every API fault comes from FakeEventsApi, wrapped by `Net`, which can also raise
transport errors or return a scripted status for one call. The only real sockets
are to the local test server on 127.0.0.1.
"""

import socket
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from reseat import mcp_server as M
from reseat import push
from reseat import rules as R
from reseat import serve as S
from reseat.auth import AuthError
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi, sample_sessions
from reseat.models import Session
from reseat.store import Store
from reseat.swap import Swap
from reseat.watcher import Watcher, WatchEvent

EV = "reinvent2026"
SECRET = "correct-horse-battery-staple"
DAY = "2026-12-01"
WRITES = {"ReserveSessions", "CancelReservation", "AssociateFavorites", "DisassociateFavorite",
          "CreatePersonalTime", "UpdatePersonalTime", "DeletePersonalTime"}


def mk(sid, abbr, date, time_, band="available"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


class Net:
    """Transport in front of the fake. `script[op]` holds one step per call: None passes
    through, an int is returned as that status, an exception class is raised."""

    def __init__(self, fake):
        self.fake = fake
        self.script: dict[str, deque] = defaultdict(deque)
        self.log: list[str] = []

    def __call__(self, req):
        op = FakeEventsApi._op_name(req.method, [p for p in req.url.path.split("/") if p])
        self.log.append(op)
        if self.script[op]:
            step = self.script[op].popleft()
            if isinstance(step, int):
                return httpx.Response(step, json={"message": "scripted"})
            if step is not None:
                raise step("scripted", request=req)
        return self.fake._handle(req)

    def count(self, op):
        return self.log.count(op)

    def read_back_after_last(self, op):
        """True when a GetSchedule follows the last `op` before any other write."""
        idx = len(self.log) - 1 - self.log[::-1].index(op)
        for later in self.log[idx + 1:]:
            if later == "GetSchedule":
                return True
            if later in WRITES:
                return False
        return False


def failing_refresh():
    raise AuthError("Refresh token expired. Run: reseat login")


# ---------------------------------------------------------------------- phone stack

PHONE_CATALOG = [
    mk("H1-session-id", "SEC201-R", DAY, "10:00"),
    mk("H2-session-id", "SEC201-R1", "2026-12-02", "10:00"),
    mk("B1-session-id", "ARC301", DAY, "10:00", band="unavailable"),
]
PHONE_RULES = f"serve_secret: {SECRET}\ntargets:\n- code: ARC301\n- code: SEC201\n"


def phone(body=PHONE_RULES, on_refresh=None, host="127.0.0.1", clock=time.time):
    """Held SEC201-R, ARC301 opens over it: one pending proposal."""
    fake = FakeEventsApi(sessions=[s.model_copy() for s in PHONE_CATALOG])
    fake.schedule.reserved.add("H1-session-id")
    net = Net(fake)
    client = EventsClient(token_provider=lambda: fake.token, on_refresh=on_refresh, quota=QuotaTracker(),
                          transport=httpx.MockTransport(net))
    store = Store(":memory:")
    rules = R.parse(body)
    w = Watcher(client, store, rules, EV,
                swapper=lambda p, ok: Swap(client, store, rules, EV).run_plan(p, approved=ok))
    app = S.App(w, store, rules, EV, host=host, port=0, clock=clock)
    w.tick()
    fake.set_band("B1-session-id", "available")
    w.tick()
    plans = w.pending()
    return SimpleNamespace(fake=fake, net=net, w=w, app=app, store=store,
                           plan_id=plans[0].plan_id if plans else None)


@contextmanager
def pumping(w):
    """Run submitted jobs the way the watcher thread does between polls."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            w.drain()
            time.sleep(0.005)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(2)


@contextmanager
def serving(app):
    server = S.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{app.port}", follow_redirects=False, timeout=10)
    try:
        yield http
    finally:
        http.close()
        server.shutdown()
        server.server_close()


def sign_in(app, http):
    r = http.get(f"/open?k={app._link_code}")
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])


H = {"X-Reseat": "1"}


# ---- approve: what reaches the API


def test_approve_without_a_fallback_never_cancels():
    st = phone()
    st.fake.set_band("H1-session-id", "unavailable")
    st.fake.set_band("H2-session-id", "unavailable")
    with pumping(st.w), serving(st.app) as http:
        sign_in(st.app, http)
        r = http.post(f"/approve/{st.plan_id}", headers=H)
    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert st.net.count("CancelReservation") == 0 and st.net.count("ReserveSessions") == 0
    assert st.fake.schedule.reserved == {"H1-session-id"}


def test_approve_when_writes_are_closed_reads_back_and_keeps_the_seat():
    st = phone()
    st.fake.closed = True
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["state"] == "failed" and "409" in out["alert"]
    assert st.net.read_back_after_last("CancelReservation")
    assert st.net.count("ReserveSessions") == 0
    assert st.fake.schedule.reserved == {"H1-session-id"}


def test_approve_cancel_5xx_with_seat_still_held_stops():
    st = phone()
    st.net.script["CancelReservation"].append(503)
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["state"] == "failed" and "still held" in out["alert"]
    assert st.net.read_back_after_last("CancelReservation")
    assert st.net.count("ReserveSessions") == 0


def test_approve_wanted_full_rolls_back_and_reads_back_every_reserve():
    st = phone()
    st.fake.full.add("B1-session-id")
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["state"] == "rolled_back"
    assert st.fake.schedule.reserved == {"H1-session-id"}
    reserves = [i for i, op in enumerate(st.net.log) if op == "ReserveSessions"]
    assert len(reserves) == 2
    for i in reserves:
        assert st.net.log[i + 1] == "GetSchedule"


def test_approve_read_back_failure_after_reserve_sends_nothing_more():
    st = phone()
    st.net.script["GetSchedule"].extend([None, 503])          # check works, read-back after B fails
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["state"] == "failed" and "UNKNOWN" in out["alert"]
    assert st.net.count("ReserveSessions") == 1


def test_approve_precheck_5xx_sends_no_write():
    st = phone()
    st.net.script["GetSchedule"].append(503)
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["kept"] and "did not run" in out["error"]
    assert not WRITES & set(st.net.log)
    assert st.fake.schedule.reserved == {"H1-session-id"}


def test_approve_precheck_5xx_keeps_the_proposal():
    st = phone()
    st.net.script["GetSchedule"].append(503)
    with pumping(st.w):
        st.app.approve(st.plan_id)
    assert [p.plan_id for p in st.w.pending()] == [st.plan_id]


def test_approve_while_signed_out_sends_nothing_and_keeps_the_proposal():
    st = phone()
    st.w.read_only = True
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert out["kept"] and "Sign in needed" in out["error"]
    assert not WRITES & set(st.net.log)
    assert [p.plan_id for p in st.w.pending()] == [st.plan_id]


@pytest.mark.parametrize("fault", ["timeout", "refresh-fails"])
def test_approve_reserve_fault_after_cancel_reads_back_and_restores(fault):
    st = phone(on_refresh=failing_refresh)
    if fault == "timeout":
        st.net.script["ReserveSessions"].append(httpx.ReadTimeout)
    else:
        st.fake.fail_next("ReserveSessions", 401)
    with pumping(st.w):
        out = st.app.approve(st.plan_id)
    assert st.net.count("CancelReservation") == 1
    assert st.net.read_back_after_last("ReserveSessions")
    assert out is not None
    assert st.fake.schedule.reserved & {"H1-session-id", "H2-session-id", "B1-session-id"}


def test_approve_that_outlasts_the_wait_still_gets_an_http_answer(monkeypatch):
    st = phone()
    monkeypatch.setattr(S, "APPROVE_WAIT", 0.2)
    with serving(st.app) as http:                     # no watcher thread: the job never runs
        sign_in(st.app, http)
        r = http.post(f"/approve/{st.plan_id}", headers=H)
    assert r.status_code in (202, 503, 504)


# ---- HTTP surface


def test_wrong_and_malformed_cookies_are_refused():
    st = phone()
    with pumping(st.w), serving(st.app) as http:
        for cookie in (f"{S.COOKIE}=not-a-token", f'{S.COOKIE}="unterminated', "garbage;;==;"):
            hdr = {"Cookie": cookie}
            assert http.get("/api/state", headers=hdr).status_code == 401
            assert http.post(f"/approve/{st.plan_id}", headers={**hdr, **H}).status_code == 403
    assert not WRITES & set(st.net.log)
    assert st.w.pending()


def test_oversized_body_is_refused_before_anything_runs():
    st = phone()
    with pumping(st.w), serving(st.app) as http:
        sign_in(st.app, http)
        r = http.post(f"/approve/{st.plan_id}", headers=H, content=b"x" * (S.MAX_BODY + 1))
        assert r.status_code == 413
        big_login = http.post("/login", content=b"secret=" + b"x" * S.MAX_BODY)
        assert big_login.status_code == 413
    assert not WRITES & set(st.net.log)
    assert [p.plan_id for p in st.w.pending()] == [st.plan_id]


@pytest.mark.parametrize("bad", ["short", "A" * 65, "a.b.c.d.e.f.g.h.i.j", "%2e%2e%2f" * 3,
                                 "AAAAAAAAAAAAAAAAAAAA/extra"])
def test_malformed_plan_ids_never_reach_the_watcher(bad):
    st = phone()
    before = list(st.net.log)
    with pumping(st.w), serving(st.app) as http:
        sign_in(st.app, http)
        assert http.post(f"/approve/{bad}", headers=H).status_code == 404
        assert http.post(f"/skip/{bad}", headers=H).status_code == 404
    assert st.net.log == before
    assert st.w.pending()


def test_skip_needs_the_header_and_plans_work_once_across_approve_and_skip():
    st = phone()
    with pumping(st.w), serving(st.app) as http:
        sign_in(st.app, http)
        assert http.post(f"/skip/{st.plan_id}").status_code == 403
        assert st.w.pending()
        assert http.post(f"/skip/{st.plan_id}", headers=H).status_code == 200
        assert http.post(f"/approve/{st.plan_id}", headers=H).status_code == 404
        assert http.post(f"/skip/{st.plan_id}", headers=H).status_code == 404
    assert st.net.count("CancelReservation") == 0


def test_one_time_link_expires_after_an_hour():
    clk = SimpleNamespace(t=1_000_000.0)
    st = phone(clock=lambda: clk.t)
    code = st.app._link_code
    clk.t += S.LINK_TTL + 1
    assert st.app.open_link(code) is None


def test_login_rate_limit_lifts_after_a_minute():
    clk = SimpleNamespace(t=1_000_000.0)
    st = phone(clock=lambda: clk.t)
    for _ in range(S.LOGIN_TRIES):
        assert st.app.login("wrong") is None
    assert st.app.login(SECRET) is False
    clk.t += 61
    assert isinstance(st.app.login(SECRET), str)


def test_loopback_without_secret_checks_host_and_header_on_writes():
    st = phone(body="targets:\n- code: ARC301\n- code: SEC201\n")
    assert not st.app.auth_required
    for host, ok in (("127.0.0.1:8490", True), ("localhost:8490", True), ("[::1]:8490", True),
                     ("127.0.0.1.evil.example", False), ("evil.example:8490", False), (None, False)):
        assert st.app.authorized(None, host) is ok, host
    with pumping(st.w), serving(st.app) as http:
        assert http.post(f"/approve/{st.plan_id}").status_code == 403
        assert http.post(f"/approve/{st.plan_id}", headers={**H, "Host": "evil.example"}).status_code == 403
        assert http.get("/open?k=x").status_code == 404           # no link or login without a secret
        assert http.post("/login", data={"secret": "x"}, headers={"Host": "evil.example"}).status_code == 403
    assert st.net.count("CancelReservation") == 0
    assert st.w.pending()


def _raw(port, request, wait=1.0):
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(request)
        try:
            return s.recv(200)
        except TimeoutError:
            return b""


def test_non_numeric_content_length_gets_a_status():
    st = phone()
    with serving(st.app):
        got = _raw(st.app.port, b"POST /approve/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                b"Content-Length: abc\r\n\r\n")
    assert got.startswith(b"HTTP/1.")


def test_negative_content_length_gets_a_status():
    st = phone()
    with serving(st.app):
        got = _raw(st.app.port, b"POST /approve/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                b"Content-Length: -1\r\n\r\n")
    assert got.startswith(b"HTTP/1.")


# ---------------------------------------------------------------------- on site

ONSITE_CATALOG = [
    mk("W0", "WRK100", DAY, "08:00", band="unavailable"),
    mk("H", "SEC201", DAY, "10:00"),
    mk("W1", "WRK101", DAY, "13:00", band="unavailable"),
    mk("W2", "WRK102", DAY, "15:00", band="unavailable"),
]
ONSITE_RULES = "targets:\n- code: WRK100\n- code: WRK101\n- code: WRK102\n"


def onsite(clock, on_refresh=None, interval=60):
    fake = FakeEventsApi(sessions=[s.model_copy() for s in ONSITE_CATALOG])
    fake.schedule.reserved.add("H")
    net = Net(fake)
    token = {"t": fake.token}
    client = EventsClient(token_provider=lambda: token["t"], on_refresh=on_refresh,
                          quota=QuotaTracker(clock=clock), sleep=clock.sleep,
                          transport=httpx.MockTransport(net))
    store = Store(":memory:")
    store.apply_sweep(EV, ONSITE_CATALOG, with_abstracts=False)
    w = Watcher(client, store, R.parse(ONSITE_RULES), EV, interval=interval, clock=clock, sleep=clock.sleep)
    events: list[WatchEvent] = []
    w.subscribe(events.append)
    return SimpleNamespace(fake=fake, net=net, token=token, w=w, store=store, events=events)


def messages(events, kind="error"):
    return " | ".join(str(e.data.get("message")) for e in events if e.kind == kind)


def test_onsite_writes_closed_is_reported_and_retried_when_open(clock):
    e = onsite(clock)
    e.fake.closed = True
    e.fake.set_band("W1", "limited")
    res = e.w.onsite_tick(DAY)
    assert res.booked == [] and "409" in res.error
    e.fake.closed = False
    sent = e.fake.counts["ReserveSessions"]
    assert e.w.onsite_tick(DAY).booked == []                  # inside the 15-minute hold: no write
    assert e.fake.counts["ReserveSessions"] == sent
    clock.sleep(15 * 60)
    assert e.w.onsite_tick(DAY).booked == ["W1"]              # the queued opening, after the hold
    assert e.net.read_back_after_last("ReserveSessions")


def test_onsite_429_on_get_session_waits_once_and_goes_on(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.fake.throttle_next("GetSession", retry_after=3)
    t0 = clock.t
    res = e.w.onsite_tick(DAY)
    assert res.failure is None and res.booked == ["W1"] and clock.t >= t0 + 3


def test_onsite_5xx_is_an_outage_and_sends_no_write(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.fake.fail_next("GetSession", 503)
    res = e.w.onsite_tick(DAY)
    assert res.failure == "outage" and e.w.down_since is not None
    assert e.net.count("ReserveSessions") == 0


def test_onsite_403_stops_without_writing(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.fake.fail_next("GetSchedule", 403)
    res = e.w.onsite_tick(DAY)
    assert res.failure == "other" and "403" in res.error
    assert e.net.count("ReserveSessions") == 0 and not e.w.read_only


def test_onsite_refresh_failure_goes_read_only_then_recovers(clock):
    e = onsite(clock, on_refresh=failing_refresh)
    e.fake.set_band("W1", "limited")
    e.token["t"] = "expired"
    res = e.w.onsite_tick(DAY)
    assert res.failure == "auth" and e.w.read_only
    assert "needed" in [ev.data.get("state") for ev in e.events if ev.kind == "signin"]
    assert e.net.count("ReserveSessions") == 0
    e.token["t"] = e.fake.token
    assert e.w.onsite_tick(DAY).booked == ["W1"] and not e.w.read_only


def test_onsite_partial_bulk_failure_with_an_unknown_code(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.fake.set_band("W2", "available")
    e.fake.refuse["W2"] = "seatHeldByCrew"
    res = e.w.onsite_tick(DAY)
    assert res.booked == ["W1"] and res.failure is None
    assert e.fake.schedule.reserved == {"H", "W1"}
    assert e.net.read_back_after_last("ReserveSessions")


def test_onsite_read_back_disagreement_is_not_reported_booked(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.fake.ghost.add("W1")
    res = e.w.onsite_tick(DAY)
    assert res.booked == [] and "may be held" in messages(e.events)


def test_onsite_read_back_failure_is_reported_and_never_resent(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.net.script["GetSchedule"].extend([None, 503])         # tick read works, read-back fails
    res = e.w.onsite_tick(DAY)
    assert "read-back failed" in res.error
    e.w.onsite_tick(DAY)                                      # the seat landed: no second reserve
    assert e.net.count("ReserveSessions") == 1 and e.fake.schedule.reserved == {"H", "W1"}


def test_onsite_read_back_failure_is_not_announced_as_booked(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.net.script["GetSchedule"].extend([None, 503])
    res = e.w.onsite_tick(DAY)
    assert res.booked == [] and not [ev for ev in e.events if ev.kind == "booked"]


def test_onsite_moved_held_session_warns_and_writes_nothing(clock):
    e = onsite(clock)
    e.fake.sessions["H"] = e.fake.sessions["H"].model_copy(update={"room": "Level 3 | Grand 300"})
    res = e.w.onsite_tick(DAY)
    assert res.moved == ["H"] and [ev.data["session_id"] for ev in e.events if ev.kind == "moved"] == ["H"]
    assert not WRITES & set(e.net.log)


def test_onsite_404_on_one_session_does_not_stop_the_rest(clock):
    e = onsite(clock)
    del e.fake.sessions["W0"]
    e.fake.set_band("W1", "limited")
    assert e.w.onsite_tick(DAY).booked == ["W1"]


def test_onsite_failure_mid_poll_does_not_lose_an_opening(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.net.script["GetSession"].extend([None, None, None, 503])    # H, W0, W1 pass, W2 fails
    assert e.w.onsite_tick(DAY).failure == "outage"
    e.w.onsite_tick(DAY)
    assert "W1" in e.fake.schedule.reserved


def test_onsite_reserve_timeout_is_read_back(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.net.script["ReserveSessions"].append(httpx.ReadTimeout)
    e.w.onsite_tick(DAY)
    assert e.net.read_back_after_last("ReserveSessions")


def test_onsite_reserve_timeout_is_retried_next_poll(clock):
    e = onsite(clock)
    e.fake.set_band("W1", "limited")
    e.net.script["ReserveSessions"].append(httpx.ReadTimeout)
    e.w.onsite_tick(DAY)
    e.w.onsite_tick(DAY)
    assert "W1" in e.fake.schedule.reserved


def test_run_polls_on_site_between_sweeps_and_not_during_an_outage(clock):
    e = onsite(clock)
    e.w.run(max_ticks=2, onsite_day=lambda: DAY)
    # 60 s interval, polls at 20 and 40 s: two polls of H and W0, W1, W2.
    assert e.net.count("ListSessions") == 2 and e.net.count("GetSession") == 2 * 4
    down = onsite(clock)
    down.net.script["ListSessions"].append(503)
    down.w.run(max_ticks=2, onsite_day=lambda: DAY)
    assert down.net.count("GetSession") == 0


# ---------------------------------------------------------------------- MCP

MCP_RULES = "targets:\n- code: SVS401\n- code: SEC201\n"


def mcp(clock, on_refresh=None):
    fake = FakeEventsApi(sessions=sample_sessions())
    net = Net(fake)
    client = EventsClient(token_provider=lambda: fake.token, on_refresh=on_refresh,
                          quota=QuotaTracker(clock=clock), sleep=clock.sleep,
                          transport=httpx.MockTransport(net))
    store = Store(":memory:")
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    tools = M.Tools(client, store, R.parse(MCP_RULES), EV, clock=clock)
    out = tools.propose_changes()
    pid = out.split("Plan ", 1)[1].split(",", 1)[0]
    return SimpleNamespace(fake=fake, net=net, tools=tools, store=store, pid=pid)


def test_mcp_partial_failure_with_unknown_code_is_reported(clock):
    m = mcp(clock)
    m.fake.refuse["S-SEC1"] = "seatHeldByCrew"
    out = m.tools.approve_changes(m.pid)
    assert "seatHeldByCrew" in out and m.fake.schedule.reserved == {"S-SVS1"}
    assert m.net.read_back_after_last("ReserveSessions")


def test_mcp_read_back_disagreement_is_unconfirmed(clock):
    m = mcp(clock)
    m.fake.ghost.add("S-SVS1")
    out = m.tools.approve_changes(m.pid)
    assert "unconfirmed" in out


def test_mcp_429_waits_once_and_books(clock):
    m = mcp(clock)
    m.fake.throttle_next("ReserveSessions", retry_after=2)
    m.tools.approve_changes(m.pid)
    assert m.fake.schedule.reserved == {"S-SVS1", "S-SEC1"}


@pytest.mark.parametrize("status", [500, 503])
def test_mcp_5xx_on_reserve_is_read_back_and_not_resent(clock, status):
    m = mcp(clock)
    m.fake.fail_next("ReserveSessions", status)
    m.tools.approve_changes(m.pid)
    assert m.net.count("ReserveSessions") == 1 and m.net.read_back_after_last("ReserveSessions")


def test_mcp_403_on_reserve_stops(clock):
    m = mcp(clock)
    m.fake.fail_next("ReserveSessions", 403)
    out = m.tools.approve_changes(m.pid)
    assert "not_sent" in out and m.net.count("ReserveSessions") == 1


def test_mcp_guard_with_writes_closed_reports_and_creates_nothing(clock):
    m = mcp(clock)
    m.fake.schedule.reserved.add("S-SVS1")
    out = m.tools.guard_sync()
    m.fake.closed = True
    done = m.tools.approve_changes(out.split("Plan ", 1)[1].split(",", 1)[0])
    assert "409" in done and m.fake.schedule.personal_time == {}


def test_mcp_read_back_failure_is_not_reported_as_an_empty_schedule(clock):
    m = mcp(clock)
    m.net.script["GetSchedule"].extend([None, 503])          # pre-check works, read-back fails
    out = m.tools.approve_changes(m.pid)
    assert "0 reserved" not in out


@pytest.mark.parametrize("fault", ["timeout", "refresh-fails"])
def test_mcp_reserve_fault_is_read_back(clock, fault):
    m = mcp(clock, on_refresh=failing_refresh)
    if fault == "timeout":
        m.net.script["ReserveSessions"].append(httpx.ReadTimeout)
    else:
        m.fake.fail_next("ReserveSessions", 401)
    out = m.tools.approve_changes(m.pid)
    assert isinstance(out, str) and m.net.read_back_after_last("ReserveSessions")


# ---------------------------------------------------------------------- push


def test_push_network_error_is_counted_not_raised():
    def down(req):
        raise httpx.ConnectError("Connection refused", request=req)

    p = push.Pusher("c" * 20, transport=httpx.MockTransport(down))
    p(WatchEvent("back", 0, {"message": "re:Seat back"}))
    p.flush()
    assert p.failures == 1 and p.sent == []


def test_push_queue_full_drops_and_counts():
    p = push.Pusher("d" * 20, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    for _ in range(101):
        p(WatchEvent("back", 0, {"message": "re:Seat back"}))
    assert p.failures == 1
