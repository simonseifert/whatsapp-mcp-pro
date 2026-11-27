"""Sending photos and files: the upload route, the private temp file and the 64 MiB cap (fake bridge, no real sends)."""

import os
import stat
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from tests import conftest as fx
from wa_app import auth, mcp_lib, sending

KEY = "2f6f0c1e-8a53-4d0e-9a53-1b1c9a3a0002"


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    answer: dict = {"success": True, "message_id": "MEDIA1"}

    def send_file(recipient, **kw):  # noqa: ANN001, ANN202
        path = kw["file_path"]
        calls.append(
            {
                "to": recipient,
                **kw,
                "dir": os.path.dirname(path),
                "mode": stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                "content": open(path, "rb").read(),  # noqa: SIM115
            }
        )
        return dict(answer)

    monkeypatch.setattr(mcp_lib, "send_file", send_file)
    return calls, answer


def upload(client: TestClient, jid: str, data: bytes = b"\xff\xd8jpeg", /, **headers: str):  # noqa: ANN201
    base = {"X-Filename": quote("Račun čćšžđ.pdf"), "X-Client-Key": KEY, "Content-Type": "application/pdf"}
    return client.post(f"/api/chats/{jid}/media", content=data, headers={**base, **headers})


def test_media_route_413_over_64_mib(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    response = upload(logged_in, fx.ANA, b"x" * (auth.MAX_UPLOAD + 1))
    assert response.status_code == 413
    assert calls == []


def test_media_route_empty_body_422(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    assert upload(logged_in, fx.ANA, b"").status_code == 422
    assert calls == []


def test_other_routes_keep_64kb_cap() -> None:
    assert auth.body_limit(f"/api/chats/{fx.ANA}/media") == 64 * 1024 * 1024
    assert auth.body_limit(f"/api/chats/{fx.ANA}/send") == 64 * 1024
    assert auth.body_limit("/api/chats/x/media/extra") == 64 * 1024
    assert auth.body_limit("/api/auth/login") == 64 * 1024


def test_other_route_over_64kb_is_413(logged_in: TestClient) -> None:
    response = logged_in.post(f"/api/chats/{fx.ANA}/send", content=b"x" * (64 * 1024 + 1))
    assert response.status_code == 413


def test_safe_filename() -> None:
    assert sending.safe_filename("../../etc/passwd") == "passwd"
    assert sending.safe_filename("C:\\Users\\a\\slika.png") == "slika.png"
    assert sending.safe_filename("Račun čćšžđ.pdf") == "Račun čćšžđ.pdf"
    assert sending.safe_filename("a\x00b\nc.txt") == "abc.txt"
    assert sending.safe_filename("") == "file"
    assert sending.safe_filename("..") == "file"
    long = sending.safe_filename("š" * 300 + ".pdf")
    assert len(long) == 120 and long.endswith(".pdf")


def test_route_sends_with_croatian_name_caption_and_quote(logged_in: TestClient, bridge) -> None:
    calls, _ = bridge
    msgs = logged_in.get(f"/api/chats/{fx.ANA}/messages").json()["messages"]
    reply_id = next(m["id"] for m in msgs if not m["from_me"])
    response = upload(
        logged_in, fx.ANA, b"%PDF", **{"X-Caption": quote("Evo računa, hvala! čćž"), "X-Reply-To": reply_id}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message_id": "MEDIA1"}
    (call,) = calls
    assert os.path.basename(call["file_path"]) == "Račun čćšžđ.pdf"
    assert call["filename"] == "Račun čćšžđ.pdf"
    assert call["caption"] == "Evo računa, hvala! čćž"
    assert call["quoted_message_id"] == reply_id
    assert call["idempotency_key"] == KEY
    assert call["content"] == b"%PDF"


def test_route_rejects_bad_headers(logged_in: TestClient, bridge) -> None:
    assert upload(logged_in, fx.ANA, **{"X-Client-Key": "nope"}).status_code == 422
    assert upload(logged_in, fx.ANA, **{"X-Kind": "video"}).status_code == 422
    assert upload(logged_in, fx.ANA, **{"X-Caption": quote("x" * 1025)}).status_code == 422
    assert upload(logged_in, "nobody@s.whatsapp.net").status_code == 404
    assert upload(logged_in, fx.ANA, **{"X-Reply-To": "missing"}).status_code == 422


def test_failure_is_mapped(logged_in: TestClient, bridge) -> None:
    _, answer = bridge
    answer.clear()
    answer.update({"success": False, "http_status": 429, "code": "send_cap", "retryable": True})
    response = upload(logged_in, fx.ANA)
    assert response.json() == {"ok": False, "error": "Not sent: limit", "retryable": True}


@pytest.mark.parametrize("succeeds", [True, False])
def test_send_media_writes_private_temp_and_removes_it(
    logged_in: TestClient,
    bridge,
    succeeds: bool,  # noqa: FBT001
) -> None:
    calls, answer = bridge
    if not succeeds:
        answer.clear()
        answer.update({"success": False, "retryable": True})
    upload(logged_in, fx.ANA)
    (call,) = calls
    assert call["dir"].startswith("/tmp/wa-app-up-")
    assert call["mode"] == 0o700
    assert not os.path.exists(call["dir"])


def test_temp_removed_when_the_bridge_call_raises(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def boom(recipient, **kw):  # noqa: ANN001, ANN202
        seen.append(os.path.dirname(kw["file_path"]))
        raise RuntimeError("down")

    monkeypatch.setattr(mcp_lib, "send_file", boom)
    response = upload(logged_in, fx.ANA)
    assert response.json()["ok"] is False
    assert not os.path.exists(seen[0])


def test_voice_kind_goes_to_audio(logged_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[dict] = []

    def audio(recipient, **kw):  # noqa: ANN001, ANN202
        got.append({"to": recipient, **kw})
        return {"success": True, "message_id": "V1"}

    monkeypatch.setattr(mcp_lib, "send_audio_message", audio)
    response = upload(logged_in, fx.ANA, b"OggS", **{"X-Kind": "voice", "X-Filename": "voice.ogg"})
    assert response.json() == {"ok": True, "message_id": "V1"}
    assert os.path.basename(got[0]["file_path"]) == "voice.ogg"
    assert got[0]["idempotency_key"] == KEY
