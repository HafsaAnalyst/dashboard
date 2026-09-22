"use client";

import { useState } from "react";
import useSWR from "swr";
import DataTable from "@/components/DataTable";
import { isTable, meta, query, type Filters } from "@/lib/api";

/**
 * Runs any one of the 87 SQL views against the current filters.
 *
 * This exists for the migration itself: while tabs are being ported, the only
 * way to trust a ported number is to put the Vercel result next to the Streamlit
 * result for the same view and binds. Rather than rebuild that comparison
 * per tab, this panel makes every view reachable directly.
 *
 * Safe to delete once the port is finished — nothing else depends on it.
 */
export default function ViewExplorer({ filters }: { filters: Filters }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState("");

  const { data: metaData } = useSWR(open ? "meta" : null, meta, {
    revalidateOnFocus: false,
  });

  const { data, isLoading } = useSWR(
    open && view ? ["explorer", view, filters] : null,
    () => query(filters, [{ key: "result", view, kind: "table" }]),
    { revalidateOnFocus: false },
  );

  const result = data?.results?.result;
  const table = isTable(result) ? result : null;

  return (
    <section className="mt-4 rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-[13px] font-semibold text-[var(--color-ink-soft)]"
      >
        {open ? "▾" : "▸"} Parity check — run any SQL view
      </button>

      {open && (
        <div className="mt-4">
          <p className="mb-3 text-[12px] text-[var(--color-hint)]">
            Runs the view with the filter bar&apos;s binds. Compare the output against the same
            view in the Streamlit app to verify a port.
          </p>

          <select
            value={view}
            onChange={(e) => setView(e.target.value)}
            className="w-full max-w-md rounded-lg border border-[var(--color-hairline)] bg-white px-3 py-2 text-[13px]"
          >
            <option value="">Select a view…</option>
            {(metaData?.views ?? []).map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>

          <div className="mt-4">
            {isLoading ? (
              <div className="h-32 animate-pulse rounded-lg bg-slate-100" />
            ) : table?.error ? (
              <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
                {table.error}
              </div>
            ) : table ? (
              <DataTable
                columns={table.columns}
                rows={table.rows}
                downloadName={view}
                initialRows={25}
              />
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}
