-- =====================================================================
-- The Migration — Marketing Intelligence DuckDB schema
-- Run with:  duckdb data/migration_dashboard.duckdb < models/schema.sql
--
-- Conventions:
--   * VARCHAR for all IDs (matches what APIs return — strings, not ints)
--   * DATE for day-grain, TIMESTAMP for instant-grain
--   * DECIMAL(12,2) for money, DECIMAL(8,4) for rates/percentages
--   * Composite PKs are stored as a derived `*_key` VARCHAR column to keep
--     INSERT OR REPLACE simple
--   * Every CREATE uses IF NOT EXISTS — re-running is safe
-- =====================================================================


-- =====================================================================
-- DIMENSIONS
-- =====================================================================

-- 1 row per named consultant.
-- Seeded below from the sprint brief (8 people).
-- ghl_user_id is backfilled by the ETL once it resolves names against /users.
CREATE TABLE IF NOT EXISTS dim_counsellors (
    counsellor_id      VARCHAR PRIMARY KEY,    -- e.g. usr_wajahat
    full_name          VARCHAR NOT NULL,
    role               VARCHAR,
    location           VARCHAR,
    is_paid            BOOLEAN NOT NULL,
    rate_in_person     DECIMAL(10,2),          -- AUD per IP consult
    rate_online        DECIMAL(10,2),          -- AUD per online consult
    ghl_user_id        VARCHAR                 -- backfilled by ETL
);

-- 1 row per GHL calendar (~53 in this account).
-- counsellor_id is NULL for calendars that don't belong to a named consultant.
CREATE TABLE IF NOT EXISTS dim_calendars (
    calendar_id              VARCHAR PRIMARY KEY,
    calendar_name            VARCHAR,
    counsellor_id            VARCHAR,          -- FK to dim_counsellors (nullable)
    is_named_consultant      BOOLEAN DEFAULT FALSE,
    is_active                BOOLEAN DEFAULT TRUE
);

-- 1 row per GHL pipeline (28 total).
-- is_canonical=TRUE for the 6 pipelines we report on:
--   L2C - Education, L2C - VISA, CLT - Onshore Admission,
--   CLT - VISA, L2C - Skill Migration, Accounts
CREATE TABLE IF NOT EXISTS dim_pipelines (
    pipeline_id        VARCHAR PRIMARY KEY,
    pipeline_name      VARCHAR NOT NULL,
    pipeline_type      VARCHAR,                -- 'admissions' | 'visa' | 'accounts' | 'other'
    is_canonical       BOOLEAN DEFAULT FALSE
);

-- 1 row per pipeline stage. funnel_step lets the dashboard map a stage to one
-- of the 6 sprint funnel buckets without hardcoding stage names per pipeline.
CREATE TABLE IF NOT EXISTS dim_stages (
    stage_id           VARCHAR PRIMARY KEY,
    stage_name         VARCHAR NOT NULL,
    pipeline_id        VARCHAR NOT NULL,       -- FK to dim_pipelines
    stage_order        INTEGER,
    funnel_step        VARCHAR                 -- lead|booking|show|initial_requested|paid|coe_voe|other
);


-- =====================================================================
-- FACTS — GHL
-- =====================================================================

-- Grain: 1 row per GHL contact.
-- canonical_source maps GHL's free-text source field to one of:
--   meta_paid | organic_seo | chatbot | referral | website_form | survey | other
CREATE TABLE IF NOT EXISTS fact_contacts (
    contact_id                   VARCHAR PRIMARY KEY,
    contact_name                 VARCHAR,
    email                        VARCHAR,
    phone                        VARCHAR,
    date_added                   TIMESTAMP,
    date_updated                 TIMESTAMP,
    source                       VARCHAR,
    canonical_source             VARCHAR,
    assigned_user_id             VARCHAR,
    first_attribution_source     VARCHAR,
    latest_attribution_source    VARCHAR,
    country                      VARCHAR,
    city                         VARCHAR,
    visa_type                    VARCHAR
);
-- Richer attribution detail captured from the GHL `attributions` array.
-- medium = facebook | tiktok | ig | survey | manual | calendar | ...
-- form   = the lead-form name when one was submitted (NULL otherwise)
-- DuckDB no-op if the column already exists.
ALTER TABLE fact_contacts ADD COLUMN IF NOT EXISTS first_attribution_medium  VARCHAR;
ALTER TABLE fact_contacts ADD COLUMN IF NOT EXISTS first_attribution_form    VARCHAR;
ALTER TABLE fact_contacts ADD COLUMN IF NOT EXISTS latest_attribution_medium VARCHAR;
ALTER TABLE fact_contacts ADD COLUMN IF NOT EXISTS latest_attribution_form   VARCHAR;
-- campaign from the GHL contact attribution (e.g. a Facebook Lead Form's
-- campaign) — the only campaign signal for Meta Instant-Form leads that never
-- create a form submission or Latest-Source row.
ALTER TABLE fact_contacts ADD COLUMN IF NOT EXISTS attribution_campaign      VARCHAR;

