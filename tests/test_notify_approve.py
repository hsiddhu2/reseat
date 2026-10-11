"""Approve from the notification: Swap now and Keep buttons in the push, read back over ntfy.

No test reaches ntfy.sh. The push and the reply stream run against httpx MockTransports.
"""

import json
import threading

import httpx
from typer.testing import CliRunner

from reseat import cli, push
from reseat import demo as D
from reseat import serve as S
from reseat.watcher import WatchEvent

TOPIC = "reseat-demo-7f3k9q2m"
PLAN = "Abc_def-1234567890XY"


def capture():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)
    return seen, httpx.MockTransport(handler)


def proposed(auto=False):
    return WatchEvent("proposed", 0, {"plan_id": PLAN, "wanted_id": "B", "held_id": "A", "auto": auto})


def test_a_swap_proposal_carries_swap_now_and_keep_buttons_when_opted_in():
    seen, transport = capture()
    p = push.Pusher(TOPIC, transport=transport, approve=True)
    p(proposed())
    p.flush()
    h = seen[0].headers
    reply = f"https://ntfy.sh/{push.reply_topic(TOPIC)}"
    assert h["actions"] == (f"http, Swap now, {reply}, method=POST, body=approve {PLAN}, clear=true; "
                            f"http, Keep, {reply}, method=POST, body=skip {PLAN}, clear=true")
    assert h["priority"] == "high" and PLAN not in seen[0].content.decode()


def test_no_buttons_without_opt_in_or_for_an_auto_swap_or_other_events():
    seen, transport = capture()
    off = push.Pusher(TOPIC, transport=transport)
    off(proposed())
    on = push.Pusher(TOPIC, transport=transport, approve=True)
    on(proposed(auto=True))
    on(WatchEvent("booked", 0, {"code": "X", "title": "T"}))
    off.flush()
    on.flush()
    assert all("actions" not in r.headers for r in seen) and len(seen) == 3


def test_the_reply_topic_is_derived_and_does_not_show_the_push_topic():
    r = push.reply_topic(TOPIC)
    assert r == push.reply_topic(TOPIC) and r != push.reply_topic(TOPIC + "x")
    assert TOPIC not in r and push.COMMAND.pattern and 16 <= len(r) <= 64


def test_the_listener_acts_only_on_approve_or_skip_with_a_plan_id():
    got = []
    li = push.ReplyListener(TOPIC, lambda v, p: got.append((v, p)), clock=lambda: 1000)
    for line in ['{"event": "open"}', '{"event": "keepalive"}', "not json",
                 json.dumps({"event": "message", "id": "m1", "message": f"approve {PLAN}"}),
                 json.dumps({"event": "message", "id": "m2", "message": f"skip {PLAN}"}),
                 json.dumps({"event": "message", "id": "m3", "message": "approve ../etc"}),
                 json.dumps({"event": "message", "id": "m4", "message": f"cancel {PLAN}"}),
                 json.dumps({"event": "message", "id": "m5", "message": f"approve {PLAN}; reserve x"})]:
        li.feed(line)
    assert got == [("approve", PLAN), ("skip", PLAN)] and li.since == "m5"


def test_the_listener_starts_from_now_and_resumes_after_the_last_message():
    calls = []
    lines = [json.dumps({"event": "message", "id": "m9", "message": f"approve {PLAN}"})]

    def handler(req):
        calls.append(dict(req.url.params))
        return httpx.Response(200, content="\n".join(lines).encode())
    got = []
    li = push.ReplyListener(TOPIC, lambda v, p: got.append(p), transport=httpx.MockTransport(handler),
                            clock=lambda: 1234)
    li.listen_once()
    li.listen_once()
    assert calls[0]["since"] == "1234" and calls[1]["since"] == "m9"      # never replays old taps
    assert li.url == f"https://ntfy.sh/{push.reply_topic(TOPIC)}/json"


def test_a_handler_error_does_not_end_the_listener():
    def boom(v, p):
        raise RuntimeError("x")
    li = push.ReplyListener(TOPIC, boom, clock=lambda: 0)
    li.feed(json.dumps({"event": "message", "id": "m1", "message": f"approve {PLAN}"}))
    assert li.errors == 1


# ---- the server side: the same approve and skip as the page, once per plan


def stack():
    d = D.Demo(port=0)
    d.watcher.tick()
    d.seat_opens()
    d.watcher.tick()
    server = S.make_server(d.app)
    S.run(d.app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return d, server


def test_swap_now_from_the_notification_runs_the_swap_once_however_often_it_is_tapped():
    d, server = stack()
    try:
        plan = d.watcher.pending()[0].plan_id
        results = [d.app.handle_reply("approve", plan) for _ in range(5)]
        assert results == [None] * 5                         # the swap's own push reports the result
        assert d._codes["SVS306-R"] in d.fake.schedule.reserved
        assert d.fake.counts.get("CancelReservation", 0) == 1
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def test_keep_from_the_notification_dismisses_and_sends_nothing():
    d, server = stack()
    try:
        plan = d.watcher.pending()[0].plan_id
        assert d.app.handle_reply("skip", plan)[0] == "Kept your seat"
        assert d.watcher.pending() == [] and d.fake.counts.get("CancelReservation", 0) == 0
        assert d.app.handle_reply("approve", plan) is None       # already handled: ignored
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def test_a_made_up_or_spent_plan_id_costs_nothing_and_pushes_nothing():
    d, server = stack()
    try:
        jobs = []
        real_submit = d.watcher.submit
        d.watcher.submit = lambda fn: (jobs.append(fn), real_submit(fn))[1]
        for i in range(50):
            assert d.app.handle_reply("approve", f"{i:02d}" + "Z" * 20) is None
        assert jobs == [] and len(d.app._replied) == 0              # no watcher work, nothing remembered
        assert d.fake.counts.get("CancelReservation", 0) == 0
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()


def test_cli_demo_push_turns_on_the_buttons_and_the_listener(monkeypatch):
    made = {}

    class FakePusher(push.Pusher):
        def __init__(self, topic, transport=None, code_title=str, click_base=None, approve=False):
            super().__init__(topic, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             code_title=code_title, click_base=click_base, approve=approve)
            made["p"] = self

        def start(self):
            pass

    class Listener:
        def __init__(self, topic, handle, transport=None, clock=None):
            made["listener"] = self
            self.topic, self.stopped = topic, False

        def start(self):
            made["listening"] = True

        def stop(self):
            self.stopped = True

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
    monkeypatch.setattr(push, "ReplyListener", Listener)
    monkeypatch.setattr(S, "make_server", lambda app: Server())
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: None)
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--push", TOPIC])
    assert r.exit_code == 0, r.output
    assert made["p"].approve and made["listening"] and made["listener"].topic == TOPIC
    assert made["listener"].stopped and "Swap now and Keep buttons are on" in r.output


def test_a_held_back_approve_can_be_tapped_again():
    d, server = stack()
    try:
        plan = d.watcher.pending()[0].plan_id
        d.watcher.read_only = True                     # as if sign-in had expired
        title, body = d.app.handle_reply("approve", plan)
        assert title == "Swap not run" and "Sign in needed" in body
        d.watcher.read_only = False
        assert d.app.handle_reply("approve", plan) is None
        assert d._codes["SVS306-R"] in d.fake.schedule.reserved           # the retry ran the swap
    finally:
        d.watcher.stop()
        server.shutdown()
        server.server_close()
