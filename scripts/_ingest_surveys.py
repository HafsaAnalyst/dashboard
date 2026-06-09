"""One-shot ingest of /surveys/submissions into fact_survey_submissions.
Run once after schema changes; future runs handled by run_etl.py."""
import sys
import logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

import duckdb
from connectors import ghl
from etl import normalize
from etl.run_etl import upsert_df, init_database

con = init_database()
try:
    surveys_list = ghl.fetch_surveys()
    surveys_by_id = {s["id"]: s["name"] for s in surveys_list if s.get("id") and s.get("name")}
    print(f"surveys: {len(surveys_by_id)}")
    surv_raw = ghl.fetch_survey_submissions("2024-01-01")
    surv_df = normalize.normalize_survey_submissions(surv_raw, surveys_by_id)
    n = upsert_df(con, "fact_survey_submissions", surv_df, "submission_id")
    print(f"fact_survey_submissions: {n} rows")
finally:
    con.close()
