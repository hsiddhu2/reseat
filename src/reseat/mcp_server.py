"""Local MCP server over stdio, so an agent can drive re:Seat. The agent proposes, the human approves.

Rules this module exists to respect:
- No tool takes a list of session ids to reserve or cancel. Writes happen only
  through `approve_changes(plan_id)`, for a plan that `propose_changes`,
  `propose_swap`, `propose_favorites` or `guard_sync` returned. Plan ids are random, expire after
  10 minutes, work once.
- Approving a booking plan reserves only the sessions that plan named, re-checked
  against the schedule at that moment, through the router: quota-sized batches,
  fallback, read-back, journal.
- A swap plan runs only through swap.py, which reads both sessions fresh again,
  needs a fallback for the held seat, takes its cross-process lock, and journals
  every state. Proposing runs the same checks and sends no write.
- A favorites plan runs only through favorites.py, the engine `reseat favorites sync` uses:
  AssociateFavorites counts each session against its quota, so batches never exceed what is left.
  It is not idempotent, so a failed session is never re-sent, and GetSchedule is read back after.
  Approving sends only the sessions the plan named, re-checked against a fresh schedule read.
- Nothing here prints to stdout. On stdio, stdout is the protocol.
- The SDK runs tool calls in parallel. One lock makes them run one at a time, so
  two approvals cannot both read the schedule before either writes.
- The approval step is an instruction to the agent, not a gate the server can
  enforce: the agent sees the plan id and could approve on its own. Session
  titles come from the catalog and reach the agent's context. Say so to users.
- The access token never appears in a tool result.

The tool logic is in `Tools`, so it is tested without the MCP SDK. The SDK is an
optional extra: pip install "reseat[mcp]".
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import favorites, guard, planning
from . import rules as R
from .client import ApiError, EventsClient, OperationClosed
from .models import Session
from .router import WRITES_CLOSED, Router, run_booking, target_name
from .rules import Rules, base_code
from .store import Store
from .swap import Swap, SwapBusy, SwapResult

PLAN_TTL = 600


@dataclass
class Plan:
    plan_id: str
    kind: str                       # book | guard | swap | rules | favorites
    expires: float
    sessions: list[str] = field(default_factory=list)
    summary: str = ""
    shown: tuple[tuple[str, ...], ...] = ()     # the guard diff the attendee saw
    draft: planning.Draft | None = None         # rules: the file to write, and the file it came from
    removals: list[str] = field(default_factory=list)   # favorites: re:Seat's own favorites to remove
    prune: bool = False                                 # favorites: whether removals were asked for


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class Tools:
    def __init__(self, client: EventsClient, store: Store, rules: Rules, event_id: str,
                 clock: Callable[[], float] = time.time, rules_path: Path | None = None):
        self.client, self.store, self.rules, self.event_id, self.clock = client, store, rules, event_id, clock
        self.rules_path = rules_path
        self.router = Router(rules, store, event_id)
        self._plans: dict[str, Plan] = {}
        self._lock = threading.Lock()
        self.serial = threading.RLock()     # every tool call holds this: one at a time
        self._stamp = self._rules_stamp()

    def _rules_stamp(self) -> tuple[int, int, int, int] | None:
        try:
            st = self.rules_path.stat() if self.rules_path else None
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode) if st else None

    def refresh(self) -> str | None:
        """An MCP server can run for days. The rules file is the attendee's instructions, so when it
        changes on disk every tool answers from the new file. Any change drops pending plans: a swap
        for a target no longer wanted could cancel a seat. A link, a file others can change, or a file
        that does not load keeps the old rules, as the watcher does, and the next answer says why."""
        stamp = self._rules_stamp()
        if stamp is None or stamp == self._stamp or self.rules_path is None:
            return None
        self._stamp = stamp
        with self._lock:
            self._plans = {}
        path = self.rules_path
        if unsafe := R.unsafe_file(path):
            return f"The rules file changed but {unsafe}, so the old rules stay."
        try:
            new = R.parse(R.read_nofollow(path), str(path))
        except (R.RulesError, OSError, ValueError) as e:
            return (f"The rules file changed but has a problem, so the old rules stay: "
                    f"{planning._plain(e, path)}")
        self.rules, self.router = new, Router(new, self.store, self.event_id)
        warn = R.exposure_warning(path, new)
        return planning._plain(ValueError(warn), path) if warn else None

    def _new_plan(self, kind: str, sessions: list[str], summary: str,
                  shown: tuple[tuple[str, ...], ...] = ()) -> Plan:
        p = Plan(secrets.token_urlsafe(16), kind, self.clock() + PLAN_TTL, sessions, summary, shown)
        with self._lock:
            self._plans = {k: v for k, v in self._plans.items() if v.expires > self.clock()}
            self._plans[p.plan_id] = p
        return p

    def _target(self, label: str) -> str:
        return planning.clean(target_name(label, self.store, self.event_id), 60)

    def _name(self, sid: str) -> str:
        """Code and title only. A session no longer in the catalog is named without its id."""
        s = self.store.get(self.event_id, sid)
        if not s:
            return "a session no longer in the local catalog"
        return f"{planning.clean(s.abbreviation, 20)} {planning.clean(s.title, 160)}"

    def _code(self, sid: str) -> str:
        s = self.store.get(self.event_id, sid)
        if not s:
            return sid
        st = s.session_time
        when = f"{st.date} {st.time}" if st and st.date else "time not set"
        return (f"{planning.clean(s.abbreviation, 20)} {planning.clean(s.title, 160)} "
                f"({when}, {planning.clean(s.campus_venue, 40) or 'venue not set'})")

    # ---- reads

    def list_targets(self) -> str:
        held = set(self.client.get_schedule(self.event_id).reserved)
        lines = []
        for i, t in enumerate(self.rules.targets, 1):
            tree = self.router.tree(t)
            mine = [s for s, _ in tree if s.session_id in held]
            own, backup = sum(1 for _, b in tree if not b), sum(1 for _, b in tree if b)
            state = (f"held: {self._code(mine[0].session_id)}" if mine
                     else f"not held, {_count(own, 'sitting')} known"
                     + (f", plus {_count(backup, 'backup sitting')}" if backup else ""))
            lines.append(f"{i}. {self._target(t.label)}{' (auto swap)' if t.auto_swap else ''}: {state}")
        return "\n".join(lines) or "The rules file has no targets."

    def explain_drift(self, hours: int = 0) -> str:
        """Changes found by the last sweep, or over the last `hours` when given."""
        if hours > 0:
            since, span = self.clock() - min(hours, 24 * 14) * 3600, f"the last {hours} hours"
        else:
            since, span = self.store.last_sweep(self.event_id) or 0.0, "the last sweep"
        rows = self.store.recent_changes(self.event_id, since)
        if not rows:
            return f"No catalog changes in {span}."
        counts: dict[str, int] = {}
        lines = []
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
            if r["kind"] in ("added", "moved") and len(lines) < 25:
                lines.append(f"- {r['kind']}: {self._code(r['session_id'])}")
        head = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
        return f"In {span}: {head}.\n" + "\n".join(lines)

    def queue_or_go(self, code: str) -> str:
        sittings = self.store.by_base_code(self.event_id, base_code(code))
        if not sittings:
            return f"{code} is not in the local catalog. Run reseat sync."
        out = []
        for s in sittings:
            a = guard.queue_or_go(s, self.store.band_history(self.event_id, s.session_id))
            out.append(f"{self._code(s.session_id)}: {a.verdict}. {a.basis}")
        return "\n".join(out)

    # ---- planning: lists and checks, never a ranking (D-018)

    def search_sessions(self, text: str = "", type: str = "", day: str = "", venue: str = "", band: str = "",
                        level: str = "", topic: str = "", service: str = "",
                        limit: int = planning.DEFAULT_ROWS) -> str:
        f = planning.Filters(text, type, day, venue, band, level, topic, service)
        return _guarded(lambda: planning.search_sessions(self.store, self.event_id, f, limit,
                                                         targets=self._target_ids(),
                                                         talks=planning.watched_talks(self.rules, self.store,
                                                                                      self.event_id)))

    def open_slots(self, day: str, type: str = "", text: str = "", band: str = "open", level: str = "",
                   topic: str = "", service: str = "", venue: str = "") -> str:
        f = planning.Filters(text, type, "", venue, band, level, topic, service)
        return _guarded(lambda: planning.open_slots(self.client, self.store, self.event_id, self.rules,
                                                    day, f))

    def check_plan(self, codes: list[str]) -> str:
        return _guarded(lambda: planning.check_plan(self.client, self.store, self.event_id, self.rules,
                                                    list(codes)))

    def draft_rules(self, add: list[str], remove: list[str] | None = None,
                    autopilot: list[str] | None = None) -> str:
        if self.rules_path is None:
            return "This server has no rules file to edit. Nothing drafted."
        path = self.rules_path
        got = _guarded_any(lambda: planning.draft_rules(self.client, self.store, self.event_id, path,
                                                        list(add), list(remove or []), list(autopilot or [])))
        if not isinstance(got, planning.Draft):
            return got
        p = self._new_plan("rules", [], got.summary)
        p.draft = got
        return (f"Plan {p.plan_id}, valid 10 minutes.\n{got.summary}\nNothing has been written. Approving "
                "writes the rules file and keeps the old one as rules.yaml.bak. It sends nothing to AWS. "
                "Ask the attendee, then call approve_changes with this plan id.")

    def _target_ids(self) -> set[str]:
        return {s.session_id for t in self.rules.targets for s, _ in self.router.tree(t)}

    # ---- proposals: no writes

    def propose_changes(self) -> str:
        held = self.client.get_schedule(self.event_id).reserved
        plan = self.router.plan(held, quota_left=self.client.quota.remaining("ReserveSessions"))
        if plan.blocked:
            return f"Nothing planned. {plan.blocked}"
        chosen = plan.batch + plan.deferred
        if not chosen:
            why = "; ".join(f"{self._target(s.target)}: {s.reason}" for s in plan.skipped[:10])
            return "Nothing to reserve now." + (f" Skipped: {why}." if why else "")
        lines = [f"- {self._code(p.session_id)} for target {self._target(p.target)}" for p in chosen]
        p = self._new_plan("book", [x.session_id for x in chosen], "\n".join(lines))
        return (f"Plan {p.plan_id}, valid 10 minutes. Reserve {len(chosen)}:\n{p.summary}\n"
                "Nothing has been sent. Ask the attendee, then call approve_changes with this plan id.")

    def guard_sync(self) -> str:
        gp = guard.compute(self.client, self.store, self.event_id, self.rules)
        if gp.empty:
            return "Leave-now blocks already match what is held. Nothing to change."
        lines = ([f"- create {b.title} at {b.start}Z" for b in gp.create]
                 + [f"- update {b.title} to {b.start}Z" for _, b in gp.update]
                 + [f"- delete {p.title}" for p in gp.delete])
        p = self._new_plan("guard", [], "\n".join(lines), _diff(gp))
        return (f"Plan {p.plan_id}, valid 10 minutes. Leave-now changes:\n{p.summary}\n"
                "Nothing has been sent. Call approve_changes with this plan id to apply.")

    def propose_favorites(self, prune: bool = False) -> str:
        """What `reseat favorites sync --dry-run` would send, as a plan. Reads GetSchedule once and
        sends no write. With `prune`, favorites re:Seat itself added for a dropped target are listed
        for removal. A favorite the attendee added, or one for a held seat, is never listed. Nothing is
        sent until approve_changes with this plan id. Ask the attendee first."""
        try:
            res = favorites.sync(self.client, self.store, self.event_id, self.rules, dry_run=True,
                                 prune=prune)
        except ApiError as e:
            return f"No favorites plan. A read failed: {planning.clean(str(e), 300)}. Nothing was sent."
        adds = [o for o in res.outcomes if o.status == "not_sent"]
        notes = _favorite_notes(res, self._target)
        if not adds and not res.removals:
            return ("Favorites already match the rules' targets. Nothing to change, no plan made."
                    + ("\n" + notes if notes else ""))
        lines = ([f"- add {self._name(o.session_id)} for target {self._target(o.target)}" for o in adds]
                 + [f"- remove {self._name(o.session_id)}, which re:Seat added for a target no longer "
                    "in the rules" for o in res.removals])
        p = self._new_plan("favorites", [o.session_id for o in adds], "\n".join(lines))
        p.removals, p.prune = [o.session_id for o in res.removals], prune
        return (f"Plan {p.plan_id}, valid 10 minutes. Favorites: add {len(adds)}, remove "
                f"{len(res.removals)}, {res.count('already')} already favorited.\n{p.summary}\n"
                + (notes + "\n" if notes else "")
                + "Nothing has been sent. Approving adds these to the official favorites in batches within "
                "the quota and reads the schedule back. It never reserves or cancels a seat. Ask the "
                "attendee, then call approve_changes with this plan id.")

    def propose_swap(self, held_code: str) -> str:
        """An open sitting of a held talk that comes earlier in the rules' order, checked as swap.py
        checks. Sends nothing."""
        try:
            return self._propose_swap(held_code)
        except ApiError as e:
            return f"No swap planned. A read failed: {e}. Nothing was sent."

    def _propose_swap(self, held_code: str) -> str:
        held = self.client.get_schedule(self.event_id).reserved
        code = held_code.strip().upper()
        mine = [s for s in (self.store.get(self.event_id, i) for i in held) if s]
        a = next((s for s in mine if (s.abbreviation or "").upper() == code), None) or next(
            (s for s in mine if s.base_code and s.base_code == base_code(code)), None)
        if a is None:
            return f"Refused: {held_code} is not held. Nothing was planned."
        trees = ({x.session_id for x, _ in self.router.tree(t)} for t in self.rules.targets)
        if not any(a.session_id in ids for ids in trees):
            return f"{a.abbreviation} is not a target in the rules file, so no sitting comes before it."
        before = self._preferred(a)
        earlier = [s for s in before if s.band and s.band.open and s.session_id not in held]
        if not earlier:
            code = planning.clean(a.abbreviation, 20)
            if not before:
                return (f"The rules list no sitting of this talk before {code}, so there is nothing to "
                        "move to. Nothing was planned.")
            names = ", ".join(planning.clean(s.abbreviation, 20) for s in before)
            return (f"The rules list {names} before {code}, and none had an open seat at the last sync. "
                    "Nothing was planned.")
        swap, reasons = Swap(self.client, self.store, self.rules, self.event_id), []
        for b in earlier:
            res = swap.check(a.session_id, b.session_id, approved=True)
            if not res.reasons:
                p = self._new_plan("swap", [a.session_id, b.session_id],
                                   f"Cancel {self._code(a.session_id)}, reserve {self._code(b.session_id)}, "
                                   "read back.")
                swap.end_proposal(res, "planned", {"plan": "waiting for approve_changes"})
                fb = ", ".join(self._code(f) for f in res.fallbacks)
                return (f"Plan {p.plan_id}, valid 10 minutes. Swap:\n- {p.summary}\nChecks, read fresh now: "
                        f"{b.abbreviation} band {b.seat_availability}, no clash with anything else held, "
                        f"fallback if it fails: {fb}.\nIf {b.abbreviation} is refused, {a.abbreviation} is "
                        "reserved again. If writes close or a read-back fails, nothing more is sent.\n"
                        "Nothing has been sent. Ask the attendee, then call approve_changes with this "
                        "plan id.")
            swap.end_proposal(res, "declined", {"preconditions": res.reasons})
            reasons += [f"{b.abbreviation}: {r}" for r in res.reasons]
        return "No swap planned. The checks failed:\n" + "\n".join(f"- {r}" for r in reasons)

    def _preferred(self, a: Session) -> list[Session]:
        """Sittings of a's talk the router would pick before a: the target's own order."""
        for t in self.rules.targets:
            order = [s for s, backup in self.router.tree(t) if not backup and s.base_code == a.base_code]
            ids = [s.session_id for s in order]
            if a.session_id in ids:
                return order[:ids.index(a.session_id)]
        return []

    # ---- the only write

    def approve_changes(self, plan_id: str) -> str:
        with self._lock:
            p = self._plans.pop(plan_id, None)
        if p is None or p.expires <= self.clock():
            return ("Refused: unknown, used or expired plan id. Call propose_changes, propose_swap, "
                    "propose_favorites, guard_sync or draft_rules first.")
        try:
            if p.kind == "swap":
                return self._swap(p)
            if p.kind == "rules":
                return self._write_rules(p)
            if p.kind == "favorites":
                return self._favorites(p)
            return self._book(p) if p.kind == "book" else self._guard(p)
        except SwapBusy as e:
            return f"Not run: {e} Nothing was sent. Propose again in a moment."
        except OperationClosed:
            return WRITES_CLOSED
        except ApiError as e:
            return (f"Stopped on an API error: {planning.clean(str(e), 300)}. Check the schedule before "
                    "trying again.")

    def _book(self, p: Plan) -> str:
        held = self.client.get_schedule(self.event_id).reserved
        run = run_booking(self.router, self.client, self.store, self.event_id, held, candidates=p.sessions)
        if run.closed:
            return WRITES_CLOSED
        lines = [f"- {o.status}: {self._code(o.session_id)}{' ' + o.code if o.code else ''}"
                 + (f" ({o.note})" if o.note else "") for o in run.outcomes]
        errors = [e.error for e in run.executions if e.error]
        if run.schedule is None:
            return ("Sent, but the schedule could not be read back, so nothing is confirmed:\n"
                    + "\n".join(lines) + "\nCheck the schedule before trying again.")
        return ("Done. Read back from the schedule:\n" + "\n".join(lines)
                + f"\nThe schedule now lists {len(run.schedule.reserved)} reserved sessions."
                + (f"\nErrors: {'; '.join(errors)}" if errors else ""))

    def _favorites(self, p: Plan) -> str:
        """One engine run, limited to the plan's sessions: quota-sized batches, no re-send of a failed
        session, one GetSchedule read-back. Reports as `reseat favorites sync` does."""
        res = favorites.sync(self.client, self.store, self.event_id, self.rules, prune=p.prune,
                             add_only=set(p.sessions), remove_only=set(p.removals))
        planned = set(p.sessions)
        mine = [o for o in res.outcomes if o.session_id in planned]
        skipped = (len(planned - {o.session_id for o in mine})
                   + len(set(p.removals) - {o.session_id for o in res.removals}))
        add_word = {"not_sent": "not sent", "already": "already favorited"}
        rm_word = {"gone": "already gone", "not_sent": "not sent"}
        lines = [f"- {add_word.get(o.status, o.status)}: {self._name(o.session_id)}"
                 + (f" ({planning.clean(o.code, 40)})" if o.code else "") for o in mine]
        lines += [f"- {rm_word.get(o.status, o.status)} from favorites: {self._name(o.session_id)}"
                  for o in res.removals]
        n = {k: sum(1 for o in mine if o.status == k) for k in ("added", "already", "failed", "unconfirmed")}
        head = (f"{_count(len(mine), 'sitting')} planned: {n['added']} added, {n['already']} already "
                f"favorited, {n['failed']} failed, {n['unconfirmed']} unconfirmed.")
        if res.removals:
            head += f" {res.removed('removed')} of {len(res.removals)} removed."
        out = [favorites.CLOSED] if res.closed else []
        if res.closed or (res.error and not res.wrote):
            out.append("Stopped before every planned change was sent:")
        elif not res.wrote:
            out.append("Nothing was sent: the fresh read showed nothing left to change.")
        elif res.read_back:
            out.append("Done. Read back from the schedule:")
        elif res.error:
            out.append("Sent, but the schedule could not be read back, so nothing is confirmed:")
        else:
            out.append("Nothing changed. Already gone:")
        out += lines + [head]
        if skipped:
            out.append(f"{_count(skipped, 'planned change')} no longer needed by the fresh read, not sent.")
        if notes := _favorite_notes(res, self._target):
            out.append(notes)
        if res.disagreements:
            out.append(f"{len(res.disagreements)} answers disagreed with the read-back. They are shown as "
                       "unconfirmed or failed above, and none is sent again.")
        if res.error:
            out.append(f"Stopped: {planning.clean(res.error, 300)}. A failed session is never re-sent. "
                       "Check the schedule before proposing again.")
        return "\n".join(out)

    def _swap(self, p: Plan) -> str:
        held_id, wanted_id = p.sessions
        res: SwapResult = Swap(self.client, self.store, self.rules, self.event_id).run(
            held_id, wanted_id, approved=True)
        lines = [f"Swap {res.state}: {' -> '.join(res.steps)}."]
        lines += [f"Not run: {r}" for r in res.reasons]
        if res.alert:
            lines.append(res.alert)
        lines.append("Held now: " + (", ".join(self._code(s) for s in res.held_now) or "nothing") + ".")
        return "\n".join(lines)

    def _write_rules(self, p: Plan) -> str:
        """Write the drafted rules file, only if the file is still the one it was drafted from. The old
        file is kept beside it. No API call: the rules file only says what to watch and book."""
        path, d = self.rules_path, p.draft
        if path is None or d is None:
            return "Refused: this plan has no rules file. Nothing written."
        try:
            R.refuse_symlink(path)
            now = R.read_nofollow(path)
            if planning._sha(now) != d.base_sha:
                return ("Refused: the rules file changed after this draft was made. Nothing written. "
                        "Call draft_rules again.")
            backup = path.with_name(path.name + ".bak")
            R.write_new(backup, now, replace=True)
            R.write_new(path, d.text, replace=True)
            self.rules = R.parse(d.text)
            self._stamp = self._rules_stamp()   # our own write: nothing to reload
            with self._lock:
                self._plans = {}                 # made under the old rules, like any change
        except Exception as e:      # noqa: BLE001  any failure: say so, never with a path or a trace
            msg = planning._plain(e, path) if isinstance(e, R.RulesError) else "the file could not be written"
            return f"Not written: {msg}. The rules file is as it was, or check rules.yaml.bak."
        self.router = Router(self.rules, self.store, self.event_id)
        auto = [self._target(t.label) for t in self.rules.targets if t.auto_swap]
        return (f"Rules file written with {len(self.rules.targets)} targets. The old file is kept as "
                f"{backup.name}. Nothing was sent to AWS by this tool. A running reseat serve uses the new "
                "rules from its next sweep: it books every target that has an open seat now, then books "
                "or proposes as seats free. "
                + (f"These targets swap on their own: {', '.join(auto)}. " if auto else
                   "Every swap asks first. ")
                + "Run reseat book --dry-run to see what it would reserve now.")

    def _guard(self, p: Plan) -> str:
        now = guard.compute(self.client, self.store, self.event_id, self.rules)
        if _diff(now) != p.shown:
            return ("Refused: the leave-now changes are no longer what was shown, because what is held "
                    "changed. Call guard_sync again and show the new plan.")
        res = guard.sync(self.client, self.store, self.event_id, self.rules)
        out = "\n".join(f"- {d}" for d in res.done) or "- nothing sent"
        probs = "\n".join(f"Problem: {x}" for x in res.problems)
        return f"Leave-now blocks:\n{out}" + (f"\n{probs}" if probs else "")


def _favorite_notes(res: favorites.FavoritesSync, name: Callable[[str], str]) -> str:
    notes = [f"{name(m)}: not in the local catalog. Run reseat sync." for m in res.missing]
    if res.prune_skipped:
        notes.append("No favorite is listed for removal, because a target is missing from the local "
                     "catalog.")
    if res.kept_held:
        notes.append(f"{_count(len(res.kept_held), 'favorite')} re:Seat added for dropped targets "
                     "kept, because a seat is held there.")
    return "\n".join(notes)


def _guarded_any(run: Callable[[], Any]) -> Any:
    try:
        return run()
    except Exception:
        return "Could not answer: re:Seat hit an internal error. Nothing was sent or changed."


def _guarded(run: Callable[[], str]) -> str:
    """A planning read that fails says so in a fixed sentence. The exception text can carry a
    local path or a database message, and it would land in the AI's context."""
    try:
        return run()
    except Exception:
        return "Could not answer: re:Seat hit an internal error. Nothing was sent or changed."


