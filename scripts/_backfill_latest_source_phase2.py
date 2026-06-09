"""Phase 2: fill Latest Source for contacts that the form-submission sync
COULDN'T resolve. Uses (in order of preference):

  1. Latest survey submission         → survey name (e.g. "Points Calculator")
  2. Latest appointment               → calendar name (e.g. "Nasir Nawaz - MARA Certified")
  3. Contact's `lastAttributionSource.campaign`  → campaign string
  4. Contact's `lastAttributionSource.utmSource` → e.g. "facebook" (last resort)
  5. Skip (no signal worth writing)

Skips contacts that already have a real (non-generic) Latest Source.

Resumable via log CSV. Rate-limited. Safe to re-run.

PREREQ: full ETL backfill (run_etl.py --full) must have completed first so
fact_appointments / fact_form_submissions / fact_contact_latest_source are
up to date.
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
logger = logging.getLogger("phase2")

from connectors import ghl  # noqa: E402

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
LOG_PATH = ROOT / "scripts" / "_backfill_latest_source_phase2.log.csv"

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


def _append_log(contact_id, value, source, contact_ok, opp_count, opp_ok):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["contact_id", "value", "source", "contact_ok",
                        "opp_count", "opp_ok"])
        w.writerow([contact_id, value, source, contact_ok, opp_count, opp_ok])


def _retry(fn, *args):
    for attempt in range(4):
        try:
            return fn(*args)
        except (requests.exceptions.ConnectionError, ConnectionResetError):
            time.sleep(2 ** attempt)
    return False


def main():
    # Lookups: forms + surveys + calendars
    forms_by_id = {f["id"]: f["name"]
                   for f in ghl.fetch_forms() if f.get("id") and f.get("name")}
    surveys_by_id = {s["id"]: s["name"]
                     for s in ghl.fetch_surveys() if s.get("id") and s.get("name")}
    logger.info("Loaded %d form names, %d survey names",
                len(forms_by_id), len(surveys_by_id))

    con = duckdb.connect(str(DB_PATH), read_only=True)
    # Calendar names from dim_calendars
    cal_rows = con.execute(
        "SELECT calendar_id, calendar_name FROM dim_calendars"
    ).fetchall()
    cal_by_id = {cid: name for cid, name in cal_rows if cid and name}
    logger.info("Loaded %d calendar names", len(cal_by_id))

    # Already-set Latest Source values (skip contacts whose value is good)
    stored = dict(con.execute(
        "SELECT contact_id, latest_source_value FROM fact_contact_latest_source"
    ).fetchall())

    # All contacts (post-full-ETL we should have ~8000)
    contacts = con.execute(
        "SELECT contact_id FROM fact_contacts"
    ).fetchall()
    logger.info("DB contacts: %d", len(contacts))

    # Latest form submission per contact (with formId fallback)
    form_rows = con.execute("""
        WITH ranked AS (
          SELECT contact_id, form_id, form_name, event_form_name, campaign, utm_content,
                 session_source, event_source, submitted_at,
                 ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) AS rn
          FROM fact_form_submissions WHERE contact_id IS NOT NULL
        ) SELECT contact_id, form_id, form_name, event_form_name, campaign,
                 utm_content, session_source, event_source, submitted_at
        FROM ranked WHERE rn=1
    """).fetchall()
    form_by_cid = {r[0]: r for r in form_rows}

    # Latest appointment per contact
    app_rows = con.execute("""
        WITH ranked AS (
          SELECT contact_id, calendar_id, start_time,
                 ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY start_time DESC) AS rn
          FROM fact_appointments WHERE contact_id IS NOT NULL
        ) SELECT contact_id, calendar_id, start_time FROM ranked WHERE rn=1
    """).fetchall()
    app_by_cid = {r[0]: r for r in app_rows}
    con.close()

    # Form-derived value (matches sync_latest_source logic)
    def from_form(cid):
        r = form_by_cid.get(cid)
        if not r: return None
        _, fid, fn, efn, camp, utm, ss, es, _ = r
        v = ""
        if camp and utm:   v = f"{camp} -- {utm}"
        elif camp:         v = camp
        elif fn:           v = fn
        elif efn:          v = efn
        elif ss:           v = ss
        elif es:           v = es
        if v in GENERIC and fid in forms_by_id:
            v = forms_by_id[fid]
        return v if v and v not in GENERIC else None

    # Survey: query latest survey submission per contact
    # (Survey data not yet in DB, fetch from API for contacts that need it.)
    # We'll pull all survey submissions once and index by contact_id.
    survey_subs = ghl.fetch_survey_submissions("2024-01-01")
    surv_by_cid = {}
    for s in survey_subs:
        cid = s.get("contactId")
        if not cid: continue
        # Keep the latest one
        prev = surv_by_cid.get(cid)
        if not prev or s.get("createdAt", "") > prev.get("createdAt", ""):
            surv_by_cid[cid] = s
    logger.info("Survey submissions indexed: %d contacts", len(surv_by_cid))

    def from_survey(cid):
        s = surv_by_cid.get(cid)
        if not s: return None
        sid = s.get("surveyId")
        if sid and sid in surveys_by_id:
            return surveys_by_id[sid]
        return None

    def from_appointment(cid):
        r = app_by_cid.get(cid)
        if not r: return None
        _, calid, _ = r
        return cal_by_id.get(calid)

    # Build TODO list: contacts whose stored Latest Source is empty/generic,
    # and we have a non-form fallback that produces a real value.
    todo = []  # (cid, value, source_type)
    for (cid,) in contacts:
        cur = stored.get(cid, "")
        if cur and cur not in GENERIC:
            continue  # already has a real value

        # Form first (in case it wasn't synced yet)
        v = from_form(cid)
        src = "form"
        if not v:
            v = from_survey(cid); src = "survey"
        if not v:
            v = from_appointment(cid); src = "appointment"
        if not v:
            continue  # nothing to write
        if cur == v:
            continue
        todo.append((cid, v, src))

    done = _load_done()
    todo = [t for t in todo if t[0] not in done]
    logger.info("Candidates: %d (skipping %d already in log)",
                len(todo), len(done))
    by_source = {}
    for _, _, s in todo:
        by_source[s] = by_source.get(s, 0) + 1
    logger.info("By source: %s", by_source)

    n_ok = n_opp_total = n_opp_ok = 0
    for i, (cid, val, src) in enumerate(todo, 1):
        ok = _retry(ghl.update_contact_custom_field,
                    cid, ghl.LATEST_SOURCE_CONTACT_FIELD, val)
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
            if _retry(ghl.update_opportunity_custom_field,
                      o.get("id"), ghl.LATEST_SOURCE_OPP_FIELD, val):
                opp_ok += 1
                n_opp_ok += 1
            n_opp_total += 1
            time.sleep(0.12)

        _append_log(cid, val, src, ok, opp_count, opp_ok)
        if i % 20 == 0 or i == len(todo):
            logger.info("[%d/%d] last: %s -> %r (%s, opps %d/%d)",
                        i, len(todo), cid, val, src, opp_ok, opp_count)

    logger.info("DONE. contacts %d ok / %d, opps %d ok / %d",
                n_ok, len(todo), n_opp_ok, n_opp_total)


if __name__ == "__main__":
    main()
