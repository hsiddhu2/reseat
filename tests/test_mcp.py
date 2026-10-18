import re

import pytest

from reseat import mcp_server as M
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi, sample_sessions
from reseat.models import Session
from reseat.store import Store

EV = "reinvent2026"
RULES = "targets:\n- code: SVS401\n- code: SEC201\n"
PLAN = re.compile(r"Plan ([A-Za-z0-9_-]{20,}),")


@pytest.fixture
def tools(fake, client, store, clock):
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    return M.Tools(client, store, R.parse(RULES), EV, clock=clock)


def plan_id(text):
    m = PLAN.search(text)
    assert m, text
    return m.group(1)


def test_propose_returns_a_plan_id_and_sends_nothing(fake, tools):
    out = tools.propose_changes()
    assert plan_id(out) and "SVS401" in out and "SEC201" in out
    assert "Nothing has been sent" in out
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_approve_without_propose_is_refused(fake, tools):
    out = tools.approve_changes("made-up-plan-id-123456")
    assert out.startswith("Refused") and fake.counts.get("ReserveSessions", 0) == 0


def test_approve_executes_reads_back_journals_and_works_once(fake, store, tools):
    pid = plan_id(tools.propose_changes())
    out = tools.approve_changes(pid)
    assert fake.schedule.reserved == {"S-SVS1", "S-SEC1"}
    assert "Read back" in out and "lists 2 reserved" in out
    assert any(r["op"] == "ReserveSessions" for r in store.journal_entries(EV))
    assert tools.approve_changes(pid).startswith("Refused")       # one use


def test_approve_reserves_only_what_the_plan_named(fake, store, tools):
    pid = plan_id(tools.propose_changes())
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    tools.rules = R.parse(RULES + "- code: DOP302\n")               # rules grew after the proposal
    tools.router = M.Router(tools.rules, store, EV)
    tools.approve_changes(pid)
    assert "S-WRK1" not in fake.schedule.reserved


def test_expired_plan_is_refused(fake, tools, clock):
    pid = plan_id(tools.propose_changes())
    clock.sleep(M.PLAN_TTL)
    assert tools.approve_changes(pid).startswith("Refused")
    assert fake.counts.get("ReserveSessions", 0) == 0


def test_writes_closed_surfaces_cleanly(fake, tools):
    pid = plan_id(tools.propose_changes())
    fake.closed = True
    assert "8 October 2026" in tools.approve_changes(pid)


def test_guard_sync_is_a_plan_then_an_approve(fake, tools):
    fake.schedule.reserved.add("S-SVS1")
    out = tools.guard_sync()
    assert "create Leave for SVS401" in out and fake.counts.get("CreatePersonalTime", 0) == 0
    done = tools.approve_changes(plan_id(out))
    assert "created Leave for SVS401" in done and len(fake.schedule.personal_time) == 1
    assert "already match" in tools.guard_sync()


def test_read_tools(fake, tools):
    fake.schedule.reserved.add("S-SVS1")
    assert "1. SVS401: held: SVS401" in tools.list_targets()
    assert "2. SEC201: not held" in tools.list_targets()
    assert "ARC301-R1" in tools.queue_or_go("ARC301")
    assert "not in the local catalog" in tools.queue_or_go("NOPE999")
    assert "6 added" in tools.explain_drift()


def _is_array(p):
    return p.get("type") == "array" or any(x.get("type") == "array" for x in p.get("anyOf", []))


async def test_server_exposes_eleven_tools_and_none_takes_a_session_list(tools, fake):
    server = M.build_server(tools)
    listed = await server.list_tools()
    assert sorted(t.name for t in listed) == sorted(
        ["list_targets", "propose_changes", "propose_swap", "approve_changes", "explain_drift", "guard_sync",
         "queue_or_go", "search_sessions", "open_slots", "check_plan", "draft_rules"])
    lists = {"check_plan": {"codes"}, "draft_rules": {"add", "remove"}}
    for t in listed:
        props = (t.input_schema or {}).get("properties", {})
        assert not any("session" in k.lower() for k in props), (t.name, props)
        arrays = {k for k, p in props.items() if _is_array(p)}
        # Only check_plan and draft_rules take lists, and they are session codes. check_plan only reads.
        # draft_rules writes the local rules file after approve_changes, never to the API.
        assert arrays == lists.get(t.name, set()), (t.name, props)
    out = await server.call_tool("propose_changes", {})
    pid = plan_id(out.content[0].text)
    done = await server.call_tool("approve_changes", {"plan_id": pid})
    assert "Read back" in done.content[0].text and fake.token not in done.content[0].text