-- Grain: 1 row per GHL opportunity.
-- canonical_loss_reason maps free-text loss reasons to 5 buckets:
--   not_qualified | no_response | budget | competitor | other
CREATE TABLE IF NOT EXISTS fact_opportunities (
    opportunity_id          VARCHAR PRIMARY KEY,
    contact_id              VARCHAR,           -- FK to fact_contacts
    opportunity_name        VARCHAR,
    pipeline_id             VARCHAR,           -- FK to dim_pipelines
    stage_id                VARCHAR,           -- FK to dim_stages
    status                  VARCHAR,           -- open | won | lost | abandoned
    monetary_value          DECIMAL(12,2),
    assigned_user_id        VARCHAR,
    source                  VARCHAR,
    canonical_source        VARCHAR,
    loss_reason             VARCHAR,
    canonical_loss_reason   VARCHAR,
    days_in_pipeline        INTEGER,
    created_at              TIMESTAMP,
    updated_at              TIMESTAMP,
    visa_type               VARCHAR,
    country                 VARCHAR,
    city                    VARCHAR
);
ALTER TABLE fact_opportunities ADD COLUMN IF NOT EXISTS last_stage_change_at  TIMESTAMP;
ALTER TABLE fact_opportunities ADD COLUMN IF NOT EXISTS last_status_change_at TIMESTAMP;

-- Grain: 1 row per (opportunity, follower user). GHL opportunities carry a
-- `followers` array (the presales agents working the lead, distinct from the
-- single assigned owner). Drives the Follower Performance tab.
CREATE TABLE IF NOT EXISTS fact_opportunity_followers (
    opportunity_id    VARCHAR,
    follower_user_id  VARCHAR,
    PRIMARY KEY (opportunity_id, follower_user_id)
);

-- Grain: 1 row per GHL conversation message (call / SMS / email / DM / activity
-- log). `user_id` = the staff member who performed it (NULL for system/automation
-- logs like TYPE_ACTIVITY_OPPORTUNITY). Powers per-person activity attribution
-- (Follower Performance), distinct from the conversation-level fact_conversations.
CREATE TABLE IF NOT EXISTS fact_messages (
    message_id        VARCHAR PRIMARY KEY,
    conversation_id   VARCHAR,
    contact_id        VARCHAR,
    user_id           VARCHAR,        -- staff actor; NULL = system/automation
    message_type      VARCHAR,        -- TYPE_CALL | TYPE_SMS | TYPE_EMAIL | ...
    direction         VARCHAR,        -- inbound | outbound
    date_added        TIMESTAMP
);

-- Grain: 1 row per GHL calendar event (appointment).
-- canonical_outcome bucketises GHL's appointmentStatus into 4 stable values:
--   show | noshow | cancelled | pending
CREATE TABLE IF NOT EXISTS fact_appointments (
    appointment_id          VARCHAR PRIMARY KEY,
    contact_id              VARCHAR,           -- FK to fact_contacts
    calendar_id             VARCHAR,           -- FK to dim_calendars
    counsellor_id           VARCHAR,           -- FK to dim_counsellors (nullable)
    appointment_status      VARCHAR,
    canonical_outcome       VARCHAR,
    start_time              TIMESTAMP,         -- when the meeting is scheduled to happen (Counsellor tab uses this)
    end_time                TIMESTAMP,
    date_added              TIMESTAMP,         -- when the BOOKING was made (Executive tab uses this)
    amount_paid             DECIMAL(10,2),
    title                   VARCHAR            -- GHL event title; "Follow Up Appointment with ..." flags follow-ups
);
-- Migration for DBs that pre-date date_added (DuckDB no-op if column exists)
ALTER TABLE fact_appointments ADD COLUMN IF NOT EXISTS date_added TIMESTAMP;
ALTER TABLE fact_appointments ADD COLUMN IF NOT EXISTS title VARCHAR;

