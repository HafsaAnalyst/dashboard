-- =====================================================================
-- The Migration Dashboard — Executive Tab SQL pack
-- =====================================================================
-- One view per scorecard. Every view returns 2 rows:
--   tag='current'  → metric for the selected period
--   tag='prior'    → metric for the same-length window immediately prior
-- Streamlit computes (current - prior)/prior for the green/red delta.
--
-- NOTE: DuckDB does NOT accept bind parameters inside CREATE VIEW
-- definitions. The dashboard uses these as TEMPLATES — it regex-extracts
-- the SELECT body for each view name and runs it via
-- con.execute(body, binds) at query time. The CREATE OR REPLACE VIEW
-- wrappers are documentation only and will fail if you try to load
-- this file directly with `duckdb < executive_cards.sql`.
--
-- Bind parameters every view expects:
--   $since         DATE    first day of selected period (inclusive)
--   $until         DATE    last day of selected period (inclusive)
--   $prior_since   DATE    first day of comparison window
--   $prior_until   DATE    last day of comparison window
--   $city          VARCHAR 'All' | 'Melbourne' | 'Sydney' | 'Others' | 'Unidentified'
--
-- Schema prerequisites:
--   * fact_appointments has date_added TIMESTAMP
--   * fact_meta_daily has objective + result_event + result_count columns
-- Both ship in models/schema.sql.
-- =====================================================================


