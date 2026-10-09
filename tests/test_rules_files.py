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
    os.chmod(tmp, 0o755)                                        # others can enter the folder
    r = CliRunner().invoke(cli.app, ["rules", "check"])
    assert "other users on this machine can read it" in " ".join(r.output.split())
    assert mode(path) == 0o644                                  # warned, never changed behind you
    os.chmod(path, 0o600)
    assert "other users" not in " ".join(CliRunner().invoke(cli.app, ["rules", "check"]).output.split())
    os.chmod(path, 0o644)
    os.chmod(tmp, 0o700)              # the file's own mode decides, whatever the folder allows
    assert "other users" in " ".join(CliRunner().invoke(cli.app, ["rules", "check"]).output.split())


def test_no_warning_without_a_secret(home):
    tmp, _ = home
    path = tmp / "rules.yaml"
    path.write_text("targets: []\n")
    os.chmod(path, 0o644)
    assert "other users" not in " ".join(CliRunner().invoke(cli.app, ["rules", "check"]).output.split())


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


def test_a_new_home_folder_is_owner_only(tmp_path, monkeypatch):
    home = tmp_path / "new" / ".reseat"
    monkeypatch.setattr(config, "HOME", home)
    config.ensure_home()
    assert mode(home) == 0o700


def test_the_default_home_is_tightened_but_a_chosen_folder_is_left_alone(tmp_path, monkeypatch):
    default, chosen = tmp_path / "default", tmp_path / "chosen"
    for d in (default, chosen):
        d.mkdir()
        os.chmod(d, 0o755)
    monkeypatch.setattr(config, "DEFAULT_HOME", default)
    monkeypatch.setattr(config, "HOME", default)
    config.ensure_home()
    assert mode(default) == 0o700
    monkeypatch.setattr(config, "HOME", chosen)                 # RESEAT_HOME, already there
    config.ensure_home()
    assert mode(chosen) == 0o755


def test_the_database_is_owner_only_new_or_existing(tmp_path):
    new = tmp_path / "new.db"
    Store(new).close()
    assert mode(new) == 0o600
    old = tmp_path / "old.db"
    Store(old).close()
    os.chmod(old, 0o644)                                        # made before this rule
    st = Store(old)
    st.apply_sweep("e", [], with_abstracts=False, force=True)
    st.close()
    assert mode(old) == 0o600


def test_a_symlinked_database_is_not_chmodded_through(tmp_path):
    real = tmp_path / "real.db"
    real.touch()
    os.chmod(real, 0o644)
    (tmp_path / "link.db").symlink_to(real)
    Store(tmp_path / "link.db").close()
    assert mode(real) == 0o644


SECRET = "targets: []\nserve_secret: abcdefghijklmnopqrstu\n"


def test_a_link_to_a_readable_file_elsewhere_is_still_flagged(tmp_path):
    private, open_dir = tmp_path / "private", tmp_path / "open"
    private.mkdir(mode=0o700)
    open_dir.mkdir()
    os.chmod(open_dir, 0o755)
    target = open_dir / "rules.yaml"
    target.write_text(SECRET)
    os.chmod(target, 0o644)
    link = private / "rules.yaml"
    link.symlink_to(target)
    os.chmod(private, 0o700)
    assert R.exposure_warning(link, R.load(link))
    hard = private / "hard.yaml"
    os.link(target, hard)                                       # a second way in from an open folder
    assert R.exposure_warning(hard, R.load(hard))


def test_a_symlinked_default_home_is_not_chmodded_through(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o755)
    link = tmp_path / ".reseat"
    link.symlink_to(real)
    monkeypatch.setattr(config, "DEFAULT_HOME", link)
    monkeypatch.setattr(config, "HOME", link)
    config.ensure_home()
    assert mode(real) == 0o755


def test_leftover_sqlite_side_files_are_tightened(tmp_path):
    db = tmp_path / "reseat.db"
    Store(db).close()
    side = tmp_path / "reseat.db-journal"
    side.write_text("")
    os.chmod(side, 0o644)
    Store(db).close()
    assert mode(side) == 0o600


def test_reseat_home_expands_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.home_from({"RESEAT_HOME": "~/rs"}) == tmp_path / "rs"
    assert config.home_from({}) == config.DEFAULT_HOME
