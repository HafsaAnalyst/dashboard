"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import DataTable from "@/components/DataTable";
import FilterBar from "@/components/FilterBar";
import TabNav from "@/components/TabNav";
import ViewExplorer from "@/components/ViewExplorer";
import { ApiError, health, isTable, query, type Filters, type ViewSpec } from "@/lib/api";
import { fmtFreshness } from "@/lib/format";

const TABS = [
  "Executive",
  "Meta Ads",
  "Funnels",
  "Counsellors",
  "SEO & Traffic",
  "Forecast & Goals",
  "Upload Reports",
  "Sales Team Perf.",
  "WBR",
  "Weekly Report",
  "Breakdown",
];

// Tabs whose Streamlit logic has been ported. Everything else renders the
// "not yet ported" panel, so the team can see migration progress at a glance
// instead of hitting a blank screen.
const PORTED = new Set(["Executive"]);

const PERIODS = ["Current month", "Last 30 days", "Last 7 days", "Custom"];
const CITIES = ["All", "Melbourne", "Sydney", "Others", "Unidentified"];

const EXEC_VIEWS: ViewSpec[] = [
  { key: "leadDetail", view: "vw_exec1_lead_detail", kind: "table" },
];

export default function DashboardPage() {
  const [tab, setTab] = useState("Executive");
  const [filters, setFilters] = useState<Filters>({ period: "Current month", city: "All" });

  const { data: healthData } = useSWR("health", health, {
    revalidateOnFocus: false,
    // The ETL refreshes hourly; re-checking freshness every 5 min is plenty.
    refreshInterval: 300_000,
  });

  const views = PORTED.has(tab) ? EXEC_VIEWS : [];
  const key = views.length ? ["query", tab, filters] : null;

  const { data, error, isLoading } = useSWR(
    key,
    () => query(filters, views),
    { revalidateOnFocus: false, keepPreviousData: true },
  );

  const leadDetail = useMemo(() => {
    const r = data?.results?.leadDetail;
    return isTable(r) ? r : null;
  }, [data]);

  const resolved = data?.binds as
    | { since: string; until: string; prior_since: string; prior_until: string }
    | undefined;

  return (
    <main>
      <header className="mb-3.5 rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] px-6 py-[18px]">
        <h1 className="text-[22px] font-bold text-[var(--color-ink)]">The Migration Dashboard</h1>
        <p className="mt-1 text-[12px] text-[var(--color-ink-faint)]">
          Live data from GHL · Meta Ads · GA4 · GSC
        </p>
      </header>

      <FilterBar
        filters={filters}
        onChange={setFilters}
        periods={PERIODS}
        cities={CITIES}
        resolved={resolved}
        freshness={fmtFreshness(healthData?.last_refreshed ?? null)}
      />

      <TabNav tabs={TABS} active={tab} onChange={setTab} ready={PORTED} />

      {error instanceof ApiError && error.lapsed && (
        <Panel tone="error">
          <strong>Data source unavailable — the MotherDuck plan has lapsed.</strong>
          <p className="mt-1">
            Every dashboard metric reads from MotherDuck. An admin needs to choose a plan (the
            Free tier is enough) at{" "}
            <a className="underline" href="https://app.motherduck.com">
              app.motherduck.com
            </a>{" "}
            to restore the dashboard.
          </p>
        </Panel>
      )}

      {error instanceof ApiError && !error.lapsed && (
        <Panel tone="error">{error.message}</Panel>
      )}

      {PORTED.has(tab) ? (
        <section className="rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-5">
          <div className="mb-2.5 flex items-baseline justify-between">
            <h2 className="text-[16px] font-bold text-[var(--color-ink)]">
              Executive_1 — Leads by source
            </h2>
            <span className="text-[12px] font-medium text-[var(--color-hint)]">
              created or revived in the selected range
            </span>
          </div>

          {isLoading && !leadDetail ? (
            <div className="h-40 animate-pulse rounded-lg bg-slate-100" />
          ) : leadDetail?.error ? (
            <Panel tone="error">{leadDetail.error}</Panel>
          ) : leadDetail ? (
            <DataTable
              columns={leadDetail.columns}
              rows={leadDetail.rows}
              downloadName="executive-lead-detail"
              emptyMessage="No leads created or revived in this window."
              caption={
                "Raw rows from vw_exec1_lead_detail. The Streamlit tab applies further pandas " +
                "filtering on top of this view (No Activity removal, created-or-revived funnel " +
                "rule, unmapped Paid-Social campaign exclusion, no-email exclusion) before " +
                "counting leads — that logic is not yet ported, so these counts are the " +
                "pre-filter population, not the headline Leads number."
              }
            />
          ) : null}
        </section>
      ) : (
        <Panel tone="info">
          <strong>{tab} — not yet ported.</strong>
          <p className="mt-1">
            This tab still runs on the Streamlit deployment. See MIGRATION.md for the porting
            order and the pattern to follow.
          </p>
        </Panel>
      )}

      <ViewExplorer filters={filters} />
    </main>
  );
}

function Panel({
  tone,
  children,
}: {
  tone: "info" | "error";
  children: React.ReactNode;
}) {
  const styles =
    tone === "error"
      ? "border-red-200 bg-red-50 text-red-800"
      : "border-[var(--color-hairline)] bg-slate-50 text-[var(--color-ink-soft)]";
  return (
    <div className={`mb-4 rounded-xl border px-4 py-3 text-[13px] leading-relaxed ${styles}`}>
      {children}
    </div>
  );
}
