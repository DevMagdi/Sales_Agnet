# Architecture

## Shape

```
                 ┌──────────────────────── dashboard (Next.js 14) ───────────────────────┐
                 │  /  /campaigns  /review  /replies  /tasks  /suppression  /settings  /system │
                 └───────────────────────────────┬──────────────────────────────────────────┘
                                                 │ same-origin /api/v1 (Next rewrite proxy)
┌────────────────────────────────────────────────▼───────────────────────────────────────────┐
│ FastAPI app (backend/app)                                                                  │
│   api/v1/*        auth, campaigns, companies, discovery, messages, replies, tasks, demos,   │
│                   reports, settings, suppression, system, templates, events, webhooks       │
│   api/public/*    /features, /bot, /u/{token} (one-click unsubscribe), /click/{token}       │
│   services/*      send_gate, sender, sequencer, composer, reply_handler, inbound,           │
│                   circuit_breakers, suppression, consent, budgets, scheduling, qualification│
│                   verification, enrichment, discovery, crawler, reports, deliverability,    │
│                   templates, claim_check, state_machine, audit, mailboxes, settings_service │
│   adapters/*      llm, email (smtp/api/dkim/fake), whatsapp, search, places, verify,        │
│                   calendar, notify — one registry, swapped by env                            │
│   db/models/*     30 tables; Alembic migrations in backend/migrations/versions              │
│   workers/*       Celery tasks + beat schedule (degrades to no-op without Redis)             │
└───────────────┬───────────────────────────────────────────────┬────────────────────────────┘
                │                                               │
        PostgreSQL (prod) / SQLite (dev, tests)          Redis (Celery broker + rate limits)
```

Everything the dashboard does goes through the same HTTP API a human or a script would use — there is
no privileged back channel into the services layer.

## Data model (30 tables)

Grouped by responsibility:

| Group | Tables |
|-------|--------|
| Tenancy & auth | `organizations`, `users`, `refresh_tokens`, `api_keys` |
| Companies & people | `companies`, `contacts`, `research_snapshots`, `page_cache`, `contact_verifications` |
| Campaigns & outreach | `campaigns`, `campaign_prospects`, `outreach_messages`, `message_events`, `templates`, `mailboxes`, `mailbox_pools` |
| Replies & humans | `inbound_messages`, `replies`, `tasks`, `demos`, `consent_records` |
| Safety & money | `suppression_list`, `audit_log`, `settings`, `llm_calls`, `api_calls`, `webhook_deliveries` |
| Discovery & ops | `discovery_jobs`, `place_results`, `outbox_events`, `playbooks`, `send_attempts` |

Notes that shaped the schema:

- PII (emails, phones, password fields) is stored **encrypted** (`ENCRYPTION_MASTER_KEY`); lookups use
  a separate keyed hash (`blind_index`). `suppression_list` keeps only the hash — `value_stored = false`.
- `audit_log` and `suppression_list` are **append-only**, enforced by PostgreSQL triggers in the first
  migration, so an application bug cannot erase evidence.
- `campaign_prospects` is unique on `(campaign_id, company_id)`: one conversation per company per
  campaign. The state machine (`queued → composing → … → do_not_contact`) records every transition.
- `outreach_messages` carries `idempotency_key` (unique), `locked_by` / `locked_until` (claim lease so
  two workers cannot double-send), `check_report` (the full gate verdict) and `rfc_message_id` for
  threading.

## Request flow: composing and sending

1. **Plan** — `sequencer.plan_sequences` walks companies, filters by industry/country/city/ICP score,
   skips already-suppressed and already-in-campaign companies, and creates `campaign_prospects`
   with a reason for every skip (`industry_filter`, `below_icp_threshold`, `suppressed`, …).
2. **Research** — `research.research_company` crawls the company site politely (robots.txt, delay,
   page cap), stores snapshots, extracts procurement signals, and keeps only evidence with a URL.
3. **Qualify** — `qualification` scores the ICP and produces a brief; nothing is invented.
4. **Compose** — `composer.compose_for_prospect` picks the step by state and gap, picks Arabic or
   English from the contact/company locale, renders the template, runs the claim checker against
   `config/claims.yaml` + evidence, then evaluates the gate.
5. **Review** (optional, `review_first_n`) — messages land in `needs_review` and wait for a human.
6. **Send** — `sender.send_due` claims due messages with an atomic `UPDATE … WHERE status='queued'`
   lease, sends via the email adapter (DKIM-signed when a key is configured), re-evaluates the gate,
   records a `message_events` row and an `outreach_messages` status.
7. **Replies** — webhooks or IMAP polling feed `inbound.ingest`, which threads by `In-Reply-To`,
   references and subject token. `reply_handler.process_inbound` classifies (deterministic rules
   first, LLM second), then acts per class: auto-reply for info/booking, suppression for opt-outs,
   task + hand-off for pricing and objections, state transitions in every case.
8. **Breakers** — every send and every inbound event feeds rolling 24h rates; tripping pauses a
   campaign or stops everything and notifies staff.

## Scheduling (Celery beat)

| Task | Cadence | Purpose |
|------|---------|---------|
| `outreach.plan` | hourly | plan prospects for active campaigns |
| `outreach.compose_due` | every 10 min | compose what is due, respecting windows |
| `outreach.send_due` | every minute | claim + send due messages |
| `outreach.poll_imap` | every minute | ingest replies |
| `outreach.handle_replies` | every 5 min | classify/handle any unhandled reply |
| `outreach.evaluate_breakers` | every 15 min | pause/stop on rates |
| `outreach.purge_retention` | daily | retention cut-offs |
| `outreach.warmup` | daily | advance mailbox warm-up stages |

Without Redis the API stays fully functional; only the timers are gone (run `python -m app.cli
compose|send|poll-imap` or hit `POST /api/v1/messages/compose-due` / `send-due` instead).

## Adapters

One registry (`app/adapters/registry.py`) holds `llm, email, whatsapp, search, places, verify, calendar,
notify`, each chosen by an environment variable and each with a `fake` implementation used by tests,
dry-run and demos.

| Adapter | Real implementations | Fake |
|---------|----------------------|------|
| email | `smtp` (+DKIM), `api` (webhook-compatible provider) | `fake` (records to an in-memory outbox) |
| llm | Anthropic / OpenAI-compatible (`providers.py`) | `fake` (deterministic, scriptable) |
| search | Brave, SerpAPI | `fake` |
| places | Google Places | `fake` |
| verify | `local` (syntax + blocklist + MX), external API | `fake` |
| whatsapp | Meta Cloud API (opt-in only, template messages) | `fake` |
| calendar | booking link | `link` |
| notify | webhook, email | `fake` |

`build_registry` refuses to boot with fake LLM/email adapters when `OUTREACH_MODE=live`.

## Public pages

`/features` (bilingual, `?lang=ar|en`), `/bot` (crawler policy page), `/u/{token}` (unsubscribe with a
confirmation page; the POST is RFC 8058 one-click) and `/click/{token}` (tracked redirect, disabled by
default). They are server-rendered HTML with no JavaScript, so they survive strict email clients.
