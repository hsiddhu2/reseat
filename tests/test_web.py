"""The web app (D-010): dashboard, approve and today views, and demo mode.

Every view is rendered from a store filled by the real watcher against FakeEventsApi.
Approvals go through the same plan-id path as the phone page. Demo mode never
touches the keychain, the network or ~/.reseat.
"""

import json
import threading
import time

import httpx
import keyring
import pytest
from typer.testing import CliRunner

from reseat import cli, config, pages
from reseat import demo as D
from reseat import serve as S
from reseat.fakeapi import FakeEventsApi
from reseat.models import Session
from reseat.store import Store
from reseat.swap import Swap


def scripted(host="127.0.0.1"):
    """A demo stack driven by hand: baseline, then each scripted change and a sweep after it."""
    d = D.Demo(host=host, port=0)
    d.watcher.tick()                 # baseline
    d.seat_opens()
    d.watcher.tick()                 # a swap proposal
    d.new_sitting()
    d.watcher.tick()                 # booked at once
    d.room_moves()
    d.watcher.tick()                 # a moved note
    d.app.refresh()
    return d


@pytest.fixture(scope="module")
def demo():
    return scripted()


def ids(d):
    return list(d.fake.sessions) + [D.NEW_SITTING_ID]


# ---- the views render from the store, with the expected codes and nothing private


def test_dashboard_shows_status_card_week_changes_and_journal(demo):
    snap = demo.app.snapshot()
    html = pages.render_dashboard(snap, demo=True)
    s = snap["view"]["status"]
    assert s["state"] == "watching" and s["where"] == "in this process" and s["sessions"] == 18
    assert s["held"] == 11 and s["quota_max"] == 120 and 0 < s["quota_left"] <= 120
    assert "Demo data" in html and "Needs your approval" in html
    [card] = snap["view"]["cards"]
    assert card["kind"] == "Seat opened" and card["title"] == "Replace CMP409-R with SVS306-R"
    assert card["sequence"] == (
        "Cancel CMP409-R, reserve SVS306-R, read back. If SVS306-R is refused, CMP409-R is reserved again. "
        "If writes close or the read-back fails, nothing more is sent and you are told what is known.")
    assert [c["ok"] for c in card["checks"]] == ["yes", "yes", "yes", "ask"]
    assert "CMP409-R1" in card["checks"][1]["text"]
    for code in ("ARC302-R", "CMP409-R", "SVS306-R", "AIM310-R1", "CMP303", "ANT335", "Week of 30 Nov"):
        assert code in html
    changes = " ".join(c["text"] for c in snap["view"]["changes"])
    assert "AIM310-R1 booked, read back ok" in changes and "CMP303 moved" in changes
    assert "SVS306-R unavailable → limited" in changes
    assert "Bookings within your rules happen at once and show under Last changes." in html
    assert f'data-approve="{card["plan_id"]}"' in html and f'data-skip="{card["plan_id"]}"' in html


def test_week_grid_marks_held_wanted_proposed_fallback_leave_moved_and_walks(demo):
    w = demo.app.snapshot()["view"]["week"]
    kinds = {(b["code"], b["kind"]) for b in w["blocks"]}
    assert {("CMP409-R", "held"), ("SVS306-R", "proposed"), ("ANT335", "wanted"),
            ("CMP409-R1", "fallback"), ("AIM310-R1", "held")} <= kinds
    by = {b["code"]: b for b in w["blocks"]}
    assert by["CMP409-R"]["pending"] and "swap pending" in by["CMP409-R"]["sub"]
    assert "moved to Level 3 | Premier 319" in by["CMP303"]["sub"]
    assert by["CMP409-R"]["lanes"] == 2 and {by["CMP409-R"]["lane"], by["SVS306-R"]["lane"]} == {0, 1}
    assert by["ANT335"]["badge"] == "walk 35 m, gap 30 m"          # after COP335 at Wynn, to Caesars Palace
    assert w["days"] == ["Mon 30", "Tue 1", "Wed 2", "Thu 3", "Fri 4"] and len(w["leaves"]) >= 10
    assert all(0 <= b["top"] < 100 and b["height"] > 0 for b in w["blocks"])


