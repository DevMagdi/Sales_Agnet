"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Badge, Banner, Button, Card } from "@/components/ui";

type Message = {
  id: string;
  company_name?: string;
  subject: string;
  body_text: string;
  status: string;
  blocked_reason?: string | null;
  step: string;
  locale: string;
  check_report?: { passed?: boolean; checks?: Array<{ rule: string; passed: boolean; reason: string }> };
};

export default function ReviewPage() {
  const [items, setItems] = useState<Message[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api<{ items: Message[] }>("/api/v1/messages/review");
      setItems(data.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load the review queue");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function review(id: string, action: "approve" | "reject") {
    setBusy(id);
    setError("");
    setNotice("");
    try {
      const result = await api<{ status?: string; blocked_reason?: string }>(`/api/v1/messages/${id}/review`, {
        method: "POST",
        body: { action, reason: "dashboard" },
      });
      setNotice(
        action === "approve"
          ? result.blocked_reason
            ? `Approved but the gate still blocks it: ${result.blocked_reason}`
            : "Approved and queued. The gate runs again immediately before sending."
          : "Rejected.",
      );
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "review failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">Review queue</h1>
      <Banner tone="info">
        Approval never bypasses the gate: it re-runs every rule right before the message is queued.
      </Banner>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}
      {items.length === 0 ? <Card><p className="text-sm text-muted">Nothing waiting for review.</p></Card> : null}
      {items.map((message) => (
        <Card
          key={message.id}
          title={`${message.company_name || "Prospect"} · ${message.step} · ${message.locale}`}
          actions={
            <div className="flex gap-2">
              <Button tone="ghost" disabled={busy === message.id} onClick={() => review(message.id, "reject")}>
                Reject
              </Button>
              <Button disabled={busy === message.id} onClick={() => review(message.id, "approve")}>
                Approve
              </Button>
            </div>
          }
        >
          <p className="mb-2 font-medium">{message.subject}</p>
          <pre dir={message.locale === "ar" ? "rtl" : "ltr"} className="whitespace-pre-wrap rounded bg-black/5 p-3 text-sm">
            {message.body_text}
          </pre>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={message.status === "needs_review" ? "warn" : "neutral"}>{message.status}</Badge>
            {message.check_report?.passed ? <Badge tone="ok">gate passed</Badge> : <Badge tone="error">gate blocked</Badge>}
            {message.blocked_reason ? <span className="text-red-700">{message.blocked_reason}</span> : null}
          </div>
          {message.check_report?.checks ? (
            <details className="mt-2 text-xs text-muted">
              <summary className="cursor-pointer">Gate checks ({message.check_report.checks.length})</summary>
              <ul className="mt-1 space-y-0.5">
                {message.check_report.checks.map((check) => (
                  <li key={check.rule}>
                    {check.passed ? "✓" : "✕"} {check.rule} {check.reason ? `— ${check.reason}` : ""}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </Card>
      ))}
    </div>
  );
}
