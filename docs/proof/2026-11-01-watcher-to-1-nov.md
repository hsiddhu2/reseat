# What the watcher saw, 12 October to 1 November

- **Date and time:** 2026-11-01, 16:02 PST, counting from the catalog's return on 2026-10-12 20:12 PDT
- **re:Seat version:** `0cbaf96` (1.0.0)
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The watcher ran on the attendee's laptop whenever it was awake, against the live catalog. Every sweep stores each seat band change and each session added, removed or moved. These counts are read from that local store, read-only. Sessions are shown by type and band only. This picks up where [the record to 30 October](2026-10-30-watcher-to-30-oct.md) left off, and counts the whole window again.

The watcher ran the code of `e3b36b8` (0.8.2) until it was restarted on 1 November at about 14:56 PST on the 1.0.0 code. The commands below were run on 1.0.0.

The window runs from the sweep at 2026-10-12 20:12:46 PDT, the one that loaded the returned catalog, to the last sweep read, at 2026-11-01 16:02:47 PST. That is 19 days and 21 hours. The store keeps unix seconds. Daylight saving time ended at 02:00 local on 1 November. Times before then are PDT. Times after are PST.

## Command

```
$ reseat findings
2026-10-12 20:12 to 2026-11-01 16:02 Las Vegas time, 21 days, 21,274 sweeps.
Seats opened on full sessions: 42 on 33 sessions, on 16 days. Full again within 
10 minutes: 5. On targets: 3.
  Chalk talk: 21 openings on 16 sessions
  Workshop: 15 openings on 12 sessions
  Code talk: 3 openings on 2 sessions
  Gamified learning: 2 openings on 2 sessions
  Bootcamp: 1 opening on 1 session
Sessions added: 221, 199 still listed. Removed 56, moves 84.
Full now: 1,448 of 1,483 that take reservations. Connection drops recovered: 49.
```

Then read-only counts from the `sweeps`, `band_history`, `catalog_changes`, `sessions` and `journal` tables of the local store, with the queries below.

## Sweeps

21,274 sweeps of the whole catalog in the window. 3,747 of them ran after the record to 30 October. Days ranged from 36 sweeps (12 October, the evening the catalog came back) to 2,401 (26 October). 31 October had 2,012. 1 November had 1,081 up to 16:02 PST. Stretches where sweeps came less than 5 minutes apart add up to about 270 hours, about 57 percent of the window. The laptop slept some nights, so this is not one sweep every minute for 20 days.

## Seats that opened on full sessions

An opening is a change from full (`unavailable`) to any open band.

```
full -> very limited    40
full -> limited          1
full -> available        1
total                   42, on 33 sessions, on 16 of the 21 days

openings by type             openings  sessions
  chalk talks                21        16
  workshops                  15        12
  code talks                  3         2
  gamified learning           2         2
  bootcamps                   1         1

full again                   openings
  within 10 minutes           5
  10 to 60 minutes            5
  1 to 6 hours               16
  over 6 hours               15
  still open                  1
```

The fastest was full again about one minute after it opened. That is the chalk talk at 23:35 PDT on 12 October. Each time is measured to the first sweep that saw the session full again, so times run long when the laptop was asleep.

One opening is new since the record to 30 October. A bootcamp went from full to very limited at 18:45 PDT on 30 October and was seen full again 164 minutes later. It is the first bootcamp opening in the window. The one still open is the workshop that went from full to limited on 27 October. It was still open at the last sweep read.

## Sessions added

221 sessions were added to the catalog after the load sweep, in 254 added rows, since some were removed and listed again. 199 of them are still listed: 89 featured experiences, 24 expo sessions, 21 event services, 20 breakouts, 17 chalk talks, 16 lightning talks, 5 keynotes, 4 workshops, 2 code talks and 1 builders' session. 3 of the 221 are new since the record to 30 October. All 3 are expo sessions, first seen in the sweep at 12:50 PST on 1 November. No sweep in the window loaded or re-keyed the whole catalog, so none was left out as a baseline.

## Removed and moved

56 sessions were removed, in 65 removed rows. There were 84 moves, on 67 sessions. None of these is new since the record to 30 October.

## Seat bands now

```
unavailable  1448
veryLimited    18
limited        13
available       4
no band       933
total        2416
```

1,448 of the 1,483 sessions that take reservations are full, up from 1,440 of 1,483 on 30 October.

## Connection drops

The journal has 51 outage starts in the window, from 14 October to 1 November. 49 have an end row, written when the watcher reached the API again on its own. 5 starts are new since the record to 30 October, 2 on 31 October and 3 on 1 November. All 5 have an end row. The other 2 are the ones from 25 October, at 11:14 and 12:56 PDT. Sweeps resumed at 12:20 and 12:57 PDT, but no end row was written for either. No write was sent inside any of the 49 outages with an end row, or between either of the other 2 starts and the next sweep.

