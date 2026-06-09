# The Migration Dashboard — Memory Prompt

Hand this file to a future Claude session to continue work on this dashboard. It is a complete, self-contained briefing of the project state, decisions, conventions, and what's done vs. what's left.

---

## Who & What

- **Client:** The Migration (Australian migration + education consultancy, AEST timezone).
- **Stack:** Streamlit (Python) dashboard at `migration-dashboard/dashboards/app.py`, DuckDB local store at `migration-dashboard/data/migration_dashboard.duckdb`, ETL at `migration-dashboard/etl/run_etl.py`, GHL/Meta/GA4/GSC connectors at `migration-dashboard/connectors/`.
- **GHL location id:** `Cy61ZIoB1Q68krX0lSZA`.
- **Marketing lead email:** `marketing@themigration.com.au`.
- **Run dashboard:** `.venv\Scripts\python.exe -m streamlit run migration-dashboard\dashboards\app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false` (Windows, PowerShell).
- **Run ETL:** `.venv\Scripts\python.exe migration-dashboard\etl\run_etl.py` (incremental — last 2 days). Add `--full` for a 2024-01-01 → today backfill, or `--since YYYY-MM-DD --until YYYY-MM-DD` for a custom window.

## Tabs in the dashboard

1. **Executive** — 7 scorecards, modal drill-downs, Goal Progress with user-editable targets.
2. **Meta Ads** — campaign performance, Mel/Syd account cards, GHL Leads alignment.
3. **Funnel & Pipeline** — placeholder, not yet built.
4. **Counsellors** — 6 scorecards, Trend/Table toggle, Performance Matrix with row-click drill-down.
5. **SEO & Traffic** — 10 scorecards, single unified Trend/Table view, modal drill-down.
6. **Forecast & Goals** — placeholder.
7. **Upload Reports** — placeholder.

---

## Core conventions (locked, don't break)

### Counsellors → city mapping
- **Melbourne office:** Gurbir Singh, Navneet Kaur.
- **Sydney office:** Turab, Nasir Nawaz, Kajal, Wajahad, Saurab.
- This is **counsellor-office** city, independent of `contact.city` (which is sparse).

### Counsellor → Type mapping
- **MARA**: Nasir Nawaz, Gurbir Singh
- **Career Counsellor**: Turab
- **Education**: Kajal, Wajahad, Saurab, **Navneet Kaur** (override — her name has "Career Counsellor" but per Marketing Lead she runs free education consults).
- Education counsellors don't accept payments — show "—" for Payment Pending + Total Payment in tables (Grand Total still aggregates).

### Calendar IDs per counsellor (hardcoded in COUNSELLORS list in `app.py` and embedded in SQL VALUES tables)
```
Turab        : aTMcDOwcpe5TOohPT1Rz, uwCBo7Y0cAWLs6ZqPjJI
Nasir Nawaz  : Zyrz08TZ6BaAruWxERy5, gttsLvMBPKFfslnOuwHT
Gurbir Singh : hsVntQS9KwIw8eF4D8ef, o4AfsJ45rEkewmENut12   ← Melbourne
Kajal        : 1FgpIJPxw6RWveeJLsb8, RF7bh7b3avrzStoTE8ho
Wajahad      : 4HLkV0BSHX7EvJ3jniC9, hsCSqcYHrXwL55NffEFi
Saurab       : 4mKKf1IPwIq50N4OzOTI, vjmOhJPIT4pAPzCyCmdT
Navneet Kaur : XJS0nt92447DgYSmxVkP, hkL937P7e6XTzy58dOZ7   ← Melbourne
```

### Excluded counsellors (per Marketing Lead)
Manhal Dandachi, Minhaz, Faheem — appointments on their calendars don't appear in any counsellor metric.

### Invalid appointments — GLOBALLY EXCLUDED
`LOWER(appointment_status) <> 'invalid'` is applied in **every** SQL view that touches `fact_appointments` (test bookings, ghost records). They never surface in any UI metric, scorecard, table, drill-down, or chart.

### Sat/Sun appointments — excluded from Counsellor metrics
`DAYOFWEEK(start_time) NOT IN (0, 6)` in counsellor views. Slots Available is also weekday-only (13/day × Mon-Fri weekdays × N counsellors) so booking rate can't exceed 100%.

