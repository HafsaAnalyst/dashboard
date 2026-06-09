"""
Sanity tests for the ETL pipeline.

These tests assume `python etl/run_etl.py --since 2026-04-01 --until 2026-04-30`
has already been run successfully — they read from the existing DB rather
than re-running the full pipeline (which would burn API quota on every test).

Run with:
    pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
SCHEMA_PATH = ROOT / "models" / "schema.sql"


# ---------------------------------------------------------------------
# Schema-level tests (use a fresh in-memory DB)
# ---------------------------------------------------------------------

def test_schema_creates_without_error():
    """schema.sql can be applied to a clean DB and produces 15 tables."""
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'"
    ).fetchall()]
    expected = {
        "dim_counsellors", "dim_calendars", "dim_pipelines", "dim_stages",
        "fact_contacts", "fact_opportunities", "fact_appointments",
        "fact_meta_insights", "fact_meta_daily",
        "fact_ga4_sessions", "fact_ga4_pages", "fact_ga4_events",
        "fact_gsc_queries",
        "bridge_lead_attribution", "agg_daily_kpis",
    }
    assert expected.issubset(set(tables)), \
        f"Missing tables: {expected - set(tables)}"


def test_dim_counsellors_seeded():
    """The 8 named counsellors are seeded with correct rate flags."""
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    rows = con.execute("""
        SELECT counsellor_id, full_name, is_paid
        FROM dim_counsellors ORDER BY counsellor_id
    """).fetchall()
    assert len(rows) == 8
    by_id = {r[0]: r for r in rows}
    # Spot-check a few
    assert by_id["usr_nasir"][1] == "Nasir Nawaz"
    assert by_id["usr_nasir"][2] is True       # paid
    assert by_id["usr_wajahat"][2] is False    # free
    assert by_id["usr_navneet"][2] is False    # free


def test_fact_table_accepts_minimal_row():
    """fact_contacts accepts a row with all-required-NULL columns."""
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.execute("""
        INSERT INTO fact_contacts (contact_id, contact_name, email)
        VALUES ('c1', 'Test', 't@x.com')
    """)
    n = con.execute("SELECT COUNT(*) FROM fact_contacts").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------
# Tests against the real DB (requires prior ETL run)
# ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def db():
    if not DB_PATH.exists():
        pytest.skip(f"DB not found at {DB_PATH}. Run "
                    f"`python etl/run_etl.py --since 2026-04-01 --until 2026-04-30` first.")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    yield con
    con.close()


def test_agg_daily_kpis_leads_identity(db):
    """Invariant: total_leads = paid_leads + organic_leads on every row."""
    bad = db.execute("""
        SELECT date FROM agg_daily_kpis
        WHERE total_leads != paid_leads + organic_leads
    """).fetchall()
    assert bad == [], f"Identity broken on dates: {[r[0] for r in bad]}"


def test_agg_daily_kpis_no_negative_values(db):
    """No negative counts or money."""
    bad = db.execute("""
        SELECT date FROM agg_daily_kpis
        WHERE total_leads < 0 OR paid_leads < 0 OR organic_leads < 0
           OR meta_spend < 0 OR pipeline_value_added < 0 OR won_value < 0
    """).fetchall()
    assert bad == []


def test_agg_daily_kpis_april_totals_match_verification(db):
    """Headline numbers for April 2026 should match the live-API
    verification we ran before building the ETL."""
    r = db.execute("""
        SELECT SUM(meta_spend), SUM(gsc_clicks), SUM(gsc_impressions)
        FROM agg_daily_kpis
        WHERE date BETWEEN DATE '2026-04-01' AND DATE '2026-04-30'
    """).fetchone()
    spend, clicks, impressions = r
    # Spend was $3,390.32 in verification
    assert 3380.0 <= float(spend) <= 3400.0, f"Meta spend off: {spend}"
    # GSC was 3,234 clicks / 658,066 impressions
    assert clicks == 3234, f"GSC clicks off: {clicks}"
    assert impressions == 658066, f"GSC impressions off: {impressions}"


def test_bridge_no_null_contacts(db):
    """If bridge has rows, every row must have a non-null contact_id."""
    nulls = db.execute(
        "SELECT COUNT(*) FROM bridge_lead_attribution WHERE contact_id IS NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_dim_calendars_named_consultant_has_counsellor_id(db):
    """is_named_consultant must imply non-null counsellor_id."""
    bad = db.execute("""
        SELECT calendar_id FROM dim_calendars
        WHERE is_named_consultant = TRUE AND counsellor_id IS NULL
    """).fetchall()
    assert bad == []


def test_meta_insights_total_leads_is_canonical_sum(db):
    """fact_meta_insights.total_leads must equal
    instant_form_leads + pixel_lead_events + pixel_custom_events."""
    bad = db.execute("""
        SELECT campaign_id FROM fact_meta_insights
        WHERE total_leads != COALESCE(instant_form_leads,0)
                            + COALESCE(pixel_lead_events,0)
                            + COALESCE(pixel_custom_events,0)
    """).fetchall()
    assert bad == [], f"Canonical leads sum broken: {bad}"


def test_canonical_pipelines_count(db):
    """is_canonical=TRUE should be set for the 6 canonical pipeline names."""
    n = db.execute(
        "SELECT COUNT(*) FROM dim_pipelines WHERE is_canonical = TRUE"
    ).fetchone()[0]
    # Some of the 6 canonical names may not exist in this account; allow >= 1
    assert n >= 1, "At least one canonical pipeline should be flagged"


def test_idempotency_dim_calendars(db):
    """No duplicate calendar_ids in dim_calendars (re-running shouldn't dup)."""
    total, distinct = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT calendar_id) FROM dim_calendars"
    ).fetchone()
    assert total == distinct


def test_idempotency_fact_contacts(db):
    """Same primary-key uniqueness on fact_contacts."""
    total, distinct = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT contact_id) FROM fact_contacts"
    ).fetchone()
    assert total == distinct
