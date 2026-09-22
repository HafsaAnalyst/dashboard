# Streamlit → Vercel migration

The dashboard is being moved from Streamlit Community Cloud to Vercel, as a
Next.js frontend over a Python data API. This document is the runbook: what was
built, how to deploy it, and how to port the remaining tabs.

The Streamlit app (`dashboards/app.py`) is **untouched and still works**. Both can
run side by side until the port is finished.

---

## Why the app had to be rewritten, not "deployed"

Vercel cannot run Streamlit. Vercel Functions are serverless: each request starts
a process, runs, and exits. Streamlit is a long-running Tornado server that holds
a **WebSocket** open per user for the whole session — that is how `st.session_state`,
widget reruns and `@st.cache_data` work. There is no configuration that bridges
this. Anything claiming to "deploy Streamlit on Vercel" either does not work or
serves a static snapshot.

So the UI layer is replaced. Everything below it is kept.

## What made the rewrite tractable

`dashboards/app.py` already separates logic from presentation:

- **4,857 lines of SQL** across `dashboards/sql/*.sql` are `CREATE OR REPLACE VIEW`
  blocks, parsed into `{view_name: body}` and executed with binds
  (`$since`, `$until`, `$prior_since`, `$prior_until`, `$city`).
- Every read goes through two functions, `run_view()` and `run_df()`.

That is already an API contract. The new API reuses **the same .sql files** — they
remain the single source of truth, read by both apps. No SQL was copied or forked.

## Architecture

```
Browser
  └── Next.js 15 (App Router, React 19, Tailwind v4)   ← UI only
        └── POST /api/query                             ← batched view execution
              └── api/index.py  (Vercel Python Function)
                    ├── _lib/queries.py  parses dashboards/sql/*.sql, binds, runs
                    ├── _lib/db.py       MotherDuck connection + retry
                    └── _lib/auth.py     shared-password session cookie
                          └── MotherDuck  (unchanged; the hourly ETL still writes it)
```

### Two platform constraints that dictated the design

1. **No web framework in the Python deps.** A Python *framework preset*
   (FastAPI/Flask/Django) takes precedence over file-based `/api` functions and
   would take over the entire project, so Next.js would never be served. Adding
   `fastapi` to the dependency list is enough to break the deployment.
   `api/index.py` is therefore a plain ASGI app with no framework.

2. **`pyproject.toml` wins over `requirements.txt`.** Vercel's uv-based build
   prefers `pyproject.toml`. That is exploited deliberately: `pyproject.toml`
   lists only the five packages the API needs, so the root `requirements.txt`
   (streamlit, prophet, matplotlib, google-\*) stays untouched for local dev and
   the existing Streamlit deployment, and never bloats the function bundle.

   **Do not add a dependency to `requirements.txt` expecting the API to get it.**
   Add it to `pyproject.toml`.

## Deploying

