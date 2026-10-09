# Live proof

Everything else in this repo is tested against an in-process fake of the AWS Events API. This folder holds the checks run against the real API by a registered re:Invent 2026 attendee. Nothing in the README or the article is described as tested live unless a file here backs it.

## Convention

- One file per check, named `YYYY-MM-DD-<check>.md`, for example `2026-10-08-live-booking.md`. Copy [`_template.md`](_template.md).
- Screenshots sit next to it with the same name and a suffix: `2026-10-08-live-booking-portal.png`.
- The command exactly as run, and its output pasted unedited, except for the redactions below.
- Say what the check proves and what it does not.
- A check repeated daily with the same result, such as the empty catalog, appends one dated line to its file instead of a new file.

## Redact before committing

This folder is public. Replace, do not just trim:

- Tokens, refresh tokens, any `Authorization` or `Bearer` line, and any URL containing `code=`. Paste `reseat login` output only after sign-in has finished.
- The one-time phone link: write its `k=` value as `k=REDACTED` in text and blur it in images, even after it was used. Never show `serve_secret`.
- `ntfy_topic`: it is the only thing that protects the pushes. Crop it out of ntfy screenshots and notification banners. If one leaks, change the topic. If you paste the rules file, remove `serve_secret` and `ntfy_topic`.
- Hostnames, `*.ts.net` names, the tailnet name, and every IP address (100.x Tailscale, LAN, public): write `<host>` and `<ip>`. Crop the phone's address bar and any Tailscale admin console.
- Your timetable. Show only the throwaway session used for the check. Write other held sessions as `<held session>`, or keep full-schedule evidence out of the repo until after 4 December.
- Portal screenshots: crop to what proves the check. Remove your name, email, registration or badge id, any QR code, and the browser address bar.
- Phone screenshots: crop the status bar and the notification shade. Strip photo metadata. Plan ids are fine once expired.

Session ids and session codes such as ARC301 are fine.

Before `git add`, search the file for `k=`, `code=`, `ts.net`, `100.`, `@`, `ntfy`, `secret`, `token` and `Bearer`.

## Checks

| Check | File | Status |
|---|---|---|
| Phone page over Tailscale from home | `2026-10-DD-tailscale.md`, on the day it is run | to do |
| Seat bands after reserved seating opens, 6 October | `2026-10-06-bands.md` | to do |
| Live booking when API writes open, 8 October | `2026-10-08-live-booking.md` | to do |
| One reversible round trip: reserve, read back, cancel, read back | `2026-10-08-round-trip.md` | to do |
| The catalog came back empty on 8 October | [`2026-10-08-catalog-empty.md`](2026-10-08-catalog-empty.md) | done |
| The catalog re-checked through re:Seat, plain HTTPS and the official MCP server, 9 October | [`2026-10-09-catalog-check.md`](2026-10-09-catalog-check.md) | done |
