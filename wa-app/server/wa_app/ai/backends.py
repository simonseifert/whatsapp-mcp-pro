"""Talk to a model and get text back. Errors carry short user-safe messages: never the key, prompt or a raw body."""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass

import httpx2

from ..config import Settings
from .config import BackendCfg

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 600
MAX_ALTERNATIVES = 2


@dataclass
class Prompt:
    system: str
    user: str


class BackendError(Exception):
    """The message is safe to show the user."""


async def complete(
    cfg: BackendCfg,
    secret: str | None,
    prompt: Prompt,
    settings: Settings,
    *,
    timeout: float = 120,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> str:
    """Raw model text. `transport` lets tests swap the network out."""
    try:
        async with asyncio.timeout(timeout):
            if cfg.kind == "claude_cli":
                return await _claude_cli(cfg, prompt, settings)
            if cfg.kind == "openai":
                return await _openai(cfg, secret, prompt, timeout, transport)
            if cfg.kind == "anthropic":
                return await _anthropic(cfg, secret, prompt, timeout, transport)
    except TimeoutError:
        raise BackendError("AI didn't answer in time") from None
    raise BackendError("Unknown AI backend")


async def _post(url: str, headers: dict, body: dict, timeout: float, transport) -> dict:
    try:
        async with httpx2.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx2.TimeoutException:
        raise BackendError("AI didn't answer in time") from None
    except httpx2.HTTPError:
        raise BackendError("Couldn't reach the AI") from None
    if resp.status_code >= 400:
        raise BackendError(f"AI request failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError:
        raise BackendError("AI sent an unreadable answer") from None
    if not isinstance(data, dict):
        raise BackendError("AI sent an unreadable answer")
    return data


async def _openai(cfg: BackendCfg, secret: str | None, prompt: Prompt, timeout: float, transport) -> str:
    headers = {"content-type": "application/json"}
    if secret:
        headers["authorization"] = f"Bearer {secret}"
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": prompt.system}, {"role": "user", "content": prompt.user}],
        "response_format": {"type": "json_object"},
    }
    url = (cfg.base_url or "").rstrip("/") + "/chat/completions"
    data = await _post(url, headers, body, timeout, transport)
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BackendError("AI sent an unreadable answer") from None
    if not isinstance(text, str):
        raise BackendError("AI sent an unreadable answer")
    return text


async def _anthropic(cfg: BackendCfg, secret: str | None, prompt: Prompt, timeout: float, transport) -> str:
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if secret:
        headers["x-api-key"] = secret
    body = {
        "model": cfg.model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt.user}],
    }
    data = await _post(ANTHROPIC_URL, headers, body, timeout, transport)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise BackendError("AI sent an unreadable answer")
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text:
        raise BackendError("AI sent an unreadable answer")
    return text


def _claude_bin() -> str:
    path = os.environ.get("WA_APP_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise BackendError("The claude command isn't installed")
    return path


async def _claude_cli(cfg: BackendCfg, prompt: Prompt, settings: Settings) -> str:
    binary = _claude_bin()
    cwd = settings.data_dir / "claude-cwd"
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd.chmod(0o700)
    argv = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--model",
        cfg.model,
        "--system-prompt",
        prompt.system,
        "--tools",
        "",
        "--disallowedTools",
        "*",
        "--max-turns",
        "1",
        "--strict-mcp-config",
        "--setting-sources",
        "",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
    except OSError:
        raise BackendError("Couldn't start the claude command") from None
    try:
        out, _ = await proc.communicate(prompt.user.encode())
    finally:
        if proc.returncode is None:  # timed out or cancelled: don't leave it running
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise BackendError("The claude command failed")
    try:
        env = json.loads(out)
    except ValueError:
        raise BackendError("The claude command sent an unreadable answer") from None
    if not isinstance(env, dict) or env.get("is_error") or not isinstance(env.get("result"), str):
        raise BackendError("The claude command failed")
    return env["result"]


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def parse_alternatives(text: str) -> list[str]:
    """`{"alternatives": [...]}` or a bare list, optionally in a ```json fence. Trimmed, de-duplicated, max 2."""
    try:
        data = json.loads(_strip_fence(text))
    except ValueError:
        raise BackendError("No usable draft") from None
    if isinstance(data, dict):
        data = data.get("alternatives")
    if not isinstance(data, list):
        raise BackendError("No usable draft")
    out: list[str] = []
    for item in data:
        if isinstance(item, str) and (s := item.strip()) and s not in out:
            out.append(s)
    if not out:
        raise BackendError("No usable draft")
    return out[:MAX_ALTERNATIVES]