def test_tool_calls_run_one_at_a_time(fake, tools):
    import threading
    a, b = plan_id(tools.propose_changes()), plan_id(tools.propose_changes())
    server = M.build_server(tools)
    results = []

    def approve(pid):
        import asyncio
        results.append(asyncio.run(server.call_tool("approve_changes", {"plan_id": pid})))

    threads = [threading.Thread(target=approve, args=(p,)) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.schedule.reserved == {"S-SVS1", "S-SEC1"}        # never a second sitting
    assert fake.counts["ReserveSessions"] == 1                   # the second saw both targets held


def test_guard_plan_refused_when_holds_changed_since_it_was_shown(fake, tools):
    fake.schedule.reserved.add("S-SVS1")
    pid = plan_id(tools.guard_sync())
    fake.schedule.reserved.add("S-SEC1")                          # another hold arrived
    out = tools.approve_changes(pid)
    assert out.startswith("Refused") and fake.counts.get("CreatePersonalTime", 0) == 0


def test_explain_drift_defaults_to_the_last_sweep(fake, store, tools, clock):
    assert "the last sweep" in tools.explain_drift()
    assert "6 added" in tools.explain_drift(hours=48)


# ---- propose_swap: a held session to an earlier open sitting of the same talk


def _s(sid, abbr, date, time_, band="available", venue="MGM Grand"):
    return Session.model_validate({
        "sessionId": sid, "abbreviation": abbr, "title": f"Title {abbr}", "type": "Breakout session",
        "venue": venue, "room": "Level 1 | Grand 117", "isReservable": True, "seatAvailability": band,
        "sessionTime": {"date": date, "time": time_, "length": "60"}})


SWAP_CATALOG = [
    _s("A1", "SVS401-R", "2026-11-30", "13:00", band="unavailable"),   # the rules prefer the earliest
    _s("A2", "SVS401-R1", "2026-12-03", "14:00"),                       # held now
    _s("A3", "SVS401-R2", "2026-12-04", "11:00"),                       # later: never "better"
]


@pytest.fixture
def swapper(clock):
    fake = FakeEventsApi(sessions=SWAP_CATALOG)
    fake.schedule.reserved.add("A2")
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    store = Store(":memory:")
    store.apply_sweep(EV, SWAP_CATALOG, with_abstracts=False)
    return fake, store, M.Tools(client, store, R.parse("targets:\n- code: SVS401\n"), EV, clock=clock)


def writes(fake):
    return fake.counts.get("ReserveSessions", 0) + fake.counts.get("CancelReservation", 0)


def _open_a1(fake, store):
    fake.set_band("A1", "limited")
    store.apply_sweep(EV, list(fake.sessions.values()), with_abstracts=False)


def test_propose_swap_with_a_better_open_sitting_returns_a_plan_and_sends_nothing(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)
    out = tools.propose_swap("SVS401-R1")
    assert plan_id(out) and "Cancel SVS401-R1" in out and "reserve SVS401-R" in out
    assert "band limited" in out and "fallback if it fails: SVS401-R1" in out
    assert "Nothing has been sent" in out
    assert writes(fake) == 0


def test_propose_swap_without_a_better_open_sitting_gives_a_reason(swapper):
    fake, _, tools = swapper
    out = tools.propose_swap("SVS401-R1")          # A1 is full and A3 is later
    assert out.startswith("No sitting of SVS401 that the rules list before") and not PLAN.search(out)
    assert writes(fake) == 0


def test_propose_swap_refuses_a_code_not_held(swapper):
    fake, _, tools = swapper
    assert tools.propose_swap("ARC999").startswith("Refused: ARC999 is not held")
    assert writes(fake) == 0


def test_propose_swap_whose_fresh_checks_fail_plans_nothing_and_journals_it(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)                           # the store says open
    fake.set_band("A1", "unavailable")              # but GetSession, read fresh, says full
    out = tools.propose_swap("SVS401-R1")
    assert out.startswith("No swap planned") and "not open" in out and writes(fake) == 0
    assert store.journal_entries(EV, 1)[0]["op"] == "swap.declined"        # nothing tried, so not "failed"


def test_approve_runs_the_swap_state_machine_and_journals(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)
    out = tools.approve_changes(plan_id(tools.propose_swap("SVS401-R1")))
    assert out.startswith("Swap verified: proposed -> checked -> cancelled -> reserved -> verified")
    assert fake.schedule.reserved == {"A1"} and "Held now: SVS401-R Title SVS401-R" in out
    ops = [r["op"] for r in store.journal_entries(EV, 40)]
    assert {"swap.cancel", "swap.reserve", "swap.verified"} <= set(ops)


def test_approve_swap_after_expiry_is_refused(swapper, clock):
    fake, store, tools = swapper
    _open_a1(fake, store)
    pid = plan_id(tools.propose_swap("SVS401-R1"))
    clock.sleep(M.PLAN_TTL)
    assert tools.approve_changes(pid).startswith("Refused") and writes(fake) == 0


def test_approve_swap_works_once_and_rolls_back_when_the_seat_is_gone(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)
    pid = plan_id(tools.propose_swap("SVS401-R1"))
    fake.full.add("A1")                              # taken between the proposal and the approve
    out = tools.approve_changes(pid)
    assert out.startswith("Swap rolled_back") and fake.schedule.reserved == {"A2"}
    assert tools.approve_changes(pid).startswith("Refused")


def test_approve_swap_while_another_swap_runs_sends_nothing(swapper):
    from reseat.swap import _IN_FLIGHT
    fake, store, tools = swapper
    _open_a1(fake, store)
    pid = plan_id(tools.propose_swap("SVS401-R1"))
    assert _IN_FLIGHT.acquire(blocking=False)
    try:
        assert tools.approve_changes(pid).startswith("Not run") and writes(fake) == 0
    finally:
        _IN_FLIGHT.release()


# ---- QA: exact-sitting targets, failed reads, writes closed


def test_propose_swap_on_an_exact_sitting_target_plans_nothing(swapper, clock):
    fake, store, tools = swapper
    _open_a1(fake, store)
    exact = M.Tools(tools.client, store, R.parse("targets:\n- session_id: A2\n  repeats: false\n"), EV,
                    clock=clock)
    out = exact.propose_swap("SVS401-R1")
    assert out.startswith("No sitting of") and not PLAN.search(out) and writes(fake) == 0


@pytest.mark.parametrize("op,status", [("GetSession", 503), ("GetSession", 404), ("GetSchedule", 401),
                                       ("GetSession", 403)])
def test_propose_swap_when_a_read_fails_sends_nothing_and_plans_nothing(swapper, op, status):
    fake, store, tools = swapper
    _open_a1(fake, store)
    fake.fail_next(op, status, times=5)
    out = tools.propose_swap("SVS401-R1")              # an answer the agent can show, never a raise
    assert out.startswith("No swap planned. A read failed") and "Nothing was sent" in out
    assert not PLAN.search(out) and writes(fake) == 0 and tools._plans == {}
    if op == "GetSession":                          # the swap check began: it must have an end state
        assert store.journal_entries(EV, 1)[0]["op"] == "swap.failed"


def test_approve_swap_when_writes_close_keeps_the_held_seat(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)
    pid = plan_id(tools.propose_swap("SVS401-R1"))
    fake.closed = True
    out = tools.approve_changes(pid)
    assert "Writes are closed (409)" in out and fake.schedule.reserved == {"A2"}
    assert fake.counts.get("ReserveSessions", 0) == 0
    assert store.journal_entries(EV, 1)[0]["op"] == "swap.failed"


def test_a_planned_swap_ends_its_journal_record_as_planned(swapper):
    fake, store, tools = swapper
    _open_a1(fake, store)
    plan_id(tools.propose_swap("SVS401-R1"))
    assert store.journal_entries(EV, 1)[0]["op"] == "swap.planned" and writes(fake) == 0


def test_propose_swap_on_a_session_no_target_covers_says_so(swapper):
    fake, store, tools = swapper
    tools.rules = R.parse("targets:\n- code: ARC301\n")
    tools.router = M.Router(tools.rules, store, EV)
    assert "is not a target in the rules file" in tools.propose_swap("SVS401-R1") and writes(fake) == 0


def test_mcp_demo_has_targets_a_plan_to_approve_and_no_session_ids():
    from reseat import demo as D
    client, store, rules, event, clock = D.mcp_parts()
    t = M.Tools(client, store, rules, event, clock)
    targets = t.list_targets()
    assert "SVS306" in targets and "ARC306" in targets
    out = t.guard_sync()
    plan = re.search(r"Plan (\S+), valid", out).group(1)
    done = t.approve_changes(plan)
    assert "Leave for" in out and "created Leave for DVT201" in done
    text = targets + t.propose_changes() + out + done + t.queue_or_go("ANT335")
    for s in store.by_base_code(event, "ARC306") + store.by_base_code(event, "SVS306"):
        assert s.session_id not in text


def test_cli_mcp_demo_needs_no_sign_in(monkeypatch):
    from typer.testing import CliRunner

    from reseat import cli
    ran = {}

    class Server:
        def run(self, how):
            ran["how"] = how
    monkeypatch.setattr(M, "build_server", lambda tools: ran.setdefault("tools", tools) and Server())
    monkeypatch.setattr(cli, "_client", lambda: pytest.fail("the demo must not read the keychain"))
    r = CliRunner().invoke(cli.app, ["mcp", "--demo"])
    assert r.exit_code == 0, r.output
    assert ran["how"] == "stdio" and "SVS306" in ran["tools"].list_targets()
    path = ran["tools"].rules_path
    assert path is not None and config_home_untouched(path)
    out = ran["tools"].draft_rules([], ["DVT201"])          # every demo session is a target already
    assert "Plan " in out and "Nothing has been written" in out


def config_home_untouched(path):
    """The demo's rules file is a temporary copy, never the attendee's ~/.reseat/rules.yaml."""
    from reseat import config
    return path.resolve() != config.RULES_PATH.resolve() and path.parent.name.startswith("reseat-mcp-demo-")


def test_target_names_keep_every_real_code_and_hide_ids():
    from reseat.router import target_name
    st = Store(":memory:")
    for code in ("ARC301", "AIM310-R1", "AIM101-S", "INV201-S-R1", "KEY001", "DEV202-S"):
        assert target_name(code, st, "ev") == code
    assert target_name("1780441463028001GEdS", st, "ev") == "a pinned sitting"
