# A swap started by the watcher on its own, there and back

- **Date and time:** 2026-10-26, 13:43 to 13:45 PDT. The journal output below is in the laptop's local time, EDT, three hours ahead
- **re:Seat version:** the running watcher, `reseat serve --tailscale --interval 30`, started on 25 October at 17:57 PDT, when the last commit was `4292044`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The watcher's automatic swap on the real API, with no command run and no button pressed. A target marked `auto_swap: true` that has an open seat but overlaps a lower-ranked held target, with nothing else in the way, is swapped in: a fresh read of both seats and a checked way back to the old one, then cancel the held one, reserve the new one, read the schedule back. Done twice, there and back, so the attendee ended where they started. The same two sessions as the [hand-run swap on 25 October](2026-10-25-live-swap-round-trip.md):

- **INV510-R1**, Fri 4 Dec 09:00, Encore, chalk talk, held, open
- **INV508-R1**, Fri 4 Dec 09:00, Encore, chalk talk, open

## How it was started

No seat freed. The swap was started by editing the rules file while the watcher ran. The watcher reloads its rules on the next sweep.

First, INV508-R1 was added as a target ranked just above INV510-R1, with `auto_swap: true`:

```
  - {session_id: "<INV508-R1 id>", repeats: false, auto_swap: true}
  - {session_id: "<INV510-R1 id>", repeats: false}
```

Then the order was flipped, with `auto_swap: true` on INV510-R1 for the swap back:

```
  - {session_id: "<INV510-R1 id>", repeats: false, auto_swap: true}
  - {session_id: "<INV508-R1 id>", repeats: false, auto_swap: true}
```

Then the rules file was restored from a copy taken before the test.

Before the first edit, a read-only run of the watcher's own planning code against the live schedule predicted the first swap and nothing else. The attendee approved the test after seeing it.

## The journal

Read-only, every row in those minutes:

```
$ sqlite3 ~/.reseat/reseat.db "select datetime(ts,'unixepoch','localtime'),op,outcome from journal where ts > strftime('%s','now','-10 minutes') order by ts"
```

```
2026-10-26 16:43:32|watcher.rules|reloaded
2026-10-26 16:43:38|swap.proposed|state:proposed
2026-10-26 16:43:41|swap.checked|state:checked
2026-10-26 16:43:41|swap.cancel|before:checked
2026-10-26 16:43:42|swap.cancel|after:checked
2026-10-26 16:43:42|swap.cancelled|state:cancelled
2026-10-26 16:43:42|swap.reserve|before:cancelled
2026-10-26 16:43:44|swap.reserve|after:cancelled
2026-10-26 16:43:44|swap.reserved|state:reserved
2026-10-26 16:43:44|swap.verified|state:verified
2026-10-26 16:44:52|watcher.rules|reloaded
2026-10-26 16:44:57|swap.proposed|state:proposed
2026-10-26 16:45:00|swap.checked|state:checked
2026-10-26 16:45:00|swap.cancel|before:checked
2026-10-26 16:45:01|swap.cancel|after:checked
2026-10-26 16:45:01|swap.cancelled|state:cancelled
2026-10-26 16:45:01|swap.reserve|before:cancelled
2026-10-26 16:45:03|swap.reserve|after:cancelled
2026-10-26 16:45:03|swap.reserved|state:reserved
2026-10-26 16:45:03|swap.verified|state:verified
```

Which sessions each swap moved, from the same journal, read-only. The ids are written as codes here:

```
$ sqlite3 ~/.reseat/reseat.db "select datetime(ts,'unixepoch','localtime'),op,outcome,substr(request,1,300) from journal where op like 'swap.%' and date(ts,'unixepoch','localtime')='2026-10-26' order by ts"
2026-10-26 16:43:38|swap.proposed|state:proposed|{"held": "<INV510-R1 id>", "wanted": "<INV508-R1 id>"}
<7 rows between, each with the same held and wanted>
2026-10-26 16:43:44|swap.verified|state:verified|{"held": "<INV510-R1 id>", "wanted": "<INV508-R1 id>"}
2026-10-26 16:44:57|swap.proposed|state:proposed|{"held": "<INV508-R1 id>", "wanted": "<INV510-R1 id>"}
<7 rows between, each with the same held and wanted>
2026-10-26 16:45:03|swap.verified|state:verified|{"held": "<INV508-R1 id>", "wanted": "<INV510-R1 id>"}
```

## After

A dry run against the live schedule, which sends no write, with the restored rules file:

```
$ reseat book --dry-run --rules ~/.reseat/rules.yaml
Already held: <14 other held targets>, INV510-R1
<skipped targets left out>
Nothing to reserve.
```

## Result

The watcher ran both swaps on its own, each verified by a read of the official schedule. Each took 6 seconds from the proposal to the verified read-back, and 2 to 3 seconds from the cancel to the read-back after the reserve. The journal times are whole seconds. Between the cancel and the reserve the attendee held neither seat. Afterwards the dry run listed 15 held targets, INV510-R1 among them.

## What it does not prove

The swaps were started by a rules edit, not by a ranked target's seat freeing while the watcher ran. A seat caught as it frees has not yet been seen live. The rollback path, where the new seat is refused after the cancel, was not exercised live. It is covered by tests against the fake API. Whether a push or an email went out for these swaps was not checked.
