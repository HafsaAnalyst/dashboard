"""Rebuild agg_daily_kpis from current fact tables. Run after a crashed ETL
so the dashboard has fresh rollups."""
import sys
import logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

import duckdb
from etl.run_etl import build_daily_rollups

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
con = duckdb.connect(str(DB_PATH))
try:
    res = build_daily_rollups(con, "2024-01-01", "2026-05-27")
    print(res)
finally:
    con.close()
