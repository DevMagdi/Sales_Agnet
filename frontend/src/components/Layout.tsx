"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { clearTokens, getToken, logout } from "@/lib/api";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/review", label: "Review queue" },
  { href: "/replies", label: "Replies" },
  { href: "/tasks", label: "Tasks" },
  { href: "/suppression", label: "Suppression" },
  { href: "/settings", label: "Settings" },
  { href: "/system", label: "System" },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [router, pathname]);

  if (pathname === "/login") return <>{children}</>;
  if (!ready) return <main className="p-10 text-muted">Loading…</main>;

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <header className="bg-navy text-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-3">
            <span className="text-lg font-semibold">Mantiq Outreach</span>
            <span className="rounded bg-gold/20 px-2 py-0.5 text-xs text-gold">ProcureAI</span>
          </div>
          <button
            className="rounded border border-white/30 px-3 py-1 text-sm hover:bg-white/10"
            onClick={async () => {
              await logout();
              clearTokens();
              router.replace("/login");
            }}
          >
            Sign out
          </button>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 pb-1 text-sm">
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-t px-3 py-2 ${
                  active ? "bg-canvas font-medium text-navy" : "text-white/80 hover:bg-white/10"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
    </div>
  );
}
