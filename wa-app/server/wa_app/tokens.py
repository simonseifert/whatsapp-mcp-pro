"""Session token hashing, shared by auth and live (live must not import auth)."""

import hashlib


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
