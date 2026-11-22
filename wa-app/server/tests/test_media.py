"""Media, thumbnails and avatars. download_media / get_profile_picture are always patched; no live bridge."""

import io
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from tests.conftest import FAMILY, NIKOLA
from wa_app import mcp_lib, media


def _jpeg(path: Path, size: tuple[int, int] = (1200, 800)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 80, 40)).save(path, "JPEG")
    return path


def _bridge(asked: list[str], answer: dict):
    def fake(jid, preview=True):
        asked.append(jid)
        return answer

    return fake


@pytest.fixture
def downloads(monkeypatch):
    """Patch download_media; returns (calls, results) where results maps message id -> path or None."""
    calls: list[tuple[str, str, bool]] = []
    results: dict[str, str | None] = {}

    def fake(message_id, chat_jid, *, no_phone_retry):
        calls.append((message_id, chat_jid, no_phone_retry))
        return results.get(message_id)

    monkeypatch.setattr(mcp_lib, "download_media", fake)
    return calls, results


def test_media_missing_returns_404_json(logged_in: TestClient, downloads):
    response = logged_in.get(f"/api/media/{NIKOLA}/M9999")
    assert response.status_code == 404
    assert response.json() == {"error": "Media not available"}


def test_media_served_inline_with_safe_headers(logged_in: TestClient, settings, downloads):
    _, results = downloads
    results["M1"] = str(_jpeg(settings.store_path / NIKOLA / "image_1.jpg"))
    response = logged_in.get(f"/api/media/{NIKOLA}/M1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content[:2] == b"\xff\xd8"


def test_media_documents_and_svg_are_attachments(logged_in: TestClient, settings, downloads):
    _, results = downloads
    doc = settings.store_path / NIKOLA / "plan.pdf"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(b"%PDF-1.4")
    svg = settings.store_path / NIKOLA / "x.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    results["D1"], results["S1"] = str(doc), str(svg)
    assert logged_in.get(f"/api/media/{NIKOLA}/D1").headers["content-disposition"].startswith("attachment")
    assert logged_in.get(f"/api/media/{NIKOLA}/S1").headers["content-disposition"].startswith("attachment")


def test_media_path_must_stay_in_store(logged_in: TestClient, settings, downloads, tmp_path):
    _, results = downloads
    outside = _jpeg(tmp_path / "elsewhere" / "secret.jpg")
    results["M1"] = str(outside)
    assert logged_in.get(f"/api/media/{NIKOLA}/M1").status_code == 404
    # A symlink inside the store that points out of it is refused too.
    link = settings.store_path / NIKOLA / "link.jpg"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    results["M2"] = str(link)
    assert logged_in.get(f"/api/media/{NIKOLA}/M2").status_code == 404
    # So is a traversal that normalises out of it.
    results["M3"] = str(settings.store_path / NIKOLA / ".." / ".." / "elsewhere" / "secret.jpg")
    assert logged_in.get(f"/api/media/{NIKOLA}/M3").status_code == 404
    thumb = logged_in.get(f"/api/media/{NIKOLA}/M1/thumb")
    assert thumb.status_code == 404


def test_automatic_loads_never_ask_the_phone(logged_in: TestClient, settings, downloads):
    calls, results = downloads
    results["M1"] = str(_jpeg(settings.store_path / NIKOLA / "image_1.jpg"))
    logged_in.get(f"/api/media/{NIKOLA}/M1")
    logged_in.get(f"/api/media/{NIKOLA}/M1/thumb")
    assert calls and all(no_retry for _, _, no_retry in calls)


def test_retry_asks_the_phone_once_per_six_hours(logged_in: TestClient, downloads):
    calls, _ = downloads
    first = logged_in.post(f"/api/media/{NIKOLA}/M1/retry")
    assert first.status_code == 404
    assert calls[-1] == ("M1", NIKOLA, False)
    second = logged_in.post(f"/api/media/{NIKOLA}/M1/retry")
    assert second.status_code == 429
    assert len(calls) == 1  # the second tap never reached the bridge
    # Another message is unaffected, and a plain load is still allowed and still silent.
    assert logged_in.post(f"/api/media/{NIKOLA}/M2/retry").status_code == 404
    logged_in.get(f"/api/media/{NIKOLA}/M1")
    assert calls[-1][2] is True


def test_media_requires_login(client: TestClient):
    assert client.get(f"/api/media/{NIKOLA}/M1").status_code == 401
    assert client.get("/api/avatar/x@s.whatsapp.net").status_code == 401


def test_thumbnail_cached(settings, monkeypatch):
    src = _jpeg(settings.store_path / "big.jpg")
    first = media.thumbnail(settings, src, max_px=480)
    assert first is not None
    assert first.is_file() and first.is_relative_to(settings.data_dir / "thumbs")
    with Image.open(first) as img:
        assert max(img.size) == 480
    opened: list[object] = []
    real_open = Image.open

    def spy(*a, **k):
        opened.append(a)
        return real_open(*a, **k)

    monkeypatch.setattr(Image, "open", spy)
    second = media.thumbnail(settings, src, max_px=480)
    assert second == first
    assert opened == []  # served from the cache, source not reopened


def test_thumbnail_keeps_transparency_of_stickers(settings):
    src = settings.store_path / "sticker.webp"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(src, "WEBP")
    thumb = media.thumbnail(settings, src)
    assert thumb is not None
    with Image.open(thumb) as img:
        assert "A" in img.getbands()


def test_thumbnail_route(logged_in: TestClient, settings, downloads):
    _, results = downloads
    results["M1"] = str(_jpeg(settings.store_path / NIKOLA / "image_1.jpg"))
    response = logged_in.get(f"/api/media/{NIKOLA}/M1/thumb")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    with Image.open(io.BytesIO(response.content)) as img:
        assert max(img.size) <= 480


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_video_thumbnail(settings):
    import subprocess

    video = settings.store_path / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    thumb = media.thumbnail(settings, video)
    assert thumb is not None
    with Image.open(thumb) as img:
        assert img.size[0] > 0


def test_thumbnail_of_a_broken_image_is_none(logged_in: TestClient, settings, downloads):
    _, results = downloads
    broken = settings.store_path / NIKOLA / "broken.jpg"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not an image")
    results["M1"] = str(broken)
    assert logged_in.get(f"/api/media/{NIKOLA}/M1/thumb").status_code == 404


def test_avatar_fetched_once_and_cached(settings, monkeypatch, tmp_path):
    picture = _jpeg(tmp_path / "pp.jpg", (200, 200))
    asked: list[str] = []
    monkeypatch.setattr(mcp_lib, "get_profile_picture", _bridge(asked, {"url": "https://pps.whatsapp.net/pp.jpg"}))
    monkeypatch.setattr(media, "_fetch", lambda url: picture.read_bytes())
    path = media.avatar(settings, NIKOLA)
    assert path is not None and path.is_relative_to(settings.data_dir / "avatars")
    assert media.avatar(settings, NIKOLA) == path
    assert asked == [NIKOLA]


def test_avatar_negative_cache(settings, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(mcp_lib, "get_profile_picture", _bridge(asked, {"has_picture": False}))
    assert media.avatar(settings, NIKOLA) is None
    assert media.avatar(settings, NIKOLA) is None
    assert asked == [NIKOLA]  # the second miss came from the negative cache


def test_avatar_cache_expiry(settings, monkeypatch, tmp_path):
    picture = _jpeg(tmp_path / "pp.jpg", (100, 100))
    asked: list[str] = []
    monkeypatch.setattr(mcp_lib, "get_profile_picture", _bridge(asked, {"url": "https://pps.whatsapp.net/p.jpg"}))
    monkeypatch.setattr(media, "_fetch", lambda url: picture.read_bytes())
    path = media.avatar(settings, NIKOLA)
    assert path is not None
    old = time.time() - media.AVATAR_TTL - 10
    import os

    os.utime(path, (old, old))
    media.avatar(settings, NIKOLA)
    assert len(asked) == 2
    # A negative marker expires after 6 hours, not 24.
    monkeypatch.setattr(mcp_lib, "get_profile_picture", lambda jid, preview=True: {"has_picture": False})
    assert media.avatar(settings, FAMILY) is None
    marker = next((settings.data_dir / "avatars").glob("*.none"))
    older = time.time() - media.AVATAR_NEGATIVE_TTL - 10
    os.utime(marker, (older, older))
    monkeypatch.setattr(
        mcp_lib, "get_profile_picture", lambda jid, preview=True: {"url": "https://pps.whatsapp.net/p.jpg"}
    )
    assert media.avatar(settings, FAMILY) is not None


def test_avatar_rejects_non_https_and_bad_jids(settings, monkeypatch):
    monkeypatch.setattr(mcp_lib, "get_profile_picture", lambda jid, preview=True: {"url": "http://10.0.0.1/x"})
    monkeypatch.setattr(media, "_fetch", lambda url: pytest.fail("must not fetch"))
    assert media.avatar(settings, NIKOLA) is None
    assert media.avatar(settings, "../../etc/passwd") is None


def test_avatar_route(logged_in: TestClient, settings, monkeypatch, tmp_path):
    picture = _jpeg(tmp_path / "pp.jpg", (200, 200))
    monkeypatch.setattr(
        mcp_lib, "get_profile_picture", lambda jid, preview=True: {"url": "https://pps.whatsapp.net/pp.jpg"}
    )
    monkeypatch.setattr(media, "_fetch", lambda url: picture.read_bytes())
    ok = logged_in.get(f"/api/avatar/{NIKOLA}")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    monkeypatch.setattr(mcp_lib, "get_profile_picture", lambda jid, preview=True: {"has_picture": False})
    assert logged_in.get(f"/api/avatar/{FAMILY}").status_code == 404


def test_avatar_host_must_be_a_whatsapp_cdn(settings, monkeypatch):
    monkeypatch.setattr(media, "_fetch", lambda url: pytest.fail("must not fetch " + url))
    for url in (
        "https://10.0.0.1/pp.jpg",
        "https://localhost/pp.jpg",
        "https://whatsapp.net.evil.example/pp.jpg",
        "https://evilwhatsapp.net/pp.jpg",
        "https://pps.whatsapp.net@evil.example/pp.jpg",
        "https://evil.example/pps.whatsapp.net",
    ):
        monkeypatch.setattr(mcp_lib, "get_profile_picture", _bridge([], {"url": url}))
        assert media.avatar(settings, NIKOLA) is None
        for leftover in (settings.data_dir / "avatars").glob("*"):
            leftover.unlink()


def test_avatar_accepts_fbcdn_and_whatsapp_hosts():
    assert media._avatar_url_ok("https://pps.whatsapp.net/v/t61/x.jpg")
    assert media._avatar_url_ok("https://scontent.xx.fbcdn.net/x.jpg")
    assert not media._avatar_url_ok("http://pps.whatsapp.net/x.jpg")


def test_thumbnail_refuses_other_formats(logged_in: TestClient, settings, downloads):
    _, results = downloads
    eps = settings.store_path / NIKOLA / "evil.jpg"  # named like a jpeg, sniffs as something else
    eps.parent.mkdir(parents=True, exist_ok=True)
    eps.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n")
    bmp = settings.store_path / NIKOLA / "x.png"
    Image.new("RGB", (8, 8)).save(bmp, "BMP")
    results["E1"], results["B1"] = str(eps), str(bmp)
    assert media.thumbnail(settings, eps) is None
    assert media.thumbnail(settings, bmp) is None
    for mid in ("E1", "B1"):
        response = logged_in.get(f"/api/media/{NIKOLA}/{mid}/thumb")
        assert response.status_code == 404 and response.json() == {"error": "No preview"}


def test_thumbnail_refuses_oversized_images(settings, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000)
    big = _jpeg(settings.store_path / "huge.jpg", (400, 400))  # 160k px, far over the cap
    assert media.thumbnail(settings, big) is None
    barely = _jpeg(settings.store_path / "warn.jpg", (110, 100))  # between 1x and 2x: warning becomes an error
    with pytest.warns(Image.DecompressionBombWarning):
        assert media.thumbnail(settings, barely) is None


def test_ffmpeg_is_locked_down(settings, monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        assert kwargs["timeout"] <= 15
        raise OSError("no ffmpeg here")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    video = settings.store_path / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x")
    assert media.thumbnail(settings, video) is None
    cmd = seen[0]
    assert "-nostdin" in cmd
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert "mp4" in cmd[cmd.index("-format_whitelist") + 1]
    assert cmd.index("-protocol_whitelist") < cmd.index("-i") and cmd.index("-format_whitelist") < cmd.index("-i")


def test_thumbnail_generation_is_limited_to_three(settings, monkeypatch):
    import threading

    lock = threading.Lock()
    running = peak = 0

    def slow(path, ctype, max_px):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return b"x", ".jpg"

    monkeypatch.setattr(media, "_generate", slow)
    files = [_jpeg(settings.store_path / f"p{i}.jpg", (20, 20)) for i in range(9)]
    threads = [threading.Thread(target=media.thumbnail, args=(settings, f)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 < peak <= 3


def test_retry_refuses_imported_messages_without_recording(logged_in: TestClient, downloads, settings):
    calls, _ = downloads
    response = logged_in.post(f"/api/media/{NIKOLA}/import-abc/retry")
    assert response.status_code == 400 and "error" in response.json()
    assert calls == []
    from wa_app import appdb

    row = appdb.connect(settings).execute("SELECT 1 FROM media_retries WHERE message_id='import-abc'").fetchone()
    assert row is None


def test_get_with_retry_flag_no_longer_asks_the_phone(logged_in: TestClient, downloads, settings):
    """A GET skips the Origin check, so it must never be able to ping the phone."""
    calls, _ = downloads
    logged_in.get(f"/api/media/{NIKOLA}/M1?retry=1")
    assert calls == [("M1", NIKOLA, True)]  # silent: no_phone_retry
    from wa_app import appdb

    assert appdb.connect(settings).execute("SELECT 1 FROM media_retries").fetchone() is None


def test_retry_post_needs_a_matching_origin(logged_in: TestClient, downloads):
    calls, _ = downloads
    url = f"/api/media/{NIKOLA}/M1/retry"
    assert logged_in.post(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert calls == []
    assert logged_in.post(url, headers={"Origin": "https://testserver"}).status_code == 404  # allowed, no file
    assert calls == [("M1", NIKOLA, False)]