def test_approve_view_is_one_proposal_with_both_sides_and_the_checks(demo):
    html = pages.render_approve(demo.app.snapshot(), demo=True, push=False)
    assert "1 waiting" in html and "Replace CMP409-R with SVS306-R?" in html
    assert "You hold" in html and "Opened" in html and "Swap now" in html and "Keep CMP409-R" in html
    assert "This page shows the result when it is verified." in html     # no push without a topic
    assert "BOOKED" in html and "AIM310-R1" in html
    assert "You will get a push" in pages.render_approve(demo.app.snapshot(), push=True)


def test_today_view_has_the_leave_countdown_next_items_and_wanted(demo):
    snap = demo.app.snapshot()
    t = snap["view"]["today"]
    assert t["label"] == "Mon 30 Nov"
    h = t["hero"]
    assert (h["code"], h["start"], h["venue"], h["origin"], h["walk"], h["doors"]) == (
        "ARC302-R", "11:30", "MGM Grand", "Caesars Forum", 40, "11:19")
    names = [n["name"] for n in t["next"]]
    assert names == ["CMP409-R · Chalk talk", "API319 · Breakout session", "Personal · Team dinner"]
    [w] = t["wanted"]
    assert w["code"] == "SVS306-R" and w["clash"] == "Overlaps held CMP409-R." and "Basis:" in w["text"]
    assert t["cover"].startswith("Laptop sweeps the catalog every 30 s and checks today's 5 sessions")
    html = pages.render_today(snap, demo=True)
    assert "LEAVE IN" in html and "Doors close 11:19" in html and "Team dinner" in html


def test_no_session_id_token_or_secret_in_any_view_or_the_json(demo):
    snap = demo.app.snapshot()
    bodies = [json.dumps(snap), pages.render_dashboard(snap), pages.render_approve(snap),
              pages.render_today(snap)]
    for body in bodies:
        assert demo.fake.token not in body
        for sid in ids(demo):
            assert sid not in body, sid


def test_catalog_text_is_escaped_everywhere():
    d = D.Demo(port=0)
    sid = d._codes["CMP303"]
    d.fake.sessions[sid] = d.fake.sessions[sid].model_copy(update={"title": "<script>alert(1)</script>",
                                                                   "room": '"><img src=x onerror=1>'})
    d.watcher.tick()
    d.room_moves()
    d.watcher.tick()
    d.app.refresh()
    snap = d.app.snapshot()
    for html in (pages.render_dashboard(snap), pages.render_approve(snap), pages.render_today(snap)):
        assert "<script>alert" not in html and "<img src=x" not in html


def test_personal_time_comes_from_the_schedule_read_and_skips_reseat_blocks(demo):
    assert [p.title for p in demo.watcher.personal_time] == ["Team dinner"]
    d = D.Demo(port=0)
    d.fake.schedule.personal_time["mine"] = {
        "personalTimeId": "mine", "startDateTime": "2026-11-30T20:00:00",
        "endDateTime": "2026-11-30T20:05:00", "title": "Leave for API319",
        "description": "re:Seat leave-now. Walk. [reseat]", "location": "Wynn"}
    d.watcher.tick()
    d.app.refresh()
    names = [n["name"] for n in d.app.snapshot()["view"]["today"]["next"]]
    assert "Personal · Team dinner" in names and not any("Leave for" in n for n in names)


# ---- approvals go through the plan-id path, over HTTP


@pytest.fixture
def served():
    d = scripted()
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=30)
    yield d, http
    d.watcher.stop()
    server.shutdown()
    server.server_close()


