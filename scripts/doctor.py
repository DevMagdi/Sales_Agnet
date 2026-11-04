#!/usr/bin/env python3
"""Readiness doctor: deliverability checks, adapter status, config blockers and advisories.

    python scripts/doctor.py              # human-readable
    python scripts/doctor.py --json       # machine-readable (CI)

Exit code is 0 when nothing *boot-blocking* is wrong, 1 otherwise. DNS/provider checks that fail
because the deployment has not been configured yet are reported as GO-LIVE items, not as bugs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


async def run(as_json: bool) -> int:
    from app.adapters.registry import build_registry
    from app.core.clock import SystemClock
    from app.core.config import get_settings, live_mode_blockers, startup_problems
    from app.db.session import create_all, dispose_engine, session_scope
    from app.services import deliverability, reports

    settings = get_settings()
    registry = build_registry(settings)
    await create_all()
    async with session_scope() as session:
        report = await deliverability.run_doctor(
            session, settings=settings, registry=registry, clock=SystemClock()
        )
        kpis = await reports.kpis(session, SystemClock(), settings)

    payload = {
        "mode": settings.outreach_mode,
        "app_env": settings.app_env,
        "startup_problems": startup_problems(settings),
        "live_mode_blockers": live_mode_blockers(settings),
        "doctor": report.as_dict(),
        "adapters": registry.status_report(),
        "kpis": kpis,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not payload["startup_problems"] else 1

    print(f"Mantiq Outreach Agent — doctor\nmode={settings.outreach_mode}  env={settings.app_env}\n")
    print("Adapters")
    for name, status in payload["adapters"].items():
        flag = "FAKE" if status["is_fake"] else ("ok" if status["ok"] else "ERROR")
        print(f"  {name:<9} {flag:<6} {status['name']} — {status['detail']}")
    print("\nDeliverability / readiness checks")
    for check in payload["doctor"]["checks"]:
        mark = "PASS" if check["ok"] else ("FAIL" if check["blocking"] else "WARN")
        print(f"  [{mark}] {check['name']:<18} {check['detail']}")
    print(f"\nOverall: {'READY' if payload['doctor']['ok'] else 'NOT READY for live sending'}")
    if payload["startup_problems"]:
        print("\nBoot-blocking problems:")
        for problem in payload["startup_problems"]:
            print(f"  - {problem}")
    if payload["live_mode_blockers"]:
        print("\nLive-mode blockers (complete docs/GO_LIVE_CHECKLIST.md before switching to live):")
        for blocker in payload["live_mode_blockers"]:
            print(f"  - {blocker}")
    print(
        "\nReminder: DNS/SPF/DKIM/DMARC failures are expected until the sending domain is configured;"
        " the application refuses live mode until they pass."
    )
    await dispose_engine()
    return 0 if not payload["startup_problems"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mantiq Outreach Agent readiness doctor")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.json)))
