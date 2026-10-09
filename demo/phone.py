"""Demo 4: the phone page, with demo data, for recording over Tailscale.

    python demo/phone.py --host <the laptop's Tailscale address>

Prints a one-time link. Open it on the phone, then tap Approve on the proposed swap.
The demo clock is set to the first morning of re:Invent, so the leave-now countdown runs.
It listens on port 8491, so its cookie never replaces the real phone page's on 8490.
Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

from _harness import EV, banner, catalog, con, setup, sid

from reseat import rules as R
from reseat import serve as S
from reseat.swap import Swap
from reseat.watcher import Watcher

DEMO_PORT = 8491
DEMO_NOW = datetime(2026, 11, 30, 10, 15, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


def start(host: str, port: int) -> tuple[S.App, ThreadingHTTPServer, threading.Thread]:
    if host == "0.0.0.0":
        raise SystemExit("Use the laptop's Tailscale address, not 0.0.0.0, so the demo stays off "
                         "the local Wi-Fi.")
    offset = DEMO_NOW - time.time()

    def clock() -> float:
        return time.time() + offset

    rules_text = f"""serve_secret: {secrets.token_urlsafe(24)}
home_venue: Venetian
targets:
  - code: SVS306
  - code: CMP409
  - code: ARC302
"""
    d = setup(rules_text, catalog({
        "SVS306-R": "unavailable", "SVS306-R1": "unavailable",
        "CMP409-R": "available", "CMP409-R1": "available",
        "ARC302-R": "available", "ARC302-R1": "available",
    }))
    d.fake.schedule.reserved.update({sid(d, "CMP409-R"), sid(d, "ARC302-R")})
    rules = R.parse(rules_text)

    def swapper(p, ok):
        return Swap(d.client, d.store, rules, EV).run_plan(p, approved=ok)

    w = Watcher(d.client, d.store, rules, EV, swapper=swapper, clock=clock)
    w.tick()                                                # baseline
    d.fake.set_band(sid(d, "SVS306-R"), "limited")          # a better seat frees up
    w.tick()                                                # becomes a swap proposal
    app = S.App(w, d.store, rules, EV, host=host, port=port, clock=clock)
    server = S.make_server(app)
    worker = S.run(app, server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return app, server, worker


def stop(app: S.App, server: ThreadingHTTPServer, worker: threading.Thread) -> None:
    app.watcher.stop()
    server.shutdown()
    worker.join(timeout=10)
    server.server_close()


def main(argv: list[str] | None = None, wait: Callable[[], object] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="the laptop's Tailscale address")
    ap.add_argument("--port", type=int, default=DEMO_PORT)
    args = ap.parse_args(argv)
    app, server, worker = start(args.host, args.port)
    banner("re:Seat: the phone page",
           "Two seats held, one swap proposed. The page refreshes every 20 seconds.")
    con.print(f"Open this once on the phone:\n  [bold]{app.one_time_link(args.host)}[/bold]")
    con.print("[dim]Open it before you start recording. A used link is harmless, an unused one "
              "works for an hour. Ctrl-C to stop.[/dim]")
    try:
        (wait or threading.Event().wait)()
    except KeyboardInterrupt:
        pass
    finally:
        stop(app, server, worker)


if __name__ == "__main__":
    main()
