# A reserve on a full session is refused

- **Date and time:** 2026-11-01, 17:24 PST
- **re:Seat version:** `0cbaf96` (1.0.0), with a one-off script, not in the repo, that builds a one-session plan and sends it through re:Seat's own `router.execute`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The API documentation says a reserve on a full session fails per session with `sessionFull`, inside a 200 answer ([API facts](../api-facts.md)). re:Seat's planner skips a session whose band is `unavailable` before it sends anything, so in normal use this answer comes only when a band was open in the last read and filled before the reserve landed. To see the refusal on the live API, one reserve was sent on purpose for a full session.

- **AIM353**, a target in the attendee's rules file, not held, full, and clashing with nothing held, so a refusal could only mean full

`router.execute` is the same code the watcher and `reseat book` use to send. It sends the batch once, never resends, reads `GetSchedule` back and journals the request, the answer and the read-back together. Had a seat been free, AIM353 would have been booked and kept, since it is a target. The attendee approved the run first.

## Command

The one-off script is not in the repo. Its steps, in order: read `GetSchedule` and stop unless AIM353 is not held, read `GetSession` for AIM353 and print its band, build a one-session `router.Plan` for AIM353, pass it to `router.execute`, then print the outcome, the journal row and the schedule count before and after. `router.execute` sends one `ReserveSessions`, reads `GetSchedule` back and journals both.

## Output

```
now 2026-11-01 17:24:50 PST
before: reserved 15 | AIM353 held: False | fresh band: unavailable
outcome: full | None
journal: ReserveSessions partial
result.successful: []
result.failed: [{'code': 'sessionFull', 'conflictsWith': [], 'sessionId': '<AIM353>'}]
error: None | disagreements: []
after: reserved 15 | AIM353 held: False | unchanged: True
```

The session id is replaced with `<AIM353>`. The script printed only counts and AIM353, not the rest of the schedule.

## Result

The live API refuses a reserve on a full session with HTTP 200, an empty `successful` list and a per-session `sessionFull` failure. re:Seat mapped it to the outcome `full`, journaled the call as `partial`, sent nothing again, and the read-back showed the same 15 reserved sessions before and after.

## What it does not prove

- The race this guards against, where a band reads open and the seat is gone by the time the reserve lands. This was a session already known to be full.
- How the router then moves down a target's sittings and backups after a `sessionFull` in a live run. That path is tested against the fake API.
- Anything about load, other accounts or other sessions.
