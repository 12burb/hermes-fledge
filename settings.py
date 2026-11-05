"""Fledge settings, read from ``plugins.entries.fledge.settings`` through ``ctx.get_config``.

Values are re-read on a short TTL so a change in the Desktop settings form or ``hermes config set``
takes effect without a restart. Anything invalid falls back to its default (and is logged once).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any

logger = logging.getLogger(__name__)

STRICTNESS_THRESHOLDS = {"lenient": 4, "standard": 3, "strict": 2}
_CHOICES = {
    "mode": ("enforce", "watch", "off"),
    "strictness": tuple(STRICTNESS_THRESHOLDS),
    "on_hand_edit": ("protect", "lock"),
    "foreground_bad_run": ("approve", "stage", "allow"),
}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    mode: str = "enforce"
    strictness: str = "standard"
    graduate_after: int = 3
    ground_after: int = 2
    auto_archive: bool = True
    protect_hand_edits: bool = True
    on_hand_edit: str = "protect"
    foreground_bad_run: str = "approve"
    notify: bool = True
    settle_hours: float = 6.0

    @property
    def bad_threshold(self) -> int:
        return STRICTNESS_THRESHOLDS[self.strictness]

    @property
    def active(self) -> bool:
        return self.mode in ("enforce", "watch")

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @property
    def settle_seconds(self) -> float:
        return self.settle_hours * 3600.0


_DEFAULTS = Settings()


def _coerce(name: str, raw: Any) -> Any:
    """Return a valid value for field *name* or raise ValueError."""
    default = getattr(_DEFAULTS, name)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    if isinstance(default, int):
        if isinstance(raw, bool):
            raise ValueError(f"expected an integer, got {raw!r}")
        value = int(raw)
        if value < 1 or value > 100:
            raise ValueError(f"expected 1..100, got {value}")
        return value
    if isinstance(default, float):
        value = float(raw)
        if value <= 0 or value > 24 * 30:
            raise ValueError(f"expected 0 < hours <= 720, got {value}")
        return value
    text = str(raw).strip().lower()
    if text not in _CHOICES[name]:
        raise ValueError(f"expected one of {', '.join(_CHOICES[name])}, got {raw!r}")
    return text


def build_settings(getter: Callable[..., Any], warn: Callable[[str], None] | None = None) -> Settings:
    """Build Settings from ``getter(key, default=...)`` (``ctx.get_config``)."""
    values: dict[str, Any] = {}
    for f in fields(Settings):
        default = getattr(_DEFAULTS, f.name)
        try:
            raw = getter(f.name, default=default)
        except Exception:  # a broken config read must never break the agent
            raw = default
        if raw is None:
            raw = default
        try:
            values[f.name] = _coerce(f.name, raw)
        except (TypeError, ValueError) as exc:
            if warn is not None:
                warn(f"fledge: setting {f.name!r} ignored ({exc}); using {default!r}")
            values[f.name] = default
    return Settings(**values)


class SettingsLoader:
    """TTL-cached Settings; thread-safe."""

    def __init__(self, getter: Callable[..., Any], ttl: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self._getter = getter
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Settings | None = None
        self._at = 0.0
        self._warned: set[str] = set()

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            logger.warning(message)

    def get(self) -> Settings:
        with self._lock:
            now = self._clock()
            if self._cached is None or now - self._at >= self._ttl:
                self._cached = build_settings(self._getter, self._warn_once)
                self._at = now
            return self._cached

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
