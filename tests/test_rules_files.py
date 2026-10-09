"""Every writer of the rules file: owner-only permissions, and never through a symbolic link.
The file can hold serve_secret, the phone page's shared secret."""

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from reseat import cli, config
from reseat import rules as R
from reseat.client import EventsClient, QuotaTracker
from reseat.fakeapi import FakeEventsApi
from reseat.store import Store

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")


@pytest.fixture
def home(tmp_path, monkeypatch, clock):
    fake = FakeEventsApi(sessions=[])
    fake.schedule.reserved |= {"R1"}
    client = EventsClient(token_provider=lambda: fake.token, quota=QuotaTracker(clock=clock),
                          sleep=clock.sleep, transport=fake.transport())
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.yaml")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: Store(":memory:"))
    export = tmp_path / "export.json"
    export.write_text(json.dumps([{"id": "S1", "shortId": "ARC301-R"}]))
    return tmp_path, export


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def commands(export):
    return {
        "init": ["rules", "init"],
        "import": ["rules", "import", str(export)],
        "from-schedule": ["rules", "from-schedule"],
    }


@pytest.mark.parametrize("cmd", ["init", "import", "from-schedule"])
def test_created_owner_only(home, cmd):
    tmp, export = home
    r = CliRunner().invoke(cli.app, commands(export)[cmd])
    assert r.exit_code == 0, r.output
    assert mode(tmp / "rules.yaml") == 0o600
    assert sorted(p.name for p in tmp.iterdir()) == ["export.json", "rules.yaml"]   # no temp file


@pytest.mark.parametrize("cmd,force", [("init", "--force"), ("import", "--force"),
                                       ("from-schedule", "--force")])
def test_replacing_a_world_readable_file_leaves_it_owner_only(home, cmd, force):
    tmp, export = home
    path = tmp / "rules.yaml"
    path.write_text("targets: []\nserve_secret: abcdefghijklmnopqrstu\n")
    os.chmod(path, 0o644)
    r = CliRunner().invoke(cli.app, [*commands(export)[cmd], force])
    assert r.exit_code == 0, r.output
    assert mode(path) == 0o600
    if cmd == "from-schedule":
        assert mode(tmp / "rules.yaml.bak") == 0o600            # the backup keeps the secret too


@pytest.mark.parametrize("cmd", ["init", "import", "from-schedule"])
@pytest.mark.parametrize("dangling", [False, True])
def test_a_symlink_in_place_of_the_rules_file_is_refused(home, cmd, dangling):
    tmp, export = home
    target = tmp / "elsewhere.yaml"
    if not dangling:
        target.write_text("targets: []\n")
    (tmp / "rules.yaml").symlink_to(target)
    r = CliRunner().invoke(cli.app, [*commands(export)[cmd], "--force"])
    assert r.exit_code == 2 and "symbolic link" in " ".join(r.output.split())
    assert (tmp / "rules.yaml").is_symlink()
    assert (target.read_text() == "targets: []\n") if not dangling else not target.exists()
    assert not (tmp / "rules.yaml.bak").exists()


def test_write_new_refuses_a_symlink_directly(tmp_path):
    (tmp_path / "rules.yaml").symlink_to(tmp_path / "x")
    with pytest.raises(R.RulesError):
        R.write_new(tmp_path / "rules.yaml", "a: 1\n", replace=True)
    assert not (tmp_path / "x").exists()


def test_a_stale_temp_file_does_not_block_the_write(tmp_path):
    (tmp_path / f".rules.yaml.{os.getpid()}.tmp").write_text("junk")
    assert R.write_new(tmp_path / "rules.yaml", "a: 1\n")
    assert (tmp_path / "rules.yaml").read_text() == "a: 1\n"


