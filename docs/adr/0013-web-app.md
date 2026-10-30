# ADR-0013: Web app on loopback by default

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The phone may reach the web app over Tailscale or the local network, and the web app can approve writes.

## Decision

`reseat serve` binds to 127.0.0.1 by default and refuses 0.0.0.0. Any other address needs `serve_secret`, entered once at /login or carried by a one-time link, which sets an HttpOnly, SameSite=Strict cookie. On loopback with no secret there is no sign-in and the Host header must be loopback. Actions need the request origin to match and an `X-Reseat` header.

## Alternatives considered

Listen on every interface with a password. Rejected: one weak secret would expose writes to the whole network.

## Consequences

Safe by default. The web app is plain HTTP, so off loopback use Tailscale.

## Where

- [`src/reseat/serve.py`](../../src/reseat/serve.py)
- [`src/reseat/tailnet.py`](../../src/reseat/tailnet.py)
