"""Encrypt API keys at rest with Fernet. The key file lives next to app.db, mode 600."""

import os
from pathlib import Path

from cryptography.fernet import Fernet

from ..config import Settings

KEY_FILE = "ai.key"


def _key_path(settings: Settings) -> Path:
    return settings.data_dir / KEY_FILE


def _fernet(settings: Settings) -> Fernet:
    path = _key_path(settings)
    if not path.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # lost a race with another thread: use its key
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
    return Fernet(path.read_bytes().strip())


def encrypt(settings: Settings, plain: str) -> bytes:
    return _fernet(settings).encrypt(plain.encode())


def decrypt(settings: Settings, blob: bytes) -> str:
    return _fernet(settings).decrypt(blob).decode()
