-- =====================================================================
-- The Migration Dashboard — Meta Ads + SEO & Traffic Card SQL pack
-- =====================================================================
-- One view per scorecard set. Each view returns 2 rows:
--   tag='current'  → metric for the selected period
--   tag='prior'    → metric for the same-length window immediately prior
-- Streamlit computes (current - prior) / prior for the green/red delta.
--
-- Same bind conventions as executive_cards.sql:
--   $since, $until, $prior_since, $prior_until, $city
--
-- City filter:
--   - Meta tabs: filtered via fact_meta_daily.account_label
--                (Melbourne / Sydney accounts; Others/Unidentified
--                are no-ops for Meta).
--   - SEO tabs:  intentionally NOT city-filtered. Sessions & search-
--                console rankings are site-wide; GA4/GSC city dims
--                don't map to the GHL contact city used elsewhere.
-- =====================================================================


-- =====================================================================
-- Meta Ads — single view returning all six KPI columns
-- =====================================================================
CREATE OR REPLACE VIEW vw_meta_tab_totals AS
SELECT
    CASE
        WHEN date BETWEEN $since AND $until            THEN 'current'
        WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    SUM(spend)                                  AS spend,
    SUM(impressions)                            AS impressions,
    SUM(clicks)                                 AS clicks,
    -- Leads = Meta 'Results' (result_count) so the headline matches Ads Manager
    -- AND the Executive tab. (total_leads is the canonical instant+pixel sum,
    -- kept available below for reference.)
    -- Real lead conversions: Meta Instant Form ('lead'), Facebook pixel Lead
    -- standard event ('offsite_conversion.fb_pixel_lead'), AND landing-page
    -- Lead custom-conversion events ('offsite_conversion.fb_pixel_custom') —
    -- the last one fires on the website when a Meta-driven landing page form
    -- is submitted and DOES become a GHL contact. Only the view-content event
    -- ('fb_pixel_view_content') is excluded as it's pure page-view noise.
    COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) AS leads,
    SUM(total_leads)                            AS canonical_leads,
    CASE WHEN SUM(impressions) > 0
         THEN CAST(SUM(clicks) AS DOUBLE) / SUM(impressions)
         ELSE NULL
    END                                         AS ctr,
    CASE WHEN COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) > 0
         THEN SUM(spend) / SUM(result_count) FILTER (WHERE result_event = 'lead')
         ELSE NULL
    END                                         AS cpl,
    COUNT(DISTINCT account_id)                  AS account_count
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
-- SEO & Traffic — GA4 totals
-- =====================================================================
-- engaged_sessions ≈ sessions × (1 - bounce_rate) per row, summed.
-- Bounce_rate is row-level (per date×source×medium×country×city), so
-- the multiply must happen BEFORE the SUM.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_ga4_tab_totals AS
-- Topline site totals come from fact_ga4_daily (date-only GA4 query) so Sessions
-- / Total Users / Engagement Rate match the GA4 UI exactly. fact_ga4_sessions is
-- segmented by source/medium/country/city and over-counts via GA4's "(other)"
-- bucketing, so it is NOT used here. Engagement Rate is re-derived over the whole
-- period (SUM engaged ÷ SUM sessions) — a daily rate can't be summed.
WITH daily AS (
    SELECT
        CASE
            WHEN date BETWEEN $since AND $until            THEN 'current'
            WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        SUM(sessions)         AS sessions,
        SUM(engaged_sessions) AS engaged_sessions,
        SUM(total_users)      AS total_users,
        SUM(active_users)     AS active_users,
        CASE WHEN SUM(sessions) > 0
             THEN SUM(engaged_sessions) * 1.0 / SUM(sessions)
             ELSE NULL END    AS engagement_rate
    FROM fact_ga4_daily
    WHERE date BETWEEN $since AND $until
       OR date BETWEEN $prior_since AND $prior_until
    GROUP BY 1
),
-- Canonical conversion events (ratified per audit Decision 5):
--   contact_us, generate_lead, book_consultation_page, blogs_to_consultation
events AS (
    SELECT
        CASE
            WHEN date BETWEEN $since AND $until            THEN 'current'
            WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
        END AS tag,
        SUM(event_count) AS key_events
    FROM fact_ga4_events
    WHERE event_name IN ('contact_us', 'generate_lead',
                         'book_consultation_page', 'blogs_to_consultation')
      AND (date BETWEEN $since AND $until
           OR date BETWEEN $prior_since AND $prior_until)
    GROUP BY 1
)
SELECT
    d.tag,
    d.sessions,
    d.engaged_sessions,
    d.total_users,
    d.active_users,
    d.engagement_rate,
    COALESCE(e.key_events, 0) AS key_events
FROM daily d
LEFT JOIN events e ON e.tag = d.tag
WHERE d.tag IS NOT NULL
  AND COALESCE($city, '') IS NOT NULL;  -- $city is intentionally not filtered (site-wide)


-- =====================================================================
-- SEO & Traffic — GSC totals
-- =====================================================================
-- We pick a single dimension (device) so totals don't double-count.
-- All four GSC dimensions roll up to the same site-wide totals;
-- device is the smallest by row count.
-- avg_position is impressions-weighted so high-impression pages dominate.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_gsc_tab_totals AS
SELECT
    CASE
        WHEN date BETWEEN $since AND $until            THEN 'current'
        WHEN date BETWEEN $prior_since AND $prior_until THEN 'prior'
    END AS tag,
    SUM(clicks)        AS clicks,
    SUM(impressions)   AS impressions,
    CASE WHEN SUM(impressions) > 0
         THEN SUM(position * impressions) / SUM(impressions)
         ELSE NULL
    END                AS avg_position
FROM fact_gsc_queries
WHERE dimension_name = 'device'
  AND (date BETWEEN $since AND $until
       OR date BETWEEN $prior_since AND $prior_until)
  AND COALESCE($city, '') IS NOT NULL  -- $city is intentionally not filtered (site-wide), but DuckDB binds require a reference
GROUP BY 1
HAVING tag IS NOT NULL;


-- =====================================================================
-- ===============  META ADS — SCORECARD DRILL-DOWN  ===================
-- =====================================================================
-- All drill-down views below use the CURRENT period only ($since..$until)
-- and accept the in-modal $city filter:
--   Meta-side views   → account_label (Melbourne / Sydney accounts)
--   GHL-side views    → fact_contacts.city pattern match (Mel / Syd / Others / Unidentified)
-- =====================================================================


-- ---- Campaigns: leads + spend + CPL per campaign (Meta) ----
CREATE OR REPLACE VIEW vw_drill_meta_campaigns AS
SELECT
    account_label,
    campaign_name,
    -- 'Results' = result_count, matching the Ads Manager Results column.
    -- Real lead-form submissions only (pixel events excluded — see vw_meta_tab_totals note)
    COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) AS results,
    MAX(result_event)       AS result_event,
    SUM(total_leads)        AS canonical_leads,
    SUM(spend)              AS spend,
    CASE WHEN COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) > 0
         THEN SUM(spend) / SUM(result_count) FILTER (WHERE result_event = 'lead')
         ELSE NULL END      AS cpl
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until
  AND ($city = 'All'
       OR ($city = 'Melbourne'    AND account_label = 'Melbourne')
       OR ($city = 'Sydney'       AND account_label = 'Sydney')
       OR ($city = 'Others'       AND 1=0)
       OR ($city = 'Unidentified' AND 1=0))
GROUP BY account_label, campaign_name
HAVING SUM(spend) > 0 OR SUM(result_count) > 0 OR SUM(total_leads) > 0
ORDER BY results DESC, spend DESC;


-- ---- Meta lead-type composition per campaign (explains Meta vs GHL gap) ----
-- Sourced from fact_meta_insights (campaign-grain snapshot of the latest ETL
-- window). instant_form_leads = real lead forms that create GHL contacts;
-- pixel_custom_events = website pixel fires (multiple per visitor, do NOT map
-- 1:1 to people) — campaigns heavy on pixel_custom show far more Meta "leads"
-- than GHL opportunities. Not date-filtered (insights is a window snapshot).
CREATE OR REPLACE VIEW vw_drill_meta_lead_types AS
SELECT
    account_label,
    campaign_name,
    SUM(instant_form_leads)  AS instant_form,
    SUM(pixel_lead_events)   AS pixel_lead,
    SUM(pixel_custom_events) AS pixel_custom,
    SUM(messenger_leads)     AS messenger,
    SUM(total_leads)         AS total_leads
FROM fact_meta_insights
WHERE ($city = 'All'
       OR ($city = 'Melbourne'    AND account_label = 'Melbourne')
       OR ($city = 'Sydney'       AND account_label = 'Sydney')
       OR ($city = 'Others'       AND 1=0)
       OR ($city = 'Unidentified' AND 1=0))
GROUP BY 1, 2
HAVING SUM(total_leads) > 0
ORDER BY total_leads DESC;


-- ---- GHL social opps grouped by CAMPAIGN (source text before ' -- ') ----
-- Lets you compare campaign-level GHL opportunity counts directly against the
-- Meta campaign. SPLIT_PART on ' -- ' keeps the campaign prefix; sources with
-- no ' -- ' (e.g. 'SURVEY', 'Points Calculator') stay whole.
CREATE OR REPLACE VIEW vw_drill_social_campaign_sources AS
SELECT
    TRIM(SPLIT_PART(COALESCE(NULLIF(TRIM(o.source), ''), '(no source)'), ' -- ', 1)) AS campaign,
    COUNT(*) AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- ---- Opportunity totals (replaces the '1 account(s)' secondary) ----
-- Returns one row: total opps created in range, plus the paid-social/social
-- subset, plus Melbourne / Sydney / Other / Unidentified split.
CREATE OR REPLACE VIEW vw_drill_opp_totals AS
SELECT
    COUNT(*)                                                                       AS total_opps,
    COUNT(*) FILTER (
        WHERE LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
    )                                                                              AS social_opps,
    COUNT(*) FILTER (
        WHERE (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
          AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
    )                                                                              AS melbourne_opps,
    COUNT(*) FILTER (
        WHERE (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
          AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
    )                                                                              AS sydney_opps,
    COUNT(*) FILTER (WHERE COALESCE(c.city,'') = '')                               AS unidentified_opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''));


-- ---- Latest-attribution counts (opps in range, by contact latest attribution) ----
CREATE OR REPLACE VIEW vw_drill_attribution_counts AS
SELECT
    COALESCE(c.latest_attribution_source, '(none)') AS latest_attribution,
    COUNT(*)                                        AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- ---- First-attribution counts (opps in range, by contact FIRST attribution) ----
CREATE OR REPLACE VIEW vw_drill_first_attribution_counts AS
SELECT
    COALESCE(c.first_attribution_source, '(none)') AS first_attribution,
    COUNT(*)                                        AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- ---- Opportunity sources for SOCIAL opps (latest attr = paid social / social media) ----
CREATE OR REPLACE VIEW vw_drill_social_opp_sources AS
SELECT
    COALESCE(NULLIF(TRIM(o.source), ''), '(no source)') AS opportunity_source,
    c.latest_attribution_source                          AS latest_attribution,
    COUNT(*)                                             AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1, 2
ORDER BY opps DESC;


-- ---- Medium breakdown (Form / Survey / Manual / Calendar / Pending) for social opps ----
CREATE OR REPLACE VIEW vw_drill_medium_counts AS
SELECT
    CASE
        WHEN c.latest_attribution_medium IS NULL OR TRIM(c.latest_attribution_medium) = ''
             THEN 'Pending (no form/survey)'
        ELSE c.latest_attribution_medium
    END                  AS medium,
    COUNT(*)             AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- ---- Pipeline breakdown for SOCIAL opps (reconcile Meta leads vs pipelines) ----
-- Shows which pipelines Paid Social / Social-media-attributed opps land in,
-- so leads aren't assumed to be only in L2C - Education.
CREATE OR REPLACE VIEW vw_drill_social_pipelines AS
SELECT
    COALESCE(p.pipeline_name, '(no pipeline)') AS pipeline,
    COUNT(*)                                   AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
LEFT JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- ---- Opportunity status breakdown for social opps ----
CREATE OR REPLACE VIEW vw_drill_opp_status AS
SELECT
    COALESCE(o.status, '(unknown)') AS status,
    COUNT(*)                        AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY 1
ORDER BY opps DESC;


