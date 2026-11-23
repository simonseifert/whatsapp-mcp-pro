import socket

import pytest

from wa_app.ai import policy
from wa_app.ai.config import BackendCfg, ChatAi


def _cfg(kind: str = "openai", base_url: str | None = "http://127.0.0.1:11434/v1") -> BackendCfg:
    return BackendCfg(1, kind, "Test", base_url, "m", False)


def _chat(local_only: bool) -> ChatAi:
    return ChatAi("111@s.whatsapp.net", True, None, local_only, None, None)


def _resolves_to(monkeypatch, *addrs: str) -> None:
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addrs]

    monkeypatch.setattr(policy.socket, "getaddrinfo", fake)


def test_loopback_ok() -> None:
    policy.check(_chat(True), _cfg())
    assert policy.is_local(_cfg())


def test_private_lan_ok() -> None:
    policy.check(_chat(True), _cfg(base_url="http://192.168.1.5"))


def test_public_ip_refused() -> None:
    with pytest.raises(policy.Refused, match="This chat is local only"):
        policy.check(_chat(True), _cfg(base_url="https://8.8.8.8/v1"))


def test_cloud_kinds_refused_even_with_local_base_url() -> None:
    for kind in ("anthropic", "claude_cli"):
        with pytest.raises(policy.Refused, match="This chat is local only"):
            policy.check(_chat(True), _cfg(kind=kind, base_url="http://127.0.0.1:11434/v1"))
        assert not policy.is_local(_cfg(kind=kind))


def test_hostname_resolving_public_refused(monkeypatch) -> None:
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(policy.Refused, match="This chat is local only"):
        policy.check(_chat(True), _cfg(base_url="http://my-ollama.example.com"))


def test_every_resolved_address_must_be_local(monkeypatch) -> None:
    _resolves_to(monkeypatch, "10.0.0.7", "93.184.216.34")
    assert not policy.is_local(_cfg(base_url="http://mixed.example.com"))
    _resolves_to(monkeypatch, "10.0.0.7", "127.0.0.1")
    assert policy.is_local(_cfg(base_url="http://lan.example.com"))


def test_unresolvable_refused(monkeypatch) -> None:
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(policy.socket, "getaddrinfo", boom)
    with pytest.raises(policy.Refused, match="This chat is local only"):
        policy.check(_chat(True), _cfg(base_url="http://nowhere.invalid"))


def test_not_local_only_allows_any_backend() -> None:
    for kind in ("openai", "anthropic", "claude_cli"):
        policy.check(_chat(False), _cfg(kind=kind, base_url="https://api.openai.com/v1"))


def test_no_backend_refused() -> None:
    with pytest.raises(policy.Refused, match="No AI set up"):
        policy.check(_chat(False), None)


def _hosts(monkeypatch, *addrs: str) -> None:
    def fake(host, port, *a, **k):
        return [(socket.AF_INET6 if ":" in a_ else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a_, 0)) for a_ in addrs]

    monkeypatch.setattr(policy.socket, "getaddrinfo", fake)


def test_https_hostname_refused_on_local_only(monkeypatch) -> None:
    _hosts(monkeypatch, "10.0.0.7")
    cfg = _cfg(base_url="https://ollama.home/v1")
    with pytest.raises(policy.Refused, match="This chat is local only"):
        policy.check(_chat(True), cfg)
    assert not policy.is_local(cfg)
    policy.check(_chat(False), cfg)  # not local only: unchanged


def test_https_literal_private_ip_ok() -> None:
    cfg = _cfg(base_url="https://192.168.1.5:8443/v1")
    assert policy.check(_chat(True), cfg) is None
    assert policy.is_local(cfg)


def test_http_hostname_is_pinned_to_a_validated_address(monkeypatch) -> None:
    _hosts(monkeypatch, "10.0.0.7", "192.168.1.9")
    pin = policy.check(_chat(True), _cfg(base_url="http://ollama.home:11434/v1"))
    assert pin == policy.Pin("http://10.0.0.7:11434/v1", "ollama.home:11434")
    assert policy.check(_chat(False), _cfg(base_url="http://ollama.home:11434/v1")) is None


def test_pin_brackets_ipv6(monkeypatch) -> None:
    _hosts(monkeypatch, "fd00::5")
    pin = policy.check(_chat(True), _cfg(base_url="http://ollama.home/v1"))
    assert pin == policy.Pin("http://[fd00::5]/v1", "ollama.home")
