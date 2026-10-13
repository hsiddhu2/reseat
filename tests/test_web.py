"""The web app (D-010): dashboard, approve and today views, and demo mode.

Every view is rendered from a store filled by the real watcher against FakeEventsApi.
Approvals go through the same plan-id path as the phone page. Demo mode never
touches the keychain, the network or ~/.reseat.
"""

import json
import pathlib
import threading
import time
from datetime import datetime

import httpx
import keyring
import pytest
from typer.testing import CliRunner

from reseat import cli, config, pages
from reseat import demo as D
from reseat import serve as S
from reseat.campus import VEGAS
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
    assert s["state"] == "watching" and s["where"] == "in this process" and s["sessions"] == 21
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
    assert "Changed sessions are labelled on the week for 15 minutes." in html
    activity = pages.render_activity(snap, demo=True)
    assert "AIM310-R1 booked, read back ok" in activity and "Journal" in activity
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
    # after COP335 at Wynn, to Caesars Palace
    assert by["ANT335"]["badge"] == "walk about 35 m (estimate), gap 30 m"
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
    assert names == ["activity.html", "after.html", "approve.html", "day-0.html", "day-1.html", "day-2.html",
                     "day-3.html", "day-4.html", "demo.html", "index.html", "status.html", "targets.html",
                     "today.html"]
    for n in names:
        html = (tmp_path / n).read_text(encoding="utf-8")
        assert "<script" not in html and "/static/" not in html and "data-approve" not in html
        assert "data-scenario" not in html
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


class StubListener:
    """Stands in for the ntfy reply stream, so no test reaches ntfy.sh."""
    made: list = []

    def __init__(self, topic, handle, transport=None, clock=None):
        self.topic, self.handle, self.started, self.stopped = topic, handle, False, False
        StubListener.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


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
        def __init__(self, topic, transport=None, code_title=str, click_base=None, approve=False, **kw):
            super().__init__(topic, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             code_title=code_title, click_base=click_base, approve=approve, **kw)
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
    monkeypatch.setattr(push, "ReplyListener", StubListener)
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
        def __init__(self, topic, transport=None, code_title=str, click_base=None, approve=False, **kw):
            super().__init__(topic, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             code_title=code_title, click_base=click_base, approve=approve, **kw)
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
    monkeypatch.setattr(push, "ReplyListener", StubListener)
    monkeypatch.setattr(S, "make_server", lambda app: Server())
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: None)
    args = ["serve", "--demo", "--push", "reseat-demo-7f3k9q2m", "--host", "100.64.0.7"]
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert made["p"].click_base == "http://100.64.0.7:8491" and "a link to http://100.64.0.7:8491" in r.output


# ---- reaching the server from another device


def test_self_check_says_whether_this_machine_reaches_the_server(served):
    d, _ = served
    assert S.self_check("127.0.0.1", d.app.port)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not S.self_check("127.0.0.1", free, timeout=1)


def test_a_dropped_or_cut_off_socket_prints_no_traceback(capsys):
    server = S.Server.__new__(S.Server)
    for err in (OSError(57, "Socket is not connected"), ConnectionResetError(), BrokenPipeError()):
        try:
            raise err
        except OSError:
            server.handle_error(None, ("100.64.0.9", 1))
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("a real bug")
    except ValueError:
        server.handle_error(None, ("100.64.0.9", 1))
    assert "a real bug" in capsys.readouterr().err


# ---- the demo control panel


EXPECT = [("seat", "proposed"), ("fill", "swap"), ("repeat", "booked"), ("blocked", "not_booked"),
          ("unsure", "unconfirmed"), ("expiring", "expiring"), ("pinned", "other_sitting"),
          ("move", "moved"), ("remove", "removed"), ("tight", "tight"), ("stale", "blocks"),
          ("briefing", "briefing"), ("leave10", "leave_soon"), ("leave", "leave"), ("soon", "wanted_soon"),
          ("close", "writes"), ("open", "writes"), ("down", "offline"), ("up", "back"),
          ("empty", "catalog"), ("signout", "signin"), ("signin", "sweep")]


