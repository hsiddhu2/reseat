import json
from pathlib import Path

import pytest

from reseat import rules as R
from reseat.fakeapi import sample_sessions
from reseat.store import Store

PLANNER = Path(__file__).parent / "fixtures" / "planner-export.sample.json"


def test_example_parses_and_round_trips():
    r = R.parse(R.EXAMPLE)
    assert [t.label for t in r.targets] == ["ARC301", "DOP302", "1780442277219001cKCh"]
    assert r.targets[0].backups == ["ARC302"]
    assert r.targets[1].prefer == "latest"
    assert r.max_per_day == 5 and r.watch_cap == 25 and r.buffer_minutes == 30
    assert R.parse(R.dump(r)) == r


def test_bad_yaml_names_the_line():
    with pytest.raises(R.RulesError, match="not valid YAML at line 3"):
        R.parse("targets:\n  - code: ARC301\n\t- code: DOP302\n")


def test_wrong_shape_is_refused():
    with pytest.raises(R.RulesError, match="mapping"):
        R.parse("- ARC301\n")
    with pytest.raises(R.RulesError, match="needs a code or a session_id"):
        R.parse("targets:\n  - auto_swap: true\n")
    with pytest.raises(R.RulesError, match="Extra inputs"):
        R.parse("targets: []\nwatchcap: 3\n")


def test_duplicate_code_is_refused_including_repeat_suffix():
    with pytest.raises(R.RulesError, match="target 2 repeats ARC301"):
        R.parse("targets:\n  - code: ARC301\n  - code: arc301-R2\n")


def test_cap_exceeded():
    body = "watch_cap: 2\ntargets:\n" + "".join(f"  - code: X{i}\n" for i in range(3))
    with pytest.raises(R.RulesError, match="3 targets, watch_cap is 2"):
        R.parse(body)


def test_meal_needs_a_real_day_and_order():
    R.parse("meals:\n  - {day: Tuesday, start: '12:00', end: '13:00'}\n")
    unquoted = R.parse("meals:\n  - day: 2026-12-01\n    start: 12:00\n    end: 13:30\n")
    assert unquoted.meals[0].model_dump() == {"day": "2026-12-01", "start": "12:00", "end": "13:30"}
    with pytest.raises(R.RulesError, match="not a weekday name"):
        R.parse("meals:\n  - {day: Someday, start: '12:00', end: '13:00'}\n")
    with pytest.raises(R.RulesError, match="ends before it starts"):
        R.parse("meals:\n  - {day: 2026-12-01, start: '13:00', end: '12:00'}\n")


def test_repeats_false_needs_a_sitting():
    with pytest.raises(R.RulesError, match="repeats: false needs"):
        R.parse("targets:\n  - code: ARC301\n    repeats: false\n")


def test_unknown_code_reported_by_check():
    store = Store(":memory:")
    store.apply_sweep("reinvent2026", sample_sessions(), with_abstracts=False)
    r = R.parse("targets:\n  - code: ARC301\n    backups: [NOPE999]\n  - code: ZZZ100\n"
                "  - session_id: missing-id\n")
    assert R.unresolved(r, store, "reinvent2026") == [
        "ARC301: backup NOPE999 not in catalog",
        "ZZZ100: code ZZZ100 not in catalog",
        "missing-id: session_id missing-id not in catalog",
    ]


def test_planner_import_keeps_order_ids_and_capacity():
    items = R.read_planner_file(PLANNER)
    r = R.from_planner_export(items)
    t = r.targets[0]
    assert t.code == "COP324"
    assert t.session_id == "1780442277219001cKCh"
    assert t.sittings == ["1780442277219001cKCh", "1790358255827001ioDc"]
    assert t.seat_capacity == 84


def test_planner_import_merges_two_sittings_of_one_talk(tmp_path):
    a = {"id": "A", "shortId": "ARC301-R", "repeats": [{"id": "B"}]}
    b = {"id": "C", "shortId": "ARC301-R2"}
    other = {"id": "D", "shortId": "SEC201"}
    r = R.from_planner_export([a, other, b])
    assert [t.code for t in r.targets] == ["ARC301", "SEC201"]
    assert r.targets[0].sittings == ["A", "B", "C"]


