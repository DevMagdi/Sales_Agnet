"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { titleCase, usd, when } from "@/lib/format";
import { Badge, Banner, Button, Card, Stat, Table } from "@/components/ui";

type Check = { name: string; ok: boolean; detail: string; blocking: boolean };
type Adapter = { name: string; is_fake: boolean; ok: boolean; detail: string; status: string };

export default function SystemPage() {
  const [status, setStatus] = useState<Record<string, any> | null>(null);
  const [adapters, setAdapters] = useState<Record<string, Adapter>>({});
  const [doctor, setDoctor] = useState<{ ok: boolean; checks: Check[] } | null>(null);
  const [audit, setAudit] = useState<Array<Record<string, unknown>>>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [budgets, setBudgets] = useState({ llm_daily_usd: 5, api_daily_usd: 5 });
  const [breakers, setBreakers] = useState<Record<string, any> | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, a, d, auditRows] = await Promise.all([
        api<Record<string, any>>("/api/v1/system/status"),
        api<Record<string, Adapter>>("/api/v1/system/adapters"),
        api<{ ok: boolean; checks: Check[] }>("/api/v1/system/doctor"),
        api<{ items: Array<Record<string, unknown>> }>("/api/v1/system/audit", { query: { limit: 20 } }),
      ]);
      setStatus(s);
      setAdapters(a);
      setDoctor(d);
      setAudit(auditRows.items || []);
      setBudgets({ llm_daily_usd: Number(s.limits?.llm ?? 5), api_daily_usd: Number(s.limits?.api ?? 5) });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load system status");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function kill(active: boolean) {
    try {
      await api("/api/v1/system/kill", { method: "POST", body: { active, reason: "dashboard" } });
      setNotice(active ? "Kill switch engaged. All sending stopped." : "Kill switch cleared.");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "action failed");
    }
  }

  async function evaluateBreakers() {
    try {
      const verdict = await api<Record<string, any>>("/api/v1/system/breakers/evaluate", {
        method: "POST",
        body: {},
      });
      setBreakers(verdict);
      setNotice(verdict.tripped ? `Breaker tripped: ${String(verdict.tripped)}` : "Breakers evaluated — nothing tripped.");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not evaluate breakers");
    }
  }

  async function resume() {
    try {
      await api("/api/v1/system/resume", { method: "POST", body: {} });
      setNotice("Global stop cleared and campaigns resumed after the circuit breaker.");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "resume failed");
    }
  }

  async function switchMode(mode: string) {
    try {
      await api("/api/v1/system/mode", { method: "POST", body: { mode, confirm_live: mode === "live" } });
      setNotice(`Mode switched to ${mode}.`);
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "mode change refused");
    }
  }

  async function saveBudgets(event: React.FormEvent) {
    event.preventDefault();
    try {
      await api("/api/v1/system/budgets", { method: "POST", body: budgets });
      setNotice("Daily budgets updated.");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not save budgets");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">System</h1>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Mode" value={String(status?.mode ?? "—")} hint={`env default ${status?.env_mode ?? "—"}`} />
        <Stat label="Kill switch" value={status?.kill_switch ? "ENGAGED" : "off"} />
        <Stat label="Spend today" value={usd(status?.spend_today?.total_usd)} hint={`LLM ${usd(status?.limits?.llm)}`} />
        <Stat label="Due" value={`${status?.due?.due_prospects ?? 0} prospects`} hint={`${status?.due?.due_messages ?? 0} messages queued`} />
      </div>

      <Card title="Controls">
        <div className="flex flex-wrap gap-2">
          <Button tone="danger" onClick={() => kill(true)}>
            Engage kill switch
          </Button>
          <Button tone="ghost" onClick={() => kill(false)}>
            Clear kill switch
          </Button>
          <Button tone="ghost" onClick={resume}>
            Resume after breaker
          </Button>
          <Button tone="ghost" onClick={() => switchMode("dry_run")}>
            Mode: dry run
          </Button>
          <Button tone="ghost" onClick={() => switchMode("sandbox")}>
            Mode: sandbox
          </Button>
          <Button tone="ghost" onClick={() => switchMode("live")}>
            Mode: live
          </Button>
          <Button tone="ghost" onClick={evaluateBreakers}>
            Evaluate breakers
          </Button>
          <Button tone="ghost" onClick={load}>
            Refresh
          </Button>
        </div>
        <form onSubmit={saveBudgets} className="mt-3 flex flex-wrap items-end gap-2 text-sm">
          <label className="text-muted">
            LLM budget
            <input
              type="number"
              step="0.5"
              value={budgets.llm_daily_usd}
              onChange={(event) => setBudgets({ ...budgets, llm_daily_usd: Number(event.target.value) })}
              className="ml-2 w-24 rounded border border-black/15 px-2 py-1"
            />
          </label>
          <label className="text-muted">
            API budget
            <input
              type="number"
              step="0.5"
              value={budgets.api_daily_usd}
              onChange={(event) => setBudgets({ ...budgets, api_daily_usd: Number(event.target.value) })}
              className="ml-2 w-24 rounded border border-black/15 px-2 py-1"
            />
          </label>
          <Button type="submit">Save budgets</Button>
        </form>
      </Card>

      {breakers ? (
        <Card title="Last breaker evaluation">
          <p className="mb-2 text-sm">
            {breakers.tripped ? <Badge tone="error">tripped: {String(breakers.tripped)}</Badge> : <Badge tone="ok">clear</Badge>}
          </p>
          <Table head={["Rate", "Value", "Threshold"]}>
            {Object.entries((breakers.rates || {}) as Record<string, Record<string, number>>).flatMap(([scope, values]) =>
              Object.entries(values).map(([name, value]) => (
                <tr key={`${scope}-${name}`} className="border-b border-black/5">
                  <td className="px-2 py-1.5 font-mono text-xs">
                    {scope}.{name}
                  </td>
                  <td className="px-2 py-1.5">{value}</td>
                  <td className="px-2 py-1.5 text-muted">{String(breakers.thresholds?.[name] ?? "—")}</td>
                </tr>
              )),
            )}
          </Table>
        </Card>
      ) : null}

      <Card title="Readiness doctor">
        {doctor ? (
          <>
            <p className="mb-2 text-sm">
              {doctor.ok ? <Badge tone="ok">ready</Badge> : <Badge tone="error">not ready for live sending</Badge>}
            </p>
            <Table head={["Check", "Result", "Detail"]}>
              {doctor.checks.map((check) => (
                <tr key={check.name} className="border-b border-black/5">
                  <td className="px-2 py-1.5 font-mono text-xs">{check.name}</td>
                  <td className="px-2 py-1.5">
                    <Badge tone={check.ok ? "ok" : check.blocking ? "error" : "warn"}>
                      {check.ok ? "pass" : check.blocking ? "fail" : "warn"}
                    </Badge>
                  </td>
                  <td className="px-2 py-1.5 text-muted">{check.detail}</td>
                </tr>
              ))}
            </Table>
          </>
        ) : (
          <p className="text-sm text-muted">Loading checks…</p>
        )}
      </Card>

      <Card title="Adapters">
        <Table head={["Adapter", "Implementation", "Status", "Detail"]}>
          {Object.entries(adapters).map(([key, adapter]) => (
            <tr key={key} className="border-b border-black/5">
              <td className="px-2 py-1.5">{key}</td>
              <td className="px-2 py-1.5 font-mono text-xs">{adapter.name}</td>
              <td className="px-2 py-1.5">
                <Badge tone={adapter.is_fake ? "warn" : adapter.ok ? "ok" : "error"}>{adapter.status}</Badge>
              </td>
              <td className="px-2 py-1.5 text-muted">{adapter.detail}</td>
            </tr>
          ))}
        </Table>
      </Card>

      <Card title="Audit trail (latest 20)">
        <Table head={["Action", "Entity", "Actor", "When"]}>
          {audit.map((entry, index) => (
            <tr key={`${String(entry.id)}-${index}`} className="border-b border-black/5">
              <td className="px-2 py-1.5">{titleCase(String(entry.action ?? ""))}</td>
              <td className="px-2 py-1.5 text-muted">{String(entry.entity_type ?? "")}</td>
              <td className="px-2 py-1.5 text-muted">{String(entry.actor_label ?? "system")}</td>
              <td className="px-2 py-1.5 text-muted">{when(entry.created_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
