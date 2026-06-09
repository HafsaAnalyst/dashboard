"""Bulk-fix Latest Source for contacts whose current stored value is NULL/empty
or a generic source ('Social media', 'Paid Social', etc.) when we have a better
real value (form name or campaign) available from their latest form submission.

Resumable via log CSV. Rate-limited. Retries on connection errors.

Also updates fact_contact_latest_source local mirror after each successful write
so future runs see the new value and skip already-correct contacts.
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
logger = logging.getLogger("backfill_null_generic")

from connectors import ghl  # noqa: E402

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
LOG_PATH = ROOT / "scripts" / "_backfill_latest_source_null_and_generic.log.csv"

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


def _append_log(contact_id, value, contact_ok, opp_count, opp_ok, action):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["contact_id", "value", "contact_ok", "opp_count",
                        "opp_ok", "action"])
        w.writerow([contact_id, value, contact_ok, opp_count, opp_ok, action])


def _retry_write_contact(cid, fid, val):
    for attempt in range(4):
        try:
            return ghl.update_contact_custom_field(cid, fid, val)
        except (requests.exceptions.ConnectionError, ConnectionResetError):
            time.sleep(2 ** attempt)
    return False


def _retry_write_opp(oid, fid, val):
    for attempt in range(4):
        try:
            return ghl.update_opportunity_custom_field(oid, fid, val)
        except (requests.exceptions.ConnectionError, ConnectionResetError):
            time.sleep(2 ** attempt)
    return False


def main():
    forms = ghl.fetch_forms()
    forms_by_id = {f["id"]: f["name"] for f in forms if f.get("id") and f.get("name")}
    logger.info("Loaded %d form-name lookups", len(forms_by_id))

    con = duckdb.connect(str(DB_PATH), read_only=True)
    stored = dict(con.execute(
        "SELECT contact_id, latest_source_value FROM fact_contact_latest_source"
    ).fetchall())

    rows = con.execute("""
        WITH ranked AS (
          SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
                 session_source, event_source,
                 ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
          FROM fact_form_submissions WHERE contact_id IS NOT NULL
        ) SELECT contact_id, form_id, form_name, event_form_name, campaign,
                 utm_content, session_source, event_source
        FROM ranked WHERE rn=1
    """).fetchall()
    con.close()

    todo = []
    for cid, fid, fn, efn, camp, utm, ss, es in rows:
        v = ""
        if camp and utm:  v = f"{camp} -- {utm}"
        elif camp:        v = camp
        elif fn:          v = fn
        elif efn:         v = efn
        elif ss:          v = ss
        elif es:          v = es
        if v in GENERIC and fid in forms_by_id:
            v = forms_by_id[fid]
        if not v or v in GENERIC:
            continue
        prev = stored.get(cid)
        if prev == v:
            continue
        action = ("new" if prev in (None, "") else
                  "overwrite_generic" if prev in GENERIC else "overwrite_real")
        todo.append((cid, v, action))

    done = _load_done()
    todo = [t for t in todo if t[0] not in done]
    logger.info("Candidates: %d  (skipping %d already in log)",
                len(todo), len(done))

    # Idempotency is handled via the log CSV (LOG_PATH) — we don't write to
    # the DB here to avoid clashing with the dashboard's read-only connection.
    # The local fact_contact_latest_source mirror will reconcile on the next
    # full ETL run.

    n_ok = n_opp_total = n_opp_ok = 0
    for i, (cid, val, action) in enumerate(todo, 1):
        ok = _retry_write_contact(cid, ghl.LATEST_SOURCE_CONTACT_FIELD, val)
        if ok:
            n_ok += 1
        time.sleep(0.12)

        opp_count = opp_ok = 0
        try:
            opps = ghl.fetch_contact_opportunities(cid)
        except Exception:
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

        _append_log(cid, val, ok, opp_count, opp_ok, action)
        if i % 10 == 0 or i == len(todo):
            logger.info("[%d/%d] last: %s -> %r (%s, opps %d/%d)",
                        i, len(todo), cid, val, action, opp_ok, opp_count)

    logger.info("DONE. contacts %d ok / %d, opps %d ok / %d",
                n_ok, len(todo), n_opp_ok, n_opp_total)


if __name__ == "__main__":
    main()
