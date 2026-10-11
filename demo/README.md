# Demos

Try re:Seat without signing in. Every demo runs the real re:Seat code against an in-process fake of the AWS Events API, with real session codes and titles from the 1 October 2026 catalog. Nothing is sent to AWS, nothing is read from your keychain, and each demo says so on screen. What has been checked against the real API is in [docs/proof](../docs/proof/).

No install at all: the [click-through demo](https://hsiddhu2.github.io/reseat/) is the web app on demo data.

| Demo | Command | Shows | Length |
|---|---|---|---|
| Web app | `reseat serve --demo`, then open `http://127.0.0.1:8491/` | The dashboard, approve and today views on a scripted week. About 30 s in, a seat opens and a swap waits for your approval. About 60 s in, a new sitting is booked. About 90 s in, a held session changes room. Press Swap now and watch the journal. Add `--tailscale` to open it on your phone over Tailscale: it prints a one-time link to open there. Add `--push <topic>` and subscribe to that topic in the ntfy app to get the pushes on your phone: swap proposed and seat booked in the first minute, swap done when you approve, and leave now for ARC302-R about 24 minutes in. The swap proposal has Swap now and Keep buttons: long-press it and tap one. | about 2 minutes |
| Book | `python demo/book.py` | The rules check, favorites sync, a dry run, then booking four targets. One sitting fills as it is booked, and re:Seat books that talk's next sitting in the same run. Another already shows unavailable, so it is never sent and its next sitting is booked instead. Every write is read back. | about 40 s |
| Watch | `python demo/watch.py` | Four sweeps. A cancelled seat is booked the moment its band opens, then a repeat sitting AWS adds is booked as soon as it appears. | about 15 s |
| Safe swap | `python demo/swap.py` | A held session and a better one at the same time. First the better seat fills between the cancel and the reserve, and re:Seat restores the original. Then the swap goes through. | about 20 s |

`reseat serve --demo` works after `pip install reseat`. The three scripts need the repository: clone it, `pip install -e .`, and run them from its root. Set `RESEAT_DEMO_PACE=0` to run them without pauses.
