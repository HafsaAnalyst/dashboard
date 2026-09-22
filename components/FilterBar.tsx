"use client";

import type { Filters } from "@/lib/api";
import { fmtPeriod } from "@/lib/format";

type Props = {
  filters: Filters;
  onChange: (next: Filters) => void;
  periods: string[];
  cities: string[];
  /** Resolved binds echoed back by the API, so the caption shows the exact
   *  window the server queried rather than the client's guess at it. */
  resolved?: { since: string; until: string; prior_since: string; prior_until: string };
  freshness?: string | null;
};

const selectClass =
  "rounded-lg border border-[var(--color-hairline)] bg-white px-3 py-2 text-[13px] " +
  "font-medium text-[var(--color-ink)] focus:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-[var(--color-active)]";

export default function FilterBar({
  filters,
  onChange,
  periods,
  cities,
  resolved,
  freshness,
}: Props) {
  const today = new Date().toISOString().slice(0, 10);

  return (
    <div className="mb-4 rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-4">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
            Duration
          </span>
          <select
            className={selectClass}
            value={filters.period}
            onChange={(e) => onChange({ ...filters, period: e.target.value })}
          >
            {periods.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        {filters.period === "Custom" && (
          <>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
                From
              </span>
              <input
                type="date"
                max={filters.until ?? today}
                value={filters.since ?? ""}
                onChange={(e) => onChange({ ...filters, since: e.target.value })}
                className={selectClass}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
                To
              </span>
              <input
                type="date"
                max={today}
                min={filters.since}
                value={filters.until ?? ""}
                onChange={(e) => onChange({ ...filters, until: e.target.value })}
                className={selectClass}
              />
            </label>
          </>
        )}

        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
            Location
          </span>
          <select
            className={selectClass}
            value={filters.city}
            onChange={(e) => onChange({ ...filters, city: e.target.value })}
          >
            {cities.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>

        {freshness && (
          <span className="ml-auto text-[12px] text-[var(--color-ink-faint)]">{freshness}</span>
        )}
      </div>

      {resolved && (
        <p className="mt-3 text-[12px] text-[var(--color-ink-faint)]">
          Period: <strong>{fmtPeriod(resolved.since, resolved.until)}</strong> · Comparison:{" "}
          {fmtPeriod(resolved.prior_since, resolved.prior_until)}
        </p>
      )}
    </div>
  );
}
