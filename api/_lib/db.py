"""MotherDuck connection + retry, extracted from dashboards/app.py.

Same semantics as the Streamlit app's get_con()/db_exec(), minus Streamlit:
  - the connection is a module-level singleton (Vercel Fluid compute keeps the
    instance warm between invocations, so this is reused like @st.cache_resource)
  - every query runs on its OWN cursor: a DuckDB connection is not safe for
    concurrent queries, and a warm function instance serves concurrent requests
  - transient MotherDuck errors are retried with backoff
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"

_CON: duckdb.DuckDBPyConnection | None = None
_CON_LOCK = threading.Lock()

# Transient MotherDuck server errors — retried rather than surfaced.
_DB_TRANSIENT = (
    "deadline_exceeded", "unavailable", "timed out", "timeout",
    "could not connect", "resource_exhausted", "connection reset",
    "rpc", "503", "try again later",
)

# Errors that mean the MotherDuck plan/trial has lapsed — a distinct, actionable
# failure the UI reports differently from a transient blip.
_DB_LAPSED = ("trial has ended", "select from", "restore access")


class DataSourceUnavailable(Exception):
    """MotherDuck is unreachable. `lapsed` distinguishes a billing/plan problem
    from a transient outage so the UI can show the right message."""

    def __init__(self, message: str, lapsed: bool = False):
        super().__init__(message)
        self.lapsed = lapsed


def get_con() -> duckdb.DuckDBPyConnection:
    """Cloud: read the shared MotherDuck DB the ETL writes to. Local dev with no
    token: fall back to the on-disk DuckDB file, read-only."""
    global _CON
    if _CON is not None:
        return _CON
    with _CON_LOCK:
        if _CON is not None:
            return _CON
        md = os.getenv("MOTHERDUCK_TOKEN")
        if md:
            dbname = os.getenv("MOTHERDUCK_DATABASE", "migration")
            _CON = duckdb.connect(f"md:{dbname}?motherduck_token={md}")
        else:
            if not DB_PATH.exists():
                raise DataSourceUnavailable(
                    "No MOTHERDUCK_TOKEN set and no local DuckDB file found."
                )
            _CON = duckdb.connect(str(DB_PATH), read_only=True)
        return _CON


def _reset_con() -> None:
    global _CON
    with _CON_LOCK:
        try:
            if _CON is not None:
                _CON.close()
        except Exception:
            pass
        _CON = None


def db_exec(sql: str, params=None, retries: int = 4):
    """Execute a query with retry on transient MotherDuck errors. Returns the
    executed relation, so callers keep their .fetchall()/.fetchdf()."""
    last: Exception | None = None
    for i in range(retries):
        try:
            cur = get_con().cursor()
            return cur.execute(sql, params) if params is not None else cur.execute(sql)
        except Exception as e:
            last = e
            msg = str(e).lower()
            if i == retries - 1 or not any(t in msg for t in _DB_TRANSIENT):
                raise
            _reset_con()   # force a fresh connection on the next attempt
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def health() -> None:
    """Force a real server round-trip. 'SELECT 1' is a constant DuckDB evaluates
    locally WITHOUT contacting MotherDuck, so it cannot detect the server being
    down — read a real table instead. Raises DataSourceUnavailable on failure."""
    try:
        db_exec("SELECT 1 FROM dim_calendars LIMIT 1").fetchall()
    except Exception as e:
        msg = str(e).lower()
        raise DataSourceUnavailable(str(e), lapsed=any(t in msg for t in _DB_LAPSED))


def last_refreshed():
    try:
        return db_exec("SELECT MAX(last_refreshed) FROM agg_daily_kpis").fetchone()[0]
    except Exception:
        return None
