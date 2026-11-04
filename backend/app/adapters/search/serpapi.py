"""SerpAPI (Google) search adapter — official API only."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import SearchResult


class SerpAPISearch:
    name = "serpapi"
    is_fake = False
    cost_per_call_usd = 0.01

    def __init__(
        self, api_key: str, endpoint: str = "https://serpapi.com/search.json", client: httpx.AsyncClient | None = None
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._api_key), "SEARCH_API_KEY missing" if not self._api_key else "serpapi configured")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        response = await self._http().get(
            self._endpoint,
            params={
                "engine": "google",
                "q": query,
                "num": min(num_results, 20),
                "api_key": self._api_key,
                "hl": "ar" if any("\u0600" <= c <= "\u06FF" for c in query) else "en",
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("link", "")),
                snippet=str(item.get("snippet", "")),
            )
            for item in (data.get("organic_results") or [])[:num_results]
            if item.get("link")
        ]
