"""Discovery jobs: create, run, inspect, approve into the pipeline."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser
from app.api.schemas import DiscoveryJobCreate
from app.core.errors import NotFound
from app.db.models import DiscoveryJob
from app.services import audit
from app.services import discovery as discovery_service

router = APIRouter(prefix="/discovery", tags=["discovery"])


def job_view(job: DiscoveryJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "query": job.query,
        "country": job.country,
        "city": job.city,
        "industry": job.industry,
        "max_results": job.max_results,
        "budget_cap_usd": job.budget_cap_usd,
        "status": job.status,
        "found": job.found,
        "created_count": job.created_count,
        "duplicates": job.duplicates,
        "cost_usd": job.cost_usd,
        "report": job.report,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@router.post("/jobs")
async def create_job(payload: DiscoveryJobCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    job = DiscoveryJob(
        kind=payload.kind,
        query=payload.query or f"{payload.industry or ''} {payload.city or ''}".strip(),
        country=payload.country,
        city=payload.city,
        industry=payload.industry,
        max_results=payload.max_results,
        budget_cap_usd=payload.budget_cap_usd,
        status="pending",
        created_by=user.id,
    )
    session.add(job)
    await session.flush()
    await audit.record(
        session, action="discovery_job_created", entity_type="discovery_job", entity_id=job.id,
        actor_id=user.id, actor_label=user.email, detail={"kind": job.kind, "city": job.city},
    )
    return job_view(job)


@router.get("/jobs")
async def list_jobs(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    rows = await session.execute(select(DiscoveryJob).order_by(DiscoveryJob.created_at.desc()).limit(50))
    return {"items": [job_view(j) for j in rows.scalars().all()]}


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str,
    session: DbDep,
    user: WriterUser,
    registry: RegistryDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    job = await session.get(DiscoveryJob, job_id)
    if job is None:
        raise NotFound("job not found")
    outcome = await discovery_service.run_job(
        session,
        job=job,
        search=registry.search,
        places=registry.places,
        clock=clock,
        settings=settings,
        industry_code=job.industry,
    )
    await audit.record(
        session, action="discovery_job_run", entity_type="discovery_job", entity_id=job.id,
        actor_id=user.id, actor_label=user.email, detail=outcome.as_dict(),
    )
    return outcome.as_dict()


@router.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str, session: DbDep, user: WriterUser, clock: ClockDep) -> dict[str, Any]:
    result = await discovery_service.approve_to_pipeline(session, job_id, clock)
    if "error" in result:
        raise NotFound(result["error"])
    await audit.record(
        session, action="discovery_approved", entity_type="discovery_job", entity_id=job_id,
        actor_id=user.id, actor_label=user.email, detail=result,
    )
    return result
