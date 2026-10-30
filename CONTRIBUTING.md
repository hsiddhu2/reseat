# Contributing

## Set up

Python 3.11 or newer.

```bash
git clone https://github.com/hsiddhu2/reseat && cd reseat
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && pytest -q
```

No test touches the network. Tests use `reseat.fakeapi.FakeEventsApi` as an httpx transport ([ADR-0017](docs/adr/0017-fake-api.md)). CI runs ruff and pytest, and both must pass. CI also runs `mypy src` in strict mode and shows the errors, but does not fail on them yet. Strict typing is a goal, not yet enforced.

## Workflow

- Open an issue before starting a feature, so the change can be agreed first.
- Branch from `main`: `feat/...`, `fix/...` or `docs/...`. One change per branch.
- Open a pull request and fill in its checklist. CI runs on Linux, macOS and Windows with Python 3.11 and 3.13. The maintainer reviews and merges once CI is green.
- Commit messages: a first line under 70 characters, a blank line, then what changed and why. Name the API rule when a change exists to honour one.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under Unreleased.

## Rules the code keeps

These are recorded as [architecture decisions](docs/adr/README.md). A change that breaks one needs a new decision record first.

- Never blind-retry a write. Read back GetSchedule after every write and journal it.
- Read before writing. Fetch the target with GetSession before any cancel or swap.
- Respect the per-operation quota. Never send a batch larger than what is left.
- Handle 409 (writes closed) everywhere a write can happen.
- Format times only through `campus.py`.
- Tokens never leave the keychain and go only to `api.awsevents.com`.
- Unknown enum values are generic. Never switch exhaustively.
- Every failure the code handles gets a fake that produces it and a test that proves the handling.

## Releases

1. Bump `version` in `pyproject.toml` and `__version__` in `src/reseat/__init__.py`, the `version` in each `plugins/**/plugin.json`, the `reseat[mcp]==` pin in each `plugins/**/.mcp.json` and `mcp.json`, and the uvx pins in README. `tests/test_version.py` and `tests/test_plugins.py` check them.
2. Move the Unreleased entries in CHANGELOG.md under the new version and date.
3. Merge to `main`, then push a tag `vX.Y.Z`. The release workflow publishes to PyPI by trusted publishing ([ADR-0018](docs/adr/0018-release.md)).

## Licence

Contributions are made under the [MIT licence](LICENSE), the same terms as the project.

## Conduct and security

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and [SECURITY.md](SECURITY.md).
