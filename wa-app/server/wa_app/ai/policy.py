"""The local-only gate: a chat marked local only may use nothing but a backend on this machine or LAN."""

import ipaddress
import socket
from urllib.parse import urlparse

from .config import BackendCfg, ChatAi


class Refused(Exception):
    """The call must not happen. The message is shown to the user as is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_local(cfg: BackendCfg) -> bool:
    """Only an OpenAI-compatible backend whose base URL host resolves, every address, to loopback or private.

    base_url is ignored for the other kinds: they always talk to the vendor. A host that does not resolve is not local.
    """
    if cfg.kind != "openai" or not cfg.base_url:
        return False
    host = urlparse(cfg.base_url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    try:
        addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    except ValueError:
        return False
    return all(a.is_loopback or a.is_private for a in addrs)


def check(chat: ChatAi, cfg: BackendCfg | None) -> None:
    if cfg is None:
        raise Refused("No AI set up")
    if chat.local_only and not is_local(cfg):
        raise Refused("This chat is local only")
