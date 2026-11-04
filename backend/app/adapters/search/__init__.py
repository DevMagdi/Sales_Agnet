"""Search + places adapter factories."""

from __future__ import annotations

from app.adapters.search.brave import BraveSearch
from app.adapters.search.fake import FakeSearch
from app.adapters.search.serpapi import SerpAPISearch
from app.core.config import Settings

__all__ = ["BraveSearch", "FakeSearch", "SerpAPISearch", "build_search"]


def build_search(settings: Settings) -> SerpAPISearch | BraveSearch | FakeSearch:
    if settings.search_provider == "serpapi":
        return SerpAPISearch(settings.search_api_key)
    if settings.search_provider == "brave":
        return BraveSearch(settings.search_api_key)
    return FakeSearch()
