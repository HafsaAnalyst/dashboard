"""Bulk-fix Latest Source for contacts whose latest form submission has
form_name='' but form_id is set. Resolves form_id -> form_name via /forms/
lookup, then writes the value to the contact + every opportunity.

Resumable: writes a log CSV; if rerun, skips contact_ids already in the log.
Rate-limited: 0.12s between calls; retries on ConnectionResetError with
exponential backoff.
"""
import csv
import logging
import sys
import time
from pathlib import Path

import duckdb
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backfill")

from connectors import ghl  # noqa: E402

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
LOG_PATH = ROOT / "scripts" / "_backfill_latest_source_formid_fallback.log.csv"

GENERIC = {"Social media", "Paid Social", "Direct traffic", "Organic Search",
           "Referral", "Email", "SMS", "Direct", ""}


def _load_done() -> set[str]:
    if not LOG_PATH.exists():
        return set()
    done = set()
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add(row["contact_id"])
    return done


def _append_log(contact_id, value, contact_ok, opp_count, opp_ok):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["contact_id", "value", "contact_ok", "opp_count", "opp_ok"])
        w.writerow([contact_id, value, contact_ok, opp_count, opp_ok])


def _retry_write_contact(cid, fid, val):
    for attempt in range(4):
        try:
            return ghl.update_contact_custom_field(cid, fid, val)
        except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
            wait = 2 ** attempt
            logger.warning("  contact %s connection err: %s — retrying in %ds",
                           cid, type(e).__name__, wait)
            time.sleep(wait)
    return False


def _retry_write_opp(oid, fid, val):
    for attempt in range(4):
        try:
            return ghl.update_opportunity_custom_field(oid, fid, val)
        except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
            wait = 2 ** attempt
            logger.warning("  opp %s connection err: %s — retrying in %ds",
                           oid, type(e).__name__, wait)
            time.sleep(wait)
    return False


def main():
    # 1. Load forms lookup
    forms = ghl.fetch_forms()
    forms_by_id = {f["id"]: f["name"] for f in forms if f.get("id") and f.get("name")}
    logger.info("Loaded %d form-name lookups", len(forms_by_id))

    # 2. Pull candidates from DB
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute("""
        WITH ranked AS (
          SELECT contact_id, form_id, form_name, campaign, utm_content,
                 session_source, event_source,
                 ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
          FROM fact_form_submissions
          WHERE contact_id IS NOT NULL
        )
        SELECT contact_id, form_id, form_name, campaign, utm_content,
               session_source, event_source
        FROM ranked
        WHERE rn=1
          AND COALESCE(form_name,'')=''
          AND COALESCE(campaign,'')=''
          AND form_id IS NOT NULL
    """).fetchall()
    con.close()
    logger.info("Found %d candidate contacts", len(rows))

    done = _load_done()
    logger.info("Skipping %d already-processed contacts from log", len(done))

    todo = [r for r in rows if r[0] not in done]
    logger.info("To process this run: %d contacts", len(todo))

    n_contact_ok = n_opp_total = n_opp_ok = 0
    for i, (cid, fid, fn, camp, utm, ss, es) in enumerate(todo, 1):
        # Compute value (same precedence as sync_latest_source)
        val = ""
        if camp and utm:   val = f"{camp} -- {utm}"
        elif camp:         val = camp
        elif fn:           val = fn
        elif ss:           val = ss
        elif es:           val = es
        if val in GENERIC and fid in forms_by_id:
            val = forms_by_id[fid]

        if not val or val in GENERIC:
            logger.info("[%d/%d] %s — no resolvable value, skipping",
                        i, len(todo), cid)
            _append_log(cid, "(none)", False, 0, 0)
            continue

        # Write contact field
        ok = _retry_write_contact(cid, ghl.LATEST_SOURCE_CONTACT_FIELD, val)
        if ok:
            n_contact_ok += 1
        time.sleep(0.12)

        # Write field on each opp
        opp_count = opp_ok = 0
        try:
            opps = ghl.fetch_contact_opportunities(cid)
        except (requests.exceptions.ConnectionError, ConnectionResetError):
            time.sleep(2)
            try:
                opps = ghl.fetch_contact_opportunities(cid)
            except Exception:
                opps = []
        for o in opps:
            opp_count += 1
            time.sleep(0.12)
            if _retry_write_opp(o.get("id"), ghl.LATEST_SOURCE_OPP_FIELD, val):
                opp_ok += 1
                n_opp_ok += 1
            n_opp_total += 1
            time.sleep(0.12)

        _append_log(cid, val, ok, opp_count, opp_ok)
        if i % 10 == 0 or i == len(todo):
            logger.info("[%d/%d] last: %s -> %r (opps %d/%d)",
                        i, len(todo), cid, val, opp_ok, opp_count)

    logger.info("DONE. contacts %d ok / %d, opps %d ok / %d",
                n_contact_ok, len(todo), n_opp_ok, n_opp_total)


if __name__ == "__main__":
    main()
