"""Investigate why Muhammad Faizan shows blank Latest Source."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

cid_row = con.execute(
    "SELECT contact_id FROM fact_contacts WHERE LOWER(email)=LOWER(?)",
    ["muhammadfaizan6335@gmail.com"],
).fetchone()
print("contact_id:", cid_row)
if not cid_row:
    sys.exit(0)
cid = cid_row[0]

print("\nFORM SUBMISSIONS:")
for r in con.execute(
    "SELECT submitted_at, form_name, event_form_name, campaign, utm_content, session_source, event_source "
    "FROM fact_form_submissions WHERE contact_id=? ORDER BY submitted_at DESC", [cid]).fetchall():
    print(" ", r)

print("\nSURVEY SUBMISSIONS:")
for r in con.execute(
    "SELECT submitted_at, survey_id, survey_name, campaign, utm_content, session_source, event_source "
    "FROM fact_survey_submissions WHERE contact_id=? ORDER BY submitted_at DESC", [cid]).fetchall():
    print(" ", r)

print("\nCONTACT ATTRIBUTION FIELDS:")
print(" ", con.execute(
    "SELECT latest_attribution_source, latest_attribution_medium, latest_attribution_form, "
    "first_attribution_source, source, canonical_source FROM fact_contacts WHERE contact_id=?",
    [cid]).fetchone())
