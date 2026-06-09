"""One-shot ingest of /payments/transactions into fact_payments."""
import sys, logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

from connectors import ghl
from etl import normalize
from etl.run_etl import upsert_df, init_database

con = init_database()
try:
    raw = ghl.fetch_payments("2024-01-01")
    print(f"fetched: {len(raw)}")
    df = normalize.normalize_payments(raw)
    n = upsert_df(con, "fact_payments", df, "transaction_id")
    print(f"fact_payments: {n} rows")
finally:
    con.close()
