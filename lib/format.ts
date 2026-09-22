/** Display formatting — mirrors the helpers in dashboards/app.py so numbers on
 *  the Vercel dashboard read identically to the Streamlit one. */

const EM_DASH = "—";

export function fmtInt(v: unknown): string {
  const n = toNum(v);
  return n === null ? EM_DASH : Math.round(n).toLocaleString("en-AU");
}

export function fmtMoney(v: unknown, decimals = 0): string {
  const n = toNum(v);
  if (n === null) return EM_DASH;
  return `$${n.toLocaleString("en-AU", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

export function fmtPct(v: unknown, decimals = 1): string {
  const n = toNum(v);
  return n === null ? EM_DASH : `${n.toFixed(decimals)}%`;
}

export function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export type Delta = { pct: number; direction: "up" | "down" | "flat" } | null;

/**
 * Period-over-period change, matching the Streamlit rule: (current - prior) / prior.
 * Returns null when there is no prior value or the prior is zero — a jump from
 * zero is not a percentage, and rendering "∞%" or "+100%" there would mislead.
 */
export function delta(current: unknown, prior: unknown): Delta {
  const c = toNum(current);
  const p = toNum(prior);
  if (c === null || p === null || p === 0) return null;
  const pct = ((c - p) / Math.abs(p)) * 100;
  const direction = pct > 0.05 ? "up" : pct < -0.05 ? "down" : "flat";
  return { pct, direction };
}

export function fmtDelta(d: Delta): string {
  if (!d) return EM_DASH;
  const sign = d.pct > 0 ? "+" : "";
  return `${sign}${d.pct.toFixed(1)}%`;
}

/** "Sep 01 – Sep 05, 2026 (5 days)" — the period caption under the filter bar. */
export function fmtPeriod(since: string, until: string): string {
  const s = new Date(`${since}T00:00:00`);
  const u = new Date(`${until}T00:00:00`);
  const days = Math.round((u.getTime() - s.getTime()) / 86_400_000) + 1;
  const short = (d: Date) =>
    d.toLocaleDateString("en-AU", { month: "short", day: "2-digit" });
  return `${short(s)} – ${short(u)}, ${u.getFullYear()} (${days} day${days === 1 ? "" : "s"})`;
}

/** "Updated 12 min ago" / "Updated 2h 5m ago" — the freshness line. */
export function fmtFreshness(iso: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return null;
  const mins = Math.floor((Date.now() - then.getTime()) / 60_000);
  if (mins < 1) return "Updated just now";
  if (mins < 90) return `Updated ${mins} min ago`;
  return `Updated ${Math.floor(mins / 60)}h ${mins % 60}m ago`;
}

/** Right-align a column only when its values are actually numeric. */
export function isNumericColumn(rows: Record<string, unknown>[], col: string): boolean {
  let seen = 0;
  for (const row of rows) {
    const v = row[col];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v !== "number") return false;
    seen += 1;
    if (seen >= 5) break;
  }
  return seen > 0;
}

export function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return EM_DASH;
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") {
    return Number.isInteger(v) ? v.toLocaleString("en-AU") : v.toFixed(2);
  }
  return String(v);
}
