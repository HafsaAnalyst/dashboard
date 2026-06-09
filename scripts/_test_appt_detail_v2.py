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
q = {m.group(1): m.group(2).strip().rstrip(';') for m in pattern.finditer(text)}

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

for since, until, label in [
    ("2026-05-01", "2026-05-15", "May 1-15"),
    ("2026-02-01", "2026-05-30", "Feb-May"),
]:
    df = con.execute(q["vw_counsellor_appointments_detail"],
                     {"since": since, "until": until}).fetchdf()
    empty = df["latest_source"].fillna("").eq("").sum()
    print(f"{label}: {len(df)} rows, {empty} empty Latest Source")
    faizan = df[df["email"] == "muhammadfaizan6335@gmail.com"]
    if not faizan.empty:
        print(f"  Faizan: {faizan.iloc[0]['latest_source']!r}")

print("\nSample of resolved:")
df = con.execute(q["vw_counsellor_appointments_detail"],
                 {"since": "2026-05-20", "until": "2026-05-29"}).fetchdf()
print(df[["email", "latest_source"]].head(20).to_string(index=False))
