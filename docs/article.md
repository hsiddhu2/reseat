# re:Seat: keeping your re:Invent seats after the plan is made

Planning your re:Invent week is the easy part. Keeping the seats you booked is the part that takes the rest of the week. I built re:Seat to do that part for you. It is a small tool that runs on your laptop from the day the Events API opens for writes until the last session ends. It watches the catalog, books seats as they free up, swaps a held seat for a better one without losing either, and tells you when to leave for the next one. You approve a swap from a notification on your phone. It is built on the AWS Events API.

You can try it in two minutes without signing in to anything:

```
pip install reseat
reseat serve --demo
```

If you would rather not install it, open the [click-through demo](https://hsiddhu2.github.io/reseat/) in your browser.

![The Week page in demo mode: a swap waiting for approval with its checks, and the calendar with what just changed labelled](screenshots/week.png)

## Why I built it

Here is what attendees have said about reserved seating over the last four years, from Reddit, blogs and Builder Center posts:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app for the associated session. Most of the sessions will have no shows or cancellations." (2022)
- "Use 11 minutes as I have found myself arguing with the staff." (2025)
- "Don't do one session at Venetian followed by a session at MGM. You'll never make it." (2022)

Two things stand out. Seats free up after opening day, and AWS adds repeat sittings for the popular sessions. The only way to catch either has been to refresh the app all week. re:Seat does the refreshing for you, and it acts only within rules you write.

## What re:Seat is not for

Reserved seating opened in the AWS portal on 6 October, two days before writes through the Events API were due to open. Until writes open, reserve and cancel return 409. So no API tool can help with the opening rush, and re:Seat does not claim to. It starts once the API is writable and runs until the last session ends on 4 December.

## What the API does not give you

The Events API is clean and well documented. I want to call out four things it does not do, because they shape everything re:Seat does.

**There is no swap.** If you hold session A and a better session B opens at the same time, reserving B is refused with `scheduleConflict` and a `conflictsWith` list. You must cancel A first. For a moment you hold neither, and someone else can take A's seat.

**There is no search.** ListSessions returns the whole catalog in pages of 250. Everything else happens on your side.

**Availability is a band, not a count.** A session is `available`, `limited`, `veryLimited`, `unavailable` or `walkUp`. There is no seat count and no waitlist.

**Sign-in only works on your own machine.** OAuth with PKCE, a callback on a loopback port, no hosted redirect. A phone cannot sign in.

## How re:Seat handles each

### A safe swap

This is the part I spent the most time on. re:Seat cancels A only when three things hold:

1. **B is open** in a fresh GetSession read, takes reservations, and clashes with nothing else you hold.
2. **A has a fallback.** Its own band is open, or another known sitting of A is open, read fresh, and clashes with nothing else you hold.
3. **You approved it**, or allowed auto-swap for that target in your rules.

Then it cancels A, reserves B, and reads your schedule back. If B failed, it reserves A again. If A is gone too, it tries each fallback once, in order, and tells you exactly what you hold.

Three cases stop it with nothing more sent. If the cancel fails and A is still held, it stops before touching B. If a read-back fails, it says the state is unknown and asks you to check the schedule. If writes close at any point after the cancel, it says A was released and needs reserving again. Sending another write in any of these cases would be a blind retry, and that is the one thing the API documentation says never to do.

One swap runs at a time, across processes. Every swap state is journaled, rolled back and failed included, and each swap write is journaled before and after it is sent. Every other write is journaled with its response and the schedule read back.

![The Approve page at phone width: the held seat and the opened seat side by side, the checks, and Swap now or Keep](screenshots/approve.png)

### The whole catalog, locally

A full sweep is about 9 calls. re:Seat sweeps once a minute, without abstracts, and keeps the catalog in SQLite with the band history of every session.

### Bands as signals

A move from `unavailable` to an open band is a freed seat. A session that was not there in the last sweep, under a code you want, is a new repeat. re:Seat books either the moment it sees it, inside your rules and inside the per-minute quota, and reads the result back.

### When to leave

When you ask it to, re:Seat writes a short personal-time block for each held session into your official schedule: leave at this time, walk from here to there. The leave time is the session start minus 11 minutes, which is when door staff stop admitting, minus a conservative walking estimate between the six 2026 venues, and minus a buffer you set when the change is tight. The Today page counts it down. When your holds change, the blocks are offered for update. They are never changed behind your back.

![The Today page at phone width: the leave-in countdown, what is next, and queue-or-go advice for a wanted talk without a seat](screenshots/today.png)

### One web app, one job per page

`reseat serve` runs the watcher and a local web app. Week shows any swap that needs you, with its checks, and the calendar, where whatever just changed is labelled: booked, swapped in, moved, released. Activity keeps every change and the journal of writes. Targets reads your rules back. Status shows the quota left this minute. A day opens in full from the week. At phone width, Approve shows one proposal at a time and Today shows when to leave.

`reseat serve --demo` runs all of this on a fake API with a scripted week and a Demo page of scenarios. Each scenario shows what AWS did, what re:Seat did, the week before and after, and the notification your phone would get.

![The Demo page: a scenario, what AWS did, what re:Seat did, and the notification as the phone draws it](screenshots/demo.png)

### The laptop is the agent, the phone is the remote

The token never leaves the laptop. Your phone gets notifications through the free ntfy app: a morning briefing, leave in 10 minutes, leave now, a heads-up when a talk you want starts in 20 minutes, and a swap proposal. With `ntfy_approve: true` in your rules, the proposal carries Swap now and Keep buttons. A tap goes back through ntfy.sh to a topic derived from yours, and the laptop reads it over an outgoing connection. The phone never has to reach the laptop.

Each button carries a plan id that expires after 10 minutes and works once. A made-up one costs nothing. No endpoint takes a session id. ntfy.sh topics have no password, so the topic name is the secret. Anyone who learns it can read the pushes, which hold session codes, titles, times and rooms, the web app's Tailscale address if you use it, and each button's single-use id, but never the token or a session id. With buttons on, they could approve or dismiss a swap that re:Seat itself proposed, or run Update on the leave-now blocks re:Seat wrote, and nothing else. Opening the web app on the phone is optional, over Tailscale.

### For agents

`reseat mcp` runs a local MCP server over stdio, so any MCP client can drive re:Seat. Every change is two steps. `propose_changes`, `propose_swap` and `guard_sync` return a plan id and send nothing. `approve_changes` carries one plan out, once, within 10 minutes. No tool takes a list of session ids. The server tells the agent to ask you first, but it cannot check that the agent did, so use a client that shows each tool call before it runs. `reseat mcp --demo` runs the tools on the fake with no sign-in.

## Why re:Seat runs on your machine

I want to be clear about this one, because the first question people ask is why there is no hosted version. The Events API signs you in with OAuth and PKCE, using your own Builder ID, through a callback on a loopback port of the machine you sign in on. It has no hosted sign-in. A hosted re:Seat would have to hold other attendees' tokens, and those tokens can cancel their seats. So re:Seat runs on your laptop, keeps your tokens in the OS keychain, sends the access token only to `api.awsevents.com` and the refresh token only to the sign-in service, `oauth.awsevents.com`. Your phone never holds either.

## Respecting the API

| The API says | re:Seat |
|---|---|
| Quotas are per operation per minute. Reserve counts each session. | Keeps its own count and never sends a batch larger than what is left. |
| A 200 on reserve can carry per-session failures. | Reads every `failed` entry, then reads back the schedule. |
| Reserve and cancel are not safe to retry. | Never retries a write. A timeout or a server error is resolved by reading the schedule back. |
| 429 carries `Retry-After`. | Waits that long, once. |
| 409 means writes are switched off. | Stops writing, keeps every opening queued, and resumes when writes open. |

## Seen live: an empty catalog

On the evening of 8 October, the day API writes were scheduled to open, I ran the band inspection I had planned for after seating opened. The API served no catalog.

```
ListSessions                -> 200, totalCount 0, items 0
GetSession <held session>   -> 404 "No session was found with the requested id"
GetSchedule                 -> 200, reserved 14, favorites 16
```

GetSession returned 404 even for the 14 sessions I held, which I had booked in the portal on 6 October. The schedule still answered.

re:Seat's first sync that evening read the empty answer as "every session removed" and emptied its local copy of the catalog. It sent no write. I then fixed three things:

- An empty catalog, a walk that ends short of the API's own `totalCount`, or a sweep that would drop most of the catalog without bringing back one of about the same size is now refused. The local copy is kept.
- A catalog of about the same size with mostly new ids is recorded as a new starting point, not as thousands of new sessions to act on.
- Nothing is booked while a held session is missing from the local catalog. Without it, re:Seat cannot tell which talk a held seat is, and could book a second sitting of it.

The same condition was reported publicly on 3 October in [a public GitHub issue](https://github.com/jasonwadsworth/reinvent-scout/issues/17): `totalCount` 0, 404 on GetSession, GetSchedule still answering, and the official MCP server returning the same empty list. A post on the hackathon's discussion page on Builder Center reported the same symptoms: ListSessions and GetSession empty while signed in.

The full record, with commands and output, is in [proof/2026-10-08-catalog-empty.md](proof/2026-10-08-catalog-empty.md). I have checked every day since. One line per check is in the same file.

As of 11 October, the Events API still served no catalog, so re:Seat has never sent a write to AWS on my behalf. The live booking check, one reversible reserve and cancel with the schedule read back, runs the day the catalog returns, and I will update this article with the output and the date.

## How it was tested

Nothing here was built against the live API first. Every write path runs against an in-process fake that produces each documented failure: partial bulk results, `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409, 429 with `Retry-After`, 5xx, dropped connections and an expired sign-in.

A fault storm runs the whole flow for an hour on the real catalog as I pulled it on 1 October, the 2,006 of its 2,068 sessions that have a time: sync, favorites, booking, sixty sweeps with bands moving, swaps approved from the phone path, then leave-now blocks. 30 percent of reserves come back full, the first request each minute gets a 429, one gets a 503, writes return 409 for fifteen minutes, and the fake enforces the real quotas. Every run checks that no request exceeds a quota, no talk is held twice, only targets from the rules are held, and every write is read back before the next. Three seeds, each reaching a booking, a swap and the 409 window. The suite has 698 tests, and CI runs them on Linux, macOS and Windows.

The storm found two bugs. The watcher proposed swaps that could never run, and it re-sent writes every minute into a 409. A third turned up before it could lose a seat: a timeout while reserving B, after A was cancelled, escaped the rollback. I fixed it in the client, so every write path inherits the fix.

## What is a heuristic

I want to be honest about which parts are rules and which are guesses.

- **Queue or go.** A rule by session type: breakouts usually admit walk-ups, chalk talks need you early, workshops and builders' sessions admit almost none. It switches to the session's own band history once there are three changes. It is never a prediction, and the page always shows its basis.
- **Walking times.** Conservative estimates between the six 2026 venues, not official figures. Every screen, push and leave-now block says "estimate". AWS publishes no walking or shuttle times, so re:Seat adds none of its own.
- **Wynn and Encore** arrive from the API as one venue, `Wynn/Encore`. re:Seat splits them by room name.
- **Booking order.** Formats that are not recorded fill first, so they are sent first: workshops, labs and bootcamps, then builders' sessions, chalk talks, code talks, breakouts.

## Limits

- It manages seats you already chose. For choosing, use the AWS portal or a planner. re:Seat reads your official schedule, or imports reinvent-planner.cloud exports.
- It cannot help with the portal's opening rush.
- The web app is plain HTTP. If you open it on the phone, do it over Tailscale.
- The ntfy topic name is the secret. Make it long and random, and rotate it if it leaks.
- It is designed for a laptop left in the hotel room, plugged in and awake.
- The MCP server tells an agent to ask before approving, but cannot check that it did.

## Wrapping up

re:Seat does one job: it keeps the seats you chose, all week, within rules you wrote, and it tells you what it did and why. The code, the proof files and the demo are all in the repo. If you are attending re:Invent 2026, install it, point it at your schedule, and let me know what it catches for you.

- Code: [github.com/hsiddhu2/reseat](https://github.com/hsiddhu2/reseat)
- Install: [pypi.org/project/reseat](https://pypi.org/project/reseat/)
- How it works: [docs/architecture.md](architecture.md)
- API facts it relies on: [docs/api-facts.md](api-facts.md)
- Live proof: [docs/proof](proof/)
- Demos: [demo](../demo/)
- Click-through demo: [hsiddhu2.github.io/reseat](https://hsiddhu2.github.io/reseat/)

re:Seat is built for the AWS re:Invent 2026 Event Catalog API Builder Challenge, on the AWS Events API.
