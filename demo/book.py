"""Demo 1: book your targets in priority order, with a full session falling back.

    python demo/book.py
"""

from _harness import banner, catalog, run, setup, sid, step

RULES = """targets:
  - code: CON304      # a Builders' session
  - code: API303      # its first sitting fills in this demo
  - code: ARC302      # its first sitting already shows unavailable
  - code: ANT316
max_per_day: 6
"""

d = setup(RULES, catalog({
    "CON304-R": "limited", "CON304-R1": "available",
    "API303-R": "available", "API303-R1": "veryLimited",
    "ARC302-R": "unavailable", "ARC302-R1": "available",
    "ANT316-R": "available", "ANT316-R1": "available",
}))
d.fake.full.add(sid(d, "API303-R"))     # shows open, but the reserve comes back sessionFull

banner("re:Seat: book on the day writes open",
       "Four targets in priority order. One sitting fills as we book, one is already full.")
step("Check that every target resolves in the catalog")
run("sync", "--no-abstracts")
run("rules", "check")
step("Mirror the targets into favorites, so the official app shows the same plan")
run("favorites", "sync")
step("See the plan first. Nothing is sent")
run("book", "--dry-run")
step("Book. A full session falls back to its next sitting in the same run, and every write is read back")
run("book", "--yes")
step("What the schedule holds now")
run("schedule")
