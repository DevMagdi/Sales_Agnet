# Operations runbooks

Every action below is also available in the dashboard (`/system`, `/suppression`, `/review`) and is
written to `audit_log` with the actor, the reason and the client IP.

## Stop the agent — immediately

**Kill switch** (stops *everything*, pauses every active campaign, alerts staff):

```bash
python -m app.cli kill --reason "reply from legal"
# or: POST /ai/v1 ... see below — dashboard: System → "Engage kill switch"
curl -sX POST http://127.0.0.1:8000/api/v1/system/kill \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"active": true, "reason": "reply from legal"}'
```

Result: gate check #1 fails for every message, all `active` campaigns flip to `paused` with
`stopped_reason="kill_switch by <actor>"`. Queued messages stay queued — nothing is lost.

To restart: clear the switch (`{"active": false}`) **and** re-activate the campaigns you want
(`POST /api/v1/campaigns/{id}/status {"action":"activate"}`). Pausing does not auto-resume by design.

**Pause one campaign**: `POST /api/v1/campaigns/{id}/status {"action":"pause","reason":"…"}`.
**Pause one mailbox**: `POST /api/v1/settings/mailboxes/{id}/status {"status":"paused"}` — its
remaining capacity becomes 0 immediately.

## A circuit breaker tripped

Symptom: `/system/status` shows `global_stop` with a reason, or a campaign is
`stopped_by_circuit_breaker`; staff got a notification; the dashboard banner is red.

1. Read the evidence: `GET /api/v1/reports/deliverability`, `GET /api/v1/system/audit?action=breakers_tripped`,
   and the campaign's messages (`GET /api/v1/campaigns/{id}/messages`). Rates need ≥30 sends in a 24h
   window, so a trip is never a one-off.
2. Fix the cause — the usual suspects are a stale address list (hard bounces), a missing
   `List-Unsubscribe` (complaints), an expired SMTP/IMAP credential (auth errors), or a budget.
3. Resume deliberately (admin only): `POST /api/v1/system/resume` (dashboard: System → "Resume after
   breaker"). This clears the global stop *and* the kill switch, and re-activates the campaigns the
   breaker paused. Then re-run `python -m app.cli doctor` before letting the worker send again.
4. Re-check rates with `POST /api/v1/system/breakers/evaluate` before walking away — if it trips
   again immediately, the data is still bad; keep campaigns paused and fix the source list.

## Someone asked to stop receiving email

Any of these is sufficient and cannot be undone by re-importing the address:

- they click the one-click link (`POST /u/{token}`) — RFC 8058, no confirmation dialog;
- they reply with "unsubscribe / stop / اوقف / مش مهتم" — the classifier flags the opt-out and the
  reply handler suppresses every channel for that company, then transitions the prospect to
  `do_not_contact`;
- they complain — the complaint webhook suppresses *and* feeds the complaint-rate breaker;
- a human adds it in the dashboard.

Manual entry: `POST /api/v1/suppression {"kind":"email|domain|phone","value":"…","reason":"manual|legal|competitor|complaint","note":"…"}`.
Check before contacting: `GET /api/v1/suppression/check?email=…`. Only a blind index is stored — the
raw value is discarded (`value_stored: false`), so a leak of the table reveals nothing.

Un-suppressing is deliberately awkward: it requires an admin SQL/CLI action (`suppression.remove` is
not exposed over HTTP) and is itself audited. Treat "we removed a suppression" as an incident.

## Erasure request (GDPR-style)

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/companies/$COMPANY_ID/erase -H "authorization: Bearer $ADMIN_TOKEN"
```

Erases the company's PII fields, its contacts' emails/phones, cached page text and research snapshots,
keeps the audit trail, and adds the addresses to the suppression list so the crawler never re-imports
them. `GET /api/v1/companies/{id}/export` produces the machine-readable copy for the data subject first.

## Budget exhaustion

`/system/status` → `spend_today` vs `limits`. The gate's `budget` check blocks sends and the breakers
stop sending when either cap is hit; both reset at local midnight. Raise the cap in the dashboard
(System → budgets) or `POST /api/v1/system/budgets {"llm_daily_usd":…,"api_daily_usd":…}`. A budget
trip is *not* a data problem — raising it and resuming is fine.

## Mailbox warm-up

New mailboxes start at `warmup_stage=0` with a low `daily_cap`; `outreach.warmup` (daily) advances the
stage and the ramp schedule on the campaign scales volume. To force a stage:
`python -m app.cli warmup --advance`. Never raise a cold mailbox to 200/day: that is how a domain gets
blacklisted.

## Missing replies

Replies arrive by webhook (`POST /api/v1/webhooks/email`, signature-checked, deduped in
`webhook_deliveries`) or IMAP polling. If the dashboard inbox looks stale:

1. `python -m app.cli poll-imap` — prints how many messages were ingested.
2. Check the mailbox is `active` and has IMAP credentials (`POST /api/v1/settings/mailboxes/{id}/test`).
3. `GET /api/v1/system/health` and the worker logs; without Redis the beat schedule does nothing and
   polling is manual.
4. Replies that cannot be threaded show as unlinked — the classifier still stores them and a human can
   hand them off from `/replies`.

## Backups and restore

`audit_log` and `suppression_list` are append-only at the database level. Nightly logical backup:

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  pg_dump -U mantiq -Fc mantiq > mantiq-$(date +%F).dump
```

Restore into an empty database, then run `alembic upgrade head` (idempotent). Keep
`ENCRYPTION_MASTER_KEY` with the backup: without it the PII columns are unreadable, and suppression
lookups (blind index) use the same key.

## Routine checks (weekly)

- `python scripts/doctor.py` — DNS/SPF/DKIM/DMARC, providers, kill switch, mailbox health.
- `GET /api/v1/reports/deliverability` — bounce/complaint/unsubscribe rates per domain.
- `GET /api/v1/reports/costs` and `/reports/daily` — cost per demo, per reply.
- `GET /api/v1/system/audit?action=kill_switch_changed` — nobody flipped a switch without a reason.
- Suppression list growth vs. reply quality: a spike means the list is stale, not that the copy is bad.
