"""Versioned templates with strict rendering.

Rendering fails loudly if any `{variable}` is unresolved — a half-rendered message is a bug, not
a message. Editing a template creates a new version; the previous one is deactivated, never
mutated (audit + history preserved).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.core.yamlconf import config_dir
from app.db.models import Template
from app.db.models.enums import TemplateStep
from app.services import audit

_VAR = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def extract_variables(*texts: str) -> list[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        found.update(_VAR.findall(text))
    return sorted(found)


def render(text: str, variables: dict[str, Any], *, strict: bool = True) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in variables and variables[key] is not None:
            return str(variables[key])
        missing.append(key)
        return match.group(0)

    rendered = _VAR.sub(replace, text or "")
    if missing and strict:
        raise ValidationFailed(
            "unresolved template variables", details={"missing": sorted(set(missing))}
        )
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()


def template_files(directory: Path | None = None) -> list[Path]:
    base = directory or (config_dir() / "templates")
    if not base.exists():
        return []
    return sorted(p for p in base.glob("*.yaml"))


def load_seed_definitions(directory: Path | None = None) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for path in template_files(directory):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in raw.get("templates", []):
            item = dict(item)
            item.setdefault("channel", "email")
            item.setdefault("locale", "ar")
            item["variables"] = extract_variables(item.get("subject", ""), item.get("body_text", ""))
            item["_source"] = path.name
            definitions.append(item)
    return definitions


async def seed_templates(
    session: AsyncSession, directory: Path | None = None, created_by: str | None = None
) -> dict[str, int]:
    """Idempotently load config/templates/*.yaml as version 1 of each (code, locale)."""
    created = 0
    skipped = 0
    for item in load_seed_definitions(directory):
        existing = await session.execute(
            select(Template).where(
                Template.code == item["code"],
                Template.locale == item["locale"],
                Template.channel == item["channel"],
                Template.is_active.is_(True),
            )
        )
        if existing.scalars().first() is not None:
            skipped += 1
            continue
        template = Template(
            code=str(item["code"]),
            version=1,
            channel=str(item["channel"]),
            locale=str(item["locale"]),
            step=str(item["step"]),
            subject=str(item.get("subject", "")),
            body_text=str(item["body_text"]),
            body_html=item.get("body_html"),
            variables=list(item["variables"]),
            is_active=True,
            created_by=created_by,
        )
        session.add(template)
        created += 1
    await session.flush()
    return {"created": created, "skipped": skipped}


async def get_active(
    session: AsyncSession, *, step: str, locale: str, code: str | None = None, channel: str = "email"
) -> Template | None:
    stmt = select(Template).where(
        Template.step == step,
        Template.locale == locale,
        Template.is_active.is_(True),
        Template.channel == channel,
    )
    if code:
        stmt = stmt.where(Template.code == code)
    result = await session.execute(stmt.order_by(Template.version.desc()))
    return result.scalars().first()


async def get_any_step(session: AsyncSession, *, step: str, locale: str) -> Template | None:
    """Fall back to the other locale when a locale-specific template is missing."""
    template = await get_active(session, step=step, locale=locale)
    if template is not None:
        return template
    other = "en" if locale == "ar" else "ar"
    return await get_active(session, step=step, locale=other)


async def create_version(
    session: AsyncSession,
    *,
    template_id: str | None,
    code: str,
    locale: str,
    step: str,
    subject: str,
    body_text: str,
    channel: str = "email",
    body_html: str | None = None,
    created_by: str | None = None,
) -> Template:
    if step not in tuple(TemplateStep):
        raise ValidationFailed(f"invalid template step: {step}")
    latest = await session.execute(
        select(Template).where(Template.code == code, Template.locale == locale).order_by(Template.version.desc())
    )
    previous = latest.scalars().first()
    next_version = (previous.version + 1) if previous is not None else 1
    if previous is not None:
        previous.is_active = False
    template = Template(
        code=code,
        version=next_version,
        channel=channel,
        locale=locale,
        step=step,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        variables=extract_variables(subject, body_text),
        is_active=True,
        created_by=created_by,
    )
    session.add(template)
    await session.flush()
    await audit.record(
        session,
        action="template_version_created",
        entity_type="template",
        entity_id=template.id,
        actor_id=created_by,
        detail={"code": code, "locale": locale, "version": next_version},
        after={"subject": subject, "body_text": body_text},
    )
    return template


async def list_templates(session: AsyncSession) -> list[Template]:
    result = await session.execute(
        select(Template).order_by(Template.code, Template.locale, Template.version.desc())
    )
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, template_id: str) -> Template:
    template = await session.get(Template, template_id)
    if template is None:
        raise NotFound("template not found")
    return template
