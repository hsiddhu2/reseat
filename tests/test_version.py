"""The package version and the pyproject version stay in step."""

import tomllib
from pathlib import Path

import reseat

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_version_matches_package() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == reseat.__version__