def test_dashboard_approve_runs_the_real_swap_once_by_plan_id(served):
    d, http = served
    html = http.get("/").text
    plan = html.split('data-approve="')[1].split('"')[0]
    assert http.post(f"/approve/{plan}").status_code == 403                 # no X-Reseat header
    r = http.post(f"/approve/{plan}", headers={"X-Reseat": "1"})
    assert r.status_code == 200 and r.json()["state"] == "verified"
    held = d.fake.schedule.reserved
    assert d._codes["SVS306-R"] in held and d._codes["CMP409-R"] not in held
    assert http.post(f"/approve/{plan}", headers={"X-Reseat": "1"}).status_code == 404     # one use
    ops = [r["op"] for r in d.store.journal_entries(D.EVENT, 30)]
    assert {"swap.cancel", "swap.reserve", "swap.verified"} <= set(ops)


def test_unknown_or_expired_plan_is_refused_and_sends_nothing(served):
    d, http = served
    h = {"X-Reseat": "1"}
    assert http.post("/approve/" + "A" * 22, headers=h).status_code == 404
    p = d.watcher.pending()[0]
    p.expires = 0
    assert http.post(f"/approve/{p.plan_id}", headers=h).status_code == 404
    assert d.fake.counts.get("CancelReservation", 0) == 0


def test_dismiss_drops_the_proposal_and_sends_nothing(served):
    d, http = served
    plan = d.watcher.pending()[0].plan_id
    assert http.post(f"/skip/{plan}", headers={"X-Reseat": "1"}).json() == {"skipped": True}
    assert d.watcher.pending() == [] and d.fake.counts.get("CancelReservation", 0) == 0


def test_every_view_and_the_static_files_are_served(served):
    _, http = served
    for path in ("/", "/approve", "/today"):
        r = http.get(path)
        assert r.status_code == 200 and "Demo data" in r.text and 'href="/static/app.css"' in r.text
    css, js = http.get("/static/app.css"), http.get("/static/app.js")
    assert css.headers["content-type"].startswith("text/css") and "--accent" in css.text
    assert js.headers["content-type"].startswith("text/javascript") and "X-Reseat" in js.text
    for bad in ("/static/../serve.py", "/static/demo_catalog.json", "/static/", "/static/app.css/x"):
        assert http.get(bad).status_code in (403, 404)


def test_views_need_the_cookie_when_a_secret_is_set():
    assert D.Demo(host="100.64.0.7", port=0).app.auth_required      # off loopback: demo makes a secret
    d = scripted()
    d.app.secret, d.app.auth_required = "a-long-random-secret", True  # as off loopback, but bindable here
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=30, follow_redirects=False)
        for path in ("/", "/approve", "/today"):
            assert http.get(path).headers["location"] == "/login"
        assert http.get("/api/state").status_code == 401
        assert http.get("/static/app.css").status_code == 200        # no data in the static files
        link = d.app.one_time_link("127.0.0.1").split(str(d.app.port), 1)[1]
        got = http.get(link).cookies
        assert S.COOKIE not in got and D.COOKIE != S.COOKIE        # never replaces a real sign-in
        http.cookies.set(D.COOKIE, got[D.COOKIE])
        assert http.get("/today").status_code == 200
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def test_page_before_the_first_snapshot_says_it_is_starting():
    d = D.Demo(port=0)
    assert b"Starting" in S.page(d.app, "/") and b"data-empty=1" in S.page(d.app, "/today")


# ---- demo mode: no keychain, no network, no ~/.reseat


def test_demo_never_reads_the_keychain_or_the_network_or_home(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("demo mode touched the keychain or the network")
    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "set_password", boom)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "home" / "reseat.db")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "home" / "rules.yaml")
    d = scripted()
    assert d.app.snapshot()["view"]["cards"] and d.store.path == ":memory:"
    assert not (tmp_path / "home").exists()


def test_cli_serve_demo_skips_rules_client_and_store(monkeypatch):
    def boom():
        raise AssertionError("serve --demo opened the real rules, client or store")
    for name in ("_rules", "_client", "_store"):
        monkeypatch.setattr(cli, name, boom)
    seen = {}

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    monkeypatch.setattr(S, "make_server", lambda app: seen.setdefault("app", app) and Server())
    def run(app, server):
        t = threading.Thread(target=lambda: None)
        t.start()
        return t
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: seen.setdefault("script", True))
    r = CliRunner().invoke(cli.app, ["serve", "--demo"])
    assert r.exit_code == 0, r.output
    assert "Demo data." in r.output and "127.0.0.1:8491" in r.output and seen["closed"] and seen["script"]
    assert seen["app"].demo and seen["app"].port == D.DEFAULT_PORT


