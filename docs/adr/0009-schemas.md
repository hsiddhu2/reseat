# ADR-0009: Strict rules file, permissive API models

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1

## Context

A typo in the rules file should fail loudly. The spec says fields and failure codes are added over time ([API facts](../api-facts.md)).

## Decision

The rules file is parsed with pydantic and `extra="forbid"`. API models use `extra="allow"`. Unknown enum values are handled as generic, never with an exhaustive switch.

## Alternatives considered

Strict API models. Rejected: a new field from AWS would break every sweep.

## Consequences

A rules typo is an error the attendee sees at once. A new API field does not break parsing, but a renamed field can drift silently, so the spec is kept in the repo to check against.

## Where

- [`src/reseat/rules.py`](../../src/reseat/rules.py)
- [`src/reseat/models.py`](../../src/reseat/models.py)
