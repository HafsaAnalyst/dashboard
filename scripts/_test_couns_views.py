"""Sanity check the new cohort-attributed counsellor views."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

text = (
    (ROOT / "dashboards" / "sql" / "executive_cards.sql").read_text(encoding="utf-8") + "\n"
  + (ROOT / "dashboards" / "sql" / "counsellor_cards.sql").read_text(encoding="utf-8") + "\n"
  + (ROOT / "dashboards" / "sql" / "tab_cards.sql").read_text(encoding="utf-8")
)
pattern = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
    re.DOTALL | re.IGNORECASE,
)
queries = {m.group(1): m.group(2).strip().rstrip(';') for m in pattern.finditer(text)}

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
binds = {"since": "2026-05-01", "until": "2026-05-27", "city": "All"}

print("--- vw_counsellors (May 2026) ---")
df = con.execute(queries["vw_counsellors"], binds).fetchdf()
print(df.to_string(index=False))

print("\n--- vw_counsellors_daily (sample) ---")
df = con.execute(queries["vw_counsellors_daily"], binds).fetchdf()
print(f"rows: {len(df)}")
print(df.head(8).to_string(index=False))
