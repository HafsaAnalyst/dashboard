"""
Emails that shifted June -> July on the dashboard.

LIST A  — June leads REVIVED in July (now counted as July leads, dropped from June):
          existed before July, had a real (non-Query-Management) form in June, and
          their LATEST form is in July -> their "revived" month moved to July.

LIST B  — their APPOINTMENTS (bookings that moved June -> July with the lead).

LIST C  — appointments BOOKED in June for a JULY meeting date (reschedule-style
          "was June, now July"), in case that's the sense you mean.

RUN (PowerShell):  $env:MOTHERDUCK_TOKEN="<token>"; python june_july_movers.py
"""
import os
import duckdb

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOTHERDUCK_TOKEN first.")
con = duckdb.connect(f"md:migration?motherduck_token={TOKEN}")

JUN_S, JUN_E = "2026-06-01", "2026-06-30"
JUL_S, JUL_E = "2026-07-01", "2026-07-31"

# ---- LIST A: June leads revived into July ----
list_a = con.execute(f"""
WITH subs AS (
  SELECT contact_id, CAST(submitted_at + INTERVAL 10 HOUR AS DATE) d
  FROM fact_form_submissions
  WHERE contact_id IS NOT NULL
    AND COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,'')) NOT LIKE 'Query Management%'
  UNION ALL
  SELECT contact_id, CAST(submitted_at + INTERVAL 10 HOUR AS DATE) d
  FROM fact_survey_submissions
  WHERE contact_id IS NOT NULL AND COALESCE(survey_name,'') NOT LIKE 'Query Management%'
),
agg AS (
  SELECT contact_id, MAX(d) AS last_form,
         BOOL_OR(d BETWEEN DATE '{JUN_S}' AND DATE '{JUN_E}') AS had_june_form
  FROM subs GROUP BY contact_id
)
SELECT c.email, a.last_form
FROM agg a JOIN fact_contacts c ON c.contact_id = a.contact_id
WHERE a.had_june_form
  AND a.last_form BETWEEN DATE '{JUL_S}' AND DATE '{JUL_E}'
  AND CAST(c.date_added + INTERVAL 10 HOUR AS DATE) < DATE '{JUL_S}'
  AND COALESCE(c.email,'') <> ''
  AND LOWER(c.email) NOT LIKE '%test%'
  AND LOWER(c.email) NOT LIKE '%@themigration.com.au'
ORDER BY a.last_form
""").fetchall()

mover_ids = set()
mover_rows = con.execute(f"""
WITH subs AS (
  SELECT contact_id, CAST(submitted_at + INTERVAL 10 HOUR AS DATE) d
  FROM fact_form_submissions WHERE contact_id IS NOT NULL
    AND COALESCE(NULLIF(form_name,''), NULLIF(event_form_name,'')) NOT LIKE 'Query Management%'
  UNION ALL
  SELECT contact_id, CAST(submitted_at + INTERVAL 10 HOUR AS DATE) d
  FROM fact_survey_submissions WHERE contact_id IS NOT NULL AND COALESCE(survey_name,'') NOT LIKE 'Query Management%'
),
agg AS (SELECT contact_id, MAX(d) last_form,
        BOOL_OR(d BETWEEN DATE '{JUN_S}' AND DATE '{JUN_E}') had_june_form FROM subs GROUP BY contact_id)
SELECT c.contact_id FROM agg a JOIN fact_contacts c ON c.contact_id=a.contact_id
WHERE a.had_june_form AND a.last_form BETWEEN DATE '{JUL_S}' AND DATE '{JUL_E}'
  AND CAST(c.date_added + INTERVAL 10 HOUR AS DATE) < DATE '{JUL_S}'
""").fetchall()
mover_ids = {r[0] for r in mover_rows}

# ---- LIST B: those movers' appointments (booking moved June -> July) ----
list_b = []
if mover_ids:
    ph = ",".join(["?"] * len(mover_ids))
    list_b = con.execute(f"""
      SELECT DISTINCT c.email, CAST(a.date_added + INTERVAL 10 HOUR AS DATE) booked_on
      FROM fact_appointments a JOIN fact_contacts c ON c.contact_id = a.contact_id
      WHERE LOWER(COALESCE(a.appointment_status,'')) <> 'invalid'
        AND a.contact_id IN ({ph}) AND COALESCE(c.email,'') <> ''
      ORDER BY booked_on
    """, list(mover_ids)).fetchall()

# ---- LIST C: appointments booked in June but scheduled for July ----
list_c = con.execute(f"""
  SELECT DISTINCT c.email,
         CAST(a.date_added + INTERVAL 10 HOUR AS DATE) booked_on,
         CAST(a.start_time + INTERVAL 10 HOUR AS DATE) meeting_on
  FROM fact_appointments a JOIN fact_contacts c ON c.contact_id = a.contact_id
  WHERE LOWER(COALESCE(a.appointment_status,'')) <> 'invalid'
    AND CAST(a.date_added + INTERVAL 10 HOUR AS DATE) BETWEEN DATE '{JUN_S}' AND DATE '{JUN_E}'
    AND CAST(a.start_time + INTERVAL 10 HOUR AS DATE) BETWEEN DATE '{JUL_S}' AND DATE '{JUL_E}'
    AND COALESCE(c.email,'') <> ''
  ORDER BY meeting_on
""").fetchall()

print(f"\n=== LIST A — June leads revived in July ({len(list_a)}) ===")
for e, lf in list_a:
    print(f"  {e}   (last form {lf})")
print(f"\n=== LIST B — their appointments that moved June -> July ({len(list_b)}) ===")
for e, d in list_b:
    print(f"  {e}   (booked {d})")
print(f"\n=== LIST C — appointments booked in June for a July meeting ({len(list_c)}) ===")
for e, b, m in list_c:
    print(f"  {e}   (booked {b} -> meeting {m})")
