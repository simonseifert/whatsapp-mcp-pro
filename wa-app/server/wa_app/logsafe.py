"""Log values that a chat partner chooses (WhatsApp message ids, JIDs) must not be able to forge a log line."""

import unicodedata


def log_safe(value: object, limit: int = 80) -> str:
    """Control and line-separator characters become spaces; the length is capped."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in str(value))
    return text if len(text) <= limit else text[:limit] + "..."
