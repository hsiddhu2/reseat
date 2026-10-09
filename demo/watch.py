"""Demo 2: the watcher catches a freed seat, then a newly added repeat sitting.

    python demo/watch.py
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from _harness import EV, banner, catalog, con, setup, step

from reseat import cli
from reseat import rules as R
from reseat.watcher import Watcher

RULES = "targets:\n  - code: ARC305\n  - code: CMP327\n"
d = setup(RULES, catalog({
    "ARC305-R": "unavailable", "ARC305-R1": "unavailable",
    "CMP327-R": "unavailable", "CMP327-R1": "unavailable",
}))


class Clock:
    t = datetime(2026, 10, 20, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


clock = Clock()
w = Watcher(d.client, d.store, R.load(cli.config.RULES_PATH), EV, clock=clock, sleep=clock.sleep)
w.subscribe(cli._print_watch_event)

banner("re:Seat: the watcher",
       "Both targets are full. Every sweep checks the whole catalog. Each line below is one sweep.")
step("The first sweep records the catalog")
w.tick()
step("A quiet minute")
clock.sleep(60)
w.tick()
step("Someone cancels: ARC305-R1 goes from unavailable to limited")
d.fake.set_band(next(s for s, x in d.fake.sessions.items() if x.abbreviation == "ARC305-R1"), "limited")
clock.sleep(60)
w.tick()
step("AWS adds a new sitting of CMP327 on Friday morning")
base = next(x for x in d.fake.sessions.values() if x.abbreviation == "CMP327-R")
d.fake.add_session(base.model_copy(update={
    "session_id": "demo-new-repeat", "abbreviation": "CMP327-R2", "seat_availability": "available",
    "session_time": base.session_time.model_copy(update={"date": "2026-12-04", "time": "09:00"})}))
clock.sleep(60)
w.tick()
con.print()
cli.schedule()
