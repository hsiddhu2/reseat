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

HOME = Path(os.environ.get("RESEAT_HOME", Path.home() / ".reseat"))
DB_PATH = HOME / "reseat.db"
RULES_PATH = HOME / "rules.yaml"


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME
