import json
import stat

from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import appdb
from wa_app.ai import config, secrets

SECRET = "sk-test-FICTIONAL-0123456789"
OPENAI = {"kind": "openai", "label": "Local Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "llama-test"}


def _db(settings):
    return appdb.connect(settings)


def test_key_file_created_mode_600(settings) -> None:
    blob = secrets.encrypt(settings, SECRET)
    path = settings.data_dir / "ai.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SECRET.encode() not in blob
    assert secrets.decrypt(settings, blob) == SECRET


def test_key_roundtrip_and_never_in_api(logged_in: TestClient, settings) -> None:
    put = logged_in.put("/api/ai/backends", json={**OPENAI, "api_key": SECRET})
    assert put.status_code == 200
    assert put.json()["backend"]["has_key"] is True
    listing = logged_in.get("/api/ai/backends")
    assert listing.json()["backends"][0]["has_key"] is True
    assert listing.json()["backends"][0]["local"] is True and put.json()["backend"]["local"] is True
    assert SECRET not in put.text + listing.text
    assert "api_key" not in json.dumps(listing.json())
    conn = _db(settings)
    assert config.backend_secret(conn, settings, put.json()["backend"]["id"]) == SECRET
    raw = conn.execute("SELECT key_enc FROM ai_backends").fetchone()[0]
    assert SECRET.encode() not in raw


def test_omitted_key_kept_empty_clears(settings) -> None:
    conn = _db(settings)
    saved = config.save_backend(conn, settings, {**OPENAI, "api_key": SECRET})
    again = config.save_backend(conn, settings, {**OPENAI, "id": saved.id, "label": "Renamed"})
    assert again.has_key and again.label == "Renamed"
    assert config.backend_secret(conn, settings, saved.id) == SECRET
    cleared = config.save_backend(conn, settings, {**OPENAI, "id": saved.id, "api_key": ""})
    assert not cleared.has_key
    assert config.backend_secret(conn, settings, saved.id) is None


def test_base_url_change_without_key_clears_key(settings) -> None:
    conn = _db(settings)
    saved = config.save_backend(conn, settings, {**OPENAI, "api_key": SECRET})
    moved = config.save_backend(conn, settings, {**OPENAI, "id": saved.id, "base_url": "https://evil.example/v1"})
    assert not moved.has_key
    assert config.backend_secret(conn, settings, saved.id) is None


def test_kind_change_without_key_clears_key(settings) -> None:
    conn = _db(settings)
    saved = config.save_backend(conn, settings, {**OPENAI, "api_key": SECRET})
    moved = config.save_backend(conn, settings, {"kind": "anthropic", "label": "A", "model": "m", "id": saved.id})
    assert not moved.has_key
    assert config.backend_secret(conn, settings, saved.id) is None


def test_base_url_change_with_new_key_keeps_new_key(settings) -> None:
    conn = _db(settings)
    saved = config.save_backend(conn, settings, {**OPENAI, "api_key": SECRET})
    body = {**OPENAI, "id": saved.id, "base_url": "https://other.example/v1", "api_key": "sk-new-FICTIONAL"}
    moved = config.save_backend(conn, settings, body)
    assert moved.has_key
    assert config.backend_secret(conn, settings, saved.id) == "sk-new-FICTIONAL"


def test_label_and_model_change_keeps_key(settings) -> None:
    conn = _db(settings)
    saved = config.save_backend(conn, settings, {**OPENAI, "api_key": SECRET})
    # the same address written with stray spaces is not a change
    body = {**OPENAI, "id": saved.id, "label": "New", "model": "other", "base_url": f"  {OPENAI['base_url']} "}
    again = config.save_backend(conn, settings, body)
    assert again.has_key and again.label == "New" and again.model == "other"
    assert config.backend_secret(conn, settings, saved.id) == SECRET


def test_chat_ai_defaults_off(settings) -> None:
    ai = config.chat_ai(_db(settings), fx.ANA)
    assert (ai.enabled, ai.backend_id, ai.local_only, ai.note, ai.rules) == (False, None, False, None, None)


def test_effective_backend_override_then_default(settings) -> None:
    conn = _db(settings)
    assert config.effective_backend(conn, fx.ANA) is None
    a = config.save_backend(conn, settings, OPENAI)
    b = config.save_backend(conn, settings, {"kind": "claude_cli", "label": "Claude CLI", "model": "sonnet"})
    config.set_default_backend(conn, a.id)
    assert config.effective_backend(conn, fx.ANA) == a
    config.save_chat_ai(conn, fx.ANA, {"backend_id": b.id})
    assert config.effective_backend(conn, fx.ANA) == b
    assert config.effective_backend(conn, fx.FAMILY) == a


def test_delete_backend_falls_back(settings) -> None:
    conn = _db(settings)
    a = config.save_backend(conn, settings, OPENAI)
    b = config.save_backend(conn, settings, {"kind": "claude_cli", "label": "Claude CLI", "model": "sonnet"})
    config.set_default_backend(conn, a.id)
    config.save_chat_ai(conn, fx.ANA, {"backend_id": b.id})
    config.delete_backend(conn, b.id)
    assert config.chat_ai(conn, fx.ANA).backend_id is None
    assert config.effective_backend(conn, fx.ANA) == a
    config.delete_backend(conn, a.id)
    assert config.default_backend_id(conn) is None
    assert config.effective_backend(conn, fx.ANA) is None


def test_rules_change_stamps_edit_time(settings) -> None:
    conn = _db(settings)
    config.save_chat_ai(conn, fx.ANA, {"enabled": True})
    assert conn.execute("SELECT rules_edited_at FROM chat_ai").fetchone()[0] is None
    config.save_chat_ai(conn, fx.ANA, {"rules": "- short replies"})
    assert conn.execute("SELECT rules_edited_at FROM chat_ai").fetchone()[0] is not None
    assert config.chat_ai(conn, fx.ANA).enabled is True


def test_validation(logged_in: TestClient) -> None:
    assert logged_in.put("/api/ai/backends", json={**OPENAI, "kind": "mystery"}).status_code == 422
    no_url = {k: v for k, v in OPENAI.items() if k != "base_url"}
    assert logged_in.put("/api/ai/backends", json=no_url).status_code == 422
    assert logged_in.put("/api/ai/backends", json={**OPENAI, "id": 999}).status_code == 404
    url = f"/api/chats/{fx.ANA}/ai"
    assert logged_in.put(url, json={"note": "x" * 501}).status_code == 422
    assert logged_in.put(url, json={"rules": "x" * 2001}).status_code == 422
    assert logged_in.put(url, json={"backend_id": 999}).status_code == 422
    assert logged_in.put(url, json={"note": "x" * 500, "rules": "y" * 2000}).status_code == 200
    assert logged_in.put("/api/chats/nobody@s.whatsapp.net/ai", json={"enabled": True}).status_code == 404


def test_chat_route_roundtrip_and_default_route(logged_in: TestClient) -> None:
    backend = logged_in.put("/api/ai/backends", json=OPENAI).json()["backend"]
    assert logged_in.put("/api/ai/default", json={"backend_id": backend["id"]}).json() == {"default_id": backend["id"]}
    assert logged_in.get("/api/ai/backends").json()["default_id"] == backend["id"]
    assert logged_in.put("/api/ai/default", json={"backend_id": 999}).status_code == 404
    url = f"/api/chats/{fx.ANA}/ai"
    assert logged_in.get(url).json()["ai"]["enabled"] is False
    out = logged_in.put(url, json={"enabled": True, "local_only": True, "note": "my sister"}).json()["ai"]
    assert out["enabled"] and out["local_only"] and out["note"] == "my sister" and out["chat_jid"] == fx.ANA
    assert logged_in.delete(f"/api/ai/backends/{backend['id']}").status_code == 200
    assert logged_in.delete(f"/api/ai/backends/{backend['id']}").status_code == 404
    assert logged_in.get("/api/ai/backends").json()["default_id"] is None


def test_routes_need_login_and_origin(client: TestClient) -> None:
    jid = f"/api/chats/{fx.ANA}/ai"
    for method, path in [("get", "/api/ai/backends"), ("put", "/api/ai/backends"), ("delete", "/api/ai/backends/1"),
                         ("put", "/api/ai/default"), ("get", jid), ("put", jid)]:  # fmt: skip
        assert client.request(method, path, json={}).status_code == 401, path
    client.post("/api/auth/setup", json={"passphrase": fx.PASSPHRASE})
    assert client.post("/api/auth/login", json={"passphrase": fx.PASSPHRASE}).status_code == 200
    bad = {"Origin": "https://evil.example"}
    for method, path in [("put", "/api/ai/backends"), ("delete", "/api/ai/backends/1"), ("put", "/api/ai/default"),
                         ("put", jid)]:  # fmt: skip
        assert client.request(method, path, json={}, headers=bad).status_code == 403, path


def test_backend_local_flag_follows_the_policy(logged_in: TestClient, monkeypatch) -> None:
    from wa_app.ai import policy

    monkeypatch.setattr(policy, "is_local", lambda cfg: cfg.base_url is not None and "ollama.home" in cfg.base_url)
    logged_in.put("/api/ai/backends", json={**OPENAI, "base_url": "http://ollama.home:11434/v1", "label": "Home"})
    logged_in.put("/api/ai/backends", json={**OPENAI, "base_url": "https://api.example.com/v1", "label": "Far"})
    by_label = {b["label"]: b["local"] for b in logged_in.get("/api/ai/backends").json()["backends"]}
    assert by_label == {"Home": True, "Far": False}
