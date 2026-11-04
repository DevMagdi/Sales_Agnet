# Go-live checklist

The application refuses `OUTREACH_MODE=live` until the **automatable** half of this list passes
(`live_mode_blockers()` in `backend/app/core/config.py`, surfaced by `python -m app.cli doctor` and
`GET /api/v1/system/doctor`). The rest is human work — do not skip it because the software says yes.

## 1. Domain and DNS (blocks: deliverability, not boot)

- [ ] Sending sub-domain exists and is **not** the primary corporate domain (`outreach.example.com`, not `example.com`).
- [ ] SPF TXT record publishes exactly one `include:` for the provider and ends in `-all`.
- [ ] DKIM: 2048-bit key, selector published, `DKIM_SELECTOR`/`DKIM_PRIVATE_KEY` set, and a test mail verifies (`dkim=pass`).
- [ ] DMARC published at `p=none` with a `rua` address, then raised to `p=quarantine` after two weeks of clean reports.
- [ ] MX records exist and accept bounces (a full bounce mailbox is how you learn your list is bad).
- [ ] Reverse DNS / PTR matches the sending host, if you send from your own IP.
- [ ] Domain is **not** on Spamhaus/SORBS/Barracuda (check with `doctor` → `dnsbl`).
- [ ] `PUBLIC_BASE_URL` is https and publicly reachable — the unsubscribe link inside every email must work from a stranger's phone.
- [ ] `/u/{token}` renders, and a one-click POST (RFC 8058) suppresses the address within seconds.

## 2. Mailbox and warm-up (blocks: volume)

- [ ] Mailbox created, status `active`, SMTP + IMAP credentials tested (`POST /api/v1/settings/mailboxes/{id}/test`).
- [ ] `daily_cap` starts at 20–30 and follows the warm-up ramp — never jump to the target volume.
- [ ] Reply handling proven: send a real message to your own address, reply to it, watch `/replies` classify it.
- [ ] A human reads the first 20 outbound messages in the review queue (`review_first_n` → 20) before trusting the copy.

## 3. Providers and keys (blocks: boot in live)

- [ ] `LLM_PROVIDER` is real (Anthropic or an OpenAI-compatible endpoint), `LLM_API_KEY` set, `LLM_MODEL_FAST`/`LLM_MODEL_STRONG` chosen and priced.
- [ ] `EMAIL_PROVIDER` is real (`smtp` or `api`); `EMAIL_PROVIDER=fake` is rejected in live mode.
- [ ] `SEARCH_PROVIDER` / `PLACES_API_KEY` are real if discovery is enabled.
- [ ] `VERIFY_PROVIDER` is real (an MX-only check is not verification; it is a first filter).
- [ ] `NOTIFY_WEBHOOK_URL` / `NOTIFY_EMAIL` point at a channel a human actually watches.
- [ ] `WHATSAPP_*` only if WhatsApp is enabled for opted-in contacts with approved templates.

## 4. Identity and legal (blocks: boot in live)

- [ ] `SENDER_NAME`, `SENDER_TITLE`, `SENDER_EMAIL` are a real person who will answer the replies.
- [ ] `COMPANY_NAME`, `COMPANY_ADDRESS`, `COMPANY_PHONE` appear in the footer of every message (required by CAN-SPAM / Egypt's data-protection law).
- [ ] `POC_OFFER_TEXT_AR` / `POC_OFFER_TEXT_EN` contain `{duration}` and describe something you can actually deliver.
- [ ] `DEMO_DURATION_MINUTES` matches the booking calendar.
- [ ] `BOOKING_URL` and `FEATURES_URL` are live and load in under 2 seconds on mobile.
- [ ] `config/claims.yaml` reviewed by whoever owns the product: every claim is true, dated and defensible.
- [ ] Legal review of the outbound copy, the suppression policy and the retention periods (`RETENTION_DAYS_*`).

## 5. Safety systems (verify, don't assume)

- [ ] Kill switch tested in staging: engage → `send-due` claims nothing → clear → sending resumes only after campaigns are re-activated.
- [ ] Breakers tested: seed a bounce storm in staging and watch the global stop trip and notify staff.
- [ ] A tripped breaker requires an admin `resume` — confirm the agent never auto-resumes.
- [ ] Budget caps set (`LLM_DAILY_BUDGET_USD`, `API_DAILY_BUDGET_USD`) and the `budget` gate check proven.
- [ ] Suppression tested from three entry points: one-click link, a reply that says "stop", and a complaint webhook.
- [ ] Erasure (`DELETE /companies/{id}/erase`) tested and the export (`GET /companies/{id}/export`) reviewed.

## 6. Operations

- [ ] `SECRET_KEY` and `ENCRYPTION_MASTER_KEY` generated with `openssl rand -base64 48`, stored in a secret manager, **backed up separately** (losing `ENCRYPTION_MASTER_KEY` destroys every stored address).
- [ ] Database is PostgreSQL, nightly `pg_dump` verified by an actual restore into a scratch database.
- [ ] Redis is running, Celery worker + beat are supervised (`docker compose ps` shows them healthy).
- [ ] Logs ship somewhere with 90-day retention; PII is not in them (`LOG_LLM_PAYLOADS=false`).
- [ ] Monitoring on: `/healthz`, `/readyz`, `/metrics` (Prometheus), plus an alert when `spend_today.total_usd` crosses 80% of the cap.
- [ ] Runbooks read by the on-call human (`docs/OPERATIONS.md`).
- [ ] Backup admin user exists (MFA enabled) so one lost device does not lock the team out.

## 7. The switch

```bash
# 1. last readiness check
python -m app.cli doctor          # exit 0 and "READY" for the checks you own
# 2. flip the mode (admin only, needs an explicit confirmation flag)
curl -sX POST $BASE/api/v1/system/mode -H "authorization: Bearer $ADMIN_TOKEN" \
     -H 'content-type: application/json' -d '{"mode":"live","confirm_live":true}'
# 3. start with one campaign and a tiny cap
curl -sX POST $BASE/api/v1/campaigns/$ID/status -H "authorization: Bearer $ADMIN_TOKEN" \
     -H 'content-type: application/json' -d '{"action":"activate","reason":"first live campaign"}'
```

- [ ] First live campaign targets **≤ 20 prospects** with `review_first_n=20` and a human watching `/review` and `/replies` for two hours.
- [ ] After 24 hours: bounce < 2%, complaints 0, at least one reply. If not, stop and fix before scaling.
- [ ] Scale volume by raising `daily_cap` — never by raising the ramp in one jump.

Rollback is one request: `POST /api/v1/system/kill {"active": true, "reason": "rollback"}`.
