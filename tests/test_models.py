from reseat.models import OBSERVED_TYPES, Session, normalize_type


def test_curly_and_straight_apostrophes_compare_equal():
    # The planner UI shows U+2019. The Events API sends U+0027.
    assert normalize_type("Builders’ session") == "Builders' session"
    assert normalize_type("  Builders'  session ") == "Builders' session"
    assert normalize_type(None) is None
    assert "Builders' session" in OBSERVED_TYPES


def test_session_campus_venue_from_room_when_venue_null():
    s = Session.model_validate({
        "sessionId": "X", "title": "t", "venue": None, "type": "Builders’ session",
        "room": "Wynn/Encore | Level 1 | Debussy 1",
    })
    assert s.campus_venue == "Encore"
    assert s.room_label == "Level 1 | Debussy 1"
    assert s.type_key == "Builders' session"
