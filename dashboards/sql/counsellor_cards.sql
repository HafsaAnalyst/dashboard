-- =====================================================================
-- The Migration Dashboard — Counsellors tab SQL pack
-- =====================================================================
-- Per the locked spec:
--   * Date field = appointment.start_time (when meeting is scheduled).
--     Counsellor utilisation reflects when meetings actually happen
--     so the dashboard matches GHL's calendar view directly.
--   * City filter cascades via fact_contacts.city of the appointment's contact
--   * Pipeline filter does NOT apply here (counsellor view is appt-driven,
--     not opp-driven)
--
-- The actual list of calendar_ids to display (and which are 'Paid' vs
-- 'Free') is hardcoded in app.py — this query returns ALL calendars
-- with at least one appointment in the period, and the app picks the
-- ones it wants.
--
-- Binds:
--   $since        DATE
--   $until        DATE
--   $city         VARCHAR  'All' | 'Melbourne' | 'Sydney' | 'Others' | 'Unidentified'
-- =====================================================================

CREATE OR REPLACE VIEW vw_counsellors AS
SELECT
    a.calendar_id,
    -- Appointments total EXCLUDES 'invalid' status (test bookings, ghost
    -- records). Sat/Sun also excluded in the WHERE clause below.
    COUNT(*)                                                                   AS appointments,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'confirmed')          AS confirmed,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'showed')             AS showed,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'noshow')             AS noshow,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'cancelled')          AS cancelled,
    COUNT(*) FILTER (WHERE a.amount_paid > 0)                                  AS paid_count,
    COALESCE(SUM(a.amount_paid), 0)                                            AS paid_total
FROM fact_appointments a
JOIN fact_contacts    c ON c.contact_id = a.contact_id
-- Date attribution = appointment.start_time (when the meeting is
-- scheduled to happen). Counsellor utilisation reflects the meeting
-- date, so this view matches what's visible in GHL's calendar view.
-- (The SEO tab uses lead-date cohort attribution separately.)
-- Invalid appointments (test bookings, ghost records) are EXCLUDED
-- globally — they shouldn't appear in any metric, drill-down, or count.
-- Sat/Sun excluded: DAYOFWEEK 0=Sun,6=Sat in DuckDB. Slots Available is
-- weekday-only (count_weekdays) so the numerator must match — otherwise
-- a Sat/Sun booking would push booking rate above 100%.
WHERE LOWER(a.appointment_status) <> 'invalid'
  AND CAST(a.start_time AS DATE) BETWEEN $since AND $until
  AND DAYOFWEEK(CAST(a.start_time AS DATE)) NOT IN (0, 6)
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
GROUP BY a.calendar_id;


-- Per-calendar, per-day breakdown — drives the Mel/Syd trend chart.
-- Same date/city filter logic as vw_counsellors above; one row per
-- (date, calendar_id). The app maps calendar_id → counsellor → office
-- city in Python (Gurbir + Navneet = Melbourne; rest = Sydney).
CREATE OR REPLACE VIEW vw_counsellors_daily AS
SELECT
    -- Daily bucket = meeting date (a.start_time), matches vw_counsellors above.
    CAST(a.start_time AS DATE)                                                  AS date,
    a.calendar_id,
    COUNT(*)                                                                    AS appointments,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'showed')              AS showed,
    COUNT(*) FILTER (WHERE LOWER(a.appointment_status) = 'noshow')              AS noshow,
    COUNT(*) FILTER (WHERE a.amount_paid > 0)                                   AS paid_count,
    COALESCE(SUM(a.amount_paid), 0)                                             AS paid_total
FROM fact_appointments a
JOIN fact_contacts    c ON c.contact_id = a.contact_id
-- Date attribution = appointment.start_time (when the meeting is
-- scheduled to happen). Counsellor utilisation reflects the meeting
-- date, so this view matches what's visible in GHL's calendar view.
-- (The SEO tab uses lead-date cohort attribution separately.)
-- Invalid appointments excluded — test bookings, ghost records.
-- Sat/Sun excluded: DAYOFWEEK 0=Sun,6=Sat in DuckDB.
WHERE LOWER(a.appointment_status) <> 'invalid'
  AND CAST(a.start_time AS DATE) BETWEEN $since AND $until
  AND DAYOFWEEK(CAST(a.start_time AS DATE)) NOT IN (0, 6)
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
GROUP BY CAST(a.start_time AS DATE), a.calendar_id;


