"""Places adapter factory."""

from __future__ import annotations

from app.adapters.places.fake import FakePlaces
from app.adapters.places.google import GooglePlaces
from app.core.config import Settings

__all__ = ["FakePlaces", "GooglePlaces", "build_places"]


def build_places(settings: Settings) -> GooglePlaces | FakePlaces:
    if settings.places_api_key:
        return GooglePlaces(settings.places_api_key)
    return FakePlaces()