-- =====================================================================
-- ============  SEO & TRAFFIC TAB — Website Form leads  ===============
-- =====================================================================
-- Calendar → counsellor → city lookup. Marketing Lead locked:
--   Melbourne: Navneet Kaur, Gurbir Singh
--   Sydney:    Turab, Nasir, Kajal, Wajahad, Saurab
-- Used by the SEO tab to attribute Website Leads to a city via the
-- counsellor whose calendar the contact booked, since contact.city is
-- empty for ~90% of organic contacts.
-- =====================================================================
CREATE OR REPLACE VIEW vw_counsellor_calendars AS
SELECT * FROM (VALUES
    ('aTMcDOwcpe5TOohPT1Rz', 'Turab',        'Sydney'),
    ('uwCBo7Y0cAWLs6ZqPjJI', 'Turab',        'Sydney'),
    ('Zyrz08TZ6BaAruWxERy5', 'Nasir Nawaz',  'Sydney'),
    ('gttsLvMBPKFfslnOuwHT', 'Nasir Nawaz',  'Sydney'),
    ('hsVntQS9KwIw8eF4D8ef', 'Gurbir Singh', 'Melbourne'),
    ('o4AfsJ45rEkewmENut12', 'Gurbir Singh', 'Melbourne'),
    ('1FgpIJPxw6RWveeJLsb8', 'Kajal',        'Sydney'),
    ('RF7bh7b3avrzStoTE8ho', 'Kajal',        'Sydney'),
    ('4HLkV0BSHX7EvJ3jniC9', 'Wajahad',      'Sydney'),
    ('hsCSqcYHrXwL55NffEFi', 'Wajahad',      'Sydney'),
    ('4mKKf1IPwIq50N4OzOTI', 'Saurab',       'Sydney'),
    ('vjmOhJPIT4pAPzCyCmdT', 'Saurab',       'Sydney'),
    ('XJS0nt92447DgYSmxVkP', 'Navneet Kaur', 'Melbourne'),
    ('hkL937P7e6XTzy58dOZ7', 'Navneet Kaur', 'Melbourne')
) AS t(calendar_id, counsellor_name, counsellor_city);


-- =====================================================================
-- Website Lead cohort (LOCKED definition, used by all SEO tab views):
--   A contact whose LATEST form OR survey submission has
--   event_source = 'Organic Search'. Lead date = contact's date_added.
-- =====================================================================
CREATE OR REPLACE VIEW vw_seo_website_lead_cohort AS
WITH all_subs AS (
    -- Union form + survey submissions; pick the most recent per contact
    SELECT contact_id, submitted_at, event_source, NULL AS survey_name, page_url, page_path, referrer
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, event_source, survey_name,        page_url, page_path, referrer
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
)
SELECT contact_id, submitted_at AS latest_sub_at, event_source, survey_name, page_url, page_path, referrer
FROM latest
WHERE rn = 1 AND event_source = 'Organic Search';


-- =====================================================================
-- Website Leads per counsellor-city. Mel/Syd are derived from the
-- contact's LATEST appointment's calendar; contacts with NO appointment
-- bucket into 'Unassigned'. Bookings/Showed inherit the lead's date
-- for windowing (cohort: leads whose contact was created in [since,until]).
-- =====================================================================
CREATE OR REPLACE VIEW vw_seo_website_leads_per_city AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab','Sydney'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab','Sydney'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz','Sydney'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz','Sydney'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh','Melbourne'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh','Melbourne'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal','Sydney'),
        ('RF7bh7b3avrzStoTE8ho','Kajal','Sydney'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad','Sydney'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad','Sydney'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab','Sydney'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab','Sydney'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur','Melbourne'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur','Melbourne')
    ) AS t(calendar_id, counsellor_name, counsellor_city)
),
all_subs AS (
    SELECT contact_id, submitted_at, event_source FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, event_source FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, event_source, submitted_at,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
website_contacts AS (
    SELECT contact_id, submitted_at FROM latest_sub WHERE rn=1
),
-- Website Lead = GHL contact whose canonical_source is website_form / organic
-- (matches the Forecast tab's Website Leads), created OR revived in window.
cohort AS (
    SELECT c.contact_id, c.date_added
    FROM fact_contacts c
    LEFT JOIN website_contacts wc ON wc.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source,'')) IN
          ('website_form','organic_seo','organic search','organic','seo')
      AND (CAST(c.date_added   + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
           OR CAST(wc.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
latest_appt AS (
    SELECT contact_id, calendar_id, canonical_outcome,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IN (SELECT contact_id FROM cohort)
      AND LOWER(appointment_status) <> 'invalid'
),
attributed AS (
    SELECT c.contact_id,
           COALESCE(cc.counsellor_city, 'Unassigned') AS city_group,
           CASE WHEN la.contact_id IS NOT NULL THEN 1 ELSE 0 END AS has_booking,
           CASE WHEN LOWER(la.canonical_outcome)='show'   THEN 1 ELSE 0 END AS showed,
           CASE WHEN LOWER(la.canonical_outcome)='noshow' THEN 1 ELSE 0 END AS noshow
    FROM cohort c
    LEFT JOIN latest_appt la ON la.contact_id = c.contact_id AND la.rn = 1
    LEFT JOIN calendars cc   ON cc.calendar_id = la.calendar_id
)
SELECT city_group,
       COUNT(*)         AS website_leads,
       SUM(has_booking) AS bookings,
       SUM(showed)      AS showed,
       SUM(noshow)      AS noshow,
       CASE WHEN COUNT(*) > 0
            THEN SUM(has_booking) * 1.0 / COUNT(*) END         AS booking_rate,
       CASE WHEN SUM(has_booking) > 0
            THEN SUM(showed) * 1.0 / SUM(has_booking) END      AS show_rate
FROM attributed
GROUP BY city_group;


-- Per-counsellor breakdown for the Bookings / Showed drill-down modal.
CREATE OR REPLACE VIEW vw_seo_website_leads_per_counsellor AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab','Sydney'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab','Sydney'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz','Sydney'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz','Sydney'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh','Melbourne'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh','Melbourne'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal','Sydney'),
        ('RF7bh7b3avrzStoTE8ho','Kajal','Sydney'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad','Sydney'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad','Sydney'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab','Sydney'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab','Sydney'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur','Melbourne'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur','Melbourne')
    ) AS t(calendar_id, counsellor_name, counsellor_city)
),
all_subs AS (
    SELECT contact_id, submitted_at, event_source FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, event_source FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, event_source, submitted_at,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
website_contacts AS (
    SELECT contact_id, submitted_at FROM latest_sub WHERE rn=1
),
cohort AS (   -- canonical website_form, created OR revived (matches Forecast)
    SELECT c.contact_id
    FROM fact_contacts c
    LEFT JOIN website_contacts wc ON wc.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source,'')) IN
          ('website_form','organic_seo','organic search','organic','seo')
      AND (CAST(c.date_added   + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
           OR CAST(wc.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
latest_appt AS (
    SELECT contact_id, calendar_id, canonical_outcome,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IN (SELECT contact_id FROM cohort)
      AND LOWER(appointment_status) <> 'invalid'
)
SELECT
    cc.counsellor_city AS city_group,
    cc.counsellor_name AS counsellor,
    COUNT(*)                                                              AS bookings,
    SUM(CASE WHEN LOWER(la.canonical_outcome)='show'   THEN 1 ELSE 0 END) AS showed,
    SUM(CASE WHEN LOWER(la.canonical_outcome)='noshow' THEN 1 ELSE 0 END) AS noshow
FROM cohort c
JOIN latest_appt la ON la.contact_id = c.contact_id AND la.rn = 1
JOIN calendars   cc ON cc.calendar_id = la.calendar_id
GROUP BY cc.counsellor_city, cc.counsellor_name;


-- Activity breakdown for the Website Lead / Bookings / Showed drill-down modal.
-- One row per cohort contact with survey_name, page_url, referrer, booking + show flags.
-- The Python side aggregates this 3 ways (survey, page, referrer) per modal.
-- =====================================================================
-- Per-form-submission activity table for the Website Leads scorecard.
-- One row PER SUBMISSION (form or survey) by a Website Lead cohort contact,
-- with the page URL, form/survey name (parent_name in the GHL activity
-- feed), source, and the contact's latest opportunity pipeline + stage +
-- lead date. Use case: drill into "where exactly did this lead come from,
-- which form did they fill?".
-- =====================================================================
CREATE OR REPLACE VIEW vw_seo_website_leads_activities AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab','Sydney'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab','Sydney'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz','Sydney'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz','Sydney'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh','Melbourne'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh','Melbourne'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal','Sydney'),
        ('RF7bh7b3avrzStoTE8ho','Kajal','Sydney'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad','Sydney'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad','Sydney'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab','Sydney'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab','Sydney'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur','Melbourne'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur','Melbourne')
    ) AS t(calendar_id, counsellor_name, counsellor_city)
),
all_subs AS (
    -- Form + survey submissions (organic-search cohort filter applied below)
    SELECT contact_id, submitted_at, event_source, page_url,
           COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,'')) AS activity_name,
           'form' AS activity_kind
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, event_source, page_url,
           NULLIF(survey_name,'')                                     AS activity_name,
           'survey' AS activity_kind
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, event_source AS latest_event_source, submitted_at,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
website_contacts AS (
    SELECT contact_id, submitted_at FROM latest_sub WHERE rn=1
),
cohort AS (   -- canonical website_form, created OR revived (matches Forecast)
    SELECT c.contact_id, c.email, c.date_added, c.assigned_user_id
    FROM fact_contacts c
    LEFT JOIN website_contacts wc ON wc.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source,'')) IN
          ('website_form','organic_seo','organic search','organic','seo')
      AND (CAST(c.date_added   + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
           OR CAST(wc.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
latest_opp AS (
    SELECT contact_id, pipeline_id, stage_id, status,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
    WHERE contact_id IN (SELECT contact_id FROM cohort)
),
latest_appt AS (
    SELECT contact_id, calendar_id,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IN (SELECT contact_id FROM cohort)
      AND LOWER(appointment_status) <> 'invalid'
)
SELECT
    c.email,
    a.submitted_at,
    a.page_url,
    a.activity_kind,
    COALESCE(a.activity_name, '(unnamed)')                AS form_name,
    a.event_source                                        AS source,
    p.pipeline_name                                       AS pipeline,
    s.stage_name                                          AS stage,
    lo.status                                             AS opp_status,
    COALESCE(cal.counsellor_city, 'Unassigned')        AS city_group,
    c.date_added                                          AS lead_date
FROM cohort c
JOIN all_subs a            ON a.contact_id = c.contact_id
LEFT JOIN latest_opp lo    ON lo.contact_id = c.contact_id AND lo.rn = 1
LEFT JOIN dim_pipelines p  ON p.pipeline_id = lo.pipeline_id
LEFT JOIN dim_stages   s   ON s.stage_id    = lo.stage_id
LEFT JOIN latest_appt la   ON la.contact_id = c.contact_id AND la.rn = 1
LEFT JOIN calendars   cal  ON cal.calendar_id = la.calendar_id;


-- =====================================================================
-- Per-contact drill-down for the SEO tab's Trend/Table view.
-- One row per Website Lead contact (Organic-Search cohort, contact created
-- in window). Columns: email, pipeline, stage, owner, opp status, latest
-- appointment status (if any), Latest Source (computed live — not from the
-- stored GHL custom field), counsellor city, has_booking, showed.
-- Used by the unified Trend/Table view on the SEO & Traffic tab.
-- =====================================================================
CREATE OR REPLACE VIEW vw_seo_website_leads_detail AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab','Sydney'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab','Sydney'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz','Sydney'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz','Sydney'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh','Melbourne'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh','Melbourne'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal','Sydney'),
        ('RF7bh7b3avrzStoTE8ho','Kajal','Sydney'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad','Sydney'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad','Sydney'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab','Sydney'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab','Sydney'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur','Melbourne'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur','Melbourne')
    ) AS t(calendar_id, counsellor_name, counsellor_city)
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
    SELECT contact_id, submitted_at, campaign, utm_content, form_name, event_form_name,
           session_source, event_source, survey_name,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
website_contacts AS (
    SELECT contact_id, submitted_at, campaign, utm_content, form_name, event_form_name,
           session_source, event_source, survey_name
    FROM latest_sub WHERE rn=1
),
cohort AS (   -- canonical website_form, created OR revived (matches Forecast)
    SELECT c.contact_id, c.email, c.contact_name, c.date_added, c.assigned_user_id,
           c.latest_attribution_source,
           wc.campaign, wc.utm_content, wc.form_name, wc.event_form_name,
           wc.session_source, wc.event_source, wc.survey_name
    FROM fact_contacts c
    LEFT JOIN website_contacts wc ON wc.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source,'')) IN
          ('website_form','organic_seo','organic search','organic','seo')
      AND (CAST(c.date_added   + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
           OR CAST(wc.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
latest_opp AS (
    SELECT contact_id, opportunity_id, pipeline_id, stage_id, status, assigned_user_id,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
    WHERE contact_id IN (SELECT contact_id FROM cohort)
),
latest_appt AS (
    SELECT contact_id, calendar_id, appointment_status, canonical_outcome,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IN (SELECT contact_id FROM cohort)
      AND LOWER(appointment_status) <> 'invalid'
)
SELECT
    c.contact_id,
    c.email,
    c.contact_name,
    c.date_added,
    -- Latest opportunity
    p.pipeline_name                                                                AS pipeline,
    s.stage_name                                                                   AS stage,
    lo.status                                                                      AS opp_status,
    -- Owner: opp's assigned_user_id, fall back to contact's
    COALESCE(u_opp.full_name, u_con.full_name, '')                                 AS owner,
    -- Latest appointment status (if any)
    la.appointment_status                                                          AS appointment_status,
    CASE WHEN la.contact_id IS NOT NULL THEN 1 ELSE 0 END                          AS has_booking,
    CASE WHEN LOWER(la.canonical_outcome) = 'show' THEN 1 ELSE 0 END               AS showed,
    -- Counsellor city via the latest appointment's calendar
    COALESCE(cal.counsellor_city, 'Unassigned')                                 AS city_group,
    cal.counsellor_name                                                            AS counsellor,
    -- Latest Source — computed LIVE (same 8-step precedence as Counsellors tab)
    COALESCE(
        NULLIF(CASE WHEN COALESCE(c.campaign,'') <> '' AND COALESCE(c.utm_content,'') <> ''
                         THEN c.campaign || ' -- ' || c.utm_content
                    WHEN COALESCE(c.campaign,'') <> ''         THEN c.campaign
                    WHEN COALESCE(c.form_name,'') <> ''        THEN c.form_name
                    WHEN COALESCE(c.survey_name,'') <> ''      THEN c.survey_name
                    WHEN COALESCE(c.event_form_name,'') <> ''  THEN c.event_form_name
                    WHEN COALESCE(c.session_source,'') <> ''   THEN c.session_source
                    WHEN COALESCE(c.event_source,'') <> ''     THEN c.event_source
                    ELSE NULL END, ''),
        cal.counsellor_name,
        c.latest_attribution_source,
        ''
    )                                                                              AS latest_source
FROM cohort c
LEFT JOIN latest_opp lo  ON lo.contact_id = c.contact_id AND lo.rn = 1
LEFT JOIN dim_pipelines p ON p.pipeline_id = lo.pipeline_id
LEFT JOIN dim_stages   s  ON s.stage_id    = lo.stage_id
LEFT JOIN dim_users u_opp ON u_opp.user_id = lo.assigned_user_id
LEFT JOIN dim_users u_con ON u_con.user_id = c.assigned_user_id
LEFT JOIN latest_appt la  ON la.contact_id = c.contact_id AND la.rn = 1
LEFT JOIN calendars   cal ON cal.calendar_id = la.calendar_id;


CREATE OR REPLACE VIEW vw_seo_lead_activity_breakdown AS
WITH calendars AS (
    SELECT * FROM (VALUES
        ('aTMcDOwcpe5TOohPT1Rz','Turab','Sydney'),
        ('uwCBo7Y0cAWLs6ZqPjJI','Turab','Sydney'),
        ('Zyrz08TZ6BaAruWxERy5','Nasir Nawaz','Sydney'),
        ('gttsLvMBPKFfslnOuwHT','Nasir Nawaz','Sydney'),
        ('hsVntQS9KwIw8eF4D8ef','Gurbir Singh','Melbourne'),
        ('o4AfsJ45rEkewmENut12','Gurbir Singh','Melbourne'),
        ('1FgpIJPxw6RWveeJLsb8','Kajal','Sydney'),
        ('RF7bh7b3avrzStoTE8ho','Kajal','Sydney'),
        ('4HLkV0BSHX7EvJ3jniC9','Wajahad','Sydney'),
        ('hsCSqcYHrXwL55NffEFi','Wajahad','Sydney'),
        ('4mKKf1IPwIq50N4OzOTI','Saurab','Sydney'),
        ('vjmOhJPIT4pAPzCyCmdT','Saurab','Sydney'),
        ('XJS0nt92447DgYSmxVkP','Navneet Kaur','Melbourne'),
        ('hkL937P7e6XTzy58dOZ7','Navneet Kaur','Melbourne')
    ) AS t(calendar_id, counsellor_name, counsellor_city)
),
all_subs AS (
    SELECT contact_id, submitted_at, event_source, campaign, utm_content, form_name,
           event_form_name, session_source, NULL AS survey_name, page_url, referrer
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, event_source, campaign, utm_content, NULL AS form_name,
           NULL AS event_form_name, session_source, survey_name, page_url, referrer
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, submitted_at, event_source, campaign, utm_content, form_name, event_form_name,
           session_source, survey_name, page_url, referrer,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
website_contacts AS (
    SELECT contact_id, submitted_at, campaign, utm_content, form_name, event_form_name,
           session_source, event_source, survey_name, page_url, referrer
    FROM latest_sub WHERE rn=1
),
cohort AS (   -- canonical website_form, created OR revived (matches Forecast)
    SELECT c.contact_id, wc.survey_name, wc.page_url, wc.referrer, c.date_added,
           wc.campaign, wc.utm_content, wc.form_name, wc.event_form_name,
           wc.session_source, wc.event_source, c.latest_attribution_source
    FROM fact_contacts c
    LEFT JOIN website_contacts wc ON wc.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source,'')) IN
          ('website_form','organic_seo','organic search','organic','seo')
      AND (CAST(c.date_added   + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
           OR CAST(wc.submitted_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until)
),
latest_appt AS (
    SELECT contact_id, calendar_id, canonical_outcome,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IN (SELECT contact_id FROM cohort)
      AND LOWER(appointment_status) <> 'invalid'
)
SELECT c.contact_id, c.date_added, c.survey_name, c.page_url, c.referrer,
       COALESCE(cc.counsellor_city, 'Unassigned') AS city_group,
       cc.counsellor_name,
       CASE WHEN la.contact_id IS NOT NULL THEN 1 ELSE 0 END AS has_booking,
       CASE WHEN LOWER(la.canonical_outcome)='show' THEN 1 ELSE 0 END AS showed,
       -- Latest Source — same live precedence as elsewhere
       COALESCE(
           NULLIF(CASE WHEN COALESCE(c.campaign,'') <> '' AND COALESCE(c.utm_content,'') <> ''
                            THEN c.campaign || ' -- ' || c.utm_content
                       WHEN COALESCE(c.campaign,'') <> ''         THEN c.campaign
                       WHEN COALESCE(c.form_name,'') <> ''        THEN c.form_name
                       WHEN COALESCE(c.survey_name,'') <> ''      THEN c.survey_name
                       WHEN COALESCE(c.event_form_name,'') <> ''  THEN c.event_form_name
                       WHEN COALESCE(c.session_source,'') <> ''   THEN c.session_source
                       WHEN COALESCE(c.event_source,'') <> ''     THEN c.event_source
                       ELSE NULL END, ''),
           cc.counsellor_name,
           c.latest_attribution_source,
           ''
       ) AS latest_source
FROM cohort c
LEFT JOIN latest_appt la ON la.contact_id = c.contact_id AND la.rn = 1
LEFT JOIN calendars   cc ON cc.calendar_id = la.calendar_id;


-- Legacy showed view kept for back-compat.
CREATE OR REPLACE VIEW vw_seo_website_showed_per_city AS
SELECT city_group, showed, noshow AS noshowed, bookings AS appts
FROM vw_seo_website_leads_per_city;


-- Top landing pages — joins form-submission page paths to GA4 page views in
-- the same window. Conversion rate = form_fills / page_views (per-page).
CREATE OR REPLACE VIEW vw_seo_top_pages AS
WITH form_fills AS (
    SELECT
        LOWER(REGEXP_REPLACE(COALESCE(page_path,''), '/$', '')) AS pp,
        COUNT(*)                       AS form_fills,
        COUNT(DISTINCT contact_id)     AS form_contacts
    FROM fact_form_submissions
    WHERE COALESCE(page_path,'') <> ''
      AND CAST(submitted_at AS DATE) BETWEEN $since AND $until
    GROUP BY 1
),
ga4_pages AS (
    SELECT
        LOWER(REGEXP_REPLACE(COALESCE(page_path,''), '/$', '')) AS pp,
        SUM(page_views)   AS page_views,
        SUM(active_users) AS active_users
    FROM fact_ga4_pages
    WHERE date BETWEEN $since AND $until
    GROUP BY 1
)
SELECT
    COALESCE(f.pp, g.pp)                              AS page_path,
    COALESCE(g.page_views, 0)                         AS page_views,
    COALESCE(g.active_users, 0)                       AS active_users,
    COALESCE(f.form_fills, 0)                         AS form_fills,
    COALESCE(f.form_contacts, 0)                      AS form_contacts,
    CASE WHEN COALESCE(g.active_users, 0) > 0
         THEN CAST(COALESCE(f.form_fills, 0) AS DOUBLE) / g.active_users
         ELSE NULL END                                AS conv_rate
FROM form_fills f
FULL OUTER JOIN ga4_pages g ON g.pp = f.pp
WHERE COALESCE(f.pp, g.pp) NOT IN ('', '/')
ORDER BY page_views DESC, form_fills DESC;


-- Top search queries — already aggregable from fact_gsc_queries, formalised
-- as a view for the SEO drill-down.
CREATE OR REPLACE VIEW vw_seo_top_queries AS
SELECT
    dimension_value                                   AS query,
    SUM(clicks)                                       AS clicks,
    SUM(impressions)                                  AS impressions,
    CASE WHEN SUM(impressions) > 0
         THEN SUM(position * impressions) / SUM(impressions)
         ELSE NULL END                                AS avg_position
FROM fact_gsc_queries
WHERE dimension_name = 'query'
  AND date BETWEEN $since AND $until
GROUP BY 1
ORDER BY clicks DESC, impressions DESC;


-- GSC top landing pages (search-result pages, distinct from GA4 page-views).
CREATE OR REPLACE VIEW vw_seo_top_pages_gsc AS
SELECT
    dimension_value                                   AS page,
    SUM(clicks)                                       AS clicks,
    SUM(impressions)                                  AS impressions,
    CASE WHEN SUM(impressions) > 0
         THEN SUM(position * impressions) / SUM(impressions)
         ELSE NULL END                                AS avg_position
FROM fact_gsc_queries
WHERE dimension_name = 'page'
  AND date BETWEEN $since AND $until
GROUP BY 1
ORDER BY clicks DESC, impressions DESC;


-- GA4 sessions per city (Melbourne / Sydney / Other) per date — used by the
-- SEO city cards so the Sessions metric + daily-trend chart are *city-specific*
-- (the GHL contact.city is sparsely set, so GA4 city is the better signal).
-- Mapping: Melbourne includes 'Victoria'/'VIC'; Sydney includes 'NSW'/'Wales'.
CREATE OR REPLACE VIEW vw_seo_ga4_per_city AS
SELECT
    CASE
        WHEN LOWER(COALESCE(city,'')) LIKE '%melb%'
          OR LOWER(COALESCE(city,'')) LIKE '%victoria%'
          OR LOWER(COALESCE(city,'')) = 'vic'                  THEN 'Melbourne'
        WHEN LOWER(COALESCE(city,'')) LIKE '%sydney%'
          OR LOWER(COALESCE(city,'')) LIKE '%wales%'
          OR LOWER(COALESCE(city,'')) = 'nsw'                  THEN 'Sydney'
        ELSE 'Other'
    END                                                        AS city_group,
    date,
    SUM(sessions)                                              AS sessions,
    SUM(sessions * (1 - COALESCE(bounce_rate, 0)))             AS engaged_sessions,
    SUM(active_users)                                          AS active_users,
    SUM(key_events)                                            AS key_events
FROM fact_ga4_sessions
WHERE date BETWEEN $since AND $until
GROUP BY 1, 2;


-- Daily GA4 + GSC trend for the SEO area chart.
CREATE OR REPLACE VIEW vw_seo_daily_trend AS
WITH ga4 AS (
    SELECT date,
           SUM(sessions)                                  AS sessions,
           SUM(sessions * (1 - COALESCE(bounce_rate, 0))) AS engaged_sessions
    FROM fact_ga4_sessions
    WHERE date BETWEEN $since AND $until
    GROUP BY 1
),
gsc AS (
    SELECT date,
           SUM(clicks)      AS gsc_clicks,
           SUM(impressions) AS gsc_impressions,
           CASE WHEN SUM(impressions) > 0
                THEN SUM(position * impressions) / SUM(impressions)
                ELSE NULL END                              AS gsc_position
    FROM fact_gsc_queries
    WHERE dimension_name = 'device'
      AND date BETWEEN $since AND $until
    GROUP BY 1
),
events AS (
    SELECT date, SUM(event_count) AS ga4_conv
    FROM fact_ga4_events
    WHERE event_name IN ('contact_us','generate_lead','book_consultation_page','blogs_to_consultation')
      AND date BETWEEN $since AND $until
    GROUP BY 1
)
SELECT
    COALESCE(ga4.date, gsc.date, ev.date)              AS date,
    COALESCE(ga4.sessions, 0)                          AS sessions,
    COALESCE(ga4.engaged_sessions, 0)                  AS engaged_sessions,
    COALESCE(ev.ga4_conv, 0)                           AS ga4_conv,
    COALESCE(gsc.gsc_clicks, 0)                        AS gsc_clicks,
    COALESCE(gsc.gsc_impressions, 0)                   AS gsc_impressions,
    gsc.gsc_position                                   AS gsc_position
FROM ga4
FULL OUTER JOIN gsc ON gsc.date = ga4.date
FULL OUTER JOIN events ev ON ev.date = COALESCE(ga4.date, gsc.date)
ORDER BY 1;


-- =====================================================================
-- ============  GHL ⇄ META LEAD ALIGNMENT (audit-ratified)  ===========
-- =====================================================================
-- Problem: counting GHL leads by contact "created_at" undercounts vs Meta,
-- because (a) returning leads re-submit a form against an OLD opportunity and
-- (b) some new contacts never get an opportunity. The May 2026 audit showed
-- adding those two buckets brings GHL to ~97% of Meta's lead count.
--
-- Aligned GHL leads (Paid Social / Social-media-attributed contacts) =
--   A  opportunities CREATED in range
-- + B  opportunities created BEFORE range whose contact FILLED A FORM in range
--      (the "old opp came again" bucket — only valid because a real form-fill
--       event exists in fact_form_submissions; a page visit is NOT counted)
-- + C  contacts CREATED in range that have NO opportunity at all
-- A / B / C are mutually exclusive (opp-in-range / opp-before-range / no-opp),
-- so the three sum without double-counting. City filter is on contact.city.
-- Returns tag='current' and tag='prior' so the Exec card can show a delta.
-- =====================================================================
CREATE OR REPLACE VIEW vw_aligned_ghl_leads AS
WITH windows AS (
    SELECT 'current' AS tag, CAST($since AS DATE) AS w_since, CAST($until AS DATE) AS w_until
    UNION ALL
    SELECT 'prior', CAST($prior_since AS DATE), CAST($prior_until AS DATE)
),
social_contacts AS (
    SELECT contact_id, date_added, city
    FROM fact_contacts
    WHERE LOWER(COALESCE(latest_attribution_source,'')) IN ('paid social','social media')
),
a AS (   -- opportunities created in window
    SELECT w.tag, COUNT(*) AS opps_in_range
    FROM windows w
    JOIN fact_opportunities o ON CAST(o.created_at AS DATE) BETWEEN w.w_since AND w.w_until
    JOIN social_contacts c ON c.contact_id = o.contact_id
    WHERE ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
    GROUP BY w.tag
),
b AS (   -- old opps (created before window) whose contact re-filled a form in window
    SELECT w.tag, COUNT(*) AS returning_opps
    FROM windows w
    JOIN fact_opportunities o ON CAST(o.created_at AS DATE) < w.w_since
    JOIN social_contacts c ON c.contact_id = o.contact_id
    WHERE EXISTS (
            SELECT 1 FROM fact_form_submissions f
            WHERE f.contact_id = o.contact_id
              AND CAST(f.submitted_at AS DATE) BETWEEN w.w_since AND w.w_until)
      AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
    GROUP BY w.tag
),
c_ AS (  -- contacts created in window with NO opportunity
    SELECT w.tag, COUNT(*) AS contacts_no_opp
    FROM windows w
    JOIN social_contacts c ON CAST(c.date_added AS DATE) BETWEEN w.w_since AND w.w_until
    WHERE NOT EXISTS (SELECT 1 FROM fact_opportunities o WHERE o.contact_id = c.contact_id)
      AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
    GROUP BY w.tag
)
SELECT
    w.tag,
    COALESCE(a.opps_in_range, 0)    AS opps_in_range,
    COALESCE(b.returning_opps, 0)   AS returning_opps,
    COALESCE(c_.contacts_no_opp, 0) AS contacts_no_opp,
    COALESCE(a.opps_in_range, 0)
      + COALESCE(b.returning_opps, 0)
      + COALESCE(c_.contacts_no_opp, 0) AS aligned_leads
FROM windows w
LEFT JOIN a  ON a.tag  = w.tag
LEFT JOIN b  ON b.tag  = w.tag
LEFT JOIN c_ ON c_.tag = w.tag;


-- =====================================================================
-- ============  META ADS TAB — new clickable-scorecard views  =========
-- =====================================================================
-- All accept $since, $until. Some accept $account ('All'|'Melbourne'|'Sydney').
--
-- GHL Leads per campaign — audit-ratified counting:
--   Take all opportunities (any pipeline) where:
--     (a) opp.created_at IN window, OR
--     (b) contact filled a form IN window (regardless of opp.created_at — i.e.
--         a returning lead, the campaign is working for the previous audience too).
--   De-dup by opportunity_id. Group by contact's Latest Source campaign.
--   Bookings = subset whose pipeline+stage is in the booking_stages set:
--     L2C - Education: Appointment Booked, Post Cons., No Show, Initial Req.,
--                       Initial Received, COE Received
--     L2C - VISA:      all stages EXCEPT 'High Potential Clients'
--     CLT - Onshore Admission: all stages
-- =====================================================================
CREATE OR REPLACE VIEW vw_meta_ghl_leads_per_campaign AS
WITH booking_stages AS (
    SELECT s.stage_id
    FROM dim_stages s JOIN dim_pipelines p ON p.pipeline_id = s.pipeline_id
    WHERE (p.pipeline_name = 'L2C - Education'
           AND s.stage_name IN ('Appointment Booked','Post Consultation','No Show',
                                'Initial Requested','Initial Received','COE Received'))
       OR (p.pipeline_name = 'L2C - VISA' AND s.stage_name <> 'High Potential Clients')
       OR (p.pipeline_name = 'CLT - Onshore Admission')
),
-- GHL Leads = distinct Meta GHL CONTACTS (canonical_source = meta_paid) who were
-- CREATED or REVIVED in the window:
--   created = fact_contacts.date_added in window (AEST +10h)
--   revived = an OLD contact whose latest form/survey submission is in window
-- Counted per campaign via the contact's stored Latest Source campaign. This is
-- the shared contact-grain definition (matches the Forecast tab) and INCLUDES
-- revived leads. Bookings / status are from each contact's latest opportunity.
subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
cohort AS (
    SELECT c.contact_id,
           COALESCE(NULLIF(cls.latest_source_campaign, ''), '(no campaign)') AS campaign
    FROM fact_contacts c
    LEFT JOIN latest_sub ls                  ON ls.contact_id  = c.contact_id
    LEFT JOIN fact_contact_latest_source cls ON cls.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source, '')) IN ('meta_paid','paid_social','paid social')
      AND ( CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
            OR CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until )
),
latest_opp_per_contact AS (
    SELECT contact_id, status, stage_id,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
    FROM fact_opportunities
    WHERE contact_id IN (SELECT contact_id FROM cohort)
)
SELECT
    co.campaign,
    COUNT(DISTINCT co.contact_id)                                            AS ghl_leads,
    COUNT(DISTINCT co.contact_id)                                            AS ghl_contacts,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE lo.stage_id IN (SELECT stage_id FROM booking_stages)) AS bookings,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE LOWER(lo.status) = 'open')      AS open_count,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE LOWER(lo.status) = 'won')       AS won_count,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE LOWER(lo.status) = 'lost')      AS lost_count,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE LOWER(lo.status) = 'abandoned') AS abandoned_count