def test_cli_serve_refuses_every_interface(monkeypatch):
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--host", "0.0.0.0"])
    assert r.exit_code == 2 and "every interface" in r.output


def test_demo_script_hands_each_step_to_the_watcher_thread(monkeypatch):
    monkeypatch.setattr(D, "STEPS", (0.0, 0.01, 0.02))
    d = D.Demo(port=0)
    d.watcher.tick()
    t = d.start_script()
    t.join(5)
    assert d.done == []                     # nothing ran off the watcher's thread
    d.watcher.drain()
    assert d.done == ["seat", "sitting", "moved"]
    assert D.NEW_SITTING_ID in d.fake.sessions
    assert d.fake.sessions[d._codes["CMP303"]].room == D.NEW_ROOM


def test_demo_clock_reads_monday_morning_in_las_vegas():
    d = D.Demo(port=0, started=time.time())
    assert abs(d.clock() - D.START) < 5
    assert pages.local(d.clock()).strftime("%a %d %b %H:%M") == "Mon 30 Nov 10:15"


# ---- the engine pieces the views read


def test_swap_preview_reads_only_the_local_catalog(demo):
    before = dict(demo.fake.counts)
    p = demo.watcher.pending()[0]
    pv = Swap(demo.client, demo.store, demo.rules, D.EVENT).preview(p.held_id, p.wanted_id,
                                                                     demo.watcher.last_held)
    assert pv.band == "limited" and pv.band_open and not pv.clashes
    assert [f for f, _ in pv.fallbacks] == [p.held_id, demo._codes["CMP409-R1"]]
    assert demo.fake.counts == before                  # no API call


def test_journal_uses_the_store_clock():
    st = Store(":memory:", clock=lambda: 1234.0)
    st.journal("ev", "op", None, None, "ok")
    assert st.journal_entries("ev")[0]["ts"] == 1234.0


def test_watcher_keeps_personal_time_from_its_schedule_read(clock):
    from reseat import rules as R
    from reseat.client import EventsClient, QuotaTracker
    from reseat.watcher import Watcher
    s = Session.model_validate({"sessionId": "S1", "abbreviation": "ABC101", "title": "T",
                                "isReservable": True,
                                "sessionTime": {"date": "2026-12-01", "time": "10:00", "length": "60"}})
    fake = FakeEventsApi(sessions=[s])
    fake.schedule.personal_time["p1"] = {"personalTimeId": "p1", "startDateTime": "2026-12-01T20:00:00",
                                         "endDateTime": "2026-12-01T21:00:00", "title": "Lunch",
                                         "description": "x", "location": None}
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    w = Watcher(client, Store(":memory:"), R.parse("targets: []\n"), D.EVENT, clock=clock, sleep=clock.sleep)
    w.tick()
    assert [p.title for p in w.personal_time] == ["Lunch"]
    assert fake.counts["GetSchedule"] == 1             # the read it already made, no extra call


# ---- QA: status line states, missing sessions, an empty store, approve while writes are closed


def test_status_line_says_signin_offline_or_paused_and_every_view_renders():
    d = scripted()
    w = d.watcher
    for setup, state in ((lambda: setattr(w, "read_only", True), "signin"),
                         (lambda: setattr(w, "_down_since", d.clock() - 600), "offline"),
                         (lambda: setattr(w, "writes_closed_until", d.clock() + 300), "paused")):
        w.read_only, w._down_since, w.writes_closed_until = False, None, None
        setup()
        d.app.refresh()
        snap = d.app.snapshot()
        assert snap["view"]["status"]["state"] == state
        for render in (pages.render_dashboard, pages.render_approve, pages.render_today):
            assert "<" in (render(snap, True) if render is not pages.render_approve
                           else render(snap, True, push=False))


