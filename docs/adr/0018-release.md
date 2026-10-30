# ADR-0018: Release by PyPI trusted publishing

- Status: accepted
- Recorded: 2026-10-30
- In place since: 0.5.1, actions pinned by SHA since 0.8.0

## Context

A stored PyPI token is a secret that can leak, and a package must never carry private files.

## Decision

A version tag runs the release workflow. It publishes through PyPI trusted publishing with no stored token, uses actions pinned by commit SHA, checks the tag matches the version, and fails if a private file is in the package.

## Alternatives considered

Upload from a laptop with an API token. Rejected: a leaked token publishes anything.

## Consequences

No publishing secret exists. A release needs a pushed tag.

## Where

- [`.github/workflows/release.yml`](../../.github/workflows/release.yml)
- [`pyproject.toml`](../../pyproject.toml)
