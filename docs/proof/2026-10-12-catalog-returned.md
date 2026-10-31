# The catalog came back, and the held seats with it

- **Date and time:** 2026-10-12, first checks 20:12 to 20:15 PDT, re-run 21:46 to 21:54 PDT
- **re:Seat version:** `636e665` for the first checks, `51b5adb` for the re-run
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The read-only checks that waited on the catalog since 8 October: sync, the band inspection, the rules file, favorites and the booking dry run. Then one probe to ask whether reservation writes are open. Nothing was reserved or cancelled.

## Sync

```
$ reseat sync
Synced 2227 sessions in 6.1s. Added 0, removed 0, moved 0, band changes 0, newly
open 0.
```

The first sync that returned sessions ran at 20:12 PDT. The output above is the re-run at 21:54 PDT.

## Seat bands

Counted from the local catalog after the 21:54 sync.

```
available      36
limited       150
veryLimited   159
unavailable  1115
no band       767
total        2227
```

1,460 sessions accept reservations. The 767 with no band are the ones that do not: 529 breakouts, 229 lightning talks and 9 labs. Of the reservable ones, 674 are chalk talks, 299 workshops, 255 builders' sessions, 125 code talks, 36 labs, 35 gamified learning, 26 bootcamps and 10 exam prep.

Six days after reserved seating opened, three in four reservable sessions were full (1,115 of 1,460).

## The catalog was not re-keyed

The local copy of the 1 October catalog (the test fixture `catalog-2026-10-01.json`, since replaced in the repo by a sample) compared with the 12 October catalog:

```
1 October    2068 sessions
12 October   2227 sessions
same id      2063
same id and same code   2050
id kept, code changed     13   for example SEC330 -> SEC501, AIM360 -> AIM2215
only on 1 October          5
only on 12 October       164   45 breakouts, 34 chalk talks, 18 lightning talks, 14 labs, ...
```

Session ids survived the outage. A session can change its code and keep its id, so a target pinned by session id follows it and a target named by code does not. `reseat rules check` reports a code that no longer resolves. It looks codes up by base code, so SEC309 becoming SEC309-R still resolves and is not flagged.

## The attendee's schedule

```
GetSchedule  -> 200, reserved 14, favorites 16, personal time 0
```

The 14 reservations were made in the AWS portal after seating opened on 6 October. They are shown by type and band only, because the codes would give the attendee's timetable. All 14 are in the returned catalog under the ids GetSchedule lists. Ten were already in the 1 October catalog. Four are sessions added since.

| Held | Type | Band now |
|---|---|---|
| <held session> | Bootcamp | unavailable |
| <held session> | Builders' session | unavailable |
| <held session> | Code talk | unavailable |
| <held session> | Lab | available |
| <held session> | Lab | unavailable |
| <held session> | Lab | unavailable |
| <held session> | Lab | unavailable |
| <held session> | Lab | veryLimited |
| <held session> | Workshop | unavailable |
| <held session> | Workshop | unavailable |
| <held session> | Workshop | unavailable |
| <held session> | Workshop | unavailable |
| <held session> | Workshop | unavailable |
| <held session> | Workshop | veryLimited |

The two favorites not reserved, a breakout and a lightning talk, do not accept reservations.

## Rules, favorites and the booking dry run

```
$ reseat rules check
16 targets (cap 25), 0 meals, max 5 per day, buffer 30 min.
All targets resolve in the local catalog.

$ reseat favorites sync --dry-run
  already <held session>
  already <held session>
  ...
  already <favorite>
  already <favorite>
16 sittings: 0 added, 16 already favorited, 0 failed, 0 unconfirmed.

$ reseat book --dry-run
Already held: <14 held sessions, by code>
Nothing left to try for: <favorite>, <favorite>
  skip <favorite>: not reservable (walk-up or no reservations)
  skip <favorite>: not reservable (walk-up or no reservations)
Nothing to reserve.
```

The `favorites sync` list is cut where it shows `...`. Output from the 21:54 re-run. Session codes of the attendee's own sessions are written as `<held session>` and `<favorite>`: re:Seat printed each by its code.

At 20:13 the same dry run printed the targets the rules file pins by session id as raw ids, for example `Already held: <session id>, <session id>, ...`. That was fixed in `51b5adb`, which also covers `watch` and `serve`. The `favorite` command's own lines were fixed with this record.

## The probe

`reseat probe` sends one CancelReservation for a session the attendee does not hold. While writes are closed the guide says every reserve and cancel answers 409. The probe session here is INV501, a reservable chalk talk not held.

At 20:14 PDT, version `636e665`:

```
$ reseat probe --session-id <id of INV501>
...
ApiError: CancelReservation: 400 The request could not be applied to this session
```

The spec documents 404 for a cancel on a session not held. The API answered 400, and the probe stopped with a traceback. The same 400 came back for a session that accepts no reservations. GetSchedule afterwards still listed 14 reserved and 16 favorites.

At 21:46 PDT, version `51b5adb`:

```
$ reseat probe --session-id <id of INV501>
Writes look open: the API refused the cancel without a 409. A reversible booking
confirms it.
```

GetSchedule afterwards: 14 reserved, 16 favorites, and INV501 not held.

## Result

- The Events API serves the full catalog again: 2,227 sessions, with seat bands.
- Session ids were kept through the outage. Thirteen sessions changed code under the same id.
- Every held session and favorite resolves. re:Seat's rules, favorites and booking dry runs work on the live catalog with no write sent.
- Cancel on a session not held answers 400 in the live API, where the spec documents 404. The probe and the cancel command now handle it, with tests against the fake.

## What it does not prove

- That writes are open. A 400 is not a 409, which points to open, but only a reserve that lands and is read back proves it. That came later the same evening: [2026-10-12-round-trip.md](2026-10-12-round-trip.md).
- Anything about booking, swapping or cancelling a held seat through the API. No such write was sent in these checks.
- One account, one evening.

## Correction to 8 October

[2026-10-08-catalog-empty.md](2026-10-08-catalog-empty.md) said the 14 reservations all had ids beginning `17842318` and that no id in the 1 October catalog began that way. Both were wrong. Five of the 14 begin `17842318`, and 42 sessions in the 1 October catalog do. The catalog was not re-keyed: see above.

## Daily checks

```
2026-10-12 20:12 PDT  reseat sync  2227 sessions (the catalog returned)
2026-10-12 21:54 PDT  reseat sync  2227 sessions, no changes
```
