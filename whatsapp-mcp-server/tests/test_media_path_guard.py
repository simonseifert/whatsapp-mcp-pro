"""Tests for the outgoing media path guard.

send_audio_message converted any readable file into /tmp before handing it to
the bridge, so the bridge's MEDIA_ALLOWED_DIRS check only ever saw the /tmp
output and could not stop e.g. ~/.ssh/id_rsa going out as a voice note. The
source path is now validated in Python first, and send_file repeats the check.
"""

import os

import pytest

import whatsapp


@pytest.fixture
def media_dirs(monkeypatch, tmp_path):
    """An allowlist of exactly one directory, with a decoy sibling outside it."""
    allowed = tmp_path / "media"
    outside = tmp_path / "media-evil"  # shares a name prefix, must not match
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(whatsapp, "BRIDGE_DEFAULT_MEDIA_DIRS", ())
    monkeypatch.setenv("MEDIA_ALLOWED_DIRS", f" {allowed} ,")
    monkeypatch.delenv("DISABLE_PATH_CHECK", raising=False)
    return allowed, outside


@pytest.fixture
def bridge_calls(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "message_id": "MSG1"}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json["media_path"])
        return FakeResponse()

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return calls


@pytest.fixture
def conversions(monkeypatch, tmp_path):
    converted: list[str] = []

    def fake_convert(path):
        converted.append(path)
        out = tmp_path / "converted.ogg"
        out.write_bytes(b"OggS")
        return str(out)

    monkeypatch.setattr(whatsapp.audio, "convert_to_opus_ogg_temp", fake_convert)
    return converted


def test_accepts_a_file_inside_an_allowed_dir(media_dirs):
    allowed, _ = media_dirs
    f = allowed / "photo.png"
    f.write_bytes(b"x")
    assert whatsapp.validate_outgoing_media_path(str(f)) == os.path.realpath(f)


def test_rejects_a_sibling_dir_sharing_the_prefix(media_dirs):
    _, outside = media_dirs
    f = outside / "photo.png"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside the allowed"):
        whatsapp.validate_outgoing_media_path(str(f))


def test_rejects_a_symlink_escaping_the_allowlist(media_dirs):
    allowed, outside = media_dirs
    target = outside / "secret.txt"
    target.write_text("s")
    link = allowed / "innocent.png"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(link))


def test_rejects_traversal_out_of_the_allowlist(media_dirs):
    allowed, outside = media_dirs
    (outside / "x.png").write_bytes(b"x")
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(allowed / ".." / "media-evil" / "x.png"))


def test_rejects_hidden_directories_below_an_allowed_root(media_dirs):
    allowed, _ = media_dirs
    hidden = allowed / "repo" / ".git"
    hidden.mkdir(parents=True)
    f = hidden / "HEAD"
    f.write_text("ref")
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(f))


def test_an_explicitly_allowed_dotted_dir_still_works(monkeypatch, tmp_path):
    """An operator who allowlists e.g. ~/.local/share/.../store meant it."""
    store = tmp_path / ".local" / "store"
    (store / "491500000001_s.whatsapp.net").mkdir(parents=True)
    f = store / "491500000001_s.whatsapp.net" / "voice.ogg"
    f.write_bytes(b"OggS")
    monkeypatch.setattr(whatsapp, "BRIDGE_DEFAULT_MEDIA_DIRS", ())
    monkeypatch.setenv("MEDIA_ALLOWED_DIRS", str(store))
    assert whatsapp.validate_outgoing_media_path(str(f)) == os.path.realpath(f)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "prod.env",
        "server.pem",
        "tls.KEY",
        "id_rsa",
        "id_ed25519.pub",
        "config.json",
        "credentials-gcp.json",
        "secrets.yaml",
        "vault.kdbx",
        "messages.db",
        # SQLite's sidecars hold live data too.
        "messages.db-wal",
        "messages.db-shm",
        "whatsapp.db-journal",
        "store.sqlite",
        "store.sqlite3",
        "store.sqlite-wal",
    ],
)
def test_refuses_secret_looking_files(media_dirs, name):
    allowed, _ = media_dirs
    f = allowed / name
    f.write_text("s")
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(f))


