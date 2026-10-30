## What and why

Closes #

<!-- What changed, and the API rule it honours if any. -->

## Checklist

- [ ] `ruff check .` and `pytest -q` pass
- [ ] Each failure path this code can hit has a test against `FakeEventsApi`
- [ ] Every write is read back with GetSchedule and journaled
- [ ] No token can reach a log, a file, a URL or any host but `api.awsevents.com`
- [ ] README and `docs/` match the code
- [ ] A line in CHANGELOG.md under Unreleased
- [ ] A new decision record in `docs/adr/` if a rule in CONTRIBUTING.md changes
