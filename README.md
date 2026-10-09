# re:Seat

re:Seat keeps your AWS re:Invent seats. From the day API writes open until the last session ends, it watches for freed seats and newly added repeats, books them within your rules, upgrades held seats without ever losing one, and tells you when to leave and whether to queue.

Built on the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html). Runs on your own machine. Your tokens stay in your OS keychain.

**Status: week four.** Sign-in, catalog sync, the rules file, booking with fallback, favorites sync, the watcher with outage handling, safe swap, leave-now blocks, the phone page and the MCP server work today against the fake API. Reservation writes open through the API on 8 October 2026.

## Why

Every planner stops when the plan is built. Attendees say the trouble starts after that:

- "Most things were full by 10:03am." (2022)
- "Some stuff that was full yesterday I was able to reserve right now." (2024)
- "The more popular sessions get 2-3x extra sessions once they get booked up." (2022)
- "Get in line and keep refreshing the app. Most sessions will have no shows." (2022)
- "I was the 50th person on the walk-up line. They only accepted 20." (2023)

re:Seat does that refreshing for you, and acts on it.

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/<you>/reseat
cd reseat
pip install -e .
```

## Sign in

```bash
reseat login
```

Your browser opens an AWS Builder ID sign-in. If it does not open, copy the printed URL. Use the Builder ID that your re:Invent registration is tied to. Then check:

```bash
reseat whoami
```

You should see `Registered for reinvent2026`. If you see `not registered`, your registration is under a different Builder ID.

## Use

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

## Rules file

`~/.reseat/rules.yaml` lists targets in priority order. re:Seat reserves nothing else.

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

## Running it all week

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

## MCP server

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

## What it respects

The API has rules and re:Seat follows them.

- Per-operation quotas per minute. Reserve and favorite count sessions, not requests. re:Seat keeps a token bucket per operation and waits rather than getting throttled.
- 429 carries `Retry-After`. re:Seat waits exactly that long, once.
- Reserve and favorite succeed per session. A 200 is not "done". re:Seat reads every `failed` entry and reads back your schedule after every write.
- Writes are not safe to blind-retry. re:Seat never does.
- Personal time is UTC to the minute in 5-minute steps. Sessions are in Las Vegas local time. re:Seat converts.

## Good citizen rules

re:Seat only reserves what you asked for. It never holds two sittings of one talk. Watch lists are capped. It does not scrape or redistribute the catalog. Seats it frees during a swap go back to the pool at once.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

A fault storm runs the whole flow for an hour on the real catalog: 30 percent of reserves full, a 429 every minute, a 503, fifteen minutes of 409, quotas enforced. Tests run against an in-process fake of the API that produces every documented failure: `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409 while writes are closed, 429 with `Retry-After`, and partial batch results. No network needed.

`tests/fixtures/catalog-2026-10-01.json` is a real catalog pull (no abstracts, no speaker names). `FakeEventsApi.from_fixture(path)` serves it, so tests see the real venue, room and type strings.

## Docs

- [How re:Seat works](docs/architecture.md): the problem, the design, each part marked built or planned.
- [AWS Events API facts](docs/api-facts.md): the API behaviour re:Seat relies on, with sources.

## Status

- Built: sign-in, catalog sync and change detection, rules file, order router with fallback, cancel, favorites sync, watcher with new-repeat booking, swap proposals and outage handling, safe swap, leave-now blocks, queue-or-go advice, the phone page with push, the local MCP server.
- Next: live booking when API writes open on 8 October 2026.
- Later: demo recordings, the write-up. Live checks against the real API are collected in [docs/proof](docs/proof/).

## License

MIT
