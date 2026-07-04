"""Utility functions and logging setup for WhatsApp MCP server."""

import logging
import os
from pathlib import Path

# Load .env file from project root - check multiple locations
try:
    from dotenv import load_dotenv
    
    # Try multiple locations for .env
    possible_paths = [
        Path(__file__).parent.parent.parent / '.env',  # /app/.env (Docker)
        Path(__file__).parent.parent.parent.parent / '.env',  # /whatsapp-mcp-extended/.env (local)
        Path.cwd() / '.env',  # Current working directory
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
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger("whatsapp-mcp")


# Initialize logger based on DEBUG env var
logger = setup_logging(os.getenv("DEBUG", "false").lower() == "true")


# Database paths - resolve in priority order, then fail loudly if missing.
# Order: explicit env override → Docker mount → bridge-relative (local dev) → legacy.
# sqlite3.connect silently creates a fresh empty file at a wrong path, so a missing
# DB must be caught here, not at first query — see issue #1.
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_candidate_stores = [
    os.environ.get("WA_STORE_PATH"),                            # explicit override
    '/app/store' if os.path.exists('/app/store') else None,     # Docker
    os.path.join(_project_root, 'whatsapp-bridge', 'store'),    # local dev (bridge writes here)
    os.path.join(_project_root, 'store'),                       # legacy / symlink workaround
]
_store_path = next((p for p in _candidate_stores if p and os.path.exists(p)), None)
if _store_path is None:
    # Fall back to the bridge-relative path; the existence check below will surface
    # the problem with a clear, actionable error rather than failing silently.
    _store_path = os.path.join(_project_root, 'whatsapp-bridge', 'store')
MESSAGES_DB_PATH = os.path.join(_store_path, 'messages.db')
WHATSAPP_DB_PATH = os.path.join(_store_path, 'whatsapp.db')

if os.environ.get("WA_SKIP_DB_CHECK") != "1" and not os.path.exists(MESSAGES_DB_PATH):
    _tried = "\n  ".join(p for p in _candidate_stores if p)
    raise FileNotFoundError(
        f"WhatsApp messages DB not found at {MESSAGES_DB_PATH}.\n"
        f"Tried these store roots in order:\n  {_tried}\n"
        f"Make sure the Go bridge is running and has synced (it writes to "
        f"<repo>/whatsapp-bridge/store/messages.db). If your store lives elsewhere, "
        f"set WA_STORE_PATH to the directory containing messages.db. "
        f"Set WA_SKIP_DB_CHECK=1 to bypass this check (e.g. for tests/lint)."
    )


# Bridge API configuration
_bridge_host = os.getenv('BRIDGE_HOST', 'localhost:8080')
if ':' not in _bridge_host:
    _bridge_host = f"{_bridge_host}:8080"
BRIDGE_HOST = _bridge_host
WHATSAPP_API_BASE_URL = f"http://{BRIDGE_HOST}/api"

def get_sender_name(sender_jid: str) -> str:
    """Get display name for a sender JID.

    Args:
        sender_jid: WhatsApp JID of the sender.

    Returns:
        Display name or phone number if name not found.
    """
    from .database import get_contact_by_jid

    if sender_jid.endswith("@s.whatsapp.net"):
        contact = get_contact_by_jid(sender_jid)
        if contact:
            return contact.name or contact.push_name or contact.phone_number
    return sender_jid.split("@")[0]
