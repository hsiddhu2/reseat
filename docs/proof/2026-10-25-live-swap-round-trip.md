# A safe swap on the live API, there and back

- **Date and time:** 2026-10-25, 13:10 PDT
- **re:Seat version:** `1f9ff86`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The safe swap on the real API: cancel a held seat and reserve another at the same time, only after a fresh read of the new seat and a checked way back to the old one, then read the schedule back. Done twice, there and back, so the attendee ended where they started. The attendee chose to show these two sessions.

- **INV510-R1**, Fri 4 Dec 09:00, Encore, chalk talk, held, open
- **INV508-R1**, Fri 4 Dec 09:00, Encore, chalk talk, open

The API has no swap. Reserving INV508-R1 while holding INV510-R1 is refused with `scheduleConflict`, so INV510-R1 must be cancelled first.

## Commands and output

The preview first. The checks ran and the prompt was answered no, so nothing was sent:

```
$ reseat swap <INV510-R1 id> <INV508-R1 id>
Checks passed. Fallbacks if this fails: INV510-R1 (2026-12-04 09:00).
Cancel INV510-R1 and reserve INV508-R1? [y/N]: n
```

Then the swap, and the swap back:

```
$ reseat swap <INV510-R1 id> <INV508-R1 id> --yes
Checks passed. Fallbacks if this fails: INV510-R1 (2026-12-04 09:00).
  swap INV510-R1 -> INV508-R1: proposed -> checked -> cancelled -> reserved -> verified
  held now: INV508-R1, <16 other held sessions>

$ reseat swap <INV508-R1 id> <INV510-R1 id> --yes
Checks passed. Fallbacks if this fails: INV508-R1 (2026-12-04 09:00).
  swap INV508-R1 -> INV510-R1: proposed -> checked -> cancelled -> reserved -> verified
  held now: INV510-R1, <16 other held sessions>
```

The journal, read-only, an excerpt with the start, before and after rows left out:

```
13:10:20  swap.checked     INV510-R1 -> INV508-R1
13:10:21  swap.cancelled
13:10:23  swap.reserved
13:10:23  swap.verified
13:10:40  swap.checked     INV508-R1 -> INV510-R1
13:10:41  swap.cancelled
13:10:43  swap.reserved
13:10:43  swap.verified
```

## Result

Both swaps verified against the official schedule. Each took 2 to 3 seconds from the cancel to the verified read-back. For that time the attendee held neither seat, and the released seat was free for anyone. The other 16 held sessions were untouched. Before each cancel, re:Seat confirmed the seat being given up still had a way back: its own open band.

The declined preview was journaled as a failed swap. That was wrong, since nothing failed, and it now journals as declined.

## What it does not prove

Both swaps were run by hand with `reseat swap`, not by the watcher on autopilot when a ranked target freed. The rollback path, where the new seat is refused after the cancel, was not exercised live. It is covered by tests against the fake API.
