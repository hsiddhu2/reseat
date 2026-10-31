# re:Seat

re:Seat keeps your AWS re:Invent seats after the plan is made. You rank the talks you want in a rules file. It watches the whole catalog, books a target when a seat frees or a new repeat sitting appears, swaps a held seat for one you rank higher only after checking a way back, and writes when to leave into your official schedule. It runs on your own laptop against the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html). Alerts reach your phone, and email through your own Amazon SNS topic.

**Plan where you already plan.** re:Seat does not rank or pick talks. Bring the plan you have, or build it with your AI app, and re:Seat keeps the seats:

```bash
reseat rules from-schedule        # your official AWS schedule: reserved first, then favorites
reseat rules import plan.json     # or a JSON export from reinvent-planner.cloud, in its order
reseat favorites sync --dry-run   # see what goes into the official app as favorites
reseat serve                      # watch, book and swap within your rules
```

Or plan with your AI app. `reseat mcp` gives any AI app or agentic IDE that runs MCP servers local search, free slots and plan checks, and drafts the rules file for your approval. Your AI app chooses, re:Seat supplies the facts and the checks. Setup for Claude Code, Kiro, Cursor, Windsurf and VS Code is in [the MCP guide](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/mcp.md).

![re:Seat demo: a seat opens and a swap is proposed with Swap now and Keep, the week labels what changed, the swap is approved and verified, and Today shows when to leave](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/demo.gif)

**Three ways in:**

1. **No install:** [the click-through demo](https://hsiddhu2.github.io/reseat/), the web app on demo data, built from this code.
2. **Two minutes, no sign-in:** `uvx --python 3.12 reseat serve --demo`, or with Python 3.11 or newer, `pip install reseat` then `reseat serve --demo`. Open `http://127.0.0.1:8491/`. `pip install "reseat[mcp]"` then `reseat mcp --demo` serves the MCP tools on the same demo data. See [Try it in two minutes](#try-it-in-two-minutes).
3. **What it did on the real API:** each dated line below links to its record. All records are in [docs/proof](https://github.com/hsiddhu2/reseat/tree/main/docs/proof/).

**Seen live, on one attendee's schedule:**

- **8 October:** the API served an empty catalog. The first sync emptied re:Seat's local copy and sent no write. A guard added that day refused every later empty answer until the catalog returned on 12 October. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-08-catalog-empty.md), [the guard's test](https://github.com/hsiddhu2/reseat/blob/main/tests/test_sweep_guards.py#L33), [the return](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-catalog-returned.md).
- **12 October:** two reversible bookings, each read back and cancelled. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-round-trip.md).
- **12 to 24 October:** 6,892 sweeps of the whole catalog. 30 openings on 23 full sessions, and 62 sessions added. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-20-watcher-first-week.md), [the Findings page](https://hsiddhu2.github.io/reseat/findings.html).
- **24 October:** a favorite added, then removed when its target was dropped, each read back. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-24-favorites-round-trip.md). A test message by email through re:Seat's notifier and the attendee's own SNS topic. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-24-email-alert.md).
- **25 October:** the rules file booked two ranked targets on its own, read back and emailed. Both seats were already open. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-25-autopilot-first-booking.md).
- **25 October:** a swap run with `reseat swap`, there and back, each verified by a read of the official schedule. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-25-live-swap-round-trip.md).
- **25 October:** 15 leave-now blocks written to the official schedule, after finding that the API refuses some characters in a description. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-25-leave-now-blocks.md).
- **26 October:** a swap run by the watcher on its own, there and back, each verified by a read of the official schedule. A rules edit started it, not a seat freeing. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-26-watcher-swap.md).
- **26 October:** a reserve that clashes with a held seat, refused by the API with `scheduleConflict` and the held seat it clashes with, as documented. Nothing changed. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-26-clash-refused.md).
- **Not yet seen live:** a seat caught as it frees.

