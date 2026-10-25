# Autopilot books two ranked targets on its own, read back and emailed

- **Date and time:** 2026-10-25, 01:10 PDT
- **re:Seat version:** `f2d2883`
- **Where:** the attendee's laptop, `reseat serve --tailscale`, with push and email alerts on

## What was checked

The first booking re:Seat made on its own, for sessions the attendee will attend. The attendee added five ranked targets at the end of the rules file, below the 24 already there, and gave the three full ones `auto_swap: true`. Then they restarted `reseat serve`. Nothing else was typed or tapped.

## The rules the attendee set

Two time slots in the attendee's free time, each with targets in rank order. The attendee chose to show these codes, days, times and venues, an exception to the timetable rule in [the proof README](README.md). Their other held sessions stay hidden.

```
Mon 30 Nov 15:00, MGM Grand    rank 1  AIM353     builders' session  full  auto_swap: true
                               rank 2  CON338     code talk          full  auto_swap: true
                               rank 3  AIM433     chalk talk         open when added
Fri 4 Dec, morning, Encore     rank 1  CON301-R1  workshop 08:00     full  auto_swap: true
                               rank 2  INV510-R1  chalk talk 09:00   open when added
```

The dry run before the restart, sending nothing, listed exactly the two open sessions to reserve and every other target as full or walk-up only.

## What re:Seat did

On the first normal sweep after the restart, the catch-up pass booked the open targets, in rank order, in one call, and read the schedule back:

```
2026-10-25 01:10:53 PDT  ReserveSessions  AIM433, INV510-R1  success
  result:     successful 2, failed 0
  read-back:  AIM433 and INV510-R1 listed as reserved. 17 reserved in all, 25 favorites
  disagreements: none
```

The attendee received a push and an email saying "Seat booked" for each.

## Later the same day

The attendee added nine more full sessions to the two slots in two edits, each with `auto_swap: true`, ranked above the held seats. Each edit was picked up by the watcher running at the time, with no restart for it. Between the two edits, at about 13:00, the attendee restarted `reseat serve` under `caffeinate -is` with `--interval 30`. The journal rows, read-only, columns trimmed:

```
12:32:22 PDT  watcher.rules  reloaded  {"targets": 31}
13:11:37 PDT  watcher.rules  reloaded  {"targets": 38}
```

The first sweeps after the restart, from the local store's `sweeps` table, read-only: 13:00:16, 13:00:52, 13:01:29, 13:02:03 PDT, 34 to 37 seconds apart (the 30-second interval plus the sweep itself).

The two slots now hold twelve full sessions on autopilot above the two held seats. The attendee chose not to show the nine added later, so they appear as counts:

```
Mon 30 Nov 15:00, MGM Grand   AIM353, CON338, 4 more full sessions   above held AIM433
Fri 4 Dec, morning            CON301-R1, 5 more full sessions        above held INV510-R1
```

## Result

re:Seat booked two real sessions on its own, from the rules file alone, within its caps, and confirmed both from the official schedule. By the end of the day twelve full sessions were on autopilot, as listed above. If one on Monday frees, re:Seat swaps up from AIM433 on its own, and if one on Friday frees, from INV510-R1, after a fresh read of the new seat and a checked way back to the old one, and says what it did.

## What it does not prove

Both sessions were already open, so this shows the catch-up pass, not a seat caught the moment it freed. No autopilot swap has run live yet. One account, one booking call.
