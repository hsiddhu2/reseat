# re:Seat: the seats you missed, booked while you sleep

Six days after reserved seating opened for re:Invent 2026, 1,115 of the 1,460 sessions that take reservations were full. re:Seat is a small tool that runs on your laptop against the AWS Events API, watches the catalog every minute, and books the seat you wanted the moment one frees up or AWS adds a repeat sitting. It never sends a booking twice, reads every write back from your schedule, checks it has a way back in before it cancels anything, and has been keeping my own week since 13 October.

![re:Seat demo: a seat opens and a swap is proposed with Swap now and Keep, the week labels what changed, the swap is approved and verified, and Today shows when to leave](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/demo.gif)

Try it in two minutes, with no sign-in, on demo data:

```
pip install reseat
reseat serve --demo
```

Or open the [click-through demo](https://hsiddhu2.github.io/reseat/) in your browser. The code is at [github.com/hsiddhu2/reseat](https://github.com/hsiddhu2/reseat).

## The problem starts after the plan

Here is what attendees have said about reserved seating, from Reddit, blogs and Builder Center posts:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Use 11 minutes as I have found myself arguing with the staff." (2025)
- "Don't do one session at Venetian followed by a session at MGM. You'll never make it." (2022)

Seats free up after opening day, and AWS adds repeat sittings for popular sessions. Both happen when you are not looking. The only way to catch them has been to keep refreshing the app.

## What it does

| | What re:Seat does | The API calls behind it |
|---|---|---|
| **Books freed seats** | A talk you want moving from full to open is booked within a minute. | ListSessions sweep, ReserveSessions, GetSchedule read-back |
| **Books new repeats** | A new sitting of a talk you want is booked the moment it appears. Never two sittings of one talk. | ListSessions sweep, ReserveSessions, GetSchedule |
| **Swaps without losing a seat** | Moves you to the sitting you want more, only after checking there is a way back in. | GetSession fresh reads, CancelReservation, ReserveSessions, GetSchedule |
| **Gets you there on time** | Writes leave-now blocks into your official schedule. Pushes a morning briefing, leave in 10 minutes, and leave now. | CreatePersonalTime, UpdatePersonalTime, DeletePersonalTime |
| **Says queue or go** | For a talk you want but do not hold, the odds of a walk-up seat, with a range and its basis. | Band history from every sweep |
| **Keeps the official app in step** | Every target sitting shows as a favorite. When you drop a target, the favorites re:Seat added for it are removed. Never one you added, never a seat you hold. | AssociateFavorites, DisassociateFavorite, GetSchedule read-back |
| **Offers what is new** | A session just added, or a seat opening on a favorite, arrives with a Book button. One tap checks again, books and reads back. Nothing outside your rules is booked without it. | GetSession fresh read, ReserveSessions, GetSchedule read-back |
| **Asks on your phone** | A swap proposal arrives as a notification with Swap now and Keep. Alerts can also go by email or text through an SNS topic in your own AWS account. | None from the phone. It never holds a token. Amazon SNS for email and text. |
| **Plans with your own AI** | Four MCP tools let the AI app you already use search, find free slots, check a plan and draft your rules. | Local catalog, GetSchedule. The planning tools never reserve or cancel. |

**By the numbers:** all 12 operations of the Events API. 11 MCP tools. 22 demo scenarios. 1,223 tests on Linux, macOS and Windows. One host ever sees the access token. Zero hosted services.

## It has to be right the first time

Reserve and cancel are not idempotent. A retried booking can hold a second seat, and a retried cancel can drop one you meant to keep. So re:Seat follows the API's rules exactly, and the code enforces each one:

- **Never a blind retry.** After every reserve or cancel, re:Seat reads your schedule back with GetSchedule and journals the result. A timeout or a 5xx is settled by that read, never by sending the write again.
- **Quota per operation, per minute.** Bulk calls count sessions, not requests. A tracker never sends a batch larger than the quota left. On a 429 it waits `Retry-After` once, then stops.
- **Writes can be closed.** Until reserved seating opened to the API, every write returned 409. re:Seat handles that answer everywhere a write can happen, and probes for it only with a session you can never hold.
- **Check the event before any time.** Session times carry no time zone. The only place the API states it is GetEvent's `timezone`, so `reseat whoami` and `reseat sync` read it first and refuse an event in another zone or on other dates. re:Seat uses the IANA zone, not the abbreviation: the live API labels this December event PDT, when Las Vegas is on PST.
- **Personal time is UTC.** No offset, seconds `00`, a whole number of 5-minute steps. Sessions are Las Vegas time. One module does every conversion.
- **The token stays home.** Sign-in is OAuth with PKCE through a loopback port, so it only works on your own machine. Tokens live in the OS keychain. The access token goes only to `api.awsevents.com`, and the client refuses any other host. The refresh token goes to the Builder ID sign-in service.

![How re:Seat fits together: the CLI, web app and MCP server on your laptop share one engine and one local store. Only the API client talks to the AWS Events API, only Auth talks to Builder ID sign-in, and the phone reaches the laptop through ntfy.sh](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/architecture.png)

## Swapping a seat without losing one

The API has no swap. If you hold session A and the sitting you want more, B, opens at the same time, reserving B is refused with `scheduleConflict`. You must cancel A first, and for a moment you hold neither.

re:Seat cancels A only when three things hold:

1. **B is open** in a fresh GetSession read, takes reservations, and clashes with nothing else you hold.
2. **A has a way back.** Its own band is open, or another known sitting of A is open, read fresh, and clashes with nothing else you hold.
3. **You approved it**, from the Approve page or the notification, or allowed auto-swap for that target.

Then it cancels A, reserves B, and reads your schedule back. If B failed, it reserves A again. If A is gone too, it tries each fallback once, in order, and tells you exactly what you hold.

Three cases stop it with nothing more sent: the cancel fails and A is still held, a read-back fails, or writes close after the cancel. Each says exactly what you hold, or that the state is unknown. One swap runs at a time, across processes, and every step is journaled before and after it is sent.

![The Approve page at phone width: the held seat and the opened seat side by side, the checks, and Swap now or Keep](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/approve.png)

## Seen live, on the real API

I tested on the live API and kept the records. Session codes of my own holds are left out.

**The catalog went empty.** On 8 October, the day API writes were due to open, the API served no catalog:

```
ListSessions                -> 200, totalCount 0, items 0
GetSession <held session>   -> 404 "No session was found with the requested id"
GetSchedule                 -> 200, reserved 14, favorites 16
```

re:Seat's first sync read that as "every session removed" and emptied its local copy. It sent no write. Since then, an empty or short catalog is refused, and nothing is booked while a held session is missing. Others hit the same empty catalog and said so in a post on [the hackathon's discussion page](https://builder.aws.com/build/hackathons/7c0e8c59-35d9-3ceb-8f80-0d5dc41bfebf/reinvent-event-catalog-api-hackathon?tab=discussion). [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-08-catalog-empty.md) has one line for each daily check.

**The catalog came back.** On 12 October it returned with 2,227 sessions, and 1,115 of the 1,460 that take reservations were full. Session ids survived the outage, and 13 sessions changed their code under the same id, so a target pinned by id followed them. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-catalog-returned.md).

