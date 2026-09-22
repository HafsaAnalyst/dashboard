/**
 * Client for the Python data API in /api.
 *
 * One POST /api/query can run several views at once, which matters: a tab's
 * scorecard row is ~6 views and MotherDuck round-trips dominate latency. Batch
 * a tab's views into a single request rather than firing one fetch per card.
 */

export type Filters = {
  period: string;
  city: string;
  since?: string; // only meaningful when period === "Custom"
  until?: string;
};

export type ViewSpec = {
  key: string;
  view: string;
  kind: "card" | "table";
  binds?: Record<string, string | number | boolean | null>;
};

/** A scorecard view returns one row per tag; the UI derives the delta. */
export type CardResult = {
  current?: Record<string, unknown>;
  prior?: Record<string, unknown>;
  error?: string;
};

export type TableResult = {
  columns: string[];
  rows: Record<string, unknown>[];
  error?: string;
};

export type QueryResponse = {
  binds: Record<string, string>;
  period: string;
  results: Record<string, CardResult | TableResult>;
};

export class ApiError extends Error {
  status: number;
  /** True when MotherDuck refused because the plan/trial lapsed, which needs a
   *  different message than a transient outage. */
  lapsed: boolean;

  constructor(message: string, status: number, lapsed = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.lapsed = lapsed;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });

  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // A non-JSON body means the platform failed before our handler ran
    // (function timeout, build error). Surface the status rather than a
    // confusing JSON parse error.
    throw new ApiError(`Request to ${path} failed (HTTP ${res.status})`, res.status);
  }

  if (!res.ok) {
    const b = body as { error?: string; lapsed?: boolean };
    throw new ApiError(b?.error ?? `Request to ${path} failed`, res.status, !!b?.lapsed);
  }
  return body as T;
}

export function query(filters: Filters, views: ViewSpec[]): Promise<QueryResponse> {
  return request<QueryResponse>("/api/query", {
    method: "POST",
    body: JSON.stringify({ ...filters, views }),
  });
}

export type Health = {
  ok: boolean;
  last_refreshed: string | null;
  server_time: string;
};

export function health(): Promise<Health> {
  return request<Health>("/api/health");
}

export type Meta = {
  tabs: string[];
  cities: string[];
  periods: string[];
  views: string[];
};

export function meta(): Promise<Meta> {
  return request<Meta>("/api/meta");
}

export async function login(password: string): Promise<void> {
  await request<{ ok: boolean }>("/api/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

/** SWR key helper: stable across renders for the same filters + view set. */
export function queryKey(filters: Filters, views: ViewSpec[]): string {
  return JSON.stringify([filters, views.map((v) => [v.key, v.view, v.kind, v.binds])]);
}

export function isTable(r: CardResult | TableResult | undefined): r is TableResult {
  return !!r && Array.isArray((r as TableResult).rows);
}
