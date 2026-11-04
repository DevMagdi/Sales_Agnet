"""FastAPI application.

Startup refuses to boot with fake providers on a critical path in staging/production, and refuses
`live` until every automatable go-live prerequisite passes (the human items live in
docs/GO_LIVE_CHECKLIST.md).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import IntegrityError

from app.adapters.registry import build_registry, get_registry, set_registry
from app.api.public import pages, unsubscribe
from app.api.router import api_router
from app.core.clock import SystemClock
from app.core.config import get_settings, startup_problems
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.core.metrics import metrics
from app.db.session import create_all, dispose_engine
from app.services import seed

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging("INFO", json_logs=settings.is_production_like)
    problems = startup_problems(settings)
    if problems:
        for problem in problems:
            logger.error("startup_blocked", problem=problem)
        raise RuntimeError("refusing to start: " + "; ".join(problems))
    set_registry(build_registry(settings))
    if settings.database_url.startswith("sqlite"):
        await create_all()
        async with __import__("app.db.session", fromlist=["session_scope"]).session_scope() as session:
            await seed.seed_all(session, settings=settings, clock=SystemClock())
    logger.info("startup_ok", mode=settings.outreach_mode, env=settings.app_env)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mantiq Outreach Agent",
        version="1.0.0",
        description=(
            "Autonomous B2B outbound agent for ProcureAI. Deterministic send gate, absolute "
            "suppression, multilingual opt-out handling, and a human hand-off for anything that "
            "is not simple."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: object) -> object:
        started = time.perf_counter()
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'",
        )
        response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        metrics.incr("http_requests_total", method=request.method, status=str(response.status_code))
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(_request: Request, exc: IntegrityError) -> JSONResponse:
        """A unique/FK violation is a conflict the caller can fix, never a 500."""
        logger.warning("db_integrity_error", error=str(exc.orig)[:300])
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "conflict",
                    "message": "that record already exists (duplicate or still referenced)",
                    "details": {"hint": str(exc.orig)[:300]},
                }
            },
        )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "service": "mantiq-outreach", "mode": settings.outreach_mode}

    @app.get("/readyz", tags=["meta"])
    async def readyz() -> dict[str, object]:
        registry = get_registry()
        return {"status": "ready", "adapters": registry.status_report()}

    @app.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
    async def prometheus_metrics() -> str:
        registry = get_registry()
        for name, status in registry.status_report().items():
            metrics.gauge("adapter_ok", 1 if status["ok"] else 0, adapter=name)
        return metrics.render_prometheus()

    app.include_router(api_router)
    app.include_router(unsubscribe.router)
    app.include_router(pages.router)
    return app


app = create_app()
