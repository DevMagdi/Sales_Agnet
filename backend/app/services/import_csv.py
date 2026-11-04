"""CSV import with column auto-detection, normalisation and three-stage dedupe.

Dedupe order: domain → phone → fuzzy name+city (rapidfuzz). Existing companies are merged,
never overwritten: fields a human edited through the API (`manual_overrides`) are always kept.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import tldextract
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.errors import ValidationFailed
from app.core.text import (
    latin_digits,
    normalise_company_name,
    normalise_email,
    normalise_phone,
    normalise_space,
)
from app.db.models import Company, Contact, ImportBatch, SavedMapping
from app.db.models.enums import CompanyStatus, EmailStatus, SourceKind
from app.services import audit
from app.services import contacts as contact_service

SAMPLE_LIMIT = 5

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "company", "company name", "business name", "الاسم", "اسم الشركة", "اسم المنشأة", "title"),
    "name_ar": ("name_ar", "الاسم بالعربي", "الاسم العربي"),
    "address": ("address", "full address", "location", "العنوان", "عنوان"),
    "phone": ("phone", "phone number", "telephone", "tel", "mobile", "الهاتف", "تليفون", "رقم الهاتف"),
    "website": ("website", "url", "site", "web", "الموقع", "الموقع الإلكتروني", "website url"),
    "email": ("email", "e-mail", "mail", "البريد", "البريد الإلكتروني", "الايميل"),
    "category": ("category", "industry", "type", "sector", "الفئة", "النشاط", "التصنيف"),
    "rating": ("rating", "stars", "التقييم"),
    "city": ("city", "town", "المدينة", "مدينة"),
    "place_link": ("place link", "google maps", "maps url", "link", "رابط الخريطة", "خرائط جوجل"),
    "country": ("country", "الدولة", "البلد"),
}
DIRECTORIES = {
    "wikipedia.org",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "yellowpages.com",
    "yelp.com",
    "amazon.com",
    "google.com",
    "goo.gl",
    "tripadvisor.com",
}


@dataclass
class ImportPreview:
    headers: list[str]
    mapping: dict[str, str | None]
    detected_confidence: dict[str, float]
    total_rows: int
    sample: list[dict[str, Any]]
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    invalid_rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "headers": self.headers,
            "mapping": self.mapping,
            "detected_confidence": self.detected_confidence,
            "total_rows": self.total_rows,
            "sample": self.sample,
            "duplicates": self.duplicates,
            "invalid_rows": self.invalid_rows,
        }


def detect_mapping(headers: list[str]) -> tuple[dict[str, str | None], dict[str, float]]:
    normalised = {h: normalise_text_header(h) for h in headers}
    mapping: dict[str, str | None] = {}
    confidence: dict[str, float] = {}
    for field_name, aliases in FIELD_ALIASES.items():
        best: tuple[float, str] | None = None
        for header, normalised_header in normalised.items():
            for alias in aliases:
                alias_norm = normalise_text_header(alias)
                if normalised_header == alias_norm:
                    score = 1.0
                elif alias_norm in normalised_header or normalised_header in alias_norm:
                    score = 0.8
                else:
                    score = fuzz.ratio(normalised_header, alias_norm) / 100 * 0.6
                if score >= 0.6 and (best is None or score > best[0]):
                    best = (score, header)
        mapping[field_name] = best[1] if best else None
        confidence[field_name] = round(best[0], 2) if best else 0.0
    return mapping, confidence


def normalise_text_header(value: str) -> str:
    return normalise_space(latin_digits(value or "").lower().replace("_", " "))


def domain_from_website(website: str) -> str:
    if not website:
        return ""
    raw = website.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    extracted = tldextract.extract(raw)
    if not extracted.domain or not extracted.suffix:
        return ""
    host = f"{extracted.domain}.{extracted.suffix}".lower()
    if host in DIRECTORIES:
        return ""
    return host


def city_from_address(address: str, fallback: str = "") -> str:
    text = normalise_space(address)
    if not text:
        return fallback
    for part in reversed([p.strip() for p in text.split(",") if p.strip()]):
        if len(part) <= 40 and not any(ch.isdigit() for ch in part):
            return part
    return fallback


@dataclass
class ParsedRow:
    row_number: int
    values: dict[str, str]
    name: str
    domain: str
    phone: str
    email: str
    city: str
    country: str
    category: str
    address: str
    website: str
    place_link: str


def parse_rows(content: str, mapping: dict[str, str | None]) -> tuple[list[ParsedRow], list[dict[str, Any]]]:
    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    rows: list[ParsedRow] = []
    invalid: list[dict[str, Any]] = []
    for index, raw in enumerate(reader, start=2):
        values = {k: (v or "").strip() for k, v in raw.items() if k}
        name = normalise_space(values.get(mapping.get("name") or "", ""))
        if not name:
            invalid.append({"row": index, "reason": "missing_name"})
            continue
        website = values.get(mapping.get("website") or "", "")
        domain = domain_from_website(website)
        address = values.get(mapping.get("address") or "", "")
        rows.append(
            ParsedRow(
                row_number=index,
                values=values,
                name=name,
                domain=domain,
                phone=normalise_phone(values.get(mapping.get("phone") or "", "")),
                email=normalise_email(values.get(mapping.get("email") or "", "")),
                city=normalise_space(values.get(mapping.get("city") or "", "")) or city_from_address(address),
                country=(values.get(mapping.get("country") or "", "") or "EG")[:2].upper(),
                category=values.get(mapping.get("category") or "", ""),
                address=address,
                website=website if website else (f"https://{domain}" if domain else ""),
                place_link=values.get(mapping.get("place_link") or "", ""),
            )
        )
    return rows, invalid


def preview(content: str, mapping: dict[str, str | None] | None = None) -> ImportPreview:
    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    headers = list(reader.fieldnames or [])
    if not headers:
        raise ValidationFailed("CSV has no header row")
    chosen = mapping or detect_mapping(headers)[0]
    confidence = detect_mapping(headers)[1]
    rows, invalid = parse_rows(content, chosen)
    return ImportPreview(
        headers=headers,
        mapping=chosen,
        detected_confidence=confidence,
        total_rows=len(rows) + len(invalid),
        sample=[
            {"name": r.name, "domain": r.domain, "phone": r.phone, "city": r.city, "email": r.email}
            for r in rows[:SAMPLE_LIMIT]
        ],
        invalid_rows=invalid[:20],
    )


async def find_company_by_domain(session: AsyncSession, domain: str) -> Company | None:
    if not domain:
        return None
    result = await session.execute(select(Company).where(Company.domain == domain))
    return result.scalars().first()


async def find_company_by_phone(session: AsyncSession, phone: str) -> Company | None:
    if not phone:
        return None
    from app.core.crypto import blind_index

    digest = blind_index(phone, purpose="phone")
    result = await session.execute(select(Contact).where(Contact.phone_hash == digest))
    contact = result.scalars().first()
    if contact is None:
        return None
    return await session.get(Company, contact.company_id)


async def find_company_fuzzy(session: AsyncSession, name: str, city: str, threshold: int = 92) -> Company | None:
    normalised = normalise_company_name(name)
    if not normalised:
        return None
    stmt = select(Company)
    if city:
        stmt = stmt.where(Company.city == city)
    rows = await session.execute(stmt)
    best: tuple[float, Company] | None = None
    for company in rows.scalars().all():
        score = fuzz.token_set_ratio(normalised, normalise_company_name(company.name))
        if score >= threshold and (best is None or score > best[0]):
            best = (score, company)
    return best[1] if best else None


MERGEABLE_FIELDS = ("name_ar", "website", "city", "industrial_zone", "industry", "source_ref", "address")


async def commit_import(
    session: AsyncSession,
    *,
    content: str,
    mapping: dict[str, str | None] | None,
    filename: str,
    actor: str,
    clock: Clock,
    country_default: str = "EG",
    verify_with: Any | None = None,
) -> dict[str, Any]:
    chosen = mapping or detect_mapping(
        list(csv.DictReader(io.StringIO(content)).fieldnames or [])
    )[0]
    rows, invalid = parse_rows(content, chosen)
    report: dict[str, Any] = {
        "imported": 0,
        "merged": 0,
        "skipped": len(invalid),
        "skip_reasons": {item["reason"]: 1 for item in invalid},
        "contacts_created": 0,
        "duplicate_details": [],
        "total_rows": len(rows) + len(invalid),
    }
    for row in rows:
        existing = await find_company_by_domain(session, row.domain) if row.domain else None
        matched_by = "domain" if existing else ""
        if existing is None and row.phone:
            existing = await find_company_by_phone(session, row.phone)
            matched_by = "phone" if existing else ""
        if existing is None:
            existing = await find_company_fuzzy(session, row.name, row.city)
            matched_by = "fuzzy_name_city" if existing else ""
        if existing is not None:
            overrides = set(existing.manual_overrides or [])
            if "name" not in overrides and len(row.name) > len(existing.name):
                existing.name = row.name
            for field_name in MERGEABLE_FIELDS:
                value = {
                    "name_ar": row.values.get(chosen.get("name_ar") or "", ""),
                    "website": row.website,
                    "city": row.city,
                    "industrial_zone": "",
                    "industry": row.category,
                    "source_ref": row.place_link,
                    "address": row.address,
                }.get(field_name, "")
                if value and field_name not in overrides and not getattr(existing, field_name, None):
                    setattr(existing, field_name, value)
            report["merged"] += 1
            report["duplicate_details"].append(
                {"row": row.row_number, "name": row.name, "matched_company_id": existing.id, "matched_by": matched_by}
            )
            company = existing
        else:
            company = Company(
                name=row.name,
                name_ar=row.values.get(chosen.get("name_ar") or "", "") or None,
                domain=row.domain or None,
                website=row.website or None,
                country=row.country or country_default,
                city=row.city or None,
                industry=None,
                source=SourceKind.CSV,
                source_ref=row.place_link or filename,
                status=CompanyStatus.DISCOVERED,
                language="unknown",
            )
            session.add(company)
            await session.flush()
            report["imported"] += 1
        if row.email:
            contact = await contact_service.create_contact(
                session,
                company_id=company.id,
                email=row.email,
                source_url=f"csv:{filename}",
                email_status=EmailStatus.UNVERIFIED,
                preferred_language="ar",
                confidence=0.7,
            )
            if contact.company_id == company.id:
                report["contacts_created"] += 1
        if row.phone:
            await contact_service.create_contact(
                session,
                company_id=company.id,
                phone=row.phone,
                phone_source_url=f"csv:{filename}",
                confidence=0.6,
            )
    batch = ImportBatch(
        filename=filename,
        mapping=chosen,
        status="committed",
        report=report,
        created_by=None,
    )
    session.add(batch)
    await session.flush()
    await audit.record(
        session,
        action="csv_imported",
        entity_type="import_batch",
        entity_id=batch.id,
        actor_label=actor,
        detail={k: v for k, v in report.items() if k != "duplicate_details"},
    )
    await audit.emit_event(
        session,
        kind="import_committed",
        entity_type="import_batch",
        entity_id=batch.id,
        payload={"imported": report["imported"], "merged": report["merged"]},
    )
    report["batch_id"] = batch.id
    return report


async def save_mapping(session: AsyncSession, name: str, mapping: dict[str, str | None]) -> SavedMapping:
    existing = await session.execute(select(SavedMapping).where(SavedMapping.name == name))
    row = existing.scalars().first()
    if row is None:
        row = SavedMapping(name=name, mapping=mapping)
        session.add(row)
    else:
        row.mapping = mapping
    await session.flush()
    return row


def export_companies_csv(companies: list[Company]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["name", "domain", "website", "country", "city", "industry", "icp_score", "status", "excluded_reason", "created_at"]
    )
    for company in companies:
        writer.writerow(
            [
                company.name,
                company.domain or "",
                company.website or "",
                company.country,
                company.city or "",
                company.industry or "",
                company.icp_score,
                company.status,
                company.excluded_reason or "",
                company.created_at.isoformat() if isinstance(company.created_at, datetime) else "",
            ]
        )
    return buffer.getvalue()
