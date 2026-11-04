"""API v1 router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    campaigns,
    companies,
    discovery,
    events,
    messages,
    replies,
    reports_api,
    settings_api,
    suppression_api,
    system,
    tasks,
    templates_api,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(companies.router)
api_router.include_router(discovery.router)
api_router.include_router(campaigns.router)
api_router.include_router(messages.router)
api_router.include_router(replies.router)
api_router.include_router(tasks.router)
api_router.include_router(suppression_api.router)
api_router.include_router(templates_api.router)
api_router.include_router(system.router)
api_router.include_router(settings_api.router)
api_router.include_router(reports_api.router)
api_router.include_router(events.router)
api_router.include_router(webhooks.router)
