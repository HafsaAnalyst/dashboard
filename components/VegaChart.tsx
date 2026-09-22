"use client";

import { useEffect, useMemo, useRef } from "react";

/**
 * Renders a Vega-Lite spec via vega-embed.
 *
 * Why Vega-Lite rather than a React chart library: the Streamlit app draws its
 * 16 charts with Altair, and Altair *is* a Vega-Lite spec builder. A ported
 * chart keeps its existing encoding almost verbatim instead of being
 * re-expressed in a different charting model, which is where subtle differences
 * (binning, stacking, tooltip fields) would otherwise creep in.
 *
 * vega-embed is used directly rather than react-vega, which still peer-depends
 * on React <= 18 and will not install alongside React 19.
 */
type Props = {
  /** A Vega-Lite spec minus `data`, which comes from `rows`. */
  spec: Record<string, unknown>;
  rows: Record<string, unknown>[];
  height?: number;
};

export default function VegaChart({ spec, rows, height = 280 }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  const merged = useMemo(
    () => ({
      $schema: "https://vega.github.io/schema/vega-lite/v5.json",
      width: "container",
      height,
      background: "transparent",
      config: {
        axis: { labelColor: "#475569", titleColor: "#475569", grid: true, gridColor: "#f1f5f9" },
        view: { stroke: "transparent" },
        legend: { labelColor: "#475569", titleColor: "#475569" },
      },
      ...spec,
      data: { values: rows },
    }),
    [spec, rows, height],
  );

  useEffect(() => {
    if (!ref.current || !rows.length) return;
    const el = ref.current;
    let cancelled = false;
    let view: { finalize: () => void } | null = null;

    // Dynamic import keeps vega (a large bundle) out of the initial page load
    // and off the server, where it cannot run.
    import("vega-embed").then(({ default: embed }) => {
      if (cancelled) return;
      embed(el, merged as never, { actions: false, renderer: "canvas" })
        .then((result) => {
          if (cancelled) {
            result.view.finalize();
            return;
          }
          view = result.view;
        })
        .catch(() => {
          // A malformed spec should not take the tab down with it.
          el.innerHTML =
            '<div style="padding:16px;font-size:13px;color:#b91c1c">Chart failed to render.</div>';
        });
    });

    return () => {
      cancelled = true;
      view?.finalize();
    };
  }, [merged, rows.length]);

  if (!rows.length) {
    return (
      <div className="rounded-lg border border-[var(--color-hairline)] bg-slate-50 px-4 py-6 text-center text-[13px] text-[var(--color-ink-soft)]">
        No data to chart for this filter.
      </div>
    );
  }

  return <div ref={ref} className="w-full" style={{ minHeight: height }} />;
}
