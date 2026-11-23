"""The local-only gate: a chat marked local only may use nothing but a backend on this machine or LAN."""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import BackendCfg, ChatAi


class Refused(Exception):
    """The call must not happen. The message is shown to the user as is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Pin:
    """Where a local-only call must connect: `base_url` has the validated IP literal in place of the host, and
    `host` is the original Host header. The address that passed the check is the address that is connected to."""

    base_url: str
    host: str


def _literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None


def _private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_loopback or addr.is_private


def _assess(cfg: BackendCfg) -> tuple[bool, Pin | None]:
    """(is local, pin). The pin exists only for an http base URL with a hostname: resolved once here, every address
    loopback or private, the first one used. An https hostname is never local (a pinned address would break the
    certificate name check); a literal private IP over https is."""
    if cfg.kind != "openai" or not cfg.base_url:
        return False, None
    try:
        parsed = urlparse(cfg.base_url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return False, None
    if not host or parsed.scheme not in ("http", "https"):
        return False, None
    literal = _literal(host)
    if literal is not None:
        return _private(literal), None
    if parsed.scheme != "http":
        return False, None
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False, None
    addrs = [_literal(str(info[4][0])) for info in infos]
    if not addrs or any(a is None or not _private(a) for a in addrs):
        return False, None
    first = str(addrs[0]).split("%")[0]
    ip = f"[{first}]" if ":" in first else first
    netloc = f"{ip}:{port}" if port else ip
    shown = f"[{host}]" if ":" in host else host
    return True, Pin(parsed._replace(netloc=netloc).geturl(), f"{shown}:{port}" if port else shown)


def is_local(cfg: BackendCfg) -> bool:
    """Only an OpenAI-compatible backend whose base URL host is loopback or private (every resolved address).

    base_url is ignored for the other kinds: they always talk to the vendor. A host that does not resolve is not
    local, and an https hostname is not local (see `Pin`). Blocking: it may resolve a name.
    """
    return _assess(cfg)[0]


def check(chat: ChatAi, cfg: BackendCfg | None) -> Pin | None:
    """Raises Refused when the call must not happen. For a local-only chat on a hostname, returns the Pin the call
    must connect through; otherwise None."""
    if cfg is None:
        raise Refused("No AI set up")
    if not chat.local_only:
        return None
    local, pin = _assess(cfg)
    if not local:
        raise Refused("This chat is local only")
    return pin
