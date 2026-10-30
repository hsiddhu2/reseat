# ADR-0008: Refuse a catalog that looks broken

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

On 8 October 2026 ListSessions answered 200 with zero sessions. Acting on that would have dropped every target ([the record](../proof/2026-10-08-catalog-empty.md)).

## Decision

A sweep that comes back empty, ends short of totalCount, or would drop more than half the catalog is refused, and the local copy kept. A sweep at least half the stored size with mostly new ids becomes a new baseline, not thousands of new sessions. Nothing is booked while a held session is missing locally.

## Alternatives considered

Trust every 200. Rejected by the 8 October outage.

## Consequences

re:Seat may miss a real mass change for one sweep. It never acts on a broken answer.

## Where

- [`src/reseat/store.py`](../../src/reseat/store.py)
- [`src/reseat/client.py`](../../src/reseat/client.py)
- [`tests/test_sweep_guards.py`](../../tests/test_sweep_guards.py)
- [`docs/proof/2026-10-08-catalog-empty.md`](../proof/2026-10-08-catalog-empty.md)
