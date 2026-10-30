# ADR-0017: A fake API is the test boundary

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

Only a registered attendee can call the real API, writes spend real seats, and failure modes are rare live.

## Decision

Tests use `FakeEventsApi` as an httpx transport. It produces every documented failure: partial bulk results, sessionFull, scheduleConflict, 409, 429 with Retry-After, 5xx and expired sign-in. Tests raise dropped connections from their own transport. No test touches the network. Live behaviour is recorded in dated proof files.

## Alternatives considered

Record and replay live traffic. Rejected: it cannot produce failures on demand.

## Consequences

Every failure path has a test. A difference between the fake and the live API is found only live, as with the 400 on a cancel of a session not held.

## Where

- [`src/reseat/fakeapi.py`](../../src/reseat/fakeapi.py)
- [`tests/test_fault_storm.py`](../../tests/test_fault_storm.py)
- [`docs/proof/README.md`](../proof/README.md)
