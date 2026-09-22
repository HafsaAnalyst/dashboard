"use client";

import { delta, fmtDelta, type Delta } from "@/lib/format";

type Props = {
  label: string;
  value: string;
  current?: unknown;
  prior?: unknown;
  hint?: string;
  /** Some metrics are better when they fall (No Show, CPL). Flips the colour
   *  without flipping the arrow, so the number still reads truthfully. */
  invertColour?: boolean;
  onClick?: () => void;
  loading?: boolean;
  error?: string;
};

function deltaColour(d: Delta, invert: boolean): string {
  if (!d || d.direction === "flat") return "text-[var(--color-ink-faint)]";
  const good = invert ? d.direction === "down" : d.direction === "up";
  return good ? "text-[var(--color-up)]" : "text-[var(--color-down)]";
}

export default function ScoreCard({
  label,
  value,
  current,
  prior,
  hint,
  invertColour = false,
  onClick,
  loading = false,
  error,
}: Props) {
  const d = delta(current, prior);
  const clickable = !!onClick && !loading && !error;

  const body = (
    <>
      <div className="text-[12px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
        {label}
      </div>

      {loading ? (
        <div className="mt-2 h-8 w-24 animate-pulse rounded bg-slate-100" />
      ) : error ? (
        <div className="mt-2 text-[13px] leading-snug text-[var(--color-down)]">{error}</div>
      ) : (
        <div className="mt-1 text-[28px] font-bold leading-tight text-[var(--color-ink)]">
          {value}
        </div>
      )}

      {!loading && !error && (
        <div className="mt-1 flex items-baseline gap-2">
          <span className={`text-[13px] font-semibold ${deltaColour(d, invertColour)}`}>
            {d && d.direction !== "flat" ? (d.direction === "up" ? "▲" : "▼") : ""} {fmtDelta(d)}
          </span>
          <span className="text-[11px] text-[var(--color-hint)]">vs prior period</span>
        </div>
      )}

      {hint && <div className="mt-2 text-[11px] text-[var(--color-hint)]">{hint}</div>}
    </>
  );

  const base =
    "rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-4 text-left";

  if (!clickable) return <div className={base}>{body}</div>;

  return (
    <button
      type="button"
      onClick={onClick}
      className={`${base} w-full cursor-pointer transition hover:border-slate-300 hover:shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-active)]`}
    >
      {body}
    </button>
  );
}
