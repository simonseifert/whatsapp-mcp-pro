"""Web Push for incoming messages. Listens to the live poller's events and pushes the fresh, visible ones.

History sync and backfill insert old messages one by one, and INSERT OR REPLACE re-inserts known ones, so a "message"
event is not proof of a new message: only messages stamped within FRESH_SECONDS of now count, each (chat, id) at most
once. Nothing here logs message text or payloads.
"""

import json
import logging
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol
from urllib.parse import quote

from . import appdb, push, store
from .config import Settings
from .live import Event

logger = logging.getLogger("wa_app.notify")

FRESH_SECONDS = 120
SEEN_KEYS = 2000
MAX_BODY = 180
MAX_TITLE = 80
MAX_PAYLOAD_BYTES = 3 * 1024

_MEDIA_ICON = {"image": "📷", "video": "🎥", "ptt": "🎤", "audio": "🎵", "document": "📄"}
_MEDIA_NAME = {"image": "Photo", "video": "Video", "ptt": "Voice message", "audio": "Audio"}


class _Hub(Protocol):
    def chat_open(self, chat_jid: str) -> bool: ...


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body(message: dict) -> str:
    text = " ".join((message.get("text") or "").split())
    media = message.get("media")
    if not media:
        return text
    kind = media.get("type") or ""
    if kind == "sticker":
        return "Sticker"
    icon = _MEDIA_ICON.get(kind, "📄")
    if text:
        return f"{icon} {text}"
    if kind == "document":
        return f"{icon} {media.get('filename') or 'Document'}"
    return f"{icon} {_MEDIA_NAME.get(kind, 'Document')}"


def payload_for(chat: store.ChatSummary, message: dict) -> dict | None:
    """The push payload for one message, or None when it must not notify (own, system or deleted messages)."""
    if message.get("from_me") or message.get("system") or message.get("deleted"):
        return None
    body = _body(message)
    sender = message.get("sender_name")
    if chat.kind in store.GROUP_KINDS and sender:
        body = f"{sender}: {body}"
    payload = {
        "chat_jid": chat.jid,
        "title": _cut(chat.name, MAX_TITLE),
        "body": _cut(body, MAX_BODY),
        "tag": chat.jid,
        "url": f"/chat/{quote(chat.jid, safe='')}",
    }
    limit = MAX_BODY
    while len(json.dumps(payload).encode()) > MAX_PAYLOAD_BYTES and limit > 20:  # astral characters weigh 12 bytes
        limit //= 2
        payload["body"] = _cut(body, limit)
    return payload


class Notifier:
    def __init__(
        self, settings: Settings, appconn: sqlite3.Connection, hub: _Hub, now: Callable[[], float] = time.time
    ) -> None:
        self.settings = settings
        self.appconn = appconn
        self.hub = hub
        self.now = now
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-notify")

    def on_events(self, events: list[Event]) -> None:
        """Called from the poller thread: hands the work to the single worker thread and returns."""
        if any(e.name == "message" for e in events):
            self._executor.submit(self._safe, events)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _safe(self, events: list[Event]) -> None:
        try:
            self.handle(events)
        except Exception as exc:  # a failing push must never stop the next one; the type only, text may be quoted
            logger.warning("notify failed (%s)", type(exc).__name__)

    def _fresh_new(self, event: Event) -> dict[str, Any] | None:
        message = event.data.get("message")
        if event.name != "message" or not isinstance(message, dict):
            return None
        key = (str(event.data.get("chat_jid")), str(message.get("id")))
        if key in self._seen:
            return None
        self._seen[key] = None
        while len(self._seen) > SEEN_KEYS:
            self._seen.popitem(last=False)
        stamped = store.parse_ts(message.get("timestamp_iso"))
        if stamped is None or self.now() - stamped.timestamp() > FRESH_SECONDS:
            return None
        return message

    def handle(self, events: list[Event]) -> None:
        """Push each fresh, first-seen incoming message of unmuted chats that no visible client has open."""
        ro: sqlite3.Connection | None = None
        try:
            for event in events:
                message = self._fresh_new(event)
                if message is None or message.get("from_me") or message.get("system") or message.get("deleted"):
                    continue
                jid = str(event.data["chat_jid"])
                if self.hub.chat_open(jid):
                    continue
                if ro is None:
                    ro = store.open_ro(self.settings)
                with appdb.db_lock:
                    chat = store.get_chat(ro, self.appconn, jid)
                if chat is None or chat.muted:
                    continue
                payload = payload_for(chat, message)
                if payload is not None:
                    push.send(self.settings, self.appconn, payload)
        finally:
            if ro is not None:
                ro.close()
