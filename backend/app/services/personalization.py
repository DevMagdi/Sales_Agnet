"""Personalisation: language choice, demo category choice and the single LLM-written opening line.

Only two slots are ever written by the model (`opening_observation`, and indirectly the category
phrase). Everything else in the email is template text from config/templates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import LLMAdapter
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.yamlconf import IndustryPlaybook, load_industries
from app.db.models import Campaign, Company, Contact
from app.services import llm_client
from app.services.schemas import OpeningLine

logger = get_logger("personalisation")

HEDGED_TEMPLATES = {
    "ar": "كثير من شركات {industry} بتشتري {category} عبر عدة موردين في نفس الوقت.",
    "en": "Many companies in {industry} buy {category} from several suppliers at once.",
}


@dataclass
class Personalisation:
    language: str
    category: str
    opening_observation: str
    style: str
    evidence_id: str = ""
    evidence_url: str = ""
    evidence_quote: str = ""
    example_items: str = ""
    playbook_code: str = "unknown"
    llm_call_id: str | None = None
    fallback_used: bool = False
    notes: list[str] = field(default_factory=list)


def choose_language(company: Company, contact: Contact | None, campaign: Campaign) -> str:
    if campaign.language_policy in ("ar", "en"):
        return campaign.language_policy
    if contact is not None and contact.preferred_language in ("ar", "en"):
        return contact.preferred_language
    if company.language == "en":
        return "en"
    # ar / mixed / unknown in Egypt and the Gulf → Arabic
    return "ar" if (company.country or "EG").upper() in ("EG", "SA", "AE", "QA", "KW") else "en"


def choose_category(
    extraction: dict[str, Any], playbook: IndustryPlaybook | None, language: str = "en"
) -> tuple[str, str, bool]:
    """Return (category, evidence_id, grounded)."""
    categories = playbook.purchased_categories if playbook else []
    candidates = extraction.get("likely_purchased_categories") or []
    for item in candidates:
        name = str(item.get("category", "")).strip()
        evidence_id = str(item.get("evidence_id", "")).strip()
        if not name or not evidence_id:
            continue
        if not categories:
            return name, evidence_id, True
        for allowed in categories:
            if allowed.lower() in name.lower() or name.lower() in allowed.lower():
                return allowed, evidence_id, True
    if playbook and playbook.purchased_categories:
        return playbook.purchased_categories[0], "", False
    return ("خامات" if language == "ar" else "raw materials"), "", False


def pick_evidence(
    extraction: dict[str, Any], evidence_id: str, language: str
) -> dict[str, str] | None:
    for item in extraction.get("evidence") or []:
        if item.get("id") == evidence_id and item.get("quote"):
            return {"id": evidence_id, "url": str(item.get("url", "")), "quote": str(item["quote"])}
    del language
    return None


async def personalise(
    session: AsyncSession,
    *,
    company: Company,
    contact: Contact | None,
    campaign: Campaign,
    extraction: dict[str, Any],
    adapter: LLMAdapter,
    clock: Clock,
    settings: Settings,
) -> Personalisation:
    language = choose_language(company, contact, campaign)
    industries = load_industries()
    playbook = industries.by_code(str(extraction.get("industry", "")))
    category, evidence_id, grounded = choose_category(extraction, playbook, language)
    evidence = pick_evidence(extraction, evidence_id, language) if grounded else None
    result = Personalisation(
        language=language,
        category=category,
        opening_observation="",
        style="grounded" if evidence else "hedged",
        evidence_id=evidence["id"] if evidence else "",
        evidence_url=evidence["url"] if evidence else "",
        evidence_quote=evidence["quote"] if evidence else "",
        example_items=", ".join((playbook.example_rfq_items if playbook else [])[:3]),
        playbook_code=playbook.code if playbook else "unknown",
    )
    if not evidence:
        # No citable evidence → hedged, general observation. Never a claim about this company.
        industry_name = (playbook.name_ar if language == "ar" else playbook.name_en) if playbook else (
            "التصنيع" if language == "ar" else "manufacturing"
        )
        result.opening_observation = HEDGED_TEMPLATES[language].format(
            industry=industry_name, category=category
        )
        result.fallback_used = True
        result.notes.append("no_evidence_for_grounded_opening")
        return result

    prompt_body, prompt_version = llm_client.load_prompt("opening_line", "v1")
    context = {
        "language": language,
        "category": category,
        "industry_name": (playbook.name_ar if language == "ar" else playbook.name_en) if playbook else "",
        "evidence_id": evidence["id"],
        "evidence_quote": evidence["quote"],
        "evidence_url": evidence["url"],
        "company": company.name,
        "style": "grounded",
    }
    user_prompt = (
        "Write the opening sentence. Return JSON only.\n"
        "CONTEXT_JSON: " + json.dumps(context, ensure_ascii=False) + "\n"
        "The quote below is the only thing you may say about this company.\n"
        + f"<untrusted_data>\n{evidence['quote']}\n</untrusted_data>"
    )
    try:
        response = await llm_client.call_json(
            session,
            adapter,
            task="compose_slot",
            system=prompt_body,
            user=user_prompt,
            clock=clock,
            settings=settings,
            schema=OpeningLine,
            model_tier="fast",
            max_tokens=300,
            temperature=0.2,
            prompt_version=prompt_version,
        )
        slot = OpeningLine.model_validate(response.data)
        if slot.evidence_id and slot.evidence_id != evidence["id"]:
            raise ValueError("model cited an unknown evidence id")
        result.opening_observation = slot.opening_observation
        result.style = slot.style
        result.llm_call_id = response.call.id
    except Exception as exc:  # noqa: BLE001 - any failure degrades safely to the hedged line
        logger.warning("opening_slot_failed", company_id=company.id, error=str(exc)[:200])
        industry_name = (playbook.name_ar if language == "ar" else playbook.name_en) if playbook else (
            "التصنيع" if language == "ar" else "manufacturing"
        )
        result.opening_observation = HEDGED_TEMPLATES[language].format(
            industry=industry_name, category=category
        )
        result.style = "hedged"
        result.evidence_id = ""
        result.evidence_url = ""
        result.evidence_quote = ""
        result.fallback_used = True
        result.notes.append("opening_slot_fallback")
    return result
