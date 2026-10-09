# Keeping your re:Invent seats after the plan is made

*Draft for editing. Places marked [LIVE] wait for checks against the real API. [LINK] marks a link still to add. See [proof](proof/).*

Planning ends when your schedule is built. Keeping the seats takes the rest of the week. re:Seat is a small tool that runs on your laptop from the day API writes open until the last session. It watches the catalog, books seats as they free up, swaps a held seat for a better one without losing either, and tells you when to leave. It is built on the AWS Events API.

## What attendees say

These are first-hand accounts from Reddit, blogs and Builder Center posts between 2022 and 2025:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app for the associated session. Most of the sessions will have no shows or cancellations." (2022)
- "Use 11 minutes as I have found myself arguing with the staff." (2025)
- "Don't do one session at Venetian followed by a session at MGM. You'll never make it." (2022)

Seats free up after opening day. AWS adds repeat sittings for popular sessions. The way to catch either is to refresh the app, all week. re:Seat does the refreshing, and acts within rules you write.

## The two-day gap

Reserved seating opened in the AWS portal on 6 October 2026. Writes through the Events API were scheduled to open on 8 October. Until they open, reserve and cancel return 409. So no API tool can help with the first rush. re:Seat does not claim to. It starts when the API opens and runs until the last session ends on 4 December.

## What the API does not give you

The Events API is clean and well documented. Four things it does not do shape everything re:Seat does.

**There is no swap.** If you hold session A and a better session B opens at the same time, reserving B is refused with `scheduleConflict` and a `conflictsWith` list. You must cancel A first. For a moment you hold neither, and someone else can take A's seat.

**There is no search.** ListSessions returns the whole catalog in pages of 250. Everything else happens on your side.

**Availability is a band, not a count.** A session is `available`, `limited`, `veryLimited`, `unavailable` or `walkUp`. There is no seat count and no waitlist.

**Sign-in only works on your own machine.** OAuth with PKCE, a callback on a loopback port, no hosted redirect. A phone cannot sign in.

## How re:Seat handles each

**A safe swap.** re:Seat cancels A only when three things hold. B is open in a fresh GetSession read, takes reservations, and clashes with nothing else you hold. A has a fallback: its own band is open, or another known sitting of A is open, read fresh, and clashes with nothing else you hold. And you approved, or allowed auto-swap for that target. Bands are read fresh from GetSession. Times for the clash check come from the last sync. Then it cancels A. If the cancel fails and A is still held, it stops there. Otherwise it reserves B and reads your schedule back. If B failed, it reserves A again. If A is gone too, it tries each fallback once, in order, and tells you exactly what you hold. Two cases stop it with nothing more sent. If a read-back fails, it says the state is unknown. If writes close at any point after the cancel, it says A was released and needs reserving again. One swap runs at a time, across processes. Every state is journaled, rolled back and failed included, and every write is journaled before and after it is sent.

**The whole catalog, locally.** A full sweep is about 9 calls. re:Seat sweeps once a minute, without abstracts, and keeps the catalog in SQLite with the band history of every session.

**Bands as signals.** A move from `unavailable` to an open band is a freed seat. A session that was not there in the last sweep, under a code you want, is a new repeat. re:Seat books either the moment it sees it, inside the per-minute quota, and reads the result back.

**One web app, three views.** `reseat serve` runs the watcher and a local web app. The dashboard shows the watch status, any swap that needs you with its checks, the week as a grid, the last changes and the journal. At phone width, Approve shows one proposal at a time and Today shows when to leave. `reseat serve --demo` runs it on a fake API with a scripted week, so anyone can try it in two minutes without a sign-in.

**The laptop is the agent, the phone is the remote.** The token never leaves the laptop. re:Seat serves a small page that your phone opens over Tailscale: today's seats, a leave-now countdown, whether to queue, and Approve or Skip for a proposed swap. Approve takes a plan id that expires after 10 minutes and works once. No endpoint takes a session id.

## Why re:Seat runs on your machine

The Events API signs you in with OAuth and PKCE, using your own Builder ID, through a callback on a loopback port of the machine you sign in on. It has no hosted sign-in. A hosted re:Seat would have to hold other attendees' tokens, and those tokens can cancel their seats. So re:Seat runs on your laptop, keeps your token in the OS keychain, and your phone talks only to your laptop.

## Respecting the API