def test_every_scenario_produces_its_event_through_the_real_engine():
    d = D.Demo(port=0)
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events = []
    d.watcher.subscribe(events.append)
    http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=60)
    try:
        assert wait_until(lambda: d.app.snapshot().get("view"))
        assert [k for k, _ in EXPECT] == [s["key"] for s in d.app.scenarios]
        for key, kind in EXPECT:
            before = len(events)
            r = http.post(f"/demo/{key}", headers={"X-Reseat": "1"})
            assert r.status_code == 200 and r.json()["message"], key
            assert kind in [e.kind for e in events[before:]], (key, [e.kind for e in events[before:]])
        swap = next(e for e in events if e.kind == "swap")
        assert swap.data["state"] == "rolled_back"
        writes = [e.data["state"] for e in events if e.kind == "writes"]
        assert writes == ["closed", "open"]
        assert http.post("/demo/seat").status_code == 403                 # still needs the X-Reseat header
        assert http.post("/demo/nope", headers={"X-Reseat": "1"}).status_code == 404
        assert "Try a scenario" in http.get("/demo").text and "Try a scenario" not in http.get("/").text
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def wait_until(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_the_control_panel_does_not_exist_on_a_real_server():
    from reseat import rules as R
    from reseat.client import EventsClient
    from reseat.fakeapi import FakeEventsApi
    from reseat.watcher import Watcher
    fake = FakeEventsApi(sessions=[])
    client = EventsClient(token_provider=lambda: fake.token, transport=fake.transport())
    store = Store(":memory:")
    rules = R.parse("targets: []\n")
    app = S.App(Watcher(client, store, rules, D.EVENT), store, rules, D.EVENT, port=0)
    server = S.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{app.port}", timeout=10)
        for key in ("seat", "close", "down", "signout"):
            assert http.post(f"/demo/{key}", headers={"X-Reseat": "1"}).status_code == 404
        assert not app.demo and app.run_scenario is None
    finally:
        server.shutdown()
        server.server_close()


def panel():
    """A demo after its baseline sweep, with each scenario callable on this thread (no server)."""
    d = D.Demo(port=0)
    d.watcher.tick()
    d.app.refresh()
    events = []
    d.watcher.subscribe(events.append)
    return d, events, {k: fn for k, _, _, fn in d.scenarios() if k != "tour"}


def test_each_scenario_leaves_the_state_its_button_promises():
    d, events, sc = panel()
    held = d.fake.schedule.reserved
    sc["signout"]()
    assert d.watcher.read_only
    sc["signin"]()
    assert not d.watcher.read_only                       # "Bookings resume" means read-only is cleared
    sc["close"]()
    assert d._codes["IND424"] not in held                # queued, nothing booked while closed
    sc["open"]()
    assert d._codes["IND424"] in held                    # "then Seat booked": the queued talk, read back
    hero = d.app.snapshot()["view"]["today"]["hero"]
    sc["leave"]()
    assert [e.data["code"] for e in events if e.kind == "leave"] == [hero["code"]]
    sc["remove"]()
    assert d._codes["SEC351"] not in held
    assert "SEC351" in [e.data["code"] for e in events if e.kind == "removed"]
    assert d._codes["COP301"] in held                    # nothing the attendee held was touched


def test_every_scenario_pressed_twice_answers_with_a_message():
    d, _, sc = panel()
    for key in sc:
        for _ in range(2):
            assert sc[key](), key


def test_fill_while_signed_out_or_closed_says_why_and_leaves_the_fake_clean():
    for off in ("signout", "close"):
        d, _, sc = panel()
        sc["seat"]()
        sc[off]()
        assert "sign" in sc["fill"]().lower() or "writes" in sc["fill"]().lower()
        assert d._codes["SVS306-R"] not in d.fake.full
        assert len(d.watcher.pending()) == 1                # the proposal is kept


def test_seat_never_claims_a_swap_waits_when_none_does():
    d, _, sc = panel()
    sc["signout"]()
    msg = sc["seat"]()
    assert d.watcher.pending() or "swap waits" not in msg


def test_scenarios_work_after_the_timed_script_and_an_approve_and_on_a_second_pass():
    """The order the README gives: watch the timed script, press Swap now, then use the panel. Twice."""
    d, events, sc = panel()
    d.seat_opens()
    d.watcher.tick()
    d.new_sitting()
    d.watcher.tick()
    d.room_moves()
    d.watcher.tick()
    d.app.refresh()
    assert d.watcher.approve(d.watcher.pending()[0].plan_id).state == "verified"
    for round_ in (1, 2):
        d.offset += 61                     # a person takes over a minute per pass: GetSchedule is 60 a minute
        for key, kind in EXPECT:
            before = len(events)
            msg = sc[key]()
            assert kind in [e.kind for e in events[before:]], (round_, key, msg)
            assert "No notification" not in msg or kind == "sweep", (round_, key, msg)
    states = [e.data["state"] for e in events if e.kind == "swap"]
    assert states == ["verified", "rolled_back", "rolled_back"]   # the approve, then one per fill


def test_a_scenario_message_lists_only_notifications_that_really_went_out():
    d, _, sc = panel()
    assert "Notifications: Swap proposed." in sc["seat"]()
    msg = sc["seat"]()                                   # pressed again: nothing new happens
    assert "already waiting" in msg and "No notification this time." in msg


def test_only_one_scenario_runs_at_a_time():
    d = D.Demo(port=0)
    assert d._busy.acquire(blocking=False)
    try:
        assert d.run_scenario("seat").startswith("Another scenario is still running")
    finally:
        d._busy.release()


def test_writes_back_on_during_an_outage_is_refused_not_claimed():
    d, events, sc = panel()
    sc["close"]()
    sc["down"]()
    msg = sc["open"]()
    assert "API is down" in msg and d.fake.closed and "resumed" not in msg


def test_the_api_coming_back_does_not_sign_you_in():
    d, _, sc = panel()
    sc["down"]()
    sc["signout"]()
    sc["up"]()
    d.watcher.tick()
    assert d.watcher.read_only                                         # still signed out
    assert sc["signin"]().startswith("Signed in again")


def test_a_demo_reset_is_said_and_journaled_never_silent():
    d, _, sc = panel()
    d.seat_opens()
    d.watcher.tick()
    d.watcher.approve(d.watcher.pending()[0].plan_id)                  # SVS306-R now held, CMP409-R not
    msg = sc["seat"]()
    assert msg.startswith("Demo reset first: CMP409-R held again; SVS306-R released.")
    ops = [(r["op"], r["outcome"]) for r in d.store.journal_entries(D.EVENT, 50)]
    assert ("demo.reset", "held") in ops and ("demo.reset", "released") in ops


def test_a_clock_jump_mentions_an_expired_swap_only_when_one_was_waiting():
    d, _, sc = panel()
    assert "expired" not in sc["down"]()
    sc["up"]()
    sc["seat"]()
    said = [sc["leave"]() for _ in range(3)]       # jumps forward until the offer's 10 minutes pass
    assert any("The waiting swap expired with the jump." in s for s in said)
    assert sum("expired" in s for s in said) == 1


# ---- the clearer dashboard: scenario headings, the result banner, session details


def test_the_demo_page_lists_every_scenario_with_what_happens():
    d, _, sc = panel()
    html = pages.render_demo(d.app.snapshot())
    for s in d.app.scenarios:
        assert f"<b>{pages.e(s['label'])}</b>" in html and pages.e(s["story"]) in html
    groups = {"Seats", "Your schedule", "On site", "AWS and the network"}
    assert {s["group"] for s in d.app.scenarios} == groups
    assert 'data-scenario="tour"' in html and "Pick a scenario" in html
    assert "data-scenario" not in pages.render_dashboard(d.app.snapshot(), demo=True)


def test_a_seat_opening_shows_the_proposal_on_the_week_and_the_phone_card():
    d, _, sc = panel()
    sc["seat"]()
    last = d.app.snapshot()["view"]["last"]
    assert last["phone"] == [{"title": "Swap proposed", "body": last["phone"][0]["body"], "buttons": True}]
    assert [(c["kind"], c["text"].split()[0]) for c in last["changes"]] == [("proposed", "SVS306-R")]
    html = pages.render_demo(d.app.snapshot())
    assert "What AWS did" in html and "Your phone" in html and '<div class="nb">' in html
    assert "PROPOSED" in html and "Before" in html and "After" in html
    week = pages.render_dashboard(d.app.snapshot(), demo=True)
    assert 'class="blk b-proposed hl' in week and '<em class="mk mk-proposed">PROPOSED</em>' in week


def test_the_guided_tour_runs_five_steps_in_order_and_starts_again():
    d = D.Demo(port=0)
    d.watcher.tick()
    tour = dict((k, fn) for k, _, _, fn in d.scenarios())["tour"]
    steps = []
    for _ in range(6):
        tour()
        steps.append((d.app.last_scenario["step"], d.app.last_scenario["label"]))
    assert [s for s, _ in steps] == [f"Tour step {i} of 5" for i in (1, 2, 3, 4, 5, 1)]
    assert [label for _, label in steps][:5] == ["A seat opens", "The new seat fills mid-swap",
                                                 "AWS adds a repeat sitting", "AWS switches writes off",
                                                 "AWS switches writes back on"]


def test_every_week_block_opens_a_detail_panel_with_its_facts(demo):
    snap = demo.app.snapshot()
    html = pages.render_dashboard(snap)
    blocks = snap["view"]["week"]["blocks"]
    for b in blocks:
        assert f'href="#s-{b["i"]}"' in html and f'id="s-{b["i"]}"' in html
    cmp = next(b for b in blocks if b["code"] == "CMP409-R")
    rows = dict(cmp["detail"]["rows"])
    assert rows["When"].startswith("Monday 30 November, 13:00") and "MGM Grand" in rows["Where"]
    assert rows["Leave"].startswith("12:") and cmp["detail"]["status"] == "You hold this seat."
    assert any(x["code"] == "CMP409-R1" for x in cmp["detail"]["sittings"])
    ant = next(b for b in blocks if b["code"] == "ANT335")
    assert "Queue or go" in dict(ant["detail"]["rows"]) and "Tight walk" in dict(ant["detail"]["rows"])
    for sid in ids(demo):
        assert sid not in html


def test_week_changes_from_scenarios_are_labelled_before_and_after():
    d, _, sc = panel()
    sc["repeat"]()
    last = d.app.snapshot()["view"]["last"]
    [new] = [c for c in last["changes"] if c["kind"] == "new"]
    after = [r for day in last["days"] for r in day["after"]]
    assert any(r["mark"] and r["mark"]["kind"] == "new" for r in after)
    assert "<em class=\"mk mk-new\">NEW</em>" in pages.render_dashboard(d.app.snapshot(), demo=True)
    sc["fill"]()                                         # the swap rolls back: the week ends as it began
    assert d.app.snapshot()["view"]["last"]["changes"] == []
    assert "Nothing on your week changed." in pages.render_demo(d.app.snapshot())
    sc["remove"]()
    assert [c["kind"] for c in d.app.snapshot()["view"]["last"]["changes"]] == ["removed"]
    css = (pathlib.Path(pages.__file__).parent / "static" / "app.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_an_approved_swap_leaves_a_ghost_of_the_released_seat():
    d, _, sc = panel()
    sc["seat"]()
    [p] = d.watcher.pending()
    d.watcher.approve(p.plan_id)
    d.app.refresh()
    kinds = {c["kind"]: c["text"] for c in d.app.snapshot()["view"]["recent"]}
    assert kinds["swapped_in"].startswith("SVS306-R") and kinds["released"].startswith("CMP409-R")
    week = pages.render_dashboard(d.app.snapshot(), demo=True)
    assert '<em class="mk mk-released">RELEASED</em><b>CMP409-R</b>' in week and "SWAPPED IN" in week
    assert "Just changed" in week
    for sid in ids(d):
        assert sid not in week


def test_the_demo_page_does_not_exist_on_a_real_server():
    d = D.Demo(port=0)
    d.app.demo = False
    d.watcher.tick()
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=30)
        assert wait_until(lambda: d.app.snapshot().get("view"))
        assert http.get("/demo").status_code in (403, 404)
        assert http.get("/activity").status_code in (200, 303, 403)
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def test_the_tour_button_says_which_step_comes_next_even_after_another_scenario():
    d, _, sc = panel()
    tour = dict((k, fn) for k, _, _, fn in d.scenarios())["tour"]
    assert d.app.snapshot()["view"]["tour"] == "Start the guided tour"
    tour()
    sc["move"]()
    assert d.app.snapshot()["view"]["tour"] == "Guided tour: step 2 of 5"
    for _ in range(4):
        tour()
    assert d.app.snapshot()["view"]["tour"] == "Start the guided tour"


def test_a_detail_panel_for_a_session_with_little_data_says_not_set():
    d = D.Demo(port=0)
    d.watcher.tick()
    bare = Session.model_validate({"sessionId": "bare-1", "abbreviation": "XYZ100", "title": "Bare talk",
                                   "isReservable": True,
                                   "sessionTime": {"date": "2026-12-01", "time": "15:00", "length": "60"}})
    d.fake.add_session(bare)
    d.fake.schedule.reserved.add("bare-1")
    d.watcher.tick()
    b = next(x for x in d.app.snapshot()["view"]["week"]["blocks"] if x["code"] == "XYZ100")
    rows = dict(b["detail"]["rows"])
    assert rows["Where"] == rows["Type"] == "Not set" and b["detail"]["sittings"] == []
    assert "bare-1" not in pages.render_dashboard(d.app.snapshot(), demo=True)


def test_an_empty_week_renders_without_blocks_or_panels():
    d = D.Demo(port=0)
    d.fake.sessions.clear()
    d.fake.schedule.reserved.clear()
    d.watcher.tick()
    snap = d.app.snapshot()
    html = pages.render_dashboard(snap, demo=True)
    assert snap["view"]["week"]["blocks"] == [] and 'class="detail"' not in html


def test_no_reminder_goes_out_before_the_first_sweep():
    d = D.Demo(port=0)
    events = []
    d.watcher.subscribe(events.append)
    tuesday_7am = datetime.fromisoformat("2026-12-01T07:00").replace(tzinfo=VEGAS).timestamp()
    d.offset += tuesday_7am - d.clock()
    d.app.refresh()
    assert not [e for e in events if e.kind == "briefing"]
    d.watcher.tick()
    assert [e.data["title"] for e in events if e.kind == "briefing"] == ["Today: 1 session"]


def test_a_failed_first_sweep_sends_no_free_day_briefing():
    d = D.Demo(port=0)
    events = []
    d.watcher.subscribe(events.append)
    d.offset += datetime.fromisoformat("2026-12-01T07:00").replace(tzinfo=VEGAS).timestamp() - d.clock()
    d.fake.fail_next("ListSessions", 503)
    d.watcher.tick()
    assert not [e for e in events if e.kind == "briefing"]
    d.watcher.tick()
    assert [e.data["title"] for e in events if e.kind == "briefing"] == ["Today: 1 session"]


# ---- QA: ghosts and the now line on the week grid


def _vegas_ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=VEGAS).timestamp()


