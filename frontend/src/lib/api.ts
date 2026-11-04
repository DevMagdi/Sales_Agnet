"use client";

/** Thin typed API client. The access token lives in memory + localStorage; refresh is automatic. */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "";

export type Json = Record<string, unknown>;

const TOKEN_KEY = "mantiq.access";
const REFRESH_KEY = "mantiq.refresh";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getRefresh(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(REFRESH_KEY);
}

export function setTokens(access: string, refresh?: string): void {
  window.localStorage.setItem(TOKEN_KEY, access);
  if (refresh) window.localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_KEY);
}

export class ApiError extends Error {
  status: number;
  details: unknown;

  constructor(status: number, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function refreshAccessToken(): Promise<boolean> {
  const refresh = getRefresh();
  if (!refresh) return false;
  const response = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!response.ok) return false;
  const data = (await response.json()) as { access_token: string; refresh_token?: string };
  setTokens(data.access_token, data.refresh_token);
  return true;
}

export async function api<T = Json>(
  path: string,
  options: { method?: string; body?: unknown; query?: Record<string, string | number | undefined> } = {},
): Promise<T> {
  const url = new URL(`${API_BASE}${path}`, typeof window === "undefined" ? "http://localhost" : window.location.origin);
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
    }
  }
  const headers: Record<string, string> = { "content-type": "application/json" };
  const token = getToken();
  if (token) headers.authorization = `Bearer ${token}`;

  const send = async (): Promise<Response> =>
    fetch(url.toString().replace(/^http:\/\/localhost/, ""), {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
    });

  let response = await send();
  if (response.status === 401 && (await refreshAccessToken())) {
    const fresh = getToken();
    if (fresh) headers.authorization = `Bearer ${fresh}`;
    response = await send();
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const payload = text ? (JSON.parse(text) as Json) : {};
  if (!response.ok) {
    const error = (payload as { error?: { message?: string; details?: unknown }; detail?: unknown }).error;
    const message =
      error?.message ||
      (typeof payload.detail === "string" ? payload.detail : "") ||
      `request failed with ${response.status}`;
    throw new ApiError(response.status, message, error?.details ?? payload.detail);
  }
  return payload as T;
}

export async function login(email: string, password: string): Promise<void> {
  const data = await api<{ access_token: string; refresh_token: string }>("/api/v1/auth/login", {
    method: "POST",
    body: { email, password },
  });
  setTokens(data.access_token, data.refresh_token);
}

export async function logout(): Promise<void> {
  const refresh = getRefresh();
  try {
    if (refresh) await api("/api/v1/auth/logout", { method: "POST", body: { refresh_token: refresh } });
  } finally {
    clearTokens();
  }
}
