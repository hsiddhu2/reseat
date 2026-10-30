# ADR-0011: SQLite store with owner-only files

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The watcher needs the catalog, band history and a journal of every write, on the attendee's machine.

## Decision

One SQLite file in `~/.reseat`, created 0600 without following links, in a 0700 folder.

## Alternatives considered

Plain JSON files, or a database server. Rejected: no atomic updates, or something to run.

## Consequences

No server to run. The store has no schema version or retention policy yet, and both are planned. Seats live in AWS, so losing the file loses history, not seats.

## Where

- [`src/reseat/store.py`](../../src/reseat/store.py)
- [`src/reseat/config.py`](../../src/reseat/config.py)
