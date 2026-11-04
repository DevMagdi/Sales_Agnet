"""Brave Search API adapter — official API only."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import SearchResult


class BraveSearch:
    name = "brave"
    is_fake = False
    cost_per_call_usd = 0.005

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.search.brave.com/res/v1/web/search",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._api_key), "SEARCH_API_KEY missing" if not self._api_key else "brave configured")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        response = await self._http().get(
            self._endpoint,
            params={"q": query, "count": min(num_results, 20)},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        results = (data.get("web") or {}).get("results") or []
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description", "")),
            )
            for item in results[:num_results]
            if item.get("url")
        ]