-- Per-appointment drill-down for the Counsellors tab scorecard modal.
-- One row per appointment in window; the app filters by status
-- (booked / showed / noshow / paid) and by counsellor city.
--
-- Latest Source is COMPUTED LIVE here (not read from the GHL-synced
-- fact_contact_latest_source mirror) so contacts whose Latest Source field
-- hasn't been backfilled yet still show a meaningful value in the modal.
-- The same precedence as sync_latest_source in run_etl.py:
--   1. campaign + utm_content  -> "campaign -- utm" (Meta-style attribution)
--   2. campaign alone
--   3. form_name (from form submission) OR survey_name (from survey submission)
--   4. event_form_name
--   5. session_source
--   6. event_source
--   7. counsellor name from contact's LATEST appointment's calendar (Phase 2 fallback)
--   8. fact_contacts.latest_attribution_source as final fallback
-- Display-only — nothing here is written back to GHL.
CREATE OR REPLACE VIEW vw_counsellor_appointments_detail AS
WITH all_subs AS (
    SELECT contact_id, submitted_at, campaign, utm_content,
           form_name, event_form_name, session_source, event_source,
           NULL AS survey_name
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
    UNION ALL
    SELECT contact_id, submitted_at, campaign, utm_content,
           NULL AS form_name, NULL AS event_form_name,
           session_source, event_source, survey_name
    FROM fact_survey_submissions WHERE contact_id IS NOT NULL
),
latest_sub AS (
    SELECT contact_id, campaign, utm_content, form_name, event_form_name,
           session_source, event_source, survey_name,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM all_subs
),
computed_source AS (
    SELECT contact_id,
           CASE
               WHEN COALESCE(campaign,'') <> '' AND COALESCE(utm_content,'') <> ''
                    THEN campaign || ' -- ' || utm_content
               WHEN COALESCE(campaign,'') <> ''        THEN campaign
               WHEN COALESCE(form_name,'') <> ''       THEN form_name
               WHEN COALESCE(survey_name,'') <> ''     THEN survey_name
               WHEN COALESCE(event_form_name,'') <> '' THEN event_form_name
               WHEN COALESCE(session_source,'') <> ''  THEN session_source
               WHEN COALESCE(event_source,'') <> ''    THEN event_source
               ELSE NULL
           END AS latest_source_live
    FROM latest_sub WHERE rn = 1
),
-- Step 7 fallback: contact's latest appointment → calendar name. Matches
-- the appointment-based step in Phase 2 backfill so contacts whose only
-- activity is an appointment booking still resolve to a counsellor name.
latest_appt AS (
    SELECT contact_id, calendar_id,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
    FROM fact_appointments
    WHERE contact_id IS NOT NULL
      AND LOWER(appointment_status) <> 'invalid'
),
appt_calendar AS (
    SELECT la.contact_id, dc.calendar_name
    FROM latest_appt la
    JOIN dim_calendars dc ON dc.calendar_id = la.calendar_id
    WHERE la.rn = 1
)
SELECT
    a.appointment_id,
    a.contact_id,
    a.calendar_id,
    c.email,
    c.contact_name,
    a.appointment_status,
    a.canonical_outcome,
    a.start_time,
    a.date_added                                                        AS appt_created_at,
    a.amount_paid,
    COALESCE(NULLIF(cs.latest_source_live, ''),
             NULLIF(ac.calendar_name, ''),
             c.latest_attribution_source,
             '')                                                        AS latest_source,
    (a.amount_paid > 0)                                                 AS payment_succeeded
FROM fact_appointments a
JOIN fact_contacts c           ON c.contact_id = a.contact_id
LEFT JOIN computed_source cs   ON cs.contact_id = a.contact_id
LEFT JOIN appt_calendar ac     ON ac.contact_id = a.contact_id
WHERE LOWER(a.appointment_status) <> 'invalid'
  AND CAST(a.start_time AS DATE) BETWEEN $since AND $until
  AND DAYOFWEEK(CAST(a.start_time AS DATE)) NOT IN (0, 6);