### Slots Available formula (LOCKED)
`SLOTS_PER_DAY (=13) × weekdays_in_window × N_counsellors` — **NOT** per-calendar. Each counsellor has one slot pool of 13/day shared between online + onsite calendars (they don't add up to 26/day).

### Timezone for date comparisons
- Meta data (`fact_meta_daily.date`) is in **AEST** (ad-account timezone).
- GHL submissions (`fact_form_submissions.submitted_at`, `fact_survey_submissions.submitted_at`) are in **UTC**.
- When comparing to user-selected window: add 10 hours to UTC timestamps before casting to DATE — `CAST(fs.submitted_at + INTERVAL 10 HOUR AS DATE)`.

### Booking-stage set (used in SEO + Meta tabs)
- `L2C - Education` → Appointment Booked, Post Consultation, No Show, Initial Requested, Initial Received, COE Received
- `L2C - VISA` → all stages EXCEPT High Potential Clients
- `CLT - Onshore Admission` → all stages

### COE definition
COE = "COE Received" stage in **either L2C - Education OR CLT - Onshore Admission** (combined). Counted by opp.created_at.

### Website Lead cohort (SEO tab)
A contact whose **latest form/survey submission** has `event_source = 'Organic Search'` AND `fact_contacts.date_added` is in window. Lead date = contact-created date, never appointment date.

### Unified Lead cohort (Executive tab — vw_exec_unified_leads)
A contact whose **latest form/survey submission** is in window, OR (if no form/survey on file) a direct-booked **appointment** in window. One row per contact. Used for Total Leads / CPL / Lead→Booking / Show Rate.

### Total Leads bucketing (source_bucket)
- `Meta Paid` — campaign field populated OR event_source='Paid Social'
- `Organic / SEO` — event_source='Organic Search'
- `Referral` — event_source='Referral'
- `Direct` — event_source='Direct' / 'Direct traffic'
- `Social Media` — event_source='Social media'
- `Chatbot` — form_name LIKE '%chat%'
- `Referral / Direct` — `kind='appointment'` (direct booking, no form)
- `Email Marketing` / `Other` — fall-through

### Latest Source — 8-step live precedence (used in EVERY drill-down)
1. `campaign -- utm_content` (Meta-style attribution)
2. `campaign` alone
3. `form_name` (form submission)
4. `survey_name` (survey submission)
5. `event_form_name` (eventData.parentName)
6. `session_source` (lastAttributionSource.sessionSource)
7. `event_source` (eventData.source)
8. **Counsellor name from latest appointment's calendar** (fallback for booking-only contacts)
9. `fact_contacts.latest_attribution_source` (final fallback)

Computed live in SQL views — never read from the stored GHL `Latest Source` custom field.

### Chat Widget Form fix
GHL chat-widget submissions come with `productType='chat-widget'` and `formId='cwf-{locationId}'` but **no form_name**. `normalize_form_submissions` in `etl/normalize.py` detects these and sets `form_name = 'Chat Widget Form'`. The DB was patched retroactively for the 37 existing rows.

### "No counsellor" → "Unassigned"
All SQL views and Streamlit UI label the bucket for cohort contacts without an appointment as `Unassigned`, not "No counsellor".

---

## Schema additions (DuckDB tables)

- `dim_users` — id, full_name, email, deleted. From GHL `/users` (68 users).
- `fact_payments` — transaction_id (PK), contact_id, amount, amount_refunded, currency, status, entity_type, entity_source_name, payment_provider, created_at, fulfilled_at. From `/payments/transactions` with `altId`+`altType=location` params (NOT `locationId` — that 403s).
- `fact_survey_submissions` — same shape as `fact_form_submissions` plus `survey_id` + `survey_name`. From `/surveys/submissions`. Surveys list from `/surveys/`.
- `fact_form_submissions` extended with `event_form_name`, `event_source`, `page_url`, `page_path`, `referrer` (additive migrations).
- `fact_contact_latest_source` — mirror of GHL "Latest Source" custom field.

---

## GHL custom fields (used for writes)

- **Contact** "Latest Source": field id `MOxKefk4DsSy146Mcg0q`, constant `LATEST_SOURCE_CONTACT_FIELD` in `connectors/ghl.py`.
- **Opportunity** "Latest Source": field id `SIrT43qV7MXhLy6Er1ti`, constant `LATEST_SOURCE_OPP_FIELD`.

Write helpers in `connectors/ghl.py`:
- `update_contact_custom_field(contact_id, field_id, value)`
- `update_opportunity_custom_field(opp_id, field_id, value)`
- `fetch_contact_opportunities(contact_id)`

Payload shape: `{"customFields": [{"id": <field>, "value": <val>}]}` — NOT `field_value`.

---

## ETL pipeline (`etl/run_etl.py`)

Steps run in order:
1. `extract_ghl` — pipelines, stages, users, calendars, contacts, opportunities, appointments, **form submissions**, **survey submissions**, **payments**
2. `extract_meta` — ad-account insights
3. `extract_ga4` — sessions, pages, events
4. `extract_gsc` — search-console queries
5. `build_attribution_bridge` — Meta lead ↔ GHL contact matching
6. `sync_latest_source` — writes Latest Source field per contact + each opp (rate-limited 0.12s, retry on connection errors, skip on timeout)
7. `build_daily_rollups` — `agg_daily_kpis` table

### Backfill scripts (in `migration-dashboard/scripts/`)
- `_backfill_latest_source_formid_fallback.py` — patches contacts where latest submission has empty form_name but has formId (resolves via /forms/ lookup).
- `_backfill_latest_source_null_and_generic.py` — patches NULL + generic latest_source values across history.
- `_backfill_latest_source_phase2.py` — for contacts with no form/survey: fallback to survey name (if any) or calendar name. Took 1,933 contacts on May 28.
- `_fix_samisoni.py` — single-contact one-shot, demo of the pattern.
- `_ingest_payments.py`, `_ingest_surveys.py` — one-shot ETL fills.
- `_rebuild_agg.py` — rebuilds agg_daily_kpis after a crashed ETL.

### Phase 3 (NOT YET ACTIVATED): scheduled task
- `migration-dashboard/scripts/register_etl_task.ps1` — registers a Windows Scheduled Task `TheMigration-ETL-10min` that runs the incremental ETL every 10 min.
- **Not registered yet** — waiting for user approval (hold for explicit "do it").

---

## Common dashboard patterns (cargo-cult these when extending)

### Card-button scorecard CSS
Injected per-tab. `st.button(...)` rendered as 120px-tall card. Primary state is highlighted with gradient + 2px blue border. See `dashboards/app.py` for the `<style>` block.

### Click-once-active / click-twice-modal pattern
A scorecard click sets `st.session_state["{tab}_card"] = label` + `st.rerun()`. Re-clicking the same active scorecard calls the `@st.dialog` modal function directly. The Counsellors + SEO + Executive tabs all use this.

### Trend / Table toggle
`st.segmented_control(["Trend","Table"], key=...)`. **Important**: drive purely from `st.session_state` — `default=` + `key=` together causes desync in Streamlit. Initialize once, never pass `default=`.

### Row-click drill-down on dataframes
`st.dataframe(df, on_select="rerun", selection_mode="single-row", key=...)`. Read selection via `sel.selection.get("rows")` → first index → `df.iloc[i]`. Skip TOTAL/SUBTOTAL/Grand Total rows.

### Streamlit `@st.cache_data(ttl=60)` for `load_queries()`
`run_view`/`run_df` are defensive — `queries.get(view)` not `[view]`, return `{}` if the body or df is None/empty. This prevents hot-reload crashes when SQL files are edited.

### Defensive timezone
For datetime values that might come back as strings (Streamlit hot-reload edge case), use `isinstance(ts, str)` + `datetime.fromisoformat(ts)` fallback. See the freshness indicator at the top of `app.py`.

---

## Known data-quality findings (not bugs in the dashboard)

- **UM | Office Video | ABO** — Meta tracks 135 `pixel_custom` events in May; GHL has 0 form submissions matching that campaign. Likely the FB Lead Ads → GHL integration isn't connected for that campaign. **Action for user: check Settings → Integrations → Facebook in GHL.**
- **Meta vs GHL Leads gap (~90% match)** is real — Instant Form leads (Meta Lead Ads) not syncing to GHL is the primary cause. Timezone slippage accounts for 1-2% at month boundaries.
- **Empty `lastAttributionSource.campaign`** on ~17% of GHL form submissions. The dashboard's `vw_meta_ghl_leads_per_campaign` falls back to `fact_contact_latest_source.latest_source_campaign` when the submission's own campaign is blank.
- **Deleted-in-GHL contacts** still exist in our local DB (a handful return 400 "Contact not found" on writes). Low priority unless user requests purge.
- **Local DB completeness:** A full backfill was run May 27 — 13,176 contacts in `fact_contacts`. ETL was crashing on a single timeout pre-May 28; resilience fix in place since.

---

## What works now (verified)

- All scorecards on Executive / Counsellors / SEO / Meta Ads tabs.
- Card-button styling, click-twice-modal pattern across tabs.
- 8-step Latest Source live computation.
- Sat/Sun + invalid exclusion globally.
- Payment data from `/payments/transactions` (altId/altType pattern).
- Survey ingestion (790 submissions across 5 surveys).
- AEST timezone correction for Meta vs GHL date alignment.
- Performance Matrix row-click drill-down (per-counsellor email/source/payment table).
- Goal Progress with user-editable inputs (COE / Revenue / Lead Volume).
- CPL only displayed for Meta Paid (other sources show "—").
- COE = L2C-Education + CLT-Onshore Admission combined.

## What's NOT built yet

- **Auto-Insights rule engine** — the "Sydney Meta CPL dropped X% — recommend scaling" panel from the user's reference screenshots.
- **Lead Trend chart** — multi-source line chart (Meta Paid / Organic / Chatbot / Referral) for last 30 days.
- **Lead Source Mix donut** — interactive donut with click-to-filter.
- **Visa vs Admissions Pipeline bar chart**.
- **Funnel & Pipeline tab** — full content.
- **Forecast & Goals tab** — full content.
- **Upload Reports tab** — full content.
- **GHL Workflows** — user must build in UI; spec drafted in conversation but not registered (GHL doesn't expose `POST /workflows`).
- **Phase 3 scheduled task** — script ready, waiting for explicit register approval.

## Dead code (safe to clean up, not blocking)

- `_couns_city_card` — old side-by-side Mel/Syd renderer, replaced with unified view.
- `_seo_city_card` — same, SEO version.
- `_seo_add_city_subtotals` — old subtotal helper.
- `_is_edu_row` in matrix — local helper that's defined but not called inline.
- `vw_card_voes` — built then user said "skip VOEs", scorecard removed but SQL view remains.

---

## User preferences (operational)

- **Approve big actions explicitly.** When something destructive or system-level (registering scheduled tasks, stopping the dashboard, bulk GHL writes), confirm before doing. The user uses "proceed" as the green light.
- **Direct, terse responses.** Lead with the change, then a tight bullet list of what & why. Skip preamble.
- **Show table breakdowns when explaining number mismatches** — the side-by-side Meta vs GHL table that diagnosed the UM Office Video gap is the pattern they appreciate.
- **They iterate visually.** Expect screenshots → "make it look like this" → quick code adjustment → screenshot → iterate. Don't over-engineer a single iteration; ship & adjust.
- **When data is wrong, refresh ETL first.** The Counsellor counts mismatch was solved by an incremental ETL, not by changing SQL. Always probe DB state vs. live GHL UI before changing logic.
- **No emoji in code or files unless requested.** They do use emoji in chat captions (💡, 📋, ✅, ⚠) and those have proven OK in dashboard captions / markdown.

---

## File locations cheat-sheet

```
migration-dashboard/
├── dashboards/
│   ├── app.py                              ← Streamlit main entry (≈3500 lines)
│   ├── metrics.yaml                        ← (locked metric definitions, rarely touched)
│   └── sql/
│       ├── executive_cards.sql             ← Executive tab views (incl. vw_exec_unified_leads)
│       ├── counsellor_cards.sql            ← Counsellors tab views
│       └── tab_cards.sql                   ← Meta + SEO tab views (large file)
├── data/
│   └── migration_dashboard.duckdb          ← Local DB; locked when dashboard is running
├── etl/
│   ├── run_etl.py                          ← Pipeline orchestrator
│   └── normalize.py                        ← Per-API normalizers
├── connectors/
│   ├── ghl.py                              ← GHL API (most active; payments uses altId)
│   ├── meta.py
│   ├── ga4.py
│   └── gsc.py
├── models/
│   └── schema.sql                          ← DuckDB schema (run on every ETL init)
├── scripts/                                ← One-shot probes, backfills, ingests
└── MEMORY_PROMPT.md                        ← THIS FILE
```

---

## Quick start for a new session

1. **Skim this file end-to-end.** Don't skip the "locked conventions" section.
2. **Read** `dashboards/app.py` for the tab the user is asking about. The tabs are big; use `grep` to find specific scorecards / modals by name (e.g. `_modal_cpl`, `_couns_scorecard`).
3. **Check ETL freshness**: `Get-Item migration-dashboard/etl.log | Select LastWriteTime`. If older than a few hours and the user is asking about counts, run `python migration-dashboard/etl/run_etl.py` first.
4. **Before changing SQL**, verify with a probe script: `python migration-dashboard/scripts/_test_<view>.py` (existing patterns to copy).
5. **DB lock**: if writing to DuckDB, stop the dashboard first (find the Streamlit PID via `Get-Process python | Where-Object {...CommandLine...streamlit run}` and `Stop-Process -Force`), then restart after.
6. **For dashboard reruns:** Streamlit hot-reloads on save; the user can press R in the browser. After a SQL file edit, the `@st.cache_data(ttl=60)` may briefly miss — `run_view` has a guard against this.
