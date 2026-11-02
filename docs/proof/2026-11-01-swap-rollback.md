# A swap whose new seat is refused rolls back

- **Date and time:** 2026-11-01, 18:50 PST
- **re:Seat version:** `0cbaf96` (1.0.0), with a one-off script, not in the repo, that runs re:Seat's own `Swap.run`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The swap's rollback on the live API. A swap cancels held seat A, then reserves wanted seat B. If B is refused after A is gone, re:Seat reserves A again. That path needs a refusal after the cancel, which normal checks are built to prevent, so the test made one on purpose.

The sessions are not named here, to keep the attendee's schedule private:

- **A**, held, band `available`
- **B**, open (`limited`), overlapping A and also overlapping **C**
- **C**, another held session

re:Seat's pre-check looks for any held session other than A that B overlaps, and would have stopped this swap before the cancel. The script skipped that one check for B, and only for B, so the API's clash refusal would come after the cancel. Every other check and every write ran through `Swap.run` unchanged: the fresh read of B, the checked way back to A, the cancel, the reserve, the rollback and the read-backs. A was chosen because its band was wide open, so the way back was safe. The attendee approved the test first.

## Command

The one-off script is not in the repo. Its steps, in order: read `GetSchedule` and the fresh bands of A and B, create re:Seat's `Swap` with the attendee's rules, replace its clash check with one that returns no clash for B only, then call `Swap.run(A, B, approved=True)` and read `GetSchedule` back. Nothing else in `Swap.run` was changed.

## Output

```
now 2026-11-01 18:50:01 PST
before: reserved 15 | A held True | B held False | C held True
fresh bands: A available | B limited
pre-check skipped on purpose for B, it would have said: ['<B> also overlaps held <C>']
state: rolled_back
steps: ['proposed', 'checked', 'cancelled', 'rolled_back']
reasons: [] | alert: None
after: reserved 15 | A held True | B held False | C held True | unchanged True
```

## The journal

Read-only, session ids replaced with `<A>`, `<B>` and `<C>`, read-back lists cut:

```
18:50:03 swap.proposed  state:proposed
18:50:06 swap.checked   state:checked
18:50:06 swap.cancel    before:checked
18:50:06 swap.cancel    after:checked
18:50:06 swap.cancelled state:cancelled
18:50:06 swap.reserve   before:cancelled  {"session": "<B>"}
18:50:08 swap.reserve   after:cancelled   {"response": {"successful": [], "failed": [{"sessionId": "<B>", "code": "scheduleConflict", "conflictsWith": ["<C>"]}]}, "readBack": [...]}
18:50:08 swap.rollback  before:cancelled  {"session": "<A>"}
18:50:10 swap.rollback  after:cancelled   {"response": {"successful": ["<A>"], "failed": []}, "readBack": [...]}
18:50:10 swap.rolled_back state:rolled_back
```

After the test, `reseat guard sync --dry-run` showed the leave-now blocks 15 already right.

## Result

On the live API, B was refused after A was cancelled, with `scheduleConflict` naming C. re:Seat sent the reserve for B once, did not resend it, reserved A again and read the schedule back. A was held again 4 seconds after its cancel. The attendee ended with the same 15 sessions.

## What it does not prove

- A refusal for a seat that filled in the gap (`sessionFull`), the case the rollback is mainly for. The refusal here was a clash let through on purpose. The rollback code does not depend on which code the refusal carries.
- The branch where A is also gone and re:Seat tries A's other open sittings. That is tested against the fake API.
- Anything under load. A had many seats open.
