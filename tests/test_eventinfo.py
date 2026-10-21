from typer.testing import CliRunner

from reseat import cli
from reseat import eventinfo as E
from reseat.models import Event

EV = "reinvent2026"


def event(**over):
    base = {"eventId": EV, "name": "re:Invent 2026", "eventType": "AWS Global Summit",
            "startDate": "2026-11-30T05:30:00.000-08:00", "endDate": "2026-12-04T23:59:00.000-08:00",
            "isOnline": False, "timezone": "America/Los_Angeles", "timezoneAbbreviation": "PDT",
            "address": {"city": "Las Vegas"}, "authenticationRequired": True}
    return Event.model_validate(base | over)


def test_the_live_answer_passes_and_the_wrong_label_is_noted():
    res = E.check(event())                       # as the live API answered on 21 October 2026
    assert res.ok
    assert res.line() == ("re:Invent 2026, Mon 30 Nov to Fri 4 Dec 2026, Las Vegas. "
                          "Times in America/Los_Angeles.")
    assert any("labels the zone PDT" in n and "PST" in n for n in res.notes)


def test_a_matching_label_needs_no_note():
    assert E.check(event(timezoneAbbreviation="PST")).notes == []


def test_another_time_zone_is_refused():
    res = E.check(event(timezone="America/New_York"))
    assert not res.ok and "would be wrong" in res.problems[0]


def test_other_dates_are_refused_because_day_names_would_shift():
    res = E.check(event(startDate="2027-11-29T08:00:00-08:00", endDate="2027-12-03T18:00:00-08:00"))
    assert not res.ok and "day names" in res.problems[0]


def test_dates_in_utc_are_read_in_las_vegas_time():
    res = E.check(event(startDate="2026-11-30T13:30:00Z", endDate="2026-12-05T07:59:00Z"))
    assert res.ok


def test_no_time_zone_is_a_note_and_unreadable_dates_are_a_note():
    assert E.check(event(timezone=None)).notes[0].startswith("The API gave no time zone")
    res = E.check(event(startDate="soon", endDate="later", timezoneAbbreviation=None))
    assert res.ok and "could not be read" in res.notes[0]


def test_fetch_unknown_event_is_a_problem(client):
    res = E.fetch(client, "reinvent2099")
    assert not res.ok and "reseat events" in res.problems[0]


def test_fetch_failure_is_a_note_not_a_stop(fake, client):
    fake.fail_next("GetEvent", 503)
    res = E.fetch(client, EV)
    assert res.ok and res.event is None and "GetEvent failed" in res.notes[0]
    assert fake.counts["GetEvent"] == 1                       # a read, still not retried here


def test_fetch_needs_no_sign_in(fake, client):
    fake.require_auth = True
    client.token_provider = None
    assert E.fetch(client, EV).ok


def test_whoami_shows_the_event_and_stops_on_a_problem(fake, client, store, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda: client)
    r = CliRunner().invoke(cli.app, ["whoami"])
    assert r.exit_code == 0 and "Las Vegas" in r.output and "Registered for" in r.output
    r = CliRunner().invoke(cli.app, ["whoami", "--event", "reinvent2099"])
    assert r.exit_code == 2 and "No event with id" in r.output
    assert fake.counts["GetSchedule"] == 1                    # the bad event never reached GetSchedule


