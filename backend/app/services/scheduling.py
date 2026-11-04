"""Send windows, working days, holidays and "when may we send next" logic.

Windows are stored as {"weekdays": [python weekday ints], "start": "HH:MM", "end": "HH:MM"}.
Python weekday(): Monday=0 … Sunday=6, so the Egyptian week Sun–Thu is [6, 0, 1, 2, 3].
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ConfigError
from app.core.yamlconf import load_holidays

DEFAULT_SEND_WINDOWS: dict[str, Any] = {
    "weekdays": [6, 0, 1, 2, 3],  # Sun–Thu
    "start": "09:30",
    "end": "15:00",
}

COUNTRY_TZ = {"EG": "Africa/Cairo", "SA": "Asia/Riyadh", "AE": "Asia/Dubai", "QA": "Asia/Qatar", "KW": "Asia/Kuwait"}


def country_timezone(country: str | None, fallback: str = "Africa/Cairo") -> str:
    return COUNTRY_TZ.get((country or "").upper(), fallback)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone: {tz}") from exc


def normalise_windows(windows: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_SEND_WINDOWS)
    if windows:
        merged.update({k: v for k, v in windows.items() if v is not None})
    weekdays = merged.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays:
        merged["weekdays"] = list(DEFAULT_SEND_WINDOWS["weekdays"])
    merged["weekdays"] = sorted({int(w) for w in merged["weekdays"] if 0 <= int(w) <= 6})
    for key in ("start", "end"):
        value = str(merged.get(key, DEFAULT_SEND_WINDOWS[key]))
        if len(value) != 5 or value[2] != ":":
            raise ConfigError(f"send window {key} must be HH:MM")
    return merged


def is_holiday(day: date, country: str) -> bool:
    try:
        return load_holidays(country.upper()).is_holiday(day)
    except ConfigError:
        return False


def within_window(
    now: datetime, tz: str, windows: dict[str, Any] | None, country: str = "EG"
) -> tuple[bool, str]:
    local = now.astimezone(_zone(tz))
    conf = normalise_windows(windows)
    if local.weekday() not in conf["weekdays"]:
        return False, "outside_weekdays"
    if is_holiday(local.date(), country):
        return False, "holiday"
    start_h, start_m = (int(x) for x in str(conf["start"]).split(":"))
    end_h, end_m = (int(x) for x in str(conf["end"]).split(":"))
    if not (time(start_h, start_m) <= local.time() <= time(end_h, end_m)):
        return False, "outside_hours"
    return True, "in_window"


def next_window_start(
    now: datetime, tz: str, windows: dict[str, Any] | None, country: str = "EG", max_days: int = 14
) -> datetime:
    conf = normalise_windows(windows)
    start_h, start_m = (int(x) for x in str(conf["start"]).split(":"))
    zone = _zone(tz)
    local = now.astimezone(zone)
    for offset in range(max_days + 1):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in conf["weekdays"] or is_holiday(day, country):
            continue
        candidate = datetime.combine(day, time(start_h, start_m), tzinfo=zone)
        if candidate > local:
            return candidate.astimezone(now.tzinfo)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def can_send_now(
    now: datetime, *, tz: str, windows: dict[str, Any] | None, country: str
) -> tuple[bool, str, datetime | None]:
    ok, reason = within_window(now, tz, windows, country)
    if ok:
        return True, "in_window", None
    return False, reason, next_window_start(now, tz, windows, country)
