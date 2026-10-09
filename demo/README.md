# Demos

Four short demos to record. Each runs the real re:Seat CLI and engine against an in-process fake of the AWS Events API, with real session codes and titles from the 1 October 2026 catalog. Nothing is sent to AWS and no sign-in is used. Each demo says so on screen.

These show behaviour against the fake. What has been checked against the real API is in [docs/proof](../docs/proof/).

Run from the repo root after `pip install -e .`:

| Demo | Command | Shows | Length |
|---|---|---|---|
| Book | `python demo/book.py` | The rules check, favorites sync, a dry run, then booking four targets. One sitting fills as it is booked, and re:Seat books that talk's next sitting in the same run. Another sitting already shows unavailable, so it is never sent and its next sitting is booked instead. Every write is read back. | about 40 s |
| Watch | `python demo/watch.py` | Four sweeps. A cancelled seat is booked the moment its band opens, then a repeat sitting AWS adds is booked as soon as it appears. | about 15 s |
| Safe swap | `python demo/swap.py` | A held session and a better one at the same time. First the better seat fills between the cancel and the reserve, and re:Seat restores the original. Then the swap goes through. | about 20 s |
| Phone page | `python demo/phone.py --host <laptop Tailscale address>` | The phone page on the first morning of re:Invent: two held seats, a leave-now countdown and a proposed swap. Tap Approve on the phone. | as long as you like |

## Recording

- **Terminal.** Use [asciinema](https://asciinema.org) (`asciinema rec book.cast -c "python demo/book.py"`), or a screen recording of a terminal at least 120 columns wide so the tables do not wrap.
- **Phone page.** On the laptop, run `demo/phone.py` with the laptop's Tailscale address. It refuses `0.0.0.0`, so the demo never listens on hotel or home Wi-Fi. It uses port 8491, so it does not sign the phone out of a real `reseat serve` on 8490. On the phone, open the printed link over Tailscale before you start recording: a used link is harmless, an unused one works for an hour. Then start the phone's screen recording and tap Approve. The swap result shows on the page within a second.
- **Before publishing a recording,** check it against the redaction rules in [docs/proof/README.md](../docs/proof/README.md). In particular, blur the one-time link and crop the phone's address bar and status bar. The demo data is not personal, but the link, host name and Tailscale address are.