def test_empty_store_and_a_proposal_for_sessions_not_in_the_store_render_without_ids():
    from reseat.watcher import Proposal
    d = D.Demo(port=0)                      # no sweep yet: the store is empty
    ghost = Proposal("G" * 22, "ghost", "held-not-in-store", "wanted-not-in-store",
                     d.clock(), d.clock() + 600, False)
    d.watcher.proposals[ghost.plan_id] = ghost
    d.app.refresh()
    snap = d.app.snapshot()
    assert snap["view"]["status"]["last_sweep"] is None
    html = (pages.render_dashboard(snap, True) + pages.render_approve(snap, True, push=False)
            + pages.render_today(snap, True))
    assert "not-in-store" not in html and "not-in-store" not in json.dumps(snap)
    assert d.fake.counts.get("CancelReservation", 0) == 0


def test_approve_while_writes_are_closed_keeps_the_proposal_and_sends_nothing(served):
    d, http = served
    plan = d.watcher.pending()[0].plan_id
    d.watcher.submit(lambda: setattr(d.watcher, "writes_closed_until", d.clock() + 300)).result(10)
    r = http.post(f"/approve/{plan}", headers={"X-Reseat": "1"})
    assert r.status_code == 409 and "closed" in r.json()["error"] and r.json()["kept"]
    assert d.fake.counts.get("CancelReservation", 0) == 0 and d.fake.counts.get("ReserveSessions", 0) <= 1
    assert [p.plan_id for p in d.watcher.pending()] == [plan]


def test_hero_stays_on_a_missed_leave_time_until_the_session_starts():
    d = D.Demo(port=0, started=time.time() - 3000)      # 11:05: the 10:39 leave time has passed
    d.watcher.tick()
    d.app.refresh()
    snap = d.app.snapshot()
    h = snap["view"]["today"]["hero"]
    assert h["code"] == "ARC302-R" and h["leave_at"] < snap["now"]
    html = pages.render_today(snap)
    assert "LEAVE NOW" in html and ">now<" in html


def test_a_view_that_fails_to_build_says_so_and_keeps_the_phone_keys(monkeypatch):
    d = D.Demo(port=0)
    d.watcher.tick()
    errors = []
    d.watcher.subscribe(lambda ev: errors.append(ev) if ev.kind == "error" else None)
    monkeypatch.setattr(pages, "build", lambda app, now: 1 / 0)
    d.app.refresh()
    snap = d.app.snapshot()
    assert "view" not in snap and snap["held"] is not None and "ZeroDivisionError" in snap["view_error"]
    assert b"could not be built" in S.page(d.app, "/") and errors


def test_pages_use_the_render_time_clock_not_the_snapshot_time(demo):
    snap = demo.app.snapshot()
    later = snap["now"] + 600
    demo_now = demo.app.clock
    try:
        demo.app.clock = lambda: later
        assert f'data-now="{later:.0f}"' in S.page(demo.app, "/").decode()
    finally:
        demo.app.clock = demo_now


def test_dashboard_counts_what_was_kept_from_the_journal(served):
    d, http = served
    k = d.app.snapshot()["view"]["kept"]
    assert (k["booked"], k["swapped"], k["restored"]) == (1, 0, 0) and k["since"] == "Mon 30 Nov"
    plan = d.watcher.pending()[0].plan_id
    assert http.post(f"/approve/{plan}", headers={"X-Reseat": "1"}).json()["state"] == "verified"
    k = d.app.snapshot()["view"]["kept"]
    assert (k["booked"], k["swapped"]) == (1, 1)                 # a swap's reserve is not a second booking
    assert "seats booked" in http.get("/").text


