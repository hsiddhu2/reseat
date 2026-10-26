# Leave-now blocks written to the official schedule

- **Date and time:** 2026-10-25, 17:23 to 17:49 PDT
- **re:Seat version:** `503f658`
- **Where:** the attendee's laptop, `reseat guard sync`, signed in with the Builder ID registered for re:Invent 2026

## What was checked

`reseat guard sync` writes a 5-minute personal time block before each held session: the start, minus the 11-minute cutoff, minus a walk allowance. It is the first time it ran against the live API. Sessions are not named here, and times are left out.

## The first run was refused

The dry run listed 15 blocks to create. The real run sent the first one and got:

```
CreatePersonalTime: 400 The personal time entry could not be applied. Stopped. Read back below.
Read-back differs: 15 missing, 0 stale, 0 extra. Run guard sync again to settle.
```

Nothing was written, and nothing more was sent. The request met every documented rule: UTC with no offset, seconds 00, a 5-minute length, a 16-character title and a 101-character description.

## Finding the cause

Four single requests, each read back, with any block that was created deleted at once and read back again. The attendee approved each step.

| Test | Title | Description | Location | Result |
|---|---|---|---|---|
| 1 | as sent | as sent | as sent | start moved to a 5-minute mark, refused |
| 2 | short | short, plain words | none | accepted, then deleted |
| 3 | as sent | as sent | none | refused |
| 4 | as sent | same words without `:` `(` `)` `[` `]` | none | accepted, then deleted |

The description as sent was `re:Seat leave-now. Walk ... allowed (estimate). [reseat]`. The API refused that description and accepted the same words with all five characters removed. Which of the five causes it was not tested one by one. The spec does not say so. The start minute and the location were not the cause. The finding is in [the API facts](../api-facts.md).

## The fix, and the second run

re:Seat now keeps descriptions to letters, digits, spaces, commas and full stops, and marks its own blocks `Leave now by reseat. ... Set by reseat`. A refused write is journaled with its status, and a 400 on one block no longer stops the rest. The fake API refuses the same characters, so a test would catch it again.

```
$ reseat guard sync
  create Leave for <held session> ...  Leave now by reseat. Walk ... allowed, estimate. Set by reseat
  ... 15 in all
15 to create, 0 to update, 0 to delete, 0 already right.

$ reseat guard sync --dry-run
0 to create, 0 to update, 0 to delete, 15 already right.
```

The journal, read-only, counts by operation:

```
17:23:27  GetSchedule         readback-disagreement  15 missing (the first run)
17:26 to 17:28  CreatePersonalTime  refused 2, sent 2 (the four tests)
17:27 to 17:28  DeletePersonalTime  sent 2 (the two test blocks removed)
17:49:29 to 17:49:36  CreatePersonalTime  sent 15
17:49:37  GetSchedule         readback-ok
```

## Result

15 leave-now blocks are in the attendee's official schedule, confirmed by one read-back. A second run sends nothing. Titles holding a hyphen were accepted, and so were blocks that fall inside a held session.

## What it does not prove

The blocks were written by hand with `reseat guard sync`, not from the phone. The first refused run is in the journal only as its read-back, because refused personal-time writes were not journaled until this fix.