def _qa_session(sid, code, time_="10:00"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": code, "title": f"Title {code}", "type": "Breakout session",
        "venue": "MGM Grand", "room": "Level 1 | Grand 117", "isReservable": True,
        "seatAvailability": "available",
        "sessionTime": {"date": "2026-12-01", "time": time_, "length": "60"}})


def test_a_released_session_that_is_not_a_target_leaves_a_ghost_block():
    d, _, _ = panel()
    t = d.app.tracker
    now = _vegas_ts(2026, 12, 1, 9)
    t.prev = None
    t.look(now - 60, [_qa_session("qa-gone", "QAX101")], [])
    t.look(now - 30, [], [])
    w = pages._week(d.app, [], [], [], now)
    [g] = [b for b in w["blocks"] if b["code"] == "QAX101"]
    assert g["kind"] == "ghost" and g["mark"]["kind"] == "released" and "detail" in g
    assert "qa-gone" not in json.dumps(g, default=str)
    later = pages._week(d.app, [], [], [], now - 30 + 15 * 60)       # the ghost fades with the label
    assert not [b for b in later["blocks"] if b["code"] == "QAX101"]


def test_a_released_session_still_on_the_week_gets_no_second_ghost():
    d, _, _ = panel()
    t = d.app.tracker
    now = _vegas_ts(2026, 12, 1, 9)
    s = _qa_session("qa-want", "QAW202")
    t.prev = None
    t.look(now - 60, [s], [])
    t.look(now - 30, [], [])
    w = pages._week(d.app, [], [("t", [s])], [], now)                # still wanted, so drawn as wanted
    blocks = [b for b in w["blocks"] if b["code"] == "QAW202"]
    assert [b["kind"] for b in blocks] == ["wanted"] and blocks[0]["mark"]["kind"] == "released"


