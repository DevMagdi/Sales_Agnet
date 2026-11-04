"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(email, password);
      router.replace("/");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-navy px-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl bg-white p-8 shadow-xl">
        <h1 className="text-xl font-semibold text-navy">Mantiq Outreach</h1>
        <p className="mb-6 mt-1 text-sm text-muted">Sign in to run ProcureAI outreach.</p>
        <label className="block text-sm text-muted" htmlFor="email">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="mb-3 w-full rounded border border-black/15 px-3 py-2"
          autoComplete="username"
        />
        <label className="block text-sm text-muted" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="mb-3 w-full rounded border border-black/15 px-3 py-2"
          autoComplete="current-password"
        />
        <label className="block text-sm text-muted" htmlFor="totp">
          Authenticator code (if enabled)
        </label>
        <input
          id="totp"
          value={totp}
          onChange={(event) => setTotp(event.target.value)}
          className="mb-4 w-full rounded border border-black/15 px-3 py-2"
          inputMode="numeric"
          autoComplete="one-time-code"
        />
        {error ? <p className="mb-3 rounded bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-navy px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
