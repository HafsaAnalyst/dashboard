"""Quick test of vw_card_total_leads_secondary to find why 'tag' is missing."""
import re, sys
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
binds = {"since": "2026-05-01", "until": "2026-05-30",
         "prior_since": "2026-04-01", "prior_until": "2026-04-30",
         "city": "All"}

body = queries.get("vw_card_total_leads_secondary")
print("body length:", len(body) if body else None)
print("first 500 chars:", body[:500] if body else None)
print()
df = con.execute(body, binds).fetchdf()
print(f"rows: {len(df)}")
print("columns:", df.columns.tolist())
print(df.to_string(index=False))
