"""Human hand-off: briefs, tasks, demo requests and staff notifications.

Nothing in this module talks to prospects except the reply templates that the handler sends
through the normal gated path. Alerts go to internal staff channels only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.yamlconf import load_industries
from app.db.models import (
    CampaignProspect,
    Company,
    Contact,
    DemoRequest,
    InboundMessage,
    OutreachMessage,
    Reply,
    ResearchSnapshot,
    Task,
)
from app.db.models.enums import DemoStatus, TaskStatus, TaskType
from app.services import audit

logger = get_logger("handoff")


@dataclass
class Brief:
    title: str
    markdown: str
    demo_scenario: str
    example_items: list[str]
    evidence: list[dict[str, Any]]


async def build_brief(
    session: AsyncSession,
    *,
    company: Company,
    contact: Contact | None,
    inbound_text: str,
    reply_class: str,
    thread: list[dict[str, Any]] | None = None,
) -> Brief:
    snapshot = (
        await session.execute(
            select(ResearchSnapshot)
            .where(ResearchSnapshot.company_id == company.id)
            .order_by(ResearchSnapshot.version.desc())
            .limit(1)
        )
    ).scalars().first()
    extraction: dict[str, Any] = dict(snapshot.extracted) if snapshot else {}
    evidence = list(snapshot.evidence) if snapshot else []
    industries = load_industries()
    playbook = industries.by_code(company.industry or "") if company.industry else None
    demo_scenario = playbook.demo_scenario if playbook else "Run their most recent RFQ end to end."
    example_items = playbook.example_rfq_items[:5] if playbook else []

    lines = [
        f"# Hand-off — {company.name}",
        "",
        f"**Reply class:** {reply_class}",
        f"**ICP score:** {company.icp_score}/100",
        f"**Industry:** {company.industry or 'unknown'} · **Country:** {company.country}"
        + (f" · **City:** {company.city}" if company.city else ""),
        f"**Website:** {company.website or '—'}",
        "",
        "## Company summary (from research)",
        str(extraction.get("company_summary", "unknown")),
        "",
        "## Why they might buy",
    ]
    for item in (extraction.get("likely_purchased_categories") or [])[:4]:
        lines.append(
            f"- {item.get('category')} — {item.get('rationale', '')} (confidence {item.get('confidence', 0)})"
        )
    if not extraction.get("likely_purchased_categories"):
        lines.append("- No evidence-backed category yet; use the playbook scenario.")
    lines += ["", "## Contact"]
    if contact is not None:
        lines.append(
            f"- {contact.full_name or 'Unknown name'} — {contact.role_title or contact.role_category} "
            f"({contact.email_status}, source: {contact.email_source_url or 'imported'})"
        )
    else:
        lines.append("- No named contact")
    lines += ["", "## Evidence (quotes the prospect can be shown)"]
    for item in evidence[:5]:
        lines.append(f"- \"{item.get('quote', '')}\" — {item.get('url', '')}")
    if not evidence:
        lines.append("- No evidence stored (hedged messaging only)")
    lines += ["", "## Suggested demo", f"- {demo_scenario}"]
    if example_items:
        lines.append(f"- Sample RFQ items: {', '.join(example_items)}")
    lines += ["", "## Their reply", "```", inbound_text[:1500], "```"]
    if thread:
        lines += ["", "## Thread (last messages)"]
        for item in thread[-4:]:
            lines.append(f"- **{item['direction']}** ({item['at']}): {str(item['text'])[:280]}")
    lines += [
        "",
        "## Next step",
        "Reply from your own mailbox after reviewing. The agent will not send anything else on this thread.",
    ]
    return Brief(
        title=f"Hand-off: {company.name} ({reply_class})",
        markdown="\n".join(lines),
        demo_scenario=demo_scenario,
        example_items=example_items,
        evidence=evidence,
    )


async def create_task(
    session: AsyncSession,
    *,
    task_type: str,
    title: str,
    brief: str = "",
    company_id: str | None = None,
    contact_id: str | None = None,
    reply_id: str | None = None,
    draft: str = "",
    assigned_to: str | None = None,
    due_in_hours: int | None = None,
    clock: Clock,
    payload: dict[str, Any] | None = None,
) -> Task:
    task = Task(
        type=task_type,
        company_id=company_id,
        contact_id=contact_id,
        reply_id=reply_id,
        assigned_to=assigned_to,
        title=title[:300],
        brief=brief,
        draft=draft,
        due_at=clock.now() + timedelta(hours=due_in_hours) if due_in_hours else None,
        status=TaskStatus.OPEN,
        payload=payload or {},
    )
    session.add(task)
    await session.flush()
    await audit.emit_event(
        session,
        kind="task_created",
        entity_type="task",
        entity_id=task.id,
        payload={"type": task_type, "title": title[:120]},
    )
    return task


async def notify_staff(
    registry: AdapterRegistry, *, title: str, body: str, payload: dict[str, Any] | None = None
) -> tuple[bool, str]:
    return await registry.notify.send_alert(title=title, body=body, payload=payload or {})


async def handoff(
    session: AsyncSession,
    *,
    company: Company,
    contact: Contact | None,
    prospect: CampaignProspect | None,
    reply: Reply,
    inbound: InboundMessage,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
) -> dict[str, Any]:
    thread: list[dict[str, Any]] = []
    if prospect is not None:
        from app.services.inbound import recent_thread

        thread = await recent_thread(session, prospect.id)
    brief = await build_brief(
        session,
        company=company,
        contact=contact,
        inbound_text=inbound.cleaned_text or inbound.body_text,
        reply_class=reply.classification,
        thread=thread,
    )
    task = await create_task(
        session,
        task_type=TaskType.BOOK_DEMO,
        title=brief.title,
        brief=brief.markdown,
        company_id=company.id,
        contact_id=contact.id if contact else None,
        reply_id=reply.id,
        due_in_hours=4,
        clock=clock,
        payload={"demo_scenario": brief.demo_scenario, "example_items": brief.example_items},
    )
    demo = DemoRequest(
        company_id=company.id,
        contact_id=contact.id if contact else None,
        source_reply_id=reply.id,
        status=DemoStatus.REQUESTED,
        proposed_slots=[],
        notes="",
    )
    session.add(demo)
    await session.flush()
    ok, detail = await notify_staff(
        registry,
        title=f"Interested prospect: {company.name}",
        body=brief.markdown[:3000],
        payload={
            "company_id": company.id,
            "task_id": task.id,
            "demo_id": demo.id,
            "booking_url": settings.booking_url,
        },
    )
    reply.status = "needs_human"
    await audit.record(
        session,
        action="handoff_created",
        entity_type="task",
        entity_id=task.id,
        detail={"company_id": company.id, "notified": ok, "demo_id": demo.id},
    )
    return {"task_id": task.id, "demo_id": demo.id, "notified": ok, "notify_detail": detail}


def task_public_view(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "type": task.type,
        "title": task.title,
        "brief": task.brief,
        "draft": task.draft,
        "status": task.status,
        "company_id": task.company_id,
        "contact_id": task.contact_id,
        "reply_id": task.reply_id,
        "assigned_to": task.assigned_to,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


async def outbound_context(session: AsyncSession, prospect_id: str) -> dict[str, Any]:
    """Everything the reply path needs about a prospect in one call."""
    prospect = await session.get(CampaignProspect, prospect_id)
    if prospect is None:
        return {}
    company = await session.get(Company, prospect.company_id)
    contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
    last = (
        await session.execute(
            select(OutreachMessage)
            .where(OutreachMessage.campaign_prospect_id == prospect.id)
            .order_by(OutreachMessage.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return {"prospect": prospect, "company": company, "contact": contact, "last_message": last}
