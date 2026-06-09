"""
GA4 connector — Google Analytics Data API v1beta.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import find_dotenv, load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _resolve_sa_path(p: str) -> str:
    """Resolve a service-account JSON path. Handles both absolute paths and
    relative paths (resolved against the .env file's directory, not CWD)."""
    if not p:
        return p
    if os.path.isabs(p) and os.path.exists(p):
        return p
    if os.path.exists(p):
        return p
    env = find_dotenv()
    if env:
        candidate = os.path.normpath(os.path.join(os.path.dirname(env), p))
        if os.path.exists(candidate):
            return candidate
    return p  # Let the downstream call raise FileNotFoundError with the bad path


def _client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account

    sa_file = os.getenv("GA4_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        raise RuntimeError("GA4_SERVICE_ACCOUNT_FILE not set in .env")
    sa_file = _resolve_sa_path(sa_file)
    creds = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _property() -> str:
    pid = os.getenv("GA4_PROPERTY_ID")
    if not pid:
        raise RuntimeError("GA4_PROPERTY_ID not set in .env")
    return f"properties/{pid}"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fetch_daily_sessions(since: str, until: Optional[str] = None) -> list[dict]:
    """Daily metrics × source × medium × country × city.

    Powers fact_ga4_sessions. Composite key in the schema:
      {date}|{session_source}|{session_medium}|{country}|{city}
    """
    until = until or _today()
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    req = RunReportRequest(
        property=_property(),
        date_ranges=[DateRange(start_date=since, end_date=until)],
        dimensions=[
            Dimension(name="date"),
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
            Dimension(name="country"),
            Dimension(name="city"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="sessions"),
            Metric(name="newUsers"),
            Metric(name="screenPageViews"),
            Metric(name="keyEvents"),
            Metric(name="bounceRate"),
            Metric(name="averageSessionDuration"),
        ],
        limit=100000,
    )
    resp = _client().run_report(req)
    out = [{
        "date": r.dimension_values[0].value,
        "session_source": r.dimension_values[1].value or "(none)",
        "session_medium": r.dimension_values[2].value or "(none)",
        "country": r.dimension_values[3].value or "(none)",
        "city": r.dimension_values[4].value or "(none)",
        "active_users": int(r.metric_values[0].value),
        "sessions": int(r.metric_values[1].value),
        "new_users": int(r.metric_values[2].value),
        "page_views": int(r.metric_values[3].value),
        "key_events": int(r.metric_values[4].value),
        "bounce_rate": float(r.metric_values[5].value),
        "avg_session_duration": float(r.metric_values[6].value),
    } for r in resp.rows]
    logger.info("GA4 daily_sessions [%s..%s]: %d rows", since, until, len(out))
    return out


def fetch_top_pages(
    since: str, until: Optional[str] = None, limit: int = 500
) -> list[dict]:
    """Top pages by views, bucketed by date and country."""
    until = until or _today()
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    req = RunReportRequest(
        property=_property(),
        date_ranges=[DateRange(start_date=since, end_date=until)],
        dimensions=[
            Dimension(name="date"),
            Dimension(name="pagePath"),
            Dimension(name="country"),
        ],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="activeUsers"),
        ],
        limit=limit,
    )
    resp = _client().run_report(req)
    out = [{
        "date": r.dimension_values[0].value,
        "page_path": r.dimension_values[1].value,
        "country": r.dimension_values[2].value or "(none)",
        "page_views": int(r.metric_values[0].value),
        "active_users": int(r.metric_values[1].value),
    } for r in resp.rows]
    logger.info("GA4 top_pages [%s..%s]: %d rows", since, until, len(out))
    return out


def fetch_key_events(since: str, until: Optional[str] = None) -> list[dict]:
    """Daily key event counts by event name."""
    until = until or _today()
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    req = RunReportRequest(
        property=_property(),
        date_ranges=[DateRange(start_date=since, end_date=until)],
        dimensions=[
            Dimension(name="date"),
            Dimension(name="eventName"),
            Dimension(name="country"),
        ],
        metrics=[
            Metric(name="eventCount"),
            Metric(name="totalUsers"),
        ],
        limit=100000,
    )
    resp = _client().run_report(req)
    out = [{
        "date": r.dimension_values[0].value,
        "event_name": r.dimension_values[1].value,
        "country": r.dimension_values[2].value or "(none)",
        "event_count": int(r.metric_values[0].value),
        "total_users": int(r.metric_values[1].value),
    } for r in resp.rows]
    logger.info("GA4 key_events [%s..%s]: %d rows", since, until, len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("Sessions (April):", len(fetch_daily_sessions("2026-04-01", "2026-04-30")))
    print("Top pages (April):", len(fetch_top_pages("2026-04-01", "2026-04-30")))
    print("Key events (April):", len(fetch_key_events("2026-04-01", "2026-04-30")))
