"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { titleCase, when } from "@/lib/format";
import { Badge, Banner, Button, Card, Table } from "@/components/ui";

type Reply = {
  id: string;
  company_name?: string;
  classification: string;
  confidence: number;
  status: string;
  body?: string;
  opt_out_flagged?: boolean;
  created_at?: string;
};

type ThreadItem = {
  id: string;
  direction: "outbound" | "inbound";
  at: string;
  subject: string;
  text: string;
  status?: string;
  step?: string;
};

type ReplyDetail = {
  reply: Reply;
  inbound: { subject: string; from: string; received_at: string | null; headers: Record<string, string> };
  company: { id: string; name: string; industry: string | null } | null;
  contact: { full_name?: string; role_title?: string; email_masked?: string } | null;
  thread: ThreadItem[];
};

const CLASSIFICATIONS = [
  "interested",
  "wants_info",
  "meeting_proposal",
  "referral",
  "not_now",
  "not_interested",
  "unsubscribe",
  "out_of_office",
  "auto_reply",
  "bounce",
  "complaint",
  "question_technical",
  "question_pricing",
  "unclear",
];

export default function RepliesPage() {
  const [items, setItems] = useState<Reply[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<ReplyDetail | null>(null);
  const [draft, setDraft] = useState({ subject: "", body_text: "" });
  const [sendResult, setSendResult] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api<{ items: Reply[] }>("/api/v1/replies", { query: { limit: 50 } });
      setItems(data.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load replies");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function open(id: string) {
    setError("");
    setSendResult(null);
    try {
      const data = await api<ReplyDetail>(`/api/v1/replies/${id}`);
      setDetail(data);
      const lastInbound = [...(data.thread || [])].reverse().find((row) => row.direction === "inbound");
      const subject = data.inbound?.subject || lastInbound?.subject || "";
      setDraft({ subject: subject.startsWith("Re:") ? subject : `Re: ${subject}`.trim(), body_text: "" });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load the reply");
    }
  }

  async function handoff() {
    if (!detail) return;
    try {
      await api(`/api/v1/replies/${detail.reply.id}/handoff`, { method: "POST", body: {} });
      setNotice("Handed to a human with the full brief and thread.");
      load();
      open(detail.reply.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "hand-off failed");
    }
  }

  async function override(classification: string) {
    if (!detail) return;
    try {
      await api(`/api/v1/replies/${detail.reply.id}/override`, {
        method: "POST",
        body: { classification, reason: "dashboard override" },
      });
      setNotice(`Classification set to ${classification}.`);
      load();
      open(detail.reply.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "override failed");
    }
  }

  async function send() {
    if (!detail) return;
    setError("");
    try {
      const result = await api<Record<string, unknown>>(`/api/v1/replies/${detail.reply.id}/send`, {
        method: "POST",
        body: draft,
      });
      setSendResult(result);
      setNotice(
        result.status === "blocked"
          ? `Blocked by the send gate (${String(result.blocked_reason ?? "see report")}).`
          : "Reply queued through the full send gate.",
      );
      setDraft({ ...draft, body_text: "" });
      load();
      open(detail.reply.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "send failed");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">Replies</h1>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}

      <Card title={`Inbox (${items.length})`}>
        <Table head={["Company", "Class", "Confidence", "Status", "Received", ""]}>
          {items.map((reply) => (
            <tr key={reply.id} className={`border-b border-black/5 ${detail?.reply.id === reply.id ? "bg-black/5" : ""}`}>
              <td className="px-2 py-2 font-medium">{reply.company_name || "—"}</td>
              <td className="px-2 py-2">
                <Badge
                  tone={
                    reply.classification === "unsubscribe" || reply.classification === "complaint"
                      ? "error"
                      : reply.classification === "unclear" || reply.classification === "not_now"
                        ? "warn"
                        : "neutral"
                  }
                >
                  {titleCase(reply.classification)}
                </Badge>
              </td>
              <td className="px-2 py-2">{Math.round((reply.confidence || 0) * 100)}%</td>
              <td className="px-2 py-2 text-muted">{reply.status}</td>
              <td className="px-2 py-2 text-muted">{when(reply.created_at)}</td>
              <td className="px-2 py-2">
                <Button tone="ghost" onClick={() => open(reply.id)}>
                  Open
                </Button>
              </td>
            </tr>
          ))}
        </Table>
        {items.length === 0 ? <p className="text-sm text-muted">No replies yet.</p> : null}
      </Card>

      {detail ? (
        <>
          <Card
            title={`${detail.company?.name || "Prospect"} — ${titleCase(detail.reply.classification)} (${Math.round(
              (detail.reply.confidence || 0) * 100,
            )}%)`}
          >
            <p className="mb-2 text-sm text-muted">
              {detail.contact?.full_name || "unknown contact"}
              {detail.contact?.role_title ? ` · ${detail.contact.role_title}` : ""}
              {detail.contact?.email_masked ? ` · ${detail.contact.email_masked}` : ""} · received{" "}
              {when(detail.inbound.received_at)}
            </p>
            <div className="max-h-80 space-y-2 overflow-y-auto">
              {detail.thread.map((row) => (
                <div
                  key={`${row.direction}-${row.id}`}
                  className={`rounded border p-2 text-sm ${
                    row.direction === "inbound" ? "border-navy/20 bg-navy/5" : "border-black/10 bg-white"
                  }`}
                >
                  <p className="text-xs text-muted">
                    {row.direction === "inbound" ? "← prospect" : "→ sent"} · {when(row.at)}
                    {row.step ? ` · ${row.step}` : ""}
                    {row.status ? ` · ${row.status}` : ""}
                  </p>
                  <p className="font-medium">{row.subject}</p>
                  <p className="whitespace-pre-wrap">{row.text}</p>
                </div>
              ))}
            </div>
          </Card>

          <Card title="Respond">
            <div className="grid gap-2">
              <input
                value={draft.subject}
                onChange={(event) => setDraft({ ...draft, subject: event.target.value })}
                className="rounded border border-black/15 px-3 py-2 text-sm"
                placeholder="Subject"
              />
              <textarea
                value={draft.body_text}
                onChange={(event) => setDraft({ ...draft, body_text: event.target.value })}
                rows={6}
                className="rounded border border-black/15 px-3 py-2 text-sm"
                placeholder="Write the reply. It goes through the full send gate."
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button disabled={!draft.body_text.trim() || !draft.subject.trim()} onClick={send}>
                  Send reply
                </Button>
                <Button tone="ghost" onClick={handoff}>
                  Hand off to a human
                </Button>
                <select
                  value={detail.reply.classification}
                  onChange={(event) => override(event.target.value)}
                  className="rounded border border-black/15 px-2 py-1 text-sm"
                >
                  {CLASSIFICATIONS.map((value) => (
                    <option key={value} value={value}>
                      override → {value}
                    </option>
                  ))}
                </select>
              </div>
              {sendResult ? (
                <pre className="max-h-48 overflow-y-auto rounded bg-black/5 p-2 text-xs">
                  {JSON.stringify(sendResult, null, 2)}
                </pre>
              ) : null}
            </div>
          </Card>
        </>
      ) : null}
    </div>
  );
}
