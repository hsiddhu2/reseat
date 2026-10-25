# What the watcher saw in its first eight days

- **Date and time:** 2026-10-20, 21:04 PDT, counting from the catalog's return on 2026-10-12 20:12 PDT
- **re:Seat version:** `5b8ff59`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The watcher ran on the attendee's laptop whenever it was awake, against the live catalog. Every sweep stores each seat band change and each session added, removed or moved. These counts are read from that local store, read-only. Sessions are shown by type and band only.

## Command

```
$ reseat sync --no-abstracts
Synced 2253 sessions in 4.3s. Added 0, removed 0, moved 0, band changes 0, newly
open 0.
```

Then read-only counts from the `sweeps`, `band_history`, `catalog_changes` and `journal` tables of the local store.

## Sweeps

3,826 sweeps of the whole catalog between 12 and 20 October. The laptop slept some nights, so this is not one every minute for eight days. Days ranged from 36 sweeps (12 October, the evening the catalog came back) to 885.

## Seats that opened on full sessions

```
full -> very limited    24
full -> available        1
total                   25, on 19 sessions

openings by type        openings  sessions
  chalk talks           19        14
  workshops              6         5
```

At least one full session opened again on every day from 12 to 20 October. Three of the openings were full again within 10 minutes, the fastest in about one minute. That is the case a person refreshing the app now and then misses, and a sweep every minute catches.

None of the 19 sessions was a talk the attendee targets, so the watcher booked nothing. The watcher reserved and cancelled nothing. The only writes in the window were the manual round trip on 12 October ([the record](2026-10-12-round-trip.md)).

## Sessions added

36 sessions were added to the catalog from 13 October. 28 of them are still listed: 10 breakouts, 6 chalk talks, 5 keynotes, 4 lightning talks, 2 workshops and 1 code talk. 2 of the 28 are two sittings of one new talk, both with repeat codes.

## Seat bands now

```
unavailable  1318
veryLimited    85
limited        52
available      14
no band       784
total        2253
```

1,318 of the 1,469 sessions that take reservations are full, up from 1,115 of 1,460 on 12 October ([the record](2026-10-12-catalog-returned.md)).

## Connection drops

The journal has 28 outages between 14 and 20 October, each with a start and an end. The watcher recovered from every one on its own, with no write sent during any of them.

## Result

Seats do open on full sessions, every day, and some close again within minutes. Repeat sittings keep being added. The watcher sees both. It booked nothing, because none of them was a session the attendee asked for. This file will be updated with the counts up to submission.

## Update, 24 October

Counted the same way, read-only, from 2026-10-12 20:12 to 2026-10-24 23:40 PDT, at re:Seat `dd09555`.

- **Sweeps:** 6,892 sweeps of the whole catalog.
- **Seats that opened on full sessions:** 30 openings on 23 sessions. 29 went to very limited and 1 to available. Chalk talks had 20 openings on 15 sessions, workshops 7 on 6, and code talks 3 on 2. Four were full again within 10 minutes. The fastest opened at 23:35:48 PDT on 12 October, a chalk talk going from full to very limited, and the very next sweep, 66.5 seconds later, saw it full again. Seven were full again within an hour. The typical one, the median, stayed open about 4 hours 53 minutes. 20 opened between 09:00 and 18:00 Las Vegas time, 8 between 22:00 and 07:00, and 2 in between. A full session opened on 11 of the 13 days.
- **Target talks:** none of the 23 sessions was a sitting the attendee targets, so the watcher booked nothing.
- **Writes:** the watcher reserved and cancelled nothing. The only reserve and cancel rows in the journal are the two round trips on 12 October.
- **Sessions added:** 62 sessions were added from 13 October, and 51 of them are still listed. 31 of the 62 were added from 21 October. The 51 by type: 16 chalk talks, 11 breakouts, 9 event services, 5 keynotes, 4 lightning talks, 3 featured experiences, 2 workshops and 1 code talk.
- **Connection drops:** the journal has 36 outages in all, 8 of them from 21 October. The watcher recovered from each one on its own.

Seat bands now:

```
unavailable  1374
veryLimited    55
limited        36
available      14
no band       794
total        2273
```

1,374 of the 1,479 sessions that take reservations are full. The same counts are in [`docs/findings.json`](../findings.json), exported with `reseat findings --json`.