-- Grain: 1 row per GHL conversation. `channel` is derived from the
-- conversation's type / lastMessageType (Facebook / Instagram / WhatsApp /
-- TikTok / Email / Phone-SMS / Web Chat). Used to attribute a lead to its
-- messaging platform — esp. Facebook-Messenger leads that never filled a form.
CREATE TABLE IF NOT EXISTS fact_conversations (
    conversation_id   VARCHAR PRIMARY KEY,
    contact_id        VARCHAR,
    channel           VARCHAR,
    conv_type         VARCHAR,
    last_message_type VARCHAR,
    last_message_at   TIMESTAMP,
    date_added        TIMESTAMP
);

-- Grain: 1 row per GHL form submission (a real form FILL event).
-- Sourced from /forms/submissions. This is the only feed that timestamps
-- when a contact actually re-submitted a form, which lets the dashboard
-- count returning leads (old opp, contact filled the form again in range).
-- product_type = 'facebook' + session_source = 'Paid Social' => a Meta lead.
-- campaign = the lastAttributionSource campaign at submission time (new source).
CREATE TABLE IF NOT EXISTS fact_form_submissions (
    submission_id      VARCHAR PRIMARY KEY,
    contact_id         VARCHAR,                -- FK to fact_contacts
    form_id            VARCHAR,
    form_name          VARCHAR,
    product_type       VARCHAR,                -- 'facebook' | 'chat-widget' | '' (website) | ...
    session_source     VARCHAR,                -- 'Paid Social' | '' | ...
    campaign           VARCHAR,                -- lastAttributionSource.campaign (the new source)
    utm_content        VARCHAR,                -- lastAttributionSource.utmContent (the ad) — builds 'campaign -- ad'
    event_form_name    VARCHAR,                -- eventData.parentName — organic/website form name (no campaign)
    event_source       VARCHAR,                -- eventData.source — 'Organic Search' | 'Referral' | 'Direct traffic'
    email              VARCHAR,
    submitted_at       TIMESTAMP
);
-- Migrations for DBs created before these columns existed (DuckDB no-op if present)
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS utm_content     VARCHAR;
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS event_form_name VARCHAR;
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS event_source    VARCHAR;
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS page_url        VARCHAR;
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS page_path       VARCHAR;
ALTER TABLE fact_form_submissions ADD COLUMN IF NOT EXISTS referrer        VARCHAR;

-- Grain: 1 row per survey submission. Same shape as fact_form_submissions
-- but sourced from /surveys/submissions. Used for Latest Source fallback +
-- the SEO tab's Website Lead drill-down (survey name breakdown).
CREATE TABLE IF NOT EXISTS fact_survey_submissions (
    submission_id   VARCHAR PRIMARY KEY,
    contact_id      VARCHAR,
    survey_id       VARCHAR,
    survey_name     VARCHAR,
    session_source  VARCHAR,
    campaign        VARCHAR,
    utm_content     VARCHAR,
    event_source    VARCHAR,
    page_url        VARCHAR,
    page_path       VARCHAR,
    referrer        VARCHAR,
    email           VARCHAR,
    submitted_at    TIMESTAMP
);

-- Grain: 1 row per GHL user (staff member). Sourced from /users.
-- Maps assigned_user_id (on contacts + opportunities) → human-readable name
-- for the SEO tab's Owner column and other "who owns this lead" lookups.
CREATE TABLE IF NOT EXISTS dim_users (
    user_id    VARCHAR PRIMARY KEY,
    full_name  VARCHAR,
    email      VARCHAR,
    deleted    BOOLEAN
);


