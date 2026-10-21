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
