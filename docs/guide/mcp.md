# MCP server

`reseat mcp` runs a local MCP server over stdio, so an agent can read your targets and plan changes for you. Your AI app starts it as a child process on your machine. Nothing is hosted.

**Install it once, with the MCP extra, and sign in:**

```bash
pipx install "reseat[mcp]"     # or pip. Already installed without it: pipx install --force "reseat[mcp]"
reseat login                   # in a terminal, once. The token goes to the system keychain
which reseat                   # the full path for the config below. On Windows: where reseat
```

**Add it to your AI app.** Most apps take the same entry. Use the full path it printed, because an app often does not see your shell's PATH:

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

Use the file for every project where you can. A project file holds only a path, but that path names your home folder, so keep it out of commits others see. Restart or reload the app after editing. It should list the reseat server with 12 tools. The four planning tools need re:Seat 0.7.0 or newer, the `autopilot` list in `draft_rules` needs 0.8.0, and `propose_favorites` needs 0.8.2. Version 0.6.1 has the other 7. Start with `reseat-demo`: it needs no sign-in and nothing reaches AWS. Use an app that shows you each tool call before it runs, because the agent could approve a plan without asking you (see the limit below).

**Without installing.** If you use [uv](https://docs.astral.sh/uv/), an app can run the server straight from PyPI: `"command": "uvx", "args": ["--python", "3.12", "--from", "reseat[mcp]==0.8.2", "reseat", "mcp"]`. `--python` picks an interpreter re:Seat supports, since uv may otherwise pick an older one on your path. Pin the version, so a new release never runs with your token before you choose it. You still sign in once, with `uvx --python 3.12 --from "reseat[mcp]==0.8.2" reseat login`. The watcher runs as `reseat serve` in a terminal, so most people install it.

**As a plugin.** This repository is also a Claude Code plugin marketplace and holds two Kiro Powers. Each comes in two forms. `reseat-demo` uses the demo's fake API and needs no sign-in. `reseat` is for your own seats. It needs a rules file, or the server will not start, and a sign-in, or each tool answers "Not signed in". Set both up first with the steps in [Install](../../README.md#install), running each command as `uvx --python 3.12 --from "reseat[mcp]==0.8.2" reseat ...` if you have not installed re:Seat. Both forms run the pinned `reseat[mcp]==0.8.2` through `uvx`, so [uv](https://docs.astral.sh/uv/) must be on your path.

- Claude Code: `claude plugin marketplace add hsiddhu2/reseat`, then `claude plugin install reseat-demo@reseat` or `claude plugin install reseat@reseat`. In a session, the same commands start with `/plugin`. Checked: `reseat-demo` installs this way and its server connects.
- Kiro: clone this repository, open the Powers panel, choose Add Custom Power, then import a power from a folder, and pick `plugins/kiro/reseat-demo` or `plugins/kiro/reseat`. The files follow Kiro's Agent Plugins format and pass its published schemas. Checked on 26 October: a copy of `plugins/kiro/reseat-demo`, placed at a path with no spaces, imported this way, and its `check_plan` tool answered in Kiro chat. An import straight from a clone whose path held a folder name ending in a space was refused with "The selected folder is not a valid power directory". Whether the path or the folder picked caused it was not narrowed down. If you see that message, pick the folder holding `plugin.json`, or copy it to a plain path. The `reseat` power has not been imported.

Tools: `list_targets`, `propose_changes`, `propose_swap`, `propose_favorites`, `approve_changes`, `explain_drift`, `guard_sync`, `queue_or_go`, and for planning `search_sessions`, `open_slots`, `check_plan` and `draft_rules`. `search_sessions` searches the local catalog by text, type, day, venue, seat band, level, topic and service, and sends nothing to AWS. `open_slots(day)` reads your schedule once and lists the free gaps between 08:00 and 18:00, with the sessions you can walk to and away from in time. `check_plan(codes)` takes up to 25 session codes and states, for each, whether it fits what you hold and the rest of the list: clashes, two sittings of one talk, walks that miss the 11-minute door rule, meals, `max_per_day` and `watch_cap`. A full sitting lists the other open sittings of the talk. `draft_rules(add, remove)` drafts a new rules file: the codes in `add` go after your current targets in the order given, and the answer shows the whole target order. re:Seat tries targets in that order, so an earlier target wins a clashing time slot. An exact code such as AIM341-R1 pins that sitting. A code without -R such as AIM341 takes any sitting, so a repeat AWS adds later can still be booked. Removing a target you hold only stops re:Seat watching it, and never cancels the seat. Drafted targets ask first before any cancel. Autopilot is opt-in per target. `draft_rules(add, remove, autopilot)` turns on `auto_swap` only for the codes in `autopilot`, each one added in the same draft or already a target. For an existing target only its `auto_swap` changes. Any other code in `autopilot` is refused and nothing is drafted. The answer names every target that may swap on its own. Such a swap cancels a seat only after a fresh read shows the new seat open and a way back to the old one. The AI asks you before it sets `autopilot`. Approving the draft writes only your local rules file, keeps the old one as `rules.yaml.bak`, and sends nothing to AWS. Comments and settings in the file stay as they were. Once `reseat serve` runs with the new rules, the watcher books a new target when a seat frees, as it does for every target. The planning tools list in start-time order, or in the order you gave. re:Seat lists and checks, and leaves the choosing to you. Every change is two steps. `propose_changes`, `propose_swap`, `propose_favorites` or `guard_sync` returns a plan id and sends nothing. `propose_favorites(prune)` lists what `reseat favorites sync --dry-run` would send: each target sitting not yet in your official AWS favorites, by session code and title. With `prune` on (off by default), it also lists the favorites re:Seat itself added for a target no longer in your rules. A favorite you added, or one for a seat you hold, is never listed. If nothing would change, it says so and makes no plan. Approving it writes only favorites, never a seat: AssociateFavorites in batches of up to 10 within the quota left, DisassociateFavorite for each removal, then one GetSchedule read-back. It sends only the sessions the plan listed, and skips any that a fresh read shows are done already. The answer lists each as added, already favorited, failed, unconfirmed or not sent, as the CLI does. A failed session is never sent again. A 409 stops the run, and a 429 waits Retry-After once. `propose_swap(held_code)` finds an open sitting of a held talk that your rules put before the one you hold, runs the swap's checks with fresh reads, and lists them. Approving it runs the same checked swap as `reseat swap`. `approve_changes` carries that plan out once, within 10 minutes, and reserves only what it named. A leave-now plan is refused if what you hold changed after it was shown. Calls run one at a time. No tool takes a list of session ids. Each call reads the rules file again if it changed on disk since the last call, so a server left running for days answers from the file as it is now. Any change drops the plans made before it. A file that does not load, has more targets than `watch_cap`, is a symbolic link, or on macOS and Linux belongs to another user or is writable by others, is refused, the old rules stay, and the next answer says why.

Try it without signing in: `reseat mcp --demo` serves the same tools on the demo's fake API, with a week of sessions held and a seat open, so a client can list targets, plan and approve leave-now blocks, ask queue-or-go, and draft a rules file. The demo's rules file is a throwaway copy in a temporary folder. Nothing reaches AWS. `python demo/plan.py` runs the four planning tools in the order an AI app would call them.

Know the limit: the server tells the agent to ask you before approving, but it cannot check that it did. The agent sees the plan id and could approve on its own. Session titles come from the catalog and reach the agent. Use a client that shows you each tool call before it runs.

[Back to the README](../../README.md)
