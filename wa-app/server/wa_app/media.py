"""Media files, thumbnails and avatars.

Every automatic load passes no_phone_retry=True: rendering a chat must never make the bridge ask the sender's
phone to re-upload media (that pings the phone and spends an hourly budget). Only an explicit "Try again" tap
(`retry=True`) may. Files are served only from inside the bridge store directory.
"""

import hashlib
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import mcp_lib
from .config import Settings

logger = logging.getLogger("wa_app.media")

THUMB_PX = 480
AVATAR_PX = 256
AVATAR_TTL = 24 * 3600
AVATAR_NEGATIVE_TTL = 6 * 3600
AVATAR_ERROR_TTL = 5 * 60
MAX_AVATAR_BYTES = 2_000_000
FFMPEG_TIMEOUT = 20

_JID = re.compile(r"^[0-9A-Za-z._:\-]{1,64}@[a-z.]{1,32}$")
_avatar_slots = threading.BoundedSemaphore(4)

# Types the browser may show inline. Everything else (documents, SVG, HTML) is a download, never rendered.
_INLINE_IMAGES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_EXTRA_TYPES = {
    ".opus": "audio/ogg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".webp": "image/webp",
}


def content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return _EXTRA_TYPES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def is_inline(ctype: str) -> bool:
    return ctype in _INLINE_IMAGES or ctype.startswith(("audio/", "video/"))


def contained(settings: Settings, raw: str | None) -> Path | None:
    """The real path of `raw` if it is a regular file inside the store (below a per-chat folder), else None."""
    if not raw:
        return None
    try:
        real = Path(raw).resolve(strict=True)
        root = settings.store_path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or not real.is_relative_to(root):
        return None
    parts = real.relative_to(root).parts
    if len(parts) < 2 or any(part.startswith(".") for part in parts):
        return None
    return real


def file_for(settings: Settings, message_id: str, chat_jid: str, *, retry: bool = False) -> Path | None:
    """The message's media file, or None. `retry=True` lets the bridge ask the sender's phone."""
    raw = mcp_lib.download_media(message_id, chat_jid, no_phone_retry=not retry)
    path = contained(settings, raw)
    if raw and path is None:
        logger.warning("download_media returned a path outside the store for %s; not served", message_id)
    return path


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _encode(img: Image.Image, max_px: int) -> tuple[bytes, str]:
    import io

    img = ImageOps.exif_transpose(img) or img
    img.thumbnail((max_px, max_px))
    buffer = io.BytesIO()
    if "A" in img.getbands() or img.mode in ("P", "LA", "PA"):
        img.convert("RGBA").save(buffer, "PNG", optimize=True)
        return buffer.getvalue(), ".png"
    img.convert("RGB").save(buffer, "JPEG", quality=82)
    return buffer.getvalue(), ".jpg"


def _video_frame(path: Path) -> bytes | None:
    try:
        run = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 and run.stdout else None


def thumbnail(settings: Settings, path: Path, max_px: int = THUMB_PX) -> Path | None:
    """A cached thumbnail (JPEG, or PNG when transparent) of an image, sticker or video; None if it can't be made."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = hashlib.sha256(f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{max_px}".encode()).hexdigest()[:40]
    folder = settings.data_dir / "thumbs"
    for ext in (".jpg", ".png"):
        if (folder / f"{key}{ext}").is_file():
            return folder / f"{key}{ext}"
    ctype = content_type(path)
    try:
        if ctype.startswith("video/"):
            frame = _video_frame(path)
            if frame is None:
                return None
            import io

            with Image.open(io.BytesIO(frame)) as img:
                data, ext = _encode(img, max_px)
        elif ctype.startswith("image/"):
            with Image.open(path) as img:
                data, ext = _encode(img, max_px)
        else:
            return None
    except Exception:  # noqa: BLE001 - Pillow raises many types on hostile or truncated files
        logger.info("no thumbnail for %s", path.name)
        return None
    target = folder / f"{key}{ext}"
    _write_atomic(target, data)
    return target


def _fetch(url: str) -> bytes | None:
    import requests

    with requests.get(url, timeout=15, stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            return None
        data = b""
        for chunk in response.iter_content(65536):
            data += chunk
            if len(data) > MAX_AVATAR_BYTES:
                return None
        return data


def _fresh(path: Path, ttl: int) -> bool:
    try:
        return time.time() - path.stat().st_mtime < ttl
    except OSError:
        return False


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def avatar(settings: Settings, jid: str) -> Path | None:
    """Profile picture for a chat or person, cached 24 h; a confirmed "no picture" is remembered for 6 h."""
    if not _JID.match(jid):
        return None
    folder = settings.data_dir / "avatars"
    stem = folder / hashlib.sha256(jid.encode()).hexdigest()[:40]
    cached, none, err = stem.with_suffix(".jpg"), stem.with_suffix(".none"), stem.with_suffix(".err")
    if _fresh(cached, AVATAR_TTL):
        return cached
    if _fresh(none, AVATAR_NEGATIVE_TTL) or _fresh(err, AVATAR_ERROR_TTL):
        return None
    with _avatar_slots:
        if _fresh(cached, AVATAR_TTL):
            return cached
        try:
            info = mcp_lib.get_profile_picture(jid, preview=True)
        except Exception:  # noqa: BLE001
            info = {"success": False}
        url = info.get("url") if isinstance(info, dict) else None
        if not url:
            _touch(
                err if (isinstance(info, dict) and info.get("success") is False) or not isinstance(info, dict) else none
            )
            return None
        if not str(url).startswith("https://"):
            _touch(none)
            return None
        try:
            raw = _fetch(str(url))
            if raw is None:
                raise ValueError("no data")
            import io

            with Image.open(io.BytesIO(raw)) as img:
                data, _ = _encode(img.convert("RGB"), AVATAR_PX)
        except Exception:  # noqa: BLE001
            _touch(err)
            return None
        _write_atomic(cached, data)
        none.unlink(missing_ok=True)
        return cached
