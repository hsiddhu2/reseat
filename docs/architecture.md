# How re:Seat works

re:Seat manages an attendee's AWS re:Invent seats after the plan is made. It watches for freed seats and newly added repeat sessions, books them within the attendee's rules, upgrades held seats without losing one, and tells the attendee when to leave and whether to queue.

It runs on the attendee's own machine against the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html). It keeps a journal of every write and its result. The CLI, a phone page served from the laptop, and a local MCP server all drive the same engine.

Status as of 2 October 2026 is marked on each part below: **built** means it is in this repo and tested against the fake API, **planned** means it is not written yet.

## The problem, in attendees' words

Quotes are from public Reddit threads, personal blogs and Builder Center posts, 2022 to 2026.

| Pain | First-hand account |
|---|---|
| Seats vanish on opening day | "Most things were full by 10:03am." (2022) "I didn't get anything I wanted even 3 hours after it went live." (2023) |
| Seats free up later | "Some stuff that was full yesterday I was able to reserve right now." (2024) |
| Capacity gets added | "The more popular sessions get 2-3x extra sessions once they get booked up." (2022) "They continue to add repeat sessions and adjust the schedule." (2025) |
| Refreshing in the queue | "Get in line and keep refreshing the app for the associated session. Most of the sessions will have no shows or cancellations." (2022) |
| The 10-minute rule | "Use 11 minutes as I have found myself arguing with the staff." (2025) "I was at the 50th person on the walk-up line. They only accepted 20 more walk-ups." (2023) |
| Venue distance | "Don't do one session at Venetian followed by a session at MGM. You'll never make it." (2022) |

Two things were not found in first-hand reports, so re:Seat does not claim them: a reserved session changing room or time after booking, and anyone losing both seats during a swap. The portal offers a Keep Existing or Replace choice on conflicts. The API does not. re:Seat restores that safety for API users.

## What the API gives, and what shapes the design

Full detail, with sources, is in [api-facts.md](api-facts.md).

- **Local only.** OAuth 2.0 code flow with PKCE, callback on localhost ports 8484 to 8489. No hosted redirect.
- **No search.** re:Seat pulls the whole catalog, about 2,000 sessions in 9 pages, and works locally.
- **Seat bands, not counts.** `available`, `limited`, `veryLimited`, `unavailable`, `walkUp`. A band change is the signal that a seat freed.
- **Partial success.** Reserve and favorite return 200 with a `failed` list. Unknown failure codes are treated as generic refusals.
- **No blind retry.** A repeated reserve reports `alreadyScheduled`. Cancel on a seat not held is a 404. Every write is followed by a `GetSchedule` read-back.
- **Quota counts sessions.** Ten reserves in one call spend 10 of the 30 per minute.
- **No swap.** Reserving a session that overlaps a held one is refused. Cancel must come first. That is the risk re:Seat manages.
- **Two-day gap.** The portal opens reserved seating on 6 October 2026. API writes open on 8 October. re:Seat cannot help with the first rush.
- **Time zones.** Sessions are Las Vegas local time. Personal time is UTC.

## What re:Seat does

**Favorites** (planned)
- `reseat favorites sync` mirrors every target sitting into favorites, so the official app shows the same plan. Works before 8 October, when reservations are still closed.

**Book** (built)
- Targets live in a rules file in priority order, with allowed repeats and backups. Each target has a fallback tree.
- Books in batches of up to 10 inside the 30 per minute budget. On `sessionFull` it moves to the target's next sitting in the same run.
- Formats that are not recorded fill first, so they are sent first: Workshop, Lab and Bootcamp, then Builders' session, Chalk talk, Code talk, Breakout session, then the rest.
- Never holds two sittings of one talk. Never books over a held session, a meal in the rules or the daily cap.
- Reads back `GetSchedule` after every write and journals request, response and read-back.

**Watch** (planned)
- Sweeps the catalog about once a minute without abstracts and records every seat band change.
- Diffs each sweep to catch new sessions and new repeats, and books a target's new sitting the moment it appears.