**The API differed from its spec.** A cancel on a session you do not hold is documented as 404. The live API answered 400. re:Seat now reads either as writes open, with a test for each.

**Writes work, and every one is read back.** I reserved a repeat sitting I did not want, read it back, cancelled it and read it back. Twice:

```
22:32:57 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
22:33:19 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
22:40:06 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
22:47:16 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
```

The second time I checked the seat in the AWS portal before cancelling. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-round-trip.md).

**It runs my week.** Since 13 October the watcher has run on my own schedule. On 14 October I opened the web app on my phone over Tailscale ([the record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-14-tailscale.md)). My sessions' codes, titles and rooms are covered:

![My week on the live catalog: held sessions coloured by type, a leave time on each, and the talks I want but do not hold beside them. Codes, titles and rooms are covered](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/live-week.png)

![Today on my phone: when to leave, the walk as an estimate, and walk-up odds for three talks I want. Codes, titles and rooms are covered](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/live-phone-today.png)

**What the watcher saw in twelve days.** From 12 to 24 October it swept the whole catalog 6,748 times while my laptop was awake. A full session opened again 30 times, on 23 sessions, on 11 of the 13 days. Four of those seats were gone again within 10 minutes, one in about a minute. 62 sessions were added, 51 still listed. Full sessions rose to 1,373 of 1,479. None of the 23 was a talk I asked for, so it booked nothing. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-20-watcher-first-week.md) will be updated up to submission. The same counts, drawn as charts, are on [the Findings page](https://hsiddhu2.github.io/reseat/findings.html), exported from my laptop with `reseat findings --json`.

**Favorites in step, both ways.** On 24 October `favorites sync` added one talk to my favorites, and after I dropped it from my rules, removed it again. Each write was read back. My own 25 favorites were never listed for removal. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-24-favorites-round-trip.md).

**Live and simulated, plainly.** Seen live: the empty catalog, its return, the 400, two booking round trips, a favorite added and removed, the watcher on my week, the phone over Tailscale, 36 connection drops it recovered from on its own. Not yet seen live: a swap, a booking the watcher caught, a Book tap, an SNS alert, a seat filling at the moment of booking. Those run against a fake of the API that produces every failure the documentation lists.

## Getting you to the room before the doors close