FROM cohort co
LEFT JOIN latest_opp_per_contact lo ON lo.contact_id = co.contact_id AND lo.rn = 1
GROUP BY 1;


-- Meta per-campaign metrics (account-filterable). Used to drive the Campaign
-- Performance table and the per-scorecard detail panels.
CREATE OR REPLACE VIEW vw_meta_per_campaign AS
SELECT
    campaign_name,
    MAX(account_label)              AS account_label,
    SUM(spend)                      AS spend,
    -- Real lead-form submissions only (pixel events excluded)
    COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) AS meta_leads,
    SUM(impressions)                AS impressions,
    SUM(clicks)                     AS clicks,
    CASE WHEN SUM(impressions) > 0
         THEN CAST(SUM(clicks) AS DOUBLE) / SUM(impressions) END AS ctr,
    CASE WHEN COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) > 0
         THEN SUM(spend) / SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')) END  AS cpl
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until
  AND ($account = 'All'
       OR ($account = 'Melbourne' AND account_label = 'Melbourne')
       OR ($account = 'Sydney'    AND account_label = 'Sydney'))
GROUP BY 1
-- Only include campaigns that actually ran (had impressions in window).
-- Matches the "Impressions > 0" filter used in Meta Ads Manager UI so the
-- campaign list on the dashboard mirrors what's active in the ad account.
HAVING SUM(impressions) > 0;


-- Daily Meta trend per account — drives the 14-day chart on Mel/Syd cards.
CREATE OR REPLACE VIEW vw_meta_daily_trend AS
SELECT
    date, account_label,
    SUM(spend)        AS spend,
    -- Real lead-form submissions only (pixel events excluded)
    COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) AS leads,
    SUM(impressions)  AS impressions,
    SUM(clicks)       AS clicks,
    CASE WHEN SUM(impressions) > 0
         THEN CAST(SUM(clicks) AS DOUBLE) / SUM(impressions) END AS ctr,
    CASE WHEN COALESCE(SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')), 0) > 0
         THEN SUM(spend) / SUM(result_count) FILTER (WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')) END  AS cpl
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until
GROUP BY 1, 2
ORDER BY 1;


-- Lead Source breakdown (by opportunity.source first segment) — opps + contacts.
CREATE OR REPLACE VIEW vw_meta_lead_source_breakdown AS
SELECT
    COALESCE(NULLIF(TRIM(SPLIT_PART(o.source, ' -- ', 1)), ''), '(no source)') AS lead_source,
    COUNT(DISTINCT o.opportunity_id) AS opportunities,
    COUNT(DISTINCT o.contact_id)     AS contacts
FROM fact_opportunities o
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
GROUP BY 1
ORDER BY opportunities DESC;


-- Latest Attribution breakdown (high-level channel like 'Paid Social', 'Organic').
CREATE OR REPLACE VIEW vw_meta_latest_attribution AS
SELECT
    COALESCE(c.latest_attribution_source, '(none)') AS latest_attribution,
    COUNT(*)                                        AS opps
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) BETWEEN $since AND $until
GROUP BY 1
ORDER BY opps DESC;


-- Bookings/Showed appointments per campaign — pairs an appt's contact to their
-- Latest Source campaign. Used by the Showed scorecard.
CREATE OR REPLACE VIEW vw_meta_showed_per_campaign AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
-- Same created-or-revived Meta cohort as GHL Leads (Showed is "of those leads").
cohort AS (
    SELECT c.contact_id,
           COALESCE(NULLIF(cls.latest_source_campaign, ''), '(no latest source)') AS campaign
    FROM fact_contacts c
    LEFT JOIN latest_sub ls                  ON ls.contact_id  = c.contact_id
    LEFT JOIN fact_contact_latest_source cls ON cls.contact_id = c.contact_id
    WHERE LOWER(COALESCE(c.canonical_source, '')) IN ('meta_paid','paid_social','paid social')
      AND ( CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
            OR CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until )
),
appt AS (
    SELECT contact_id,
           MAX(CASE WHEN LOWER(canonical_outcome) = 'show'   THEN 1 ELSE 0 END) AS showed,
           MAX(CASE WHEN LOWER(canonical_outcome) = 'noshow' THEN 1 ELSE 0 END) AS noshowed,
           MAX(1) AS has_appt
    FROM fact_appointments
    WHERE LOWER(appointment_status) <> 'invalid' AND contact_id IS NOT NULL
    GROUP BY contact_id
)
SELECT
    co.campaign,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE a.showed   = 1) AS showed,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE a.noshowed = 1) AS noshowed,
    COUNT(DISTINCT co.contact_id) FILTER (WHERE a.has_appt = 1) AS appts
FROM cohort co
LEFT JOIN appt a ON a.contact_id = co.contact_id
GROUP BY 1;


-- ---- Returning-lead detail: old opportunity source vs new (re-filled) source ----
-- One row per returning contact: their old opportunity campaign(s) and the
-- campaign of the form they re-submitted inside the range. Drives the
-- "old source -> new source" table the Marketing Lead asked for.
CREATE OR REPLACE VIEW vw_drill_returning_leads AS
SELECT
    c.email                                                                       AS email,
    MIN(CAST(o.created_at AS DATE))                                               AS old_opp_created,
    STRING_AGG(DISTINCT TRIM(SPLIT_PART(COALESCE(NULLIF(TRIM(o.source),''),'(none)'), ' -- ', 1)), ' ; ') AS old_source,
    MAX(CAST(f.submitted_at AS DATE))                                             AS new_form_date,
    arg_max(CASE WHEN COALESCE(f.utm_content,'') <> ''
                 THEN f.campaign || ' -- ' || f.utm_content ELSE f.campaign END,
            f.submitted_at)                                                       AS new_source
