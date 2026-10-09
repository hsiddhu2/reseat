"""Demo 3: a safe swap. First the wanted seat fills mid-swap and the original is restored,
then the same swap goes through.

    python demo/swap.py
"""

from _harness import banner, catalog, con, run, setup, sid, step

RULES = """targets:
  - code: SVS306      # wanted more
  - code: CMP409      # held now, at the same time
"""
sessions = catalog({
    "SVS306-R": "limited", "SVS306-R1": "unavailable",
    "CMP409-R": "available", "CMP409-R1": "available",
})
d = setup(RULES, sessions)
A, B = sid(d, "CMP409-R"), sid(d, "SVS306-R")
d.fake.schedule.reserved.add(A)

banner("re:Seat: safe swap",
       "CMP409-R is held. SVS306-R, a higher priority at the same time, shows open seats. "
       "The API has no swap: CMP409-R must be cancelled before SVS306-R can be reserved, "
       "and for that moment neither is held.")
con.print(f"[dim]reseat swap takes session ids. Here {A} is CMP409-R and {B} is SVS306-R.[/dim]")
run("sync", "--no-abstracts")
run("schedule")
step("Attempt 1. The last seat in SVS306-R goes to someone else between our cancel and our reserve")
d.fake.full.add(B)
run("swap", A, B, "--yes")
step("re:Seat rolled back: CMP409-R is held again")
run("schedule")
step("Attempt 2. This time the seat is still there")
d.fake.full.discard(B)
run("swap", A, B, "--yes")
run("schedule")
