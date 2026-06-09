"""Look up pipeline, stage, contact created date for a given email list."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

EMAILS = [
    "mehran123123@gmail.com", "jawadali224400@gmail.com",
    "homaayoonhassan0480@gmail.com", "farooqrasheed25@gmail.com",
    "ehsansunny5666@gmail.com", "navi.tung007@gmail.com",
    "hassansohail20@gmail.com", "zainammar396@gmail.com",
    "mohsinraza3324090@gmail.com", "nabeel.auth@gmail.com",
    "ahsanzaib51214@gmail.com", "zainmuhammad626@gmail.com",
    "hussainsyed1412@gmail.com", "nomihassan82@gmail.com",
    "tehseenshah36@gmail.com", "maazbinkaleem@gmail.com",
    "mahal20kj@gmail.com", "usamakalyar6071@gmail.com",
    "kaleemullahbhutta10@gmail.com", "nodirmaksudov@gmail.com",
    "arhamarif06@gmail.com", "ihsankhan2662@gmail.com",
    "sardar1232018@gmail.com", "shahalidepar2@gmail.com",
    "asadbekuzb2008@gmail.com", "ajamesfelix.work@gmail.com",
    "ukatyal4@gmail.com", "alih77200@gmail.com",
    "sherazgujjar1555@gmail.com", "mianabrar0336@gmail.com",
    "usmanchadhar222@icloud.com",
]
EMAILS = [e.strip().lower() for e in EMAILS]

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

# Get one row per email: latest opportunity + pipeline + stage
sql = """
WITH ranked_opp AS (
    SELECT o.contact_id, o.pipeline_id, o.stage_id, o.created_at AS opp_created_at,
           ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.created_at DESC) AS rn
    FROM fact_opportunities o
)
SELECT
    LOWER(c.email)                AS email,
    p.pipeline_name               AS pipeline,
    s.stage_name                  AS stage,
    CAST(c.date_added AS DATE)    AS contact_created
FROM fact_contacts c
LEFT JOIN ranked_opp ro ON ro.contact_id = c.contact_id AND ro.rn = 1
LEFT JOIN dim_pipelines p ON p.pipeline_id = ro.pipeline_id
LEFT JOIN dim_stages s    ON s.stage_id    = ro.stage_id
WHERE LOWER(c.email) = ANY(?)
ORDER BY LOWER(c.email)
"""
df = con.execute(sql, [EMAILS]).fetchdf()

# Find emails not in DB
found = set(df["email"].tolist())
missing = [e for e in EMAILS if e not in found]

print(f"Found: {len(df)} / {len(set(EMAILS))} unique emails")
print(f"Missing: {len(missing)}")
print()
print(df.to_string(index=False))
print()
if missing:
    print("NOT IN DB:")
    for e in missing:
        print(f"  - {e}")
