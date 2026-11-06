"""Chat notices. ``ctx.post_message`` only reaches the conversation bound to the calling context
(hermes_cli/plugin_messages.py ``post``: ``bound_session_key()``), and Hermes runs the background-review
fork in a plain thread with no session bound. So each foreground hook remembers its session's context,
and a notice raised in the fork is posted at once from a copy of THAT session's context: it can only reach
the conversation the hold belongs to. If that fails, it stays queued and the next foreground hook of the
session (turn start or turn end) posts it.

Local sessions (CLI, TUI, Desktop) get the notice as a keyed ``notification.show`` event
(gateway/plugin_messages.py ``_publish_local``). The TUI and Desktop show it at once, but the classic
``hermes chat`` CLI only reads session events while a turn is running, so one posted between turns never
appears there. So a local notice posted from the review thread is re-published once (``handle.edit`` with
the same text and key, which replaces it in place where it was already shown) at the session's next
foreground hook. Never raises."""

from __future__ import annotations

import contextvars
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

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
        self._contexts: OrderedDict[str, contextvars.Context] = OrderedDict()
        self._echo: OrderedDict[str, list[tuple[Any, str]]] = OrderedDict()

    def remember_context(self, session_id: str) -> None:
        """Called from a foreground hook of *session_id*: keep its context (the session binding)."""
        if not session_id:
            return
        try:
            context = contextvars.copy_context()
        except Exception:
            return
        with self._lock:
            self._contexts[session_id] = context
            self._contexts.move_to_end(session_id)
            while len(self._contexts) > _MAX_SESSIONS:
                self._contexts.popitem(last=False)

    def forget_context(self, session_id: str) -> None:
        with self._lock:
            self._contexts.pop(session_id, None)
            self._echo.pop(session_id, None)

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

    def flush_soon(self, session_id: str) -> threading.Thread | None:
        """Try to post now, from a short-lived thread running in a copy of the session's remembered
        context (or the caller's, if none was remembered). The hook never waits on the post; anything
        that can't be posted stays queued for the next foreground hook, as before."""
        if not self.pending(session_id):
            return None
        try:
            with self._lock:
                remembered = self._contexts.get(session_id) if session_id else None
            ctx = remembered.copy() if remembered is not None else contextvars.copy_context()
            thread = threading.Thread(target=ctx.run, args=(self._flush, session_id, True), name="fledge-notice",
                                      daemon=True)
            thread.start()
            return thread
        except Exception:
            logger.debug("fledge: could not start a notice thread", exc_info=True)
            return None

    def pending(self, session_id: str) -> list[str]:
        with self._lock:
            return list(self._queue.get(session_id or "*", [])) + list(self._queue.get("*", []))

    def flush(self, session_id: str) -> int:
        """From a foreground hook: post queued notices for *session_id* (and session-less ones) into the
        bound conversation, and re-publish local notices posted since the last turn."""
        self._echo_local(session_id)
        return self._flush(session_id, False)

    def _echo_local(self, session_id: str) -> None:
        with self._lock:
            echoes = self._echo.pop(session_id, []) if session_id else []
        for handle, text in echoes:
            try:
                handle.edit(text)
            except Exception:
                logger.debug("fledge: re-publishing a notice failed", exc_info=True)

    def _flush(self, session_id: str, background: bool) -> int:
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
        result = getattr(handle, "result", None)
        if background and session_id and isinstance(result, dict) and "delivered" in result:
            with self._lock:  # a local session: show it again where the CLI is listening (next turn)
                self._echo.setdefault(session_id, []).append((handle, text))
                self._echo.move_to_end(session_id)
                while len(self._echo) > _MAX_SESSIONS:
                    self._echo.popitem(last=False)
        return len(items)
