# ADR-0006: Quota is tracked per operation, per minute, on the client

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

The API sets a quota per operation per minute and counts each session in a bulk call. A 429 carries Retry-After ([API facts](../api-facts.md)).

## Decision

A sliding one-minute window per operation counts what each call spends. No batch larger than what is left is sent. A 429 waits its Retry-After once, then fails.

## Alternatives considered

React to 429s only. Rejected: every 429 costs a minute and a bulk write could fail part way.

## Consequences

Within quota by construction, checked by the fault storm test. The window lives in each process, so `serve` and `mcp` side by side can each spend a full minute's quota. On site, polling 40 sessions every 20 seconds uses the whole GetSession quota, so a fresh read before a swap can wait. Both are known limits.

## Where

- [`src/reseat/client.py`](../../src/reseat/client.py)
- [`src/reseat/watcher.py`](../../src/reseat/watcher.py)
- [`tests/test_fault_storm.py`](../../tests/test_fault_storm.py)
