"""Compare computed value to stored value to estimate write count."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from connectors import ghl

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
forms = ghl.fetch_forms()
forms_by_id = {f["id"]: f["name"] for f in forms if f.get("id") and f.get("name")}

# Current stored values from our local mirror
stored = dict(con.execute(
    "SELECT contact_id, latest_source_value FROM fact_contact_latest_source"
).fetchall())

# Latest submission per contact
rows = con.execute("""
  WITH ranked AS (
    SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
           session_source, event_source,
           ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
    FROM fact_form_submissions WHERE contact_id IS NOT NULL
  ) SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
           session_source, event_source FROM ranked WHERE rn=1
""").fetchall()

GENERIC = {"Social media","Paid Social","Direct traffic","Organic Search",
           "Referral","Email","SMS","Direct",""}

stats = {"unchanged": 0, "to_write_real": 0, "to_write_overwrite_generic": 0,
         "to_write_new": 0, "skip_no_real_value": 0}

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
    if not v or v in GENERIC:
        stats["skip_no_real_value"] += 1
        continue
    prev = stored.get(cid)
    if prev == v:
        stats["unchanged"] += 1
    elif prev is None or prev == "":
        stats["to_write_new"] += 1
    elif prev in GENERIC:
        stats["to_write_overwrite_generic"] += 1
    else:
        stats["to_write_real"] += 1

print(f"contacts with form submissions: {len(rows)}")
for k, v in stats.items():
    print(f"  {k:32s} {v}")
total_writes = (stats["to_write_real"] + stats["to_write_overwrite_generic"]
                + stats["to_write_new"])
print(f"\nTOTAL contacts needing GHL write: {total_writes}")
print(f"Estimated runtime: ~{total_writes * 0.5 / 60:.0f} min")