def _diff(gp: guard.GuardPlan) -> tuple[tuple[str, ...], ...]:
    return (tuple(f"+{b.title}@{b.start}" for b in gp.create)
            + tuple(f"~{pid}:{b.title}@{b.start}" for pid, b in gp.update)
            + tuple(f"-{p.personal_time_id}" for p in gp.delete),)


def _noted(note: str | None, answer: str) -> str:
    """A refused or exposed rules file is said before the answer, once per change."""
    return f"{note}\n{answer}" if note else answer


def build_server(tools: Tools) -> Any:
    """The MCP server. Imports the SDK here so the core install does not need it."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("reseat", instructions=(
        "re:Seat manages AWS re:Invent seats for one attendee. Read tools are safe. "
        "Every change is two steps: propose_changes, propose_swap, propose_favorites, guard_sync or "
        "draft_rules returns a plan id and changes nothing. "
        "Show the plan to the attendee. Only after they agree, call approve_changes with that plan id. "
        "re:Seat lists and checks. Choosing is for the attendee: give the facts and let the attendee "
        "pick and order the sessions."))

    @server.tool()
    def list_targets() -> str:
        """The attendee's targets in priority order and which are held now."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.list_targets())

    @server.tool()
    def propose_changes() -> str:
        """Plan the reservations the rules ask for. Returns a plan id. Sends nothing."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.propose_changes())

    @server.tool()
    def propose_swap(held_code: str) -> str:
        """Plan moving a held session to an open sitting of the same talk that its target lists
        earlier, by the rules' order (earliest or latest sitting first). Returns a plan id and the
        checks. Sends nothing."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.propose_swap(held_code))

    @server.tool()
    def propose_favorites(prune: bool = False) -> str:
        """Plan adding every target sitting in the rules to the official AWS favorites, as reseat
        favorites sync does. With prune, also remove favorites re:Seat added for targets no longer in
        the rules. A favorite the attendee added is never removed. Returns a plan id. Sends nothing.
        Nothing is sent until approve_changes with this plan id. Ask the attendee first."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.propose_favorites(prune))

    @server.tool()
    def approve_changes(plan_id: str) -> str:
        """Carry out a plan from propose_changes, propose_swap, propose_favorites, guard_sync or
        draft_rules, once, within 10 minutes. Ask the attendee first."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.approve_changes(plan_id))

    @server.tool()
    def explain_drift(hours: int = 0) -> str:
        """What changed in the catalog since the last sweep, or over the last `hours` if given."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.explain_drift(hours))

    @server.tool()
    def guard_sync() -> str:
        """Plan leave-now block changes. Returns a plan id. Sends nothing."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.guard_sync())

    @server.tool()
    def search_sessions(text: str = "", type: str = "", day: str = "", venue: str = "", band: str = "",
                        level: str = "", topic: str = "", service: str = "", limit: int = 25) -> str:
        """Sessions in the local catalog that pass every filter, in start-time order, at most 50.
        text matches title, code, abstract, speakers, topics and services. band is a seat band or
        "open". day is a weekday name or YYYY-MM-DD. Reads the local catalog only."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.search_sessions(text, type, day, venue, band, level, topic, service,
                                                      limit))

    @server.tool()
    def open_slots(day: str, type: str = "", text: str = "", band: str = "open", level: str = "",
                   topic: str = "", service: str = "", venue: str = "") -> str:
        """Free gaps in one day between 08:00 and 18:00, after held sessions and meals, and the
        sessions that fit each gap with the walk in and out, in start-time order. Reads the
        schedule once. Writes nothing."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.open_slots(day, type, text, band, level, topic, service, venue))

    @server.tool()
    def check_plan(codes: list[str]) -> str:
        """Facts about up to 25 session codes against what is held and against each other: in the
        catalog, seat band, clashes, two sittings of one talk, walks with the 11-minute door rule,
        meals, max_per_day and watch_cap. A base code such as AIM341 checks every sitting. An exact
        code such as AIM341-R1 checks that sitting. Reads the schedule once. Writes nothing."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.check_plan(codes))

    @server.tool()
    def draft_rules(add: list[str], remove: list[str] | None = None,
                    autopilot: list[str] | None = None) -> str:
        """Draft a new rules file: `add` codes go after the current targets in the order given, and
        `remove` codes are dropped. An earlier target wins a clashing time slot. An exact code such as
        AIM341-R1 pins that sitting. A code without -R such as AIM341 takes any sitting. Returns a plan
        id and the full target order. Writes nothing until approve_changes, then only the local rules
        file, with a .bak copy. Never reserves or cancels. Removing a held target stops watching it, never
        cancels. Drafted targets ask first before any cancel. `autopilot` is opt-in per target: codes,
        each in `add` or already a target, that get auto_swap on, so the watcher may cancel a held seat
        for an earlier target after a fresh read shows the new seat open and a way back. Ask the attendee
        before setting it, and name each code. Any other code in `autopilot` is refused."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.draft_rules(add, remove, autopilot))

    @server.tool()
    def queue_or_go(code: str) -> str:
        """Whether to queue for a session code, by session type and band history. A heuristic."""
        with tools.serial:
            note = tools.refresh()
            return _noted(note, tools.queue_or_go(code))

    return server
