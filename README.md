# re:Seat

re:Seat keeps your AWS re:Invent seats after the plan is made. It watches the catalog all day, books a seat the moment one frees up or a new repeat sitting appears, swaps a held seat for a better one without losing either, and tells you when to leave and whether to queue. It runs on your laptop against the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html), and it can ask you on your phone, as a notification you approve with one tap.

![re:Seat demo: a seat opens and a swap is proposed with Swap now and Keep, the week labels what changed, the swap is approved and verified, and Today shows when to leave](https://raw.githubusercontent.com/hsiddhu2/reseat/main/docs/demo.gif)

**Read the story:** [re:Seat: the seats you missed, booked while you sleep](https://builder.aws.com/project/3KZhHk8a0XfKPUaIdZUTKjSLoZ4/reseat-the-seats-you-missed-booked-while-you-sleep) on AWS Builder Center.

**Try it without installing anything:** [the click-through demo](https://hsiddhu2.github.io/reseat/) is the web app on demo data, built from this code. **Run it live in two minutes:** `pip install reseat` then `reseat serve --demo`. See [Try it in two minutes](#try-it-in-two-minutes).

**What re:Seat does:**

- **Books what frees up.** Every minute it checks the catalog. A freed seat or a new repeat sitting of a talk you want is booked at once, inside your rules and the API's quota, and read back from your schedule.
- **Swaps without losing a seat.** When a better sitting opens at the same time as one you hold, it asks you: on the Week page, and with `ntfy_approve: true` as a notification with Swap now and Keep. Swap now cancels only after a fresh read shows the new seat open and a fallback for yours, and books yours again if the new one is refused.
- **Tells you when to leave.** Leave-now blocks in your official schedule, a morning briefing, leave in 10 minutes, and whether to queue for a walk-up seat. Walking times are labelled as estimates.
- **Keeps your token on your laptop.** Nothing is hosted. The token goes only to `api.awsevents.com`.

**At a glance.** Each line links to its evidence. The research each decision rests on is in [The models behind re:Seat](docs/models.md).

- **Never sends a booking twice.** Reserve and cancel are never retried. A timeout or a server error is settled by reading your schedule back. Tests: [5xx on a booking](tests/test_router.py#L243), [5xx on a cancel](tests/test_cancel_and_cli.py#L41), [a timeout mid-swap](tests/test_swap.py#L318).
- **Every write is read back** from your official schedule. Tests: [after a booking](tests/test_router.py#L155), [through a whole fault storm](tests/test_fault_storm.py#L149).
- **Tested under a fault storm:** an hour on the real 2026 catalog, with 30 percent of bookings full, a 429 every minute, a server error, fifteen minutes of writes switched off, and the API's quotas enforced. [The test](tests/test_fault_storm.py).
- **698 tests**, run on Linux, macOS and Windows with Python 3.11 and 3.13 on every push. [CI](https://github.com/hsiddhu2/reseat/actions/workflows/ci.yml).
- **The 2026 campus:** Caesars Forum, Caesars Palace, Encore, MGM Grand, The Venetian, Wynn. [The venue table](src/reseat/campus.py#L18).
- **Seen live:** on 8 October the API served an empty catalog. re:Seat refused to act on it, and a check is logged every day since. [The record](docs/proof/2026-10-08-catalog-empty.md), [the test](tests/test_sweep_guards.py#L33).
- **Try it in two minutes, no sign-in:** `pip install reseat`, then `reseat serve --demo`. [PyPI](https://pypi.org/project/reseat/), [click-through demo](https://hsiddhu2.github.io/reseat/).

## The problem

Planners help you pick sessions. Attendees say the trouble starts after that:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app. Most sessions will have no shows." (2022)
- "I was the 50th person on the walk-up line. They only accepted 20." (2023)

Seats free up, repeats get added, rooms move. Catching that means refreshing the app all week. re:Seat does the refreshing and acts on what it finds, within rules you set.

## Try it in two minutes

```bash
pip install reseat         # or: pipx install reseat
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

Python 3.11 or newer. `login` opens your AWS Builder ID sign-in. Use the Builder ID your re:Invent registration is under: `reseat whoami` should then say `Registered for reinvent2026`. The tokens stay in your OS keychain and are sent only to `api.awsevents.com`. Then point re:Seat at the sessions you want:

```bash
reseat sync                                  # the catalog, about 9 calls
reseat rules from-schedule                   # your reserved sessions and favorites from the AWS portal
reseat favorites sync                        # mirror them into the official app
reseat book --dry-run                        # see the plan. Nothing is sent
```

## Your week with re:Seat

| When | What re:Seat does |
|---|---|
| Before writes open | Takes your targets from the schedule you built in the AWS portal, or from a [reinvent-planner.cloud](https://reinvent-planner.cloud) export, and mirrors them into your favorites. |
| The day API writes open | `reseat book` reserves your targets, scarcest formats first and then in your order, 10 per call, inside the quota. A full session falls back to its next sitting in the same run. Every write is read back. |
| Every day until the event | `reseat serve` sweeps the catalog every minute. A freed seat or a new repeat of a target is booked at once. A better sitting that clashes with a lower-priority hold becomes a swap proposal: a card on the Week page, and a push, with Swap now and Keep if you opt in. Leave-now reminders go into your official schedule. |
| At re:Invent | The laptop stays in the hotel room. Your phone gets the morning briefing, leave in 10 minutes and leave now, and a heads-up when a talk you want starts soon, with whether to queue. During the day the laptop checks your sessions every 20 seconds, so a no-show's seat can be booked while you stand in the walk-up line. |

## Safe swap

The API has no swap. To move from held session A to a better B at the same time, A must be cancelled before B can be reserved, and for that moment you hold neither. re:Seat cancels A only when a fresh read shows B open, B clashes with nothing else you hold, A has a fallback (its own open seat or another open sitting of A, read fresh), and you approved or allowed auto-swap for that target. If the cancel fails and A is still held, it stops. If B fails, it re-reserves A. If A is gone too, it tries each fallback once and tells you exactly what you hold. If a read-back fails, or writes close mid-swap, it stops and says so. One swap at a time, every step journaled.

## The phone remote

Sign-in only works on your own machine, so the token stays on the laptop and the laptop makes every call. The phone gets notifications through the free ntfy app. With `ntfy_approve: true`, a swap proposal carries **Swap now** and **Keep** buttons: the tap goes back through ntfy.sh to the laptop, which only makes outgoing connections, so the phone never has to reach it. The topic name is the secret: see [what a leaked topic allows](#running-it-all-week). Opening the web app itself on the phone is optional, over [Tailscale](https://tailscale.com). Designed for a laptop left in the hotel room.

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
| There is no search | Pulls the whole catalog, about 9 calls, and works locally. |
| Personal time is UTC in 5-minute steps | Writes leave-now blocks exactly that way. |

## Seen live

On 8 October 2026, between 20:19 and 20:28 PDT, on the day writes were scheduled to open, the Events API answered GetSchedule but served no sessions:

```
ListSessions                -> 200, totalCount 0, items 0
GetSession <held session>   -> 404 "No session was found with the requested id"
GetSchedule                 -> 200, reserved 14, favorites 16
```

re:Seat's sync read the empty answer as every session removed and emptied its local copy of the catalog. It sent no write, and nothing on the attendee's AWS schedule changed. Three fixes followed, each tested against the fake: a sweep that comes back empty, or would drop more than half the catalog without bringing back one of about the same size, is refused and the local copy kept, a sweep of about the same size whose ids are mostly new is recorded as a new baseline, and nothing is booked while a held session is missing from the local catalog. The record, with commands and output, is in [docs/proof/2026-10-08-catalog-empty.md](docs/proof/2026-10-08-catalog-empty.md), with one line for each daily check since. The catalog was still empty at the last check, on 11 October. It shows nothing about booking through the API.

## Limits

- It manages seats you already chose. It does not recommend sessions or solve your schedule.
- Queue-or-go advice is odds with a range: the rule for the session type, updated each time re:Seat sees the session fill and free a seat. It is a heuristic, never a prediction, and the range is wide until there is data. See [the models](docs/models.md).
- Walking times between venues are conservative estimates, not official figures, and every screen, push and leave-now block says so: "about 60 min (estimate)". AWS publishes no walking or shuttle times, so re:Seat adds none of its own. Wynn and Encore come from the API as one venue and are split by room name.
- The web app is plain HTTP. Use it over Tailscale, not open hotel Wi-Fi. It is access control, not a security product.
- It cannot help with the first rush when reserved seating opens in the portal, two days before the API opens.
- The MCP server tells an agent to ask before approving a change, but cannot check that it did.

## How it is tested

Every write path runs against an in-process fake of the API that produces each documented failure: partial bulk results, `sessionFull`, `scheduleConflict`, 409, 429 with `Retry-After`, 5xx, dropped connections and expired sign-in. A fault storm runs the whole flow for an hour on the real 2026 catalog with 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, and the real quotas enforced, and checks that no quota is exceeded, no talk is held twice and every write is read back. 698 tests. CI runs them on Linux, macOS and Windows, on Python 3.11 and 3.13. Details in [How re:Seat works](docs/architecture.md).

## Reference

### Use

```bash
reseat events                      # list events, no sign-in needed
reseat sync                        # pull the whole catalog, about 9 API calls
reseat sync --no-abstracts         # cheap sweep, reports band changes and new sessions
reseat sync --force                # apply even an empty or much smaller catalog. Exit 4 means a sweep was refused
reseat search "serverless"
reseat show <sessionId>            # details, repeats, seat band history
reseat schedule                    # your reserved, favorites, personal time
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
reseat favorites sync              # mirror every target sitting into favorites. Works before 8 October
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

`~/.reseat/rules.yaml` lists targets in priority order. re:Seat reserves nothing else. re:Seat writes it readable by you only, because it can hold `serve_secret`, and refuses to write through a symbolic link. If you made the file yourself and others on the machine can read a secret in it, re:Seat says so and gives the `chmod` to run.

If you built your schedule in the AWS portal, `reseat rules from-schedule` writes the file for you. Your reserved sessions come first, then your favorites, in the order the API lists them. The API does not promise that order, so reorder the targets to set your priority. Each target is the exact sitting you picked, by session id with `repeats: false`, so the file can be written even while the catalog is empty. `favorites sync` and `book` still need `reseat sync` first. Every reserved session is listed. A favorite past `watch_cap` is written as a comment, not dropped. The API treats a favorite as interest only, not a reservation. This command turns your favorites into booking targets on purpose, and says how many, so delete any you only want to keep an eye on. When an exact sitting is full and the talk has other sittings, `book` says so in one line. Nothing moves to another sitting unless you set `repeats: true` on that target. Other settings are the `rules init` defaults, with its example lunch left as a comment. Without `--force` it never replaces an existing file. With `--force` the old file is kept as `rules.yaml.bak`, because its settings, `serve_secret` included, are reset.

```yaml
targets:
  - code: ARC301          # any sitting of ARC301. Earliest first unless prefer: latest
    backups: [ARC302]     # tried if no sitting of ARC301 can be held
  - session_id: 1780442277219001cKCh
    repeats: false        # this sitting only
meals:
  - {day: Tuesday, start: "12:00", end: "13:00"}
max_per_day: 5
arrive_confidence: 0.8      # leave early enough to finish the estimated walk in time this often (0.5 to 0.99)
watch_cap: 25
home_venue: Venetian      # where the first walk of each day starts
serve_secret: <long random string>   # needed for the web app off this laptop
ntfy_topic: <random 16 to 64 characters>   # optional push
probe_session: <id of a session that takes no reservations>   # resume the moment writes open
```

The first target wins a time slot. Within a batch, formats that are not recorded go first, because they fill first: Workshop, Lab and Bootcamp, then Builders' session, Chalk talk, Code talk, Breakout session, then the rest.

### Running it all week

re:Seat is designed for a laptop left in the hotel room, plugged in and awake, while you carry only your phone. Your sign-in can only happen on your own machine, so the laptop makes every API call. The phone gets notifications through the ntfy app and approves a swap from the notification. Opening the web app on the phone is optional, over Tailscale.

**Keep the laptop awake with the lid closed.** Start it with `reseat serve`.

- macOS. `caffeinate -s reseat serve` keeps the Mac awake while it is plugged in. Closing the lid still puts most MacBooks to sleep unless an external display is attached. Either leave the lid open with the screen dimmed, or run `sudo pmset -a disablesleep 1` before you leave and `sudo pmset -a disablesleep 0` when you are back.
- Windows. Settings, System, Power and battery: when plugged in, sleep after Never. Control Panel, Power Options, Choose what closing the lid does: when plugged in, Do nothing.

**Set up the phone: notifications.** Set `ntfy_topic` in the rules file to a long random name, install the ntfy app on the phone and subscribe to that topic. Push is off unless the topic is set. The laptop posts to `https://ntfy.sh/<topic>`, each kind once:

- **Swaps:** a swap proposed, 2 minutes from lapsing, done, rolled back or failed.
- **Bookings:** a seat booked. A seat opened or a new sitting appeared but your rules stopped it, with the reason. The only sitting your rules allow is full while another has seats. A booking the read-back could not confirm. The catalog back after the API served none.
- **Schedule:** a session you hold moved or left the catalog. Two held sessions too far apart to walk between in the gap. The leave-now blocks re:Seat wrote no longer match what you hold, with, under `ntfy_approve: true`, an **Update** button that rewrites them and reads the schedule back (only if you use the blocks).
- **On site:** a morning briefing between 06:00 and 10:00 on each conference day, leave in 10 minutes, leave now, and a talk you want but hold no seat for starting in 20 minutes, with whether to queue.
- **Health:** writes paused or resumed, the API unreachable for 10 minutes or back, sign-in needed.

Each push carries the event, session codes, titles and those status lines, never your token, an abstract or a session id. When `reseat serve` listens on an address other than 127.0.0.1, a tap opens the right page there. Use your Tailscale address: it works only inside your own Tailscale network.

**Choose what reaches the phone.** Under `notify:` in the rules file, `quiet: "22:00-07:00"` (Las Vegas time) holds pushes back and sends one summary when the quiet hours end, and `mute: [on_site]` drops a category: swaps, bookings, schedule, on_site, health. Sign in needed and a swap that failed always come through.

**Approve from the notification.** Add `ntfy_approve: true` and a swap proposal, and its 2-minute reminder, arrive with **Swap now** and **Keep** buttons, and out-of-date leave-now blocks with **Update**. Long-press the notification to see them. The tap goes back through ntfy.sh to a second topic derived from yours, and re:Seat reads it over an outgoing connection, so the phone never has to reach the laptop: no Tailscale, no link, no sign-in. It runs the same checked swap as the web app, and a second push tells you the result. The button carries the proposal's plan id, which is random, works once and expires after 10 minutes, so tapping twice runs the swap once. To try it first on demo data: `reseat serve --demo --push <your topic>`.

**Your topic name is the secret.** ntfy.sh topics have no password: anyone who knows the name can read what is posted to it and post to it. So treat `ntfy_topic` like a password. Make it long and random, for example `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`, never reuse it, and do not put it in a screenshot. re:Seat accepts only 16 to 64 letters, digits, `-` and `_`, and on macOS and Linux warns when a rules file holding the topic can be read by other users on the machine.

If the name leaks, this is what someone else could do, and what limits it:

- **Read your pushes.** They hold session codes, titles, times, rooms, venues and status lines. When the web app is reachable from your phone, they also hold its address on your Tailscale network, and with `ntfy_approve` each button's single-use id. Never your token, an abstract, a session id, or anything that signs in to AWS. Knowing the topic gives no access to your schedule or your account.
- **Post to your topic.** A fake notification could appear on your phone. re:Seat never acts on what is posted there. Check anything odd in the web app or the official app.
- **With `ntfy_approve: true`, tap your buttons.** They could approve or dismiss a swap that is waiting for you, or run Update on your leave-now blocks. That is all. A button only works on a proposal re:Seat made from your rules, while it waits: its plan id is random, works once and expires after 10 minutes. Swap now runs the same checked swap as the web app: a fresh read of the new seat, a fallback for yours, and your seat booked again if the new one is refused. Update only adds, changes or removes the leave-now blocks re:Seat wrote. Nobody can use the topic to book, cancel or favorite anything else.
- **What to do.** Put a new random name in `ntfy_topic`, subscribe to it in the ntfy app, unsubscribe from the old one, and restart `reseat serve`. re:Seat no longer posts to or listens on the old name. ntfy.sh keeps past messages for a while, so pushes already sent stay readable on the old name until they expire there. To keep push but drop the buttons, remove `ntfy_approve`.

**When writes are switched off.** A 409 means the API has reservation writes turned off, and retrying will not help until they are back. re:Seat keeps watching and keeps every opening queued, but sends no reserve and runs no swap. It tries once after 15 minutes. With `probe_session` set to a session that takes no reservations, it checks every minute instead and resumes the moment writes open. Push says "Booking paused" and "Booking resumed".

**On site.** During the event days the laptop also checks the day's held and wanted sessions every 20 seconds, at most 40 of them, so a seat freed by a no-show is booked while you are in the walk-up line.

**When the hotel Wi-Fi drops.** The watcher keeps running. It retries with a growing wait, up to 5 minutes between tries, and goes back to its normal pace as soon as a sweep works. A sweep that fails halfway is not saved, so a seat that opened during the outage is still caught afterwards. Each outage goes in the journal. Your reserved seats stay reserved, because they live in your AWS schedule, not on the laptop. While the laptop is offline it cannot book anything and the phone cannot reach it, so use the official app until it is back. If the AWS API is down but the internet is up, push tells you "re:Seat offline since HH:MM" after 10 minutes and "re:Seat back" when it recovers.

**When sign-in expires.** If the token can no longer be refreshed, re:Seat stops booking, keeps watching for the moment it can read again, and tells you once: "sign in needed". Run `reseat login` on the laptop. Booking resumes on the next sweep.

#### Optional: the web app on the phone

**Set it up.** Install [Tailscale](https://tailscale.com) on the laptop and the phone and sign in to the same account on both. Add a long random `serve_secret` to the rules file, then start it on the laptop:

```bash
reseat serve --tailscale
```

re:Seat listens on 127.0.0.1 only and asks Tailscale to forward `http://<your laptop's Tailscale name>:8490` to it, so the page is reachable from your own Tailscale devices and nowhere else. Stopping `reseat serve` removes the forwarding. This also avoids macOS's Local Network rules, which can drop connections from other devices to a program they do not recognise. `--host <Tailscale address>` still works where those rules allow it.

It prints a one-time link. Open it once on each device. It sets a cookie and the address bar is left clean. If the cookie is lost, open `/login` on the same address and type the secret. A sign-in lasts 7 days. To sign every device out, stop and restart `reseat serve`: sign-ins live only in the running process. The page is plain HTTP, so open it over Tailscale, which encrypts the connection, not across open hotel Wi-Fi. Without `serve_secret`, `reseat serve` listens on 127.0.0.1 only and refuses any other address. It never listens on every interface: `--host 0.0.0.0` is refused. The page is access control for a hotel network, not a security product: it stops a neighbour on the same Wi-Fi from approving your swaps. Swap now and Dismiss take a plan id that expires after 10 minutes. No page or endpoint takes a session id.

**Reach it from the phone.** With `--tailscale`, open the printed link on the phone. The phone then reaches the laptop from any venue, and the page is not offered on the hotel network.

**If the phone cannot open it.** `reseat serve` checks it can reach its own address when it starts. On macOS, a terminal app without Local Network permission accepts connections and then drops them. Allow the app you run reseat in (Terminal, iTerm or VS Code) under System Settings, Privacy & Security, Local Network, then quit and reopen it. Also check Tailscale is switched on on both devices.

**Put it on the home screen.** On the iPhone, open the web app in Safari, tap Share, then Add to Home Screen. re:Seat then opens full screen with its own icon, on Approve. If it asks you to sign in, open `/login` and enter `serve_secret`.

### MCP server

`reseat mcp` runs a local MCP server over stdio, so an agent can read your targets and plan changes for you. Install the extra first: `pip install "reseat[mcp]"`. A client config looks like this, with the path to your `reseat` command:

```json
{
  "mcpServers": {
    "reseat": { "command": "/path/to/.venv/bin/reseat", "args": ["mcp"] }
  }
}
```

Tools: `list_targets`, `propose_changes`, `propose_swap`, `approve_changes`, `explain_drift`, `guard_sync`, `queue_or_go`. Every change is two steps. `propose_changes`, `propose_swap` or `guard_sync` returns a plan id and sends nothing. `propose_swap(held_code)` finds the open sitting of a held talk that your rules prefer to the one you hold, runs the swap's checks with fresh reads, and lists them. Approving it runs the same checked swap as `reseat swap`. `approve_changes` carries that plan out once, within 10 minutes, and reserves only what it named. A leave-now plan is refused if what you hold changed after it was shown. Calls run one at a time. No tool takes a list of session ids.

Try it without signing in: `reseat mcp --demo` serves the same tools on the demo's fake API, with a week of sessions held and a seat open, so a client can list targets, plan and approve leave-now blocks, and ask queue-or-go. Nothing reaches AWS.

Know the limit: the server tells the agent to ask you before approving, but it cannot check that it did. The agent sees the plan id and could approve on its own. Session titles come from the catalog and reach the agent. Use a client that shows you each tool call before it runs.

### Good citizen rules

re:Seat only reserves what you asked for. It never holds two sittings of one talk. Watch lists are capped. It does not scrape or redistribute the catalog. Seats it frees during a swap go back to the pool at once.

### Development

```bash
git clone https://github.com/hsiddhu2/reseat && cd reseat
pip install -e ".[dev]"
pytest
ruff check .
```

A fault storm runs the whole flow for an hour on the real catalog: 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, quotas enforced. Tests run against an in-process fake of the API that produces every documented failure: `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409 while writes are closed, 429 with `Retry-After`, and partial batch results. No network needed.

`tests/fixtures/catalog-2026-10-01.json` is a real catalog pull (no abstracts, no speaker names). `FakeEventsApi.from_fixture(path)` serves it, so tests see the real venue, room and type strings.

### Docs

- [How re:Seat works](docs/architecture.md): the design, each part marked built or planned, and how it is tested.
- [AWS Events API facts](docs/api-facts.md): the API behaviour re:Seat relies on, with sources.
- [Live proof](docs/proof/): checks run against the real API.
- [Demo scripts](demo/): demos against the fake API, no sign-in. `reseat serve --demo` is the web app version.
- [Click-through demo](https://hsiddhu2.github.io/reseat/): the web app on demo data, rebuilt from `scripts/build_site.py` on every push to main.

## License

MIT
