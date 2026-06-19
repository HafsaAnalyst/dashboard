"""
ETL orchestrator.

Usage:
    python etl/run_etl.py            # incremental: last 2 days
    python etl/run_etl.py --full     # full backfill from 2024-01-01

Reads credentials from .env (via python-dotenv inside the connectors).
Writes to data/migration_dashboard.duckdb.

Each extract step is wrapped in try/except so a single source failure
doesn't roll back data already written by earlier steps.
INSERT OR REPLACE everywhere — re-running for the same window is idempotent.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

# Make sibling packages importable when run directly
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load the project .env early so MOTHERDUCK_TOKEN (and API keys) are available
# before init_database() decides where to write.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT.parent / ".env")
except Exception:
    pass

from connectors import ga4, ghl, gsc, meta  # noqa: E402
from etl import normalize  # noqa: E402

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
SCHEMA_PATH = ROOT / "models" / "schema.sql"
FULL_REFRESH_START = "2024-01-01"
INCREMENTAL_DAYS = 2  # overlap window for late-arriving data
META_DAILY_LOOKBACK = 30  # re-fetch this many days of Meta daily spend each run
                          # (spend retro-updates; fills skipped days)
GSC_LOOKBACK = 14         # re-fetch this many days of GSC each run (data lands
                          # 2-3 days late and back-fills; avoids per-day gaps)

logger = logging.getLogger("etl")


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------

def init_database() -> duckdb.DuckDBPyConnection:
    # If a MotherDuck token is present, write to the shared cloud DB (so the
    # deployed + local dashboards refresh together). Otherwise the local file.
    md = os.getenv("MOTHERDUCK_TOKEN")
    if md:
        dbname = os.getenv("MOTHERDUCK_DATABASE", "migration")
        con = duckdb.connect(f"md:{dbname}?motherduck_token={md}")
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        logger.info("DB initialized on MotherDuck (%s)", dbname)
        return con
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    logger.info("DB initialized at %s", DB_PATH)
    return con


def upsert_df(con, table: str, df: pd.DataFrame, key_col: str) -> int:
    """INSERT OR REPLACE INTO {table} SELECT * FROM df, keyed by key_col.
    Returns the number of rows written."""
    if df is None or df.empty:
        logger.info("  %s: 0 rows (skipped)", table)
        return 0
    # Align column order with the table
    table_cols = [r[0] for r in con.execute(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_name='{table}' ORDER BY ordinal_position"
    ).fetchall()]
    # Drop columns we don't have, fill missing with None
    df2 = df.copy()
    for c in table_cols:
        if c not in df2.columns:
            df2[c] = None
    df2 = df2[table_cols]
    con.register("_stage", df2)
    # Delete then insert (DuckDB INSERT OR REPLACE on PK works cleanly)
    con.execute(f"DELETE FROM {table} WHERE {key_col} IN (SELECT {key_col} FROM _stage)")
    con.execute(f"INSERT INTO {table} SELECT * FROM _stage")
    con.unregister("_stage")
    logger.info("  %s: %d rows upserted", table, len(df2))
    return len(df2)


def rebuild_stage_observations(con) -> dict:
    """Rebuild fact_stage_observations: the point-in-time stage timeline, 1 row per
    (contact_id, AEST date) = the stage that contact was in that day (latest
    observation across all its opps). Derived from fact_opp_stage_events (stage
    transitions) + fact_opportunities (initial / no-event fallback). Computed
    server-side on MotherDuck so the Follower Performance views just ASOF-join it
    instead of rebuilding the timeline on every query."""
    logger.info("== STAGE OBSERVATIONS ==")
    try:
        con.execute("""
            CREATE OR REPLACE TABLE fact_stage_observations AS
            WITH events_obs AS (
                SELECT contact_id, changed_at AS obs_ts,
                       CAST(changed_at + INTERVAL 10 HOUR AS DATE) AS obs_date,
                       new_stage AS stage
                FROM fact_opp_stage_events
                WHERE contact_id IS NOT NULL AND new_stage IS NOT NULL
            ),
            first_evt AS (
                SELECT opportunity_id,
                       arg_min(old_stage, changed_at) FILTER (WHERE old_stage IS NOT NULL) AS init_stage
                FROM fact_opp_stage_events GROUP BY 1
            ),
            init_obs AS (
                SELECT o.contact_id, o.created_at AS obs_ts,
                       CAST(o.created_at + INTERVAL 10 HOUR AS DATE) AS obs_date,
                       f.init_stage AS stage
                FROM first_evt f
                JOIN fact_opportunities o ON o.opportunity_id = f.opportunity_id
                WHERE f.init_stage IS NOT NULL AND o.contact_id IS NOT NULL AND o.created_at IS NOT NULL
            ),
            noevt_obs AS (
                SELECT o.contact_id, o.created_at AS obs_ts,
                       CAST(o.created_at + INTERVAL 10 HOUR AS DATE) AS obs_date,
                       st.stage_name AS stage
                FROM fact_opportunities o
                LEFT JOIN dim_stages st ON st.stage_id = o.stage_id
                WHERE o.contact_id IS NOT NULL AND o.created_at IS NOT NULL
                  AND o.opportunity_id NOT IN (SELECT opportunity_id FROM fact_opp_stage_events
                                               WHERE opportunity_id IS NOT NULL)
            ),
            obs AS (
                SELECT * FROM events_obs
                UNION ALL SELECT * FROM init_obs
                UNION ALL SELECT * FROM noevt_obs
            )
            SELECT contact_id, obs_date, arg_max(stage, obs_ts) AS stage
            FROM obs WHERE obs_date IS NOT NULL GROUP BY 1, 2
        """)
        n = con.execute("SELECT COUNT(*) FROM fact_stage_observations").fetchone()[0]
        logger.info("  fact_stage_observations: %d rows", n)
        return {"fact_stage_observations": n}
    except Exception as e:
        logger.exception("rebuild_stage_observations failed: %s", e)
        return {}


# ---------------------------------------------------------------------
# Extract steps
# ---------------------------------------------------------------------

def extract_ghl(con, since: str, until: str) -> dict:
    logger.info("== GHL ==")
    summary = {}
    try:
        # Dim refresh — pipelines, stages, calendars (cheap, always do)
        pipelines_raw = ghl.fetch_pipelines()
        users_raw = ghl.fetch_users()
        calendars_raw = ghl.fetch_calendars()

        dim_pipe_df, dim_stages_df = normalize.normalize_pipelines(pipelines_raw)
        upsert_df(con, "dim_pipelines", dim_pipe_df, "pipeline_id")
        upsert_df(con, "dim_stages", dim_stages_df, "stage_id")
        summary["dim_pipelines"] = len(dim_pipe_df)
        summary["dim_stages"] = len(dim_stages_df)

        dim_cal_df = normalize.normalize_calendars(calendars_raw, users_raw)
        upsert_df(con, "dim_calendars", dim_cal_df, "calendar_id")
        summary["dim_calendars"] = len(dim_cal_df)

        # dim_users — maps assigned_user_id → human name for Owner columns
        users_df = pd.DataFrame([{
            "user_id":   u.get("id"),
            "full_name": u.get("name") or (
                f"{u.get('firstName') or ''} {u.get('lastName') or ''}".strip() or None),
            "email":     u.get("email"),
            "deleted":   bool(u.get("deleted") or False),
        } for u in (users_raw or [])])
        if not users_df.empty:
            users_df = users_df.dropna(subset=["user_id"]).drop_duplicates(subset=["user_id"])
            upsert_df(con, "dim_users", users_df, "user_id")
            summary["dim_users"] = len(users_df)

        # Contacts — must come before opportunities for FK lookups
        contacts_raw = ghl.fetch_contacts(since, until)
        contacts_df = normalize.normalize_contacts(contacts_raw)
        upsert_df(con, "fact_contacts", contacts_df, "contact_id")
        summary["fact_contacts"] = len(contacts_df)

        # Opportunities
        opps_raw = ghl.fetch_opportunities(since, until)
        stage_name_by_id = dict(zip(dim_stages_df["stage_id"], dim_stages_df["stage_name"])) \
            if not dim_stages_df.empty else {}
        opps_df = normalize.normalize_opportunities(opps_raw, stage_name_by_id)
        upsert_df(con, "fact_opportunities", opps_df, "opportunity_id")
        summary["fact_opportunities"] = len(opps_df)

        # Opportunity followers (presales agents). Replace the follower rows for
        # the opps in THIS batch (delete-then-insert) so follower changes — and
        # removals — are reflected, not just additions.
        if not opps_df.empty:
            con.register("_opp_batch", opps_df[["opportunity_id"]])
            con.execute("DELETE FROM fact_opportunity_followers WHERE opportunity_id "
                        "IN (SELECT opportunity_id FROM _opp_batch)")
            con.unregister("_opp_batch")
            foll_df = normalize.normalize_opportunity_followers(opps_raw)
            if not foll_df.empty:
                con.register("_foll_batch", foll_df)
                con.execute("INSERT INTO fact_opportunity_followers "
                            "SELECT opportunity_id, follower_user_id FROM _foll_batch")
                con.unregister("_foll_batch")
            summary["fact_opportunity_followers"] = len(foll_df)

        # Appointments — slowest GHL call (per-calendar pagination)
        appts_raw = ghl.fetch_appointments(since, until)
        cal_to_couns = dict(zip(dim_cal_df["calendar_id"], dim_cal_df["counsellor_id"]))
        appts_df = normalize.normalize_appointments(appts_raw, cal_to_couns)
        upsert_df(con, "fact_appointments", appts_df, "appointment_id")
        summary["fact_appointments"] = len(appts_df)

        # Form submissions — real form-fill events (powers returning-lead /
        # Meta-vs-GHL alignment). Server-side date filter, so cheap.
        try:
            subs_raw = ghl.fetch_form_submissions(since, until)
            subs_df = normalize.normalize_form_submissions(subs_raw)
            upsert_df(con, "fact_form_submissions", subs_df, "submission_id")
            summary["fact_form_submissions"] = len(subs_df)
        except Exception as e:
            logger.exception("GHL form submissions failed: %s", e)

        # Payment transactions — succeeded payments power the Counsellors tab
        # "Paid Consults" + Total Payment columns.
        try:
            pays_raw = ghl.fetch_payments(since, until)
            pays_df  = normalize.normalize_payments(pays_raw)
            upsert_df(con, "fact_payments", pays_df, "transaction_id")
            summary["fact_payments"] = len(pays_df)
        except Exception as e:
            logger.exception("GHL payments failed: %s", e)

        # Survey submissions — same shape as form submissions but from
        # /surveys/submissions. Needed for Latest Source fallback (Madina-style
        # contacts) and the SEO tab's Website Lead survey-name breakdown.
        try:
            surveys_list = ghl.fetch_surveys()
            surveys_by_id = {s["id"]: s["name"] for s in surveys_list
                             if s.get("id") and s.get("name")}
            surv_raw = ghl.fetch_survey_submissions(since, until)
            surv_df = normalize.normalize_survey_submissions(surv_raw, surveys_by_id)
            upsert_df(con, "fact_survey_submissions", surv_df, "submission_id")
            summary["fact_survey_submissions"] = len(surv_df)
        except Exception as e:
            logger.exception("GHL survey submissions failed: %s", e)

        # Conversations — messaging channel per contact (Facebook / Instagram /
        # WhatsApp / TikTok / ...). Lets us attribute Messenger-ad leads that
        # never filled a form. Incremental by lastMessageDate >= since.
        try:
            conv_raw = ghl.fetch_conversations(since)
            conv_df = normalize.normalize_conversations(conv_raw)
            upsert_df(con, "fact_conversations", conv_df, "conversation_id")
            summary["fact_conversations"] = len(conv_df)
        except Exception as e:
            conv_raw = []
            logger.exception("GHL conversations failed: %s", e)

        # Messages within the active conversations (those with a message since
        # `since`) → fact_messages, so we know WHICH staff member performed each
        # call / SMS / email / DM and WHEN (the basis for follower activity).
        try:
            msg_rows = []
            for cv in conv_raw:
                cvid = cv.get("id")
                if not cvid:
                    continue
                try:
                    msg_rows.extend(ghl.fetch_conversation_messages(cvid))
                except Exception:
                    continue
            msg_df = normalize.normalize_messages(msg_rows)
            upsert_df(con, "fact_messages", msg_df, "message_id")
            summary["fact_messages"] = len(msg_df)

            # Same message payloads carry opportunity stage-change activity logs
            # (TYPE_ACTIVITY_OPPORTUNITY) — reconstruct the per-opp stage timeline
            # so we can answer "what stage was this lead in on date D".
            stage_df = normalize.normalize_stage_events(msg_rows)
            upsert_df(con, "fact_opp_stage_events", stage_df, "event_id")
            summary["fact_opp_stage_events"] = len(stage_df)
        except Exception as e:
            logger.exception("GHL messages failed: %s", e)

        return summary
    except Exception as e:
        logger.exception("GHL extract failed: %s", e)
        return summary


def extract_meta(con, since: str, until: str) -> dict:
    logger.info("== META ==")
    summary = {}
    accounts = [
        ("Melbourne", os.getenv("META_MELBOURNE_AD_ACCOUNT_ID")),
        ("Sydney", os.getenv("META_SYDNEY_AD_ACCOUNT_ID")),
    ]
    # Meta spend retro-updates for several days (attribution windows), and a
    # single missed run leaves a permanent gap. So re-fetch a WIDER window for the
    # daily facts than the 2-day incremental overlap — this corrects late-arriving
    # spend and backfills any skipped day each run.
    daily_since = min(since, (datetime.now() - timedelta(days=META_DAILY_LOOKBACK)).strftime("%Y-%m-%d"))
    insights_total = 0
    daily_total = 0
    for label, acct in accounts:
        if not acct:
            logger.warning("%s ad account ID not set", label)
            continue
        try:
            campaigns = meta.fetch_campaign_insights(acct, since, until)
            df = normalize.normalize_meta_insights(campaigns, label, since, until)
            upsert_df(con, "fact_meta_insights", df, "insight_key")
            insights_total += len(df)
        except Exception as e:
            logger.exception("Meta campaign insights for %s failed: %s", label, e)
        try:
            daily = meta.fetch_daily_insights(acct, daily_since, until)
            df = normalize.normalize_meta_daily(daily, label)
            upsert_df(con, "fact_meta_daily", df, "daily_key")
            daily_total += len(df)
        except Exception as e:
            logger.exception("Meta daily for %s failed: %s", label, e)
    summary["fact_meta_insights"] = insights_total
    summary["fact_meta_daily"] = daily_total
    return summary


def extract_ga4(con, since: str, until: str) -> dict:
    logger.info("== GA4 ==")
    summary = {}
    try:
        sess = ga4.fetch_daily_sessions(since, until)
        df = normalize.normalize_ga4_sessions(sess)
        upsert_df(con, "fact_ga4_sessions", df, "session_key")
        summary["fact_ga4_sessions"] = len(df)
    except Exception as e:
        logger.exception("GA4 sessions failed: %s", e)
    try:
        # Date-only site totals so Sessions / Total Users / Engagement Rate match
        # the GA4 UI (the 5-dimension fact_ga4_sessions over-counts via (other)).
        daily = ga4.fetch_daily_totals(since, until)
        df = normalize.normalize_ga4_daily(daily)
        upsert_df(con, "fact_ga4_daily", df, "date")
        summary["fact_ga4_daily"] = len(df)
    except Exception as e:
        logger.exception("GA4 daily totals failed: %s", e)
    try:
        pages = ga4.fetch_top_pages(since, until)
        df = normalize.normalize_ga4_pages(pages)
        upsert_df(con, "fact_ga4_pages", df, "page_key")
        summary["fact_ga4_pages"] = len(df)
    except Exception as e:
        logger.exception("GA4 pages failed: %s", e)
    try:
        events = ga4.fetch_key_events(since, until)
        df = normalize.normalize_ga4_events(events)
        upsert_df(con, "fact_ga4_events", df, "event_key")
        summary["fact_ga4_events"] = len(df)
    except Exception as e:
        logger.exception("GA4 events failed: %s", e)
    return summary


def extract_gsc(con, since: str, until: str) -> dict:
    logger.info("== GSC ==")
    summary = {}
    total = 0
    # GSC data lands 2-3 days late and back-fills retroactively — longer than the
    # 2-day incremental overlap, so days otherwise fall through the cracks (gaps).
    # Re-fetch a wider window each run so lagging days are captured and corrected.
    gsc_since = min(since, (datetime.now() - timedelta(days=GSC_LOOKBACK)).strftime("%Y-%m-%d"))
    # query/page limits raised to cover the wider window without truncation; the
    # site-wide totals (vw_gsc_tab_totals) use the 'device' dimension, which is
    # tiny (≤ devices × dates) and never truncates.
    for dim, limit in [("query", 25000), ("page", 25000), ("country", 1000), ("device", 100)]:
        try:
            rows = gsc.fetch_search_analytics(gsc_since, until, dim, limit)
            df = normalize.normalize_gsc(rows)
            n = upsert_df(con, "fact_gsc_queries", df, "gsc_key")
            total += n
        except Exception as e:
            logger.exception("GSC %s failed: %s", dim, e)
    summary["fact_gsc_queries"] = total
    return summary


# ---------------------------------------------------------------------
# Bridge + rollups
# ---------------------------------------------------------------------

def build_attribution_bridge(con, since: str, until: str) -> dict:
    """Pull Meta lead-form submissions, match to GHL contacts."""
    logger.info("== BRIDGE ==")
    accounts = [
        ("Melbourne", os.getenv("META_MELBOURNE_AD_ACCOUNT_ID")),
        ("Sydney", os.getenv("META_SYDNEY_AD_ACCOUNT_ID")),
    ]
    all_leads: list[dict] = []
    for label, acct in accounts:
        if not acct:
            continue
        try:
            leads = meta.fetch_lead_form_submissions(acct, since, until)
            all_leads.extend(leads)
        except Exception as e:
            logger.warning("Meta lead retrieval %s failed (likely missing scope): %s",
                           label, e)
    if not all_leads:
        logger.info("  no Meta leads pulled (scope or empty)")
        return {"bridge_lead_attribution": 0}
    contacts_df = con.execute(
        "SELECT contact_id, email, phone, date_added FROM fact_contacts"
    ).fetchdf()
    bridge_df = normalize.build_bridge(all_leads, contacts_df)
    n = upsert_df(con, "bridge_lead_attribution", bridge_df, "bridge_id")
    return {"bridge_lead_attribution": n}


def sync_latest_source(con, since: str, until: str) -> dict:
    """Push the 'Latest Source' custom field to GHL for any contact who filled a
    form in [since, until]. Value = full source (campaign -- ad) of their most
    recent in-window form fill. Writes the contact field + the field on ALL of
    that contact's opportunities. Skips fills with no campaign. Rate-limited."""
    logger.info("== LATEST SOURCE SYNC ==")
    # form_id → form_name lookup. Some submissions (funnel page-visit type)
    # have form_name='' on the row itself but still carry a real form_id —
    # we fall back to this dict so e.g. /confirm-your-seat-page submissions
    # resolve to their actual form name ('Trade Course Offer Form') instead
    # of dropping to the generic 'Social media' session source.
    forms_by_id: dict[str, str] = {}
    try:
        for f in ghl.fetch_forms():
            fid = f.get("id"); fnm = f.get("name")
            if fid and fnm:
                forms_by_id[fid] = fnm
        logger.info("  loaded %d form-name lookups", len(forms_by_id))
    except Exception:
        logger.exception("forms-list lookup failed (continuing without)")

    try:
        rows = con.execute(
            """
            WITH ranked AS (
                SELECT contact_id, form_id,
                       -- Value precedence: Meta 'campaign -- ad' first, then any FORM NAME
                       -- (facebookFormName / last_attr.formName / parentName — all stored in
                       -- form_name), THEN session/event source as a last resort. Putting
                       -- form_name before session_source avoids overwriting specific form
                       -- names like 'Facebook Lead Form' with the generic 'Paid Social'.
                       COALESCE(
                           CASE WHEN COALESCE(campaign,'') <> '' AND COALESCE(utm_content,'') <> ''
                                     THEN campaign || ' -- ' || utm_content
                                WHEN COALESCE(campaign,'') <> '' THEN campaign END,
                           NULLIF(form_name, ''),
                           NULLIF(event_form_name, ''),
                           NULLIF(session_source, ''),
                           NULLIF(event_source, '')
                       ) AS full_source,
                       ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
                FROM fact_form_submissions
                WHERE CAST(submitted_at AS DATE) BETWEEN ? AND ?
            )
            SELECT contact_id, form_id, full_source FROM ranked WHERE rn = 1
            """,
            [since, until],
        ).fetchall()
    except Exception as e:
        logger.exception("latest-source query failed: %s", e)
        return {"latest_source_synced": 0}

    # Resolve generic / empty values via formId → form_name lookup.
    GENERIC = {"Social media", "Paid Social", "Direct traffic", "Organic Search",
               "Referral", "Email", "SMS", "Direct", ""}
    resolved: list[tuple[str, str]] = []
    upgraded = 0
    for cid, fid, val in rows:
        v = val or ""
        if v in GENERIC and fid and fid in forms_by_id:
            v = forms_by_id[fid]
            upgraded += 1
        if v:
            resolved.append((cid, v))
    rows = resolved
    if upgraded:
        logger.info("  upgraded %d generic-value latest sources via forms lookup",
                    upgraded)

    # Resilience: any single API write may time out or get reset by the GHL
    # gateway. Catch everything per-call so one bad contact never kills the
    # whole ETL run. Errors are logged, the loop continues.
    import requests as _requests

    def _safe_write_contact(cid_, val_):
        try:
            return ghl.update_contact_custom_field(
                cid_, ghl.LATEST_SOURCE_CONTACT_FIELD, val_)
        except (_requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError,
                ConnectionResetError) as e:
            logger.warning("  contact %s write skipped: %s", cid_, type(e).__name__)
            return False

    def _safe_write_opp(oid_, val_):
        try:
            return ghl.update_opportunity_custom_field(
                oid_, ghl.LATEST_SOURCE_OPP_FIELD, val_)
        except (_requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError,
                ConnectionResetError) as e:
            logger.warning("  opp %s write skipped: %s", oid_, type(e).__name__)
            return False

    def _safe_fetch_opps(cid_):
        try:
            return ghl.fetch_contact_opportunities(cid_)
        except (_requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError,
                ConnectionResetError) as e:
            logger.warning("  opps fetch skipped for %s: %s",
                           cid_, type(e).__name__)
            return []

    n = opp_writes = 0
    for cid, val in rows:
        if _safe_write_contact(cid, val):
            n += 1
        time.sleep(0.12)
        for o in _safe_fetch_opps(cid):
            time.sleep(0.12)
            if _safe_write_opp(o.get("id"), val):
                opp_writes += 1
            time.sleep(0.12)
    logger.info("  latest source: %d contacts, %d opportunities updated", n, opp_writes)
    return {"latest_source_synced": n}


