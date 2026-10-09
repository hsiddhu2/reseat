"""Local MCP server over stdio, so an agent can drive re:Seat. The agent proposes, the human approves.

Rules this module exists to respect:
- No tool takes a list of session ids to reserve or cancel. Writes happen only
  through `approve_changes(plan_id)`, for a plan that `propose_changes`,
  `propose_swap` or `guard_sync` returned. Plan ids are random, expire after 10 minutes, work once.
- Approving a booking plan reserves only the sessions that plan named, re-checked
  against the schedule at that moment, through the router: quota-sized batches,
  fallback, read-back, journal.
- A swap plan runs only through swap.py, which reads both sessions fresh again,
  needs a fallback for the held seat, takes its cross-process lock, and journals
  every state. Proposing runs the same checks and sends no write.
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
from typing import Any

from . import guard
from .client import ApiError, EventsClient, OperationClosed
from .models import Session
from .router import WRITES_CLOSED, Router, run_booking
from .rules import Rules, base_code
from .store import Store
from .swap import Swap, SwapBusy, SwapResult

PLAN_TTL = 600


@dataclass
class Plan:
    plan_id: str
    kind: str                       # book | guard | swap
    expires: float
    sessions: list[str] = field(default_factory=list)
    summary: str = ""
    shown: tuple[tuple[str, ...], ...] = ()     # the guard diff the attendee saw


class Tools:
    def __init__(self, client: EventsClient, store: Store, rules: Rules, event_id: str,
                 clock: Callable[[], float] = time.time):
        self.client, self.store, self.rules, self.event_id, self.clock = client, store, rules, event_id, clock
        self.router = Router(rules, store, event_id)
        self._plans: dict[str, Plan] = {}
        self._lock = threading.Lock()
        self.serial = threading.RLock()     # every tool call holds this: one at a time

    def _new_plan(self, kind: str, sessions: list[str], summary: str,
                  shown: tuple[tuple[str, ...], ...] = ()) -> Plan:
        p = Plan(secrets.token_urlsafe(16), kind, self.clock() + PLAN_TTL, sessions, summary, shown)
        with self._lock:
            self._plans = {k: v for k, v in self._plans.items() if v.expires > self.clock()}
            self._plans[p.plan_id] = p
        return p

    def _code(self, sid: str) -> str:
        s = self.store.get(self.event_id, sid)
        if not s:
            return sid
        st = s.session_time
        when = f"{st.date} {st.time}" if st and st.date else "time not set"
        return f"{s.abbreviation} {s.title} ({when}, {s.campus_venue or 'venue not set'})"

    # ---- reads

    def list_targets(self) -> str:
        held = set(self.client.get_schedule(self.event_id).reserved)
        lines = []
        for i, t in enumerate(self.rules.targets, 1):
            tree = [s for s, _ in self.router.tree(t)]
            mine = [s for s in tree if s.session_id in held]
            state = (f"held: {self._code(mine[0].session_id)}" if mine
                     else f"not held, {len(tree)} sittings known")
            lines.append(f"{i}. {t.label}{' (auto swap)' if t.auto_swap else ''}: {state}")
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

    # ---- proposals: no writes

    def propose_changes(self) -> str:
        held = self.client.get_schedule(self.event_id).reserved
        plan = self.router.plan(held, quota_left=self.client.quota.remaining("ReserveSessions"))
        if plan.blocked:
            return f"Nothing planned. {plan.blocked}"
        chosen = plan.batch + plan.deferred
        if not chosen:
            why = "; ".join(f"{s.target}: {s.reason}" for s in plan.skipped[:10])
            return "Nothing to reserve now." + (f" Skipped: {why}." if why else "")
        lines = [f"- {self._code(p.session_id)} for target {p.target}" for p in chosen]
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

    def propose_swap(self, held_code: str) -> str:
        """A held session's best open sitting of the same talk, checked as swap.py checks. Sends nothing."""
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
            return f"{a.abbreviation} is not a target in the rules file, so no sitting is preferred to it."
        better = [s for s in self._preferred(a) if s.band and s.band.open and s.session_id not in held]
        if not better:
            return (f"No better sitting of {a.base_code} is open. {a.abbreviation} is the first choice the "
                    "rules allow, or every sitting the rules prefer is full. Nothing was planned.")
        swap, reasons = Swap(self.client, self.store, self.rules, self.event_id), []
        for b in better:
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
            return "Refused: unknown, used or expired plan id. Call propose_changes or guard_sync first."
        try:
            if p.kind == "swap":
                return self._swap(p)
            return self._book(p) if p.kind == "book" else self._guard(p)
        except SwapBusy as e:
            return f"Not run: {e} Nothing was sent. Propose again in a moment."
        except OperationClosed:
            return WRITES_CLOSED
        except ApiError as e:
            return f"Stopped on an API error: {e}. Check the schedule before trying again."

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

    def _guard(self, p: Plan) -> str:
        now = guard.compute(self.client, self.store, self.event_id, self.rules)
        if _diff(now) != p.shown:
            return ("Refused: the leave-now changes are no longer what was shown, because what is held "
                    "changed. Call guard_sync again and show the new plan.")
        res = guard.sync(self.client, self.store, self.event_id, self.rules)
        out = "\n".join(f"- {d}" for d in res.done) or "- nothing sent"
        probs = "\n".join(f"Problem: {x}" for x in res.problems)
        return f"Leave-now blocks:\n{out}" + (f"\n{probs}" if probs else "")


def _diff(gp: guard.GuardPlan) -> tuple[tuple[str, ...], ...]:
    return (tuple(f"+{b.title}@{b.start}" for b in gp.create)
            + tuple(f"~{pid}:{b.title}@{b.start}" for pid, b in gp.update)
            + tuple(f"-{p.personal_time_id}" for p in gp.delete),)


def build_server(tools: Tools) -> Any:
    """The MCP server. Imports the SDK here so the core install does not need it."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("reseat", instructions=(
        "re:Seat manages AWS re:Invent seats for one attendee. Read tools are safe. "
        "Every change is two steps: propose_changes, propose_swap or guard_sync returns a plan id and sends "
        "nothing. "
        "Show the plan to the attendee. Only after they agree, call approve_changes with that plan id."))

    @server.tool()
    def list_targets() -> str:
        """The attendee's targets in priority order and which are held now."""
        with tools.serial:
            return tools.list_targets()

    @server.tool()
    def propose_changes() -> str:
        """Plan the reservations the rules ask for. Returns a plan id. Sends nothing."""
        with tools.serial:
            return tools.propose_changes()

    @server.tool()
    def propose_swap(held_code: str) -> str:
        """Plan moving a held session to a better open sitting of the same talk.
        Returns a plan id and the checks. Sends nothing."""
        with tools.serial:
            return tools.propose_swap(held_code)

    @server.tool()
    def approve_changes(plan_id: str) -> str:
        """Carry out a plan from propose_changes, propose_swap or guard_sync, once, within 10 minutes."""
        with tools.serial:
            return tools.approve_changes(plan_id)

    @server.tool()
    def explain_drift(hours: int = 0) -> str:
        """What changed in the catalog since the last sweep, or over the last `hours` if given."""
        with tools.serial:
            return tools.explain_drift(hours)

    @server.tool()
    def guard_sync() -> str:
        """Plan leave-now block changes. Returns a plan id. Sends nothing."""
        with tools.serial:
            return tools.guard_sync()

    @server.tool()
    def queue_or_go(code: str) -> str:
        """Whether to queue for a session code, by session type and band history. A heuristic."""
        with tools.serial:
            return tools.queue_or_go(code)

    return server