-- Grain: 1 row per GHL payment transaction. Sourced from /payments/transactions
-- (altId/altType=location API). Status can be succeeded / failed / refunded /
-- pending. We treat a "paid consultation" as a transaction with
-- status='succeeded' AND (amount - amount_refunded) > 0.
CREATE TABLE IF NOT EXISTS fact_payments (
    transaction_id      VARCHAR PRIMARY KEY,
    contact_id          VARCHAR,                  -- FK to fact_contacts
    contact_email       VARCHAR,
    amount              DECIMAL(12, 2),           -- dollars (NOT cents — already in $)
    amount_refunded     DECIMAL(12, 2),
    currency            VARCHAR,                  -- 'AUD' usually
    status              VARCHAR,                  -- 'succeeded' / 'failed' / 'refunded' / 'pending'
    entity_type         VARCHAR,                  -- 'invoice' / 'order' / 'subscription'
    entity_source_name  VARCHAR,                  -- "Payment Invoice - SC 485 ..." etc.
    payment_provider    VARCHAR,                  -- 'manual' / 'stripe' / etc.
    created_at          TIMESTAMP,
    fulfilled_at        TIMESTAMP
);


-- Grain: 1 row per contact. Mirrors the GHL custom field "Latest Source" we
-- maintain via sync_latest_source. Lets dashboard views group opportunities
-- by Latest Source campaign without re-deriving from form submissions every
-- query. Populated by load_contact_latest_source() from the backfill CSVs.
CREATE TABLE IF NOT EXISTS fact_contact_latest_source (
    contact_id              VARCHAR PRIMARY KEY,
    latest_source_value     VARCHAR,    -- full 'campaign -- ad' or form name
    latest_source_campaign  VARCHAR     -- the campaign portion (before ' -- ')
);

-- Grain: 1 row per contact, mirrors GHL "Lead Source" custom field
-- (bRfV1xLav1shfrxitnre). Powers the SEO tab's Website-Forms filter and any
-- other Lead-Source-driven views. Populated by a one-time loader script that
-- pulls contacts via the contacts/search Lead Source filter.
CREATE TABLE IF NOT EXISTS fact_contact_lead_source (
    contact_id    VARCHAR PRIMARY KEY,
    lead_source   VARCHAR
);


-- =====================================================================
-- FACTS — META ADS
-- =====================================================================

-- Grain: 1 row per (account_id, campaign_id, since..until) ETL window.
-- The ETL's `since` and `until` arguments become date_start / date_end here.
-- Re-running for the same window REPLACEs the row (idempotent).
--
-- total_leads is the canonical headline metric:
--   total_leads = instant_form_leads + pixel_lead_events + pixel_custom_events
-- result_event/result_count mimic the Ads Manager 'Results' column for
-- per-campaign drill-down.
CREATE TABLE IF NOT EXISTS fact_meta_insights (
    insight_key              VARCHAR PRIMARY KEY,   -- {account_id}|{campaign_id}|{date_start}|{date_end}
    account_id               VARCHAR NOT NULL,
    account_label            VARCHAR NOT NULL,      -- 'Melbourne' | 'Sydney'
    campaign_id              VARCHAR NOT NULL,
    campaign_name            VARCHAR,
    objective                VARCHAR,
    date_start               DATE,
    date_end                 DATE,
    spend                    DECIMAL(12,2),
    impressions              BIGINT,
    reach                    BIGINT,
    frequency                DECIMAL(10,4),
    clicks                   BIGINT,
    link_clicks              BIGINT,
    ctr                      DECIMAL(8,4),
    link_ctr                 DECIMAL(8,4),
    cpc                      DECIMAL(10,4),
    cpm                      DECIMAL(10,4),
    -- Canonical leads breakdown
    instant_form_leads       INTEGER,
    pixel_lead_events        INTEGER,
    pixel_custom_events      INTEGER,
    messenger_leads          INTEGER,               -- separate column, not in total_leads
    call_leads               INTEGER,               -- separate column, not in total_leads
    total_leads              INTEGER,               -- canonical sum
    -- Per-campaign Ads-Manager Results
    result_event             VARCHAR,
    result_count             INTEGER,
    -- Video retention (for hook/hold metrics)
    video_3s_views           INTEGER,
    video_thruplays          INTEGER,
    video_p50_views          INTEGER,
    video_p95_views          INTEGER
);

