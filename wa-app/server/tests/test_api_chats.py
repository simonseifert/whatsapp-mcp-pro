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
