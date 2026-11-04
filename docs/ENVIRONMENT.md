# Environment

Every setting comes from the process environment (or a local `.env` file, which `pydantic-settings`
reads from the working directory — `backend/.env` in development, `deploy/.env` for compose).
`.env.example` at the repository root and `deploy/.env.example` list all 70+ variables with the
production defaults; this page explains *why* each one matters.

## Boot-critical (the app refuses to start without them)

| Variable | Why |
|----------|-----|
| `SECRET_KEY` | signs access/refresh tokens and public links. Rotating it logs everyone out and invalidates pending unsubscribe links (tokens carry their own TTL). |
| `ENCRYPTION_MASTER_KEY` | wraps the data-encryption key for every PII field. **Losing it means losing every stored email/phone/credential** — store it in a secret manager and back it up. |
| `DATABASE_URL` | `postgresql+asyncpg://…` in production, `sqlite+aiosqlite:///./mantiq.db` for a laptop. |
| `APP_ENV` | `dev` / `test` / `staging` / `production`. Anything production-like turns on the fake-provider guard. |
| `OUTREACH_MODE` | `dry_run` (default), `sandbox`, `live`. See below. |

## Modes

| Mode | Behaviour |
|------|-----------|
| `dry_run` | Composes, gates and records `would_send`. **No adapter is called.** Safe default; use it for every demo. |
| `sandbox` | Really sends, but only to addresses in `SANDBOX_ALLOWLIST` (comma-separated). Everything else is blocked by the gate with `sandbox:*`. |
| `live` | Real sending. Refused unless every `live_mode_blockers()` entry is satisfied — real LLM + email providers, https `PUBLIC_BASE_URL`, sender identity, offer text, booking/features URLs — **and** an admin confirms the switch (`POST /api/v1/system/mode {"mode":"live","confirm_live":true}`). |

`ALLOW_FAKE_PROVIDERS=true` is the deliberate, logged escape hatch that lets a staging environment
run fake LLM/email adapters. It never applies to `live` mode.

## Providers

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_PROVIDER` | `fake` | `anthropic` or `openai` (compatible base URL). `fake` is deterministic and scriptable — perfect for tests, useless in production. |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_FAST`, `LLM_MODEL_STRONG` | — | `LLM_MODEL_FAST` for classification/qualification, `LLM_MODEL_STRONG` for research and copy. |
| `LLM_DAILY_BUDGET_USD`, `API_DAILY_BUDGET_USD` | `5`, `5` | Soft caps enforced by the gate (`budget` check) and by the breakers. Also editable in the dashboard (`POST /api/v1/system/budgets`). |
| `LLM_PRICE_INPUT_PER_M`, `LLM_PRICE_OUTPUT_PER_M` | `1`, `3` | Used to price every call into `llm_calls` so cost per demo is real. |
| `EMAIL_PROVIDER` | `fake` | `smtp` (DKIM-signed, IMAP for replies) or `api` (webhook-compatible). |
| `SMTP_*`, `IMAP_*` | — | Per-mailbox credentials in the database normally win; these are the fallback identity. |
| `EMAIL_API_URL`, `EMAIL_API_KEY`, `EMAIL_WEBHOOK_SECRET` | — | API provider + signature secret for `POST /api/v1/webhooks/email`. |
| `SENDING_DOMAIN`, `DKIM_SELECTOR`, `DKIM_PRIVATE_KEY` | — | Without a key, outbound mail is unsigned — unacceptable for first contact, so live mode blocks it. |
| `SEARCH_PROVIDER` | `fake` | `brave` / `serpapi`; `PLACES_API_KEY` adds Google Places discovery. |
| `VERIFY_PROVIDER` | `local` | `local` = syntax + blocklist + MX lookup (no network in tests, returns `risky/mx_unverified_offline`). |
| `WHATSAPP_*` | — | Meta Cloud API. Only used for opted-in contacts and pre-approved templates. |

## Public surface

| Variable | Why |
|----------|-----|
| `PUBLIC_BASE_URL` | Prefix for unsubscribe/click links inside every email. Must be https in production or the gate blocks sending — otherwise one-click opt-out cannot work. |
| `FEATURES_URL`, `BOOKING_URL` | Destinations used in copy and in `/click/{token}`. |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the API from a browser (the dashboard is same-origin via a proxy, so this is mostly for tooling). |
| `CLICK_TRACKING_ENABLED`, `OPEN_TRACKING_ENABLED` | Off by default: pixel tracking is a deliverability and privacy liability. |

## Sender identity (required for `live`)

`SENDER_NAME`, `SENDER_TITLE`, `SENDER_EMAIL`, `COMPANY_NAME`, `COMPANY_ADDRESS`, `COMPANY_PHONE`,
`POC_OFFER_TEXT_AR`, `POC_OFFER_TEXT_EN` (must contain `{duration}`), `DEMO_DURATION_MINUTES`.
These end up in the email footer, which is why the app refuses to send without them.

## Crawler (polite by design)

`CRAWLER_USER_AGENT` (default `MantiqOutreachBot/1.0`), `CRAWLER_BOT_URL`, `CRAWL_MAX_PAGES=6`,
`CRAWL_DELAY_SECONDS=1`, `CRAWL_TIMEOUT_SECONDS=10`, `CRAWL_RECRAWL_AFTER_DAYS=30`,
`CRAWL_MAX_PAGE_BYTES=2000000`, `CRAWL_ALLOW_LOCAL_HOSTS=false` (only tests set this true, so they can
crawl a local fixture server).

## Sessions, notifications, retention

| Variable | Default | Notes |
|----------|---------|-------|
| `ACCESS_TOKEN_MINUTES` / `REFRESH_TOKEN_DAYS` | `30` / `14` | Short access tokens, rotating refresh tokens (they are hashed, revoked and replaced on use). |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | `5` / `900` | Per-account lockout; attempts are audited. |
| `UNSUBSCRIBE_TOKEN_TTL_DAYS` | `90` | Unsubscribe links stay valid for 90 days (they stop mattering once the address is suppressed). |
| `NOTIFY_WEBHOOK_URL`, `NOTIFY_EMAIL` | — | Where breakers, hand-offs and kill-switch events are announced. Fake adapter keeps them in memory in dev. |
| `RETENTION_DAYS_PAGE_TEXT` / `_CLOSED_CONTACTS` / `_MESSAGES` | `180` / `365` / `2555` | Cut-offs used by the daily purge. |
| `LOG_LLM_PAYLOADS` | `false` | Turn on only while debugging a prompt — it stores customer text in logs. |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Run tasks inline (useful in tests / a single-process demo). |

## Compose-only variables

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (PostgreSQL credentials), `API_PORT`,
`DASHBOARD_PORT` (published ports), `BACKEND_URL` (the dashboard's proxy target — `http://api:8000`
inside compose).

## Minimal `.env` for a laptop

```dotenv
APP_ENV=dev
SECRET_KEY=dev-secret-key-change-me-0123456789abcdef
ENCRYPTION_MASTER_KEY=dev-master-key-change-me-0123456789abcdef
DATABASE_URL=sqlite+aiosqlite:///./mantiq.db
PUBLIC_BASE_URL=http://127.0.0.1:8000
OUTREACH_MODE=dry_run
LLM_PROVIDER=fake
EMAIL_PROVIDER=fake
SEARCH_PROVIDER=fake
VERIFY_PROVIDER=local
```

Everything else has a working default; `python -m app.cli doctor` tells you what is still missing.