**Swap** (planned)
- Before replacing held session A with wanted session B, checks three things: B is fresh from `GetSession` and open, A has a fallback, and the attendee allowed auto-swap for this target or approves now.
- Cancels A, reserves B at once, reads back. If B fails, re-reserves A. If A is gone, books A's fallback and alerts.
- Ask first by default. Auto mode is opt-in per target.

**Guard** (planned)
- Writes a leave-now block into personal time for each held session, from venue walking times, an 11-minute cutoff and the attendee's buffer.
- Queue or go: for a session not held, advice from session type, improved by band history. Presented as a heuristic, never a prediction.

**Phone remote** (planned)
- The token never leaves the laptop, because the API only allows sign-in on the attendee's own machine. So the laptop runs the watcher and serves a small page that the phone opens, over Tailscale or the hotel Wi-Fi.
- `reseat serve` listens on `127.0.0.1:8490` by default. Binding to the network needs a shared secret. The phone opens a one-time link that sets a cookie and redirects to a clean URL.
- The page shows today's held sessions, a leave-now countdown, queue-or-go advice and proposed swaps with Approve and Skip. Approve takes a plan id that expires after 10 minutes. No endpoint accepts a raw session id.
- Optional push through ntfy, off unless a topic is set. Messages carry session codes, titles and the event type only.
- On site the laptop checks today's sessions every 20 seconds, at most 40 of them, inside the 120 per minute quota.

**Interfaces**
- CLI (built): `login`, `whoami`, `sync`, `search`, `show`, `schedule`, `favorite`, `probe`, `rules`, `book`, `cancel`.
- Phone page (planned), as above.
- Local MCP server (planned): an agent proposes changes, the human approves.

## Architecture

![re:Seat architecture](architecture.png)

| Component | Status | Responsibility |
|---|---|---|
| Auth | built | PKCE on port 8484, fallback to 8489. OS keychain. Refresh once on 401. Sign-out with revoke. |
| API client | built | Typed models that mirror the OpenAPI spec. Quota tracker per operation. Honors `Retry-After` once. Per-session failures as typed results. |
| Catalog store | built | SQLite. Full sync, then cheap sweeps. Added, removed and moved sessions. Band history. Write journal. |
| Campus | built | The 2026 venues, a walking-time table, Las Vegas to UTC conversion. |
| Rules | built | `~/.reseat/rules.yaml`, import from reinvent-planner.cloud exports, validation against the catalog. |
| Order router | built | Priority, quota, fallback trees, scarcity ordering, read-back. |
| Cancel | built | `GetSession` first, ask first, one DELETE, read-back. |
| Watcher | planned | Sweep loop. Emits band-change and new-session events to the router. |
| Safe swap | planned | State machine: proposed, checked, cancelled, reserved, verified, rolled back. |
| Cutoff guard | planned | Leave-now blocks, queue-or-go advice, venue-switch warnings. |
| Favorites sync | planned | Mirror target sittings into favorites, read back. |
| Phone remote | planned | Local HTTP server, phone page, approve by plan id, optional push. |

## Key flows

**8 October, writes open**
1. Probe with `CancelReservation` on a session never held. 409 while closed, 404 once open. Never changes the schedule.
2. Book in priority order, 10 per call, inside 30 per minute.
3. On `sessionFull` walk the fallback tree. On `scheduleConflict` record `conflictsWith` and move on.
4. Read back. Report what landed, what failed and why.

**A seat frees up** (planned)
1. A sweep shows a target moved from `unavailable` to an open band.
2. If the slot is free, reserve and read back.
3. If the slot holds a lower-priority session, run the safe swap.

## API operations used

| Operation | Used by |
|---|---|
| ListEvents, GetEvent | Setup and event dates. |
| ListSessions | Full sync, band sweeps, new-session diffs. |
| GetSession | Fresh check before any cancel or swap. |
| GetSchedule | Read-back after every write. Source of truth. |
| ReserveSessions, CancelReservation | Booking, fallback trees, cancel, swap. |
| AssociateFavorites, DisassociateFavorite | Favorites from the CLI. |
| Create, Update, DeletePersonalTime | Leave-now blocks (planned). |