def test_the_now_line_shows_only_inside_the_event_days_and_hours():
    d, _, _ = panel()
    inside = pages._week(d.app, [], [], [], _vegas_ts(2026, 12, 1, 10, 30))
    assert inside["now"] == {"day": 1, "top": pages._pct(10 * 60 + 30), "label": "10:30"}
    assert pages._week(d.app, [], [], [], _vegas_ts(2026, 11, 20, 10))["now"] is None
    assert pages._week(d.app, [], [], [], _vegas_ts(2026, 12, 1, 19))["now"] is None
    assert pages._week(d.app, [], [], [], _vegas_ts(2026, 12, 1, 7, 59))["now"] is None


def test_stale_blocks_offer_one_update_that_rewrites_them_and_reads_back():
    d, events, sc = panel()
    server = S.make_server(d.app)
    S.run(d.app, server)
    try:
        msg = d.app.run_scenario("stale")
        assert "Leave-now blocks out of date" in msg
        state = d.app.snapshot()["view"]["blocks"]
        assert state and state["update"] >= 1
        today = pages.render_today(d.app.snapshot(), demo=True)
        assert f'data-update="{state["token"]}"' in today
        assert d.app.update_blocks("Made_up_token_0123456789") is None        # a stranger's token: nothing
        res = d.app.update_blocks(state["token"])
        assert res["ok"] and "read back" in res["message"]
        assert d.app.update_blocks(state["token"]) is None                    # single use
        assert d.app.snapshot()["view"]["blocks"] is None
        done = [e for e in events if e.kind == "blocks_done"]
        assert done and done[-1].data["ok"]
        ops = d.watcher.submit(lambda: [r["op"] for r in d.store.journal_entries(D.EVENT, 50)]).result(30)
        assert "UpdatePersonalTime" in ops and "GetSchedule" in ops
    finally:
        d.watcher.stop()
        server.server_close()


