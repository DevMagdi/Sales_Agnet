"""Fake Places adapter."""

from __future__ import annotations

from app.adapters.base import PlaceResult


class FakePlaces:
    name = "fake"
    is_fake = True
    cost_per_call_usd = 0.0

    def __init__(self, results: list[PlaceResult] | None = None) -> None:
        self.results: list[PlaceResult] = results or []
        self.calls: list[str] = []

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake places adapter"

    async def text_search(self, query: str, max_results: int = 20) -> list[PlaceResult]:
        self.calls.append(query)
        return self.results[:max_results]
