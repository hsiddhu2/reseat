# What the watcher saw, 12 to 30 October

- **Date and time:** 2026-10-30, 17:24 PDT, counting from the catalog's return on 2026-10-12 20:12 PDT
- **re:Seat version:** `7d6f326`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The watcher ran on the attendee's laptop whenever it was awake, against the live catalog. Every sweep stores each seat band change and each session added, removed or moved. These counts are read from that local store, read-only. Sessions are shown by type and band only. This picks up where [the first week's record](2026-10-20-watcher-first-week.md) and its 24 October update left off, and counts the whole window again.

The window runs from the sweep at 2026-10-12 20:12:46 PDT, the one that loaded the returned catalog, to the last sweep read, at 2026-10-30 17:24:22 PDT. That is 17 days and 21 hours. The store keeps unix seconds. Every time here is PDT.

## Command

```
$ reseat findings
2026-10-12 20:12 to 2026-10-30 17:22 Las Vegas time, 19 days, 17,524 sweeps.
Seats opened on full sessions: 41 on 32 sessions, on 16 days. Full again within 
10 minutes: 5. On targets: 3.
  Chalk talk: 21 openings on 16 sessions
  Workshop: 15 openings on 12 sessions
  Code talk: 3 openings on 2 sessions
  Gamified learning: 2 openings on 2 sessions
Sessions added: 218, 196 still listed. Removed 56, moves 84.
Full now: 1,440 of 1,483 that take reservations. Connection drops recovered: 44.
```

Then read-only counts from the `sweeps`, `band_history`, `catalog_changes`, `sessions` and `journal` tables of the local store, with the queries below.

## Sweeps

17,527 sweeps of the whole catalog in the window. Days ranged from 36 sweeps (12 October, the evening the catalog came back) to 2,401 (26 October). Stretches where sweeps came less than 5 minutes apart add up to about 232 hours, about 54 percent of the window. The laptop slept some nights, so this is not one sweep every minute for 18 days.

## Seats that opened on full sessions

An opening is a change from full (`unavailable`) to any open band.

```
full -> very limited    39
full -> limited          1
full -> available        1
total                   41, on 32 sessions, on 16 of the 19 days

openings by type             openings  sessions
  chalk talks                21        16
  workshops                  15        12
  code talks                  3         2
  gamified learning           2         2

full again                   openings
  within 10 minutes           5
  10 to 60 minutes            5
  1 to 6 hours               15
  over 6 hours               15
  still open                  1
```

The fastest was full again about one minute after it opened. That is the chalk talk at 23:35 PDT on 12 October from the 24 October update. The fifth under 10 minutes is new. A workshop went from full to very limited at 05:12 PDT on 30 October and was seen full again 9.8 minutes later. Each time is measured to the first sweep that saw the session full again, so times run long when the laptop was asleep.

11 of the 41 openings came after the 24 October update: 8 on workshops, 2 on gamified learning sessions and 1 on a chalk talk. The one still open is a workshop that went from full to limited on 27 October.

## Sessions added

218 sessions were added to the catalog after the load sweep, in 251 added rows, since some were removed and listed again. 196 of them are still listed: 89 featured experiences, 21 event services, 21 expo sessions, 20 breakouts, 17 chalk talks, 16 lightning talks, 5 keynotes, 4 workshops, 2 code talks and 1 builders' session. 156 of the 218 were first added after the 24 October update. No sweep in the window loaded or re-keyed the whole catalog, so none was left out as a baseline.

## Removed and moved

56 sessions were removed, in 65 removed rows. There were 84 moves, on 67 sessions.

## Seat bands now

```
unavailable  1440
veryLimited    20
limited        17
available       6
no band       930
total        2413
```

1,440 of the 1,483 sessions that take reservations are full, up from 1,374 of 1,479 on 24 October.

## Connection drops

The journal has 46 outage starts in the window, from 14 to 30 October. 44 have an end row, written when the watcher reached the API again on its own. The other 2 started at 11:14 and 12:56 PDT on 25 October. Sweeps resumed at 12:20 and 12:57 PDT, but no end row was written for either. No write was sent inside any of the 44 outages with an end row, or between either of the other 2 starts and the next sweep.

## Blind spells

A blind spell is a gap of more than 60 minutes between two sweeps. No sweep ran in it, so a seat that opened and filled again inside it was not seen. There were 44, adding up to about 168 hours. Start of the gap, PDT, and its length:

```
10-12 20:13  100 min     10-16 03:45   61 min     10-21 00:39  498 min
10-12 23:54  1326 min    10-16 04:46   61 min     10-21 18:29  100 min
10-14 06:04  130 min     10-16 22:25  118 min     10-21 21:13  398 min
10-14 08:13  111 min     10-17 00:23  278 min     10-23 00:05  234 min
10-14 16:40  126 min     10-17 05:45   80 min     10-23 03:59  138 min
10-15 06:05   61 min     10-17 15:27  107 min     10-23 20:58  570 min
10-15 07:13   77 min     10-17 17:29   91 min     10-25 01:34  258 min
10-15 08:30  159 min     10-17 22:16  817 min     10-25 06:44  189 min
10-15 11:10   61 min     10-18 23:43  661 min     10-25 10:01  139 min
10-15 12:11   61 min     10-19 10:45  355 min     10-27 03:25  752 min
10-15 13:12  122 min     10-19 18:00  237 min     10-29 15:22  116 min
10-15 15:14  122 min     10-19 23:50  498 min     10-29 17:18  139 min
10-15 17:16   61 min     10-20 15:59  208 min     10-30 06:27   85 min
10-15 18:17   71 min
10-15 22:54  108 min
10-16 00:42   61 min
10-16 01:43   61 min
10-16 02:44   61 min
```

