"""
GSC (Google Search Console) connector.

One function: fetch_search_analytics(). Caller specifies the breakdown
dimension (query | page | country | device). Date dimension is always
included so we get a daily time-series.
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
    return p


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_file = os.getenv("GSC_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        raise RuntimeError("GSC_SERVICE_ACCOUNT_FILE not set in .env")
    sa_file = _resolve_sa_path(sa_file)
    creds = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _site() -> str:
    s = os.getenv("GSC_SITE_URL")
    if not s:
        raise RuntimeError("GSC_SITE_URL not set in .env")
    return s


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


VALID_DIMENSIONS = {"query", "page", "country", "device"}


def fetch_search_analytics(
    since: str,
    until: Optional[str] = None,
    dimension: str = "query",
    limit: int = 5000,
) -> list[dict]:
    """One report by `dimension`, broken down by date.
    `dimension` ∈ {query, page, country, device}.

    Composite key in fact_gsc_queries: {date}|{dimension}|{dimension_value}
    """
    if dimension not in VALID_DIMENSIONS:
        raise ValueError(
            f"dimension must be one of {VALID_DIMENSIONS}, got {dimension!r}")
    until = until or _today()
    body = {
        "startDate": since,
        "endDate": until,
        "dimensions": ["date", dimension],
        "rowLimit": limit,
    }
    resp = _service().searchanalytics().query(siteUrl=_site(), body=body).execute()
    out = []
    for r in resp.get("rows", []):
        keys = r.get("keys", [])
        out.append({
            "date": keys[0] if len(keys) > 0 else None,
            "dimension_name": dimension,
            "dimension_value": keys[1] if len(keys) > 1 else None,
            "clicks": int(r.get("clicks", 0)),
            "impressions": int(r.get("impressions", 0)),
            "ctr": float(r.get("ctr", 0)),
            "position": float(r.get("position", 0)),
        })
    logger.info("GSC %s [%s..%s]: %d rows", dimension, since, until, len(out))
    return out


def fetch_all_breakdowns(since: str, until: Optional[str] = None) -> dict[str, list[dict]]:
    """Convenience wrapper: pull all 4 breakdowns in one call."""
    return {
        "query": fetch_search_analytics(since, until, "query", limit=5000),
        "page": fetch_search_analytics(since, until, "page", limit=5000),
        "country": fetch_search_analytics(since, until, "country", limit=300),
        "device": fetch_search_analytics(since, until, "device", limit=10),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    breakdowns = fetch_all_breakdowns("2026-04-01", "2026-04-30")
    for k, rows in breakdowns.items():
        print(f"{k}: {len(rows)} rows")
