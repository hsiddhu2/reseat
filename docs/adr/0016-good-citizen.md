# ADR-0016: Good-citizen limits

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

Seats are scarce and shared. A tool that grabs or holds more than an attendee asked for harms other attendees.

## Decision

Reserve only what the rules file asks for. Never hold two sittings of one talk. Cap the watch list (`watch_cap`, default 25, at most 100). When a swap frees a seat, release it at once. Sweep no faster than every 30 seconds.

## Alternatives considered

Book every open sitting and sort it out later. Rejected: it takes seats from others.

## Consequences

re:Seat may miss a seat it was not told to want.

## Where

- [`src/reseat/router.py`](../../src/reseat/router.py)
- [`src/reseat/rules.py`](../../src/reseat/rules.py)
- [`src/reseat/watcher.py`](../../src/reseat/watcher.py)