FROM fact_opportunities o
JOIN fact_contacts c ON c.contact_id = o.contact_id
JOIN fact_form_submissions f ON f.contact_id = o.contact_id
WHERE CAST(o.created_at AS DATE) < $since
  AND CAST(f.submitted_at AS DATE) BETWEEN $since AND $until
  AND LOWER(COALESCE(c.latest_attribution_source,'')) IN ('paid social','social media')
  AND ($city = 'All'
       OR ($city = 'Melbourne'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw'))
       OR ($city = 'Sydney'
           AND (LOWER(COALESCE(c.city,'')) LIKE '%sydney%' OR LOWER(COALESCE(c.city,'')) LIKE '%wales%' OR LOWER(COALESCE(c.city,'')) = 'nsw')
           AND NOT (LOWER(COALESCE(c.city,'')) LIKE '%melb%' OR LOWER(COALESCE(c.city,'')) LIKE '%victoria%' OR LOWER(COALESCE(c.city,'')) = 'vic'))
       OR ($city = 'Others' AND COALESCE(c.city,'') <> ''
           AND NOT ((LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')
                    AND NOT (LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw'))
           AND NOT ((LOWER(c.city) LIKE '%sydney%' OR LOWER(c.city) LIKE '%wales%' OR LOWER(c.city) = 'nsw')
                    AND NOT (LOWER(c.city) LIKE '%melb%' OR LOWER(c.city) LIKE '%victoria%' OR LOWER(c.city) = 'vic')))
       OR ($city = 'Unidentified' AND COALESCE(c.city,'') = ''))
GROUP BY c.email
ORDER BY new_form_date;


-- =====================================================================
-- LEAD JOURNEY PERFORMANCE ANALYSIS  (Upload Reports tab)
-- ---------------------------------------------------------------------
-- Cohort = every contact CREATED or REVIVED (latest form/survey fill) in
-- [$since, $until] — the same "total leads" definition used elsewhere.
--
-- DATA NOTE: GHL's sync stores only each opportunity's created_at +
-- updated_at (days_in_pipeline is always 0 — there is NO per-stage change
-- history). So true "days spent in each stage" cannot be derived. The cells
-- expose the honest proxy `days_to_current = updated_at - created_at` at the
-- opp's furthest stage, plus REAL milestone days (lead -> appointment ->
-- show) which DO have their own timestamps.
-- =====================================================================

-- One row per cohort contact = the "total leads" universe.
CREATE OR REPLACE VIEW vw_journey_leads AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
cohort AS (
    SELECT c.contact_id, c.email, c.contact_name,
           CAST(c.date_added + INTERVAL 10 HOUR AS DATE) AS created_date,
           CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) AS revived_date
    FROM fact_contacts c
    LEFT JOIN latest_sub ls ON ls.contact_id = c.contact_id
    WHERE CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
       OR CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
cur_opp AS (
    SELECT o.contact_id, p.pipeline_name, st.stage_name, o.status,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id
                              ORDER BY o.updated_at DESC, o.created_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
),
appt AS (
    SELECT contact_id,
           MIN(date_added) AS first_appt_booked,
           MIN(CASE WHEN LOWER(canonical_outcome) = 'show' THEN start_time END) AS first_show
    FROM fact_appointments
    WHERE LOWER(COALESCE(appointment_status,'')) <> 'invalid'
    GROUP BY contact_id
),
pay AS (
    SELECT contact_id, SUM(amount - COALESCE(amount_refunded, 0)) AS total_payment,
           MIN(created_at) AS first_payment
    FROM fact_payments WHERE LOWER(status) = 'succeeded' GROUP BY contact_id
)
SELECT
    ch.contact_id,
    ch.email,
    ch.contact_name,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
         ELSE ch.revived_date END                                      AS lead_date,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN 1 ELSE 0 END AS is_created,
    CASE WHEN ch.revived_date BETWEEN $since AND $until
              AND (ch.created_date IS NULL OR ch.created_date < $since) THEN 1 ELSE 0 END AS is_revived,
    COALESCE(co.pipeline_name, '(no opportunity)')                     AS cur_pipeline,
    COALESCE(co.stage_name, '-')                                       AS cur_stage,
    COALESCE(co.status, '-')                                           AS cur_status,
    COALESCE(pa.total_payment, 0)                                      AS total_payment,
    DATE_DIFF('day',
        CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
             ELSE ch.revived_date END, CURRENT_DATE)                   AS age_days,
    DATE_DIFF('day',
        CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
             ELSE ch.revived_date END,
        CAST(ap.first_appt_booked + INTERVAL 10 HOUR AS DATE))         AS days_lead_to_appt,
    DATE_DIFF('day',
        CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
             ELSE ch.revived_date END,
        CAST(ap.first_show + INTERVAL 10 HOUR AS DATE))                AS days_lead_to_show,
    DATE_DIFF('day',
        CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
             ELSE ch.revived_date END,
        CAST(pa.first_payment + INTERVAL 10 HOUR AS DATE))             AS days_lead_to_pay
FROM cohort ch
LEFT JOIN cur_opp co ON co.contact_id = ch.contact_id AND co.rn = 1
LEFT JOIN appt    ap ON ap.contact_id = ch.contact_id
LEFT JOIN pay     pa ON pa.contact_id = ch.contact_id;


-- One row per (cohort contact, target pipeline) = furthest stage reached in
-- that pipeline + days_to_current proxy. Pivoted into the stage matrix in app.py.
CREATE OR REPLACE VIEW vw_journey_stage_cells AS
WITH subs AS (
    SELECT contact_id, submitted_at FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM subs GROUP BY contact_id),
cohort AS (
    SELECT c.contact_id
    FROM fact_contacts c
    LEFT JOIN latest_sub ls ON ls.contact_id = c.contact_id
    WHERE CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
       OR CAST(ls.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
),
ranked AS (
    SELECT o.contact_id, p.pipeline_name, st.stage_name, st.stage_order,
           o.created_at, o.updated_at,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id, p.pipeline_name
                              ORDER BY st.stage_order DESC, o.updated_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
         AND p.pipeline_name IN ('L2C - Education', 'L2C - VISA',
                                 'CLT - Onshore Admission', 'CLT - Admissions Sub-Applications')
    JOIN dim_stages st ON st.stage_id = o.stage_id
    WHERE o.contact_id IN (SELECT contact_id FROM cohort)
)
SELECT contact_id, pipeline_name, stage_name, stage_order,
       DATE_DIFF('day', created_at, updated_at)   AS days_to_current,
       DATE_DIFF('day', updated_at, CURRENT_DATE) AS days_in_stage,
       CAST(created_at AS DATE) AS opp_created,
       CAST(updated_at AS DATE) AS opp_updated
FROM ranked
WHERE rn = 1;


-- =====================================================================
-- EXECUTIVE SCORECARD EXTRAS
-- =====================================================================

-- COE Received (special definition): distinct contacts who reached
--   L2C - Education            -> 'Initial Received' or 'COE Received', OR
--   CLT - Onshore Admission    -> 'Requested for COE' or 'COE Received'
-- anchored on the opportunity's LAST stage-change date (updated_at) in window.
-- City = derived from the opportunity's assigned counsellor
-- (Gurbir/Navneet -> Melbourne, any other named owner -> Sydney, unassigned -> Others).
-- One city per contact = their LATEST (rn=1) COE-stage opp, so the city slices
-- sum back to the All total (a contact is never double-counted across cities).
CREATE OR REPLACE VIEW vw_exec_coe_stage AS
WITH ranked AS (
    SELECT o.contact_id,
           CASE WHEN u.full_name LIKE '%Gurbir%' OR u.full_name LIKE '%Navneet%' THEN 'Melbourne'
                WHEN u.full_name IS NULL OR u.full_name = '' THEN 'Unassigned'
                ELSE 'Sydney' END AS city,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.updated_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   s ON s.stage_id   = o.stage_id
    LEFT JOIN dim_users u ON u.user_id = o.assigned_user_id
    WHERE ((p.pipeline_name = 'L2C - Education'         AND s.stage_name IN ('Initial Received', 'COE Received'))
        OR (p.pipeline_name = 'CLT - Onshore Admission' AND s.stage_name IN ('Requested for COE', 'COE Received')))
      AND CAST(o.updated_at AS DATE) BETWEEN $since AND $until
)
SELECT COUNT(*) AS coes
FROM ranked
WHERE rn = 1
  AND ($city = 'All'
       OR ($city = 'Melbourne' AND city = 'Melbourne')
       OR ($city = 'Sydney'    AND city = 'Sydney')
       OR ($city IN ('Others', 'Unidentified') AND city = 'Unassigned'));


-- Per-contact succeeded GHL payment total in window (for the Meta-ROAS multiple).
CREATE OR REPLACE VIEW vw_exec_contact_payments AS
SELECT contact_id, SUM(amount - COALESCE(amount_refunded, 0)) AS paid
FROM fact_payments
WHERE LOWER(status) = 'succeeded'
  AND CAST(created_at AS DATE) BETWEEN $since AND $until
GROUP BY contact_id;


-- Per-contact latest form-fill UTM (parsed from page_url + utm fields).
-- Drives the Total-Leads drill-down's utm_source / utm_medium / utm_content
-- / utm_campaign tables.
CREATE OR REPLACE VIEW vw_exec_latest_utm AS
WITH ranked AS (
    SELECT contact_id, page_url, campaign, utm_content, session_source, event_source,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
)
SELECT contact_id,
    NULLIF(replace(replace(regexp_extract(page_url, 'utm_source=([^&]+)', 1), '+', ' '), '%2B', '+'), '') AS utm_source,
    NULLIF(replace(replace(regexp_extract(page_url, 'utm_medium=([^&]+)', 1), '+', ' '), '%2B', '+'), '') AS utm_medium,
    COALESCE(NULLIF(campaign, ''),
             NULLIF(replace(regexp_extract(page_url, 'utm_campaign=([^&]+)', 1), '+', ' '), '')) AS utm_campaign,
    COALESCE(NULLIF(utm_content, ''),
             NULLIF(replace(regexp_extract(page_url, 'utm_content=([^&]+)', 1), '+', ' '), '')) AS utm_content,
    NULLIF(session_source, '') AS session_source,
    NULLIF(event_source, '')   AS event_source
FROM ranked WHERE rn = 1;


-- COE Received drill-down: one row per contact who reached a COE stage in the
-- window, with assigned counsellor, derived city, latest source, and the date
-- it moved to that stage (= opp updated_at). The app filters by city in pandas.
CREATE OR REPLACE VIEW vw_exec_coe_detail AS
WITH ranked AS (
    SELECT o.contact_id, c.email,
           COALESCE(u.full_name, '(unassigned)') AS counsellor,
           CASE WHEN u.full_name LIKE '%Gurbir%' OR u.full_name LIKE '%Navneet%' THEN 'Melbourne'
                WHEN u.full_name IS NULL OR u.full_name = '' THEN 'Unassigned'
                ELSE 'Sydney' END AS city,
           COALESCE(NULLIF(c.latest_attribution_source, ''),
                    NULLIF(c.first_attribution_source, ''), '-') AS latest_source,
           p.pipeline_name, s.stage_name,
           CAST(o.updated_at AS DATE) AS moved_date,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.updated_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   s ON s.stage_id   = o.stage_id
    JOIN fact_contacts c ON c.contact_id = o.contact_id
    LEFT JOIN dim_users u ON u.user_id = o.assigned_user_id
    WHERE ((p.pipeline_name = 'L2C - Education'         AND s.stage_name IN ('Initial Received', 'COE Received'))
        OR (p.pipeline_name = 'CLT - Onshore Admission' AND s.stage_name IN ('Requested for COE', 'COE Received')))
      AND CAST(o.updated_at AS DATE) BETWEEN $since AND $until
)
SELECT contact_id, email, counsellor, city, latest_source, pipeline_name, stage_name, moved_date
FROM ranked WHERE rn = 1;


-- Revenue (succeeded GHL payments in window) split by the paying contact's
-- LATEST form-fill source. Drives the Revenue drill-down + the Meta ROAS.
CREATE OR REPLACE VIEW vw_exec_revenue_by_source AS
WITH pay AS (
    SELECT contact_id, SUM(amount - COALESCE(amount_refunded, 0)) AS paid
    FROM fact_payments
    WHERE LOWER(status) = 'succeeded'
      AND CAST(created_at AS DATE) BETWEEN $since AND $until
    GROUP BY contact_id
),
lf AS (
    SELECT contact_id, campaign, event_source, session_source,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
)
SELECT
    CASE
        WHEN COALESCE(lf.campaign,'') <> '' OR LOWER(COALESCE(lf.session_source,'')) = 'paid social'
             OR LOWER(COALESCE(lf.event_source,'')) = 'paid social'
             OR c.canonical_source = 'meta_paid'                                      THEN 'Meta Paid'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'organic search'
             OR c.canonical_source IN ('organic_seo', 'website_form')                 THEN 'Organic / SEO'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'referral'
             OR LOWER(COALESCE(c.first_attribution_source,'')) = 'referral'           THEN 'Referral'
        WHEN LOWER(COALESCE(lf.event_source,'')) IN ('direct', 'direct traffic')      THEN 'Direct'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'social media'                     THEN 'Social Media'
        ELSE 'Other'
    END AS source,
    COUNT(*)        AS contacts,
    SUM(pay.paid)   AS revenue
FROM pay
JOIN fact_contacts c ON c.contact_id = pay.contact_id
LEFT JOIN lf ON lf.contact_id = pay.contact_id AND lf.rn = 1
GROUP BY 1;


-- Per-contact messaging CHANNEL from GHL Conversations (Facebook / Instagram /
-- WhatsApp / TikTok / ...). One row per contact = their most "social" channel.
CREATE OR REPLACE VIEW vw_exec_contact_channel AS
WITH ranked AS (
    SELECT contact_id, channel,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY
               CASE channel WHEN 'Facebook' THEN 1 WHEN 'Instagram' THEN 2
                            WHEN 'WhatsApp' THEN 3 WHEN 'TikTok' THEN 4
                            WHEN 'Google Business' THEN 5 ELSE 9 END,
               last_message_at DESC NULLS LAST) AS rn
    FROM fact_conversations WHERE contact_id IS NOT NULL
)
SELECT contact_id, channel FROM ranked WHERE rn = 1;


-- Per-contact PLATFORM / channel. Priority: the form-fill origin (referrer +
-- utm_source + event_source) wins when a form exists; otherwise we fall back to
-- the GHL Conversations channel — which is how Messenger / Instagram / WhatsApp
-- DM leads (no form) get attributed, incl. Facebook Messenger ads.
CREATE OR REPLACE VIEW vw_exec_lead_platform AS
WITH l AS (
    SELECT contact_id, referrer, event_source, page_url,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
),
f AS (
    SELECT contact_id, COALESCE(referrer,'') AS ref, event_source,
           LOWER(regexp_extract(page_url, 'utm_source=([^&]+)', 1)) AS us
    FROM l WHERE rn = 1
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
SELECT c.contact_id,
    CASE
        -- 1) form-fill origin (strongest where present)
        WHEN LOWER(f.ref) LIKE '%instagram%' OR f.us IN ('ig', 'instagram')       THEN 'Instagram'
        WHEN LOWER(f.ref) LIKE '%linkedin%'  OR f.us LIKE '%linkedin%'            THEN 'LinkedIn'
        WHEN LOWER(f.ref) LIKE '%tiktok%'    OR f.us LIKE '%tiktok%'              THEN 'TikTok'
        WHEN LOWER(f.ref) LIKE '%youtube%'   OR f.us LIKE '%youtube%'             THEN 'YouTube'
        WHEN LOWER(f.ref) LIKE '%facebook%'  OR LOWER(f.ref) LIKE '%fb.%'
             OR f.us IN ('fb', 'facebook', 'facebook ad', 'facebook+ad', 'meta', 'an') THEN 'Facebook / Meta'
        WHEN LOWER(f.ref) LIKE '%google%'    OR f.us = 'google'                   THEN 'Google'
        WHEN LOWER(f.ref) LIKE '%bing%'                                           THEN 'Bing'
        WHEN f.us = 'email' OR f.event_source = 'Email Marketing'                 THEN 'Email'
        WHEN f.event_source = 'Paid Social'                                       THEN 'Meta Ads (Paid Social)'
        WHEN f.event_source = 'Organic Search'                                    THEN 'Organic Search'
        WHEN f.event_source = 'Referral'                                          THEN 'Referral'
        WHEN f.event_source IN ('Direct', 'Direct traffic')                       THEN 'Direct'
        WHEN f.event_source = 'Social media'                                      THEN 'Social (other)'
        -- 2) no form -> GHL conversation channel (DM-origin leads, incl. WhatsApp)
        WHEN ch.channel = 'WhatsApp'                                              THEN 'WhatsApp'
        WHEN ch.channel = 'Facebook'                                              THEN 'Facebook / Meta'
        WHEN ch.channel = 'Instagram'                                            THEN 'Instagram'
        WHEN ch.channel = 'TikTok'                                                THEN 'TikTok'
        WHEN ch.channel = 'Google Business'                                       THEN 'Google'
        WHEN ch.channel IN ('Web Chat', 'Email', 'Phone/SMS')                    THEN ch.channel
        ELSE 'Other / Unknown'
    END AS platform
FROM fact_contacts c
LEFT JOIN f    ON f.contact_id  = c.contact_id
LEFT JOIN chan ch ON ch.contact_id = c.contact_id;


-- =====================================================================
-- EXECUTIVE_1 tab — Leads (created OR revived in window) with a REFINED
-- source classification + contact-level detail (pipeline / stage /
-- appointment + calendar / notes). One row per cohort contact.
--   Refined source rules:
--     * form campaign / Paid Social            -> Paid Social
--     * form Organic Search                    -> Organic Search
--     * form Referral + search/LLM utm/referrer-> Organic Search (deep-dive)
--     * form Referral (other)                  -> Referral
--     * form Social media                      -> Social media
--     * no form, FB DM (conversation)          -> Paid Social
--     * no form, IG/WhatsApp/TikTok DM         -> Social media
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec1_lead_detail AS
WITH all_subs AS (
    SELECT contact_id, submitted_at, campaign, event_source, page_url, referrer,
           COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,'')) AS form_name
    FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, event_source, page_url, referrer,
           survey_name AS form_name
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
ls AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
        FROM all_subs
    ) WHERE rn = 1
),
last_sub AS (SELECT contact_id, MAX(submitted_at) AS last_sub FROM all_subs GROUP BY contact_id),
-- contacts whose appointment was BOOKED (date_added) within the window
appt_in_range AS (
    SELECT contact_id, MAX(date_added) AS booked_at
    FROM fact_appointments
    WHERE LOWER(COALESCE(appointment_status,'')) <> 'invalid' AND contact_id IS NOT NULL
      AND CAST(date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
    GROUP BY contact_id
),
cohort AS (
    SELECT c.contact_id, c.email, c.contact_name, c.phone, c.visa_type,
           c.attribution_campaign, c.source AS raw_source,
           c.first_attribution_source, c.latest_attribution_source, c.canonical_source,
           CAST(c.date_added + INTERVAL 10 HOUR AS DATE)  AS created_date,
           CAST(s.last_sub  + INTERVAL 10 HOUR AS DATE)   AS revived_date,
           air.contact_id IS NOT NULL                     AS booked_in_range
    FROM fact_contacts c
    LEFT JOIN last_sub s ON s.contact_id = c.contact_id
    LEFT JOIN appt_in_range air ON air.contact_id = c.contact_id
    -- exclude the Instagram AI auto-responder contact (AI-generated messages)
    -- and our own agency staff accounts (@themigration.com.au) — not leads.
    WHERE LOWER(TRIM(COALESCE(c.contact_name, ''))) NOT IN ('insta user', 'insta ai')
      AND LOWER(COALESCE(c.email, '')) NOT LIKE '%@themigration.com.au'
      AND (CAST(c.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
       OR CAST(s.last_sub  + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
       OR air.contact_id IS NOT NULL)
),
chan AS (
    SELECT contact_id, channel, last_message_type FROM (
        SELECT contact_id, channel, last_message_type,
               ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY
                   CASE channel WHEN 'Facebook' THEN 1 WHEN 'Instagram' THEN 2
                                WHEN 'WhatsApp' THEN 3 WHEN 'TikTok' THEN 4 ELSE 9 END,
                   last_message_at DESC NULLS LAST) AS rn
        FROM fact_conversations WHERE contact_id IS NOT NULL
    ) WHERE rn = 1
),
appt AS (
    SELECT contact_id, calendar_id, appointment_status, canonical_outcome, title, date_added, start_time,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY
               -- prefer an appointment BOOKED within the window, then most recent
               CASE WHEN CAST(date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until THEN 0 ELSE 1 END,
               date_added DESC) AS rn
    FROM fact_appointments
    WHERE LOWER(COALESCE(appointment_status,'')) <> 'invalid' AND contact_id IS NOT NULL
),
lopp AS (
    SELECT o.contact_id, p.pipeline_name, st.stage_name, o.status,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.updated_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
    JOIN dim_stages   st ON st.stage_id   = o.stage_id
),
opp_cnt AS (SELECT contact_id, COUNT(*) AS n_opps FROM fact_opportunities GROUP BY contact_id),
-- Returning client = had an opportunity (a prior service / engagement) BEFORE
-- the current window, then came back. Used to re-label would-be Other/Unknown.
prior_opp AS (
    SELECT DISTINCT contact_id
    FROM fact_opportunities
    WHERE CAST(created_at + INTERVAL 10 HOUR AS DATE) < $since
),
-- contacts who ever made a real (succeeded) payment — used to flag historical
-- Agentcis clients (migrated to GHL Mar 2026) that have no source / form / DM.
paid_ever AS (
    SELECT DISTINCT contact_id FROM fact_payments WHERE LOWER(status) = 'succeeded'
),
-- contacts with ANY walk-in-titled appointment (not just their primary one) so
-- a walk-in is detected even when a later appointment is the "most relevant".
walkin_appt AS (
    SELECT DISTINCT contact_id FROM fact_appointments
    WHERE contact_id IS NOT NULL AND LOWER(COALESCE(appointment_status,'')) <> 'invalid'
      AND (LOWER(COALESCE(title,'')) LIKE '%walk-in%' OR LOWER(COALESCE(title,'')) LIKE '%walk in%')
),
-- normalised keys of every known Meta campaign (ad delivery ran on Meta). A lead
-- whose utm_campaign matches one of these came from a paid Meta campaign even if
-- GHL labelled the form source 'Social media' / 'Direct'. Keys are lowercased,
-- alphanumeric-only — matching the dashboard's campaign-join normalisation.
meta_ck AS (
    SELECT DISTINCT regexp_replace(LOWER(campaign_name), '[^a-z0-9]', '', 'g') AS ck
    FROM (SELECT campaign_name FROM fact_meta_daily
          UNION ALL SELECT campaign_name FROM fact_meta_insights)
    WHERE COALESCE(campaign_name, '') <> ''
),
-- GHL "Lead Source" custom field (Meta Ads / Walk-in / Website Form / Social
-- Media DM / Chatbot / Email Marketing) — an explicit, human/CRM-set source.
clead AS (SELECT contact_id, lead_source FROM fact_contact_lead_source)
-- Organic Search and Direct require an opportunity (pipeline + stage). A contact
-- tagged Organic Search / Direct with no pipeline AND no stage is just untracked
-- web traffic, not a genuine lead, so re-bucket it to 'No Activity' (which is
-- excluded from the Leads count).
SELECT * REPLACE (
    CASE WHEN refined_source IN ('Organic Search', 'Direct')
              AND (pipeline IS NULL OR stage IS NULL)
         THEN 'No Activity' ELSE refined_source END AS refined_source,
    -- Only count an appointment as this lead's Booking/Showed if it was created
    -- ON OR AFTER the lead entered the cohort. An appointment created BEFORE the
    -- lead_date is an OLD appointment (e.g. a contact who booked months ago and
    -- only filled the form / revived now) — not a booking driven by this lead.
    CASE WHEN appt_booked = 1 AND appt_booked_date >= lead_date THEN 1 ELSE 0 END AS appt_booked,
    CASE WHEN appt_showed = 1 AND appt_booked_date >= lead_date THEN 1 ELSE 0 END AS appt_showed
)
FROM (
SELECT
    ch.contact_id, ch.email, ch.contact_name, ch.phone,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN ch.created_date
         WHEN ch.revived_date BETWEEN $since AND $until THEN ch.revived_date
         ELSE CAST(a.date_added + INTERVAL 10 HOUR AS DATE) END             AS lead_date,
    ch.booked_in_range                                                     AS booked_in_range,
    CASE WHEN ch.created_date BETWEEN $since AND $until THEN 1 ELSE 0 END   AS is_created,
    CASE WHEN ch.revived_date BETWEEN $since AND $until
              AND (ch.created_date IS NULL OR ch.created_date < $since) THEN 1 ELSE 0 END AS is_revived,
    ls.event_source                                                        AS form_source,
    NULLIF(LOWER(regexp_extract(ls.page_url, 'utm_source=([^&]+)', 1)), '') AS utm_source,
    cc.channel                                                             AS dm_channel,
    -- which social platform a (social) lead came from — conversation channel
    -- first, then the form referrer / utm_source.
    CASE
        WHEN cc.channel = 'Instagram'                                          THEN 'Instagram'
        WHEN cc.channel = 'WhatsApp'                                           THEN 'WhatsApp'
        WHEN cc.channel = 'TikTok'                                             THEN 'TikTok'
        WHEN cc.channel = 'Facebook'                                           THEN 'Facebook'
        WHEN LOWER(COALESCE(ls.referrer,'')) LIKE '%instagram%'
             OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) IN ('ig','instagram') THEN 'Instagram'
        WHEN LOWER(COALESCE(ls.referrer,'')) LIKE '%linkedin%'
             OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%linkedin%' THEN 'LinkedIn'
        WHEN LOWER(COALESCE(ls.referrer,'')) LIKE '%tiktok%'
             OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%tiktok%'  THEN 'TikTok'
        WHEN LOWER(COALESCE(ls.referrer,'')) LIKE '%youtube%'                  THEN 'YouTube'
        WHEN LOWER(COALESCE(ls.referrer,'')) LIKE '%facebook%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%fb.%'
             OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) IN ('fb','facebook') THEN 'Facebook'
        ELSE 'Other social'
    END                                                                    AS social_platform,
    CASE
        WHEN COALESCE(ls.campaign,'') <> '' OR ls.event_source = 'Paid Social'      THEN 'Paid Social'
        -- LEAD QUALIFICATION: a contact with NO form, NO appointment, NOT in any
        -- pipeline and NO payment is an inquiry, not a lead. With a real inbound
        -- conversation -> Queries; otherwise -> No Activity. This overrides bare
        -- attribution tags ('Referral'/'Social media' alone is not a lead).
        -- canonical meta_paid / website_form / organic_seo = a real form, kept.
        WHEN ls.contact_id IS NULL AND a.contact_id IS NULL
             AND lo.pipeline_name IS NULL AND pe.contact_id IS NULL
             AND COALESCE(ch.canonical_source,'') NOT IN ('meta_paid','organic_seo','website_form')
           THEN (CASE WHEN cc.channel IS NOT NULL AND cc.channel <> 'Email'
                      THEN 'Queries' ELSE 'No Activity' END)
        -- Facebook (conversation channel OR form referrer/utm) -> Paid Social
        -- (Facebook is a paid channel here). Requires a captured email so an
        -- anonymous Facebook DM still falls through to Queries.
        WHEN (cc.channel = 'Facebook'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%facebook%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%fb.%'
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) IN ('fb','facebook'))
             AND COALESCE(ch.email,'') <> ''                                        THEN 'Paid Social'
        -- utm_campaign matches a real Meta campaign -> Paid Social, regardless of
        -- the GHL form source (catches Instagram/DM leads tagged with a Meta
        -- campaign that GHL labels 'Social media' / 'Direct').
        WHEN regexp_replace(LOWER(COALESCE(
                 NULLIF(ls.campaign, ''),
                 NULLIF(replace(replace(replace(replace(replace(
                     regexp_extract(ls.page_url, 'utm_campaign=([^&]+)', 1),
                     '%7C','|'),'%2F','/'),'%2B','+'),'%20',' '),'+',' '), ''),
                 NULLIF(lcs.latest_source_campaign, ''))), '[^a-z0-9]', '', 'g')
             IN (SELECT ck FROM meta_ck)                                            THEN 'Paid Social'
        WHEN ls.event_source = 'Organic Search'                                     THEN 'Organic Search'
        WHEN ls.event_source = 'Referral' AND (
                 LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1))
                     IN ('google','bing','chatgpt','perplexity','gemini','duckduckgo','yahoo','yandex','brave','claude')
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%chatgpt%'
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%perplexity%'
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%gemini%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%google%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%bing%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%chatgpt%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%perplexity%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%yahoo%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%duckduckgo%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%brave%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%yandex%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%gemini%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%claude%'
           )                                                                        THEN 'Organic Search'
        WHEN ls.event_source = 'Referral'                                           THEN 'Referral'
        WHEN ls.event_source = 'Social media'                                       THEN 'Social media'
        WHEN ls.event_source IN ('Direct','Direct traffic')                         THEN 'Direct'
        -- DM-channel leads (no form): only classify as a real source when we
        -- actually captured an EMAIL. A conversation with no email (no form
        -- filled) is an anonymous inquiry -> falls through to Queries.
        WHEN cc.channel = 'Facebook'
             AND COALESCE(ch.email,'') <> ''                                        THEN 'Paid Social'
        WHEN cc.channel IN ('Instagram','WhatsApp','TikTok')
             AND COALESCE(ch.email,'') <> ''                                        THEN 'Social media'
        WHEN COALESCE(ls.event_source,'') <> ''                                     THEN ls.event_source
        -- Anonymous inquiry: no form, no email, not in a pipeline, AND there is
        -- a real inbound conversation (DM/chat/phone) -> Queries. Without a
        -- conversation it's a bare CRM record, not an inquiry -> Unknown.
        WHEN ls.contact_id IS NULL AND lo.pipeline_name IS NULL
             AND COALESCE(ch.email,'') = ''
             AND cc.channel IS NOT NULL AND cc.channel <> 'Email'                   THEN 'Queries'
        -- contact-level attribution (catches Meta Instant-Form leads that never
        -- create a GHL form-submission row, e.g. hemakshipillay -> meta_paid)
        WHEN ch.canonical_source = 'meta_paid'                                      THEN 'Paid Social'
        WHEN ch.canonical_source IN ('organic_seo','website_form')                  THEN 'Organic Search'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'paid social'        THEN 'Paid Social'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'organic search'     THEN 'Organic Search'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'referral'           THEN 'Referral'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) = 'social media'       THEN 'Social media'
        WHEN LOWER(COALESCE(ch.first_attribution_source,'')) IN ('direct','direct traffic') THEN 'Direct'
        -- LATEST attribution — a contact who booked/returned via a referral link
        -- (first attribution may be a stale 'CRM UI'). e.g. Muhammad Waqas.
        WHEN LOWER(COALESCE(ch.latest_attribution_source,'')) = 'referral'          THEN 'Referral'
        WHEN LOWER(COALESCE(ch.latest_attribution_source,'')) = 'paid social'       THEN 'Paid Social'
        WHEN LOWER(COALESCE(ch.latest_attribution_source,'')) = 'organic search'    THEN 'Organic Search'
        WHEN LOWER(COALESCE(ch.latest_attribution_source,'')) = 'social media'      THEN 'Social media'
        WHEN LOWER(COALESCE(ch.latest_attribution_source,'')) IN ('direct','direct traffic') THEN 'Direct'
        -- Walk-in = any walk-in-titled appointment (e.g. "Walk-in - New Lead").
        -- Highest priority among the booking/CRM sources — takes precedence over
        -- Returning Client / Direct Bookings.
        WHEN wa.contact_id IS NOT NULL                                              THEN 'Walk-in'
        -- Returning client = re-engaged after a prior service/opportunity that
        -- predates this window (e.g. an existing client who came back via DM).
        WHEN po.contact_id IS NOT NULL                                              THEN 'Returning Client'
        -- Direct booking = no form and no inbound inquiry, but the contact booked
        -- an appointment directly (the booking is their first action — e.g. they
        -- got a booking link and booked themselves). Catches would-be Agentcis /
        -- Unknown contacts that actually have an appointment.
        WHEN a.contact_id IS NOT NULL AND ls.contact_id IS NULL
             AND (cc.channel IS NULL OR cc.channel = 'Email')                       THEN 'Direct Bookings'
        -- Agentcis = historical client migrated from Agentcis (Mar 2026 switch):
        -- a real payment but no form, no conversation, no source and no booking.
        -- BUT a contact actively in a marketing/service funnel (L2C-* / CLT-*) is a
        -- real (source mis-attributed) lead, not a migrated record — e.g. a TikTok
        -- DM lead whose channel GHL never surfaced. Label those 'Unknown' (source
        -- undetermined) so they are not counted as Agentcis migrants.
        WHEN pe.contact_id IS NOT NULL AND ls.contact_id IS NULL
             AND (cc.channel IS NULL OR cc.channel = 'Email')
           THEN CASE WHEN COALESCE(lo.pipeline_name, '') LIKE 'L2C%'
                       OR COALESCE(lo.pipeline_name, '') LIKE 'CLT%'
                     THEN 'Unknown' ELSE 'Agentcis' END
        -- Queries = a RAW inbound inquiry: a real 2-way conversation (DM / chat /
        -- phone), no form, not in a pipeline. An outbound-only 'Email' channel
        -- (e.g. a booking-confirmation we sent) is NOT an inquiry -> Unknown.
        WHEN lo.pipeline_name IS NULL AND ls.contact_id IS NULL
             AND cc.channel IS NOT NULL AND cc.channel <> 'Email'                   THEN 'Queries'
        -- No Activity = a bare CRM record: no form, no conversation, no pipeline,
        -- no appointment and no payment (mostly created directly in the CRM).
        -- These are NOT real leads and are excluded from the Leads count.
        WHEN ls.contact_id IS NULL AND cc.channel IS NULL AND lo.pipeline_name IS NULL
             AND a.contact_id IS NULL AND pe.contact_id IS NULL                     THEN 'No Activity'
        -- ===== rescue would-be 'Unknown' leads via additional signals =====
        -- 1) the explicit GHL "Lead Source" custom field (most authoritative)
        WHEN cl.lead_source = 'Walk-in'                                             THEN 'Walk-in'
        WHEN cl.lead_source = 'Meta Ads'                                            THEN 'Paid Social'
        WHEN cl.lead_source = 'Social Media DM'                                     THEN 'Social media'
        WHEN cl.lead_source = 'Website Form'                                        THEN 'Organic Search'
        WHEN cl.lead_source = 'Chatbot'                                             THEN 'Web Chat'
        WHEN cl.lead_source = 'Email Marketing'                                     THEN 'Email'
        -- 2) the raw contact source (ig / fb / Check-in walk-in kiosk)
        WHEN ch.raw_source = 'Check-in App'                                         THEN 'Walk-in'
        WHEN LOWER(COALESCE(ch.raw_source,'')) IN ('fb','facebook')                 THEN 'Paid Social'
        WHEN LOWER(COALESCE(ch.raw_source,'')) IN ('ig','instagram')                THEN 'Social media'
        -- 3) Agentcis-migrated contacts (in an Agentcis_* pipeline) stay 'Unknown'
        -- per business decision — keep them out of the channel buckets below.
        WHEN lo.pipeline_name LIKE 'Agentcis%'                                      THEN 'Unknown'
        -- 4) a real inbound conversation channel with no tracked marketing source
        -- becomes a channel-named source (grouped under Executive 'Others').
        -- Phone/SMS splits into Direct call (phone) vs SMS by last message type.
        WHEN cc.channel = 'Phone/SMS' AND cc.last_message_type = 'TYPE_SMS'         THEN 'SMS'
        WHEN cc.channel = 'Phone/SMS'                                               THEN 'Direct call'
        WHEN cc.channel = 'Web Chat'                                                THEN 'Web Chat'
        WHEN cc.channel = 'Email'                                                   THEN 'Email'
        ELSE 'Unknown'
    END                                                                    AS refined_source,
    lo.pipeline_name AS pipeline, lo.stage_name AS stage, lo.status,
    dc.calendar_name AS calendar_name,
    a.appointment_status AS appt_status,
    CASE WHEN a.contact_id IS NOT NULL THEN 1 ELSE 0 END                   AS appt_booked,
    CASE WHEN LOWER(COALESCE(a.canonical_outcome,'')) = 'show' THEN 1 ELSE 0 END AS appt_showed,
    CAST(a.date_added + INTERVAL 10 HOUR AS DATE)                          AS appt_booked_date,
    a.title                                                                AS notes,
    COALESCE(oc.n_opps, 0)                                                 AS n_opps,
    -- campaign (form field -> page_url utm_campaign -> latest-source fallback)
    COALESCE(
        NULLIF(ls.campaign, ''),
        NULLIF(replace(replace(replace(replace(replace(
            regexp_extract(ls.page_url, 'utm_campaign=([^&]+)', 1),
            '%7C', '|'), '%2F', '/'), '%2B', '+'), '%20', ' '), '+', ' '), ''),
        NULLIF(lcs.latest_source_campaign, ''),
        NULLIF(ch.attribution_campaign, '')
    )                                                                      AS campaign,
    ls.form_name                                                           AS form_name,
    ch.visa_type                                                           AS visa
