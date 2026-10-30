# ADR-0002: The access token goes only to api.awsevents.com

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The base URL can be overridden for tests and the demo. A mistake there must never send a real token elsewhere.

## Decision

The client attaches the access token only when the base URL is exactly `https://api.awsevents.com`, whatever the configuration says. Tests use an in-process transport, never another host.

## Alternatives considered

An allowlist of hosts, or a host per environment. Rejected: one fixed host leaves nothing to misconfigure.

## Consequences

A token cannot reach another host through the client. A mock server can be used only without sign-in.

## Where

- [`src/reseat/client.py`](../../src/reseat/client.py)
