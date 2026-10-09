"""Constants and paths. Values come from the AWS Events API developer guide."""

from __future__ import annotations

import os
from pathlib import Path

API_BASE = os.environ.get("RESEAT_API_BASE", "https://api.awsevents.com")
OAUTH_BASE = os.environ.get("RESEAT_OAUTH_BASE", "https://oauth.awsevents.com")
CLIENT_ID = "7vmom55m1qstvq8i71ph127bfq"
SCOPES = "openid email events/access"
IDENTITY_PROVIDER = "AWSBuilderID"
CALLBACK_PORTS = range(8484, 8490)
DEFAULT_EVENT = "reinvent2026"

KEYRING_SERVICE = "reseat"
KEYRING_USER = "tokens"

DEFAULT_HOME = Path.home() / ".reseat"


def home_from(environ: dict[str, str] | os._Environ[str]) -> Path:
    """RESEAT_HOME if set, with ~ expanded, else ~/.reseat."""
    return Path(environ.get("RESEAT_HOME", DEFAULT_HOME)).expanduser()


HOME = home_from(os.environ)
DB_PATH = HOME / "reseat.db"
RULES_PATH = HOME / "rules.yaml"


def ensure_home() -> Path:
    """re:Seat's folder holds the catalog, the schedule journal and the rules file, which can hold
    serve_secret. A folder re:Seat creates is owner-only (0700). An existing default ~/.reseat is
    tightened to 0700 too. A folder named by RESEAT_HOME that already exists is the attendee's
    own choice and is left as it is."""
    if not HOME.exists() and not HOME.is_symlink():
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(HOME, 0o700)                 # mkdir's mode is cut by the umask
    elif HOME == DEFAULT_HOME and not HOME.is_symlink():
        _owner_only(HOME, 0o700)
    return HOME


def _owner_only(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass                     # not ours to change, for example on a shared mount
