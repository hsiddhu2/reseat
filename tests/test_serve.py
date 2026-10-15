import threading
import time

import httpx
import pytest

from reseat import push
from reseat import rules as R
from reseat import serve as S
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.swap import Swap
from reseat.watcher import ONSITE_CAP, Watcher, WatchEvent

EV = "reinvent2026"
SECRET = "correct-horse-battery-staple"


def mk(sid, abbr, date, time_, band="available"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": band, "sessionTime": {"date": date, "time": time_, "length": "60"},
    })


CATALOG = [
    mk("H1-session-id", "SEC201-R", "2026-12-01", "10:00"),
    mk("H2-session-id", "SEC201-R1", "2026-12-02", "10:00"),
    mk("B1-session-id", "ARC301", "2026-12-01", "10:00", band="unavailable"),
]


def stack(body, host="127.0.0.1"):
    fake = FakeEventsApi(sessions=[s.model_copy() for s in CATALOG])
    fake.schedule.reserved.add("H1-session-id")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(),
                          transport=fake.transport())
    store = Store(":memory:")
    rules = R.parse(body)
    w = Watcher(client, store, rules, EV,
                swapper=lambda p, ok: Swap(client, store, rules, EV).run_plan(p, approved=ok))
    app = S.App(w, store, rules, EV, host=host, port=0)
    return fake, w, app, store


RULES = f"serve_secret: {SECRET}\ntargets:\n- code: ARC301\n- code: SEC201\n"


@pytest.fixture
def served():
    fake, w, app, store = stack(RULES)
    w.tick()                                   # baseline
    fake.set_band("B1-session-id", "available")
    w.tick()                                   # ARC301 opens over held SEC201: a proposal
    server = S.make_server(app)
    S.run(app, server)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{app.port}", follow_redirects=False, timeout=30)
    yield fake, w, app, http
    w.stop()
    server.shutdown()
    server.server_close()


def signed_in(app, http):
    code = app._link_code
    r = http.get(f"/open?k={code}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])
    return http


def wait_for(cond, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_non_loopback_without_secret_refused():
    with pytest.raises(S.ServeError, match="without serve_secret"):
        stack("targets: []\n", host="100.64.0.7")
    stack(f"serve_secret: {SECRET}\ntargets: []\n", host="100.64.0.7")     # with a secret it starts


def test_every_interface_is_refused_even_with_a_secret():
    with pytest.raises(S.ServeError, match="every interface"):
        stack(f"serve_secret: {SECRET}\ntargets: []\n", host="0.0.0.0")


def test_request_without_cookie_refused(served):
    _, _, _, http = served
    assert http.get("/api/state").status_code == 401
    r = http.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert http.post("/approve/" + "x" * 20, headers={"X-Reseat": "1"}).status_code == 403


def test_one_time_link_sets_cookie_redirects_clean_and_works_once(served):
    _, _, app, http = served
    code = app._link_code
    r = http.get(f"/open?k={code}")
    assert r.status_code == 303 and r.headers["location"] == "/"     # clean URL, no secret, no code
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and SECRET not in cookie
    again = httpx.get(f"http://127.0.0.1:{app.port}/open?k={code}", follow_redirects=False)
    assert again.headers["location"] == "/login"
    assert httpx.get(f"http://127.0.0.1:{app.port}/open?k=guess", follow_redirects=False
                     ).headers["location"] == "/login"
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])
    assert http.get("/api/state").status_code == 200


def test_one_time_link_signs_in_the_phone_through_the_tailnet_name(served):
    """Through Tailscale Serve the phone's Host is the laptop's tailnet name. The printed link
    still signs it in once, and the cookie works on that Host."""
    _, _, app, http = served
    ts = f"laptop.tail1234.ts.net:{app.port}"
    assert app.one_time_link("laptop.tail1234.ts.net").startswith(f"http://{ts}/open?k=")
    r = http.get(f"/open?k={app._link_code}", headers={"Host": ts})
    assert r.status_code == 303 and r.headers["location"] == "/"
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])
    assert http.get("/api/state", headers={"Host": ts}).status_code == 200
    assert http.get("/today", headers={"Host": ts}).status_code == 200


def test_login_form_posts_the_secret_and_is_rate_limited(served):
    _, _, app, http = served
    assert http.get("/login").status_code == 200
    assert http.post("/login", data={"secret": "wrong"}).status_code == 401
    ok = http.post("/login", data={"secret": SECRET})
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    for _ in range(S.LOGIN_TRIES):
        http.post("/login", data={"secret": "wrong"})
    assert http.post("/login", data={"secret": SECRET}).status_code == 429