-- Per-calendar conversion counts for the Performance Matrix table.
-- Booked = distinct contacts who booked an appointment on this calendar
--          in the window (Sat/Sun excluded).
-- Converted = subset whose contact has at least one opportunity in:
--   - L2C - Education            stage 'COE Received'          (COE-converted)
--   - CLT - Onshore Admission    stage 'COE Received'          (COE-converted)
--   - L2C - VISA                 stage 'MARA Appointment Booked' (VOE-converted)
-- A contact who converted on multiple pipelines still counts once per counsellor.
CREATE OR REPLACE VIEW vw_counsellor_conversions AS
WITH window_appts AS (
    SELECT contact_id, calendar_id
    FROM fact_appointments
    WHERE CAST(start_time AS DATE) BETWEEN $since AND $until
      AND DAYOFWEEK(CAST(start_time AS DATE)) NOT IN (0, 6)
      AND LOWER(appointment_status) <> 'invalid'
      AND contact_id IS NOT NULL
),
converted_contacts AS (
    SELECT DISTINCT o.contact_id
    FROM fact_opportunities o
    JOIN dim_stages s    ON s.stage_id    = o.stage_id
    JOIN dim_pipelines p ON p.pipeline_id = s.pipeline_id
    WHERE (p.pipeline_name IN ('L2C - Education','CLT - Onshore Admission')
           AND s.stage_name = 'COE Received')
       OR (p.pipeline_name = 'L2C - VISA'
           AND s.stage_name = 'MARA Appointment Booked')
)
SELECT
    wa.calendar_id,
    COUNT(DISTINCT wa.contact_id)                                                         AS booked_contacts,
    COUNT(DISTINCT CASE WHEN wa.contact_id IN (SELECT contact_id FROM converted_contacts)
                        THEN wa.contact_id END)                                           AS converted_contacts
FROM window_appts wa
GROUP BY wa.calendar_id;


-- Per-calendar payment aggregation. A succeeded payment counts toward a
-- counsellor's "Paid Consults" if the contact had at least one appointment
-- on that calendar in the window AND the payment was made in the same
-- window. Net amount = amount - amount_refunded.
-- Payment data comes from GHL's /payments/transactions API (status field
-- is the source of truth, not fact_appointments.amount_paid which is often
-- empty because consultations are paid via separate invoices).
CREATE OR REPLACE VIEW vw_counsellor_payments AS
WITH window_appts AS (
    SELECT DISTINCT contact_id, calendar_id
    FROM fact_appointments
    WHERE CAST(start_time AS DATE) BETWEEN $since AND $until
      AND DAYOFWEEK(CAST(start_time AS DATE)) NOT IN (0, 6)
      AND LOWER(appointment_status) <> 'invalid'
      AND contact_id IS NOT NULL
)
SELECT
    wa.calendar_id,
    COUNT(DISTINCT p.contact_id)                                                AS paid_contacts,
    COUNT(*)                                                                    AS paid_count,
    COALESCE(SUM(p.amount - COALESCE(p.amount_refunded, 0)), 0)                 AS paid_total
FROM window_appts wa
JOIN fact_payments p ON p.contact_id = wa.contact_id
WHERE LOWER(p.status) = 'succeeded'
  AND (p.amount - COALESCE(p.amount_refunded, 0)) > 0
  AND CAST(p.created_at AS DATE) BETWEEN $since AND $until
GROUP BY wa.calendar_id;


-- Per-counsellor historical show rate over the last 90 days. Used to
-- compute the 75th-percentile benchmark for the Mel/Syd mini tables.
-- Filtered to weekday appointments only to match the live metric.
CREATE OR REPLACE VIEW vw_counsellor_show_rate_90d AS
SELECT
    a.calendar_id,
    COUNT(*)                                                              AS appts_90d,
    COUNT(*) FILTER (WHERE LOWER(a.canonical_outcome) = 'show')           AS showed_90d,
    CASE WHEN COUNT(*) > 0
         THEN COUNT(*) FILTER (WHERE LOWER(a.canonical_outcome) = 'show') * 1.0 / COUNT(*)
         END                                                              AS show_rate_90d
FROM fact_appointments a
WHERE CAST(a.start_time AS DATE) >= CURRENT_DATE - INTERVAL '90 days'
  AND CAST(a.start_time AS DATE) <  CURRENT_DATE
  AND DAYOFWEEK(CAST(a.start_time AS DATE)) NOT IN (0, 6)
  AND LOWER(a.appointment_status) <> 'invalid'
GROUP BY a.calendar_id
HAVING COUNT(*) >= 3;  -- skip calendars with too few data points to trust