def test_update_from_the_notification_runs_once_and_unknown_tokens_cost_nothing():
    d, events, sc = panel()
    server = S.make_server(d.app)
    S.run(d.app, server)
    try:
        d.app.run_scenario("stale")
        token = d.app.snapshot()["view"]["blocks"]["token"]
        writes = d.fake.counts.get("UpdatePersonalTime", 0)
        assert d.app.handle_reply("update", "Made_up_token_0123456789") is None
        assert d.fake.counts.get("UpdatePersonalTime", 0) == writes
        assert d.app.handle_reply("update", token) is None
        assert wait_until(lambda: [e for e in events if e.kind == "blocks_done"])   # runs in the background
        after = d.fake.counts.get("UpdatePersonalTime", 0)
        assert after > writes
        for _ in range(3):
            assert d.app.handle_reply("update", token) is None
        assert d.fake.counts.get("UpdatePersonalTime", 0) == after
        assert len([e for e in events if e.kind == "blocks_done"]) == 1
    finally:
        d.watcher.stop()
        server.server_close()


def test_no_stale_blocks_offer_for_an_attendee_who_never_used_them():
    d, events, sc = panel()
    d.fake.schedule.personal_time = {k: v for k, v in d.fake.schedule.personal_time.items()
                                     if k == "demo-dinner"}
    d.watcher.tick()
    assert d.app.snapshot()["view"]["blocks"] is None and not [e for e in events if e.kind == "blocks"]


def _stale_offer():
    """A served demo with a stale-blocks offer up. Returns the stack, events, server and token."""
    d, events, sc = panel()
    server = S.make_server(d.app)
    S.run(d.app, server)
    d.app.run_scenario("stale")
    return d, events, server, d.app.snapshot()["view"]["blocks"]["token"]


def _guard_readbacks(d):
    rows = d.watcher.submit(lambda: [dict(r) for r in d.store.journal_entries(D.EVENT, 100)]).result(30)
    return [r for r in rows if r["op"] == "GetSchedule" and "guard read-back" in str(r)]


def test_update_blocks_while_writes_are_closed_reports_409_and_reads_back():
    d, events, server, token = _stale_offer()
    try:
        d.fake.closed = True
        writes = d.fake.counts.get("UpdatePersonalTime", 0)
        res = d.app.update_blocks(token)
        assert res is not None and not res["ok"] and "closed" in res["message"]
        done = [e for e in events if e.kind == "blocks_done"]
        assert len(done) == 1 and not done[0].data["ok"] and done[0].data["message"]
        assert d.fake.counts.get("UpdatePersonalTime", 0) == writes + 1   # one attempt, no retry
        assert _guard_readbacks(d)
    finally:
        d.watcher.stop()
        server.server_close()


def test_update_blocks_after_a_5xx_stops_does_not_retry_and_reads_back():
    d, events, server, token = _stale_offer()
    try:
        d.fake.fail_next("UpdatePersonalTime", 503)
        before = d.fake.counts.get("UpdatePersonalTime", 0)
        res = d.app.update_blocks(token)
        assert res is not None and not res["ok"] and "Stopped" in res["message"]
        assert d.fake.counts.get("UpdatePersonalTime", 0) == before + 1
        done = [e for e in events if e.kind == "blocks_done"]
        assert len(done) == 1 and not done[0].data["ok"]
        assert _guard_readbacks(d)
        assert d.app.update_blocks(token) is None                           # the token is spent
    finally:
        d.watcher.stop()
        server.server_close()