def test_planner_import_over_cap_and_bad_file(tmp_path):
    items = [{"id": f"S{i}", "shortId": f"X{i}"} for i in range(4)]
    with pytest.raises(R.RulesError, match="4 targets, watch_cap is 3"):
        R.from_planner_export(items, watch_cap=3)
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(R.RulesError, match="JSON list"):
        R.read_planner_file(p)


def test_load_missing_file_says_how_to_start(tmp_path):
    with pytest.raises(R.RulesError, match="reseat rules init"):
        R.load(tmp_path / "rules.yaml")


# ---------------------------------------------------------------- edit_targets on hand-written layouts

NESTED = ("targets:\n  - code: AAA100\n    backups:\n      - XXX100\n  # between items\n  - code: BBB100\n"
          "    backups:\n      - YYY100\nmax_per_day: 4\n")


def _labels(text):
    return [(t.label, t.backups) for t in R.parse(text).targets]


def test_edit_targets_keeps_comments_between_items_and_other_keys():
    out = R.edit_targets("# top\ntargets:\n  - code: AAA100\n  # note\n  - code: BBB100\nmax_per_day: 4\n",
                         ["{code: CCC100}"], [0])
    assert out == "# top\ntargets:\n  # note\n  - code: BBB100\n  - {code: CCC100}\nmax_per_day: 4\n"


def test_edit_targets_column_zero_items():
    text = "targets:\n- code: AAA100\n- code: BBB100\nmax_per_day: 4\n"
    out = R.edit_targets(text, ["{code: CCC100}"], [1])
    assert out == "targets:\n- code: AAA100\n- {code: CCC100}\nmax_per_day: 4\n"


def test_edit_targets_comment_inside_an_item_stays_with_its_item():
    text = "targets:\n  - code: AAA100\n  - code: BBB100\n    # mine\n    prefer: latest\nmax_per_day: 4\n"
    assert _labels(R.edit_targets(text, [], [1])) == [("AAA100", [])]
    assert R.parse(R.edit_targets(text, [], [1])).targets[0].prefer == "earliest"
    r = R.parse(R.edit_targets(text, ["{code: CCC100}"], []))
    assert [(t.label, t.prefer) for t in r.targets] == [("AAA100", "earliest"), ("BBB100", "latest"),
                                                        ("CCC100", "earliest")]


def test_edit_targets_crlf_file_parses_the_same_after_an_edit():
    text = "# c\r\ntargets:\r\n  - code: AAA100\r\n  # between\r\n  - code: BBB100\r\nmax_per_day: 4\r\n"
    out = R.edit_targets(text, ["{code: CCC100}"], [0])
    assert _labels(out) == [("BBB100", []), ("CCC100", [])] and R.parse(out).max_per_day == 4
    assert "# between" in out and "# c" in out


def test_edit_targets_a_remove_out_of_range_or_no_targets_line_raises():
    with pytest.raises(R.RulesError, match="not found"):
        R.edit_targets("targets:\n  - code: AAA100\n", [], [3])
    with pytest.raises(R.RulesError, match="no top-level targets"):
        R.edit_targets("max_per_day: 4\n", ["{code: AAA100}"], [])
    with pytest.raises(R.RulesError, match="no top-level targets"):
        R.edit_targets("targets: [{code: AAA100}]\n", ["{code: BBB100}"], [])


def test_edit_targets_removes_an_item_with_a_nested_block_list():
    assert _labels(R.edit_targets(NESTED, [], [0])) == [("BBB100", ["YYY100"])]


def test_edit_targets_removing_the_last_item_leaves_the_one_before_unchanged():
    assert _labels(R.edit_targets(NESTED, [], [1])) == [("AAA100", ["XXX100"])]


def test_edit_targets_appends_after_an_item_with_a_nested_block_list():
    assert _labels(R.edit_targets(NESTED, ["{code: CCC100}"], [])) == \
        [("AAA100", ["XXX100"]), ("BBB100", ["YYY100"]), ("CCC100", [])]
