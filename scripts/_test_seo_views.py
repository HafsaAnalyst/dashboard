"""Sanity check: parse tab_cards.sql with the same regex the dashboard uses,
and try executing the new SEO views with a sample window."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

SQL = (ROOT / "dashboards" / "sql" / "tab_cards.sql").read_text(encoding="utf-8")
COUN_SQL = (ROOT / "dashboards" / "sql" / "counsellor_cards.sql").read_text(encoding="utf-8")
EXEC_SQL = (ROOT / "dashboards" / "sql" / "executive_cards.sql").read_text(encoding="utf-8")
text = EXEC_SQL + "\n" + COUN_SQL + "\n" + SQL

pattern = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
    re.DOTALL | re.IGNORECASE,
)
queries = {m.group(1): m.group(2).strip().rstrip(';') for m in pattern.finditer(text)}
print(f"parsed views: {len(queries)}")
for name in sorted(queries):
    if "seo" in name or "counsellor_calendars" in name:
        print(f"  {name}")

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
binds = {"since": "2026-05-01", "until": "2026-05-27"}

print("\n--- vw_seo_website_leads_per_city ---")
try:
    df = con.execute(queries["vw_seo_website_leads_per_city"], binds).fetchdf()
    print(df.to_string(index=False))
except Exception as e:
    print("FAILED:", e)

print("\n--- vw_seo_website_leads_per_counsellor ---")
try:
    df = con.execute(queries["vw_seo_website_leads_per_counsellor"], binds).fetchdf()
    print(df.to_string(index=False))
except Exception as e:
    print("FAILED:", e)

print("\n--- vw_seo_lead_activity_breakdown (first 5 rows) ---")
try:
    df = con.execute(queries["vw_seo_lead_activity_breakdown"], binds).fetchdf()
    print(f"rows: {len(df)}")
    print(df.head().to_string(index=False))
except Exception as e:
    print("FAILED:", e)
