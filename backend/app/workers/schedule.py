"""Human-readable view of the beat schedule (also served by the dashboard)."""

from __future__ import annotations

from typing import Any

from app.workers.celery_app import celery_app


def schedule_summary() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name, entry in celery_app.conf.beat_schedule.items():
        items.append({"name": name, "task": entry["task"], "schedule": str(entry["schedule"])})
    return items
