"""propose_favorites and approve_changes for a favorites plan, against FakeEventsApi.

API rules under test: AssociateFavorites counts each session against its quota and is not
idempotent, so a failed session is never re-sent. Every write is read back with GetSchedule.
Writes closed (409) and a 429 then Retry-After once are handled as favorites.py handles them.
"""

import re

import pytest

from reseat import favorites as F
from reseat import mcp_server as M
from reseat import rules as R
from reseat.fakeapi import sample_sessions

EV = "reinvent2026"
RULES = "targets:\n- code: ARC301\n- code: SVS401\n"
PLAN = re.compile(r"Plan ([A-Za-z0-9_-]{20,}),")
IDS = ("S-ARC1", "S-ARC2", "S-SVS1", "S-SEC1", "S-WRK1")


@pytest.fixture
def tools(fake, client, store, clock):
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    return M.Tools(client, store, R.parse(RULES), EV, clock=clock)


def plan_id(text):
    m = PLAN.search(text)
    assert m, text
    return m.group(1)


def writes(fake):
    return [op for op, _ in fake.calls if op != "GetSchedule"]


def set_rules(tools, store, body):
    tools.rules = R.parse(body)
    tools.router = M.Router(tools.rules, store, EV)


def no_ids(fake, *texts):
    for t in texts:
        assert not any(i in t for i in IDS), t
        assert fake.token not in t


def test_propose_lists_codes_and_titles_and_sends_no_write(fake, tools):
    out = tools.propose_favorites()
    assert plan_id(out) and "add ARC301-R1 Resilient architectures for target ARC301" in out
    assert "add SVS401 Serverless at scale" in out and "add 3, remove 0" in out
    assert "Nothing has been sent" in out
    assert writes(fake) == [] and fake.counts["GetSchedule"] == 1
    no_ids(fake, out)


def test_approve_adds_in_one_batch_reads_back_and_journals(fake, store, tools):
    pid = plan_id(tools.propose_favorites())
    done = tools.approve_changes(pid)
    assert fake.schedule.favorites == {"S-ARC1", "S-ARC2", "S-SVS1"}
    assert fake.counts[F.OP] == 1 and fake.counts["GetSchedule"] == 3     # propose, fresh read, read-back
    assert done.startswith("Done. Read back") and "3 sittings planned: 3 added" in done
    assert store.journal_entries(EV)[0]["outcome"] == "readback-ok"
    no_ids(fake, done)


def test_nothing_to_change_makes_no_plan(fake, tools):
    fake.schedule.favorites |= {"S-ARC1", "S-ARC2", "S-SVS1"}
    out = tools.propose_favorites(prune=True)
    assert "Nothing to change, no plan made" in out and not PLAN.search(out)
    assert writes(fake) == []


def test_prune_removes_only_favorites_re_seat_added(fake, store, tools):
    tools.approve_changes(plan_id(tools.propose_favorites()))          # re:Seat adds ARC301 x2, SVS401
    fake.schedule.favorites.add("S-SEC1")                               # the attendee's own
    set_rules(tools, store, "targets:\n- code: SVS401\n")
    assert "remove" not in tools.propose_favorites()                    # prune is off by default
    out = tools.propose_favorites(prune=True)
    assert "remove ARC301-R1" in out and "remove ARC301-R2" in out and "SEC201" not in out
    assert "add 0, remove 2" in out and writes(fake) == [F.OP]
    done = tools.approve_changes(plan_id(out))
    assert fake.schedule.favorites == {"S-SVS1", "S-SEC1"} and fake.counts[F.UNOP] == 2
    assert "2 of 2 removed" in done and "removed from favorites: ARC301-R1" in done
    no_ids(fake, out, done)


def test_prune_keeps_a_favorite_for_a_held_seat(fake, store, tools):
    tools.approve_changes(plan_id(tools.propose_favorites()))
    fake.schedule.reserved.add("S-ARC1")
    set_rules(tools, store, "targets:\n- code: SVS401\n")
    out = tools.propose_favorites(prune=True)
    assert "remove ARC301-R2" in out and "remove ARC301-R1" not in out
    assert "1 favorite re:Seat added for dropped targets kept, because a seat is held there" in out


def test_a_failed_session_in_a_200_is_reported_and_never_resent(fake, tools):
    fake.refuse["S-SVS1"] = "insufficientAccess"
    pid = plan_id(tools.propose_favorites())
    done = tools.approve_changes(pid)
    assert "failed: SVS401 Serverless at scale (insufficientAccess)" in done
    assert "2 added, 0 already favorited, 1 failed" in done
    assert fake.counts[F.OP] == 1 and fake.schedule.favorites == {"S-ARC1", "S-ARC2"}
    assert tools.approve_changes(pid).startswith("Refused") and fake.counts[F.OP] == 1


