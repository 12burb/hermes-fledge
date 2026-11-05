"""Chat notices. ``ctx.post_message`` only reaches the conversation bound to the calling hook, and the
background-review fork may not be bound to one, so notices are queued per session and flushed from the
next foreground hook of that session (turn start or turn end). Never raises."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable

logger = logging.getLogger(__name__)

PREFIX = "🪶 Fledge: "
_MAX_PER_SESSION = 5
_MAX_SESSIONS = 128


class Notifier:
    def __init__(self, post: Callable[[str], object], enabled: Callable[[], bool]):
        self._post = post
        self._enabled = enabled
        self._lock = threading.Lock()
        self._queue: OrderedDict[str, list[str]] = OrderedDict()

    def queue(self, session_id: str, text: str) -> None:
        if not text or not self._enabled():
            return
        key = session_id or "*"
        with self._lock:
            items = self._queue.setdefault(key, [])
            if text not in items:
                items.append(text)
                del items[:-_MAX_PER_SESSION]
            self._queue.move_to_end(key)
            while len(self._queue) > _MAX_SESSIONS:
                self._queue.popitem(last=False)

    def pending(self, session_id: str) -> list[str]:
        with self._lock:
            return list(self._queue.get(session_id or "*", [])) + list(self._queue.get("*", []))

    def flush(self, session_id: str) -> int:
        """Post queued notices for *session_id* (and session-less ones) into the bound conversation."""
        with self._lock:
            items = self._queue.pop(session_id, []) if session_id else []
            items += self._queue.pop("*", [])
        if not items:
            return 0
        text = PREFIX + items[0] if len(items) == 1 else PREFIX + "\n" + "\n".join(f"• {i}" for i in items)
        try:
            handle = self._post(text)
            ok = getattr(handle, "ok", True)
            if ok is False:
                logger.debug("fledge: notice not posted (%s)", getattr(handle, "error", "unknown"))
                if getattr(handle, "error", "") in ("no_conversation", "on_event_loop", "gateway_unavailable"):
                    with self._lock:  # keep it for the next bound hook of this session
                        self._queue.setdefault(session_id or "*", [])[:0] = items
                return 0
        except Exception:
            logger.debug("fledge: posting a notice failed", exc_info=True)
            return 0
        return len(items)
