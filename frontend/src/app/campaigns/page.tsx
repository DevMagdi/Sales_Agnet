"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Badge, Banner, Button, Card, Table } from "@/components/ui";

type Campaign = {
  id: string;
  name: string;
  status: string;
  min_icp_score: number;
  daily_cap: number;
  industry_filter: string[];
  country_filter: string[];
  review_first_n: number;
};

export default function CampaignsPage() {
  const [items, setItems] = useState<Campaign[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [name, setName] = useState("");
  const [industry, setIndustry] = useState("packaging_printing");
  const [icp, setIcp] = useState(60);
  const [cap, setCap] = useState(20);
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api<{ items: Campaign[] }>("/api/v1/campaigns");
      setItems(data.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load campaigns");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/v1/campaigns", {
        method: "POST",
        body: {
          name,
          industry_filter: industry ? [industry] : [],
          country_filter: ["EG"],
          min_icp_score: icp,
          daily_cap: cap,
          send_windows: { weekdays: [6, 0, 1, 2, 3], start: "09:30", end: "15:00" },
          review_first_n: 1,
        },
      });
      setName("");
      setNotice("Campaign created in draft. Activate it when the ICP and windows look right.");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "create failed");
    }
  }

  async function act(id: string, action: string) {
    setError("");
    setNotice("");
    try {
      await api(`/api/v1/campaigns/${id}/status`, { method: "POST", body: { action, reason: "dashboard" } });
      setNotice(`Campaign ${action}d.`);
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "action failed");
    }
  }

  async function plan(id: string) {
    try {
      const result = await api<{ created: number; skipped: number; reasons: Record<string, number> }>(
        `/api/v1/campaigns/${id}/plan`,
        { method: "POST" },
      );
      setNotice(`Planned ${result.created} prospect(s); skipped ${result.skipped} (${JSON.stringify(result.reasons)}).`);
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "plan failed");
    }
  }

  async function showStats(id: string) {
    try {
      setStats(await api(`/api/v1/campaigns/${id}/stats`));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "stats failed");
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-navy">Campaigns</h1>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}

      <Card title="New campaign">
        <form onSubmit={create} className="grid gap-3 sm:grid-cols-5">
          <input
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Name (e.g. Egypt packaging Q1)"
            className="rounded border border-black/15 px-3 py-2 text-sm sm:col-span-2"
          />
          <input
            value={industry}
            onChange={(event) => setIndustry(event.target.value)}
            placeholder="industry code"
            className="rounded border border-black/15 px-3 py-2 text-sm"
          />
          <label className="flex items-center gap-2 text-sm text-muted">
            ICP ≥
            <input
              type="number"
              value={icp}
              min={0}
              max={100}
              onChange={(event) => setIcp(Number(event.target.value))}
              className="w-20 rounded border border-black/15 px-2 py-1"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-muted">
            cap
            <input
              type="number"
              value={cap}
              min={1}
              onChange={(event) => setCap(Number(event.target.value))}
              className="w-20 rounded border border-black/15 px-2 py-1"
            />
          </label>
          <div className="sm:col-span-5">
            <Button type="submit">Create campaign</Button>
          </div>
        </form>
      </Card>

      <Card title={`Campaigns (${items.length})`}>
        <Table head={["Name", "Status", "ICP", "Cap", "Industry", "Actions"]}>
          {items.map((campaign) => (
            <tr key={campaign.id} className="border-b border-black/5">
              <td className="px-2 py-2 font-medium">{campaign.name}</td>
              <td className="px-2 py-2">
                <Badge tone={campaign.status === "active" ? "ok" : campaign.status.includes("stopped") ? "error" : "neutral"}>
                  {campaign.status}
                </Badge>
              </td>
              <td className="px-2 py-2">{campaign.min_icp_score}</td>
              <td className="px-2 py-2">{campaign.daily_cap}</td>
              <td className="px-2 py-2 text-muted">{campaign.industry_filter.join(", ") || "all"}</td>
              <td className="flex flex-wrap gap-1 px-2 py-2">
                <Button tone="ghost" onClick={() => plan(campaign.id)}>
                  Plan
                </Button>
                <Button tone="ghost" onClick={() => showStats(campaign.id)}>
                  Stats
                </Button>
                {campaign.status === "active" ? (
                  <Button tone="ghost" onClick={() => act(campaign.id, "pause")}>
                    Pause
                  </Button>
                ) : (
                  <Button onClick={() => act(campaign.id, "activate")}>Activate</Button>
                )}
              </td>
            </tr>
          ))}
        </Table>
        {items.length === 0 ? <p className="text-sm text-muted">No campaigns yet.</p> : null}
      </Card>

      {stats ? (
        <Card title="Campaign stats">
          <pre className="overflow-x-auto rounded bg-black/5 p-3 text-xs">{JSON.stringify(stats, null, 2)}</pre>
        </Card>
      ) : null}
    </div>
  );
}