FROM cohort ch
LEFT JOIN ls       ON ls.contact_id = ch.contact_id
LEFT JOIN chan cc  ON cc.contact_id = ch.contact_id
LEFT JOIN appt a   ON a.contact_id  = ch.contact_id AND a.rn = 1
LEFT JOIN dim_calendars dc ON dc.calendar_id = a.calendar_id
LEFT JOIN lopp lo  ON lo.contact_id = ch.contact_id AND lo.rn = 1
LEFT JOIN opp_cnt oc ON oc.contact_id = ch.contact_id
LEFT JOIN prior_opp po ON po.contact_id = ch.contact_id
LEFT JOIN paid_ever pe ON pe.contact_id = ch.contact_id
LEFT JOIN walkin_appt wa ON wa.contact_id = ch.contact_id
LEFT JOIN fact_contact_latest_source lcs ON lcs.contact_id = ch.contact_id
LEFT JOIN clead cl ON cl.contact_id = ch.contact_id
) _lead_base;


-- =====================================================================
-- EXECUTIVE_1 — Conversions: contacts who reached 'COE Received' or
-- 'Initial Received' (or status = Won) in L2C - Education / CLT - Onshore
-- Admission, anchored on the LAST stage-change date (updated_at) in window.
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec1_conversions AS
WITH all_subs AS (
    SELECT contact_id, submitted_at, campaign, event_source, page_url, referrer,
           COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,'')) AS form_name
    FROM fact_form_submissions   WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, event_source, page_url, referrer,
           survey_name AS form_name
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
ls AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
        FROM all_subs
    ) WHERE rn = 1
),
chan AS (
    SELECT contact_id, channel FROM (
        SELECT contact_id, channel,
               ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY
                   CASE channel WHEN 'Facebook' THEN 1 WHEN 'Instagram' THEN 2
                                WHEN 'WhatsApp' THEN 3 WHEN 'TikTok' THEN 4 ELSE 9 END,
                   last_message_at DESC NULLS LAST) AS rn
        FROM fact_conversations WHERE contact_id IS NOT NULL
    ) WHERE rn = 1
),
conv AS (
    -- COE = reached COE/Initial Received or Won in L2C-Education / CLT-Onshore;
    -- POC = reached Application Submitted / Acknowledgment Sent + Doc or Won in
    -- CLT - VISA. One row per contact PER TYPE (a contact can be both).
    SELECT o.contact_id, p.pipeline_name, s.stage_name, o.status,
           CAST(o.updated_at AS DATE) AS changed_date,
           CASE WHEN p.pipeline_name = 'CLT - VISA' THEN 'POC' ELSE 'COE' END AS conv_type,
           ROW_NUMBER() OVER (
               PARTITION BY o.contact_id,
                   CASE WHEN p.pipeline_name = 'CLT - VISA' THEN 'POC' ELSE 'COE' END
               ORDER BY o.updated_at DESC) AS rn
    FROM fact_opportunities o
    JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
         AND p.pipeline_name IN ('L2C - Education', 'CLT - Onshore Admission', 'CLT - VISA')
    JOIN dim_stages   s ON s.stage_id = o.stage_id
    WHERE CAST(o.updated_at AS DATE) BETWEEN $since AND $until
      AND (
        (p.pipeline_name IN ('L2C - Education', 'CLT - Onshore Admission')
         AND (s.stage_name IN ('COE Received', 'Initial Received') OR LOWER(o.status) = 'won'))
        OR
        (p.pipeline_name = 'CLT - VISA'
         AND (s.stage_name IN ('Application Submitted', 'Acknowledgment Sent + Doc')
              OR LOWER(o.status) = 'won'))
      )
)
SELECT cv.contact_id, c.email,
    CASE
        WHEN COALESCE(ls.campaign,'') <> '' OR ls.event_source = 'Paid Social'      THEN 'Paid Social'
        WHEN ls.event_source = 'Organic Search'                                     THEN 'Organic Search'
        WHEN ls.event_source = 'Referral' AND (
                 LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%google%'
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%bing%'
              OR LOWER(regexp_extract(ls.page_url,'utm_source=([^&]+)',1)) LIKE '%chatgpt%'
              OR LOWER(COALESCE(ls.referrer,'')) LIKE '%google%' OR LOWER(COALESCE(ls.referrer,'')) LIKE '%bing%')
                                                                                    THEN 'Organic Search'
        WHEN ls.event_source = 'Referral'                                           THEN 'Referral'
        WHEN ls.event_source = 'Social media'                                       THEN 'Social media'
        WHEN ls.event_source IN ('Direct','Direct traffic')                         THEN 'Direct'
        WHEN cc.channel = 'Facebook'                                                THEN 'Paid Social'
        WHEN cc.channel IN ('Instagram','WhatsApp','TikTok')                        THEN 'Social media'
        WHEN COALESCE(ls.event_source,'') <> ''                                     THEN ls.event_source
        WHEN c.canonical_source = 'meta_paid'                                       THEN 'Paid Social'
        WHEN c.canonical_source IN ('organic_seo','website_form')                   THEN 'Organic Search'
        WHEN LOWER(COALESCE(c.first_attribution_source,'')) = 'paid social'         THEN 'Paid Social'
        WHEN LOWER(COALESCE(c.first_attribution_source,'')) = 'social media'        THEN 'Social media'
        WHEN LOWER(COALESCE(c.first_attribution_source,'')) = 'referral'            THEN 'Referral'
        WHEN LOWER(COALESCE(c.first_attribution_source,'')) = 'organic search'      THEN 'Organic Search'
        WHEN LOWER(COALESCE(c.first_attribution_source,'')) IN ('direct','direct traffic') THEN 'Direct'
        ELSE 'Other / Unknown'
    END                                                                    AS source,
    cv.pipeline_name AS pipeline, cv.stage_name AS stage, cv.status, cv.changed_date,
    cv.conv_type,
    -- detailed source: campaign → utm_campaign → form/survey name → social channel
    COALESCE(
        NULLIF(ls.campaign, ''),
        NULLIF(replace(replace(replace(replace(replace(
            regexp_extract(ls.page_url, 'utm_campaign=([^&]+)', 1),
            '%7C', '|'), '%2F', '/'), '%2B', '+'), '%20', ' '), '+', ' '), ''),
        NULLIF(ls.form_name, ''),
        CASE WHEN cc.channel IN ('Instagram','WhatsApp','TikTok','Facebook') THEN cc.channel END
    )                                                                      AS detail
