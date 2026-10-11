"""Tailscale Serve for the phone: reseat serve --tailscale listens on 127.0.0.1 and Tailscale forwards to it.
The Tailscale command line is replaced by a recorder. No test runs the real tool."""

import json
import subprocess
import threading

import pytest
from typer.testing import CliRunner

from reseat import cli
from reseat import demo as D
from reseat import serve as S
from reseat import tailnet as T

NAME = "my-laptop.tail1234.ts.net"


class Recorder:
    def __init__(self, status=None, serve_status="", fail=None):
        self.calls = []
        running = {"BackendState": "Running", "Self": {"DNSName": NAME + "."}}
        self.status = status if status is not None else running
        self.serve_status, self.fail = serve_status, fail or set()

    def __call__(self, args):
        args = list(args[1:])
        self.calls.append(args)
        code, out = 0, ""
        if args[:2] == ["status", "--json"]:
            out = json.dumps(self.status)
        elif args[:3] == ["serve", "status", "--json"]:
            out = self.serve_status
        if tuple(args[:2]) in self.fail:
            code = 1
        return subprocess.CompletedProcess(args, code, out, "boom" if code else "")


def test_name_comes_from_tailscale_status():
    assert T.Tailnet("ts", Recorder()).name() == NAME


@pytest.mark.parametrize("status,why", [
    ({"BackendState": "Stopped"}, "not connected"),
    ({"BackendState": "Running", "Self": {}}, "MagicDNS"),
])
def test_name_explains_what_to_fix(status, why):
    with pytest.raises(T.TailnetError, match=why):
        T.Tailnet("ts", Recorder(status=status)).name()


def test_forward_and_stop_touch_only_our_port():
    rec = Recorder()
    tn = T.Tailnet("ts", rec)
    tn.forward(8491)
    tn.stop(8491)
    assert rec.calls[1] == ["serve", "--bg", "--http=8491", "http://127.0.0.1:8491"]
    assert rec.calls[2] == ["serve", "--http=8491", "off"]


def test_forward_refuses_a_port_tailscale_already_sends_elsewhere():
    rec = Recorder(serve_status='{"TCP": {"8491": {}}, "Web": {"x:8491": {"Handlers": {"/": '
                                '{"Proxy": "http://127.0.0.1:3000"}}}}}')
    with pytest.raises(T.TailnetError, match="already forwards port 8491"):
        T.Tailnet("ts", rec).forward(8491)
    assert not any(c[:2] == ["serve", "--bg"] for c in rec.calls)


def test_forward_failure_is_explained():
    with pytest.raises(T.TailnetError, match="would not forward"):
        T.Tailnet("ts", Recorder(fail={("serve", "--bg")})).forward(8491)


def test_missing_tailscale_says_how_to_install(monkeypatch):
    monkeypatch.setattr(T, "find_cli", lambda: None)
    with pytest.raises(T.TailnetError, match="not installed"):
        T.Tailnet.locate()


def _fake_server(monkeypatch, made):
    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            made["closed"] = True

    def run(app, server):
        t = threading.Thread(target=lambda: None)
        t.start()
        return t
    monkeypatch.setattr(S, "make_server", lambda app: made.setdefault("app", app) and Server())
    monkeypatch.setattr(S, "run", run)
    monkeypatch.setattr(S, "self_check", lambda host, port, timeout=3.0: True)
    monkeypatch.setattr(D.Demo, "start_script", lambda self: None)


def test_cli_tailscale_listens_on_loopback_links_the_tailnet_name_and_cleans_up(monkeypatch):
    rec, made = Recorder(), {}
    monkeypatch.setattr(T.Tailnet, "locate", classmethod(lambda cls, run=None: cls("ts", rec)))
    _fake_server(monkeypatch, made)
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--tailscale"])
    assert r.exit_code == 0, r.output
    app = made["app"]
    assert app.host == "127.0.0.1" and app.auth_required            # loopback, but a secret is required
    assert f"http://{NAME}:8491/open?k=" in r.output and "Tailscale forwarding removed." in r.output
    assert ["serve", "--bg", "--http=8491", "http://127.0.0.1:8491"] in rec.calls
    assert rec.calls[-1] == ["serve", "--http=8491", "off"]


def test_cli_tailscale_with_host_is_refused():
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--tailscale", "--host", "100.64.0.7"])
    assert r.exit_code == 2 and "Leave out --host" in r.output


def test_cli_tailscale_for_real_seats_needs_a_secret(monkeypatch):
    from reseat import rules as R
    rec = Recorder()
    monkeypatch.setattr(T.Tailnet, "locate", classmethod(lambda cls, run=None: cls("ts", rec)))
    monkeypatch.setattr(cli, "_rules", lambda: R.parse("targets: []\n"))
    monkeypatch.setattr(cli, "_client", lambda: None)
    monkeypatch.setattr(cli, "_store", lambda: None)
    r = CliRunner().invoke(cli.app, ["serve", "--tailscale"])
    assert r.exit_code == 2 and "needs serve_secret" in r.output
    assert not any(c[:2] == ["serve", "--bg"] for c in rec.calls)      # nothing forwarded


def _web(proxy):
    return json.dumps({"TCP": {"8491": {}}, "Web": {"n:8491": {"Handlers": {"/": {"Proxy": proxy}}}}})


@pytest.mark.parametrize("status,foreign", [
    ("", False),                                            # nothing configured
    ("not json", False),
    (_web("http://127.0.0.1:8491"), False),                 # our own forwarding, left from an earlier run
    (_web("http://127.0.0.1:3000"), True),
    ('{"TCP": {"8491": {"TCPForward": "127.0.0.1:22"}}}', True),     # a raw TCP forward
])
def test_only_another_use_of_our_port_counts_as_foreign(status, foreign):
    assert T._foreign(8491, status) is foreign


def test_port_849_is_not_mistaken_for_8491():
    assert T._foreign(849, _web("http://127.0.0.1:3000")) is False


def test_forwarding_is_removed_even_when_startup_fails_after_it(monkeypatch):
    rec, made = Recorder(), {}
    monkeypatch.setattr(T.Tailnet, "locate", classmethod(lambda cls, run=None: cls("ts", rec)))
    _fake_server(monkeypatch, made)

    def boom(app, server):
        raise RuntimeError("the watcher thread would not start")
    monkeypatch.setattr(S, "run", boom)
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--tailscale"])
    assert r.exit_code != 0 and made["closed"]
    assert rec.calls[-1] == ["serve", "--http=8491", "off"]


def test_a_failed_removal_says_how_to_remove_it(monkeypatch):
    rec, made = Recorder(fail={("serve", "--http=8491")}), {}
    monkeypatch.setattr(T.Tailnet, "locate", classmethod(lambda cls, run=None: cls("ts", rec)))
    _fake_server(monkeypatch, made)
    r = CliRunner().invoke(cli.app, ["serve", "--demo", "--tailscale"])
    assert "Could not remove the Tailscale forwarding" in r.output and "--http=8491 off" in r.output


def test_signal_handlers_are_restored_after_serve(monkeypatch):
    import signal
    before = signal.getsignal(signal.SIGTERM)
    made = {}
    _fake_server(monkeypatch, made)
    CliRunner().invoke(cli.app, ["serve", "--demo"])
    assert signal.getsignal(signal.SIGTERM) == before
