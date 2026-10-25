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
- Amazon SNS: write the topic ARN as `arn:aws:sns:<region>:<account>:<topic>`, the `aws_profile` value as `<profile>`, and never show the subscribed email address or phone number.
- `ntfy_topic`: it is the only thing that protects the pushes. Crop it out of ntfy screenshots and notification banners. If one leaks, change the topic. If you paste the rules file, remove `serve_secret` and `ntfy_topic`.
- Hostnames, `*.ts.net` names, the tailnet name, and every IP address (100.x Tailscale, LAN, public): write `<host>` and `<ip>`. Crop the phone's address bar and any Tailscale admin console.
- Your timetable. Show only the throwaway session used for the check. Write other held sessions as `<held session>`, or keep full-schedule evidence out of the repo until after 4 December.
- Portal screenshots: crop to what proves the check. Remove your name, email, registration or badge id, any QR code, and the browser address bar.
- Phone screenshots: crop the status bar and the notification shade. Strip photo metadata. Plan ids are fine once expired.

Session ids and session codes such as ARC301 are fine.

Before `git add`, search the file for `k=`, `code=`, `ts.net`, `100.`, `@`, `ntfy`, `secret`, `token`, `Bearer`, `arn:` and any 12-digit number.

## Checks

| Check | File | Status |
|---|---|---|
| A safe swap on the live API, there and back, 25 October | [`2026-10-25-live-swap-round-trip.md`](2026-10-25-live-swap-round-trip.md) | done |
| Autopilot books two ranked targets on its own, read back and emailed, 25 October | [`2026-10-25-autopilot-first-booking.md`](2026-10-25-autopilot-first-booking.md) | done |
| An email alert through the attendee's own SNS topic, 24 October | [`2026-10-24-email-alert.md`](2026-10-24-email-alert.md) | done |
| Favorites sync adds a favorite, then removes it when its target is dropped, 24 October | [`2026-10-24-favorites-round-trip.md`](2026-10-24-favorites-round-trip.md) | done |
| The planning tools on the real schedule, 17 and 20 October | [`2026-10-21-check-plan.md`](2026-10-21-check-plan.md) | done |
| What the watcher saw, 12 to 20 October, updated 24 October | [`2026-10-20-watcher-first-week.md`](2026-10-20-watcher-first-week.md) | done, to be updated to submission |
| The web app on a phone over Tailscale, 14 October | [`2026-10-14-tailscale.md`](2026-10-14-tailscale.md) | done |
| Seat bands after reserved seating opens | in [`2026-10-12-catalog-returned.md`](2026-10-12-catalog-returned.md) | done |
| Booking the attendee's real targets with `reseat book` | none | not run: those seats were booked in the AWS portal after seating opened on 6 October |
| Two reversible round trips: reserve, read back, cancel, read back, 12 October | [`2026-10-12-round-trip.md`](2026-10-12-round-trip.md) | done |
| The catalog came back empty on 8 October | [`2026-10-08-catalog-empty.md`](2026-10-08-catalog-empty.md) | done |
| The catalog re-checked through re:Seat, plain HTTPS and the official MCP server, 9 October | [`2026-10-09-catalog-check.md`](2026-10-09-catalog-check.md) | done |
| The catalog came back, ids kept, read-only checks and the probe, 12 October | [`2026-10-12-catalog-returned.md`](2026-10-12-catalog-returned.md) | done |