def build_daily_rollups(con, since: str, until: str) -> dict:
    """Populate agg_daily_kpis from fact tables."""
    logger.info("== AGG ==")
    sql = f"""
    -- Build agg_daily_kpis for every date in [since, until].
    WITH date_spine AS (
        SELECT * FROM (
            SELECT unnest(generate_series(
                DATE '{since}', DATE '{until}', INTERVAL '1 day')) AS d
        )
    ),
    meta_by_date AS (
        SELECT date AS d,
               SUM(spend) AS spend,
               SUM(total_leads) AS paid_leads
        FROM fact_meta_daily
        WHERE date BETWEEN DATE '{since}' AND DATE '{until}'
        GROUP BY date
    ),
    ga4_by_date AS (
        SELECT date AS d,
               SUM(sessions) AS sessions,
               SUM(active_users) AS active_users
        FROM fact_ga4_sessions
        WHERE date BETWEEN DATE '{since}' AND DATE '{until}'
        GROUP BY date
    ),
    gsc_by_date AS (
        -- Use the `device` dimension (3 buckets max) for accurate daily totals.
        -- The `query` dimension is capped at 5000 rows per ETL pull and
        -- under-counts daily clicks once long-tail queries are truncated.
        SELECT date AS d,
               SUM(clicks) AS clicks,
               SUM(impressions) AS impressions
        FROM fact_gsc_queries
        WHERE dimension_name = 'device'
          AND date BETWEEN DATE '{since}' AND DATE '{until}'
        GROUP BY date
    ),
    contacts_by_date AS (
        SELECT CAST(date_added AS DATE) AS d,
               COUNT(*) FILTER (WHERE canonical_source = 'meta_paid') AS paid_c,
               COUNT(*) FILTER (WHERE canonical_source <> 'meta_paid') AS organic_c,
               COUNT(*) AS total_c
        FROM fact_contacts
        WHERE date_added BETWEEN TIMESTAMP '{since}' AND TIMESTAMP '{until} 23:59:59'
        GROUP BY CAST(date_added AS DATE)
    ),
    opps_by_date AS (
        SELECT CAST(created_at AS DATE) AS d,
               COUNT(*) AS new_opps,
               COUNT(*) FILTER (WHERE status='won') AS won_opps,
               SUM(monetary_value) AS total_value,
               SUM(monetary_value) FILTER (WHERE status='won') AS won_val
        FROM fact_opportunities
        WHERE created_at BETWEEN TIMESTAMP '{since}' AND TIMESTAMP '{until} 23:59:59'
        GROUP BY CAST(created_at AS DATE)
    ),
    appts_by_date AS (
        SELECT CAST(start_time AS DATE) AS d,
               COUNT(*) AS bookings,
               COUNT(*) FILTER (WHERE canonical_outcome='show') AS shows,
               COUNT(*) FILTER (WHERE canonical_outcome='noshow') AS no_shows
        FROM fact_appointments
        WHERE start_time BETWEEN TIMESTAMP '{since}' AND TIMESTAMP '{until} 23:59:59'
        GROUP BY CAST(start_time AS DATE)
    ),
    initials_by_date AS (
        SELECT CAST(o.updated_at AS DATE) AS d,
               COUNT(*) FILTER (WHERE s.funnel_step='initial_requested') AS initials,
               COUNT(*) FILTER (WHERE s.funnel_step='paid')              AS paid_n,
               COUNT(*) FILTER (WHERE s.funnel_step='coe_voe')           AS coe_voe
        FROM fact_opportunities o
        LEFT JOIN dim_stages s ON s.stage_id = o.stage_id
        WHERE o.updated_at BETWEEN TIMESTAMP '{since}' AND TIMESTAMP '{until} 23:59:59'
        GROUP BY CAST(o.updated_at AS DATE)
    )
    SELECT
        ds.d AS date,
        COALESCE(c.total_c, 0)                  AS total_leads,
        COALESCE(c.paid_c, 0)                   AS paid_leads,
        COALESCE(c.organic_c, 0)                AS organic_leads,
        COALESCE(a.bookings, 0)                 AS bookings,
        COALESCE(a.shows, 0)                    AS shows,
        COALESCE(a.no_shows, 0)                 AS no_shows,
        COALESCE(i.initials, 0)                 AS initials_requested,
        COALESCE(i.paid_n, 0)                   AS paid_count,
        COALESCE(i.coe_voe, 0)                  AS coe_voe,
        COALESCE(m.spend, 0)                    AS meta_spend,
        COALESCE(o.new_opps, 0)                 AS new_opportunities,
        COALESCE(o.won_opps, 0)                 AS won_opportunities,
        COALESCE(o.total_value, 0)              AS pipeline_value_added,
        COALESCE(o.won_val, 0)                  AS won_value,
        COALESCE(g.sessions, 0)                 AS ga4_sessions,
        COALESCE(g.active_users, 0)             AS ga4_active_users,
        COALESCE(s.clicks, 0)                   AS gsc_clicks,
        COALESCE(s.impressions, 0)              AS gsc_impressions,
        CASE WHEN COALESCE(c.total_c,0) > 0
             THEN COALESCE(m.spend,0) / c.total_c
             ELSE 0 END                         AS cpl,
        CURRENT_TIMESTAMP                       AS last_refreshed
    FROM date_spine ds
    LEFT JOIN meta_by_date     m ON m.d = ds.d
    LEFT JOIN ga4_by_date      g ON g.d = ds.d
    LEFT JOIN gsc_by_date      s ON s.d = ds.d
    LEFT JOIN contacts_by_date c ON c.d = ds.d
    LEFT JOIN opps_by_date     o ON o.d = ds.d
    LEFT JOIN appts_by_date    a ON a.d = ds.d
    LEFT JOIN initials_by_date i ON i.d = ds.d
    ORDER BY ds.d
    """
    df = con.execute(sql).fetchdf()
    if df.empty:
        logger.info("  agg_daily_kpis: 0 rows (date spine empty?)")
        return {"agg_daily_kpis": 0}
    n = upsert_df(con, "agg_daily_kpis", df, "date")
    return {"agg_daily_kpis": n}


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def run_etl(full_refresh: bool = False) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if full_refresh:
        since = FULL_REFRESH_START
        logger.info("FULL REFRESH from %s to %s", since, today)
    else:
        since = (datetime.now() - timedelta(days=INCREMENTAL_DAYS)).strftime("%Y-%m-%d")
        logger.info("INCREMENTAL from %s to %s", since, today)
    until = today

    con = init_database()
    try:
        summary = {}
        summary.update(extract_ghl(con, since, until))
        summary.update(rebuild_stage_observations(con))
        summary.update(extract_meta(con, since, until))
        summary.update(extract_ga4(con, since, until))
        summary.update(extract_gsc(con, since, until))
        summary.update(build_attribution_bridge(con, since, until))
        summary.update(sync_latest_source(con, since, until))
        summary.update(build_daily_rollups(con, since, until))

        logger.info("== ETL SUMMARY ==")
        for k, v in sorted(summary.items()):
            logger.info("  %-30s %d rows", k, v)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="The Migration ETL")
    parser.add_argument("--full", action="store_true",
                        help=f"Full backfill from {FULL_REFRESH_START}")
    parser.add_argument("--since", help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Override end date (YYYY-MM-DD)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ROOT / "etl.log", mode="a", encoding="utf-8"),
        ],
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.since or args.until:
        # Custom window mode
        since = args.since or (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        until = args.until or datetime.now().strftime("%Y-%m-%d")
        logger.info("CUSTOM RANGE %s to %s", since, until)
        con = init_database()
        try:
            summary = {}
            summary.update(extract_ghl(con, since, until))
            summary.update(rebuild_stage_observations(con))
            summary.update(extract_meta(con, since, until))
            summary.update(extract_ga4(con, since, until))
            summary.update(extract_gsc(con, since, until))
            summary.update(build_attribution_bridge(con, since, until))
            summary.update(build_daily_rollups(con, since, until))
            logger.info("== SUMMARY ==")
            for k, v in sorted(summary.items()):
                logger.info("  %-30s %d", k, v)
        finally:
            con.close()
    else:
        run_etl(full_refresh=args.full)


if __name__ == "__main__":
    main()