FROM conv cv
JOIN fact_contacts c ON c.contact_id = cv.contact_id
LEFT JOIN ls   ON ls.contact_id = cv.contact_id
LEFT JOIN chan cc ON cc.contact_id = cv.contact_id
WHERE cv.rn = 1;


-- =====================================================================
-- EXECUTIVE_1 — Ad-spend detail (per Meta campaign / account). Spend is
-- USD in fact_meta_daily; the app converts to AUD with usd_to_aud().
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec1_adspend_detail AS
SELECT account_label, campaign_name,
       SUM(spend)       AS spend,
       SUM(impressions) AS impressions,
       SUM(clicks)      AS clicks,
       SUM(total_leads) AS leads
FROM fact_meta_daily
WHERE date BETWEEN $since AND $until
GROUP BY account_label, campaign_name;


-- =====================================================================
-- EXECUTIVE_1 — Revenue detail: one row per paying contact (succeeded GHL
-- payments in window), with email, refined source (same labels as the
-- Executive_1 lead detail), net amount paid (AUD) and last payment date.
-- =====================================================================
CREATE OR REPLACE VIEW vw_exec1_revenue_detail AS
WITH pay AS (
    SELECT contact_id,
           SUM(amount - COALESCE(amount_refunded, 0)) AS paid,
           MAX(CAST(created_at AS DATE))              AS last_payment_date
    FROM fact_payments
    WHERE LOWER(status) = 'succeeded'
      AND CAST(created_at AS DATE) BETWEEN $since AND $until
    GROUP BY contact_id
),
lf AS (
    SELECT contact_id, campaign, event_source, session_source,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
)
SELECT c.email,
    CASE
        WHEN COALESCE(lf.campaign,'') <> '' OR LOWER(COALESCE(lf.session_source,'')) = 'paid social'
             OR LOWER(COALESCE(lf.event_source,'')) = 'paid social'
             OR c.canonical_source = 'meta_paid'                              THEN 'Paid Social'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'organic search'
             OR c.canonical_source IN ('organic_seo', 'website_form')         THEN 'Organic Search'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'referral'
             OR LOWER(COALESCE(c.first_attribution_source,'')) = 'referral'   THEN 'Referral'
        WHEN LOWER(COALESCE(lf.event_source,'')) IN ('direct', 'direct traffic') THEN 'Direct'
        WHEN LOWER(COALESCE(lf.event_source,'')) = 'social media'             THEN 'Social media'
        ELSE 'Other / Unknown'
    END AS source,
    pay.paid AS revenue, pay.last_payment_date