-- Grain: 1 row per (account_id, campaign_id, date, country).
-- Used for daily time-series and geo charts.
CREATE TABLE IF NOT EXISTS fact_meta_daily (
    daily_key            VARCHAR PRIMARY KEY,       -- {account_id}|{campaign_id}|{date}|{country}
    account_id           VARCHAR NOT NULL,
    account_label        VARCHAR NOT NULL,
    campaign_id          VARCHAR,
    campaign_name        VARCHAR,
    objective            VARCHAR,                   -- e.g. OUTCOME_LEADS, LEAD_GENERATION (needed by Exec Total Leads card)
    date                 DATE,
    country              VARCHAR,
    spend                DECIMAL(12,2),
    impressions          BIGINT,
    clicks               BIGINT,
    total_leads          INTEGER,                   -- canonical sum (instant_form + pixel_lead + pixel_custom)
    result_event         VARCHAR,                   -- which action_type was picked as 'Results' per Ads Manager
    result_count         INTEGER                    -- the count for that event — Exec 'Total Leads' card uses this
);
-- Migrations for DBs that pre-date these columns (DuckDB no-op if column exists)
ALTER TABLE fact_meta_daily ADD COLUMN IF NOT EXISTS objective    VARCHAR;
ALTER TABLE fact_meta_daily ADD COLUMN IF NOT EXISTS result_event VARCHAR;
ALTER TABLE fact_meta_daily ADD COLUMN IF NOT EXISTS result_count INTEGER;


-- =====================================================================
-- FACTS — GA4
-- =====================================================================

-- Grain: 1 row per (date, source, medium, country, city).
CREATE TABLE IF NOT EXISTS fact_ga4_sessions (
    session_key           VARCHAR PRIMARY KEY,      -- {date}|{source}|{medium}|{country}|{city}
    date                  DATE,
    session_source        VARCHAR,
    session_medium        VARCHAR,
    country               VARCHAR,
    city                  VARCHAR,
    active_users          INTEGER,
    sessions              INTEGER,
    new_users             INTEGER,
    page_views            INTEGER,
    key_events            INTEGER,
    bounce_rate           DECIMAL(8,4),
    avg_session_duration  DECIMAL(10,2)
);

