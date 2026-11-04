"""WhatsApp Cloud API adapter (Meta).

Outbound rules live above this adapter (Send Gate): cold messaging is impossible — only an
in-window reply or an approved template to an opted-in number reaches `send_*`.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.adapters.base import WhatsAppEvent, WhatsAppSendResult


class WhatsAppCloudAdapter:
    name = "whatsapp_cloud"
    is_fake = False

    def __init__(
        self,
        phone_number_id: str,
        token: str,
        app_secret: str,
        api_url: str = "https://graph.facebook.com/v20.0",
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._phone_id = phone_number_id
        self._token = token
        self._app_secret = app_secret
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        if not (self._phone_id and self._token and self._app_secret):
            return False, "WHATSAPP_PHONE_NUMBER_ID/WHATSAPP_TOKEN/WHATSAPP_APP_SECRET missing"
        return True, "WhatsApp Cloud API configured"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _post(self, body: dict[str, Any]) -> WhatsAppSendResult:
        try:
            response = await self._http().post(
                f"{self._api_url}/{self._phone_id}/messages",
                json=body,
                headers={"Authorization": f"Bearer {self._token}", "content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            return WhatsAppSendResult(ok=False, error=str(exc))
        if response.status_code >= 400:
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            return WhatsAppSendResult(
                ok=False,
                error=f"HTTP {response.status_code}: {response.text[:300]}",
                permanent=permanent,
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        messages = data.get("messages") or []
        provider_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        return WhatsAppSendResult(ok=True, provider_message_id=provider_id, detail=data)

    async def send_text(self, to_phone: str, text: str) -> WhatsAppSendResult:
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            }
        )

    async def send_template(
        self, to_phone: str, template_name: str, language: str, params: list[str]
    ) -> WhatsAppSendResult:
        components: list[dict[str, Any]] = []
        if params:
            components.append(
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in params[:3]]}
            )
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": components,
                },
            }
        )

    def verify_signature(self, raw_body: bytes, signature_header: str) -> bool:
        if not self._app_secret or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.split("=", 1)[1])

    def parse_events(self, payload: dict[str, Any]) -> list[WhatsAppEvent]:
        events: list[WhatsAppEvent] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    text = ""
                    if msg.get("type") == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    events.append(
                        WhatsAppEvent(
                            type="message",
                            from_phone=msg.get("from"),
                            text=text,
                            message_id=msg.get("id"),
                            timestamp=msg.get("timestamp"),
                            raw=msg,
                        )
                    )
                for status in value.get("statuses") or []:
                    events.append(
                        WhatsAppEvent(
                            type="status",
                            message_id=status.get("id"),
                            status=status.get("status"),
                            from_phone=status.get("recipient_id"),
                            timestamp=status.get("timestamp"),
                            raw=status,
                        )
                    )
        return events
