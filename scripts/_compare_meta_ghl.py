"""Compare Meta Leads vs GHL Leads totals after the new counting."""
import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

text = open(ROOT / "dashboards" / "sql" / "tab_cards.sql", encoding="utf-8").read()
p = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
    re.DOTALL | re.IGNORECASE,
)
q = {m.group(1): m.group(2).strip().rstrip(";") for m in p.finditer(text)}

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

binds = {"since": "2026-05-01", "until": "2026-05-30", "account": "All"}

df_ghl = con.execute(q["vw_meta_ghl_leads_per_campaign"],
                     {"since": binds["since"], "until": binds["until"]}).fetchdf()
df_ghl = df_ghl.sort_values("ghl_leads", ascending=False)
print("=== GHL Leads per campaign (May 2026) ===")
print(df_ghl[["campaign", "ghl_leads", "ghl_contacts", "bookings"]].head(15).to_string(index=False))
print(f"\nTOTAL ghl_leads: {int(df_ghl['ghl_leads'].sum())}")

df_meta = con.execute(q["vw_meta_per_campaign"], binds).fetchdf()
print(f"\n=== Meta side (May 2026) ===")
print(f"TOTAL meta_leads: {int(df_meta['meta_leads'].sum())}")
total_spend = float(df_meta["spend"].sum())
print(f"TOTAL spend: ${total_spend:,.0f}")

# Side by side per campaign (top 15)
print(f"\n=== Side-by-side (May 2026) ===")
m = df_meta[["campaign_name", "meta_leads"]].rename(columns={"campaign_name": "campaign"})
g = df_ghl[["campaign", "ghl_leads"]]
merged = m.merge(g, on="campaign", how="outer").fillna(0)
merged["meta_leads"] = merged["meta_leads"].astype(int)
merged["ghl_leads"]  = merged["ghl_leads"].astype(int)
merged["gap"]        = merged["meta_leads"] - merged["ghl_leads"]
merged = merged.sort_values("meta_leads", ascending=False).head(15)
print(merged.to_string(index=False))
