"""Google Places API (New) text search adapter.

Only place_id and business facts are stored; refresh on demand per the API terms.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import PlaceResult


class GooglePlaces:
    name = "google_places"
    is_fake = False
    cost_per_call_usd = 0.032

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://places.googleapis.com/v1/places:searchText",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._client = client

    def healthcheck(self) -> tuple[bool, str]:
        return (bool(self._api_key), "PLACES_API_KEY missing" if not self._api_key else "places configured")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def text_search(self, query: str, max_results: int = 20) -> list[PlaceResult]:
        response = await self._http().post(
            self._endpoint,
            json={"textQuery": query, "languageCode": "ar", "pageSize": min(max_results, 20)},
            headers={
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": (
                    "places.id,places.displayName,places.formattedAddress,places.websiteUri,"
                    "places.nationalPhoneNumber,places.primaryTypeDisplayName,places.location"
                ),
                "content-type": "application/json",
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        out: list[PlaceResult] = []
        for place in (data.get("places") or [])[:max_results]:
            location = place.get("location") or {}
            out.append(
                PlaceResult(
                    name=str((place.get("displayName") or {}).get("text", "")),
                    address=str(place.get("formattedAddress", "")),
                    website=str(place.get("websiteUri", "")),
                    phone=str(place.get("nationalPhoneNumber", "")),
                    category=str((place.get("primaryTypeDisplayName") or {}).get("text", "")),
                    place_id=str(place.get("id", "")),
                    lat=location.get("latitude"),
                    lng=location.get("longitude"),
                )
            )
        return out
