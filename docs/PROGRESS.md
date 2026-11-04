# Progress log — what is proven, what is only wired

Last verified: 2026-11-04. Every command below was run in this workspace and the output is pasted
verbatim (trimmed only where noted).

## Verified in this build

| Area | Command | Result |
|------|---------|--------|
| Unit + service + API tests | `cd backend && pytest tests/ -q` | **196 passed** in ~42 s |
| Coverage | `pytest tests/ --cov=app --cov-report=term` | **70%** of 8,650 statements |
| Lint | `ruff check .` | `All checks passed!` |
| Types | `mypy app` (strict) | `Success: no issues found in 118 source files` |
| Migrations | `alembic upgrade head && alembic check` | two revisions applied, `No new upgrade operations detected.` |
| End-to-end | `python scripts/e2e_journeys.py` | `ALL JOURNEYS PASSED` (44 assertions, 5 journeys) |
| Readiness | `python scripts/doctor.py` | runs, exit 0, prints adapters + deliverability + live blockers |
| Dashboard API contract | `node scripts/verify_dashboard.mjs` | **58 checks, 0 failures**, `DASHBOARD CONTRACT OK` |
| Dashboard build | `cd frontend && npm run typecheck && npm run lint && npm run build` | 10 routes prerendered, no type or lint errors |

### Test breakdown

```
tests/test_core.py             48   crypto, text normalisation, intents, injection, clock, auth helpers
tests/test_send_gate.py        43   the 14 gate checks, each with a scenario that must block
tests/test_reply_pipeline.py   21   inbound threading, classification, per-class actions
tests/test_services_flow.py    20   plan → compose → send, breakers, budgets, reports, discovery, retention
tests/test_api_business.py     18   campaign/reply/task/report endpoints through the ASGI client
tests/test_adapters.py         17   fake + real adapter contracts, DKIM, webhook signature
tests/test_api_auth.py         12   login, refresh rotation, lockout, RBAC, MFA
tests/test_suppression.py      10   absolute suppression, parent domains, phone normalisation
tests/test_yamlconf.py          7   the YAML config is pure data and validated
```

### End-to-end output (trimmed of the payload dumps)

```
$ python scripts/e2e_journeys.py
  [PASS] login                     [PASS] auth /me               [PASS] role is admin
  [PASS] system status             [PASS] deliverability doctor  [PASS] create campaign
  [PASS] activate campaign         [PASS] plan sequences         [PASS] prospects created
  [PASS] compose due messages      [PASS] message composed and held for review
  [PASS] send-due                  [PASS] send-due ran through the gate
  [PASS] review queue              [PASS] first message held for review
  [PASS] review approval re-runs the gate   [PASS] KPIs      [PASS] kpis present
  [PASS] funnel                    [PASS] funnel csv             [PASS] campaign has prospects
  [PASS] prospect exists           [PASS] inbound ingested and threaded
  [PASS] reply classified          [PASS] reply list             [PASS] reply visible to staff
  [PASS] reply detail              [PASS] hand-off created        [PASS] tasks list
  [PASS] contact available         [PASS] unsubscribe page renders
  [PASS] RFC 8058 one-click accepted    [PASS] address is suppressed
  [PASS] prospect moved to do_not_contact  [PASS] kill switch engages
  [PASS] sending is refused while stopped  [PASS] resume clears the stop
  [PASS] breaker evaluation        [PASS] breaker verdict returned
  [PASS] feature page (ar)         [PASS] feature page (en)      [PASS] crawler page
  [PASS] prometheus metrics
ALL JOURNEYS PASSED
```

### Doctor output (dev machine, fake providers, no DNS)

```
$ python scripts/doctor.py
mode=dry_run  env=dev
Adapters
  llm       FAKE   fake — deterministic fake LLM (dev/test/dry_run only)
  email     FAKE   fake — fake email adapter (dry_run/sandbox/test only)
  verify    ok     local — local syntax + blocklist + MX checks
  calendar  ok     link — booking link set
Deliverability / readiness checks
  [FAIL] spf   [FAIL] dkim   [FAIL] dmarc   [FAIL] mx        ← no outbound DNS in this sandbox
  [PASS] dnsbl  [FAIL] unsubscribe_url  [FAIL] mailbox
  [PASS] bounce_rate 0.00%   [PASS] complaint_rate 0.000%   [PASS] kill_switch_off
  [FAIL] provider_llm   [FAIL] provider_email
Overall: NOT READY for live sending
```

