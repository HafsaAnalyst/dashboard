"""Investigate why Kajal / Wajahad / Saurab / Navneet appointment counts
differ from what's visible in GHL's calendar view (week May 24-30, 2026)."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

COUNSELLOR_CALS = {
    "Kajal":   ["1FgpIJPxw6RWveeJLsb8", "RF7bh7b3avrzStoTE8ho"],
    "Wajahad": ["4HLkV0BSHX7EvJ3jniC9", "hsCSqcYHrXwL55NffEFi"],
    "Saurab":  ["4mKKf1IPwIq50N4OzOTI", "vjmOhJPIT4pAPzCyCmdT"],
    "Navneet": ["XJS0nt92447DgYSmxVkP", "hkL937P7e6XTzy58dOZ7"],
}

SINCE = "2026-05-24"
UNTIL = "2026-05-30"

for name, cals in COUNSELLOR_CALS.items():
    print(f"\n=== {name} ({cals}) — May 24–30 ===")
    rows = con.execute(f"""
        SELECT appointment_id, calendar_id,
               CAST(start_time AS TIMESTAMP) AS start_time,
               appointment_status,
               canonical_outcome,
               DAYOFWEEK(CAST(start_time AS DATE)) AS dow
        FROM fact_appointments
        WHERE calendar_id = ANY(?)
          AND CAST(start_time AS DATE) BETWEEN '{SINCE}' AND '{UNTIL}'
        ORDER BY start_time
    """, [cals]).fetchall()
    print(f"  total rows in window: {len(rows)}")
    by_status = {}
    weekend_rows = []
    for r in rows:
        st = (r[3] or "").lower()
        by_status[st] = by_status.get(st, 0) + 1
        if r[5] in (0, 6):
            weekend_rows.append(r)
    print(f"  by status: {by_status}")
    if weekend_rows:
        print(f"  WEEKEND rows ({len(weekend_rows)}): {[(r[2], r[3]) for r in weekend_rows]}")
    for r in rows:
        print(f"    {r[2]} dow={r[5]} status={r[3]} outcome={r[4]} cal={r[1]}")

# Also check what vw_counsellors returns
print("\n=== vw_counsellors output ===")
text = (ROOT / "dashboards" / "sql" / "counsellor_cards.sql").read_text(encoding="utf-8")
import re
pat = re.compile(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)", re.DOTALL | re.IGNORECASE)
q = {m.group(1): m.group(2).strip().rstrip(";") for m in pat.finditer(text)}
df = con.execute(q["vw_counsellors"], {"since": SINCE, "until": UNTIL, "city": "All"}).fetchdf()
for _, r in df.iterrows():
    for name, cals in COUNSELLOR_CALS.items():
        if r["calendar_id"] in cals:
            print(f"  {name} cal={r['calendar_id']}: appts={r['appointments']} confirmed={r['confirmed']} showed={r['showed']} noshow={r['noshow']} cancelled={r['cancelled']}")
