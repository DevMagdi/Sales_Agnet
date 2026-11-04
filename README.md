# Mantiq Outreach Agent

An autonomous B2B outbound sales agent for **ProcureAI** (Mantiq Technologies): it discovers Egyptian
manufacturers, qualifies them against an ICP, researches them, writes Arabic/English outreach that
passes a claim checker, sends it inside send windows and budget limits, reads the replies, books demos
and hands warm leads to a human — with a kill switch and an audit trail over everything.

Built to be *boringly safe*: nothing leaves the system without passing the **Send Gate**, every
suppression is absolute, and the agent can be stopped by one click or one environment variable.

---

## What is in the repository

```
backend/     FastAPI app, 30 ORM tables, 6 services layers, adapters, Celery workers, CLI, migrations, tests
frontend/    Next.js 14 + Tailwind dashboard (Arabic/English data, English UI), same-origin API proxy
config/      YAML that is *pure data*: claims fact sheet, industries, blocklists, holidays, email/WhatsApp templates
scripts/     e2e_journeys.py (end-to-end proof), doctor.py (readiness audit), verify_dashboard.mjs (API contract)
deploy/      docker-compose stack (Postgres, Redis, API, worker, beat, dashboard) + .env template
docs/        architecture, environment, operations runbooks, go-live checklist, honest progress log
```

Numbers that matter today: **95 API paths / 107 operations**, **30 tables**, **196 backend tests
(70% coverage)**, **5 end-to-end journeys**, **14 ordered send-gate checks**.

---

## Quick start (development, no Docker)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"

cp .env.example backend/.env          # then set SECRET_KEY + ENCRYPTION_MASTER_KEY (32+ chars)
cd backend
python -m app.cli seed --demo         # playbooks, templates, mailbox, demo companies, a draft campaign
python -m app.cli create-admin admin@mantiq.example 'a-long-password'
uvicorn app.main:app --reload --port 8000
```

Dashboard:

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000, proxies /api/v1 to BACKEND_URL (default 127.0.0.1:8000)
```

Everything defaults to `OUTREACH_MODE=dry_run` with fake providers: the agent composes, gates and
*logs* what it would send (`would_send`) but never talks to a real mailbox, LLM or search API.

## Production-shaped stack

```bash
cp deploy/.env.example deploy/.env    # secrets + sender identity + public https URLs
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.yml exec api python -m app.cli seed --demo
docker compose -f deploy/docker-compose.yml exec api python -m app.cli doctor
```

The dashboard is the only published port (default 3000) and proxies `/api/v1` to the API container.
The API container runs `alembic upgrade head` on boot; `worker` and `beat` reuse the same image.

---

## How a message gets out (the Send Gate)

`app/services/send_gate.py` evaluates **14 ordered checks** and stops at the first failure. A message
is only sent when all of them pass:

| # | Check | What it refuses |
|---|-------|-----------------|
| 1 | `kill_switch` | anything at all while the kill switch is on |
| 2 | `mode` | dry-run traffic, live mode without real providers, sandbox traffic to addresses outside `SANDBOX_ALLOWLIST` |
| 3 | `suppression` | any suppressed email/domain/phone (the only exceptions are the unsubscribe confirmation, a 1:1 thread reply, and a prospect already in `do_not_contact`) |
| 4 | `channel` | a channel the campaign or the consent record does not allow |
| 5 | `contact_eligible` | unverified/risky addresses unless the campaign opted in |
| 6 | `one_conversation` | a second open conversation with the same company |
| 7 | `step_logic` | a follow-up before its gap, or more follow-ups than `max_followups` |
| 8 | `send_window` | outside the campaign window, on a weekend, or on an Egyptian/Saudi/UAE holiday |
| 9 | `rate_limits` | mailbox daily cap, warm-up ramp, per-hour limits |
| 10 | `breaker` | a tripped circuit breaker or a global stop that a human has not cleared |
| 11 | `content` | injected instructions, missing unsubscribe link, forbidden phrasing |
| 12 | `claims` | a factual claim that is not backed by the fact sheet or the research evidence |
| 13 | `budget` | the daily LLM/API budget |
| 14 | `idempotency` | a second send for the same (prospect, step, channel) |

The gate runs **again** immediately before sending, so approving a message in the review queue
cannot bypass a rule that started failing in the meantime.

## What stops everything (circuit breakers)

`app/services/circuit_breakers.py` pauses a campaign when its hard-bounce rate exceeds 3% (over at
least 30 sends) and stops *all* sending on: global hard bounce > 5%, complaint rate > 0.1%, provider
auth errors, or a budget breach. Tripping is terminal until a human runs `resume` — the agent never
auto-resumes. Both paths alert staff and write to `audit_log`.

## Compliance built in

- One-click unsubscribe (`POST /u/{token}`) → suppressed immediately, `do_not_contact`, RFC 8058 headers on every message.
- `List-Unsubscribe` + `List-Unsubscribe-Post`, physical address and phone in the footer.
- Suppression stores **only a blind index** of the address (`value_stored = false`) — you can check a
  match, you cannot mine the list.
- Erasure (`DELETE /api/v1/companies/{id}/erase`) and retention purge with per-table cut-offs.
- Arabic and English copy written for the Egyptian market; no invented prices, no fake case studies —
  the claim checker refuses facts that are not in `config/claims.yaml` or the company's own pages.

---

## Verify it yourself

```bash
cd backend
pytest tests/ -q                                   # 196 tests
ruff check . && mypy app                           # lint + strict types
alembic upgrade head && alembic check              # migrations match the models
cd .. && python scripts/e2e_journeys.py            # 5 journeys through the real app
python scripts/doctor.py                           # readiness audit (SPF/DKIM/DMARC, adapters, blockers)
node scripts/verify_dashboard.mjs                  # every dashboard call, against a running stack
```

`scripts/e2e_journeys.py` drives the real ASGI app with real auth, the real gate, the real reply
pipeline and a real database: login → campaign → plan → compose → review → approve → KPIs/funnel/CSV,
inbound reply → classify → hand-off, one-click unsubscribe → suppressed, kill switch blocks sending
then resume, and the public pages + `/metrics`.

## CLI (no dashboard required)

```bash
python -m app.cli seed --demo        # seed config + demo data
python -m app.cli status             # mode, kill switch, due counts, KPIs
python -m app.cli doctor             # deliverability + readiness checks
python -m app.cli kill --reason "..." ; python -m app.cli resume
python -m app.cli plan|compose|send  # run the pipeline once, the way the worker does
python -m app.cli poll-imap          # pull replies once
python -m app.cli breakers           # evaluate breakers now
python -m app.cli import-csv <path>  # import companies from a CSV
python -m app.cli report             # daily report
```

---

## Status and honesty

`docs/PROGRESS.md` is the short version of what is proven and what is *wired but not witnessed*:
in this build environment there is no outbound DNS, no live mailbox, no live LLM key and no
PostgreSQL/Redis, so the adapters for those paths are implemented and unit-tested but have never
talked to a real provider. `docs/GO_LIVE_CHECKLIST.md` lists the human steps (DNS, DKIM rotation,
warm-up, legal review) that must happen before `OUTREACH_MODE=live` is even accepted by the app.

- `docs/ARCHITECTURE.md` — data model, services, request flow, scheduling.
- `docs/ENVIRONMENT.md` — every environment variable, what breaks without it.
- `docs/OPERATIONS.md` — runbooks: kill switch, breaker resume, suppression requests, erasure, purge.
- `docs/GO_LIVE_CHECKLIST.md` — the gate between sandbox and a real prospect.
