"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { Banner, Button, Card, Table } from "@/components/ui";

type Entry = { id: string; kind: string; value_hash: string; reason: string; created_at?: string };

export default function SuppressionPage() {
  const [items, setItems] = useState<Entry[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [kind, setKind] = useState("email");
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("manual");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checkValue, setCheckValue] = useState("");
  const [checkResult, setCheckResult] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const data = await api<{ items: Entry[]; counts: Record<string, number> }>("/api/v1/suppression");
      setItems(data.items);
      setCounts(data.counts || {});
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load the suppression list");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    try {
      const result = await api<{ created: boolean }>("/api/v1/suppression", {
        method: "POST",
        body: { kind, value, reason },
      });
      setNotice(result.created ? "Added. The raw value is discarded; only the hash is stored." : "Already suppressed.");
      setValue("");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not add");
    }
  }

  async function check() {
    try {
      const result = await api<{ suppressed: boolean; kind?: string; reason?: string }>("/api/v1/suppression/check", {
        query: { email: checkValue },
      });
      setCheckResult(
        result.suppressed ? `Suppressed (${result.kind}: ${result.reason})` : "Not suppressed — this address can be contacted.",
      );
    } catch (caught) {
      setCheckResult(caught instanceof Error ? caught.message : "check failed");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">Suppression list</h1>
      <Banner tone="warn">
        This list is absolute and append-only. The database stores only a keyed hash of the address, never the address itself.
      </Banner>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Add an entry">
          <form onSubmit={add} className="grid gap-2">
            <select value={kind} onChange={(event) => setKind(event.target.value)} className="rounded border border-black/15 px-3 py-2 text-sm">
              <option value="email">email</option>
              <option value="domain">domain (covers sub-domains)</option>
              <option value="phone">phone</option>
            </select>
            <input
              required
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={kind === "domain" ? "factory.example" : kind === "phone" ? "+201234567890" : "name@factory.example"}
              className="rounded border border-black/15 px-3 py-2 text-sm"
            />
            <select value={reason} onChange={(event) => setReason(event.target.value)} className="rounded border border-black/15 px-3 py-2 text-sm">
              <option value="manual">manual</option>
              <option value="legal">legal</option>
              <option value="competitor">competitor</option>
              <option value="complaint">complaint</option>
            </select>
            <Button type="submit">Suppress</Button>
          </form>
        </Card>
        <Card title="Check an address">
          <div className="flex gap-2">
            <input
              value={checkValue}
              onChange={(event) => setCheckValue(event.target.value)}
              placeholder="name@factory.example"
              className="flex-1 rounded border border-black/15 px-3 py-2 text-sm"
            />
            <Button tone="ghost" onClick={check}>
              Check
            </Button>
          </div>
          {checkResult ? <p className="mt-2 text-sm text-muted">{checkResult}</p> : null}
          <p className="mt-3 text-xs text-muted">
            Counts: {Object.entries(counts).map(([key, count]) => `${key} ${count}`).join(" · ") || "empty"}
          </p>
        </Card>
      </div>

      <Card title={`Entries (${items.length})`}>
        <Table head={["Kind", "Hash", "Reason", "Added"]}>
          {items.map((entry) => (
            <tr key={entry.id} className="border-b border-black/5">
              <td className="px-2 py-2">{entry.kind}</td>
              <td className="px-2 py-2 font-mono text-xs text-muted">{entry.value_hash}</td>
              <td className="px-2 py-2">{entry.reason}</td>
              <td className="px-2 py-2 text-muted">{when(entry.created_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
