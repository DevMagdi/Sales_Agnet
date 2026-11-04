"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { Badge, Banner, Button, Card, Table } from "@/components/ui";

type Mailbox = {
  id: string;
  from_email: string;
  from_name: string;
  status: string;
  daily_cap: number;
  warmup_stage: number;
  provider: string;
};

type User = { id: string; email: string; name: string; role: string; is_active: boolean; mfa_enabled: boolean };

export default function SettingsPage() {
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [templatesCount, setTemplatesCount] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState({
    from_email: "",
    from_name: "",
    daily_cap: 20,
    smtp_host: "",
    smtp_username: "",
    smtp_password: "",
    imap_host: "",
    imap_username: "",
    imap_password: "",
  });
  const [newUser, setNewUser] = useState({ email: "", name: "", password: "", role: "sales" });

  const load = useCallback(async () => {
    try {
      const [boxes, people, templates] = await Promise.all([
        api<{ items: Mailbox[] }>("/api/v1/settings/mailboxes"),
        api<{ items: User[] }>("/api/v1/settings/users"),
        api<{ items: Array<Record<string, unknown>> }>("/api/v1/templates"),
      ]);
      setMailboxes(boxes.items);
      setUsers(people.items);
      setTemplatesCount(templates.items.length);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load settings");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function addMailbox(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/v1/settings/mailboxes", { method: "POST", body: form });
      setNotice("Mailbox added. Credentials are encrypted and never returned by the API.");
      setForm({ ...form, from_email: "", from_name: "", smtp_password: "", imap_password: "" });
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not add the mailbox");
    }
  }

  async function setMailboxStatus(id: string, status: string) {
    try {
      await api(`/api/v1/settings/mailboxes/${id}/status`, { method: "POST", body: { status } });
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not change the mailbox");
    }
  }

  async function addUser(event: React.FormEvent) {
    event.preventDefault();
    try {
      await api("/api/v1/settings/users", { method: "POST", body: newUser });
      setNotice(`User ${newUser.email} created.`);
      setNewUser({ email: "", name: "", password: "", role: "sales" });
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not create the user");
    }
  }

  async function deactivate(id: string) {
    try {
      await api(`/api/v1/settings/users/${id}/deactivate`, { method: "POST" });
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not deactivate");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">Settings</h1>
      {error ? <Banner tone="error">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}

      <Card title="Sending mailboxes">
        <Table head={["From", "Name", "Status", "Cap", "Warm-up", "Provider", ""]}>
          {mailboxes.map((mailbox) => (
            <tr key={mailbox.id} className="border-b border-black/5">
              <td className="px-2 py-2 font-mono text-xs">{mailbox.from_email}</td>
              <td className="px-2 py-2">{mailbox.from_name}</td>
              <td className="px-2 py-2">
                <Badge tone={mailbox.status === "active" ? "ok" : "warn"}>{mailbox.status}</Badge>
              </td>
              <td className="px-2 py-2">{mailbox.daily_cap}</td>
              <td className="px-2 py-2">{mailbox.warmup_stage}</td>
              <td className="px-2 py-2 text-muted">{mailbox.provider}</td>
              <td className="px-2 py-2">
                <div className="flex gap-1">
                  <Button tone="ghost" onClick={() => setMailboxStatus(mailbox.id, "paused")}>
                    Pause
                  </Button>
                  <Button onClick={() => setMailboxStatus(mailbox.id, "active")}>Activate</Button>
                </div>
              </td>
            </tr>
          ))}
        </Table>
        {mailboxes.length === 0 ? <p className="text-sm text-muted">No mailbox yet — the agent cannot send without one.</p> : null}
      </Card>

      <Card title="Add a mailbox (credentials are write-only)">
        <form onSubmit={addMailbox} className="grid gap-2 sm:grid-cols-2">
          {(
            [
              ["from_email", "From address"],
              ["from_name", "From name"],
              ["smtp_host", "SMTP host"],
              ["smtp_username", "SMTP username"],
              ["smtp_password", "SMTP password"],
              ["imap_host", "IMAP host"],
              ["imap_username", "IMAP username"],
              ["imap_password", "IMAP password"],
            ] as const
          ).map(([key, label]) => (
            <input
              key={key}
              type={key.includes("password") ? "password" : "text"}
              placeholder={label}
              value={String(form[key])}
              onChange={(event) => setForm({ ...form, [key]: event.target.value })}
              className="rounded border border-black/15 px-3 py-2 text-sm"
              required={key === "from_email" || key === "from_name"}
            />
          ))}
          <label className="flex items-center gap-2 text-sm text-muted">
            daily cap
            <input
              type="number"
              value={form.daily_cap}
              min={1}
              onChange={(event) => setForm({ ...form, daily_cap: Number(event.target.value) })}
              className="w-24 rounded border border-black/15 px-2 py-1"
            />
          </label>
          <div className="sm:col-span-2">
            <Button type="submit">Add mailbox</Button>
          </div>
        </form>
      </Card>

      <Card title={`Users (${users.length})`}>
        <Table head={["Email", "Name", "Role", "Active", "MFA", ""]}>
          {users.map((user) => (
            <tr key={user.id} className="border-b border-black/5">
              <td className="px-2 py-2">{user.email}</td>
              <td className="px-2 py-2">{user.name}</td>
              <td className="px-2 py-2">
                <Badge tone={user.role === "admin" ? "warn" : "neutral"}>{user.role}</Badge>
              </td>
              <td className="px-2 py-2">{user.is_active ? "yes" : "no"}</td>
              <td className="px-2 py-2">{user.mfa_enabled ? "on" : "off"}</td>
              <td className="px-2 py-2">
                {user.is_active ? (
                  <Button tone="ghost" onClick={() => deactivate(user.id)}>
                    Deactivate
                  </Button>
                ) : null}
              </td>
            </tr>
          ))}
        </Table>
        <form onSubmit={addUser} className="mt-3 grid gap-2 sm:grid-cols-5">
          <input
            required
            type="email"
            placeholder="new user email"
            value={newUser.email}
            onChange={(event) => setNewUser({ ...newUser, email: event.target.value })}
            className="rounded border border-black/15 px-3 py-2 text-sm"
          />
          <input
            placeholder="name"
            value={newUser.name}
            onChange={(event) => setNewUser({ ...newUser, name: event.target.value })}
            className="rounded border border-black/15 px-3 py-2 text-sm"
          />
          <input
            required
            type="password"
            placeholder="password (10+ chars)"
            value={newUser.password}
            onChange={(event) => setNewUser({ ...newUser, password: event.target.value })}
            className="rounded border border-black/15 px-3 py-2 text-sm"
          />
          <select
            value={newUser.role}
            onChange={(event) => setNewUser({ ...newUser, role: event.target.value })}
            className="rounded border border-black/15 px-3 py-2 text-sm"
          >
            <option value="sales">sales</option>
            <option value="viewer">viewer</option>
            <option value="admin">admin</option>
          </select>
          <Button type="submit">Create user</Button>
        </form>
      </Card>

      <Card title="Templates">
        <p className="text-sm text-muted">
          {templatesCount === null ? "…" : `${templatesCount} seeded (Arabic + English, every step, both channels).`} Edit
          them through the API so versions stay auditable.
        </p>
      </Card>
      <p className="text-xs text-muted">Loaded {when(new Date().toISOString())}</p>
    </div>
  );
}