## The sign-in expiry, 31 October

On 31 October the watcher's sign-in ran out. The journal has a `watcher.signin` row with outcome `needed` at 20:48:34 PDT. Its recorded error is the token endpoint refusing the refresh with `invalid_grant`, seen on a `ListSessions` call. The attendee ran `reseat login`. The journal has a `watcher.signin` row with outcome `ok` at 20:54:45 PDT, 6 minutes later. No write was sent between the two rows. The `needed` row is the only journal row in that stretch.

No sweep ran from 19:31:53 to 20:54:42 PDT, about 83 minutes. That stretch starts 77 minutes before the `needed` row. It is in the blind spells below. The store does not say why no sweep ran before the `needed` row.

## Blind spells

A blind spell is a gap of more than 60 minutes between two sweeps. No sweep ran in it, so a seat that opened and filled again inside it was not seen. There were 50, adding up to about 175 hours. Start of the gap and its length, PDT up to 31 October and PST on 1 November:

```
10-12 20:13  100 min     10-16 02:44   61 min     10-23 00:05  234 min
10-12 23:54  1326 min    10-16 03:45   61 min     10-23 03:59  138 min
10-14 06:04  130 min     10-16 04:46   61 min     10-23 20:58  570 min
10-14 08:13  111 min     10-16 22:25  118 min     10-25 01:34  258 min
10-14 16:40  126 min     10-17 00:23  278 min     10-25 06:44  189 min
10-15 06:05   61 min     10-17 05:45   80 min     10-25 10:01  139 min
10-15 07:13   77 min     10-17 15:27  107 min     10-27 03:25  752 min
10-15 08:30  159 min     10-17 17:29   91 min     10-29 15:22  116 min
10-15 11:10   61 min     10-17 22:16  817 min     10-29 17:18  139 min
10-15 12:11   61 min     10-18 23:43  661 min     10-30 06:27   85 min
10-15 13:12  122 min     10-19 10:45  355 min     10-31 08:49   63 min
10-15 15:14  122 min     10-19 18:00  237 min     10-31 19:31   83 min
10-15 17:16   61 min     10-19 23:50  498 min     11-01 06:40   60 min
10-15 18:17   71 min     10-20 15:59  208 min     11-01 07:40   71 min
10-15 22:54  108 min     10-21 00:39  498 min     11-01 08:51   73 min
10-16 00:42   61 min     10-21 18:29  100 min     11-01 11:22   88 min
10-16 01:43   61 min     10-21 21:13  398 min
```

Minutes are rounded. The 60-minute entry on 1 November ran 60.4 minutes. The longest was 22 hours from 23:54 PDT on 12 October. 6 blind spells are new since the record to 30 October, adding up to about 7.3 hours. The longest of those was 88 minutes from 11:22 PST on 1 November. The 31 October one at 19:31 PDT is the stretch before the sign-in row above.

## Target seats

No target seat was caught as it freed in this window.

`reseat findings` reports 3 sessions "on targets". It compares the openings with the target list in the rules file as it is today, backups included, not with the list as it was when each seat opened. Those 3 sessions are chalk talks with 6 openings between them, the last on 19 October. None of the 3 has opened since. The bootcamp that opened on 30 October is not on today's target list.

The journal has no reserve row after 26 October 18:19 PDT, the clash test ([the record](2026-10-26-clash-refused.md)). These are the only write rows after that:

- 30 October, 15:42 PDT: a writes-open probe, a cancel sent for a session the attendee never holds.
- 1 November, 14:54 PST: 15 `UpdatePersonalTime` rows, all `sent`, then one `GetSchedule` row with outcome `readback-ok` a second later. These moved the attendee's leave-now blocks ([the record](2026-10-25-leave-now-blocks.md)) to Las Vegas wall-clock time after the API spec changed how personal time is read. See [1.0.0 in the changelog](../../CHANGELOG.md).

No seat was reserved or cancelled by either.

## Cross-check against `reseat findings`

Every count matches. `reseat findings` was run after the last sweep read, at 16:02:47 PST, and counted the same 21,274 sweeps. It counts its 21 days as calendar days in Las Vegas time, 12 October to 1 November inclusive. Its "Connection drops recovered: 49" is the 49 outage end rows.

## Queries

All read-only, with `sqlite3 -readonly ~/.reseat/reseat.db`. `1791861166.36` is the load sweep at 20:12:46 PDT on 12 October, and `1793577768` is just after the last sweep read. `1793523600` is 02:00 PDT on 1 November, when the clock went back. Times are shown in PDT with `datetime(ts-25200,'unixepoch')` before it and in PST with `datetime(ts-28800,'unixepoch')` after it.

