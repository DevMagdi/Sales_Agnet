"""Internal alerting to the sales team (Slack/Teams/Telegram-compatible JSON webhook).

Prospects are never notified through this adapter — it is staff-only.
"""

from __future__ import annotations

from typing import Any

import httpx


class WebhookNotify:
    name = "webhook"
    is_fake = False

    def __init__(self, url: str, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._url), "NOTIFY_WEBHOOK_URL missing" if not self._url else "notify webhook configured")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def send_alert(
        self, *, title: str, body: str, payload: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        if not self._url:
            return False, "notify webhook not configured"
        text = f"*{title}*\n{body}"
        try:
            response = await self._http().post(
                self._url, json={"text": text, "title": title, "body": body, "payload": payload or {}}
            )
        except httpx.HTTPError as exc:
            return False, str(exc)
        return (response.status_code < 400), f"HTTP {response.status_code}"
