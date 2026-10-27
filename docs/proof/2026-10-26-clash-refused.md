# A reserve that clashes with a held seat is refused

- **Date and time:** 2026-10-26, 18:19 PDT. The output below is in the laptop's local time, EDT, three hours ahead
- **re:Seat version:** `560257a`, with a one-off script, not in the repo, that calls re:Seat's own client
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The API documentation says reserving a session that overlaps a held one fails per session with `scheduleConflict` and `conflictsWith`, the held sessions it overlaps ([API facts](../api-facts.md)). re:Seat checks for overlaps with the seats it knows are held before it sends, and if a clash gets through it records the `scheduleConflict` and does not resend. So a one-off script sent one on purpose, to see the refusal on the live API.

- **INV510-R1**, Fri 4 Dec 09:00, Encore, chalk talk, held
- **INV508-R1**, Fri 4 Dec 09:00, Encore, chalk talk, not held, seats open

The script reads first and stops unless INV510-R1 is held, INV508-R1 is not, and INV508-R1 has an open seat, so a refusal could only come from the clash. It sends one reserve, never resends it, and reads the schedule back. Had the API accepted it, the script would have cancelled INV508-R1 once and read back again. The attendee approved the run after a read-only dry run.

## Output

```
$ python clash_test.py --send
GetSchedule: 15 reserved, INV510-R1 held=True, INV508-R1 held=False
GetSession INV508-R1: band=available
ReserveSessions answer: {'successful': [], 'failed': [{'sessionId': '<INV508-R1 id>', 'code': 'scheduleConflict', 'conflictsWith': ['<INV510-R1 id>']}]}
Read-back: 15 reserved, INV510-R1 held=True, INV508-R1 held=False
```

The journal, read-only:

```
2026-10-26 21:19:44|clashtest.reserve|before|{}
2026-10-26 21:19:45|clashtest.reserve|after|{"successful": [], "failed": [{"sessionId": "<INV508-R1 id>", "code": "scheduleConflict", "conflictsWith": ["<INV510-R1 id>"]}]}
2026-10-26 21:19:46|clashtest.readback|readback|{"reserved": 15, "held": true, "wanted": false}
```

## Result

The live API refused the reserve exactly as documented: `scheduleConflict`, with `conflictsWith` naming the held INV510-R1. Nothing changed. The schedule read back 15 reserved, INV510-R1 held, INV508-R1 not held. The refusal came as a per-session failure inside the answer, as the documentation says, not as an error status.

## What it does not prove

One clash, between two sessions at the same start time and venue. A partial overlap was not tried. The reserve was sent by a script, not by re:Seat's booking path, which would have skipped it as an overlap. A reserve refused because a session is full has not yet been seen live.
