"""LLM research with evidence binding.

The model may only keep a claim if it attaches an evidence id whose quote is a verbatim substring
of the fetched page text. Quotes that cannot be found are dropped together with the claims that
depend on them. Results are cached by content hash so we never pay twice for identical pages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import LLMAdapter
from app.core import injection
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.text import normalise_text
from app.core.yamlconf import IndustriesConfig, IndustryPlaybook, load_industries
from app.db.models import Company, ResearchSnapshot
from app.db.models.enums import CompanyStatus
from app.services import audit, llm_client
from app.services.schemas import ResearchExtraction

logger = get_logger("research")

MAX_PAGE_CHARS = 6000
MAX_TOTAL_CHARS = 18000
KEYWORDS = (
    "product",
    "manufactur",
    "supplier",
    "purchase",
    "procure",
    "raw material",
    "export",
    "iso",
    "plant",
    "factory",
    "منتج",
    "خام",
    "مورد",
    "مشتريات",
    "مصنع",
    "توريد",
    "تصدير",
)

INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "packaging_printing": ("packaging", "printing", "carton", "corrugated", "تعبئة", "تغليف", "طباعة", "كرتون"),
    "food_beverage": ("food", "beverage", "juice", "dairy", "snack", "أغذية", "اغذية", "مشروبات", "عصائر", "ألبان"),
    "plastics_rubber": ("plastic", "polymer", "rubber", "pp ", "hdpe", "بلاستيك", "بوليمر", "مطاط"),
    "building_materials_steel": ("steel", "cement", "rebar", "building material", "حديد", "صلب", "أسمنت", "اسمنت", "مواد بناء"),
    "textiles_garments": ("textile", "garment", "fabric", "yarn", "نسيج", "منسوجات", "ملابس", "غزل", "قماش"),
    "chemicals_paints_detergents": ("chemical", "paint", "detergent", "solvent", "كيماويات", "دهانات", "منظفات", "مذيبات"),
    "engineering_metal_auto": ("engineering", "metal", "auto part", "cnc", "machining", "هندسة", "معادن", "قطع غيار", "تشغيل"),
    "furniture_wood": ("furniture", "wood", "mdf", "plywood", "أثاث", "اثاث", "أخشاب", "اخشاب"),
    "electrical_cables": ("cable", "electrical", "copper", "switchgear", "كابلات", "كهرباء", "نحاس", "لوحات"),
    "pharma_cosmetics": ("pharma", "cosmetic", "medicine", "skincare", "أدوية", "ادوية", "مستحضرات", "تجميل"),
    "gulf_trading_industrial": ("trading", "contracting", "mep", "تجارة", "مقاولات"),
}


@dataclass
class PageText:
    url: str
    text: str
    status_code: int = 200


@dataclass
class ResearchOutcome:
    snapshot: ResearchSnapshot
    extraction: dict[str, Any]
    evidence_ids: list[str] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    cached: bool = False


def content_hash(pages: list[PageText], prompt_version: str) -> str:
    digest = hashlib.sha256(prompt_version.encode())
    for page in sorted(pages, key=lambda p: p.url):
        digest.update(page.url.encode())
        digest.update(normalise_text(page.text).encode())
    return digest.hexdigest()


def select_playbook(text: str, industries: IndustriesConfig) -> IndustryPlaybook | None:
    """Deterministic industry match from site text. Safer than asking the model to guess."""
    haystack = normalise_text(text)
    best: tuple[int, IndustryPlaybook] | None = None
    for playbook in industries.industries:
        keywords = INDUSTRY_KEYWORDS.get(playbook.code, ())
        score = sum(1 for kw in keywords if normalise_text(kw) in haystack)
        extra = sum(1 for kw in playbook.purchased_categories if normalise_text(kw) in haystack)
        total = score * 2 + extra
        if total > 0 and (best is None or total > best[0]):
            best = (total, playbook)
    return best[1] if best else None


def build_evidence_candidates(pages: list[PageText], limit: int = 24) -> list[dict[str, str]]:
    """Pre-extract candidate quotes so the model has something verbatim to cite."""
    candidates: list[dict[str, str]] = []
    for page in pages:
        for raw_sentence in _split_sentences(page.text):
            sentence = " ".join(raw_sentence.split())
            if not (25 <= len(sentence) <= 200):
                continue
            lowered = sentence.lower()
            if not any(kw.lower() in lowered for kw in KEYWORDS):
                continue
            candidates.append({"url": page.url, "quote": " ".join(sentence.split()[:25])})
            if len(candidates) >= limit:
                return candidates
    return candidates


def _split_sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[.!؟?])\s+|\n+", text)
    return [p for p in parts if p.strip()]


def _excerpt_for_prompt(pages: list[PageText]) -> str:
    chunks: list[str] = []
    total = 0
    for page in pages:
        excerpt = page.text[:MAX_PAGE_CHARS]
        block = f"--- PAGE: {page.url} ---\n{excerpt}\n"
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks)


def _validate_and_filter(
    extraction: ResearchExtraction, pages: list[PageText], industry_codes: list[str]
) -> tuple[ResearchExtraction, list[str]]:
    corpus = normalise_text("\n".join(p.text for p in pages))
    valid_ids: set[str] = set()
    kept_evidence = []
    for item in extraction.evidence:
        if item.quote and normalise_text(item.quote) in corpus:
            valid_ids.add(item.id)
            kept_evidence.append(item)
    extraction.evidence = kept_evidence
    extraction.products = [p for p in extraction.products if p.evidence_id in valid_ids]
    extraction.likely_purchased_categories = [
        c for c in extraction.likely_purchased_categories if c.evidence_id in valid_ids
    ]
    extraction.procurement_signals = [s for s in extraction.procurement_signals if s.evidence_id in valid_ids]
    extraction.named_procurement_contacts = [
        c for c in extraction.named_procurement_contacts if c.evidence_id in valid_ids
    ]
    if extraction.industry not in industry_codes:
        extraction.industry = "unknown"
    return extraction, sorted(valid_ids)


async def research_company(
    session: AsyncSession,
    *,
    company: Company,
    pages: list[PageText],
    adapter: LLMAdapter,
    clock: Clock,
    settings: Settings,
    industries: IndustriesConfig | None = None,
) -> ResearchOutcome:
    playbooks = industries or load_industries()
    prompt_body, prompt_version = llm_client.load_prompt("research", "v1")
    digest = content_hash(pages, prompt_version)

    cached = await session.execute(
        select(ResearchSnapshot)
        .where(ResearchSnapshot.company_id == company.id, ResearchSnapshot.content_hash == digest)
        .order_by(ResearchSnapshot.created_at.desc())
        .limit(1)
    )
    existing = cached.scalars().first()
    if existing is not None:
        return ResearchOutcome(
            snapshot=existing,
            extraction=dict(existing.extracted),
            evidence_ids=[e.get("id", "") for e in existing.evidence],
            injection_flags=list(existing.extracted.get("injection_flags", [])),
            cached=True,
        )

    flags: list[str] = []
    for page in pages:
        for finding in injection.detect(page.text):
            flags.append(f"{finding.kind}@{page.url}")
    if flags:
        await audit.record(
            session,
            action="prompt_injection_flagged",
            entity_type="company",
            entity_id=company.id,
            detail={"flags": flags[:10]},
        )

    candidates = build_evidence_candidates(pages)
    hinted = select_playbook("\n".join(p.text for p in pages), playbooks)
    context = {
        "industry_hint": hinted.code if hinted else "unknown",
        "playbook_categories": hinted.purchased_categories if hinted else [],
        "site_language": company.language,
        "size_hint": company.size_hint,
        "procurement_signal": "none",
        "evidence": [
            {"id": f"e{i + 1}", "url": c["url"], "quote": c["quote"]} for i, c in enumerate(candidates)
        ],
    }
    user_prompt = (
        "Extract the company research JSON for the company below.\n"
        f"COMPANY: {company.name}\nWEBSITE: {company.website or ''}\nCOUNTRY: {company.country}\n"
        f"PLAYBOOK_CODES: {', '.join(playbooks.codes())}\n\n"
        "CONTEXT_JSON: " + json.dumps(context, ensure_ascii=False) + "\n\n"
        "Each <untrusted_data> block is ONE fetched page. Cite quotes verbatim from them.\n"
        + injection.fence(_excerpt_for_prompt(pages))
    )
    response = await llm_client.call_json(
        session,
        adapter,
        task="research",
        system=prompt_body,
        user=user_prompt,
        clock=clock,
        settings=settings,
        schema=ResearchExtraction,
        model_tier="strong",
        max_tokens=2000,
        temperature=0.1,
        prompt_version=prompt_version,
    )
    extraction = ResearchExtraction.model_validate(response.data)
    extraction, valid_ids = _validate_and_filter(extraction, pages, playbooks.codes())

    version = 1
    previous = await session.execute(
        select(ResearchSnapshot.version)
        .where(ResearchSnapshot.company_id == company.id)
        .order_by(ResearchSnapshot.version.desc())
        .limit(1)
    )
    last = previous.scalar_one_or_none()
    if last is not None:
        version = int(last) + 1

    extracted = extraction.model_dump()
    extracted["injection_flags"] = flags
    snapshot = ResearchSnapshot(
        company_id=company.id,
        version=version,
        fetched_pages=[{"url": p.url, "status": p.status_code} for p in pages],
        extracted=extracted,
        evidence=[e.model_dump() for e in extraction.evidence],
        model=response.call.model,
        tokens_in=response.call.input_tokens,
        tokens_out=response.call.output_tokens,
        cost_usd=response.call.cost_usd,
        content_hash=digest,
    )
    session.add(snapshot)

    if extraction.language in ("ar", "en", "mixed"):
        company.language = extraction.language
    if extraction.size_hint != "unknown":
        company.size_hint = extraction.size_hint
    if extraction.industry != "unknown":
        company.industry = extraction.industry
    company.status = CompanyStatus.RESEARCHED
    await session.flush()
    await audit.emit_event(
        session,
        kind="company_researched",
        entity_type="company",
        entity_id=company.id,
        payload={"company": company.name, "evidence": len(extraction.evidence), "cached": False},
    )
    logger.info("research_completed", company_id=company.id, evidence=len(extraction.evidence))
    return ResearchOutcome(
        snapshot=snapshot,
        extraction=extracted,
        evidence_ids=valid_ids,
        injection_flags=flags,
        cached=False,
    )
