#!/usr/bin/env node
/**
 * Dashboard contract check: replays exactly the calls the dashboard makes — through the Next.js
 * origin, so the rewrite proxy is exercised too — and asserts the fields each page reads exist.
 *
 *   # terminal 1
 *   uvicorn app.main:app --port 8000          # backend, seeded: python -m app.cli seed --demo
 *   # terminal 2
 *   cd frontend && npm run dev                 # http://127.0.0.1:3000
 *   # terminal 3
 *   node scripts/verify_dashboard.mjs
 *
 * Env: BASE (default http://127.0.0.1:3000), DASH_EMAIL, DASH_PASSWORD.
 * Exit code 0 == every page's data contract holds.
 */
const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const EMAIL = process.env.DASH_EMAIL ?? "admin@mantiq.example";
const PASSWORD = process.env.DASH_PASSWORD ?? "admin-password-123";

let token = "";
let failures = 0;

async function call(path, { method = "GET", body, query } = {}) {
  const url = new URL(path, BASE);
  for (const [key, value] of Object.entries(query ?? {})) if (value != null) url.searchParams.set(key, String(value));
  const res = await fetch(url, {
    method,
    headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text.slice(0, 200)}`);
  return data;
}

function check(name, ok, detail = "") {
  if (ok) console.log(`  ok   ${name}`);
  else { failures += 1; console.log(`  FAIL ${name} ${detail}`); }
}

function fields(name, obj, keys) {
  const missing = keys.filter((k) => obj == null || obj[k] === undefined);
  check(name, missing.length === 0, missing.length ? `missing: ${missing.join(", ")}` : "");
}

const login = await call("/api/v1/auth/login", { method: "POST", body: { email: EMAIL, password: PASSWORD } });
token = login.access_token ?? login.token;
check("login returns a token", Boolean(token));
fields("login payload", login, ["access_token"]);

// --- Overview page -------------------------------------------------------
const kpis = await call("/api/v1/reports/kpis");
fields("kpis", kpis, ["sent", "replies", "replies_positive", "bounce_rate", "complaint_rate", "unsubscribe_rate", "demos"]);
const status = await call("/api/v1/system/status");
fields("system status", status, ["mode", "kill_switch", "spend_today", "limits", "due"]);
fields("system status.due", status.due, ["due_prospects", "due_messages"]);
fields("system status.spend_today", status.spend_today, ["llm_usd", "api_usd", "total_usd"]);
const events = await call("/api/v1/events", { query: { limit: 10 } });
check("events list", Array.isArray(events?.items), JSON.stringify(events).slice(0, 120));
fields("event row", events.items[0] ?? {}, ["seq", "kind", "entity_type", "at"]);

// --- Campaigns -----------------------------------------------------------
const campaigns = await call("/api/v1/campaigns");
check("campaigns list", Array.isArray(campaigns?.items) && campaigns.items.length > 0);
const campaign = campaigns.items[0];
fields("campaign row", campaign, ["id", "name", "status"]);
const stats = await call(`/api/v1/campaigns/${campaign.id}/stats`);
check("campaign stats object", typeof stats === "object" && stats !== null);
const prospects = await call(`/api/v1/campaigns/${campaign.id}/prospects`);
check("campaign prospects list", Array.isArray(prospects?.items));
const capacity = await call(`/api/v1/campaigns/${campaign.id}/capacity`);
check("campaign capacity object", typeof capacity === "object" && capacity !== null);

// --- Review queue --------------------------------------------------------
let review = await call("/api/v1/messages/review");
if (!review.items?.length) {
  // The queue is empty (already approved on a previous run): ask the agent for more work first.
  await call("/api/v1/messages/compose-due", { method: "POST", body: {} }).catch(() => null);
  review = await call("/api/v1/messages/review");
}
if (review.items?.length) {
  const reviewMessage = review.items[0];
  fields("review row", reviewMessage, ["id", "subject", "status"]);
  const message = await call(`/api/v1/messages/${reviewMessage.id}`);
  check("message detail", Boolean(message?.id));
  const approved = await call(`/api/v1/messages/${reviewMessage.id}/review`, {
    method: "POST",
    body: { action: "approve" },
  });
  check("approve review action", Boolean(approved));
  const after = await call("/api/v1/messages/review");
  check("approved message left the queue", !after.items.some((row) => row.id === reviewMessage.id));
} else {
  console.log("  skip review queue (nothing due to compose in this data set)");
}

// --- Replies / tasks / demos -------------------------------------------
const replies = await call("/api/v1/replies");
check("replies list", Array.isArray(replies?.items) && replies.items.length > 0);
const reply = replies.items[0];
fields("reply row", reply, ["id", "classification", "confidence", "status", "company_name"]);
const replyDetail = await call(`/api/v1/replies/${reply.id}`);
fields("reply detail", replyDetail ?? {}, ["reply", "inbound", "company", "contact", "thread"]);
check("reply thread rows", Array.isArray(replyDetail.thread) && replyDetail.thread.length > 0 && replyDetail.thread[0].direction !== undefined);
const handoff = await call(`/api/v1/replies/${reply.id}/handoff`, { method: "POST", body: {} });
check("reply handoff", typeof handoff === "object");
const tasks = await call("/api/v1/tasks");
check("tasks list", Array.isArray(tasks?.items));
if (tasks.items.length) {
  fields("task row", tasks.items[0], ["id", "title", "status"]);
  const patched = await call(`/api/v1/tasks/${tasks.items[0].id}`, { method: "PATCH", body: { status: "done" } });
  check("task patched", Boolean(patched));
}
const demos = await call("/api/v1/demos");
check("demos list", Array.isArray(demos?.items) && demos.items.length > 0);
fields("demo row", demos.items[0], ["id", "status"]);

// --- Suppression ---------------------------------------------------------
const suppression = await call("/api/v1/suppression");
check("suppression list", Array.isArray(suppression?.items));
check("suppression counts", typeof suppression?.counts === "object");
const suppressionCheck = await call("/api/v1/suppression/check", { query: { email: "nobody@example.com" } });
check("suppression check", suppressionCheck !== null);
const added = await call("/api/v1/suppression", {
  method: "POST",
  body: { kind: "email", value: "unsubscribe-me@example.com", reason: "manual", note: "dashboard verify" },
});
check("suppression add", Boolean(added?.entry?.id) && typeof added?.created === "boolean");

// --- Settings ------------------------------------------------------------
const mailboxes = await call("/api/v1/settings/mailboxes");
check("mailboxes list", Array.isArray(mailboxes?.items) && mailboxes.items.length > 0);
fields("mailbox row", mailboxes.items[0] ?? {}, ["id", "from_email", "from_name", "status", "daily_cap", "warmup_stage", "provider"]);
const users = await call("/api/v1/settings/users");
check("users list", Array.isArray(users?.items) && users.items.length > 0);
fields("user row", users.items[0] ?? {}, ["id", "email", "role", "is_active", "mfa_enabled"]);
const templates = await call("/api/v1/templates");
check("templates list", Array.isArray(templates?.items) && templates.items.length > 0);
const stamp = Date.now();
const newMailbox = await call("/api/v1/settings/mailboxes", {
  method: "POST",
  body: {
    from_email: `verify-${stamp}@mantiq.example`,
    from_name: "Second Sender",
    daily_cap: 15,
    smtp_host: "smtp.example.com",
    smtp_username: "second",
    smtp_password: "secret-secret",
    imap_host: "imap.example.com",
    imap_username: "second",
    imap_password: "secret-secret",
  },
});
check("mailbox created", Boolean(newMailbox?.id));
const duplicate = await fetch(new URL("/api/v1/settings/mailboxes", BASE), {
  method: "POST",
  headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
  body: JSON.stringify({ from_email: `verify-${stamp}@mantiq.example`, from_name: "Dup" }),
});
check("duplicate mailbox is a 409, not a 500", duplicate.status === 409, `got ${duplicate.status}`);
check("mailbox secret is masked", !JSON.stringify(newMailbox).includes("secret-secret"));
const paused = await call(`/api/v1/settings/mailboxes/${newMailbox.id}/status`, { method: "POST", body: { status: "paused" } });
check("mailbox paused", paused?.status === "paused");

// --- System --------------------------------------------------------------
const adapters = await call("/api/v1/system/adapters");
check("adapters report", Object.keys(adapters ?? {}).length > 0);
fields("adapter row", Object.values(adapters)[0] ?? {}, ["name", "is_fake", "ok", "detail", "status"]);
const doctor = await call("/api/v1/system/doctor");
check("doctor checks", Array.isArray(doctor?.checks) && doctor.checks.length > 0);
fields("doctor check", doctor.checks[0] ?? {}, ["name", "ok", "detail"]);
const audit = await call("/api/v1/system/audit", { query: { limit: 20 } });
check("audit list", Array.isArray(audit?.items) && audit.items.length > 0);
fields("audit row", audit.items[0] ?? {}, ["id", "action", "created_at"]);
const breakers = await call("/api/v1/system/breakers/evaluate", { method: "POST", body: {} });
check("breakers evaluated", typeof breakers?.tripped === "boolean" && typeof breakers?.rates === "object");
const budgets = await call("/api/v1/system/budgets", { method: "POST", body: { llm_daily_usd: 6, api_daily_usd: 4 } });
check("budgets saved", budgets?.llm === 6 && budgets?.api === 4);
const killed = await call("/api/v1/system/kill", { method: "POST", body: { active: true, reason: "dashboard verify" } });
check("kill switch on", killed?.kill_switch === true);
const cleared = await call("/api/v1/system/kill", { method: "POST", body: { active: false } });
check("kill switch off", cleared?.kill_switch === false);
const resumed = await call("/api/v1/system/resume", { method: "POST", body: {} });
check("resume ok", typeof resumed === "object");
const mode = await call("/api/v1/system/mode", { method: "POST", body: { mode: "dry_run" } });
check("mode switch", mode?.mode === "dry_run");

// --- Page rendering (Next.js routes) ------------------------------------
for (const page of ["/", "/login", "/campaigns", "/review", "/replies", "/tasks", "/suppression", "/settings", "/system"]) {
  const res = await fetch(new URL(page, BASE));
  check(`page ${page} renders`, res.ok, `status ${res.status}`);
  const html = await res.text();
  if (page === "/") check("overview page has heading", /Overview|KPI|Mantiq/i.test(html));
}

console.log(`\n${failures === 0 ? "DASHBOARD CONTRACT OK" : `${failures} FAILURE(S)`}`);
process.exit(failures === 0 ? 0 : 1);
