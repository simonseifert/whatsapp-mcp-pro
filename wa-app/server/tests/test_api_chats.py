from starlette.testclient import TestClient

from tests import conftest as fx


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/chats").status_code == 401


def test_chats_json_shape(logged_in: TestClient) -> None:
    body = logged_in.get("/api/chats").json()
    assert body["archived_count"] == 2
    first = body["chats"][0]
    assert first["jid"] == fx.FAMILY
    assert set(first) == {
        "jid", "name", "kind", "avatar_url", "last_message", "unread", "marked_unread", "pinned", "muted",
        "archived", "community_name",
    }  # fmt: skip
    assert set(first["last_message"]) == {
        "id", "sender_name", "from_me", "preview", "media_type", "deleted", "timestamp_iso", "ticks",
    }  # fmt: skip


def test_filter_archived_query_params(logged_in: TestClient) -> None:
    unread = logged_in.get("/api/chats", params={"filter": "unread"}).json()["chats"]
    assert {c["jid"] for c in unread} >= {fx.ANA, fx.FAMILY}
    archived = logged_in.get("/api/chats", params={"archived": "1"}).json()["chats"]
    assert {c["jid"] for c in archived} == {fx.OLD_ARCHIVED, fx.OLD_ARCHIVED_GROUP}
    found = logged_in.get("/api/chats", params={"q": "durdica"}).json()["chats"]
    assert [c["jid"] for c in found] == [fx.ACCOUNTANT]


def test_bad_filter_is_400(logged_in: TestClient) -> None:
    assert logged_in.get("/api/chats", params={"filter": "nope"}).status_code == 400


def test_missing_store_is_503(logged_in: TestClient, settings) -> None:
    (settings.store_path / "messages.db").unlink()
    assert logged_in.get("/api/chats").status_code == 503


def test_chat_detail_and_messages(logged_in: TestClient) -> None:
    detail = logged_in.get(f"/api/chats/{fx.EKIPA}").json()
    assert detail["chat"]["name"] == "Ekipa za vikend"
    assert "Nikola" in detail["members_line"]
    body = logged_in.get(f"/api/chats/{fx.EKIPA}/messages").json()
    assert body["has_more"] is False and len(body["messages"]) == 15
    first = body["messages"][0]
    assert {"id", "actionable", "contact_cards", "reactions", "poll", "reply_to", "ticks"} <= set(first)


def test_messages_before_cursor(logged_in: TestClient) -> None:
    body = logged_in.get(f"/api/chats/{fx.EKIPA}/messages").json()
    cut = body["messages"][5]["timestamp_iso"]
    older = logged_in.get(f"/api/chats/{fx.EKIPA}/messages", params={"before": cut}).json()
    assert [m["id"] for m in older["messages"]] == ["E01", "E02", "E03", "E04", "E05"]


def test_chat_routes_need_login_and_known_chat(client: TestClient, logged_in: TestClient) -> None:
    assert logged_in.get("/api/chats/999@s.whatsapp.net").status_code == 404
    assert logged_in.get("/api/chats/999@s.whatsapp.net/messages").json() == {"messages": [], "has_more": False}
    assert logged_in.get(f"/api/chats/{fx.EKIPA}/messages", params={"before": "junk"}).status_code == 400
    client.cookies.clear()
    assert client.get(f"/api/chats/{fx.EKIPA}").status_code == 401
