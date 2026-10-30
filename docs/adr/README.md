# Architecture decision records

Each record states one decision, why it was made, what was rejected, and what it costs. To change a decision, add a new record that supersedes the old one and mark the old one superseded. Start from [the template](template.md).

These records were written on 30 October 2026 for decisions already in the code. "In place since" gives the first release that carried each one.

| ADR | Decision | Status | In place since |
| --- | --- | --- | --- |
| [ADR-0001](0001-local-only.md) | Local-only, no hosted service | accepted | 0.5.1 |
| [ADR-0002](0002-token-host.md) | The access token goes only to api.awsevents.com | accepted | 0.5.1 |
| [ADR-0003](0003-no-blind-retry.md) | Writes are never blind-retried, and every write is read back | accepted | 0.5.1 |
| [ADR-0004](0004-safe-swap.md) | Safe swap: check a way back before any cancel | accepted | 0.5.1 |
| [ADR-0005](0005-ask-first.md) | Ask first for every cancel, with auto mode opt-in per target | accepted | 0.5.1 |
| [ADR-0006](0006-quota.md) | Quota is tracked per operation, per minute, on the client | accepted | 0.5.1 |
| [ADR-0007](0007-writes-closed.md) | Writes closed (409) is a normal state | accepted | 0.5.1 |
| [ADR-0008](0008-sweep-guards.md) | Refuse a catalog that looks broken | accepted | 0.5.1 |
| [ADR-0009](0009-schemas.md) | Strict rules file, permissive API models | accepted | 0.5.1 |
| [ADR-0010](0010-time.md) | Times through one module | accepted | 0.5.1, zone check 0.8.0 |
| [ADR-0011](0011-store.md) | SQLite store with owner-only files | accepted | 0.5.1 |
| [ADR-0012](0012-one-engine.md) | One engine under three surfaces | accepted | 0.5.1 |
| [ADR-0013](0013-web-app.md) | Web app on loopback by default | accepted | 0.5.1 |
| [ADR-0014](0014-plan-ids.md) | Remote writes need a plan id re:Seat proposed | accepted | 0.5.1 |
| [ADR-0015](0015-notifications.md) | Notifications through the attendee's own channels | accepted | 0.5.1, link and approval 0.5.2, email 0.8.0 |
| [ADR-0016](0016-good-citizen.md) | Good-citizen limits | accepted | 0.5.1 |
| [ADR-0017](0017-fake-api.md) | A fake API is the test boundary | accepted | 0.5.1 |
| [ADR-0018](0018-release.md) | Release by PyPI trusted publishing | accepted | 0.5.1, SHA pins 0.8.0 |
