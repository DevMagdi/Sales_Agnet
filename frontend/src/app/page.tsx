"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { pct, usd, when } from "@/lib/format";
import { Badge, Banner, Card, Stat } from "@/components/ui";

type Kpis = Record<string, number>;
type Status = {
  mode: string;
  app_env: string;
  kill_switch: boolean;
  global_stop: { reason?: string } | null;
  spend_today: { llm_usd: number; api_usd: number; total_usd: number };
  limits: { llm: number; api: number };
  due: { due_prospects?: number; due_messages?: number };
};

export default function OverviewPage() {
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [feed, setFeed] = useState<Array<Record<string, unknown>>>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [k, s, f] = await Promise.all([
          api<Kpis>("/api/v1/reports/kpis"),
          api<Status>("/api/v1/system/status"),
          api<{ items: Array<Record<string, unknown>> }>("/api/v1/events", { query: { limit: 12 } }),
        ]);
        if (cancelled) return;
        setKpis(k);
        setStatus(s);
        setFeed(f.items || []);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : "failed to load");
      }
    }
    load();
    const timer = setInterval(load, 20000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-navy">Overview</h1>
        {status ? (
          <div className="flex items-center gap-2 text-sm">
            <Badge tone={status.mode === "live" ? "ok" : status.mode === "sandbox" ? "warn" : "neutral"}>
              mode: {status.mode}
            </Badge>
            <Badge tone={status.kill_switch ? "error" : "ok"}>
              kill switch: {status.kill_switch ? "ENGAGED" : "off"}
            </Badge>
            {status.global_stop ? <Badge tone="error">global stop: {status.global_stop.reason || "yes"}</Badge> : null}
          </div>
        ) : null}
      </div>

      {error ? <Banner tone="error">{error}</Banner> : null}
      {status?.kill_switch ? (
        <Banner tone="error">All sending is stopped. Clear it in System once the cause is understood.</Banner>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Sent" value={kpis?.sent ?? "—"} hint="all time" />
        <Stat label="Replies" value={kpis?.replies ?? "—"} hint={`${pct(kpis?.reply_rate)} positive`} />
        <Stat label="Bounce rate" value={pct(kpis?.bounce_rate)} hint="24h window" />
        <Stat label="Spend today" value={usd(status?.spend_today?.total_usd)} hint={`LLM cap ${usd(status?.limits?.llm)}`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Queue">
          <ul className="space-y-1 text-sm">
            <li className="flex justify-between">
              <span>Prospects due</span>
              <span className="font-medium">{status?.due?.due_prospects ?? 0}</span>
            </li>
            <li className="flex justify-between">
              <span>Messages queued</span>
              <span className="font-medium">{status?.due?.due_messages ?? 0}</span>
            </li>
            <li className="flex justify-between">
              <span>Complaint rate</span>
              <span className="font-medium">{pct(kpis?.complaint_rate)}</span>
            </li>
            <li className="flex justify-between">
              <span>Demos booked</span>
              <span className="font-medium">{kpis?.demos ?? 0}</span>
            </li>
          </ul>
          <div className="mt-3 flex gap-2 text-sm">
            <Link className="rounded bg-navy px-3 py-1.5 text-white" href="/review">
              Open review queue
            </Link>
            <Link className="rounded border border-black/15 px-3 py-1.5" href="/campaigns">
              Campaigns
            </Link>
          </div>
        </Card>
        <Card title="Live feed">
          {feed.length === 0 ? (
            <p className="text-sm text-muted">No events yet. Actions you take here are recorded in the audit trail.</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {feed.map((item, index) => (
                <li key={`${String(item.seq ?? index)}-${index}`} className="flex items-start justify-between gap-3 border-b border-black/5 pb-2">
                  <span>
                    <span className="font-medium">{String(item.kind ?? item.event ?? "event")}</span>
                    <span className="text-muted"> · {String(item.entity_type ?? "")}</span>
                  </span>
                  <span className="whitespace-nowrap text-xs text-muted">{when(item.at ?? item.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
