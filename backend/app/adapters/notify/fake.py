"""Fake notify adapter: records alerts."""

from __future__ import annotations

from typing import Any


class FakeNotify:
    name = "fake"
    is_fake = True

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake notify adapter"

    async def send_alert(
        self, *, title: str, body: str, payload: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        self.alerts.append({"title": title, "body": body, "payload": payload or {}})
        return True, "recorded"