def test_page_has_strict_headers_and_no_token(served):
    fake, _, app, http = served
    signed_in(app, http)
    assert wait_for(lambda: http.get("/api/state").json().get("view"))
    bodies = [http.get("/api/state").text]
    for path in ("/", "/approve", "/today"):
        r = http.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
        assert '<script src="/static/app.js"' in r.text and "<script>" not in r.text   # no inline script
        bodies.append(r.text)
    for body in bodies:
        assert fake.token not in body and SECRET not in body and "session-id" not in body


def test_state_shows_held_and_the_proposal(served):
    _, _, app, http = served
    signed_in(app, http)
    assert wait_for(lambda: http.get("/api/state").json().get("proposals"))
    s = http.get("/api/state").json()
    assert [h["code"] for h in s["held"]] == ["SEC201-R"] and s["day"] == "2026-12-01"
    p = s["proposals"][0]
    assert p["wanted"] == "ARC301 Title ARC301" and p["held"] == "SEC201-R Title SEC201-R"
    assert "session-id" not in str(s["proposals"])          # codes and titles, no session ids


def test_approve_runs_the_swap_once(served):
    fake, w, app, http = served
    signed_in(app, http)
    plan_id = w.pending()[0].plan_id
    assert http.post(f"/approve/{plan_id}").status_code == 403          # no X-Reseat header
    r = http.post(f"/approve/{plan_id}", headers={"X-Reseat": "1"})
    assert r.status_code == 200 and r.json()["state"] == "verified"
    assert fake.schedule.reserved == {"B1-session-id"}
    assert http.post(f"/approve/{plan_id}", headers={"X-Reseat": "1"}).status_code == 404   # one use


def test_approve_with_unknown_or_expired_plan_refused(served):
    fake, w, app, http = served
    signed_in(app, http)
    h = {"X-Reseat": "1"}
    assert http.post("/approve/" + "A" * 22, headers=h).status_code == 404
    p = w.pending()[0]
    p.expires = 0                                                     # ten minutes have passed
    assert http.post(f"/approve/{p.plan_id}", headers=h).status_code == 404
    assert fake.counts.get("CancelReservation", 0) == 0


def test_no_endpoint_takes_a_session_id(served):
    fake, _, app, http = served
    signed_in(app, http)
    h = {"X-Reseat": "1"}
    for sid in ("B1-session-id", "H1-session-id"):
        for path in (f"/approve/{sid}", f"/skip/{sid}", f"/reserve/{sid}", f"/cancel/{sid}"):
            assert http.post(path, headers=h).status_code == 404, path
        assert http.get(f"/reserve/{sid}").status_code == 404
    assert fake.counts.get("ReserveSessions", 0) == 0 and fake.counts.get("CancelReservation", 0) == 0


def test_skip_drops_the_proposal(served):
    _, w, app, http = served
    signed_in(app, http)
    plan_id = w.pending()[0].plan_id
    assert http.post(f"/skip/{plan_id}", headers={"X-Reseat": "1"}).json() == {"skipped": True}
    assert w.pending() == []