def test_a_hand_made_readable_file_with_a_secret_is_flagged_not_changed(home):
    tmp, _ = home
    path = tmp / "rules.yaml"
    path.write_text("targets: []\nserve_secret: abcdefghijklmnopqrstu\n")
    os.chmod(path, 0o644)
    r = CliRunner().invoke(cli.app, ["rules", "check"])
    assert "other users on this machine can read it" in " ".join(r.output.split())
    assert mode(path) == 0o644                                  # warned, never changed behind you
    os.chmod(path, 0o600)
    assert "other users" not in CliRunner().invoke(cli.app, ["rules", "check"]).output


def test_no_warning_without_a_secret(home):
    tmp, _ = home
    path = tmp / "rules.yaml"
    path.write_text("targets: []\n")
    os.chmod(path, 0o644)
    assert "other users" not in CliRunner().invoke(cli.app, ["rules", "check"]).output


def test_read_nofollow_refuses_a_link(tmp_path):
    (tmp_path / "real").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert R.read_nofollow(tmp_path / "real") == "x"
    with pytest.raises(R.RulesError):
        R.read_nofollow(tmp_path / "link")


def test_a_filesystem_without_hard_links_still_gets_an_exclusive_owner_only_write(tmp_path, monkeypatch):
    def no_links(*a, **k):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_links)
    path = tmp_path / "rules.yaml"
    assert R.write_new(path, "a: 1\n") and path.read_text() == "a: 1\n" and mode(path) == 0o600
    assert not R.write_new(path, "b: 2\n") and path.read_text() == "a: 1\n"      # still no clobber


def test_an_os_failure_is_a_rules_error_not_a_traceback(home, monkeypatch):
    tmp, export = home
    (tmp / "rules.yaml").write_text("targets: []\n")

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(os, "replace", denied)
    r = CliRunner().invoke(cli.app, ["rules", "init", "--force"])
    assert r.exit_code == 2 and "Nothing written" in " ".join(r.output.split())
    assert "Traceback" not in r.output
    assert (tmp / "rules.yaml").read_text() == "targets: []\n"


def test_init_without_force_refuses_a_dangling_link(home):
    tmp, export = home
    (tmp / "rules.yaml").symlink_to(tmp / "nowhere")
    r = CliRunner().invoke(cli.app, ["rules", "init"])
    assert r.exit_code == 2 and "symbolic link" in " ".join(r.output.split())
    assert not (tmp / "nowhere").exists()


def test_a_symlinked_backup_is_refused_and_nothing_changes(home):
    tmp, _ = home
    (tmp / "rules.yaml").write_text("targets: []\nserve_secret: abcdefghijklmnopqrstu\n")
    victim = tmp / "victim"
    victim.write_text("keep\n")
    (tmp / "rules.yaml.bak").symlink_to(victim)
    r = CliRunner().invoke(cli.app, ["rules", "from-schedule", "--force"])
    assert r.exit_code == 2 and "symbolic link" in " ".join(r.output.split())
    assert victim.read_text() == "keep\n" and (tmp / "rules.yaml.bak").is_symlink()
    assert "serve_secret" in (tmp / "rules.yaml").read_text()


def test_from_schedule_refuses_a_link_before_any_api_call(home, monkeypatch):
    tmp, _ = home
    (tmp / "rules.yaml").symlink_to(tmp / "t.yaml")
    monkeypatch.setattr(cli, "_client", lambda: pytest.fail("no API call for a refused run"))
    assert CliRunner().invoke(cli.app, ["rules", "from-schedule", "--force"]).exit_code == 2


def test_import_refuses_a_link_before_reading_it(home):
    tmp, export = home
    target = tmp / "t.yaml"
    target.write_text("targets: [not, valid\n")                 # a read would fail on this YAML
    (tmp / "rules.yaml").symlink_to(target)
    r = CliRunner().invoke(cli.app, ["rules", "import", str(export), "--force"])
    assert r.exit_code == 2 and "symbolic link" in " ".join(r.output.split())
    assert target.read_text() == "targets: [not, valid\n"