def test_writes_closed_409_stops_cleanly(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.closed = True
    done = tools.approve_changes(pid)
    assert F.CLOSED in done and "not sent: ARC301-R1" in done and fake.schedule.favorites == set()
    assert fake.counts[F.OP] == 1


def test_429_waits_retry_after_once_then_adds(fake, tools, clock):
    pid = plan_id(tools.propose_favorites())
    fake.throttle_next(F.OP, retry_after=7)
    t0 = clock.t
    done = tools.approve_changes(pid)
    assert clock.t - t0 >= 7 and fake.counts[F.OP] == 2               # refused one, then one retry
    assert "3 added" in done and len(fake.schedule.favorites) == 3


def test_429_twice_stops_reads_back_and_sends_no_third(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.fail_next(F.OP, 429, times=2)
    done = tools.approve_changes(pid)
    assert fake.counts[F.OP] == 2 and fake.schedule.favorites == set()
    assert "Stopped: AssociateFavorites: 429" in done and "3 failed" in done and "never re-sent" in done


def test_read_back_failure_says_nothing_is_confirmed(fake, client, tools):
    pid = plan_id(tools.propose_favorites())
    real = client.favorite

    def then_fail_read(ev, ids):
        out = real(ev, ids)
        fake.fail_next("GetSchedule", 503)
        return out

    client.favorite = then_fail_read
    done = tools.approve_changes(pid)
    assert "could not be read back, so nothing is confirmed" in done and "read-back failed" in done
    assert fake.counts[F.OP] == 1


def test_a_failed_read_at_propose_makes_no_plan(fake, tools):
    fake.fail_next("GetSchedule", 503)
    out = tools.propose_favorites()
    assert out.startswith("No favorites plan. A read failed") and not PLAN.search(out)
    assert writes(fake) == []


def test_a_failed_fresh_read_at_approve_sends_nothing(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.fail_next("GetSchedule", 503)
    assert tools.approve_changes(pid).startswith("Stopped on an API error") and writes(fake) == []


def test_an_expired_plan_is_refused(fake, tools, clock):
    pid = plan_id(tools.propose_favorites())
    clock.sleep(M.PLAN_TTL)
    assert tools.approve_changes(pid).startswith("Refused") and writes(fake) == []


def test_a_reused_plan_is_refused(fake, tools):
    pid = plan_id(tools.propose_favorites())
    tools.approve_changes(pid)
    fake.schedule.favorites.clear()                                     # even if it would add again
    assert tools.approve_changes(pid).startswith("Refused") and fake.counts[F.OP] == 1


def test_approve_sends_only_what_the_plan_named(fake, store, tools):
    pid = plan_id(tools.propose_favorites())
    set_rules(tools, store, RULES + "- code: DOP302\n")                # rules grew after the proposal
    tools.approve_changes(pid)
    assert "S-WRK1" not in fake.schedule.favorites and len(fake.schedule.favorites) == 3


def test_a_session_favorited_since_the_proposal_is_not_sent(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.schedule.favorites |= {"S-ARC1", "S-ARC2", "S-SVS1"}           # the attendee did it in the app
    done = tools.approve_changes(pid)
    assert "Nothing was sent" in done and writes(fake) == []


def test_a_rules_file_change_drops_the_plan(fake, client, store, clock, tmp_path):
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    path = tmp_path / "rules.yaml"
    path.write_text(RULES)
    path.chmod(0o600)
    tools = M.Tools(client, store, R.load(path), EV, clock=clock, rules_path=path)
    pid = plan_id(tools.propose_favorites())
    path.write_text("targets:\n- code: SVS401\n")
    path.chmod(0o600)
    tools.refresh()
    assert tools.approve_changes(pid).startswith("Refused") and writes(fake) == []


async def test_the_server_tool_plans_then_approves(fake, tools):
    server = M.build_server(tools)
    out = (await server.call_tool("propose_favorites", {"prune": True})).content[0].text
    assert writes(fake) == []
    done = (await server.call_tool("approve_changes", {"plan_id": plan_id(out)})).content[0].text
    assert "Read back" in done and len(fake.schedule.favorites) == 3
    no_ids(fake, out, done)


# ---------------------------------------------------------------- failure paths at the MCP layer


def _mirror_then_drop(tools, store, keep="targets:\n- code: SVS401\n"):
    """re:Seat favorites ARC301 x2 and SVS401, then ARC301 leaves the rules."""
    tools.approve_changes(plan_id(tools.propose_favorites()))
    set_rules(tools, store, keep)


def test_approve_removes_only_the_plans_removals_even_if_more_are_stale_now(fake, store, tools):
    _mirror_then_drop(tools, store)
    out = tools.propose_favorites(prune=True)
    assert "remove ARC301-R1" in out and "remove ARC301-R2" in out and "SVS401" not in out
    set_rules(tools, store, "targets:\n- code: DOP302\n")              # SVS401 is now stale too
    done = tools.approve_changes(plan_id(out))
    assert fake.counts[F.UNOP] == 2 and "S-SVS1" in fake.schedule.favorites
    assert "S-WRK1" not in fake.schedule.favorites and fake.counts[F.OP] == 1
    assert "2 of 2 removed" in done


def test_a_removal_plan_reads_back_and_never_cancels(fake, store, tools):
    _mirror_then_drop(tools, store)
    before = fake.counts["GetSchedule"]
    tools.approve_changes(plan_id(tools.propose_favorites(prune=True)))
    assert fake.counts["GetSchedule"] == before + 3                    # propose, fresh read, read-back
    assert not any(op == "CancelReservation" for op, _ in fake.calls)


def test_a_removal_already_gone_answers_404_and_is_reported(fake, client, store, tools):
    _mirror_then_drop(tools, store)
    out = tools.propose_favorites(prune=True)
    real = client.unfavorite

    def gone_first(ev, sid):
        fake.schedule.favorites.discard(sid)                            # removed in the app meanwhile
        return real(ev, sid)

    client.unfavorite = gone_first
    done = tools.approve_changes(plan_id(out))
    assert "already gone from favorites: ARC301-R1" in done and fake.counts[F.UNOP] == 2
    assert not (fake.schedule.favorites & {"S-ARC1", "S-ARC2"})


def test_a_5xx_on_remove_stops_and_is_read_back(fake, store, tools):
    _mirror_then_drop(tools, store)
    out = tools.propose_favorites(prune=True)
    fake.fail_next(F.UNOP, 503)
    done = tools.approve_changes(plan_id(out))
    assert fake.counts[F.UNOP] == 1 and "never re-sent" in done
    assert "Stopped: DisassociateFavorite: 503" in done
    assert "failed from favorites: ARC301-R1" in done and "not sent from favorites: ARC301-R2" in done


def test_a_5xx_on_add_is_read_back_not_retried(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.fail_next(F.OP, 503)
    done = tools.approve_changes(pid)
    assert fake.counts[F.OP] == 1 and fake.schedule.favorites == set()
    assert "Stopped: AssociateFavorites: 503" in done and "3 failed" in done and "Read back" in done


def test_an_already_favorited_race_in_the_batch_is_reported_as_already(fake, client, tools):
    pid = plan_id(tools.propose_favorites())
    real = client.favorite

    def race(ev, ids):
        fake.schedule.favorites.add("S-SVS1")                           # the app got there first
        return real(ev, ids)

    client.favorite = race
    done = tools.approve_changes(pid)
    assert "already favorited: SVS401 Serverless at scale" in done
    assert "2 added, 1 already favorited, 0 failed" in done and fake.counts[F.OP] == 1


def test_no_quota_left_sends_nothing_and_says_so(fake, client, tools, monkeypatch):
    pid = plan_id(tools.propose_favorites())
    monkeypatch.setattr(client.quota, "remaining", lambda op: 0)
    monkeypatch.setattr(client.quota, "seconds_until", lambda op, n: 1.0)
    done = tools.approve_changes(pid)
    assert writes(fake) == [] and "quota" in done
    assert "Stopped before every planned change was sent" in done and "Nothing was sent" not in done


# ---------------------------------------------------------------- review follow-ups


def test_a_409_never_claims_nothing_was_left_to_change(fake, tools):
    pid = plan_id(tools.propose_favorites())
    fake.closed = True
    done = tools.approve_changes(pid)
    assert "Stopped before every planned change was sent" in done and "Nothing was sent" not in done


def test_a_planned_removal_is_never_turned_into_an_add(fake, tools):
    p = tools._new_plan("favorites", [], "- remove SVS401")             # S-SVS1 planned only for removal
    p.removals, p.prune = ["S-SVS1"], True
    tools.approve_changes(p.plan_id)                                     # the rules still want S-SVS1
    assert writes(fake) == [] and fake.schedule.favorites == set()


def test_a_planned_add_no_longer_wanted_is_reported_and_not_sent(fake, store, tools):
    pid = plan_id(tools.propose_favorites())
    set_rules(tools, store, "targets:\n- code: SVS401\n")
    done = tools.approve_changes(pid)
    assert fake.schedule.favorites == {"S-SVS1"}
    assert "2 planned changes no longer needed by the fresh read, not sent" in done


def test_a_race_that_answers_already_favorited_for_the_whole_batch_still_says_sent(fake, client, tools):
    pid = plan_id(tools.propose_favorites())
    real = client.favorite

    def race(ev, ids):
        fake.schedule.favorites |= set(ids)                             # the app got there first, for all
        return real(ev, ids)

    client.favorite = race
    done = tools.approve_changes(pid)
    assert fake.counts[F.OP] == 1 and "Nothing was sent" not in done
    assert done.startswith("Done. Read back") and "0 added, 3 already favorited" in done


def test_removals_that_all_answer_404_say_nothing_changed_not_read_back(fake, client, store, tools):
    _mirror_then_drop(tools, store)
    out = tools.propose_favorites(prune=True)
    real = client.unfavorite

    def gone_first(ev, sid):
        fake.schedule.favorites.discard(sid)
        return real(ev, sid)

    client.unfavorite = gone_first
    reads = fake.counts["GetSchedule"]
    done = tools.approve_changes(plan_id(out))
    assert fake.counts["GetSchedule"] == reads + 1                      # the fresh read, no read-back
    assert done.startswith("Nothing changed. Already gone:") and "Read back" not in done


def test_the_demo_server_plans_and_approves_favorites():
    from reseat import demo as D
    client, store, rules, event, clock = D.mcp_parts()
    t = M.Tools(client, store, rules, event, clock)
    out = t.propose_favorites()
    done = t.approve_changes(plan_id(out))
    assert "Nothing has been sent" in out and done.startswith("Done. Read back")
    assert "0 failed, 0 unconfirmed" in done
    ids = {sid for sid, _ in F.wanted_sittings(rules, store, event)}
    assert ids and ids <= set(client.get_schedule(event).favorites)
    assert not any(sid in out + done for sid in ids)


def _client_with(fake, clock, on_post):
    """A client whose transport runs `on_post(req)` for each AssociateFavorites."""
    import httpx

    from reseat.client import EventsClient, QuotaTracker

    def route(req):
        if req.method == "POST" and req.url.path.endswith("/favorites"):
            return on_post(req)
        return fake._handle(req)

    return EventsClient(token_provider=lambda: fake.token, base_url="https://api.awsevents.com",
                        quota=QuotaTracker(clock=clock), sleep=clock.sleep,
                        transport=httpx.MockTransport(route))


def test_a_timeout_on_add_is_settled_by_read_back_and_never_resent(fake, store, clock):
    import httpx
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)

    def lands_then_times_out(req):
        fake._handle(req)
        raise httpx.ReadTimeout("timed out", request=req)

    tools = M.Tools(_client_with(fake, clock, lands_then_times_out), store, R.parse(RULES), EV, clock=clock)
    done = tools.approve_changes(plan_id(tools.propose_favorites()))
    assert fake.counts[F.OP] == 1 and len(fake.schedule.favorites) == 3
    assert "3 added" in done and "never re-sent" in done
    assert "Nothing to change" in tools.propose_favorites() and fake.counts[F.OP] == 1


def test_a_409_during_a_prune_removal_stops_after_one_request(fake, store, tools):
    _mirror_then_drop(tools, store)
    out = tools.propose_favorites(prune=True)
    fake.closed = True
    done = tools.approve_changes(plan_id(out))
    assert fake.counts[F.UNOP] == 1 and F.CLOSED in done
    assert {"S-ARC1", "S-ARC2"} <= fake.schedule.favorites


def test_no_batch_is_larger_than_the_quota_left(fake, client, store, tools, monkeypatch):
    import json
    pid = plan_id(tools.propose_favorites())
    monkeypatch.setattr(client.quota, "remaining", lambda op: 2)
    tools.approve_changes(pid)
    sizes = [len(json.loads(r["request"])) for r in store.journal_entries(EV) if r["op"] == F.OP]
    assert sorted(sizes) == [1, 2] and fake.counts[F.OP] == 2 and len(fake.schedule.favorites) == 3