**Read the story** on [AWS Builder Center](https://builder.aws.com/project/3KZhHk8a0XfKPUaIdZUTKjSLoZ4/reseat-the-seats-you-missed-booked-while-you-sleep).

**What re:Seat does:**

- **Books what frees up.** Every minute by default it checks the whole catalog. A freed seat or a new repeat sitting of a talk you want is booked at once, inside your rules and the API's quota, and read back from your schedule.
- **Swaps without losing a seat.** When a better sitting opens at the same time as one you hold, it asks you: on the Week page, and with `ntfy_approve: true` as a notification with Swap now and Keep. Swap now cancels only after a fresh read shows the new seat open and a fallback for yours, and books yours again if the new one is refused. A target marked `auto_swap: true` swaps on its own after the same checks, and tells you.
- **Offers what is new, booked on your tap.** A session added to the catalog, or a seat that opens on one of your favorites, comes as an offer with a **Book** button: on the Approve page, and with `ntfy_book: true` on the notification. Book checks the seat and your day again, reserves, and reads your schedule back. Nothing outside your rules is booked without the tap.
- **Shows what it saw.** The Findings page counts every opening on a full session, how fast it filled again, sessions added and outages recovered. [Live counts from one attendee's laptop](https://hsiddhu2.github.io/reseat/findings.html).
- **Tells you when to leave.** Leave-now blocks in your official schedule, a morning briefing, leave in 10 minutes, and whether to queue for a walk-up seat. Walking times are labelled as estimates.
- **Plans with your AI app, without ranking.** Through `reseat mcp`, the app searches the local catalog, finds free slots you can walk to, checks a plan for clashes, walks and full sittings, and drafts the rules file. It is written only when you approve the plan id.
- **Keeps your token on your laptop.** Nothing is hosted. The token goes only to `api.awsevents.com`.

Booking freed seats, the safe swap and leave-now blocks were committed on 2 October ([ee7655a](https://github.com/hsiddhu2/reseat/commit/ee7655a)) and released on PyPI in 0.5.1 on 10 October. When each feature landed: [Release history](#release-history).

**At a glance.** Each line links to its evidence. The research each decision rests on is in [The models behind re:Seat](https://github.com/hsiddhu2/reseat/blob/main/docs/models.md).

- **Never resends a booking on a guess.** A timeout or a server error on a reserve or cancel is settled by reading your schedule back, never by sending it again. A write is resent in two cases only: once after a 429, when its `Retry-After` has passed, and once after a 401, when the sign-in is refreshed. Tests: [each resend case](https://github.com/hsiddhu2/reseat/blob/main/tests/test_write_resends.py), [5xx on a booking](https://github.com/hsiddhu2/reseat/blob/main/tests/test_router.py#L262), [5xx on a cancel](https://github.com/hsiddhu2/reseat/blob/main/tests/test_cancel_and_cli.py#L58), [a timeout mid-swap](https://github.com/hsiddhu2/reseat/blob/main/tests/test_swap.py#L318).
- **Every booking and cancel is read back** from your official schedule. Tests: [after a booking](https://github.com/hsiddhu2/reseat/blob/main/tests/test_router.py#L155), [through a whole fault storm](https://github.com/hsiddhu2/reseat/blob/main/tests/test_fault_storm.py#L150).
- **Tested under a fault storm:** a simulated hour against the fake API on a 200-session sample of the real 2026 catalog, with 30 percent of bookings full, a 429 every minute, a server error, fifteen minutes of writes switched off, and the API's quotas enforced. [The test](https://github.com/hsiddhu2/reseat/blob/main/tests/test_fault_storm.py).
- **All 12 operations of the Events API**, each in a real feature and under the API's own rules: GetEvent checks the event's time zone before any session time is trusted, and DisassociateFavorite removes only favorites re:Seat added for a dropped target. [The table](https://github.com/hsiddhu2/reseat/blob/main/docs/architecture.md#api-operations-used).
- **Over 1,200 tests**, run on Linux, macOS and Windows with Python 3.11 and 3.13 on every push. [CI](https://github.com/hsiddhu2/reseat/actions/workflows/ci.yml).
- **The 2026 campus:** Caesars Forum, Caesars Palace, Encore, MGM Grand, The Venetian, Wynn. [The venue table](https://github.com/hsiddhu2/reseat/blob/main/src/reseat/campus.py#L18).

## The problem

Planners help you pick sessions. Attendees say the trouble starts after that:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app. Most sessions will have no shows." (2022)
- "I was the 50th person on the walk-up line. They only accepted 20." (2023)

Seats free up, repeats get added, rooms move. Catching that means refreshing the app all week. re:Seat does the refreshing and acts on what it finds, within rules you set.

## Try it in two minutes

Python 3.11 or newer.

```bash
pip install reseat         # or: pipx install reseat, or run it with uvx --python 3.12 reseat serve --demo
reseat serve --demo        # a scripted week on a fake Events API. No sign-in. Open http://127.0.0.1:8491/
```

Demo mode runs the real watcher, router and swap code against an in-process fake of the API, with a week of real sessions from the 1 October catalog. Within two minutes a seat opens and a swap waits for your approval, a new sitting is booked, and a held session changes room. The **Demo** page, with a guided tour, then lets you press each thing that can happen at AWS. Beside the list it shows what AWS did, what re:Seat did, your week before and after with the changed sessions labelled, and the notifications your phone got. The scenarios: a seat filling mid-swap (rolled back), a booking the read-back cannot confirm, writes switched off and on, the API going down, an empty catalog, a lost sign-in, the clock jumping to the morning briefing or to a leave time, and more. Each runs a real sweep on the fake API, and with `--push` each one reaches your phone. Nothing is sent to AWS, nothing is read from the keychain, and nothing is written to `~/.reseat`. Every page says "Demo data".

With your own seats, `reseat serve` runs the watcher and the same web app at `http://127.0.0.1:8490/`, in one process.

| Week, laptop width | Approve, phone width | Today, phone width |
|---|---|---|
| ![Week: status line, a swap waiting for approval, what just changed, and the week grid with a NEW label](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/week.png) | ![Approve: the held and opened sessions side by side, the checks and one button](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/approve.png) | ![Today: leave-in countdown, next sessions, wanted sessions with queue-or-go advice](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/today.png) |

![Demo page: a scenario list, and beside it what AWS did, what re:Seat did, the week before and after, and the phone's notification](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/demo.png)

| Page | What it is for |
|---|---|
| **Week** (`/`) | Swaps that need you, with their checks. What just changed. The calendar: held, wanted, proposed and fallback sessions, leave-now strips, a line at the current time, and a 15-minute label on anything that changed: NEW, SWAPPED IN, MOVED, RELEASED, LEFT CATALOG, PROPOSED. Click a session for its details, or a day for the Day view. |
| **Today** (`/today`) | How long until you leave, the walk (an estimate) and when the doors close. The rest of today. Talks you want but do not hold, with queue-or-go advice and its basis. |
| **Approve** (`/approve`) | One swap at a time: what you hold and what opened, side by side, the checks, Swap now and Keep. Seats booked today beside it. |
| **Activity** (`/activity`) | Seats booked, swaps verified and seats restored, counted from the journal. Every change to your week, catalog changes, and every write with its read-back. |
| **Targets**, **Status** | Your rules read back in order with each sitting's seats. The watcher, bookings open or closed, the quota left this minute per operation, and your push settings. |
| **Demo** (`/demo`, demo mode only) | Scenarios, and what each did at AWS, in re:Seat, on your week and on your phone. |

Every page fits the screen: two columns on a laptop, one on a phone, with one menu, a bottom bar on the phone. It follows your system's light or dark setting. Swap now sends a plan id and nothing else, and runs the same checked swap as `reseat swap`, with fresh reads. The CLI and the MCP server use the same engine.

## Install

```bash
pipx install reseat        # or: pip install reseat
reseat login
```

Python 3.11 or newer. `login` opens your AWS Builder ID sign-in. Use the Builder ID your re:Invent registration is under: `reseat whoami` should then say `Registered for reinvent2026`. The tokens stay in your OS keychain and are sent only to `api.awsevents.com`.

**Dependencies**, installed with it: httpx, pydantic, typer, keyring, rich and PyYAML, plus tzdata on Windows. Optional extras: `pip install "reseat[mcp]"` adds the MCP SDK for `reseat mcp`, and `pip install "reseat[aws]"` adds boto3 for email and text alerts through your own Amazon SNS topic.

Then point re:Seat at the sessions you want:

```bash
reseat sync                                  # the catalog, about 10 calls
reseat rules from-schedule                   # your reserved sessions and favorites from the AWS portal
reseat favorites sync                        # mirror them into the official app
reseat book --dry-run                        # see the plan. Nothing is sent
```

## Your week with re:Seat

| When | What re:Seat does |
|---|---|
| While you plan | Your own AI app plans with you through re:Seat's MCP server: it searches the catalog, finds the free gaps in a day with the walks counted, checks a list of sessions against what you hold, and drafts the rules file. You approve the draft. re:Seat lists and checks, and the choosing stays with you. See [MCP server](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/mcp.md). |
| Before writes open | Takes your targets from the schedule you built in the AWS portal, or from a [reinvent-planner.cloud](https://reinvent-planner.cloud) export, and mirrors them into your favorites. |
| The day API writes open | `reseat book` reserves your targets, scarcest formats first and then in your order, 10 per call, inside the quota. A full session falls back to its next sitting in the same run. Every write is read back. |
| Every day until the event | `reseat serve` sweeps the catalog every minute. A freed seat or a new repeat of a target is booked at once. A sitting your rules put first that clashes with a lower-priority hold becomes a swap proposal: a card on the Week page, and a push, with Swap now and Keep if you opt in. Leave-now reminders go into your official schedule. |
| At re:Invent | The laptop stays in the hotel room. Your phone gets the morning briefing, leave in 10 minutes and leave now, and a heads-up when a talk you want starts soon, with whether to queue. During the day the laptop checks your sessions every 20 seconds, so a no-show's seat can be booked while you stand in the walk-up line. |

## Safe swap

The API has no swap. To move from held session A to a better B at the same time, A must be cancelled before B can be reserved, and for that moment you hold neither. re:Seat cancels A only when a fresh read shows B open, B clashes with nothing else you hold, A has a fallback (its own open seat or another open sitting of A, read fresh), and you approved or allowed auto-swap for that target. If the cancel fails and A is still held, it stops. If B fails, it re-reserves A. If A is gone too, it tries each fallback once and tells you exactly what you hold. If a read-back fails, or writes close mid-swap, it stops and says so. One swap at a time, every step journaled.

## Autopilot: the rules file is your instructions

The rules file is the whole instruction set. Write it by hand, from your schedule with `reseat rules from-schedule`, or with your own AI app through `draft_rules` (see the MCP server). Then `reseat serve` works on its own and tells you what it did, by push and, with `sns_topic_arn`, by email.

- **Rank is order.** Put the sittings you want most first. Several targets can share a time slot: a lower one holds the slot until a higher one frees.
- **Open targets are booked at start.** On the first normal sweep after `reseat serve` starts, and after the rules file changes, every target with an open seat is booked in rank order, read back and pushed.
- **The rules file is re-read while it runs.** Save a change and it is used on the next sweep, with no restart. A file that does not load, has more targets than `watch_cap`, is a symbolic link, or on macOS and Linux belongs to another user or is writable by others, is refused and the old rules stay. Windows has no such owner check, so keep `RESEAT_HOME` inside your user profile. Targets, ranks, caps, meals and `auto_swap` follow the file. `serve_secret`, `ntfy_topic`, the push buttons (`ntfy_approve`, `ntfy_book`) and `sns_topic_arn` keep the values from the start. Each change is pushed and emailed, with how many targets are on autopilot.
- **`auto_swap: true` swaps on its own.** When a higher-ranked target with `auto_swap` frees while a lower-ranked target holds its slot, re:Seat runs the safe swap above without asking, and pushes and emails the result. Without it, the swap waits for your approval. It only ever swaps between targets in your rules. A seat you hold that is not a target is never touched.
- **To drop a talk, take it out of the rules.** A target you cancel in the official app is booked again if it is still open, at the next start or the next time you save the rules file, even for an unrelated change.

## The phone remote

Sign-in only works on your own machine, so the token stays on the laptop and the laptop makes every call. The phone gets notifications through the free ntfy app. With `ntfy_approve: true`, a swap proposal carries **Swap now** and **Keep** buttons: the tap goes back through ntfy.sh to the laptop, which only makes outgoing connections, so the phone never has to reach it. The topic name is the secret: see [what a leaked topic allows](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/phone.md). Opening the web app itself on the phone is optional, over [Tailscale](https://tailscale.com). Designed for a laptop left in the hotel room.

## Why re:Seat runs on your machine

The Events API signs you in with OAuth and PKCE, using your own Builder ID, through a callback on a loopback port of the machine you sign in on. It has no hosted sign-in. A hosted re:Seat would have to hold other attendees' tokens, and those tokens can cancel their seats. So re:Seat runs on your laptop, keeps your token in the OS keychain, sends it only to `api.awsevents.com`, and your phone talks only to your laptop.

## What the API requires, and what re:Seat does

| The API says | re:Seat |
|---|---|
| Quotas are per operation per minute, and reserve counts each session | Keeps its own count per operation and never sends a batch larger than what is left. |
| A 200 on reserve can still carry per-session failures | Reads every `failed` entry, then reads back your schedule after every write. |
| Reserve and cancel are not safe to retry | Never retries a write. A failure is resolved by reading the schedule back. |
| 429 carries `Retry-After` | Waits that long, once. |
| 409 means writes are switched off | Stops writing, keeps every opening queued, resumes when writes open. |
| Seat availability is a band, not a count | Treats a move from `unavailable` to an open band as a freed seat. |
| There is no search | Pulls the whole catalog, about 10 calls, and works locally. |
| Personal time is UTC in 5-minute steps | Writes leave-now blocks exactly that way. |

## Seen live

On 8 October 2026, between 20:19 and 20:28 PDT, on the day writes were scheduled to open, the Events API answered GetSchedule but served no sessions:

```
ListSessions                -> 200, totalCount 0, items 0
GetSession <held session>   -> 404 "No session was found with the requested id"
GetSchedule                 -> 200, reserved 14, favorites 16
```

re:Seat's sync read the empty answer as every session removed and emptied its local copy of the catalog. It sent no write, and nothing on the attendee's AWS schedule changed. Three fixes followed, each tested against the fake: a sweep that comes back empty, or would drop more than half the catalog without bringing back one of about the same size, is refused and the local copy kept, a sweep of about the same size whose ids are mostly new is recorded as a new baseline, and nothing is booked while a held session is missing from the local catalog. The record, with commands and output, is in [docs/proof/2026-10-08-catalog-empty.md](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-08-catalog-empty.md), with one line for each daily check since. The catalog stayed empty through 11 October.

On 12 October at 20:12 PDT it came back: 2,227 sessions with seat bands. Six days after reserved seating opened, 1,115 of the 1,460 sessions that take reservations were full. Session ids survived the outage. Thirteen sessions changed code under the same id, so a target pinned by session id followed them. The probe found one place where the live API and the spec differ: a cancel on a session not held answers 400 "The request could not be applied to this session", where the spec documents 404. re:Seat handles both. The record is in [docs/proof/2026-10-12-catalog-returned.md](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-catalog-returned.md).

The same evening, two reversible round trips on a repeat sitting the attendee did not want, AMZ202-R1, confirmed that writes are open:

```
22:32:57 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
22:33:19 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
22:40:06 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
22:47:16 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
```

The rest of the schedule was the same before and after. The commands and output are in [docs/proof/2026-10-12-round-trip.md](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-12-round-trip.md). On 25 October a swap run with `reseat swap` and a booking made by the rules file were seen live, and on 26 October a swap run by the watcher on its own, linked above. On 26 October a reserve that clashes with a held seat was refused live with `scheduleConflict`. A seat caught as it frees and a reserve refused as full have not yet been seen live. They are tested against the fake.

### On the real catalog

Since 12 October the watcher has run against the live API on the attendee's own schedule, on the attendee's laptop. On 14 October the attendee opened the web app on a phone over Tailscale. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-14-tailscale.md). From 12 to 24 October the watcher swept the catalog 6,892 times. A full session opened again 30 times on 23 sessions, on 11 of the 13 days, and four of those openings were full again within 10 minutes. 62 sessions were added, 51 still listed. None was a talk the attendee asked for, so nothing was booked. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-20-watcher-first-week.md). On 24 October `favorites sync` added a favorite and removed it again after its target was dropped, each write read back. [The record](https://github.com/hsiddhu2/reseat/blob/main/docs/proof/2026-10-24-favorites-round-trip.md). In these screenshots the session codes, titles and rooms of the attendee's sessions are covered. Times and everything re:Seat adds are not.

![The Week page on the live catalog: held sessions coloured by type, a leave time on each, wanted talks in a strip beside them, and the watcher's last sweep of 2,240 sessions in the top bar. Session codes, titles and rooms are covered](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/live-week.png)

![The Today page on the attendee's phone, opened over Tailscale from the laptop running re:Seat: leave in 46 days for the first session, the walk as an estimate, and walk-up odds with a range for three wanted talks. Codes, titles and rooms are covered](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/live-phone-today.png)

![The Activity page: seat-band changes seen in the live catalog, three network outages on 14 October that the watcher recovered from on its own, and both round trips](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/screenshots/live-activity.png)

## Limits

- It lists and checks sessions. Choosing them is for you, with your AI app or a planner. There is no schedule solver.
- Queue-or-go advice is odds with a range: the rule for the session type, updated each time re:Seat sees the session fill and free a seat. It is a heuristic, never a prediction, and the range is wide until there is data. See [the models](https://github.com/hsiddhu2/reseat/blob/main/docs/models.md).
- Walking times between venues are conservative estimates, not official figures, and every screen, push and leave-now block says so: "about 60 min (estimate)". AWS publishes no walking or shuttle times, so re:Seat adds none of its own. Wynn and Encore come from the API as one venue and are split by room name.
- The web app is plain HTTP. Use it over Tailscale, not open hotel Wi-Fi. It is access control, not a security product.
- It cannot help with the first rush when reserved seating opens in the portal, two days before the API opens.
- The MCP server tells an agent to ask before approving a change, but cannot check that it did.

## How it is tested

Every write path runs against an in-process fake of the API that produces each documented failure: partial bulk results, `sessionFull`, `scheduleConflict`, 409, 429 with `Retry-After`, 5xx, dropped connections and expired sign-in. A fault storm runs the whole flow for a simulated hour against the fake API, on a 200-session sample of the real 2026 catalog, with 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, and the real quotas enforced, and checks that no quota is exceeded, no talk is held twice and every write is read back. Over 1,200 tests. CI runs them on Linux, macOS and Windows, on Python 3.11 and 3.13. Details in [How re:Seat works](https://github.com/hsiddhu2/reseat/blob/main/docs/architecture.md).

## Reference

### Use

```bash
reseat events                      # list events, no sign-in needed
reseat sync                        # pull the whole catalog, about 10 API calls
reseat sync --no-abstracts         # cheap sweep, reports band changes and new sessions
reseat sync --force                # apply even an empty or much smaller catalog. Exit 4 means a sweep was refused
reseat search "serverless"
reseat show <sessionId>            # details, repeats, seat band history
reseat schedule                    # your reserved, favorites, personal time
reseat findings                    # what the watcher saw, in counts. --json <file> writes them out
reseat favorite <id> <id> ...      # add favorites, 10 per call, quota aware
reseat probe --session-id <id>     # are reservation writes open yet? never changes anything
reseat save-fixture                # dev: save a catalog pull for tests, no abstracts or speakers
reseat rules init                  # write a commented ~/.reseat/rules.yaml
reseat rules import export.json    # targets from a reinvent-planner.cloud export, in its order
reseat rules from-schedule         # targets from your official schedule: reserved first, then favorites
reseat rules check                 # every code resolves in the local catalog?
reseat book --dry-run              # sweep, then print the plan. Sends nothing
reseat book                        # reserve, fall back on full sessions, read back. --yes skips the confirmation
reseat cancel <sessionId>          # shows the seat band, asks first, reads back
reseat favorites sync              # mirror target sittings into favorites, remove re:Seat's own for dropped targets
reseat watch                       # sweep every minute, book freed seats and new repeats. --once for cron
reseat swap <held> <wanted>        # replace a held session safely: fallback checked, rolls back on failure
reseat guard sync                  # leave-now blocks in your official schedule. --dry-run shows the diff
reseat serve                       # watcher plus the web app and push. See "Running it all week"
reseat serve --demo                # the web app on a fake API with a scripted week. No sign-in
reseat mcp                         # local MCP server on stdio. Needs pip install "reseat[mcp]"
reseat mcp --demo                  # the same tools on the demo's fake API. No sign-in
reseat logout
```

Data lives in `~/.reseat/reseat.db`. Set `RESEAT_HOME` to move it. On macOS and Linux, re:Seat creates the folder readable by you only and the database the same way, and tightens an existing `~/.reseat`. A folder you name with `RESEAT_HOME` that already exists, or a symbolic link, keeps its own permissions.

### Rules file

`~/.reseat/rules.yaml` lists targets in priority order. re:Seat reserves nothing else. `reseat rules init` writes a commented one, and `reseat rules from-schedule` writes one from the schedule you built in the AWS portal.

The full reference, with every setting and an example file: [Rules file](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/rules.md).

### Running it all week

re:Seat is designed for a laptop left in the hotel room, plugged in and awake, while you carry only your phone. Your sign-in can only happen on your own machine, so the laptop makes every API call. The phone gets notifications through the ntfy app and approves a swap from the notification. Opening the web app on the phone is optional, over Tailscale.

How to keep the laptop awake with the lid closed, set up ntfy push, approve from the notification, keep the topic secret, and open the web app on the phone over Tailscale: [Running it all week](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/phone.md). Email and text alerts through your own Amazon SNS topic: [Email and text alerts](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/alerts.md).

### MCP server

`reseat mcp` runs a local MCP server over stdio, so an agent can read your targets and plan changes for you. Your AI app starts it as a child process on your machine. Nothing is hosted.

Once added, your AI app should list the reseat server with 11 tools.

Tools: `list_targets`, `propose_changes`, `propose_swap`, `approve_changes`, `explain_drift`, `guard_sync`, `queue_or_go`, and for planning `search_sessions`, `open_slots`, `check_plan` and `draft_rules`. Every change is two steps. `propose_changes`, `propose_swap` or `guard_sync` returns a plan id and sends nothing, and `approve_changes` carries that plan out once, within 10 minutes.

Without installing, with [uv](https://docs.astral.sh/uv/): `uvx --python 3.12 --from "reseat[mcp]==0.8.1" reseat mcp`. `reseat mcp --demo` serves the same tools on the demo's fake API, with no sign-in.

Install, the config entry for each AI app, the plugins, every tool and its limits: [MCP server](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/mcp.md).

### Good citizen rules

re:Seat only reserves what you asked for. It never holds two sittings of one talk. Watch lists are capped. It does not scrape or redistribute the catalog. Seats it frees during a swap go back to the pool at once.

### Development

```bash
git clone https://github.com/hsiddhu2/reseat && cd reseat
pip install -e ".[dev]"
pytest
ruff check .
```

A fault storm runs the whole flow for a simulated hour on a 200-session sample of the real catalog: 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, quotas enforced. Tests run against an in-process fake of the API that produces each documented failure re:Seat handles: `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409 while writes are closed, 429 with `Retry-After`, and partial batch results. No network needed.

`tests/fixtures/catalog-sample-2026-10-01.json` is a 200-session sample of the 1 October 2026 catalog pull (no abstracts, no speaker names), not the whole catalog. It keeps every session type and venue, and each sampled talk with all its sittings. `FakeEventsApi.from_fixture(path)` serves it, so tests see the real venue, room and type strings.

### Docs

- [How re:Seat works](https://github.com/hsiddhu2/reseat/blob/main/docs/architecture.md): the design, each part marked built or planned, and how it is tested.
- [Design decisions](https://github.com/hsiddhu2/reseat/blob/main/docs/adr/README.md): each architecture decision, why it was made and what it costs.
- Guides: [Rules file](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/rules.md), [Running it all week](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/phone.md) (phone, push, Tailscale), [Email and text alerts](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/alerts.md) and [MCP server](https://github.com/hsiddhu2/reseat/blob/main/docs/guide/mcp.md).
- [AWS Events API facts](https://github.com/hsiddhu2/reseat/blob/main/docs/api-facts.md): the API behaviour re:Seat relies on, with sources.
- [Changelog](https://github.com/hsiddhu2/reseat/blob/main/CHANGELOG.md), [Security](https://github.com/hsiddhu2/reseat/blob/main/SECURITY.md), [Contributing](https://github.com/hsiddhu2/reseat/blob/main/CONTRIBUTING.md) and [Code of conduct](https://github.com/hsiddhu2/reseat/blob/main/CODE_OF_CONDUCT.md).
- [Live proof](https://github.com/hsiddhu2/reseat/tree/main/docs/proof/): checks run against the real API.
- [Demo scripts](https://github.com/hsiddhu2/reseat/tree/main/demo/): demos against the fake API, no sign-in. `reseat serve --demo` is the web app version.
- [Click-through demo](https://hsiddhu2.github.io/reseat/): the web app on demo data, rebuilt from `scripts/build_site.py` on every push to main.

## Release history

The main releases. Every release is in the [changelog](https://github.com/hsiddhu2/reseat/blob/main/CHANGELOG.md). Dates are UTC. Each line gives the PyPI version, then the commit. [PyPI's release history](https://pypi.org/project/reseat/#history) shows when each version was published.

- **0.5.1, 10 October:** the first release on PyPI. The watcher, which books a freed seat or a new repeat sitting of a target. The safe swap, which cancels a held seat only after a fresh read shows the new seat open and a way back to that talk, your own seat or another open sitting of it, and books yours again if the new one is refused. Leave-now blocks in the official schedule. Committed 2 October, [ee7655a](https://github.com/hsiddhu2/reseat/commit/ee7655a). The MCP server for your AI app. It writes only by approving a plan id it proposed, never from a list of session ids. Committed 2 October, [2e85cac](https://github.com/hsiddhu2/reseat/commit/2e85cac).
- **0.5.2, 11 October:** Swap now and Keep on the phone notification. [37bf396](https://github.com/hsiddhu2/reseat/commit/37bf396).
- **0.7.0, 19 October:** walk-up odds with a range, and leave times with a chosen confidence, committed 13 October, [636e665](https://github.com/hsiddhu2/reseat/commit/636e665). Planning tools for your AI app, with no ranking, committed 15 October, [7dc6421](https://github.com/hsiddhu2/reseat/commit/7dc6421).
- **0.8.0, 26 October:** the Findings page, Book offers on a tap, and email and text alerts through your own Amazon SNS topic. Committed 25 October, [86e5740](https://github.com/hsiddhu2/reseat/commit/86e5740).
- **0.8.1, 31 October:** the MCP server re-reads the rules file before each call. On site, polling leaves GetSession quota for the read before a swap. The test fixture is a sample, not the whole catalog. Every change is in the [changelog](https://github.com/hsiddhu2/reseat/blob/main/CHANGELOG.md).

## License

MIT
