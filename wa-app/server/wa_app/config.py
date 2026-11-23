"""Environment -> Settings."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    store_path: Path
    secure_cookies: bool
    agent_token: str | None = None  # bearer token for /api/agent/*; unset = those routes answer 404


def _default_store_path() -> Path:
    """Same resolution order as whatsapp-mcp-server/lib/utils.py (minus the WA_STORE_PATH override)."""
    if os.path.exists("/app/store"):
        return Path("/app/store")
    project_root = Path(__file__).resolve().parents[3]
    local_dev = project_root / "whatsapp-bridge" / "store"
    if local_dev.exists():
        return local_dev
    return project_root / "store"


def load() -> Settings:
    store = os.getenv("WA_STORE_PATH")
    data = os.getenv("WA_APP_DATA")
    return Settings(
        host=os.getenv("WA_APP_HOST", "127.0.0.1"),
        port=int(os.getenv("WA_APP_PORT", "8085")),
        data_dir=Path(data) if data else Path.home() / ".local" / "share" / "wa-app",
        store_path=Path(store) if store else _default_store_path(),
        secure_cookies=os.getenv("WA_APP_INSECURE_COOKIES") != "1",
        agent_token=(os.getenv("WA_APP_AGENT_TOKEN") or "").strip() or None,
    )
