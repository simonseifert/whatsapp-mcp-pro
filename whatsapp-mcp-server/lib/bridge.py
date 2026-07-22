"""Bridge API client for WhatsApp Go bridge."""

from pathlib import Path

try:
    from dotenv import load_dotenv

    possible_paths = [
        Path(__file__).parent.parent.parent / ".env",
        Path(__file__).parent.parent.parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in possible_paths:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass

import os
from typing import Any

import requests

from .utils import WHATSAPP_API_BASE_URL, logger


class BridgeError(Exception):
    """Exception for bridge API errors."""

    pass


def _get_headers() -> dict[str, str]:
    """Get request headers including API key if configured."""
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("API_KEY") or os.getenv("WHATSAPP_API_KEY")
    logger.info(f"[BRIDGE-HEADERS] API_KEY loaded: {bool(api_key)}")
    if api_key:
        headers["X-API-Key"] = api_key
    return headers
