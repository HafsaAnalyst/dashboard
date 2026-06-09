"""One-shot fix: re-derive Latest Source for Samisoni using the new
forms-list fallback, then push to GHL."""
import sys, time, logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(message)s")

import duckdb
from connectors import ghl

TARGET_CID = "0ZlWFOdx008U18OV14BW"

forms = ghl.fetch_forms()
forms_by_id = {f["id"]: f["name"] for f in forms if f.get("id") and f.get("name")}
print(f"forms loaded: {len(forms_by_id)}")

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
row = con.execute("""
    WITH ranked AS (
      SELECT contact_id, form_id, form_name, campaign, utm_content,
             session_source, event_source, submitted_at,
             ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
      FROM fact_form_submissions WHERE contact_id=?
    ) SELECT * FROM ranked WHERE rn=1
""", [TARGET_CID]).fetchone()
print("latest sub:", row)

cid, fid, fn, camp, utm, ss, es, sa, _ = row
val = ""
if camp and utm:   val = f"{camp} -- {utm}"
elif camp:         val = camp
elif fn:           val = fn
elif ss:           val = ss
elif es:           val = es

GENERIC = {"Social media","Paid Social","Direct traffic","Organic Search",
           "Referral","Email","SMS","Direct",""}
if val in GENERIC and fid in forms_by_id:
    val = forms_by_id[fid]
print(f"NEW VALUE: {val!r}")

ok = ghl.update_contact_custom_field(TARGET_CID, ghl.LATEST_SOURCE_CONTACT_FIELD, val)
print(f"contact updated: {ok}")
for o in ghl.fetch_contact_opportunities(TARGET_CID):
    time.sleep(0.12)
    oid = o.get("id")
    ok_o = ghl.update_opportunity_custom_field(oid, ghl.LATEST_SOURCE_OPP_FIELD, val)
    print(f"  opp {oid} -> {ok_o}")
