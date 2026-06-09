# The Migration — Marketing Intelligence Dashboard

Backend ETL + DuckDB store that consolidates data from **GHL** (CRM), **Meta Ads**, **Google Analytics 4**, and **Google Search Console** into a single analytical store ready for a Streamlit dashboard.

---

## Quick start

```powershell
# 1. Install deps
pip install -r requirements.txt   # or: pip install requests pandas duckdb python-dotenv google-analytics-data google-api-python-client pytest

# 2. Verify .env is configured at the project root (parent of migration-dashboard/)
#    Required keys: GHL_API_KEY, GHL_LOCATION_ID, META_ACCESS_TOKEN,
#    META_MELBOURNE_AD_ACCOUNT_ID, META_SYDNEY_AD_ACCOUNT_ID,
#    GA4_PROPERTY_ID, GSC_SITE_URL,
#    GA4_SERVICE_ACCOUNT_FILE, GSC_SERVICE_ACCOUNT_FILE

# 3. Full backfill from 2024-01-01
python etl/run_etl.py --full

# 4. Or pull a specific window
python etl/run_etl.py --since 2026-04-01 --until 2026-04-30

# 5. Daily incremental (last 2 days, default)
python etl/run_etl.py

# 6. Tests
pytest tests/ -v
```

---

## Architecture

```
+----------------+     +--------------------+     +---------------+
|  4 connectors  | --> | etl/normalize.py   | --> | DuckDB store  |
|  (raw lists)   |     | (canonical fields) |     | (15 tables)   |
+----------------+     +--------------------+     +---------------+
        ^                                                 |
        |                                                 v
   .env credentials                              data/migration_dashboard.duckdb
                                                    (Streamlit dashboard reads from here)
```

| Folder | Purpose |
|--------|---------|
| `connectors/` | Plain-Python wrappers around each external API. Return `list[dict]`. No DataFrames, no DB writes. |
| `etl/normalize.py` | Maps raw API responses → DataFrames matching fact-table schemas. Handles canonical mappings (sources, loss reasons, funnel stages). |
| `etl/run_etl.py` | Orchestrator. Init DB → 4 extracts → bridge → daily rollups. Each step in `try/except` so one failure doesn't roll back the others. |
| `models/schema.sql` | Full DuckDB schema — `CREATE TABLE IF NOT EXISTS` everywhere; safe to re-run. |
| `data/` | Where `migration_dashboard.duckdb` is written (gitignored). |
| `tests/` | pytest sanity checks — schema, leads identity, idempotency, April matches verification. |

---

## Schema overview (15 tables)

### Dimensions
| Table | Grain |
|-------|-------|
| `dim_counsellors` | 1 row per named consultant (8 seeded). Has `is_paid`, `rate_in_person`, `rate_online`. |
| `dim_calendars` | 1 row per GHL calendar (~53). Maps to a counsellor where the calendar name fuzzy-matches one of the 8 named consultants. |
| `dim_pipelines` | 1 row per GHL pipeline (28). `is_canonical=TRUE` for the 6 we report on. |
| `dim_stages` | 1 row per pipeline stage. `funnel_step` maps to one of: `lead`, `booking`, `show`, `initial_requested`, `paid`, `coe_voe`. |

### Facts
| Table | Grain | Composite key |
|-------|-------|---------------|
| `fact_contacts` | 1 row per GHL contact | `contact_id` |
| `fact_opportunities` | 1 row per GHL opportunity | `opportunity_id` |
| `fact_appointments` | 1 row per GHL calendar event | `appointment_id` |
| `fact_meta_insights` | 1 row per (account, campaign, since..until ETL window) | `insight_key` = `{account}|{campaign}|{since}|{until}` |
| `fact_meta_daily` | 1 row per (account, campaign, date, country) | `daily_key` |
| `fact_ga4_sessions` | 1 row per (date, source, medium, country, city) | `session_key` |
| `fact_ga4_pages` | 1 row per (date, page_path, country) | `page_key` |
| `fact_ga4_events` | 1 row per (date, event_name, country) | `event_key` |
| `fact_gsc_queries` | 1 row per (date, dimension_name, dimension_value) — single table holds all 4 GSC dimensions | `gsc_key` |

### Bridge & Aggregation
| Table | Grain |
|-------|-------|
| `bridge_lead_attribution` | Maps Meta `lead_id` ↔ GHL `contact_id` (matched on email then phone). Currently empty pending `leads_retrieval` token scope. |
| `agg_daily_kpis` | 1 row per date — pre-computed daily metrics for fast dashboard reads. |

---

## Canonical decisions baked into the schema

### Meta "Total Leads"
The Meta `Results` column varies per campaign by optimization goal — it's not stable. We define a **canonical** total instead:

```
total_leads = instant_form_leads               (action_type 'lead')
            + pixel_lead_events                (offsite_conversion.fb_pixel_lead)
            + pixel_custom_events              (offsite_conversion.fb_pixel_custom)
```

