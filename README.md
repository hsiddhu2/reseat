# re:Seat

re:Seat keeps your AWS re:Invent seats after the plan is made. It watches the catalog all day, books a seat the moment one frees up or a new repeat sitting appears, swaps a held seat for a better one without losing either, and tells you when to leave and whether to queue. It runs on your laptop against the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html), and your phone is the remote.

## The problem

Planners help you pick sessions. Attendees say the trouble starts after that:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app. Most sessions will have no shows." (2022)
- "I was the 50th person on the walk-up line. They only accepted 20." (2023)

Seats free up, repeats get added, rooms move. Catching that means refreshing the app all week. re:Seat does the refreshing and acts on what it finds, within rules you set.

## Install in three commands

```bash
git clone https://github.com/hsiddhu2/reseat && cd reseat
pip install -e .
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
| Every day until the event | `reseat serve` sweeps the catalog every minute. A freed seat or a new repeat of a target is booked at once. A better sitting that clashes with a lower-priority hold becomes a swap proposal on your phone. Leave-now reminders go into your official schedule. |
| At re:Invent | The laptop stays in the hotel room. Your phone shows today's seats, a leave-now countdown and whether to queue. During the day the laptop checks your sessions every 20 seconds, so a no-show's seat can be booked while you stand in the walk-up line. |

## Safe swap

The API has no swap. To move from held session A to a better B at the same time, A must be cancelled before B can be reserved, and for that moment you hold neither. re:Seat only starts when B is open right now, A has a fallback (its own seat or another open sitting), and you approved or allowed auto-swap for that target. If B fails, it re-reserves A. If A is gone, it books A's fallback and tells you exactly what you hold. One swap at a time, every step journaled.

## The phone remote

Sign-in only works on your own machine, so the token stays on the laptop and the laptop makes every call. `reseat serve` also serves a small page your phone opens over [Tailscale](https://tailscale.com): held sessions, the next leave time, queue-or-go advice, and Approve or Skip for a proposed swap. Optional push through ntfy. Designed for a laptop left in the hotel room. See [Running it all week](#running-it-all-week).

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

re:Seat's sync read the empty answer as every session removed and emptied its local copy of the catalog. It sent no write, and nothing on the attendee's AWS schedule changed. Three fixes followed, each tested against the fake: a sweep that comes back empty, or would drop more than half the catalog without bringing back one of about the same size, is refused and the local copy kept, a sweep of about the same size whose ids are mostly new is recorded as a new baseline, and nothing is booked while a held session is missing from the local catalog. The record, with commands and output, is in [docs/proof/2026-10-08-catalog-empty.md](docs/proof/2026-10-08-catalog-empty.md). It shows nothing about booking through the API.

## Limits

- It manages seats you already chose. It does not recommend sessions or solve your schedule.
- Queue-or-go advice is a rule by session type, improved by seat band history once there is some. It is a heuristic, never a prediction.
- Walking times between venues are conservative estimates, not official figures. Wynn and Encore come from the API as one venue and are split by room name.
- The phone page is plain HTTP. Use it over Tailscale, not open hotel Wi-Fi. It is access control, not a security product.
- It cannot help with the first rush when reserved seating opens in the portal, two days before the API opens.
- The MCP server tells an agent to ask before approving a change, but cannot check that it did.

## How it is tested

Every write path runs against an in-process fake of the API that produces each documented failure: partial bulk results, `sessionFull`, `scheduleConflict`, 409, 429 with `Retry-After`, 5xx, dropped connections and expired sign-in. A fault storm runs the whole flow for an hour on the real 2026 catalog with 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, and the real quotas enforced, and checks that no quota is exceeded, no talk is held twice and every write is read back. Details in [How re:Seat works](docs/architecture.md).

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
reseat serve                       # watcher plus the phone page. See "Running it all week"
reseat mcp                         # local MCP server on stdio. Needs pip install -e ".[mcp]"
reseat logout
```

Data lives in `~/.reseat/reseat.db`. Set `RESEAT_HOME` to move it.

### Rules file

