# A swap approved with a tap on the phone, there and back

- **Date and time:** 2026-11-01, 17:47 to 18:00 PST
- **re:Seat version:** `0cbaf96` (1.0.0), the running watcher, `reseat serve --tailscale --interval 30`, restarted on 1.0.0 at about 14:56 PST
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026, and the attendee's phone, with push through ntfy

## What was checked

A swap proposed by the watcher and approved with the **Swap now** button on the phone notification, on the live API. Done twice, there and back, so the attendee ended where they started. The target in this test has no `auto_swap`, so the watcher could not run either swap on its own. Each ran only after the attendee tapped Swap now.

The two sessions are not named here, to keep the attendee's schedule private:

- **A**, a held session, with seats open, so a way back existed
- **B**, an open workshop at the same hour, clashing with nothing else held

## How it was started

No seat freed. The rules file was copied first. Then B was added as a target ranked just above A, without `auto_swap`. The watcher reloaded the rules, proposed the swap and sent the push. For the swap back, the order was flipped, and a second push was tapped. Then the rules file was restored from the copy, and `cmp` showed it identical.

The attendee approved the test before the first edit.

## The journal

Read-only, every swap row in those minutes, times PST:

```
17:47:01 watcher.rules reloaded
17:47:24 swap.proposed state:proposed
17:47:28 swap.checked state:checked
17:47:28 swap.cancel before:checked
17:47:28 swap.cancel after:checked
17:47:28 swap.cancelled state:cancelled
17:47:28 swap.reserve before:cancelled
17:47:30 swap.reserve after:cancelled
17:47:30 swap.reserved state:reserved
17:47:30 swap.verified state:verified
17:51:50 watcher.rules reloaded
18:00:11 swap.proposed state:proposed
18:00:14 swap.checked state:checked
18:00:14 swap.cancel before:checked
18:00:14 swap.cancel after:checked
18:00:14 swap.cancelled state:cancelled
18:00:14 swap.reserve before:cancelled
18:00:16 swap.reserve after:cancelled
18:00:16 swap.reserved state:reserved
18:00:16 swap.verified state:verified
18:01:20 swap.proposed state:proposed
18:01:31 watcher.rules reloaded
```

The journal does not record where an approval came from. The attendee confirmed tapping Swap now on the phone for both swaps. The target had no `auto_swap`, so the watcher had no other way to run them.

The proposal at 18:01:20 was never approved. Restoring the rules file at 18:01:31 dropped it, because a proposal made under the old rules does not outlive them. Nothing was sent for it.

A read of the schedule after the test: 15 reserved, A held, B not held, as before.

## Leave-now blocks

After the first swap, re:Seat moved the leave-now blocks to the new seat at 17:48 (a delete, an update and a create, then `GetSchedule` readback-ok). After the swap back, `reseat guard sync --dry-run` showed 1 to create, 1 to update and 1 to delete. `reseat guard sync`, run with the attendee's yes, made them, and a second dry run showed 15 already right.

## Result

A swap can be approved from the phone notification. Each tap ran the full sequence: a fresh read and a checked way back, cancel, reserve, read back, `verified`. The cancel to verified took 2 seconds each way. The attendee ended with the same 15 sessions and the same leave-now blocks.

## What it does not prove

- A swap started by a seat that freed on its own. Both were started by a rules edit.
- The rollback, when the new seat is refused after the cancel. That is shown in [its own record](2026-11-01-swap-rollback.md).
- How long a push takes to arrive. The second push was tapped about 8 minutes after the rules edit, and the journal does not record when it reached the phone.
- Book taps on a new or opened session. Those come only from a real catalog event.
