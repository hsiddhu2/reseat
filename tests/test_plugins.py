"""The plugin and Power files run the release they name. A stale pin would start an old server."""

import json
from pathlib import Path

import reseat

ROOT = Path(__file__).resolve().parent.parent
PIN = f"reseat[mcp]=={reseat.__version__}"
NAMES = ("reseat", "reseat-demo")


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def _args(servers: dict, name: str) -> list[str]:
    s = servers["mcpServers"][name]
    assert s["command"] == "uvx"
    return s["args"]


def test_marketplace_lists_both_plugins_and_their_folders_exist():
    m = _load(".claude-plugin/marketplace.json")
    assert sorted(p["name"] for p in m["plugins"]) == sorted(NAMES)
    for p in m["plugins"]:
        assert p["source"].startswith("./")
        assert (ROOT / p["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_every_manifest_and_server_pins_this_version():
    for name in NAMES:
        for manifest in (f"plugins/{name}/.claude-plugin/plugin.json", f"plugins/kiro/{name}/plugin.json"):
            m = _load(manifest)
            assert m["name"] == name and m["version"] == reseat.__version__
        for servers in (f"plugins/{name}/.mcp.json", f"plugins/kiro/{name}/mcp.json"):
            args = _args(_load(servers), name)
            assert args[:4] == ["--python", "3.12", "--from", PIN]
            assert args[4:6] == ["reseat", "mcp"]


def test_only_the_demo_entries_run_the_demo():
    for servers in ("plugins/reseat-demo/.mcp.json", "plugins/kiro/reseat-demo/mcp.json"):
        assert _args(_load(servers), "reseat-demo")[-1] == "--demo"
    for servers in ("plugins/reseat/.mcp.json", "plugins/kiro/reseat/mcp.json"):
        assert "--demo" not in _args(_load(servers), "reseat")


def test_each_server_file_holds_one_server_with_exact_args():
    for name in NAMES:
        tail = ["--demo"] if name == "reseat-demo" else []
        for servers in (f"plugins/{name}/.mcp.json", f"plugins/kiro/{name}/mcp.json"):
            s = _load(servers)
            assert list(s["mcpServers"]) == [name]
            assert _args(s, name) == ["--python", "3.12", "--from", PIN, "reseat", "mcp", *tail]


def test_marketplace_names_match_the_plugin_folders_and_none_is_unlisted():
    m = _load(".claude-plugin/marketplace.json")
    for p in m["plugins"]:
        assert _load(f"{p['source']}/.claude-plugin/plugin.json")["name"] == p["name"]
    folders = {d.name for d in (ROOT / "plugins").iterdir() if (d / ".claude-plugin").is_dir()}
    assert folders == set(NAMES)
    assert {d.name for d in (ROOT / "plugins" / "kiro").iterdir() if d.is_dir()} == set(NAMES)


def test_readme_pins_this_version_wherever_it_names_a_release():
    """README's uvx and plugin text must name the same release the plugin files run."""
    import re
    pins = re.findall(r"reseat\[mcp\]==([0-9][0-9A-Za-z.]*)", (ROOT / "README.md").read_text())
    assert pins and set(pins) == {reseat.__version__}


def test_guide_pages_pin_this_version_wherever_they_name_a_release():
    """The guides in docs/guide/ must name the same release as README, or a bump misses them."""
    import re
    pages = sorted((ROOT / "docs" / "guide").glob("*.md"))
    pins = {p.name: re.findall(r"reseat\[mcp\]==([0-9][0-9A-Za-z.]*)", p.read_text()) for p in pages}
    assert pages and pins.get("mcp.md"), "the MCP guide names the release it installs"
    for name, found in pins.items():
        assert set(found) <= {reseat.__version__}, name


def test_sdist_ships_the_plugin_files_this_test_reads():
    """tests/ ships in the sdist, so the files it reads must ship too."""
    import tomllib
    inc = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert {"plugins", ".claude-plugin"} <= set(inc["only-include"])
