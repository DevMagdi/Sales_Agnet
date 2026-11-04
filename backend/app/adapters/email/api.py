"""Generic provider-API email adapter (SES / Mailgun / Postmark / Brevo style JSON API).

One interface, configurable endpoint: the request shape below is the common denominator
(`to`, `from`, `subject`, `text`, `html`, `headers`). Swap EMAIL_API_URL/EMAIL_API_KEY for the
provider you use and map the response id in `_extract_id` if it differs.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal

import httpx

from app.adapters.base import EmailEvent, OutboundEmail, SendResult
from app.adapters.email.mime import new_message_id


class ApiEmailAdapter:
    name = "api"
    is_fake = False

    def __init__(
        self,
        api_url: str,
        api_key: str,
        webhook_secret: str = "",
        from_email: str = "",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._from_email = from_email
        self._timeout = timeout
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        if not self._api_url or not self._api_key:
            return False, "EMAIL_API_URL/EMAIL_API_KEY missing"
        return True, f"api {self._api_url}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @staticmethod
    def _extract_id(data: dict[str, Any]) -> str | None:
        for key in ("id", "message_id", "MessageId", "messageId", "data"):
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                for inner in ("id", "message_id"):
                    if isinstance(value.get(inner), str):
                        return str(value[inner])
        return None

    async def send(self, message: OutboundEmail) -> SendResult:
        domain = message.from_email.partition("@")[2]
        message_id = message.message_id or new_message_id(domain)
        headers: dict[str, str] = {"Message-ID": message_id, **message.headers}
        if message.unsubscribe_url:
            headers["List-Unsubscribe"] = f"<{message.unsubscribe_url}>"
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        if message.reply_to:
            headers["Reply-To"] = message.reply_to
        body = {
            "to": message.to_email,
            "from": f"{message.from_name} <{message.from_email}>",
            "subject": message.subject,
            "text": message.body_text,
            "html": message.body_html,
            "headers": headers,
        }
        try:
            response = await self._http().post(
                self._api_url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            return SendResult(ok=False, provider="api", error=str(exc), permanent=False)
        if response.status_code >= 400:
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            return SendResult(
                ok=False,
                provider="api",
                error=f"HTTP {response.status_code}: {response.text[:300]}",
                permanent=permanent,
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        return SendResult(
            ok=True,
            provider="api",
            provider_message_id=self._extract_id(data),
            rfc_message_id=message_id,
        )

    def parse_event(self, payload: dict[str, Any]) -> list[EmailEvent]:
        raw_type = str(payload.get("event") or payload.get("type") or payload.get("notificationType") or "").lower()
        mapping: dict[str, str] = {
            "delivered": "delivered",
            "delivery": "delivered",
            "bounce": "bounce_hard",
            "permanent_fail": "bounce_hard",
            "bounced": "bounce_hard",
            "soft_bounce": "bounce_soft",
            "transient_fail": "bounce_soft",
            "dropped": "bounce_hard",
            "complaint": "complaint",
            "spam": "complaint",
            "opened": "open",
            "open": "open",
            "clicked": "click",
            "click": "click",
        }
        if raw_type not in mapping:
            return []
        kind: Literal["delivered", "bounce_hard", "bounce_soft", "complaint", "open", "click"] = mapping[raw_type]  # type: ignore[assignment]
        bounce_subtype = str(payload.get("bounce_type") or payload.get("bounceType") or "").lower()
        if kind == "bounce_hard" and bounce_subtype in ("soft", "transient", "temporary"):
            kind = "bounce_soft"
        return [
            EmailEvent(
                type=kind,
                message_id=payload.get("message_id") or payload.get("MessageId") or payload.get("message-id"),
                recipient=payload.get("recipient") or payload.get("email"),
                reason=payload.get("reason") or payload.get("description"),
                occurred_at=payload.get("timestamp"),
                meta=payload,
            )
        ]

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        if not self._webhook_secret:
            return False
        normalised = {k.lower(): v for k, v in headers.items()}
        supplied = normalised.get("x-mantiq-signature") or normalised.get("x-signature") or ""
        expected = hmac.new(self._webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, supplied)