def test_loopback_without_secret_checks_the_host_header():
    fake, w, app, store = stack("targets:\n- code: ARC301\n")
    assert not app.auth_required
    server = S.make_server(app)
    S.run(app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{app.port}/api/state"
        assert httpx.get(url).status_code == 200
        assert httpx.get(url, headers={"Host": "attacker.example:8490"}).status_code == 403
    finally:
        w.stop()
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------- push


def test_push_payload_has_no_ids_or_tokens():
    sent = []

    def capture(req):
        sent.append(req)
        return httpx.Response(200)

    names = {"B1-session-id": "ARC301 Title", "H1-session-id": "SEC201 Old"}
    p = push.Pusher("a" * 20, transport=httpx.MockTransport(capture), code_title=lambda sid: names[sid])
    events = [
        WatchEvent("booked", 0, {"session_id": "B1-session-id", "target": "ARC301", "code": "ARC301",
                                 "title": "Title"}),
        WatchEvent("proposed", 0, {"plan_id": "PLANPLANPLANPLAN1234", "target": "ARC301",
                                   "held_id": "H1-session-id", "wanted_id": "B1-session-id", "auto": False}),
        WatchEvent("swap", 0, {"plan_id": "PLANPLANPLANPLAN1234", "state": "verified"}),
        WatchEvent("offline", 0, {"message": "re:Seat offline since 14:05"}),
        WatchEvent("back", 0, {"message": "re:Seat back"}),
        WatchEvent("signin", 0, {"state": "needed", "message": "x"}),
        WatchEvent("leave", 0, {"code": "ARC301", "message": "Walk MGM Grand to Wynn, 55 min."}),
        WatchEvent("sweep", 0, {"count": 1}),                       # not pushed
    ]
    for ev in events:
        p(ev)
    p.flush()
    assert len(sent) == 7
    for req in sent:
        text = req.content.decode() + req.headers.get("title", "")
        assert "session-id" not in text and "PLANPLAN" not in text
        assert "authorization" not in {k.lower() for k in req.headers}
        assert str(req.url) == "https://ntfy.sh/" + "a" * 20
    assert sent[3].content.decode() == "re:Seat offline since 14:05"


def test_push_is_off_without_a_topic_and_topic_must_be_random_looking():
    assert R.parse("targets: []\n").ntfy_topic is None
    with pytest.raises(R.RulesError):
        R.parse("ntfy_topic: reseat\n")                             # short, guessable
    with pytest.raises(R.RulesError):
        R.parse("serve_secret: short\n")


def test_push_failure_never_raises():
    p = push.Pusher("b" * 20, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    p(WatchEvent("back", 0, {"message": "re:Seat back"}))
    p.flush()
    assert p.failures == 1 and p.sent == []


# ---------------------------------------------------------------------- on site


def test_onsite_cap_of_40_is_enforced(clock):
    sessions = [mk(f"S{i:02d}", f"X{i:02d}", "2026-12-01", "10:00", band="unavailable") for i in range(55)]
    fake = FakeEventsApi(sessions=sessions)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, sessions, with_abstracts=False)
    rules = R.parse("watch_cap: 60\nmax_per_day: 20\ntargets:\n"
                    + "".join(f"- code: X{i:02d}\n" for i in range(55)))
    w = Watcher(client, store, rules, EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    res = w.onsite_tick("2026-12-01")
    assert res.count == ONSITE_CAP and fake.counts["GetSession"] == ONSITE_CAP
    assert any("polling the first 40" in e.data.get("message", "") for e in events if e.kind == "error")


def test_onsite_band_flip_books_the_seat(clock):
    sessions = [mk("W1", "WRK101", "2026-12-01", "13:00", band="unavailable")]
    fake = FakeEventsApi(sessions=sessions)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, sessions, with_abstracts=False)
    w = Watcher(client, store, R.parse("targets:\n- code: WRK101\n"), EV, clock=clock, sleep=clock.sleep)
    assert w.onsite_tick("2026-12-01").booked == []
    fake.set_band("W1", "limited")                                   # a no-show freed a seat
    assert w.onsite_tick("2026-12-01").booked == ["W1"]
    assert ("unavailable", "limited") in [h[1:] for h in store.band_history(EV, "W1")]


def test_leave_now_is_announced_once(clock):
    fake, w, app, store = stack("home_venue: Wynn\ntargets:\n- code: SEC201\n")
    w.tick()
    w.tick()
    leaves = []
    w.subscribe(lambda ev: ev.kind == "leave" and leaves.append(ev))
    # H1 is 10:00 Tue at MGM Grand. Wynn to MGM is 55 min typical, 65 allowed at 80 percent, plus 11:
    # leave at 08:44 PST = 16:44Z.
    from datetime import UTC, datetime
    at = datetime(2026, 12, 1, 16, 45, tzinfo=UTC).timestamp()
    app.clock = lambda: at
    app.refresh()
    app.refresh()
    assert len(leaves) == 1 and leaves[0].data["code"] == "SEC201-R"
    assert app.snapshot()["next_leave"]["leave_local"] == "08:44"


def test_cross_site_post_is_refused_even_to_login(served):
    _, _, app, http = served
    signed_in(app, http)
    bad = {"Origin": "https://attacker.example", "X-Reseat": "1"}
    plan_id = "x" * 22
    assert http.post(f"/approve/{plan_id}", headers=bad).status_code == 403
    assert http.post("/login", data={"secret": "wrong"}, headers={"Origin": "https://attacker.example"}
                     ).status_code == 403
    assert len(app._failures) == 0               # a cross-site form cannot lock the owner out


def test_a_browser_post_from_the_page_itself_is_accepted(served):
    """Browsers send the real Origin on a same-origin POST only when the page allows a referrer.
    Under no-referrer they send "Origin: null", and the login and every button were refused."""
    _, _, app, http = served
    assert http.get("/login").headers["Referrer-Policy"] == "same-origin"
    page = {"Origin": str(http.base_url).rstrip("/")}
    secret = app.rules.serve_secret
    r = http.post("/login", data={"secret": secret}, headers=page)
    assert r.status_code == 303 and S.COOKIE in r.cookies
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])
    plan_id = "x" * 22
    assert http.post(f"/approve/{plan_id}", headers={**page, "X-Reseat": "1"}).status_code == 404


