# ADR-0014: Remote writes need a plan id re:Seat proposed

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

An AI app or a phone tap must not be able to write anything re:Seat did not propose.

## Decision

Neither the web app, the phone nor the MCP server takes raw session ids for a write. Each write approves a plan id re:Seat proposed, once, before it expires. From the next release, the MCP server also re-reads the rules file before each call, and any change drops pending plans.

## Alternatives considered

Expose reserve and cancel as tools. Rejected: an AI app could write anything.

## Consequences

An AI app can propose but not invent a write. The approval step is an instruction to the AI app, not a gate the server can enforce on the app itself.

## Where

- [`src/reseat/mcp_server.py`](../../src/reseat/mcp_server.py)
- [`src/reseat/serve.py`](../../src/reseat/serve.py)
- [`src/reseat/push.py`](../../src/reseat/push.py)