FROM pay
JOIN fact_contacts c ON c.contact_id = pay.contact_id
LEFT JOIN lf ON lf.contact_id = pay.contact_id AND lf.rn = 1;


-- =====================================================================
-- FOLLOWER PERFORMANCE — two views
-- =====================================================================

-- TABLE 1: Lead funnel (cohort). Of the opportunities CREATED in [since, until],
-- how many REACHED each presales stage (cumulative, by stage rank — an opp now in
-- a later stage counts toward every earlier one). New Leads = whole cohort. No
-- Show / Lost / Open are current terminal/status snapshots. Binds: $since,$until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_funnel_cohort AS
WITH cohort AS (
    SELECT o.opportunity_id,
           LOWER(COALESCE(o.status, ''))      AS status,
           LOWER(COALESCE(st.stage_name, '')) AS stage,
           CASE
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'new lead%'          THEN 1
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'no show%'           THEN 1
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'pre sales (1)%'     THEN 2
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'pre sales (2)%'     THEN 3
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'booking link%'      THEN 4
               WHEN LOWER(COALESCE(st.stage_name,'')) LIKE 'post consultation%' THEN 5
               ELSE 6   -- progressed beyond presales (admission / won / etc.)
           END AS rnk
    FROM fact_opportunities o
    LEFT JOIN dim_stages st ON st.stage_id = o.stage_id
    WHERE CAST(o.created_at + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
)
SELECT
    COUNT(*)                                       AS total_opps,
    COUNT(*)                                       AS new_leads,
    COUNT(*) FILTER (WHERE rnk >= 2)               AS presales_1,
    COUNT(*) FILTER (WHERE rnk >= 3)               AS presales_2,
    COUNT(*) FILTER (WHERE rnk >= 4)               AS booking_link_shared,
    COUNT(*) FILTER (WHERE rnk >= 5)               AS post_consultation,
    COUNT(*) FILTER (WHERE stage LIKE 'no show%')  AS no_show,
    COUNT(*) FILTER (WHERE status = 'lost')        AS lost,
    COUNT(*) FILTER (WHERE status = 'open')        AS open_opps
FROM cohort
WHERE COALESCE($city, '') IS NOT NULL;  -- $city unused (site-wide); reference for binds


-- TABLE 2: Follower activity. Per staff member, the opportunities they personally
-- performed an activity on within [since, until] — attributed by the message
-- ACTOR (fact_messages.user_id), excluding system/automation logs. Stage/status
-- columns use the point-in-time guard (don't credit a stage entered after the
-- range). Binds: $since, $until.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_follower_activity AS
WITH acts AS (
    SELECT DISTINCT
           m.user_id,
           o.opportunity_id,
           LOWER(COALESCE(o.status, ''))      AS status,
           LOWER(COALESCE(st.stage_name, '')) AS stage,
           (o.last_stage_change_at IS NULL
            OR CAST(o.last_stage_change_at + INTERVAL 10 HOUR AS DATE) <= $until)  AS stage_asof,
           (o.last_status_change_at IS NULL
            OR CAST(o.last_status_change_at + INTERVAL 10 HOUR AS DATE) <= $until) AS status_asof
    FROM fact_messages m
    JOIN fact_opportunities o ON o.contact_id = m.contact_id
    LEFT JOIN dim_stages st ON st.stage_id = o.stage_id
    WHERE m.user_id IS NOT NULL
      AND UPPER(COALESCE(m.message_type, '')) NOT LIKE 'TYPE_ACTIVITY%'  -- exclude system logs
      AND CAST(m.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
)
SELECT
    COALESCE(u.full_name, a.user_id)                                          AS follower,
    COUNT(*)                                                                  AS total_opps,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'new lead%')          AS new_leads,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'pre sales (1)%')     AS presales_1,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'pre sales (2)%')     AS presales_2,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'booking link%')      AS booking_link_shared,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'post consultation%') AS post_consultation,
    COUNT(*) FILTER (WHERE a.stage_asof AND a.stage LIKE 'no show%')           AS no_show,
    COUNT(*) FILTER (WHERE a.status_asof AND a.status = 'lost')                AS lost,
    COUNT(*) FILTER (WHERE a.status_asof AND a.status = 'open')                AS open_opps
FROM acts a
LEFT JOIN dim_users u ON u.user_id = a.user_id
GROUP BY 1
HAVING COUNT(*) > 0
ORDER BY total_opps DESC;


-- Drill-down: the contacts/opps a given staff member acted on in the range.
-- Binds: $since, $until. App filters to the picked follower; adds Source/notes.
CREATE OR REPLACE VIEW vw_follower_activity_detail AS
WITH acts AS (
    SELECT DISTINCT m.user_id, o.opportunity_id, o.contact_id
    FROM fact_messages m
    JOIN fact_opportunities o ON o.contact_id = m.contact_id
    WHERE m.user_id IS NOT NULL
      AND UPPER(COALESCE(m.message_type, '')) NOT LIKE 'TYPE_ACTIVITY%'
      AND CAST(m.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN $since AND $until
)
SELECT
    COALESCE(u.full_name, a.user_id) AS follower,
    a.contact_id,
    c.email,
    c.contact_name,
    c.phone,
    p.pipeline_name    AS pipeline,
    st.stage_name      AS stage,
    o.status,
    o.opportunity_name AS opportunity
FROM acts a
JOIN fact_opportunities o ON o.opportunity_id = a.opportunity_id
LEFT JOIN dim_stages st ON st.stage_id = o.stage_id
LEFT JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
LEFT JOIN dim_users u ON u.user_id = a.user_id
LEFT JOIN fact_contacts c ON c.contact_id = a.contact_id;
