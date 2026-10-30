# ADR-0015: Notifications through the attendee's own channels

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1. Link and approval since 0.5.2, email and text since 0.8.0

## Context

The phone needs alerts and a way to approve, without the token leaving the laptop.

## Decision

ntfy pushes carry session codes, titles, status lines, and a link to the web app when it is reachable off loopback, never the access token, an abstract or a session id. With approval on, the buttons carry a plan id, and a tap posts to a reply topic derived from the push topic by HMAC, which the laptop reads over one outgoing connection. Email and text go through the attendee's own Amazon SNS topic with their AWS profile. re:Seat stores no AWS keys.

## Alternatives considered

An inbound webhook on the laptop. Rejected: it needs an open port.

## Consequences

No inbound port. Knowing the push topic is enough to post a tap, so the topic is the secret. A tap can only approve a plan id re:Seat proposed.

## Where

- [`src/reseat/push.py`](../../src/reseat/push.py)
- [`src/reseat/sns.py`](../../src/reseat/sns.py)
- [`SECURITY.md`](../../SECURITY.md)