1. Import the repo at [vercel.com/new](https://vercel.com/new). Framework preset:
   **Next.js**. Root directory: repo root. No build-command overrides.

2. Set environment variables (Project → Settings → Environment Variables):

   | Variable | Purpose |
   |---|---|
   | `MOTHERDUCK_TOKEN` | **Required.** Same token the Streamlit app uses. |
   | `MOTHERDUCK_DATABASE` | Optional, defaults to `migration`. |
   | `DASHBOARD_PASSWORD` | Shared team password. Unset ⇒ **no auth at all**. |
   | `DASHBOARD_SECRET` | Random string used to sign session cookies. |

   Add `META_ACCESS_TOKEN`, `META_*_AD_ACCOUNT_ID`, `STRIPE_*` etc. as the tabs
   that need them get ported — the Streamlit app reads them via `os.getenv`, and
   the ported code will too.

   Generate a secret with: `python -c "import secrets; print(secrets.token_hex(32))"`

3. Deploy. `vercel.json` rewrites `/api/*` to the Python function; everything else
   is Next.js.

### Local development

```powershell
npm install
npm run dev          # http://localhost:3000
```

With no `DASHBOARD_PASSWORD` set, auth is disabled on both the middleware and the
API so they never disagree. With no `MOTHERDUCK_TOKEN`, the API falls back to
`data/migration_dashboard.duckdb` read-only — note that local snapshot is stale
and some views will fail against its older schema.

To run the API the way Vercel does, use `vercel dev` (`npm i -g vercel`); plain
`next dev` serves the UI but not the Python function.

## Porting a tab

Ported so far: **Executive** (partially — see below). The other ten render a
"not yet ported" panel and still live on Streamlit.

The work per tab is *not* rewriting logic in TypeScript. Much of the business
logic is **pandas post-processing**, not SQL — the Executive tab alone does
campaign→account remapping, a created-or-revived funnel rule, unmapped
Paid-Social exclusion and a no-email exclusion in Python
(`dashboards/app.py:5349-5450`). Reimplementing that in the browser would fork
the definition of a lead. Don't.

The pattern instead:

1. **Move the pandas block into the API**, as a function in a new
   `api/_lib/tabs/<tab>.py` that takes `binds` and returns finished frames.
   Lift it from `app.py` with the `st.*` calls stripped — the pandas stays
   identical.
2. **Add a route** for it in `api/index.py` (or a `kind` in `/api/query`) that
   returns `{columns, rows}` via `df_to_payload`, or `{current, prior}` for a
   scorecard.
3. **Render it** with `<ScoreCard>`, `<DataTable>` and `<VegaChart>`. These match
   the Streamlit look (cream canvas, white cards, dark pills) so ported tabs sit
   next to unported ones without looking foreign.
4. **Verify with the parity panel.** Expand "Parity check — run any SQL view" at
   the bottom of the dashboard, run the same view with the same filters, and
   compare against Streamlit. Every ported number should be checked this way
   before the tab is marked ready.
5. Add the tab name to `PORTED` in `app/page.tsx`.

Charts: keep them as Vega-Lite. Altair *is* a Vega-Lite spec builder, so an
existing `alt.Chart(...)` can be carried over as a spec passed to `<VegaChart>`
rather than re-expressed in a different charting library, where binning,
stacking and tooltip differences would creep in silently.

### Suggested order

Cheapest first, so the pattern is proven before the hard tabs:

| Order | Tab | Why |
|---|---|---|
| 1 | Executive (finish) | Highest use; pandas block is the template for the rest. |
| 2 | Meta Ads | Mostly straight view reads. |
| 3 | Counsellors | Self-contained; `COUNSELLORS` config moves to the API as-is. |
| 4 | SEO & Traffic | Site-wide, not city-filtered — fewer interactions. |
| 5 | Funnels, Breakdown, Sales Team Perf. | Heavier pandas. |
| 6 | WBR, Weekly Report | Live Meta Graph API calls; needs `META_ACCESS_TOKEN`. |
| 7 | Upload Reports | File upload — needs a rethink, see below. |
| 8 | Forecast & Goals | **Hardest, do last.** See below. |

### Two tabs that need decisions

- **Forecast & Goals** uses `prophet`, which with its Stan backend is far too
  large and slow to cold-start in a serverless function. Don't try to bundle it.
  Move forecasting into the hourly ETL (it already runs on GitHub Actions), write
  the results to a MotherDuck table, and let the tab read that table like any
  other view. This is better regardless of hosting — forecasts get recomputed on
  every rerun today.

- **Upload Reports** takes user file uploads. Vercel caps request **and response**
  bodies at **4.5 MB**. For larger files, upload straight to blob storage from the
  browser and pass the API a reference.

### Response size

The 4.5 MB response cap also applies to query results. `vw_exec1_lead_detail`
returns ~1,000 rows × 32 columns for a 30-day window, which is comfortable, but a
wide drill table over a long custom range could exceed it. If a tab hits this,
aggregate server-side or paginate rather than shipping raw rows.

## Two things found while building this

- **The ETL has not run since 2026-08-31.** `MAX(last_refreshed)` in
  `agg_daily_kpis` is 5 days old as of 2026-09-05, while `.github/workflows/etl.yml`
  is scheduled hourly. This affects the **live Streamlit dashboard right now** and
  is unrelated to the migration — worth checking the Actions tab.

- **42 of the 86 parsed views are dead** — never referenced by `app.py`. Some no
  longer even run: `vw_card_total_leads_main` fails against live MotherDuck with
  `Referenced column "result_event" not found`. Only **44 views** need to work for
  the port, and the dead ones can be deleted from the `.sql` files.

## Verified

- `npm run build` — clean; `tsc --noEmit` — clean.
- 86 views parse from the unmodified `.sql` files; all 44 views the app actually
  uses execute against live MotherDuck.
- End-to-end against live data: unauthenticated request → 401; wrong password →
  401; correct password → session cookie; `/api/health` → ok; `/api/query` →
  973 rows for the last 30 days; an unknown view returns an error for that key
  alone without blanking the response.
- Not yet verified: a real Vercel deployment (needs the account), and cold-start
  latency with duckdb + pandas + pyarrow in the bundle.
