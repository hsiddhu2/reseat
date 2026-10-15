# Writes are open: two reversible round trips

- **Date and time:** 2026-10-12, 22:31 to 22:47 PDT
- **re:Seat version:** `d0b93b8`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

Whether reservation writes are open, by booking a seat through the API and cancelling it again. The session is AMZ202-R1, a two-hour workshop on Monday 30 November at 12:30 in Caesars Forum. It is a repeat sitting the attendee does not want, it was open, and it clashed with nothing held. The round trip ran twice. The second time the seat was kept for a few minutes so the attendee could see it in the AWS portal, and was cancelled once the attendee said so.

`book` ran against a rules file with this one target and nothing else:

```yaml
targets:
- session_id: 1780442280564001c38d
  repeats: false
```

Before the first write, GetSchedule listed 14 reserved and 16 favorites. That list was saved as the baseline.

## Round trip 1

```
$ reseat book --rules <one-target rules file> --dry-run
                              Plan, sent in this order
┏━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ # ┃ Code      ┃ Type     ┃ When             ┃ Venue         ┃ Target    ┃ Kind    ┃
┡━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ 1 │ AMZ202-R1 │ Workshop │ 2026-11-30 12:30 │ Caesars Forum │ AMZ202-R1 │ sitting │
└───┴───────────┴──────────┴──────────────────┴───────────────┴───────────┴─────────┘

$ reseat book --rules <one-target rules file> --yes          # 22:32:49 PDT
                              Plan, sent in this order
┏━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ # ┃ Code      ┃ Type     ┃ When             ┃ Venue         ┃ Target    ┃ Kind    ┃
┡━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ 1 │ AMZ202-R1 │ Workshop │ 2026-11-30 12:30 │ Caesars Forum │ AMZ202-R1 │ sitting │
└───┴───────────┴──────────┴──────────────────┴───────────────┴───────────┴─────────┘
  reserved AMZ202-R1
Reserved 1 of 1 targets. GetSchedule now lists 15 reserved.
```

GetSchedule read separately at 22:33:07: 15 reserved, 16 favorites. Added against the baseline: AMZ202-R1. Lost: none. Favorites unchanged.

```
$ reseat cancel 1780442280564001c38d                          # 22:33:14 PDT
AMZ202-R1 Turn a Bee wearable into your re:Invent notetaking companion  [band:
available]
Cancel this reservation? [y/N]: Cancelled. GetSchedule no longer lists it.
```

GetSchedule at 22:33:28: 14 reserved, 16 favorites, both identical to the baseline.

## Round trip 2

```
$ reseat book --rules <one-target rules file> --yes          # 22:39:57 PDT
                              Plan, sent in this order
┏━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ # ┃ Code      ┃ Type     ┃ When             ┃ Venue         ┃ Target    ┃ Kind    ┃
┡━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ 1 │ AMZ202-R1 │ Workshop │ 2026-11-30 12:30 │ Caesars Forum │ AMZ202-R1 │ sitting │
└───┴───────────┴──────────┴──────────────────┴───────────────┴───────────┴─────────┘
  reserved AMZ202-R1
Reserved 1 of 1 targets. GetSchedule now lists 15 reserved.
```

The attendee reported seeing AMZ202-R1 in the schedule in the AWS portal. No screenshot was kept. Then:

```
$ reseat cancel 1780442280564001c38d                          # 22:47:12 PDT
AMZ202-R1 Turn a Bee wearable into your re:Invent notetaking companion  [band:
available]
Cancel this reservation? [y/N]: Cancelled. GetSchedule no longer lists it.
```

GetSchedule afterwards: 14 reserved, 16 favorites, identical to the baseline.

## The journal

Both round trips, from re:Seat's journal. Each entry records the request, the answer and the GetSchedule read back after it.

```
2026-10-12 22:32:57 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
2026-10-12 22:33:19 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
2026-10-12 22:40:06 PDT  ReserveSessions    AMZ202-R1  success    read back: 15 reserved, AMZ202-R1 held
2026-10-12 22:47:16 PDT  CancelReservation  AMZ202-R1  cancelled  read back: 14 reserved, AMZ202-R1 not held
```

## Result

- Reservation writes through the Events API are open. ReserveSessions answered with AMZ202-R1 in `successful` and an empty `failed`, and GetSchedule listed it. CancelReservation succeeded and GetSchedule no longer listed it.
- re:Seat read the schedule back after every write, and the journal holds each read-back.
- Nothing else in the schedule changed. The 14 reserved sessions and 16 favorites were the same before and after both round trips.
- The attendee reported that the booking made through the API showed in the AWS portal.

## What it does not prove

- A swap, a booking caught by the watcher, or a write under load. Those are tested against the fake only.
- How the API answers a full session or a schedule conflict live. Both sittings were open and clashed with nothing.
- Anything about the seat a cancel frees. The band read "available" before, during and after both round trips. A band is not a count, so one seat taken and given back does not show.
- One account, one evening.
