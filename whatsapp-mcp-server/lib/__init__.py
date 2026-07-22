"""Shared internals for the WhatsApp MCP server.

Deliberately small. The MCP tool surface lives in `main.py` and its
implementation in `whatsapp.py`; this package holds only what those two
actually import:

- utils:      paths, logger, bridge URL
- bridge:     auth headers for the bridge REST API
- recall:     semantic search over message history (pro toolset)
- transcribe: local voice-note transcription (pro toolset)

An earlier parallel implementation (models/database, and most of bridge) was
removed: production never called it, but it was 1,700 lines and the CI coverage
gate pointed at it, so the coverage number measured code nothing ran.
"""

from .bridge import BridgeError, _get_headers
from .utils import (
    BRIDGE_HOST,
    MESSAGES_DB_PATH,
    WHATSAPP_API_BASE_URL,
    WHATSAPP_DB_PATH,
    logger,
    setup_logging,
)

__all__ = [
    "BridgeError",
    "_get_headers",
    "logger",
    "setup_logging",
    "MESSAGES_DB_PATH",
    "WHATSAPP_DB_PATH",
    "BRIDGE_HOST",
    "WHATSAPP_API_BASE_URL",
]
