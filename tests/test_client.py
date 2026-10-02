import pytest

from reseat.client import NotRegistered, OperationClosed, QuotaTracker, Throttled
from reseat.models import FailureCode, PersonalTimeInput


def test_list_events_needs_no_auth(fake, client):
    fake.token = "something-else"  # our provider now sends a stale token
    events = client.list_events()
    assert events[0].event_id == "reinvent2026"
    assert events[0].timezone == "America/Los_Angeles"


def test_iter_sessions_walks_pages(fake, client):
    fake.page_size = 2
    got = list(client.iter_sessions("reinvent2026"))
    assert len(got) == 6
    assert fake.counts["ListSessions"] == 3


def test_sweep_without_abstracts_omits_field(fake, client):
    page = client.list_sessions_page("reinvent2026", include_abstracts=False)
    assert all(s.abstract is None for s in page.items)


def test_reserve_reports_per_session(fake, client):
    fake.full.add("S-SEC1")
    res = client.reserve("reinvent2026", ["S-SVS1", "S-SEC1", "S-ARC1"])
    assert res.successful == ["S-SVS1"]
    codes = {f.session_id: f.known_code for f in res.failed}
    assert codes["S-SEC1"] == FailureCode.SESSION_FULL
    # S-ARC1 overlaps S-SVS1, which landed earlier in the same batch.
    assert codes["S-ARC1"] == FailureCode.SCHEDULE_CONFLICT
    assert res.failure_for("S-ARC1").conflicts_with == ["S-SVS1"]


def test_unavailable_band_is_full(fake, client):
    res = client.reserve("reinvent2026", ["S-ARC1"])
    assert res.failure_for("S-ARC1").known_code == FailureCode.SESSION_FULL


def test_conflict_names_the_held_session(fake, client):
    client.reserve("reinvent2026", ["S-ARC2"])       # Dec 2 14:00-15:00
    res = client.reserve("reinvent2026", ["S-SEC1"])  # Dec 2 14:30
    f = res.failure_for("S-SEC1")
    assert f and f.known_code == FailureCode.SCHEDULE_CONFLICT
    assert f.conflicts_with == ["S-ARC2"]


def test_resend_is_not_a_safe_retry(fake, client):
    client.reserve("reinvent2026", ["S-SVS1"])
    res = client.reserve("reinvent2026", ["S-SVS1"])
    assert res.failure_for("S-SVS1").known_code == FailureCode.ALREADY_SCHEDULED


def test_cancel_missing_is_404(fake, client):
    from reseat.client import ApiError
    with pytest.raises(ApiError) as e:
        client.cancel("reinvent2026", "S-SVS1")
    assert e.value.status == 404


def test_writes_closed_before_october_8(fake, client):
    fake.closed = True
    with pytest.raises(OperationClosed):
        client.reserve("reinvent2026", ["S-SVS1"])
    assert client.writes_open("reinvent2026", "S-KEY1") is False
    fake.closed = False
    assert client.writes_open("reinvent2026", "S-KEY1") is True
    assert "S-KEY1" not in fake.schedule.reserved


def test_batch_limits():
    from reseat.client import _check_batch
    with pytest.raises(ValueError):
        _check_batch([f"S{i}" for i in range(11)])
    with pytest.raises(ValueError):
        _check_batch(["A", "A"])
    assert _check_batch([" A ", "B"]) == ["A", "B"]


def test_quota_counts_sessions_not_requests(clock):
    q = QuotaTracker(clock=clock)
    q.spend("ReserveSessions", 10)
    q.spend("ReserveSessions", 10)
    assert q.remaining("ReserveSessions") == 10
    assert q.seconds_until("ReserveSessions", 10) == 0
    assert q.seconds_until("ReserveSessions", 11) == pytest.approx(60)
    clock.sleep(61)
    assert q.remaining("ReserveSessions") == 30


def test_client_waits_for_quota(fake, client, clock):
    for i in range(3):
        client.favorite("reinvent2026", [f"X{i}-{j}" for j in range(10)])
    t0 = clock.t
    client.favorite("reinvent2026", ["Y"])
    assert clock.t - t0 >= 59  # slept until the window freed


def test_429_waits_retry_after_once(fake, client, clock):
    fake.throttle_next("GetSchedule", retry_after=7)
    t0 = clock.t
    sched = client.get_schedule("reinvent2026")
    assert clock.t - t0 == 7
    assert sched.reserved == []


def test_429_twice_raises(fake, client):
    fake.throttle_next("GetSchedule", 1)
    orig = fake._handle

    def always_throttle(req):
        fake._throttle["GetSchedule"] = 1
        return orig(req)
    client.http = __import__("httpx").Client(base_url=client.base_url,
                                              transport=__import__("httpx").MockTransport(always_throttle))
    with pytest.raises(Throttled):
        client.get_schedule("reinvent2026")


def test_401_refreshes_once(fake, client):
    fake.token = "new"
    calls = []

    def refresh():
        calls.append(1)
        client.token_provider = lambda: "new"
    client.token_provider = lambda: "old"
    client.on_refresh = refresh
    sched = client.get_schedule("reinvent2026")
    assert calls == [1] and sched is not None


def test_403_is_not_registered(fake, client):
    fake._handle_orig = fake._handle

    def deny(req):
        import httpx
        return httpx.Response(403, json={"message": "not registered"})
    import httpx
    client.http = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(deny))
    with pytest.raises(NotRegistered):
        client.get_schedule("reinvent2026")


def test_personal_time_roundtrip(fake, client):
    body = PersonalTimeInput(startDateTime="2026-12-01T17:00:00", endDateTime="2026-12-01T17:15:00",
                             title="Leave now", description="Walk to Wynn", location="Venetian")
    client.create_personal_time("reinvent2026", body)
    sched = client.get_schedule("reinvent2026")
    assert sched.personal_time[0].title == "Leave now"
    pid = sched.personal_time[0].personal_time_id
    client.delete_personal_time("reinvent2026", pid)
    client.delete_personal_time("reinvent2026", pid)  # idempotent
    assert client.get_schedule("reinvent2026").personal_time == []


def test_personal_time_rejects_bad_length(fake, client):
    from reseat.client import ApiError
    body = PersonalTimeInput(startDateTime="2026-12-01T17:00:00", endDateTime="2026-12-01T17:07:00",
                             title="x", description="y")
    with pytest.raises(ApiError) as e:
        client.create_personal_time("reinvent2026", body)
    assert e.value.status == 400
