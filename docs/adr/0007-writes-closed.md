# ADR-0007: Writes closed (409) is a normal state

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

Until reserved seating opened, every reserve and cancel answered 409. The spec documents it ([API facts](../api-facts.md)).

## Decision

A 409 holds writes for 15 minutes and is reported once. Openings stay queued. With `probe_session` set, the watcher checks every sweep with a cancel on a session the attendee can never hold, and resumes the moment writes open. A 409 after a swap's cancel stops the swap.

## Alternatives considered

Treat 409 as an error and stop. Rejected: the watcher would miss the moment writes open.

## Consequences

No write storm into a closed window. The probe never changes the schedule.

## Where

- [`src/reseat/watcher.py`](../../src/reseat/watcher.py)
- [`src/reseat/client.py`](../../src/reseat/client.py)
