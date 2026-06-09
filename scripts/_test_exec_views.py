"""Verify the new Executive-tab views all run."""
import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

text = (ROOT / "dashboards" / "sql" / "executive_cards.sql").read_text(encoding="utf-8")
p = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)",
    re.DOTALL | re.IGNORECASE,
)
q = {m.group(1): m.group(2).strip().rstrip(";") for m in p.finditer(text)}

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)
binds = {"since": "2026-05-01", "until": "2026-05-30",
         "prior_since": "2026-04-01", "prior_until": "2026-04-30",
         "city": "All"}

for view in ("vw_card_voes", "vw_exec_leads_detail",
             "vw_exec_show_rate_by_counsellor", "vw_exec_revenue_breakdown"):
    body = q.get(view)
    if not body:
        print(f"MISSING: {view}")
        continue
    needed = {k: v for k, v in binds.items() if ("$" + k) in body}
    try:
        df = con.execute(body, needed).fetchdf()
        print(f"\n=== {view} ({len(df)} rows) ===")
        print(df.head(10).to_string(index=False))
    except Exception as e:
        print(f"FAIL {view}: {e}")