`~/.reseat/rules.yaml` lists targets in priority order. re:Seat reserves nothing else.

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
watch_cap: 25
home_venue: Venetian      # where the first walk of each day starts
serve_secret: <long random string>   # needed for the phone page off this laptop
ntfy_topic: <random 16 to 64 characters>   # optional push
probe_session: <id of a session that takes no reservations>   # resume the moment writes open
```

The first target wins a time slot. Within a batch, formats that are not recorded go first, because they fill first: Workshop, Lab and Bootcamp, then Builders' session, Chalk talk, Code talk, Breakout session, then the rest.

### Running it all week

re:Seat is designed for a laptop left in the hotel room, plugged in and awake, while you carry only your phone. Your sign-in can only happen on your own machine, so the laptop does every API call and the phone only talks to the laptop. Run `reseat serve` there. It runs the watcher and serves a phone page with today's held sessions, a leave-now countdown, queue-or-go advice for the next session you want, and Approve and Skip for proposed swaps.

**Set up the phone page.** Add a long random `serve_secret` to the rules file, then start it on the laptop:

```bash
reseat serve --host 0.0.0.0
```

It prints a one-time link. Open it on the phone once. It sets a cookie and the address bar is left clean. If the cookie is lost, open `/login` on the same address and type the secret. Restarting `reseat serve` signs every phone out, and a sign-in lasts 7 days. The page is plain HTTP, so open it over Tailscale, which encrypts the connection, not across open hotel Wi-Fi. Without `serve_secret`, `reseat serve` listens on 127.0.0.1 only and refuses any other address. The page is access control for a hotel network, not a security product: it stops a neighbour on the same Wi-Fi from approving your swaps. Approve and Skip take a plan id that expires after 10 minutes. No page or endpoint takes a session id.

**Keep the laptop awake with the lid closed.**

- macOS. `caffeinate -s reseat serve --host 0.0.0.0` keeps the Mac awake while it is plugged in. Closing the lid still puts most MacBooks to sleep unless an external display is attached. Either leave the lid open with the screen dimmed, or run `sudo pmset -a disablesleep 1` before you leave and `sudo pmset -a disablesleep 0` when you are back.
- Windows. Settings, System, Power and battery: when plugged in, sleep after Never. Control Panel, Power Options, Choose what closing the lid does: when plugged in, Do nothing.

**Reach it from the phone.** Install [Tailscale](https://tailscale.com) on the laptop and the phone and sign in to the same account on both. Open the printed link with the laptop's Tailscale name in place of its local name, for example `http://my-laptop:8490/open?k=...`. The phone then reaches the laptop from any venue. To keep the page off the hotel network entirely, use `--host` with the laptop's Tailscale address instead of `0.0.0.0`.

**Push, if you want it.** Set `ntfy_topic` in the rules file to a long random name, install the ntfy app on the phone and subscribe to that topic. The laptop posts to `https://ntfy.sh/<topic>` when a seat is booked, a swap is proposed, done or rolled back, it is time to leave, the API has been unreachable for 10 minutes, it is back, or sign-in is needed. What leaves the laptop: the event type, session codes and titles, and those status lines. Never your token, an abstract, a session id or a plan id. Push is off unless the topic is set.

**When writes are switched off.** A 409 means the API has reservation writes turned off, and retrying will not help until they are back. re:Seat keeps watching and keeps every opening queued, but sends no reserve and runs no swap. It tries once after 15 minutes. With `probe_session` set to a session that takes no reservations, it checks every minute instead and resumes the moment writes open. Push says "Booking paused" and "Booking resumed".

**On site.** During the event days the laptop also checks the day's held and wanted sessions every 20 seconds, at most 40 of them, so a seat freed by a no-show is booked while you are in the walk-up line.

**When the hotel Wi-Fi drops.** The watcher keeps running. It retries with a growing wait, up to 5 minutes between tries, and goes back to its normal pace as soon as a sweep works. A sweep that fails halfway is not saved, so a seat that opened during the outage is still caught afterwards. Each outage goes in the journal. Your reserved seats stay reserved, because they live in your AWS schedule, not on the laptop. While the laptop is offline it cannot book anything and the phone cannot reach it, so use the official app until it is back. If the AWS API is down but the internet is up, push tells you "re:Seat offline since HH:MM" after 10 minutes and "re:Seat back" when it recovers.

**When sign-in expires.** If the token can no longer be refreshed, re:Seat stops booking, keeps watching for the moment it can read again, and tells you once: "sign in needed". Run `reseat login` on the laptop. Booking resumes on the next sweep.

### MCP server

`reseat mcp` runs a local MCP server over stdio, so an agent can read your targets and plan changes for you. Install the extra first: `pip install -e ".[mcp]"`. A client config looks like this, with the path to your `reseat` command:

```json
{
  "mcpServers": {
    "reseat": { "command": "/path/to/.venv/bin/reseat", "args": ["mcp"] }
  }
}
```

Tools: `list_targets`, `propose_changes`, `approve_changes`, `explain_drift`, `guard_sync`, `queue_or_go`. Every change is two steps. `propose_changes` or `guard_sync` returns a plan id and sends nothing. `approve_changes` carries that plan out once, within 10 minutes, and reserves only what it named. A leave-now plan is refused if what you hold changed after it was shown. Calls run one at a time. No tool takes a list of session ids.

Know the limit: the server tells the agent to ask you before approving, but it cannot check that it did. The agent sees the plan id and could approve on its own. Session titles come from the catalog and reach the agent. Use a client that shows you each tool call before it runs.

### Good citizen rules

re:Seat only reserves what you asked for. It never holds two sittings of one talk. Watch lists are capped. It does not scrape or redistribute the catalog. Seats it frees during a swap go back to the pool at once.

### Development

```bash
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
- [Demo scripts](demo/): recordable demos against the fake API.

## License

MIT
