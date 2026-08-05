"""
June leads that were REVIVED or UPDATED in July (and August) — i.e. leads June was
showing whose activity later moved forward, so the dashboard now attributes them to
a later month and June shrank.

JUNE set = vw_exec1_lead_detail, since=2026-06-01, until=2026-06-30, funnel
(is_created OR is_revived), exclude No Activity & Queries, email present.

A June lead is flagged if, on/after 2026-07-01, ANY of these happened:
  - a new form/survey submission (REVIVED),
  - the contact record was updated (date_updated),
  - one of its opportunities was updated (updated_at),
  - a new appointment was booked.

RUN (PowerShell):  $env:MOTHERDUCK_TOKEN="<token>"; python june_pulled_out.py
"""
import os
import re
from pathlib import Path
import duckdb
import pandas as pd

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOTHERDUCK_TOKEN first.")
JUL = "2026-07-01"

ROOT = Path(__file__).resolve().parent
q = {m.group(1): m.group(2).strip().rstrip(';')
     for m in re.compile(r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
                         re.DOTALL | re.IGNORECASE).finditer(
                         (ROOT / "dashboards" / "sql" / "tab_cards.sql").read_text(encoding="utf-8"))}
body = q["vw_exec1_lead_detail"]
ix = body.find(") _lead_base")
if ix != -1:
    body = body[:ix] + ") _lead_base"
con = duckdb.connect(f"md:migration?motherduck_token={TOKEN}")

# June cohort (the leads June was showing)
b = body.replace("$since", "DATE '2026-06-01'").replace("$until", "DATE '2026-06-30'")
june = con.execute(f"SELECT * FROM ( {b} ) ld").fetchdf()
june = june[~june["refined_source"].isin(["No Activity", "Queries"])]
june = june[june["email"].fillna("").astype(str).str.strip() != ""]
june = june[(june["is_created"] == 1) | (june["is_revived"] == 1)].copy()
ids = june["contact_id"].tolist()
con.register("_june_ids", pd.DataFrame({"contact_id": ids}))

# what July+ activity each June contact had
act = con.execute(f"""
WITH j AS (SELECT contact_id FROM _june_ids)
SELECT c AS contact_id, STRING_AGG(DISTINCT kind, ', ') AS july_activity FROM (
  SELECT j.contact_id c, 'new form' kind FROM j JOIN fact_form_submissions f ON f.contact_id=j.contact_id
    WHERE CAST(f.submitted_at + INTERVAL 10 HOUR AS DATE) >= DATE '{JUL}'
      AND COALESCE(NULLIF(f.form_name,''),NULLIF(f.event_form_name,'')) NOT LIKE 'Query Management%'
  UNION ALL
  SELECT j.contact_id, 'contact updated' FROM j JOIN fact_contacts ct ON ct.contact_id=j.contact_id
    WHERE CAST(ct.date_updated + INTERVAL 10 HOUR AS DATE) >= DATE '{JUL}'
  UNION ALL
  SELECT j.contact_id, 'opp updated' FROM j JOIN fact_opportunities o ON o.contact_id=j.contact_id
    WHERE CAST(o.updated_at + INTERVAL 10 HOUR AS DATE) >= DATE '{JUL}'
  UNION ALL
  SELECT j.contact_id, 'new appointment' FROM j JOIN fact_appointments a ON a.contact_id=j.contact_id
    WHERE CAST(a.date_added + INTERVAL 10 HOUR AS DATE) >= DATE '{JUL}'
      AND LOWER(COALESCE(a.appointment_status,'')) <> 'invalid'
) GROUP BY 1
""").fetchdf()

out = june.merge(act, on="contact_id", how="inner")[["email", "lead_date", "refined_source", "july_activity"]]
out["lead_date"] = pd.to_datetime(out["lead_date"]).dt.date
out = out.sort_values("lead_date")
print(f"\n{len(out)} June leads were revived/updated in July+:\n")
print("| Email | June lead date | Source | July+ activity |")
print("|---|---|---|---|")
for _, r in out.iterrows():
    print(f"| {r.email} | {r.lead_date} | {r.refined_source} | {r.july_activity} |")
