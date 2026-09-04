// Thin client for the StartupOS API. The web app uses the API only — never Postgres, never a model.

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TENANT_KEY = "sos.tenant";

export type Tile = { label: string; val: string; sub: string; cls: "" | "warn" | "bad" };
export type SignalView = {
  id: string;
  kind: "reply" | "click" | "open" | "bounce";
  title: string;
  meta: string;
  action: string;
  href: string | null;
  approval_id: string | null;
};
export type Exec = { server: string; tool: string; input: Record<string, unknown> } | null;
export type Approval = {
  id: string;
  tenant_id: string;
  module: string;
  type: string;
  target: string;
  preview: string;
  exec: Exec;
  status: "pending" | "approved" | "declined" | "executed" | "failed";
  signal_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decline_reason: string | null;
  result: Record<string, unknown> | null;
  created_at: string | null;
};
export type Table = { title: string; accent: string; cols: string[]; rows: string[][] };
export type ModuleSnapshot = {
  name: string;
  title: string;
  crumb: string;
  source: string;
  snapshot_at: string;
  live: boolean;
  memory: string;
  tiles: Tile[];
  signals: SignalView[];
  table: Table | null;
  table2: Table | null;
  approvals: Approval[];
  next: string[];
};
export type MemoryCard = { slice: "identity" | "icp" | "voice" | "pricing" | "team"; draft: string; sources: string[] };
export type Cockpit = {
  tenant_id: string;
  snapshot_at: string;
  pulse: { text: string; at: string; tier: number; model: string | null; source: "run" | "fallback" };
  tiles: Tile[];
  quick_glance: { module: string; title: string; line: string; signals: number; approvals: number; cls: string }[];
  pending_approvals: number;
  finance: { cash_on_hand: string; ap_next_7d: string; received_mtd: string; last_inflow_at: string | null };
  spend: {
    month: string;
    runs: number;
    tokens: number;
    cost_usd: string;
    by_tier: { tier: number; runs: number; tokens_in: number; tokens_out: number; cost_usd: string }[];
    by_skill: { skill: string; tier: number; runs: number; tokens_in: number; tokens_out: number; cost_usd: string }[];
    budget: { tier2_tokens_allowed: number; tier2_tokens_used: number; state: string } | null;
  };
};
export type OnboardingStatus = {
  tenant_id: string;
  steps: { tenant: boolean; connections: boolean; compiled: boolean; cards: boolean; pulse: boolean; cadence: boolean };
  connections: number;
  cards_confirmed: string[];
  done: number;
  total: number;
};

export function getTenant(): string | null {
  try {
    return typeof window !== "undefined" ? window.localStorage.getItem(TENANT_KEY) : null;
  } catch {
    return null;
  }
}

export function setTenant(id: string) {
  try {
    window.localStorage.setItem(TENANT_KEY, id);
  } catch {
    /* private mode */
  }
}

function url(path: string, params?: Record<string, string | undefined>): string {
  const u = new URL(path, API_BASE);
  const tenant = getTenant();
  if (tenant) u.searchParams.set("tenant", tenant);
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined) u.searchParams.set(k, v);
  return u.toString();
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* no body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export async function apiGet<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  return handle<T>(await fetch(url(path, params), { cache: "no-store" }));
}

export async function apiPost<T>(path: string, body?: unknown, params?: Record<string, string | undefined>): Promise<T> {
  return handle<T>(
    await fetch(url(path, params), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export function decide(id: string, decision: "approve" | "decline", extra: { reason?: string; edited_preview?: string } = {}) {
  return apiPost<Approval>(`/approvals/${encodeURIComponent(id)}/decide`, { decision, decided_by: "owner", ...extra });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  if (min < 1440) return `${Math.round(min / 60)} hr ago`;
  return `${Math.round(min / 1440)} d ago`;
}