def test_static_site_builds_four_pages_with_no_script_and_a_verified_swap(tmp_path):
    import runpy
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "build_site.py"
    site = runpy.run_path(str(script), run_name="site")
    names = site["build"](tmp_path)
    assert names == ["after.html", "approve.html", "index.html", "today.html"]
    for n in names:
        html = (tmp_path / n).read_text(encoding="utf-8")
        assert "<script" not in html and "/static/" not in html and "data-approve" not in html
        assert "Static snapshot of demo data" in html and 'href="app.css"' in html
    assert 'href="after.html">Swap now</a>' in (tmp_path / "index.html").read_text(encoding="utf-8")
    after = (tmp_path / "after.html").read_text(encoding="utf-8")
    assert "proposed → checked → cancelled → reserved → verified" in after and "swaps verified" in after
    assert (tmp_path / "app.css").exists()


def test_kept_counts_only_ids_the_read_back_showed_and_skips_failed_swaps():
    d = D.Demo(port=0)
    assert pages._kept(d.app) == {"booked": 0, "swapped": 0, "restored": 0, "since": None}
    ev, j = d.app.event_id, d.store.journal
    j(ev, "ReserveSessions", ["a"], {"status": 409}, "closed")                       # writes closed
    j(ev, "ReserveSessions", ["b"], {"result": None, "error": "503; read-back failed: 503",
                                     "readBack": None}, "error")                   # unknown, not booked
    j(ev, "ReserveSessions", ["c", "d"], {"result": {}, "error": None, "readBack": {"reserved": ["c"]},
                                          "disagreements": ["d"]}, "disagreement")  # only c read back
    j(ev, "ReserveSessions", ["e", "f", "g"], {"result": {}, "readBack": {"reserved": ["e", "g", "x"]}},
      "partial")                                                                   # x was not asked for
    j(ev, "swap.failed", {"held": "h", "wanted": "w"}, {"fallback_held": None}, "state:failed")
    j(ev, "swap.rolled_back", {"held": "h", "wanted": "w"}, {}, "state:rolled_back")
    j(ev, "swap.verified", {"held": "h", "wanted": "w"}, {}, "state:verified")
    k = pages._kept(d.app)
    assert (k["booked"], k["swapped"], k["restored"]) == (3, 1, 1) and k["since"]
    assert "3</b><span>seats booked" in pages._kept_html(k)
    assert "nothing yet" in pages._kept_html(pages._kept(D.Demo(port=0).app))


def test_static_site_has_no_absolute_or_external_links(tmp_path):
    import re
    import runpy
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "build_site.py"
    site = runpy.run_path(str(script), run_name="site")
    for n in site["build"](tmp_path):
        html = (tmp_path / n).read_text(encoding="utf-8")
        for url in re.findall(r'(?:href|src|action)="([^"]*)"', html):
            assert not url.startswith(("/", "http:", "https:", "//")), (n, url)
        assert "data-skip" not in html and "<form" not in html


def test_a_failed_swap_that_booked_a_fallback_counts_as_restored():
    d = D.Demo(port=0)
    req = {"held": "a", "wanted": "b"}
    d.store.journal(D.EVENT, "swap.failed", req, {"fallback_held": "c"}, "state:failed")
    d.store.journal(D.EVENT, "swap.failed", req, {"fallback_held": None}, "state:failed")
    k = pages._kept(d.app)
    assert (k["booked"], k["swapped"], k["restored"]) == (0, 0, 1)


# ---- push in demo mode: the same pusher as the real server


def test_demo_push_topic_goes_into_the_demo_rules_and_a_bad_one_is_refused():
    from reseat.rules import RulesError
    d = D.Demo(port=0, push_topic="reseat-demo-7f3k9q2m")
    assert d.rules.ntfy_topic == "reseat-demo-7f3k9q2m"
    assert D.Demo(port=0).rules.ntfy_topic is None                     # off unless asked
    for bad in ("short", "#aaaaaaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa # x", "aaaaaaaaaaaaaaaa\nserve_secret: x",
                "a" * 65):
        with pytest.raises(RulesError):
            D.Demo(port=0, push_topic=bad)


