"""The only resends of a reserve or cancel: one after a 429, once Retry-After has passed,
and one after a 401, once the sign-in is refreshed. Every
other outcome is settled by a GetSchedule read-back, and each send is read back."""
import pytest

from reseat import cancel as C
from reseat import rules as R
from reseat.fakeapi import sample_sessions
from reseat.router import OP, Router, run_booking

EV = "reinvent2026"
RULES = "targets:\n- session_id: S-SEC1\n"


@pytest.fixture
def booking(fake, client, store):
    store.apply_sweep(EV, sample_sessions(), with_abstracts=False)
    return Router(R.parse(RULES), store, EV)


def sends(fake, op):
    return [s for o, s in fake.log if o == op]


def after(fake, op):
    """Operations sent after the last `op`, so a read-back can be checked to follow the write."""
    ops = [o for o, _ in fake.log]
    return ops[len(ops) - 1 - ops[::-1].index(op):]


@pytest.fixture
def expired(fake, client):
    """The held token is stale. One refresh fetches the token the fake accepts."""
    refreshes: list[int] = []
    token = {"v": "stale"}
    client.token_provider = lambda: token["v"]

    def refresh() -> str:
        refreshes.append(1)
        token["v"] = fake.token
        return token["v"]
    client.on_refresh = refresh
    return refreshes


def test_reserve_429_is_sent_twice_then_read_back(fake, client, store, booking, clock):
    fake.throttle_next(OP, retry_after=5)
    t0 = clock.t
    run = run_booking(booking, client, store, EV, held=[])
    assert sends(fake, OP) == [429, 200] and clock.t - t0 >= 5
    assert "GetSchedule" in after(fake, OP)
    assert run.outcomes[0].status == "reserved" and "S-SEC1" in fake.schedule.reserved


def test_reserve_429_twice_is_not_sent_a_third_time(fake, client, store, booking):
    fake.fail_next(OP, 429, times=2)
    run = run_booking(booking, client, store, EV, held=[])
    assert sends(fake, OP) == [429, 429]
    assert "GetSchedule" in after(fake, OP)
    assert run.outcomes[0].status == "refused" and "S-SEC1" not in fake.schedule.reserved


def test_cancel_429_is_sent_twice_then_read_back(fake, client, store):
    fake.schedule.reserved.add("S-ARC2")
    fake.throttle_next("CancelReservation", retry_after=2)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert sends(fake, "CancelReservation") == [429, 204]
    assert "GetSchedule" in after(fake, "CancelReservation")
    assert res.status == "cancelled"


def test_reserve_401_refreshes_once_resends_once_and_reads_back(fake, client, store, booking, expired):
    run = run_booking(booking, client, store, EV, held=[])
    assert expired == [1] and sends(fake, OP) == [401, 200]
    assert "GetSchedule" in after(fake, OP)
    assert run.outcomes[0].status == "reserved" and "S-SEC1" in fake.schedule.reserved


def test_cancel_401_after_refresh_is_not_sent_a_third_time(fake, client, store, expired):
    fake.schedule.reserved.add("S-ARC2")
    fake.fail_next("CancelReservation", 401, times=3)
    res = C.cancel(client, store, EV, "S-ARC2", was_held=True)
    assert expired == [1] and sends(fake, "CancelReservation") == [401, 401]
    assert res.status == "error" and "S-ARC2" in fake.schedule.reserved


def test_reserve_5xx_and_transport_error_are_sent_once(fake, client, store, booking):
    import httpx
    fake.fail_next(OP, 503)
    run_booking(booking, client, store, EV, held=[])
    assert sends(fake, OP) == [503]

    def drop(req):
        if req.method == "POST":
            fake.counts[OP] += 1
            raise httpx.ConnectError("reset", request=req)
        return fake._handle(req)
    client.http = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(drop))
    before = fake.counts[OP]
    run = run_booking(booking, client, store, EV, held=[])
    assert fake.counts[OP] - before == 1 and run.outcomes[0].status in ("refused", "unconfirmed")


def test_429_then_401_is_the_most_a_write_is_sent(fake, client, store, booking, expired):
    """Both refusals in one call: three sends at most, each answer applied nothing."""
    fake.throttle_next(OP, retry_after=1)
    run_booking(booking, client, store, EV, held=[])
    assert expired == [1] and sends(fake, OP) == [429, 401, 200]


def test_reserve_429_odd_retry_after_waits_a_minute_then_read_back(fake, client, store, booking, clock):
    fake.throttle_next(OP, retry_after="soon")
    t0 = clock.t
    run = run_booking(booking, client, store, EV, held=[])
    assert sends(fake, OP) == [429, 200] and clock.t - t0 == 60
    assert "GetSchedule" in after(fake, OP)
    assert run.outcomes[0].status == "reserved" and "S-SEC1" in fake.schedule.reserved
