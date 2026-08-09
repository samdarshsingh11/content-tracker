"""Configuration, loaded from a .env file sitting next to this module.

Nothing secret is ever hardcoded here. Copy .env.example to .env and fill it in.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE, # comments, optional surrounding quotes.

    Real environment variables always win, so you can override any value at the
    shell without editing the file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(ENV_FILE)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- Server -----------------------------------------------------------------
HOST = _get("HOST", "127.0.0.1")
PORT = int(_get("PORT", "8787"))

# --- MongoDB ----------------------------------------------------------------
MONGODB_URI = _get("MONGODB_URI")
MONGODB_DB = _get("MONGODB_DB", "content_tracker")
MONGODB_COLLECTION = _get("MONGODB_COLLECTION", "content_items")

# --- Meta / Instagram Graph API --------------------------------------------
# Graph API version. Bump this deliberately; Meta deprecates versions on a
# ~2 year cycle and silently changing it can change field behaviour.
GRAPH_VERSION = _get("META_GRAPH_VERSION", "v21.0")
IG_USER_ID = _get("IG_USER_ID")
IG_ACCESS_TOKEN = _get("IG_ACCESS_TOKEN")
META_APP_ID = _get("META_APP_ID")
META_APP_SECRET = _get("META_APP_SECRET")

# Safety rail: the app refuses to publish more than this many times per run
# unless you raise it. Meta's own limit is 25 posts / 24h per IG account.
PUBLISH_DAILY_LIMIT = int(_get("PUBLISH_DAILY_LIMIT", "25"))


def mongo_configured() -> bool:
    return bool(MONGODB_URI)


def meta_configured() -> bool:
    return bool(IG_USER_ID and IG_ACCESS_TOKEN)


def redacted_summary() -> dict:
    """What the UI is allowed to know about the config. No secrets leave here."""

    def tail(value: str) -> str:
        return f"...{value[-4:]}" if len(value) > 4 else ""

    return {
        "mongo_configured": mongo_configured(),
        "mongo_db": MONGODB_DB if mongo_configured() else None,
        "mongo_collection": MONGODB_COLLECTION if mongo_configured() else None,
        "meta_configured": meta_configured(),
        "graph_version": GRAPH_VERSION,
        "ig_user_id": IG_USER_ID or None,
        "token_hint": tail(IG_ACCESS_TOKEN) if IG_ACCESS_TOKEN else None,
        "app_id": META_APP_ID or None,
        "publish_daily_limit": PUBLISH_DAILY_LIMIT,
    }
