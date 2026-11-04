"""Calendar adapter factory."""

from __future__ import annotations

from app.adapters.calendar.link import FakeCalendarAdapter, LinkCalendarAdapter
from app.core.config import Settings

__all__ = ["FakeCalendarAdapter", "LinkCalendarAdapter", "build_calendar"]


def build_calendar(settings: Settings) -> LinkCalendarAdapter:
    if settings.calendar_provider == "fake":
        return FakeCalendarAdapter(settings.booking_url, tz=settings.tz_default)
    return LinkCalendarAdapter(settings.booking_url, tz=settings.tz_default)
