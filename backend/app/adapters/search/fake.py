"""Fake search adapter: scripted results, no network."""

from __future__ import annotations

from app.adapters.base import SearchResult


class FakeSearch:
    name = "fake"
    is_fake = True
    cost_per_call_usd = 0.0

    def __init__(self, results: dict[str, list[SearchResult]] | None = None) -> None:
        self.results: dict[str, list[SearchResult]] = results or {}
        self.calls: list[str] = []

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake search adapter"

    def add(self, query: str, results: list[SearchResult]) -> None:
        self.results[query] = results

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        for key, value in self.results.items():
            if key.lower() in query.lower():
                return value[:num_results]
        return []
