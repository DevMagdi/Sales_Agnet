"""Tasks and demo requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AnyUser, ClockDep, DbDep, WriterUser
from app.api.schemas import DemoUpdate, TaskUpdate
from app.core.errors import NotFound
from app.db.models import Company, DemoRequest, Task
from app.services import audit

router = APIRouter(tags=["tasks"])


def task_view(task: Task, company_name: str = "") -> dict[str, Any]:
    return {
        "id": task.id,
        "type": task.type,
        "title": task.title,
        "brief": task.brief,
        "draft": task.draft,
        "status": task.status,
        "company_id": task.company_id,
        "company_name": company_name,
        "contact_id": task.contact_id,
        "reply_id": task.reply_id,
        "assigned_to": task.assigned_to,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "payload": task.payload,
    }


@router.get("/tasks")
async def list_tasks(session: DbDep, user: AnyUser, status: str | None = "open", type: str | None = None) -> dict[str, Any]:
    del user
    stmt = select(Task).order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Task.status == status)
    if type:
        stmt = stmt.where(Task.type == type)
    rows = await session.execute(stmt)
    items = []
    for task in rows.scalars().all():
        company = await session.get(Company, task.company_id) if task.company_id else None
        items.append(task_view(task, company.name if company else ""))
    return {"items": items}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("task not found")
    company = await session.get(Company, task.company_id) if task.company_id else None
    return task_view(task, company.name if company else "")


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskUpdate, session: DbDep, user: WriterUser, clock: ClockDep) -> dict[str, Any]:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("task not found")
    task.status = payload.status
    if payload.assigned_to is not None:
        task.assigned_to = payload.assigned_to
    if payload.status == "done":
        task.completed_at = clock.now()
    await session.flush()
    await audit.record(
        session, action="task_updated", entity_type="task", entity_id=task.id, actor_id=user.id,
        actor_label=user.email, detail={"status": payload.status, "note": payload.note},
    )
    return task_view(task)


@router.get("/demos")
async def list_demos(session: DbDep, user: AnyUser, status: str | None = None) -> dict[str, Any]:
    del user
    stmt = select(DemoRequest).order_by(DemoRequest.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(DemoRequest.status == status)
    rows = await session.execute(stmt)
    items = []
    for demo in rows.scalars().all():
        company = await session.get(Company, demo.company_id)
        items.append(
            {
                "id": demo.id,
                "company_id": demo.company_id,
                "company_name": company.name if company else "",
                "contact_id": demo.contact_id,
                "status": demo.status,
                "proposed_slots": demo.proposed_slots,
                "booked_at": demo.booked_at.isoformat() if demo.booked_at else None,
                "assigned_to": demo.assigned_to,
                "notes": demo.notes,
                "created_at": demo.created_at.isoformat() if demo.created_at else None,
            }
        )
    return {"items": items}


@router.patch("/demos/{demo_id}")
async def update_demo(demo_id: str, payload: DemoUpdate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    demo = await session.get(DemoRequest, demo_id)
    if demo is None:
        raise NotFound("demo not found")
    demo.status = payload.status
    demo.notes = payload.notes or demo.notes
    if payload.booked_at:
        demo.booked_at = datetime.fromisoformat(payload.booked_at)
    await session.flush()
    await audit.record(
        session, action="demo_updated", entity_type="demo_request", entity_id=demo.id, actor_id=user.id,
        actor_label=user.email, detail={"status": payload.status},
    )
    return {"id": demo.id, "status": demo.status}
