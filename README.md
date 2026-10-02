# re:Seat

re:Seat keeps your AWS re:Invent seats. From the day API writes open until the last session ends, it watches for freed seats and newly added repeats, books them within your rules, upgrades held seats without ever losing one, and tells you when to leave and whether to queue.

Built on the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html). Runs on your own machine. Your tokens stay in your OS keychain.

**Status: week three.** Sign-in, catalog sync, the rules file, booking with fallback, favorites sync, the watcher, safe swap and leave-now blocks work today against the fake API. Reservation writes open through the API on 8 October 2026.

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
```

The first target wins a time slot. Within a batch, formats that are not recorded go first, because they fill first: Workshop, Lab and Bootcamp, then Builders' session, Chalk talk, Code talk, Breakout session, then the rest.

## Running it all week

re:Seat is designed for a laptop left in the hotel room, plugged in and awake, while you carry only your phone. Your sign-in can only happen on your own machine, so the laptop does every API call. Run `reseat watch` there. The phone page, `reseat serve`, is on the way and uses the same watcher.

**Keep the laptop awake with the lid closed.**

- macOS. `caffeinate -s reseat watch` keeps the Mac awake while it is plugged in. Closing the lid still puts most MacBooks to sleep unless an external display is attached. Either leave the lid open with the screen dimmed, or run `sudo pmset -a disablesleep 1` before you leave and `sudo pmset -a disablesleep 0` when you are back.
- Windows. Settings, System, Power and battery: when plugged in, sleep after Never. Control Panel, Power Options, Choose what closing the lid does: when plugged in, Do nothing.

**Reach it from the phone.** Install [Tailscale](https://tailscale.com) on the laptop and the phone and sign in to the same account on both. The phone can then reach the laptop by its Tailscale name from any venue, without exposing it to the hotel network.

**When the hotel Wi-Fi drops.** The watcher keeps running. It retries with a growing wait, up to 5 minutes between tries, and goes back to its normal pace as soon as a sweep works. A sweep that fails halfway is not saved, so a seat that opened during the outage is still caught afterwards. Each outage goes in the journal. Your reserved seats stay reserved, because they live in your AWS schedule, not on the laptop. While the laptop is offline it cannot book anything and the phone cannot reach it, so use the official app until it is back. If the AWS API is down but the internet is up, push tells you "re:Seat offline since HH:MM" after 10 minutes and "re:Seat back" when it recovers.

**When sign-in expires.** If the token can no longer be refreshed, re:Seat stops booking, keeps watching for the moment it can read again, and tells you once: "sign in needed". Run `reseat login` on the laptop. Booking resumes on the next sweep.

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

Tests run against an in-process fake of the API that produces every documented failure: `sessionFull`, `scheduleConflict` with `conflictsWith`, `alreadyScheduled`, 409 while writes are closed, 429 with `Retry-After`, and partial batch results. No network needed.

`tests/fixtures/catalog-2026-10-01.json` is a real catalog pull (no abstracts, no speaker names). `FakeEventsApi.from_fixture(path)` serves it, so tests see the real venue, room and type strings.

## Docs

- [How re:Seat works](docs/architecture.md): the problem, the design, each part marked built or planned.
- [AWS Events API facts](docs/api-facts.md): the API behaviour re:Seat relies on, with sources.

## Status

- Built: sign-in, catalog sync and change detection, rules file, order router with fallback, cancel, favorites sync, watcher with new-repeat booking and swap proposals, safe swap, leave-now blocks and queue-or-go advice.
- Next: live booking when API writes open on 8 October 2026, then the phone remote: `reseat serve`, a page the phone opens while the laptop does the work.
- Later: local MCP server, hardening.

## License

MIT
