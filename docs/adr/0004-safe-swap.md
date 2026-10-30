# ADR-0004: Safe swap: check a way back before any cancel

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The API has no swap. Reserving B while a held A overlaps it is refused, so A must be cancelled first, and in that gap A's seat can go to someone else ([API facts](../api-facts.md), [the live refusal](../proof/2026-10-26-clash-refused.md)).

## Decision

A swap runs as a state machine: proposed, checked, cancelled, reserved, verified. Before the cancel, a fresh GetSession must show B open, and A must have a way back: A's own band open, or another open sitting of A. If B is refused, A is reserved again, then A's other sittings are tried. A 409 after the cancel, a failed read-back or a failed cancel stops the swap and alerts. One swap runs at a time across processes, through a lock in the store.

## Alternatives considered

Cancel then reserve with one restore attempt and no precondition. Rejected: it can lose both seats.

## Consequences

The worst case is bounded and reported, never silent. Seen live on 25 and 26 October, 2 to 3 seconds holding neither seat by whole-second journal times. The rollback path is tested against the fake only.

## Where

- [`src/reseat/swap.py`](../../src/reseat/swap.py)
- [`tests/test_swap.py`](../../tests/test_swap.py)
- [`docs/proof/2026-10-26-watcher-swap.md`](../proof/2026-10-26-watcher-swap.md)
