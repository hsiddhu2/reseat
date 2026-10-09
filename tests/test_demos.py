"""The recordable demos must keep working and must show what demo/README.md says they show.
Each runs against the fake API only."""

import runpy
import sys
from pathlib import Path

import httpx
import pytest

from reseat import cli, config

DEMO = Path(__file__).resolve().parent.parent / "demo"


@pytest.fixture
def demo_env(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RESEAT_DEMO_PACE", "0")
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    for name in ("HOME", "DB_PATH", "RULES_PATH"):          # the harness repoints these
        monkeypatch.setattr(config, name, getattr(config, name))
    for name in ("_client", "_store"):
        monkeypatch.setattr(cli, name, getattr(cli, name))
    monkeypatch.syspath_prepend(str(DEMO))
    sys.modules.pop("_harness", None)
    yield capsys
    sys.modules.pop("_harness", None)


def _run(name: str, capsys) -> tuple[dict, str]:
    g = runpy.run_path(str(DEMO / f"{name}.py"), run_name="__main__")
    out = " ".join(capsys.readouterr().out.split())
    assert "Nothing is sent to AWS" in out
    return g, out


def _ids(g: dict, *codes: str) -> set[str]:
    sid = sys.modules["_harness"].sid
    return {sid(g["d"], c) for c in codes}


def test_book_falls_back_and_skips_the_full_sitting(demo_env):
    g, out = _run("book", demo_env)
    fake = g["d"].fake
    assert fake.schedule.reserved == _ids(g, "CON304-R", "API303-R1", "ARC302-R1", "ANT316-R")
    assert "full API303 API303-R sessionFull" in out          # filled as it was booked
    assert "reserved API303 API303-R1" in out                 # its next sitting, same run
    assert "skip ARC302 ARC302-R" in out                      # already unavailable, never sent
    assert "Reserved 4 of 4 targets" in out


def test_watch_books_a_freed_seat_then_a_new_repeat(demo_env):
    g, out = _run("watch", demo_env)
    assert g["d"].fake.schedule.reserved == _ids(g, "ARC305-R1", "CMP327-R2")
    assert out.index("booked ARC305 ARC305-R1") < out.index("booked CMP327 CMP327-R2")


def test_swap_rolls_back_then_succeeds(demo_env):
    g, out = _run("swap", demo_env)
    assert g["d"].fake.schedule.reserved == {g["B"]}
    first, second = out.split("Attempt 2", 1)
    assert "swap CMP409-R -> SVS306-R: proposed -> checked -> cancelled -> rolled_back" in first
    assert first.rstrip().endswith("held now: CMP409-R") or "held now: CMP409-R" in first
    assert "cancelled -> reserved -> verified" in second and "held now: SVS306-R" in second
    assert g["A"] not in out.split("is CMP409-R")[1].replace(f"reseat swap {g['A']}", "")


def test_phone_page_serves_the_proposal_and_approve_runs_it(demo_env):
    g = runpy.run_path(str(DEMO / "phone.py"), run_name="demo_phone")
    with pytest.raises(SystemExit):
        g["start"]("0.0.0.0", 0)                              # never on every interface
    app, server, worker = g["start"]("127.0.0.1", 0)
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{app.port}", timeout=30)
        link = app.one_time_link("127.0.0.1").split(str(app.port), 1)[1]
        assert http.get(link).status_code == 303                # sets the cookie, clean redirect
        state = http.get("/api/state").json()
        assert state["day"] == "2026-11-30" and state["next_leave"]["code"] == "ARC302-R"
        [p] = state["proposals"]
        r = http.post(f"/approve/{p['plan_id']}", headers={"X-Reseat": "1"})
        assert r.status_code == 200 and r.json()["state"] == "verified"
    finally:
        g["stop"](app, server, worker)
