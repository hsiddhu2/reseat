"""Email and text alerts through the attendee's own SNS topic. No network: a stand-in client."""

import builtins
import threading

import pytest
from typer.testing import CliRunner

from reseat import cli, sns
from reseat import rules as R
from reseat import serve as S
from reseat.store import Store
from reseat.watcher import WatchEvent

ARN = "arn:aws:sns:us-west-2:123456789012:reseat-alerts"


class FakeSns:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def publish(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied for arn:aws:iam::123456789012:user/someone")
        self.calls.append(kw)
        return {"MessageId": "m1"}


def offer():
    return WatchEvent("offer", 0, {"plan_id": "p" * 22, "session_id": "N1", "reason": "added",
                                   "code": "NEW101", "title": "Title", "band": "available", "expires": 600})


def test_an_offer_is_published_with_the_push_words_and_a_link_to_approve():
    fake = FakeSns()
    n = sns.SnsNotifier(ARN, code_title=lambda sid: "NEW101 Title", click_base="http://laptop:8490",
                        client=fake)
    n(offer())
    assert n.publish(*n._q.get_nowait())
    [call] = fake.calls
    assert call["TopicArn"] == ARN and call["Subject"] == "re:Seat: New session: NEW101"
    assert "NEW101 Title" in call["Message"] and "http://laptop:8490/approve" in call["Message"]
    assert "N1," not in call["Message"] and "p" * 22 not in call["Message"]    # no ids, no plan id


def test_only_alerts_worth_an_email_are_queued():
    n = sns.SnsNotifier(ARN, client=FakeSns())
    for kind in ("sweep", "leave_soon", "briefing", "back", "writes"):
        n(WatchEvent(kind, 0, {"message": "x", "count": 1}))
    assert n._q.empty()


def test_a_failed_publish_is_counted_and_never_keeps_the_error_text():
    n = sns.SnsNotifier(ARN, client=FakeSns(fail=True))
    assert n.publish("Seat booked", "x") is False
    assert n.info() == {"sent": 0, "failures": 1, "last_error": "RuntimeError"}


def test_subject_is_ascii_one_line_and_capped():
    fake = FakeSns()
    n = sns.SnsNotifier(ARN, client=fake)
    n.publish("Café\nnew " + "x" * 200, "body")
    subj = fake.calls[0]["Subject"]
    assert len(subj) == sns.SUBJECT_MAX and "\n" not in subj and subj.isascii()


def test_without_boto3_it_says_how_to_install_and_counts_a_failure(monkeypatch):
    real = builtins.__import__

    def no_boto(name, *a, **k):
        if name == "boto3":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_boto)
    with pytest.raises(RuntimeError, match=r"reseat\[aws\]"):
        sns._client(None, "us-west-2")
    n = sns.SnsNotifier(ARN)
    assert n.publish("t", "b") is False and n.failures == 1


def test_region_comes_from_the_topic_arn():
    assert sns.region_of(ARN) == "us-west-2"


@pytest.mark.parametrize("arn, ok", [
    (ARN, True),
    ("arn:aws-us-gov:sns:us-gov-west-1:123456789012:t", True),
    ("arn:aws:sns:us-west-2:12345:t", False),
    ("arn:aws:sqs:us-west-2:123456789012:t", False),
    ("https://evil.example/arn:aws:sns:us-west-2:123456789012:t", False),
])
def test_rules_accept_only_an_sns_topic_arn(arn, ok):
    body = f"sns_topic_arn: \"{arn}\"\naws_profile: shared\n"
    if ok:
        assert R.parse(body).sns_topic_arn == arn
    else:
        with pytest.raises(R.RulesError):
            R.parse(body)


def test_cli_serve_wires_the_notifier_from_the_rules_file_and_sends_no_token(monkeypatch, client, fake):
    made = {}

    class Notifier(sns.SnsNotifier):
        def __init__(self, topic_arn, **kw):
            super().__init__(topic_arn, client=FakeSns(), **{k: v for k, v in kw.items() if k != "client"})
            made["n"], made["kw"] = self, kw

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
    rules = R.parse(f"sns_topic_arn: \"{ARN}\"\naws_profile: shared\ntargets:\n- code: ARC301\n")
    monkeypatch.setattr(cli, "_rules", lambda path=None: rules)
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: Store(":memory:"))
    monkeypatch.setattr(sns, "SnsNotifier", Notifier)
    monkeypatch.setattr(S, "make_server", lambda app: Server())
    monkeypatch.setattr(S, "run", run)
    r = CliRunner().invoke(cli.app, ["serve"])
    assert r.exit_code == 0, r.output
    assert made["started"] and made["n"].topic_arn == ARN and made["kw"]["profile"] == "shared"
    assert made["kw"]["click_base"] is None                    # loopback: no link a phone cannot open
    assert "Email and text alerts are on" in r.output
    assert made["n"]._q.get_nowait() is None              # serve stopped the notifier on its way out
    made["n"](offer())
    title, body = made["n"]._q.get_nowait()
    assert fake.token not in title + body


def test_cli_serve_without_a_topic_makes_no_notifier(monkeypatch, client):
    monkeypatch.setattr(cli, "_rules", lambda path=None: R.parse("targets:\n- code: ARC301\n"))
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: Store(":memory:"))
    monkeypatch.setattr(sns, "SnsNotifier", lambda *a, **k: pytest.fail("no SNS without a topic"))

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(S, "make_server", lambda app: Server())
    monkeypatch.setattr(S, "run", lambda app, server: None)
    r = CliRunner().invoke(cli.app, ["serve"])
    assert r.exit_code == 0 and "Email and text alerts" not in r.output