def test_cli_serve_demo_push_wires_the_pusher_and_sends_codes_only(monkeypatch):
    from reseat import push
    made = {}

    class FakePusher(push.Pusher):
        def __init__(self, topic, transport=None, code_title=str, click_base=None):
            super().__init__(topic, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             code_title=code_title, click_base=click_base)
            made["p"] = self

        def start(self):
            made["started"] = True

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def run(app, server):
        t = threading.Thread(target=lambda: None)
        t.start()
        return t
    monkeypatch.setattr(push, "Pusher", FakePusher)
    monkeypatch.setattr(S, "make_server", lambda app: made.setdefault("app", app) and Server())
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: None)
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--push", "reseat-demo-7f3k9q2m"])
    assert r.exit_code == 0, r.output
    p = made["p"]
    assert made["started"] and p.url == "https://ntfy.sh/reseat-demo-7f3k9q2m" and "Push is on" in r.output
    assert p.click_base is None                    # loopback: a phone could not open that link
    w = made["app"].watcher
    w.tick()
    sid = next(s for s in w.store.all(D.EVENT) if s.abbreviation == "SVS306-R").session_id
    w.emit("proposed", plan_id="x" * 22, target="SVS306", held_id=sid, wanted_id=sid, auto=False, expires=0)
    p.flush()
    [(title, body)] = p.sent
    assert title == "Swap proposed" and "SVS306-R" in body and sid not in body


def test_cli_push_without_demo_is_refused():
    r = CliRunner().invoke(cli.app, ["serve", "--push", "reseat-demo-7f3k9q2m"])
    assert r.exit_code == 2 and "ntfy_topic" in r.output


def test_a_push_carries_a_link_to_the_right_page_when_the_phone_can_reach_it():
    from reseat import push
    from reseat.watcher import WatchEvent
    seen = []

    def handler(req):
        seen.append(dict(req.headers))
        return httpx.Response(200)
    p = push.Pusher("reseat-demo-7f3k9q2m", transport=httpx.MockTransport(handler),
                    click_base="http://100.64.0.7:8491")
    events = (("proposed", {"wanted_id": "B", "held_id": "A"}), ("booked", {"code": "X", "title": "T"}),
              ("leave", {"code": "X", "message": "Walk"}))
    for kind, data in events:
        p(WatchEvent(kind, 0, data))
    p.flush()
    assert [h["click"] for h in seen] == ["http://100.64.0.7:8491/approve", "http://100.64.0.7:8491/",
                                          "http://100.64.0.7:8491/today"]
    seen.clear()
    loop = push.Pusher("reseat-demo-7f3k9q2m", transport=httpx.MockTransport(handler))
    loop(WatchEvent("booked", 0, {"code": "X", "title": "T"}))
    loop.flush()
    assert "click" not in seen[0]


def test_the_app_can_be_added_to_a_phone_home_screen(served):
    _, http = served
    html = http.get("/approve").text
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in html and 'rel="apple-touch-icon"' in html
    m = http.get("/static/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "standalone" and m.json()["start_url"] == "/approve"
    icon = http.get("/static/icon-180.png")
    assert icon.headers["content-type"] == "image/png" and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    csp = http.get("/").headers["content-security-policy"]
    assert "manifest-src 'self'" in csp and "img-src 'self'" in csp


def test_cli_push_links_to_the_page_when_served_on_a_reachable_address(monkeypatch):
    from reseat import push
    made = {}

    class FakePusher(push.Pusher):
        def __init__(self, topic, transport=None, code_title=str, click_base=None):
            super().__init__(topic, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             code_title=code_title, click_base=click_base)
            made["p"] = self

        def start(self):
            pass

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def run(app, server):
        t = threading.Thread(target=lambda: None)
        t.start()
        return t
    monkeypatch.setattr(push, "Pusher", FakePusher)
    monkeypatch.setattr(S, "make_server", lambda app: Server())
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: None)
    args = ["serve", "--demo", "--push", "reseat-demo-7f3k9q2m", "--host", "100.64.0.7"]
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert made["p"].click_base == "http://100.64.0.7:8491" and "a link to http://100.64.0.7:8491" in r.output
