"""Investigate the Chat Widget Form contact: why does Latest Source resolve to
'Organic Search' instead of 'Chat Widget Form'?"""
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

EMAIL = "danishulislam1999@gmail.com"
cid_row = con.execute(
    "SELECT contact_id FROM fact_contacts WHERE LOWER(email)=LOWER(?)", [EMAIL]).fetchone()
print("contact_id:", cid_row)
if not cid_row:
    sys.exit(0)
cid = cid_row[0]

print("\nFORM SUBMISSIONS in DB:")
for r in con.execute("""
    SELECT submitted_at, form_id, form_name, event_form_name, campaign,
           session_source, event_source, page_path
    FROM fact_form_submissions WHERE contact_id=?
    ORDER BY submitted_at DESC""", [cid]).fetchall():
    print(" ", r)

print("\nSURVEY SUBMISSIONS in DB:")
for r in con.execute("""
    SELECT submitted_at, survey_id, survey_name, session_source, event_source
    FROM fact_survey_submissions WHERE contact_id=?
    ORDER BY submitted_at DESC""", [cid]).fetchall():
    print(" ", r)

# Now go to API and inspect the raw submission
print("\n\n=== LIVE GHL PROBE ===")
from connectors import ghl
subs = ghl.fetch_form_submissions("2026-05-01", "2026-05-31")
for s in subs:
    if (s.get("contactId") == cid or
        (s.get("email") or "").lower() == EMAIL):
        print("MATCH:")
        print(json.dumps(s, indent=2, default=str)[:3000])
        print("---")