def test_origin_null_is_still_refused(served):
    _, _, app, http = served
    signed_in(app, http)
    assert http.post("/login", data={"secret": "wrong"}, headers={"Origin": "null"}).status_code == 403
    assert http.post(f"/approve/{'x' * 22}", headers={"Origin": "null", "X-Reseat": "1"}).status_code == 403


def test_session_tokens_expire(served):
    _, _, app, http = served
    signed_in(app, http)
    assert http.get("/api/state").status_code == 200
    real = app.clock
    app.clock = lambda: real() + S.COOKIE_DAYS * 86400 + 1
    assert http.get("/api/state").status_code == 401


def test_ipv6_host_is_refused_cleanly():
    with pytest.raises(S.ServeError, match="IPv6"):
        stack(f"serve_secret: {SECRET}\ntargets: []\n", host="::")


def test_snapshot_shows_the_new_seat_right_after_a_swap(served):
    fake, w, app, http = served
    signed_in(app, http)
    plan_id = w.pending()[0].plan_id
    assert http.post(f"/approve/{plan_id}", headers={"X-Reseat": "1"}).json()["state"] == "verified"
    held = [h["code"] for h in http.get("/api/state").json()["held"]]
    assert held == ["ARC301"]                    # not the cancelled SEC201-R, no sweep needed


def test_worker_thread_is_joinable_so_a_swap_can_finish():
    fake, w, app, store = stack(RULES)
    server = S.make_server(app)
    worker = S.run(app, server)
    try:
        assert not worker.daemon
    finally:
        w.stop()
        worker.join(timeout=5)
        server.server_close()
    assert not worker.is_alive()


def test_advice_skips_targets_already_held():
    fake, w, app, store = stack("targets:\n- code: SEC201\n- code: ARC301\n")
    w.tick()
    fake.set_band("B1-session-id", "available")
    w.tick()
    from datetime import UTC, datetime
    app.clock = lambda: datetime(2026, 12, 1, 8, 0, tzinfo=UTC).timestamp()   # 00:00 PST
    app.refresh()
    advice = app.snapshot()["advice"]
    assert advice is None or advice["code"] != "SEC201-R1"     # SEC201 is held via SEC201-R


def test_push_title_with_non_ascii_never_kills_the_sender():
    sent = []
    p = push.Pusher("c" * 20, transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    p(WatchEvent("leave", 0, {"code": "CAFÉ101", "message": "Walk to Café, 5 min."}))
    p(WatchEvent("back", 0, {"message": "re:Seat back"}))
    p.flush()
    assert len(sent) == 2 and p.failures == 0
    assert "Café" in sent[0].content.decode()


def test_onsite_cap_warning_fires_once_a_day(clock):
    sessions = [mk(f"S{i:02d}", f"X{i:02d}", "2026-12-01", "10:00", band="unavailable") for i in range(45)]
    fake = FakeEventsApi(sessions=sessions)
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, sessions, with_abstracts=False)
    rules = R.parse("watch_cap: 60\ntargets:\n" + "".join(f"- code: X{i:02d}\n" for i in range(45)))
    w = Watcher(client, store, rules, EV, clock=clock, sleep=clock.sleep)
    events = []
    w.subscribe(events.append)
    for _ in range(3):
        w.onsite_tick("2026-12-01")
    assert len([e for e in events if e.kind == "error" and "first 40" in e.data["message"]]) == 1


# ---------------------------------------------------------------------- Origin on every POST route

BAD_ORIGINS = ["null", "https://attacker.example", "http://127.0.0.1:1"]    # opaque, cross-site, other port


def _page_origin(http):
    return str(http.base_url).rstrip("/")


