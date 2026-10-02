import re

import pytest

from reseat import mcp_server as M
from reseat import rules as R
from reseat.fakeapi import sample_sessions

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


async def test_server_exposes_six_tools_and_none_takes_a_session_list(tools, fake):
    server = M.build_server(tools)
    listed = await server.list_tools()
    assert sorted(t.name for t in listed) == sorted(
        ["list_targets", "propose_changes", "approve_changes", "explain_drift", "guard_sync", "queue_or_go"])
    for t in listed:
        props = (t.input_schema or {}).get("properties", {})
        assert not any("session" in k.lower() for k in props), (t.name, props)
        assert all(p.get("type") != "array" for p in props.values()), (t.name, props)
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