def test_sync_refuses_before_any_catalog_call_when_the_event_is_wrong(fake, client, store, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(E, "check", lambda ev: E.EventCheck(event=ev, problems=["zone differs"]))
    r = CliRunner().invoke(cli.app, ["sync"])
    assert r.exit_code == 4 and "zone differs" in r.output
    assert fake.counts.get("ListSessions", 0) == 0 and store.sweeps(EV) == []


def test_event_id_is_one_path_segment(client, monkeypatch):
    paths = []
    monkeypatch.setattr(client, "_request", lambda op, method, path, **kw: paths.append(path) or 1 / 0)
    for call in (client.get_event, client.get_schedule, client.list_sessions_page):
        try:
            call("reinvent2026/../x")
        except ZeroDivisionError:
            pass
    assert paths and all(p.startswith("/v1/events/reinvent2026%2F..%2Fx") for p in paths)


def test_c1_control_characters_are_stripped_from_terminal_text():
    assert cli._esc("a\x9b31mb") == "a?31mb"


# ---------------------------------------------------------------- failure paths (QA pass)


def _client_with(fake, clock, route):
    import httpx

    from reseat.client import EventsClient, QuotaTracker
    return EventsClient(token_provider=lambda: fake.token, base_url="https://api.awsevents.com",
                        quota=QuotaTracker(clock=clock), sleep=clock.sleep,
                        transport=httpx.MockTransport(route))


def test_fetch_500_is_a_note(fake, client):
    fake.fail_next("GetEvent", 500)
    res = E.fetch(client, EV)
    assert res.ok and "500" in res.notes[0]


def test_fetch_404_from_the_api_on_a_known_id_is_a_problem(fake, client):
    fake.fail_next("GetEvent", 404)
    assert not E.fetch(client, EV).ok


def test_fetch_network_error_is_a_note(fake, clock):
    import httpx

    def down(req):
        raise httpx.ConnectError("Connection refused", request=req)

    res = E.fetch(_client_with(fake, clock, down), EV)
    assert res.ok and res.event is None and "ConnectError" in res.notes[0]


def test_fetch_timeout_is_a_note(fake, clock):
    import httpx

    def slow(req):
        raise httpx.ReadTimeout("timed out", request=req)

    assert "ReadTimeout" in E.fetch(_client_with(fake, clock, slow), EV).notes[0]


def test_fetch_malformed_body_is_a_note(fake, clock):
    import httpx
    res = E.fetch(_client_with(fake, clock, lambda req: httpx.Response(200, json={"nope": 1})), EV)
    assert res.ok and "GetEvent failed" in res.notes[0]


def test_fetch_429_waits_once_then_checks(fake, client, clock):
    fake.throttle_next("GetEvent", retry_after=3)
    t0 = clock.t
    res = E.fetch(client, EV)
    assert res.ok and res.event is not None and clock.t - t0 >= 3


def test_fetch_another_zone_from_the_api_is_a_problem(fake, client, monkeypatch):
    real = fake._event
    monkeypatch.setattr(fake, "_event", lambda: real() | {"timezone": "Europe/Berlin"})
    res = E.fetch(client, EV)
    assert not res.ok and "Europe/Berlin" in res.problems[0]


def test_missing_zone_and_label_still_passes_and_reads_las_vegas_time():
    res = E.check(event(timezone=None, timezoneAbbreviation=None))
    assert res.ok and len(res.notes) == 1 and res.line().endswith("Times in Las Vegas time.")


def test_other_zone_is_refused_even_when_dates_match():
    res = E.check(event(timezone="America/Denver", timezoneAbbreviation="MST"))
    assert not res.ok and len(res.problems) == 1 and not any("labels" in n for n in res.notes)


def test_utc_dates_that_fall_on_another_las_vegas_day_are_refused():
    # 07:00Z on 30 Nov is 23:00 on 29 Nov in Las Vegas
    res = E.check(event(startDate="2026-11-30T07:00:00Z", endDate="2026-12-05T07:59:00Z"))
    assert not res.ok and "2026-11-29" in res.problems[0]


def test_one_day_short_is_refused():
    res = E.check(event(endDate="2026-12-03T18:00:00-08:00"))
    assert not res.ok and "day names" in res.problems[0]


def test_online_event_line_says_online():
    res = E.check(event(isOnline=True, address=None))
    assert res.line().startswith("re:Invent 2026, Mon 30 Nov to Fri 4 Dec 2026, online.")


def test_sync_still_runs_when_get_event_fails(fake, client, store, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    fake.fail_next("GetEvent", 503)
    r = CliRunner().invoke(cli.app, ["sync", "--no-abstracts"])
    assert r.exit_code == 0 and fake.counts["ListSessions"] >= 1


def test_sync_stops_on_an_unknown_event(fake, client, store, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: store)
    r = CliRunner().invoke(cli.app, ["sync", "--event", "reinvent2099"])
    assert r.exit_code == 4 and "No event with id" in r.output
    assert fake.counts.get("ListSessions", 0) == 0
