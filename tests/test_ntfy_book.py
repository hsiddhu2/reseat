"""Book buttons on the push: their own opt-in, and at most NTFY_BOOKS_PER_DAY bookings a day from a tap."""

from types import SimpleNamespace

from reseat import push as P
from reseat import serve as S
from reseat.offers import BookResult
from reseat.watcher import WatchEvent


class StubWatcher:
    def __init__(self, offers=(), proposals=()):
        self.offers = {o: SimpleNamespace(plan_id=o) for o in offers}
        self.proposals = set(proposals)

    def pending_offers(self):
        return list(self.offers.values())

    def is_pending(self, pid):
        return pid in self.offers or pid in self.proposals


def app(book=False, approve=False, offers=(), proposals=()):
    """A bare App with only what handle_reply reads. approve() is stubbed to a booked result."""
    a = S.App.__new__(S.App)
    a.rules = SimpleNamespace(ntfy_book=book, ntfy_approve=approve)
    a.watcher = StubWatcher(offers, proposals)
    a.clock = lambda: 1_796_000_000.0
    a._lock = S.threading.Lock()
    a._replied = S.deque(maxlen=200)
    a._ntfy_books = []
    a.ran = []

    def approve_(pid, wait=None):
        a.ran.append(pid)
        return {"kind": "book", "state": BookResult(pid, "booked").state}
    a.approve = approve_
    a.skip = lambda pid: True
    return a


def pid(i):
    return f"offer{i:02d}" + "x" * 16


def test_a_book_tap_does_nothing_without_ntfy_book_even_with_ntfy_approve():
    a = app(approve=True, offers=[pid(1)])
    assert a.handle_reply("approve", pid(1)) is None and a.ran == []


def test_a_swap_tap_does_nothing_with_only_ntfy_book():
    a = app(book=True, proposals=["swap" + "y" * 20])
    assert a.handle_reply("approve", "swap" + "y" * 20) is None and a.ran == []


def test_book_taps_stop_at_the_daily_cap_and_say_so():
    ids = [pid(i) for i in range(S.NTFY_BOOKS_PER_DAY + 1)]
    a = app(book=True, offers=ids)
    for i in ids[:-1]:
        assert a.handle_reply("approve", i) is None
    title, body = a.handle_reply("approve", ids[-1])
    assert title == "Not booked" and "web app" in body and a.ran == ids[:-1]
    assert ids[-1] not in a._replied                 # the offer still waits for the web app


def test_the_cap_resets_on_a_new_las_vegas_day():
    ids = [pid(i) for i in range(S.NTFY_BOOKS_PER_DAY + 1)]
    a = app(book=True, offers=ids)
    for i in ids[:-1]:
        a.handle_reply("approve", i)
    a.clock = lambda: 1_796_000_000.0 + 86400
    assert a.handle_reply("approve", ids[-1]) is None and a.ran[-1] == ids[-1]


def test_dismiss_from_the_push_does_not_count_toward_the_cap():
    a = app(book=True, offers=[pid(1)])
    assert a.handle_reply("skip", pid(1)) == ("Dismissed", "Not booked. Nothing was sent.")
    assert a._ntfy_books == []


def test_book_buttons_are_added_only_with_book_on():
    ev = WatchEvent("offer", 0, {"plan_id": pid(1), "session_id": "N1", "reason": "added", "code": "NEW101",
                                 "band": "available"})
    for book, has in ((False, False), (True, True)):
        p = P.Pusher("t" * 20, approve=True, book=book)
        p(ev)
        title, body, page, buttons = p._q.get_nowait()
        assert (buttons is not None) is has and page == "/approve"
        assert ("Long-press for Book" in body) is has
