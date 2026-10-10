# Demos

Five short demos to record. Each runs the real re:Seat CLI and engine against an in-process fake of the AWS Events API, with real session codes and titles from the 1 October 2026 catalog. Nothing is sent to AWS and no sign-in is used. Each demo says so on screen.

These show behaviour against the fake. What has been checked against the real API is in [docs/proof](../docs/proof/).

Run from the repo root after `pip install -e .`:

| Demo | Command | Shows | Length |
|---|---|---|---|
| Book | `python demo/book.py` | The rules check, favorites sync, a dry run, then booking four targets. One sitting fills as it is booked, and re:Seat books that talk's next sitting in the same run. Another sitting already shows unavailable, so it is never sent and its next sitting is booked instead. Every write is read back. | about 40 s |
| Watch | `python demo/watch.py` | Four sweeps. A cancelled seat is booked the moment its band opens, then a repeat sitting AWS adds is booked as soon as it appears. | about 15 s |
| Safe swap | `python demo/swap.py` | A held session and a better one at the same time. First the better seat fills between the cancel and the reserve, and re:Seat restores the original. Then the swap goes through. | about 20 s |
| Approve on the phone | `python demo/phone.py --host <laptop Tailscale address>` | The approve view on the first morning of re:Invent: two held seats, a leave-now countdown and a proposed swap. Tap Approve on the phone. | as long as you like |
| Web app | `reseat serve --demo` | The dashboard, approve and today views on a scripted week. About 30 s in a seat opens and a swap waits for approval. About 60 s in a new sitting is booked. About 90 s in a held session changes room. Open `http://127.0.0.1:8491/`, or add `--host <laptop Tailscale address>` for the phone. | about 2 minutes |

## Recording

- **The web app, about 2 minutes.** Set the browser to 1440 by 900 and close other tabs. Start `reseat serve --demo`, then open `http://127.0.0.1:8491/` at once, since the script starts with the command. Shot list:
  1. 0:00 to 0:20. The week: held sessions, the yellow leave-now strips, the status line saying it watches in this process.
  2. About 0:30. The swap card appears. Read the four checks and the sequence line under the buttons.
  3. About 1:00. Last changes shows the new sitting booked and read back. The counts panel goes to 1 seat booked.
  4. About 1:30. CMP303 on Wednesday says it moved room.
  5. Press Swap now. The result line says verified and what is held now. Scroll to the journal: proposed, checked, cancel, reserve, verified.
  6. Narrow the window to phone width, or open `/today`: the leave countdown, the next sessions, the personal dinner, the wanted session and its clash line.

- **Terminal.** Use [asciinema](https://asciinema.org) (`asciinema rec book.cast -c "python demo/book.py"`), or a screen recording of a terminal at least 120 columns wide so the tables do not wrap.
- **Phone page.** On the laptop, run `demo/phone.py` with the laptop's Tailscale address. It refuses `0.0.0.0`, so the demo never listens on hotel or home Wi-Fi. It uses port 8491, so it does not sign the phone out of a real `reseat serve` on 8490. On the phone, open the printed link over Tailscale before you start recording: a used link is harmless, an unused one works for an hour. Then start the phone's screen recording and tap Approve. The swap result shows on the page within a second.
- **Before publishing a recording,** check it against the redaction rules in [docs/proof/README.md](../docs/proof/README.md). In particular, blur the one-time link and crop the phone's address bar and status bar. The demo data is not personal, but the link, host name and Tailscale address are.
