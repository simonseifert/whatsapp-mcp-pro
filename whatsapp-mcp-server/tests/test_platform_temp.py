"""The temp root the MCP server stages files in, and the ffmpeg-missing error, on every OS."""

import os
import subprocess

import pytest

import audio
import whatsapp

WIN_ENV = {"TMP": r"C:\Users\Me\AppData\Local\Temp", "TEMP": r"D:\scratch", "USERPROFILE": r"C:\Users\Me"}


def test_linux_keeps_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(whatsapp.tempfile, "tempdir", str(tmp_path))
    assert whatsapp.platform_temp_dir("linux", {"TMPDIR": str(tmp_path)}) == "/tmp"


def test_darwin_uses_the_platform_temp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(whatsapp.tempfile, "tempdir", str(tmp_path))
    assert whatsapp.platform_temp_dir("darwin", {}) == str(tmp_path)


def test_windows_follows_go_order_tmp_then_temp_then_userprofile():
    assert whatsapp.platform_temp_dir("win32", WIN_ENV) == WIN_ENV["TMP"]
    assert whatsapp.platform_temp_dir("win32", {"TEMP": WIN_ENV["TEMP"], "USERPROFILE": "C:\\u"}) == WIN_ENV["TEMP"]
    assert whatsapp.platform_temp_dir("win32", {"USERPROFILE": r"C:\Users\Me"}) == r"C:\Users\Me"
    assert whatsapp.platform_temp_dir("win32", {"TMP": "relative"}) == whatsapp.tempfile.gettempdir()


def test_default_dirs_per_platform():
    linux = whatsapp.default_media_dirs("/var/tmp/x", "linux", {})
    assert "/tmp" in linux
    assert "/var/tmp/x" not in linux  # a stray TMPDIR never widens the Linux list
    darwin = whatsapp.default_media_dirs("/var/folders/x/T", "darwin", {})
    assert "/tmp" in darwin
    assert "/var/folders/x/T" in darwin
    win = whatsapp.default_media_dirs(WIN_ENV["TMP"], "win32", WIN_ENV)
    assert "/tmp" not in win
    assert WIN_ENV["TMP"] in win
    assert WIN_ENV["TEMP"] in win
    assert len([d for d in win if d.lower() == WIN_ENV["TMP"].lower()]) == 1


@pytest.mark.parametrize(
    ("path", "root", "platform", "expected"),
    [
        (r"C:\Users\ME\AppData\Local\TEMP\a.ogg", r"c:\users\me\appdata\local\temp", "win32", True),
        ("c:/users/me/appdata/local/temp/a.ogg", r"C:\Users\Me\AppData\Local\Temp\\", "win32", True),
        (r"C:\Users\Me\AppData\Local\Temp-evil\a.ogg", r"C:\Users\Me\AppData\Local\Temp", "win32", False),
        (r"D:\Users\Me\AppData\Local\Temp\a.ogg", r"C:\Users\Me\AppData\Local\Temp", "win32", False),
        ("/tmp/a.ogg", "/tmp", "linux", True),
        ("/TMP/a.ogg", "/tmp", "linux", False),
        ("/tmproot/a.ogg", "/tmp", "linux", False),
    ],
)
def test_path_inside(path, root, platform, expected):
    assert whatsapp.path_inside(path, root, platform) is expected


def test_allowlist_accepts_platform_temp_and_refuses_sibling(monkeypatch, tmp_path):
    temp = tmp_path / "T"
    sibling = tmp_path / "T-evil"
    temp.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(
        whatsapp, "BRIDGE_DEFAULT_MEDIA_DIRS", whatsapp.default_media_dirs(str(temp), "linux", {})[:3] + (str(temp),)
    )
    monkeypatch.delenv("MEDIA_ALLOWED_DIRS", raising=False)
    monkeypatch.delenv("DISABLE_PATH_CHECK", raising=False)
    inside = temp / "voice.ogg"
    inside.write_bytes(b"x")
    outside = sibling / "voice.ogg"
    outside.write_bytes(b"x")
    assert whatsapp.validate_outgoing_media_path(str(inside)) == os.path.realpath(inside)
    with pytest.raises(ValueError):
        whatsapp.validate_outgoing_media_path(str(outside))


def test_converted_audio_lands_in_the_given_temp_dir(monkeypatch, tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"x")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **k: None)
    result = audio.convert_to_opus_ogg_temp(str(src), temp_dir=str(out_dir))
    assert os.path.dirname(result) == str(out_dir)


@pytest.mark.parametrize(
    ("platform", "command"),
    [
        ("linux", "sudo apt install ffmpeg"),
        ("darwin", "brew install ffmpeg"),
        ("win32", "winget install --id Gyan.FFmpeg -e"),
    ],
)
def test_missing_ffmpeg_names_the_install_command(monkeypatch, tmp_path, platform, command):
    src = tmp_path / "in.wav"
    src.write_bytes(b"x")

    def boom(*_a, **_k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(audio.subprocess, "run", boom)
    monkeypatch.setattr(audio, "_platform", lambda: platform)
    with pytest.raises(RuntimeError, match="ffmpeg is not installed") as err:
        audio.convert_to_opus_ogg(str(src), str(tmp_path / "o.ogg"))
    assert command in str(err.value)


def test_failed_conversion_still_reports_ffmpeg_output(monkeypatch, tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"x")

    def fail(*_a, **_k):
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr="bad input")

    monkeypatch.setattr(audio.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="bad input"):
        audio.convert_to_opus_ogg(str(src), str(tmp_path / "o.ogg"))