For per-campaign drill-down that mirrors Ads Manager's `Results` column, `fact_meta_insights` also stores `result_event` and `result_count` — populated using a heuristic validated to match Melbourne April 2026 = 153 exactly.

### Source canonicalisation (GHL)
Free-text `source` field → one of: `meta_paid`, `organic_seo`, `chatbot`, `referral`, `website_form`, `survey`, `other`. See `etl/normalize.py::map_canonical_source`.

### Loss reasons (GHL)
Free-text `lostReason` → one of: `not_qualified`, `no_response`, `budget`, `competitor`, `other`.

### Appointment outcomes (GHL)
GHL has many statuses (`showed`, `noshow`, `confirmed`, `cancelled`, `invalid`, `new`, ...). We collapse to: `show`, `noshow`, `cancelled`, `pending`.

---

## Inspecting the DB

```powershell
duckdb data/migration_dashboard.duckdb
```

```sql
-- Headline daily KPIs
SELECT * FROM agg_daily_kpis
WHERE date BETWEEN '2026-04-01' AND '2026-04-30'
ORDER BY date DESC;

-- April Meta campaign performance
SELECT account_label, campaign_name, spend, total_leads, result_event, result_count
FROM fact_meta_insights
WHERE date_start = '2026-04-01' AND date_end = '2026-04-30'
ORDER BY spend DESC;

-- Pipeline funnel for L2C - Education in April
SELECT s.stage_name, s.funnel_step, COUNT(*) AS opps
FROM fact_opportunities o
JOIN dim_stages s ON s.stage_id = o.stage_id
JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
WHERE p.pipeline_name = 'L2C - Education'
  AND o.created_at BETWEEN '2026-04-01' AND '2026-04-30'
GROUP BY s.stage_name, s.funnel_step, s.stage_order
ORDER BY s.stage_order;

-- Counsellor capacity (April)
SELECT c.full_name, c.is_paid,
       COUNT(*) FILTER (WHERE a.canonical_outcome = 'show')   AS shows,
       COUNT(*) FILTER (WHERE a.canonical_outcome = 'noshow') AS no_shows,
       SUM(a.amount_paid) AS revenue
FROM fact_appointments a
JOIN dim_counsellors c ON c.counsellor_id = a.counsellor_id
WHERE a.start_time BETWEEN '2026-04-01' AND '2026-04-30'
GROUP BY c.counsellor_id, c.full_name, c.is_paid
ORDER BY shows DESC;

-- Top 20 organic queries (April)
SELECT dimension_value AS query, SUM(clicks) AS clicks,
       SUM(impressions) AS impressions, AVG(position) AS avg_pos
FROM fact_gsc_queries
WHERE dimension_name = 'query'
  AND date BETWEEN '2026-04-01' AND '2026-04-30'
GROUP BY dimension_value
ORDER BY clicks DESC LIMIT 20;
```

---

## Idempotency & re-runs

Every `INSERT` is wrapped via `INSERT OR REPLACE` semantics in `etl/run_etl.py::upsert_df`:

```python
DELETE FROM {table} WHERE {key_col} IN (SELECT {key_col} FROM stage)
INSERT INTO {table} SELECT * FROM stage
```

Running the same window twice produces identical row counts. The `tests/test_etl.py` suite has explicit idempotency checks (`test_idempotency_*`).

---

## Known limitations

| Area | Limitation | Mitigation |
|------|------------|------------|
| `bridge_lead_attribution` | Token lacks `leads_retrieval` scope, so `/leadgen_forms` returns 400. Bridge stays empty. | Generate a System User token with `ads_management` + `leads_retrieval` scopes. Code path is in place — will populate as soon as scope is granted. |
| GHL contacts/payments | API doesn't support server-side date filtering. Currently paginates DESC by `dateAdded`/`createdAt` then stops once `since` is passed. Slow as data grows. | Acceptable for current data volume (~1k contacts/month). Re-evaluate at 50k+ contacts/month. |
| GSC `query` dimension | 5,000-row cap truncates the long tail. `agg_daily_kpis.gsc_clicks` is rolled up from `device` dimension instead (small cardinality, accurate totals). | Working as designed — dashboard should reference `device`-rolled-up totals for headline numbers, `query` for keyword analysis only. |
| GA4 sampling | High-volume reports may apply sampling. | Fetch sizes are well below GA4 thresholds; revisit if discrepancies appear. |

---

## Files at the project root (one level up from `migration-dashboard/`)

These live outside this folder because they're project-wide secrets/keys:
- `.env`
- `ghldataset-711aae0e1998 (1).json` — GA4 service account
- `ghldataset-6a82bf2f346d.json` — GSC service account

The connectors find them by resolving paths relative to the `.env` location, so they work regardless of which directory you run the ETL from.

---

## Running on a schedule

```powershell
# Every night at 03:00 — Windows Task Scheduler
python etl/run_etl.py >> etl_cron.log 2>&1
```

The default (no flags) is incremental: pulls the last 2 days. Re-running for overlapping dates is safe.
