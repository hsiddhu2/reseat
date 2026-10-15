"""Demo 4: plan with your own AI. The four planning tools the MCP server gives an AI app, in the
order an AI would call them, then the attendee's approval and the booking dry run.

    python demo/plan.py

re:Seat lists and checks. The choosing is the attendee's, through their AI app. Here the script
plays the AI's part with fixed calls, so the demo needs no AI app and no sign-in.
"""

from _harness import EV, banner, catalog, con, run, setup, step

from reseat import cli
from reseat import mcp_server as M
from reseat import rules as R

RULES = """# Tuesday so far: a workshop in the middle of the day, a chalk talk at four.
targets:
  - code: CON344
  - code: IND406
max_per_day: 5
home_venue: Caesars Forum
"""
d = setup(RULES, catalog({
    "CON344": "available", "IND406-R1": "limited",            # held
    "OPN309": "available", "IND311-R1": "limited",            # AgentCore builders' sessions
    "PEX304-R": "unavailable", "IND350-R": "available", "COP319-R": "available",
}), held=())
for code in ("CON344", "IND406-R1"):
    d.fake.schedule.reserved.add(next(s for s, x in d.fake.sessions.items() if x.abbreviation == code))
d.store.apply_sweep(EV, list(d.fake.sessions.values()), with_abstracts=False)
tools = M.Tools(d.client, d.store, R.load(cli.config.RULES_PATH), EV, rules_path=cli.config.RULES_PATH)


def ai(call: str, answer: str) -> None:
    con.print(f"[dim]AI calls[/dim] [bold]{call}[/bold]")
    con.print(answer, markup=False, highlight=False)


banner("re:Seat: plan with your own AI",
       "The attendee holds two Tuesday sessions at Caesars Forum and asks their AI for an AgentCore "
       "builders' session in between. re:Seat answers in start-time order and never ranks.")
step("The AI searches the local catalog. Nothing is sent to AWS.")
ai('search_sessions(text="agentcore", day="Tuesday", type="Builders\' session")',
   tools.search_sessions(text="agentcore", day="Tuesday", type="Builders' session"))
step("The AI asks what fits Tuesday, with the walks in and out")
ai('open_slots("Tuesday", type="Builders\' session")', tools.open_slots("Tuesday", type="Builders' session"))
step("The AI checks two sessions it is weighing, against what is held and each other")
ai('check_plan(["OPN309", "IND350-R"])', tools.check_plan(["OPN309", "IND350-R"]))
step("The attendee picks OPN309. The AI drafts the rules file. Nothing is written yet.")
draft = tools.draft_rules(["OPN309"])
ai('draft_rules(add=["OPN309"])', draft)
plan_id = draft.split("Plan ", 1)[1].split(",", 1)[0]
step("The attendee says yes. The AI approves the plan id. Only the local rules file changes.")
ai(f"approve_changes({plan_id[:6]}…)", tools.approve_changes(plan_id))
step("Booking is the attendee's own step, with a dry run first")
run("book", "--dry-run")
con.print("\n[dim]The line draft_rules added. Comments and settings are left as they were:[/dim]")
con.print("\n".join(ln for ln in cli.config.RULES_PATH.read_text().splitlines() if "draft_rules" in ln),
          markup=False, highlight=False)
