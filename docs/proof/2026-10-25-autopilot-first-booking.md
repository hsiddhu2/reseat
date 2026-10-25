# Autopilot books two ranked targets on its own, read back and emailed

- **Date and time:** 2026-10-25, 01:10 PDT
- **re:Seat version:** `f2d2883`
- **Where:** the attendee's laptop, `reseat serve --tailscale`, with push and email alerts on

## What was checked

The first booking re:Seat made on its own, for sessions the attendee will attend. The attendee added five ranked targets at the end of the rules file, below the 24 already there, and gave the three full ones `auto_swap: true`. Then they restarted `reseat serve`. Nothing else was typed or tapped.

## The rules the attendee set

Two time slots, each with targets in rank order. Codes are left out because these are now the attendee's own timetable.

```
Monday afternoon slot:  rank 1  <full session>    auto_swap: true
                        rank 2  <full session>    auto_swap: true
                        rank 3  <held session>    open when added
Friday morning slot:    rank 1  <full workshop>   auto_swap: true
                        rank 2  <held session>    open when added
```

The dry run before the restart, sending nothing, listed exactly the two open sessions to reserve and every other target as full or walk-up only.

## What re:Seat did

On the first normal sweep after the restart, the catch-up pass booked the open targets, in rank order, in one call, and read the schedule back:

```
2026-10-25 01:10:53 PDT  ReserveSessions  success
  result:     successful 2, failed 0
  read-back:  both sessions listed as reserved. 17 reserved in all, 25 favorites
  disagreements: none
```

The attendee received a push and an email saying "Seat booked" for each.

## Result

re:Seat booked two real sessions on its own, from the rules file alone, within its caps, and confirmed both from the official schedule. From here the three full targets ranked above them are on autopilot. If one frees, re:Seat swaps up on its own after a fresh read of the new seat and a checked way back to the old one, and says what it did.

## What it does not prove

Both sessions were already open, so this shows the catch-up pass, not a seat caught the moment it freed. No autopilot swap has run live yet. One account, one booking call.
