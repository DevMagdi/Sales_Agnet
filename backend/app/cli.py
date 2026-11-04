"""Command line interface: `python -m app.cli <command>`.

Everything an operator needs without the dashboard: bootstrap, doctor, kill switch, sequence
planning, sending, IMAP polling, breaker evaluation and reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.adapters.registry import build_registry, set_registry
from app.core.clock import SystemClock
from app.core.config import get_settings, live_mode_blockers, startup_problems
from app.core.logging import configure_logging
from app.db.models import Campaign, Mailbox, User
from app.db.session import create_all, dispose_engine, session_scope
from app.services import (
    circuit_breakers,
    deliverability,
    export_erase,
    handoff,
    imap_poller,
    mailboxes,
    qualification,
    reports,
    seed,
    sender,
    sequencer,
    settings_service,
    templates,
    verification,
)


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def _bootstrap() -> None:
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        await create_all()


async def cmd_seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    clock = SystemClock()
    async with session_scope() as session:
        result = await seed.seed_all(
            session,
            settings=settings,
            clock=clock,
            admin_email=args.admin_email or "",
            admin_password=args.admin_password or "",
        )
        if args.demo:
            companies = await seed.seed_demo_companies(session, clock)
            campaign = await seed.seed_campaign(session, settings, clock)
            result["demo_companies"] = len(companies)
            result["demo_campaign"] = campaign.name
        _print(result)
    return 0


async def cmd_admin(args: argparse.Namespace) -> int:
    await _bootstrap()
    async with session_scope() as session:
        user = await seed.ensure_admin(session, email=args.email, password=args.password, name=args.name)
        _print({"id": user.id, "email": user.email, "role": user.role})
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = build_registry(settings)
    set_registry(registry)
    clock = SystemClock()
    await _bootstrap()
    async with session_scope() as session:
        report = await deliverability.run_doctor(session, settings=settings, registry=registry, clock=clock)
    payload = report.as_dict()
    payload["startup_problems"] = startup_problems(settings)
    payload["live_blockers"] = live_mode_blockers(settings)
    payload["mode"] = settings.outreach_mode
    payload["adapters"] = registry.status_report()
    _print(payload)
    print(
        "\nNOTE: failing DNS/provider checks are expected before go-live. "
        "They are the owner's checklist (docs/GO_LIVE_CHECKLIST.md).",
        file=sys.stderr,
    )
    return 0 if not startup_problems(settings) else 1


async def cmd_kill(args: argparse.Namespace) -> int:
    await _bootstrap()
    async with session_scope() as session:
        await settings_service.set_kill_switch(
            session, True, actor=args.actor or "cli", reason=args.reason or "cli kill"
        )
        _print({"kill_switch": True, "actor": args.actor or "cli"})
    return 0


async def cmd_resume(args: argparse.Namespace) -> int:
    await _bootstrap()
    registry = build_registry(get_settings())
    async with session_scope() as session:
        result = await circuit_breakers.resume_all(
            session, actor=args.actor or "cli", registry=registry, clock=SystemClock()
        )
        await settings_service.set_kill_switch(
            session, False, actor=args.actor or "cli", reason="cli resume"
        )
        _print(result)
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    clock = SystemClock()
    async with session_scope() as session:
        _print(
            {
                "mode": await settings_service.effective_mode(session, settings),
                "kill_switch": await settings_service.kill_switch_active(session),
                "global_stop": await settings_service.global_stop(session),
                "due": await sequencer.due_counts(session, clock),
                "kpis": await reports.kpis(session, clock, settings),
            }
        )
    return 0


async def cmd_plan(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    clock = SystemClock()
    async with session_scope() as session:
        campaign = await session.get(Campaign, args.campaign_id)
        if campaign is None:
            print(f"campaign not found: {args.campaign_id}", file=sys.stderr)
            return 2
        result = await sequencer.plan_sequences(session, campaign=campaign, clock=clock, settings=settings)
        _print({"created": result.created, "skipped": result.skipped, "reasons": result.reasons})
    return 0


async def cmd_compose(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = build_registry(settings)
    set_registry(registry)
    await _bootstrap()
    async with session_scope() as session:
        batch = await sequencer.compose_due(
            session, registry=registry, clock=SystemClock(), settings=settings, limit=args.limit
        )
        _print({"composed": batch.composed, "queued": batch.queued, "blocked": batch.blocked, "review": batch.review})
    return 0


async def cmd_send(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = build_registry(settings)
    set_registry(registry)
    await _bootstrap()
    clock = SystemClock()
    async with session_scope() as session:
        allowed, reason = await settings_service.sending_allowed(session, clock)
        if not allowed:
            _print({"skipped": reason})
            return 0
        batch = await sender.send_due(
            session, registry=registry, clock=clock, settings=settings, limit=args.limit
        )
        _print(batch.as_dict())
    return 0


async def cmd_poll_imap(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = build_registry(settings)
    set_registry(registry)
    await _bootstrap()
    clock = SystemClock()
    await _bootstrap()
    ingested = 0
    async with session_scope() as session:
        rows = await session.execute(select(Mailbox).where(Mailbox.status == "active"))
        for mailbox in rows.scalars().all():
            if not mailbox.imap_host:
                continue
            parsed = await imap_poller.poll_mailbox(mailbox, settings=settings, clock=clock)
            for item in parsed:
                await imap_poller.ingest(session, item.raw, clock=clock, settings=settings)
                ingested += 1
    _print({"ingested": ingested, "registry_email": registry.email.name})
    return 0


async def cmd_breakers(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = build_registry(settings)
    await _bootstrap()
    async with session_scope() as session:
        verdict = await circuit_breakers.evaluate(
            session, registry=registry, clock=SystemClock(), settings=settings
        )
        _print(verdict.as_dict())
    return 0


async def cmd_qualify(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    async with session_scope() as session:
        _print(await qualification.qualify_pending(session, clock=SystemClock(), settings=settings))
    return 0


async def cmd_import_csv(args: argparse.Namespace) -> int:
    settings = get_settings()
    del settings
    from app.services import import_csv

    await _bootstrap()
    content = await asyncio.to_thread(Path(args.path).read_text, encoding="utf-8")
    async with session_scope() as session:
        report = await import_csv.commit_import(
            session,
            content=content,
            mapping=None,
            filename=args.path.split("/")[-1],
            actor=args.actor or "cli",
            clock=SystemClock(),
        )
        _print({k: v for k, v in report.items() if k != "duplicate_details"})
    return 0


async def cmd_verify(args: argparse.Namespace) -> int:
    registry = build_registry(get_settings())
    await _bootstrap()
    async with session_scope() as session:
        _print((await verification.verify_company_contacts(session, args.company_id, registry.verify, SystemClock())).as_dict())
    return 0


async def cmd_purge(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    async with session_scope() as session:
        _print(await export_erase.purge_retention(session, settings=settings, clock=SystemClock()))
    return 0


async def cmd_report(args: argparse.Namespace) -> int:
    settings = get_settings()
    await _bootstrap()
    async with session_scope() as session:
        _print(await reports.daily_report(session, SystemClock(), settings))
    return 0


async def cmd_users(args: argparse.Namespace) -> int:
    await _bootstrap()
    async with session_scope() as session:
        rows = await session.execute(select(User).order_by(User.created_at))
        _print([{"email": u.email, "role": u.role, "active": u.is_active} for u in rows.scalars().all()])
    return 0


async def cmd_warmup(args: argparse.Namespace) -> int:
    await _bootstrap()
    async with session_scope() as session:
        _print(await mailboxes.advance_warmup(session, SystemClock()))
    return 0


async def cmd_templates(args: argparse.Namespace) -> int:
    await _bootstrap()
    async with session_scope() as session:
        _print(await templates.seed_templates(session))
    return 0


async def cmd_notify_test(args: argparse.Namespace) -> int:
    registry = build_registry(get_settings())
    await _bootstrap()
    ok, detail = await handoff.notify_staff(
        registry, title="Test alert", body="Connectivity test from mantiq-outreach CLI.", payload={}
    )
    _print({"ok": ok, "detail": detail})
    return 0 if ok else 1


async def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(prog="mantiq-outreach", description="Mantiq Outreach Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="seed playbooks, templates, mailbox and (optionally) demo data")
    p.add_argument("--admin-email", default="")
    p.add_argument("--admin-password", default="")
    p.add_argument("--demo", action="store_true")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("create-admin", help="create or fetch an admin user")
    p.add_argument("email")
    p.add_argument("password")
    p.add_argument("--name", default="Administrator")
    p.set_defaults(func=cmd_admin)

    p = sub.add_parser("doctor", help="run deliverability + readiness checks")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("kill", help="engage the kill switch")
    p.add_argument("--actor", default="cli")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("resume", help="clear the global stop and kill switch (admin action)")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", help="mode, kill switch, due counts, KPIs")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("plan", help="plan campaign prospects")
    p.add_argument("campaign_id")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("compose", help="compose due messages")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("send", help="send due messages")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("poll-imap", help="poll IMAP mailboxes once")
    p.set_defaults(func=cmd_poll_imap)

    p = sub.add_parser("breakers", help="evaluate circuit breakers")
    p.set_defaults(func=cmd_breakers)

    p = sub.add_parser("qualify", help="score pending companies")
    p.set_defaults(func=cmd_qualify)

    p = sub.add_parser("import-csv", help="import a CSV of companies")
    p.add_argument("path")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("verify", help="verify contacts of a company")
    p.add_argument("company_id")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("purge", help="run retention purge")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("report", help="print the daily report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("users", help="list users")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("warmup", help="advance warm-up stages")
    p.set_defaults(func=cmd_warmup)

    p = sub.add_parser("templates", help="seed templates from config")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("notify-test", help="send a test staff notification")
    p.set_defaults(func=cmd_notify_test)

    args = parser.parse_args(argv)
    try:
        return int(await args.func(args))
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