This is the honest state: the *checks work*, and they correctly refuse to call a machine with fake
providers and no DNS "ready".

### Dashboard

Nine routes (`/`, `/login`, `/campaigns`, `/review`, `/replies`, `/tasks`, `/suppression`, `/settings`,
`/system`) talking to 40+ endpoints, all reached through the Next.js rewrite proxy exactly as the
browser does. `scripts/verify_dashboard.mjs` also asserts that credentials are never echoed back
(`mailbox secret is masked`) and that a duplicate mailbox returns **409**, not 500.

## Wired, not witnessed (needs real infrastructure)

These paths are implemented, unit-tested against fakes, and reachable from the API/CLI, but have
never talked to a real provider from this workspace:

| Path | Why not witnessed | What to do on a real host |
|------|-------------------|---------------------------|
| SMTP send + DKIM signing | no mailbox, no DNS | `EMAIL_PROVIDER=smtp`, real credentials, send to a Gmail address, check `dkim=pass` |
| IMAP reply polling | same | `python -m app.cli poll-imap` against a real inbox |
| Provider webhooks (bounces/complaints) | needs a public https endpoint | register `POST /api/v1/webhooks/email` with the provider secret |
| Real LLM calls | no API key in the sandbox | `LLM_PROVIDER=anthropic` + key; the adapter contract is shared with the fake |
| Search / Places discovery | no API keys | `SEARCH_PROVIDER=brave`, `PLACES_API_KEY` |
| DNS checks (SPF/DKIM/DMARC/MX) | outbound DNS blocked | `doctor` on a networked host |
| PostgreSQL-specific behaviour | no Postgres here | append-only triggers, `FOR UPDATE SKIP LOCKED` claim path |
| Celery worker + beat | no Redis | `docker compose up`, watch the schedule in `/system` |
| DNSBL lookups | outbound DNS blocked | `doctor` → `dnsbl` |

The fakes are not stubs of convenience: `FakeEmailAdapter` records RFC-shaped messages and can emit
bounce/complaint events, the fake LLM is scriptable per task, and the verification adapter is a real
local implementation (syntax + blocklist + MX). That is why 70% coverage means something.

## Known gaps / next work

1. **WhatsApp is 0% covered.** `whatsapp_service.py` and the Cloud API adapter are written but only
   exercised by the adapter test; treat WhatsApp as experimental.
2. **`whatsapp_meta.yaml` templates** are seeded but never previewed against Meta's template rules.
3. **A/B testing of templates** is not implemented — `template_set` is a single pointer.
4. **Reporting** is counters and CSV, not charts; the dashboard shows numbers, not trends.
5. **Frontend has no automated tests** beyond type-check, lint, build and the API contract script.
6. **i18n of the dashboard UI** is English; only the outbound copy and `/features` are bilingual.
7. **`ruff format`** is not enforced (78 files would change) — deliberately deferred to keep diffs readable.
8. **ML-based reply classification** is heuristic + LLM; no evaluation set with real replies yet.

## Changelog highlights (this build)

- Send gate with 14 ordered checks, re-evaluated at approval *and* at send time.
- Claim checker that refuses any fact not present in `config/claims.yaml` or the company's own pages.
- Reply pipeline: deterministic intent detection first, LLM second, per-class actions (auto-reply,
  hand-off, suppression, snooze).
- Circuit breakers with human-only resume; kill switch that pauses every campaign and audits itself.
- One-click unsubscribe (RFC 8058) with blind-index suppression (`value_stored = false`).
- Bilingual Arabic/English templates for every sequence step and both channels.
- Dashboard: overview, campaigns, review queue, replies (thread + override + send), tasks,
  suppression, settings (mailboxes/users), system (kill switch, mode, budgets, doctor, breakers, audit).
- CLI with 18 commands, readiness doctor, end-to-end journey script, dashboard contract script.
- Docker compose stack (Postgres, Redis, API, worker, beat, dashboard) and a CI workflow
  (lint, types, tests with coverage gate, migrations, e2e, doctor, dashboard build).
