"""For each email, list ALL opportunities (pipeline, stage, created_at, status)
and flag duplicates."""
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
EMAILS = sorted(set(e.strip().lower() for e in EMAILS))

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

sql = """
SELECT
    LOWER(c.email)                AS email,
    o.opportunity_id,
    p.pipeline_name               AS pipeline,
    s.stage_name                  AS stage,
    o.status,
    CAST(o.created_at AS DATE)    AS opp_created
FROM fact_contacts c
LEFT JOIN fact_opportunities o ON o.contact_id = c.contact_id
LEFT JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
LEFT JOIN dim_stages s    ON s.stage_id    = o.stage_id
WHERE LOWER(c.email) = ANY(?)
ORDER BY LOWER(c.email), o.created_at DESC
"""
df = con.execute(sql, [EMAILS]).fetchdf()

# Per-email opp count
counts = df.dropna(subset=["opportunity_id"]).groupby("email").size().reset_index(
    name="opp_count").sort_values("opp_count", ascending=False)

print("=== OPPORTUNITY COUNT PER EMAIL ===")
print(counts.to_string(index=False))
print()

dup_emails = counts[counts["opp_count"] > 1]["email"].tolist()
print(f"\n=== EMAILS WITH MULTIPLE OPPORTUNITIES ({len(dup_emails)}) ===\n")

for em in dup_emails:
    rows = df[df["email"] == em]
    print(f"--- {em} ({len(rows)} opps) ---")
    print(rows[["pipeline", "stage", "status", "opp_created", "opportunity_id"]].to_string(index=False))
    # Check same-pipeline duplicates
    same_pipe = rows.groupby("pipeline").size()
    same_pipe_dups = same_pipe[same_pipe > 1]
    if not same_pipe_dups.empty:
        print(f"  [SAME-PIPELINE DUPLICATE] {dict(same_pipe_dups)}")
    print()
