"""Calendar adapter backed by a configured booking link (no vendor OAuth needed)."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from app.core.clock import Clock, SystemClock


class LinkCalendarAdapter:
    name = "link"
    is_fake = False

    def __init__(self, booking_url: str, clock: Clock | None = None, tz: str = "Africa/Cairo") -> None:
        self._booking_url = booking_url
        self._clock = clock or SystemClock()
        self._tz = tz

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._booking_url), "BOOKING_URL missing" if not self._booking_url else "booking link set")

    def booking_link(self) -> str:
        return self._booking_url

    def propose_slots(self, count: int = 3) -> list[str]:
        """Next `count` working days (Sun–Thu) at 11:00 and 14:00 local time."""
        now = self._clock.now().astimezone(ZoneInfo(self._tz))
        slots: list[str] = []
        day = now.date()
        while len(slots) < count:
            day = day + timedelta(days=1)
            if day.weekday() in (4, 5):  # Friday/Saturday weekend in Egypt
                continue
            for hour in (11, 14):
                slots.append(f"{day.isoformat()} {hour:02d}:00 {self._tz}")
                if len(slots) >= count:
                    break
        return slots


class FakeCalendarAdapter(LinkCalendarAdapter):
    name = "fake"
    is_fake = True
