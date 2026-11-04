"""In-process fixed-window rate limiting (login, public endpoints, webhooks)."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.core.clock import Clock, SystemClock
from app.core.errors import RateLimited


@dataclass
class _Window:
    start: float
    count: int


class FixedWindowLimiter:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = self._clock.now().timestamp()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.start >= window_seconds:
                self._windows[key] = _Window(start=now, count=1)
                return
            if window.count >= limit:
                retry = int(window_seconds - (now - window.start)) + 1
                raise RateLimited("too many requests", details={"retry_after_seconds": retry})
            window.count += 1

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._windows.clear()
            else:
                self._windows.pop(key, None)