@pytest.mark.parametrize("route", ["approve", "skip"])
def test_plan_buttons_accept_the_page_origin_and_refuse_any_other(served, route):
    """Approve and Keep (skip) act only on a plan id, and only from the page's own origin."""
    fake, w, app, http = served
    signed_in(app, http)
    plan_id = w.pending()[0].plan_id
    for origin in BAD_ORIGINS:
        r = http.post(f"/{route}/{plan_id}", headers={"Origin": origin, "X-Reseat": "1"})
        assert r.status_code == 403 and r.json() == {"error": "cross-site request"}, origin
    assert [p.plan_id for p in w.pending()] == [plan_id]               # nothing taken or run
    assert fake.counts.get("ReserveSessions", 0) == 0 and fake.counts.get("CancelReservation", 0) == 0
    r = http.post(f"/{route}/{plan_id}", headers={"Origin": _page_origin(http), "X-Reseat": "1"})
    assert r.status_code == 200
    if route == "approve":
        assert r.json()["state"] == "verified" and fake.schedule.reserved == {"B1-session-id"}
    else:
        assert r.json() == {"skipped": True} and w.pending() == []


def test_blocks_update_accepts_the_page_origin_and_refuses_any_other(served):
    """The leave-now blocks Update button posts to /guard/<token>. A refused POST leaves the offer."""
    _, _, app, http = served
    signed_in(app, http)
    token = "t" * 22
    app.blocks_state = {"token": token}
    for origin in BAD_ORIGINS:
        r = http.post(f"/guard/{token}", headers={"Origin": origin, "X-Reseat": "1"})
        assert r.status_code == 403, origin
    assert app.blocks_state == {"token": token}                         # the offer is still there
    r = http.post(f"/guard/{token}", headers={"Origin": _page_origin(http), "X-Reseat": "1"})
    assert r.status_code == 200 and "ok" in r.json()
    assert app.blocks_state is None or app.blocks_state.get("token") != token     # spent


def test_login_accepts_the_page_origin_and_refuses_any_other_without_counting_a_failure(served):
    _, _, app, http = served
    for origin in BAD_ORIGINS:
        r = http.post("/login", data={"secret": SECRET}, headers={"Origin": origin})
        assert r.status_code == 403 and S.COOKIE not in r.cookies, origin
    assert len(app._failures) == 0
    r = http.post("/login", data={"secret": SECRET}, headers={"Origin": _page_origin(http)})
    assert r.status_code == 303 and S.COOKIE in r.cookies


def test_tailscale_host_with_a_matching_origin_is_accepted(served):
    """From the phone over Tailscale the Host is the laptop's tailnet name, not loopback.
    With serve_secret set, a POST whose Origin matches that Host goes through. Any other is refused."""
    _, w, app, http = served
    ts = f"laptop.tail1234.ts.net:{app.port}"
    page = {"Host": ts, "Origin": f"http://{ts}"}
    r = http.post("/login", data={"secret": SECRET}, headers=page)
    assert r.status_code == 303 and S.COOKIE in r.cookies
    http.cookies.set(S.COOKIE, r.cookies[S.COOKIE])
    assert http.get("/api/state", headers={"Host": ts}).status_code == 200
    plan_id = w.pending()[0].plan_id
    for origin in ["null", f"http://127.0.0.1:{app.port}", "http://laptop.tail1234.ts.net:1"]:
        assert http.post(f"/skip/{plan_id}", headers={"Host": ts, "Origin": origin, "X-Reseat": "1"}
                         ).status_code == 403, origin
    r = http.post(f"/skip/{plan_id}", headers={**page, "X-Reseat": "1"})
    assert r.status_code == 200 and r.json() == {"skipped": True}


def test_post_with_page_origin_still_needs_the_cookie_and_the_header(served):
    """A matching Origin is necessary, not sufficient."""
    _, w, app, http = served
    plan_id = w.pending()[0].plan_id
    page = {"Origin": _page_origin(http)}
    no_cookie = http.post(f"/approve/{plan_id}", headers={**page, "X-Reseat": "1"})
    assert no_cookie.status_code == 403
    signed_in(app, http)
    assert http.post(f"/approve/{plan_id}", headers=page).status_code == 403      # no X-Reseat
    assert [p.plan_id for p in w.pending()] == [plan_id]


def test_every_response_carries_the_same_origin_referrer_policy(served):
    _, _, app, http = served
    signed_in(app, http)
    for path in ("/login", "/", "/today", "/api/state", "/static/app.js", "/no-such-page"):
        assert http.get(path).headers["Referrer-Policy"] == "same-origin", path
