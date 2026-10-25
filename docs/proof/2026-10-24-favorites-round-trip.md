# Favorites sync adds a favorite, then removes it when the target is dropped

- **Date and time:** 2026-10-24, 21:06 to 21:07 PDT
- **re:Seat version:** `dd09555`
- **Where:** the attendee's laptop at home, signed in with the Builder ID registered for re:Invent 2026

## What was checked

`reseat favorites sync` against the real API, both ways. AssociateFavorites added one session the attendee had not favorited. After the target was dropped from the rules, DisassociateFavorite removed it again. Each write was followed by a GetSchedule read-back.

The check ran in a separate re:Seat home folder with its own rules file and journal, so the attendee's own rules were not used. The session was AMZ202-R1, which the attendee neither holds nor had favorited. A read-only check before the first write confirmed both.

## Commands and output

Before. AMZ202-R1 was neither reserved nor favorited.

```
$ reseat whoami
re:Invent 2026, Mon 30 Nov to Fri 4 Dec 2026, Las Vegas. Times in 
America/Los_Angeles.
The API labels the zone PDT, but on the first day Las Vegas is on PST. re:Seat 
uses the zone, not the label.
Registered for reinvent2026. 15 reserved, 25 favorites, 0 personal time entries.
```

The rules file in the test home had one target, AMZ202-R1 by its session id.

```
$ reseat favorites sync --dry-run
  would add AMZ202-R1
1 sitting: 0 added, 0 already favorited, 0 failed, 0 unconfirmed.

$ reseat favorites sync
  added AMZ202-R1
1 sitting: 1 added, 0 already favorited, 0 failed, 0 unconfirmed.

$ reseat whoami
Registered for reinvent2026. 15 reserved, 26 favorites, 0 personal time entries.
```

Then the rules file was set to `targets: []`.

```
$ reseat favorites sync --dry-run
  would remove AMZ202-R1 (no longer a target)
0 sittings: 0 added, 0 already favorited, 0 failed, 0 unconfirmed.
1 favorite re:Seat added for dropped targets to remove. Favorites you added are 
never removed.

$ reseat favorites sync
  removed AMZ202-R1 (no longer a target)
0 sittings: 0 added, 0 already favorited, 0 failed, 0 unconfirmed.
1 favorite re:Seat added for dropped targets removed. Favorites you added are 
never removed.

$ reseat whoami
Registered for reinvent2026. 15 reserved, 25 favorites, 0 personal time entries.
```

The journal in the test home, read-only:

```
2026-10-24 21:06:42  AssociateFavorites    success
2026-10-24 21:06:43  GetSchedule           readback-ok
2026-10-24 21:07:03  DisassociateFavorite  removed
2026-10-24 21:07:04  GetSchedule           readback-ok
```

## Result

Both favorite writes work against the real API, and each was confirmed by a read-back. The removal list held only the favorite re:Seat had added itself. The 25 favorites the attendee already had were not listed for removal, and were all still there after it. The attendee's 15 reserved seats were not touched. The schedule ended as it started.

## What it does not prove

One account, one session, one evening. It does not show a removal skipped because the attendee holds a seat in that session, or a sync skipped because a target is missing from the catalog. Both are covered by tests against the fake only.
