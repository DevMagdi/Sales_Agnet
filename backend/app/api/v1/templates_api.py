"""Versioned template management and check-only preview."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AnyUser, DbDep, WriterUser
from app.api.schemas import TemplateCreate
from app.core.errors import ValidationFailed
from app.db.models import Company, Contact, Template
from app.services import audit
from app.services import templates as template_service

router = APIRouter(prefix="/templates", tags=["templates"])


def template_view(template: Template) -> dict[str, Any]:
    return {
        "id": template.id,
        "code": template.code,
        "version": template.version,
        "channel": template.channel,
        "locale": template.locale,
        "step": template.step,
        "subject": template.subject,
        "body_text": template.body_text,
        "variables": template.variables,
        "is_active": template.is_active,
        "created_at": template.created_at.isoformat() if template.created_at else None,
    }


@router.get("")
async def list_templates(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    rows = await template_service.list_templates(session)
    return {"items": [template_view(t) for t in rows]}


@router.post("")
async def create_version(payload: TemplateCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    if payload.step not in (
        "initial",
        "followup_1",
        "followup_2",
        "breakup",
        "reply_info",
        "reply_booking",
        "reply_unsubscribe",
        "reply_referral_thanks",
        "demo_followup",
    ):
        raise ValidationFailed("invalid template step")
    if "{unsubscribe_url}" not in payload.body_text and payload.channel == "email" and payload.step.startswith(("initial", "followup")):
        raise ValidationFailed("outreach templates must include {unsubscribe_url}")
    template = await template_service.create_version(
        session,
        template_id=None,
        code=payload.code,
        locale=payload.locale,
        step=payload.step,
        subject=payload.subject,
        body_text=payload.body_text,
        channel=payload.channel,
        created_by=user.id,
    )
    return template_view(template)


@router.get("/{template_id}")
async def get_template(template_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return template_view(await template_service.get_by_id(session, template_id))


@router.post("/{template_id}/preview")
async def preview_template(
    template_id: str, session: DbDep, user: AnyUser, sample: dict[str, Any] | None = None
) -> dict[str, Any]:
    del user
    template = await template_service.get_by_id(session, template_id)
    values = {var: f"[{var}]" for var in template.variables}
    values.update(sample or {})
    try:
        subject = template_service.render(template.subject, values)
        body = template_service.render(template.body_text, values)
    except ValidationFailed as exc:
        return {"ok": False, "error": exc.message, "missing": exc.details.get("missing", [])}
    return {"ok": True, "subject": subject, "body_text": body}


@router.post("/{template_id}/activate")
async def activate(template_id: str, session: DbDep, user: WriterUser) -> dict[str, Any]:
    template = await template_service.get_by_id(session, template_id)
    siblings = await session.execute(
        select(Template).where(
            Template.code == template.code,
            Template.locale == template.locale,
            Template.id != template.id,
        )
    )
    for sibling in siblings.scalars().all():
        sibling.is_active = False
    template.is_active = True
    await session.flush()
    await audit.record(
        session, action="template_activated", entity_type="template", entity_id=template.id,
        actor_id=user.id, actor_label=user.email,
    )
    return template_view(template)


@router.get("/playbooks/all")
async def list_playbooks(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user, session
    from app.core.yamlconf import load_industries

    industries = load_industries()
    return {
        "items": [
            {
                "code": p.code,
                "name_en": p.name_en,
                "name_ar": p.name_ar,
                "purchased_categories": p.purchased_categories,
                "pain_hypotheses": p.pain_hypotheses,
                "demo_scenario": p.demo_scenario,
                "example_rfq_items": p.example_rfq_items,
                "typical_zones": p.typical_zones,
                "regulated": p.regulated,
            }
            for p in industries.industries
        ]
    }


@router.get("/claims/fact-sheet")
async def fact_sheet(user: AnyUser) -> dict[str, Any]:
    del user
    from app.core.yamlconf import load_claims

    claims = load_claims()
    return {
        "product_name": claims.product_name,
        "company": claims.company,
        "facts": [{"id": f.id, "ar": f.ar, "en": f.en} for f in claims.facts],
        "status_note": claims.status_note,
        "forbidden": claims.forbidden,
    }


def company_lookup_stub(company: Company, contact: Contact) -> dict[str, str]:
    return {"company_id": company.id, "contact_id": contact.id}
