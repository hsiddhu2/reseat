# ADR-0012: One engine under three surfaces

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The CLI, the web app and the MCP server must behave the same way.

## Decision

All three call the same engine modules. No business logic in `cli.py`.

## Alternatives considered

Separate logic per surface. Rejected: a fix would have to land three times.

## Consequences

A fix in the engine reaches every surface.

## Where

- [`src/reseat/cli.py`](../../src/reseat/cli.py)
- [`docs/architecture.md`](../architecture.md)