```sql
-- sweeps in the window
SELECT COUNT(*) FROM sweeps WHERE ts BETWEEN 1791861166 AND 1793577768;

-- time covered by sweeps less than 5 minutes apart, hours and share of window
SELECT SUM(nxt-ts)/3600.0, 100.0*SUM(nxt-ts)/(1793577767.13-1791861166.36)
FROM (SELECT ts, LEAD(ts) OVER (ORDER BY ts) AS nxt FROM sweeps
      WHERE ts BETWEEN 1791861166 AND 1793577768)
WHERE nxt - ts < 300;

-- blind spells: gaps of more than 60 minutes
SELECT datetime(CASE WHEN ts < 1793523600 THEN ts-25200 ELSE ts-28800 END,'unixepoch')
         AS start_local, ROUND((nxt-ts)/60) AS minutes
FROM (SELECT ts, LEAD(ts) OVER (ORDER BY ts) AS nxt FROM sweeps
      WHERE ts BETWEEN 1791861166 AND 1793577768)
WHERE nxt - ts > 3600;

-- openings on full sessions, and the sessions they were on
SELECT COUNT(*), COUNT(DISTINCT session_id) FROM band_history
WHERE ts BETWEEN 1791861166 AND 1793577768
  AND old_band = 'unavailable' AND new_band IS NOT 'unavailable';

-- openings by type and band
SELECT s.type, b.new_band, COUNT(*), COUNT(DISTINCT b.session_id)
FROM band_history b JOIN sessions s USING (event_id, session_id)
WHERE b.ts BETWEEN 1791861166 AND 1793577768
  AND b.old_band = 'unavailable' AND b.new_band IS NOT 'unavailable'
GROUP BY 1, 2;

-- added, removed and moved: rows and sessions
SELECT kind, COUNT(*), COUNT(DISTINCT session_id) FROM catalog_changes
WHERE ts > 1791861166.36 AND ts <= 1793577768 GROUP BY kind;

-- added and still listed, by type
SELECT type, COUNT(*) FROM sessions WHERE session_id IN
  (SELECT session_id FROM catalog_changes
   WHERE kind = 'added' AND ts > 1791861166.36 AND ts <= 1793577768)
GROUP BY type;

-- seat bands now
SELECT COALESCE(band, 'none'), COUNT(*) FROM sessions GROUP BY 1;

-- outages: start and end rows, and the sign-in rows
SELECT op, outcome, COUNT(*) FROM journal
WHERE op IN ('watcher.outage', 'watcher.signin')
  AND ts BETWEEN 1791861166 AND 1793577768 GROUP BY 1, 2;

-- every journal row that is not the watcher's own, after the clash test
SELECT datetime(CASE WHEN ts < 1793523600 THEN ts-25200 ELSE ts-28800 END,'unixepoch'),
       op, outcome FROM journal
WHERE ts > 1792113540 AND op NOT LIKE 'watcher.%'
ORDER BY ts;
```

The time to refill was measured per opening, to the next row for the same session whose new band is `unavailable`, the same way `reseat findings` does it. The 3 sessions on today's target list were found by matching each opened session's id, code and base code against the rules file and printing only a match count, with each match's type and opening times. The error text of the sign-in row was printed with every long string masked.

## Screenshots

- `2026-11-01-watcher-to-1-nov-findings.png`: the top of re:Seat's Findings page for this window, rendered by `pages.findings_body` from a copy of the local store cut off at the last sweep read here, so its counts match this file. "3 of these sessions were targets" on it compares with the current target list, as `reseat findings` does. None of those 3 opened while it was a target (see Target seats above).

## Result

Seats keep opening on full sessions, on 16 of the 21 days, and some fill again within minutes. Openings have slowed. There was one in the two days since 30 October. Sessions keep being added, 3 since 30 October. The watcher saw both, and recovered from 49 connection drops on its own. When its sign-in ran out on 31 October, it recorded the refused refresh and sent no write. It swept again 6 minutes later, once the attendee signed in. It caught no target seat as it freed in this window. No session on today's target list opened after 19 October. Since the clash test on 26 October, the journal has no reserve and no cancel of a held seat. Its only writes since then are the probe and the leave-now blocks moved for the spec change.

## What it does not prove

- That the watcher would catch a target seat as it frees. No session on today's target list opened while it was a target, as far as these counts show, so that path has not run live. The store does not keep earlier versions of the target list, so a session dropped from it since could have opened while it was still listed. The live swap and autopilot records show the booking side on seats that were already open.
- Anything inside the 50 blind spells, about 175 hours. A seat that opened and filled again inside one is not in these counts.
- Why no sweep ran for 77 minutes before the sign-in row on 31 October.
- Exact refill times. Each is measured to the next sweep that saw the session full, so it is an upper bound.
- How the 2 outages with no end row ended.
- That the 1.0.0 watcher behaves the same over days. It had run about an hour on that code at the last sweep read.
- Anything about other attendees, other accounts or other laptops. This is one account on one laptop that slept.
