# The web app on a phone over Tailscale

- **Date and time:** 2026-10-14, 23:19 PDT (the screenshots' own time)
- **re:Seat version:** the server was started at `ee7ad16` plus the uncommitted Activity fix, which was later committed in `6e56369`
- **Where:** `reseat serve --tailscale` on the attendee's laptop, opened from the attendee's phone over Tailscale, both on the attendee's own tailnet

## What was checked

That the web app works from a phone while re:Seat runs on the laptop, against the live API, with the attendee's real schedule.

## Command

On the laptop:

```
caffeinate -i reseat serve --tailscale
```

`reseat serve` listens on 127.0.0.1 only. With `--tailscale` it asks Tailscale to forward the laptop's Tailscale name to that address, so another device can reach it only through the tailnet. The phone opened the printed one-time link, then the Today and Week pages.

## Output

Not captured. The server's printout, with the Tailscale name and the one-time link, was not saved, so this file cannot show the route or the forwarding being removed on stop. Both are tested against a faked Tailscale CLI in the test suite.

## Screenshots

The two files sit in `docs/screenshots/` rather than next to this file, because the README and the article show them too. Taken on the phone and cropped by the attendee: no address bar, no status bar. Session codes, titles and rooms of the attendee's sessions are covered with solid boxes. Times, types and venues are not.

- [`../screenshots/live-phone-today.png`](../screenshots/live-phone-today.png): Today. The watcher's last sweep at 23:18:49 over 2,240 sessions, leave in 46 days for the first held session with the walk as an estimate, and walk-up odds with a range for three wanted talks.
- [`../screenshots/live-phone-week.png`](../screenshots/live-phone-week.png): Week at phone width. Monday and Tuesday, held sessions coloured by type with their leave times, wanted talks dashed beside them.

## Result

- The web app served by `reseat serve --tailscale` on the laptop opened on the attendee's phone, showed live data from the watcher, and showed the dark theme.
- The token stays on the laptop by design. Nothing in this check sent it to the phone.

## What it does not prove

- Approving a swap from the phone, or the push buttons. No swap was proposed.
- Use on hotel Wi-Fi or a cellular network. Tailscale is designed for that, and it is not shown here.
- The tailnet name and the one-time link are not shown, so the screenshots alone do not show the route. It rests on `serve` listening on 127.0.0.1 only, and the attendee's account of opening it on the phone.
- One phone, one evening.