-- =====================================================================
-- Card 1 — Total Leads
-- =====================================================================
-- MAIN: Meta result_count summed per DAY (from fact_meta_daily, which
--       carries objective + result_count thanks to extended
--       normalize_meta_daily). Lead/conversion objectives only.
--       Accounts honor city filter. Date in period.
-- Also returns spend in those same campaigns so Card 2 (CPL) can divide.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_total_leads_main AS
WITH bucketed AS (
    SELECT
        CASE
            WHEN date BETWEEN $since AND $until            THEN 'current'
            WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        result_count,
        spend,
        account_label
    FROM fact_meta_daily
    WHERE objective IN ('OUTCOME_LEADS', 'LEAD_GENERATION',
                        'OUTCOME_SALES', 'CONVERSIONS', 'LEADS')
      AND (date BETWEEN $since AND $until
           OR date BETWEEN $prior_since AND $prior_until)
      AND ($city = 'All'
           OR ($city = 'Melbourne'    AND account_label = 'Melbourne')
           OR ($city = 'Sydney'       AND account_label = 'Sydney')
           OR ($city = 'Others'       AND 1=0)   -- Meta has no 'Others' account
           OR ($city = 'Unidentified' AND 1=0))  -- Meta has no 'Unidentified' account
)
SELECT
    tag,
    -- Real lead conversions: Meta Instant Form + Facebook pixel Lead + Lead
    -- custom-conversion (landing page form fills). Only fb_pixel_view_content
    -- is excluded (pure page-view noise; never becomes a GHL contact).
    COALESCE(SUM(result_count) FILTER (WHERE result_event IN
        ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) AS leads,
    COALESCE(SUM(spend),        0) AS spend_in_lead_campaigns
FROM bucketed
WHERE tag IS NOT NULL
GROUP BY tag;


-- =====================================================================
-- Card 1 — Total Leads (secondary: GHL Paid Social opps)
-- =====================================================================
-- Distinct L2C - Education opps created in period whose contact's
-- first_attribution_source equals 'paid_social' (case-insensitive),
-- with city filter on fact_contacts.city.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_total_leads_secondary AS
WITH l2c AS (
    SELECT pipeline_id
    FROM dim_pipelines
    WHERE pipeline_name = 'L2C - Education'
),
opps AS (
    SELECT
        CASE
            WHEN CAST(o.created_at AS DATE) BETWEEN $since AND $until            THEN 'current'
            WHEN CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        o.opportunity_id,
        o.contact_id
    FROM fact_opportunities o
    WHERE o.pipeline_id IN (SELECT pipeline_id FROM l2c)
      AND (CAST(o.created_at AS DATE) BETWEEN $since AND $until
           OR CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until)
)
SELECT
    o.tag,
    COUNT(DISTINCT o.opportunity_id) AS paid_social_opps
FROM opps o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE LOWER(COALESCE(c.first_attribution_source, '')) IN ('paid social', 'paid_social')
  AND ($city = 'All'
       -- Melbourne = matches Mel pattern AND does NOT also match Syd pattern
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw'))
       -- Sydney = matches Syd pattern AND does NOT also match Mel pattern
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic'))
       -- Others = non-null AND NOT Mel-exclusive AND NOT Syd-exclusive
       -- (so ambiguous strings like 'Outside Melbourne, Sydney' fall here)
       OR ($city = 'Others' AND COALESCE(c.city, '') <> ''
           AND NOT (
               (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
               AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
           )
           AND NOT (
               (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
               AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
           ))
       OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
  AND o.tag IS NOT NULL
GROUP BY o.tag;


-- =====================================================================
-- Card 2 — Avg CPL
-- =====================================================================
-- Main:      spend_in_lead_campaigns / leads (from Card 1 main)
-- Secondary: spend_in_lead_campaigns / paid_social_opps (Card 1 sec)
-- These views below are convenience — the dashboard typically computes
-- in Python from Card 1's results.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_avg_cpl_main AS
SELECT
    tag,
    spend_in_lead_campaigns,
    leads,
    CASE WHEN leads > 0
         THEN spend_in_lead_campaigns / leads
         ELSE NULL
    END AS cpl
FROM vw_card_total_leads_main;

CREATE OR REPLACE VIEW vw_card_avg_cpl_secondary AS
SELECT
    m.tag,
    m.spend_in_lead_campaigns,
    s.paid_social_opps,
    CASE WHEN COALESCE(s.paid_social_opps, 0) > 0
         THEN m.spend_in_lead_campaigns / s.paid_social_opps
         ELSE NULL
    END AS cpl_paid_social_opps
FROM vw_card_total_leads_main m
LEFT JOIN vw_card_total_leads_secondary s ON s.tag = m.tag;


-- =====================================================================
-- Card 3 — Lead → Booking
-- =====================================================================
-- Denominator: distinct L2C - Education opps with created_at in period
--              (city filter on contact.city).
-- Numerator:   of those, distinct opps whose contact has any
--              fact_appointments row with date_added in period.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_lead_to_booking AS
WITH l2c AS (
    SELECT pipeline_id
    FROM dim_pipelines
    WHERE pipeline_name = 'L2C - Education'
),
opps_in_period AS (
    SELECT
        CASE
            WHEN CAST(o.created_at AS DATE) BETWEEN $since AND $until            THEN 'current'
            WHEN CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        o.opportunity_id,
        o.contact_id
    FROM fact_opportunities o
    JOIN fact_contacts c ON c.contact_id = o.contact_id
    WHERE o.pipeline_id IN (SELECT pipeline_id FROM l2c)
      AND (CAST(o.created_at AS DATE) BETWEEN $since AND $until
           OR CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until)
      AND ($city = 'All'
           -- Melbourne = matches Mel pattern AND does NOT also match Syd pattern
           OR ($city = 'Melbourne'
               AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                    OR LOWER(COALESCE(c.city, '')) = 'vic')
               AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                    OR LOWER(COALESCE(c.city, '')) = 'nsw'))
           -- Sydney = matches Syd pattern AND does NOT also match Mel pattern
           OR ($city = 'Sydney'
               AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                    OR LOWER(COALESCE(c.city, '')) = 'nsw')
               AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                    OR LOWER(COALESCE(c.city, '')) = 'vic'))
           -- Others = non-null AND NOT Mel-exclusive AND NOT Syd-exclusive
           -- (so ambiguous strings like 'Outside Melbourne, Sydney' fall here)
           OR ($city = 'Others' AND COALESCE(c.city, '') <> ''
               AND NOT (
                   (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                   AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
               )
               AND NOT (
                   (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                   AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
               ))
           OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
),
booked_opps AS (
    SELECT DISTINCT
        o.tag,
        o.opportunity_id
    FROM opps_in_period o
    JOIN fact_appointments a ON a.contact_id = o.contact_id
    WHERE (o.tag = 'current'
           AND CAST(a.date_added AS DATE) BETWEEN $since AND $until)
       OR (o.tag = 'prior'
           AND CAST(a.date_added AS DATE) BETWEEN $prior_since AND $prior_until)
)
SELECT
    o.tag,
    COUNT(DISTINCT o.opportunity_id)              AS leads,
    COUNT(DISTINCT b.opportunity_id)              AS booked,
    CASE WHEN COUNT(DISTINCT o.opportunity_id) > 0
         THEN CAST(COUNT(DISTINCT b.opportunity_id) AS DOUBLE)
              / COUNT(DISTINCT o.opportunity_id)
         ELSE NULL
    END                                            AS lead_to_booking_rate
FROM opps_in_period o
LEFT JOIN booked_opps b
       ON b.opportunity_id = o.opportunity_id
      AND b.tag            = o.tag
WHERE o.tag IS NOT NULL
GROUP BY o.tag;


-- =====================================================================
-- Card 4 — Show Rate
-- =====================================================================
-- Denominator: appointments with date_added in period, for contacts who
--              have at least one L2C - Education opp, where the meeting
--              has actually happened (start_time <= now()) and the
--              outcome has resolved (show/noshow/cancelled).
-- Numerator:   those with canonical_outcome = 'show'.
-- City filter applies via fact_contacts.city.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_show_rate AS
WITH l2c AS (
    SELECT pipeline_id
    FROM dim_pipelines
    WHERE pipeline_name = 'L2C - Education'
),
l2c_contacts AS (
    SELECT DISTINCT contact_id
    FROM fact_opportunities
    WHERE pipeline_id IN (SELECT pipeline_id FROM l2c)
),
resolved_appts AS (
    SELECT
        CASE
            WHEN CAST(a.date_added AS DATE) BETWEEN $since AND $until            THEN 'current'
            WHEN CAST(a.date_added AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        a.appointment_id,
        a.canonical_outcome
    FROM fact_appointments a
    JOIN l2c_contacts lc ON lc.contact_id = a.contact_id
    JOIN fact_contacts c ON c.contact_id  = a.contact_id
    WHERE a.start_time <= CURRENT_TIMESTAMP
      AND a.canonical_outcome IN ('show', 'noshow', 'cancelled')
      AND LOWER(a.appointment_status) <> 'invalid'
      AND (CAST(a.date_added AS DATE) BETWEEN $since AND $until
           OR CAST(a.date_added AS DATE) BETWEEN $prior_since AND $prior_until)
      AND ($city = 'All'
           -- Melbourne = matches Mel pattern AND does NOT also match Syd pattern
           OR ($city = 'Melbourne'
               AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                    OR LOWER(COALESCE(c.city, '')) = 'vic')
               AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                    OR LOWER(COALESCE(c.city, '')) = 'nsw'))
           -- Sydney = matches Syd pattern AND does NOT also match Mel pattern
           OR ($city = 'Sydney'
               AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                    OR LOWER(COALESCE(c.city, '')) = 'nsw')
               AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                    OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                    OR LOWER(COALESCE(c.city, '')) = 'vic'))
           -- Others = non-null AND NOT Mel-exclusive AND NOT Syd-exclusive
           -- (so ambiguous strings like 'Outside Melbourne, Sydney' fall here)
           OR ($city = 'Others' AND COALESCE(c.city, '') <> ''
               AND NOT (
                   (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                   AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
               )
               AND NOT (
                   (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                   AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
               ))
           OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
)
SELECT
    tag,
    COUNT(*) FILTER (WHERE canonical_outcome = 'show') AS shows,
    COUNT(*)                                           AS resolved_appointments,
    CASE WHEN COUNT(*) > 0
         THEN CAST(COUNT(*) FILTER (WHERE canonical_outcome = 'show') AS DOUBLE)
              / COUNT(*)
         ELSE NULL
    END                                                AS show_rate
FROM resolved_appts
WHERE tag IS NOT NULL
GROUP BY tag;


-- =====================================================================
-- Card 5 — COEs
-- =====================================================================
-- Count of L2C - Education opps CREATED in period whose current stage
-- is 'COE Received'. (Cohort: leads that came in this period → how
-- many already reached COE Received.)
-- City filter on fact_contacts.city.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_coes AS
-- COE = opportunities at "COE Received" stage in EITHER L2C - Education
-- OR CLT - Onshore Admission (both pipelines have a "COE Received" stage).
-- Counted by opp.created_at within window.
WITH coe_pipelines AS (
    SELECT pipeline_id
    FROM dim_pipelines
    WHERE pipeline_name IN ('L2C - Education', 'CLT - Onshore Admission')
),
coe_stages AS (
    SELECT stage_id
    FROM dim_stages
    WHERE LOWER(stage_name) = 'coe received'
      AND pipeline_id IN (SELECT pipeline_id FROM coe_pipelines)
)
SELECT
    CASE
        WHEN CAST(o.created_at AS DATE) BETWEEN $since AND $until            THEN 'current'
        WHEN CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    -- Unique students: a contact with a 'COE Received' opp in BOTH
    -- L2C-Education and CLT-Onshore Admission is one COE, not two.
    COUNT(DISTINCT o.contact_id) AS coes
FROM fact_opportunities o
JOIN fact_contacts    c ON c.contact_id = o.contact_id
WHERE o.pipeline_id IN (SELECT pipeline_id FROM coe_pipelines)
  AND o.stage_id    IN (SELECT stage_id    FROM coe_stages)
  AND (CAST(o.created_at AS DATE) BETWEEN $since AND $until
       OR CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until)
  AND ($city = 'All'
       -- Melbourne = matches Mel pattern AND does NOT also match Syd pattern
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw'))
       -- Sydney = matches Syd pattern AND does NOT also match Mel pattern
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic'))
       -- Others = non-null AND NOT Mel-exclusive AND NOT Syd-exclusive
       -- (so ambiguous strings like 'Outside Melbourne, Sydney' fall here)
       OR ($city = 'Others' AND COALESCE(c.city, '') <> ''
           AND NOT (
               (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
               AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
           )
           AND NOT (
               (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
               AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
           ))
       OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
GROUP BY 1
HAVING tag IS NOT NULL;


-- =====================================================================
-- Card 6 — Ad Spend
-- =====================================================================
-- Total Meta spend across selected accounts in the period. Day grain
-- from fact_meta_daily. No objective filter — includes ALL campaigns
-- (brand, awareness, leads, conversion, traffic, etc.).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_ad_spend AS
SELECT
    CASE
        WHEN date BETWEEN $since AND $until            THEN 'current'
        WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    SUM(spend) AS spend
FROM fact_meta_daily
WHERE (date BETWEEN $since AND $until
       OR date BETWEEN $prior_since AND $prior_until)
  AND ($city = 'All'
       OR ($city = 'Melbourne'    AND account_label = 'Melbourne')
       OR ($city = 'Sydney'       AND account_label = 'Sydney')
       OR ($city = 'Others'       AND 1=0)
       OR ($city = 'Unidentified' AND 1=0))
GROUP BY 1
HAVING tag IS NOT NULL;


-- =====================================================================
-- Card 7 — Revenue
-- =====================================================================
-- SUM(monetary_value) across ALL L2C - Education opportunities
-- (any status: open/won/lost/abandoned) created in the period.
-- City filter on fact_contacts.city.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_card_revenue AS
WITH l2c AS (
    SELECT pipeline_id
    FROM dim_pipelines
    WHERE pipeline_name = 'L2C - Education'
)
SELECT
    CASE
        WHEN CAST(o.created_at AS DATE) BETWEEN $since AND $until            THEN 'current'
        WHEN CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    SUM(o.monetary_value) AS revenue,
    COUNT(*)              AS opp_count
FROM fact_opportunities o
JOIN fact_contacts    c ON c.contact_id = o.contact_id
WHERE o.pipeline_id IN (SELECT pipeline_id FROM l2c)
  AND (CAST(o.created_at AS DATE) BETWEEN $since AND $until
       OR CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until)
  AND ($city = 'All'
       -- Melbourne = matches Mel pattern AND does NOT also match Syd pattern
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw'))
       -- Sydney = matches Syd pattern AND does NOT also match Mel pattern
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic'))
       -- Others = non-null AND NOT Mel-exclusive AND NOT Syd-exclusive
       -- (so ambiguous strings like 'Outside Melbourne, Sydney' fall here)
       OR ($city = 'Others' AND COALESCE(c.city, '') <> ''
           AND NOT (
               (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
               AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
           )
           AND NOT (
               (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
               AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
           ))
       OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
GROUP BY 1
HAVING tag IS NOT NULL;

-- =====================================================================
-- Executive — VOE (Visa Offer / Engagement)
-- Cohort: contacts whose first opportunity in L2C - VISA reached
--         "MARA Appointment Booked" stage. Counted by opp created_at
--         in window (similar to COEs).
-- =====================================================================
CREATE OR REPLACE VIEW vw_card_voes AS
WITH l2c_visa AS (
    SELECT pipeline_id FROM dim_pipelines WHERE pipeline_name = 'L2C - VISA'
),
voe_stages AS (
    SELECT stage_id FROM dim_stages
    WHERE LOWER(stage_name) = 'mara appointment booked'
      AND pipeline_id IN (SELECT pipeline_id FROM l2c_visa)
)
SELECT
    CASE
        WHEN CAST(o.created_at AS DATE) BETWEEN $since AND $until            THEN 'current'
        WHEN CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    COUNT(*) AS voes
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE o.pipeline_id IN (SELECT pipeline_id FROM l2c_visa)
  AND o.stage_id    IN (SELECT stage_id    FROM voe_stages)
  AND (CAST(o.created_at AS DATE) BETWEEN $since AND $until
       OR CAST(o.created_at AS DATE) BETWEEN $prior_since AND $prior_until)
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city, '')) LIKE '%sydney%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%wales%'
                OR LOWER(COALESCE(c.city, '')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city, '')) LIKE '%melb%'
                OR LOWER(COALESCE(c.city, '')) LIKE '%victoria%'
                OR LOWER(COALESCE(c.city, '')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city, '') <> '')
       OR ($city = 'Unidentified' AND COALESCE(c.city, '') = ''))
GROUP BY 1
HAVING tag IS NOT NULL;


-- =====================================================================
-- Executive — Total Leads breakdown: per contact in window with source,
-- city, visa type, latest source, pipeline+stage of latest opp.
-- Drives Total Leads drill-down: source / city / visa-type splits + the
-- email list when a segment is clicked.
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec_leads_detail AS
WITH latest_opp AS (
    SELECT contact_id, pipeline_id, stage_id, status,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
),
all_subs AS (
    SELECT contact_id, submitted_at, campaign, utm_content, form_name,
           event_form_name, session_source, event_source, NULL AS survey_name
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, utm_content, NULL AS form_name,
           NULL AS event_form_name, session_source, event_source, survey_name
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, campaign, utm_content, form_name, event_form_name,
           session_source, event_source, survey_name,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
)
SELECT
    c.contact_id,
    c.email,
    c.date_added,
    -- Source bucket (Meta Paid / Organic / Chatbot / Referral / Direct / Other)
    CASE
        WHEN LOWER(COALESCE(c.canonical_source,'')) IN ('paid social','paid_social','facebook ads','meta ads')
             THEN 'Meta Paid'
        WHEN LOWER(COALESCE(c.canonical_source,'')) IN ('organic search','organic','seo')
             THEN 'Organic / SEO'
        WHEN LOWER(COALESCE(c.canonical_source,'')) LIKE '%chat%'
             THEN 'Chatbot'
        WHEN LOWER(COALESCE(c.canonical_source,'')) = 'referral'
             THEN 'Referral'
        WHEN LOWER(COALESCE(c.canonical_source,'')) IN ('direct','direct traffic')
             THEN 'Direct'
        WHEN COALESCE(c.canonical_source,'') = ''
             THEN 'Unattributed'
        ELSE 'Other'
    END AS source_bucket,
    -- City bucket
    CASE
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
          AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
            THEN 'Melbourne'
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
          AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
            THEN 'Sydney'
        WHEN COALESCE(c.city,'') = '' THEN 'Unidentified'
        ELSE 'Other AU'
    END AS city_bucket,
    -- Visa type bucket (best-effort from c.visa_type)
    CASE
        WHEN LOWER(COALESCE(c.visa_type,'')) LIKE '%485%' OR LOWER(COALESCE(c.visa_type,'')) LIKE '%extension%'
             THEN '485 / Visa Extension'
        WHEN LOWER(COALESCE(c.visa_type,'')) LIKE '%pr%' OR LOWER(COALESCE(c.visa_type,'')) LIKE '%permanent%'
             THEN 'PR Pathway'
        WHEN LOWER(COALESCE(c.visa_type,'')) LIKE '%education%' OR LOWER(COALESCE(c.visa_type,'')) LIKE '%diploma%' OR LOWER(COALESCE(c.visa_type,'')) LIKE '%student%'
             THEN 'Education / Diploma'
        WHEN LOWER(COALESCE(c.visa_type,'')) LIKE '%partner%'
             THEN 'Partner Visa'
        WHEN COALESCE(c.visa_type,'') = '' THEN 'Unspecified'
        ELSE 'Other'
    END AS visa_bucket,
    -- Latest Source live-computed
    COALESCE(
        NULLIF(CASE WHEN COALESCE(ls.campaign,'') <> '' AND COALESCE(ls.utm_content,'') <> ''
                         THEN ls.campaign || ' -- ' || ls.utm_content
                    WHEN COALESCE(ls.campaign,'') <> ''        THEN ls.campaign
                    WHEN COALESCE(ls.form_name,'') <> ''       THEN ls.form_name
                    WHEN COALESCE(ls.survey_name,'') <> ''     THEN ls.survey_name
                    WHEN COALESCE(ls.event_form_name,'') <> '' THEN ls.event_form_name
                    WHEN COALESCE(ls.session_source,'') <> ''  THEN ls.session_source
                    WHEN COALESCE(ls.event_source,'') <> ''    THEN ls.event_source
                    ELSE NULL END, ''),
        c.latest_attribution_source,
        ''
    ) AS latest_source,
    p.pipeline_name AS pipeline,
    s.stage_name    AS stage,
    lo.status       AS status
FROM fact_contacts c
LEFT JOIN latest_opp lo  ON lo.contact_id = c.contact_id AND lo.rn = 1
LEFT JOIN dim_pipelines p ON p.pipeline_id = lo.pipeline_id
LEFT JOIN dim_stages   s  ON s.stage_id    = lo.stage_id
LEFT JOIN latest_sub  ls ON ls.contact_id = c.contact_id AND ls.rn = 1
WHERE CAST(c.date_added AS DATE) BETWEEN $since AND $until;


-- =====================================================================
-- Executive — Show Rate by counsellor (current window)
-- Used in Show Rate drill-down modal.
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec_show_rate_by_counsellor AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal'),
        ('RF7bh7b3avrzStoTE8ho','Kajal'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur')
    ) AS t(calendar_id, counsellor)
)
SELECT
    cal.counsellor,
    COUNT(*)                                                            AS appts,
    COUNT(*) FILTER (WHERE LOWER(a.canonical_outcome) = 'show')         AS showed,
    CASE WHEN COUNT(*) > 0
         THEN COUNT(*) FILTER (WHERE LOWER(a.canonical_outcome) = 'show') * 1.0 / COUNT(*)
         END                                                            AS show_rate
FROM fact_appointments a
JOIN calendars cal ON cal.calendar_id = a.calendar_id
WHERE CAST(a.start_time AS DATE) BETWEEN $since AND $until
  AND LOWER(a.appointment_status) <> 'invalid'
GROUP BY cal.counsellor
HAVING COUNT(*) >= 1;


-- =====================================================================
-- Executive — Revenue breakdown (current window).
-- "service" classified by pipeline (Visa / Education / Paid consult).
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec_revenue_breakdown AS
WITH pay_in_w AS (
    SELECT p.contact_id,
           SUM(p.amount - COALESCE(p.amount_refunded, 0)) AS revenue
    FROM fact_payments p
    WHERE LOWER(p.status) = 'succeeded'
      AND CAST(p.created_at AS DATE) BETWEEN $since AND $until
    GROUP BY p.contact_id
),
latest_opp_pipe AS (
    SELECT o.contact_id, dp.pipeline_name,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.created_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines dp ON dp.pipeline_id = o.pipeline_id
)
SELECT
    CASE
        WHEN lp.pipeline_name LIKE '%VISA%' OR lp.pipeline_name LIKE '%Onshore Admission%'
             THEN 'Visa applications'
        WHEN lp.pipeline_name LIKE '%Education%' OR lp.pipeline_name LIKE '%Admissions%'
             THEN 'Education placements'
        WHEN lp.pipeline_name LIKE '%Skill%' THEN 'Skill Migration'
        WHEN lp.pipeline_name IS NULL THEN 'Paid consultations'
        ELSE 'Other'
    END                            AS service,
    -- City bucket
    CASE
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%')
            THEN 'Melbourne'
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%')
            THEN 'Sydney'
        ELSE 'Other AU'
    END                            AS city_bucket,
    SUM(pw.revenue)                AS revenue,
    COUNT(DISTINCT pw.contact_id)  AS contacts
FROM pay_in_w pw
JOIN fact_contacts c ON c.contact_id = pw.contact_id
LEFT JOIN latest_opp_pipe lp ON lp.contact_id = pw.contact_id AND lp.rn = 1
GROUP BY 1, 2;

-- =====================================================================
-- Executive — UNIFIED LEADS
-- =====================================================================
-- A "Lead" = contact whose LATEST form/survey submission OR (if no form/
-- survey at all) latest appointment booking falls in the window. This
-- catches:
--   1. Form/survey submissions  → attributed to that form's campaign/source
--   2. Referral / direct appointment bookings (no form on file) → bucketed
--      as "Referral / Direct booking"
-- One row per contact. Use this view for Total Leads count, source
-- breakdown, CPL, Lead→Booking, and Show Rate drill-downs.
--
-- TIMEZONE: Form submissions are stored in UTC; convert +10h (AEST) so
-- window boundaries align with Meta's ad-account timezone.
-- =====================================================================
-- Total Leads cohort = ALL contacts NEWLY CREATED in window (date_added, AEST)
-- OR REVIVED (created earlier, but their latest form/survey submission lands in
-- the window). One row per contact. Attribution columns come from the contact's
-- latest form/survey; created-with-no-form contacts fall back to their canonical
-- source / first attribution.
CREATE OR REPLACE VIEW vw_exec_unified_leads AS
WITH all_subs AS (
    SELECT contact_id, submitted_at, campaign, utm_content, event_source,
           COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,''), '(unnamed)') AS form_name,
           'form' AS kind
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, utm_content, event_source,
           COALESCE(NULLIF(survey_name,''), '(unnamed survey)') AS form_name,
           'survey' AS kind
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
ls AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
        FROM all_subs
    ) WHERE rn = 1
),
cohort AS (
    SELECT c.contact_id, c.email, c.canonical_source, c.first_attribution_source,
           c.latest_attribution_source,
           CAST(c.date_added + INTERVAL 10 HOUR AS DATE)   AS created_date,
           CAST(ls.submitted_at + INTERVAL 10 HOUR AS DATE) AS revived_date,
           ls.form_name, ls.kind, ls.campaign, ls.utm_content, ls.event_source
    FROM fact_contacts c
    LEFT JOIN ls ON ls.contact_id = c.contact_id
    -- exclude the Instagram AI auto-responder contact (AI-generated messages)
    WHERE LOWER(TRIM(COALESCE(c.contact_name, ''))) NOT IN ('insta user', 'insta ai')
      AND (CAST(c.date_added + INTERVAL 10 HOUR AS DATE)   BETWEEN $since AND $until
       OR CAST(ls.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
booked_in_w AS (
    SELECT contact_id,
           MAX(CASE WHEN LOWER(canonical_outcome) = 'show' THEN 1 ELSE 0 END) AS showed
    FROM fact_appointments
    WHERE LOWER(appointment_status) <> 'invalid'
      AND CAST(start_time AS DATE) BETWEEN $since AND $until
    GROUP BY contact_id
),
latest_opp AS (
    SELECT contact_id, pipeline_id, stage_id, status,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
),
chan AS (
    SELECT contact_id, channel FROM (
        SELECT contact_id, channel,
               ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY
                   CASE channel WHEN 'Facebook' THEN 1 WHEN 'Instagram' THEN 2
                                WHEN 'WhatsApp' THEN 3 WHEN 'TikTok' THEN 4
                                WHEN 'Google Business' THEN 5 ELSE 9 END,
                   last_message_at DESC NULLS LAST) AS rn
        FROM fact_conversations WHERE contact_id IS NOT NULL
    ) WHERE rn = 1
)
SELECT
    ch.contact_id,
    ch.email,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
         ELSE ch.revived_date END                                       AS event_at,
    COALESCE(ch.form_name, '(direct / no form)')                        AS form_name,
    COALESCE(ch.kind, 'direct')                                         AS kind,
    ch.campaign,
    ch.event_source,
    CASE
        WHEN COALESCE(ch.campaign,'') <> '' AND COALESCE(ch.event_source,'') = 'Paid Social' THEN 'Meta Paid'
        WHEN COALESCE(ch.campaign,'') <> ''                            THEN 'Meta Paid'
        WHEN ch.event_source = 'Organic Search'                        THEN 'Organic / SEO'
        WHEN ch.event_source = 'Referral'                              THEN 'Referral'
        WHEN ch.event_source IN ('Direct','Direct traffic')            THEN 'Direct'
        WHEN ch.event_source = 'Paid Social'                           THEN 'Meta Paid'
        -- Facebook-Messenger leads (no form): GHL conversation channel = Facebook
        -- -> Paid Social per the marketing rule. (Caught here only when no
        -- form-based source above matched.)
        WHEN vcc.channel = 'Facebook'                                  THEN 'Meta Paid'
        WHEN ch.event_source = 'Social media'                          THEN 'Social Media'
        WHEN LOWER(COALESCE(ch.form_name,'')) LIKE '%chat%'            THEN 'Chatbot'
        WHEN ch.canonical_source = 'meta_paid'                         THEN 'Meta Paid'
        WHEN ch.canonical_source IN ('website_form','organic_seo')    THEN 'Organic / SEO'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'referral'     THEN 'Referral'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'social media' THEN 'Social Media'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) IN ('direct','direct traffic') THEN 'Direct'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'organic search' THEN 'Organic / SEO'
        WHEN COALESCE(NULLIF(ch.event_source,''), NULLIF(ch.first_attribution_source,'')) IS NOT NULL
             THEN COALESCE(NULLIF(ch.event_source,''), ch.first_attribution_source)
        ELSE 'Other'
    END                                                                AS source_bucket,
    COALESCE(bw.showed, 0)                                             AS showed,
    CASE WHEN bw.contact_id IS NOT NULL THEN 1 ELSE 0 END               AS has_booking,
    p.pipeline_name                                                    AS pipeline,
    s.stage_name                                                       AS stage,
    lo.status                                                          AS status,
    COALESCE(
        NULLIF(CASE WHEN COALESCE(ch.campaign,'') <> '' AND COALESCE(ch.utm_content,'') <> ''
                         THEN ch.campaign || ' -- ' || ch.utm_content
                    WHEN COALESCE(ch.campaign,'') <> ''        THEN ch.campaign
                    WHEN COALESCE(ch.form_name,'') NOT IN ('','(unnamed)','(unnamed survey)')
                                                              THEN ch.form_name
                    WHEN COALESCE(ch.event_source,'') <> ''    THEN ch.event_source
                    ELSE NULL END, ''),
        ch.latest_attribution_source,
        ''
    )                                                                  AS latest_source
FROM cohort ch
JOIN fact_contacts c       ON c.contact_id = ch.contact_id
LEFT JOIN booked_in_w bw   ON bw.contact_id = ch.contact_id
LEFT JOIN latest_opp lo    ON lo.contact_id = ch.contact_id AND lo.rn = 1
LEFT JOIN dim_pipelines p  ON p.pipeline_id = lo.pipeline_id
LEFT JOIN dim_stages   s   ON s.stage_id    = lo.stage_id
LEFT JOIN chan vcc         ON vcc.contact_id = ch.contact_id;


-- =====================================================================
-- Meta spend per source bucket — Meta Paid only has spend; everything
-- else = 0. Used to compute CPL per source in the Executive CPL modal.
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec_meta_spend AS
SELECT 'Meta Paid' AS source_bucket, SUM(spend) AS spend
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until;


-- =====================================================================
-- Executive — End-to-End Lead Journey (CHAINED funnel, detail grain).
-- =====================================================================
-- Cohort = L2C - Education contacts that are NEW (opp.created_at in window)
-- OR REVIVED (contact's latest form/survey submission in window, AEST +10h).
--
-- A TRUE funnel: each stage is a STRICT SUBSET of the one above — the same
-- contacts traced forward by contact_id (1:1 with email) — so the bars
-- always descend. 'Consultation Showed' uses the REAL appointment outcome
-- (canonical_outcome = 'show'), not a pipeline-stage guess.
--   1. Leads                    — every cohort contact
--   2. Booking Link Shared      — of Leads, reached L2C-Education 'Booking
--                                 Link Shared' (stage_order >= 4, excluding
--                                 the 'Don't Touch' parking stage = 12)
--   3. Appointment Booked       — of (2), has a (non-invalid) appointment
--   4. Consultation Showed      — of (3), appointment outcome = 'show'
--   5. Entered Admission / Visa — of (4), opened a CLT-Onshore OR L2C-VISA opp
--   6. COE Received             — of (5), reached 'COE Received' in
--                                 CLT-Onshore OR L2C-Education
--
-- Each row carries drill-down fields: email, total_payment (all-time
-- succeeded payments), last_activity (contact date_updated), pipeline
-- (which service pipeline the contact entered — for stages 5-6).
-- No city filter (matches vw_exec_unified_leads). Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_exec_l2c_funnel AS
WITH edu_pipe AS (
    SELECT pipeline_id FROM dim_pipelines WHERE pipeline_name = 'L2C - Education'
),
subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id
),
-- Cohort: new-or-revived L2C-Education contacts.
cohort AS (
    SELECT DISTINCT o.contact_id
    FROM fact_opportunities o
    LEFT JOIN latest_sub ls ON ls.contact_id = o.contact_id
    WHERE o.pipeline_id IN (SELECT pipeline_id FROM edu_pipe)
      AND ( CAST(o.created_at AS DATE) BETWEEN $since AND $until
            OR CAST(ls.last_sub + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until )
),
-- Reached 'Booking Link Shared' (order >= 4) inside L2C-Education.
edu_flags AS (
    SELECT o.contact_id,
           MAX(CASE WHEN st.stage_order >= 4 AND st.stage_order <> 12 THEN 1 ELSE 0 END) AS reached_bls
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    GROUP BY o.contact_id
),
-- Real appointment signal: did the contact attend a consultation?
appt AS (
    SELECT contact_id,
           MAX(1)                                                            AS has_appt,
           MAX(CASE WHEN LOWER(canonical_outcome) = 'show' THEN 1 ELSE 0 END) AS showed
    FROM fact_appointments
    WHERE LOWER(appointment_status) <> 'invalid'
    GROUP BY contact_id
),
-- Entered a service pipeline (admission or visa) + which one.
svc AS (
    SELECT o.contact_id,
           MAX(CASE WHEN p.pipeline_name = 'CLT - Onshore Admission' THEN 1 ELSE 0 END) AS in_clt,
           MAX(CASE WHEN p.pipeline_name = 'L2C - VISA'              THEN 1 ELSE 0 END) AS in_visa
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    WHERE p.pipeline_name IN ('CLT - Onshore Admission', 'L2C - VISA')
    GROUP BY o.contact_id
),
-- Reached 'COE Received' in admission or education.
coe AS (
    SELECT o.contact_id,
           MAX(CASE WHEN p.pipeline_name = 'CLT - Onshore Admission' THEN 1 ELSE 0 END) AS coe_clt,
           MAX(CASE WHEN p.pipeline_name = 'L2C - Education'         THEN 1 ELSE 0 END) AS coe_edu
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    WHERE st.stage_name = 'COE Received'
      AND p.pipeline_name IN ('CLT - Onshore Admission', 'L2C - Education')
    GROUP BY o.contact_id
),
-- L2C-VISA milestones folded into the funnel:
--   reached_mara              -> Appointment Booked  (a booked visa consult)
--   reached_visa_postconsult  -> Consultation Showed (attended visa consult)
visa_flags AS (
    SELECT o.contact_id,
           MAX(CASE WHEN st.stage_order >= 1 THEN 1 ELSE 0 END) AS reached_mara,            -- MARA Appointment Booked
           MAX(CASE WHEN st.stage_order >= 2 THEN 1 ELSE 0 END) AS reached_visa_postconsult -- Post Consultation
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - VISA'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    GROUP BY o.contact_id
),
pay AS (
    SELECT contact_id, SUM(amount - COALESCE(amount_refunded, 0)) AS total_payment
    FROM fact_payments WHERE LOWER(status) = 'succeeded' GROUP BY contact_id
),
-- The contact's CURRENT opp (latest by updated_at) across the canonical
-- pipelines — used to show "Pipeline — Stage" + status in the drill-down.
cur_latest AS (
    SELECT contact_id, pipeline_name, stage_name, status FROM (
        SELECT o.contact_id, p.pipeline_name, st.stage_name, o.status,
               ROW_NUMBER() OVER (PARTITION BY o.contact_id
                                  ORDER BY o.updated_at DESC, o.created_at DESC) AS rn
        FROM fact_opportunities o
        JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
            AND p.pipeline_name IN ('L2C - Education', 'L2C - VISA', 'CLT - Onshore Admission')
        JOIN dim_stages   st ON st.stage_id   = o.stage_id
    ) WHERE rn = 1
),
-- One row per cohort contact carrying every funnel flag + drill-down field.
base AS (
    SELECT ch.contact_id, c.email,
           COALESCE(p.total_payment, 0)  AS total_payment,
           CAST(c.date_updated AS DATE)  AS last_activity,
           -- "Stage (Pipeline)" of the contact's current opp ('-' if none).
           COALESCE(el.stage_name || ' (' || el.pipeline_name || ')', '-') AS lead_stage,
           el.status                     AS lead_status,
           COALESCE(ef.reached_bls, 0)   AS reached_bls,
           COALESCE(a.has_appt, 0)       AS has_appt,
           COALESCE(vf.reached_mara, 0)             AS reached_mara,
           COALESCE(vf.reached_visa_postconsult, 0) AS reached_visa_postconsult,
           COALESCE(a.showed, 0)         AS showed,
           COALESCE(s.in_clt, 0)         AS in_clt,
           COALESCE(s.in_visa, 0)        AS in_visa,
           COALESCE(co.coe_clt, 0)       AS coe_clt,
           COALESCE(co.coe_edu, 0)       AS coe_edu
    FROM cohort ch
    JOIN fact_contacts c    ON c.contact_id  = ch.contact_id
    LEFT JOIN edu_flags ef  ON ef.contact_id = ch.contact_id
    LEFT JOIN appt a        ON a.contact_id  = ch.contact_id
    LEFT JOIN visa_flags vf ON vf.contact_id = ch.contact_id
    LEFT JOIN svc s         ON s.contact_id  = ch.contact_id
    LEFT JOIN coe co        ON co.contact_id = ch.contact_id
    LEFT JOIN pay p         ON p.contact_id  = ch.contact_id
    LEFT JOIN cur_latest el ON el.contact_id = ch.contact_id
)
SELECT * FROM (
    -- 1. Leads
    SELECT 'Leads' AS stage, 1 AS stage_order, contact_id, email, total_payment,
           last_activity, 'L2C - Education' AS pipeline, lead_stage, lead_status
    FROM base

    UNION ALL
    -- 2. Booking Link Shared  (of Leads)
    SELECT 'Booking Link Shared', 2, contact_id, email, total_payment,
           last_activity, 'L2C - Education', lead_stage, lead_status
    FROM base WHERE reached_bls = 1

    UNION ALL
    -- 3. Appointment Booked  (of 2 — real appointment OR MARA Appointment Booked)
    SELECT 'Appointment Booked', 3, contact_id, email, total_payment,
           last_activity, 'L2C - Education', lead_stage, lead_status
    FROM base WHERE reached_bls = 1 AND (has_appt = 1 OR reached_mara = 1)

    UNION ALL
    -- 4. Consultation Showed  (of 3 — appt outcome = show OR L2C-VISA Post Consultation)
    SELECT 'Consultation Showed', 4, contact_id, email, total_payment,
           last_activity, 'L2C - Education', lead_stage, lead_status
    FROM base WHERE reached_bls = 1 AND (showed = 1 OR reached_visa_postconsult = 1)

    UNION ALL
    -- 5. Entered Admission / Visa  (of 4 — opened a CLT-Onshore or L2C-VISA opp)
    SELECT 'Entered Admission / Visa', 5, contact_id, email, total_payment, last_activity,
           CASE WHEN in_clt = 1 AND in_visa = 1 THEN 'CLT-Onshore + L2C-VISA'
                WHEN in_clt = 1                 THEN 'CLT - Onshore Admission'
                ELSE 'L2C - VISA' END,
           lead_stage, lead_status
    FROM base WHERE reached_bls = 1 AND has_appt = 1 AND showed = 1
              AND (in_clt = 1 OR in_visa = 1)

    UNION ALL
    -- 6. COE Received  — every cohort contact with a COE Received opp in
    --    CLT-Onshore OR L2C-Education (one row per contact, so a CLT-Onshore
    --    COE that is NOT duplicated in L2C-Education is still counted once).
    SELECT 'COE Received', 6, contact_id, email, total_payment, last_activity,
           CASE WHEN coe_clt = 1 AND coe_edu = 1 THEN 'CLT-Onshore + L2C-Education'
                WHEN coe_clt = 1                 THEN 'CLT - Onshore Admission'
                ELSE 'L2C - Education' END,
           lead_stage, lead_status
    FROM base WHERE coe_clt = 1 OR coe_edu = 1
) t
ORDER BY stage_order;


-- =====================================================================
-- Meta Ads tab — COE Received by city (replaces the old "Converted" card).
-- =====================================================================
-- City = the COE opp OWNER's office (Gurbir / Navneet = Melbourne; the five
-- Sydney counsellors = Sydney). For COEs owned by admission staff or with NO
-- owner, fall back to the CONTACT's city bucket. Unique students — a COE in
-- both L2C-Education and CLT-Onshore counts once. Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_meta_coe_by_city AS
WITH coe_pipelines AS (
    SELECT pipeline_id FROM dim_pipelines
    WHERE pipeline_name IN ('L2C - Education', 'CLT - Onshore Admission')
),
coe_stages AS (
    SELECT stage_id FROM dim_stages
    WHERE LOWER(stage_name) = 'coe received'
      AND pipeline_id IN (SELECT pipeline_id FROM coe_pipelines)
),
coe AS (
    SELECT o.contact_id, LOWER(COALESCE(u.full_name, '')) AS owner_l
    FROM fact_opportunities o
    LEFT JOIN dim_users u ON u.user_id = o.assigned_user_id
    WHERE o.pipeline_id IN (SELECT pipeline_id FROM coe_pipelines)
      AND o.stage_id    IN (SELECT stage_id    FROM coe_stages)
      AND CAST(o.created_at AS DATE) BETWEEN $since AND $until
),
per_contact AS (
    SELECT contact_id,
           MAX(CASE WHEN owner_l LIKE '%gurbir%' OR owner_l LIKE '%navneet%'
                    THEN 1 ELSE 0 END) AS own_mel,
           MAX(CASE WHEN owner_l LIKE '%turab%'  OR owner_l LIKE '%nasir%'
                     OR owner_l LIKE '%kajal%'   OR owner_l LIKE '%wajah%'
                     OR owner_l LIKE '%saurab%'  THEN 1 ELSE 0 END) AS own_syd
    FROM coe GROUP BY contact_id
)
SELECT
    CASE
        WHEN pc.own_mel = 1 THEN 'Melbourne'
        WHEN pc.own_syd = 1 THEN 'Sydney'
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
             AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
             THEN 'Melbourne'
        WHEN (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
             AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
             THEN 'Sydney'
        ELSE 'Other'
    END AS city,
    COUNT(DISTINCT pc.contact_id) AS coes
FROM per_contact pc
JOIN fact_contacts c ON c.contact_id = pc.contact_id
GROUP BY 1;


-- =====================================================================
-- Funnel & Pipeline tab — fresh-leads cohort journey.
-- =====================================================================
-- Cohort = contacts currently in L2C-Education 'New Lead' stage, OR who filled
-- a form/survey in the window (AEST +10h). Binds: $since, $until.
--
-- vw_funnel_stage_journey  — how far the cohort gets through L2C-Education
--   stages (cumulative "reached"; Showed+ excludes No Show / High Potential /
--   Don't Touch). Returns (stage, stage_order, n).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_funnel_stage_journey AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
form_win AS (
    SELECT DISTINCT contact_id FROM subs
    WHERE CAST(submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
newlead AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id   AND st.stage_name   = 'New Lead'
),
cohort AS (SELECT contact_id FROM form_win UNION SELECT contact_id FROM newlead),
edu AS (
    SELECT o.contact_id, st.stage_order, st.stage_name
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    WHERE o.contact_id IN (SELECT contact_id FROM cohort)
),
flags AS (
    SELECT contact_id,
        MAX(CASE WHEN stage_order >= 4 AND stage_order <> 12 THEN 1 ELSE 0 END) AS bls,
        MAX(CASE WHEN stage_order >= 5 AND stage_order <> 12 THEN 1 ELSE 0 END) AS appt,
        MAX(CASE WHEN stage_name IN ('Post Consultation','Initial Requested',
                                     'Initial Received','COE Received') THEN 1 ELSE 0 END) AS showed,
        MAX(CASE WHEN stage_name IN ('Initial Received','COE Received') THEN 1 ELSE 0 END) AS initial,
        MAX(CASE WHEN stage_name = 'COE Received' THEN 1 ELSE 0 END) AS coe
    FROM edu GROUP BY contact_id
)
SELECT * FROM (
    SELECT 'Leads' AS stage, 1 AS stage_order, (SELECT COUNT(*) FROM cohort) AS n
    UNION ALL SELECT 'In L2C-Education',    2, (SELECT COUNT(DISTINCT contact_id) FROM edu)
    UNION ALL SELECT 'Booking Link Shared', 3, (SELECT COALESCE(SUM(bls),     0) FROM flags)
    UNION ALL SELECT 'Appointment Booked',  4, (SELECT COALESCE(SUM(appt),    0) FROM flags)
    UNION ALL SELECT 'Consultation Showed', 5, (SELECT COALESCE(SUM(showed),  0) FROM flags)
    UNION ALL SELECT 'Initial Received',    6, (SELECT COALESCE(SUM(initial), 0) FROM flags)
    UNION ALL SELECT 'COE Received',        7, (SELECT COALESCE(SUM(coe),     0) FROM flags)
) t
ORDER BY stage_order;


-- =====================================================================
-- Funnel & Pipeline tab v2 — one row per LEAD (created or revived in range).
-- =====================================================================
-- Each lead is checked INDEPENDENTLY against every stage (we do NOT chain a
-- funnel): reached flags use the lead's current stage_order per pipeline.
--   reached_qual  = L2C-Education stage_order >= 1 (Qualifier)
--   reached_bls   = L2C-Education stage_order >= 4 (Booking Link Shared)
--   reached_appt  = L2C-Education stage_order >= 5 (Appointment Booked)
--   reached_mara  = L2C-VISA stage_order >= 1 (MARA Appointment Booked)
--   reached_showed= real appointment 'show' OR Post Consultation (Edu/Visa)
--   reached_coe   = COE Received in L2C-Education OR CLT-Onshore Admission
-- Timing (no stage history exists, so only two proxies):
--   days_to_current = current opp updated_at - created_at
--   total_days      = lead date_added -> today
-- Plus source (GHL canonical_source / attribution), current pipeline+stage+
-- status, and all-time succeeded Total Payment. Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pipeline_cohort AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
-- Latest FORM/SURVEY submission detail per contact — drives the form-fill
-- attribution Source (high-level) and Attribution Source (form-fill detail).
sub_detail AS (
    SELECT contact_id, submitted_at, campaign, utm_content, event_source, session_source,
           form_name, event_form_name, CAST(NULL AS VARCHAR) AS survey_name
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, utm_content, event_source, session_source,
           CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR), survey_name
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub_d AS (
    SELECT contact_id, campaign, utm_content, event_source, session_source,
           form_name, event_form_name, survey_name,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM sub_detail
),
cohort AS (
    SELECT c.contact_id, c.email, c.canonical_source, c.first_attribution_source,
           c.assigned_user_id, c.date_added,
           CAST(c.date_added + INTERVAL 10 HOUR AS DATE) AS created_date,
           CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) AS revived_date
    FROM fact_contacts c
    LEFT JOIN latest_sub ls ON ls.contact_id = c.contact_id
    WHERE CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
       OR CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
edu AS (
    SELECT o.contact_id,
           MAX(CASE WHEN st.stage_order >= 1                      THEN 1 ELSE 0 END) AS reached_qual,
           MAX(CASE WHEN st.stage_order >= 4 AND st.stage_order <> 12 THEN 1 ELSE 0 END) AS reached_bls,
           MAX(CASE WHEN st.stage_order >= 5 AND st.stage_order <> 12 THEN 1 ELSE 0 END) AS reached_appt,
           MAX(CASE WHEN st.stage_name IN ('Post Consultation','Initial Requested',
                                           'Initial Received','COE Received') THEN 1 ELSE 0 END) AS edu_postconsult,
           MAX(CASE WHEN st.stage_name = 'COE Received' THEN 1 ELSE 0 END) AS edu_coe
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    GROUP BY o.contact_id
),
visa AS (
    SELECT o.contact_id,
           MAX(CASE WHEN st.stage_order >= 1 THEN 1 ELSE 0 END) AS reached_mara,
           MAX(CASE WHEN st.stage_name IN ('Post Consultation','50% Pro Fee Requested','50% Fee Paid')
                    THEN 1 ELSE 0 END) AS visa_postconsult
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - VISA'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    GROUP BY o.contact_id
),
clt_coe AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'CLT - Onshore Admission'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    WHERE st.stage_name = 'COE Received'
),
appt AS (
    SELECT contact_id, MAX(CASE WHEN LOWER(canonical_outcome) = 'show' THEN 1 ELSE 0 END) AS showed
    FROM fact_appointments WHERE LOWER(appointment_status) <> 'invalid' GROUP BY contact_id
),
cur_opp AS (
    SELECT o.contact_id, p.pipeline_name, st.stage_name, o.status, o.created_at, o.updated_at,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.updated_at DESC, o.created_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
),
pay AS (
    SELECT contact_id, SUM(amount - COALESCE(amount_refunded, 0)) AS total_payment
    FROM fact_payments WHERE LOWER(status) = 'succeeded' GROUP BY contact_id
)
SELECT
    ch.contact_id, ch.email,
    GREATEST(COALESCE(ch.created_date, DATE '1900-01-01'),
             COALESCE(ch.revived_date, DATE '1900-01-01'))         AS event_date,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN 1 ELSE 0 END AS is_created,
    CASE WHEN ch.revived_date BETWEEN $since AND $until
              AND (ch.created_date IS NULL OR ch.created_date < $since) THEN 1 ELSE 0 END AS is_revived,
    -- Source = the FORM-FILL attribution, high-level (Referral / Meta Paid / ...).
    CASE
        WHEN COALESCE(lsd.campaign,'') <> '' OR LOWER(COALESCE(lsd.session_source,'')) = 'paid social'
             OR LOWER(COALESCE(lsd.event_source,'')) = 'paid social'             THEN 'Meta Paid'
        WHEN LOWER(COALESCE(lsd.event_source,'')) = 'organic search'             THEN 'Organic / SEO'
        WHEN LOWER(COALESCE(lsd.event_source,'')) = 'referral'                   THEN 'Referral'
        WHEN LOWER(COALESCE(lsd.event_source,'')) IN ('direct','direct traffic') THEN 'Direct'
        WHEN LOWER(COALESCE(lsd.event_source,'')) = 'social media'               THEN 'Social Media'
        WHEN LOWER(COALESCE(lsd.event_source,'')) = 'email marketing'            THEN 'Email Marketing'
        WHEN lsd.survey_name IS NOT NULL                                         THEN 'Survey'
        WHEN COALESCE(lsd.event_source,'') <> ''                                 THEN lsd.event_source
        WHEN ch.canonical_source = 'meta_paid'                                   THEN 'Meta Paid'
        WHEN ch.canonical_source = 'website_form'                                THEN 'Website Form'
        WHEN ch.canonical_source = 'organic_seo'                                 THEN 'Organic / SEO'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'referral'        THEN 'Referral'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'social media'    THEN 'Social Media'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'direct traffic'  THEN 'Direct'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'crm ui'          THEN 'CRM / Manual'
        ELSE 'Other'
    END                                                            AS source,
    -- Attribution Source = the FORM-FILL detail (campaign / form / survey name).
    COALESCE(NULLIF(
        CASE WHEN COALESCE(lsd.campaign,'') <> '' AND COALESCE(lsd.utm_content,'') <> ''
                  THEN lsd.campaign || ' -- ' || lsd.utm_content
             WHEN COALESCE(lsd.campaign,'') <> ''        THEN lsd.campaign
             WHEN COALESCE(lsd.form_name,'') <> ''       THEN lsd.form_name
             WHEN COALESCE(lsd.event_form_name,'') <> '' THEN lsd.event_form_name
             WHEN COALESCE(lsd.survey_name,'') <> ''     THEN lsd.survey_name
             WHEN COALESCE(lsd.session_source,'') <> ''  THEN lsd.session_source
             WHEN COALESCE(lsd.event_source,'') <> ''    THEN lsd.event_source
             ELSE NULL END, ''), '(no form attribution)')          AS attribution_source,
    COALESCE(u.full_name, '(unassigned)')                          AS owner_name,
    CASE WHEN u.full_name LIKE '%Gurbir%' OR u.full_name LIKE '%Navneet%' THEN 'Melbourne'
         WHEN u.full_name IS NULL OR u.full_name = '' THEN 'Unassigned'
         ELSE 'Sydney' END                                         AS owner_city,
    COALESCE(e.reached_qual, 0)  AS reached_qual,
    COALESCE(e.reached_bls, 0)   AS reached_bls,
    COALESCE(e.reached_appt, 0)  AS reached_appt,
    COALESCE(v.reached_mara, 0)  AS reached_mara,
    CASE WHEN COALESCE(a.showed,0)=1 OR COALESCE(e.edu_postconsult,0)=1
              OR COALESCE(v.visa_postconsult,0)=1 THEN 1 ELSE 0 END  AS reached_showed,
    CASE WHEN COALESCE(e.edu_coe,0)=1 OR cc.contact_id IS NOT NULL THEN 1 ELSE 0 END AS reached_coe,
    co.pipeline_name AS cur_pipeline,
    co.stage_name    AS cur_stage,
    co.status        AS cur_status,
    GREATEST(DATEDIFF('day', co.created_at, co.updated_at), 0) AS days_to_current,
    GREATEST(DATEDIFF('day', ch.date_added, CURRENT_DATE), 0)  AS total_days,
    COALESCE(p.total_payment, 0) AS total_payment
FROM cohort ch
LEFT JOIN edu e         ON e.contact_id   = ch.contact_id
LEFT JOIN visa v        ON v.contact_id   = ch.contact_id
LEFT JOIN clt_coe cc    ON cc.contact_id  = ch.contact_id
LEFT JOIN appt a        ON a.contact_id   = ch.contact_id
LEFT JOIN cur_opp co    ON co.contact_id  = ch.contact_id AND co.rn = 1
LEFT JOIN pay p         ON p.contact_id   = ch.contact_id
LEFT JOIN latest_sub_d lsd ON lsd.contact_id = ch.contact_id AND lsd.rn = 1
LEFT JOIN dim_users u   ON u.user_id      = ch.assigned_user_id;


-- vw_funnel_pipeline_switch — how many cohort leads appear in each pipeline
-- (a lead can be in several; that overlap IS the "switch"). Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_funnel_pipeline_switch AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
form_win AS (
    SELECT DISTINCT contact_id FROM subs
    WHERE CAST(submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
newlead AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id   AND st.stage_name   = 'New Lead'
),
cohort AS (SELECT contact_id FROM form_win UNION SELECT contact_id FROM newlead)
SELECT p.pipeline_name AS pipeline, COUNT(DISTINCT o.contact_id) AS contacts
FROM cohort ch
JOIN fact_opportunities o ON o.contact_id = ch.contact_id
JOIN dim_pipelines      p ON p.pipeline_id = o.pipeline_id
GROUP BY 1
ORDER BY 2 DESC;


-- =====================================================================
-- Meta Ads tab — Bookings by city (new "BOOKED" card logic).
-- =====================================================================
-- Of the fresh-leads cohort (L2C-Education 'New Lead' stage OR filled a form in
-- the window, AEST +10h), how many booked a calendar. City is decided by the
-- CALENDAR: Gurbir's + Navneet's calendars = Melbourne; every other calendar =
-- Sydney. Counts distinct cohort contacts per city (a lead who booked in both
-- cities counts in each). Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_meta_bookings_by_city AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
form_win AS (
    SELECT DISTINCT contact_id FROM subs
    WHERE CAST(submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
newlead AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id   AND st.stage_name   = 'New Lead'
),
cohort AS (SELECT contact_id FROM form_win UNION SELECT contact_id FROM newlead)
SELECT
    CASE WHEN a.calendar_id IN ('hsVntQS9KwIw8eF4D8ef', 'o4AfsJ45rEkewmENut12',   -- Gurbir
                                'XJS0nt92447DgYSmxVkP', 'hkL937P7e6XTzy58dOZ7')   -- Navneet
         THEN 'Melbourne' ELSE 'Sydney' END AS city,
    COUNT(DISTINCT a.contact_id) AS bookings
FROM cohort ch
JOIN fact_appointments a ON a.contact_id = ch.contact_id
WHERE LOWER(a.appointment_status) <> 'invalid'
GROUP BY 1;


-- vw_meta_bookings_detail — the booked cohort leads (one row per contact+city)
-- for the Bookings drill-down: email, campaign (Latest Source), pipeline, stage,
-- status. City by calendar (Gurbir/Navneet = Melbourne). Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_meta_bookings_detail AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
form_win AS (
    SELECT DISTINCT contact_id FROM subs
    WHERE CAST(submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
newlead AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - Education'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id   AND st.stage_name   = 'New Lead'
),
cohort AS (SELECT contact_id FROM form_win UNION SELECT contact_id FROM newlead),
booked AS (
    SELECT DISTINCT a.contact_id,
        CASE WHEN a.calendar_id IN ('hsVntQS9KwIw8eF4D8ef', 'o4AfsJ45rEkewmENut12',
                                    'XJS0nt92447DgYSmxVkP', 'hkL937P7e6XTzy58dOZ7')
             THEN 'Melbourne' ELSE 'Sydney' END AS city
    FROM cohort ch
    JOIN fact_appointments a ON a.contact_id = ch.contact_id
    WHERE LOWER(a.appointment_status) <> 'invalid'
),
latest_opp AS (
    SELECT contact_id, pipeline_id, stage_id, status,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
)
SELECT
    b.contact_id,
    c.email,
    b.city,
    COALESCE(NULLIF(ls.latest_source_campaign, ''), '(no campaign)') AS campaign,
    COALESCE(p.pipeline_name, '-')                                   AS pipeline,
    COALESCE(s.stage_name, '-')                                      AS stage,
    COALESCE(lo.status, '-')                                         AS status
FROM booked b
JOIN fact_contacts c                     ON c.contact_id  = b.contact_id
LEFT JOIN fact_contact_latest_source ls  ON ls.contact_id = b.contact_id
LEFT JOIN latest_opp lo                  ON lo.contact_id = b.contact_id AND lo.rn = 1
LEFT JOIN dim_pipelines p                ON p.pipeline_id = lo.pipeline_id
LEFT JOIN dim_stages    s                ON s.stage_id    = lo.stage_id;


-- =====================================================================
-- Forecast & Goals tab — per-lead / per-appointment / per-day feeds.
-- =====================================================================
-- The dashboard buckets these into Month / Week / Day periods in pandas.
-- Binds: $since, $until (a wide lookback range).

-- vw_forecast_leads — one row per CONTACT created OR revived in range:
--   created = fact_contacts.date_added in range (AEST +10h)
--   revived = OLD contact (created before range) whose latest form/survey
--             submission falls in range (they re-filled a form)
-- Source bucket = GHL contact canonical_source: Meta = meta_paid, Website =
-- website_form/organic, Others = the rest. has_coe / has_mara flag COE Received
-- / MARA Appointment Booked. event_date = the more recent of (created, revived).
-- Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_forecast_leads AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
coe AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    WHERE st.stage_name = 'COE Received'
      AND p.pipeline_name IN ('L2C - Education', 'CLT - Onshore Admission')
),
mara AS (
    SELECT DISTINCT o.contact_id FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - VISA'
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
    WHERE st.stage_order >= 1                                  -- MARA Appointment Booked onward
),
base AS (
    SELECT c.contact_id, c.canonical_source,
           CAST(c.date_added + INTERVAL 10 HOUR AS DATE) AS created_date,
           CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) AS revived_date
    FROM fact_contacts c
    LEFT JOIN latest_sub ls ON ls.contact_id = c.contact_id
)
SELECT
    b.contact_id,
    GREATEST(COALESCE(b.created_date, DATE '1900-01-01'),
             COALESCE(b.revived_date, DATE '1900-01-01'))              AS event_date,
    CASE WHEN b.created_date BETWEEN $since AND $until THEN 1 ELSE 0 END AS is_created,
    CASE WHEN b.revived_date BETWEEN $since AND $until
              AND (b.created_date IS NULL OR b.created_date < $since)
         THEN 1 ELSE 0 END                                            AS is_revived,
    CASE
        WHEN LOWER(COALESCE(b.canonical_source, '')) IN ('meta_paid','paid_social','paid social')
            THEN 'Meta'
        WHEN LOWER(COALESCE(b.canonical_source, '')) IN
             ('website_form','organic_seo','organic search','organic','seo')
            THEN 'Website'
        ELSE 'Others'
    END                                                               AS bucket,
    CASE WHEN co.contact_id IS NOT NULL THEN 1 ELSE 0 END             AS has_coe,
    CASE WHEN ma.contact_id IS NOT NULL THEN 1 ELSE 0 END             AS has_mara
FROM base b
LEFT JOIN coe  co ON co.contact_id = b.contact_id
LEFT JOIN mara ma ON ma.contact_id = b.contact_id
WHERE (b.created_date BETWEEN $since AND $until)
   OR (b.revived_date BETWEEN $since AND $until);


-- vw_forecast_appts — one row per non-invalid appointment created in range,
-- with whether it showed. Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_forecast_appts AS
SELECT CAST(date_added AS DATE) AS appt_date,
       CASE WHEN LOWER(canonical_outcome) = 'show' THEN 1 ELSE 0 END AS showed
FROM fact_appointments
WHERE LOWER(appointment_status) <> 'invalid'
  AND CAST(date_added AS DATE) BETWEEN $since AND $until;


-- vw_forecast_spend — Meta ad spend per day in range. Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_forecast_spend AS
SELECT date AS spend_date, SUM(spend) AS spend
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until
GROUP BY date;
