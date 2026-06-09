import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

text = (ROOT / "dashboards" / "sql" / "counsellor_cards.sql").read_text(encoding="utf-8")
pattern = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
    re.DOTALL | re.IGNORECASE,
)
queries = {m.group(1): m.group(2).strip().rstrip(';') for m in pattern.finditer(text)}

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
binds = {"since": "2026-05-24", "until": "2026-05-30", "city": "All"}

print("--- vw_counsellor_appointments_detail (May 24-30) ---")
df = con.execute(queries["vw_counsellor_appointments_detail"], {"since": binds["since"], "until": binds["until"]}).fetchdf()
print(f"rows: {len(df)}")
print(df.head(15)[["email","appointment_status","canonical_outcome","amount_paid","latest_source","start_time"]].to_string(index=False))

print("\n--- vw_counsellors (May 24–30, start_time-based) ---")
df = con.execute(queries["vw_counsellors"], binds).fetchdf()
print(df.to_string(index=False))