Door staff stop admitting 11 minutes before the start. re:Seat's leave time is the session start, minus those 11 minutes, minus enough time for the walk to be done in time 80 percent of the time, minus a buffer you set when the change is tight. Walking speeds vary by about 19 percent from person to person, so a typical 40-minute walk gets 47 minutes. When you ask it to, re:Seat writes the leave time into your official schedule, and the Today page counts it down. Walking times between the six 2026 venues are labelled as estimates everywhere they appear.

![The Today page at phone width: the leave-in countdown, what is next, and queue-or-go advice for a wanted talk without a seat](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/today.png)

## Your own AI plans, re:Seat keeps the seats

With over 2,000 sessions, picking still takes hours. `reseat mcp` runs a local MCP server, so the AI app you already use can plan with you. It gets four planning tools:

- `search_sessions` searches the local catalog by text, type, day, venue, seat band, level, topic and service.
- `open_slots(day)` lists the free gaps in a day, with the sessions you can walk to and away from before the doors close.
- `check_plan(codes)` states facts about a list: full, clashes, two sittings of one talk, a walk you cannot make, a meal, your caps.
- `draft_rules(add, remove)` drafts your rules file and shows the whole target order. Approving writes only that local file.

Every answer lists in start-time order and never ranks. The planning tools never reserve or cancel. Any change to your seats is a propose, then a separate approve by plan id.

The walk check caught something the API does not. The repeat sitting from the round trip above starts at 12:30 at Caesars Forum. One of my own sessions ends at 12:30 at Caesars Palace. The API accepted the booking, because the times do not overlap. Asked to check it, `check_plan` answered ([the record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-21-check-plan.md)):

```
- AMZ202-R1  Mon 30 Nov 12:30 PT, 120 min  Caesars Forum, Level 1 | Forum 101  unavailable
  full, no seat to reserve now.
  cannot be reached in time: held <held session> ends 12:30 at Caesars Palace, the walk is about 12 min
  (estimate) and 15 min is allowed, enough to arrive in time 80% of the time, so arrival is
  about 12:45, 26 min after doors close at 12:19.
  No other sitting of this talk is open.
```

## How it was tested

Every write path runs against an in-process fake of the API that produces each documented failure: partial bulk results, `scheduleConflict`, 409, 429 with `Retry-After`, 5xx, dropped connections and an expired sign-in.

A fault storm runs the whole flow for an hour on the 2,006 timed sessions of the real catalog. 30 percent of reserves come back full, a 429 lands every minute, plus a 503 and fifteen minutes of 409, under the real quotas. It checks that no quota is exceeded, no talk is held twice and every write is read back. The storm found two bugs: swaps proposed that could never run, and writes re-sent every minute into a 409. A third turned up beside it before it could lose a seat: a timeout while reserving B, after A was cancelled, escaped the rollback. The fix went into the client, so every write path inherits it.

The planning tools were also run from an AI app against my real schedule, and every answer was read for wording that ranks or misleads. That run found six things to fix before release ([the record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-21-check-plan.md)).

## Limits, honestly

- It cannot help with the opening rush. Reserved seating opened in the AWS portal two days before API writes did.
- It is built for a laptop left in the hotel room, plugged in and awake.
- Walking times and walk-up odds are estimates, and labelled as such.
- It lists and checks sessions. Choosing them is yours. It reads your official schedule, or imports reinvent-planner.cloud exports.
- The MCP server tells your AI app to ask you before approving a plan, but cannot check that it did. Use an app that shows each tool call.
- The web app is plain HTTP. On the phone, open it over Tailscale.

## What's next

I will run re:Seat through re:Invent week on my own schedule and publish what it caught: the seats it booked, the swaps it proposed and the walks it timed. The proof files in the repo will grow with it.

If you are attending re:Invent 2026, install it, point it at your schedule, and tell me what it catches for you.

- Code: [github.com/hsiddhu2/reseat](https://github.com/hsiddhu2/reseat)
- Install: [pypi.org/project/reseat](https://pypi.org/project/reseat/)
- Click-through demo: [hsiddhu2.github.io/reseat](https://hsiddhu2.github.io/reseat/)
- How it works, and how each of the 12 API operations is used: [docs/architecture.md](https://github.com/hsiddhu2/reseat/blob/main/docs/architecture.md#api-operations-used)
- The models behind it, with sources: [docs/models.md](https://github.com/hsiddhu2/reseat/blob/main/docs/models.md)
- API facts it relies on: [docs/api-facts.md](https://github.com/hsiddhu2/reseat/blob/main/docs/api-facts.md)
- Every live record: [docs/proof](https://github.com/hsiddhu2/reseat/tree/main/docs/proof)

re:Seat is built for the AWS re:Invent 2026 Event Catalog API Builder Challenge, on the AWS Events API.