| The API says | re:Seat |
|---|---|
| Quotas are per operation per minute. Reserve counts each session. | Keeps its own count and never sends a batch larger than what is left. |
| A 200 on reserve can carry per-session failures. | Reads every `failed` entry, then reads back the schedule. |
| Reserve and cancel are not safe to retry. | Never retries a write. A timeout or a server error is resolved by reading the schedule back. |
| 429 carries `Retry-After`. | Waits that long, once. |
| 409 means writes are switched off. | Stops writing, keeps every opening queued, and resumes when writes open. |

## Seen live: an empty catalog

On the evening of 8 October, the day API writes were scheduled to open, I ran the band inspection planned for after seating opened. The API served no catalog.

```
ListSessions                -> 200, totalCount 0, items 0
GetSession <held session>   -> 404 "No session was found with the requested id"
GetSchedule                 -> 200, reserved 14, favorites 16
```

GetSession returned 404 even for the 14 sessions I held, booked in the portal on 6 October. The schedule still answered.

re:Seat's first sync that evening read the empty answer as "every session removed" and emptied its local copy of the catalog. It sent no write. Then I fixed three things:

- An empty catalog, a walk that ends short of the API's own `totalCount`, or a sweep that would drop most of the catalog without bringing back one of about the same size is now refused. The local copy is kept.
- A catalog of about the same size with mostly new ids is recorded as a new starting point, not as thousands of new sessions to act on.
- Nothing is booked while a held session is missing from the local catalog. Without it, re:Seat cannot tell which talk a held seat is, and could book a second sitting of it.

The same condition was reported publicly on 3 October in [a public GitHub issue](https://github.com/jasonwadsworth/reinvent-scout/issues/17): `totalCount` 0, 404 on GetSession, GetSchedule still answering, and the official MCP server returning the same empty list. [A post on the hackathon discussion]([LINK]) reported the same symptoms: ListSessions and GetSession empty while signed in.

The full record, with commands and output, is in [proof/2026-10-08-catalog-empty.md](proof/2026-10-08-catalog-empty.md).

[LIVE] The live booking check and one reversible round trip, when the catalog returns.

## How it was tested

Nothing here was built against the live API first. Every write path runs against an in-process fake that produces each documented failure: partial bulk results, `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409, 429 with `Retry-After`, 5xx, dropped connections and an expired sign-in.

A fault storm runs the whole flow for an hour on the real 2026 catalog of 2,068 sessions: sync, favorites, booking, sixty sweeps with bands moving, swaps approved from the phone path, then leave-now blocks. 30 percent of reserves come back full, the first request each minute gets a 429, one gets a 503, writes return 409 for fifteen minutes, and the fake enforces the real quotas. Every run checks that no request exceeds a quota, no talk is held twice, only targets from the rules are held, and every write is read back before the next. Three seeds, each reaching a booking, a swap and the 409 window.

The storm found two bugs. The watcher proposed swaps that could never run, and it re-sent writes every minute into a 409. The worst bug found before it could have lost a seat: a timeout while reserving B, after A was cancelled, escaped the rollback. It was fixed in the client, so every write path inherits the fix.

## What is a heuristic

- **Queue or go.** A rule by session type: breakouts usually admit walk-ups, chalk talks need you early, workshops and builders' sessions admit almost none. It switches to the session's own band history once there are three changes. It is never a prediction, and the page always shows its basis.
- **Walking times.** Conservative estimates between the six 2026 venues, not official figures.
- **Wynn and Encore** arrive from the API as one venue, `Wynn/Encore`. re:Seat splits them by room name.
- **Booking order.** Formats that are not recorded fill first, so they are sent first: workshops, labs and bootcamps, then builders' sessions, chalk talks, code talks, breakouts.

## Limits

- It manages seats you already chose. For choosing, use the AWS portal or a planner. re:Seat reads your official schedule, or imports reinvent-planner.cloud exports.
- It cannot help with the portal's opening rush.
- The web app is plain HTTP. Use it over Tailscale.
- Designed for a laptop left in the hotel room. [LIVE] Tailscale run from home.
- The MCP server tells an agent to ask before approving, but cannot check that it did.

## Links

- Code: [github.com/hsiddhu2/reseat](https://github.com/hsiddhu2/reseat)
- How it works: [docs/architecture.md](architecture.md)
- API facts it relies on: [docs/api-facts.md](api-facts.md)
- Live proof: [docs/proof](proof/)
- Demos: [demo](../demo/)
