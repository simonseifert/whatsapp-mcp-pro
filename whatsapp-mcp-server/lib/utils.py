"""Utility functions and logging setup for WhatsApp MCP server."""

import logging
import os
import sqlite3
from pathlib import Path

# Load .env file from project root - check multiple locations
try:
    from dotenv import load_dotenv

    # Try multiple locations for .env
    possible_paths = [
        Path(__file__).parent.parent.parent / ".env",  # /app/.env (Docker)
        Path(__file__).parent.parent.parent.parent / ".env",  # /whatsapp-mcp-extended/.env (local)
        Path.cwd() / ".env",  # Current working directory
    ]

    for env_path in possible_paths:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass  # python-dotenv not available, continue without it


# Set up logging
def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure and return the application logger.

    Args:
        debug: Enable DEBUG level logging if True.

    Returns:
        Configured logger instance.
    """
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    return logging.getLogger("whatsapp-mcp")


# Initialize logger based on DEBUG env var
logger = setup_logging(os.getenv("DEBUG", "false").lower() == "true")


# Database paths — resolution order:
#   1. WA_STORE_PATH env var (explicit override)
#   2. /app/store  (Docker bind mount)
#   3. <project_root>/whatsapp-bridge/store  (local dev, bridge-relative)
#   4. <project_root>/store  (legacy / symlink)
_wa_store_env = os.getenv("WA_STORE_PATH")
if _wa_store_env:
    _store_path = _wa_store_env
elif os.path.exists("/app/store"):
    _store_path = "/app/store"
else:
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    _local_dev_path = os.path.join(_project_root, "whatsapp-bridge", "store")
    _legacy_path = os.path.join(_project_root, "store")
    _store_path = _local_dev_path if os.path.exists(_local_dev_path) else _legacy_path

MESSAGES_DB_PATH = os.path.join(_store_path, "messages.db")
WHATSAPP_DB_PATH = os.path.join(_store_path, "whatsapp.db")

# Fail loudly on import if the bridge DB is missing — sqlite3.connect() silently
# creates an empty file at a wrong path, which causes every query to return empty
# results with no error. Set WA_SKIP_DB_CHECK=1 to bypass (tests, lint).
if not os.getenv("WA_SKIP_DB_CHECK") and not os.path.exists(MESSAGES_DB_PATH):
    raise FileNotFoundError(
        f"WhatsApp MCP: messages.db not found at {MESSAGES_DB_PATH}\n"
        f"  Bridge store resolved to: {_store_path}\n"
        f"  Override with: WA_STORE_PATH=/path/to/store\n"
        f"  Bridge not running or store not mounted?"
    )


# Bridge API configuration
_bridge_host = os.getenv("BRIDGE_HOST", "localhost:8080")
if ":" not in _bridge_host:
    _bridge_host = f"{_bridge_host}:8080"
BRIDGE_HOST = _bridge_host
WHATSAPP_API_BASE_URL = f"http://{BRIDGE_HOST}/api"


# Merge chains are one hop in practice (LID -> phone JID). The cap only exists
# so a corrupt store with a cycle cannot spin a tool call forever.
_MAX_MERGE_HOPS = 8


def canonical_chat_jid(conn: sqlite3.Connection, jid: str) -> str:
    """Follow chats.merged_into from ``jid`` to the chat that now holds its messages.

    The bridge folds a LID-filed chat into its phone-JID twin and keeps the old
    row as an empty shell stamped with merged_into (see MergeChat in
    whatsapp-bridge/internal/database/identities.go). A caller holding the old
    LID would otherwise read the shell and see no messages. Takes the caller's
    connection rather than a path so it reads whichever store the caller does.

    Returns ``jid`` unchanged when it is not merged, unknown, the store predates
    the column, or the chain loops.
    """
    if not jid:
        return jid
    current = jid
    seen = {jid}
    for _ in range(_MAX_MERGE_HOPS):
        try:
            row = conn.execute("SELECT merged_into FROM chats WHERE jid = ?", (current,)).fetchone()
        except sqlite3.OperationalError:
            # A store from before merged_into existed has nothing to follow.
            return current
        # The bridge treats NULL and '' alike as "not merged".
        if not row or not row[0]:
            return current
        nxt = row[0]
        if nxt in seen:
            logger.warning("chats.merged_into loops at %s (from %s); using the original JID", nxt, jid)
            return jid
        seen.add(nxt)
        current = nxt
    logger.warning("chats.merged_into chain from %s exceeds %d hops; using the original JID", jid, _MAX_MERGE_HOPS)
    return jid
