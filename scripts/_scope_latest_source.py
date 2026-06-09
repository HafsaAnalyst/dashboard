"""Scope: count contacts and bucket their resolvable Latest Source value."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from connectors import ghl

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

forms = ghl.fetch_forms()
forms_by_id = {f["id"]: f["name"] for f in forms if f.get("id") and f.get("name")}

rows = con.execute("""
  WITH ranked AS (
    SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
           session_source, event_source,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
  ) SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
           session_source, event_source FROM ranked WHERE rn=1
""").fetchall()

GENERIC = {"Social media", "Paid Social", "Direct traffic", "Organic Search",
           "Referral", "Email", "SMS", "Direct", ""}

buckets = {"real_value": 0, "generic_only": 0, "no_value": 0}
for cid, fid, fn, efn, camp, utm, ss, es in rows:
    v = ""
    if camp and utm:  v = f"{camp} -- {utm}"
    elif camp:        v = camp
    elif fn:          v = fn
    elif efn:         v = efn
    elif ss:          v = ss
    elif es:          v = es
    if v in GENERIC and fid in forms_by_id:
        v = forms_by_id[fid]
    if not v:                buckets["no_value"] += 1
    elif v in GENERIC:       buckets["generic_only"] += 1
    else:                    buckets["real_value"] += 1

print(f"contacts with at least one form submission: {len(rows)}")
for k, v in buckets.items():
    print(f"  {k}: {v}")

# Also count contacts with NO form submission
total_contacts = con.execute("SELECT COUNT(*) FROM fact_contacts").fetchone()[0]
contacts_with_subs = len(rows)
print(f"\ntotal contacts in DB: {total_contacts}")
print(f"contacts with NO form submission: {total_contacts - contacts_with_subs}")