The longest was 22 hours from 23:54 PDT on 12 October. The longest after the 24 October update was 12.5 hours from 03:25 PDT on 27 October.

## Target seats

No target seat was caught as it freed in this window.

`reseat findings` reports 3 sessions "on targets". It compares the openings with the target list in the rules file as it is today, backups included, not with the list as it was when each seat opened. Those 3 sessions had 6 openings between them, all between 13 and 19 October. The 24 October update recorded that none of the sessions that had opened by then was a target. None of the 3 has opened since 19 October.

The journal has these reserve rows in the window, and no others:

- 12 October, 22:32 and 22:40 PDT: the two manual round trips ([the record](2026-10-12-round-trip.md)).
- 25 October, 01:10 PDT: the autopilot booking of two targets that were open when added ([the record](2026-10-25-autopilot-first-booking.md)).
- 25 October 13:10 and 26 October 13:43 to 13:45 PDT: the swap round trips ([live swap](2026-10-25-live-swap-round-trip.md), [watcher swap](2026-10-26-watcher-swap.md)).
- 26 October, 18:19 PDT: the clash test, refused ([the record](2026-10-26-clash-refused.md)).

There is no reserve row after 26 October 18:19 PDT. The only write row after that is a writes-open probe at 15:42 PDT on 30 October, a cancel sent for a session the attendee never holds.

## Cross-check against `reseat findings`

Every count matches except the sweeps and the end time. `reseat findings` was run at 17:22 PDT and counted 17,524 sweeps. Three more sweeps ran before the last one read here, at 17:24:22 PDT. None of them changed a band or the catalog. `reseat findings` counts its 19 days as calendar days in Las Vegas time, 12 to 30 October inclusive.

## Queries

All read-only, with `sqlite3 -readonly ~/.reseat/reseat.db`. `1791861166.36` is the load sweep at 20:12:46 PDT on 12 October, and `1793406263` is just after the last sweep read. Times are shown in PDT with `datetime(ts-25200,'unixepoch')`.

```sql
-- sweeps in the window
SELECT COUNT(*) FROM sweeps WHERE ts BETWEEN 1791861166 AND 1793406263;

-- blind spells: gaps of more than 60 minutes
SELECT datetime(ts-25200,'unixepoch') AS start_pdt, ROUND((nxt-ts)/60) AS minutes
FROM (SELECT ts, LEAD(ts) OVER (ORDER BY ts) AS nxt FROM sweeps
      WHERE ts BETWEEN 1791861166 AND 1793406263)
WHERE nxt - ts > 3600;

-- openings on full sessions, and the sessions they were on
SELECT COUNT(*), COUNT(DISTINCT session_id) FROM band_history
WHERE ts BETWEEN 1791861166 AND 1793406263
  AND old_band = 'unavailable' AND new_band IS NOT 'unavailable';

-- openings by type and band
SELECT s.type, b.new_band, COUNT(*), COUNT(DISTINCT b.session_id)
FROM band_history b JOIN sessions s USING (event_id, session_id)
WHERE b.ts BETWEEN 1791861166 AND 1793406263
  AND b.old_band = 'unavailable' AND b.new_band IS NOT 'unavailable'
GROUP BY 1, 2;

-- added, removed and moved: rows and sessions
SELECT kind, COUNT(*), COUNT(DISTINCT session_id) FROM catalog_changes
WHERE ts > 1791861166.36 AND ts <= 1793406263 GROUP BY kind;

-- added and still listed, by type
SELECT type, COUNT(*) FROM sessions WHERE session_id IN
  (SELECT session_id FROM catalog_changes
   WHERE kind = 'added' AND ts > 1791861166.36 AND ts <= 1793406263)
GROUP BY type;

-- seat bands now
SELECT COALESCE(band, 'none'), COUNT(*) FROM sessions GROUP BY 1;

-- outages: start and end rows
SELECT outcome, COUNT(*) FROM journal
WHERE op = 'watcher.outage' AND ts BETWEEN 1791861166 AND 1793406263 GROUP BY outcome;

-- every reserve and cancel row, with its time
SELECT datetime(ts-25200,'unixepoch'), op, outcome FROM journal
WHERE op IN ('ReserveSessions', 'CancelReservation', 'swap.reserve', 'clashtest.reserve')
ORDER BY ts;
```

The time to refill was measured per opening, to the next row for the same session whose new band is `unavailable`, the same way `reseat findings` does it. The 3 sessions on today's target list were found by matching the opened sessions against the target sittings and backups the rules file names, on a copy of the store. Only their opening times and types were printed.

## Result

Seats keep opening on full sessions, on 16 of the 19 days, and some fill again within minutes. Sessions keep being added, 156 of them first added since 24 October. The watcher saw both, and recovered from 44 connection drops on its own. It caught no target seat as it freed in this window. No session on today's target list opened after 19 October, and the journal has no reserve outside the test rows listed above.

## What it does not prove

- That the watcher would catch a target seat as it frees. No session on today's target list opened while it was a target, as far as these counts show, so that path has not run live. The store does not keep earlier versions of the target list, so a session dropped from it since could have opened while it was still listed. The live swap and autopilot records show the booking side on seats that were already open.
- Anything inside the 44 blind spells, about 168 hours. A seat that opened and filled again inside one is not in these counts.
- Exact refill times. Each is measured to the next sweep that saw the session full, so it is an upper bound.
- How the 2 outages with no end row ended.
- Anything about other attendees, other accounts or other laptops. This is one account on one laptop that slept.
