# Changelog

Notable changes in each release. Dates are UTC, as published on [PyPI](https://pypi.org/project/reseat/#history). The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Tags before 0.5.1 were never published and are not listed.

## [Unreleased]

## [0.8.1] - 2026-10-31

### Added

- Plugin packages for the MCP server.
- Architecture decision records in `docs/adr/`, plus SECURITY.md, CONTRIBUTING.md and this changelog.
- README: dependencies, release history, and uvx commands pinned to Python 3.12.

### Changed

- README trimmed to an overview. The rules file reference, running it all week, email and text alerts, and the MCP server setup moved to `docs/guide/`, with a short summary and a link left in the README.
- CI runs mypy on src and shows the errors. Strict typing is not yet enforced.

### Fixed

- The MCP server re-reads the rules file before each tool call, and any change drops pending plans.
- Today page: headings in "Wanted today, not held" are no longer cut off.
- The test fixture is now a 200-session sample of the 1 October catalog, not the whole catalog, so the working tree and the sdist no longer carry it. Git history still holds the old file.
- On site, the watcher polls at most 35 sessions, not 40. That spends 105 of GetSession's 120 a minute and leaves 15 for the fresh reads before a swap or booking, which no longer wait on the quota.

### Security

- CI: every GitHub Action pinned by commit SHA, Dependabot for pip and Actions, CodeQL for Python, and pip-audit of the installed dependencies.
- A Retry-After that is not a number, or is outside 0 to 60, now waits 60 seconds instead of skipping the write's read-back and journal.

## [0.8.0] - 2026-10-26

### Added

- Autopilot: the rules file runs re:Seat on its own.
- Findings page: openings on full sessions, how fast they filled, sessions added, outages recovered.
- Book offers on a tap, on the Approve page and on the notification.
- Email and text alerts through the attendee's own Amazon SNS topic.
- All 12 operations in use: a GetEvent check of the event's time zone, and removal of favorites re:Seat added for a dropped target.

### Changed

- A full catalog sweep is about 10 calls.
- The writes probe is journaled.

### Fixed

- Leave-now blocks avoid characters the live API refuses in a description.

### Security

- Sign-in attempts to the web app are limited per device, with a shared cap.

## [0.7.0] - 2026-10-19

### Added

- Planning tools on the MCP server for the attendee's own AI app: `search_sessions`, `open_slots`, `check_plan` and `draft_rules`. They state facts and never rank.
- Walk-up odds with an 80 percent range, and leave times with a chosen confidence.

### Fixed

- Probe and cancel handle the live API's 400 for a session not held.
- The web app works in real browsers and on phones.

## [0.6.1] - 2026-10-11

### Changed

- One menu on every page. Today and Approve fit a phone screen. Every walking time is labelled an estimate.

## [0.6.0] - 2026-10-11

### Added

- Week, Day, Activity, Targets, Status and Demo pages.
- Morning briefing, leave in 10 minutes and a wanted talk soon. Quiet hours and muted categories.

## [0.5.3] - 2026-10-11

### Added

- Demo control panel with scenarios run through the real engine.
- Pushes when the catalog returns, a held session moves or goes, and why a seat was not booked.

## [0.5.2] - 2026-10-11

### Added

- Swap now and Keep on the phone notification.
- The web app on a phone through Tailscale Serve.

## [0.5.1] - 2026-10-10

First release on PyPI.

### Added

- The watcher, which books a freed seat or a new repeat sitting of a target.
- The safe swap, which cancels a held seat only after a fresh read shows the new seat open and a way back.
- Leave-now blocks in the official schedule, and the cutoff guard.
- Sweep guards that refuse an empty or gutted catalog.
- `reseat favorites sync` and `reseat rules from-schedule`.
- The MCP server, which writes only by approving a plan id it proposed.
- The local web app and ntfy pushes.

[Unreleased]: https://github.com/hsiddhu2/reseat/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/hsiddhu2/reseat/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/hsiddhu2/reseat/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/hsiddhu2/reseat/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/hsiddhu2/reseat/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/hsiddhu2/reseat/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/hsiddhu2/reseat/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/hsiddhu2/reseat/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/hsiddhu2/reseat/tree/v0.5.1
