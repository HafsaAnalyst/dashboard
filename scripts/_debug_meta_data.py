"""Debug what's in fact_meta_daily for the suspicious campaigns."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import duckdb

con = duckdb.connect(str(ROOT / "data" / "migration_dashboard.duckdb"), read_only=True)

print("=== fact_meta_daily distinct columns ===")
print([r[0] for r in con.execute("DESCRIBE fact_meta_daily").fetchall()])
print()

print("=== Per-campaign aggregate (May 2026) ===")
df = con.execute("""
    SELECT campaign_name, account_label,
           SUM(impressions) AS impr,
           SUM(clicks)      AS clk,
           SUM(spend)       AS spend,
           SUM(result_count) AS results,
           SUM(result_count) FILTER (
             WHERE result_event IN ('lead','offsite_conversion.fb_pixel_lead','offsite_conversion.fb_pixel_custom')
           ) AS lead_count,
           STRING_AGG(DISTINCT result_event, ', ') AS events
    FROM fact_meta_daily
    WHERE date BETWEEN '2026-05-01' AND '2026-05-30'
    GROUP BY 1, 2
    ORDER BY impr DESC
""").fetchdf()
print(df.to_string(index=False))
