# MCP server

`reseat mcp` runs a local MCP server over stdio, so an agent can read your targets and plan changes for you. Your AI app starts it as a child process on your machine. Nothing is hosted.

- [Install and sign in](#install-and-sign-in)
- [Add it to your AI app](#add-it-to-your-ai-app)
- [Without installing](#without-installing)
- [As a plugin](#as-a-plugin)
- [The tools](#the-tools)
- [Planning with your AI](#planning-with-your-ai)
- [Every change is two steps](#every-change-is-two-steps)
- [Try it without signing in](#try-it-without-signing-in)
- [Know the limit](#know-the-limit)

## Install and sign in

Install it once, with the MCP extra, and sign in:

```bash
pipx install "reseat[mcp]"     # or pip. Already installed without it: pipx install --force "reseat[mcp]"
reseat login                   # in a terminal, once. The token goes to the system keychain
which reseat                   # the full path for the config below. On Windows: where reseat
```

## Add it to your AI app

Most apps take the same entry. Use the full path it printed, because an app often does not see your shell's PATH:

```json
{
  "mcpServers": {
    "reseat":      { "command": "/full/path/to/reseat", "args": ["mcp"] },
    "reseat-demo": { "command": "/full/path/to/reseat", "args": ["mcp", "--demo"] }
  }
}
```

| App | Where the entry goes |
|---|---|
| Claude Code | No file. Run `claude mcp add reseat -- /full/path/to/reseat mcp`, and for the demo `claude mcp add reseat-demo -- /full/path/to/reseat mcp --demo` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows |
| Cursor | `~/.cursor/mcp.json` for every project, or `.cursor/mcp.json` in one project |
| Kiro | `~/.kiro/settings/mcp.json` for every workspace, or `.kiro/settings/mcp.json` in one workspace |
| Windsurf (Devin Desktop) | `~/.codeium/windsurf/mcp_config.json`, or `~/.config/devin/mcp_config.json` under its new name |
| VS Code | `.vscode/mcp.json` in one workspace, or the user `mcp.json` from the command MCP: Open User Configuration. The top key is `servers`, not `mcpServers`, and each entry adds `"type": "stdio"` |

- **Use the file for every project where you can.** A project file holds only a path, but that path names your home folder, so keep it out of commits others see.
- **Restart or reload the app after editing.** It should list the reseat server with 12 tools.
- **Start with `reseat-demo`.** It needs no sign-in and nothing reaches AWS.
- **Use an app that shows you each tool call before it runs**, because the agent could approve a plan without asking you. See [Know the limit](#know-the-limit).

Which version you need:

| Tools | re:Seat version |
|---|---|
| The other 7 tools | 0.6.1 or newer |
| The four planning tools | 0.7.0 or newer |
| The `autopilot` list in `draft_rules` | 0.8.0 or newer |
| `propose_favorites` | 0.8.2 or newer |
| `guard_sync` | 1.0.0 or newer. Older versions write leave-now blocks in UTC, which the API now reads as Las Vegas time |

## Without installing

If you use [uv](https://docs.astral.sh/uv/), an app can run the server straight from PyPI:

```json
"command": "uvx", "args": ["--python", "3.12", "--from", "reseat[mcp]==1.0.1", "reseat", "mcp"]
```

- `--python` picks an interpreter re:Seat supports, since uv may otherwise pick an older one on your path.
- Pin the version, so a new release never runs with your token before you choose it.
- You still sign in once, with `uvx --python 3.12 --from "reseat[mcp]==1.0.1" reseat login`.
- The watcher runs as `reseat serve` in a terminal, so most people install it.

## As a plugin

This repository is also a Claude Code plugin marketplace and holds two Kiro Powers. Each comes in two forms:

| Form | What it is |
|---|---|
| `reseat-demo` | The demo's fake API. Needs no sign-in |
| `reseat` | Your own seats. It needs a rules file, or the server will not start, and a sign-in, or each tool answers "Not signed in" |

Set both up first with the steps in [Install](../../README.md#install), running each command as `uvx --python 3.12 --from "reseat[mcp]==1.0.1" reseat ...` if you have not installed re:Seat. Both forms run the pinned `reseat[mcp]==1.0.1` through `uvx`, so [uv](https://docs.astral.sh/uv/) must be on your path.

### Claude Code

```bash
claude plugin marketplace add hsiddhu2/reseat
claude plugin install reseat-demo@reseat     # or: claude plugin install reseat@reseat
```

In a session, the same commands start with `/plugin`. Checked: `reseat-demo` installs this way and its server connects.

### Kiro

1. Clone this repository.
2. Open the Powers panel and choose Add Custom Power.
3. Import a power from a folder, and pick `plugins/kiro/reseat-demo` or `plugins/kiro/reseat`.

The files follow Kiro's Agent Plugins format and pass its published schemas.

- **Checked on 26 October:** a copy of `plugins/kiro/reseat-demo`, placed at a path with no spaces, imported this way, and its `check_plan` tool answered in Kiro chat.
- **A known refusal:** an import straight from a clone whose path held a folder name ending in a space was refused with "The selected folder is not a valid power directory". Whether the path or the folder picked caused it was not narrowed down. If you see that message, pick the folder holding `plugin.json`, or copy it to a plain path.
- The `reseat` power has not been imported.

## The tools

| Tool | What it does | Writes |
|---|---|---|
| `list_targets` | Your targets in priority order, and which are held now | Nothing |
| `search_sessions` | Searches the local catalog by text, type, day, venue, seat band, level, topic and service | Nothing, sends nothing to AWS |
| `open_slots(day)` | Reads your schedule once and lists the free gaps between 08:00 and 18:00, with the sessions you can walk to and away from in time | Nothing |
| `check_plan(codes)` | Up to 25 session codes, each checked against what you hold and the rest of the list | Nothing |
| `draft_rules(add, remove, autopilot)` | Drafts a new rules file | A plan id |
| `propose_changes` | Plans the reservations the rules ask for | A plan id |
| `propose_swap(held_code)` | Plans moving a held talk to an open sitting your rules put before the one you hold | A plan id |
| `propose_favorites(prune)` | Plans adding your target sittings to your official AWS favorites | A plan id |
| `guard_sync` | Plans leave-now block changes | A plan id |
| `approve_changes(plan_id)` | Carries out one plan, once, within 10 minutes | Only what the plan named |
| `explain_drift` | What changed in the catalog since the last sweep, or over the last hours | Nothing |
| `queue_or_go(code)` | Whether to queue for a session, by session type and band history. A heuristic | Nothing |

## Planning with your AI

**`check_plan(codes)`** states, for each code, whether it fits what you hold and the rest of the list:

- clashes, and two sittings of one talk
- walks that miss the 11-minute arrive-by rule (AWS closes the doors 10 minutes before the start)
- meals, `max_per_day` and `watch_cap`
- a full sitting lists the other open sittings of the talk

**`draft_rules(add, remove)`** drafts a new rules file:

- The codes in `add` go after your current targets in the order given, and the answer shows the whole target order.
- re:Seat tries targets in that order, so an earlier target wins a clashing time slot.
- An exact code such as AIM341-R1 pins that sitting. A code without -R such as AIM341 takes any sitting, so a repeat AWS adds later can still be booked.
- Removing a target you hold only stops re:Seat watching it, and never cancels the seat.
- Drafted targets ask first before any cancel.

**Autopilot is opt-in per target.** `draft_rules(add, remove, autopilot)` turns on `auto_swap` only for the codes in `autopilot`:

- Each one is added in the same draft or is already a target. For an existing target only its `auto_swap` changes.
- Any other code in `autopilot` is refused and nothing is drafted.
- The answer names every target that may swap on its own. Such a swap cancels a seat only after a fresh read shows the new seat open and a way back to the old one.
- The AI asks you before it sets `autopilot`.

**Approving the draft** writes only your local rules file, keeps the old one as `rules.yaml.bak`, and sends nothing to AWS. Comments and settings in the file stay as they were. Once `reseat serve` runs with the new rules, the watcher books a new target when a seat frees, as it does for every target.

The planning tools list in start-time order, or in the order you gave. re:Seat lists and checks, and leaves the choosing to you.

## Every change is two steps

`propose_changes`, `propose_swap`, `propose_favorites`, `guard_sync` or `draft_rules` returns a plan id and sends nothing. `approve_changes` carries that plan out once, within 10 minutes, and reserves only what it named.

**Favorites.** `propose_favorites(prune)` lists what `reseat favorites sync --dry-run --no-prune` would send: each target sitting not yet in your official AWS favorites, by session code and title.

- With `prune` on (off by default), it also lists the favorites re:Seat itself added for a target no longer in your rules.
- A favorite you added, or one for a seat you hold, is never listed.
- If nothing would change, it says so and makes no plan.
- Approving it writes only favorites, never a seat: AssociateFavorites in batches of up to 10 within the quota left, DisassociateFavorite for each removal, then one GetSchedule read-back.
- It sends only the sessions the plan listed, and skips any that a fresh read shows are done already.
- The answer lists each as added, already favorited, failed, unconfirmed or not sent, as the CLI does. A failed session is never sent again. A 409 stops the run, and a 429 waits Retry-After once.

**Swaps.** `propose_swap(held_code)` finds an open sitting of a held talk that your rules put before the one you hold, runs the swap's checks with fresh reads, and lists them. Approving it runs the same checked swap as `reseat swap`.

**Leave-now blocks.** A leave-now plan is refused if what you hold changed after it was shown.

**Safety rules for every call:**

- Calls run one at a time. No tool takes a list of session ids.
- Each call reads the rules file again if it changed on disk since the last call, so a server left running for days answers from the file as it is now. Any change drops the plans made before it.
- A rules file that does not load, has more targets than `watch_cap`, is a symbolic link, or on macOS and Linux belongs to another user or is writable by others, is refused. The old rules stay, and the next answer says why.

## Try it without signing in

`reseat mcp --demo` serves the same tools on the demo's fake API, with a week of sessions held and a seat open, so a client can list targets, plan and approve leave-now blocks, ask queue-or-go, and draft a rules file. The demo's rules file is a throwaway copy in a temporary folder. Nothing reaches AWS.

`python demo/plan.py` runs the four planning tools in the order an AI app would call them.

## Know the limit

The server tells the agent to ask you before approving, but it cannot check that it did. The agent sees the plan id and could approve on its own. Session titles come from the catalog and reach the agent. Use a client that shows you each tool call before it runs.

[Back to the README](../../README.md)
