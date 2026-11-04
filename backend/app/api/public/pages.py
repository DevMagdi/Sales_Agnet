"""Public static pages: bilingual feature page (rendered from claims.yaml), crawler info, click redirect."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.api.deps import ClockDep, DbDep, SettingsDep
from app.core.crypto import verify_token
from app.core.yamlconf import load_claims, load_industries
from app.db.models import MessageEvent, OutreachMessage
from app.db.models.enums import MessageEventType

router = APIRouter(tags=["public"])

FEATURES_PAGE = """<!doctype html>
<html lang="{lang}" dir="{direction}">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
 :root{{--navy:#0b1b33;--gold:#b8892b;--ink:#111827;--muted:#5b6779;--bg:#f7f8fb}}
 *{{box-sizing:border-box}} body{{margin:0;font-family:system-ui,Arial,sans-serif;color:var(--ink);background:var(--bg)}}
 header{{background:var(--navy);color:#fff;padding:40px 24px}}
 .wrap{{max-width:900px;margin:0 auto}}
 h1{{margin:0 0 8px;font-size:30px}} .gold{{color:var(--gold)}}
 .langs{{display:flex;gap:8px;justify-content:flex-end;padding:12px 24px;max-width:900px;margin:0 auto}}
 .langs a{{color:var(--navy);text-decoration:none;border:1px solid #ccd3df;border-radius:6px;padding:6px 12px;font-size:14px}}
 .langs a.active{{background:var(--navy);color:#fff;border-color:var(--navy)}}
 main{{padding:28px 24px 60px}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}
 .card{{background:#fff;border:1px solid #e6e9f0;border-radius:12px;padding:18px 20px;box-shadow:0 2px 10px rgba(11,27,51,.05)}}
 .card h2{{font-size:17px;margin:0 0 8px;color:var(--navy)}} .card p{{margin:0;line-height:1.7;color:#26303f}}
 .status{{margin-top:28px;background:#fff7e6;border:1px solid #f0d9a8;border-radius:10px;padding:14px 16px;font-size:14px}}
 footer{{padding:24px;color:var(--muted);font-size:13px;text-align:center}}
 .cta{{display:inline-block;margin-top:22px;background:var(--gold);color:#fff;padding:12px 20px;border-radius:8px;
 text-decoration:none;font-weight:600}}
</style></head>
<body>
<header><div class="wrap"><h1>{heading}</h1><p class="gold">{subtitle}</p></div></header>
<div class="langs"><a href="/features?lang=ar" class="{ar_active}">العربية</a><a href="/features?lang=en" class="{en_active}">English</a></div>
<main class="wrap">
 <div class="grid">{cards}</div>
 <div class="status">{status_note}</div>
 {cta}
</main>
<footer>{footer}</footer>
</body></html>"""


@router.get("/features", response_class=HTMLResponse)
async def features(lang: str = "ar", settings: SettingsDep = None) -> HTMLResponse:  # type: ignore[assignment]
    claims = load_claims()
    chosen = "en" if lang == "en" else "ar"
    cards = "".join(
        f'<div class="card"><h2>{claims.product_name} · {fact.id}</h2><p>{fact.ar if chosen == "ar" else fact.en}</p></div>'
        for fact in claims.facts
    )
    heading = "ProcureAI — منصة المشتريات الذكية" if chosen == "ar" else "ProcureAI — procurement that runs on data"
    subtitle = (
        "من طلب الشراء إلى أمر الشراء، بقرار بشري في كل خطوة."
        if chosen == "ar"
        else "From purchase need to purchase order, with a human decision at every step."
    )
    status_note = (
        "نسخة تجريبية (Pilot): لا نقدّم مكالمات صوتية آلية، ولا نعرض شعارات عملاء، ولا أرقام توفير."
        if chosen == "ar"
        else "Pilot build: no automated voice calls, no customer logos, no savings figures."
    )
    cta = ""
    booking = getattr(settings, "booking_url", "") if settings else ""
    if booking:
        cta = (
            f'<a class="cta" href="{booking}">{"احجز Demo" if chosen == "ar" else "Book a demo"}</a>'
        )
    body = FEATURES_PAGE.format(
        lang=chosen,
        direction="rtl" if chosen == "ar" else "ltr",
        title=f"{claims.product_name} — {claims.company}",
        heading=heading,
        subtitle=subtitle,
        cards=cards,
        status_note=status_note,
        cta=cta,
        footer=f"{claims.company} — {claims.status_note}",
        ar_active="active" if chosen == "ar" else "",
        en_active="active" if chosen == "en" else "",
    )
    return HTMLResponse(body)


@router.get("/bot", response_class=HTMLResponse)
async def bot_page(settings: SettingsDep) -> HTMLResponse:
    industries = load_industries()
    names = ", ".join(f"{p.name_en} / {p.name_ar}" for p in industries.industries[:11])
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<title>MantiqOutreachBot</title><style>body{{font-family:system-ui,Arial;max-width:760px;margin:40px auto;
padding:0 20px;line-height:1.7;color:#111}}</style></head><body>
<h1>MantiqOutreachBot/1.0</h1>
<p>This is the automated crawler used by {settings.company_name or "Mantiq Technologies"} to read
public company websites (home, about, products, contact, suppliers) for B2B outreach research.</p>
<ul>
<li>It identifies itself with the User-Agent <code>{settings.crawler_user_agent} (+{settings.bot_url})</code>.</li>
<li>It obeys <code>robots.txt</code> including <code>crawl-delay</code>, fetches at most
{settings.crawl_max_pages} pages per site, waits {settings.crawl_delay_seconds}s between requests,
and never fetches more than {settings.crawl_max_page_bytes // 1_000_000}MB per page.</li>
<li>It only stores public business information. To have your site excluded,
email us or write to the postal address on our main site and we will add your domain to the
do-not-contact list permanently.</li>
<li>It never submits forms, never logs in, and never automates LinkedIn.</li>
</ul>
<p>Industries of interest: {names}.</p>
</body></html>"""
    return HTMLResponse(body)


@router.get("/click/{token}")
async def click(
    token: str, request: Request, session: DbDep, clock: ClockDep, settings: SettingsDep
) -> RedirectResponse:
    target = settings.booking_url or settings.features_url or settings.public_base_url
    try:
        payload = verify_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid click token") from exc
    message_id = str(payload.get("m", ""))
    if message_id and settings.click_tracking_enabled:
        message = await session.get(OutreachMessage, message_id)
        if message is not None:
            session.add(
                MessageEvent(
                    message_id=message.id,
                    type=MessageEventType.CLICK,
                    payload={"ua": request.headers.get("user-agent", "")[:200]},
                    occurred_at=clock.now(),
                )
            )
            await session.flush()
    return RedirectResponse(target, status_code=302)


async def public_stats(session: DbDep) -> dict[str, int]:
    rows = await session.execute(select(OutreachMessage.id).limit(1))
    return {"ok": len(list(rows.scalars().all()))}
