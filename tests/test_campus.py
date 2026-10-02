from datetime import UTC, datetime

from reseat.campus import (
    CUTOFF_MINUTES,
    VENUES,
    normalize_venue,
    overlaps,
    room_label,
    round_to_5,
    session_start_utc,
    session_window,
    to_personal_time,
    walk_minutes,
)


def test_2026_campus_has_no_mandalay_bay():
    assert "Mandalay Bay" not in VENUES
    assert "Caesars Palace" in VENUES


def test_normalize_from_room_prefix():
    assert normalize_venue(None, "Venetian, Level 2, Titian 2301") == "The Venetian"
    assert normalize_venue("Caesars Forum", None) == "Caesars Forum"
    assert normalize_venue("MGM", None) == "MGM Grand"


def test_walk_symmetry_and_same_venue():
    assert walk_minutes("MGM Grand", "Wynn") == walk_minutes("Wynn", "MGM Grand") == 55
    assert walk_minutes("Wynn", "Wynn") == 10
    assert walk_minutes(None, "Wynn") is None


def test_vegas_is_pst_in_december():
    start = session_start_utc("2026-12-01", "10:00")
    assert start == datetime(2026, 12, 1, 18, 0, tzinfo=UTC)


def test_personal_time_format():
    dt = datetime(2026, 12, 1, 18, 3, 45, tzinfo=UTC)
    assert to_personal_time(dt) == "2026-12-01T18:03:00"
    assert round_to_5(dt).minute == 0
    assert round_to_5(dt, up=True).minute == 5


def test_overlap():
    a = session_window("2026-12-02", "14:00", 60)
    b = session_window("2026-12-02", "14:30", 60)
    c = session_window("2026-12-02", "15:00", 60)
    assert overlaps(a, b) and not overlaps(a, c)


def test_cutoff_is_eleven():
    assert CUTOFF_MINUTES == 11


def test_normalize_real_2026_strings():
    # Strings copied from the reinvent2026 catalog pulled on 1 Oct 2026.
    assert normalize_venue("Venetian", "Level 2 | Ballroom F | Content Hub | Red Theater") == "The Venetian"
    assert normalize_venue("MGM Grand", "Level 3 | Chairman's 363 | Content Hub | Code Talk") == "MGM Grand"
    forum_room = "Level 1 | Forum 120 | Content Hub | Blue Theater"
    assert normalize_venue("Caesars Forum", forum_room) == "Caesars Forum"
    assert normalize_venue(None, "Caesars Palace | Promenade Level | Trevi") == "Caesars Palace"
    assert normalize_venue(None, "") is None
    assert normalize_venue(None, None) is None


def test_wynn_encore_split_by_room():
    # The catalog sends one combined venue. Longest-alias matching used to call all of it Encore.
    assert normalize_venue(None, "Wynn/Encore | Convention Promenade | Latour 5") == "Wynn"
    assert normalize_venue(None, "Wynn/Encore | Upper Convention Promenade | Cristal 2 | Content Hub | "
                                 "Lightning Theater") == "Wynn"
    assert normalize_venue(None, "Wynn/Encore | Level 1 | Chopin 2") == "Encore"
    assert normalize_venue(None, "Wynn/Encore | Level 1 | Encore Ballroom 3") == "Encore"
    assert normalize_venue(None, "Wynn/Encore | Level 1 | Brahms 4") == "Encore"
    assert normalize_venue("Wynn/Encore", None) == "Wynn"


def test_room_label_strips_venue_prefix_only_when_venue_missing():
    assert room_label(None, "Caesars Palace | Promenade Level | Trevi") == "Promenade Level | Trevi"
    assert room_label(None, "Wynn/Encore | Level 1 | Chopin 2") == "Level 1 | Chopin 2"
    assert room_label("MGM Grand", "Level 1 | Grand 117") == "Level 1 | Grand 117"
    assert room_label(None, "Level 1 | Grand 117") == "Level 1 | Grand 117"
    assert room_label(None, None) is None