-- Grain: 1 row per date — site-wide totals with NO segmentation, so GA4's
-- session/user metrics match the GA4 UI exactly (fact_ga4_sessions, broken out
-- by source/medium/country/city, over-counts via GA4's "(other)" bucketing).
-- Drives the SEO tab topline scorecards (Sessions / Total Users / Engagement
-- Rate). Sessions & engaged_sessions are session-grain (additive across days);
-- total_users / active_users are day-unique (summing across days slightly
-- over-counts cross-day returners — acceptable, and the best offline estimate).
CREATE TABLE IF NOT EXISTS fact_ga4_daily (
    date                  DATE PRIMARY KEY,
    sessions              INTEGER,
    engaged_sessions      INTEGER,
    total_users           INTEGER,
    active_users          INTEGER,
    new_users             INTEGER,
    page_views            INTEGER,
    key_events            INTEGER,
    avg_session_duration  DECIMAL(10,2)
);

-- Grain: 1 row per (date, page_path, country).
CREATE TABLE IF NOT EXISTS fact_ga4_pages (
    page_key             VARCHAR PRIMARY KEY,       -- {date}|{page_path}|{country}
    date                 DATE,
    page_path            VARCHAR,
    country              VARCHAR,
    page_views           INTEGER,
    active_users         INTEGER
);

-- Grain: 1 row per (date, event_name, country).
CREATE TABLE IF NOT EXISTS fact_ga4_events (
    event_key            VARCHAR PRIMARY KEY,       -- {date}|{event_name}|{country}
    date                 DATE,
    event_name           VARCHAR,
    country              VARCHAR,
    event_count          INTEGER,
    total_users          INTEGER
);


-- =====================================================================
-- FACTS — GSC
-- =====================================================================

-- Grain: 1 row per (date, dimension_name, dimension_value).
-- Single table holds all 4 GSC dimensions (query/page/country/device) with
-- dimension_name as discriminator. Cleaner than 4 narrow tables.
CREATE TABLE IF NOT EXISTS fact_gsc_queries (
    gsc_key              VARCHAR PRIMARY KEY,       -- {date}|{dimension_name}|{dimension_value}
    date                 DATE,
    dimension_name       VARCHAR,                   -- query | page | country | device
    dimension_value      VARCHAR,
    clicks               INTEGER,
    impressions          INTEGER,
    ctr                  DECIMAL(8,4),
    position             DECIMAL(8,2)
);


-- =====================================================================
-- BRIDGE
-- =====================================================================

-- Maps Meta lead-form submissions to GHL contacts.
-- The ETL matches first on email; if no match, then on phone (last 9 digits
-- to handle country-code variation). Unmatched leads are NOT inserted.
CREATE TABLE IF NOT EXISTS bridge_lead_attribution (
    bridge_id            VARCHAR PRIMARY KEY,       -- {meta_lead_id}|{contact_id}
    meta_lead_id         VARCHAR NOT NULL,
    meta_ad_id           VARCHAR,
    meta_campaign_id     VARCHAR,
    meta_form_id         VARCHAR,
    contact_id           VARCHAR NOT NULL,          -- FK to fact_contacts
    match_method         VARCHAR NOT NULL,          -- 'email' | 'phone'
    matched_email        VARCHAR,
    matched_phone        VARCHAR,
    meta_lead_created    TIMESTAMP,
    contact_created      TIMESTAMP
);


-- =====================================================================
-- AGGREGATIONS
-- =====================================================================

-- Grain: 1 row per date.
-- Pre-computed by build_daily_rollups() in run_etl.py so the dashboard
-- doesn't have to scan fact tables for every render.
-- paid_leads = sum(total_leads) from fact_meta_daily
-- organic_leads = total_leads - paid_leads (derived from contacts +
--                 attribution); see normalize.py for source-categorization.
CREATE TABLE IF NOT EXISTS agg_daily_kpis (
    date                       DATE PRIMARY KEY,
    -- Lead metrics
    total_leads                INTEGER,
    paid_leads                 INTEGER,
    organic_leads              INTEGER,
    -- Funnel counts (from GHL appointments + opportunity stages)
    bookings                   INTEGER,
    shows                      INTEGER,
    no_shows                   INTEGER,
    initials_requested         INTEGER,
    paid_count                 INTEGER,
    coe_voe                    INTEGER,
    -- Spend
    meta_spend                 DECIMAL(12,2),
    -- Pipeline
    new_opportunities          INTEGER,
    won_opportunities          INTEGER,
    pipeline_value_added       DECIMAL(14,2),
    won_value                  DECIMAL(14,2),
    -- Web/SEO
    ga4_sessions               INTEGER,
    ga4_active_users           INTEGER,
    gsc_clicks                 INTEGER,
    gsc_impressions            BIGINT,
    -- Derived
    cpl                        DECIMAL(10,4),       -- meta_spend / total_leads
    last_refreshed             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- =====================================================================
-- SEED DATA — dim_counsellors (8 named consultants per sprint brief)
-- Use INSERT OR REPLACE so re-running won't error.
-- =====================================================================

INSERT OR REPLACE INTO dim_counsellors
    (counsellor_id, full_name, role, location, is_paid, rate_in_person, rate_online, ghl_user_id)
VALUES
    ('usr_wajahat',      'Wajahat Ghafoor',    'Student Counsellor TL', 'Harris Park',          FALSE,    0.00,    0.00, NULL),
    ('usr_saurab',       'Saurab Gautam',      'Student Counsellor',    'Harris Park',          FALSE,    0.00,    0.00, NULL),
    ('usr_kajal',        'Kajal Garg',         'Student Counsellor',    'Harris Park',          FALSE,    0.00,    0.00, NULL),
    ('usr_navneet',      'Navneet Kaur',       'Student Counsellor',    'Melbourne',            FALSE,    0.00,    0.00, NULL),
    ('usr_nasir',        'Nasir Nawaz',        'RMA / CEO',             'Harris Park',          TRUE,   100.00,  150.00, NULL),
    ('usr_gurbir',       'Gurbir Singh',       'MARA Agent',            'Harris Park',          TRUE,     0.00,  100.00, NULL),
    ('usr_syedturab',    'Syed Turab Raza',    'Career Counsellor',     'Harris Park / Parramatta', TRUE,  50.00,   75.00, NULL),
    ('usr_manhal',       'Manhal Dandachi',    'Migration Admin',       'Harris Park / Parramatta', FALSE,  0.00,    0.00, NULL);
