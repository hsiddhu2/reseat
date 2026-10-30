# ADR-0003: Writes are never blind-retried, and every write is read back

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

ReserveSessions, CancelReservation and the favorite calls are not idempotent. A 200 on a bulk write can still carry per-session failures. A timeout leaves the outcome unknown ([API facts](../api-facts.md)).

## Decision

No write is resent on a guess. A timeout or server error is settled by reading GetSchedule back. A write is resent in two cases only: once after a 429 when its Retry-After has passed, and once after a 401 when the sign-in is refreshed. A later sweep may plan the same target again, from a fresh schedule read. Every write is journaled with its request, its response and the read-back. A swap journals each step before and after. DeletePersonalTime is the only idempotent write.

## Alternatives considered

Retry with backoff, as for reads. Rejected: a resent reserve can report a held session as failed and a resent cancel can release a seat booked in between.

## Consequences

No double bookings and no state lost to a lost reply. A write whose read-back disagrees is recorded as unconfirmed and blocks a second sitting of that talk.

## Where

- [`src/reseat/router.py`](../../src/reseat/router.py)
- [`src/reseat/client.py`](../../src/reseat/client.py)
- [`tests/test_write_resends.py`](../../tests/test_write_resends.py)