def test_sign_in_expiry_withdraws_the_offer_and_a_late_tap_sends_no_write():
    d, events, server, token = _stale_offer()
    try:
        d.fake.fail_next("GetSchedule", 401, times=10_000)   # expires after the offer, before the tap
        before = sum(d.fake.counts.get(op, 0) for op in
                     ("UpdatePersonalTime", "CreatePersonalTime", "DeletePersonalTime"))
        res = d.app.update_blocks(token)
        assert res is not None and not res["ok"] and res["message"]
        done = [e for e in events if e.kind == "blocks_done"]
        assert len(done) == 1 and not done[0].data["ok"]
        after = sum(d.fake.counts.get(op, 0) for op in
                    ("UpdatePersonalTime", "CreatePersonalTime", "DeletePersonalTime"))
        assert after == before
    finally:
        d.watcher.stop()
        server.server_close()


def test_a_read_only_watcher_offers_no_update():
    d, events, sc = panel()
    sc["stale"]()
    token = d.app.snapshot()["view"]["blocks"]["token"]
    sc["signout"]()
    assert d.watcher.read_only and d.app.snapshot()["view"]["blocks"] is None
    assert d.app.update_blocks(token) is None


def test_a_failed_update_is_offered_again_once_writes_reopen():
    d, events, server, token = _stale_offer()
    try:
        d.fake.closed = True
        assert not d.app.update_blocks(token)["ok"]
        d.fake.closed = False
        d.watcher.submit(d.watcher.tick).result(timeout=30)
        again = d.app.snapshot()["view"]["blocks"]
        assert again and again["token"] != token
    finally:
        d.watcher.stop()
        server.server_close()


def test_no_other_sitting_push_when_the_talk_is_held_or_the_sitting_is_past():
    d, events, sc = panel()
    sc["pinned"]()
    other = next(s for s in d.fake.sessions.values() if s.abbreviation == "ARC306-R1")
    held = sorted(d.watcher.last_held)
    assert d.app._other_sittings(held)
    d.fake.schedule.reserved.add(other.session_id)                    # held by hand in the official app
    d.watcher.tick()
    assert d.app._other_sittings(sorted(d.watcher.last_held)) == []
    d.fake.schedule.reserved.discard(other.session_id)
    d.watcher.tick()
    d.offset += 7 * 86400                                            # the week is over
    assert d.app._other_sittings(sorted(d.watcher.last_held)) == []


# ---- Targets, Status, Day


def test_targets_status_and_day_views_show_codes_and_never_a_secret_or_id():
    d = D.Demo(port=0, push_topic="reseat-demo-7f3k9q2m", require_secret=True)
    d.watcher.tick()
    d.seat_opens()
    d.watcher.tick()
    snap = d.app.snapshot()
    targets = pages.render_targets(snap, demo=True)
    status = pages.render_status(snap, demo=True, version="9.9.9")
    day = pages.render_day(snap, 0, demo=True)
    v = snap["view"]
    names = [t["name"] for t in v["targets"]]
    assert names[0] == "SVS306" and "ARC306" in names               # the pinned target named by its code
    assert next(t for t in v["targets"] if t["name"] == "SVS306")["state"] == "proposed"
    assert "only the sittings you picked" in targets and "Swap proposed: SVS306-R" in targets
    assert "Quota left this minute" in status and "ListSessions" in status and "9.9.9" in status
    assert "Swap now and Keep in the notification" in status
    assert "Mon 30" in day and "CMP409-R" in day and "Leave" in day
    page_text = targets + status + day + json.dumps(snap, default=str)
    assert "reseat-demo-7f3k9q2m" not in page_text and d.secret not in page_text
    for sid in ids(d):
        assert sid not in targets + status + day
    assert pages.render_day(snap, 99, demo=True) == pages.render_day(snap, 0, demo=True)
    assert 'href="/day?d=2"' in pages.render_dashboard(snap, demo=True)
    d.watcher.stop()


def test_the_new_views_are_served_behind_the_same_access():
    d = D.Demo(port=0)
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert wait_until(lambda: d.app.snapshot().get("view"))
        http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=10)
        for path in ("/targets", "/status", "/day?d=1", "/day?d=x", "/day?d=12345", "/day?d=%C2%B2",
                     "/day?d=-1"):
            r = http.get(path)
            assert r.status_code == 200 and "re:Seat" in r.text, path
        assert http.get("/targets", headers={"Host": "evil.example"}).status_code != 200
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


# ---- QA: Targets, Status, Day edge cases, and target names


def test_targets_with_backups_or_nothing_in_the_catalog_never_show_an_id():
    from reseat import rules as R
    d = D.Demo(port=0)
    d.watcher.tick()
    d.app.rules = R.parse("targets:\n- code: ZZZ999\n  backups: [ARC306]\n- code: QQQ111\n"
                          "- session_id: not-a-real-session-id-0001\n")
    t = pages._targets(d.app, sorted(d.watcher.last_held), d.watcher.pending())
    assert [x["name"] for x in t] == ["ZZZ999", "QQQ111", "A pinned sitting not in the catalog yet"]
    assert t[0]["notes"] == ["backups: ARC306"] and t[0]["sittings"]
    assert all(s["backup"] and s["code"].startswith("ARC306") for s in t[0]["sittings"])
    assert t[1]["state"] == "none" and t[2]["state"] == "none" and t[2]["sittings"] == []
    snap = d.app.snapshot()
    snap = {**snap, "view": {**snap["view"], "targets": t}}
    html = pages.render_targets(snap, demo=True)
    assert "No sitting in the catalog yet" in html and "not-a-real-session-id-0001" not in html
    empty = pages.render_targets({**snap, "view": {**snap["view"], "targets": []}}, demo=True)
    assert "No targets yet" in empty
    d.watcher.stop()


