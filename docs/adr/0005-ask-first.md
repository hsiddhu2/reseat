# ADR-0005: Ask first for every cancel, with auto mode opt-in per target

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

A cancel gives up a real seat. The attendee decides, unless they said in advance that they accept it for one target.

## Decision

In the watcher, the web app, the phone and the MCP server, every swap is a proposal with a random plan id that works once and expires in 10 minutes. It runs on the attendee's approval. A target marked `auto_swap: true` swaps on its own after the same checks, and the attendee is told. On the command line, `reseat swap` and `reseat cancel` show what will change and ask y/N, which `--yes` skips, except that `cancel --yes` is refused while the band is not open. Nothing outside the rules file is booked without a tap.

## Alternatives considered

Full automation for every target. Rejected: the attendee would lose seats to choices they never made.

## Consequences

Slower than full automation on targets without auto. No surprise cancels.

## Where

- [`src/reseat/watcher.py`](../../src/reseat/watcher.py)
- [`src/reseat/rules.py`](../../src/reseat/rules.py)
- [`src/reseat/cli.py`](../../src/reseat/cli.py)
