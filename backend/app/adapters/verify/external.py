"""External email-verification provider adapter (ZeroBounce-style)."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import VerifyResult


class ExternalVerifyAdapter:
    name = "zerobounce"
    is_fake = False

    def __init__(
        self, api_key: str, endpoint: str = "https://api.zerobounce.net/v2/validate",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._api_key), "VERIFY_API_KEY missing" if not self._api_key else "zerobounce configured")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def verify(self, email: str) -> VerifyResult:
        try:
            response = await self._http().get(
                self._endpoint, params={"api_key": self._api_key, "email": email}
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            return VerifyResult(status="unknown", reason=f"provider_error: {exc}", provider=self.name)
        raw = str(data.get("status", "")).lower()
        mapping = {"valid": "valid", "invalid": "invalid", "catch-all": "risky", "unknown": "unknown", "spamtrap": "invalid", "abuse": "invalid", "do_not_mail": "invalid"}
        status = mapping.get(raw, "unknown")
        return VerifyResult(status=status, reason=str(data.get("sub_status", "")), provider=self.name)  # type: ignore[arg-type]
