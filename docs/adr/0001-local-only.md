# ADR-0001: Local-only, no hosted service

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The AWS Events API signs an attendee in through a loopback port on their own machine and issues tokens for that attendee only ([API facts](../api-facts.md)). A hosted service would hold other people's tokens.

## Decision

re:Seat runs only on the attendee's machine. Tokens live in the OS keychain through `keyring`. Every API call starts on the laptop. The phone is a remote that reaches the laptop over ntfy or, optionally, Tailscale.

## Alternatives considered

A hosted service with per-user token storage. Rejected: it would hold other attendees' tokens, which the sign-in flow is not built for.

## Consequences

Nothing to operate and no other attendee's data. The laptop must stay awake and online for the watcher to work. The README says how under Running it all week.

## Where

- [`src/reseat/auth.py`](../../src/reseat/auth.py)
- [`src/reseat/config.py`](../../src/reseat/config.py)