def test_status_rows_for_push_off_offline_writes_closed_and_read_only():
    d = D.Demo(port=0)
    d.watcher.tick()
    now = d.clock()
    rows = dict(pages._health(d.app, now, None)["rows"])
    assert rows["Push"].startswith("Off") and rows["Watcher"] == "Watching"
    assert rows["Last sweep"] == "None yet"
    assert rows["Events API"] == "Reachable" and rows["Bookings"] == "Open" and rows["Quiet hours"] == "None"
    w = d.watcher
    w.read_only, w._down_since, w.writes_closed_until = True, now - 120, now + 60
    rows = dict(pages._health(d.app, now, now)["rows"])
    assert rows["Watcher"].startswith("Reading only") and rows["Events API"].startswith("Unreachable since")
    assert rows["Bookings"].startswith("Closed (409)")
    on = D.Demo(port=0, push_topic="reseat-demo-7f3k9q2m")
    on.watcher.tick()
    rows = dict(pages._health(on.app, now, None)["rows"])
    assert rows["Push"].startswith("On") and "reseat-demo-7f3k9q2m" not in json.dumps(rows)
    d.watcher.stop()
    on.watcher.stop()


def test_day_view_shows_a_ghost_and_a_mark_in_time_order():
    d = D.Demo(port=0)
    d.watcher.tick()
    snap = d.app.snapshot()
    week = snap["view"]["week"]
    first = next(b for b in week["blocks"] if b["day"] == 0)
    ghost = {**first, "kind": "ghost", "code": "GHO101", "top": -1,
             "mark": {"kind": "released", "label": "Released"},
             "detail": {"code": "GHO101", "title": "Gone talk", "rows": [("Was", "Venetian")],
                        "status": "Released at 09:00.", "history": [], "sittings": []}}
    snap = {**snap, "view": {**snap["view"], "week": {**week, "blocks": week["blocks"] + [ghost]}}}
    html = pages.render_day(snap, 0, demo=True)
    assert "d-ghost" in html and "mk-released" in html and "Gone talk" in html
    assert html.index("GHO101") < html.index(first["code"] + " ·")
    d.watcher.stop()


def test_target_name_shows_codes_and_never_an_id():
    from reseat.router import target_name
    d = D.Demo(port=0)
    d.watcher.tick()
    for code in ("AIM310-R1", "KEY001", "ARC306", "SVS306-R"):
        assert target_name(code, d.store, D.EVENT) == code
    sid = next(iter(d.fake.sessions))
    assert target_name(sid, d.store, D.EVENT) == d.store.get(D.EVENT, sid).abbreviation
    assert target_name("arc306", d.store, D.EVENT) == "arc306"       # a code, whatever its case
    for unknown in ("not-a-real-session-id-0001", ""):
        assert target_name(unknown, d.store, D.EVENT) == "a pinned sitting"
    d.watcher.stop()


def test_every_page_has_the_same_menu_under_its_header():
    d, _, sc = panel()
    sc["seat"]()
    snap = d.app.snapshot()
    rendered = {"week": pages.render_dashboard(snap, demo=True), "today": pages.render_today(snap, True),
                "approve": pages.render_approve(snap, True), "activity": pages.render_activity(snap, True),
                "targets": pages.render_targets(snap, True), "status": pages.render_status(snap, True),
                "demo": pages.render_demo(snap), "week-day": pages.render_day(snap, 1, True)}
    for name, html in rendered.items():
        assert html.count('<nav class="tabs"') == 1, name
        assert html.index("</header>") < html.index('<nav class="tabs"'), name       # right under the header
        for href in ('href="/"', 'href="/today"', 'href="/approve"', 'href="/activity"', 'href="/targets"',
                     'href="/status"', 'href="/demo"'):
            assert href in html, (name, href)
        current = {"week-day": "week"}.get(name, name)
        here = {"week": 'href="/"', "today": 'href="/today"', "approve": 'href="/approve"',
                "activity": 'href="/activity"', "targets": 'href="/targets"', "status": 'href="/status"',
                "demo": 'href="/demo"'}[current]
        assert f'{here} aria-current=page' in html, name
    assert 'Approve <span class="count">1</span>' in rendered["week"]
    assert '<details class="more here">' in rendered["targets"]
    assert 'href="/demo"' not in pages.render_dashboard(snap, demo=False)


def test_static_site_pages_each_have_one_menu_with_sibling_links(tmp_path):
    import runpy
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "build_site.py"
    site = runpy.run_path(str(script), run_name="site")
    for n in site["build"](tmp_path):
        html = (tmp_path / n).read_text(encoding="utf-8")
        assert html.count('<nav class="tabs"') == 1, n
        nav = html[html.index('<nav class="tabs"'):html.index("</nav>", html.index('<nav class="tabs"'))]
        for page in ("index.html", "today.html", "approve.html", "activity.html", "targets.html",
                     "status.html", "demo.html"):
            assert f'href="{page}"' in nav, (n, page)
        assert nav.count('href="targets.html"') == 2 and "<details" in nav, n    # row link and More item


