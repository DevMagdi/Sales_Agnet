"""Injectable clock so every time-dependent rule is testable."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC instant."""

    def today(self, tz: str = "Africa/Cairo") -> date: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self, tz: str = "Africa/Cairo") -> date:
        return self.now().astimezone(ZoneInfo(tz)).date()


class FrozenClock:
    """Test clock: advance time explicitly."""

    def __init__(self, instant: datetime | None = None) -> None:
        self._instant = instant or datetime(2026, 1, 4, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant

    def today(self, tz: str = "Africa/Cairo") -> date:
        return self._instant.astimezone(ZoneInfo(tz)).date()

    def advance(self, **kwargs: float) -> None:
        self._instant = self._instant + timedelta(**kwargs)

    def set(self, instant: datetime) -> None:
        self._instant = instant


UTC = UTC