@pytest.mark.parametrize(
    "relative",
    [
        "store/media/491500000001@s.whatsapp.net/ABC123.jpg",  # /api/download
        "store/491500000001_s.whatsapp.net/photo.jpg",  # auto-download
        "store/120363322317399301_g.us/clip.mp4",
        "store/120363402588966290_newsletter/post.png",
        "store/154713345548441_lid/voice.ogg",
        "Store/Media/491500000001@s.whatsapp.net/ABC123.jpg",
    ],
)
def test_accepts_media_inside_a_store(media_dirs, relative):
    allowed, _ = media_dirs
    f = allowed / relative
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    assert whatsapp.validate_outgoing_media_path(str(f)) == os.path.realpath(f)


@pytest.mark.parametrize(
    "relative",
    [
        "store/history-export.json",  # directly in store/
        "store/session-backup.bin",
        "store/backups/messages.tar.gz",  # a subfolder that is not media
        "store/not-a-jid/photo.jpg",
        # Case-insensitive filesystems (macOS standby) see these as store/.
        "STORE/session-backup.bin",
        "Store/history-export.json",
        "Store/backups/messages.tar.gz",
        # Every store segment counts, not just the first.
        "store/media/x/store/pair-qr.txt",
        "store/491500000001_s.whatsapp.net/store/notes.txt",
    ],
)
def test_refuses_non_media_inside_a_store(media_dirs, relative):
    allowed, _ = media_dirs
    f = allowed / relative
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="store"):
        whatsapp.validate_outgoing_media_path(str(f))


def test_store_rule_holds_even_with_path_check_disabled(media_dirs, monkeypatch):
    allowed, _ = media_dirs
    monkeypatch.setenv("DISABLE_PATH_CHECK", "true")
    f = allowed / "store" / "anything.bin"
    f.parent.mkdir()
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(f))


def test_refuses_a_link_whose_target_looks_like_a_secret(media_dirs):
    allowed, _ = media_dirs
    target = allowed / "id_rsa"
    target.write_text("k")
    link = allowed / "cat.jpg"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="id_rsa"):
        whatsapp.validate_outgoing_media_path(str(link))


def test_disable_path_check_still_refuses_secrets(media_dirs, monkeypatch):
    _, outside = media_dirs
    monkeypatch.setenv("DISABLE_PATH_CHECK", "true")
    ok = outside / "song.mp3"
    ok.write_bytes(b"x")
    assert whatsapp.validate_outgoing_media_path(str(ok)) == os.path.realpath(ok)
    key = outside / "server.pem"
    key.write_text("k")
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(key))


def test_bridge_defaults_are_always_allowed(monkeypatch):
    monkeypatch.delenv("MEDIA_ALLOWED_DIRS", raising=False)
    assert whatsapp._allowed_media_dirs() == list(whatsapp.BRIDGE_DEFAULT_MEDIA_DIRS)
    assert "/tmp" in whatsapp.BRIDGE_DEFAULT_MEDIA_DIRS


def test_send_audio_refuses_before_converting(media_dirs, bridge_calls, conversions):
    _, outside = media_dirs
    f = outside / "notes.mp3"
    f.write_bytes(b"x")

    result = whatsapp.send_audio_message("123", str(f))

    assert result["success"] is False
    # The whole bug: conversion produced a /tmp file the bridge happily sent.
    assert conversions == []
    assert bridge_calls == []


def test_send_audio_converts_an_allowed_file(media_dirs, bridge_calls, conversions):
    allowed, _ = media_dirs
    f = allowed / "notes.mp3"
    f.write_bytes(b"x")

    result = whatsapp.send_audio_message("123", str(f))

    assert result["success"] is True
    assert conversions == [os.path.realpath(f)]
    assert len(bridge_calls) == 1


def test_send_file_refuses_a_path_outside_the_allowlist(media_dirs, bridge_calls):
    _, outside = media_dirs
    f = outside / "report.pdf"
    f.write_bytes(b"x")

    result = whatsapp.send_file("123", media_path=str(f))

    assert result["success"] is False
    assert bridge_calls == []


def test_send_file_refuses_a_secret_inside_the_allowlist(media_dirs, bridge_calls):
    allowed, _ = media_dirs
    f = allowed / ".env"
    f.write_text("TOKEN=x")

    result = whatsapp.send_file("123", media_path=str(f))

    assert result["success"] is False
    assert bridge_calls == []