def test_today_and_approve_render_two_balanced_columns_empty_and_full():
    import copy
    d, _, sc = panel()
    sc["seat"]()
    full = d.app.snapshot()
    v = full["view"]
    assert v["cards"] and v["today"]["hero"]
    if not v.get("bookings"):
        v["bookings"] = [{"at": "09:12", "code": "ABC101", "when": "Tue 10:00", "venue": "Wynn"}]
    empty = copy.deepcopy(full)
    ev = empty["view"]
    ev["cards"], ev["bookings"], ev["blocks"] = [], [], None
    ev["today"].update(hero=None, next=[], wanted=[])
    for snap in (full, empty):
        for html in (pages.render_today(snap, True), pages.render_approve(snap, True, push=False)):
            assert html.count('<div class="two">') == 1 and html.count('<div class="col">') == 2
            assert html.count("<div") == html.count("</div>")
            assert html.count("<section") == html.count("</section>")
            assert html.count("<main") == html.count("</main>") == 1
            two = html.index('<div class="two">')
            assert html.index("</header>") < two < html.index("</main>")
            assert 'class="ptop"' not in html and 'class="phone"' not in html
    assert "Nothing booked in the last day." in pages.render_approve(empty, True)
    assert "Booked today" in pages.render_approve(full, True) and "BOOKED" in pages.render_approve(full, True)
    assert "data-leave=" in pages.render_today(full, True) and "data-leave-label" in pages.render_today(full)
    assert "data-leave=" not in pages.render_today(empty, True)


@pytest.mark.parametrize("mins,text", [
    (1, "1 min"), (59, "59 min"), (60, "1 h"), (61, "1 h 1 min"), (320, "5 h 20 min"),
    (1440, "1 day"), (1500, "1 day 1 h"), (3180, "2 days 5 h"), (10079, "6 days 23 h"),
    (10080, "7 days"), (69668, "48 days"),
])
def test_leave_countdown_reads_in_days_hours_and_minutes(mins, text):
    """Seen live on 13 October: 'LEAVE IN 69668 min' for a session 48 days away."""
    assert pages.wait_text(mins) == text


def test_the_live_countdown_uses_the_same_rule():
    js = (pathlib.Path(pages.__file__).parent / "static" / "app.js").read_text()
    assert "waitText(mins)" in js and 'mins + " min" : "now"' not in js


@pytest.mark.parametrize("mins,text", [
    (1439, "23 h 59 min"), (1441, "1 day"), (1499, "1 day"), (2880, "2 days"),
    (2940, "2 days 1 h"), (10140, "7 days"), (10199, "7 days"),
])
def test_leave_countdown_edges_inside_a_unit(mins, text):
    """Minutes are dropped from a day count, hours from a week or more. Never 0 h, never '1 days'."""
    assert pages.wait_text(mins) == text


def test_today_hero_shows_the_readable_countdown_and_leave_now():
    d, _, sc = panel()
    sc["seat"]()
    snap = d.app.snapshot()
    hero = snap["view"]["today"]["hero"]
    for secs, want in [(69668 * 60, ">48 days<"), (61, ">2 min<"), (60, ">1 min<"),
                       (5 * 3600 + 20 * 60, ">5 h 20 min<"), (0, ">now<"), (-300, ">now<")]:
        hero["leave_at"] = snap["now"] + secs
        html = pages.render_today(snap, True)
        assert want in html, (secs, want)
        assert ("LEAVE NOW" in html) == (secs <= 0), secs
    d.watcher.stop()


def test_the_page_and_the_live_countdown_share_every_boundary():
    """app.js waitText must keep pages.wait_text's breakpoints: 60 min, 1440 min, 7 days, plural at 2."""
    js = (pathlib.Path(pages.__file__).parent / "static" / "app.js").read_text()
    body = js[js.index("function waitText"):js.index("function tick")]
    for piece in ("mins < 60", "mins < 1440", "d < 7 && h", "d > 1", '" days"', '" day"', '" h "', '" min"'):
        assert piece in body, piece


def test_demo_and_blocks_update_buttons_refuse_a_foreign_origin_and_accept_the_page(served):
    """Every remaining POST route on the demo page: a scenario button and the blocks Update button."""
    d, http = served
    page = str(http.base_url).rstrip("/")
    key = d.app.scenarios[0]["key"]
    for origin in ("null", "https://attacker.example"):
        assert http.post(f"/demo/{key}", headers={"Origin": origin, "X-Reseat": "1"}).status_code == 403
    assert http.post(f"/demo/{key}", headers={"Origin": page, "X-Reseat": "1"}).status_code == 200


def test_blocks_update_over_http_from_the_page_origin():
    d, events, server, token = _stale_offer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{d.app.port}", timeout=60)
        page = str(http.base_url).rstrip("/")
        for origin in ("null", "https://attacker.example"):
            r = http.post(f"/guard/{token}", headers={"Origin": origin, "X-Reseat": "1"})
            assert r.status_code == 403
        assert d.app.blocks_state and d.app.blocks_state["token"] == token          # offer kept
        r = http.post(f"/guard/{token}", headers={"Origin": page, "X-Reseat": "1"})
        assert r.status_code == 200 and r.json()["ok"], r.json()
        assert _guard_readbacks(d)
        assert http.post(f"/guard/{token}", headers={"Origin": page, "X-Reseat": "1"}).status_code == 404
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()
