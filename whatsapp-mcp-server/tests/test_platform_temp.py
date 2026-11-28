"""The temp root the MCP server stages files in, and the ffmpeg-missing error, on every OS."""

import os
import subprocess

import pytest

import audio
import whatsapp


def test_linux_keeps_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(whatsapp.sys, "platform", "linux")
    monkeypatch.setattr(whatsapp.tempfile, "tempdir", str(tmp_path))
    assert whatsapp.platform_temp_dir() == "/tmp"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_other_platforms_use_the_platform_temp_dir(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(whatsapp.sys, "platform", platform)
    monkeypatch.setattr(whatsapp.tempfile, "tempdir", str(tmp_path))
    assert whatsapp.platform_temp_dir() == str(tmp_path)


def test_default_dirs_per_platform():
    assert "/tmp" in whatsapp.default_media_dirs("/var/folders/x/T", "darwin")
    assert "/var/folders/x/T" in whatsapp.default_media_dirs("/var/folders/x/T", "darwin")
    win = whatsapp.default_media_dirs(r"C:\Temp", "win32")
    assert "/tmp" not in win
    assert r"C:\Temp" in win
    assert whatsapp.default_media_dirs("/tmp", "linux").count("/tmp") == 1


def test_allowlist_accepts_platform_temp_and_refuses_sibling(monkeypatch, tmp_path):
    temp = tmp_path / "T"
    sibling = tmp_path / "T-evil"
    temp.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(whatsapp, "BRIDGE_DEFAULT_MEDIA_DIRS", whatsapp.default_media_dirs(str(temp), "win32"))
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
    monkeypatch.setattr(audio.sys, "platform", platform)
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
