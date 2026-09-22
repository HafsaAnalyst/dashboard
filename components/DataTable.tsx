"use client";

import { useMemo, useState } from "react";
import { fmtCell, isNumericColumn } from "@/lib/format";

type Props = {
  columns: string[];
  rows: Record<string, unknown>[];
  /** Rename raw view columns for display, e.g. {opps: "Opps"}. */
  labels?: Record<string, string>;
  /** Custom renderer per column, for currency/percent columns. */
  format?: Record<string, (v: unknown, row: Record<string, unknown>) => string>;
  caption?: string;
  emptyMessage?: string;
  /** Rows to show before "Show all" — keeps long drill tables from dominating
   *  the page, matching how the Streamlit tables scroll inside a fixed height. */
  initialRows?: number;
  downloadName?: string;
};

function toCsv(columns: string[], rows: Record<string, unknown>[]): string {
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [
    columns.map(esc).join(","),
    ...rows.map((r) => columns.map((c) => esc(r[c])).join(",")),
  ].join("\n");
}

export default function DataTable({
  columns,
  rows,
  labels = {},
  format = {},
  caption,
  emptyMessage = "No data for this filter.",
  initialRows = 50,
  downloadName,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [sort, setSort] = useState<{ col: string; dir: "asc" | "desc" } | null>(null);

  const numeric = useMemo(
    () => new Set(columns.filter((c) => isNumericColumn(rows, c))),
    [columns, rows],
  );

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sort.col];
      const bv = b[sort.col];
      if (av === bv) return 0;
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sort]);

  const visible = expanded ? sorted : sorted.slice(0, initialRows);
  const hiddenCount = sorted.length - visible.length;

  if (!rows.length) {
    return (
      <div className="rounded-lg border border-[var(--color-hairline)] bg-slate-50 px-4 py-3 text-[13px] text-[var(--color-ink-soft)]">
        {emptyMessage}
      </div>
    );
  }

  const download = () => {
    const blob = new Blob([toCsv(columns, sorted)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${downloadName ?? "export"}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="table-scroll max-h-[520px] overflow-y-auto rounded-lg border border-[var(--color-hairline)]">
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c}
                  className={`${numeric.has(c) ? "num" : ""} cursor-pointer select-none`}
                  onClick={() =>
                    setSort((s) =>
                      s?.col === c
                        ? { col: c, dir: s.dir === "asc" ? "desc" : "asc" }
                        : { col: c, dir: "desc" },
                    )
                  }
                  title="Click to sort"
                >
                  {labels[c] ?? c}
                  {sort?.col === c ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c} className={numeric.has(c) ? "num" : ""}>
                    {format[c] ? format[c](row[c], row) : fmtCell(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-2 flex items-center gap-3 text-[11px] text-[var(--color-hint)]">
        <span>
          {sorted.length.toLocaleString("en-AU")} row{sorted.length === 1 ? "" : "s"}
        </span>
        {hiddenCount > 0 && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="font-semibold text-[var(--color-ink-soft)] underline underline-offset-2"
          >
            Show all {sorted.length.toLocaleString("en-AU")}
          </button>
        )}
        {downloadName && (
          <button
            type="button"
            onClick={download}
            className="font-semibold text-[var(--color-ink-soft)] underline underline-offset-2"
          >
            Download CSV
          </button>
        )}
      </div>

      {caption && (
        <p className="mt-2 text-[11px] leading-relaxed text-[var(--color-hint)]">{caption}</p>
      )}
    </div>
  );
}
